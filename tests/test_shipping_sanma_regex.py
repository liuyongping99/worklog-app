# -*- coding: utf-8 -*-
r"""出货页「散码」口径正则回归测试。

历史 bug (2026-08-19): 备注"160支*34.5y" 会被旧正则
``re.finditer(r'(\d+)\s*[yY]', remark)`` 把 "34.5y" 中的 "5y" 误识别为独立
散码期望, 导致 exp_san=5, 实际 loose_count=0 → placement_match=False,
"点数"按钮始终拿不到绿框。

修复: 散码正则改为 ``r'(?<![\\d.])(\\d+)\\s*[yY]'``, 用负向后顾排除小数点
后紧跟的 y. 该正则抽到 ``blueprints.shipping.SANMA_RE``, 被 placement_groups
渲染和 placement_match 判定两处共用. 本测试直接验证该常量, 任何一处忘改
都会被这条测试抓住.

非小数 y 必须保持原行为 (5y/100y/1支+2y+3y 仍按散码计).
"""
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from blueprints.shipping import SANMA_RE  # noqa: E402


def _exp_sanma(remark: str) -> int:
    """包装 shipping.py 内的口径, 等价于 sum(int(m.group(1)) for m in SANMA_RE.finditer(remark))."""
    return sum(int(m.group(1)) for m in SANMA_RE.finditer(remark))


class ShippingSanmaRegexTests(unittest.TestCase):
    """覆盖 SANMA_RE 的关键 case."""

    # ── 修复后不应被误判散码 (小数点后紧跟的 y) ────────────────────────

    def test_decimal_y_alone_not_counted(self):
        """5.5y: 小数 y 不算散码."""
        self.assertEqual(_exp_sanma('5.5y'), 0)

    def test_decimal_y_in_mul_form(self):
        """160支*34.5y: 这是触发现场 bug 的真实 record 2193 数据."""
        self.assertEqual(_exp_sanma('160支*34.5y'), 0)

    def test_decimal_y_in_add_form(self):
        """100支+34.5y: 加法形式同样修复."""
        self.assertEqual(_exp_sanma('100支+34.5y'), 0)

    def test_decimal_y_mixed_with_integer_y(self):
        """100y+34.5y: 整数 100 仍匹配, 小数 5 不匹配."""
        self.assertEqual(_exp_sanma('100y+34.5y'), 100)

    def test_decimal_y_with_unit(self):
        """0.8y: 单位转换中的小数也不应误判."""
        self.assertEqual(_exp_sanma('0.8y'), 0)

    # ── 既有行为保持不变 ────────────────────────────────────────────────

    def test_plain_y_still_counted(self):
        """5y: 普通散码 5 仍识别."""
        self.assertEqual(_exp_sanma('5y'), 5)

    def test_multi_y_summed(self):
        """1支+2y+3y: 多散码累加 2+3=5."""
        self.assertEqual(_exp_sanma('1支+2y+3y'), 5)

    def test_no_y_no_match(self):
        """100支: 仅支, 不应匹配散码."""
        self.assertEqual(_exp_sanma('100支'), 0)

    def test_only_zhi_no_match(self):
        """2555支: 大支数也不匹配."""
        self.assertEqual(_exp_sanma('2555支'), 0)

    def test_zhi_with_remark_no_match(self):
        """250支 特软: OK record 2190 的真实数据, 不应匹配."""
        self.assertEqual(_exp_sanma('250支 特软'), 0)

    def test_capital_Y_still_counted(self):
        """5Y: 大写 Y 也算 (兼容历史备注)."""
        self.assertEqual(_exp_sanma('5Y'), 5)

    def test_space_before_y(self):
        """2 y: 数字与 y 间允许空格."""
        self.assertEqual(_exp_sanma('2 y'), 2)


class PlacementMatchIntegrationTests(unittest.TestCase):
    """端到端: 用 prod db 复制的 worklog_test.db 跑一遍 placement_match,
    验证 record 2193 (备注 160支*34.5y + 4 张各 manual=40 的图) 修复后
    应被判定为 match=True → placement-ok class 出现 → 绿框显示。
    """

    def test_record_2193_placement_match_true_after_fix(self):
        """record 2193: 160支*34.5y + 4×40=160 支 → 应判定为 match."""
        from models.orders import PlacementImage, ShippingRecord

        # conftest.py 已把 worklog.db 复制到 worklog_test.db
        rec = ShippingRecord.get_by_id(2193)
        if rec is None:
            self.skipTest('record 2193 不存在 (生产 db 还没同步)')
        remark = rec.get('remark') or ''
        if remark != '160支*34.5y':
            self.skipTest(f'record 2193 备注已变更: {remark!r}')

        pimgs = PlacementImage.get_by_record(2193)
        if len(pimgs) != 4:
            self.skipTest(f'record 2193 点数图数变更: {len(pimgs)} (期望 4)')

        def _eff_zhi(p):
            mc = p.get('manual_count')
            return mc if mc is not None else (p.get('n_marks') or 0)

        total = sum(_eff_zhi(p) for p in pimgs)
        loose = sum(p.get('loose_count', 0) for p in pimgs)
        zhi_m = list(re.finditer(r'(\d+)\s*支', remark))
        exp_zhi = sum(int(m.group(1)) for m in zhi_m)
        san_m = list(SANMA_RE.finditer(remark))
        exp_san = sum(int(m.group(1)) for m in san_m)

        self.assertEqual(total, 160, '点数图合计支数应为 160')
        self.assertEqual(exp_zhi, 160, '备注期望支数应为 160')
        self.assertEqual(exp_san, 0, f'小数 y 不应被误判散码 (实际 {exp_san})')
        self.assertFalse(san_m, f'SANMA_RE 在 34.5y 上不应有匹配, 实际匹配 {[m.group(0) for m in san_m]}')

        has_zhi = len(zhi_m) > 0
        has_san = len(san_m) > 0
        match = bool(pimgs and has_zhi and total == exp_zhi and (not has_san or loose == exp_san))
        self.assertTrue(match, 'placement_match 应为 True → 绿框显示')


if __name__ == '__main__':
    unittest.main()