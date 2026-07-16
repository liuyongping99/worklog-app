"""核心业务逻辑补充单测

覆盖 4 块此前缺少测试的核心逻辑：
1. YPP 匹配逻辑 — _match_unit_in_cache() + get_ypp()
2. calc_hint() 的 unit='支' 和 remark 兜底分支
3. check_remark() 的乘法语义和混合语义
4. 模型层单位归一化（码 → y）+ UnifiedSearch 基本搜索

运行方法（在项目根目录）：
    python -m unittest tests.test_core_logic -v
"""
import os
import sys
import unittest
import sqlite3
import tempfile

# 让 python 找到项目根目录
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'blueprints'))

from blueprints._helpers import (
    _match_unit_in_cache, get_ypp, calc_hint, check_remark
)


# =====================================================================
#  1. YPP 匹配逻辑 — _match_unit_in_cache()
# =====================================================================

class MatchUnitInCacheTests(unittest.TestCase):
    """_match_unit_in_cache(product_name, spec, units_cache) → matched unit dict or None"""

    def _make_units(self):
        """构造测试用 units_cache（模拟 sqlite3.Row 的 dict 形式）"""
        return [
            # 珍珠棉 — 默认行（无 spec_keyword）
            {'product_name': '珍珠棉', 'spec_keyword': None, 'yards_per_piece': 200, 'is_usingyardforcounting': 1},
            # 珍珠棉 — 精确匹配 0.5T
            {'product_name': '珍珠棉', 'spec_keyword': '0.5T', 'yards_per_piece': 200, 'is_usingyardforcounting': 1},
            # 珍珠棉 — 精确匹配 1T
            {'product_name': '珍珠棉', 'spec_keyword': '1T', 'yards_per_piece': 100, 'is_usingyardforcounting': 1},
            # PE板 — 默认行
            {'product_name': 'PE板', 'spec_keyword': None, 'yards_per_piece': 50, 'is_usingyardforcounting': 1},
            # 喷胶 — 默认行，不启用码数计数
            {'product_name': '喷胶', 'spec_keyword': None, 'yards_per_piece': 0, 'is_usingyardforcounting': 0},
        ]

    def test_exact_keyword_match(self):
        """spec 包含 spec_keyword 时应匹配到精确行"""
        units = self._make_units()
        matched = _match_unit_in_cache('珍珠棉', '0.5T平纹', units)
        self.assertIsNotNone(matched)
        self.assertEqual(matched['spec_keyword'], '0.5T')
        self.assertEqual(matched['yards_per_piece'], 200)

    def test_different_keyword_match(self):
        """不同 spec_keyword 匹配到不同行"""
        units = self._make_units()
        matched = _match_unit_in_cache('珍珠棉', '1T平纹', units)
        self.assertEqual(matched['spec_keyword'], '1T')
        self.assertEqual(matched['yards_per_piece'], 100)

    def test_fallback_to_default(self):
        """spec 不包含任何 keyword 时，兜底取默认行"""
        units = self._make_units()
        matched = _match_unit_in_cache('珍珠棉', '2T平纹', units)
        self.assertIsNotNone(matched)
        self.assertIsNone(matched['spec_keyword'])
        self.assertEqual(matched['yards_per_piece'], 200)

    def test_default_does_not_override_exact(self):
        """关键：默认行不能抢在精确匹配之前返回（这是历史 bug 的根源）"""
        units = self._make_units()
        # 默认行在列表第一位，但 spec='0.5T平纹' 应匹配到第二行的精确行
        matched = _match_unit_in_cache('珍珠棉', '0.5T平纹', units)
        self.assertIsNotNone(matched['spec_keyword'], '应匹配到有 keyword 的行，而非默认行')

    def test_no_match_different_product(self):
        """商品名不匹配时返回 None"""
        units = self._make_units()
        matched = _match_unit_in_cache('不存在的商品', '0.5T', units)
        self.assertIsNone(matched)

    def test_empty_spec_falls_to_default(self):
        """spec 为空字符串时直接走兜底"""
        units = self._make_units()
        matched = _match_unit_in_cache('珍珠棉', '', units)
        self.assertIsNotNone(matched)
        self.assertIsNone(matched['spec_keyword'])

    def test_none_spec_falls_to_default(self):
        """spec 为 None 时不报错，走兜底"""
        units = self._make_units()
        matched = _match_unit_in_cache('珍珠棉', None, units)
        self.assertIsNotNone(matched)
        self.assertIsNone(matched['spec_keyword'])

    def test_keyword_case_insensitive(self):
        """spec_keyword 匹配应大小写不敏感"""
        units = [
            {'product_name': '珍珠棉', 'spec_keyword': '0.5t', 'yards_per_piece': 200, 'is_usingyardforcounting': 1},
            {'product_name': '珍珠棉', 'spec_keyword': None, 'yards_per_piece': 100, 'is_usingyardforcounting': 1},
        ]
        # spec 用大写 T，keyword 是小写 t
        matched = _match_unit_in_cache('珍珠棉', '0.5T平纹', units)
        self.assertIsNotNone(matched)
        self.assertEqual(matched['spec_keyword'], '0.5t')


