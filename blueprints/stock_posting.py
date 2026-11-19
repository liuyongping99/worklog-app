# -*- coding: utf-8 -*-
"""入库过账 / 红字冲销 服务层(阶段 3)。

上游口径:
    docs/superpowers/plans/2026-11-15-库存管理-多仓过账.md  §4.2 过账流程 / §3.9 A1~A8
    docs/superpowers/plans/2026-11-16-库存管理-开发计划.md      §阶段 3

为什么单独一个模块:
    过账要跨 **4 张表** 写在**同一个事务**里(stock_postings / stock_ledger /
    product_stock / stock_posting_skips)。HTTP 层(blueprints/stock.py)只该负责
    参数校验 + 返回 JSON,**不该**知道事务怎么开、幂等键怎么拼。
    拆成服务层之后,阶段 5(盘点)/ 阶段 7(归零)可以照抄同一个骨架。

⭐ **A2 事务边界**(上游 §3.9):
    整个过账 = **一个** `BEGIN IMMEDIATE` 事务 + **一个**连接。
    模型层方法一律传 `conn=conn`(传了就不 commit / 不 close)。
    中途任何异常 → `ROLLBACK` → 4 张表全回滚,不留孤儿流水。

⭐ **A3 幂等写入契约**:
    `stock_ledger` 一律**普通 INSERT**。禁用 `INSERT OR REPLACE` / `OR IGNORE` /
    `ON CONFLICT` —— 第 3 条守卫触发器会 RAISE(ABORT)。
    幂等靠「**先查后写**」:写前按 `uniq_key` 查 ledger,查到就跳过。

⭐ **三道幂等防线**(开发计划 3.4):
    ① `uniq_key` 明细行级(`inbound:{record_id}`)—— 正常路径靠它
    ② `BEGIN IMMEDIATE` —— 写锁串行化,两个并发请求不可能同时进临界区
    ③ `UNIQUE(uniq_key)` 兜底 —— 万一撞上,IntegrityError 转成「已过账」跳过

⭐ **A1 / A4 写入契约**:
    · `occurred_at` = **订单头 date**(业务日期),不是系统时间
      → 补录上周的入库单,流水就归到上周;期间汇总也按它切
    · `balance_after` = 本笔记账后该 (仓, SKU) 的余额快照
      → 流水链逐笔可勾稽,`tools/verify_stock_balance.py` 能验

⭐ **过账不改变启用状态**(上游 §4.2):
    `is_tracking=0` 的商品 —— 流水照记、内部累计照加、**对外显示仍是 0**,
    结果里单列 `not_tracking` 清单提示「去盘点 / 归零后生效」。
    ⚠️ 因此 `not_tracking` **不进** `stock_posting_skips`(那是「没入账」的待处理清单),
       它是「已入账但未启用」的提示清单 —— 两者语义完全不同,别混。
"""
import collections
import sqlite3

from models._db import get_db
from models.inventory import (ProductStock, StockLedger, StockPosting,
                               StockPostingSkip)

from blueprints.inventory_engine import (build_product_index, convert_to_base_unit,
                                         norm_unit, resolve_product,
                                         REASON_BAD_QTY, REASON_NO_CONVERSION,
                                         REASON_NO_PRODUCT)

# 余额容差 —— A1 明令:所有余额比较一律用容差,**禁止 `== 0`**。
# 原因:「支→码」走 36.5/46/92 这类非整系数,逐笔累加与 SUM(ledger) 不严格相等。
TOL = 1e-6

BIZ_INBOUND = 'inbound'


class PostingError(Exception):
    """带 HTTP 状态码的业务异常(蓝图直接转成 JSON 响应)。"""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


# ── 内部工具 ──────────────────────────────────────────────────────
def _tx_conn():
    """开一个「手动管事务」的连接。

    ⚠️ 必须把 `isolation_level` 设成 None:
       Python 的 sqlite3 默认是「隐式事务」模式 —— DML 语句会自动开/提交事务,
       这跟我们要的「显式 BEGIN IMMEDIATE」打架
       (典型报错:cannot start a transaction within a transaction)。
       设成 None 后,Python 不再自己管事务,全部由我们显式 BEGIN / COMMIT / ROLLBACK。
    """
    conn = get_db()
    conn.isolation_level = None
    return conn


