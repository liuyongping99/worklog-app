"""入库模型层 —— 新增字段/方法。

覆盖 (Task 2 of 2026-08-13 入库对齐出货):
- InboundOrder.set_img_cols(order_id, cols) — UPDATE inbound_orders.img_cols
- InboundImage.create(source_tag=...) → InboundImage.get_all_by_orders(...) → source_tag 落库并回读
- InboundRecord.get_verified_warnings(record_id) — 默认空 dict

运行:PYTHONUTF8=1 python -m pytest tests/test_inbound_model.py -q -p no:warnings
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models._db as _db


class _TempDb(unittest.TestCase):
    """每个测试一个独立临时 db（避免污染生产 worklog.db）。"""

    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        self._orig = _db.DB_PATH
        _db.DB_PATH = self.tmp.name
        from models import init_db
        init_db()

    def tearDown(self):
        _db.DB_PATH = self._orig
        for ext in ('', '-wal', '-shm'):
            p = self.tmp.name + ext
            if os.path.exists(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass


class InboundModelTests(_TempDb):

    def test_inbound_order_set_img_cols(self):
        """InboundOrder.set_img_cols 应 UPDATE inbound_orders.img_cols 列。"""
        from models import InboundOrder
        oid = InboundOrder.create('2026-08-13', '供应商X')
        InboundOrder.set_img_cols(oid, 4)
        # 直接落库校验,避开 get_by_id 的字段过滤
        import sqlite3
        conn = sqlite3.connect(self.tmp.name)
        row = conn.execute('SELECT img_cols FROM inbound_orders WHERE id = ?', (oid,)).fetchone()
        conn.close()
        self.assertEqual(row[0], 4)

    def test_inbound_image_source_tag_roundtrip(self):
        """InboundImage.create 接受 source_tag,get_all_by_orders 回读含 source_tag key。"""
        from models import InboundOrder, InboundImage
        oid = InboundOrder.create('2026-08-13', '供应商Y')
        iid = InboundImage.create(
            order_pk=oid,
            file_path='/tmp/fake.png',
            source='manual',
            source_tag='备货照',
        )
        self.assertIsInstance(iid, int)
        result = InboundImage.get_all_by_orders([oid])
        self.assertIn(oid, result)
        self.assertEqual(len(result[oid]), 1)
        self.assertEqual(result[oid][0]['source_tag'], '备货照')

    def test_inbound_record_get_verified_warnings_default_empty(self):
        """新建明细的 verified_warnings 默认空 dict(不是 None)。"""
        from models import InboundOrder, InboundRecord
        oid = InboundOrder.create('2026-08-13', '供应商Z')
        rid = InboundRecord.create(oid, '品名', '规格', '10', 'y', '')
        result = InboundRecord.get_verified_warnings(rid)
        self.assertEqual(result, {})


if __name__ == '__main__':
    unittest.main()