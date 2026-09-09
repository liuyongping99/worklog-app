"""COMPARE_PROMPT 规则 1 拓宽覆盖所有品类的回归测试。

2026-07-30 修复：原规则 1 把"环保等价词(7P/15P/18P/21P)"只限定杂胶,
实际生产数据里 7P 还覆盖 纯胶/三文治/磅布三文治/HA猪皮纹/LB鱼鳞布/路华里,
prompt 必须拓宽为"任何品名含'环保'的商品"。

测试要点:
- COMPARE_PROMPT 文本不再含"对于品名中含\"杂胶\"的商品" 这一限定
- 仍要求 7P/15P/18P/21P 等价词列表
- 版本号 bump 至 compare_rows_v2
"""
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_FILE = REPO_ROOT / 'blueprints' / 'ocr_engine.py'


class ComparePromptRule1Tests(unittest.TestCase):
    """源码层断言:不依赖 DeepSeek API,直接 grep 整个源文件(中文双引号不会被 regex 解析吃掉)。"""

    @classmethod
    def setUpClass(cls):
        cls.src = SOURCE_FILE.read_text(encoding='utf-8')
        # 截出 COMPARE_PROMPT 的整段定义范围(从 'COMPARE_PROMPT = (' 到下一个方法定义前)
        m = re.search(r"COMPARE_PROMPT\s*=\s*\(([\s\S]*?)\n\s*def compare_rows", cls.src)
        if not m:
            raise RuntimeError('找不到 COMPARE_PROMPT 定义/方法边界')
        cls.prompt_block = m.group(1)

    def test_rule1_no_longer_limits_to_misc_rubber_only(self):
        """规则 1 不应再写「对于品名中含"杂胶"的商品」这种限定。"""
        # 注意源文件里是中文双引号 "..."
        self.assertNotIn('对于品名中含"杂胶"的商品', self.prompt_block,
            '规则 1 已拓宽为所有含"环保"的商品,不再限定杂胶')
        # 反向断言:新文本必须包含「对于品名中含"环保"字样的商品」
        self.assertIn('对于品名中含"环保"字样的商品', self.prompt_block)

    def test_rule1_lists_eco_equivalence_words(self):
        """保留 7P / 15P / 18P / 21P 等价词。"""
        for kw in ('7P', '15P', '18P', '21P'):
            with self.subTest(kw=kw):
                self.assertIn(kw, self.prompt_block,
                    f'规则 1 仍需列环保等价词 {kw}')

    def test_rule1_examples_non_misc_rubber_categories(self):
        """规则 1 正文应枚举至少 2 个非杂胶品类(纯胶/三文治/磅布三文治/HA猪皮纹/LB鱼鳞布/路华里),
        让 LLM 知道哪些品类也算"环保"范围。"""
        non_misc_examples = ('纯胶', '三文治', 'HA猪皮纹', 'LB鱼鳞布', '路华里', '磅布三文治')
        found = [ex for ex in non_misc_examples if ex in self.prompt_block]
        self.assertGreaterEqual(len(found), 2,
            f'规则 1 应枚举至少 2 个非杂胶品类的"环保"案例,实际找到 {found}')

    def test_reason_example_uses_eco_x_not_eco_misc(self):
        """reason 示例里的 "环保杂胶" 字样应改成中性 "环保 X",
        让 LLM 知道该模式适用于所有环保品类。"""
        self.assertNotIn('环保杂胶匹配', self.prompt_block)
        self.assertIn('环保 X 匹配', self.prompt_block)

    def test_version_no_longer_at_compare_rows_v2(self):
        """历史基线:2026-08-28 已 bump 到 compare_rows_v3(加严 Rule 4 加面例外)。

        本测试只断言「不再停在 v2」(即 v3 测试负责断言「现在在 v3」),
        v2/v3 字面量本身必须保留作为 audit baseline —— 见下方 test_old_baseline_preserved。
        """
        self.assertNotIn("OCR_MATCH_PROMPT_VERSION = 'compare_rows_v2'", self.src,
            'OCR_MATCH_PROMPT_VERSION 已 bump 到 v3,v2 不应再作为当前版本常量')

    def test_old_baseline_preserved_in_history(self):
        """防回归:tests/test_ocr_match_event_triggers.py 不能因版本 bump 而删除
        compare_rows_v1/v2 的字面量 —— 这是历史 baseline 锚点(给 audit 页做对照)。"""
        test_src = (REPO_ROOT / 'tests' / 'test_ocr_match_event_triggers.py').read_text(encoding='utf-8')
        self.assertTrue(
            "compare_rows_v1" in test_src or "compare_rows_v2" in test_src,
            '历史测试必须保留 v1/v2 字面量作为 baseline 锚点(不能因为 prompt 版本 bump 而改 / 删)'
        )
        # 同一个文件也应该至少一处提到 baseline 历史
        self.assertIn('baseline', test_src.lower(),
            '历史测试的 baseline 注释要保留,标记版本演进历史')


if __name__ == '__main__':
    unittest.main()
