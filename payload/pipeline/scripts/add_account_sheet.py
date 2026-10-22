import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import argparse
import datetime as dt
import json
import re
import shutil
import sys
from pathlib import Path
PIPE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PIPE))
import openpyxl
sys.path.insert(0, str(Path(__file__).resolve().parent))
from desc_split import HEADER_13 as HEADER_EXPECT, COL_COUNT, COL_LINK, split_desc
from build_ledger_15col import find_template_sheet
TS_RE = re.compile('^\\[(\\d{2}:\\d{2}:\\d{2},\\d{3})\\s*->\\s*(\\d{2}:\\d{2}:\\d{2},\\d{3})\\]\\s*(.*)$')

def norm_int(v) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0

def load_cfg() -> dict:
    return json.loads((PIPE / 'accounts.json').read_text(encoding='utf-8'))

def resolve_out_dir(acc_cfg: dict, cfg: dict) -> Path:
    d = acc_cfg.get('out_dir') or cfg.get('output_root') or PIPE / 'output'
    return Path(d)

def read_transcript(out: Path, acc: str, vid: str) -> tuple[str, int, bool]:
    """返回 (去时间戳全文, 分段数, 是否有音频/转写文件)。

    has_audio **按转写文件是否存在**判定（与权威实现 build_ledger_15col.py 一致）。
    不能用 `bool(nseg) or bool(text)`——无口播视频转写结果为空，那样会被误判成
    「没有音频」而标成 `未标注`，实际应标 `（纯BGM，无口播）`。
    """
    p = out / f'{acc}_audio' / 'transcripts' / f'{acc}_{vid}_transcript.txt'
    if not p.exists():
        return ('', 0, False)
    text, nseg = ([], 0)
    for ln in p.read_text(encoding='utf-8').splitlines():
        m = TS_RE.match(ln.strip())
        if m:
            nseg += 1
            text.append(m.group(3).strip())
        elif ln.strip():
            text.append(ln.strip())
    return (' '.join((t for t in text if t)), nseg, True)

def style_label(has_audio: bool, nseg: int, chars: str) -> str:
    """【已停用 · 2026-10-14】「风格标签」列随 13 列改版移除，不再被调用。
    保留定义仅为兼容可能的外部引用。"""
    if not has_audio:
        return '未标注'
    if nseg == 0 or len(chars) < 30:
        return '（纯BGM，无口播）'
    return f'转写{nseg}句'

