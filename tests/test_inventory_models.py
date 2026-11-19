# -*- coding: utf-8 -*-
"""库存模型层测试(阶段 1:七张表 + 7 个模型类)。

被测范围:
  · Warehouse       仓库(种子行 / 默认仓 / 增改)
  · ProductStock    当前库存(内部累计 vs 对外显示 / 启用开关)
  · StockLedger     库存流水(幂等键 UNIQUE / 红字冲销 / 对账)
  · StockPosting    过账批次(posted → reversed)
  · StockPostingSkip 跳过明细(可补 vs 不可补 的两类 reason)
  · StockCount      盘点单(open/closed / 统计 / 删除)
  · StockCountItem  盘点明细(快照 / 录数 / UPSERT)

重点验证的**业务约定**(写错了会静默算错账,必须有测试兜住):
  1. **过账绝不自动启用** —— add_quantity 后 is_tracking 仍然是 0
  2. **停用不丢内部累计** —— set_tracking(0) 后 quantity 保留
  3. **重复启用保留首次启用时刻** —— tracking_started_at 不被覆盖
  4. **幂等靠 UNIQUE(uniq_key)** —— 同键二次插入必须抛 IntegrityError
  5. **红字冲销取负数**,原记录留着不改
  6. **product_stock.quantity ≡ stock_ledger 累计** —— 两边要对得上
  7. **有库存的商品不允许直接删** —— FK 真的开着
  8. **盘点差异相对 book_quantity(建单快照)**,不是相对当前余额
"""
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models._db as _db   # noqa: E402


