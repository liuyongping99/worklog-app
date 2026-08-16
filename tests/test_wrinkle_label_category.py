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
        """5 个 variant 品名都返回 True。"""
        variants = [
            '白磅布三文治',
            '黑磅布三文治',
            'B级 磅布三文治',
            '7P环保磅布三文治',
            '磅布三文治',
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
            '磅布三文治 3mm',
            '7P环保磅布三文治 1.5mm',
        ]
        for name in names:
            with self.subTest(product_name=name):
                self.assertTrue(is_wrinkle_label_category(name),
                    f'期望 {name!r} 命中, 实际 False')

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
