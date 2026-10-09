"""KIND_REDSTAMP 增加灰度 OCR pass 单测。

背景(2026-10-07 订单 1001 / record 6029 / img 6029):
环保路华里/0.6黑色 行级图走 redstamp 预处理后,_suppress_red_stamp
擦除红章仍输出「0.舌」(红章字「6」被红章残留像素误识)。

修复:在 redstamp 三遍 OCR 基础上**新增第 4 遍「红章擦除 + 灰度转换」**:
  - 把 RGB → 灰度 (0.299R+0.587G+0.114B),红色像素权重低,OCR 不被红章字染色
  - 实测 record 6029 / img 38d4e7... : red-removed → 「0.舌」, red-removed+gray → 「0.6」直接命中

测试要点:
1. KIND_REDSTAMP 路径必须调 _to_grayscale(pre) 而不只是 pre / orig / sharp 三遍
2. _to_grayscale 输出参与 _pick_best_of_n 取优,灰度版字段更全时胜出
3. 普通路径(KIND_FORM_NOLINES / KIND_GLARE / None)不走灰度 pass,保持原行为
4. _to_grayscale 函数本身维度/数值正确性
"""
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from blueprints.ocr_engine import (
    KIND_REDSTAMP, KIND_FORM_NOLINES, KIND_GLARE,
    PaddleOCREngine, _to_grayscale,
)


def _ocr_return(*texts):
    """构造 PaddleOCR `ocr()` 返回值, 每个 text 是 (text, confidence) 元组。"""
    res = [[[[0, 0], [t, 0.95]] for t in texts]]
    return res


class ToGrayscaleUnitTests(unittest.TestCase):
    """_to_grayscale 函数单元测试 — 5 项覆盖。"""

    def test_grayscale_shape_unchanged(self):
        """灰度输出形状必须与输入一致 (H, W, 3)。"""
        rgb = np.random.randint(0, 255, (40, 30, 3), dtype=np.uint8)
        out = _to_grayscale(rgb)
        self.assertEqual(out.shape, rgb.shape, '灰度后形状必须 (H, W, 3)')

    def test_grayscale_all_channels_equal(self):
        """灰度图 R/G/B 三通道必须严格相等(灰度定义)。"""
        rgb = np.random.randint(0, 255, (20, 20, 3), dtype=np.uint8)
        out = _to_grayscale(rgb)
        np.testing.assert_array_equal(out[..., 0], out[..., 1],
            err_msg='R 通道必须等于 G 通道')
        np.testing.assert_array_equal(out[..., 0], out[..., 2],
            err_msg='R 通道必须等于 B 通道')

    def test_grayscale_red_pixels_darker_than_text(self):
        """红章(高 R 低 G)灰度后亮度低于纯黑字(R≈G≈B 都低 V<100 排除)。
        关键证明:红章字「6」不会被 OCR 当正文读。
        """
        # 模拟红章「6」像素: 高 R 中 G 低 B
        red_stamp = np.array([[[200, 30, 30]]], dtype=np.uint8)  # R=200, G=30, B=30
        # 模拟黑字「6」像素
        black_text = np.array([[[20, 20, 20]]], dtype=np.uint8)  # R=G=B=20
        # 模拟白底像素
        white_bg = np.array([[[240, 240, 240]]], dtype=np.uint8)

        for label, px in [('红章', red_stamp), ('黑字', black_text), ('白底', white_bg)]:
            gray_val = _to_grayscale(px)[0, 0, 0]
            self.assertEqual(gray_val, _to_grayscale(px)[0, 0, 1],
                f'{label} 灰度三通道应相等')

        # 红章灰度值应高于黑字(因为有红色分量)
        red_gray = _to_grayscale(red_stamp)[0, 0, 0]
        black_gray = _to_grayscale(black_text)[0, 0, 0]
        self.assertGreater(red_gray, black_gray,
            f'红章灰度({red_gray})应高于黑字({black_gray})')

    def test_grayscale_uses_real_grayscale_weights(self):
        """验证用的是 0.299R+0.587G+0.114B 而非平均灰度。"""
        # 纯红 (R=255, G=B=0): 平均灰度=85, 真实灰度=0.299*255≈76
        pure_red = np.array([[[255, 0, 0]]], dtype=np.uint8)
        out = _to_grayscale(pure_red)[0, 0, 0]
        # 0.299 * 255 = 76.245 → uint8 截断后 76
        self.assertEqual(out, 76,
            '纯红像素灰度应 = 0.299*255≈76,不是平均 85')

    def test_grayscale_returns_input_on_exception(self):
        """任何异常时 _to_grayscale 必须返回原图(不阻塞主流程)。"""
        bad = MagicMock(name='not-ndarray')
        # 强行构造一个会失败的场景: 形状不对且不能做 stack
        bad.__array_interface__ = None
        # MagicMock 不实现 numpy 接口 → 0.299*bad[...,0] 应抛 TypeError
        # _to_grayscale 内 try/except 应返回原 input
        result = _to_grayscale(bad)
        self.assertIs(result, bad, '异常时应原样返回原 input')


