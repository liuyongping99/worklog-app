"""PaddleOCREngine.extract_text() 单测：mock 掉 paddle 模型，只验证拼接逻辑。"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from blueprints.ocr_engine import PaddleOCREngine


class ExtractTextTests(unittest.TestCase):
    def test_joins_lines_from_ocr_result(self):
        eng = PaddleOCREngine()
        # 伪造 PaddleOCR 3.x 结果结构：[[ [bbox,[text,conf]], ... ]]
        fake_result = [[
            [[[0, 0], [1, 0], [1, 1], [0, 1]], ['硬加面', 0.99]],
            [[[0, 2], [1, 2], [1, 3], [0, 3]], ['黑色 1.5m', 0.95]],
        ]]
        eng._ensure_model = mock.MagicMock()
        eng._resize_if_needed = mock.MagicMock(return_value=b'x')
        eng._ocr = mock.MagicMock()
        eng._ocr.ocr = mock.MagicMock(return_value=fake_result)

        text = eng.extract_text(b'fake-bytes')
        self.assertIn('硬加面', text)
        self.assertIn('黑色 1.5m', text)

    def test_empty_result_returns_empty(self):
        eng = PaddleOCREngine()
        eng._ensure_model = mock.MagicMock()
        eng._resize_if_needed = mock.MagicMock(return_value=b'x')
        eng._ocr = mock.MagicMock()
        eng._ocr.ocr = mock.MagicMock(return_value=[[]])
        self.assertEqual(eng.extract_text(b'x'), '')


if __name__ == '__main__':
    unittest.main()
