"""HA 猪皮纹系列 routing → KIND_REDSTAMP 单测。

背景(2026-10-07 全表 yellow/red 统计):
HA猪皮纹特软 ×19 / 环保HA猪皮纹 ×15 / 7P环保HA猪皮纹 ×8 — 未进 redstamp 白名单,
失败模式同环保路华里:横版表格标签常带「检验合格」红章盖在厚度数字行,
OCR 输出「0.V」「0.舌」「0.18」等误识,AI 比对 yellow。

修复:把 'HA猪皮纹' 和 '环保HA猪皮纹' 加到 _REDSTAMP_VARIANTS,
     走红章擦除 + 灰度转换 4 遍 OCR 取优(同环保路华里案例)。

子串覆盖关系:
- 'HA猪皮纹' → HA猪皮纹特软 / 7P环保HA猪皮纹 / 8P环保HA猪皮纹A黑 等
- '环保HA猪皮纹' → 环保HA猪皮纹 主名(避免被 _FORM_NOLINES_VARIANTS 误中)
- 不进 form_nolines(虽然有表格线):与环保磅布三文治/环保路华里 同设计,
   redstamp 走整图 OCR 不分行,保留表格线无副作用

防回归:
  1. routing 验证:HA猪皮纹全名 + 7P前缀 + 短名 → redstamp
  2. 不破坏其它品名(无纺布 → glare / 黑磅布三文治 → form_nolines)
  3. 其它 redstamp 类别(杂胶/纯胶/环保磅布三文治/环保路华里)不变
  4. 与 LB鱼鳞布系列并存:HA鱼鳞布 仍命中 redstamp(via '鱼鳞布' 子串)
"""
import unittest

from blueprints.ocr_engine import (
    KIND_FORM_NOLINES, KIND_GLARE, KIND_REDSTAMP,
    ocr_preprocess_kind, _REDSTAMP_VARIANTS,
)


class HaZhupiwenRedstampRoutingTests(unittest.TestCase):
    """HA 猪皮纹 + HA 猪皮纹变体 → redstamp routing — 7 项覆盖。"""

    def test_ha_zhupiwen_routes_to_redstamp(self):
        """HA猪皮纹特软 主名 → KIND_REDSTAMP。"""
        self.assertEqual(
            ocr_preprocess_kind('HA猪皮纹特软'), KIND_REDSTAMP,
            'HA猪皮纹特软 横版表格标签常带红章,应走 redstamp')

    def test_eco_ha_zhupiwen_routes_to_redstamp(self):
        """环保HA猪皮纹 主名 → KIND_REDSTAMP。"""
        self.assertEqual(
            ocr_preprocess_kind('环保HA猪皮纹'), KIND_REDSTAMP,
            '环保HA猪皮纹 与 HA猪皮纹特软 同形态,应走 redstamp')

    def test_7p_eco_ha_zhupiwen_routes_to_redstamp_via_substring(self):
        """7P环保HA猪皮纹 → KIND_REDSTAMP(子串「环保HA猪皮纹」命中)。"""
        self.assertEqual(
            ocr_preprocess_kind('7P环保HA猪皮纹'), KIND_REDSTAMP,
            '7P 前缀不影响子串命中')

    def test_eco_ha_fish_scale_unaffected(self):
        """环保HA鱼鳞布 仍命中 redstamp(via '鱼鳞布' 子串)。新加 'HA白'/'HA猪皮纹'
        不能破坏既有 '鱼鳞布' 系列。"""
        self.assertEqual(
            ocr_preprocess_kind('环保HA鱼鳞布'), KIND_REDSTAMP,
            '环保HA鱼鳞布 仍应走 redstamp,不能被本次改动破坏')

    def test_other_redstamp_categories_unchanged(self):
        """杂胶 / 纯胶 / 环保磅布三文治 / LB鱼鳞布 / LB特软 / 环保路华里 仍走 KIND_REDSTAMP。"""
        for name in ('杂胶', '纯胶', '环保磅布三文治', '环保杂胶',
                     'LB鱼鳞布', 'LB特软', '环保LB鱼鳞布',
                     '环保路华里', '7P环保路华里'):
            with self.subTest(name=name):
                self.assertEqual(
                    ocr_preprocess_kind(name), KIND_REDSTAMP,
                    f'{name} 原本就走 redstamp,不能被本次改动破坏')

    def test_glare_categories_unchanged(self):
        """无纺布 仍走 KIND_GLARE,不变。"""
        self.assertEqual(
            ocr_preprocess_kind('无纺布A白'), KIND_GLARE)

    def test_redstamp_variants_contains_new_entries(self):
        """_REDSTAMP_VARIANTS 已包含「HA猪皮纹」+「环保HA猪皮纹」。"""
        self.assertIn('HA猪皮纹', _REDSTAMP_VARIANTS)
        self.assertIn('环保HA猪皮纹', _REDSTAMP_VARIANTS)


if __name__ == '__main__':
    unittest.main()