def _parse_qty(raw):
    """数量文本 → float。非法 / 非正数返回 None(调用方记 bad_qty)。"""
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    try:
        qty = float(s)
    except (TypeError, ValueError):
        return None
    # NaN / inf 一律当非法(NaN 传进 SQL 会变成 NULL,比拒绝更糟)
    if qty != qty or qty in (float('inf'), float('-inf')):
        return None
    if qty <= 0:
        return None
    return qty


def _load_ref_data(conn):
    """一次把折算要用的三张表读进内存(算法全部复用 inventory_engine,别重写)。"""
    products = conn.execute(
        'SELECT product_id, product_code, product_name, specification, '
        'base_unit, aux_unit FROM product').fetchall()
    index = build_product_index(products)
    unit_map = collections.defaultdict(list)
    for u in conn.execute(
            'SELECT product_name, spec_keyword, yards_per_piece FROM product_units'):
        unit_map[u['product_name']].append(dict(u))
    piece_map = collections.defaultdict(list)
    for x in conn.execute(
            'SELECT product_name, spec_keyword, units_per_piece, target_unit '
            'FROM piece_conversions'):
        piece_map[x['product_name']].append(dict(x))
    return index, unit_map, piece_map


def _uniq_key(record_id):
    """明细行级幂等键。粒度 = 明细行,不是整单 —— 整单粒度没法做「部分过账」。"""
    return f'{BIZ_INBOUND}:{int(record_id)}'


def _skip_row(rec, reason_code, reason_text):
    """构造一条跳过明细(写库 + 返回前端展示用的形状)。"""
    return {
        'record_id': int(rec['id']),
        'product_name': rec.get('product_name') or '',
        'specification': rec.get('specification') or '',
        'raw_quantity': rec.get('quantity'),
        'raw_unit': rec.get('unit') or '',
        'reason_code': reason_code,
        'reason_text': reason_text,
        # 修好数据后能不能补过账 —— not_tracking 不可补(盘点值已含,补了重复计算)
        'resolvable': reason_code in StockPostingSkip.RESOLVABLE,
    }


