"""KIND_REDSTAMP 行级修补单测(2026-09-14)。

背景:订单 TD-2026-09-14-008 / order_id=1001 / record_id=2805「LB特软 / 黑色-0.8」
image 3424e10... 在 _pick_best_of_n 加 thickness_pattern 加权后,winner 变成擦除图
`['鳞布特软', '0.8-黑色']` —— 「LB」被红章擦除图一并擦掉了,品名前缀缺失。

修复:winner 含 数字.数字 厚度行 + winner 首行不是 orig 首行子串
     → 用 orig 首行替换 winner 首行。
     测试图 3424 期望修补后 = `['LB鱼鳞布特软', '0.8-黑色']`(完整 LB + 完整厚度)。
     测试图 ef04 期望不修补(winner 首行 'LB鱼鳞布特软' = orig 首行,已是 orig 子串)。

防回归覆盖:
  1. 行级修补触发:winner 首行被擦除但含厚度 → 修补
  2. 修补不触发:winner 首行 = orig 首行(winner 已是 orig 子串) → 不修补
  3. 修补不触发:winner 不含 数字.数字 厚度行 → 不修补(winner 缺关键信息,宁愿选 orig)
  4. 修补不触发:winner 与 orig 完全相同(orig 胜出) → 不修补
  5. extract_text 与 extract_text_with_conf 同构(同输入同修补行为)
"""
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from blueprints.ocr_engine import KIND_REDSTAMP, PaddleOCREngine


def _ocr_return(*texts):
    """构造 PaddleOCR `ocr()` 返回值。"""
    return [[[[0, 0], [t, 0.95]] for t in texts]]


