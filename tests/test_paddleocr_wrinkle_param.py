"""PaddleOCREngine `apply_wrinkle_enhance` 参数路由单测。

本任务(Task 3)在 PaddleOCREngine 类内加 4 处修改:
  1. __init__ 加 _wrinkle_ocr = None
  2. _ensure_model 加第二个 PaddleOCR(...) 实例化(更激进参数)
  3. extract_text 加 apply_wrinkle_enhance 参数 + 路由
  4. extract_text_with_conf 加 apply_wrinkle_enhance 参数 + 路由

测试要点:
- 用 MagicMock 完全替代 PaddleOCR(避免 200MB 模型初始化)
- 验证参数路由正确(True → _wrinkle_ocr, False → _ocr)
- 验证 CLAHE 调用时机(True → 调, False → 不调)
- 默认 False 保持向后兼容

全局约束:
- apply_wrinkle_enhance=False 为默认值,老调用方零感知
- _wrinkle_ocr 加载失败时路由回 _ocr(在 _ensure_model 内部 try/except 处理)
"""
import unittest
from unittest.mock import MagicMock, patch

from blueprints.ocr_engine import PaddleOCREngine


class PaddleOCRWrinkleParamTests(unittest.TestCase):
    """PaddleOCREngine `apply_wrinkle_enhance` 参数路由 — 6 项覆盖。

    所有测试用 MagicMock 替代真实 PaddleOCR 实例,只验证路由逻辑,不实际跑 OCR。
    """

    def _make_engine(self):
        """构造 PaddleOCREngine 实例,塞入两个 MagicMock 替代真实 _ocr / _wrinkle_ocr。"""
        engine = PaddleOCREngine.__new__(PaddleOCREngine)
        engine._ocr = MagicMock(name='_ocr')
        engine._wrinkle_ocr = MagicMock(name='_wrinkle_ocr')
        return engine

    def test_extract_text_accepts_default_param(self):
        """无参调用不抛 TypeError — apply_wrinkle_enhance 应有默认值 False。"""
        engine = self._make_engine()
        # 不传 apply_wrinkle_enhance — 必须不抛 TypeError
        with patch.object(engine, '_ensure_model'), \
             patch.object(engine, '_resize_if_needed', return_value=b'data'), \
             patch.object(engine, '_enhance_wrinkle_label') as mock_enhance:
            engine._ocr.ocr.return_value = [[[[0, 0], ['hello', 0.99]]]]
            result = engine.extract_text(b'fake_image_bytes')
        self.assertEqual(result, 'hello',
            '无参调用应走默认 apply_wrinkle_enhance=False 路径, 正常返回 OCR 文本')
        # 默认 False → 不应调 CLAHE
        mock_enhance.assert_not_called()

    def test_extract_text_routes_to_wrinkle_ocr_when_true(self):
        """apply_wrinkle_enhance=True → _wrinkle_ocr.ocr 被调, _ocr.ocr 不被调。"""
        engine = self._make_engine()
        with patch.object(engine, '_ensure_model'), \
             patch.object(engine, '_resize_if_needed', return_value=b'data'), \
             patch.object(engine, '_enhance_wrinkle_label', return_value=b'enhanced'):
            engine._wrinkle_ocr.ocr.return_value = [[[[0, 0], ['wrinkle', 0.95]]]]
            engine._ocr.ocr.return_value = [[[[0, 0], ['normal', 0.95]]]]
            result = engine.extract_text(b'fake', apply_wrinkle_enhance=True)
        self.assertEqual(result, 'wrinkle',
            'apply_wrinkle_enhance=True 时应返回 _wrinkle_ocr 的结果')
        engine._wrinkle_ocr.ocr.assert_called_once()
        engine._ocr.ocr.assert_not_called()

    def test_extract_text_routes_to_normal_ocr_when_false(self):
        """apply_wrinkle_enhance=False → _ocr.ocr 被调, _wrinkle_ocr.ocr 不被调。"""
        engine = self._make_engine()
        with patch.object(engine, '_ensure_model'), \
             patch.object(engine, '_resize_if_needed', return_value=b'data'), \
             patch.object(engine, '_enhance_wrinkle_label') as mock_enhance:
            engine._ocr.ocr.return_value = [[[[0, 0], ['normal', 0.95]]]]
            result = engine.extract_text(b'fake', apply_wrinkle_enhance=False)
        self.assertEqual(result, 'normal')
        engine._ocr.ocr.assert_called_once()
        engine._wrinkle_ocr.ocr.assert_not_called()
        # False → 不应调 CLAHE
        mock_enhance.assert_not_called()

    def test_extract_text_with_conf_routes_correctly(self):
        """extract_text_with_conf 也支持参数 — True/False 各路由一次。"""
        engine = self._make_engine()
        # True 路径
        with patch.object(engine, '_ensure_model'), \
             patch.object(engine, '_resize_if_needed', return_value=b'data'), \
             patch.object(engine, '_enhance_wrinkle_label', return_value=b'enhanced'):
            engine._wrinkle_ocr.ocr.return_value = [[[[0, 0], ['wrinkle', 0.95]]]]
            text, conf = engine.extract_text_with_conf(b'fake', apply_wrinkle_enhance=True)
        self.assertEqual(text, 'wrinkle')
        self.assertAlmostEqual(conf, 0.95)
        engine._wrinkle_ocr.ocr.assert_called_once()
        engine._ocr.ocr.assert_not_called()

        # 重置 mock, False 路径
        engine._wrinkle_ocr.reset_mock()
        engine._ocr.reset_mock()
        with patch.object(engine, '_ensure_model'), \
             patch.object(engine, '_resize_if_needed', return_value=b'data'), \
             patch.object(engine, '_enhance_wrinkle_label') as mock_enhance:
            engine._ocr.ocr.return_value = [[[[0, 0], ['normal', 0.92]]]]
            text, conf = engine.extract_text_with_conf(b'fake', apply_wrinkle_enhance=False)
        self.assertEqual(text, 'normal')
        self.assertAlmostEqual(conf, 0.92)
        engine._ocr.ocr.assert_called_once()
        engine._wrinkle_ocr.ocr.assert_not_called()
        mock_enhance.assert_not_called()

    def test_extract_text_with_enhance_runs_clahe(self):
        """apply_wrinkle_enhance=True 时 _enhance_wrinkle_label 必须被调一次。"""
        engine = self._make_engine()
        with patch.object(engine, '_ensure_model'), \
             patch.object(engine, '_resize_if_needed', return_value=b'data'), \
             patch.object(engine, '_enhance_wrinkle_label', return_value=b'enhanced') as mock_enhance:
            engine._wrinkle_ocr.ocr.return_value = [[[[0, 0], ['x', 0.99]]]]
            engine.extract_text(b'fake', apply_wrinkle_enhance=True)
        mock_enhance.assert_called_once_with(b'data')

    def test_extract_text_without_enhance_skips_clahe(self):
        """apply_wrinkle_enhance=False 时 _enhance_wrinkle_label 不被调(默认 False 也同样)。"""
        engine = self._make_engine()
        with patch.object(engine, '_ensure_model'), \
             patch.object(engine, '_resize_if_needed', return_value=b'data'), \
             patch.object(engine, '_enhance_wrinkle_label') as mock_enhance:
            engine._ocr.ocr.return_value = [[[[0, 0], ['x', 0.99]]]]
            engine.extract_text(b'fake', apply_wrinkle_enhance=False)
        mock_enhance.assert_not_called()


if __name__ == '__main__':
    unittest.main()