# ── 过账 ──────────────────────────────────────────────────────────
def post_inbound_order(order_id, operator_id=None, operator_name=''):
    """入库单过账。返回结果 dict;业务失败抛 PostingError。

    闸门:
      ① 订单必须已锁定(is_locked=1)—— 锁了才代表核查完成
      ② 订单的 warehouse_id 必须指向真实存在的仓库
    """
    order_id = int(order_id)
    conn = _tx_conn()
    try:
        conn.execute('BEGIN IMMEDIATE')

        order_row = conn.execute(
            'SELECT * FROM inbound_orders WHERE id = ?', (order_id,)).fetchone()
        if not order_row:
            raise PostingError('入库单不存在', 404)
        order = dict(order_row)

        # 闸门①:先锁定才能过账
        if not order.get('is_locked'):
            raise PostingError('请先核查并锁定该订单,再执行过账', 409)

        # 闸门②:仓库必须有效(FK 只当兜底,应用层要给出人话)
        wid = int(order.get('warehouse_id') or 1)
        wh_row = conn.execute('SELECT * FROM warehouses WHERE id = ?', (wid,)).fetchone()
        if not wh_row:
            raise PostingError(f'仓库不存在(id={wid}),无法过账', 400)
        warehouse = dict(wh_row)

        # A4:业务日期取订单头 date —— 补录上周的单,流水就归上周
        occurred_at = (order.get('date') or '').strip()
        if not occurred_at:
            raise PostingError('入库单缺少业务日期,无法过账(流水需要 occurred_at)', 400)

        records = [dict(r) for r in conn.execute(
            'SELECT id, product_name, specification, quantity, unit, remark, sort_order '
            'FROM inbound_records WHERE order_pk = ? ORDER BY sort_order ASC, id ASC',
            (order_id,)).fetchall()]
        if not records:
            raise PostingError('该入库单没有明细行,无法过账', 400)

        index, unit_map, piece_map = _load_ref_data(conn)

        # ── 计划阶段:只读决策,不写任何东西 ───────────────────────
        # 为什么拆两段:「这一行能不能入账」和「入账」是两件事,
        # 拆开后才能在**知道有没有东西可写**之后再决定要不要建批次
        # (纯幂等空转不该留下一个 posted_count=0 的僵尸批次 —— 那会让 A6 解锁守卫误判)。
        plan = []
        for rec in records:
            rid = int(rec['id'])
            uniq_key = _uniq_key(rid)

            # ── 防线①:先查后写(已过账的行直接跳过,不重复入账)────────
            existing = StockLedger.get_by_uniq_key(uniq_key, conn=conn)
            if existing:
                plan.append({'rec': rec, 'kind': 'already', 'existing': existing})
                continue

            # ── 匹配 SKU ──────────────────────────────────────────
            product, how = resolve_product(
                rec.get('product_name'), rec.get('specification'), None, index)
            if product is None:
                plan.append({
                    'rec': rec, 'kind': 'skip',
                    'reason_code': REASON_NO_PRODUCT,
                    'reason_text': f'product 表匹配不到「{rec.get("product_name") or ""}」'
                                   f'/{rec.get("specification") or ""}这个品',
                })
                continue

            # ── 数量校验 ─────────────────────────────────────────
            qty = _parse_qty(rec.get('quantity'))
            if qty is None:
                plan.append({
                    'rec': rec, 'kind': 'skip',
                    'reason_code': REASON_BAD_QTY,
                    'reason_text': f'数量「{rec.get("quantity") or ""}」不是正数',
                })
                continue

            # ── 折算到基本单位(算法复用 inventory_engine)──────────
            ok, factor, reason_code, reason_text = convert_to_base_unit(
                qty, rec.get('unit'), product, unit_map, piece_map)
            if not ok:
                plan.append({'rec': rec, 'kind': 'skip',
                             'reason_code': reason_code, 'reason_text': reason_text})
                continue
            if not factor or factor <= TOL:
                # 0 系数会把库存清零 —— 当「无规则」处理(与引擎「0 视为无规则」口径一致)
                plan.append({
                    'rec': rec, 'kind': 'skip',
                    'reason_code': REASON_NO_CONVERSION,
                    'reason_text': '折算系数为 0,视为无换算规则',
                })
                continue

            plan.append({
                'rec': rec, 'kind': 'post',
                'product': product, 'how': how, 'qty': qty, 'factor': factor,
                'base_qty': qty * factor,
                'base_unit': norm_unit(product.get('base_unit')),
            })

        to_post = [p for p in plan if p['kind'] == 'post']
        to_skip = [p for p in plan if p['kind'] == 'skip']
        to_already = [p for p in plan if p['kind'] == 'already']

        already = [{
            'record_id': int(p['rec']['id']),
            'product_name': p['rec'].get('product_name') or '',
            'specification': p['rec'].get('specification') or '',
            'quantity': float(p['existing']['quantity']),
            'unit': p['existing']['unit'],
        } for p in to_already]

        # 本单已被冲销过的行(正向 uniq_key 仍在原流水里占着 → 不能再正向过账)
        reversed_keys = {r['uniq_key'] for r in conn.execute(
            'SELECT uniq_key FROM stock_ledger WHERE biz_type = ? AND order_pk = ? '
            'AND is_reversal = 1', (BIZ_INBOUND, order_id)).fetchall()}
        order_reversed = any(
            f'{_uniq_key(int(p["rec"]["id"]))}:rev' in reversed_keys for p in to_already)

        # ── 纯幂等空转:一笔新的都没有 → **不建批次** ──────────────
        # ⚠️ 为什么要特意不建:一个 posted_count=0 的空批次 status='posted',
        #    A6 解锁守卫若拿「批次状态」当判据,就会把「其实没入账」误判成「已过账」
        #    → 冲销之后仍然解不了锁(实测踩到)。批次语义应是「一次真正入账的动作」。
        if not to_post and not to_skip:
            conn.execute('COMMIT')
            return {
                'posting_id': None,
                'warehouse': warehouse,
                'occurred_at': occurred_at,
                'posted_count': 0,
                'posted_qty': 0.0,
                'posted': [],
                'skipped_count': 0,
                'skipped': [],
                'not_tracking': [],
                'already_posted_count': len(already),
                'already_posted': already,
                'order_reversed': order_reversed,
                'already_posted': bool(already),
            }

        # 本次过账要重新评估所有行 → 上一批的未解决跳过记录先标「已解决」,
        # 仍然失败的行会重新插一条新的未解决记录(避免同一行出现两条待办)。
        record_ids = [int(r['id']) for r in records]
        StockPostingSkip.mark_resolved_by_record(order_id, record_ids, conn=conn)

        posting_id = StockPosting.create(
            BIZ_INBOUND, order_id, wid,
            operator_id=operator_id, operator_name=operator_name, conn=conn)

        posted, skipped, not_tracking = [], [], []
        posted_qty = 0.0

        # ── 写入阶段 ──────────────────────────────────────────
        for p in to_post:
            rec = p['rec']
            rid = int(rec['id'])
            uniq_key = _uniq_key(rid)
            product = p['product']
            base_qty = p['base_qty']
            base_unit = p['base_unit']
            pid = int(product['product_id'])

            # ── 算 balance_after(本笔之后的余额)──────────────────
            cur = ProductStock.get(wid, pid, conn=conn)
            cur_qty = float(cur['quantity']) if cur else 0.0
            balance_after = cur_qty + base_qty

            # ── 写流水(先写流水,再动余额)────────────────────────
            # 顺序很重要:流水 INSERT 失败(uniq_key 撞车)时,余额还没动,
            # 跳过这一行不会留下「余额加了、流水没写」的脏账。
            try:
                StockLedger.create(
                    wid, BIZ_INBOUND, uniq_key, pid, base_qty, base_unit,
                    order_pk=order_id, record_id=rid,
                    raw_quantity=p['qty'], raw_unit=rec.get('unit') or '',
                    factor=p['factor'],
                    is_reversal=0, posting_id=posting_id, operator_id=operator_id,
                    memo='', occurred_at=occurred_at, balance_after=balance_after,
                    conn=conn)
            except sqlite3.IntegrityError:
                # ── 防线③:UNIQUE 兜底 ──────────────────────────
                # 撞的若是 uniq_key(并发重复提交 / 已被别的批次写过)→ 当「已过账」跳过;
                # 撞的若是别的完整性错误(外键等)→ 不是幂等问题,必须整体回滚。
                if StockLedger.get_by_uniq_key(uniq_key, conn=conn):
                    already.append({
                        'record_id': rid,
                        'product_name': rec.get('product_name') or '',
                        'specification': rec.get('specification') or '',
                        'quantity': base_qty, 'unit': base_unit,
                    })
                    continue
                raise

            # ── 更新内部累计(不改 is_tracking)────────────────────
            row = ProductStock.add_quantity(wid, pid, base_qty, conn=conn)

            posted.append({
                'record_id': rid,
                'product_id': pid,
                'product_name': product.get('product_name') or '',
                'specification': product.get('specification') or '',
                'raw_quantity': p['qty'],
                'raw_unit': rec.get('unit') or '',
                'quantity': base_qty,
                'unit': base_unit,
                'factor': p['factor'],
                'balance_after': float(row['quantity']),
                'match_rule': p['how'],
            })
            posted_qty += base_qty

            # 未启用:流水照记、内部累计照加,**对外显示仍是 0** → 单列提示清单
            if not row.get('is_tracking'):
                not_tracking.append({
                    'record_id': rid,
                    'product_id': pid,
                    'product_name': product.get('product_name') or '',
                    'specification': product.get('specification') or '',
                    'quantity': base_qty,
                    'unit': base_unit,
                    'internal_total': float(row['quantity']),
                })

        # 写跳过清单(待处理)
        for p in to_skip:
            rec = p['rec']
            StockPostingSkip.create(
                posting_id, order_id, int(rec['id']),
                rec.get('product_name') or '', rec.get('specification') or '',
                rec.get('quantity'), rec.get('unit') or '',
                p['reason_code'], p['reason_text'], conn=conn)

        StockPosting.update_stats(
            posting_id,
            posted_count=len(posted),
            posted_qty=posted_qty,
            skipped_count=len(to_skip),
            note=_posting_note(len(posted), len(to_skip), len(already)),
            conn=conn)

        conn.execute('COMMIT')
    except Exception:
        try:
            conn.execute('ROLLBACK')
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()

    skipped = [_skip_row(p['rec'], p['reason_code'], p['reason_text']) for p in to_skip]
    return {
        'posting_id': posting_id,
        'warehouse': warehouse,
        'occurred_at': occurred_at,
        'posted_count': len(posted),
        'posted_qty': posted_qty,
        'posted': posted,
        'skipped_count': len(skipped),
        'skipped': skipped,
        'not_tracking': not_tracking,
        'already_posted_count': len(already),
        'already_posted': already,
        'order_reversed': order_reversed,
        # 「本单已过账」= 这次一笔新的都没入账(重复点击 / 补过账时全是老行)
        'already_posted': len(posted) == 0 and len(already) > 0,
    }


