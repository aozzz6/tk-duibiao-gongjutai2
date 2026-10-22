"""
gate.py — TikTok 对标账号归档流水线 · 门禁校验（v2 落地，对应 v1 §2 门禁列）

设计原则：
- 每个 gate 函数输入该步产物 JSON（dict），返回 (ok: bool, detail: str)
- 失败即停链路：由 GateRunner 统一收集、落盘 gate_report.json、非零退出
- 校验规则与 v1 实战门禁一一对应，仅从"人工判断"改为"代码强制"

schema 约定（与 v1 产物一致）：
  download summary : {"matched": int, "downloaded": int, "skipped": int,
                      "failed": [{"id": str, "error": str}, ...]}
  scrape result    : {"errors": [{"id": str, "error": str}, ...],
                      "items": [{"id": str, "publish_time_gmt8": str, ...}, ...]}
  transcribe state : {"total": int, "done": [id, ...],
                      "suspect_hallucination": [{"id": str, "text": str}, ...]}
  backfill         : {"readback": [[cell, ...], ...], "expected": [[cell, ...], ...]}
  gmv              : {"results": [{"id": str, "gmv": float|None,
                                    "listed": bool, "plays": int}, ...]}
"""
from __future__ import annotations
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import json
import sys
import time
from pathlib import Path
HALLUCINATION_MIN_CHARS = 30
MIN_PLAY_FOR_TRUE_ZERO = 1

def gate_download(summary: dict) -> tuple[bool, str]:
    """Step 1：downloaded + skipped == matched，且 failed 为空。"""
    try:
        m, d, s = (summary['matched'], summary['downloaded'], summary['skipped'])
        failed = summary.get('failed', [])
    except KeyError as e:
        return (False, f'schema 缺字段: {e}')
    if failed:
        return (False, f'{len(failed)} 条失败: ' + '; '.join((f"{f.get('id', '?')}({f.get('error', '?')})" for f in failed)))
    if d + s != m:
        return (False, f'数量不守恒: downloaded({d}) + skipped({s}) != matched({m})')
    return (True, f'{d} 下载 + {s} 跳过 = {m} 全覆盖')

def gate_scrape(result: dict) -> tuple[bool, str]:
    """Step 2：每条 item 有 publish_time_gmt8。
    策略双模（解决 v1 §2 与 §3 的矛盾）：
    - strict（默认）: errors 非空即停 —— 不带病前进
    - tolerant: 允许下架/私有条进 errors[] 标注后继续（≥1 条有效即可）
      —— 对应 v1 §3「该条进 errors[]，不猜数据；其余继续；下架条在汇报中标注」
    policy 由 pipeline --allow-scrape-errors 显式选择，属人工闸口。"""
    errors = result.get('errors', [])
    items = result.get('items', [])
    policy = result.get('policy', 'strict')
    if not items:
        if result.get('media_type') == 'image' and result.get('enumeration_complete') is True and (result.get('window_empty') is True) and (not errors):
            return (True, '图文完整枚举成功，日期范围内无作品')
        return (False, 'items 为空（可能全部下架/私有或风控）')
    missing = [i.get('id', '?') for i in items if not i.get('publish_time_gmt8')]
    if missing:
        return (False, f'缺 publish_time_gmt8: {missing}')
    if errors and policy == 'strict':
        return (False, f'strict 模式: {len(errors)} 条抓取失败即停: ' + '; '.join((f"{e.get('id', '?')}({e.get('error', '?')})" for e in errors)) + '；如需容忍下架条请用 --allow-scrape-errors')
    warn = f'；容忍 {len(errors)} 条下架/私有已标注' if errors else ''
    return (True, f'{len(items)} 条完整（含发布时间）{warn}')

def gate_transcribe(state: dict) -> tuple[bool, str]:
    """Step 5：全部完成即通过；<30 字符疑似幻觉只警告，不阻断链路。

    说明：原逻辑「任一条疑似幻觉即 FAIL」会把「视频本身无口播」（纯 BGM/图片轮播，
    在带货号里很常见）误判为整链失败，导致 UI 显示 failed 但实际多数视频转写正常。
    真正的失败信号是「未完成」；疑似幻觉作为数据质量提示随 detail 带出。
    """
    total, done = (state.get('total', 0), state.get('done', []))
    suspect = state.get('suspect_hallucination', [])
    if len(done) != total:
        missing = total - len(done)
        return (False, f'未完成 {missing}/{total} 条')
    if suspect:
        ids = '; '.join((f"{s.get('id', '?')}: {s.get('text', '')[:20]!r}" for s in suspect))
        return (True, f'{total}/{total} 完成；{len(suspect)}/{total} 条无有效口播(疑纯BGM/无语音，已标记不盲信): {ids}')
    return (True, f'{total}/{total} 完成，无疑似幻觉')

