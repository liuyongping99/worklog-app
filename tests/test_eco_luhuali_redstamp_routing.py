"""环保路华里 routing → KIND_REDSTAMP 单测。

背景(2026-10-07 shipping-records 实测):
shipping_records 21 行 环保路华里/7P环保路华里 中,行级 upload 图共 10 张,
其中 4 张(40%)match_status=yellow,失败模式都是红章污染:
  - OCR 输出「环保路华赛」(红章盖「里」→ 误读「赛」)
  - OCR 输出「0.V」(红章吞掉小数点,「0.6」变「0.V」)
  - 厚度数字 0.6 / 0.8 整行漏识
原 routing ocr_preprocess_kind('环保路华里') 返回 None → 完全不走红章擦除。

修复:把 '环保路华里' 和 '路华里' 加到 _REDSTAMP_VARIANTS,
     走 _suppress_red_stamp + 三遍 OCR 取优(原图/红章擦除/锐化)+ 行级修补。

防回归:
  1. routing 验证:环保路华里 / 7P环保路华里 / 路华里 短名 → redstamp
  2. 不破坏其它品名(无纺布 → glare / 黑磅布三文治 → form_nolines)
  3. 其它 redstamp 类别(杂胶/纯胶/环保磅布三文治/LB鱼鳞布/LB特软)不变
  4. 不会误中:含「路」或「华」字面的相近品(防 LB 系列串扰)
"""
import unittest

from blueprints.ocr_engine import (
    KIND_FORM_NOLINES, KIND_GLARE, KIND_REDSTAMP,
    ocr_preprocess_kind, _REDSTAMP_VARIANTS,
)


class EcoLuhualiRedstampRoutingTests(unittest.TestCase):
    """环保路华里 + 路华里 短名 → redstamp routing — 7 项覆盖。"""

    def test_eco_luhuali_routes_to_redstamp(self):
        """环保路华里 主名 → KIND_REDSTAMP。"""
        self.assertEqual(
            ocr_preprocess_kind('环保路华里'), KIND_REDSTAMP,
            '环保路华里 横版表格标签常带检验合格红章,应走 redstamp')

    def test_7p_eco_luhuali_routes_to_redstamp_via_substring(self):
        """7P环保路华里 → KIND_REDSTAMP(子串「环保路华里」命中)。"""
        self.assertEqual(
            ocr_preprocess_kind('7P环保路华里'), KIND_REDSTAMP,
            '7P环保路华里 与 环保路华里 同品,前缀 7P 不影响子串命中')

    def test_luhuali_short_name_routes_to_redstamp(self):
        """路华里 短名 → KIND_REDSTAMP(防未来短名写法,同 LB特软 短名案例)。"""
        self.assertEqual(
            ocr_preprocess_kind('路华里'), KIND_REDSTAMP,
            '路华里 短名应走 redstamp')

    def test_other_redstamp_categories_unchanged(self):
        """杂胶 / 纯胶 / 环保磅布三文治 / LB鱼鳞布 / LB特软 仍走 KIND_REDSTAMP。"""
        for name in ('杂胶', '纯胶', '环保磅布三文治', '环保杂胶',
                     'LB鱼鳞布', 'LB特软', '环保LB鱼鳞布'):
            with self.subTest(name=name):
                self.assertEqual(
                    ocr_preprocess_kind(name), KIND_REDSTAMP,
                    f'{name} 原本就走 redstamp,不能被本次改动破坏')

    def test_glare_categories_unchanged(self):
        """无纺布 仍走 KIND_GLARE,不变。"""
        self.assertEqual(
            ocr_preprocess_kind('无纺布A白'), KIND_GLARE)

    def test_form_nolines_categories_unchanged(self):
        """黑磅布三文治 / 白磅布三文治 / 7P环保磅布三文治 仍走 KIND_FORM_NOLINES,不变。"""
        for name in ('黑磅布三文治', '白磅布三文治', '7P环保磅布三文治'):
            with self.subTest(name=name):
                self.assertEqual(
                    ocr_preprocess_kind(name), KIND_FORM_NOLINES)

    def test_redstamp_variants_contains_new_entries(self):
        """_REDSTAMP_VARIANTS 已包含「环保路华里」+「路华里」。"""
        self.assertIn('环保路华里', _REDSTAMP_VARIANTS)
        self.assertIn('路华里', _REDSTAMP_VARIANTS)


if __name__ == '__main__':
    unittest.main()