"""is_wrinkle_label_category 类别门控单测。

双轨判定:
  轨 1:子串匹配(5 variant)
  轨 2:DeepSeekEngine._classify_product 返回的 category_code 命中白名单

任一轨异常 → 视为未命中, 不阻塞主流程。
"""
import unittest
from unittest.mock import patch

from blueprints.ocr_engine import is_wrinkle_label_category


class WrinkleLabelCategoryTests(unittest.TestCase):
    """类别门控函数 6 项覆盖。"""

    def test_true_for_known_variants(self):
        """4 个 variant 品名都返回 True。
        「磅布三文治」不在白名单——它是其他变体的公共子串,
        命中会误中「环保磅布三文治」(见 test_false_for_huanbao_pangbu_sanwenzhi)。
        """
        variants = [
            '白磅布三文治',
            '黑磅布三文治',
            'B级 磅布三文治',
            '7P环保磅布三文治',
        ]
        for name in variants:
            with self.subTest(product_name=name):
                self.assertTrue(is_wrinkle_label_category(name),
                    f'期望 {name!r} 命中, 实际 False')

    def test_true_for_variant_with_appended_spec(self):
        """「白磅布三文治 1.2硬性」「黑磅布三文治 2m」等带规格后缀的品名。"""
        names = [
            '白磅布三文治 1.2硬性',
            '黑磅布三文治 2m',
            '7P环保磅布三文治 1.5mm',
        ]
        for name in names:
            with self.subTest(product_name=name):
                self.assertTrue(is_wrinkle_label_category(name),
                    f'期望 {name!r} 命中, 实际 False')

    def test_false_for_huanbao_pangbu_sanwenzhi(self):
        """【2026-08-27 修复】环保磅布三文治是有表格线的标准横版标签,
        应走默认 _ocr(整图)路径,不应被识别为 form_nolines 进入分行 OCR 算法。

        背景:订单 TD-2026-08-27-006(879/阿桂)三条环保磅布三文治明细 OCR
        识别不全。根因不是「分行 OCR 聚类算法错」,而是「白名单 '磅布三文治'
        是公共子串,误中 '环保磅布三文治'」让本该走整图 OCR 的有表格线标签
        跑进了无表格线分支。

        删 '磅布三文治' 白名单条目后:其它变体(白磅布三文治/黑磅布三文治/
        B级 磅布三文治/7P环保磅布三文治)靠各自完整字符串命中不受影响。
        """
        # 单独的「环保磅布三文治」+ 各种带规格后缀
        names = [
            '环保磅布三文治',
            '环保磅布三文治 1.2黑双中性',
            '环保磅布三文治 1.0白双中性',
            '环保磅布三文治 0.8软性',
        ]
        for name in names:
            with self.subTest(product_name=name):
                self.assertFalse(is_wrinkle_label_category(name),
                    f'环保磅布三文治是有表格线标签,不应命中 form_nolines;'
                    f'实际 True 表明「磅布三文治」公共子串未被移除')

    def test_pangbu_sanwenzhi_removed_from_whitelist(self):
        """【2026-08-27 修复】'磅布三文治' 不再作为白名单条目。
        历史原因:曾作为「白磅布三文治/黑磅布三文治」的公共子串兜底,
        但子串匹配会误中「环保磅布三文治」(有表格线),已移除。
        单独「磅布三文治」(不带任何前缀)业务中不存在,本断言只验证白名单
        内容,不再对其命中状态做强断言(轨 2 品类 code 仍可能命中)。
        """
        from blueprints.ocr_engine import _FORM_NOLINES_VARIANTS
        self.assertNotIn('磅布三文治', _FORM_NOLINES_VARIANTS,
            '「磅布三文治」公共子串条目已删除 — '
            '它会误中「环保磅布三文治」(有表格线标签)')
        # 7P环保磅布三文治/白磅布三文治/黑磅布三文治 等仍各自完整字符串命中
        self.assertIn('白磅布三文治', _FORM_NOLINES_VARIANTS)
        self.assertIn('黑磅布三文治', _FORM_NOLINES_VARIANTS)
        self.assertIn('7P环保磅布三文治', _FORM_NOLINES_VARIANTS)
        self.assertIn('B级 磅布三文治', _FORM_NOLINES_VARIANTS)

    def test_false_for_other_products(self):
        """无纺布 / PVC桌布 / 杂胶 / 纯胶 / LD特软三文治。"""
        names = [
            '无纺布',
            'PVC桌布',
            '杂胶',
            '纯胶',
            'LD特软三文治',
        ]
        for name in names:
            with self.subTest(product_name=name):
                self.assertFalse(is_wrinkle_label_category(name),
                    f'期望 {name!r} 不命中, 实际 True')

    def test_false_for_empty_and_none(self):
        """'' 和 None。"""
        for name in ('', None):
            with self.subTest(product_name=name):
                self.assertFalse(is_wrinkle_label_category(name),
                    f'期望 {name!r} 不命中, 实际 True')

    def test_false_when_db_exception(self):
        """_classify_product 抛 RuntimeError → 视为未命中, 返回 False。"""
        with patch('blueprints.ocr_engine.DeepSeekEngine._classify_product',
                   side_effect=RuntimeError('模拟 DB 挂')):
            # 用一个不会被子串命中的品名, 强制走轨 2
            self.assertFalse(is_wrinkle_label_category('某未知商品'),
                'DB 异常时应降级返回 False, 不应抛')

    def test_uses_category_code_when_provided(self):
        """轨 2: _classify_product 返回 '021202' → 命中。"""
        with patch('blueprints.ocr_engine.DeepSeekEngine._classify_product',
                   return_value='021202'):
            # 用一个不会被子串命中的品名, 强制走轨 2
            self.assertTrue(is_wrinkle_label_category('某不在白名单的名称'),
                '_classify_product 返回 021202 应命中')


if __name__ == '__main__':
    unittest.main()
