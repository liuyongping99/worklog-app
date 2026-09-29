"""YPP 规则冲突扫描 helper 的回归测试。

`find_ypp_mismatches` 是 _helpers.py 的纯函数:
  输入:records 列表 + 可选 units_cache
  输出:每个 record 的 YPP 校验结果(包含 mismatch 明细)

行为契约:
  - 没有 YPP 配置的产品 → 跳过(返回中不含)
  - 备注里没有 "X支" → 跳过(无 YPP 校验对象)
  - 数量与备注一致 → 不出现在结果中
  - 多支不一致 → severity='warn'
  - 单支不一致 → severity='info'
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from blueprints._helpers import find_ypp_mismatches  # noqa: E402


# YPP=48.5 的产品(数据库实际值 4850/100)
UNITS_CACHE = [
    {'product_name': '环保杂胶', 'spec_keyword': None, 'yards_per_piece': 4850, 'is_usingyardforcounting': 1},
    {'product_name': '7P环保杂胶', 'spec_keyword': None, 'yards_per_piece': 4850, 'is_usingyardforcounting': 1},
    {'product_name': '白磅布三文治', 'spec_keyword': None, 'yards_per_piece': 3650, 'is_usingyardforcounting': 1},
]


class TestFindYppMismatches:
    """YPP 规则冲突扫描 helper 行为契约测试。"""

    def test_no_ypp_config_skipped(self):
        """没 YPP 配置的产品不应出现在结果里。"""
        records = [
            {'id': 1, 'product_name': 'PVC桌布', 'specification': '', 'quantity': '50',
             'unit': 'y', 'remark': '3支*20y'},
        ]
        result = find_ypp_mismatches(records, UNITS_CACHE)
        assert result == [], f'PVC桌布无 YPP 配置,应跳过,实际返回 {result}'

    def test_no_branch_in_remark_skipped(self):
        """备注里没有 "X支" → 无 YPP 校验对象,跳过。"""
        records = [
            {'id': 1, 'product_name': '环保杂胶', 'specification': '', 'quantity': '100',
             'unit': 'y', 'remark': '急单'},  # 无"支"
        ]
        result = find_ypp_mismatches(records, UNITS_CACHE)
        assert result == [], f'无"X支"备注应跳过,实际 {result}'

    def test_consistent_skipped(self):
        """数量与备注一致 → 不出现在结果里。"""
        records = [
            {'id': 1, 'product_name': '环保杂胶', 'specification': '', 'quantity': '48.5',
             'unit': 'y', 'remark': '1支'},  # 1×48.5=48.5 ✓
            {'id': 2, 'product_name': '7P环保杂胶', 'specification': '', 'quantity': '97',
             'unit': 'y', 'remark': '2支'},  # 2×48.5=97 ✓
        ]
        result = find_ypp_mismatches(records, UNITS_CACHE)
        assert result == [], f'一致的不应出现,实际 {result}'

    def test_multi_branch_mismatch_is_warn(self):
        """多支不一致 → severity='warn'。"""
        records = [
            {'id': 1, 'product_name': '环保杂胶', 'specification': '', 'quantity': '242.5',
             'unit': 'y', 'remark': '10支'},  # 10×48.5=485 vs 实际 242.5
        ]
        result = find_ypp_mismatches(records, UNITS_CACHE)
        assert len(result) == 1
        assert result[0]['record_id'] == 1
        assert result[0]['severity'] == 'warn'
        assert result[0]['pieces'] == 10
        assert result[0]['expected'] == 485.0
        assert result[0]['actual'] == 242.5
        assert abs(result[0]['diff'] - (-242.5)) < 0.01

    def test_single_branch_mismatch_is_info(self):
        """单支不一致 → severity='info'。"""
        records = [
            {'id': 1, 'product_name': '环保杂胶', 'specification': '', 'quantity': '50',
             'unit': 'y', 'remark': '1支'},  # 1×48.5=48.5 vs 实际 50
        ]
        result = find_ypp_mismatches(records, UNITS_CACHE)
        assert len(result) == 1
        assert result[0]['severity'] == 'info'
        assert result[0]['pieces'] == 1
        assert result[0]['expected'] == 48.5
        assert result[0]['actual'] == 50.0

    def test_returns_required_fields(self):
        """返回结果应包含前端展示需要的字段。"""
        records = [
            {'id': 42, 'product_name': '环保杂胶', 'specification': '1.0黑中加面',
             'quantity': '242.5', 'unit': 'y', 'remark': '10支',
             'order_pk': 7, 'date': '2026-08-15', 'customer': '阿桂'},
        ]
        result = find_ypp_mismatches(records, UNITS_CACHE)
        assert len(result) == 1
        m = result[0]
        # 必填字段
        for field in ('record_id', 'order_id', 'date', 'customer', 'product_name',
                      'specification', 'quantity', 'unit', 'remark',
                      'pieces', 'ypp', 'expected', 'actual', 'diff', 'severity'):
            assert field in m, f'缺少字段 {field}'
        assert m['record_id'] == 42
        assert m['order_id'] == 7
        assert m['date'] == '2026-08-15'
        assert m['customer'] == '阿桂'
        assert m['ypp'] == 48.5

    def test_mul_form_per_piece_override(self):
        """X支*Yy 形式时 Y 应优先于 YPP。"""
        records = [
            {'id': 1, 'product_name': '环保杂胶', 'specification': '', 'quantity': '145.5',
             'unit': 'y', 'remark': '3支*48.5y'},  # 3×48.5=145.5 ✓
            {'id': 2, 'product_name': '环保杂胶', 'specification': '', 'quantity': '150',
             'unit': 'y', 'remark': '3支*48.5y'},  # 期望 145.5,实际 150,warn
        ]
        result = find_ypp_mismatches(records, UNITS_CACHE)
        # 只有第 2 条不一致
        assert len(result) == 1
        assert result[0]['record_id'] == 2
        assert result[0]['severity'] == 'warn'
        assert result[0]['per_piece_yards'] == 48.5

    def test_loose_yards_in_remark(self):
        """备注「3支+2y」→ 期望 = 3×ypp + 2(yyp=48.5 → 147.5)。"""
        records = [
            {'id': 1, 'product_name': '环保杂胶', 'specification': '', 'quantity': '150',
             'unit': 'y', 'remark': '3支+2y'},  # 期望 147.5 vs 实际 150
        ]
        result = find_ypp_mismatches(records, UNITS_CACHE)
        assert len(result) == 1
        assert result[0]['loose_yards'] == 2.0
        assert result[0]['expected'] == 147.5
        assert result[0]['severity'] == 'warn'

    def test_mixed_form(self):
        """「3支*48.5y+2y」→ 期望 = 3×48.5 + 2 = 147.5。"""
        records = [
            {'id': 1, 'product_name': '环保杂胶', 'specification': '', 'quantity': '147.5',
             'unit': 'y', 'remark': '3支*48.5y+2y'},  # ✓ 一致
            {'id': 2, 'product_name': '环保杂胶', 'specification': '', 'quantity': '200',
             'unit': 'y', 'remark': '3支*48.5y+2y'},  # 期望 147.5 vs 200
        ]
        result = find_ypp_mismatches(records, UNITS_CACHE)
        assert len(result) == 1
        assert result[0]['record_id'] == 2