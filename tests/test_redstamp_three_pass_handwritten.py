"""KIND_REDSTAMP 三遍 OCR 取优单测(印刷体+手写体混排场景)。

背景(2026-09-02 订单 TD-2026-09-02-005):
订单 911(司机)明细 2510「环保杂胶 / 1.4白中加面 / 500y」行级图
(image 3805, file 50e06c26...)标签是「印刷体 + 手写马克笔数字」混排 ——
工人手填厚度「1.4」。PaddleOCR 训练集以印刷体为主,对训练集外的手写数字
识别率先天弱。

实测(image 3805, 4 路径):
  原图         → 「小色」   (0 数字)
  红章擦除    → 「规格行空」(字段丢失)
  锐化 r2p200 → 「小4色」   (抓到「4」)
  锐化 r4p300 → 「AZ1色」   (抓到「1」,非确定)

修复:KIND_REDSTAMP 路径从 2-pass 升到 3-pass = 原图 + 红章擦除 + 锐化版
(详见 _pick_best_of_n 评分函数:line_count / total_chars / prefix_bonus)。

预期上限:PaddleOCR 对手写数字先天弱,3-pass 取优能保证至少 1 个数字被识别,
但不一定能同时抓到「1」和「4」。完整解决需 Moonshot Kimi vision 接入
(结构性改动,本次不动)。

测试覆盖:
  1. 真实 fixture 跑 extract_text 至少抓到 1 个数字 (核心场景)
  2. 真实 fixture 跑 extract_text_with_conf 也命中 3-pass
  3. mock 测试 _pick_best_of_n 的打分校准
  4. mock 测试 3-pass 调用:同分时原图胜(更保守)
  5. 非 KIND_REDSTAMP 路径不调 3-pass(避免性能回归)
"""
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from blueprints.ocr_engine import (
    KIND_REDSTAMP, PaddleOCREngine,
)


def _ocr_return(*texts):
    """构造 PaddleOCR `ocr()` 返回值。"""
    return [[[[0, 0], [t, 0.95]] for t in texts]]


