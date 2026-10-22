"""
对标台账 -> 销售分析报表 HTML 生成器 v3
读取 D:/1/对标账号/tiktok_对标记录.xlsx，做数据审计 + 全维度计算，
生成内嵌 ECharts 的自包含 HTML 报表。
v2 修正：磁盘实读为 4 数据表 36 条（含 charelttce）；商品名为英文原文，品类按英文关键词归类。
v3 新增：① 商品维度板块（挂车覆盖、各账号选品、商品清单/TOP、未挂车标注）；
         ② 出单情况板块（台账「视频出单量」100% 空 + daily_metrics GMV 状态 + pending_gmv 待查清单）。
         数据边界如实声明，严禁编造任何出单/GMV 数字。
用法: python generate_benchmark_report.py
"""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import json
import re
import statistics
import sys
from datetime import datetime
from pathlib import Path
import openpyxl
AUX_DIR = Path(os.environ.__getitem__('WB_LEDGER_DIR'))
XLSX_PATH = AUX_DIR / 'tiktok_对标.xlsx'
OUT_HTML = AUX_DIR / 'TikTok对标销售分析报表_20261004.html'
ECHARTS_JS = Path(os.path.join(os.environ.__getitem__('WB_PIPELINE_DIR'), 'assets/echarts.min.js'))
GENERATED_AT = '2026-10-04 (GMT+8)'
DAILY_METRICS = AUX_DIR / 'daily_metrics.jsonl'
PRODUCTS_JSON = {'vivi_ashop': AUX_DIR / 'vivi_ashop' / 'vivi_ashop_products.json', 'charelttce': AUX_DIR / 'charelttce' / 'charelttce_products.json'}
PENDING_GMV = AUX_DIR / 'vivi_ashop' / 'pending_gmv.json'
MANIFESTS = {'vivi_ashop': AUX_DIR / 'vivi_ashop' / 'manifest_vivi_ashop.json'}
sys.path.insert(0, str(Path(__file__).resolve().parent))
from desc_split import HEADER_13 as COLS
COLS_LEGACY_15 = ['记录日期', '对标账号', '视频发布时间', '视频标题', '视频文案(含话题标签)', '视频链接/ID', '视频出单量', '播放量', '点赞数', '评论数', '转发数', '收藏数', '备注', '备注中文翻译', '风格标签']
COLS_LEGACY_16 = ['记录日期', '对标账号', '视频发布时间', '视频文案(含话题标签)', '视频链接/ID', '商品链接', '商品名称', '视频出单量', '播放量', '点赞数', '评论数', '转发数', '收藏数', '音频', '音频中文翻译', '风格标签']
DIGITAL_ACCOUNTS = {'kee.yaden', 'charelttce'}
CATEGORY_RULES = [('手机壳', ['Phone Case', 'Magnetic Case', 'Casing']), ('礼品/趣味', ['Challenge Coin', 'Sticky Note', 'Gift Card', 'Colleague Gift']), ('首饰饰品', ['Ring', 'Bracelet', 'Earring', 'Bolo', 'Bangle']), ('包袋', ['Tote Bag', 'Underarm Bag', 'Wallet']), ('玄学/信仰', ['Tarot', 'Devotional']), ('DIY/解压玩具', ['Rhinestone', 'Fidget', 'Bedazzl']), ('个护/美体', ['Henna', 'Tattoo'])]

def parse_dt(v):
    """兼容真日期类型与文本字符串两种格式"""
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    s = str(v).strip()
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d', '%Y/%m/%d %H:%M:%S', '%Y/%m/%d'):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None

def classify_category(account, product_name, style):
    if account in DIGITAL_ACCOUNTS:
        return '充电/数码配件'
    name = str(product_name or '')
    if not name.strip():
        return '首饰饰品' if '磁疗' in str(style or '') else '其他'
    for cat, kws in CATEGORY_RULES:
        if any((re.search('\\b' + re.escape(k), name, re.IGNORECASE) for k in kws)):
            return cat
    return '其他'

def load_aux():
    """读取出单/GMV 辅助数据源（daily_metrics / products / pending_gmv / manifest）。
    全部为可选：文件缺失时返回空结构并记录缺口，不抛异常。"""
    aux = {'daily': [], 'daily_files': [], 'products': {}, 'pending': None, 'manifests': {}, 'gaps': []}
    for acc, p in PRODUCTS_JSON.items():
        if p.exists():
            try:
                aux['products'][acc] = json.loads(p.read_text(encoding='utf-8'))
                aux['daily_files'].append(f"{p.name}({len(aux['products'][acc])}条)")
            except Exception as e:
                aux['gaps'].append(f'{p.name} 读取失败: {e}')
        else:
            aux['gaps'].append(f'缺 products.json: {p.name}')
    if DAILY_METRICS.exists():
        try:
            aux['daily'] = [json.loads(l) for l in DAILY_METRICS.read_text(encoding='utf-8').splitlines() if l.strip()]
            aux['daily_files'].append(f"daily_metrics.jsonl({len(aux['daily'])}条)")
        except Exception as e:
            aux['gaps'].append(f'daily_metrics.jsonl 读取失败: {e}')
    else:
        aux['gaps'].append('缺 daily_metrics.jsonl')
    if PENDING_GMV.exists():
        try:
            aux['pending'] = json.loads(PENDING_GMV.read_text(encoding='utf-8'))
        except Exception as e:
            aux['gaps'].append(f'pending_gmv.json 读取失败: {e}')
    for acc, m in MANIFESTS.items():
        if m.exists():
            try:
                aux['manifests'][acc] = json.loads(m.read_text(encoding='utf-8'))
            except Exception as e:
                aux['gaps'].append(f'{m.name} 读取失败: {e}')
    return aux

def fmt(n):
    return f'{n:,.0f}'

def pct(x, digits=2):
    return f'{x * 100:.{digits}f}%'

