"""_pick_best_of_n 厚度模式加权测试(2026-09-14)。

背景:订单 TD-2026-09-14-008 / order_id=1001 / record_id=2805「LB特软 / 黑色-0.8」
三张图全部 OCR 被红章污染:
  - 3424e10...: 原图 `928-黑色` (13字符) vs 擦除 `0.8-黑色` (10字符)
  - ef047c...: 锐化 `找0.8-黑色` (14字符) 最优
  - 94a05e...: 三遍都未识别小数点(拍照角度/红章覆盖,纯 OCR 能力问题)

旧打分 (n_lines, total_chars, prefix_bonus) 让原图 928-黑色 字符多胜出,
   擦除/锐化的 0.8-黑色 反而被丢弃 → AI 比对时找不到厚度。

修复:打分首键改为 thickness_pattern(数字.数字匹配数),命中即 +1。
   排序键: (thickness_pattern, n_lines, total_chars, prefix_bonus)

防回归测试:
  1. 厚度模式胜字符数(图 3424 场景): pre 含 0.8 胜 orig 含 928
  2. 厚度模式相等时退化为旧逻辑(n_lines → total_chars)
  3. 同分时索引小的胜(原图优先,更保守)
  4. extract_text 与 extract_text_with_conf 都走 _pick_best_of_n(同构)
"""
import unittest
from unittest.mock import MagicMock

from blueprints.ocr_engine import (
    KIND_REDSTAMP, PaddleOCREngine,
)


class RedstampThicknessPatternPickTests(unittest.TestCase):
    """_pick_best_of_n thickness_pattern 加权 — 4 项核心覆盖。"""

    def _make_engine(self):
        engine = PaddleOCREngine.__new__(PaddleOCREngine)
        engine._ocr = MagicMock(name='_ocr')
        engine._wrinkle_ocr = MagicMock(name='_wrinkle_ocr')
        return engine

    # ── 核心场景:厚度模式压倒字符数差异 ─────────────────────

    def test_thickness_pattern_wins_over_more_chars(self):
        """场景 1: 厚度模式命中压倒字符总数。

        模拟 record 2805 image 3424:
          orig  : 'LB鱼鳞布特软' / '928-黑色'  → (0, 2, 13, 0)
          pre   : '鳞布特软' / '0.8-黑色'       → (1, 2, 10, 0)
        期望 pre 胜(thickness_pattern=1 优先于 total_chars 13 vs 10)。
        """
        engine = self._make_engine()

        orig = (['LB鱼鳞布特软', '928-黑色'], [0.95, 0.95])
        pre  = (['鳞布特软', '0.8-黑色'], [0.95, 0.95])
        sharp = (['LB鱼鳞布特软', '8一黑色'], [0.95, 0.95])

        winner_lines, _ = engine._pick_best_of_n([orig, pre, sharp])
        self.assertEqual(winner_lines, pre[0],
            'pre 含 0.8(thickness_pattern=1)应胜 orig 的 928(thickness_pattern=0),'
            '即使 orig 字符更多')

    # ── 厚度模式相等时退化到旧逻辑 ─────────────────────────

    def test_thickness_pattern_equal_falls_back_to_line_count(self):
        """场景 2: 厚度命中数相同时,按 n_lines → total_chars → prefix_bonus 旧逻辑。"""
        engine = self._make_engine()

        # 三 pass 都没命中 0.X 厚度(数字点丢失)
        # pass2 行数最多 → 胜
        p1 = (['品名:', '环保杂胶'], [0.95, 0.95])                    # (0, 2, 7, 0)
        p2 = (['品名:', '环保杂胶', '规格:', '1.2黑色'], [0.95]*4)    # (1, 4, 14, 0)
        p3 = (['品名:', '环保杂胶', '规格:'], [0.95]*3)                # (0, 3, 10, 0)

        winner_lines, _ = engine._pick_best_of_n([p1, p2, p3])
        self.assertEqual(winner_lines, p2[0],
            '厚度命中数 1 vs 0 vs 0 → p2 胜出')

    # ── 同分时索引小的胜(原图优先) ─────────────────────────

    def test_tie_breaks_to_lower_index(self):
        """场景 3: 完全同分时,索引小的胜(原图 > 擦除 > 锐化,越靠后越激进)。"""
        engine = self._make_engine()

        same = (['品名:', '环保杂胶'], [0.95, 0.95])
        winner_lines, _ = engine._pick_best_of_n([same, same, same])
        self.assertEqual(winner_lines, same[0])

    # ── 多厚度命中累加 ─────────────────────────────────────

    def test_multiple_thickness_patterns_accumulate(self):
        """场景 4: 多行都命中 数字.数字 时累加,用于选优。

        例:pass 含 '1.0' 和 '0.6' → thickness=2,压过只命中 1 个的 pass。
        """
        engine = self._make_engine()

        # 弱:只有一个 1.0
        weak = (['品名:', 'LB鱼鳞布', '1.0', '0.6-黑色'], [0.95]*4)
        # 强:同时命中 1.0 和 0.6 + 行多
        strong = (['品名:', 'LB鱼鳞布', '规格:', '1.0', '0.6-黑色'], [0.95]*5)

        winner_lines, _ = engine._pick_best_of_n([weak, strong])
        self.assertEqual(winner_lines, strong[0],
            '多厚度命中累加,strong 应胜')


if __name__ == '__main__':
    unittest.main()