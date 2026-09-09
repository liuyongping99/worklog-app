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


class InboundTemplatePlacementAlignmentTests(_TempDb):
    """入库模板应与出货页面对齐:摆放图(点数)按钮 + 徽章 + 平铺区 + JS。

    后端(blueprints/inbound.py)11 个 placement 端点早已对齐出货/装柜(2026-08-26),
    但模板的「📷 点数」按钮、「✓ 点数」徽章、摆放图平铺区、placement_count.js 引入
    一直未补,导致入库页面行操作列无「点数」入口。本类测试防止再次漂移。
    """

    def _create_record_with_zhi_hint(self):
        """建一条有 unit_hint 含「支」的单子(满足按钮触发条件)。"""
        from models import InboundOrder, InboundRecord
        oid = InboundOrder.create('2026-09-05', '供应商K')
        InboundRecord.create(oid, '环保磅布三文治', '1.0黑双中性', '400', 'y', '8支')
        return self.client.get('/inbound-records?start_date=2026-09-05&end_date=2026-09-05').get_data(as_text=True)

    def test_placement_count_js_included(self):
        """模板应引入 static/js/placement_count.js,跟出货一致。"""
        tpl_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'templates', 'inbound-records.html'
        )
        with open(tpl_path, encoding='utf-8') as f:
            content = f.read()
        self.assertIn('placement_count.js', content,
            '模板应引入 static/js/placement_count.js')

    def test_placement_add_btn_rendered_for_zhi_row(self):
        """unit_hint 含「支」的明细行应渲染行级 placement-add-btn 「📷 点数」按钮
        (跟出货一致:data-record-id 在 button 上,onclick=openPlacementImageModal)。

        注意:_smart_add_modal.html 也有 .placement-add-btn(智能添加弹框按钮),
        所以测试必须按「行级按钮特征」精准匹配,不能只看 class 字符串。
        """
        html = self._create_record_with_zhi_hint()
        # 行级按钮的形态:button.placement-add-btn 带 data-record-id + onclick="openPlacementImageModal('<id>','<oid>')"
        import re
        row_btn = re.search(
            r'<button[^>]*class="[^"]*placement-add-btn[^"]*"[^>]*data-record-id="(\d+)"[^>]*data-order-id="(\d+)"[^>]*onclick="openPlacementImageModal\([^)]+\)"',
            html,
        )
        self.assertIsNotNone(row_btn,
            '明细行应有 placement-add-btn 行级按钮(带 data-record-id/onclick)')
        # 该按钮的文字应是「点数」,不是「智能添加」之类
        btn_text_match = re.search(
            r'<button[^>]*placement-add-btn[^>]*>([^<]+)</button>',
            html,
        )
        self.assertIsNotNone(btn_text_match)
        self.assertIn('点数', btn_text_match.group(1),
            f'行级按钮文字应为「点数」,实际 {btn_text_match.group(1)!r}')

    def test_placement_area_rendered(self):
        """每个订单表下方应有 placement-area 平铺区(对照出货 line 1025)。"""
        html = self._create_record_with_zhi_hint()
        self.assertIn('placement-area', html,
            '订单表下方应有 placement-area 平铺区')


if __name__ == '__main__':
    unittest.main()
