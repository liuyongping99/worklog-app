# -*- coding: utf-8 -*-
r"""出货/入库/装柜 三页 placement「散码」口径回归测试。

历史 bug 链:
  - 2026-08-19: 备注 ``160支*34.5y`` 会被旧 SANMA_RE ``(?<![\d.])(\d+)\s*[yY]``
    漏判 — 但更深的 bug 是它**只**对小数 y 用负向后顾巧劲, 整数 y 仍误算:
    ``105支*32y+37y`` → 旧 SANMA_RE 把 32y + 37y 都算进散码 → exp_san=69,
    与真实期望 37 永远对不上 (用户在出货页 / TD-2026-10-09-006 报的就是这个 69).

修复 (2026-10-09): 把散码解析抽到 ``blueprints._helpers.parse_loose_yards``,
  算法是「先抠掉 X支*Yy / Yy*X支 整段乘法项, 再累加剩余 Yy」, 与
  ``check_remark`` 早就用的一致口径。

测试覆盖:
  - 乘法项里的 y 不算散码 (整数 32y / 小数 34.5y 都不算)
  - 加法项 / 裸 y 算散码 (整数 37y / 小数 5.5y 都算)
  - 多散码累加
  - 大写 Y、含空格 兼容
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from blueprints._helpers import parse_loose_yards  # noqa: E402


def _loose(remark: str) -> float:
    """包装 helper 口径, 等价于 parse_loose_yards(remark)[0]."""
    loose, _has = parse_loose_yards(remark)
    return loose


class ShippingSanmaRegexTests(unittest.TestCase):
    """覆盖 parse_loose_yards 的关键 case."""

    # ── 乘法项里的 y 不算散码(整数 / 小数都不算) ──────────────────

    def test_mul_form_integer_y_not_counted(self):
        """105支*32y: 32y 是每支码数, 不是散码."""
        self.assertEqual(_loose('105支*32y'), 0)

    def test_mul_form_decimal_y_not_counted(self):
        """160支*34.5y: 触发现场 bug 的真实 record 2193 数据, 34.5y 是每支码数."""
        self.assertEqual(_loose('160支*34.5y'), 0)

    def test_mul_form_reversed_y_first(self):
        """32y*105支: 颠倒顺序的乘法形式, 32y 也不应算散码."""
        self.assertEqual(_loose('32y*105支'), 0)

    # ── 加法项 / 裸 y 算散码 ────────────────────────────────────────

    def test_add_form_integer_y_counted(self):
        """105支+37y: 37y 是散码."""
        self.assertEqual(_loose('105支+37y'), 37)

    def test_add_form_decimal_y_counted(self):
        """100支+34.5y: 34.5y 是小数散码."""
        self.assertEqual(_loose('100支+34.5y'), 34.5)

    def test_naked_decimal_y_counted(self):
        """5.5y / 0.8y: 裸小数 y 是合法散码."""
        self.assertEqual(_loose('5.5y'), 5.5)
        self.assertEqual(_loose('0.8y'), 0.8)

    # ── 用户原 case(2026-10-09 出货页 TD-2026-10-09-006 报 69) ──

    def test_user_case_mul_plus_add(self):
        """105支*32y+37y: 乘法项 32y 不算, 加法项 37y 算 → 散码 37."""
        self.assertEqual(_loose('105支*32y+37y'), 37)

    def test_user_case_loose_only(self):
        """纯加法(无乘法项): 37y 应识别为散码 37."""
        self.assertEqual(_loose('37y'), 37)

    # ── 多散码累加 ────────────────────────────────────────────────

    def test_multi_y_summed(self):
        """1支+2y+3y: 多散码累加 2+3=5."""
        self.assertEqual(_loose('1支+2y+3y'), 5)

    def test_multi_y_with_decimal(self):
        """20支*32y+5y+3y: 乘法项 32y 不算, 加法项 5y+3y 累加=8."""
        self.assertEqual(_loose('20支*32y+5y+3y'), 8)

    # ── 既有行为保持不变 ──────────────────────────────────────────

    def test_no_y_no_match(self):
        """100支: 仅支, 不应匹配散码."""
        self.assertEqual(_loose('100支'), 0)

    def test_only_zhi_no_match(self):
        """2555支: 大支数也不匹配."""
        self.assertEqual(_loose('2555支'), 0)

    def test_zhi_with_remark_no_match(self):
        """250支 特软: OK record 2190 的真实数据, 不应匹配."""
        self.assertEqual(_loose('250支 特软'), 0)

    def test_capital_Y_still_counted(self):
        """5Y: 大写 Y 也算 (兼容历史备注)."""
        self.assertEqual(_loose('5Y'), 5)

    def test_space_before_y(self):
        """2 y: 数字与 y 间允许空格."""
        self.assertEqual(_loose('2 y'), 2)

    def test_empty_string(self):
        """'': 空字符串返回 0."""
        self.assertEqual(_loose(''), 0)


class PlacementMatchIntegrationTests(unittest.TestCase):
    """端到端: 用 prod db 复制的 worklog_test.db 跑一遍 placement_match,
    验证 record 2193 (备注 160支*34.5y + 4 张各 manual=40 的图) 修复后
    应被判定为 match=True → placement-ok class 出现 → 绿框显示。
    """

    def test_record_2193_placement_match_true_after_fix(self):
        """record 2193: 160支*34.5y + 4×40=160 支 → 应判定为 match."""
        from models.orders import PlacementImage, ShippingRecord

        rec = ShippingRecord.get_by_id(2193)
        if rec is None:
            self.skipTest('record 2193 不存在 (生产 db 还没同步)')
        remark = rec.get('remark') or ''
        if remark != '160支*34.5y':
            self.skipTest(f'record 2193 备注已变更: {remark!r}')

        pimgs = PlacementImage.get_by_record(2193)
        if len(pimgs) != 4:
            self.skipTest(f'record 2193 点数图数变更: {len(pimgs)} (期望 4)')

        total = sum(
            (p.get('manual_count') if p.get('manual_count') is not None else (p.get('n_marks') or 0))
            for p in pimgs
        )
        loose = sum(p.get('loose_count', 0) for p in pimgs)
        exp_san, has_san = parse_loose_yards(remark)

        self.assertEqual(total, 160, '点数图合计支数应为 160')
        self.assertEqual(exp_san, 0, f'小数 y 不应被误判散码 (实际 {exp_san})')
        self.assertFalse(has_san, f'has_san 应为 False, 实际 {has_san}')

        from blueprints.shipping import compute_placement_match
        match = compute_placement_match(rec, pimgs)
        self.assertTrue(match, 'placement_match 应为 True → 绿框显示')


if __name__ == '__main__':
    unittest.main()