def _posting_note(posted_n, skipped_n, already_n):
    """批次备注 —— 一行话说明这次过账干了啥。"""
    parts = [f'入账 {posted_n} 行']
    if already_n:
        parts.append(f'跳过已过账 {already_n} 行')
    if skipped_n:
        parts.append(f'待处理 {skipped_n} 行')
    return ' · '.join(parts)


# ── 红字冲销 ──────────────────────────────────────────────────────
def reverse_inbound_order(order_id, operator_id=None, operator_name=''):
    """红字冲销一次过账。

    ⚠️ **红字冲销,不删不改**:原流水一行不动,新写一条 quantity 取负的流水,
       `uniq_key` 加 `:rev` 后缀。原记录永远留着(审计链完整)。
    ⚠️ **冲销过的行不能再正向过账** —— 正向 uniq_key 仍被原流水占着
       (UNIQUE 约束 + 先查后写都会挡住),这是有意设计:改单据请改源头再重新建单。
    """
    order_id = int(order_id)
    conn = _tx_conn()
    try:
        conn.execute('BEGIN IMMEDIATE')

        order_row = conn.execute(
            'SELECT * FROM inbound_orders WHERE id = ?', (order_id,)).fetchone()
        if not order_row:
            raise PostingError('入库单不存在', 404)
        order = dict(order_row)
        wid = int(order.get('warehouse_id') or 1)

        originals = [dict(r) for r in conn.execute(
            "SELECT * FROM stock_ledger WHERE biz_type = ? AND order_pk = ? "
            'AND is_reversal = 0 ORDER BY id ASC',
            (BIZ_INBOUND, order_id)).fetchall()]
        if not originals:
            raise PostingError('该入库单没有可冲销的过账流水', 409)

        already_revs = conn.execute(
            "SELECT COUNT(*) n FROM stock_ledger WHERE biz_type = ? AND order_pk = ? "
            'AND is_reversal = 1',
            (BIZ_INBOUND, order_id)).fetchone()['n']
        if already_revs:
            raise PostingError('该入库单已冲销,不能重复冲销', 409)

        occurred_at = (order.get('date') or '').strip()
        reversed_rows = []
        reversed_qty = 0.0
        not_tracking = []

        for orig in originals:
            rev_key = f"{orig['uniq_key']}:rev"
            qty = -float(orig['quantity'])
            pid = int(orig['product_id'])
            cur = ProductStock.get(wid, pid, conn=conn)
            cur_qty = float(cur['quantity']) if cur else 0.0
            balance_after = cur_qty + qty

            try:
                StockLedger.create(
                    wid, BIZ_INBOUND, rev_key, pid, qty, orig['unit'],
                    order_pk=order_id, record_id=int(orig['record_id'] or 0),
                    raw_quantity=-float(orig['raw_quantity'] or 0),
                    raw_unit=orig['raw_unit'] or '',
                    factor=float(orig['factor'] or 1),
                    is_reversal=1, posting_id=orig['posting_id'],
                    operator_id=operator_id,
                    memo=f"红字冲销 {orig['uniq_key']}",
                    occurred_at=occurred_at or orig['occurred_at'] or '',
                    balance_after=balance_after,
                    conn=conn)
            except sqlite3.IntegrityError:
                if StockLedger.get_by_uniq_key(rev_key, conn=conn):
                    continue
                raise

            row = ProductStock.add_quantity(wid, pid, qty, conn=conn)
            reversed_rows.append({
                'record_id': int(orig['record_id'] or 0),
                'product_id': pid,
                'quantity': qty,
                'unit': orig['unit'],
                'balance_after': float(row['quantity']),
            })
            reversed_qty += qty
            if not row.get('is_tracking'):
                not_tracking.append({
                    'product_id': pid,
                    'quantity': qty,
                    'unit': orig['unit'],
                    'internal_total': float(row['quantity']),
                })

        # 批次状态:该单的批次全部标「已冲销」(批次本身不删)
        # ⚠️ 标**全部** 'posted' 批次,不只是挂了流水的那些 ——
        #   「补过账」会留下多个批次,漏标会让 A6 解锁守卫继续拦着。
        posting_ids = []
        for p in conn.execute(
                "SELECT id FROM stock_postings WHERE biz_type = ? AND order_pk = ? "
                "AND status = 'posted' ORDER BY id ASC",
                (BIZ_INBOUND, order_id)).fetchall():
            posting_ids.append(int(p['id']))
            StockPosting.mark_reversed(int(p['id']), conn=conn)

        conn.execute('COMMIT')
    except Exception:
        try:
            conn.execute('ROLLBACK')
        except sqlite3.Error:
            pass
        raise
    finally:
        conn.close()

    return {
        'reversed_count': len(reversed_rows),
        'reversed_qty': reversed_qty,
        'reversed': reversed_rows,
        'not_tracking': not_tracking,
        'posting_ids': posting_ids,
    }