# =====================================================================
#  2. get_ypp() — 从匹配结果提取 YPP 值
# =====================================================================

class GetYppTests(unittest.TestCase):
    """get_ypp(product_name, spec, units_cache) → float YPP"""

    def _make_units(self):
        return [
            {'product_name': '珍珠棉', 'spec_keyword': None, 'yards_per_piece': 200, 'is_usingyardforcounting': 1},
            {'product_name': '珍珠棉', 'spec_keyword': '1T', 'yards_per_piece': 100, 'is_usingyardforcounting': 1},
            {'product_name': '喷胶', 'spec_keyword': None, 'yards_per_piece': 750, 'is_usingyardforcounting': 0},
        ]

    def test_ypp_with_counting_enabled(self):
        """启用码数计数时返回 yards_per_piece / 100"""
        ypp = get_ypp('珍珠棉', '1T平纹', self._make_units())
        self.assertEqual(ypp, 1.0)  # 100 / 100

    def test_ypp_with_counting_disabled(self):
        """不启用码数计数时返回 0"""
        ypp = get_ypp('喷胶', '', self._make_units())
        self.assertEqual(ypp, 0)

    def test_ypp_no_match(self):
        """商品不在 units_cache 中时返回 0"""
        ypp = get_ypp('不存在的商品', '0.5T', self._make_units())
        self.assertEqual(ypp, 0)

    def test_ypp_default_row(self):
        """兜底默认行的 YPP"""
        ypp = get_ypp('珍珠棉', '未知规格', self._make_units())
        self.assertEqual(ypp, 2.0)  # 200 / 100


# =====================================================================
#  3. calc_hint() — unit='支' 和 remark 兜底分支
# =====================================================================

class CalcHintBranchTests(unittest.TestCase):
    """calc_hint() 之前未覆盖的分支：unit='支' 直接返回 + remark 兜底"""

    # --- unit='支' 分支 ---

    def test_unit_zhi_integer(self):
        """unit='支' 时直接返回数量作为支数（整数）"""
        result = calc_hint('50', 0, unit='支')
        self.assertEqual(result, '50支')

    def test_unit_zhi_decimal(self):
        """unit='支' 时小数也保留"""
        result = calc_hint('50.5', 0, unit='支')
        self.assertEqual(result, '50.5支')

    def test_unit_zhi_ignores_ypp(self):
        """unit='支' 时忽略 YPP，直接用数量"""
        result = calc_hint('50', 2.0, unit='支')
        self.assertEqual(result, '50支')

    def test_unit_zhi_zero(self):
        """unit='支' 数量为 0"""
        result = calc_hint('0', 0, unit='支')
        self.assertEqual(result, '0支')

    def test_unit_zhi_invalid_qty(self):
        """unit='支' 但数量无效时返回空串"""
        result = calc_hint('abc', 0, unit='支')
        self.assertEqual(result, '')

    # --- remark 兜底分支（无 YPP，从备注提取） ---

    def test_remark_fallback_basic(self):
        """无 YPP + 有备注 '3支' → 从备注提取"""
        result = calc_hint('100', 0, unit='kg', remark='3支')
        self.assertEqual(result, '3支')

    def test_remark_fallback_with_yards(self):
        """无 YPP + 备注 '3支+5码' → 只提取支数"""
        result = calc_hint('100', 0, unit='kg', remark='3支+5码')
        self.assertEqual(result, '3支')

    def test_remark_fallback_no_pieces(self):
        """无 YPP + 备注无 'X支' → 返回空串"""
        result = calc_hint('100', 0, unit='kg', remark='50码')
        self.assertEqual(result, '')

    def test_remark_fallback_empty(self):
        """无 YPP + 空备注 → 返回空串"""
        result = calc_hint('100', 0, unit='kg', remark='')
        self.assertEqual(result, '')

    def test_remark_fallback_none(self):
        """无 YPP + None 备注 → 返回空串"""
        result = calc_hint('100', 0, unit='kg', remark=None)
        self.assertEqual(result, '')

    def test_remark_not_used_when_ypp_present(self):
        """有 YPP 时不走 remark 兜底"""
        result = calc_hint('50', 2.0, unit='y', remark='3支')
        self.assertEqual(result, '25支')  # 50 / 2.0 = 25


