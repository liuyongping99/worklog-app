"""入库页 placement 图与主商品图分离测试(2026-09-15)。

背景:用户在 /inbound-records 点「点数」按钮添加 placement 图后,图同时出现在
主商品图区(.order-images-area)和 .placement-area,重复显示。
出货页 /shipping-records 已正确(后端过滤 source='placement'),入库页后端漏过滤。

修复:blueprints/inbound.py 在传给模板前从 group.images 排除 source='placement',
让 placement 图只走 .placement-area 平铺区,不再混到主商品图区。

防回归测试:
  1. 端到端:GET /inbound-records,parse HTML,.order-images-area 段内不应含 placement 图
  2. 端到端:.placement-area 段内应含 placement 图(数据源未丢)
  3. 普通图源 (upload/ai/copy_paper_label) 仍展示在主图区
  4. 对照:出货页 shipping.py 仍含 source='placement' 过滤(防 shipping 退化)
"""
import os
import re
import sqlite3
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app


class InboundPlacementImageSeparationTests(unittest.TestCase):
    """入库页 placement 与主图分离 — 4 项核心覆盖。"""

    @classmethod
    def setUpClass(cls):
        cls.app = create_app()
        cls.client = cls.app.test_client()
        # 测试用 session:直接设 operator_id,绕开登录页
        # 找一个 active staff id
        import sqlite3
        conn = sqlite3.connect('worklog.db')
        cur = conn.cursor()
        cur.execute('SELECT id FROM staff WHERE is_active=1 LIMIT 1')
        row = cur.fetchone()
        conn.close()
        if row:
            with cls.client.session_transaction() as sess:
                sess['operator_id'] = row[0]

    def _has_placement_order(self):
        conn = sqlite3.connect('worklog.db')
        cur = conn.cursor()
        cur.execute("""
            SELECT 1 FROM inbound_images WHERE source='placement' LIMIT 1
        """)
        row = cur.fetchone()
        conn.close()
        return row is not None

    def test_main_image_area_excludes_placement(self):
        """核心:模板主图区(.order-images-area)不应含 inb_placement_ 图。

        修前:模板 line 724 selectattr('ne', 'ai') 漏过滤,placement 图混到主图区
        修后:后端排除 placement,主图区无 inb_placement_ 路径
        """
        if not self._has_placement_order():
            self.skipTest('worklog.db 无 placement 图,跳过')

        resp = self.client.get('/inbound-records?focus_date=2026-09-15')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)

        # 截取 .order-images-area 段到 .placement-area 段之间的内容
        match = re.search(
            r'class="order-images-area[^"]*"[^>]*>(.*?)class="placement-area',
            html, re.DOTALL)
        self.assertIsNotNone(match, '模板结构可能改了,无法解析 .order-images-area 段')
        assert match is not None
        main_html = match.group(1)

        placement_in_main = main_html.count('inb_placement_')
        self.assertEqual(placement_in_main, 0,
            f'.order-images-area 段内不应含 inb_placement_ 图片(出现 {placement_in_main} 次)'
            f'\n\nmain_segment_start={main_html[:500]}')

    def test_placement_area_keeps_placement_images(self):
        """.placement-area 段仍正确显示 placement 图(数据未丢)。"""
        if not self._has_placement_order():
            self.skipTest('worklog.db 无 placement 图,跳过')

        resp = self.client.get('/inbound-records?focus_date=2026-09-15')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)

        # 遍历所有 .placement-area 段,只要任意一段含 inb_placement_ 图就算通过
        all_placement_segments = re.findall(
            r'class="placement-area[^"]*"[^>]*>(.*?)(?=<form|</body)',
            html, re.DOTALL)
        has_placement_img = any(
            'inb_placement_' in seg for seg in all_placement_segments)
        self.assertTrue(has_placement_img,
            f'所有 .placement-area 段内都没 inb_placement_ 图(共 {len(all_placement_segments)} 段),'
            f'placement 数据源可能丢了')

    def test_normal_image_sources_kept_in_main_area(self):
        """主图区仍展示普通图(upload/ai/copy_paper_label)。"""
        # 找今日有普通图的订单
        conn = sqlite3.connect('worklog.db')
        cur = conn.cursor()
        cur.execute("""
            SELECT 1 FROM inbound_images
            WHERE source IN ('upload', 'ai', 'copy_paper_label')
            AND date(order_pk, 'unixepoch') IS NULL  -- placeholder
            LIMIT 1
        """)
        # 改用更简单:只要今天有图就行
        cur.execute("""
            SELECT 1 FROM inbound_orders o
            JOIN inbound_images i ON i.order_pk = o.id
            WHERE i.source IN ('upload', 'ai', 'copy_paper_label')
            AND o.date = '2026-09-15' LIMIT 1
        """)
        row = cur.fetchone()
        conn.close()
        if not row:
            self.skipTest('今日 (2026-09-15) 无普通图入库订单,跳过')

        resp = self.client.get('/inbound-records?focus_date=2026-09-15')
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)

        match = re.search(
            r'class="order-images-area[^"]*"[^>]*>(.*?)class="placement-area',
            html, re.DOTALL)
        self.assertIsNotNone(match)
        assert match is not None
        main_html = match.group(1)

        # 主图区应至少含 1 张图(可能是 upload/ai/copy_paper_label)
        self.assertIn('class="img-item', main_html,
            '主图区仍应展示普通商品图')

    def test_shipping_baseline_unchanged(self):
        """对照:出货页后端仍过滤 source='placement',防 shipping 退化。"""
        with open('blueprints/shipping.py', 'r', encoding='utf-8') as f:
            content = f.read()
        # 出货页应在后端构造 non_ai 时排除 placement
        self.assertIn("'placement'", content,
            '出货页后端应过滤 source=placement(防止 shipping 退化)')
        self.assertIn("'ai'", content,
            '出货页后端应过滤 source=ai')


if __name__ == '__main__':
    unittest.main()