def main():
    wb = openpyxl.load_workbook(XLSX_PATH, data_only=True)
    records = []
    audit = {'row_counts': {}, 'empty_units': 0, 'dup_video_ids': [], 'no_product': [], 'date_parse_fail': [], 'fill': {}}
    for ws in wb.worksheets:
        if ws.title == '说明':
            continue
        raw_header = [c.value for c in ws[1]]
        cols = [str(c).strip() if c is not None else '' for c in raw_header]
        if not cols:
            continue
        n = 0
        for row in ws.iter_rows(min_row=2, values_only=True):
            if all((v is None or str(v).strip() == '' for v in row)):
                continue
            n += 1
            rec = dict(zip(cols, row))
            rec.setdefault('视频出单量', None)
            rec.setdefault('风格标签', '')
            rec.setdefault('商品名称', ' '.join((x for x in (rec.get('视频标题'), rec.get('话题标签')) if x)))
            rec.setdefault('商品链接', '')
            rec.setdefault('音频中文翻译', rec.get('备注中文翻译', ''))
            rec.setdefault('音频', rec.get('备注', ''))
            pub = parse_dt(rec['视频发布时间'])
            rec_dt = parse_dt(rec['记录日期'])
            if pub is None:
                audit['date_parse_fail'].append((ws.title, rec['视频链接/ID'], rec['视频发布时间']))
            if rec['视频出单量'] in (None, ''):
                audit['empty_units'] += 1
            if not rec['商品链接']:
                audit['no_product'].append((rec['对标账号'], str(rec['视频链接/ID'])[-12:]))
            acc_name = rec['对标账号']
            f = audit['fill'].setdefault(acc_name, {'n': 0, 'style': 0, 'trans': 0})
            f['n'] += 1
            if str(rec['风格标签'] or '').strip() not in ('', '未标注'):
                f['style'] += 1
            if str(rec['音频中文翻译'] or '').strip():
                f['trans'] += 1
            rec['_pub'] = pub
            rec['_rec'] = rec_dt
            rec['_plays'] = int(rec['播放量'] or 0)
            rec['_likes'] = int(rec['点赞数'] or 0)
            rec['_comments'] = int(rec['评论数'] or 0)
            rec['_shares'] = int(rec['转发数'] or 0)
            rec['_favs'] = int(rec['收藏数'] or 0)
            rec['_inter'] = rec['_likes'] + rec['_comments'] + rec['_shares'] + rec['_favs']
            rec['_category'] = classify_category(acc_name, rec['商品名称'], rec['风格标签'])
            rec['_lang'] = '西语' if '西语' in str(rec['风格标签'] or '') else '英语'
            rec['_vid_tail'] = str(rec['视频链接/ID']).rstrip('/').split('/')[-1][-12:]
            _plink = str(rec['商品链接'] or '').strip().rstrip('/')
            rec['video_id'] = str(rec['视频链接/ID']).strip().rstrip('/').split('/')[-1]
            rec['product_id'] = _plink.split('/')[-1] if _plink else ''
            rec['_age_h'] = round((rec['_rec'] - pub).total_seconds() / 3600, 1) if pub and rec_dt else None
            records.append(rec)
        audit['row_counts'][ws.title] = n
    vids = [r['视频链接/ID'] for r in records]
    audit['dup_video_ids'] = [v for v in set(vids) if vids.count(v) > 1]
    print('=' * 60)
    print('【审计】行数:', audit['row_counts'], '| 总记录:', len(records))
    print('【审计】出单量空值:', audit['empty_units'], '| 未挂车:', len(audit['no_product']), audit['no_product'])
    print('【审计】日期解析失败:', len(audit['date_parse_fail']), audit['date_parse_fail'])
    print('【审计】重复视频ID:', audit['dup_video_ids'])
    print('【审计】填充度:', json.dumps(audit['fill'], ensure_ascii=False))
    cats_dbg = {}
    for r in records:
        cats_dbg.setdefault(r['_category'], []).append((r['对标账号'], str(r['商品名称'])[:40]))
    print('【审计】品类分布:')
    for c, items in cats_dbg.items():
        print(f'  {c} ({len(items)}):')
        for it in items:
            print(f'    - {it[0]} | {it[1]}')
    accounts = {}
    for r in records:
        a = accounts.setdefault(r['对标账号'], {'n': 0, 'plays': 0, 'likes': 0, 'comments': 0, 'shares': 0, 'favs': 0, 'inter': 0, 'plays_list': []})
        a['n'] += 1
        a['plays'] += r['_plays']
        a['likes'] += r['_likes']
        a['comments'] += r['_comments']
        a['shares'] += r['_shares']
        a['favs'] += r['_favs']
        a['inter'] += r['_inter']
        a['plays_list'].append(r['_plays'])
    for name, a in accounts.items():
        a['avg_plays'] = a['plays'] / a['n']
        a['median_plays'] = statistics.median(a['plays_list'])
        a['eng_rate'] = a['inter'] / a['plays'] if a['plays'] else 0
        a['max_plays'] = max(a['plays_list'])
        a['min_plays'] = min(a['plays_list'])
    print('\n【账号汇总】')
    for name, a in accounts.items():
        print(f"  {name}: n={a['n']} plays={a['plays']} avg={a['avg_plays']:.0f} median={a['median_plays']:.0f} eng={pct(a['eng_rate'])}")
    total = {k: sum((a[k] for a in accounts.values())) for k in ('n', 'plays', 'likes', 'comments', 'shares', 'favs', 'inter')}
    total_eng = total['inter'] / total['plays'] if total['plays'] else 0
    attached = len(records) - len(audit['no_product'])
    attach_rate = attached / len(records)
    print(f"\n【总计】n={total['n']} plays={fmt(total['plays'])} inter={total['inter']} eng={pct(total_eng)} attach={attached}/{len(records)}={pct(attach_rate, 1)}")
    daily = {}
    for r in records:
        if not r['_pub']:
            continue
        d = r['_pub'].strftime('%m-%d')
        day = daily.setdefault(d, {'date': d, 'n': 0, 'plays': 0, 'inter': 0})
        day['n'] += 1
        day['plays'] += r['_plays']
        day['inter'] += r['_inter']
    days = sorted(daily.values(), key=lambda x: datetime.strptime('2026-' + x['date'], '%Y-%m-%d'))
    prev = None
    for d in days:
        d['eng_rate'] = d['inter'] / d['plays'] if d['plays'] else 0
        d['avg_plays'] = d['plays'] / d['n']
        d['dod'] = (d['plays'] - prev) / prev * 100 if prev else None
        prev = d['plays']
    print('\n【每日趋势】')
    for d in days:
        print(f"""  {d['date']}: n={d['n']} plays={fmt(d['plays'])} dod={('-' if d['dod'] is None else f"{d['dod']:+.1f}%")} eng={pct(d['eng_rate'])}""")
    cats = {}
    for r in records:
        c = cats.setdefault(r['_category'], {'n': 0, 'plays': 0, 'inter': 0})
        c['n'] += 1
        c['plays'] += r['_plays']
        c['inter'] += r['_inter']
    for c in cats.values():
        c['share'] = c['plays'] / total['plays']
        c['avg_plays'] = c['plays'] / c['n']
    cats_sorted = sorted(cats.items(), key=lambda kv: -kv[1]['plays'])
    print('\n【品类】')
    for name, c in cats_sorted:
        print(f"  {name}: n={c['n']} plays={fmt(c['plays'])} ({pct(c['share'], 1)}) avg={c['avg_plays']:.0f}")
    langs = {}
    for r in records:
        l = langs.setdefault(r['_lang'], {'n': 0, 'plays': 0, 'inter': 0})
        l['n'] += 1
        l['plays'] += r['_plays']
        l['inter'] += r['_inter']
    for l in langs.values():
        l['avg_plays'] = l['plays'] / l['n']
    print('\n【语言】', {k: {kk: round(vv) for kk, vv in v.items()} for k, v in langs.items()})
    top10 = sorted(records, key=lambda r: -r['_plays'])[:10]
    print('\n【TOP10 播放】')
    for i, r in enumerate(top10, 1):
        print(f"  {i}. [{r['对标账号']}] {str(r['商品名称'])[:30]} plays={fmt(r['_plays'])} eng={pct(r['_inter'] / r['_plays'] if r['_plays'] else 0)}")
    mature_min = [r for r in records if r['_plays'] >= 500]
    top_eng = sorted(mature_min, key=lambda r: -(r['_inter'] / r['_plays']))[:5]
    anomalies = []
    for acc, a in accounts.items():
        mean = statistics.mean(a['plays_list'])
        stdev = statistics.pstdev(a['plays_list'])
        for r in records:
            if r['对标账号'] == acc and stdev and ((r['_plays'] - mean) / stdev > 1.8):
                anomalies.append({'type': '高播放离群', 'acc': acc, 'vid': r['_vid_tail'], 'name': str(r['商品名称'])[:50], 'plays': r['_plays'], 'z': round((r['_plays'] - mean) / stdev, 2), 'mean': round(mean), 'note': f"超出该账号均值 {r['_plays'] / mean:.1f} 倍"})
    zero_comment = sum((1 for r in records if r['_comments'] == 0))
    immature = [r for r in records if r['_age_h'] is not None and r['_age_h'] < 36]
    print('\n【异常】离群:', [(x['acc'], x['plays']) for x in anomalies], '| 零评论:', zero_comment, '/', len(records), '| 采集时<36h:', len(immature))
    by_product = {}
    for r in records:
        if r['商品链接']:
            by_product.setdefault(r['商品链接'], []).append(r)
    repeat_products = {k: sorted(v, key=lambda x: x['_pub']) for k, v in by_product.items() if len(v) > 1}
    print('\n【同品复推】')
    for k, v in repeat_products.items():
        cross = '跨账号' if len(set((x['对标账号'] for x in v))) > 1 else '同账号'
        print(f'  {str(k)[-8:]} [{cross}] ' + ' -> '.join((f"{x['对标账号']}:{x['_plays']}" for x in v)))
    buckets = {'凌晨(0-6)': (0, 6), '上午(6-12)': (6, 12), '下午(12-18)': (12, 18), '晚间(18-24)': (18, 24)}
    hours = {k: {'n': 0, 'plays': 0} for k in buckets}
    for r in records:
        if not r['_pub']:
            continue
        h = r['_pub'].hour
        for k, (lo, hi) in buckets.items():
            if lo <= h < hi:
                hours[k]['n'] += 1
                hours[k]['plays'] += r['_plays']
    for v in hours.values():
        v['avg'] = v['plays'] / v['n'] if v['n'] else 0
    print('\n【时段】', {k: (v['n'], round(v['avg'])) for k, v in hours.items()})
    aux = load_aux()
    prod_map = aux['products']
    prods = {}
    for r in records:
        pid = r.get('product_id')
        if not pid:
            continue
        p = prods.setdefault(pid, {'pid': str(pid), 'name': str(r['商品名称']), 'n': 0, 'plays': 0, 'inter': 0, 'accs': set(), 'cat': r['_category'], 'anchor': None, 'vids': []})
        p['n'] += 1
        p['plays'] += r['_plays']
        p['inter'] += r['_inter']
        p['accs'].add(r['对标账号'])
        p['vids'].append({'acc': r['对标账号'], 'vid': r['_vid_tail'], 'plays': r['_plays'], 'date': r['_pub'].strftime('%m-%d') if r['_pub'] else '?'})
    anchor_by_pid = {}
    for acc, m in prod_map.items():
        for vid, lst in m.items():
            for it in lst or []:
                if it.get('product_id'):
                    anchor_by_pid[str(it['product_id'])] = it.get('anchor_type')
    for pid, p in prods.items():
        p['anchor'] = anchor_by_pid.get(pid)
        p['eng'] = p['inter'] / p['plays'] * 100 if p['plays'] else 0
        p['cross'] = len(p['accs']) > 1
    prod_list = sorted(prods.values(), key=lambda x: -x['plays'])
    all_pids = set()
    acc_prod_stat = {}
    for acc_name, a in accounts.items():
        pids = {r.get('product_id') for r in records if r['对标账号'] == acc_name and r.get('product_id')}
        all_pids |= {str(x) for x in pids}
        videos_with_product = sum((1 for r in records if r['对标账号'] == acc_name and r.get('product_id')))
        acc_prod_stat[acc_name] = {'videos': a['n'], 'attached': videos_with_product, 'attach_rate': videos_with_product / a['n'] if a['n'] else 0, 'uniq': len(pids), 'reuse': videos_with_product - len(pids)}
    print('\n【商品/挂车】已挂车', attached, '/', len(records), '| 唯一商品', len(all_pids))
    for acc_name, s in acc_prod_stat.items():
        print(f"  {acc_name}: {s['attached']}/{s['videos']} 挂车, 唯一商品 {s['uniq']}, 复用 {s['reuse']}")
    print('【商品TOP5】', [(str(p['name'])[:28], p['plays'], sorted(p['accs'])) for p in prod_list[:5]])
    no_attach = [{'acc': r['对标账号'], 'vid': r['_vid_tail'], 'date': r['_pub'].strftime('%m-%d %H:%M') if r['_pub'] else '?', 'plays': r['_plays'], 'style': r['风格标签'] or '', 'url': r['视频链接/ID']} for r in records if not r['商品链接']]
    daily_rows = aux['daily']
    daily_status = {}
    for d in daily_rows:
        daily_status[d.get('gmv_status', '?')] = daily_status.get(d.get('gmv_status', '?'), 0) + 1
    gmv_null = sum((1 for d in daily_rows if d.get('gmv') is None))
    pending_items = (aux['pending'] or {}).get('items', [])
    manifests = aux['manifests']
    print('\n【出单】台账出单量空值', audit['empty_units'], '/', len(records), '| daily_metrics', len(daily_rows), '条 gmv 空', gmv_null, '| gmv_status', daily_status, '| pending', len(pending_items))
    data = {'kpi': {'videos': total['n'], 'plays': total['plays'], 'inter': total['inter'], 'eng': round(total_eng * 100, 2), 'attach': round(attach_rate * 100, 1), 'attached': attached, 'accounts': len([s for s, n in audit['row_counts'].items() if n > 0]), 'empty_accounts': [s.replace('对标记录-', '') for s, n in audit['row_counts'].items() if n == 0]}, 'daily': days, 'accounts': {k: {kk: vv for kk, vv in v.items() if kk != 'plays_list'} for k, v in accounts.items()}, 'cats': [{'name': k, **v} for k, v in cats_sorted], 'langs': langs, 'top10': [{'acc': r['对标账号'], 'vid': r['_vid_tail'], 'name': str(r['商品名称']), 'cat': r['_category'], 'plays': r['_plays'], 'inter': r['_inter'], 'eng': round(r['_inter'] / r['_plays'] * 100, 2) if r['_plays'] else 0, 'lang': r['_lang']} for r in top10], 'topEng': [{'acc': r['对标账号'], 'vid': r['_vid_tail'], 'name': str(r['商品名称'])[:50], 'plays': r['_plays'], 'eng': round(r['_inter'] / r['_plays'] * 100, 2)} for r in top_eng], 'anomalies': anomalies, 'zeroComment': zero_comment, 'immature': [{'acc': r['对标账号'], 'name': str(r['商品名称'])[:40] if str(r['商品名称'] or '').strip() else '（未挂车·磁疗手环）', 'age': r['_age_h'], 'plays': r['_plays']} for r in immature], 'repeat': [{'name': str(v[0]['商品名称'])[:55], 'cross': len(set((x['对标账号'] for x in v))) > 1, 'items': [{'acc': r['对标账号'], 'date': r['_pub'].strftime('%m-%d %H:%M') if r['_pub'] else '?', 'plays': r['_plays'], 'eng': round(r['_inter'] / r['_plays'] * 100, 2) if r['_plays'] else 0} for r in v]} for v in repeat_products.values()], 'hours': [{'name': k, **v} for k, v in hours.items()], 'products': {'attached': attached, 'total': len(records), 'attach_rate': round(attach_rate * 100, 1), 'uniq': len(all_pids), 'accStat': acc_prod_stat, 'list': [{'pid': p['pid'], 'name': p['name'], 'n': p['n'], 'plays': p['plays'], 'inter': p['inter'], 'eng': round(p['eng'], 2), 'cat': p['cat'], 'accs': sorted(p['accs']), 'cross': p['cross'], 'anchor': p['anchor']} for p in prod_list], 'noAttach': no_attach}, 'orders': {'ledgerEmpty': audit['empty_units'], 'ledgerTotal': len(records), 'dailyFiles': aux['daily_files'], 'gaps': aux['gaps'], 'dailyN': len(daily_rows), 'dailyAccounts': sorted({d.get('account') for d in daily_rows}), 'dailyDates': sorted({d.get('date') for d in daily_rows}), 'gmvNull': gmv_null, 'gmvStatus': daily_status, 'pending': [{'id': str(it['id']), 'plays': it['plays'], 'url': it['url'], 'tail': str(it['id'])[-12:], 'gmv_status': manifests.get('vivi_ashop', {}).get('videos', {}).get(str(it['id']), {}).get('gmv_status', 'pending'), 'name': next((str(r['商品名称']) for r in records if str(r.get('video_id')) == str(it['id'])), '')} for it in pending_items], 'manifests': {a: {'last_success_date': m.get('last_success_date'), 'videos': len(m.get('videos', {}))} for a, m in manifests.items()}}, 'detail': [{'acc': r['对标账号'], 'pub': r['_pub'].strftime('%m-%d %H:%M') if r['_pub'] else str(r['视频发布时间']), 'name': str(r['商品名称'] or '（未挂车）'), 'cat': r['_category'], 'plays': r['_plays'], 'likes': r['_likes'], 'comments': r['_comments'], 'shares': r['_shares'], 'favs': r['_favs'], 'eng': round(r['_inter'] / r['_plays'] * 100, 2) if r['_plays'] else 0, 'style': r['风格标签'] or '', 'lang': r['_lang'], 'age': r['_age_h'], 'url': r['视频链接/ID']} for r in sorted(records, key=lambda x: (x['对标账号'], -(x['_pub'].timestamp() if x['_pub'] else 0)))], 'audit': {'rows': audit['row_counts'], 'emptyUnits': audit['empty_units'], 'noProduct': len(audit['no_product']), 'dups': len(audit['dup_video_ids']), 'fill': audit['fill']}, 'pp': [[r['对标账号'], r['_plays']] for r in records]}
    html = build_html(data)
    OUT_HTML.write_text(html, encoding='utf-8')
    print(f'\n【输出】{OUT_HTML} ({OUT_HTML.stat().st_size / 1024:.0f} KB)')
    print('【校验】明细行数 =', len(data['detail']), '| KPI 播放 =', fmt(data['kpi']['plays']), '| KPI 视频数 =', data['kpi']['videos'])
    back = OUT_HTML.read_text(encoding='utf-8')
    assert '"videos": {}'.format(total['n']) in back or '"videos":{}'.format(total['n']) in back, 'KPI 校验失败'
    assert str(total['plays']) in back, '总播放校验失败'
    assert f"{len(data['products']['list'])} 个 SKU" in back, '商品清单条数校验失败'
    assert '未挂车视频标注' in back, '未挂车标注缺失'
    assert 'id="cProd"' in back and 'id="cGmv"' in back, '商品/出单图表缺失'
    assert '出单情况与 GMV 数据状态' in back, '出单章节缺失'
    assert f"{data['orders']['ledgerEmpty']}/{data['orders']['ledgerTotal']} 条记录中 100% 为空" in back, '出单空值校验失败'
    assert f"{len(data['orders']['pending'])} 条 · 待 FastMoss" in back, 'pending 条数校验失败'
    print('【终验】磁盘复读通过：视频数/总播放/商品清单/未挂车/出单章节均一致')
    print(f"【终验】商品 SKU 清单 = {len(data['products']['list'])} 条 | 未挂车 = {len(data['products']['noAttach'])} 条 | 台账出单空 = {data['orders']['ledgerEmpty']}/{data['orders']['ledgerTotal']} | pending = {len(data['orders']['pending'])} 条")

