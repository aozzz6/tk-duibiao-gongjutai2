"""groups.py — 对标账号分组（数据层，单一事实源）

模型（data/account_groups.json，version 2）：
{
  "version": 2,
  "categories": [
    {"id": "own",   "name": "自有账号", "desc": "本公司自营账号…",  "builtin": true, "order": 10},
    {"id": "bench", "name": "对标账号", "desc": "外部同行/竞品账号…", "builtin": true, "order": 20},
    {"id": "inbox", "name": "未分类",   "desc": "新账号暂存区…",     "builtin": true, "order": 90},
    {"id": "<uuid>", "name": "自建大类", "desc": "",                "builtin": false, "order": 100}
  ],
  "groups":  [{"id": "default", "name": "默认分组", "category_id": "inbox",
               "builtin": true, "created_at": ...}],
  "members": [{"account": "gutpickde", "group_id": "bench", "joined_at": ...}]
}

层级（三级，逐级归属，账号不直接挂大类）：
  大分类 category  ←  分组 group  ←  账号 account
- 一个分组属于恰好一个大分类；一个账号只归属一个分组，因此也只属于一个大分类。
- 大分类只约束「归档口径」，不改变账号本身的采集/转写/台账行为。

语义：
- 不在 members 里的账号视为「默认分组」成员；默认分组固定属于内置大类「未分类」（inbox）。
- 「自有账号」(own) 用来归集本公司自营账号，只容纳人工显式迁入的账号。
- 删除分组 → 成员自动回到默认分组（账号本身永远不动 accounts.json）。
- 内置分组「默认分组」不可删除、不可重命名；内置大分类不可删除、不可改名（描述可改）。
- 删除大分类要求其下已无分组（避免误删整类）；非内置大分类可自由增删改，用于横向扩展。
"""
from __future__ import annotations
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import threading
import time
import uuid
from pathlib import Path
import config
GROUPS_FILE = Path(__file__).resolve().parent / 'data' / 'account_groups.json'
DEFAULT_ID = 'default'
DEFAULT_CATEGORY_ID = 'inbox'
CATEGORY_OWN = 'own'
CATEGORY_BENCH = 'bench'
SCHEMA_VERSION = 2
_lock = threading.Lock()
CATEGORY_SEEDS = [{'id': CATEGORY_OWN, 'name': '自有账号', 'desc': '本公司自营账号，仅归集自有内容', 'builtin': True, 'order': 10}, {'id': CATEGORY_BENCH, 'name': '对标账号', 'desc': '外部同行/竞品账号，用于对标分析', 'builtin': True, 'order': 20}, {'id': DEFAULT_CATEGORY_ID, 'name': '未分类', 'desc': '尚未归类的账号暂存区', 'builtin': True, 'order': 90}]
BUILTIN_CATEGORY_IDS = {c['id'] for c in CATEGORY_SEEDS}

class GroupStorageError(RuntimeError):
    """分组存储不可读时停止操作，避免覆盖原数据。"""

def _now() -> float:
    return time.time()

def _load() -> dict:
    import json
    if GROUPS_FILE.exists():
        try:
            d = json.loads(GROUPS_FILE.read_text(encoding='utf-8'))
            if not isinstance(d, dict) or not isinstance(d.get('groups'), list) or (not isinstance(d.get('members'), list)):
                raise ValueError('groups/members 必须为数组')
            if any((not isinstance(g, dict) or not isinstance(g.get('id'), str) or (not isinstance(g.get('name'), str)) for g in d['groups'])):
                raise ValueError('分组记录格式无效')
            if any((not isinstance(m, dict) or not isinstance(m.get('account'), str) or (not isinstance(m.get('group_id'), str)) for m in d['members'])):
                raise ValueError('成员记录格式无效')
            cats = d.get('categories')
            if cats is not None:
                if not isinstance(cats, list):
                    raise ValueError('categories 必须为数组')
                if any((not isinstance(c, dict) or not isinstance(c.get('id'), str) or (not isinstance(c.get('name'), str)) for c in cats)):
                    raise ValueError('大分类记录格式无效')
            return d
        except (OSError, ValueError) as e:
            raise GroupStorageError(f'分组文件读取失败，原文件已保留：{e}') from e
    return {'version': SCHEMA_VERSION, 'categories': [], 'groups': [], 'members': []}

def _save(d: dict) -> None:
    import json
    GROUPS_FILE.parent.mkdir(parents=True, exist_ok=True)
    d['version'] = SCHEMA_VERSION
    tmp = GROUPS_FILE.with_suffix('.json.tmp')
    tmp.write_text(json.dumps(d, ensure_ascii=False, indent=1), encoding='utf-8')
    tmp.replace(GROUPS_FILE)

