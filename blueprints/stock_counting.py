# -*- coding: utf-8 -*-
"""盘点服务层 —— 阶段 5（对齐上游 §4.3 + §3.9 A5）。

盘点是**一项作业**：拿清单去数 → 分几批录 → 最后统一提交。
所以它是**单据**（stock_counts + stock_count_items），不是弹框。

本模块只做**业务**，HTTP 层在 blueprints/stock_count.py：

  · create_count()     建单(范围选择 → product_ids → 快照账面数 + 写死 as_of)
  · bulk_input()       批量粘贴回填(编号容错)
  · export_rows()      导出盘点表
  · preview_submit()   提交预览(⭐ A5:账面 → 实盘 → 期间变动 → 基准余额 → 调整额)
  · submit_count()     提交(单一 BEGIN IMMEDIATE 事务)

⭐ **A5 时点口径(本阶段最核心的一条)**：
    差额 = `actual_quantity` − **基准余额** − **期间变动**
    而不是「实盘 − 当前余额」。

    为什么:「现场数完货之后又入库」与「先入库再数货」,
    **同样两个数、顺序相反、结果相反**,只按当前余额算根本分不出来。
    基准时点 `as_of` 在**建单那一刻**写死(不能改 —— 改了就失去意义),
    提交时按它把流水切成两段:
      · 基准余额  = as_of **之前**的流水合计
      · 期间变动  = as_of **之后**的流水合计(逐笔列出,供人工确认)
    切分口径的坑见 `models/inventory.py::StockLedger.split_by_as_of` 的 docstring。

⭐ **A1 / A2 / A4 写入契约**:
    · A1:`balance_after` = 本笔之后的余额;余额比较一律用容差 1e-6
    · A2:整个提交 = **一个** `BEGIN IMMEDIATE` 事务 + **一个**连接,
          模型层方法一律传 `conn=conn`(传了就不 commit / 不 close)
    · A4:`occurred_at` = 盘点基准日(as_of 所在日),不是今天
    · A3:流水一律**普通 INSERT**(禁 OR REPLACE / OR IGNORE / ON CONFLICT)
"""
import csv
import io
import re
import sqlite3

from models import ProductStock, StockCount, StockCountItem, StockLedger
from models.products import ProductCategory

# 容差:余额比较一律用它,禁止 `== 0`(上游 §3.9 A1)
TOL = 1e-6

# 盘点模式
MODE_INVENTORY = 'inventory'   # 盘点(实盘数 = 真实库存)
MODE_ZERO = 'zero'             # 归零(实盘数 = 0,快捷入口)

# 建单范围
SCOPE_ALL = 'all'             # 全仓
SCOPE_CATEGORY = 'category'   # 按分类(含子树)
SCOPE_SEARCH = 'search'       # 按搜索结果
SCOPE_SELECTED = 'selected'   # 勾选商品

# CSV 公式注入防护(同 blueprints/stock.py)
_CSV_FORMULA_PREFIXES = '=+-@\t\r\n'


class CountError(Exception):
    """盘点业务异常(带 HTTP 状态码),蓝图直接转 JSON。"""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


def _now():
    from datetime import datetime
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _tx_conn():
    """开一个「手动管事务」的连接。

    ⚠️ 必须把 `isolation_level` 设成 None:Python 的 sqlite3 默认会隐式管事务,
       跟显式 `BEGIN IMMEDIATE` 打架(典型报错:cannot start a transaction
       within a transaction)。设成 None 后全部由我们显式 BEGIN / COMMIT / ROLLBACK。
    """
    from models._db import get_db
    conn = get_db()
    conn.isolation_level = None
    return conn


def _sanitize_csv_cell(value):
    """把可能被表格软件解释为公式的字符串强制标记为文本。"""
    if not isinstance(value, str) or not value:
        return value
    if value[0] in _CSV_FORMULA_PREFIXES:
        return "'" + value
    return value