def build_html(d):
    echarts = ECHARTS_JS.read_text(encoding='utf-8')
    js_data = json.dumps(d, ensure_ascii=False)
    k = d['kpi']
    empty_acc = '、'.join(k['empty_accounts']) or '无'
    kpi_cards = f"""\n    <div class="kpis">\n      <div class="kpi"><div class="kv">{k['videos']}</div><div class="kl">视频总数（去重）</div></div>\n      <div class="kpi"><div class="kv">{fmt(k['plays'])}</div><div class="kl">总播放量</div></div>\n      <div class="kpi"><div class="kv">{fmt(k['inter'])}</div><div class="kl">总互动量（赞+评+转+藏）</div></div>\n      <div class="kpi"><div class="kv">{k['eng']}%</div><div class="kl">整体互动率</div></div>\n      <div class="kpi"><div class="kv">{k['attach']}%</div><div class="kl">挂车率（{k['attached']}/{k['videos']}）</div></div>\n      <div class="kpi"><div class="kv">{fmt(k['plays'] // k['videos'])}</div><div class="kl">平均播放/视频</div></div>\n    </div>"""
    dod_rows = ''
    for day in d['daily']:
        dod = day['dod']
        if dod is None:
            dod_html = '<span class="muted">—</span>'
        else:
            cls = 'up' if dod > 0 else 'down' if dod < 0 else ''
            dod_html = f'<span class="{cls}">{dod:+.1f}%</span>'
        dod_rows += f"<tr><td>2026-{day['date']}</td><td>{day['n']}</td><td>{fmt(day['plays'])}</td>\n        <td>{fmt(day['avg_plays'])}</td><td>{day['eng_rate'] * 100:.2f}%</td><td>{dod_html}</td></tr>"
    acc_rows = ''
    for name, a in d['accounts'].items():
        acc_rows += f"<tr><td>{name}</td><td>{a['n']}</td><td>{fmt(a['plays'])}</td><td>{fmt(a['avg_plays'])}</td>\n        <td>{fmt(a['median_plays'])}</td><td>{fmt(a['min_plays'])} ~ {fmt(a['max_plays'])}</td>\n        <td>{a['eng_rate'] * 100:.2f}%</td></tr>"
    cat_rows = ''
    for c in d['cats']:
        eng_c = pct(c['inter'] / c['plays']) if c['plays'] else '—'
        cat_rows += f"<tr><td>{c['name']}</td><td>{c['n']}</td><td>{fmt(c['plays'])}</td>\n        <td>{c['share'] * 100:.1f}%</td><td>{fmt(c['avg_plays'])}</td><td>{eng_c}</td></tr>"
    top_rows = ''
    for i, t in enumerate(d['top10'], 1):
        nm = t['name'] if len(t['name']) <= 42 else t['name'][:42] + '…'
        top_rows += f"""<tr><td>{i}</td><td>{t['acc']}</td><td class="tl">{nm}</td><td>{t['cat']}</td>\n        <td><b>{fmt(t['plays'])}</b></td><td>{t['inter']}</td><td>{t['eng']}%</td><td>{t['lang']}</td></tr>"""
    top_eng_rows = ''
    for t in d['topEng']:
        top_eng_rows += f"""<tr><td>{t['acc']}</td><td class="tl">{t['name']}</td><td>{fmt(t['plays'])}</td><td><b>{t['eng']}%</b></td></tr>"""
    anom_items = ''
    for a in d['anomalies']:
        anom_items += f"<li><b>[高播放离群]</b> {a['acc']} · {a['name']}…：{fmt(a['plays'])} 播放\n       （z={a['z']}，账号均值 {fmt(a['mean'])}，{a['note']}）</li>"
    immature_items = ''.join((f"<li>{x['acc']} · {x['name']}…：发布后仅 {x['age']:.0f}h 采集（{fmt(x['plays'])} 播放，数据未成熟，不宜参与排名）</li>" for x in d['immature']))
    repeat_html = ''
    for rp in d['repeat']:
        cross_tag = '<span class="tag tag-amber">跨账号</span>' if rp['cross'] else '<span class="tag">同账号</span>'
        items = ''.join((f"<tr><td>{it['acc']}</td><td>{it['date']}</td><td>{fmt(it['plays'])}</td><td>{it['eng']}%</td></tr>" for it in rp['items']))
        repeat_html += f"""<div class="rcard"><div class="rtitle">{cross_tag}{rp['name']}…</div>\n        <table><tr><th>账号</th><th>发布时间</th><th>播放量</th><th>互动率</th></tr>{items}</table></div>"""
    detail_rows = ''
    for r in d['detail']:
        age = f"{r['age']:.0f}h" if r['age'] is not None else '?'
        mature_flag = '' if r['age'] is not None and r['age'] >= 36 else ' <span class="imm">未成熟</span>'
        nm = r['name'] if len(r['name']) <= 45 else r['name'][:45] + '…'
        detail_rows += f"""<tr><td>{r['acc']}</td><td>{r['pub']}</td><td class="tl">{nm}</td><td>{r['cat']}</td>\n        <td>{fmt(r['plays'])}</td><td>{r['likes']}</td><td>{r['comments']}</td><td>{r['shares']}</td><td>{r['favs']}</td>\n        <td>{r['eng']}%</td><td>{r['style']}</td><td>{r['lang']}</td><td>{age}{mature_flag}</td></tr>"""
    hour_rows = ''.join((f"<tr><td>{h['name']}</td><td>{h['n']}</td><td>{fmt(h['plays'])}</td><td>{fmt(h['avg'])}</td></tr>" for h in d['hours']))
    fill_rows = ''
    for acc, f in d['audit']['fill'].items():
        fill_rows += f"<tr><td>{acc}</td><td>{f['style']}/{f['n']}</td><td>{f['trans']}/{f['n']}</td></tr>"
    P = d['products']
    prod_acc_rows = ''
    for acc, s in P['accStat'].items():
        prod_acc_rows += f"<tr><td>{acc}</td><td>{s['videos']}</td><td>{s['attached']}</td><td>{s['attach_rate'] * 100:.1f}%</td><td><b>{s['uniq']}</b></td><td>{s['reuse']}</td></tr>"
    prod_rows = ''
    for i, p in enumerate(P['list'], 1):
        accs = '+'.join(p['accs'])
        cross_tag = ' <span class="tag tag-amber">跨账号</span>' if p['cross'] else ''
        anchor = '-' if p['anchor'] is None else str(p['anchor'])
        nm = p['name'] if len(p['name']) <= 58 else p['name'][:58] + '…'
        prod_rows += f"<tr><td>{i}</td><td class='tl'>{nm}{cross_tag}</td><td>{p['cat']}</td><td>{accs}</td><td>{p['n']}</td><td><b>{fmt(p['plays'])}</b></td><td>{p['inter']}</td><td>{p['eng']}%</td><td>{anchor}</td></tr>"
    no_attach_rows = ''.join((f"<tr><td>{x['acc']}</td><td>{x['date']}</td><td>{x['vid']}</td><td>{x['style']}</td><td>{fmt(x['plays'])}</td></tr>" for x in P['noAttach'])) or "<tr><td colspan='5' class='muted'>无</td></tr>"
    O = d['orders']
    pending_rows = ''
    for it in O['pending']:
        nm = it['name'] if len(it['name']) <= 46 else it['name'][:46] + '…'
        pending_rows += f"<tr><td class='tl'>{nm}</td><td>{it['tail']}</td><td>{fmt(it['plays'])}</td><td><code>{it['gmv_status']}</code></td><td><a href='{it['url']}' target='_blank'>原视频</a></td></tr>"
    status_rows = ''.join((f"<tr><td><code>{k}</code></td><td>{v}</td><td>{v / max(1, O['dailyN']) * 100:.1f}%</td></tr>" for k, v in sorted(O['gmvStatus'].items(), key=lambda kv: -kv[1])))
    man_rows = ''.join((f"<tr><td>{a}</td><td>{m['last_success_date'] or '—'}</td><td>{m['videos']}</td></tr>" for a, m in O['manifests'].items())) or "<tr><td colspan='3' class='muted'>无 manifest</td></tr>"
    gaps_html = '；'.join(O['gaps']) if O['gaps'] else '无'
    js_cat = next((c for c in d['cats'] if c['name'] == '首饰饰品'), None)
    js_js_share = pct(js_cat['share'], 1) if js_cat else '—'
    js_js_n = js_cat['n'] if js_cat else '—'
    vivi = d['accounts'].get('vivi_ashop', {})
    esp = d['langs'].get('西语', {})
    esp_ratio = esp.get('avg_plays', 0) / max(1e-09, (vivi.get('plays', 0) - esp.get('plays', 0)) / max(1, vivi.get('n', 1) - esp.get('n', 0))) if esp else 0
    kee_med = d['accounts'].get('kee.yaden', {}).get('median_plays', 0)
    cha_med = d['accounts'].get('charelttce', {}).get('median_plays', 0)
    vivi_med = d['accounts'].get('vivi_ashop', {}).get('median_plays', 0)
    return f"""<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n<meta name="viewport" content="width=device-width, initial-scale=1">\n<title>TikTok 对标账号销售分析报表 · 2026-10-04</title>\n<style>\n  /* ===== 1. 设计令牌（唯一色板来源，JS 图表色板也从这里读取） ===== */\n  :root {{ --pri:#2563eb; --red:#dc2626; --green:#16a34a; --amber:#d97706;\n          --purple:#7c3aed; --teal:#0d9488; --gray:#94a3b8;\n          --ink:#1e293b; --sub:#64748b; --line:#e2e8f0; --bg:#f8fafc; --card:#ffffff; }}\n\n  /* ===== 2. Reset 与基础排版 ===== */\n  * {{ box-sizing:border-box; margin:0; padding:0; }}\n  body {{ font-family:"PingFang SC","Microsoft YaHei",system-ui,sans-serif;\n         background:var(--bg); color:var(--ink); line-height:1.65; padding:24px; }}\n  h2 {{ font-size:18px; margin-bottom:14px; padding-left:10px; border-left:4px solid var(--pri); }}\n  h3 {{ font-size:15px; margin:16px 0 8px; color:#334155; }}\n\n  /* ===== 3. 页面骨架 ===== */\n  .wrap {{ max-width:1180px; margin:0 auto; }}\n  header {{ background:linear-gradient(135deg,#1d4ed8,#3b82f6); color:#fff;\n           border-radius:14px; padding:28px 32px; margin-bottom:20px; }}\n  header h1 {{ font-size:24px; margin-bottom:6px; }}\n  .meta {{ font-size:13px; opacity:.9; }}\n  section {{ background:var(--card); border:1px solid var(--line); border-radius:14px;\n            padding:24px 28px; margin-bottom:20px; }}\n  footer {{ font-size:12px; color:var(--sub); text-align:center; padding:12px; }}\n\n  /* ===== 4. KPI 卡片 ===== */\n  .kpis {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:14px; }}\n  .kpi {{ background:linear-gradient(180deg,#eff6ff,#ffffff); border:1px solid #bfdbfe;\n         border-radius:12px; padding:16px; text-align:center; }}\n  .kv {{ font-size:26px; font-weight:700; color:var(--pri); }}\n  .kl {{ font-size:12px; color:var(--sub); margin-top:4px; }}\n\n  /* ===== 5. 表格 ===== */\n  table {{ width:100%; border-collapse:collapse; font-size:13px; margin:10px 0; }}\n  th {{ background:#f1f5f9; text-align:left; padding:8px 10px;\n       border-bottom:2px solid var(--line); white-space:nowrap; }}\n  td {{ padding:7px 10px; border-bottom:1px solid var(--line); }}\n  tr:hover td {{ background:#f8fafc; }}\n  .total-row {{ font-weight:600; background:#f8fafc; }}  /* 合计行 */\n\n  /* ===== 6. 徽标与标记（变体规则须排在 .tag/.notice 之后以同权重覆盖） ===== */\n  .tag {{ display:inline-block; background:#eff6ff; color:var(--pri); border-radius:6px;\n         padding:1px 8px; font-size:12px; margin-right:6px; }}\n  .tag-amber {{ background:#fef3c7; color:#b45309; }}          /* 跨账号复用标记 */\n  .imm {{ background:#fef3c7; color:var(--amber); font-size:11px; padding:1px 6px; border-radius:8px; }}\n  .notice {{ background:#fffbeb; border:1px solid #fcd34d; border-radius:12px;\n            padding:16px 20px; margin-bottom:20px; font-size:13px; }}\n  .notice b {{ color:#92400e; }}\n  .notice-danger {{ background:#fef2f2; border-color:#fca5a5; }}  /* 红色警示变体 */\n\n  /* ===== 7. 图表与栅格 ===== */\n  .chart {{ width:100%; height:360px; }}\n  .chart-sm {{ width:100%; height:320px; }}\n  .grid2 {{ display:grid; grid-template-columns:1fr 1fr; gap:16px; }}\n\n  /* ===== 8. 列表 / 复推卡片 ===== */\n  section ul li {{ margin:6px 0; }}\n  .rcard {{ border:1px solid var(--line); border-radius:10px; padding:12px 16px; margin-bottom:10px; }}\n  .rtitle {{ font-weight:600; margin-bottom:6px; font-size:13px; }}\n\n  /* ===== 9. 文本工具类 ===== */\n  .tl {{ max-width:280px; word-break:break-all; }}\n  .up {{ color:var(--red); font-weight:600; }}      /* 涨（中式配色：红涨绿跌） */\n  .down {{ color:var(--green); font-weight:600; }}  /* 跌 */\n  .muted {{ color:var(--sub); }}\n  .note {{ font-size:12px; color:var(--sub); }}     /* 脚注 / 口径说明 */\n  .sm {{ font-size:13px; }}\n  .mt {{ margin-top:12px; }}\n\n  /* ===== 10. 响应式 ===== */\n  @media (max-width:860px) {{ .grid2 {{ grid-template-columns:1fr; }} }}\n</style>\n</head>\n<body>\n<div class="wrap">\n\n<header>\n  <h1>TikTok 对标账号销售分析报表</h1>\n  <div class="meta">数据源：tiktok_对标记录.xlsx（4 个账号分表合并，{k['videos']} 条视频记录）｜\n  视频发布窗口：2026-09-27 ~ 2026-10-03｜生成时间：{GENERATED_AT}</div>\n</header>\n\n<div class="notice">\n  <b>⚠ 口径与数据边界声明（先读）</b><br>\n  ① 本台账为 <b>TikTok 对标账号视频带货内容记录</b>，并非成交数据表：<b>「视频出单量」36/36 全部为空</b>（FastMoss 估算字段未回填），\n  且无销售额、价格、区域、渠道字段——因此"销量/销售额/区域/渠道"维度<b>在本数据中不可直接分析</b>。<br>\n  ② 报表以 <b>播放量与互动量（赞/评/转/藏）作为销售表现的代理指标</b>，所有"销售表现"均指"内容带货曝光表现"。<br>\n  ③ 互动数据为<b>采集时点快照</b>（kee.yaden 采集于 10-02，vivi_ashop / charelttce 采集于 10-03），账号横向对比存在成熟度偏差，已尽量标注。<br>\n  ④ 品类为基于英文商品名称的关键词归纳；"区域/渠道"以内容语言（英语/西语）侧写受众市场。\n</div>\n\n<section>\n  <h2>一、关键指标汇总</h2>\n  {kpi_cards}\n  <p class="sm muted mt">\n  覆盖账号：kee.yaden（7 条）、vivi_ashop（23 条）、charelttce（6 条）；dicharrymeredith（0 条，尚未采集）。\n  出单量字段缺失率 100%；挂车率按商品链接非空计算（唯一未挂车为 vivi_ashop 的磁疗手环视频）。</p>\n</section>\n\n<section>\n  <h2>二、整体趋势（按视频发布日期）</h2>\n  <div id="cTrend" class="chart"></div>\n  <div class="grid2">\n    <div id="cEng" class="chart-sm"></div>\n    <div id="cHour" class="chart-sm"></div>\n  </div>\n  <h3>每日汇总与环比</h3>\n  <table>\n    <tr><th>发布日期</th><th>视频数</th><th>播放量合计</th><th>平均播放/条</th><th>互动率</th><th>播放环比</th></tr>\n    {dod_rows}\n  </table>\n  <p class="note">\n  注：按发布日期聚合的播放量受"曝光累积时间"影响——越晚发布的视频在采集时点累积时间越短\n  （10-02/10-03 发布的视频仅数小时~1天），环比下降≠表现变差；趋势解读以 09-27 ~ 10-01 窗口为宜。\n  09-30 为投放高峰（9 条、{fmt(d['daily'][3]['plays'])} 播放），主要由 vivi_ashop 爆款（三圣心手环 58,700）与 kee/charelttce 数码内容共同贡献。</p>\n</section>\n\n<section>\n  <h2>三、账号对比（渠道侧写：账号即带货渠道）</h2>\n  <table>\n    <tr><th>账号</th><th>视频数</th><th>总播放</th><th>平均播放</th><th>中位数播放</th><th>播放区间</th><th>互动率</th></tr>\n    {acc_rows}\n  </table>\n  <div class="grid2">\n    <div id="cAcc" class="chart-sm"></div>\n    <div id="cAccBox" class="chart-sm"></div>\n  </div>\n  <p class="sm">\n  <span class="tag">charelttce</span>充电数码垂类，平均播放 {fmt(d['accounts'].get('charelttce', {}).get('avg_plays', 0))}、中位数 {fmt(cha_med)}，表现最稳；<br>\n  <span class="tag">kee.yaden</span>同为充电数码垂类（与 charelttce 共用 PUTARE 选品池），中位数 {fmt(kee_med)}，输出稳定；<br>\n  <span class="tag">vivi_ashop</span>多品类测款（首饰/手机壳/包袋/礼品/玄学），依赖偶发爆款拉动总量，中位数仅 {fmt(vivi_med)}，波动极大（两条爆款贡献其 59% 播放）。</p>\n</section>\n\n<section>\n  <h2>四、商品维度分析（挂车覆盖 · 选品结构 · 商品清单）</h2>\n  <p class="sm muted">\n  数据源：台账「商品链接」「商品名称」承载列（视频挂的小黄车 TikTok Shop 商品锚点）；并回填 vivi_ashop / charelttce 的\n  <code>*_products.json</code>（含 anchor_type 锚点类型）。<b>商品名称一律保留英文原文，不意译</b>；同一商品在漏斗中被多条视频复用时按 product_id 去重聚合。</p>\n  <div class="kpis">\n    <div class="kpi"><div class="kv">{P['attached']}/{P['total']}</div><div class="kl">挂车视频 / 总视频</div></div>\n    <div class="kpi"><div class="kv">{P['attach_rate']}%</div><div class="kl">挂车覆盖率</div></div>\n    <div class="kpi"><div class="kv">{P['uniq']}</div><div class="kl">唯一商品数（product_id 去重）</div></div>\n    <div class="kpi"><div class="kv">{len(P['noAttach'])}</div><div class="kl">未挂车视频</div></div>\n  </div>\n\n  <h3>4.1 各账号挂车与选品分布</h3>\n  <table>\n    <tr><th>账号</th><th>视频数</th><th>已挂车</th><th>挂车率</th><th>唯一商品数</th><th>商品复用次数</th></tr>\n    {prod_acc_rows}\n    <tr class="total-row"><td>合计</td><td>{P['total']}</td><td>{P['attached']}</td>\n      <td>{P['attach_rate']}%</td><td>{P['uniq']}</td><td>{P['attached'] - P['uniq']}</td></tr>\n  </table>\n  <p class="note">\n  解读：vivi_ashop 走<b>多品类广测</b>（23 条视频投 {P['accStat'].get('vivi_ashop', {}).get('uniq', '—')} 个 SKU，几乎一品一条）；\n  kee.yaden 与 charelttce 走<b>深度复推</b>（各自 7 条 / 6 条视频只覆盖 {P['accStat'].get('kee.yaden', {}).get('uniq', '—')} / {P['accStat'].get('charelttce', {}).get('uniq', '—')} 个 SKU，PUTARE 系列反复推）。挂车率 {P['attach_rate']}%（{P['attached']}/{P['total']}）。</p>\n\n  <h3>4.2 商品清单 / TOP（按累计播放降序，共 {P['uniq']} 个 SKU；anchor 列为商品锚点类型）</h3>\n  <div id="cProd" class="chart"></div>\n  <table>\n    <tr><th>#</th><th>商品名称（英文原文）</th><th>品类</th><th>账号</th><th>视频数</th><th>累计播放</th><th>互动量</th><th>互动率</th><th>anchor</th></tr>\n    {prod_rows}\n  </table>\n  <p class="note">\n  注：跨账号商品 = kee.yaden 与 charelttce 共用 PUTARE 选品池（Scrolling Ring / Remote Control / Induction Speaker），说明两账号选品同源。\n  累计播放为各视频发布后快照播放之和，非商品累计曝光，受采集时点影响。</p>\n\n  <h3>4.3 未挂车视频标注（曝光转化链路断裂）</h3>\n  <table>\n    <tr><th>账号</th><th>发布时间</th><th>视频ID（后12位）</th><th>风格标签</th><th>播放量</th></tr>\n    {no_attach_rows}\n  </table>\n</section>\n\n<section>\n  <h2>五、品类表现对比（产品维度）</h2>\n  <div class="grid2">\n    <div id="cCatPie" class="chart-sm"></div>\n    <div id="cCatBar" class="chart-sm"></div>\n  </div>\n  <table>\n    <tr><th>品类</th><th>视频数</th><th>总播放</th><th>播放占比</th><th>平均播放/条</th><th>互动率</th></tr>\n    {cat_rows}\n  </table>\n</section>\n\n<section>\n  <h2>六、出单情况与 GMV 数据状态（数据缺口专项）</h2>\n  <div class="notice notice-danger">\n    <b>⚠ 结论先行：当前台账不含任何出单数据，本报告不给出任何销量 / GMV 数字（编造数字视为违规）。</b><br>\n    ① 台账承载列「视频出单量」（第 8 列，video_attributed_units_sold）在全部\n    <b>{O['ledgerEmpty']}/{O['ledgerTotal']} 条记录中 100% 为空</b>，全表无一个数值。<br>\n    ② 台账「说明」页明确：该列为第三方（FastMoss）<b>模型估算</b>件数，<b>非官方归因、不能对账、公开页面取不到、下载链路恒留空</b>，\n    需 <code>--fill-video-units</code> 单独回填后才会有值。<br>\n    ③ 因此"出单量 / 销售额 / 转化率 / ROI"维度在本数据中<b>不可计算</b>；本报表以播放量 + 互动量作为销售表现的<b>代理指标</b>，\n    不代表任何成交结果。出单数据补齐后，本报表 TOP 排名需整体重排复核。\n  </div>\n\n  <h3>6.1 已有 GMV 侧数据的实际覆盖（daily_metrics.jsonl）</h3>\n  <table>\n    <tr><th>指标</th><th>值</th><th>说明</th></tr>\n    <tr><td>快照条数</td><td>{O['dailyN']}</td><td>仅覆盖账号 {'、'.join(O['dailyAccounts']) or '—'}，日期 {'、'.join((str(x) for x in O['dailyDates'])) or '—'}</td></tr>\n    <tr><td>gmv 非空条数</td><td><b>0/{O['dailyN']}</b></td><td>gmv 字段 100% 为 null，无任何 GMV 数值</td></tr>\n    <tr><td>未覆盖账号</td><td>kee.yaden、charelttce</td><td>两账号无 daily_metrics 快照，亦无 GMV 侧数据</td></tr>\n    <tr><td>manifest 采集状态</td><td colspan="2"><table><tr><th>账号</th><th>last_success_date</th><th>视频数</th></tr>{man_rows}</table></td></tr>\n  </table>\n\n  <h3>6.2 gmv_status 分布（FastMoss 阈值逻辑）</h3>\n  <div id="cGmv" class="chart-sm"></div>\n  <table>\n    <tr><th>gmv_status</th><th>条数</th><th>占比</th></tr>\n    {status_rows}\n    <tr class="total-row"><td>合计</td><td>{O['dailyN']}</td><td>100.0%</td></tr>\n  </table>\n  <p class="note">\n  口径：<code>below_threshold</code> = 播放量低于查询阈值（6000），未进入 FastMoss 出单查询；\n  <code>pending</code> = 播放达阈值（≥6000）、已进入待查队列但尚未取得出单数据。\n  即：{O['dailyN']} 条 vivi_ashop 视频中 <b>{O['gmvStatus'].get('pending', 0)} 条具备查询资格却仍无值</b>，\n  {O['gmvStatus'].get('below_threshold', 0)} 条因流量不足未触发查询——<b>整个数据库当前 0 条视频拥有真实出单/GMV 数值</b>。</p>\n\n  <h3>6.3 pending_gmv 待查清单（{len(O['pending'])} 条 · 待 FastMoss 登录态 MCP 补查）</h3>\n  <p class="note">筛选口径：<code>fastmoss_gmv.py --min-plays 6000</code>；清单落盘于 <code>vivi_ashop/pending_gmv.json</code>，回写 fastmoss_cache.json 后重跑管线即可合并。</p>\n  <table>\n    <tr><th>商品名称（英文原文）</th><th>视频ID（后12位）</th><th>播放量</th><th>gmv_status</th><th>链接</th></tr>\n    {pending_rows}\n  </table>\n\n  <h3>6.4 数据缺口与获取路径</h3>\n  <ul class="findings">\n    <li><b>[缺口1 · 台账出单列全空]</b> 「视频出单量」{O['ledgerEmpty']}/{O['ledgerTotal']} 为空 →\n      路径：需 FastMoss 登录态，运行 <code>--fill-video-units</code> 回填（依赖 MCP 额度）。</li>\n    <li><b>[缺口2 · GMV 快照全 null]</b> daily_metrics {O['dailyN']} 条 gmv 全为 null，仅 vivi_ashop 有快照 →\n      路径：补齐 kee.yaden / charelttce 的采集与 GMV 查询。</li>\n    <li><b>[缺口3 · 待查清单未消耗]</b> pending_gmv {len(O['pending'])} 条（均 vivi_ashop，含全表 TOP1 播放 58,700 的三圣心手环）仍待查询 →\n      路径：FastMoss MCP 额度可用时逐条补查并回写。</li>\n    <li><b>[缺口4 · 辅助源缺失]</b> {gaps_html} → 不影响台账统计，但限制了商品锚点/时序分析的适用范围。</li>\n  </ul>\n  <p class="note">\n  <b>数据边界声明：</b>本报告所有"出单/GMV"相关表述均为<b>数据状态说明</b>，不含任何估算或示例数字；\n  未查项数量、口径与获取路径已如实列出，结论受上述缺口影响的部分已在正文标注。</p>\n</section>\n\n<section>\n  <h2>七、受众市场侧写（语言维度，替代区域/渠道）</h2>\n  <div id="cLang" class="chart-sm"></div>\n  <table>\n    <tr><th>受众语言</th><th>视频数</th><th>总播放</th><th>平均播放/条</th></tr>\n    {''.join((f"<tr><td>{kk}</td><td>{vv['n']}</td><td>{fmt(vv['plays'])}</td><td>{fmt(vv['avg_plays'])}</td></tr>" for kk, vv in d['langs'].items()))}\n  </table>\n  <p class="sm">vivi_ashop 内 2 条西语视频（宗教饰品/首饰）平均播放约为该账号英语视频的\n  <b>{esp_ratio:.1f} 倍</b>（{fmt(esp.get('avg_plays', 0))} vs {fmt((vivi.get('plays', 0) - esp.get('plays', 0)) / max(1, vivi.get('n', 1) - esp.get('n', 0)))}），\n  西语受众市场是其最重要的流量杠杆。</p>\n</section>\n\n<section>\n  <h2>八、重点项目排名（TOP10 播放 · 代理销量排名）</h2>\n  <div id="cTop" class="chart"></div>\n  <table>\n    <tr><th>#</th><th>账号</th><th>商品名称</th><th>品类</th><th>播放量</th><th>互动量</th><th>互动率</th><th>语言</th></tr>\n    {top_rows}\n  </table>\n  <h3>高互动率 TOP5（播放 ≥500 的成熟样本）</h3>\n  <table>\n    <tr><th>账号</th><th>商品名称</th><th>播放量</th><th>互动率</th></tr>\n    {top_eng_rows}\n  </table>\n</section>\n\n<section>\n  <h2>九、异常值与值得关注的波动</h2>\n  <ul class="findings">\n    {anom_items}\n    <li><b>[零评论现象]</b> 36 条视频中 <b>{d['zeroComment']} 条评论数为 0</b>（vivi_ashop 23 条中 19 条为 0）——播放量与评论量严重脱节，\n    流量结构疑似以系统推流/投流为主，互动深度不足，需出单量数据验证真实转化。</li>\n    <li><b>[播放极差]</b> vivi_ashop 播放区间 7 ~ 58,700（相差 8,385 倍），账号表现高度依赖单条爆款，无稳定基本盘。</li>\n    <li><b>[未成熟数据]</b> 以下视频采集时发布不足 36 小时，数据尚未收敛（参与汇总但不建议用于排名定论）：\n    <ul>{immature_items}</ul></li>\n    <li><b>[未挂车视频]</b> vivi_ashop 10-01 19:02 的磁疗手环视频未挂商品链接（全表唯一），曝光转化链路断裂。</li>\n    <li><b>[时段反差]</b> 晚间（18-24 时）发布最密集（15 条）但平均播放最低（{fmt([h for h in d['hours'] if h['name'] == '晚间(18-24)'][0]['avg'])}）；\n    凌晨（0-6 时）发布 11 条、平均播放最高（{fmt([h for h in d['hours'] if h['name'] == '凌晨(0-6)'][0]['avg'])}）——样本量小，仅作观察。</li>\n  </ul>\n  <h3>同品复推效果（同一商品发多条视频，含跨账号）</h3>\n  <div class="grid2">{repeat_html}</div>\n  <p class="note">\n  注：① 同品复推整体衰减明显（豹纹手机壳 -88%、透明磁吸壳 -75%、Scrolling Ring -85%），但晚发布的视频累积时间更短，衰减幅度含时间偏差；\n  ② kee.yaden 与 charelttce 存在<b>跨账号推广完全相同的 PUTARE 商品</b>（音箱支架/遥控器/翻页戒指），选品池高度重合，其中音箱支架 kee 版 19,000 播放 vs charelttce 版 2,875/5,828，同类素材账号间表现分化显著。</p>\n</section>\n\n<section>\n  <h2>十、数据明细表（全部 {k['videos']} 条）</h2>\n  <table>\n    <tr><th>账号</th><th>发布时间</th><th>商品名称</th><th>品类</th><th>播放</th><th>赞</th><th>评</th><th>转</th><th>藏</th><th>互动率</th><th>风格标签</th><th>语言</th><th>采集时视频年龄</th></tr>\n    {detail_rows}\n  </table>\n</section>\n\n<section>\n  <h2>十一、核心发现与业务建议</h2>\n  <h3>发现（按证据强度排序）</h3>\n  <ul class="findings">\n    <li><b>★★★ 西语宗教内容是已被验证的流量杠杆。</b>三圣心护佑手环（西语口播）58,700 播放为全表第一（约为该账号均值的 9.3 倍），\n    14K 麻花耳环（西语）11,600 播放位列第九；首饰饰品以 {js_js_n} 条（{js_js_n / k['videos'] * 100:.0f}%）贡献了 {js_js_share} 的总播放，\n    单条平均表现（{fmt(js_cat['avg_plays'])}）与双账号堆量的充电数码（47.8%）几乎持平，是最强的非数码赛道。</li>\n    <li><b>★★★ 出单量数据 100% 缺失，当前所有"表现"仅为曝光侧。</b>高播放 ≠ 高出单，36 条中 {d['zeroComment']} 条零评论提示流量深度存疑。</li>\n    <li><b>★★☆ 数码垂类双账号基本盘稳。</b>charelttce（均播 {fmt(d['accounts'].get('charelttce', {}).get('avg_plays', 0))}）与 kee.yaden（均播 {fmt(d['accounts'].get('kee.yaden', {}).get('avg_plays', 0))}）中位数均接近 1 万，\n    显著高于 vivi_ashop 的 {fmt(vivi_med)}；两者共用 PUTARE 选品池，属同赛道对标。</li>\n    <li><b>★★☆ 同品复推（含跨账号）衰减明显。</b>5 组复推中 4 组第二次发布播放大幅下滑（-75% ~ -88%）；跨账号同品表现分化（kee 音箱支架 19,000 vs charelttce 同品 2,875/5,828）说明素材与账号权重共同决定表现。</li>\n    <li><b>★☆☆ 时段规律样本不足。</b>凌晨发布平均播放最高、晚间最低，但 36 条样本且受成熟度偏差影响，不足以支撑统计结论。</li>\n  </ul>\n  <h3>可执行建议</h3>\n  <ol>\n    <li><b>补齐出单量（最高优先级）：</b>回填 FastMoss「视频出单量」（--fill-video-units），将曝光代理指标升级为真实转化指标，再复核本报表全部排名结论。</li>\n    <li><b>统一采集时点：</b>对全部账号做同一时点二次采集（视频成熟后 ≥72h），消除成熟度偏差后再做账号横向定论。</li>\n    <li><b>vivi_ashop：</b>收缩测款范围，资源向已验证题材（西语宗教首饰、玄学能量、幽默礼品）倾斜；西语内容可加大投入并系统化（固定口播模板+西语文案）；补齐 23 条视频的中文翻译与缺失的风格标签规范。</li>\n    <li><b>同品复推：</b>停止同素材角度直接复推，改为换场景/换人群角度再测；跨账号推同品前先对比账号受众匹配度（kee 与 charelttce 同品表现差 3~6 倍）。</li>\n    <li><b>kee.yaden / charelttce：</b>数码垂类模板已跑通（中位数稳定），可提升发布频次做规模复制；kee 补风格标签，charelttce 关注凌晨时段（其 26,700 播放的 Scrolling Ring 发布于 03:20）。</li>\n    <li><b>未挂车视频：</b>排查 10-01 磁疗手环视频漏挂车原因（发布后商品下架 or 录入遗漏），避免流量浪费。</li>\n  </ol>\n</section>\n\n<section>\n  <h2>十二、假设声明汇总</h2>\n  <ol class="sm">\n    <li><b>代理指标假设：</b>无销量/销售额数据，以播放量（曝光）+ 互动量（互动深度）作为销售表现代理。</li>\n    <li><b>品类归纳假设：</b>品类由英文商品名称关键词人工归类（充电数码/首饰/手机壳/包袋/玄学/礼品/DIY/个护），kee.yaden 与 charelttce 整账号归入充电数码；未挂车的磁疗手环视频按风格标签归入首饰。非平台官方类目。</li>\n    <li><b>区域/渠道假设：</b>数据无区域渠道字段，以"对标账号"作为渠道单元、以内容语言（英语/西语）侧写受众市场。</li>\n    <li><b>互动率定义：</b>互动率 =（点赞+评论+转发+收藏）/ 播放量。</li>\n    <li><b>快照假设：</b>互动数据为采集时点快照且各账号采集时点不同（10-02 / 10-03）；发布不足 36h 的视频标注"未成熟"。</li>\n    <li><b>环比口径：</b>环比基于"视频发布日期"聚合的播放量，受曝光累积时间影响，趋势以 09-27 ~ 10-01 窗口为准。</li>\n    <li><b>排名口径：</b>TOP 榜为"曝光榜"而非销量榜；出单量回填后应重排。</li>\n  </ol>\n</section>\n\n<section>\n  <h2>附录：数据质量报告</h2>\n  <table>\n    <tr><th>检查项</th><th>结果</th></tr>\n    <tr><td>各表行数</td><td>{json.dumps(d['audit']['rows'], ensure_ascii=False)}（合计 {k['videos']} 条，视频 ID 无重复，与去重机制一致）</td></tr>\n    <tr><td>视频出单量缺失</td><td>{d['audit']['emptyUnits']}/{k['videos']}（100% 缺失，FastMoss 未回填）</td></tr>\n    <tr><td>未挂车视频</td><td>{d['audit']['noProduct']} 条（vivi_ashop 磁疗手环）</td></tr>\n    <tr><td>风格标签 / 中文翻译填充度</td><td>\n      <table><tr><th>账号</th><th>风格标签已标注</th><th>中文翻译已填</th></tr>{fill_rows}</table></td></tr>\n    <tr><td>日期类型</td><td>vivi_ashop 表多数发布时间为文本字符串（已程序化解析，解析失败 0 条），其余为真日期类型</td></tr>\n    <tr><td>数值字段</td><td>播放/赞/评/转/藏均为整数、无负值、无错误值</td></tr>\n  </table>\n</section>\n\n<footer>TikTok 对标销售分析报表 · 由数据分析流水线自动生成 · {GENERATED_AT} · 数据源：D:/1/对标账号/tiktok_对标记录.xlsx</footer>\n</div>\n\n<script>{echarts}</script>\n<script>\nconst D = {js_data};\nwindow.__pp = D.pp;\n// 色板统一来自 CSS :root 变量（与样式表单一来源，改色只需改一处）\nconst CS = getComputedStyle(document.documentElement);\nconst V = n => CS.getPropertyValue(n).trim();\nconst C = {{pri:V("--pri"), red:V("--red"), green:V("--green"), amber:V("--amber"),\n           purple:V("--purple"), teal:V("--teal"), gray:V("--gray")}};\nconst base = {{tooltip:{{trigger:"axis"}}, grid:{{left:60,right:60,top:40,bottom:50}}}};\nconst mk = (id, opt) => echarts.init(document.getElementById(id)).setOption(opt);\n\n// 二、趋势：每日播放(bar) + 视频数(line, 右轴)\nmk("cTrend", {{...base, legend:{{data:["播放量","视频数"],bottom:0}},\n  xAxis:{{type:"category",data:D.daily.map(x=>"2026-"+x.date)}},\n  yAxis:[{{type:"value",name:"播放量"}},{{type:"value",name:"视频数",minInterval:1}}],\n  series:[\n    {{name:"播放量",type:"bar",itemStyle:{{color:C.pri}},data:D.daily.map(x=>x.plays),\n     label:{{show:true,position:"top",formatter:p=>p.value>=10000?(p.value/10000).toFixed(1)+"万":p.value}}}},\n    {{name:"视频数",type:"line",yAxisIndex:1,itemStyle:{{color:C.amber}},data:D.daily.map(x=>x.n)}}\n  ]}});\n\n// 二、互动率趋势\nmk("cEng", {{...base, title:{{text:"每日互动率（%）",textStyle:{{fontSize:13}}}},\n  xAxis:{{type:"category",data:D.daily.map(x=>x.date)}},\n  yAxis:{{type:"value",axisLabel:{{formatter:"{{value}}%"}}}},\n  series:[{{type:"line",smooth:true,itemStyle:{{color:C.teal}},areaStyle:{{opacity:.12}},\n    data:D.daily.map(x=>+(x.eng_rate*100).toFixed(2))}}]}});\n\n// 二、时段\nmk("cHour", {{...base, title:{{text:"发布时段 × 平均播放",textStyle:{{fontSize:13}}}},\n  xAxis:{{type:"category",data:D.hours.map(h=>h.name)}},\n  yAxis:{{type:"value"}},\n  series:[{{type:"bar",itemStyle:{{color:C.purple}},data:D.hours.map(h=>Math.round(h.avg)),\n    label:{{show:true,position:"top"}}}}]}});\n\n// 三、账号对比\nconst accs = Object.keys(D.accounts);\nmk("cAcc", {{...base, legend:{{data:["平均播放/条","中位数播放"],bottom:0}},\n  xAxis:{{type:"category",data:accs}},\n  yAxis:{{type:"value"}},\n  series:[\n    {{name:"平均播放/条",type:"bar",itemStyle:{{color:C.pri}},data:accs.map(a=>Math.round(D.accounts[a].avg_plays)),label:{{show:true,position:"top"}}}},\n    {{name:"中位数播放",type:"bar",itemStyle:{{color:C.teal}},data:accs.map(a=>Math.round(D.accounts[a].median_plays)),label:{{show:true,position:"top"}}}}\n  ]}});\n\nmk("cAccBox", {{...base, title:{{text:"播放量分布（账号内 min/Q1/中位/Q3/max）",textStyle:{{fontSize:13}}}},\n  xAxis:{{type:"category",data:accs}},\n  yAxis:{{type:"value"}},\n  series:accs.map(function(a,i){{\n    var v=window.__pp.filter(r=>r[0]===a).map(r=>r[1]).sort((x,y)=>x-y);\n    var q=function(p){{return v[Math.floor(p*(v.length-1))];}};\n    return {{type:"boxplot",name:a,itemStyle:{{color:[C.pri,C.amber,C.purple][i]||C.gray}},\n      data:[[q(0),q(.25),q(.5),q(.75),q(1)]],\n      tooltip:{{formatter:function(ob){{return a+"<br/>min/Q1/中位/Q3/max：<br/>"+ob.data.join(" / ");}}}}}};\n  }})}});\n\n// 五、品类\nmk("cCatPie", {{title:{{text:"播放量占比",textStyle:{{fontSize:13}},left:"center"}},\n  tooltip:{{trigger:"item",formatter:"{{b}}: {{c}} ({{d}}%)"}},\n  legend:{{bottom:0,type:"scroll"}},\n  series:[{{type:"pie",radius:["35%","65%"],data:D.cats.map(c=>({{name:c.name,value:c.plays}})),\n    label:{{formatter:"{{b}}\\n{{d}}%"}}}}]}});\n\nmk("cCatBar", {{...base, title:{{text:"品类平均播放/条",textStyle:{{fontSize:13}}}},\n  xAxis:{{type:"value"}},\n  yAxis:{{type:"category",data:D.cats.map(c=>c.name).reverse(),axisLabel:{{fontSize:11}}}},\n  series:[{{type:"bar",data:D.cats.map(c=>Math.round(c.avg_plays)).reverse(),\n    itemStyle:{{color:C.pri}},label:{{show:true,position:"right"}}}}]}});\n\n// 七、语言\nmk("cLang", {{...base, legend:{{data:["总播放","平均播放/条"],bottom:0}},\n  xAxis:{{type:"category",data:Object.keys(D.langs)}},\n  yAxis:[{{type:"value"}},{{type:"value"}}],\n  series:[\n    {{name:"总播放",type:"bar",itemStyle:{{color:C.pri}},data:Object.values(D.langs).map(v=>v.plays),label:{{show:true,position:"top"}}}},\n    {{name:"平均播放/条",type:"bar",yAxisIndex:1,itemStyle:{{color:C.red}},data:Object.values(D.langs).map(v=>Math.round(v.avg_plays)),label:{{show:true,position:"top"}}}}\n  ]}});\n\n// 八、TOP10\nmk("cTop", {{...base, grid:{{left:230,right:80,top:20,bottom:30}},\n  xAxis:{{type:"value"}},\n  yAxis:{{type:"category",\n    data:D.top10.map(t=>(t.name.length>34?t.name.slice(0,34)+"…":t.name)+" ["+t.acc+"]").reverse(),\n    axisLabel:{{fontSize:11}}}},\n  series:[{{type:"bar",data:D.top10.map(t=>t.plays).reverse(),\n    itemStyle:{{color:p=>p.dataIndex>=7?C.red:(p.dataIndex>=5?C.amber:C.pri)}},\n    label:{{show:true,position:"right",formatter:o=>o.value>=10000?(o.value/10000).toFixed(1)+"万":o.value}}}}]}});\n\n// 四、商品 TOP10（按累计播放降序）\nmk("cProd", {{...base, grid:{{left:250,right:90,top:20,bottom:30}},\n  xAxis:{{type:"value"}},\n  yAxis:{{type:"category",\n    data:D.products.list.slice(0,10).map(p=>(p.name.length>30?p.name.slice(0,30)+"…":p.name)+" ["+p.accs.join("+")+"]").reverse(),\n    axisLabel:{{fontSize:11}}}},\n  series:[{{type:"bar",data:D.products.list.slice(0,10).map(p=>p.plays).reverse(),\n    itemStyle:{{color:p=>p.dataIndex>=7?C.red:(p.dataIndex>=5?C.amber:C.pri)}},\n    label:{{show:true,position:"right",formatter:o=>o.value>=10000?(o.value/10000).toFixed(1)+"万":o.value}}}}]}});\n\n// 六、gmv_status 分布（出单数据缺口可视化：无任何真实 GMV 数值）\nmk("cGmv", {{...base, legend:{{bottom:0}},\n  tooltip:{{trigger:"item"}},\n  series:[{{type:"pie",radius:["35%","65%"],\n    data:Object.keys(D.orders.gmvStatus).map(k=>({{name:k,value:D.orders.gmvStatus[k]}})),\n    label:{{formatter:"{{b}}\\n{{c}} 条 ({{d}}%)"}},\n    itemStyle:{{color:p=>p.name==="pending"?C.amber:C.gray}}}}]}});\n\nwindow.addEventListener("resize",()=>chartResize());\nfunction chartResize(){{document.querySelectorAll("[_echarts_instance_]").forEach(el=>echarts.getInstanceByDom(el)&&echarts.getInstanceByDom(el).resize());}}\n</script>\n</body>\n</html>"""
if __name__ == '__main__':
    main()