# =====================================================================
#  4. check_remark() — 乘法语义和混合语义
# =====================================================================

class CheckRemarkMulTests(unittest.TestCase):
    """check_remark() 乘法语义：X支*Yy 和 Yy*X支"""

    def test_mul_consistent(self):
        """33支 * 1.5y = 49.5，实际 49.5 → 一致"""
        result = check_remark('33支*1.5y', '49.5', 1.5)
        self.assertEqual(result, '')

    def test_mul_inconsistent_warn(self):
        """33支 * 1.5y = 49.5，实际 100 → warn"""
        result = check_remark('33支*1.5y', '100', 1.5)
        self.assertEqual(result, 'warn')

    def test_mul_reversed_order(self):
        """1.5y*33支 = 49.5，实际 49.5 → 一致（Yy 在前）"""
        result = check_remark('1.5y*33支', '49.5', 1.5)
        self.assertEqual(result, '')

    def test_mul_reversed_inconsistent(self):
        """1.5y*33支 = 49.5，实际 60 → warn"""
        result = check_remark('1.5y*33支', '60', 1.5)
        self.assertEqual(result, 'warn')

    def test_mul_with_chinese_yard(self):
        """33支*1.5码 = 49.5，实际 49.5 → 一致（用'码'不用'y'）"""
        result = check_remark('33支*1.5码', '49.5', 1.5)
        self.assertEqual(result, '')

    def test_mul_single_piece_info(self):
        """1支*1.5y = 1.5，实际 5 → info（单支轻微不一致）"""
        result = check_remark('1支*1.5y', '5', 1.5)
        self.assertEqual(result, 'info')

    # --- 混合语义：X支*Yy + 散码 ---

    def test_mixed_mul_plus_loose(self):
        """3支*48.5y + 2y = 3×48.5+2 = 147.5，实际 147.5 → 一致"""
        result = check_remark('3支*48.5y+2y', '147.5', 1.5)
        self.assertEqual(result, '')

    def test_mixed_mul_plus_loose_inconsistent(self):
        """3支*48.5y + 2y = 147.5，实际 200 → warn"""
        result = check_remark('3支*48.5y+2y', '200', 1.5)
        self.assertEqual(result, 'warn')

    def test_mixed_mul_uses_per_piece_not_ypp(self):
        """乘法语义下用 per_piece 而非 ypp：
        3支*48.5y + 2y = 147.5，ypp=2.0 但不影响计算"""
        result = check_remark('3支*48.5y+2y', '147.5', 2.0)
        self.assertEqual(result, '')

    def test_mul_does_not_count_as_loose(self):
        """乘法段被抠掉后，其中的码数不算散码：
        33支*1.5y = 49.5，实际 49.5 → 一致（1.5 不算散码）"""
        result = check_remark('33支*1.5y', '49.5', 1.5)
        self.assertEqual(result, '')

    def test_add_semantics_still_works(self):
        """回归：加法语义 33支+0.5y = 33×1.5+0.5 = 50，实际 50 → 一致"""
        result = check_remark('33支+0.5y', '50', 1.5)
        self.assertEqual(result, '')

    def test_mul_with_spaces(self):
        """带空格的乘法：33支 * 1.5y = 49.5"""
        result = check_remark('33支 * 1.5y', '49.5', 1.5)
        self.assertEqual(result, '')


