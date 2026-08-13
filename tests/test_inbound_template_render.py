"""入库模板 — 像素级对齐出货的视觉/类断言。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models._db as _db


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
        _db.DB_PATH = self._orig
        for ext in ('', '-wal', '-shm'):
            p = self.tmp.name + ext
            if os.path.exists(p):
                try: os.unlink(p)
                except OSError: pass


class InboundTemplateCssTests(_TempDb):

    def test_css_classes_present(self):
        """CSS 必须包含 6 个新类(与出货完全相同)。"""
        tpl_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'templates', 'inbound-records.html'
        )
        with open(tpl_path, encoding='utf-8') as f:
            content = f.read()
        for cls in [
            '.jia-mian-icon',
            '.eco-icon',
            '.eco-col',
            '.no-eco .eco-col',
            'tr.row-eco',
            'tr.row-out-of-stock',
            '.qty-badge',
            '.record-image-btn.has-image',
        ]:
            self.assertIn(cls, content,
                f'CSS 缺少 {cls}')

    def test_render_eco_row_has_classes_and_icon(self):
        from models import InboundOrder, InboundRecord
        oid = InboundOrder.create('2026-08-13', '供应商H')
        InboundRecord.create(oid, '环保杂胶', '1m', '10', 'y', '')
        res = self.client.get('/inbound-records?start_date=2026-08-13&end_date=2026-08-13')
        html = res.get_data(as_text=True)
        self.assertIn('class="eco-col"', html, '应有 eco-col 表头')
        self.assertIn('row-eco', html, '环保行应有 row-eco class')
        self.assertIn('eco-icon', html, '环保行应有 eco-icon')

    def test_render_jia_mian_row_has_svg(self):
        from models import InboundOrder, InboundRecord
        oid = InboundOrder.create('2026-08-13', '供应商I')
        InboundRecord.create(oid, '杂胶', '0.8黑中加面', '50', 'y', '')
        res = self.client.get('/inbound-records?start_date=2026-08-13&end_date=2026-08-13')
        html = res.get_data(as_text=True)
        self.assertIn('jia-mian-icon', html, '加面行应有 jia-mian-icon SVG')

    def test_render_empty_qty_marks_out_of_stock(self):
        from models import InboundOrder, InboundRecord
        oid = InboundOrder.create('2026-08-13', '供应商J')
        InboundRecord.create(oid, '品名', '规格', '', 'y', '')
        res = self.client.get('/inbound-records?start_date=2026-08-13&end_date=2026-08-13')
        html = res.get_data(as_text=True)
        self.assertIn('row-out-of-stock', html)
        self.assertIn('stock-icon', html)


if __name__ == '__main__':
    unittest.main()
