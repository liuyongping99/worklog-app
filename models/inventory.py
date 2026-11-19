# -*- coding: utf-8 -*-
"""库存管理的数据模型(多仓 + 入库过账)。

2026-11-16 阶段 1 产出。设计文档:
    docs/superpowers/plans/2026-11-15-库存管理-多仓过账.md  §三 数据模型
    docs/superpowers/plans/2026-11-16-库存管理-开发计划.md      §阶段 1

七张表(DDL 在 models/_init.py):
    warehouses            仓库(当前只有 'FR' 芙蓉仓)
    product_stock         当前库存(仓库 × SKU),UNIQUE(warehouse_id, product_id)
    stock_ledger          库存流水,**一切的唯一来源**,UNIQUE(uniq_key)
    stock_postings        过账批次(一次点「过账」= 一个批次)
    stock_posting_skips   跳过明细(待处理清单)
    stock_counts          盘点单(盘点是一项作业 → 要单据,不是弹框)
    stock_count_items     盘点明细(UNIQUE(count_id, product_id))

几条贯穿全局的约定(改代码前先读):
  · **期初数不导入** —— product_stock 从空表起步,库存靠「盘点」起算。
    product.stock_quantity 是建档时从目录图上抄的期初数,不是实时库存。
  · **is_tracking 区分「真 0」和「未启用」** —— 默认 0(未启用,对外按 0 显示),
    只有盘点 / 归零才置 1。**过账绝不自动启用**。
    未启用期间:流水照记、内部累计照加、对外显示 0。
  · **幂等靠 stock_ledger.UNIQUE(uniq_key)**,粒度 = 明细行:
        inbound:{record_id} / outbound:{record_id} / loading:{record_id}
        adjust:{seq} / {biz_type}:{record_id}:rev(红字冲销)
  · **红字冲销,不删不改** —— 冲销写一条 quantity 取负的流水,原记录留着。
  · 仓库是**数据不是结构** —— 加「田心仓」只插一行 warehouses,不改表结构。
    API 一律接受 warehouse_id,单仓阶段 UI 显示静态标签「🏭 芙蓉仓」。

⭐ **A2 事务边界(阶段 3 起)** —— 本模块所有**写方法**都多了一个 `conn=None` 参数:
    · 不传 conn  → 老行为:自己 get_db() → commit() → close()(单次调用自成一个事务)
    · 传了 conn  → **复用外部连接,不 commit、不 close**
  原因:过账要「stock_postings(1) + stock_ledger(N) + product_stock + stock_posting_skips(M)」
  四张表写在**同一个 BEGIN IMMEDIATE 事务**里,中途失败必须全回滚。
  模型层自行 commit 会把「流水没写成、余额已经改了」这种脏账放出去。
  上游口径见 §3.9 A2;事务编排放在 blueprints/stock_posting.py。
"""
from datetime import datetime

from ._db import get_db


def _now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


# 流水明细查询的公共 FROM 片段(阶段 2.5)。
# 抽成常量是为了 list_ledger / count_ledger / 导出 三处的筛选口径**逐字一致** ——
# 口径一旦分叉,「导出的 CSV 与页面逐行一致」这条判据就废了。
_LEDGER_SELECT_FROM = '''SELECT l.id, l.biz_type, l.uniq_key, l.order_pk, l.record_id,
                                l.product_id, l.quantity, l.unit, l.raw_quantity,
                                l.raw_unit, l.factor, l.is_reversal, l.posting_id,
                                l.operator_id, l.memo, l.occurred_at, l.created_at,
                                l.balance_after, l.unit_cost, l.amount,
                                p.product_code, p.product_name, p.specification,
                                p.base_unit
                         FROM stock_ledger l
                         LEFT JOIN product p ON p.product_id = l.product_id
                         WHERE l.warehouse_id = ?'''


# ── 仓库 ──────────────────────────────────────────────────────────
class Warehouse:
    """仓库。V1 只有一个('FR' 芙蓉仓),但表结构已经是多仓的。"""

    @staticmethod
    def list_all(include_disabled=False):
        conn = get_db()
        sql = 'SELECT * FROM warehouses'
        if not include_disabled:
            sql += ' WHERE status = 1'
        sql += ' ORDER BY sort_order ASC, id ASC'
        rows = conn.execute(sql).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    @staticmethod
    def get_by_id(warehouse_id):
        conn = get_db()
        row = conn.execute('SELECT * FROM warehouses WHERE id = ?', (int(warehouse_id),)).fetchone()
        conn.close()
        return dict(row) if row else None

    @staticmethod
    def get_by_code(code):
        conn = get_db()
        row = conn.execute('SELECT * FROM warehouses WHERE code = ?', (code,)).fetchone()
        conn.close()
        return dict(row) if row else None

    @staticmethod
    def get_default():
        """默认仓(is_default=1)。没有标记的退回第一行 —— API 缺省取它。"""
        conn = get_db()
        row = conn.execute(
            'SELECT * FROM warehouses WHERE is_default = 1 AND status = 1 ORDER BY id ASC LIMIT 1'
        ).fetchone()
        if not row:
            row = conn.execute('SELECT * FROM warehouses WHERE status = 1 ORDER BY id ASC LIMIT 1').fetchone()
        conn.close()
        return dict(row) if row else None

    @staticmethod
    def create(code, name, is_default=0, sort_order=0):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            '''INSERT INTO warehouses (code, name, is_default, sort_order, status,
                                     created_at, updated_at)
               VALUES (?, ?, ?, ?, 1, ?, ?)''',
            (code.strip(), name.strip(), int(is_default), int(sort_order), _now(), _now()),
        )
        wid = cursor.lastrowid
        conn.commit()
        conn.close()
        return wid

    @staticmethod
    def update(warehouse_id, **fields):
        allowed = {'code', 'name', 'is_default', 'sort_order', 'status'}
        sets, values = [], []
        for k in allowed:
            if k in fields:
                sets.append(f'{k} = ?')
                values.append(fields[k])
        if not sets:
            return False
        sets.append("updated_at = datetime('now','localtime')")
        values.append(int(warehouse_id))
        conn = get_db()
        conn.execute(f'UPDATE warehouses SET {", ".join(sets)} WHERE id = ?', values)
        conn.commit()
        conn.close()
        return True


