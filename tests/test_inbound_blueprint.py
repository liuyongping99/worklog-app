"""入库蓝图 — has_eco/has_jia_mian/qty_invalid/verified_warnings + img_cols PATCH + source_tag 入参。"""
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models._db as _db


def _drain_async_jobs(timeout=15):
    import time
    from blueprints.shipping import _ASYNC_JOBS
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not any(j.get('state') == 'processing' for j in list(_ASYNC_JOBS.values())):
            return
        time.sleep(0.02)
    raise AssertionError('后台 OCR 任务超时未结束')


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
        sid = StaffDB.create('test', '调度')['id']
        with self.client.session_transaction() as s:
            s['operator_id'] = sid

    def tearDown(self):
        _drain_async_jobs()
        _db.DB_PATH = self._orig
        for ext in ('', '-wal', '-shm'):
            p = self.tmp.name + ext
            if os.path.exists(p):
                try: os.unlink(p)
                except OSError: pass


class InboundGroupComputeTests(_TempDb):

    def test_group_has_eco_true_when_product_contains_环保(self):
        from models import InboundOrder, InboundRecord
        oid = InboundOrder.create('2026-08-13', '供应商A')
        InboundRecord.create(oid, '环保杂胶', '1m', '10', 'y', '')
        res = self.client.get('/inbound-records?start_date=2026-08-13&end_date=2026-08-13')
        html = res.get_data(as_text=True)
        self.assertIn('row-eco', html, '环保行应有 row-eco class')
        self.assertIn('eco-icon', html, '环保行应有 eco-icon span')

    def test_group_has_jia_mian_true_when_杂胶_加面(self):
        from models import InboundOrder, InboundRecord
        oid = InboundOrder.create('2026-08-13', '供应商B')
        InboundRecord.create(oid, '杂胶', '0.8黑中加面', '50', 'y', '')
        res = self.client.get('/inbound-records?start_date=2026-08-13&end_date=2026-08-13')
        html = res.get_data(as_text=True)
        self.assertIn('jia-mian-icon', html, '加面行应有 jia-mian-icon SVG')
        self.assertIn('eco-col', html, '表头应有 eco-col 列')

    def test_qty_invalid_marks_row_out_of_stock(self):
        from models import InboundOrder, InboundRecord
        oid = InboundOrder.create('2026-08-13', '供应商C')
        InboundRecord.create(oid, '品名', '规格', '', 'y', '')
        res = self.client.get('/inbound-records?start_date=2026-08-13&end_date=2026-08-13')
        html = res.get_data(as_text=True)
        self.assertIn('row-out-of-stock', html, '空数量行应有 row-out-of-stock class')
        self.assertIn('stock-icon', html, '空数量行应有 stock-icon ❌')


class InboundPatchImgColsTests(_TempDb):

    def test_patch_img_cols_persists(self):
        from models import InboundOrder
        oid = InboundOrder.create('2026-08-13', '供应商D')
        res = self.client.patch(
            f'/api/v1/inbound-orders/{oid}',
            json={'img_cols': 4}
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data.get('success'))
        self.assertEqual(data.get('img_cols'), 4)
        # 验证落库
        import sqlite3
        conn = sqlite3.connect(self.tmp.name)
        row = conn.execute('SELECT img_cols FROM inbound_orders WHERE id = ?', (oid,)).fetchone()
        conn.close()
        self.assertEqual(row[0], 4)

    def test_patch_img_cols_rejects_out_of_range(self):
        from models import InboundOrder
        oid = InboundOrder.create('2026-08-13', '供应商E')
        res = self.client.patch(
            f'/api/v1/inbound-orders/{oid}',
            json={'img_cols': 9}
        )
        self.assertEqual(res.status_code, 400)


class InboundSourceTagTests(_TempDb):

    def test_record_image_upload_accepts_source_tag(self):
        from models import InboundOrder, InboundRecord
        oid = InboundOrder.create('2026-08-13', '供应商F')
        rid = InboundRecord.create(oid, '品名', '规格', '10', 'y', '')
        from tests import fake_png_bytes
        data = {
            'image': (io.BytesIO(fake_png_bytes()), 'test.png'),
            'source_tag': '打板照',
        }
        res = self.client.post(
            f'/api/v1/inbound-orders/records/{rid}/images',
            data=data,
            content_type='multipart/form-data'
        )
        self.assertEqual(res.status_code, 201, res.get_data(as_text=True))
        rj = res.get_json()
        self.assertTrue(rj.get('success'))
        # 验证落库 source_tag
        import sqlite3
        conn = sqlite3.connect(self.tmp.name)
        row = conn.execute('SELECT source_tag FROM inbound_images WHERE record_pk = ?', (rid,)).fetchone()
        conn.close()
        self.assertEqual(row[0], '打板照')

    def test_record_image_upload_rejects_invalid_source_tag(self):
        from models import InboundOrder, InboundRecord
        oid = InboundOrder.create('2026-08-13', '供应商G')
        rid = InboundRecord.create(oid, '品名', '规格', '10', 'y', '')
        from tests import fake_png_bytes
        data = {
            'image': (io.BytesIO(fake_png_bytes()), 'test.png'),
            'source_tag': '非法值',
        }
        res = self.client.post(
            f'/api/v1/inbound-orders/records/{rid}/images',
            data=data,
            content_type='multipart/form-data'
        )
        self.assertEqual(res.status_code, 201)
        # 非法值被清空 → source_tag 应为 NULL
        import sqlite3
        conn = sqlite3.connect(self.tmp.name)
        row = conn.execute('SELECT source_tag FROM inbound_images WHERE record_pk = ?', (rid,)).fetchone()
        conn.close()
        self.assertIsNone(row[0])


if __name__ == '__main__':
    unittest.main()
