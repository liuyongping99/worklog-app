"""出货页商品行 OCR 比对事件记录 —— 测试入口"""
import os
import sys
import unittest
import unittest.mock
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models._db as _db


class OcrMatchEventTableTests(unittest.TestCase):
    def test_create_table_idempotent(self):
        """init_db 跑两次不应报错；表存在 + 索引存在。"""
        tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        tmp.close()
        orig = _db.DB_PATH
        _db.DB_PATH = tmp.name
        try:
            from models import init_db
            init_db()
            init_db()  # 第二次必须不报错
            from models._db import get_db
            conn = get_db()
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='ocr_match_event'"
            ).fetchall()
            self.assertEqual(len(rows), 1, '表 ocr_match_event 应存在')
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_ocr_match_%'"
            ).fetchall()
            self.assertEqual(len(rows), 2, '应有 2 个索引')
            conn.close()
        finally:
            _db.DB_PATH = orig
            os.unlink(tmp.name)


class OcrMatchEventModelTests(unittest.TestCase):
    """Task 2: 模型类 CRUD + 不抛回"""
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        self._orig = _db.DB_PATH
        _db.DB_PATH = self.tmp.name
        from models import init_db, ShippingOrder, ShippingRecord
        init_db()
        self.oid = ShippingOrder.create('2026-07-25', 'C')
        self.rid = ShippingRecord.create('2026-07-25', 'C', 'P', 'S', '1', 'y', '', self.oid)

    def tearDown(self):
        _db.DB_PATH = self._orig
        for ext in ('', '-wal', '-shm'):
            p = self.tmp.name + ext
            if os.path.exists(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass

    def test_create_basic(self):
        from models import OcrMatchEvent
        eid = OcrMatchEvent.create('record_ocr', self.rid, self.oid, image_id=None,
                                   ai_match_status='green', ai_match_score=90.0,
                                   product_name='P', specification='S')
        self.assertIsNotNone(eid)

    def test_create_returns_none_on_failure(self):
        """create 内部异常不抛回,只返回 None + logger.error。"""
        from models import OcrMatchEvent
        with unittest.mock.patch('models._db.get_db', side_effect=RuntimeError('boom')):
            result = OcrMatchEvent.create('record_ocr', self.rid, self.oid)
        self.assertIsNone(result)

    def test_get_by_record_ordering(self):
        """get_by_record 应按时间升序(老→新)。"""
        from models import OcrMatchEvent
        OcrMatchEvent.create('record_ocr', self.rid, self.oid, ai_match_status='green')
        OcrMatchEvent.create('ai_match', self.rid, self.oid, ai_match_status='red')
        OcrMatchEvent.create('human_verify', self.rid, self.oid, human_status='green')
        rows = OcrMatchEvent.get_by_record(self.rid)
        types = [r['event_type'] for r in rows]
        self.assertEqual(types, ['record_ocr', 'ai_match', 'human_verify'])

    def test_get_by_record_filter_event_type(self):
        from models import OcrMatchEvent
        OcrMatchEvent.create('record_ocr', self.rid, self.oid)
        OcrMatchEvent.create('ai_match', self.rid, self.oid)
        rows = OcrMatchEvent.get_by_record(self.rid, event_type='ai_match')
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['event_type'], 'ai_match')

    def test_get_by_order_orders_by_record(self):
        """get_by_order: 多 record 时按 record_id 自然顺序,每个 record 内按时间升序。"""
        from models import ShippingRecord, OcrMatchEvent
        rid2 = ShippingRecord.create('2026-07-25', 'C', 'Q', 'sq', '2', 'y', '', self.oid)
        OcrMatchEvent.create('record_ocr', self.rid, self.oid)
        OcrMatchEvent.create('record_ocr', rid2, self.oid)
        rows = OcrMatchEvent.get_by_order(self.oid)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]['record_id'], self.rid)

    def test_get_ai_human_delta(self):
        """get_ai_human_delta 返回同 record 的 ai/human 字段对。"""
        from models import OcrMatchEvent
        OcrMatchEvent.create('ai_match', self.rid, self.oid, ai_match_status='red',
                             ai_match_reason='规格冲突')
        OcrMatchEvent.create('human_verify', self.rid, self.oid,
                             human_status='green', human_reason='标签印刷模糊')
        rows = OcrMatchEvent.get_ai_human_delta(self.rid)
        self.assertEqual(len(rows), 2)
        # 至少含 ai_match_status 与 human_status 字段
        keys_first = sorted(rows[0].keys())
        self.assertIn('event_type', keys_first)


if __name__ == '__main__':
    unittest.main()