# ── 当前库存 ──────────────────────────────────────────────────────
class ProductStock:
    """某个 SKU 在某个仓的库存行。

    ⚠️ `quantity` 是**内部累计**(自启用以来所有流水的和),
    不是对外显示值。对外显示 = `is_tracking=1` 时才是 quantity,否则是 0。
    """

    @staticmethod
    def get(warehouse_id, product_id, conn=None):
        """取库存行。conn 传入则复用外部连接(不 close,见 §3.9 A2)。"""
        own = conn is None
        c = conn if conn is not None else get_db()
        row = c.execute(
            'SELECT * FROM product_stock WHERE warehouse_id = ? AND product_id = ?',
            (int(warehouse_id), int(product_id)),
        ).fetchone()
        if own:
            c.close()
        return dict(row) if row else None

    @staticmethod
    def ensure(warehouse_id, product_id, conn=None):
        """取库存行;没有就建一行(quantity=0, is_tracking=0)。返回 row dict。

        ⭐ **A2 事务边界**(上游 §3.9):传入 conn 时**不 commit、不 close** ——
        过账要把「建行 + 加数量 + 写流水 + 写批次」放在同一个 BEGIN IMMEDIATE
        事务里,模型层自行 commit 会把中途失败变成「流水没写、余额改了」的脏账。
        """
        own = conn is None
        c = conn if conn is not None else get_db()
        row = c.execute(
            'SELECT * FROM product_stock WHERE warehouse_id = ? AND product_id = ?',
            (int(warehouse_id), int(product_id)),
        ).fetchone()
        if row is None:
            c.execute(
                'INSERT OR IGNORE INTO product_stock '
                '(warehouse_id, product_id, quantity, is_tracking, updated_at) VALUES (?, ?, 0, 0, ?)',
                (int(warehouse_id), int(product_id), _now()),
            )
            if own:
                c.commit()
            row = c.execute(
                'SELECT * FROM product_stock WHERE warehouse_id = ? AND product_id = ?',
                (int(warehouse_id), int(product_id)),
            ).fetchone()
        if own:
            c.close()
        return dict(row) if row else None

    @staticmethod
    def add_quantity(warehouse_id, product_id, delta, conn=None):
        """内部累计加减。**不改 is_tracking** —— 启用只由盘点/归零触发。

        ⚠️ `UPDATE quantity = quantity + ?` 这个**原子累加不要改成"读出来再写回去"** ——
        那是经典的丢失更新(concurrent writers 会互相覆盖)。这条 SQL 是对的,保持不动。
        """
        own = conn is None
        c = conn if conn is not None else get_db()
        ProductStock.ensure(warehouse_id, product_id, conn=c)
        c.execute(
            'UPDATE product_stock SET quantity = quantity + ?, '
            "updated_at = datetime('now','localtime') WHERE warehouse_id = ? AND product_id = ?",
            (float(delta), int(warehouse_id), int(product_id)),
        )
        if own:
            c.commit()
        row = ProductStock.get(warehouse_id, product_id, conn=c)
        if own:
            c.close()
        return row

    @staticmethod
    def set_quantity(warehouse_id, product_id, quantity, conn=None):
        """直接把内部累计**设**成某个值(盘点提交 / 归零用)。"""
        own = conn is None
        c = conn if conn is not None else get_db()
        ProductStock.ensure(warehouse_id, product_id, conn=c)
        c.execute(
            'UPDATE product_stock SET quantity = ?, '
            "updated_at = datetime('now','localtime') WHERE warehouse_id = ? AND product_id = ?",
            (float(quantity), int(warehouse_id), int(product_id)),
        )
        if own:
            c.commit()
        row = ProductStock.get(warehouse_id, product_id, conn=c)
        if own:
            c.close()
        return row

    @staticmethod
    def set_tracking(warehouse_id, product_id, is_tracking, started_at=None, conn=None):
        """启用 / 停用库存计算。

        · 启用(is_tracking=1)→ tracking_started_at 记首次启用时刻
        · 停用(is_tracking=0)→ 内部累计保留,但对外显示 0
        ⚠️ 停用后重新启用,内部累计是**接着上次**的 —— 想从头算请用归零。
        """
        own = conn is None
        c = conn if conn is not None else get_db()
        ProductStock.ensure(warehouse_id, product_id, conn=c)
        if is_tracking:
            c.execute(
                'UPDATE product_stock SET is_tracking = 1, '
                'tracking_started_at = COALESCE(tracking_started_at, ?), '
                "updated_at = datetime('now','localtime') WHERE warehouse_id = ? AND product_id = ?",
                (started_at or _now(), int(warehouse_id), int(product_id)),
            )
        else:
            c.execute(
                'UPDATE product_stock SET is_tracking = 0, '
                "updated_at = datetime('now','localtime') WHERE warehouse_id = ? AND product_id = ?",
                (int(warehouse_id), int(product_id)),
            )
        if own:
            c.commit()
        row = ProductStock.get(warehouse_id, product_id, conn=c)
        if own:
            c.close()
        return row

    @staticmethod
    def list_by_warehouse(warehouse_id, only_tracking=None, category_id=None,
                           q=None, limit=100, offset=0):
        """库存页主查询(阶段 3 用)。联 product / product_categories 拿品名规格单位。"""
        sql = ['''SELECT ps.*, p.product_code, p.product_name, p.specification,
                         p.base_unit, p.category_id, pc.category_name
                  FROM product_stock ps
                  JOIN product p ON p.product_id = ps.product_id
                  LEFT JOIN product_categories pc ON pc.id = p.category_id
                  WHERE ps.warehouse_id = ?''']
        params = [int(warehouse_id)]
        if only_tracking is True:
            sql.append('AND ps.is_tracking = 1')
        elif only_tracking is False:
            sql.append('AND ps.is_tracking = 0')
        if category_id:
            sql.append('AND p.category_id = ?')
            params.append(int(category_id))
        if q:
            sql.append('AND (p.product_name LIKE ? OR p.specification LIKE ? OR p.product_code LIKE ?)')
            like = f'%{q}%'
            params.extend([like, like, like])
        sql.append('ORDER BY pc.sort_order ASC, p.product_name ASC, p.specification ASC')
        sql.append('LIMIT ? OFFSET ?')
        params.extend([int(limit), int(offset)])
        conn = get_db()
        # ⚠️ 用 ' ' join,不能用 '' —— 片段之间没有空格会粘成 'ASCLIMIT' 之类的坏 SQL
        rows = conn.execute(' '.join(sql), params).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    @staticmethod
    def count_by_warehouse(warehouse_id, only_tracking=None):
        conn = get_db()
        sql = 'SELECT COUNT(*) n FROM product_stock WHERE warehouse_id = ?'
        params = [int(warehouse_id)]
        if only_tracking is not None:
            sql += ' AND is_tracking = ?'
            params.append(1 if only_tracking else 0)
        n = conn.execute(sql, params).fetchone()['n']
        conn.close()
        return n

    # ── 阶段 2:库存页查询 ────────────────────────────────────────
    @staticmethod
    def list_stock_view(warehouse_id, only_tracking=None, category_ids=None,
                        q=None, limit=50, offset=0):
        """库存页主查询(阶段 2)。**以 product 为驱动表** LEFT JOIN product_stock。

        ⚠️⚠️ 为什么不用 product_stock 驱动(阶段 2 踩到的第一坑):
           product_stock 只在「盘点 / 归零」之后才会有行 —— 现在是 **0 行**。
           若从 product_stock 出发 INNER JOIN product,库存页上线当天就是**一片空白**。
           而库存页**必须**能看到「还没启用库存计算」的商品(显示 0 + 灰色「未启用」标签),
           否则用户根本不知道有哪些货要盘 —— 这是完成判据里明确要求的。

        ⚠️ `quantity` 取的是 product_stock 的**内部累计**;
           对外显示值 = is_tracking=1 时才是 quantity,否则恒为 0。
           页面用 `display_quantity` 字段承载「对外显示值」,两者分开放,别混。

        返回字段:
            product_id / product_code / product_name / specification / base_unit
            category_id / category_name / status
            quantity           内部累计(product_stock.quantity,没建行时为 0)
            is_tracking         1=已启用 0=未启用
            display_quantity    对外显示值(is_tracking=0 时恒为 0)
            tracking_started_at / updated_at
        """
        sql = ['''SELECT p.product_id, p.product_code, p.product_name, p.specification,
                         p.base_unit, p.category_id, p.status,
                         pc.category_name,
                         COALESCE(ps.quantity, 0)                AS quantity,
                         COALESCE(ps.is_tracking, 0)             AS is_tracking,
                         CASE WHEN COALESCE(ps.is_tracking, 0) = 1
                              THEN COALESCE(ps.quantity, 0) ELSE 0 END AS display_quantity,
                         ps.tracking_started_at,
                         ps.updated_at
                  FROM product p
                  LEFT JOIN product_stock ps
                         ON ps.product_id = p.product_id AND ps.warehouse_id = ?
                  LEFT JOIN product_categories pc ON pc.id = p.category_id
                  WHERE 1 = 1''']
        params = [int(warehouse_id)]
        if only_tracking is True:
            sql.append('AND COALESCE(ps.is_tracking, 0) = 1')
        elif only_tracking is False:
            sql.append('AND COALESCE(ps.is_tracking, 0) = 0')
        if category_ids:
            sql.append('AND p.category_id IN (%s)' % ','.join(['?'] * len(category_ids)))
            params.extend([int(c) for c in category_ids])
        if q:
            sql.append('AND (p.product_name LIKE ? OR p.specification LIKE ? '
                       'OR p.product_code LIKE ?)')
            like = f'%{q}%'
            params.extend([like, like, like])
        sql.append('ORDER BY pc.sort_order ASC, p.product_name ASC, '
                   'p.specification ASC, p.product_id ASC')
        sql.append('LIMIT ? OFFSET ?')
        params.extend([int(limit), int(offset)])
        conn = get_db()
        # ⚠️ 用 ' ' join,不能用 '' —— 片段之间没有空格会粘成 'ASCLIMIT' 之类的坏 SQL
        rows = conn.execute(' '.join(sql), params).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    @staticmethod
    def count_stock_view(warehouse_id, only_tracking=None, category_ids=None, q=None):
        """和 list_stock_view 配套的总数(分页用)。筛选条件必须完全一致。"""
        sql = ['''SELECT COUNT(*) n
                  FROM product p
                  LEFT JOIN product_stock ps
                         ON ps.product_id = p.product_id AND ps.warehouse_id = ?
                  WHERE 1 = 1''']
        params = [int(warehouse_id)]
        if only_tracking is True:
            sql.append('AND COALESCE(ps.is_tracking, 0) = 1')
        elif only_tracking is False:
            sql.append('AND COALESCE(ps.is_tracking, 0) = 0')
        if category_ids:
            sql.append('AND p.category_id IN (%s)' % ','.join(['?'] * len(category_ids)))
            params.extend([int(c) for c in category_ids])
        if q:
            sql.append('AND (p.product_name LIKE ? OR p.specification LIKE ? '
                       'OR p.product_code LIKE ?)')
            like = f'%{q}%'
            params.extend([like, like, like])
        conn = get_db()
        n = conn.execute(' '.join(sql), params).fetchone()['n']
        conn.close()
        return int(n or 0)

    @staticmethod
    def stats_by_warehouse(warehouse_id, only_tracking=None, category_ids=None, q=None):
        """库存页顶部统计格:总 SKU / 已启用 / 未启用 / 有库存数。

        ⚠️ 统计**跟随当前筛选条件**(分类 / 搜索 / 仅看已启用)——
           主流进销存的通用做法:用户筛了「灰板类」,统计格就该说这批有多少,
           不能还报全仓的数(看着像筛错了)。要看全仓就把筛选清空。
           筛选参数与 list_stock_view 完全一致。

        ⚠️ 用 COALESCE(ps.is_tracking, 0) 而不是 ps.is_tracking ——
           还没建过 product_stock 行的商品,ps.is_tracking 是 NULL,
           `NULL = 0` 在 SQL 里是 NULL(不是真),会把「未启用」漏算。
        ⚠️ 「有库存数」= 已启用 **且** quantity≠0 —— 不是「有 product_stock 行」,
           因为未启用的商品也可能已经建了行(流水照记,只是对外显示 0)。
        """
        sql = ['''SELECT
                     COUNT(*) AS total,
                     SUM(CASE WHEN COALESCE(ps.is_tracking, 0) = 1 THEN 1 ELSE 0 END) AS tracking,
                     SUM(CASE WHEN COALESCE(ps.is_tracking, 0) = 0 THEN 1 ELSE 0 END) AS untracked,
                     SUM(CASE WHEN COALESCE(ps.is_tracking, 0) = 1
                               AND COALESCE(ps.quantity, 0) <> 0 THEN 1 ELSE 0 END) AS with_stock
                   FROM product p
                   LEFT JOIN product_stock ps
                          ON ps.product_id = p.product_id AND ps.warehouse_id = ?
                   WHERE 1 = 1''']
        params = [int(warehouse_id)]
        if only_tracking is True:
            sql.append('AND COALESCE(ps.is_tracking, 0) = 1')
        elif only_tracking is False:
            sql.append('AND COALESCE(ps.is_tracking, 0) = 0')
        if category_ids:
            sql.append('AND p.category_id IN (%s)' % ','.join(['?'] * len(category_ids)))
            params.extend([int(c) for c in category_ids])
        if q:
            sql.append('AND (p.product_name LIKE ? OR p.specification LIKE ? '
                       'OR p.product_code LIKE ?)')
            like = f'%{q}%'
            params.extend([like, like, like])
        conn = get_db()
        row = conn.execute(' '.join(sql), params).fetchone()
        conn.close()
        d = dict(row) if row else {}
        return {
            'total': int(d.get('total') or 0),
            'tracking': int(d.get('tracking') or 0),
            'untracked': int(d.get('untracked') or 0),
            'with_stock': int(d.get('with_stock') or 0),
        }