def _sorted_categories(d: dict) -> list[dict]:
    return sorted(d.get('categories') or [], key=lambda c: (c.get('order', 100), c.get('name', '')))

def _category_of(d: dict, gid: str) -> str:
    """分组所属大类；分组缺失或大类失效时回落到「未分类」。"""
    ids = {c.get('id') for c in d.get('categories') or []}
    g = next((x for x in d.get('groups', []) if x.get('id') == gid), None)
    cid = (g or {}).get('category_id')
    return cid if cid in ids else DEFAULT_CATEGORY_ID

def _ensure_seed(d: dict) -> dict:
    """保证内置大分类 / 默认分组 / 「对标账号」分组存在（首次自动创建），并迁移旧数据。"""
    cats = d.get('categories') or []
    have = {c.get('id') for c in cats}
    for seed in CATEGORY_SEEDS:
        if seed['id'] not in have:
            cats.append(dict(seed))
    d['categories'] = _sorted_categories({'categories': cats})
    groups = d.get('groups') or []
    ids = {g.get('id') for g in groups}
    if DEFAULT_ID not in ids:
        groups.insert(0, {'id': DEFAULT_ID, 'name': '默认分组', 'builtin': True, 'created_at': _now()})
    if not GROUPS_FILE.exists() and 'bench' not in ids and (not any((g.get('name') == '对标账号' for g in groups))):
        groups.append({'id': 'bench', 'name': '对标账号', 'builtin': False, 'created_at': _now()})
    valid = {c.get('id') for c in d['categories']}
    for g in groups:
        cid = g.get('category_id')
        if cid not in valid:
            if g.get('id') == DEFAULT_ID:
                g['category_id'] = DEFAULT_CATEGORY_ID
            elif g.get('id') == 'bench' and g.get('name') == '对标账号':
                g['category_id'] = CATEGORY_BENCH
            else:
                g['category_id'] = CATEGORY_OWN
    d['groups'] = groups
    return d

def _group_of(d: dict, account: str) -> str:
    for m in d.get('members', []):
        if m.get('account') == account:
            gid = m.get('group_id') or DEFAULT_ID
            return gid if any((g.get('id') == gid for g in d.get('groups', []))) else DEFAULT_ID
    return DEFAULT_ID

def _account_exists(account: str) -> bool:
    names = {a.get('account') for a in config.load_accounts(include_inactive=True).get('accounts', [])}
    return account in names

def _category_by_id(d: dict, cid: str) -> dict:
    c = next((x for x in d.get('categories') or [] if x.get('id') == cid), None)
    if c is None:
        raise KeyError(f'大分类不存在：{cid}')
    return c

def list_groups() -> dict:
    with _lock:
        d = _load()
        have_cats = {x.get('id') for x in d.get('categories') or []}
        needs_seed = not any((g.get('id') == DEFAULT_ID for g in d['groups'])) or any((c['id'] not in have_cats for c in CATEGORY_SEEDS)) or any((not g.get('category_id') for g in d['groups']))
        d = _ensure_seed(d)
        if needs_seed:
            _save(d)
        all_accounts = [a.get('account') for a in config.load_accounts(include_inactive=True).get('accounts', []) if a.get('account')]
        joined = {m.get('account'): m for m in d.get('members', [])}
        counts: dict = {}
        members_by: dict = {}
        for acc in all_accounts:
            gid = _group_of(d, acc)
            counts[gid] = counts.get(gid, 0) + 1
            members_by.setdefault(gid, []).append({'account': acc, 'joined_at': (joined.get(acc) or {}).get('joined_at')})
        cat_name = {c['id']: c['name'] for c in d['categories']}
        groups = [{**g, 'category_id': _category_of(d, g['id']), 'category_name': cat_name.get(_category_of(d, g['id']), ''), 'count': counts.get(g['id'], 0), 'members': sorted(members_by.get(g['id'], []), key=lambda x: x['account'])} for g in d['groups']]
        categories = []
        for c in _sorted_categories(d):
            gs = [g for g in groups if g['category_id'] == c['id']]
            categories.append({**c, 'group_count': len(gs), 'account_count': sum((g['count'] for g in gs)), 'builtin': bool(c.get('builtin'))})
        return {'version': SCHEMA_VERSION, 'categories': categories, 'groups': groups}

def members_of(gid: str) -> list[str]:
    return [m['account'] for g in list_groups()['groups'] if g['id'] == gid for m in g['members']]

def accounts_of_category(cid: str) -> list[str]:
    """某大类下的全部账号（跨分组）。"""
    return [m['account'] for g in list_groups()['groups'] if g.get('category_id') == cid for m in g['members']]

def _clean_category_name(name: str) -> str:
    if not isinstance(name, str):
        raise ValueError('大分类名称必须是字符串')
    name = (name or '').strip()
    if not name:
        raise ValueError('大分类名称不能为空')
    if len(name) > 12:
        raise ValueError('大分类名称过长（≤12 字符）')
    return name

