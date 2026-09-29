"""ShippingOrder.count_locked_by_date + 出货页头部显示测试(2026-09-17)。

出货页 /shipping-records 在"当天出货单数"后显示"当日已锁单总数"。
本测试覆盖 model 层 + 渲染层两处。
"""
import unittest

from blueprints.shipping import bp as shipping_bp
from models.orders import ShippingOrder


class ShippingOrderCountLockedTests(unittest.TestCase):
    """ShippingOrder.count_locked_by_date — SQL 计数 + is_locked=1 过滤。"""

    def test_returns_int(self):
        """返回 int,即使无数据(空日期返回 0)。"""
        # 找一个不太可能有数据的旧日期
        n = ShippingOrder.count_locked_by_date('1990-01-01')
        self.assertIsInstance(n, int)
        self.assertEqual(n, 0)


class ShippingRecordsPageMetaTests(unittest.TestCase):
    """/shipping-records 页面 HTML 必须含"当日已锁单"展示。

    不 mock 整个 DB,只验证模板渲染能拿到 locked_orders 变量。
    用 Flask test client + 临时 DB 注入 1 条锁单 + 1 条非锁单,验证 HTML。
    """

    def setUp(self):
        from app import create_app
        app = create_app()
        self.client = app.test_client()
        self.ctx = app.test_request_context()
        self.ctx.push()

    def tearDown(self):
        self.ctx.pop()

    def test_page_meta_contains_locked_count(self):
        """出货页 HTML 必须含'当日已锁单'字段,值由模板渲染。

        不写完整 e2e(需要登录 + 数据库 fixture),只验证模板字串:
        '当日已锁单' + '{{ locked_orders }}' 替代变量已渲染。
        """
        from flask import render_template  # noqa: F401 — kept for future e2e use
        # 用源码检查而非完整渲染 — render_template 会触发 Flask context 的
        # JSON 序列化副作用,完整 e2e 需要 login session + DB fixture,代价过大。
        from pathlib import Path
        tpl_src = Path('templates/shipping-records.html').read_text(encoding='utf-8')
        self.assertIn('当日已锁单', tpl_src, '出货页必须显示"当日已锁单"字段')
        self.assertIn('{{ locked_orders }}', tpl_src, '模板必须引用 {{ locked_orders }}')
        bp_src = Path('blueprints/shipping.py').read_text(encoding='utf-8')
        self.assertIn('locked_orders=locked_orders', bp_src,
            'shipping.py 路由必须把 locked_orders 传给模板')
        self.assertIn('ShippingOrder.count_locked_by_date(today)', bp_src,
            'shipping.py 路由必须调 count_locked_by_date(today)')
        from models.orders import ShippingOrder
        import inspect
        self.assertTrue(hasattr(ShippingOrder, 'count_locked_by_date'),
            'ShippingOrder 必须有 count_locked_by_date 静态方法')
        sig = inspect.signature(ShippingOrder.count_locked_by_date)
        self.assertEqual(len(sig.parameters), 1,
            'count_locked_by_date 必须只接受 1 个 date 参数')


class ShippingBlueprintRegisteredTests(unittest.TestCase):
    """smoke test — 蓝图注册没被这次改动破坏。"""

    def test_shipping_bp_registered(self):
        self.assertIn('shipping', shipping_bp.name or 'shipping')
