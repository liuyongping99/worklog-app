"""COMPARE_PROMPT 回归测试 — order 575 baseline。

目的:
- 改 COMPARE_PROMPT 后,跑此测试立刻看到 verdict 变化
- 当前 baseline: 5 条 record → 3 green(7P 杂胶×3)+ 2 red(无纺布反例×2)

用法:
    # 跑测试(需要 DEEPSEEK_API_KEY + 真实 API 调用,约 3-5 秒)
    python -m unittest tests.regression.test_compare_prompt_order575 -v

    # 改完 COMPARE_PROMPT 后再跑,verdict 变化会在表格里直接显示

设计:
- OCR 文字从 fixture 加载(已固定,不被 LLM 抖动影响)
- 5 条 record 列表硬编码(避免依赖 db 状态)
- expected_verdicts 在文件里直接列出,改 baseline 时一眼能看到
- 不 mock DeepSeek: 真实 API 调用才能反映真实提示词效果
- 失败时打印完整响应(含 reason 字段),便于定位提示词哪条规则没生效
"""
import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from blueprints.ocr_engine import DeepSeekEngine


# ── Baseline 数据:order 575 的 5 条 record + 预期 verdict ───────
ROWS = [
    {'record_id': 1506, 'product_name': '无纺布A白', 'specification': 'A16 100g'},
    {'record_id': 1507, 'product_name': '无纺布A白', 'specification': 'A16 150g'},
    {'record_id': 1508, 'product_name': '7P环保三文治', 'specification': '1.0黑双'},
    {'record_id': 1509, 'product_name': '7P环保杂胶', 'specification': '0.6黑中加面'},
    {'record_id': 1510, 'product_name': '7P环保杂胶', 'specification': '1.0黑中加面'},
]

EXPECTED = {
    1506: 'red',    # OCR 无无纺布 → 找不到
    1507: 'red',    # OCR 无无纺布 → 找不到
    1508: 'green',  # 7P+黑+双面 三条规则同时命中
    1509: 'green',  # 18P ≡ 环保(规则1)+ 黑(规则2)+ 加面(规则4)
    1510: 'green',  # 同上
}

# ── Fixture 路径 ─────────────────────────────────────────────
FIXTURE_PATH = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    'fixtures', 'order_575_ocr.txt',
)


def _load_ocr_text():
    """加载 fixture,返回拼接后的 OCR 文字(去掉注释行)。"""
    with open(FIXTURE_PATH, encoding='utf-8') as f:
        lines = []
        for line in f:
            line = line.rstrip('\n')
            if not line.startswith('#'):
                lines.append(line)
        return '\n'.join(lines)


def _print_verdict_table(actual, ocr_text):
    """打印预期 vs 实测对照表,改提示词后一眼能看到 verdict 漂移。"""
    print('\n' + '=' * 72)
    print(f'{"record":>6} {"product":<14} {"spec":<14} {"expect":<6} {"actual":<6} {"reason"}')
    print('-' * 72)
    by_rid = {v['record_id']: v for v in actual}
    g = y = r = 0
    for row in ROWS:
        rid = row['record_id']
        v = by_rid.get(rid, {})
        actual_status = v.get('match_status', '???')
        reason = v.get('reason', '')[:40]
        marker = '[OK]' if actual_status == EXPECTED[rid] else '[!!]'
        print(f'{rid:>6} {row["product_name"]:<14} {row["specification"]:<14} '
              f'{EXPECTED[rid]:<6} {actual_status:<6} {marker} {reason}')
        if actual_status == 'green': g += 1
        elif actual_status == 'yellow': y += 1
        elif actual_status == 'red': r += 1
    print('-' * 72)
    print(f'合计: {g} green / {y} yellow / {r} red  (期望: 3 / 0 / 2)')
    print('=' * 72)


class ComparePromptOrder575Tests(unittest.TestCase):
    """回归测试:用真实 DeepSeek API 跑 baseline,verdict 应 = EXPECTED。"""

    @classmethod
    def setUpClass(cls):
        cls.ocr_text = _load_ocr_text()
        if not os.environ.get('DEEPSEEK_API_KEY') or \
                os.environ['DEEPSEEK_API_KEY'].startswith('sk-your-'):
            raise unittest.SkipTest(
                'DEEPSEEK_API_KEY 未设置,跳过(本测试需要真实 API 调用)'
            )
        cls.engine = DeepSeekEngine()
        # compare_rows 现在返回 {'verdicts': [...], 'prompt': '...'} 取 verdicts 部分
        cls.actual = cls.engine.compare_rows(cls.ocr_text, ROWS)['verdicts']
        # 打印对照表,无论 pass/fail 都输出,方便调试
        _print_verdict_table(cls.actual, cls.ocr_text)

    def test_all_records_returned(self):
        """DeepSeek 必须对每条 record 返回 verdict。"""
        self.assertEqual(len(self.actual), len(ROWS),
                         f'期望 {len(ROWS)} 条 verdict,实际 {len(self.actual)} 条')

    def test_record_ids_match(self):
        """返回的 record_id 集合必须与请求一致。"""
        actual_ids = {v['record_id'] for v in self.actual}
        expected_ids = {r['record_id'] for r in ROWS}
        self.assertEqual(actual_ids, expected_ids)

    def test_verdicts_match_baseline(self):
        """核心断言:verdict 必须与 baseline 一致。改 COMPARE_PROMPT 后,
        如果 verdict 漂移,会精确指出哪条 record 出错 + 实际 reason 是什么。"""
        by_rid = {v['record_id']: v for v in self.actual}
        failures = []
        for row in ROWS:
            rid = row['record_id']
            actual_status = by_rid.get(rid, {}).get('match_status', 'MISSING')
            if actual_status != EXPECTED[rid]:
                failures.append({
                    'record_id': rid,
                    'product': row['product_name'],
                    'spec': row['specification'],
                    'expected': EXPECTED[rid],
                    'actual': actual_status,
                    'reason': by_rid.get(rid, {}).get('reason', ''),
                })
        if failures:
            msg = '\nverdict 与 baseline 不符:\n' + json.dumps(
                failures, ensure_ascii=False, indent=2)
            self.fail(msg)

    def test_reasons_present(self):
        """reason 字段不能为空(给前端 hover 用)。"""
        for v in self.actual:
            self.assertTrue(
                v.get('reason', '').strip(),
                f'record {v["record_id"]} 缺 reason: {v}'
            )


if __name__ == '__main__':
    unittest.main()