# ── 建单范围 ──────────────────────────────────────────────────────
def resolve_scope(warehouse_id, scope, category_ids=None, q=None, product_ids=None):
    """建单范围 → product_id 列表。

    ⚠️ 口径与库存页**完全一致**(复用 `ProductStock.list_stock_view` 的筛选):
       盘点清单要跟用户在库存页看到的货对得上,否则「盘了个寂寞」。
    """
    scope = (scope or SCOPE_ALL).strip() or SCOPE_ALL
    if scope == SCOPE_SELECTED:
        ids = []
        for pid in (product_ids or []):
            try:
                ids.append(int(pid))
            except (TypeError, ValueError):
                continue
        if not ids:
            raise CountError('勾选商品范围必须至少选一个商品')
        return sorted(set(ids))

    if scope == SCOPE_CATEGORY:
        if not category_ids:
            raise CountError('按分类建单必须指定分类')
    elif scope == SCOPE_SEARCH:
        if not (q or '').strip():
            raise CountError('按搜索结果建单必须给搜索关键词')

    rows = ProductStock.list_stock_view(
        int(warehouse_id),
        only_tracking=None,
        category_ids=category_ids,
        q=(q or '').strip() or None,
        limit=100000, offset=0,
    )
    return [int(r['product_id']) for r in rows]


def create_count(warehouse_id, title, remark='', scope=SCOPE_ALL,
                 category_ids=None, q=None, product_ids=None,
                 operator_id=None, operator_name=''):
    """建盘点单。

    ⚠️ `as_of` 在这里**写死**(建单那一刻),之后不许改 ——
       它是 A5 的基准时点,改了「先数货还是先入库」的顺序就判不准了。
    """
    title = (title or '').strip()
    if not title:
        raise CountError('盘点单名称不能为空')
    if warehouse_id is None:
        raise CountError('请指定仓库')
    try:
        wid = int(warehouse_id)
    except (TypeError, ValueError):
        raise CountError('仓库 id 必须是整数')

    pids = resolve_scope(wid, scope, category_ids, q, product_ids)
    if not pids:
        raise CountError('该范围内没有商品可盘')

    as_of = _now()
    cid = StockCount.create(title, warehouse_id=wid, remark=remark,
                             created_by=operator_id, product_ids=pids, as_of=as_of)
    return {
        'count_id': cid,
        'as_of': as_of,
        'total_items': len(pids),
        'scope': scope,
        'title': title,
    }


# ── 批量粘贴回填 ──────────────────────────────────────────────────
def _norm_code(s):
    """商品编号归一:纯数字去掉前导 0(用户手抄容易漏 0)。

    `02050127` → `2050127`;`S4BROWSE` → 原样。
    """
    s = (s or '').strip()
    if s.isdigit():
        return str(int(s))
    return s


def _build_match_index(items):
    """盘点单所有行 → 各种写法的索引。

    一个商品可能被用户用好几种方式指认:
      · 商品编号 `02050127`(可能漏前导 0)
      · product_id(内部 id)
      · 品名 + 规格(中间空格分隔)
      · 只给品名(可能命中多行 → 明确报错,让人补规格)
    """
    idx = {'product_id': {}, 'code': {}, 'name_spec': {}, 'name': {}}
    for it in items:
        pid = int(it['product_id'])
        idx['product_id'].setdefault(pid, []).append(it)
        code = _norm_code(it.get('product_code'))
        if code:
            idx['code'].setdefault(code, []).append(it)
        name = (it.get('product_name') or '').strip()
        spec = (it.get('specification') or '').strip()
        if name and spec:
            idx['name_spec'].setdefault(f'{name} {spec}', []).append(it)
        if name:
            idx['name'].setdefault(name, []).append(it)
    return idx


def _match_one(ident, idx):
    """标识符 → 匹配到的行列表(可能 0 条 / 多条)。"""
    ident = (ident or '').strip()
    if not ident:
        return []
    # ① 内部 product_id
    if ident.isdigit():
        hit = idx['product_id'].get(int(ident))
        if hit:
            return hit
    # ② 商品编号(容忍漏前导 0)
    hit = idx['code'].get(_norm_code(ident))
    if hit:
        return hit
    # ③ 品名 + 规格
    hit = idx['name_spec'].get(ident)
    if hit:
        return hit
    # ④ 只给品名 → 可能多行,交给调用方处理
    return idx['name'].get(ident, [])


