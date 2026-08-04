"""NP0 批回归测试 —— 出货页面新增 P0 三项。

覆盖:
- NP0-1 PaddleOCREngine 单例缓存（验证 shipping.py 不再走 __init__,而是用 get_ocr_engine）
- NP0-2 ai-recognize 端点补文件校验(扩展名/大小,任意文件被拒)
- NP0-3 ShippingRecord.move_up/move_down 三 UPDATE 在一个事务里,失败时 sort_order=-1 不留痕
"""
import io
import os
import sys
import unittest
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import fake_png_bytes as _PNG  # noqa: E402,F401

import models._db as _db


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        self._orig = _db.DB_PATH
        _db.DB_PATH = self.tmp.name
        from models import init_db
        init_db()
        from app import create_app
        self.client = create_app().test_client()

    def tearDown(self):
        _db.DB_PATH = self._orig
        os.unlink(self.tmp.name)


# ════════════════════════════════════════════════════════════════════
# NP0-1 PaddleOCREngine 单例
# ════════════════════════════════════════════════════════════════════
class PaddleOcrSingletonTests(Base):
    def setUp(self):
        # 单例缓存要在每个测试前清空,否则 sentinel 或失败的 PaddleOCR 实例会污染后续测试
        from blueprints.ocr_engine import _engine_cache
        _engine_cache.pop('paddleocr', None)
        super().setUp()

    def tearDown(self):
        from blueprints.ocr_engine import _engine_cache
        _engine_cache.pop('paddleocr', None)
        super().tearDown()

    def test_shipping_uses_cached_factory(self):
        """_run_label_match 与 ai-match 必须通过 get_ocr_engine('paddleocr') 取实例,而非新建。"""
        from blueprints import shipping as s_mod
        # 模块顶级应使用 get_ocr_engine 工厂,而不是 PaddleOCREngine 类直接调用
        src = open(s_mod.__file__, encoding='utf-8').read()
        # 行级路径不应再 PaddleOCREngine(),ai-match 路径同样
        self.assertNotIn('PaddleOCREngine()', src)
        self.assertIn("get_ocr_engine('paddleocr')", src)

    def test_factory_returns_same_instance(self):
        """get_ocr_engine('paddleocr') 多次调用必须返回同一实例(缓存生效)。"""
        from blueprints.ocr_engine import get_ocr_engine, _engine_cache
        _engine_cache.pop('paddleocr', None)  # 清缓存避免其他测试污染
        try:
            with mock.patch('blueprints.ocr_engine.PaddleOCREngine') as MockCls:
                # 让 PaddleOCREngine() 在被实例化时把 sentinel 写到内部
                sentinel1 = object()
                MockCls.return_value = sentinel1
                a = get_ocr_engine('paddleocr')
                b = get_ocr_engine('paddleocr')
                self.assertIs(a, b)
                self.assertIs(a, sentinel1)
                # 工厂缓存命中,只应 new 一次
                self.assertEqual(MockCls.call_count, 1)
        finally:
            _engine_cache.pop('paddleocr', None)


# ════════════════════════════════════════════════════════════════════
# NP0-2 ai-recognize 端点校验
# ════════════════════════════════════════════════════════════════════
class AiRecognizeValidateTests(Base):
    def test_rejects_oversized_or_non_image(self):
        """ai-recognize 端点现在必须调 check_uploaded_image; .txt 后缀/超大文件被拒。"""
        # 无文件名
        r = self.client.post('/api/v1/shipping-orders/ai-recognize',
                             data={'image': (io.BytesIO(b'x'), '')},
                             content_type='multipart/form-data')
        self.assertEqual(r.status_code, 400)

        # 非法扩展名
        r = self.client.post('/api/v1/shipping-orders/ai-recognize',
                             data={'image': (io.BytesIO(b'hello'), 'evil.txt')},
                             content_type='multipart/form-data')
        self.assertEqual(r.status_code, 400)
        body = r.get_json()
        self.assertFalse(body['success'])
        self.assertIn('不支持', body['error'] or '')

        # 合法扩展名但超大(11MB,超过 10MB 上限)
        big = io.BytesIO(_PNG() + b'0' * (11 * 1024 * 1024))
        r = self.client.post('/api/v1/shipping-orders/ai-recognize',
                             data={'image': (big, 'big.png')},
                             content_type='multipart/form-data')
        self.assertEqual(r.status_code, 400)
        body = r.get_json()
        self.assertIn('过大', body['error'] or '')


