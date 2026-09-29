"""PaddleOCREngine form_nolines 路径 glare 抑制兜底单测。

背景(2026-09-02 订单 TD-2026-09-02-002 / 阿海):
明细 2502「黑磅布三文治 / 1.0中性 / 730y」行级图(image 3788,
file f888b1ab77744922bd483e28b7a8c44d.jpg)实际标注「厚度:1.0mm」,
PaddleOCR 走 form_nolines 路径时却漏第 2 行(厚度行),只识出品名/手感/底布 3 行,
AI 比对误判「厚度缺失」yellow。

实测:
  - 原图 form_nolines band OCR  → [品名, (空), 手感, 底布]         ← 漏厚度
  - glare 抑制后 form_nolines    → [(空), 厚度:1.0mm, 手感, 底布] ← 漏品名
  - 合并后                      → [品名, 厚度:1.0mm, 手感, 底布]    ← 完整

修复方向:extract_text / extract_text_with_conf 在 KIND_FORM_NOLINES 分支里
对原图 + glare 抑制图各跑一遍 _extract_form_lines,再按 _merge_form_candidates
逐行合并择优(空行让另一边的非空行补上)。

覆盖:
  - 原图漏字段(厚度)→ glare 图补回 (核心场景,本案)
  - glare 图漏字段(品名)→ 原图保留 (防御反向回归)
  - 非 form_nolines 路径不触发 glare 兜底 (避免性能回归)
"""
import unittest
from unittest.mock import MagicMock, patch

from blueprints.ocr_engine import PaddleOCREngine


