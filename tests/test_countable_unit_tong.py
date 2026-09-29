"""桶装商品点数功能 — compute_placement_expected_zhi 扩展到 unit='桶' (2026-09-18)

确认:
- unit='桶' + quantity 整数 → 走 quantity 兜底 (expected=quantity, has_zhi=True)
- unit='桶' + quantity 浮点 → 走 quantity 兜底 (expected=浮点, has_zhi=True)
- unit='桶' + quantity=0 / 空 / 解析失败 → 兜底失效 (has_zhi=False)
- unit='桶' + 备注含「X支」 → 走 remark 路径(优先于 quantity 兜底)
- unit='桶' + 备注含「Ny」散码 → has_san=True 由 JS/placement_match 另算
- is_countable_unit(unit) 白名单正确
- 现有支/令/张/y/空 unit 行为不变(回归测试)
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'blueprints'))

from blueprints._helpers import compute_placement_expected_zhi, is_countable_unit, _COUNTABLE_UNITS


class CountableUnitTongTests(unittest.TestCase):
    """unit='桶' (桶装胶水,白乳胶/喷胶/万能胶 等) 的 placement 期望值计算"""

    def test_unit_tong_with_int_quantity_fallback(self):
        # unit='桶', quantity=15, remark='' → 走 quantity 兜底,返回 (15.0, True)
        self.assertEqual(compute_placement_expected_zhi('', '15', '桶'), (15.0, True))

    def test_unit_tong_with_int_quantity_match(self):
        # 整数 total=15 == expected=15.0 → abs diff = 0 ≤ 0.01,匹配
        exp, has_zhi = compute_placement_expected_zhi('', '15', '桶')
        total = 15
        self.assertTrue(has_zhi)
        self.assertLessEqual(abs(total - exp), 0.01)

    def test_unit_tong_with_int_quantity_mismatch(self):
        # total=12 vs expected=15.0 → diff=3 > 0.01,不匹配
        exp, _ = compute_placement_expected_zhi('', '15', '桶')
        total = 12
        self.assertGreater(abs(total - exp), 0.01)

    def test_unit_tong_with_float_quantity_tolerance(self):
        # quantity='15.5', total=15.5 → abs diff = 0 ≤ 0.01,匹配
        exp, has_zhi = compute_placement_expected_zhi('', '15.5', '桶')
        self.assertEqual(exp, 15.5)
        self.assertTrue(has_zhi)
        total = 15.5
        self.assertLessEqual(abs(total - exp), 0.01)

    def test_unit_tong_quantity_zero_no_fallback(self):
        # quantity='0' → 兜底无效,has_zhi=False
        self.assertEqual(compute_placement_expected_zhi('', '0', '桶'), (0.0, False))

    def test_unit_tong_quantity_invalid_no_fallback(self):
        # quantity='abc' 解析失败 → has_zhi=False
        self.assertEqual(compute_placement_expected_zhi('', 'abc', '桶'), (0.0, False))

    def test_unit_tong_quantity_empty_no_fallback(self):
        # quantity='' → 兜底失效,has_zhi=False
        self.assertEqual(compute_placement_expected_zhi('', '', '桶'), (0.0, False))

    def test_unit_tong_remark_with_zhi_takes_precedence(self):
        # unit='桶', remark='10支', quantity=15 → 走 remark 路径 (10.0, True)
        self.assertEqual(compute_placement_expected_zhi('10支', '15', '桶'), (10.0, True))

    def test_unit_tong_remark_multiple_zhi_sum(self):
        # unit='桶', remark='3支+5支' → sum=8 走 remark 路径 (8.0, True)
        self.assertEqual(compute_placement_expected_zhi('3支+5支', '', '桶'), (8.0, True))

    def test_unit_tong_remark_with_yang(self):
        # unit='桶', remark='5桶+2y' → 走 remark 路径 (无「X支」),has_remark_zhi=False
        # 因为是桶装,quantity 兜底仍有效 → (5.0, True)
        self.assertEqual(compute_placement_expected_zhi('5桶+2y', '5', '桶'), (5.0, True))

    def test_unit_tong_remark_only_zhi(self):
        # unit='桶', remark='20支', quantity=15 → 走 remark (20.0, True),quantity=15 忽略
        self.assertEqual(compute_placement_expected_zhi('20支', '15', '桶'), (20.0, True))

    def test_unit_tong_with_whitespace(self):
        # unit=' 桶  '(带空格) → 仍走兜底 (15.0, True)
        self.assertEqual(compute_placement_expected_zhi('', '15', '  桶  '), (15.0, True))


class IsCountableUnitTests(unittest.TestCase):
    """is_countable_unit() 白名单正确性"""

    def test_countable_units_set(self):
        # 白名单必须包含 支/桶/令/张
        self.assertIn('支', _COUNTABLE_UNITS)
        self.assertIn('桶', _COUNTABLE_UNITS)
        self.assertIn('令', _COUNTABLE_UNITS)
        self.assertIn('张', _COUNTABLE_UNITS)

    def test_countable_true_for_known_units(self):
        for u in ['支', '桶', '令', '张']:
            self.assertTrue(is_countable_unit(u), u + ' should be countable')

    def test_countable_false_for_continuous_units(self):
        # kg/y/码 不应走兜底
        for u in ['kg', 'y', '码', 'Y']:
            self.assertFalse(is_countable_unit(u), u + ' should NOT be countable')

    def test_countable_false_for_empty_or_none(self):
        self.assertFalse(is_countable_unit(''))
        self.assertFalse(is_countable_unit(None))
        self.assertFalse(is_countable_unit('   '))


class RegressionTests(unittest.TestCase):
    """回归:现有 unit=支/令/张/y/空 的行为不应被改动"""

    def test_unit_zhi_unchanged(self):
        # 支类:原有测试用例
        self.assertEqual(compute_placement_expected_zhi('', '33', '支'), (33.0, True))

    def test_unit_ling_unchanged(self):
        # 令(拷贝纸):原有
        self.assertEqual(compute_placement_expected_zhi('', '5', '令'), (5.0, True))

    def test_unit_zhang_unchanged(self):
        # 张(日本纸):原有
        self.assertEqual(compute_placement_expected_zhi('', '300', '张'), (300.0, True))

    def test_unit_y_still_no_fallback(self):
        # y(码数) 不应走兜底 —— 回归
        self.assertEqual(compute_placement_expected_zhi('', '500', 'y'), (0.0, False))

    def test_unit_kg_still_no_fallback(self):
        # kg 不应走兜底 —— 回归(新白名单未包含)
        self.assertEqual(compute_placement_expected_zhi('', '100', 'kg'), (0.0, False))

    def test_unit_empty_still_no_fallback(self):
        # 空 unit 不应走兜底 —— 回归
        self.assertEqual(compute_placement_expected_zhi('', '10', ''), (0.0, False))

    def test_remark_with_zhi_unit_y_kept(self):
        # unit='y', remark='20支' → 走 remark (20.0, True) —— 回归
        self.assertEqual(compute_placement_expected_zhi('20支', '500', 'y'), (20.0, True))


if __name__ == '__main__':
    unittest.main()