"""match_label_to_row() 单测：标签 OCR 文本 vs 本行品名+规格。"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'blueprints'))

from blueprints._helpers import match_label_to_row, _normalize_for_match


class NormalizeTests(unittest.TestCase):
    def test_fullwidth_and_punct_and_code(self):
        # 全角数字→半角、去标点、码→y、小写
        self.assertEqual(_normalize_for_match('Ａ１２ 码, 黑-色'), 'a12y黑色')

    def test_empty(self):
        self.assertEqual(_normalize_for_match(''), '')
        self.assertEqual(_normalize_for_match(None), '')


class MatchLabelToRowTests(unittest.TestCase):
    def test_exact_name_is_green(self):
        status, score, reason = match_label_to_row('硬加面 黑色 1.5m', '硬加面', '黑色')
        self.assertEqual(status, 'green')
        self.assertGreaterEqual(score, 85)
        # reason 必须含本地模糊匹配 + 命中信息(给前端 hover 用)
        self.assertIn('本地模糊匹配', reason)
        self.assertIn('品名命中', reason)
        self.assertIn('规格命中', reason)

    def test_scrambled_order_still_green(self):
        # OCR 把顺序打乱："加面硬" 仍应匹配 "硬加面"
        status, score, reason = match_label_to_row('加面硬 黑色', '硬加面', '')
        self.assertEqual(status, 'green')
        self.assertIn('本地模糊匹配', reason)

    def test_noise_around_target_still_matches(self):
        # 标签夹杂无关文字/编码
        status, score, reason = match_label_to_row('厂家直销 硬加面 批次20240715 电话13800', '硬加面', '')
        self.assertIn(status, ('green', 'yellow'))
        self.assertGreaterEqual(score, 60)
        self.assertIn('本地模糊匹配', reason)

    def test_wrong_product_is_red(self):
        status, score, reason = match_label_to_row('珍珠棉 白色 200y', '硬加面', '黑色')
        self.assertEqual(status, 'red')
        self.assertLess(score, 60)
        # 红牌 reason 必须含"品名未命中",让用户 hover 时知道原因
        self.assertIn('本地模糊匹配', reason)
        self.assertIn('品名未命中', reason)

    def test_spec_bonus_lifts_score(self):
        # 品名对上、规格也对上 → 分数不低于只对品名
        _, sc_no_spec, _ = match_label_to_row('硬加面', '硬加面', '')
        _, sc_spec, reason_with_spec = match_label_to_row('硬加面 黑色', '硬加面', '黑色')
        self.assertGreaterEqual(sc_spec, sc_no_spec)
        # 有 spec 时 reason 应同时提及品名 + 规格
        self.assertIn('规格命中', reason_with_spec)

    def test_empty_text_or_name_is_red(self):
        _, _, reason_empty_text = match_label_to_row('', '硬加面', '黑色')
        self.assertEqual(reason_empty_text[:2], '本地')  # 退化提示
        _, _, reason_empty_name = match_label_to_row('硬加面', '', '')
        self.assertEqual(reason_empty_name[:2], '本地')

    def test_red_reason_explains_mismatch(self):
        """红牌场景是用户最关心的:reason 必须清楚说明为什么不匹配。"""
        status, score, reason = match_label_to_row('珍珠棉 白色 200y', '硬加面', '黑色')
        self.assertEqual(status, 'red')
        # 应该同时报告品名和规格都没命中
        self.assertIn('品名未命中', reason)
        self.assertIn('规格未命中', reason)


if __name__ == '__main__':
    unittest.main()
