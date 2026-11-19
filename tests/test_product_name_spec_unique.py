# -*- coding: utf-8 -*-
"""product 表「品名 + 规格」唯一约束(uk_product_name_spec,2026-11-16 新增)。

背景:
  库存系统靠「品名+规格」认货(订单明细表没有 product_code 列,用户拍板不加)。
  同一个 (品名,规格) 指向两个 SKU 时,引擎只能拒绝匹配 → 落「待人工」,库存永远对不上。
  数据库层面直接不让建,比事后发现强。

约束的精确语义(与唯一索引的键完全一致):
  · 键 = (product_name, COALESCE(specification, ''))
  · **空格原样保留** —— 'A1.5米 200g' 与 'A1.5米200g' 允许并存为两个 SKU
  · COALESCE 把 NULL 规格归成 '' → 拦得住「规格为空」的重复
    (SQLite 里 NULL 互不相同,普通 UNIQUE(name, spec) 拦不住)

被测:
  1. 索引存在且键是 COALESCE 表达式
  2. 精确重复 → ProductConflictError(400)
  3. 规格为空的重复 → ProductConflictError(400)
  4. 空格变体 → **允许**并存(不报错)
  5. 编号重复 → ProductConflictError(400)
  6. API 层:重复时返回 400 + 中文提示,不是 500 + sqlite 原文
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models._db as _db   # noqa: E402


class _TempDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        self._orig = _db.DB_PATH
        _db.DB_PATH = self.tmp.name
        from models import init_db
        init_db()
        from app import create_app
        self.client = create_app().test_client()
        from models.tasks_flow import StaffDB
        sid = StaffDB.create('test', '文员')['id']
        with self.client.session_transaction() as s:
            s['operator_id'] = sid
        self.cat_id = self._mk_category()

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
                     "VALUES ('T001', '测试类', NULL, 1, 1, 1)")
        conn.commit()
        cid = conn.execute("SELECT id FROM product_categories WHERE category_code='T001'").fetchone()[0]
        conn.close()
        return cid

    def _make(self, code, name, spec=''):
        from models import Product
        return Product.create(product_code=code, product_name=name, category_id=self.cat_id,
                              specification=spec, base_unit='码')


class TestIndexShape(unittest.TestCase):
    def test_index_exists_with_coalesce_key(self):
        """索引必须存在,且键是 COALESCE 表达式(否则 NULL 规格重复拦不住)。"""
        import sqlite3
        conn = sqlite3.connect(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'worklog.db'))
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='index' "
                           "AND name='uk_product_name_spec'").fetchone()
        conn.close()
        self.assertIsNotNone(row, 'uk_product_name_spec 索引不存在')
        self.assertIn('COALESCE', row[0].upper())


class TestUniqueness(_TempDb):
    def test_exact_duplicate_raises(self):
        from models.products import ProductConflictError
        self._make('C001', '测试胶', '0.4黑')
        with self.assertRaises(ProductConflictError) as cm:
            self._make('C002', '测试胶', '0.4黑')
        self.assertIn('已存在', cm.exception.message)

    def test_duplicate_with_empty_spec_raises(self):
        """规格为空也要拦 —— 普通 UNIQUE 拦不住的那类。"""
        from models.products import ProductConflictError
        self._make('C011', '测试蜡光纸', '')
        with self.assertRaises(ProductConflictError):
            self._make('C012', '测试蜡光纸', '')

    def test_space_variant_is_allowed(self):
        """空格原样保留 → 'A1.5米 200g' 与 'A1.5米200g' 允许并存。"""
        pid1 = self._make('C021', '测试无纺布', 'A1.5米 200g')
        pid2 = self._make('C022', '测试无纺布', 'A1.5米200g')
        self.assertNotEqual(pid1, pid2)

    def test_duplicate_product_code_raises(self):
        from models.products import ProductConflictError
        self._make('C031', '测试A', 'x')
        with self.assertRaises(ProductConflictError) as cm:
            self._make('C031', '测试B', 'y')
        self.assertIn('编号', cm.exception.message)

    def test_update_to_conflict_raises(self):
        from models.products import ProductConflictError
        from models import Product
        self._make('C041', '测试同品', '0.6')
        pid2 = self._make('C042', '测试异品', '0.8')
        with self.assertRaises(ProductConflictError):
            Product.update(pid2, {'product_name': '测试同品', 'specification': '0.6'})

    def test_update_self_no_conflict(self):
        """改自己的其它字段不算冲突(要排除自己)。"""
        from models import Product
        pid = self._make('C051', '测试自改', '0.9')
        Product.update(pid, {'specification': '0.9', 'model': '新型号'})
        from models import get_db
        conn = get_db()
        m = conn.execute('SELECT model FROM product WHERE product_id=?', (pid,)).fetchone()[0]
        conn.close()
        self.assertEqual(m, '新型号')


class TestApiError(unittest.TestCase):
    """API 层必须返回 400 + 中文提示,而不是 500 + sqlite 原文。"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        self._orig = _db.DB_PATH
        _db.DB_PATH = self.tmp.name
        from models import init_db
        init_db()
        from app import create_app
        self.client = create_app().test_client()
        from models.tasks_flow import StaffDB
        sid = StaffDB.create('test', '文员')['id']
        with self.client.session_transaction() as s:
            s['operator_id'] = sid
        from models import get_db
        conn = get_db()
        conn.execute("INSERT OR IGNORE INTO product_categories "
                     "(category_code, category_name, parent_id, level, sort_order, status) "
                     "VALUES ('T002', '测试类2', NULL, 1, 1, 1)")
        conn.commit()
        self.cat_id = conn.execute(
            "SELECT id FROM product_categories WHERE category_code='T002'").fetchone()[0]
        conn.close()

    def tearDown(self):
        _db.DB_PATH = self._orig
        for ext in ('', '-wal', '-shm'):
            p = self.tmp.name + ext
            if os.path.exists(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass

    def _post(self, code, name, spec):
        return self.client.post('/api/v1/products', json={
            'product_code': code, 'product_name': name, 'category_id': self.cat_id,
            'specification': spec, 'base_unit': '码',
        })

    def test_create_duplicate_returns_400_with_message(self):
        r1 = self._post('D001', '接口测试品', '0.4')
        self.assertEqual(r1.status_code, 201, r1.get_data(as_text=True))
        r2 = self._post('D002', '接口测试品', '0.4')
        self.assertEqual(r2.status_code, 400, r2.get_data(as_text=True))
        body = r2.get_json()
        self.assertFalse(body['success'])
        self.assertIn('已存在', body['error'])
        self.assertNotIn('UNIQUE constraint', body['error'])

    def test_create_space_variant_ok(self):
        r1 = self._post('D011', '接口空格品', 'A1.5米 200g')
        self.assertEqual(r1.status_code, 201, r1.get_data(as_text=True))
        r2 = self._post('D012', '接口空格品', 'A1.5米200g')
        self.assertEqual(r2.status_code, 201, r2.get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