def bulk_input(count_id, text):
    """批量粘贴回填。

    接受的每行格式:`编号<TAB或空格>实盘数`
    ⚠️ **从右往左**取最后一个 token 当实盘数 —— 否则「白磅布 A1 20」
       里的空格会让「品名+规格」这种写法解析不出来。

    返回 {parsed, matched, unmatched, skipped, lines:[...], error}
    """
    count = StockCount.get_by_id(count_id)
    if not count:
        raise CountError('盘点单不存在', 404)
    if count['status'] != 'open':
        raise CountError('盘点单已提交,不能再改', 409)

    items = StockCountItem.list_by_count(count_id)
    idx = _build_match_index(items)

    lines = []
    matched = unmatched = skipped = 0
    pending = []          # [(product_id, qty)] —— 解析完**一次性**批量落库
    for lineno, raw in enumerate((text or '').splitlines(), start=1):
        raw = raw.strip()
        if not raw:
            continue
        parts = re.split(r'[\t ]+', raw)
        parts = [p for p in parts if p]
        if len(parts) < 2:
            skipped += 1
            lines.append({'line': lineno, 'text': raw, 'status': 'skipped',
                          'message': '格式不对(应为「编号 实盘数」)'})
            continue
        qty_raw = parts[-1]
        ident = ' '.join(parts[:-1]).strip()
        try:
            qty = float(qty_raw)
        except ValueError:
            skipped += 1
            lines.append({'line': lineno, 'text': raw, 'status': 'skipped',
                          'message': f'「{qty_raw}」不是数字'})
            continue
        if qty < 0:
            skipped += 1
            lines.append({'line': lineno, 'text': raw, 'status': 'skipped',
                          'message': '实盘数不能是负数'})
            continue

        hits = _match_one(ident, idx)
        if not hits:
            unmatched += 1
            lines.append({'line': lineno, 'text': raw, 'status': 'unmatched',
                          'message': f'找不到「{ident}」对应的商品'})
            continue
        if len(hits) > 1:
            unmatched += 1
            lines.append({'line': lineno, 'text': raw, 'status': 'unmatched',
                          'message': f'「{ident}」匹配到 {len(hits)} 行,请补规格'})
            continue

        it = hits[0]
        pending.append((int(it['product_id']), qty))
        matched += 1
        lines.append({'line': lineno, 'text': raw, 'status': 'matched',
                      'product_id': int(it['product_id']),
                      'product_name': it.get('product_name') or '',
                      'specification': it.get('specification') or '',
                      'quantity': qty})

    # ⭐ 一次性批量落库(逐行 set_actual 会开 N 个连接 + N 次 commit,
    #   粘贴上千行时慢到不可用)
    StockCountItem.set_actual_many(count_id, pending)

    stats = StockCount.refresh_stats(count_id)
    return {
        'parsed': matched + unmatched + skipped,
        'matched': matched,
        'unmatched': unmatched,
        'skipped': skipped,
        'lines': lines,
        'stats': stats,
    }


# ── 导出 ──────────────────────────────────────────────────────────
EXPORT_FIELDS = ('product_code', 'product_name', 'specification', 'base_unit',
                 'book_quantity', 'actual_quantity', 'diff')


def export_rows(count_id):
    """盘点表数据(导出与页面共用同一份)。"""
    items = StockCountItem.list_by_count(count_id)
    rows = []
    for it in items:
        book = float(it.get('book_quantity') or 0)
        actual = it.get('actual_quantity')
        diff = None if actual is None else float(actual) - book
        rows.append({
            'product_code': it.get('product_code') or '',
            'product_name': it.get('product_name') or '',
            'specification': it.get('specification') or '',
            'base_unit': it.get('base_unit') or '',
            'book_quantity': book,
            'actual_quantity': actual,
            'diff': diff,
        })
    return rows