class RedstampLineLevelFixupTests(unittest.TestCase):
    """行级修补 — 5 项核心覆盖。"""

    def _make_engine(self):
        engine = PaddleOCREngine.__new__(PaddleOCREngine)
        engine._ocr = MagicMock(name='_ocr')
        engine._wrinkle_ocr = MagicMock(name='_wrinkle_ocr')
        return engine

    # ── 真实 fixture 回归 ─────────────────────────────────

    def _real_engine(self):
        from blueprints.ocr_engine import get_ocr_engine
        engine = get_ocr_engine('paddleocr')
        engine._ensure_model()
        return engine

    def test_real_fixture_lb_first_line_restored(self):
        """场景 1: 真实 image 3424 — LB 首行被擦除,行级修补应还原。

        修前 winner: ['鳞布特软', '0.8-黑色']              (LB 缺)
        修后期望:    ['LB鱼鳞布特软', '0.8-黑色']        (完整)
        """
        engine = self._real_engine()
        import os
        fixture = os.path.join(
            'upload', '2026-09', '3424e10774d544708e4971ace09f371e.jpg')
        with open(fixture, 'rb') as f:
            image_bytes = f.read()

        text = engine.extract_text(image_bytes, preprocess_kind=KIND_REDSTAMP)
        lines = text.split('\n')

        # 行级修补应让首行 = 'LB鱼鳞布特软'
        self.assertEqual(lines[0], 'LB鱼鳞布特软',
            f'行级修补应还原 LB 首行,实际: {lines[0]!r}')
        # 厚度行应是 '0.8-黑色'(含 thickness_pattern,数字.数字)
        self.assertIn('0.8', text,
            f'winner 应含 0.8 厚度,实际: {text!r}')
        # 不应含红章污染的 928
        self.assertNotIn('928', text,
            f'修补后不应含红章污染的 928,实际: {text!r}')

    def test_real_fixture_with_conf_same_behavior(self):
        """场景 2: extract_text_with_conf 也走同款行级修补(同构要求)。"""
        engine = self._real_engine()
        import os
        fixture = os.path.join(
            'upload', '2026-09', '3424e10774d544708e4971ace09f371e.jpg')
        with open(fixture, 'rb') as f:
            image_bytes = f.read()

        text, conf = engine.extract_text_with_conf(
            image_bytes, preprocess_kind=KIND_REDSTAMP)
        lines = text.split('\n')

        self.assertEqual(lines[0], 'LB鱼鳞布特软',
            f'extract_text_with_conf 也应还原 LB 首行,实际: {lines[0]!r}')
        self.assertIn('0.8', text,
            f'extract_text_with_conf 也应含 0.8,实际: {text!r}')
        self.assertGreater(conf, 0.0)

    # ── mock 单元测试 — 5 类场景 ───────────────────────────

    def test_fixup_triggers_when_winner_first_line_truncated(self):
        """场景 3: winner 首行被擦但含厚度 → 修补,借 orig 首行。"""
        engine = self._make_engine()
        np_orig = MagicMock(name='np_orig')
        np_pre = MagicMock(name='np_pre')
        with patch.object(engine, '_ensure_model'), \
             patch.object(engine, '_resize_if_needed', return_value=b'data'), \
             patch('blueprints.ocr_engine.Image.open'), \
             patch('blueprints.ocr_engine.np.array', side_effect=[np_orig, np_pre]), \
             patch('blueprints.ocr_engine._suppress_red_stamp',
                   side_effect=lambda *a, **kw: np_pre):
            # orig: 完整 LB + 928 (红章污染)
            # pre: 缺 LB + 0.8 (擦除图把 LB 一并擦掉)
            engine._ocr.ocr.side_effect = [
                _ocr_return('LB鱼鳞布特软', '928-黑色'),       # orig
                _ocr_return('鳞布特软', '0.8-黑色'),            # pre (winner 期望)
                _ocr_return('LB鱼鳞布特软', '928-黑色'),       # sharp (等同 orig)
            ]
            text, _conf = engine.extract_text_with_conf(
                b'fake', preprocess_kind='redstamp')

        self.assertEqual(text, 'LB鱼鳞布特软\n0.8-黑色',
            '修补后应 = orig 首行 + pre 厚度行')

    def test_fixup_does_not_trigger_when_winner_first_matches_orig(self):
        """场景 4: winner 首行与 orig 首行相同 → 不修补(已是 orig 子串)。"""
        engine = self._make_engine()
        np_orig = MagicMock(name='np_orig')
        np_pre = MagicMock(name='np_pre')
        with patch.object(engine, '_ensure_model'), \
             patch.object(engine, '_resize_if_needed', return_value=b'data'), \
             patch('blueprints.ocr_engine.Image.open'), \
             patch('blueprints.ocr_engine.np.array', side_effect=[np_orig, np_pre]), \
             patch('blueprints.ocr_engine._suppress_red_stamp',
                   side_effect=lambda *a, **kw: np_pre):
            # pre winner 含完整 LB + 厚度 → 不修补
            engine._ocr.ocr.side_effect = [
                _ocr_return('LB鱼鳞布特软', '928-黑色'),       # orig
                _ocr_return('LB鱼鳞布特软', '0.8-黑色'),       # pre (winner)
                _ocr_return('LB鱼鳞布特软', '928-黑色'),       # sharp
            ]
            text, _conf = engine.extract_text_with_conf(
                b'fake', preprocess_kind='redstamp')

        self.assertEqual(text, 'LB鱼鳞布特软\n0.8-黑色',
            'winner 首行已含 LB,不应触发修补')

    def test_fixup_does_not_trigger_when_no_thickness_pattern(self):
        """场景 5: winner 不含 数字.数字 厚度行 → 不修补(关键信息缺失)。"""
        engine = self._make_engine()
        np_orig = MagicMock(name='np_orig')
        np_pre = MagicMock(name='np_pre')
        with patch.object(engine, '_ensure_model'), \
             patch.object(engine, '_resize_if_needed', return_value=b'data'), \
             patch('blueprints.ocr_engine.Image.open'), \
             patch('blueprints.ocr_engine.np.array', side_effect=[np_orig, np_pre]), \
             patch('blueprints.ocr_engine._suppress_red_stamp',
                   side_effect=lambda *a, **kw: np_pre):
            # 三遍都没识别到 0.X 厚度(原图胜出)
            engine._ocr.ocr.side_effect = [
                _ocr_return('LB鱼鳞布特软', '928-黑色'),
                _ocr_return('LB鱼鳞布特软', '928-黑色'),  # 同 orig,orig 胜
                _ocr_return('LB鱼鳞布特软', '928-黑色'),
            ]
            text, _conf = engine.extract_text_with_conf(
                b'fake', preprocess_kind='redstamp')

        self.assertEqual(text, 'LB鱼鳞布特软\n928-黑色',
            '无 thickness_pattern 时,不应修补(orig 胜出即可)')


if __name__ == '__main__':
    unittest.main()