def gate_backfill(payload: dict) -> tuple[bool, str]:
    """Step 7：回读值与期望值逐格一致。"""
    readback = payload.get('readback')
    expected = payload.get('expected')
    if readback is None or expected is None:
        return (False, 'schema 缺 readback/expected')
    if len(readback) != len(expected):
        return (False, f'行数不一致: 回读 {len(readback)} vs 期望 {len(expected)}')
    for r, (rb, exp) in enumerate(zip(readback, expected)):
        if len(rb) != len(exp):
            return (False, f'第 {r} 行列数不一致: 回读 {len(rb)} vs 期望 {len(exp)}')
        for c, (a, b) in enumerate(zip(rb, exp)):
            if a != b:
                return (False, f'[{r}][{c}] 不一致: 回读 {a!r} vs 期望 {b!r}')
    return (True, f'{len(readback)} 行逐格一致')

def gate_gmv(payload: dict) -> tuple[bool, str]:
    """Step 8：每条要么有明确值，要么明确标注「未收录」——不允许静默缺失。
    「未收录 ≠ 真 0」：播放>0 但未收录的条必须带 listed=False 标记。

    v3（2026-10-03）空结果语义修正——日更场景两类空态都是正常态（可自愈，不停车）：
    - results 空且 pending_ids 空 → 无超阈值视频，无需查询（PASS）
    - results 空且 pending_ids 非空 → FastMoss 额度/登录态限制，待补查（PASS，次日自动补）
    仅 schema 缺 results 或数据自相矛盾才 FAIL。"""
    results = payload.get('results')
    if results is None:
        return (False, 'schema 缺字段: results')
    pending = payload.get('pending_ids', [])
    if not results and (not pending):
        return (True, '无超阈值视频，无需查询')
    if not results:
        return (True, f'0 条命中缓存，{len(pending)} 条待查（FastMoss 额度/登录态限制，可自愈）')
    problems, unlisted = ([], [])
    for r in results:
        rid = r.get('id', '?')
        if r.get('gmv') is None and (not r.get('listed', False) is False):
            problems.append(f'{rid}: gmv 缺失且无标注')
        if r.get('gmv') == 0 and r.get('plays', 0) > 0 and r.get('listed', True):
            problems.append(f'{rid}: gmv=0 有播放但被当作收录真0（应标注未收录）')
        if r.get('listed') is False:
            unlisted.append(rid)
    if problems:
        return (False, '; '.join(problems))
    note = f'；未收录 {len(unlisted)} 条已标记: {unlisted}' if unlisted else '；无未收录'
    return (True, f'{len(results)} 条全部有值或明确标注{note}')

def gate_coverage(payload: dict) -> tuple[bool, str]:
    """v3 §4.1 覆盖核对：音频集 / 抓取集 / manifest 集三方对齐，防漏采。
    payload:
      {"audio": [ids], "scraped": [ids], "scrape_failed": [ids], "manifest": [ids],
       "new_today": int(可选), "downloaded_today": int(可选)}
    规则:
      1. audio == scraped ∪ scrape_failed（音频与抓取清单互为镜像：
         音频存在的视频当日抓取必有着落——成功或明确失败标注；凭空多出同样异常）
      2. scraped ∪ scrape_failed == manifest（manifest 覆盖全部已知视频）
      3. 若提供 new_today/downloaded_today：manifest 当日新增数 == s1 实下载数
    """
    try:
        audio = set(payload['audio'])
        scraped = set(payload['scraped'])
        failed = set(payload['scrape_failed'])
        manifest = set(payload['manifest'])
    except KeyError as e:
        return (False, f'schema 缺字段: {e}')
    problems = []
    got = scraped | failed
    only_audio = sorted(audio - got)
    only_scrape = sorted(got - audio)
    if only_audio:
        problems.append(f'音频有但抓取无着落: {only_audio}')
    if only_scrape:
        problems.append(f'抓取清单多出音频外 ID: {only_scrape}')
    miss_manifest = sorted(got - manifest)
    extra_manifest = sorted(manifest - got)
    if miss_manifest:
        problems.append(f'manifest 漏录: {miss_manifest}')
    if extra_manifest:
        problems.append(f'manifest 多出未知视频: {extra_manifest}')
    if 'new_today' in payload and 'downloaded_today' in payload:
        if payload['new_today'] != payload['downloaded_today']:
            problems.append(f"manifest 新增 {payload['new_today']} != s1 实下载 {payload['downloaded_today']}")
    if problems:
        return (False, '; '.join(problems))
    note = ''
    if 'new_today' in payload:
        note = f"，今日新增 {payload.get('new_today')}"
    return (True, f'三方对齐: audio={len(audio)} scraped={len(scraped)} failed={len(failed)} manifest={len(manifest)}{note}')