# ── 过账状态查询 ──────────────────────────────────────────────────
def has_live_posting(biz_type, order_pk):
    """该单是否还有「**没被冲销**」的入账流水 —— A6 解锁守卫的判据。

    ⭐ **判据是流水,不是批次状态**(2026-11-17 实测踩坑):
    上游原文写的是「查 stock_postings,存在非冲销的 posted 批次 → 403」,
    但「重复点过账」会留下**空的**批次记录 →
    拿批次状态当判据,会把「其实一笔都没入账」误判成「已过账」,
    结果是**冲销之后仍然解不了锁**。
    流水才是「库存真的记了这笔账」的证据:
    有一行正向流水、且没有对应的 `:rev` 冲销流水 → 才算「还欠一笔」。

    返回 bool。
    """
    conn = _tx_conn()
    try:
        live = conn.execute(
            'SELECT uniq_key FROM stock_ledger WHERE biz_type = ? AND order_pk = ? '
            'AND is_reversal = 0', (biz_type, int(order_pk))).fetchall()
        if not live:
            return False
        revs = {r[0] for r in conn.execute(
            'SELECT uniq_key FROM stock_ledger WHERE biz_type = ? AND order_pk = ? '
            'AND is_reversal = 1', (biz_type, int(order_pk))).fetchall()}
        for r in live:
            if f'{r[0]}:rev' not in revs:
                return True
        return False
    finally:
        conn.close()


