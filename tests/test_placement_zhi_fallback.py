"""compute_placement_expected_zhi(remark, quantity_str, unit) → (expected_zhi, has_zhi)

placement 期望值兜底逻辑 — unit='支' + 备注无「X支」→ 用 quantity 兜底核对。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'blueprints'))

from blueprints._helpers import compute_placement_expected_zhi


class PlacementExpectedZhiTests(unittest.TestCase):

    def test_unit_zhi_with_int_quantity_fallback(self):
        # unit='支', quantity=33, remark='', 走 quantity 兜底,返回 (33.0, True)
        self.assertEqual(compute_placement_expected_zhi('', '33', '支'), (33.0, True))

    def test_unit_zhi_with_int_quantity_match(self):
        # 整数 total=33 == expected=33.0 → abs diff = 0 ≤ 0.01,匹配
        exp, has_zhi = compute_placement_expected_zhi('', '33', '支')
        total = 33
        self.assertTrue(has_zhi)
        self.assertLessEqual(abs(total - exp), 0.01)

    def test_unit_zhi_with_int_quantity_mismatch(self):
        # total=30 vs expected=33.0 → diff=3 > 0.01,不匹配
        exp, has_zhi = compute_placement_expected_zhi('', '33', '支')
        total = 30
        self.assertGreater(abs(total - exp), 0.01)  # False

    def test_unit_zhi_with_float_quantity_tolerance(self):
        # quantity=33.5, total=33.5 → abs diff = 0 ≤ 0.01,匹配
        exp, has_zhi = compute_placement_expected_zhi('', '33.5', '支')
        total = 33.5  # 浮点:整数清点通常凑不出 0.5,但 helper 返回 33.5 是正确的"目标"
        # 注意:实际匹配在调用方用 abs(total - exp) <= 0.01 判断
        self.assertEqual(exp, 33.5)
        self.assertTrue(has_zhi)
        self.assertLessEqual(abs(total - exp), 0.01)

    def test_unit_zhi_with_float_quantity_no_match(self):
        # quantity=33.5, total=33 → 差 0.5 > 0.01,不匹配
        exp, has_zhi = compute_placement_expected_zhi('', '33.5', '支')
        total = 33
        self.assertGreater(abs(total - exp), 0.01)

    def test_unit_zhi_quantity_zero_no_fallback(self):
        # quantity='0', 兜底无效 → has_zhi=False
        self.assertEqual(compute_placement_expected_zhi('', '0', '支'), (0.0, False))

    def test_unit_zhi_quantity_invalid_no_fallback(self):
        # quantity='abc' 解析失败 → has_zhi=False
        self.assertEqual(compute_placement_expected_zhi('', 'abc', '支'), (0.0, False))

    def test_unit_zhi_quantity_empty_no_fallback(self):
        # quantity='', 兜底无效 → has_zhi=False
        self.assertEqual(compute_placement_expected_zhi('', '', '支'), (0.0, False))

    def test_remark_with_zhi_takes_precedence(self):
        # unit='支', remark='10支', quantity=33 → 走 remark 路径(10.0, True),忽略 quantity
        self.assertEqual(compute_placement_expected_zhi('10支', '33', '支'), (10.0, True))

    def test_unit_y_no_fallback(self):
        # unit='y', remark='', quantity='500' → 不走兜底,has_zhi=False
        self.assertEqual(compute_placement_expected_zhi('', '500', 'y'), (0.0, False))

    def test_unit_empty_no_fallback(self):
        # unit='', remark='', quantity='10' → 不走兜底
        self.assertEqual(compute_placement_expected_zhi('', '10', ''), (0.0, False))

    def test_remark_multiple_zhi_sum(self):
        # remark='3支+5支' → sum=8(原逻辑)
        self.assertEqual(compute_placement_expected_zhi('3支+5支', '', 'y'), (8.0, True))

    def test_remark_with_zhi_unit_y_kept(self):
        # unit='y', remark='20支' → 用 remark(20.0, True),不走 quantity 兜底
        self.assertEqual(compute_placement_expected_zhi('20支', '500', 'y'), (20.0, True))


if __name__ == '__main__':
    unittest.main()