class RedstampThreePassHandwrittenTests(unittest.TestCase):
    """KIND_REDSTAMP 三遍 OCR — 5 项核心覆盖。"""

    def _make_engine(self):
        engine = PaddleOCREngine.__new__(PaddleOCREngine)
        engine._ocr = MagicMock(name='_ocr')
        engine._wrinkle_ocr = MagicMock(name='_wrinkle_ocr')
        return engine

    # ── 真实 fixture 回归 ─────────────────────────────────

    def _real_engine(self):
        """真实 PaddleOCR 引擎(用于 fixture 回归测试)。

        与 _make_engine 的区别:不 mock PaddleOCR 实例,直接用真实模型。
        跑得慢(2-3 秒),只用于 fixture 类回归。
        """
        from blueprints.ocr_engine import get_ocr_engine
        engine = get_ocr_engine('paddleocr')
        engine._ensure_model()
        return engine

    def test_real_fixture_captures_at_least_one_digit(self):
        """场景 1: 真实 image 3805 跑修复后路径至少抓 1 个数字。

        修复前:`小色` (0 数字)
        修复后:`小4色` 或类似(至少含 1 个数字,非确定哪个)
        """
        engine = self._real_engine()

        import os
        fixture = os.path.join(
            'tests', 'fixtures',
            'eco_zajiao_printed_handwritten',
            '50e06c26_2510_1.4_white_handprinted.jpg')
        self.assertTrue(os.path.exists(fixture),
            f'fixture 不存在: {fixture} (需先复制原图)')
        with open(fixture, 'rb') as f:
            image_bytes = f.read()

        text = engine.extract_text(image_bytes, preprocess_kind=KIND_REDSTAMP)

        import re
        digits = re.findall(r'\d+', text)
        self.assertGreaterEqual(len(digits), 1,
            f'3-pass OCR 取优应至少抓到 1 个数字,实际结果:\n{text!r}\n数字: {digits}')

        # 品名/手感行应保留(印刷体字段不被锐化破坏)
        self.assertIn('环保加面杂胶', text,
            f'「环保加面杂胶」应保留(印刷体不被锐化破坏),实际:\n{text!r}')
        self.assertIn('中性', text,
            f'「中性」手感应保留,实际:\n{text!r}')

    def test_real_fixture_extract_text_with_conf_same_behavior(self):
        """场景 2: extract_text_with_conf 也走 3-pass(必须与 extract_text 严格同构)。

        /re-ocr 端点走 extract_text_with_conf — 首次修复漏掉过同构问题。
        """
        engine = self._real_engine()

        import os
        fixture = os.path.join(
            'tests', 'fixtures',
            'eco_zajiao_printed_handwritten',
            '50e06c26_2510_1.4_white_handprinted.jpg')
        with open(fixture, 'rb') as f:
            image_bytes = f.read()

        text, conf = engine.extract_text_with_conf(
            image_bytes, preprocess_kind=KIND_REDSTAMP)

        import re
        digits = re.findall(r'\d+', text)
        self.assertGreaterEqual(len(digits), 1,
            f'extract_text_with_conf 也应至少抓 1 个数字,实际:\n{text!r}')
        self.assertGreater(conf, 0.0)
        self.assertLessEqual(conf, 1.0)

    # ── mock 测试 _pick_best_of_n 打分逻辑 ──────────────────

    def test_pick_best_of_n_picks_highest_score(self):
        """场景 3: _pick_best_of_n 按 (n_lines, total_chars, prefix_bonus) 取最高分。"""
        engine = self._make_engine()

        # 三 pass:pass2 字符最多 → 胜出
        pass1 = (['品名:', '环保加面杂胶'], [0.95, 0.95])  # 2 lines, 8 chars
        pass2 = (['品名:', '环保加面杂胶', '规格:', '小4色'], [0.95, 0.95, 0.95, 0.95])  # 4 lines, 13 chars
        pass3 = (['品名:', '环保加面杂胶', '规格:'], [0.95, 0.95, 0.95])  # 3 lines, 10 chars

        winner_lines, winner_confs = engine._pick_best_of_n([pass1, pass2, pass3])
        self.assertEqual(winner_lines, pass2[0],
            '字符最多的 pass 应胜出')

    def test_pick_best_of_n_orig_wins_tie(self):
        """场景 4: 同分时原图胜(更保守,避免噪声引入)。"""
        engine = self._make_engine()

        # 三 pass 同分 → index 0(原图)胜
        same = (['品名:', '环保杂胶'], [0.95, 0.95])
        winner_lines, _ = engine._pick_best_of_n([same, same, same])
        self.assertEqual(winner_lines, same[0])

    # ── 防御反向 ────────────────────────────────────────────

    def test_non_redstamp_path_does_not_call_three_pass(self):
        """场景 5: 非 KIND_REDSTAMP 路径不调第三遍 OCR (避免性能回归)。

        验证 KIND_GLARE / KIND_FORM_NOLINES / None 都不调用 _sharpen_for_ocr。
        """
        engine = self._make_engine()

        for kind in (None, 'glare', 'form_nolines'):
            engine._ocr.ocr.reset_mock()
            engine._wrinkle_ocr.ocr.reset_mock()
            engine._ocr.ocr.return_value = _ocr_return('hello')
            engine._wrinkle_ocr.ocr.return_value = _ocr_return('hello')
            np_mock = MagicMock(name='np_array', spec=np.ndarray)

            with patch.object(engine, '_ensure_model'), \
                 patch.object(engine, '_resize_if_needed', return_value=b'data'), \
                 patch('blueprints.ocr_engine.Image.open'), \
                 patch('blueprints.ocr_engine.np.array', return_value=np_mock), \
                 patch('blueprints.ocr_engine._sharpen_for_ocr') as mock_sharpen, \
                 patch('blueprints.ocr_engine._sharpen_strong_for_ocr'), \
                 patch('blueprints.ocr_engine._suppress_glare',
                       side_effect=lambda *a, **kw: np_mock), \
                 patch('blueprints.ocr_engine._suppress_red_stamp',
                       side_effect=lambda *a, **kw: np_mock), \
                 patch('blueprints.ocr_engine._detect_bg_color_from_np',
                       return_value='black'):
                engine.extract_text(b'fake', preprocess_kind=kind)

            self.assertEqual(mock_sharpen.call_count, 0,
                f'preprocess_kind={kind!r} 不应调 _sharpen_for_ocr, '
                f'实际 {mock_sharpen.call_count} 次')


if __name__ == '__main__':
    unittest.main()