# =====================================================================
#  5. 模型层单位归一化（码 → y）
# =====================================================================

class UnitNormalizationTests(unittest.TestCase):
    """测试 ShippingRecord.create / InboundRecord.create / LoadingOrderRecord.create
    中的 unit='码' → 'y' 归一化逻辑。

    使用临时数据库，不污染 worklog.db。
    """

    @classmethod
    def setUpClass(cls):
        """创建临时数据库并初始化表结构"""
        cls.tmpdir = tempfile.mkdtemp(prefix='worklog_test_')
        cls.db_path = os.path.join(cls.tmpdir, 'test.db')

        # 保存原始 DB_PATH 并替换
        from models import _db
        cls._orig_db_path = _db.DB_PATH
        _db.DB_PATH = cls.db_path

        # 初始化数据库
        from models._init import init_db
        init_db()

    @classmethod
    def tearDownClass(cls):
        """恢复原始 DB_PATH 并清理临时数据库"""
        from models import _db
        _db.DB_PATH = cls._orig_db_path
        import shutil
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_shipping_normalizes_ma_to_y(self):
        """ShippingRecord.create 将 unit='码' 归一化为 'y'"""
        from models.orders import ShippingRecord, ShippingOrder
        # 先创建订单
        order_pk = ShippingOrder.create('2026-07-06', '测试客户')
        # 创建明细，unit 传 '码'
        record_id = ShippingRecord.create(
            date='2026-07-06', customer='测试客户',
            product_name='珍珠棉', specification='1T',
            quantity='100', unit='码', remark='测试',
            order_pk=order_pk
        )
        # 查回来验证
        record = ShippingRecord.get_by_id(record_id)
        self.assertEqual(record['unit'], 'y')

    def test_inbound_normalizes_ma_to_y(self):
        """InboundRecord.create 将 unit='码' 归一化为 'y'"""
        from models.orders import InboundRecord, InboundOrder
        order_pk = InboundOrder.create('2026-07-06', '测试供应商')
        record_id = InboundRecord.create(
            order_pk=order_pk,
            product_name='珍珠棉', specification='1T',
            quantity='100', unit='码', remark='测试'
        )
        record = InboundRecord.get_by_id(record_id)
        self.assertEqual(record['unit'], 'y')

    def test_loading_normalizes_ma_to_y(self):
        """LoadingOrderRecord.create 将 unit='码' 归一化为 'y'"""
        from models.orders import LoadingOrderRecord, LoadingOrder
        order_pk = LoadingOrder.create('2026-07-06', '测试客户')
        record_id = LoadingOrderRecord.create(
            date='2026-07-06', customer='测试客户',
            product_name='珍珠棉', specification='1T',
            quantity='100', unit='码', remark='测试',
            order_pk=order_pk
        )
        record = LoadingOrderRecord.get_by_id(record_id)
        self.assertEqual(record['unit'], 'y')

    def test_shipping_keeps_y_as_is(self):
        """unit='y' 保持不变"""
        from models.orders import ShippingRecord, ShippingOrder
        order_pk = ShippingOrder.create('2026-07-06', '测试客户')
        record_id = ShippingRecord.create(
            date='2026-07-06', customer='测试客户',
            product_name='珍珠棉', specification='1T',
            quantity='100', unit='y', remark='',
            order_pk=order_pk
        )
        record = ShippingRecord.get_by_id(record_id)
        self.assertEqual(record['unit'], 'y')

    def test_shipping_keeps_zhi_as_is(self):
        """unit='支' 保持不变"""
        from models.orders import ShippingRecord, ShippingOrder
        order_pk = ShippingOrder.create('2026-07-06', '测试客户')
        record_id = ShippingRecord.create(
            date='2026-07-06', customer='测试客户',
            product_name='PE板', specification='0.6',
            quantity='50', unit='支', remark='',
            order_pk=order_pk
        )
        record = ShippingRecord.get_by_id(record_id)
        self.assertEqual(record['unit'], '支')


# =====================================================================
#  6. UnifiedSearch 基本搜索
# =====================================================================