class _Base(unittest.TestCase):
    """每个用例一套干净的临时库。"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        self._orig = _db.DB_PATH
        _db.DB_PATH = self.tmp.name
        from models import init_db
        init_db()
        self._cat_id = self._mk_category()

    def tearDown(self):
        _db.DB_PATH = self._orig
        for ext in ('', '-wal', '-shm'):
            p = self.tmp.name + ext
            if os.path.exists(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass

    def _mk_category(self):
        from models import get_db
        conn = get_db()
        conn.execute("INSERT OR IGNORE INTO product_categories "
                     "(category_code, category_name, parent_id, level, sort_order, status) "
                     "VALUES ('INV01', '库存测试类', NULL, 1, 1, 1)")
        conn.commit()
        cid = conn.execute(
            "SELECT id FROM product_categories WHERE category_code='INV01'").fetchone()[0]
        conn.close()
        return cid

    def _make_product(self, code, name=None, spec=None, base_unit='码'):
        """建测试商品。

        ⚠️ 规格默认带序号 —— uk_product_name_spec(品名+规格) 是唯一索引,
        同名同规格建第二个会被拦。测试里每个商品都得有自己的品名或规格。
        """
        from models import Product
        if name is None:
            name = f'测试胶{code}'
        if spec is None:
            spec = f'0.4-{code}'
        return Product.create(product_code=code, product_name=name, category_id=self._cat_id,
                              specification=spec, base_unit=base_unit)

    def _wh(self):
        """默认仓 id。"""
        from models import Warehouse
        return Warehouse.get_default()['id']


# ── Warehouse ────────────────────────────────────────────────────
class TestWarehouse(_Base):
    def test_seed_row_exists(self):
        """init_db() 必须种出芙蓉仓。"""
        from models import Warehouse
        w = Warehouse.get_by_code('FR')
        self.assertIsNotNone(w, '种子仓 FR 不存在')
        self.assertEqual(w['name'], '芙蓉仓')
        self.assertEqual(w['is_default'], 1)
        self.assertEqual(w['status'], 1)

    def test_get_default(self):
        from models import Warehouse
        w = Warehouse.get_default()
        self.assertIsNotNone(w)
        self.assertEqual(w['code'], 'FR')
        self.assertEqual(w['id'], 1)

    def test_get_by_id_and_missing(self):
        from models import Warehouse
        self.assertIsNone(Warehouse.get_by_id(999999))
        self.assertIsNotNone(Warehouse.get_by_id(1))

    def test_list_all(self):
        from models import Warehouse
        rows = Warehouse.list_all()
        self.assertTrue(any(r['code'] == 'FR' for r in rows))
        # 默认仓排最前(sort_order=1)
        self.assertEqual(rows[0]['code'], 'FR')

    def test_create_second_warehouse(self):
        """仓库是数据不是结构 —— 加仓只插一行。"""
        from models import Warehouse
        wid = Warehouse.create('TX', '田心仓', is_default=0, sort_order=2)
        w = Warehouse.get_by_id(wid)
        self.assertEqual(w['name'], '田心仓')
        self.assertEqual(w['is_default'], 0)
        self.assertEqual(len(Warehouse.list_all()), 2)

    def test_code_unique(self):
        from models import Warehouse
        Warehouse.create('TX', '田心仓')
        with self.assertRaises(sqlite3.IntegrityError):
            Warehouse.create('TX', '另一个田心仓')

    def test_update(self):
        from models import Warehouse
        wid = Warehouse.create('TX', '田心仓')
        Warehouse.update(wid, name='田心仓2', sort_order=9)
        w = Warehouse.get_by_id(wid)
        self.assertEqual(w['name'], '田心仓2')
        self.assertEqual(w['sort_order'], 9)

    def test_update_no_fields_is_noop(self):
        from models import Warehouse
        self.assertFalse(Warehouse.update(1))


# ── ProductStock ─────────────────────────────────────────────────
class TestProductStock(_Base):
    def test_ensure_creates_row(self):
        from models import ProductStock
        pid = self._make_product('P001')
        row = ProductStock.ensure(self._wh(), pid)
        self.assertIsNotNone(row)
        self.assertEqual(row['quantity'], 0)
        self.assertEqual(row['is_tracking'], 0)

    def test_ensure_is_idempotent(self):
        """重复 ensure 不建第二行(UNIQUE 兜底)。"""
        from models import ProductStock
        pid = self._make_product('P002')
        wid = self._wh()
        a = ProductStock.ensure(wid, pid)
        b = ProductStock.ensure(wid, pid)
        self.assertEqual(a['id'], b['id'])
        self.assertEqual(ProductStock.count_by_warehouse(wid), 1)

    def test_add_quantity_does_not_enable_tracking(self):
        """⚠️ 核心约定:过账绝不自动启用库存计算。"""
        from models import ProductStock
        pid = self._make_product('P003')
        wid = self._wh()
        ProductStock.add_quantity(wid, pid, 100)
        row = ProductStock.get(wid, pid)
        self.assertEqual(row['quantity'], 100)
        self.assertEqual(row['is_tracking'], 0, '过账把 is_tracking 打开了')

    def test_set_quantity(self):
        from models import ProductStock
        pid = self._make_product('P004')
        wid = self._wh()
        ProductStock.set_quantity(wid, pid, 42.5)
        self.assertEqual(ProductStock.get(wid, pid)['quantity'], 42.5)

    def test_add_quantity_negative(self):
        from models import ProductStock
        pid = self._make_product('P005')
        wid = self._wh()
        ProductStock.set_quantity(wid, pid, 100)
        ProductStock.add_quantity(wid, pid, -30)
        self.assertEqual(ProductStock.get(wid, pid)['quantity'], 70)

    def test_set_tracking_enable(self):
        from models import ProductStock
        pid = self._make_product('P006')
        wid = self._wh()
        row = ProductStock.set_tracking(wid, pid, 1)
        self.assertEqual(row['is_tracking'], 1)
        self.assertIsNotNone(row['tracking_started_at'])

    def test_reenable_keeps_first_start_time(self):
        """重新启用时首次启用时刻不能被覆盖(否则「自启用以来累计」的口径就变了)。"""
        from models import ProductStock
        pid = self._make_product('P007')
        wid = self._wh()
        first = ProductStock.set_tracking(wid, pid, 1)['tracking_started_at']
        ProductStock.set_tracking(wid, pid, 0)
        again = ProductStock.set_tracking(wid, pid, 1)['tracking_started_at']
        self.assertEqual(first, again)

    def test_disable_keeps_internal_quantity(self):
        """停用只是「对外显示 0」,内部累计要留着。"""
        from models import ProductStock
        pid = self._make_product('P008')
        wid = self._wh()
        ProductStock.set_quantity(wid, pid, 88)
        ProductStock.set_tracking(wid, pid, 1)
        row = ProductStock.set_tracking(wid, pid, 0)
        self.assertEqual(row['is_tracking'], 0)
        self.assertEqual(row['quantity'], 88)

    def test_count_by_warehouse(self):
        from models import ProductStock
        wid = self._wh()
        for i in range(3):
            ProductStock.ensure(wid, self._make_product(f'P1{i:02d}'))
        self.assertEqual(ProductStock.count_by_warehouse(wid), 3)
        self.assertEqual(ProductStock.count_by_warehouse(wid, only_tracking=False), 3)
        self.assertEqual(ProductStock.count_by_warehouse(wid, only_tracking=True), 0)

    def test_list_by_warehouse_joins_product(self):
        from models import ProductStock
        wid = self._wh()
        pid = self._make_product('P020', name='列表胶', spec='0.8')
        ProductStock.ensure(wid, pid)
        rows = ProductStock.list_by_warehouse(wid)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['product_name'], '列表胶')
        self.assertEqual(rows[0]['specification'], '0.8')
        self.assertEqual(rows[0]['base_unit'], '码')

    def test_list_by_warehouse_filter_q(self):
        from models import ProductStock
        wid = self._wh()
        ProductStock.ensure(wid, self._make_product('P030', name='甲胶'))
        ProductStock.ensure(wid, self._make_product('P031', name='乙胶'))
        self.assertEqual(len(ProductStock.list_by_warehouse(wid, q='甲')), 1)

    def test_different_warehouse_is_separate_row(self):
        """同一个 SKU 在两个仓 = 两行,互不影响。"""
        from models import ProductStock, Warehouse
        pid = self._make_product('P040')
        wid = self._wh()
        wid2 = Warehouse.create('TX', '田心仓')
        ProductStock.add_quantity(wid, pid, 100)
        ProductStock.add_quantity(wid2, pid, 7)
        self.assertEqual(ProductStock.get(wid, pid)['quantity'], 100)
        self.assertEqual(ProductStock.get(wid2, pid)['quantity'], 7)


# ── StockLedger ──────────────────────────────────────────────────
class TestStockLedger(_Base):
    def test_create_and_fetch(self):
        from models import StockLedger
        pid = self._make_product('L001')
        wid = self._wh()
        lid = StockLedger.create(wid, 'inbound', 'inbound:1', pid, 10, '码',
                                 order_pk=9, record_id=1, raw_quantity=10, raw_unit='码')
        row = StockLedger.get_by_uniq_key('inbound:1')
        self.assertIsNotNone(row)
        self.assertEqual(row['id'], lid)
        self.assertEqual(row['biz_type'], 'inbound')
        self.assertEqual(row['quantity'], 10)
        self.assertEqual(row['unit'], '码')
        self.assertEqual(row['record_id'], 1)

    def test_uniq_key_is_unique(self):
        """幂等的第一道也是最硬的一道防线:同键只能有一条。"""
        from models import StockLedger
        pid = self._make_product('L002')
        wid = self._wh()
        StockLedger.create(wid, 'inbound', 'inbound:1', pid, 10, '码')
        with self.assertRaises(sqlite3.IntegrityError):
            StockLedger.create(wid, 'inbound', 'inbound:1', pid, 10, '码')

    def test_exists(self):
        from models import StockLedger
        pid = self._make_product('L003')
        wid = self._wh()
        self.assertFalse(StockLedger.exists('inbound:99'))
        StockLedger.create(wid, 'inbound', 'inbound:99', pid, 1, '码')
        self.assertTrue(StockLedger.exists('inbound:99'))

    def test_reversal_is_negative(self):
        """红字冲销:取负数流水,原记录不动。"""
        from models import StockLedger
        pid = self._make_product('L004')
        wid = self._wh()
        StockLedger.create(wid, 'inbound', 'inbound:5', pid, 30, '码', record_id=5)
        StockLedger.create(wid, 'inbound', 'inbound:5:rev', pid, -30, '码',
                           record_id=5, is_reversal=1)
        self.assertEqual(StockLedger.sum_by_product(wid, pid), 0)
        # 原记录还在
        orig = StockLedger.get_by_uniq_key('inbound:5')
        self.assertEqual(orig['quantity'], 30)
        self.assertEqual(orig['is_reversal'], 0)

    def test_sum_by_product(self):
        from models import StockLedger
        pid = self._make_product('L005')
        wid = self._wh()
        StockLedger.create(wid, 'inbound', 'inbound:1', pid, 10, '码')
        StockLedger.create(wid, 'inbound', 'inbound:2', pid, 5.5, '码')
        StockLedger.create(wid, 'adjust', 'adjust:1', pid, -2, '码')
        self.assertAlmostEqual(StockLedger.sum_by_product(wid, pid), 13.5)

    def test_sum_scoped_by_warehouse(self):
        """同 SKU 不同仓的流水不能串。"""
        from models import StockLedger, Warehouse
        pid = self._make_product('L006')
        wid = self._wh()
        wid2 = Warehouse.create('TX', '田心仓')
        StockLedger.create(wid, 'inbound', 'inbound:1', pid, 10, '码')
        StockLedger.create(wid2, 'inbound', 'inbound:2', pid, 99, '码')
        self.assertEqual(StockLedger.sum_by_product(wid, pid), 10)
        self.assertEqual(StockLedger.sum_by_product(wid2, pid), 99)

    def test_uniq_key_is_globally_unique(self):
        """uniq_key 全局唯一(不按仓分组)。

        这是有意的:record_id 在订单表里本来就唯一,一条明细一生只过账一次,
        不该因为换了仓就又能过一遍 —— 那正是「双重计入」的口子。
        """
        from models import StockLedger, Warehouse
        pid = self._make_product('L006b')
        wid = self._wh()
        wid2 = Warehouse.create('TX', '田心仓')
        StockLedger.create(wid, 'inbound', 'inbound:1', pid, 10, '码')
        with self.assertRaises(sqlite3.IntegrityError):
            StockLedger.create(wid2, 'inbound', 'inbound:1', pid, 99, '码')

    def test_list_by_product_and_posting(self):
        from models import StockLedger, StockPosting
        pid = self._make_product('L007')
        wid = self._wh()
        # ⭐2026-11-16 阶段 1.6:stock_ledger.posting_id 现在有 FK → stock_postings(id),
        #   必须先建真实的过账批次,不能再随手写个不存在的 posting_id。
        posting_id = StockPosting.create('inbound', 1, wid)
        StockLedger.create(wid, 'inbound', 'inbound:1', pid, 10, '码', record_id=1, posting_id=posting_id)
        StockLedger.create(wid, 'inbound', 'inbound:2', pid, 20, '码', record_id=2, posting_id=posting_id)
        self.assertEqual(len(StockLedger.list_by_product(wid, pid)), 2)
        self.assertEqual(len(StockLedger.list_by_posting(posting_id)), 2)

    def test_ledger_rejects_unknown_posting(self):
        """阶段 1.6 FK 兜底:posting_id 指向不存在的批次 → 直接被 DB 挡下。

        这是 B 方案的核心价值 —— 「流水挂在一个不存在的过账批次上」这种脏数据
        从此不可能落库。FK 只当兜底,阶段 3 的过账 API 仍要做应用层校验,
        把 IntegrityError 翻译成 400 + 中文提示(否则用户看到的是 500)。
        """
        from models import StockLedger
        pid = self._make_product('L007f')
        wid = self._wh()
        with self.assertRaises(sqlite3.IntegrityError):
            StockLedger.create(wid, 'inbound', 'inbound:bad', pid, 1, '码', posting_id=999999)

    def test_next_adjust_seq(self):
        from models import StockLedger
        pid = self._make_product('L008')
        wid = self._wh()
        self.assertEqual(StockLedger.next_adjust_seq(wid), 1)
        StockLedger.create(wid, 'adjust', 'adjust:1', pid, 1, '码')
        self.assertEqual(StockLedger.next_adjust_seq(wid), 2)

    def test_ledger_matches_product_stock(self):
        """product_stock.quantity ≡ stock_ledger 累计 —— 两边必须对得上。"""
        from models import ProductStock, StockLedger
        pid = self._make_product('L009')
        wid = self._wh()
        for i, qty in enumerate([10, 20.5, -5], start=1):
            StockLedger.create(wid, 'inbound', f'inbound:{i}', pid, qty, '码')
            ProductStock.add_quantity(wid, pid, qty)
        self.assertAlmostEqual(
            ProductStock.get(wid, pid)['quantity'],
            StockLedger.sum_by_product(wid, pid))

    def test_new_columns_are_writable(self):
        """阶段 1.5 新列:occurred_at / balance_after / unit_cost / amount。"""
        from models import StockLedger
        pid = self._make_product('L010')
        wid = self._wh()
        StockLedger.create(wid, 'inbound', 'inbound:1', pid, 10, '码',
                           occurred_at='2026-11-01', balance_after=10.0,
                           unit_cost=3.5, amount=35.0)
        row = StockLedger.get_by_uniq_key('inbound:1')
        self.assertEqual(row['occurred_at'], '2026-11-01')
        self.assertAlmostEqual(row['balance_after'], 10.0)
        self.assertAlmostEqual(row['unit_cost'], 3.5)
        self.assertAlmostEqual(row['amount'], 35.0)

    def test_new_columns_default_empty(self):
        """不传新列 → occurred_at 空串、其余 NULL(写入契约在阶段 3)。"""
        from models import StockLedger
        pid = self._make_product('L011')
        wid = self._wh()
        StockLedger.create(wid, 'inbound', 'inbound:1', pid, 10, '码')
        row = StockLedger.get_by_uniq_key('inbound:1')
        self.assertEqual(row['occurred_at'], '')
        self.assertIsNone(row['balance_after'])
        self.assertIsNone(row['unit_cost'])
        self.assertIsNone(row['amount'])

    def test_occurred_at_not_null_enforced(self):
        """occurred_at 是 NOT NULL → 写入路径必须给值(空串合法,NULL 不行)。"""
        from models import get_db
        pid = self._make_product('L012')
        wid = self._wh()
        conn = get_db()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                'INSERT INTO stock_ledger (warehouse_id, biz_type, uniq_key, product_id, '
                'quantity, unit, occurred_at, created_at) '
                'VALUES (?, ?, ?, ?, ?, ?, NULL, ?)',
                (wid, 'inbound', 'inbound:1', pid, 1, '码', '2026-11-16 00:00:00'))
        conn.rollback()
        conn.close()


# ── ⭐A3 只追加(阶段 1.5)────────────────────────────────────────
class TestLedgerAppendOnly(_Base):
    """流水只追加由 **DB 层**强制 —— 手工 SQL 改不动。"""

    def test_update_is_blocked(self):
        from models import StockLedger, get_db
        pid = self._make_product('T001')
        wid = self._wh()
        StockLedger.create(wid, 'inbound', 'inbound:1', pid, 10, '码', occurred_at='2026-11-01')
        conn = get_db()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("UPDATE stock_ledger SET quantity = 999 WHERE uniq_key = 'inbound:1'")
        conn.rollback()
        conn.close()

    def test_delete_is_blocked(self):
        from models import StockLedger, get_db
        pid = self._make_product('T002')
        wid = self._wh()
        StockLedger.create(wid, 'inbound', 'inbound:1', pid, 10, '码')
        conn = get_db()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM stock_ledger WHERE uniq_key = 'inbound:1'")
        conn.rollback()
        conn.close()

    def test_row_survives_blocked_update(self):
        """被拦下之后,原记录完好无损(没有半改)。"""
        from models import StockLedger, get_db
        pid = self._make_product('T003')
        wid = self._wh()
        StockLedger.create(wid, 'inbound', 'inbound:1', pid, 10, '码')
        conn = get_db()
        try:
            conn.execute("UPDATE stock_ledger SET quantity = 999 WHERE uniq_key = 'inbound:1'")
        except sqlite3.IntegrityError:
            pass
        conn.rollback()
        conn.close()
        self.assertAlmostEqual(StockLedger.get_by_uniq_key('inbound:1')['quantity'], 10)

    def test_insert_still_allowed(self):
        """禁改不禁增 —— 红字冲销靠 INSERT 实现。"""
        from models import StockLedger
        pid = self._make_product('T004')
        wid = self._wh()
        StockLedger.create(wid, 'inbound', 'inbound:1', pid, 10, '码')
        StockLedger.create(wid, 'inbound', 'inbound:1:rev', pid, -10, '码', is_reversal=1)
        self.assertAlmostEqual(StockLedger.sum_by_product(wid, pid), 0)

    def test_insert_or_replace_is_blocked(self):
        """⭐ SQLite 默认 recursive_triggers=0 时,REPLACE 删旧行**不触发** DELETE 触发器
        → 不加第 3 条触发器,就能无声把数量从 10 改成 999。必须拦住。"""
        from models import StockLedger, get_db
        pid = self._make_product('T005')
        wid = self._wh()
        StockLedger.create(wid, 'inbound', 'inbound:1', pid, 10, '码')
        conn = get_db()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT OR REPLACE INTO stock_ledger "
                "(warehouse_id, biz_type, uniq_key, product_id, quantity, unit, created_at) "
                "VALUES (?, 'inbound', 'inbound:1', ?, 999, '码', '2026-11-16 00:00:00')",
                (wid, pid))
        conn.rollback()
        conn.close()
        # 原记录没被动过
        self.assertAlmostEqual(StockLedger.get_by_uniq_key('inbound:1')['quantity'], 10)

    def test_upsert_do_update_is_blocked(self):
        """ON CONFLICT DO UPDATE 同样必须被拦。"""
        from models import StockLedger, get_db
        pid = self._make_product('T006')
        wid = self._wh()
        StockLedger.create(wid, 'inbound', 'inbound:1', pid, 10, '码')
        conn = get_db()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute(
                'INSERT INTO stock_ledger (warehouse_id, biz_type, uniq_key, product_id, '
                "quantity, unit, created_at) VALUES (?, 'inbound', 'inbound:1', ?, 999, '码', "
                "'2026-11-16 00:00:00') "
                'ON CONFLICT(uniq_key) DO UPDATE SET quantity = 999',
                (wid, pid))
        conn.rollback()
        conn.close()
        self.assertAlmostEqual(StockLedger.get_by_uniq_key('inbound:1')['quantity'], 10)

    def test_create_if_absent_is_idempotent(self):
        """⭐幂等正规入口:先查后写 → 第二次不写、返回原 id。"""
        from models import StockLedger
        pid = self._make_product('T007')
        wid = self._wh()
        lid1, created1 = StockLedger.create_if_absent(wid, 'inbound', 'inbound:1', pid, 10, '码')
        lid2, created2 = StockLedger.create_if_absent(wid, 'inbound', 'inbound:1', pid, 10, '码')
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(lid1, lid2)
        self.assertEqual(len(StockLedger.list_by_product(wid, pid)), 1)

    def test_create_if_absent_writes_new(self):
        from models import StockLedger
        pid = self._make_product('T008')
        wid = self._wh()
        lid, created = StockLedger.create_if_absent(wid, 'inbound', 'inbound:1', pid, 10, '码',
                                                    occurred_at='2026-11-01')
        self.assertTrue(created)
        self.assertEqual(StockLedger.get_by_uniq_key('inbound:1')['occurred_at'], '2026-11-01')


# ── StockPosting ─────────────────────────────────────────────────
class TestStockPosting(_Base):
    def test_create_and_fetch(self):
        from models import StockPosting
        wid = self._wh()
        pid = StockPosting.create('inbound', 55, wid, operator_id=3, operator_name='张三')
        row = StockPosting.get_by_id(pid)
        self.assertEqual(row['biz_type'], 'inbound')
        self.assertEqual(row['order_pk'], 55)
        self.assertEqual(row['warehouse_id'], wid)
        self.assertEqual(row['status'], 'posted')
        self.assertEqual(row['operator_name'], '张三')
        self.assertEqual(row['posted_count'], 0)

    def test_get_by_order(self):
        from models import StockPosting
        wid = self._wh()
        StockPosting.create('inbound', 77, wid)
        row = StockPosting.get_by_order('inbound', 77)
        self.assertIsNotNone(row)
        self.assertEqual(row['order_pk'], 77)
        self.assertIsNone(StockPosting.get_by_order('outbound', 77))

    def test_update_stats(self):
        from models import StockPosting
        wid = self._wh()
        pid = StockPosting.create('inbound', 88, wid)
        StockPosting.update_stats(pid, posted_count=5, posted_qty=123.5, skipped_count=2, note='ok')
        row = StockPosting.get_by_id(pid)
        self.assertEqual(row['posted_count'], 5)
        self.assertAlmostEqual(row['posted_qty'], 123.5)
        self.assertEqual(row['skipped_count'], 2)
        self.assertEqual(row['note'], 'ok')

    def test_update_stats_no_fields(self):
        from models import StockPosting
        wid = self._wh()
        pid = StockPosting.create('inbound', 89, wid)
        self.assertFalse(StockPosting.update_stats(pid))

    def test_mark_reversed(self):
        """冲销只改状态,批次留着(可追溯)。"""
        from models import StockPosting
        wid = self._wh()
        pid = StockPosting.create('inbound', 90, wid)
        StockPosting.mark_reversed(pid)
        row = StockPosting.get_by_id(pid)
        self.assertEqual(row['status'], 'reversed')
        self.assertIsNotNone(row['reversed_at'])

    def test_list_recent(self):
        from models import StockPosting
        wid = self._wh()
        for i in range(3):
            StockPosting.create('inbound', 100 + i, wid)
        StockPosting.create('outbound', 200, wid)
        rows = StockPosting.list_recent(limit=10)
        self.assertEqual(len(rows), 4)
        self.assertEqual(len(StockPosting.list_recent(limit=10, biz_type='outbound')), 1)


# ── StockPostingSkip ─────────────────────────────────────────────
class TestStockPostingSkip(_Base):
    def test_create_and_list(self):
        from models import StockPosting, StockPostingSkip
        wid = self._wh()
        posting_id = StockPosting.create('inbound', 1, wid)
        sid = StockPostingSkip.create(posting_id, 1, 11, '缺货品', '0.4', '5', '码',
                                       'no_product', '找不到对应商品')
        self.assertIsNotNone(sid)
        rows = StockPostingSkip.list_by_posting(posting_id)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['reason_code'], 'no_product')
        self.assertEqual(rows[0]['raw_quantity'], '5')
        self.assertEqual(rows[0]['resolved'], 0)

    def test_list_unresolved_and_resolve(self):
        from models import StockPosting, StockPostingSkip
        wid = self._wh()
        posting_id = StockPosting.create('inbound', 2, wid)
        sid = StockPostingSkip.create(posting_id, 2, 21, '品', '', '1', '码', 'bad_qty', '数量非法')
        self.assertEqual(len(StockPostingSkip.list_unresolved()), 1)
        StockPostingSkip.mark_resolved(sid)
        self.assertEqual(len(StockPostingSkip.list_unresolved()), 0)

    def test_count_by_reason(self):
        from models import StockPosting, StockPostingSkip
        wid = self._wh()
        posting_id = StockPosting.create('inbound', 3, wid)
        StockPostingSkip.create(posting_id, 3, 31, 'A', '', '1', '码', 'no_product', 'x')
        StockPostingSkip.create(posting_id, 3, 32, 'B', '', '1', '码', 'no_product', 'x')
        StockPostingSkip.create(posting_id, 3, 33, 'C', '', '1', '码', 'not_tracking', 'x')
        self.assertEqual(
            StockPostingSkip.count_by_reason(posting_id),
            {'no_product': 2, 'not_tracking': 1})

    def test_reason_codes_partition(self):
        """可补 vs 不可补 的两类,必须不重不漏。"""
        from models import StockPostingSkip
        self.assertEqual(
            set(StockPostingSkip.REASON_CODES),
            set(StockPostingSkip.RESOLVABLE) | {'not_tracking'})
        self.assertNotIn('not_tracking', StockPostingSkip.RESOLVABLE)

    def test_raw_quantity_zero_is_kept(self):
        """数量 0 必须存成 '0' 而不是空串 —— 否则页面分不清「数量0」和「没数量」。"""
        from models import StockPosting, StockPostingSkip
        wid = self._wh()
        posting_id = StockPosting.create('inbound', 4, wid)
        StockPostingSkip.create(posting_id, 4, 41, '零货', '', 0, '码', 'bad_qty', '数量非法')
        row = StockPostingSkip.list_by_posting(posting_id)[0]
        self.assertEqual(row['raw_quantity'], '0')

    def test_raw_quantity_none_is_empty(self):
        from models import StockPosting, StockPostingSkip
        wid = self._wh()
        posting_id = StockPosting.create('inbound', 5, wid)
        StockPostingSkip.create(posting_id, 5, 51, '无货', '', None, '', 'no_product', '缺货')
        row = StockPostingSkip.list_by_posting(posting_id)[0]
        self.assertEqual(row['raw_quantity'], '')


# ── StockCount ───────────────────────────────────────────────────
class TestStockCount(_Base):
    def test_create_empty(self):
        from models import StockCount
        wid = self._wh()
        cid = StockCount.create('11月盘点', warehouse_id=wid, remark='测试')
        row = StockCount.get_by_id(cid)
        self.assertEqual(row['status'], 'open')
        self.assertEqual(row['title'], '11月盘点')
        self.assertEqual(row['warehouse_id'], wid)
        self.assertEqual(row['total_items'], 0)
        self.assertEqual(row['counted_items'], 0)
        self.assertEqual(row['diff_items'], 0)

    def test_create_with_products_snapshots_book_quantity(self):
        """建单时快照账面数。"""
        from models import ProductStock, StockCount
        wid = self._wh()
        pid = self._make_product('C001')
        ProductStock.set_quantity(wid, pid, 66)
        cid = StockCount.create('盘点', warehouse_id=wid, product_ids=[pid])
        row = StockCount.get_by_id(cid)
        self.assertEqual(row['total_items'], 1)
        self.assertEqual(row['counted_items'], 0)
        from models import StockCountItem
        item = StockCountItem.get(cid, pid)
        self.assertAlmostEqual(item['book_quantity'], 66)
        self.assertIsNone(item['actual_quantity'])

    def test_close_and_reopen(self):
        from models import StockCount
        wid = self._wh()
        cid = StockCount.create('盘点', warehouse_id=wid)
        self.assertTrue(StockCount.close(cid))
        self.assertEqual(StockCount.get_by_id(cid)['status'], 'closed')
        self.assertIsNotNone(StockCount.get_by_id(cid)['closed_at'])
        self.assertTrue(StockCount.reopen(cid))
        self.assertEqual(StockCount.get_by_id(cid)['status'], 'open')
        self.assertIsNone(StockCount.get_by_id(cid)['closed_at'])

    def test_close_twice_returns_false(self):
        from models import StockCount
        wid = self._wh()
        cid = StockCount.create('盘点', warehouse_id=wid)
        self.assertTrue(StockCount.close(cid))
        self.assertFalse(StockCount.close(cid))

    def test_update_meta(self):
        from models import StockCount
        wid = self._wh()
        cid = StockCount.create('盘点', warehouse_id=wid)
        StockCount.update_meta(cid, title='改名了', remark='备注')
        row = StockCount.get_by_id(cid)
        self.assertEqual(row['title'], '改名了')
        self.assertEqual(row['remark'], '备注')

    def test_list_all_filter(self):
        from models import StockCount
        wid = self._wh()
        a = StockCount.create('甲盘点', warehouse_id=wid)
        StockCount.create('乙盘点', warehouse_id=wid)
        StockCount.close(a)
        self.assertEqual(len(StockCount.list_all()), 2)
        self.assertEqual(len(StockCount.list_all(status='open')), 1)
        self.assertEqual(len(StockCount.list_all(status='closed')), 1)
        self.assertEqual(len(StockCount.list_all(warehouse_id=wid)), 2)

    def test_delete_removes_items_first(self):
        """FK 是 RESTRICT —— 删单必须先删明细,否则报错。"""
        from models import StockCount
        wid = self._wh()
        pid = self._make_product('C002')
        cid = StockCount.create('盘点', warehouse_id=wid, product_ids=[pid])
        self.assertTrue(StockCount.delete(cid))
        self.assertIsNone(StockCount.get_by_id(cid))
        from models import get_db
        conn = get_db()
        n = conn.execute('SELECT COUNT(*) FROM stock_count_items WHERE count_id=?', (cid,)).fetchone()[0]
        conn.close()
        self.assertEqual(n, 0)

    def test_refresh_stats_counts_diff(self):
        from models import StockCount, StockCountItem
        wid = self._wh()
        pid1 = self._make_product('C003')
        pid2 = self._make_product('C004')
        cid = StockCount.create('盘点', warehouse_id=wid, product_ids=[pid1, pid2])
        StockCountItem.set_actual(cid, pid1, 10)   # 与账面 0 有差异
        StockCountItem.set_actual(cid, pid2, 0)    # 与账面 0 相同
        row = StockCount.get_by_id(cid)
        self.assertEqual(row['total_items'], 2)
        self.assertEqual(row['counted_items'], 2)
        self.assertEqual(row['diff_items'], 1)

    def test_as_of_written_on_create(self):
        """⭐A5:建单即写 as_of(盘点基准时点)。"""
        from models import StockCount
        wid = self._wh()
        cid = StockCount.create('盘点', warehouse_id=wid)
        row = StockCount.get_by_id(cid)
        self.assertTrue(row['as_of'], 'as_of 建单时没写')
        self.assertRegex(row['as_of'], r'^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$')

    def test_as_of_can_be_explicit(self):
        from models import StockCount
        wid = self._wh()
        cid = StockCount.create('盘点', warehouse_id=wid, as_of='2026-11-10 08:00:00')
        self.assertEqual(StockCount.get_by_id(cid)['as_of'], '2026-11-10 08:00:00')

    def test_as_of_updatable(self):
        from models import StockCount
        wid = self._wh()
        cid = StockCount.create('盘点', warehouse_id=wid)
        StockCount.update_meta(cid, as_of='2026-11-09 09:00:00')
        self.assertEqual(StockCount.get_by_id(cid)['as_of'], '2026-11-09 09:00:00')


# ── StockCountItem ───────────────────────────────────────────────
class TestStockCountItem(_Base):
    def test_set_actual_creates_row(self):
        """没在清单里的商品也能现场补录(UPSERT)。"""
        from models import StockCount, StockCountItem
        wid = self._wh()
        pid = self._make_product('I001')
        cid = StockCount.create('盘点', warehouse_id=wid)
        item = StockCountItem.set_actual(cid, pid, 12.5)
        self.assertAlmostEqual(item['actual_quantity'], 12.5)
        self.assertIsNotNone(item['counted_at'])

    def test_set_actual_upsert(self):
        from models import StockCount, StockCountItem
        wid = self._wh()
        pid = self._make_product('I002')
        cid = StockCount.create('盘点', warehouse_id=wid)
        StockCountItem.set_actual(cid, pid, 10)
        StockCountItem.set_actual(cid, pid, 20)
        item = StockCountItem.get(cid, pid)
        self.assertAlmostEqual(item['actual_quantity'], 20)
        from models import get_db
        conn = get_db()
        n = conn.execute('SELECT COUNT(*) FROM stock_count_items WHERE count_id=?', (cid,)).fetchone()[0]
        conn.close()
        self.assertEqual(n, 1)

    def test_set_actual_none_clears(self):
        """清空 = 回到待录。"""
        from models import StockCount, StockCountItem
        wid = self._wh()
        pid = self._make_product('I003')
        cid = StockCount.create('盘点', warehouse_id=wid)
        StockCountItem.set_actual(cid, pid, 10)
        StockCountItem.set_actual(cid, pid, None)
        item = StockCountItem.get(cid, pid)
        self.assertIsNone(item['actual_quantity'])
        self.assertIsNone(item['counted_at'])
        self.assertEqual(StockCount.get_by_id(cid)['counted_items'], 0)

    def test_unique_count_product(self):
        from models import StockCount, StockCountItem
        wid = self._wh()
        pid = self._make_product('I004')
        cid = StockCount.create('盘点', warehouse_id=wid, product_ids=[pid])
        StockCountItem.bulk_create(cid, [(pid, 5)])
        from models import get_db
        conn = get_db()
        n = conn.execute('SELECT COUNT(*) FROM stock_count_items WHERE count_id=?', (cid,)).fetchone()[0]
        conn.close()
        self.assertEqual(n, 1)

    def test_list_by_count_joins_product(self):
        from models import StockCount, StockCountItem
        wid = self._wh()
        pid = self._make_product('I005', name='明细胶', spec='0.9')
        cid = StockCount.create('盘点', warehouse_id=wid, product_ids=[pid])
        rows = StockCountItem.list_by_count(cid)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['product_name'], '明细胶')
        self.assertEqual(rows[0]['specification'], '0.9')

    def test_list_unresolved(self):
        from models import StockCount, StockCountItem
        wid = self._wh()
        pid1 = self._make_product('I006')
        pid2 = self._make_product('I007')
        cid = StockCount.create('盘点', warehouse_id=wid, product_ids=[pid1, pid2])
        StockCountItem.set_actual(cid, pid1, 5)
        self.assertEqual(len(StockCountItem.list_unresolved(cid)), 1)

    def test_delete_item(self):
        from models import StockCount, StockCountItem
        wid = self._wh()
        pid = self._make_product('I008')
        cid = StockCount.create('盘点', warehouse_id=wid, product_ids=[pid])
        StockCountItem.delete(cid, pid)
        self.assertIsNone(StockCountItem.get(cid, pid))
        self.assertEqual(StockCount.get_by_id(cid)['total_items'], 0)

    def test_adhoc_row_snapshots_current_balance(self):
        """现场补录(不在清单里)→ book_quantity 取当前账面数,不是 0。"""
        from models import ProductStock, StockCount, StockCountItem
        wid = self._wh()
        pid = self._make_product('I009')
        ProductStock.set_quantity(wid, pid, 55)
        cid = StockCount.create('盘点', warehouse_id=wid)
        item = StockCountItem.set_actual(cid, pid, 60)
        self.assertAlmostEqual(item['book_quantity'], 55)
        self.assertAlmostEqual(item['actual_quantity'], 60)

    def test_existing_row_keeps_build_snapshot(self):
        """清单内的行 → book_quantity 保持建单时快照,不被后来录数改写。"""
        from models import ProductStock, StockCount, StockCountItem
        wid = self._wh()
        pid = self._make_product('I010')
        ProductStock.set_quantity(wid, pid, 20)
        cid = StockCount.create('盘点', warehouse_id=wid, product_ids=[pid])
        # 盘点期间又入了库 → 当前余额变了,但建单快照不该变
        ProductStock.set_quantity(wid, pid, 999)
        StockCountItem.set_actual(cid, pid, 25)
        item = StockCountItem.get(cid, pid)
        self.assertAlmostEqual(item['book_quantity'], 20)


# ── 外键与跨表约束 ───────────────────────────────────────────────
class TestForeignKeys(_Base):
    def test_fk_on_delete_restricts_product(self):
        """有库存行的商品不允许直接删(FK 真的开着)。"""
        from models import ProductStock, get_db
        pid = self._make_product('F001')
        ProductStock.ensure(self._wh(), pid)
        conn = get_db()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute('DELETE FROM product WHERE product_id=?', (pid,))
        conn.rollback()
        conn.close()

    def test_fk_on_delete_restricts_warehouse(self):
        from models import ProductStock, get_db
        pid = self._make_product('F002')
        wid = self._wh()
        ProductStock.ensure(wid, pid)
        conn = get_db()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute('DELETE FROM warehouses WHERE id=?', (wid,))
        conn.rollback()
        conn.close()

    def test_order_table_has_warehouse_id_default_1(self):
        """三张订单表都有 warehouse_id,默认 1。"""
        from models import get_db
        conn = get_db()
        for t in ('inbound_orders', 'shipping_orders', 'loading_orders'):
            cols = {r['name']: r for r in conn.execute(f'PRAGMA table_info({t})')}
            self.assertIn('warehouse_id', cols, f'{t} 缺 warehouse_id')
            self.assertEqual(cols['warehouse_id']['notnull'], 1)
            self.assertEqual(str(cols['warehouse_id']['dflt_value']), '1')
        conn.close()

    def test_new_order_row_defaults_warehouse_1(self):
        """不显式给 warehouse_id 时,新行落在默认仓。"""
        from models import get_db
        conn = get_db()
        conn.execute("INSERT INTO inbound_orders (date, supplier, is_locked, created_at) "
                     "VALUES ('2026-11-16', '测试供应商', 0, '2026-11-16 00:00:00')")
        conn.commit()
        row = conn.execute('SELECT warehouse_id FROM inbound_orders ORDER BY id DESC LIMIT 1').fetchone()
        conn.close()
        self.assertEqual(row['warehouse_id'], 1)


# ── 对账脚本(阶段 1.5 / ⭐A1)────────────────────────────────────
class TestVerifyStockBalanceTool(_Base):
    """tools/verify_stock_balance.py —— 余额可重算是硬不变量,不是可选工具。"""

    def _run(self, *args):
        import subprocess
        tool = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            'tools', 'verify_stock_balance.py')
        # ⚠️ 必须钉死 UTF-8 —— Windows 中文 locale 下管道默认是 GBK,
        #    工具输出中文会被解码成乱码甚至抛 UnicodeDecodeError
        env = dict(os.environ, PYTHONIOENCODING='utf-8')
        r = subprocess.run([sys.executable, tool, '--db', self.tmp.name, *args],
                           capture_output=True, text=True, encoding='utf-8',
                           errors='replace', env=env, timeout=120)
        return r.returncode, (r.stdout or '') + (r.stderr or '')

    def _seed(self, ledger_rows, cache_rows):
        """ledger_rows: [(uniq_key, qty, occurred_at, balance_after)]"""
        from models import get_db
        pid = self._make_product('VB001')   # product_stock 的 FK 指向 product,得先有真实商品
        conn = get_db()
        for key, qty, occurred, ba in ledger_rows:
            conn.execute(
                'INSERT INTO stock_ledger (warehouse_id, biz_type, uniq_key, product_id, '
                'quantity, unit, occurred_at, balance_after, created_at) '
                "VALUES (1, 'inbound', ?, ?, ?, '码', ?, ?, '2026-11-16 00:00:00')",
                (key, pid, qty, occurred, ba))
        for qty in cache_rows:
            conn.execute('INSERT INTO product_stock (warehouse_id, product_id, quantity) '
                         'VALUES (1, ?, ?)', (pid, qty))
        conn.commit()
        conn.close()

    def test_clean_ledger_passes(self):
        self._seed([('inbound:1', 10.0, '2026-11-01', 10.0)], [10.0])
        code, out = self._run()
        self.assertEqual(code, 0, out)
        self.assertIn('对账通过', out)

    def test_cache_mismatch_is_detected(self):
        self._seed([('inbound:1', 10.0, '2026-11-01', 10.0)], [7.0])
        code, out = self._run()
        self.assertEqual(code, 1, out)
        self.assertIn('缓存 vs 流水累计', out)

    def test_drill_mode_does_not_write(self):
        """演练模式只报告,不改库。"""
        self._seed([('inbound:1', 10.0, '2026-11-01', 10.0)], [7.0])
        self._run()
        from models import get_db
        conn = get_db()
        qty = conn.execute('SELECT quantity FROM product_stock').fetchone()[0]
        conn.close()
        self.assertAlmostEqual(qty, 7.0, msg='演练模式改了库!')

    def test_apply_rebuilds_cache_from_ledger(self):
        """--apply 以流水为准重算缓存(不改流水)。"""
        self._seed([('inbound:1', 10.0, '2026-11-01', 10.0)], [7.0])
        code, out = self._run('--apply')
        self.assertEqual(code, 0, out)
        from models import get_db
        conn = get_db()
        qty = conn.execute('SELECT quantity FROM product_stock').fetchone()[0]
        n_ledger = conn.execute('SELECT COUNT(*) FROM stock_ledger').fetchone()[0]
        conn.close()
        self.assertAlmostEqual(qty, 10.0)
        self.assertEqual(n_ledger, 1, '--apply 动了流水')

    def test_occurred_at_empty_is_detected(self):
        self._seed([('inbound:1', 10.0, '', 10.0)], [10.0])
        code, out = self._run()
        self.assertEqual(code, 1, out)
        self.assertIn('occurred_at', out)

    def test_chain_break_is_detected(self):
        """balance_after 链断 → 报告(且 --apply 也修不了,流水只追加)。"""
        self._seed([('inbound:1', 10.0, '2026-11-01', 10.0),
                    ('inbound:2', 5.0, '2026-11-02', 99.0)], [15.0])
        code, out = self._run()
        self.assertEqual(code, 1, out)
        self.assertIn('balance_after', out)

    def test_orphan_ledger_is_detected(self):
        """有流水但没有缓存行 → 孤儿流水。"""
        self._seed([('inbound:1', 10.0, '2026-11-01', 10.0)], [])
        code, out = self._run()
        self.assertEqual(code, 1, out)
        self.assertIn('孤儿流水', out)


# ── 公共约定:模型层 re-export ────────────────────────────────────
class TestReExports(unittest.TestCase):
    def test_models_package_exports(self):
        from models import (Warehouse, ProductStock, StockLedger, StockPosting,
                            StockPostingSkip, StockCount, StockCountItem)
        for cls in (Warehouse, ProductStock, StockLedger, StockPosting,
                    StockPostingSkip, StockCount, StockCountItem):
            self.assertTrue(callable(cls))


if __name__ == '__main__':
    unittest.main()
