"""三个页面商品行明细区域对齐测试。

验证出货 / 装柜 / 入库三个页面的商品行明细区域:
1. 共享同一份 product_row_utils.js 工具 (findUnit/getPieceConversion/checkMismatch 等)
2. CSS 都包含 eco-col / jia-mian-icon / row-out-of-stock 等关键 class
3. 服务端计算的 qty_invalid / has_eco / has_jia_mian 字段都齐备
4. 行内编辑 / 删除 / 移动 / 备注汇总 都使用同一套 DOM 选择器
"""
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models._db as _db


SHARED_UTILS_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'static', 'js', 'product_row_utils.js'
)


class _TempDb(unittest.TestCase):
    """共享 in-memory DB, 三个页面都要插入测试数据"""

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


class SharedUtilsFileTests(unittest.TestCase):
    """共享工具文件存在性 + 关键 API 完整性"""

    def test_file_exists(self):
        self.assertTrue(os.path.exists(SHARED_UTILS_PATH),
            f'共享工具文件缺失: {SHARED_UTILS_PATH}')

    def test_exposes_required_apis(self):
        """ProductRowUtils 必须暴露所有出货页面依赖的 API"""
        with open(SHARED_UTILS_PATH, encoding='utf-8') as f:
            content = f.read()
        required_apis = [
            'findUnit',
            'findPieceConversion',
            'extractPiecesFromRemark',
            'getPieceHint',
            'calcPieceQuantity',
            'checkPieceMismatch',
            'getUnitHint',
            'checkMismatch',
            'mismatchIcon',
            'applyMismatchClass',
            'isInvalidQty',
            'invalidQtyIcon',
            'applyInvalidQtyClass',
            'autoFillQtyFromRemark',
            'refreshRemarkSummary',
            'beginInlineEdit',
            'collectInlineEdit',
            'applyEditedRow',
            'moveRecord',
            'bindMoveButtons',
            'deleteRecord',
            'bindEditDeleteButtons',
        ]
        for api in required_apis:
            self.assertIn(api, content, f'ProductRowUtils.{api} 未导出')

    def test_exposes_global_autoFillQtyFromRemark(self):
        """模板里 oninput="autoFillQtyFromRemark(this)" 直接调用, 必须在 window 上"""
        with open(SHARED_UTILS_PATH, encoding='utf-8') as f:
            content = f.read()
        self.assertIn('global.autoFillQtyFromRemark = autoFillQtyFromRemark',
            content, 'window.autoFillQtyFromRemark 未暴露, 模板 oninput 会失败')


class TemplateAlignmentTests(unittest.TestCase):
    """三个页面模板都引用共享工具并声明相同的关键 CSS class"""

    def _read_template(self, name):
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'templates', name
        )
        with open(path, encoding='utf-8') as f:
            return f.read(), path

    def _assert_uses_shared_utils(self, name, content):
        self.assertIn('product_row_utils.js', content,
            f'{name} 未引入共享工具 product_row_utils.js')

    def _assert_wrappers_exist(self, name, content):
        """每个页面应该把原函数改写为薄包装, 调用 ProductRowUtils"""
        wrappers = ['findPieceConversion', 'checkPieceMismatch']
        for w in wrappers:
            self.assertIn(f'ProductRowUtils.{w}', content,
                f'{name} 缺少对 ProductRowUtils.{w} 的包装')

    def _assert_refresh_summary_uses_shared(self, name, content):
        """备注汇总函数应委托给 ProductRowUtils.refreshRemarkSummary"""
        self.assertIn("ProductRowUtils.refreshRemarkSummary", content,
            f'{name} 未使用共享 refreshRemarkSummary')

    def test_shipping_uses_shared(self):
        content, _ = self._read_template('shipping-records.html')
        self._assert_uses_shared_utils('shipping-records.html', content)
        self._assert_wrappers_exist('shipping-records.html', content)
        self._assert_refresh_summary_uses_shared('shipping-records.html', content)
        # shipping 用 'row-after-table' 模式
        self.assertIn("'row-after-table'", content,
            'shipping refreshRemarkSummary 应使用 row-after-table 模式')

    def test_loading_uses_shared(self):
        content, _ = self._read_template('loading-orders.html')
        self._assert_uses_shared_utils('loading-orders.html', content)
        self._assert_wrappers_exist('loading-orders.html', content)
        self._assert_refresh_summary_uses_shared('loading-orders.html', content)
        # loading 用 'tbody-tr' 模式
        self.assertIn("'tbody-tr'", content,
            'loading refreshRemarkSummary 应使用 tbody-tr 模式')

    def test_inbound_uses_shared(self):
        content, _ = self._read_template('inbound-records.html')
        self._assert_uses_shared_utils('inbound-records.html', content)
        self._assert_wrappers_exist('inbound-records.html', content)
        self._assert_refresh_summary_uses_shared('inbound-records.html', content)
        self.assertIn("'tbody-tr'", content,
            'inbound refreshRemarkSummary 应使用 tbody-tr 模式')

    def test_all_three_have_eco_column(self):
        """三个页面的明细表都应有 eco-col 列 (重点列)"""
        for name in ('shipping-records.html', 'loading-orders.html', 'inbound-records.html'):
            content, _ = self._read_template(name)
            self.assertIn('eco-col', content,
                f'{name} 缺少 eco-col 列定义/类名')
            self.assertIn('eco-icon', content,
                f'{name} 缺少 eco-icon 类')

    def test_all_three_have_jia_mian_icon(self):
        for name in ('shipping-records.html', 'loading-orders.html', 'inbound-records.html'):
            content, _ = self._read_template(name)
            self.assertIn('jia-mian-icon', content,
                f'{name} 缺少 jia-mian-icon')

    def test_all_three_have_qty_invalid_handling(self):
        """三个页面都应展示 qty_invalid 时的 row-out-of-stock + stock-icon"""
        for name in ('shipping-records.html', 'loading-orders.html', 'inbound-records.html'):
            content, _ = self._read_template(name)
            self.assertIn('qty_invalid', content,
                f'{name} 未引用 qty_invalid 字段')
            self.assertIn('row-out-of-stock', content,
                f'{name} 缺少 row-out-of-stock 样式')
            self.assertIn('stock-icon', content,
                f'{name} 缺少 stock-icon 类')

    def test_all_three_have_summary_row(self):
        """三个页面都应有备注汇总行"""
        for name in ('shipping-records.html', 'loading-orders.html', 'inbound-records.html'):
            content, _ = self._read_template(name)
            self.assertIn('summary-row', content,
                f'{name} 缺少 summary-row 汇总行')

    def test_all_three_have_class_selectors(self):
        """三个页面都应使用统一的 class 选择器 (.qty-cell / .unit-cell / .remark-cell / .unit-hint-cell)"""
        required_classes = ['qty-cell', 'unit-cell', 'remark-cell', 'unit-hint-cell',
                            'product-name-cell', 'spec-cell']
        for name in ('shipping-records.html', 'loading-orders.html', 'inbound-records.html'):
            content, _ = self._read_template(name)
            for cls in required_classes:
                self.assertIn(cls, content,
                    f'{name} 缺少统一 class 选择器 .{cls}')


