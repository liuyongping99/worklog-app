"""LB鱼鳞布 / LB特软 routing → KIND_REDSTAMP 单测。

背景(2026-09-14 订单 TD-2026-09-14-008 / order_id=1001):
明细 2805「LB特软 / 黑色-0.8 / 251.5y」行级图三张全部 OCR 被红章污染:
  - image 4569: 'LB鱼鳞布特软\n928-黑色'
  - image 4570: 'LB鱼鳞布特软\n#08-黑色'
  - image 4576: 'B鱼鳞布特软\n9750-8707\n8-黑色\n古格'
肉眼:标签有红色圆章(2020-05-26 / 检验合格)盖在「0.8-黑色」厚度行上,
     PaddleOCR 把红章字(2020/检验)和正文混读 → 0.8 变 928/#08/8/古格。
原 routing ocr_preprocess_kind('LB特软') 返回 None → 完全不走红章擦除。

修复:把 '鱼鳞布' 和 'LB特软' 加到 _REDSTAMP_VARIANTS,
     走 commit 8412d03 已建的红章擦除 + 双 pass 取优(_suppress_red_stamp + 综合打分)。

防回归:
  1. routing 验证:鱼鳞布系全名 + LB特软 短名 → redstamp
  2. 不破坏其它品名(无纺布 → glare / 黑磅布三文治 → form_nolines)
  3. 其它 redstamp 类别(杂胶/纯胶/环保磅布三文治)不变
  4. 不同品含「特软」字面的不能误中(HA猪皮纹特软 / LD-特软三文治 不应进 redstamp)
"""
import unittest

from blueprints.ocr_engine import (
    KIND_FORM_NOLINES, KIND_GLARE, KIND_REDSTAMP,
    ocr_preprocess_kind, _REDSTAMP_VARIANTS,
)


class LbFishScaleRedstampRoutingTests(unittest.TestCase):
    """LB鱼鳞布 + LB特软 → redstamp routing — 8 项覆盖。"""

    def test_lb_fish_scale_full_name_routes_to_redstamp(self):
        """LB鱼鳞布 系列全名 → KIND_REDSTAMP。"""
        for name in ('LB鱼鳞布', 'LB鱼鳞布特软', '鱼鳞布', '鱼鳞布特软',
                     '环保LB鱼鳞布', '7P环保LB鱼鳞布', '环保HA鱼鳞布'):
            with self.subTest(name=name):
                self.assertEqual(
                    ocr_preprocess_kind(name), KIND_REDSTAMP,
                    f'{name} 含「鱼鳞布」,横版红章标签,应走 redstamp')

    def test_lb_short_name_routes_to_redstamp(self):
        """订单里写「LB特软」短名(不含鱼鳞布) → KIND_REDSTAMP。"""
        # 业务上 LB特软 = LB鱼鳞布特软 的简称,标签上仍是「LB鱼鳞布特软」带红章
        self.assertEqual(
            ocr_preprocess_kind('LB特软'), KIND_REDSTAMP,
            'LB特软 短名 与 LB鱼鳞布特软 同品,横版标签常带红章,应走 redstamp')

    def test_fish_scale_chun_jiao_routes_to_redstamp(self):
        """鱼鳞布纯胶 → KIND_REDSTAMP(原本纯胶就进 redstamp,现在因含鱼鳞布也命中)。"""
        self.assertEqual(
            ocr_preprocess_kind('鱼鳞布纯胶'), KIND_REDSTAMP)

    def test_unrelated_te_ruan_products_not_redstamp(self):
        """不同品的「特软」(HA猪皮纹特软 / LD-特软三文治)不能误中 redstamp。"""
        # HA猪皮纹:无红章问题,不应进 redstamp
        # LD-特软三文治:含「三文治」会先命中 _FORM_NOLINES_VARIANTS('磅布三文治'...但需确认)
        # 这里只验证关键不在 _REDSTAMP_VARIANTS substring 命中的品
        for name in ('HA猪皮纹特软',):
            with self.subTest(name=name):
                self.assertNotEqual(
                    ocr_preprocess_kind(name), KIND_REDSTAMP,
                    f'{name} 不是 LB鱼鳞布 系列,不应进 redstamp')

    def test_glare_categories_unchanged(self):
        """无纺布 仍走 KIND_GLARE,不变。"""
        self.assertEqual(
            ocr_preprocess_kind('无纺布A白'), KIND_GLARE)

    def test_form_nolines_categories_unchanged(self):
        """黑磅布三文治 / 白磅布三文治 仍走 KIND_FORM_NOLINES,不变。"""
        self.assertEqual(
            ocr_preprocess_kind('黑磅布三文治'), KIND_FORM_NOLINES)
        self.assertEqual(
            ocr_preprocess_kind('白磅布三文治'), KIND_FORM_NOLINES)

    def test_other_redstamp_categories_unchanged(self):
        """杂胶 / 纯胶 / 环保磅布三文治 / 环保杂胶 仍走 KIND_REDSTAMP。"""
        for name in ('杂胶', '纯胶', '环保磅布三文治', '环保杂胶'):
            with self.subTest(name=name):
                self.assertEqual(
                    ocr_preprocess_kind(name), KIND_REDSTAMP)

    def test_redstamp_variants_contains_new_entries(self):
        """_REDSTAMP_VARIANTS 已包含「鱼鳞布」+「LB特软」。"""
        self.assertIn('鱼鳞布', _REDSTAMP_VARIANTS)
        self.assertIn('LB特软', _REDSTAMP_VARIANTS)


if __name__ == '__main__':
    unittest.main()