def get_stock_status(order_id):
    """某入库单的过账状态:已过账 / 跳过清单 / 未启用提示 / 能否过账。"""
    order_id = int(order_id)
    conn = _tx_conn()
    try:
        order_row = conn.execute(
            'SELECT * FROM inbound_orders WHERE id = ?', (order_id,)).fetchone()
        if not order_row:
            raise PostingError('入库单不存在', 404)
        order = dict(order_row)
        wid = int(order.get('warehouse_id') or 1)
        # 阶段 4:弹框要显示仓库名(不只是 id)
        wh_row = conn.execute(
            'SELECT id, code, name, is_default FROM warehouses WHERE id = ?', (wid,)
        ).fetchone()

        records = [dict(r) for r in conn.execute(
            'SELECT id, product_name, specification, quantity, unit '
            'FROM inbound_records WHERE order_pk = ? ORDER BY sort_order ASC, id ASC',
            (order_id,)).fetchall()]

        postings = [dict(r) for r in conn.execute(
            'SELECT * FROM stock_postings WHERE biz_type = ? AND order_pk = ? ORDER BY id ASC',
            (BIZ_INBOUND, order_id)).fetchall()]
        skips = [dict(r) for r in conn.execute(
            'SELECT * FROM stock_posting_skips WHERE order_pk = ? ORDER BY id ASC',
            (order_id,)).fetchall()]

        posted_keys = {}
        posted_rows = []
        for r in conn.execute(
                "SELECT record_id, uniq_key, product_id, quantity, unit, is_reversal "
                'FROM stock_ledger WHERE biz_type = ? AND order_pk = ? AND is_reversal = 0',
                (BIZ_INBOUND, order_id)).fetchall():
            posted_keys[int(r['record_id'])] = dict(r)
            posted_rows.append(dict(r))

        skip_by_record = {}
        for s in skips:
            if not s.get('resolved'):
                skip_by_record[int(s['record_id'])] = s

        items = []
        posted_n = skipped_n = pending_n = 0
        for rec in records:
            rid = int(rec['id'])
            led = posted_keys.get(rid)
            sk = skip_by_record.get(rid)
            if led:
                state = 'posted'
                posted_n += 1
            elif sk:
                state = 'skipped'
                skipped_n += 1
            else:
                state = 'pending'
                pending_n += 1
            items.append({
                'record_id': rid,
                'product_name': rec.get('product_name') or '',
                'specification': rec.get('specification') or '',
                'raw_quantity': rec.get('quantity'),
                'raw_unit': rec.get('unit') or '',
                'state': state,
                'posted_quantity': float(led['quantity']) if led else None,
                'posted_unit': led['unit'] if led else None,
                'reason_code': sk.get('reason_code') if sk else None,
                'reason_text': sk.get('reason_text') if sk else None,
                # 阶段 4:这条跳过行「补好数据后能不能再点过账补入」
                'resolvable': bool(sk) and sk.get('reason_code') in StockPostingSkip.RESOLVABLE,
            })

        # 未启用提示清单(已入账但 is_tracking=0)
        # ⚠️ 必须限定在「本单已入账的 SKU」里 —— 查全仓会把别的单的未启用商品也列进来,
        #    用户看到一堆不相关的行,还以为是自己这单没入账。
        untracked = []
        posted_pids = sorted({int(v['product_id']) for v in posted_rows}) \
            if posted_rows else []
        if posted_pids:
            marks = ','.join(['?'] * len(posted_pids))
            for r in conn.execute(
                    'SELECT ps.product_id, ps.quantity, ps.is_tracking, '
                    '       p.product_name, p.specification '
                    'FROM product_stock ps LEFT JOIN product p ON p.product_id = ps.product_id '
                    f'WHERE ps.warehouse_id = ? AND ps.is_tracking = 0 '
                    f'AND ps.product_id IN ({marks})',
                    [wid] + posted_pids).fetchall():
                untracked.append({
                    'product_id': int(r['product_id']),
                    'product_name': r['product_name'] or '',
                    'specification': r['specification'] or '',
                    'internal_total': float(r['quantity'] or 0),
                })

        reversed_batches = [p for p in postings if p.get('status') == 'reversed']

        # 「还欠一笔没冲销的」—— 跟 A6 守卫同一口径(流水为准,不看批次状态)
        rev_keys = {r[0] for r in conn.execute(
            'SELECT uniq_key FROM stock_ledger WHERE biz_type = ? AND order_pk = ? '
            'AND is_reversal = 1', (BIZ_INBOUND, order_id)).fetchall()}
        has_live = any(f'{r["uniq_key"]}:rev' not in rev_keys
                       for r in posted_rows)
    finally:
        conn.close()

    return {
        'order': {
            'id': order_id,
            'date': order.get('date'),
            'supplier': order.get('supplier'),
            'is_locked': int(order.get('is_locked') or 0),
            'warehouse_id': wid,
        },
        # 阶段 4:弹框要显示仓库名
        'warehouse': (dict(wh_row) if wh_row else
                      {'id': wid, 'code': '', 'name': '未知仓库', 'is_default': 0}),
        'postings': postings,
        'posting': postings[-1] if postings else None,
        'has_posting': bool(postings),
        'is_reversed': bool(reversed_batches),
        'items': items,
        'counts': {
            'total': len(records),
            'posted': posted_n,
            'skipped': skipped_n,
            'pending': pending_n,
        },
        'skipped': [s for s in skips if not s.get('resolved')],
        'skipped_all': skips,
        'not_tracking': untracked,
        # 能不能点「过账」:必须已锁定,且还有没入账的行
        'can_post': bool(order.get('is_locked')) and (pending_n > 0 or skipped_n > 0),
        # 能不能冲销:还有没被冲销的入账流水
        'can_reverse': bool(has_live),
    }