def export_csv(count_id):
    """导出盘点表 CSV(编号/品名/规格/单位/账面数/实盘数/差异)。"""
    count = StockCount.get_by_id(count_id)
    if not count:
        raise CountError('盘点单不存在', 404)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(['商品编号', '品名', '规格', '单位', '账面数', '实盘数', '差异'])
    for r in export_rows(count_id):
        writer.writerow([
            _sanitize_csv_cell(r['product_code']),
            _sanitize_csv_cell(r['product_name']),
            _sanitize_csv_cell(r['specification']),
            _sanitize_csv_cell(r['base_unit']),
            r['book_quantity'],
            '' if r['actual_quantity'] is None else r['actual_quantity'],
            '' if r['diff'] is None else r['diff'],
        ])
    return buf.getvalue()


# ── 提交预览(⭐ A5)────────────────────────────────────────────────
def _diff_rows(count_id, mode, conn=None):
    """算出每个已录数行的差额明细(A5 核心)。

    差额 = `actual_quantity` − **基准余额** − **期间变动**
    """
    count = StockCount.get_by_id(count_id)
    if not count:
        raise CountError('盘点单不存在', 404)
    wid = int(count['warehouse_id'])
    as_of = count['as_of'] or ''
    items = StockCountItem.list_by_count(count_id)

    rows = []
    for it in items:
        if it.get('actual_quantity') is None:
            continue          # ⭐ 未录的行不参与提交(判据 3)
        pid = int(it['product_id'])
        actual = 0.0 if mode == MODE_ZERO else float(it['actual_quantity'])
        split = StockLedger.split_by_as_of(wid, pid, as_of, conn=conn)
        base = float(split['base_qty'])
        period = float(split['period_qty'])
        delta = actual - base - period
        cur = ProductStock.get(wid, pid, conn=conn)
        rows.append({
            'product_id': pid,
            'product_code': it.get('product_code') or '',
            'product_name': it.get('product_name') or '',
            'specification': it.get('specification') or '',
            'base_unit': it.get('base_unit') or '',
            'book_quantity': float(it.get('book_quantity') or 0),
            'actual_quantity': actual,
            'base_qty': base,
            'period_qty': period,
            'period_rows': split['period_rows'],
            'current_qty': float(cur['quantity']) if cur else 0.0,
            'delta': delta,
            'need_adjust': abs(delta) > TOL,
        })
    return count, rows


def preview_submit(count_id, mode=MODE_INVENTORY):
    """提交预览 —— 弹框要显示的「账面 → 实盘 → 期间变动 → 基准余额 → 调整额」。

    ⭐ 这是**只读**的,提交前先给用户看清楚要写多少条 adjust 流水。
    """
    mode = MODE_ZERO if mode == MODE_ZERO else MODE_INVENTORY
    count, rows = _diff_rows(count_id, mode)
    adjusted = sum(1 for r in rows if r['need_adjust'])
    period_changes = sum(len(r['period_rows']) for r in rows)
    return {
        'count_id': int(count_id),
        'title': count['title'],
        'mode': mode,
        'as_of': count['as_of'],
        'warehouse_id': int(count['warehouse_id']),
        'rows': rows,
        'summary': {
            'counted': len(rows),
            'adjusted': adjusted,
            'unchanged': len(rows) - adjusted,
            'total_period_changes': period_changes,
            'total_items': int(count['total_items'] or 0),
            'unfilled': int(count['total_items'] or 0) - len(rows),
        },
    }


# ── 提交 ──────────────────────────────────────────────────────────
def _adjust_memo(actual, base, period, delta, mode, reason=''):
    """adjust 流水的备注 —— 写明这四个数,事后能看懂这笔调整是怎么算出来的。

    格式:`实际 X / 基准 Y / 期间 Z / 调整 D`(上游 §4.3)
    """
    bits = [f'实际 {actual:g}', f'基准 {base:g}', f'期间 {period:g}', f'调整 {delta:g}']
    memo = ' / '.join(bits)
    if mode == MODE_ZERO:
        reason = (reason or '').strip()
        memo = f'归零：{reason or "未填原因"} | {memo}' if reason else memo
    return memo