class UnifiedSearchTests(unittest.TestCase):
    """UnifiedSearch.search() 跨表搜索基本功能测试。

    使用与上面相同的临时数据库。
    """

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.mkdtemp(prefix='worklog_search_test_')
        cls.db_path = os.path.join(cls.tmpdir, 'test.db')

        from models import _db
        cls._orig_db_path = _db.DB_PATH
        _db.DB_PATH = cls.db_path

        from models._init import init_db
        init_db()

        # 插入测试数据
        from models.orders import (
            ShippingRecord, ShippingOrder,
            InboundRecord, InboundOrder,
            LoadingOrderRecord, LoadingOrder,
        )

        # 出货：2 单 3 条明细
        so1 = ShippingOrder.create('2026-07-01', '客户A')
        ShippingRecord.create('2026-07-01', '客户A', '珍珠棉', '1T', '100', 'y', '5支', order_pk=so1)
        ShippingRecord.create('2026-07-01', '客户A', 'PE板', '0.6', '50', '支', '', order_pk=so1)
        so2 = ShippingOrder.create('2026-07-03', '客户B')
        ShippingRecord.create('2026-07-03', '客户B', '珍珠棉', '2T', '200', 'y', '10支', order_pk=so2)

        # 入库：1 单 1 条明细
        io1 = InboundOrder.create('2026-07-02', '供应商D')
        InboundRecord.create(io1, '珍珠棉', '1T', '100', 'y', '5支')

        # 装柜：1 单 1 条明细
        lo1 = LoadingOrder.create('2026-07-04', '客户C')
        LoadingOrderRecord.create('2026-07-04', '客户C', 'PE板', '0.8', '30', 'kg', '', order_pk=lo1)

    @classmethod
    def tearDownClass(cls):
        from models import _db
        _db.DB_PATH = cls._orig_db_path
        import shutil
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_search_all_scopes(self):
        """搜索全部三个库，应返回 5 条"""
        from models.orders import UnifiedSearch
        results = UnifiedSearch.search(['shipping', 'inbound', 'loading'])
        self.assertEqual(len(results), 5)

    def test_search_shipping_only(self):
        """只搜出货，应返回 3 条"""
        from models.orders import UnifiedSearch
        results = UnifiedSearch.search(['shipping'])
        self.assertEqual(len(results), 3)
        for r in results:
            self.assertEqual(r['type'], 'shipping')

    def test_search_by_product_name(self):
        """按商品名模糊搜索"""
        from models.orders import UnifiedSearch
        results = UnifiedSearch.search(['shipping', 'inbound', 'loading'], product_name='珍珠棉')
        self.assertEqual(len(results), 3)  # 出货 2 + 入库 1
        for r in results:
            self.assertIn('珍珠棉', r['product_name'])

    def test_search_by_customer(self):
        """按客户/供应商模糊搜索"""
        from models.orders import UnifiedSearch
        results = UnifiedSearch.search(['shipping', 'inbound', 'loading'], customer='客户A')
        self.assertEqual(len(results), 2)  # 客户A 的 2 条出货明细

    def test_search_by_supplier(self):
        """按供应商模糊搜索入库记录"""
        from models.orders import UnifiedSearch
        results = UnifiedSearch.search(['inbound'], customer='供应商D')
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]['customer'], '供应商D')

    def test_search_by_date_range(self):
        """按日期范围搜索"""
        from models.orders import UnifiedSearch
        results = UnifiedSearch.search(['shipping', 'inbound', 'loading'],
                                       start_date='2026-07-02', end_date='2026-07-03')
        # 07-02 入库 1 条 + 07-03 出货 1 条 = 2 条
        self.assertEqual(len(results), 2)

    def test_search_empty_scope(self):
        """空 scope 返回空列表"""
        from models.orders import UnifiedSearch
        results = UnifiedSearch.search([])
        self.assertEqual(len(results), 0)

    def test_search_results_sorted_by_date_desc(self):
        """结果按日期倒序排列"""
        from models.orders import UnifiedSearch
        results = UnifiedSearch.search(['shipping', 'inbound', 'loading'])
        dates = [r['date'] for r in results]
        self.assertEqual(dates, sorted(dates, reverse=True))


if __name__ == '__main__':
    unittest.main(verbosity=2)