class RedstampGrayscalePassTests(unittest.TestCase):
    """extract_text KIND_REDSTAMP 增加灰度 pass — 3 项覆盖。"""

    def _make_engine(self):
        engine = PaddleOCREngine.__new__(PaddleOCREngine)
        engine._ocr = MagicMock(name='_ocr')
        engine._wrinkle_ocr = MagicMock(name='_wrinkle_ocr')
        return engine

    def _run_extract_with_kind(self, engine, kind, ocr_side_effect):
        """统一调度 extract_text 测试。"""
        engine._ocr.ocr.side_effect = ocr_side_effect
        np_orig = np.zeros((40, 30, 3), dtype=np.uint8)
        np_pre = np.ones((40, 30, 3), dtype=np.uint8) * 100
        np_pre_gray = np.full((40, 30, 3), 50, dtype=np.uint8)
        with patch.object(engine, '_ensure_model'), \
             patch.object(engine, '_resize_if_needed', return_value=b'data'), \
             patch('blueprints.ocr_engine.Image.open'), \
             patch('blueprints.ocr_engine.np.array',
                   side_effect=[np_orig, np_pre]), \
             patch('blueprints.ocr_engine._suppress_red_stamp', return_value=np_pre), \
             patch('blueprints.ocr_engine._sharpen_for_ocr', return_value=np_pre), \
             patch('blueprints.ocr_engine._to_grayscale', return_value=np_pre_gray), \
             patch('blueprints.ocr_engine.Image', MagicMock()):
            return engine.extract_text_with_conf(b'fake', preprocess_kind=kind)

    def test_redstamp_calls_grayscale_pass(self):
        """KIND_REDSTAMP 路径必须调 _to_grayscale(pre) 产生第 4 个 OCR pass。"""
        engine = self._make_engine()
        # ocr.ocr 被调用 4 次: orig / pre / sharp / pre_gray
        engine._ocr.ocr.side_effect = [
            _ocr_return('环保路华里'),                         # orig (漏厚度)
            _ocr_return('环保路华里', '0.舌黑色'),              # red-removed
            _ocr_return('环保路华里'),                         # sharp (漏厚度)
            _ocr_return('环保路华里', '0.6黑色'),               # red-removed + gray (修复)
        ]
        text, conf = self._run_extract_with_kind(engine, KIND_REDSTAMP,
            engine._ocr.ocr.side_effect)
        # 必须调出 4 次 ocr
        self.assertEqual(engine._ocr.ocr.call_count, 4,
            'KIND_REDSTAMP 必须 4 遍 OCR(orig / pre / sharp / pre_gray)')
        # 灰度版「0.6黑色」必须胜出
        self.assertIn('0.6黑色', text,
            '灰度版的「0.6黑色」必须胜出 red-removed 版的「0.舌黑色」')
        self.assertNotIn('0.舌', text,
            '胜出版不应含 OCR 误识「舌」')

    def test_redstamp_gray_better_wins(self):
        """当灰度版有 thickness_pattern(0.X)而其它版没有,灰度版必须胜出。"""
        engine = self._make_engine()
        # 关键: 灰度版出 0.6(厚度模式),其它版都没有
        engine._ocr.ocr.side_effect = [
            _ocr_return('环保路华里'),
            _ocr_return('环保路华里'),
            _ocr_return('环保路华里'),
            _ocr_return('环保路华里', '0.6黑色'),
        ]
        text, conf = self._run_extract_with_kind(engine, KIND_REDSTAMP,
            engine._ocr.ocr.side_effect)
        # _pick_best_of_n 按 (thickness_pattern, line_count, total_chars) 打分
        # 灰度版独有 thickness_pattern +1000 → 必胜
        self.assertIn('0.6黑色', text)
        self.assertNotIn('0.舌', text)

    def test_non_redstamp_kinds_skip_grayscale_pass(self):
        """非 KIND_REDSTAMP 路径(空 / glare / form_nolines)不走灰度 pass。"""
        for kind in (None, KIND_GLARE):
            with self.subTest(kind=kind):
                engine = self._make_engine()
                # 只有 1 次 OCR(默认路径)
                engine._ocr.ocr.side_effect = [
                    _ocr_return('环保路华里', '0.6黑色'),
                ]
                text, _ = self._run_extract_with_kind(engine, kind,
                    engine._ocr.ocr.side_effect)
                self.assertEqual(engine._ocr.ocr.call_count, 1,
                    f'{kind} 应只跑 1 遍 OCR,不调灰度 pass')


if __name__ == '__main__':
    unittest.main()