def submit_count(count_id, mode=MODE_INVENTORY, operator_id=None, operator_name='',
                 reason=''):
    """提交盘点单 —— ⭐ A1 / A2 / A4 / A5 / A7 全在这一处落地。

    · A2:整个提交 = 一个 `BEGIN IMMEDIATE` 事务 + 一个连接
    · A1:adjust 流水写 `balance_after` = 本笔之后的余额
    · A4:`occurred_at` = 盘点基准日(as_of 所在日),不是今天
    · A5:差额 = 实盘 − 基准余额 − 期间变动
    · A8:启用**只**走这里(盘点 / 归零),不提供「只置 flag」的快捷启用
    """
    mode = MODE_ZERO if mode == MODE_ZERO else MODE_INVENTORY
    reason = (reason or '').strip()
    if mode == MODE_ZERO and not reason:
        raise CountError('归零必须填原因(这批货为什么没了 / 清仓了)')

    conn = _tx_conn()
    try:
        conn.execute('BEGIN IMMEDIATE')

        row = conn.execute('SELECT * FROM stock_counts WHERE id = ?',
                            (int(count_id),)).fetchone()
        if not row:
            raise CountError('盘点单不存在', 404)
        count = dict(row)
        if count['status'] != 'open':
            raise CountError('盘点单已提交,不能重复提交', 409)

        wid = int(count['warehouse_id'])
        as_of = count['as_of'] or ''
        occurred_at = as_of[:10] or _now()[:10]

        # ── 计划阶段:算清每行的差额(还在事务里,但还没写任何东西)──
        _, rows = _diff_rows(count_id, mode, conn=conn)

        adjusted = 0
        unchanged = 0
        for r in rows:
            pid = r['product_id']
            delta = float(r['delta'])
            if r['need_adjust']:
                # ── 幂等防线:同一张盘点单的同一个 SKU 只能有一条 adjust 流水 ──
                uniq = f'adjust:count:{int(count_id)}:{pid}'
                if StockLedger.exists(uniq, conn=conn):
                    raise CountError(
                        f'该盘点单已写过 {pid} 的调整流水,不能重复提交', 409)
                # ── 算 balance_after:本笔之后的余额(A1)──
                cur = ProductStock.get(wid, pid, conn=conn)
                cur_qty = float(cur['quantity']) if cur else 0.0
                balance_after = cur_qty + delta
                memo = _adjust_memo(r['actual_quantity'], r['base_qty'],
                                    r['period_qty'], delta, mode, reason)
                # ── 写流水(先写流水,再动余额;A3:一律普通 INSERT)──
                StockLedger.create(
                    wid, 'adjust', uniq,
                    pid, delta, r['base_unit'] or '码',
                    order_pk=int(count_id), record_id=0,
                    raw_quantity=r['actual_quantity'], raw_unit=r['base_unit'] or '码',
                    factor=1, is_reversal=0, posting_id=None,
                    operator_id=operator_id, memo=memo,
                    occurred_at=occurred_at, balance_after=balance_after,
                    conn=conn)
                # ── 动余额 ──
                ProductStock.add_quantity(wid, pid, delta, conn=conn)
                adjusted += 1
            else:
                unchanged += 1
            # ── A8:启用只走盘点 / 归零(不论有没有差额都启用)──
            ProductStock.set_tracking(wid, pid, True, conn=conn)

        # ── 关单 ──
        cur = conn.execute(
            "UPDATE stock_counts SET status = 'closed', closed_at = ? "
            "WHERE id = ? AND status = 'open'", (_now(), int(count_id)))
        if cur.rowcount == 0:
            raise CountError('盘点单已提交,不能重复提交', 409)

        conn.execute('COMMIT')
    except Exception:
        try:
            conn.execute('ROLLBACK')
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()

    stats = StockCount.refresh_stats(count_id)
    return {
        'count_id': int(count_id),
        'mode': mode,
        'adjusted': adjusted,
        'unchanged': unchanged,
        'updated': adjusted + unchanged,
        'as_of': as_of,
        'occurred_at': occurred_at,
        'stats': stats,
    }