# ── 批量版(阶段 4 · 列表页徽章)─────────────────────────────────────
def get_stock_status_bulk(order_ids):
    """批量查多张入库单的过账状态(只读)—— 给列表页渲染徽章用。

    ⚠️ 为什么要批量版:
       列表页一个日期区间可能有几十张单,前端逐单 GET /stock-status
       会打出同样多个请求(还带 N 次建连)。这里一次连接查完,O(1) 次查询。

    返回 {order_id: {posted, skipped, pending, total,
                     has_posting, is_reversed, can_reverse}}

    口径与 `get_stock_status` 完全一致:
      · posted  = 有正向流水的明细行数
      · skipped = 未解决的跳过行数(resolved=0)
      · pending = 总行数 − posted − skipped
      · can_reverse = 还有**没被冲销**的正向流水(A6 守卫同一口径:流水为准)
    """
    ids = []
    for i in (order_ids or []):
        try:
            ids.append(int(i))
        except (TypeError, ValueError):
            continue
    ids = sorted(set(ids))
    if not ids:
        return {}

    marks = ','.join(['?'] * len(ids))
    conn = _tx_conn()
    try:
        totals = {oid: 0 for oid in ids}
        for r in conn.execute(
                f'SELECT order_pk, COUNT(*) AS n FROM inbound_records '
                f'WHERE order_pk IN ({marks}) GROUP BY order_pk', ids).fetchall():
            totals[int(r['order_pk'])] = int(r['n'])

        posted_recs = {oid: set() for oid in ids}
        posted_keys = {oid: set() for oid in ids}
        for r in conn.execute(
                f'SELECT order_pk, record_id, uniq_key FROM stock_ledger '
                f'WHERE biz_type = ? AND is_reversal = 0 AND order_pk IN ({marks})',
                [BIZ_INBOUND] + ids).fetchall():
            oid = int(r['order_pk'])
            posted_recs[oid].add(int(r['record_id']))
            posted_keys[oid].add(r['uniq_key'])

        rev_keys = {oid: set() for oid in ids}
        for r in conn.execute(
                f'SELECT order_pk, uniq_key FROM stock_ledger '
                f'WHERE biz_type = ? AND is_reversal = 1 AND order_pk IN ({marks})',
                [BIZ_INBOUND] + ids).fetchall():
            rev_keys[int(r['order_pk'])].add(r['uniq_key'])

        skip_recs = {oid: set() for oid in ids}
        for r in conn.execute(
                f'SELECT order_pk, record_id FROM stock_posting_skips '
                f'WHERE resolved = 0 AND order_pk IN ({marks})', ids).fetchall():
            skip_recs[int(r['order_pk'])].add(int(r['record_id']))

        batch_status = {oid: [] for oid in ids}
        for r in conn.execute(
                f'SELECT order_pk, status FROM stock_postings '
                f'WHERE biz_type = ? AND order_pk IN ({marks})',
                [BIZ_INBOUND] + ids).fetchall():
            batch_status[int(r['order_pk'])].append(r['status'])
    finally:
        conn.close()

    out = {}
    for oid in ids:
        posted_n = len(posted_recs[oid])
        # ⚠️ 「已入账」优先于「未入账」—— 与 `get_stock_status` 的逐行判定同一口径
        #   (那边是 `if led: posted / elif sk: skipped`,一行只归一类)。
        #   万一同一行既留了正向流水、又残留一条未解决的跳过行,这里也按「已入账」算,
        #   否则 skipped_n 会虚高 → 订单头徽章多报「N 行未入账」。
        skip_ids = {rid for rid in skip_recs[oid] if rid not in posted_recs[oid]}
        skipped_n = len(skip_ids)
        total = totals[oid]
        pending_n = max(0, total - posted_n - skipped_n)
        statuses = batch_status[oid]
        has_live = any(f'{k}:rev' not in rev_keys[oid] for k in posted_keys[oid])
        out[oid] = {
            'total': total,
            'posted': posted_n,
            'skipped': skipped_n,
            'pending': pending_n,
            'has_posting': bool(statuses),
            'is_reversed': any(s == 'reversed' for s in statuses),
            'can_reverse': bool(has_live),
        }
    return out
