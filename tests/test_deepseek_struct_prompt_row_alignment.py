"""DeepSeekEngine.STRUCT_PROMPT 表行对齐规则防回归测试(2026-09-15)。

背景:订单 1011 (KILA GROUP装柜, doc=TD-2026-09-15-003) 上传同价调拨单
028878151...png,OCR 文字 100% 正确(3 行明细 + 1 汇总行),但 DeepSeek
结构化时把"纯胶"行的品名错配到第二行的"2001"数量 — 导致 3 条新增
明细 (2853/2854/2855) 数量与原 2827/2828/2829 一致但品名+规格串行。

根因:STRUCT_PROMPT 没显式要求 LLM 按"行号"列对齐,DeepSeek 把"行号
1"的纯胶标错到"行号 2"的 3504 数量上。

修复:STRUCT_PROMPT 加「表格行严格按「行号」列对齐」5 条规则。

防回归覆盖:
  1. prompt 必须含「表格行严格按「行号」列对齐」标题段
  2. 必须含「同行的字段才属于同一商品」(防跨行拼接)
  3. 必须含「行号列只读」(防 1/2/3 被当 quantity)
  4. 必须含「数量与品名不能错配」(核心防错配规则)
  5. 必须含「OCR 已按 Y 把同行的文字放一起」(强调 OCR 阶段已对齐)
  6. 必须含「校验兜底 quantity 必须与该行 OCR 数字一致」
  7. 必须含「空白单元格不猜填」(防从其他行借数字)
"""
import unittest

from blueprints.ocr_engine import DeepSeekEngine


class DeepSeekStructPromptRowAlignmentTests(unittest.TestCase):
    """STRUCT_PROMPT 表行对齐规则 — 7 项核心覆盖。"""

    @classmethod
    def setUpClass(cls):
        cls.prompt = DeepSeekEngine.STRUCT_PROMPT

    def test_prompt_contains_row_alignment_title(self):
        """核心:prompt 必须含「表格行严格按「行号」列对齐」标题段。"""
        self.assertIn('表格行严格按「行号」列对齐', self.prompt,
            'STRUCT_PROMPT 缺「表格行严格按「行号」列对齐」段(防品名/规格/数量串行核心段)')

    def test_prompt_says_same_row_only(self):
        """规则 1:同行的字段才属于同一商品(防跨行拼接)。"""
        self.assertIn('同行的字段才属于同一商品', self.prompt,
            '缺规则:同行的字段才属于同一商品')

    def test_prompt_says_row_number_is_not_field(self):
        """规则 2:行号列只读(防 1/2/3 被当 quantity 或 specification)。"""
        self.assertIn('行号列只读', self.prompt,
            '缺规则:行号列只读,不是商品字段')

    def test_prompt_says_qty_not_misaligned_with_pn(self):
        """规则 3(核心):数量与品名不能错配 — 订单 1011 案例的直接根因规则。"""
        self.assertIn('数量与品名不能错配', self.prompt,
            '缺核心规则:数量与品名不能错配')

    def test_prompt_explains_ocr_row_y_aligned(self):
        """规则 3 补充:OCR 已按 Y 把同行的文字放一起,LLM 别跨行借数字。"""
        self.assertIn('OCR 已按 Y 把同一行所有文字放在一起', self.prompt,
            '缺 OCR 行对齐说明(让 LLM 知道 OCR 阶段已经按 Y 聚类)')

    def test_prompt_has_qty_ocr_cross_check(self):
        """规则 4:校验兜底 — quantity 必须与该行 OCR 数字字段一致,不一致立刻修正。"""
        self.assertIn('quantity 必须与该行 OCR 中的数字字段一致', self.prompt,
            '缺校验兜底规则')

    def test_prompt_says_no_borrowing_from_other_rows(self):
        """规则 5:空白单元格不猜填,不从其他行借数字。"""
        self.assertIn('不要从其他行借数字', self.prompt,
            '缺规则:空白单元格不猜填,禁止跨行借数字')


if __name__ == '__main__':
    unittest.main()