def build_rows(acc: str, out: Path, today: str, trans_map: dict, links_map: dict | None=None, bio_link: str='') -> list[list]:
    scrape_p = out / f'scrape_{acc}.json'
    if not scrape_p.exists():
        raise SystemExit(f'缺抓取产物：{scrape_p}（请先跑 s2）')
    scrape = json.loads(scrape_p.read_text(encoding='utf-8'))
    items = {i['id']: i for i in scrape.get('items', []) if i.get('id')}
    if not items:
        raise SystemExit('抓取产物 items 为空，中止')
    rows = []
    for vid in sorted(items, key=lambda v: items[v].get('publish_time_gmt8') or '', reverse=True):
        it = items[vid]
        text, nseg, has_audio = read_transcript(out, acc, vid)
        stats = it.get('stats') or {}
        url = it.get('url') or f'https://www.tiktok.com/@{acc}/video/{vid}'
        desc = it.get('desc') or ''
        title, tags = split_desc(desc)
        rows.append([today, f'@{acc}', it.get('publish_time_gmt8', ''), title, tags, f'{url}（ID: {vid}）', (links_map or {}).get(vid) or bio_link or '', norm_int(stats.get('playCount')), norm_int(stats.get('diggCount')), norm_int(stats.get('commentCount')), norm_int(stats.get('shareCount')), norm_int(stats.get('collectCount')), text, trans_map.get(vid, '') or None])
        assert len(rows[-1]) == COL_COUNT, f'列数不符: {len(rows[-1])} != {COL_COUNT}'
        print(f"  · {vid}  publish={it.get('publish_time_gmt8')}  句数={nseg}  L={len(text)}字  M={len(trans_map.get(vid, ''))}字")
    return rows

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--account', required=True)
    ap.add_argument('--template', default=None, help='格式模板 sheet（默认自动选择当前主簿中的规范账号表）')
    ap.add_argument('--translations', default='', help='可选 JSON：{video_id: 中文翻译}')
    ap.add_argument('--today', default=dt.date.today().isoformat(), help='A 列记录日期，默认今天')
    ap.add_argument('--dry-run', action='store_true', help='只预览不写盘')
    ap.add_argument('--append', action='store_true', help='向已存在的 sheet 追加行（sheet 已有历史数据、需补新视频时用）；按 F 列 video_id 去重，幂等')
    a = ap.parse_args()
    cfg = load_cfg()
    acc_cfg = next((x for x in cfg.get('accounts', []) if x.get('account') == a.account), None)
    if acc_cfg is None:
        raise SystemExit(f'账号不在 accounts.json：{a.account}')
    xlsx = Path(cfg.get('xlsx') or '')
    if not xlsx.exists():
        raise SystemExit(f'台账不存在：{xlsx}')
    out = resolve_out_dir(acc_cfg, cfg)
    sheet_name = f'对标记录-{a.account}'
    trans_map = {}
    if a.translations:
        tp = Path(a.translations)
        if not tp.exists():
            raise SystemExit(f'翻译文件不存在：{tp}')
        trans_map = json.loads(tp.read_text(encoding='utf-8'))
    print(f'账号 = {a.account}\n产物目录 = {out}\n台账 = {xlsx}')
    links_map, bio_link = ({}, '')
    pl = Path(os.environ.__getitem__('WB_LEDGER_DIR')) / a.account / f'{a.account}_products.json'
    if pl.exists():
        try:
            pdata = json.loads(pl.read_text(encoding='utf-8'))
            for vid, prods in pdata.items() if isinstance(pdata, dict) else []:
                if isinstance(prods, list) and prods:
                    pid = str((prods[0] or {}).get('product_id') or '')
                    if pid:
                        links_map[vid] = f'https://shop.tiktok.com/view/product/{pid}'
        except Exception:
            pass
    plf = Path(os.environ.__getitem__('WB_LEDGER_DIR')) / a.account / f'{a.account}_product_links.json'
    if plf.exists():
        try:
            ldata = json.loads(plf.read_text(encoding='utf-8'))
            bio_link = str((ldata or {}).get('bio_link') or '')
            for vid, link in ((ldata or {}).get('videos') or {}).items():
                if link and vid not in links_map:
                    links_map[vid] = str(link)
        except Exception:
            pass
    rows = build_rows(a.account, out, a.today, trans_map, links_map, bio_link)
    print(f'待写入 = {len(rows)} 行')
    if a.dry_run:
        print('\n[dry-run] 未写盘。首行预览：')
        for i, v in enumerate(rows[0], 1):
            print(f'  c{i}: {str(v)[:80]}')
        return 0
    wb = openpyxl.load_workbook(xlsx)
    if a.append:
        if sheet_name not in wb.sheetnames:
            raise SystemExit(f'{sheet_name} 不存在；--append 要求 sheet 已存在')
    else:
        if sheet_name in wb.sheetnames:
            raise SystemExit(f'{sheet_name} 已存在，中止（不覆盖）')
        if a.template is None:
            template = find_template_sheet(wb)
            if template is None:
                raise SystemExit('未找到规范的账号表作为格式模板，请显式指定 --template')
            a.template = template.title
        if a.template not in wb.sheetnames:
            raise SystemExit(f'模板 sheet 不存在：{a.template}')

    def _vid_of(s):
        m = re.search('/video/(\\d{15,25})', str(s)) or re.search('(\\d{15,25})', str(s))
        return m.group(1) if m else None
    if a.append:
        ws_probe = wb[sheet_name]
        header = next(ws_probe.iter_rows(min_row=1, max_row=1, values_only=True), ())
        fidx = next((i for i, h in enumerate(header) if h and '视频链接' in str(h)), 5)
        have = set()
        for row in ws_probe.iter_rows(min_row=2, max_col=fidx + 1, values_only=True):
            if fidx < len(row) and row[fidx]:
                v = _vid_of(row[fidx])
                if v:
                    have.add(v)
        fresh = [r for r in rows if _vid_of(r[5]) not in have]
        print(f'去重：待追加 {len(fresh)} 行（跳过已存在 {len(rows) - len(fresh)} 行）')
        if not fresh:
            print('无新增行，无需写盘')
            return 0
    bak = str(xlsx) + f'.bak_{dt.datetime.now():%Y%m%d-%H%M%S}'
    shutil.copy2(xlsx, bak)
    print('备份 ->', bak)
    if a.append:
        import copy as _copy
        ws = wb[sheet_name]
        style_r = max(2, ws.max_row)
        start = ws.max_row + 1
        for i, vals in enumerate(fresh):
            for c, v in enumerate(vals, 1):
                cell = ws.cell(start + i, c)
                cell.value = v
                s = ws.cell(style_r, c)
                cell.font = _copy.copy(s.font)
                cell.fill = _copy.copy(s.fill)
                cell.border = _copy.copy(s.border)
                cell.alignment = _copy.copy(s.alignment)
                cell.number_format = s.number_format
            ws.row_dimensions[start + i].height = ws.row_dimensions[style_r].height or 150
        if not ws.freeze_panes:
            ws.freeze_panes = 'C2'
        wb.save(xlsx)
    else:
        ws = wb.copy_worksheet(wb[a.template])
        ws.title = sheet_name
        for i, vals in enumerate(rows):
            for c, v in enumerate(vals, 1):
                ws.cell(2 + i, c).value = v
        keep = 1 + len(rows)
        if ws.max_row > keep:
            ws.delete_rows(keep + 1, ws.max_row - keep)
        ws.freeze_panes = wb[a.template].freeze_panes
        wb.save(xlsx)
    wb2 = openpyxl.load_workbook(xlsx)
    ws2 = wb2[sheet_name]
    hdr = [ws2.cell(1, c).value for c in range(1, COL_COUNT + 1)]
    ok_hdr = hdr == HEADER_EXPECT
    print(f'\n复读: rows={ws2.max_row} cols={ws2.max_column} freeze={ws2.freeze_panes} 表头一致={ok_hdr}')
    if not ok_hdr:
        raise SystemExit('表头校验失败！')
    print(f'完成：{sheet_name} 已写入 {len(rows)} 行')
    return 0
if __name__ == '__main__':
    sys.exit(main())