def create_category(name: str, desc: str='') -> dict:
    name = _clean_category_name(name)
    desc = (desc or '').strip() if isinstance(desc, str) else ''
    if len(desc) > 60:
        raise ValueError('大分类说明过长（≤60 字符）')
    with _lock:
        d = _ensure_seed(_load())
        if any((c['name'] == name for c in d['categories'])):
            raise ValueError(f'大分类「{name}」已存在')
        inbox_order = next((c.get('order', 90) for c in d['categories'] if c.get('id') == DEFAULT_CATEGORY_ID), 90)
        base = max([c.get('order', 0) for c in d['categories'] if c.get('id') != DEFAULT_CATEGORY_ID] or [0]) + 10
        order = min(base, inbox_order - 1)
        c = {'id': uuid.uuid4().hex[:10], 'name': name, 'desc': desc, 'builtin': False, 'order': order}
        d['categories'].append(c)
        d['categories'] = _sorted_categories(d)
        _save(d)
        return c

def update_category(cid: str, name: str | None=None, desc: str | None=None) -> dict:
    with _lock:
        d = _ensure_seed(_load())
        c = _category_by_id(d, cid)
        if name is not None:
            name = _clean_category_name(name)
            if c.get('builtin'):
                raise ValueError('内置大分类不可改名')
            if any((x['name'] == name and x['id'] != cid for x in d['categories'])):
                raise ValueError(f'大分类「{name}」已存在')
            c['name'] = name
        if desc is not None:
            if not isinstance(desc, str):
                raise ValueError('说明必须是字符串')
            desc = desc.strip()
            if len(desc) > 60:
                raise ValueError('大分类说明过长（≤60 字符）')
            c['desc'] = desc
        _save(d)
        return c

def delete_category(cid: str) -> dict:
    """删除大分类：要求其下无分组（分组是账号归属载体，不能悬空）。"""
    with _lock:
        d = _ensure_seed(_load())
        c = _category_by_id(d, cid)
        if c.get('builtin') or cid in BUILTIN_CATEGORY_IDS:
            raise ValueError('内置大分类不可删除')
        gs = [g['name'] for g in d['groups'] if g.get('category_id') == cid]
        if gs:
            raise ValueError(f"该大分类下还有 {len(gs)} 个分组（{'、'.join(gs[:3])}…），请先移出")
        d['categories'] = [x for x in d['categories'] if x['id'] != cid]
        _save(d)
        return {'id': cid}

def create_group(name: str, category_id: str | None=None) -> dict:
    if not isinstance(name, str):
        raise ValueError('分组名称必须是字符串')
    name = (name or '').strip()
    if not name:
        raise ValueError('分组名称不能为空')
    if len(name) > 20:
        raise ValueError('分组名称过长（≤20 字符）')
    with _lock:
        d = _ensure_seed(_load())
        if any((g['name'] == name for g in d['groups'])):
            raise ValueError(f'分组「{name}」已存在')
        cid = category_id or DEFAULT_CATEGORY_ID
        _category_by_id(d, cid)
        g = {'id': uuid.uuid4().hex[:10], 'name': name, 'builtin': False, 'category_id': cid, 'created_at': _now()}
        d['groups'].append(g)
        _save(d)
        return g

def update_group(gid: str, name: str | None=None, category_id: str | None=None) -> dict:
    """改名 + 改所属大类（两个动作可单发或同发）。"""
    if name is not None:
        if not isinstance(name, str):
            raise ValueError('分组名称必须是字符串')
        name = name.strip()
        if not name:
            raise ValueError('分组名称不能为空')
        if len(name) > 20:
            raise ValueError('分组名称过长（≤20 字符）')
    with _lock:
        d = _ensure_seed(_load())
        g = next((x for x in d['groups'] if x['id'] == gid), None)
        if g is None:
            raise KeyError(f'分组不存在：{gid}')
        if name is not None:
            if g.get('builtin'):
                raise ValueError('内置分组不可重命名')
            if any((x['name'] == name and x['id'] != gid for x in d['groups'])):
                raise ValueError(f'分组「{name}」已存在')
            g['name'] = name
        if category_id is not None:
            if g.get('builtin') and gid == DEFAULT_ID:
                raise ValueError('默认分组固定属于「未分类」，不可改大类')
            _category_by_id(d, category_id)
            g['category_id'] = category_id
        _save(d)
        return {**g, 'category_name': next((c['name'] for c in d['categories'] if c['id'] == g.get('category_id')), '')}

def rename_group(gid: str, name: str) -> dict:
    """兼容旧调用：只改名。"""
    return update_group(gid, name=name)