class FormNolinesGlareFallbackTests(unittest.TestCase):
    """form_nolines 路径 glare 兜底 — 3 项核心覆盖。"""

    def _make_engine(self):
        engine = PaddleOCREngine.__new__(PaddleOCREngine)
        engine._ocr = MagicMock(name='_ocr')
        engine._wrinkle_ocr = MagicMock(name='_wrinkle_ocr')
        return engine

    # ── 核心场景 ─────────────────────────────────────────

    def test_orig_missing_thickness_glare_fills_it(self):
        """场景 1: 原图漏「厚度」(本案),glare 图应补回。

        _extract_form_lines 返回值约定: (lines: list[str], confs: list[float])
        - 第一次调(原图)→ [品名, (空), 手感, 底布]
        - 第二次调(glare) → [(空), 厚度, 手感, 底布]

        期望 extract_text 返回 ['品名', '厚度:1.0mm', '手感', '底布']。
        """
        engine = self._make_engine()
        np_orig = MagicMock(name='np_orig')
        np_glare = MagicMock(name='np_glare')

        # _extract_form_lines 是被调两次的关键函数 (原图 + glare)
        engine._extract_form_lines = MagicMock(side_effect=[
            (['品名:磅布三文治', '', '手感:中性', '底布:磅布'],
             [0.99, 0.88, 0.97, 0.77]),  # 原图:漏第 2 行(厚度)
            (['', '厚度:1.0mm', '手感:中性', '底布:磅布'],
             [0.99, 0.99, 0.99, 0.99]),  # glare:补回第 2 行,但丢第 1 行
        ])

        with patch.object(engine, '_ensure_model'), \
             patch.object(engine, '_resize_if_needed', return_value=b'data'), \
             patch('blueprints.ocr_engine.Image.open'), \
             patch('blueprints.ocr_engine.np.array', return_value=np_orig), \
             patch('blueprints.ocr_engine._suppress_glare',
                   side_effect=lambda *a, **kw: np_glare), \
             patch('blueprints.ocr_engine._detect_bg_color_from_np',
                   return_value='black'):
            text = engine.extract_text(b'fake', preprocess_kind='form_nolines')

        lines = [l for l in text.split('\n') if l.strip()]
        joined = '|'.join(lines)

        # 厚度字段必须出现
        self.assertTrue(
            any('厚度' in l and '1.0' in l for l in lines),
            f'glare 兜底应补回「厚度:1.0mm」,实际结果:\n{text!r}')

        # 品名字段必须保留
        self.assertTrue(
            any('品名' in l and '磅布三文治' in l for l in lines),
            f'原图「品名:磅布三文治」应保留,实际结果:\n{text!r}')

        # 总行数=4 (空行被过滤)
        self.assertEqual(len(lines), 4,
            f'应输出 4 行非空字段,实际 {len(lines)} 行: {joined}')

    # ── 防御反向 ─────────────────────────────────────────

    def test_orig_complete_glare_worse_keeps_orig(self):
        """场景 2: 原图完整,glare 图反而漏字段 → 保留原图(更保守)。

        - 原图 → [品名, 厚度, 手感, 底布]
        - glare → [品名, (空), 手感, 底布]

        期望结果里仍有「厚度:1.0mm」。
        """
        engine = self._make_engine()
        np_orig = MagicMock(name='np_orig')
        np_glare = MagicMock(name='np_glare')

        engine._extract_form_lines = MagicMock(side_effect=[
            (['品名:磅布三文治', '厚度:1.0mm', '手感:中性', '底布:磅布'],
             [0.99, 0.99, 0.99, 0.99]),
            (['品名:磅布三文治', '', '手感:中性', '底布:磅布'],
             [0.99, 0.88, 0.97, 0.77]),
        ])

        with patch.object(engine, '_ensure_model'), \
             patch.object(engine, '_resize_if_needed', return_value=b'data'), \
             patch('blueprints.ocr_engine.Image.open'), \
             patch('blueprints.ocr_engine.np.array', return_value=np_orig), \
             patch('blueprints.ocr_engine._suppress_glare',
                   side_effect=lambda *a, **kw: np_glare), \
             patch('blueprints.ocr_engine._detect_bg_color_from_np',
                   return_value='black'):
            text = engine.extract_text(b'fake', preprocess_kind='form_nolines')

        self.assertIn('厚度', text,
            f'原图完整 → glare 漏字段也应保留「厚度」,实际:\n{text!r}')
        self.assertIn('1.0', text,
            f'原图完整 → glare 漏字段也应保留「1.0」,实际:\n{text!r}')

    # ── glare 异常路径不破坏主流程 ──────────────────────

    def test_glare_suppress_failure_falls_back_to_orig(self):
        """场景 3: _suppress_glare 抛异常时,主流程仍返回原图结果。"""
        engine = self._make_engine()
        np_orig = MagicMock(name='np_orig')

        engine._extract_form_lines = MagicMock(return_value=(
            ['品名:磅布三文治', '厚度:1.0mm', '手感:中性', '底布:磅布'],
            [0.99, 0.99, 0.99, 0.99],
        ))

        with patch.object(engine, '_ensure_model'), \
             patch.object(engine, '_resize_if_needed', return_value=b'data'), \
             patch('blueprints.ocr_engine.Image.open'), \
             patch('blueprints.ocr_engine.np.array', return_value=np_orig), \
             patch('blueprints.ocr_engine._suppress_glare',
                   side_effect=RuntimeError('glare fail')), \
             patch('blueprints.ocr_engine._detect_bg_color_from_np',
                   return_value='black'):
            text = engine.extract_text(b'fake', preprocess_kind='form_nolines')

        # 原图结果应保留 (不被 glare 异常吃掉)
        self.assertIn('厚度', text)
        self.assertIn('1.0', text)

    # ── 非 form_nolines 路径不触发 ────────────────────────

    def test_non_form_nolines_path_does_not_call_suppress_glare(self):
        """场景 4: 仅 form_nolines 路径触发 glare 兜底,其他路径不重复调。

        KIND_GLARE 本身就要调 _suppress_glare(那是它的设计目的),不在本测试范围。
        KIND_REDSTAMP 用 _suppress_red_stamp 不调 glare;None 路径不预处理。
        """
        engine = self._make_engine()
        for kind in (None, 'redstamp'):
            engine._ocr.ocr.reset_mock()
            engine._ocr.ocr.return_value = [[[[0, 0], ['hello', 0.95]]]]

            with patch.object(engine, '_ensure_model'), \
                 patch.object(engine, '_resize_if_needed', return_value=b'data'), \
                 patch('blueprints.ocr_engine.Image.open'), \
                 patch('blueprints.ocr_engine.np.array',
                       return_value=MagicMock(name='np_array')), \
                 patch('blueprints.ocr_engine._suppress_glare') as mock_glare, \
                 patch('blueprints.ocr_engine._suppress_red_stamp',
                       side_effect=lambda *a, **kw: MagicMock(name='np_red')):
                engine.extract_text(b'fake', preprocess_kind=kind)

            self.assertEqual(mock_glare.call_count, 0,
                f'preprocess_kind={kind!r} 不应调 _suppress_glare 兜底, '
                f'实际 {mock_glare.call_count} 次')

    def test_form_nolines_path_calls_suppress_glare_once(self):
        """场景 5: form_nolines 路径调 _suppress_glare 恰好 1 次(兜底)。

        确保兜底逻辑真实生效,而不是被 silent skip 掉。
        """
        engine = self._make_engine()
        np_orig = MagicMock(name='np_orig')
        np_glare = MagicMock(name='np_glare')

        engine._extract_form_lines = MagicMock(return_value=(
            ['品名:磅布三文治', '厚度:1.0mm', '手感:中性', '底布:磅布'],
            [0.99, 0.99, 0.99, 0.99],
        ))

        with patch.object(engine, '_ensure_model'), \
             patch.object(engine, '_resize_if_needed', return_value=b'data'), \
             patch('blueprints.ocr_engine.Image.open'), \
             patch('blueprints.ocr_engine.np.array', return_value=np_orig), \
             patch('blueprints.ocr_engine._suppress_glare') as mock_glare, \
             patch('blueprints.ocr_engine._detect_bg_color_from_np',
                   return_value='black'):
            mock_glare.return_value = np_glare  # glare 返回新 numpy
            engine.extract_text(b'fake', preprocess_kind='form_nolines')

        # 兜底路径必须真的调用 _suppress_glare,且只调 1 次
        self.assertEqual(mock_glare.call_count, 1,
            f'form_nolines glare 兜底必须调 1 次,实际 {mock_glare.call_count} 次')

    # ── extract_text_with_conf 路径同构覆盖 ─────────────────

    def test_extract_text_with_conf_also_has_glare_fallback(self):
        """场景 6: extract_text_with_conf 也必须有 glare 兜底。

        背景:/re-ocr 端点(出货页 ↻重 OCR 按钮)走的是 extract_text_with_conf,
        而 extract_text 是整单路径。如果只修 extract_text,/re-ocr 按钮仍然漏字段
        (用户反馈 TD-2026-09-02-002 点重 OCR 后还是没厚度)。
        """
        engine = self._make_engine()
        np_orig = MagicMock(name='np_orig')
        np_glare = MagicMock(name='np_glare')

        engine._extract_form_lines = MagicMock(side_effect=[
            (['品名:磅布三文治', '', '手感:中性', '底布:磅布'],
             [0.99, 0.88, 0.97, 0.77]),  # 原图漏第 2 行
            (['', '厚度:1.0mm', '手感:中性', '底布:磅布'],
             [0.99, 0.99, 0.99, 0.99]),  # glare 补回
        ])

        with patch.object(engine, '_ensure_model'), \
             patch.object(engine, '_resize_if_needed', return_value=b'data'), \
             patch('blueprints.ocr_engine.Image.open'), \
             patch('blueprints.ocr_engine.np.array', return_value=np_orig), \
             patch('blueprints.ocr_engine._suppress_glare',
                   side_effect=lambda *a, **kw: np_glare), \
             patch('blueprints.ocr_engine._detect_bg_color_from_np',
                   return_value='black'):
            text, conf = engine.extract_text_with_conf(
                b'fake', preprocess_kind='form_nolines')

        # extract_text_with_conf 也必须能合并出厚度行
        self.assertIn('厚度', text,
            f'extract_text_with_conf glare 兜底也应补回「厚度」,实际:\n{text!r}')
        self.assertIn('1.0', text,
            f'extract_text_with_conf glare 兜底也应保留「1.0」,实际:\n{text!r}')

        # 平均置信度应被计算
        self.assertGreater(conf, 0.0)
        self.assertLessEqual(conf, 1.0)


if __name__ == '__main__':
    unittest.main()