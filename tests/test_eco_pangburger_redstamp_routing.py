"""环保磅布三文治 routing → KIND_REDSTAMP 单测。

背景(2026-09-02 订单 TD-2026-09-02-005):
订单 911(司机)明细 2511「环保磅布三文治 / 0.6黑双中性 / 500y」行级图
(image 3804, file a3861ef5...)标签含「品名:/规格:/手感:」横版表格 + 检验合格红章,
原图 OCR 把红章内文字误读为「台」字符(在「0.6黑色」下方),低置信度但仍污染输出。

根因:「环保磅布三文治」之前在 _FORM_NOLINES_EXCLUDES 被一刀切排除 → ocr_preprocess_kind
返回 None,完全不走任何 kind 预处理 → 红章盖字段仍漏字。

修复:把「环保磅布三文治」从 _FORM_NOLINES_EXCLUDES 移除,加到 _REDSTAMP_VARIANTS。
KIND_REDSTAMP 路径 = 整图 _ocr(对表格线无副作用)+ 红章擦除 + 双 pass 取优
(commit 8412d03 已建好的基础设施)。

防回归:
  1. routing 验证:环保磅布三文治 → redstamp,其它磅布三文治变体不变
  2. 不破坏 7P环保磅布三文治(横版表格标签但无红章,继续走 form_nolines)
  3. 不破坏 黑磅布三文治/白磅布三文治(form_nolines 不变)
  4. 其它 kind 白名单不动(无纺布/杂胶/纯胶)
"""
import unittest

from blueprints.ocr_engine import (
    KIND_FORM_NOLINES, KIND_GLARE, KIND_REDSTAMP,
    ocr_preprocess_kind, _FORM_NOLINES_EXCLUDES, _REDSTAMP_VARIANTS,
)


class EcoPangBurgerRedstampRoutingTests(unittest.TestCase):
    """环保磅布三文治 → redstamp routing — 6 项覆盖。"""

    def test_eco_pangburger_routes_to_redstamp(self):
        """核心:环保磅布三文治 必须走 KIND_REDSTAMP(红章擦除 + 双 pass)。"""
        kind = ocr_preprocess_kind('环保磅布三文治')
        self.assertEqual(kind, KIND_REDSTAMP,
            '环保磅布三文治 横版表格标签常带红章,应走 redstamp 路径')

    def test_7p_eco_pangburger_keeps_form_nolines(self):
        """7P环保磅布三文治 仍走 KIND_FORM_NOLINES(轨 1 顺序匹配,不被覆盖)。"""
        kind = ocr_preprocess_kind('7P环保磅布三文治')
        self.assertEqual(kind, KIND_FORM_NOLINES,
            '7P环保磅布三文治 含 7P 前缀,优先匹配 _FORM_NOLINES_VARIANTS')

    def test_black_pangburger_keeps_form_nolines(self):
        """黑磅布三文治 routing 不变(KIND_FORM_NOLINES)。"""
        self.assertEqual(
            ocr_preprocess_kind('黑磅布三文治'),
            KIND_FORM_NOLINES)

    def test_white_pangburger_keeps_form_nolines(self):
        """白磅布三文治 routing 不变(KIND_FORM_NOLINES)。"""
        self.assertEqual(
            ocr_preprocess_kind('白磅布三文治'),
            KIND_FORM_NOLINES)

    def test_other_redstamp_categories_unchanged(self):
        """杂胶 / 纯胶 / 环保杂胶 仍走 KIND_REDSTAMP,不变。"""
        for name in ('杂胶', '纯胶', '环保杂胶'):
            self.assertEqual(
                ocr_preprocess_kind(name),
                KIND_REDSTAMP,
                f'{name} 应继续走 redstamp')

    def test_glare_categories_unchanged(self):
        """无纺布 仍走 KIND_GLARE,不变。"""
        self.assertEqual(
            ocr_preprocess_kind('无纺布A白'),
            KIND_GLARE)

    def test_excludes_no_longer_contains_eco_pangburger(self):
        """_FORM_NOLINES_EXCLUDES 已移除 环保磅布三文治(改走 redstamp)。"""
        self.assertNotIn('环保磅布三文治', _FORM_NOLINES_EXCLUDES,
            '环保磅布三文治 已迁出 _FORM_NOLINES_EXCLUDES,改为走 redstamp 路径')

    def test_redstamp_variants_contains_eco_pangburger(self):
        """_REDSTAMP_VARIANTS 已包含 环保磅布三文治。"""
        self.assertIn('环保磅布三文治', _REDSTAMP_VARIANTS,
            '环保磅布三文治 必须显式声明在 redstamp 白名单')


if __name__ == '__main__':
    unittest.main()