def set_group_category(gid: str, category_id: str) -> dict:
    return update_group(gid, category_id=category_id)

def delete_group(gid: str) -> dict:
    """删除分组：成员自动回默认分组（账号绝不被删除）。"""
    if gid == DEFAULT_ID:
        raise ValueError('默认分组不可删除')
    with _lock:
        d = _ensure_seed(_load())
        g = next((x for x in d['groups'] if x['id'] == gid), None)
        if g is None:
            raise KeyError(f'分组不存在：{gid}')
        moved = [m['account'] for m in d.get('members', []) if _group_of(d, m.get('account', '')) == gid]
        d['members'] = [m for m in d.get('members', []) if _group_of(d, m.get('account', '')) != gid]
        d['groups'] = [x for x in d['groups'] if x['id'] != gid]
        _save(d)
        return {'moved': moved}

def add_member(gid: str, account: str) -> dict:
    if not isinstance(account, str):
        raise ValueError('账号名必须是字符串')
    account = (account or '').strip()
    if not _account_exists(account):
        raise KeyError(f'账号不存在：{account}')
    with _lock:
        d = _ensure_seed(_load())
        if not any((g['id'] == gid for g in d['groups'])):
            raise KeyError(f'分组不存在：{gid}')
        cur = _group_of(d, account)
        if cur == gid:
            raise ValueError(f'账号 {account} 已在该分组中')
        d['members'] = [m for m in d.get('members', []) if m.get('account') != account]
        d['members'].append({'account': account, 'group_id': gid, 'joined_at': _now()})
        _save(d)
        return {'account': account, 'group_id': gid}

def transfer_members(gid: str, accounts) -> dict:
    """批量迁移账号到目标分组（一次加锁、一次落盘）。

    与 add_member 的差别：不因单条失败中断，逐条返回结果；
    「默认分组」的账号没有 members 记录，也能正确迁移。
    返回 {"group_id", "category_id", "moved": [...], "results": [{account,status,from,reason}]}
    status: moved / same / not_found / invalid / duplicated
    """
    if not isinstance(accounts, list):
        raise ValueError('accounts 必须是数组')
    results, moved = ([], [])
    with _lock:
        d = _ensure_seed(_load())
        if not any((g['id'] == gid for g in d['groups'])):
            raise KeyError(f'分组不存在：{gid}')
        members = list(d.get('members', []))
        seen = set()
        for raw in accounts:
            acc = raw.strip() if isinstance(raw, str) else ''
            if not acc:
                results.append({'account': str(raw), 'status': 'invalid', 'reason': '账号名为空'})
                continue
            if acc in seen:
                results.append({'account': acc, 'status': 'duplicated', 'reason': '清单内重复'})
                continue
            seen.add(acc)
            if not _account_exists(acc):
                results.append({'account': acc, 'status': 'not_found', 'reason': 'accounts.json 中不存在'})
                continue
            cur = _group_of(d, acc)
            if cur == gid:
                results.append({'account': acc, 'status': 'same', 'from': cur, 'reason': '已在该分组'})
                continue
            members = [m for m in members if m.get('account') != acc]
            members.append({'account': acc, 'group_id': gid, 'joined_at': _now()})
            results.append({'account': acc, 'status': 'moved', 'from': cur})
            moved.append({'account': acc, 'from': cur})
        if moved:
            d['members'] = members
            _save(d)
        cid = _category_of(d, gid)
    return {'group_id': gid, 'category_id': cid, 'moved': moved, 'results': results}

def forget_account(account: str) -> bool:
    """账号被删除后清掉它的分组记录（幂等；返回是否真的改过文件）。

    与 remove_member 的区别：不校验"是否在组里"、不需要 gid —— 删除账号时
    accounts.json 已没有这条记录，list_groups 也早就不显示它，但 members
    里的历史行会残留；这里一次清干净，避免重新创建同名账号时"凭空回到旧组"。
    """
    account = (account or '').strip()
    if not account:
        return False
    with _lock:
        d = _ensure_seed(_load())
        before = len(d.get('members', []))
        d['members'] = [m for m in d.get('members', []) if m.get('account') != account]
        if len(d.get('members', [])) == before:
            return False
        _save(d)
        return True

def remove_member(gid: str, account: str) -> dict:
    """移出分组 = 回到默认分组（账号保留）。"""
    with _lock:
        d = _ensure_seed(_load())
        if not any((g['id'] == gid for g in d['groups'])):
            raise KeyError(f'分组不存在：{gid}')
        before = len(d.get('members', []))
        d['members'] = [m for m in d.get('members', []) if not (m.get('account') == account and _group_of(d, account) == gid)]
        if len(d.get('members', [])) == before:
            raise ValueError(f'账号 {account} 不在该分组中')
        _save(d)
        return {'account': account, 'group_id': DEFAULT_ID}