# ── 库存流水 ──────────────────────────────────────────────────────
class StockLedger:
    """库存流水。**一切的唯一来源** —— product_stock.quantity 只是它的缓存。

    biz_type: opening(期初,当前不用) | inbound(入库 +) | adjust(盘点/归零)
              outbound(出货 −) | loading(装柜 −)
    uniq_key: 幂等键。同一键只能有一条 → 重复过账会被 DB 挡下。
    """

    @staticmethod
    def create(warehouse_id, biz_type, uniq_key, product_id, quantity, unit,
               order_pk=0, record_id=0, raw_quantity=0, raw_unit='', factor=1,
               is_reversal=0, posting_id=None, operator_id=None, memo='',
               occurred_at='', balance_after=None, unit_cost=None, amount=None,
               conn=None):
        """写一条流水。uniq_key 冲突 → sqlite3.IntegrityError(调用方转成「已过账」提示)。

        A 档 / B 档新列(阶段 1.5 加的,写入契约在阶段 3 / 5):
          occurred_at    业务日期(取订单 `date`),期间汇总与盘点切分一律用它;空串 = 还没写
          balance_after  本笔记账后该 (仓, SKU) 的余额快照 → 流水链逐笔可勾稽
          unit_cost      每个**基本单位**的成本(可空,V1 不写)
          amount         本笔金额 = quantity × unit_cost(可空,V1 不写)

        ⚠️ 本表有禁改触发器(trg_stock_ledger_no_update / no_delete)——
        **写进去就改不了了**,改错只能写一条 quantity 取负的红字流水。

        ⭐ **A3 写入契约**:这里只允许**普通 INSERT**。
        禁用 `INSERT OR REPLACE` / `OR IGNORE` / `ON CONFLICT` ——
        第 3 条触发器(trg_stock_ledger_no_reuse)会把它们变成 RAISE(ABORT),
        幂等只能靠「先查后写」(create_if_absent)。

        ⭐ **A2 事务边界**:传入 conn 时**不 commit、不 close**,
        由过账服务持有唯一的 BEGIN IMMEDIATE 事务。
        """
        own = conn is None
        c = conn if conn is not None else get_db()
        cursor = c.cursor()
        cursor.execute(
            '''INSERT INTO stock_ledger
               (warehouse_id, biz_type, uniq_key, order_pk, record_id, product_id,
                quantity, unit, raw_quantity, raw_unit, factor, is_reversal,
                posting_id, operator_id, memo, occurred_at, balance_after,
                unit_cost, amount, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (int(warehouse_id), biz_type, uniq_key, int(order_pk or 0), int(record_id or 0),
             int(product_id), float(quantity), unit, float(raw_quantity or 0), raw_unit or '',
             float(factor or 1), int(is_reversal or 0), posting_id, operator_id, memo or '',
             occurred_at or '', balance_after, unit_cost, amount, _now()),
        )
        lid = cursor.lastrowid
        if own:
            c.commit()
            c.close()
        return lid

    @staticmethod
    def exists(uniq_key, conn=None):
        own = conn is None
        c = conn if conn is not None else get_db()
        row = c.execute('SELECT id FROM stock_ledger WHERE uniq_key = ?', (uniq_key,)).fetchone()
        if own:
            c.close()
        return row is not None

    @staticmethod
    def create_if_absent(warehouse_id, biz_type, uniq_key, product_id, quantity, unit,
                         conn=None, **kwargs):
        """⭐**幂等写入的正规入口** —— 先查后写。

        ⚠️ 为什么不能靠 `INSERT OR REPLACE` / UPSERT 做幂等:
        SQLite 默认 `recursive_triggers=0` 时,**REPLACE 删旧行不触发 DELETE 触发器**
        → 能无声把流水数量从 10 改成 999(A3 第 3 条触发器就是堵这个洞)。
        所以幂等必须「先查后写」:查到了就跳过,查不到才 INSERT。

        返回 (ledger_id, created):
          created=True  → 新写入,ledger_id 是新行 id
          created=False → 之前已过账,**未写入**,ledger_id 是原有行 id
        """
        existing = StockLedger.get_by_uniq_key(uniq_key, conn=conn)
        if existing:
            return existing['id'], False
        lid = StockLedger.create(warehouse_id, biz_type, uniq_key, product_id, quantity, unit,
                                 conn=conn, **kwargs)
        return lid, True

    @staticmethod
    def get_by_uniq_key(uniq_key, conn=None):
        own = conn is None
        c = conn if conn is not None else get_db()
        row = c.execute('SELECT * FROM stock_ledger WHERE uniq_key = ?', (uniq_key,)).fetchone()
        if own:
            c.close()
        return dict(row) if row else None

    @staticmethod
    def list_by_product(warehouse_id, product_id, limit=100):
        conn = get_db()
        rows = conn.execute(
            '''SELECT * FROM stock_ledger
               WHERE warehouse_id = ? AND product_id = ?
               ORDER BY id DESC LIMIT ?''',
            (int(warehouse_id), int(product_id), int(limit)),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    @staticmethod
    def list_by_posting(posting_id):
        conn = get_db()
        rows = conn.execute(
            'SELECT * FROM stock_ledger WHERE posting_id = ? ORDER BY id ASC',
            (int(posting_id),),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    @staticmethod
    def sum_by_product(warehouse_id, product_id):
        """流水累计(= 内部累计,用来对账 product_stock.quantity)。"""
        conn = get_db()
        row = conn.execute(
            'SELECT COALESCE(SUM(quantity), 0) s FROM stock_ledger WHERE warehouse_id = ? AND product_id = ?',
            (int(warehouse_id), int(product_id)),
        ).fetchone()
        conn.close()
        return float(row['s'] if row else 0)

    @staticmethod
    def sum_by_warehouse(warehouse_id):
        """按仓**一次**算出所有 SKU 的内部累计 → {product_id: quantity}。

        ⚠️ 不要用 sum_by_product 逐行查(那是 N+1):库存页 1600 行 = 1600 次查询。
           阶段 2 的「内部累计」列必须走这个聚合。
        """
        conn = get_db()
        rows = conn.execute(
            'SELECT product_id, COALESCE(SUM(quantity), 0) s FROM stock_ledger '
            'WHERE warehouse_id = ? GROUP BY product_id',
            (int(warehouse_id),)).fetchall()
        conn.close()
        return {int(r['product_id']): float(r['s']) for r in rows}

    @staticmethod
    def split_by_as_of(warehouse_id, product_id, as_of, conn=None):
        """⭐ **A5**：按盘点基准时点 `as_of` 把某 SKU 的流水切成两段。

        返回:
            {
              'base_qty':   基准余额(as_of 之前的流水合计),
              'period_qty': 期间变动(as_of 之后的流水合计),
              'period_rows':[期间变动的逐笔明细,供提交确认弹框列给人看],
            }

        ⚠️⚠️ **口径陷阱（本方法存在的全部理由）**：
           `occurred_at` 是**日期**(`YYYY-MM-DD`,取订单 date),
           `as_of` 是**时刻**(`YYYY-MM-DD HH:MM:SS`)→ **两者不能直接比大小**。
           直接写 `occurred_at > as_of` 会把「同日的流水」一律判成基准内,
           于是「先数货、后入库(同日)」这种顺序就算不出期间变动 → 差额算错。

           正确口径:
             · `occurred_at > date(as_of)`  → 期间变动(业务日期在盘点日之后)
             · `occurred_at == date(as_of)` 且 `created_at > as_of` → 期间变动
                  (同日但记账时刻在盘点建单之后 = 先数货后入库)
             · 其余 → 基准内
                  (含「先入库后数货」:业务日期同日、记账时刻早于建单)

           ⚠️ **冲销流水(is_reversal=1)也要算进去** —— 它是负数流水,
              不算的话「冲销后余额」和「基准余额」会对不上。
        """
        as_of = (as_of or '').strip()
        if not as_of:
            return {'base_qty': 0.0, 'period_qty': 0.0, 'period_rows': []}
        as_of_day = as_of[:10]          # 'YYYY-MM-DD'
        own = conn is None
        c = conn if conn is not None else get_db()
        try:
            rows = c.execute(
                '''SELECT id, biz_type, uniq_key, quantity, unit, raw_quantity, raw_unit,
                          occurred_at, created_at, memo, posting_id
                   FROM stock_ledger
                   WHERE warehouse_id = ? AND product_id = ?
                   ORDER BY id ASC''',
                (int(warehouse_id), int(product_id))).fetchall()
        finally:
            if own:
                c.close()

        base_qty = 0.0
        period_qty = 0.0
        period_rows = []
        for r in rows:
            occurred = (r['occurred_at'] or '')[:10]
            created = (r['created_at'] or '')
            qty = float(r['quantity'] or 0)
            is_period = False
            if occurred > as_of_day:
                is_period = True
            elif occurred == as_of_day and created > as_of:
                is_period = True
            if is_period:
                period_qty += qty
                period_rows.append({
                    'id': int(r['id']),
                    'biz_type': r['biz_type'],
                    'quantity': qty,
                    'unit': r['unit'],
                    'raw_quantity': r['raw_quantity'],
                    'raw_unit': r['raw_unit'],
                    'occurred_at': r['occurred_at'],
                    'created_at': created,
                    'memo': r['memo'] or '',
                    'posting_id': r['posting_id'],
                })
            else:
                base_qty += qty
        return {'base_qty': base_qty, 'period_qty': period_qty, 'period_rows': period_rows}

    @staticmethod
    def list_ledger(warehouse_id, product_id=None, biz_type=None,
                    date_from=None, date_to=None, limit=100, offset=0):
        """流水明细查询(阶段 2.5)。按 **SKU / 业务类型 / 日期** 筛。

        ⚠️ 日期筛的是 `occurred_at`(业务日期,`YYYY-MM-DD`,取订单 date),
           **不是** `created_at`(记账时刻)。口径见 §3.9 A4:
           期间汇总/盘点切分一律用 occurred_at;created_at 只用于「同日先后」判定。

        ⚠️ 日期是**闭区间**:date_from <= occurred_at <= date_to。
        """
        sql = [_LEDGER_SELECT_FROM]
        params = [int(warehouse_id)]
        if product_id:
            sql.append('AND l.product_id = ?')
            params.append(int(product_id))
        if biz_type:
            sql.append('AND l.biz_type = ?')
            params.append(str(biz_type))
        if date_from:
            sql.append('AND l.occurred_at >= ?')
            params.append(str(date_from))
        if date_to:
            sql.append('AND l.occurred_at <= ?')
            params.append(str(date_to))
        sql.append('ORDER BY l.occurred_at DESC, l.id DESC')
        sql.append('LIMIT ? OFFSET ?')
        params.extend([int(limit), int(offset)])
        conn = get_db()
        # ⚠️ 用 ' ' join,不能用 '' —— 片段之间没有空格会粘成坏 SQL
        rows = conn.execute(' '.join(sql), params).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    @staticmethod
    def count_ledger(warehouse_id, product_id=None, biz_type=None,
                     date_from=None, date_to=None):
        """和 list_ledger 配套的总数(分页用)。筛选条件必须完全一致。"""
        sql = ['''SELECT COUNT(*) n FROM stock_ledger l
                  WHERE l.warehouse_id = ?''']
        params = [int(warehouse_id)]
        if product_id:
            sql.append('AND l.product_id = ?')
            params.append(int(product_id))
        if biz_type:
            sql.append('AND l.biz_type = ?')
            params.append(str(biz_type))
        if date_from:
            sql.append('AND l.occurred_at >= ?')
            params.append(str(date_from))
        if date_to:
            sql.append('AND l.occurred_at <= ?')
            params.append(str(date_to))
        conn = get_db()
        n = conn.execute(' '.join(sql), params).fetchone()['n']
        conn.close()
        return int(n or 0)

    @staticmethod
    def next_adjust_seq(warehouse_id):
        """adjust:{seq} 的下一个 seq —— 盘点/归零写流水时用。"""
        conn = get_db()
        row = conn.execute(
            "SELECT COUNT(*) n FROM stock_ledger WHERE warehouse_id = ? AND biz_type = 'adjust'",
            (int(warehouse_id),),
        ).fetchone()
        conn.close()
        return int(row['n'] if row else 0) + 1


# ── 过账批次 ──────────────────────────────────────────────────────
class StockPosting:
    """一次「过账」= 一个批次。冲销只改 status,不删批次。"""

    @staticmethod
    def create(biz_type, order_pk, warehouse_id, operator_id=None, operator_name='', conn=None):
        own = conn is None
        c = conn if conn is not None else get_db()
        cursor = c.cursor()
        cursor.execute(
            '''INSERT INTO stock_postings
               (biz_type, order_pk, warehouse_id, posted_at, operator_id, operator_name,
                status, posted_count, posted_qty, skipped_count, note)
               VALUES (?, ?, ?, ?, ?, ?, 'posted', 0, 0, 0, '')''',
            (biz_type, int(order_pk), int(warehouse_id), _now(), operator_id, operator_name or ''),
        )
        pid = cursor.lastrowid
        if own:
            c.commit()
            c.close()
        return pid

    @staticmethod
    def get_by_id(posting_id, conn=None):
        own = conn is None
        c = conn if conn is not None else get_db()
        row = c.execute('SELECT * FROM stock_postings WHERE id = ?', (int(posting_id),)).fetchone()
        if own:
            c.close()
        return dict(row) if row else None

    @staticmethod
    def get_by_order(biz_type, order_pk, conn=None):
        own = conn is None
        c = conn if conn is not None else get_db()
        row = c.execute(
            'SELECT * FROM stock_postings WHERE biz_type = ? AND order_pk = ? ORDER BY id DESC LIMIT 1',
            (biz_type, int(order_pk)),
        ).fetchone()
        if own:
            c.close()
        return dict(row) if row else None

    @staticmethod
    def list_active(biz_type, order_pk, conn=None):
        """**未冲销**的过账批次(status='posted')。

        ⚠️ **别拿它当 A6 解锁守卫的判据**(2026-11-17 实测踩坑):
        「重复点过账」会留下 posted_count=0 的空批次,status 同样是 'posted'
        → 拿批次状态当判据会把「其实没入账」误判成「已过账」,
          结果是**冲销之后仍然解不了锁**。
        守卫的正确判据是「还有没被冲销的**流水**」——
        见 `blueprints/stock_posting.py:has_live_posting()`。
        本方法保留给「列出某单还欠哪些批次」这类查询用。
        """
        own = conn is None
        c = conn if conn is not None else get_db()
        rows = c.execute(
            "SELECT * FROM stock_postings WHERE biz_type = ? AND order_pk = ? AND status = 'posted' "
            'ORDER BY id ASC',
            (biz_type, int(order_pk)),
        ).fetchall()
        if own:
            c.close()
        return [dict(r) for r in rows]

    @staticmethod
    def update_stats(posting_id, posted_count=None, posted_qty=None, skipped_count=None,
                     note=None, conn=None):
        sets, values = [], []
        if posted_count is not None:
            sets.append('posted_count = ?')
            values.append(int(posted_count))
        if posted_qty is not None:
            sets.append('posted_qty = ?')
            values.append(float(posted_qty))
        if skipped_count is not None:
            sets.append('skipped_count = ?')
            values.append(int(skipped_count))
        if note is not None:
            sets.append('note = ?')
            values.append(note)
        if not sets:
            return False
        values.append(int(posting_id))
        own = conn is None
        c = conn if conn is not None else get_db()
        c.execute(f'UPDATE stock_postings SET {", ".join(sets)} WHERE id = ?', values)
        if own:
            c.commit()
            c.close()
        return True

    @staticmethod
    def mark_reversed(posting_id, at=None, conn=None):
        own = conn is None
        c = conn if conn is not None else get_db()
        c.execute(
            "UPDATE stock_postings SET status = 'reversed', reversed_at = ? WHERE id = ?",
            (at or _now(), int(posting_id)),
        )
        if own:
            c.commit()
            c.close()
        return True

    @staticmethod
    def list_recent(limit=50, biz_type=None):
        conn = get_db()
        sql = 'SELECT * FROM stock_postings'
        params = []
        if biz_type:
            sql += ' WHERE biz_type = ?'
            params.append(biz_type)
        sql += ' ORDER BY id DESC LIMIT ?'
        params.append(int(limit))
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]


# ── 跳过明细 ──────────────────────────────────────────────────────
class StockPostingSkip:
    """过账时跳过的行(待处理清单)。

    reason_code 分两类,处置**完全不同**:
      可补过账:no_product / no_conversion / bad_base_unit / bad_qty
               → 修好数据后可以再点一次过账,只补这些行
      不可补  :not_tracking
               → 商品未启用库存,盘点值已含这些货,补了会重复计算
    """

    REASON_CODES = ('no_product', 'no_conversion', 'bad_base_unit', 'bad_qty', 'not_tracking')
    RESOLVABLE = ('no_product', 'no_conversion', 'bad_base_unit', 'bad_qty')

    @staticmethod
    def create(posting_id, order_pk, record_id, product_name, specification,
               raw_quantity, raw_unit, reason_code, reason_text, conn=None):
        own = conn is None
        c = conn if conn is not None else get_db()
        cursor = c.cursor()
        cursor.execute(
            '''INSERT INTO stock_posting_skips
               (posting_id, order_pk, record_id, product_name, specification,
                raw_quantity, raw_unit, reason_code, reason_text, resolved, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)''',
            (int(posting_id), int(order_pk), int(record_id), product_name or '',
             specification or '',
             # ⚠️ 不能写 raw_quantity or '' —— 数量为 0 时 0 or '' 会变成空串,
             #    页面上「数量 0」和「没数量」就分不开了
             str(raw_quantity) if raw_quantity is not None else '',
             raw_unit or '', reason_code, reason_text or '', _now()),
        )
        sid = cursor.lastrowid
        if own:
            c.commit()
            c.close()
        return sid

    @staticmethod
    def list_by_posting(posting_id, conn=None):
        own = conn is None
        c = conn if conn is not None else get_db()
        rows = c.execute(
            'SELECT * FROM stock_posting_skips WHERE posting_id = ? ORDER BY id ASC',
            (int(posting_id),),
        ).fetchall()
        if own:
            c.close()
        return [dict(r) for r in rows]

    @staticmethod
    def list_by_order(order_pk, resolved=None, conn=None):
        """某订单的跳过明细(跨批次)。补过账时按 record_id 标 resolved。"""
        own = conn is None
        c = conn if conn is not None else get_db()
        sql = 'SELECT * FROM stock_posting_skips WHERE order_pk = ?'
        params = [int(order_pk)]
        if resolved is not None:
            sql += ' AND resolved = ?'
            params.append(1 if resolved else 0)
        sql += ' ORDER BY id ASC'
        rows = c.execute(sql, params).fetchall()
        if own:
            c.close()
        return [dict(r) for r in rows]

    @staticmethod
    def mark_resolved_by_record(order_pk, record_ids, conn=None):
        """把某些明细行的历史跳过记录标成「已解决」(补过账后调用)。"""
        ids = [int(x) for x in (record_ids or [])]
        if not ids:
            return 0
        own = conn is None
        c = conn if conn is not None else get_db()
        marks = ','.join(['?'] * len(ids))
        cur = c.execute(
            f'UPDATE stock_posting_skips SET resolved = 1 '
            f'WHERE order_pk = ? AND resolved = 0 AND record_id IN ({marks})',
            [int(order_pk)] + ids,
        )
        changed = cur.rowcount
        if own:
            c.commit()
            c.close()
        return changed

    @staticmethod
    def list_unresolved(limit=100):
        conn = get_db()
        rows = conn.execute(
            'SELECT * FROM stock_posting_skips WHERE resolved = 0 ORDER BY id DESC LIMIT ?',
            (int(limit),),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    @staticmethod
    def mark_resolved(skip_id):
        conn = get_db()
        conn.execute('UPDATE stock_posting_skips SET resolved = 1 WHERE id = ?', (int(skip_id),))
        conn.commit()
        conn.close()
        return True

    @staticmethod
    def count_by_reason(posting_id):
        conn = get_db()
        rows = conn.execute(
            'SELECT reason_code, COUNT(*) n FROM stock_posting_skips WHERE posting_id = ? GROUP BY reason_code',
            (int(posting_id),),
        ).fetchall()
        conn.close()
        return {r['reason_code']: r['n'] for r in rows}


# ── 盘点单 ────────────────────────────────────────────────────────
class StockCount:
    """盘点单 —— 盘点是一项作业(拿清单去数、分几批录、最后统一提交),所以要有单据。

    status: open(进行中) | closed(已提交,不可再改)
    """

    @staticmethod
    def create(title, warehouse_id=1, remark='', created_by=None, product_ids=None, as_of=None):
        """建盘点单。product_ids 给了就同时建明细(快照当前账面数)。

        ⭐A5 `as_of` = **盘点基准时点**,默认取建单时刻,建单即写死。
        提交时按它把流水切成「基准余额」与「期间变动」两段 → 解决
        「先数货后入库」与「先入库后数货」同样两个数、顺序相反结果相反的问题。

        ⚠️ **口径陷阱（阶段 5 提交时必须显式处理）**：
        `occurred_at` 是**日期**(取订单 `date`,`YYYY-MM-DD`),`as_of` 是**时刻**
        (`YYYY-MM-DD HH:MM:SS`)→ 两者不能直接比大小。
        建议口径：`occurred_at > date(as_of)` 的算「期间变动」;
        **同日**流水用 `created_at > as_of` 判定(先入库后数货 = 基准内,
        先数货后入库 = 期间变动)。
        """
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            '''INSERT INTO stock_counts
               (warehouse_id, title, remark, status, total_items, counted_items,
                diff_items, created_by, created_at, as_of)
               VALUES (?, ?, ?, 'open', 0, 0, 0, ?, ?, ?)''',
            (int(warehouse_id), (title or '').strip(), (remark or '').strip(),
             created_by, _now(), as_of or _now()),
        )
        cid = cursor.lastrowid
        conn.commit()
        conn.close()

        if product_ids:
            StockCountItem.bulk_create_with_snapshot(cid, product_ids, int(warehouse_id))
            StockCount.refresh_stats(cid)
        return cid

    @staticmethod
    def get_by_id(count_id):
        conn = get_db()
        row = conn.execute('SELECT * FROM stock_counts WHERE id = ?', (int(count_id),)).fetchone()
        conn.close()
        return dict(row) if row else None

    @staticmethod
    def list_all(status=None, warehouse_id=None, limit=100):
        conn = get_db()
        sql = 'SELECT * FROM stock_counts'
        where, params = [], []
        if status:
            where.append('status = ?')
            params.append(status)
        if warehouse_id:
            where.append('warehouse_id = ?')
            params.append(int(warehouse_id))
        if where:
            sql += ' WHERE ' + ' AND '.join(where)
        sql += ' ORDER BY id DESC LIMIT ?'
        params.append(int(limit))
        rows = conn.execute(sql, params).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    @staticmethod
    def update_meta(count_id, **fields):
        allowed = {'title', 'remark', 'warehouse_id', 'as_of'}
        sets, values = [], []
        for k in allowed:
            if k in fields:
                sets.append(f'{k} = ?')
                values.append(fields[k])
        if not sets:
            return False
        values.append(int(count_id))
        conn = get_db()
        conn.execute(f'UPDATE stock_counts SET {", ".join(sets)} WHERE id = ?', values)
        conn.commit()
        conn.close()
        return True

    @staticmethod
    def close(count_id):
        conn = get_db()
        cur = conn.execute(
            "UPDATE stock_counts SET status = 'closed', closed_at = ? WHERE id = ? AND status = 'open'",
            (_now(), int(count_id)),
        )
        # ⚠️ 用 cursor.rowcount,不要用 conn.total_changes ——
        #    total_changes 是**连接累计**改动数,复用连接时会误判成 True
        changed = cur.rowcount > 0
        conn.commit()
        conn.close()
        return changed

    @staticmethod
    def reopen(count_id):
        conn = get_db()
        cur = conn.execute(
            "UPDATE stock_counts SET status = 'open', closed_at = NULL WHERE id = ? AND status = 'closed'",
            (int(count_id),),
        )
        changed = cur.rowcount > 0
        conn.commit()
        conn.close()
        return changed

    @staticmethod
    def delete(count_id):
        """删盘点单。先删明细 —— FK 是 RESTRICT,不先删明细会报错。"""
        conn = get_db()
        conn.execute('DELETE FROM stock_count_items WHERE count_id = ?', (int(count_id),))
        cur = conn.execute('DELETE FROM stock_counts WHERE id = ?', (int(count_id),))
        changed = cur.rowcount > 0
        conn.commit()
        conn.close()
        return changed

    @staticmethod
    def refresh_stats(count_id):
        """刷新统计:应盘数 / 已录数 / 有差异数。

        ⚠️ 差异是相对 **book_quantity(建单时的账面快照)** —— 用来展示「账面 vs 实盘」。
        提交时调整的目标不是 book_quantity,而是**提交那一刻的当前余额**
        (盘点期间可能又发生了出入库)—— 那是阶段 5 的事。
        """
        conn = get_db()
        row = conn.execute(
            '''SELECT COUNT(*) total,
                      SUM(CASE WHEN actual_quantity IS NOT NULL THEN 1 ELSE 0 END) counted,
                      SUM(CASE WHEN actual_quantity IS NOT NULL
                                AND ABS(actual_quantity - book_quantity) > 0.000001
                               THEN 1 ELSE 0 END) diff
               FROM stock_count_items WHERE count_id = ?''',
            (int(count_id),),
        ).fetchone()
        conn.execute(
            'UPDATE stock_counts SET total_items = ?, counted_items = ?, diff_items = ? WHERE id = ?',
            (int(row['total'] or 0), int(row['counted'] or 0), int(row['diff'] or 0), int(count_id)),
        )
        conn.commit()
        conn.close()
        return {'total_items': int(row['total'] or 0),
                'counted_items': int(row['counted'] or 0),
                'diff_items': int(row['diff'] or 0)}


# ── 盘点明细 ──────────────────────────────────────────────────────
class StockCountItem:
    """盘点明细:每个商品一行。

    book_quantity   建单时的账面快照(建单那一刻 product_stock.quantity)
    actual_quantity 实盘数;NULL = 待录
    """

    @staticmethod
    def bulk_create(count_id, items):
        """items = [(product_id, book_quantity), ...]"""
        conn = get_db()
        for pid, book in items:
            conn.execute(
                'INSERT OR IGNORE INTO stock_count_items '
                '(count_id, product_id, book_quantity, actual_quantity) VALUES (?, ?, ?, NULL)',
                (int(count_id), int(pid), float(book or 0)),
            )
        conn.commit()
        conn.close()
        return True

    @staticmethod
    def bulk_create_with_snapshot(count_id, product_ids, warehouse_id):
        """建单时按当前库存快照账面数。

        ⚠️ **批量口径**(阶段 5 性能修正):
           原实现是「逐行 SELECT + 逐行 INSERT」—— 1600 个 SKU = 3200 次往返,
           建一张全仓盘点单要等好几秒。这里改成
           **一次 IN 查询把该仓全部库存捞出来 + executemany 批量插入**,
           查询次数从 N 降到 1,插入从 N 次降到 1 次批量。
        """
        conn = get_db()
        pids = [int(p) for p in product_ids]
        if not pids:
            conn.close()
            return True

        # 一次查全仓库存(分批 IN,避开 SQLite 变量数上限 999)
        qty_map = {}
        BATCH = 900
        for i in range(0, len(pids), BATCH):
            chunk = pids[i:i + BATCH]
            marks = ','.join('?' * len(chunk))
            rows = conn.execute(
                f'SELECT product_id, quantity FROM product_stock '
                f'WHERE warehouse_id = ? AND product_id IN ({marks})',
                [int(warehouse_id)] + chunk,
            ).fetchall()
            for r in rows:
                qty_map[int(r['product_id'])] = float(r['quantity'] or 0)

        conn.executemany(
            'INSERT OR IGNORE INTO stock_count_items '
            '(count_id, product_id, book_quantity, actual_quantity) VALUES (?, ?, ?, NULL)',
            [(int(count_id), pid, qty_map.get(pid, 0.0)) for pid in pids],
        )
        conn.commit()
        conn.close()
        return True

    @staticmethod
    def get(count_id, product_id):
        conn = get_db()
        row = conn.execute(
            'SELECT * FROM stock_count_items WHERE count_id = ? AND product_id = ?',
            (int(count_id), int(product_id)),
        ).fetchone()
        conn.close()
        return dict(row) if row else None

    @staticmethod
    def list_by_count(count_id):
        """盘点单的所有明细行。

        ⭐ 阶段 6 新增两列:`category_id` / `category_name` —— 移动端盘点要
           **按分类分组**(站在货堆前按类翻比按字母翻快)。用 LEFT JOIN,
           没有分类的行(category_id 为空)归到「未分类」组,不能被 JOIN 丢掉。
        ⚠️ ORDER BY **保持原样**(品名 → 规格)—— PC 单据页已经按这个顺序展示,
           阶段 6 不改已交付页面的排序,分组在移动端用 Python 做。
        """
        conn = get_db()
        rows = conn.execute(
            '''SELECT i.*, p.product_code, p.product_name, p.specification, p.base_unit,
                      p.category_id, pc.category_name, pc.sort_order AS category_sort_order
               FROM stock_count_items i
               JOIN product p ON p.product_id = i.product_id
               LEFT JOIN product_categories pc ON pc.id = p.category_id
               WHERE i.count_id = ?
               ORDER BY p.product_name ASC, p.specification ASC''',
            (int(count_id),),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    @staticmethod
    def list_unresolved(count_id):
        """还没录数的行。"""
        conn = get_db()
        rows = conn.execute(
            '''SELECT i.*, p.product_code, p.product_name, p.specification, p.base_unit
               FROM stock_count_items i
               JOIN product p ON p.product_id = i.product_id
               WHERE i.count_id = ? AND i.actual_quantity IS NULL
               ORDER BY p.product_name ASC, p.specification ASC''',
            (int(count_id),),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    @staticmethod
    def set_actual(count_id, product_id, actual_quantity):
        """录实盘数(草稿即时保存)。actual_quantity=None 表示清空=回到待录。

        ⚠️ **现场补录**(商品不在盘点单清单里)时,book_quantity 取**当前账面数**,
        而不是 0 —— 否则差异列会显示「账面 0」,跟实际对不上。
        已在清单里的行走 ON CONFLICT 分支 → book_quantity 保持**建单时的快照**不变。
        """
        conn = get_db()
        cursor = conn.cursor()
        row = conn.execute(
            'SELECT warehouse_id FROM stock_counts WHERE id = ?', (int(count_id),)).fetchone()
        wid = int(row['warehouse_id']) if row else 1
        stock = conn.execute(
            'SELECT quantity FROM product_stock WHERE warehouse_id = ? AND product_id = ?',
            (wid, int(product_id))).fetchone()
        book = float(stock['quantity']) if stock else 0.0
        cursor.execute(
            '''INSERT INTO stock_count_items
               (count_id, product_id, book_quantity, actual_quantity, counted_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(count_id, product_id) DO UPDATE SET
                   actual_quantity = excluded.actual_quantity,
                   counted_at = excluded.counted_at''',
            (int(count_id), int(product_id), book, actual_quantity,
             _now() if actual_quantity is not None else None),
        )
        conn.commit()
        conn.close()
        StockCount.refresh_stats(count_id)
        return StockCountItem.get(count_id, product_id)

    @staticmethod
    def set_actual_many(count_id, pairs):
        """批量录实盘数。pairs = [(product_id, actual_quantity), ...]

        ⚠️ **为什么不用循环调 `set_actual`**:
           `set_actual` 每次都开一个连接 + 一次 commit(还带 3 条 PRAGMA),
           粘贴 1600 行 = 1600 次连接 + 1600 次事务提交,慢到不可用。
           这里改成**一个连接内批量做**:一次查当前库存 → 一次查已存在行 →
           批量 UPDATE / INSERT → 一次 commit。

        语义与 `set_actual` 完全一致:
          · 已在清单里的行 → 只更新 actual_quantity / counted_at,
            **book_quantity 保持建单时的快照不变**
          · 不在清单里的行(现场补录)→ 新建行,book_quantity = **当前账面数**
            (不是 0,否则差异列显示「账面 0」跟实际对不上)
        """
        pairs = [(int(pid), qty) for pid, qty in (pairs or [])]
        if not pairs:
            return 0

        conn = get_db()
        count_row = conn.execute(
            'SELECT warehouse_id FROM stock_counts WHERE id = ?', (int(count_id),)).fetchone()
        if not count_row:
            conn.close()
            return 0
        wid = int(count_row['warehouse_id'])

        pids = [pid for pid, _ in pairs]
        # ① 查当前账面数(分批 IN,避开 SQLite 变量数上限 999)
        stock_map = {}
        BATCH = 900
        for i in range(0, len(pids), BATCH):
            chunk = pids[i:i + BATCH]
            marks = ','.join('?' * len(chunk))
            for r in conn.execute(
                f'SELECT product_id, quantity FROM product_stock '
                f'WHERE warehouse_id = ? AND product_id IN ({marks})',
                [wid] + chunk,
            ).fetchall():
                stock_map[int(r['product_id'])] = float(r['quantity'] or 0)

        # ② 查盘点单里已存在的行
        exists_map = set()
        for i in range(0, len(pids), BATCH):
            chunk = pids[i:i + BATCH]
            marks = ','.join('?' * len(chunk))
            for r in conn.execute(
                f'SELECT product_id FROM stock_count_items '
                f'WHERE count_id = ? AND product_id IN ({marks})',
                [int(count_id)] + chunk,
            ).fetchall():
                exists_map.add(int(r['product_id']))

        stamp = _now()
        updates, inserts = [], []
        for pid, qty in pairs:
            counted_at = stamp if qty is not None else None
            if pid in exists_map:
                updates.append((qty, counted_at, int(count_id), pid))
            else:
                inserts.append((int(count_id), pid,
                               stock_map.get(pid, 0.0), qty, counted_at))

        if updates:
            conn.executemany(
                'UPDATE stock_count_items SET actual_quantity = ?, counted_at = ? '
                'WHERE count_id = ? AND product_id = ?', updates)
        if inserts:
            conn.executemany(
                'INSERT INTO stock_count_items '
                '(count_id, product_id, book_quantity, actual_quantity, counted_at) '
                'VALUES (?, ?, ?, ?, ?)', inserts)
        conn.commit()
        conn.close()
        return len(pairs)

    @staticmethod
    def delete(count_id, product_id):
        conn = get_db()
        conn.execute('DELETE FROM stock_count_items WHERE count_id = ? AND product_id = ?',
                     (int(count_id), int(product_id)))
        conn.commit()
        conn.close()
        StockCount.refresh_stats(count_id)
        return True
