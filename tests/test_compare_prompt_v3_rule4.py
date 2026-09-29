"""COMPARE_PROMPT 规则 4 收严：加面嵌在品名词组内时,规格无加面应判冲突。

回归背景(2026-08-28):
  订单 TD-2026-08-28-004(陈钱罐)rec_id=2441「环保杂胶 / 0.6白中」
  配图 image_id=3630 OCR 原文 = "品名：\n18P加面杂胶中硬\n规格：\n0.6白色"
  旧 Rule 4 末条「规格里无加面/单面字样 + OCR 文字里有'加面' → 视为多余信息」
  导致 DeepSeek 直接判 green (AI reason 完全未提"加面"二字)。

修复要点:
  - 拆分末条为两条:
      * 默认情形(加面作为单独标注)→ 维持「视为多余信息,不判错」
      * 例外情形(加面嵌入品名词组,如 "18P加面杂胶") → 视为产品类型标识,
        规格必须含"加面"才算对得上,否则关键冲突 red
  - 版本号 bump 至 compare_rows_v3
  - 保留历史 compare_rows_v1/v2 字面量作为 audit 页 baseline 锚点
"""
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_FILE = REPO_ROOT / 'blueprints' / 'ocr_engine.py'


class ComparePromptRule4Tests(unittest.TestCase):
    """源码层断言:不依赖 DeepSeek API,直接 grep 整个源文件。"""

    @classmethod
    def setUpClass(cls):
        cls.src = SOURCE_FILE.read_text(encoding='utf-8')
        # 截出 COMPARE_PROMPT 整段(从 'COMPARE_PROMPT = (' 到下一个方法定义前)
        m = re.search(r"COMPARE_PROMPT\s*=\s*\(([\s\S]*?)\n\s*def compare_rows", cls.src)
        if not m:
            raise RuntimeError('找不到 COMPARE_PROMPT 定义/方法边界')
        cls.prompt_block = m.group(1)

    def test_rule4_default_remains_extra_info(self):
        """默认情形仍走「视为多余信息」路径(向后兼容)。"""
        self.assertIn('视为多余信息', self.prompt_block,
            '默认情形(加面作为标注)应保留「视为多余信息,不判错」')

    def test_rule4_exception_product_name_phrase(self):
        """新增例外条款:加面嵌在品名词组时,规格无加面 → 关键冲突 red。

        例子 OCR 文本里"18P加面杂胶" / "7P加面纯胶" / "加面三文治" 这种
        "X加面Y" 词组意味着实物就是加面型,规格必须含「加面」字样才算对得上。
        """
        # 至少包含一个"X加面Y"示例(加面嵌在品名词组)
        phrase_clue_found = bool(
            re.search(r'加面(?:杂胶|纯胶|三文治|猪皮纹)', self.prompt_block)
        )
        self.assertTrue(phrase_clue_found,
            '例外条款应至少给一个「加面嵌在品名词组」的具体示例(如 "加面杂胶")')

        # 必须明确"规格无加面"在例外情形下应判冲突(red)
        # prompt_block 是源码 raw 文本(含 \n 转义和 \' 续行),从「例外」抓
        # 到下一条「▌规则 5」(或 prompt_block 末尾)做局部断言
        # 注：「例外」后可能跟「：」(中文冒号)或「（」(中文全角左括号 U+FF08),两种都接受
        exc_match = re.search(
            r'例外[：（]([\s\S]*?)(?=▌规则\s*5|\Z)', self.prompt_block)
        self.assertIsNotNone(exc_match,
            '规则 4 应有一段以「例外」开头的子条款,在 ▌规则 5 之前结束')
        exc_block = exc_match.group(1)
        for must in ('加面', '规格'):
            self.assertIn(must, exc_block,
                f'例外条款必须同时提及 "{must}"')
        # "关键冲突"或"red"至少有其一(允许不同措辞)
        self.assertTrue(
            ('冲突' in exc_block) or ('red' in exc_block.lower()) or ('红' in exc_block),
            f'例外条款必须明示判定为冲突/red,实际: {exc_block!r}')

    def test_rule4_symmetric_single_side_clause(self):
        """对称条款:单面嵌在品名词组时,规格含"加面"应判冲突 red(2026-08-28)。

        业务原理:单面是商品默认形态,OCR 标签若明确是单面型
        (如 "18P单面杂胶中软"),规格里**不应**出现加面 —— 二者互斥。

        防回归:删掉此条款时 LLM 容易漏看上方已有的"规格里有加面 + OCR 文字里有单面 → 加面不匹配 ✗"规则,
        通过显式重申 + 单测锁住。
        """
        sym_match = re.search(
            r'对称条款[\s\S]*?(?=▌|\Z)', self.prompt_block)
        self.assertIsNotNone(sym_match,
            'Rule 4 必须包含「对称条款」子段,显式说明单面↔非加面的互斥关系')
        sym_block = sym_match.group()
        for must in ('单面', '加面'):
            self.assertIn(must, sym_block,
                f'对称条款必须同时提及 "{must}"')
        # 必须明确判定为冲突(red)
        self.assertTrue(
            ('冲突' in sym_block) or ('red' in sym_block.lower()) or ('红' in sym_block),
            f'对称条款必须明示规格含加面时判冲突/red,实际: {sym_block!r}')

    def test_rule4_no_legacy_lenient_clause(self):
        """旧末条不能继续作为唯一兜底 —— 必须新增例外条款。

        防回归:有人后续误删例外条款时这个测试应失败。
        """
        # 实现方式:检查 prompt_block 中是否有显式提到 "例外"/"嵌入"/"词组"
        has_exception_clause = (
            '例外' in self.prompt_block
            or ('品名词组' in self.prompt_block)
            or ('加面杂胶' in self.prompt_block)
        )
        self.assertTrue(has_exception_clause,
            'Rule 4 必须新增例外条款区分"加面"在品名词组内 vs 单独标注两种情形')

    def test_version_bumped_to_compare_rows_v3(self):
        """OCR_MATCH_PROMPT_VERSION 应 bump 到 compare_rows_v3,
        让审计页能按版本号分流比对结果。"""
        self.assertIn("OCR_MATCH_PROMPT_VERSION = 'compare_rows_v3'", self.src,
            'OCR_MATCH_PROMPT_VERSION 应已 bump 到 compare_rows_v3')

    def test_baseline_history_preserved(self):
        """防回归:历史测试中 compare_rows_v1/v2 字面量必须保留作为 audit baseline。"""
        for hist_v in ('compare_rows_v1', 'compare_rows_v2'):
            with self.subTest(version=hist_v):
                found = False
                for test_path in (REPO_ROOT / 'tests').glob('test_*.py'):
                    if hist_v in test_path.read_text(encoding='utf-8'):
                        found = True
                        break
                self.assertTrue(found,
                    f'历史版本 {hist_v} 字面量必须在某个 test_*.py 中保留(baseline 锚点)')


if __name__ == '__main__':
    unittest.main()