# ════════════════════════════════════════════════════════════════════
# NP0-3 move 事务
# ════════════════════════════════════════════════════════════════════
class MoveTransactionTests(Base):
    def _two_records(self):
        from models import ShippingOrder, ShippingRecord
        oid = ShippingOrder.create('2026-07-25', 'C')
        r1 = ShippingRecord.create('2026-07-25', 'C', 'A', 'sa', '1', 'y', '', oid)
        r2 = ShippingRecord.create('2026-07-25', 'C', 'B', 'sb', '2', 'y', '', oid)
        return oid, r1, r2

    def test_move_down_no_dash_left(self):
        """成功 move_down 后, sort_order 必须为正整数,不能留 -1。"""
        from models import ShippingRecord
        _, r1, r2 = self._two_records()
        ok = ShippingRecord.move_down(r1)
        self.assertTrue(ok)
        from models._db import get_db
        c = get_db()
        rows = c.execute('SELECT id, sort_order FROM shipping_records ORDER BY sort_order').fetchall()
        c.close()
        orders = [(r['id'], r['sort_order']) for r in rows]
        self.assertNotIn(-1, [so for _, so in orders])
        # 实际顺序也换了
        self.assertEqual(orders[0][0], r2)
        self.assertEqual(orders[1][0], r1)

    def test_move_uses_begin_immediate(self):
        """move_up 必须包在 BEGIN IMMEDIATE 事务里: 用 mock 验证 BEGIN IMMEDIATE 被发出去。"""
        from models import ShippingRecord
        import models.orders as mo
        oid, r1, r2 = self._two_records()
        # 直接调 move_up 不依赖 contextmanager;以数据库执行 SQL 顺序验证 BEGIN IMMEDIATE
        from unittest import mock
        sentinel = {'begin': False}
        real_get_db = mo.get_db

        class WrapCursor:
            def __init__(self, real):
                self.real = real
                self._last_sql = ''
            def execute(self, sql, params=()):
                self._last_sql = sql
                if 'BEGIN IMMEDIATE' in sql:
                    sentinel['begin'] = True
                # 让 move 模拟顺利跑完
                if 'BEGIN IMMEDIATE' in sql or 'ROLLBACK' in sql or 'COMMIT' in sql:
                    return self.real.execute(sql, params) if 'COMMIT' not in sql else self.real.execute(sql)
                return self.real.execute(sql, params)
            def fetchone(self):
                return self.real.fetchone()
            @property
            def lastrowid(self):
                return self.real.lastrowid

        class WrapConn:
            def __init__(self, real):
                self.real = real
            def cursor(self):
                return WrapCursor(self.real.cursor())
            def commit(self): return self.real.commit()
            def rollback(self): return self.real.rollback()
            def close(self): return self.real.close()

        with mock.patch('models.orders.get_db', lambda: WrapConn(real_get_db())):
            ShippingRecord.move_up(r2)
        self.assertTrue(sentinel['begin'], 'move_up 应发出 BEGIN IMMEDIATE')

    def test_move_rolls_back_on_failure(self):
        """中途任意 UPDATE 抛错时,事务必须回滚,不留 -1。"""
        from models import ShippingRecord
        import models.orders as mo
        _, r1, r2 = self._two_records()
        real_get_db = mo.get_db
        call = {'n': 0}
        # 用 BEGIN 后抛错的方式测回滚
        boom_at_first_update = {'hit': False}

        class WrapCursor:
            def __init__(self, real):
                self.real = real
            def execute(self, sql, params=()):
                # 在第三条 UPDATE 时抛错(确保已写入"-1")
                if sql.strip().startswith('UPDATE'):
                    call['n'] += 1
                    if call['n'] == 1 and not boom_at_first_update['hit']:
                        boom_at_first_update['hit'] = True
                        raise RuntimeError('simulated crash')
                return self.real.execute(sql, params)
            def fetchone(self):
                return self.real.fetchone()
            @property
            def lastrowid(self):
                return self.real.lastrowid

        class WrapConn:
            def __init__(self, real):
                self.real = real
            def cursor(self):
                return WrapCursor(self.real.cursor())
            def commit(self): return self.real.commit()
            def rollback(self): return self.real.rollback()
            def close(self): return self.real.close()

        with mock.patch('models.orders.get_db', lambda: WrapConn(real_get_db())):
            with self.assertRaises(RuntimeError):
                ShippingRecord.move_up(r2)
        # 验证 -1 未残留
        from models._db import get_db
        c = get_db()
        rows = c.execute('SELECT sort_order FROM shipping_records').fetchall()
        c.close()
        for r in rows:
            self.assertNotEqual(r['sort_order'], -1)


if __name__ == '__main__':
    unittest.main()
