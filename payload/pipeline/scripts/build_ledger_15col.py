"""build_ledger_15col.py — 台账统一写入工具 v3（13 列，2026-10-14 改版；全部账号 accounts.json 驱动）

★ v3（2026-10-14，用户定稿，以台账磁盘现状为准）：列结构 15 列 → **13 列**
  删除「视频出单量」（旧 G）与「风格标签」（旧 O）；其后各列左移 1 位
  （旧 H–L 播放/赞/评/转/藏 → 新 G–K；旧 M 备注 → 新 L；旧 N 译文 → 新 M）。
  同批变更 D/E 口径：原 D=desc[:120]、E=desc 全量
                   现 D=正文（剔除 #话题标签）、E=仅话题标签
  常量与拆分逻辑统一走 scripts/desc_split.py（单一事实源），本文件不再自带表头。
  文件名保留 15col 以免打断既有调用（post_update.py / 自动化 prompt 均按名引用）。

用法:

用法:
  python scripts/build_ledger_15col.py                    # 写台账（对账式：新增+补空，幂等）
  python scripts/build_ledger_15col.py --accounts noah.hd57 vivi_ashop   # 只处理指定账号
  python scripts/build_ledger_15col.py export-trans       # 导出 N 列待译清单 pending_trans.json
  python scripts/build_ledger_15col.py apply-trans <json>  # 把 {acc: {row: 译文}} 写入 N 列
  python scripts/build_ledger_15col.py verify             # 仅 openpyxl 磁盘终验

v2 变更（2026-10-10，审查报告 P2/P4）：
  - ★ 账号清单从 accounts.json 读取（active 且非 ledger_skip），废弃硬编码 ORDER 6 账号
    —— 新账号接入零人肉记忆（noah.hd57 事故根因之一）。
  - ★ lang_map 从 accounts.json 的 lang 字段读取（废弃硬编码 lifewillcool=es）。
  - ★ F 列去重改按 **video_id 正则**（不再按 F 列原始字符串比对）——修复 v1 隐患：
    v1 新行写裸 url、add_account_sheet 写 `{url}（ID: {vid}）`，两种格式互认不出 →
    一旦混用会造重复行。现新行统一写规范格式 `{url}（ID: {vid}）`，旧格式行也能正确去重。
  - ★ 新增行复制所在 sheet 最后一行数据样式（列宽/行高/边框一致）；新 sheet 从
    模板「对标记录-gylwwcxb」复制（缺失时退回纯表头）。
  - ★ 写前自动备份台账。
  - 台账路径从 accounts.json 的 xlsx 字段读取（废弃硬编码）。

规则（§8 铁律）：
  - 单会话一次 load→写全部 sheet→save→重开验证；不碰「说明」页与 ledger_skip 账号
  - 新 sheet 表头与 13 列一致（desc_split.HEADER_13）
  - D=正文（无 #话题标签）、E=仅话题标签；零值格显式写 0；缺失 L 留空、M 由翻译流程补
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import argparse
from contextlib import contextmanager
import datetime as dt
import json
import re
import shutil
import sys
import tempfile
from pathlib import Path
import openpyxl
PIPE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
from desc_split import HEADER_13 as HEADER, COL_COUNT, COL_NOTE, COL_NOTE_ZH, COL_TITLE, COL_TAGS, split_desc
CFG_PATH = PIPE / 'accounts.json'
TS_RE = re.compile('^\\[(\\d{2}:\\d{2}:\\d{2},\\d{3})\\s*->\\s*(\\d{2}:\\d{2}:\\d{2},\\d{3})\\]\\s?(.*)$')
VID_RE = re.compile('/video/(\\d{15,25})')
TODAY = dt.date.today().isoformat()
STAT_KEYS = ['playCount', 'diggCount', 'commentCount', 'shareCount', 'collectCount']
IMG_HEADER = ['记录日期', '对标账号', '图文发布时间', '图文标题', '话题标签', '视频链接/ID', '商品链接', '播放量', '点赞数', '评论数', '转发数', '收藏数']
IMG_COL_COUNT = len(IMG_HEADER)
IMG_LINK_COL = 6
IMG_ACCOUNT_COL = 2
IMG_SHEET = '图文账号记录'

def is_image(acc_cfg: dict) -> bool:
    """账号是否图文类型（accounts.json: media_type == "image"）。"""
    return acc_cfg.get('media_type') == 'image'

def image_sheet_name(acc: str) -> str:
    """图文账号统一写共用表「图文账号记录」（与账号无关）。"""
    return IMG_SHEET

def img_account_val(acc: str) -> str:
    """共用表 B 列「对标账号」的取值（@ 前缀，与视频表一致）。"""
    return f'@{acc}'

def _img_account_of_row(row: list, link_val=None) -> str:
    """从共用表某数据行取账号名（B 列 @xxx → xxx）。"""
    v = row[IMG_ACCOUNT_COL - 1] if len(row) >= IMG_ACCOUNT_COL else None
    return str(v or '').lstrip('@').strip()

def load_cfg() -> dict:
    return json.loads(CFG_PATH.read_text(encoding='utf-8'))

def ledger_accounts(cfg: dict) -> list[dict]:
    """参与台账写入的账号 = active 且非 ledger_skip。
    注：gylwwcxb 虽是格式模板，但历史上经 add_account_sheet --append 日更，
    v2 统一后默认参与（不加 ledger_skip）；仅当账号确需冻结时才设该标记。"""
    return [a for a in cfg.get('accounts', []) if a.get('active', True) and (not a.get('ledger_skip', False))]

def account_lang(acc: str, cfg: dict) -> str:
    return next((a.get('lang') or 'en' for a in cfg.get('accounts', []) if a.get('account') == acc), 'en')

def account_out(acc: str, cfg: dict) -> Path:
    return Path(next((a.get('out_dir') or '' for a in cfg.get('accounts', []) if a.get('account') == acc), '') or PIPE / 'output')

def vid_of(cell) -> str | None:
    """从 F 列值提取 video_id（兼容裸 url / {url}（ID: {vid}）/ 纯数字三种格式）。"""
    if cell is None:
        return None
    s = str(cell)
    m = VID_RE.search(s) or re.search('(\\d{15,25})', s)
    return m.group(1) if m else None

def norm_int(v):
    if isinstance(v, bool) or v is None:
        return 0
    s = str(v).strip()
    return int(s) if s.isdigit() else 0

def read_transcript(out: Path, acc: str, vid: str):
    """返回 (text, nseg, has_audio)。text=无时间戳全文，nseg=分段数。"""
    p = out / f'{acc}_audio' / 'transcripts' / f'{acc}_{vid}_transcript.txt'
    if not p.exists():
        return ('', 0, False)
    lines, nseg, text = ([], 0, [])
    for ln in p.read_text(encoding='utf-8').splitlines():
        m = TS_RE.match(ln.strip())
        if m:
            nseg += 1
            text.append(m.group(3).strip())
        elif ln.strip():
            text.append(ln.strip())
    return (' '.join((t for t in text if t)), nseg, True)

def style_label(has_audio, nseg, chars):
    """【已停用 · 2026-10-14】「风格标签」列随 13 列改版一并移除，本函数不再被调用。
    保留定义仅为兼容可能的外部引用；如需恢复该列请先与业务确认。"""
    if not has_audio:
        return '未标注'
    if nseg == 0 or len(chars) < 30:
        return '（纯BGM，无口播）'
    return f'转写{nseg}句'

def load_items(acc: str, out: Path):
    p = out / f'scrape_{acc}.json'
    d = json.loads(p.read_text(encoding='utf-8'))
    items = list({str(i['id']): dict(i, id=str(i['id'])) for i in d.get('items', []) if i.get('id')}.values())
    items.sort(key=lambda x: x.get('publish_time_gmt8') or '')
    return items

def valid_header(ws) -> bool:
    header = [str(c.value or '').strip() for c in ws[1]]
    return header[:COL_COUNT] == HEADER and (not any(header[COL_COUNT:]))

def find_template_sheet(wb):
    return next((ws for ws in wb.worksheets if ws.title.startswith('对标记录-') and valid_header(ws)), None)

def backup_ledger(ledger: Path) -> None:
    bak = str(ledger) + f'.bak_{dt.datetime.now():%Y%m%d-%H%M%S-%f}'
    shutil.copy2(ledger, bak)
    print('备份 ->', bak)

def _valid_img_header(ws) -> bool:
    header = [str(c.value or '').strip() for c in ws[1]]
    return header[:IMG_COL_COUNT] == IMG_HEADER and (not any(header[IMG_COL_COUNT:]))

def _find_img_template(wb):
    """共用图文表存在则返回；否则 None（按 IMG_HEADER 新建）。"""
    return wb[IMG_SHEET] if IMG_SHEET in wb.sheetnames else None

def _write_image_sheet(wb, acc: str, out: Path) -> dict:
    """图文账号写入共用表 图文账号记录（12 列；B 列区分账号，只动本账号的行）。
    数据源 = load_items（scrape_{acc}.json，图文帖 /video/{id} 解析含 desc/stats/hashtags）。
    新增：追加整行；已有（本账号 B 列 + 同 video_id）：仅刷动态列（播放/点赞/评论/转发/收藏，列 8-12）。
    幂等且不误伤其他账号的行。"""
    sheet = image_sheet_name(acc)
    if sheet not in wb.sheetnames:
        ws = wb.create_sheet(sheet)
        ws.append(IMG_HEADER)
        ws.freeze_panes = 'C2'
    else:
        ws = wb[sheet]
    me = img_account_val(acc)
    own = {}
    for r in range(2, ws.max_row + 1):
        b = ws.cell(r, IMG_ACCOUNT_COL).value
        if str(b or '').lstrip('@').strip() == acc:
            v = vid_of(ws.cell(r, IMG_LINK_COL).value)
            if v:
                own[v] = r
    style_r = max(2, ws.max_row)
    added = updated = 0
    import copy as _copy
    for it in load_items(acc, out):
        vid = it['id']
        stats = it.get('stats') or {}
        url = it.get('url') or f'https://www.tiktok.com/@{acc}/video/{vid}'
        title, _ = split_desc(it.get('desc') or '')
        tags = ' '.join((h for h in it.get('hashtags') or [] if h))
        if not tags:
            _, tags = split_desc(it.get('desc') or '')
        if vid in own:
            r = own[vid]
            for ci, k in zip(range(8, 13), STAT_KEYS):
                ws.cell(r, ci).value = norm_int(stats.get(k, 0))
            updated += 1
            continue
        row_vals = [TODAY, me, it.get('publish_time_gmt8', ''), title, tags, f'{url}（ID: {vid}）', '', *[norm_int(stats.get(k, 0)) for k in STAT_KEYS]]
        assert len(row_vals) == IMG_COL_COUNT, f'图文列数不符: {len(row_vals)} != {IMG_COL_COUNT}'
        r = ws.max_row + 1
        for ci, v in enumerate(row_vals, 1):
            cell = ws.cell(r, ci)
            cell.value = v
            if style_r >= 2:
                s = ws.cell(style_r, ci)
                cell.font = _copy.copy(s.font)
                cell.fill = _copy.copy(s.fill)
                cell.border = _copy.copy(s.border)
                cell.alignment = _copy.copy(s.alignment)
                cell.number_format = s.number_format
        if not ws.freeze_panes:
            ws.freeze_panes = 'C2'
        own[vid] = r
        added += 1
    return {'rows': len(own), 'added': added, 'refilled': updated}

@contextmanager
def _writer_lock(paths):
    """同一目标的写进程互斥；进程退出后由系统释放锁，保留空闲锁文件。"""
    handles = []
    try:
        for path in sorted({Path(p).resolve() for p in paths}, key=str):
            handle = Path(str(path) + '.write.lock').open('a+b')
            try:
                if handle.tell() == 0:
                    handle.write(b'0')
                    handle.flush()
                handle.seek(0)
                if sys.platform == 'win32':
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as e:
                handle.close()
                raise BlockingIOError(f'台账已有写入任务：{path}') from e
            handles.append(handle)
        yield
    finally:
        for handle in reversed(handles):
            handle.close()

def _save_ledger(wb, path: Path) -> None:
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix='.ledger-', suffix='.tmp', delete=False) as f:
        temp = Path(f.name)
    try:
        wb.save(temp)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)

def main_write(scope: list[str] | None=None, cfg: dict | None=None, force_refill: bool=False) -> int:
    cfg = cfg or load_cfg()
    chosen = [a for a in ledger_accounts(cfg) if scope is None or a['account'] in scope]
    targets = [Path(cfg.get('img_xlsx' if is_image(a) else 'xlsx') or '') for a in chosen]
    try:
        with _writer_lock((p for p in targets if p.is_file())):
            return _main_write(scope, cfg, force_refill)
    except BlockingIOError as e:
        print(f'[error] {e}，未写入，请稍后重试')
        return 1

def _main_write(scope: list[str] | None=None, cfg: dict | None=None, force_refill: bool=False) -> int:
    cfg = cfg or load_cfg()
    ledgers = ledger_accounts(cfg)
    if scope is not None:
        unknown = [a for a in scope if a not in [x['account'] for x in ledgers]]
        if unknown:
            print(f'[error] 账号不在台账范围（检查 active/ledger_skip）: {unknown}')
            return 1
        ledgers = [x for x in ledgers if x['account'] in scope]
    if not ledgers:
        return 0
    need_video = any((not is_image(a) for a in ledgers))
    need_image = any((is_image(a) for a in ledgers))
    LEDGER = Path(cfg.get('xlsx') or '')
    if need_video and (not LEDGER.is_file()):
        print(f'[error] 视频台账不存在: {LEDGER}')
        return 1
    IMG_LEDGER = Path(cfg.get('img_xlsx') or '')
    if need_image and (not cfg.get('img_xlsx') or IMG_LEDGER.resolve() == LEDGER.resolve()):
        print('[error] img_xlsx 必须配置为独立于视频主簿的图文文件，未写入')
        return 1
    if need_image and (not IMG_LEDGER.is_file()):
        print(f'[error] 图文台账不存在: {IMG_LEDGER}')
        return 1
    wb = openpyxl.load_workbook(LEDGER) if need_video else None
    img_wb = openpyxl.load_workbook(IMG_LEDGER) if need_image else None
    for acc_cfg in ledgers:
        if is_image(acc_cfg):
            target = img_wb
            sheet = image_sheet_name(acc_cfg['account'])
            if sheet in target.sheetnames and (not _valid_img_header(target[sheet])):
                print(f'[error] {sheet}: 图文表头与 {IMG_COL_COUNT} 列规范不符，未写入')
                if wb is not None:
                    wb.close()
                if img_wb:
                    img_wb.close()
                return 1
        else:
            sheet = f"对标记录-{acc_cfg['account']}"
            if sheet in wb.sheetnames and (not valid_header(wb[sheet])):
                print(f'[error] {sheet}: 表头与现行 {COL_COUNT} 列规范不符，未写入')
                wb.close()
                if img_wb:
                    img_wb.close()
                return 1
    summary = {}
    for acc_cfg in ledgers:
        acc = acc_cfg['account']
        out = account_out(acc, cfg)
        scrape_p = out / f'scrape_{acc}.json'
        if not scrape_p.exists():
            print(f'[skip] {acc}: 无抓取产物 {scrape_p}，跳过入账')
            continue
        if is_image(acc_cfg):
            target = img_wb
            summary[acc] = _write_image_sheet(target, acc, out)
            continue
        sheet = f'对标记录-{acc}'
        if sheet not in wb.sheetnames:
            template = find_template_sheet(wb)
            if template is not None:
                ws = wb.copy_worksheet(template)
                ws.title = sheet
                if ws.max_row > 1:
                    ws.delete_rows(2, ws.max_row - 1)
                for c in ws[1]:
                    c.value = None
                for ci, h in enumerate(HEADER, 1):
                    ws.cell(1, ci).value = h
                ws.freeze_panes = template.freeze_panes or 'C2'
            else:
                ws = wb.create_sheet(sheet)
                ws.append(HEADER)
        else:
            ws = wb[sheet]
        f_map = {}
        for r in range(2, ws.max_row + 1):
            v = vid_of(ws.cell(r, 6).value)
            if v:
                f_map[v] = r
        style_r = max(2, ws.max_row)
        added = filled = 0
        for it in load_items(acc, out):
            vid = it['id']
            if vid in f_map:
                r = f_map[vid]
                text, nseg, has_audio = read_transcript(out, acc, vid)
                old = ws.cell(r, COL_NOTE).value
                if text and text != old and (force_refill or not str(old or '').strip()):
                    ws.cell(r, COL_NOTE).value = text
                    filled += 1
                title, _ = split_desc(it.get('desc') or '')
                if title and (not str(ws.cell(r, COL_TITLE).value or '').strip()):
                    ws.cell(r, COL_TITLE).value = title
                    filled += 1
                tags = ' '.join((h for h in it.get('hashtags') or [] if h))
                if not tags:
                    _, tags = split_desc(it.get('desc') or '')
                if tags and (not str(ws.cell(r, COL_TAGS).value or '').strip()):
                    ws.cell(r, COL_TAGS).value = tags
                    filled += 1
                continue
            url = it.get('url') or f'https://www.tiktok.com/@{acc}/video/{vid}'
            text, nseg, has_audio = read_transcript(out, acc, vid)
            stats = it.get('stats') or {}
            tags = ' '.join((h for h in it.get('hashtags') or [] if h))
            if not tags:
                _, tags = split_desc(it.get('desc') or '')
            title, _ = split_desc(it.get('desc') or '')
            row_vals = [TODAY, f'@{acc}', it.get('publish_time_gmt8', ''), title, tags, f'{url}（ID: {vid}）', None, *[norm_int(stats.get(k, 0)) for k in STAT_KEYS], text, None]
            assert len(row_vals) == COL_COUNT, f'列数不符: {len(row_vals)} != {COL_COUNT}'
            r = ws.max_row + 1
            import copy as _copy
            for ci, v in enumerate(row_vals, 1):
                cell = ws.cell(r, ci)
                cell.value = v if v is not None else ''
                s = ws.cell(style_r, ci)
                if style_r >= 2:
                    cell.font = _copy.copy(s.font)
                    cell.fill = _copy.copy(s.fill)
                    cell.border = _copy.copy(s.border)
                    cell.alignment = _copy.copy(s.alignment)
                    cell.number_format = s.number_format
            ws.row_dimensions[r].height = ws.row_dimensions[style_r].height or 150
            if not ws.freeze_panes:
                ws.freeze_panes = 'C2'
            f_map[vid] = r
            added += 1
        summary[acc] = {'rows': ws.max_row - 1, 'added': added, 'refilled': filled}
    if wb is not None and any((a['account'] in summary and (not is_image(a)) for a in ledgers)):
        backup_ledger(LEDGER)
        _save_ledger(wb, LEDGER)
    if img_wb is not None and any((a['account'] in summary and is_image(a) for a in ledgers)):
        backup_ledger(IMG_LEDGER)
        _save_ledger(img_wb, IMG_LEDGER)
    if wb is not None:
        wb.close()
    if img_wb is not None:
        img_wb.close()
    return _verify(LEDGER, IMG_LEDGER, [x['account'] for x in ledgers], summary, cfg)

def _verify(ledger: Path, img_ledger: Path | None=None, accounts: list[str] | None=None, summary: dict | None=None, cfg: dict | None=None) -> int:
    cfg = cfg or load_cfg()
    accounts = [x['account'] for x in ledger_accounts(cfg)] if accounts is None else accounts
    rows = {a['account']: a for a in cfg.get('accounts', [])}
    need_video = any((not is_image(rows.get(a, {})) for a in accounts))
    need_image = any((is_image(rows.get(a, {})) for a in accounts))
    img_ledger = Path(img_ledger or cfg.get('img_xlsx') or '')
    if need_image and (not cfg.get('img_xlsx') or img_ledger.resolve() == ledger.resolve()):
        print('[error] img_xlsx 必须为独立图文文件')
        return 1
    if need_video and (not ledger.is_file()) or (need_image and (not img_ledger.is_file())):
        print('[error] 当前账号范围的台账文件不存在')
        return 1
    wb2 = openpyxl.load_workbook(ledger, read_only=True) if need_video else None
    img_wb = openpyxl.load_workbook(img_ledger, read_only=True) if need_image else None
    print('=== 磁盘终验 ===')
    print('视频台账 sheets:', wb2.sheetnames if wb2 else [])
    print('图文台账 sheets:', img_wb.sheetnames if img_wb else [])
    bad = []
    for acc in accounts:
        acc_cfg = next((a for a in cfg.get('accounts', []) if a.get('account') == acc), {})
        scrape_p = account_out(acc, cfg) / f'scrape_{acc}.json'
        if not scrape_p.exists():
            print(f'  [skip] {acc}: 无抓取产物 {scrape_p}，跳过终验')
            continue
        expected = {i['id'] for i in load_items(acc, account_out(acc, cfg))}
        if is_image(acc_cfg):
            sn = image_sheet_name(acc)
            if sn not in img_wb.sheetnames:
                print(f'  {sn} (图文文件): ✗ 图文 sheet 缺失')
                bad.append(acc)
                continue
            ws = img_wb[sn]
            actual, n = ([], 0)
            for row in ws.iter_rows(min_row=2, values_only=True):
                b = _img_account_of_row(row)
                if b != acc:
                    continue
                n += 1
                v = vid_of(row[IMG_LINK_COL - 1]) if len(row) >= IMG_LINK_COL else None
                if v:
                    actual.append(v)
            missing = expected - set(actual)
            duplicates = len(actual) - len(set(actual))
            ok = _valid_img_header(ws) and (not missing) and (not duplicates)
            print(f"  {sn} [图文·{acc}]: 账号行={n} 预期>={len(expected)} 列={ws.max_column}(={IMG_COL_COUNT}) {('✓' if ok else '⚠')}")
            if not ok:
                print(f'    缺失={sorted(missing)} 重复ID={duplicates} 表头正确={_valid_img_header(ws)}')
                bad.append(acc)
            continue
        sn = f'对标记录-{acc}'
        if sn not in wb2.sheetnames:
            print(f'  {sn}: ✗ sheet 缺失')
            bad.append(acc)
            continue
        ws = wb2[sn]
        n = ws.max_row - 1
        actual = [vid_of(row[0]) for row in ws.iter_rows(min_row=2, min_col=6, max_col=6, values_only=True)]
        actual = [vid for vid in actual if vid]
        items_n = len(expected)
        missing = expected - set(actual)
        duplicates = len(actual) - len(set(actual))
        ok = valid_header(ws) and (not missing) and (not duplicates)
        print(f"  {sn}: 数据行={n} 预期>={items_n} 列={ws.max_column}(>={COL_COUNT}) {('✓' if ok else '⚠')}")
        if not ok:
            print(f'    缺失视频={sorted(missing)} 重复ID={duplicates} 表头正确={valid_header(ws)}')
            bad.append(acc)
    if wb2 is not None:
        wb2.close()
    if img_wb is not None:
        img_wb.close()
    print('终验:', 'FAIL ' + str(bad) if bad else 'PASS（sheet 数/名单/列数/行数 全对）')
    if summary:
        print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 1 if bad else 0

def main_export_trans(scope: list[str] | None=None, cfg: dict | None=None) -> int:
    cfg = cfg or load_cfg()
    ledgers = ledger_accounts(cfg)
    if scope is not None:
        ledgers = [x for x in ledgers if x['account'] in scope]
    ledgers = [a for a in ledgers if not is_image(a)]
    if not ledgers:
        print('无视频翻译范围，保留既有待译队列')
        return 0
    LEDGER = Path(cfg['xlsx'])
    wb = openpyxl.load_workbook(LEDGER, read_only=True)
    pending = {}
    for acc_cfg in ledgers:
        acc = acc_cfg['account']
        sn = f'对标记录-{acc}'
        if sn not in wb.sheetnames:
            continue
        ws = wb[sn]
        lang = acc_cfg.get('lang') or 'en'
        rows = list(ws.iter_rows(min_row=2, values_only=True))
        lst = []
        for i, row in enumerate(rows, start=2):
            m = (row[COL_NOTE - 1] or '').strip() if len(row) >= COL_NOTE else ''
            n = (row[COL_NOTE_ZH - 1] or '').strip() if len(row) >= COL_NOTE_ZH else ''
            if m and (not n):
                lst.append({'row': i, 'vid': vid_of(row[5] if len(row) > 5 else '') or '', 'text': m, 'lang': lang})
        if lst:
            pending[acc] = lst
    wb.close()
    p = account_out(ledgers[0]['account'], cfg) / 'pending_trans.json' if ledgers else PIPE / 'output' / 'pending_trans.json'
    p.write_text(json.dumps(pending, ensure_ascii=False, indent=1), encoding='utf-8')
    print(json.dumps({k: len(v) for k, v in pending.items()}, ensure_ascii=False))
    print(f'pending -> {p}')
    return 0

def main_apply_trans(path: str, expected: dict | None=None) -> int:
    cfg = load_cfg()
    try:
        with _writer_lock([Path(cfg['xlsx'])]):
            return _apply_trans(path, expected, cfg)
    except BlockingIOError as e:
        print(f'[error] {e}，未写入，请稍后重试')
        return 1

def _apply_trans(path: str, expected: dict | None, cfg: dict) -> int:
    LEDGER = Path(cfg['xlsx'])
    data = json.loads(Path(path).read_text(encoding='utf-8'))
    wb = openpyxl.load_workbook(LEDGER)
    try:
        if not isinstance(data, dict):
            raise ValueError('译文必须是账号到行号的对象')
        for acc, rows in data.items():
            sn = f'对标记录-{acc}'
            if sn not in wb.sheetnames or not isinstance(rows, dict):
                raise ValueError(f'无效账号或行对象: {acc}')
            ws = wb[sn]
            if not valid_header(ws):
                raise ValueError(f'{sn}: 表头与现行规范不符')
            for rk, trans in rows.items():
                if not str(rk).isdigit() or not 2 <= int(rk) <= ws.max_row:
                    raise ValueError(f'{acc}: 无效数据行 {rk}')
                r = int(rk)
                if not isinstance(trans, str) or not trans.strip():
                    raise ValueError(f'{acc} row{r}: 译文必须是非空字符串')
                if not vid_of(ws.cell(r, 6).value) or not ws.cell(r, COL_NOTE).value:
                    raise ValueError(f'{acc} row{r}: 缺少视频ID或转写原文')
                if expected is not None:
                    source = expected.get(acc, {}).get(str(r))
                    if not source or source.get('vid') != vid_of(ws.cell(r, 6).value) or source.get('text') != str(ws.cell(r, COL_NOTE).value or '').strip() or (source.get('translation', '') != str(ws.cell(r, COL_NOTE_ZH).value or '').strip()):
                        raise ValueError(f'{acc} row{r}: 翻译期间原文/视频/译文发生变化，未写入')
    except ValueError as e:
        wb.close()
        print(f'[error] {e}')
        return 1
    backup_ledger(LEDGER)
    total = 0
    for acc, rows in data.items():
        ws = wb[f'对标记录-{acc}']
        for r, trans in rows.items():
            ws.cell(int(r), COL_NOTE_ZH).value = trans
            ws.cell(int(r), COL_NOTE_ZH).data_type = 's'
            total += 1
    _save_ledger(wb, LEDGER)
    wb.close()
    wb2 = openpyxl.load_workbook(LEDGER, read_only=True)
    for acc, rows in data.items():
        ws = wb2[f'对标记录-{acc}']
        for r in rows:
            v = ws.cell(int(r), COL_NOTE_ZH).value
            assert v == rows[r], f'{acc} row{r} 译文落盘不一致!'
    wb2.close()
    print(f'apply-trans: 写入 {total} 条 M 列译文，重开终验通过')
    return 0
if __name__ == '__main__':
    ap = argparse.ArgumentParser(description='台账统一写入工具 v3（13 列，accounts.json 驱动）')
    ap.add_argument('cmd', nargs='?', default='write', choices=['write', 'export-trans', 'apply-trans', 'verify'])
    ap.add_argument('--accounts', nargs='*', default=None, help='只处理指定账号（write/export-trans 有效；缺省=全部 active 且非 ledger_skip）')
    ap.add_argument('trans_file', nargs='?', default=None, help='apply-trans 的 JSON 路径')
    ap.add_argument('--force-refill', action='store_true', help='write：已有行也强制用新转写文件覆盖 M 列（默认空才补；用于修复历史 lang 误转乱码）')
    ap.add_argument('--expected-source', default=None, help='apply-trans：自动翻译的原文快照，用于校验期间数据未变化')
    a = ap.parse_args()
    if a.cmd == 'write':
        sys.exit(main_write(a.accounts, force_refill=a.force_refill))
    elif a.cmd == 'export-trans':
        sys.exit(main_export_trans(a.accounts))
    elif a.cmd == 'apply-trans':
        if not a.trans_file:
            print('[error] apply-trans 需要JSON 路径参数')
            sys.exit(1)
        expected = json.loads(Path(a.expected_source).read_text(encoding='utf-8')) if a.expected_source else None
        sys.exit(main_apply_trans(a.trans_file, expected=expected))
    else:
        cfg = load_cfg()
        scope = a.accounts or [x['account'] for x in ledger_accounts(cfg)]
        sys.exit(_verify(Path(cfg['xlsx']), Path(cfg.get('img_xlsx') or cfg['xlsx']), scope))