class LoadingBlueprintTests(_TempDb):
    """装柜后端补齐 qty_invalid / has_eco / has_jia_mian 字段"""

    def test_loading_qty_invalid_field(self):
        from models import LoadingOrder, LoadingOrderRecord
        oid = LoadingOrder.create('2026-08-29', '客户A')
        # 正常记录
        LoadingOrderRecord.create(oid, '正常商品', '1m', '5', 'y', '')
        # 数量为空
        LoadingOrderRecord.create(oid, '空数量商品', '1m', '', 'y', '')
        # 数量含字母 (非数字)
        LoadingOrderRecord.create(oid, '异常商品', '1m', 'abc', 'y', '')

        res = self.client.get('/loading-orders?start_date=2026-08-29&end_date=2026-08-29')
        html = res.get_data(as_text=True)
        # qty_invalid=True 的行应被标 row-out-of-stock
        self.assertIn('row-out-of-stock', html,
            '装柜页: 数量异常的记录应渲染为 row-out-of-stock')
        # 环保/加面字段应被计算
        self.assertIn('has_eco', html,
            '装柜页: 模板需消费 has_eco (用于 no-eco class 决定)')
        self.assertIn('has_jia_mian', html,
            '装柜页: 模板需消费 has_jia_mian')

    def test_loading_eco_and_jia_mian_render(self):
        from models import LoadingOrder, LoadingOrderRecord
        oid = LoadingOrder.create('2026-08-29', '客户B')
        LoadingOrderRecord.create(oid, '环保杂胶', '0.8米加面', '10', 'y', '')

        res = self.client.get('/loading-orders?start_date=2026-08-29&end_date=2026-08-29')
        html = res.get_data(as_text=True)
        self.assertIn('eco-icon', html, '环保商品应渲染 eco-icon')
        self.assertIn('jia-mian-icon', html, '加面商品应渲染 jia-mian-icon')


class ShippingTemplateHasSharedUtilsTests(unittest.TestCase):
    """出货模板的薄包装和共享工具的对接"""

    def test_shipping_wrappers_call_shared(self):
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'templates', 'shipping-records.html'
        )
        with open(path, encoding='utf-8') as f:
            content = f.read()
        # 关键包装存在
        self.assertIn('function findUnit(productName, spec)', content)
        self.assertIn('ProductRowUtils.findUnit(UNIT_LIST, productName, spec)', content)
        self.assertIn('function getUnitHint(productName, spec, quantity, unit, remark)', content)
        self.assertIn('ProductRowUtils.getUnitHint(UNIT_LIST, productName, spec, quantity, unit, remark)', content)
        self.assertIn("ProductRowUtils.refreshRemarkSummary(dateGroup, 'row-after-table')", content)


class InboundTemplateHasSharedUtilsTests(unittest.TestCase):
    """入库模板的薄包装和共享工具的对接"""

    def test_inbound_wrappers_call_shared(self):
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'templates', 'inbound-records.html'
        )
        with open(path, encoding='utf-8') as f:
            content = f.read()
        # findInboundUnit 包装无调用点已删除; 共享工具入口由 getInboundUnitHint/checkInboundMismatch 走
        self.assertIn("ProductRowUtils.refreshRemarkSummary(dateGroup, 'tbody-tr')", content)


class LoadingTemplateHasSharedUtilsTests(unittest.TestCase):
    """装柜模板的薄包装和共享工具的对接"""

    def test_loading_wrappers_call_shared(self):
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'templates', 'loading-orders.html'
        )
        with open(path, encoding='utf-8') as f:
            content = f.read()
        # findLoadingUnit 包装无调用点已删除; 共享工具入口由 getLoadingUnitHint/checkLoadingMismatch 走
        self.assertIn("ProductRowUtils.refreshRemarkSummary(dateGroup, 'tbody-tr')", content)
        # 装柜 buildLoadingRecordRow 应使用 ProductRowUtils.isInvalidQty
        self.assertIn('ProductRowUtils.isInvalidQty(rec.quantity)', content,
            'buildLoadingRecordRow 应使用 ProductRowUtils.isInvalidQty')


if __name__ == '__main__':
    unittest.main()