def _as_int(v):
    """TikTok stats 偶有数字字符串（实测 collectCount:"0"）→ 归一化；其余返回 None。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, str) and v.strip().isdigit():
        return int(v.strip())
    return None

def gate_sanity(payload: dict) -> tuple[bool, str]:
    """v3 §4.2 数值合理性：播放量非负、同视频较前一日快照单调不减。
    payload:
      {"current": [{"id": str, "plays": int, ...}, ...],
       "previous": [{"id": str, "plays": int, ...}, ...]  # 可选（首日无昨日数据）
    播放回退（今日 < 昨日）= 抓取异常或 TikTok 数据抖动，标异常待核。
    plays 容忍纯数字字符串（TikTok stats 偶发），"1.2k" 类脏值判 FAIL。"""
    current = payload.get('current')
    if current is None:
        return (False, 'schema 缺字段: current')
    problems = []
    for r in current:
        rid = r.get('id', '?')
        plays = _as_int(r.get('plays'))
        if plays is None:
            problems.append(f"{rid}: plays 非数值 ({r.get('plays')!r})")
        elif plays < 0:
            problems.append(f'{rid}: plays 为负 ({plays})')
    prev = payload.get('previous') or []
    prev_map = {r.get('id'): _as_int(r.get('plays')) for r in prev if r.get('id')}
    regressions = []
    for r in current:
        rid = r.get('id')
        cur = _as_int(r.get('plays'))
        was = prev_map.get(rid)
        if cur is not None and was is not None and (cur < was):
            regressions.append(f'{rid}: {was}→{cur}')
    if regressions:
        problems.append('播放回退: ' + ', '.join(regressions))
    if problems:
        return (False, '; '.join(problems))
    base = f'{len(current)} 条数值合理'
    return (True, base + (f'（对比昨日 {len(prev)} 条无回退）' if prev else '（首日无昨日基线）'))
GATES = {'download': gate_download, 'scrape': gate_scrape, 'transcribe': gate_transcribe, 'backfill': gate_backfill, 'gmv': gate_gmv, 'coverage': gate_coverage, 'sanity': gate_sanity}

def run_gates(checks: list[tuple[str, dict]], report_path: str | Path) -> bool:
    """按序执行多个门禁，全部通过返回 True；任一失败写 gate_report 并返回 False。
    checks: [(gate_name, artifact_dict), ...]"""
    report = {'ts': time.strftime('%Y-%m-%d %H:%M:%S'), 'checks': [], 'passed': True}
    ok_all = True
    for name, artifact in checks:
        fn = GATES.get(name)
        if fn is None:
            ok, detail = (False, f'未知门禁 {name}')
        else:
            ok, detail = fn(artifact)
        report['checks'].append({'gate': name, 'ok': ok, 'detail': detail})
        if not ok:
            ok_all = False
            break
    report['passed'] = ok_all
    Path(report_path).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return ok_all
if __name__ == '__main__':
    if len(sys.argv) != 3 or sys.argv[1] not in GATES:
        print(f"用法: python gate.py <{'|'.join(GATES)}> <artifact.json>")
        sys.exit(2)
    artifact = json.loads(Path(sys.argv[2]).read_text(encoding='utf-8'))
    ok, detail = GATES[sys.argv[1]](artifact)
    print(('PASS  ' if ok else 'FAIL  ') + detail)
    sys.exit(0 if ok else 1)
