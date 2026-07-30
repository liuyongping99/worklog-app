"""AI 识别结果校验规则 —— 单元测试

被测对象:`templates/_smart_add_modal.html` 里的 JS 函数 `getItemWarnings(item)`。

设计要点:
1. **单一真相源**:测试从模板文件里正则抽取真实的函数,避免 JS/Python 双份实现漂移。
2. **通过 Node 驱动**:Windows 上 node 也装在 PATH 上,`node -e <script>` 跑通就行。
3. **覆盖 3 条规则 + 边界**:每个用例显式标注触发哪条规则;warning 数量 + 关键子串断言。
4. **跳过**:Node 不可用时用 `@unittest.skip`,CI 环境不会因缺 node 全挂。

运行:
    python -m unittest tests.test_validation_rules -v
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_PATH = REPO_ROOT / 'templates' / '_smart_add_modal.html'


# ── 工具:从模板里抽 getItemWarnings 函数 ────────────────────────────────────
def _extract_get_item_warnings() -> str:
    src = TEMPLATE_PATH.read_text(encoding='utf-8')
    m = re.search(
        r'function getItemWarnings\(item\) \{[\s\S]*?\n\}',
        src,
    )
    if not m:
        raise RuntimeError(
            f"未在 {TEMPLATE_PATH} 中找到 getItemWarnings 函数"
        )
    return m.group(0)


# ── Node 测试运行器 ─────────────────────────────────────────────────────────
_RUNNER_TEMPLATE = """\
{func}

var inputs = {json_inputs};
var results = inputs.map(function(it) {{
    return {{
        product_name: it.product_name,
        specification: it.specification || '',
        warnings: getItemWarnings(it)
    }};
}});
process.stdout.write(JSON.stringify(results));
"""


def _run_cases(inputs: list[dict]) -> list[dict]:
    """通过 Node subprocess 调用抽取出的 getItemWarnings,返回每项的 warning 列表。"""
    func_src = _extract_get_item_warnings()
    runner = _RUNNER_TEMPLATE.format(
        func=func_src,
        json_inputs=json.dumps(inputs, ensure_ascii=False),
    )
    proc = subprocess.run(
        ['node', '-e', runner],
        capture_output=True,
        cwd=str(REPO_ROOT),
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"Node 调用失败:rc={proc.returncode}\n"
            f"STDOUT:{proc.stdout.decode('utf-8', errors='replace')}\n"
            f"STDERR:{proc.stderr.decode('utf-8', errors='replace')}"
        )
    # 显式 UTF-8 解码:Windows 默认 GBK 会把中文 byte 0x... 当非法多字节
    out = proc.stdout.decode('utf-8')
    return json.loads(out)


# ── 用例表 ─────────────────────────────────────────────────────────────────
# (描述, product_name, specification, expected_count, expected_substrings)
# expected_substrings: 每条 warning 应至少含其中一个子串
CASES = [
    # ── 规则 1 ──
    ('规则1: 纯胶 + 任意规格',          '纯胶',       '1.0',           1, ['环保纯胶']),
    ('规则1: 黑白纯胶(子类)',          '黑白纯胶',   '1.0',           1, ['环保纯胶']),
    ('规则1: 已是环保纯胶 不报',       '环保纯胶',   '1.0',           0, []),
    ('规则1: 含环保纯胶子串不算',      '环保纯胶替代','1.0',          0, []),

    # ── 规则 3a(规格含「中软」→ 提示「软性纯胶」)──
    ('规则3a: 1.0中软纯胶 → 软性',     '环保纯胶',   '1.0中软纯胶',   1, ['软性']),
    ('规则3a: 黑中软纯胶 → 软性',     '环保纯胶',   '黑中软纯胶',    1, ['软性']),
    # 注: 规则 3b 已排除 "中软",所以中软规格不会同时被 3b 二次提示(防循环)

    # ── 规则 3b(规格含「软」且不含「终端」→ 提示「中软纯胶」)──
    ('规则3b: 0.6黑软纯胶',           '环保纯胶',   '0.6黑软纯胶',   1, ['中软']),
    ('规则3b: 消光软',                '环保纯胶',   '0.6消光软',     1, ['中软']),
    ('规则3b: 1.0黑硬纯胶 不触发',   '环保纯胶',   '1.0黑硬纯胶',   0, []),
    ('规则3b: 非纯胶族 不触发',      '消光',       '0.6软',         0, []),

    # ── 规则 2a(规格无「加面」→ 提示「加面」)──
    ('规则2a: 7P环保杂胶 无加面',      '7P环保杂胶', '1.0黑中',       1, ['加面']),
    ('规则2a: 杂胶 无加面',            '杂胶',       '1.0',           1, ['加面']),

    # ── 规则 2b(规格有「加面」(无「不加面」)→ 提示「不加面」)──
    ('规则2b: 7P环保杂胶 有加面',      '7P环保杂胶', '1.0黑中加面',   1, ['不加面']),
    ('规则2b: 杂胶 有加面',            '杂胶',       '1.0黑中加面',   1, ['不加面']),
    ('规则2b: 杂胶 不加面 已就绪',    '杂胶',       '1.0黑中不加面', 0, []),

    # ── 组合(同一行多个规则触发)──
    ('组合: 用户原例 R1+R3b',         '纯胶',       '0.6黑软纯胶',   2, ['环保纯胶', '中软']),
    ('组合: 0 警告(环保纯胶+硬规格)', '环保纯胶',   '1.0黑硬纯胶',   0, []),
    ('组合: R1+R2+R3b 三触发',        '纯胶',       '软性杂胶',      3, ['环保纯胶', '中软', '加面']),

    # ── 反向(应不误报)──
    ('反向: PVC 桌布',                 'PVC桌布',    '1.2m',          0, []),
    ('反向: 消光(非纯胶族)',          '消光',       '0.6软',         0, []),
]


@unittest.skipIf(shutil.which('node') is None, 'node 不可用,跳过(需 Node ≥18 + tests/render_check.js)')
class TestGetItemWarnings(unittest.TestCase):
    """getItemWarnings() 三条规则的完整覆盖。"""

    @classmethod
    def setUpClass(cls):
        cls.results = _run_cases([
            {'product_name': pn, 'specification': sp}
            for _, pn, sp, _, _ in CASES
        ])

    def test_each_case(self):
        for case_def, result in zip(CASES, self.results):
            desc, pn, sp, expect_n, expect_substrs = case_def
            with self.subTest(case=desc):
                raw = result['warnings']
                # 契约(per-rule 核查改造后):getItemWarnings 返回 [{rule_id, message}],
                # 不再是纯字符串数组 —— rule_id 是「单条警告已核查」状态的追踪键。
                actual = [w['message'] for w in raw]
                self.assertEqual(
                    len(actual), expect_n,
                    f"[{desc}] pn={pn!r} sp={sp!r}\n"
                    f"  期望 {expect_n} 条 warning, 实际 {len(actual)} 条: {actual}",
                )
                # 每条警告必须带非空 rule_id,且同一条目内不重复 ——
                # 否则 verified_warnings[rule_id] 会互相覆盖,核查一条等于核查两条。
                rule_ids = [w.get('rule_id') for w in raw]
                self.assertTrue(
                    all(rid for rid in rule_ids),
                    f"[{desc}] 存在缺失 rule_id 的警告: {raw}",
                )
                self.assertEqual(
                    len(set(rule_ids)), len(rule_ids),
                    f"[{desc}] rule_id 重复,核查状态会互相覆盖: {rule_ids}",
                )
                for substr in expect_substrs:
                    self.assertTrue(
                        any(substr in msg for msg in actual),
                        f"[{desc}] 期望 warning 含 '{substr}',实际:\n  " +
                        "\n  ".join(actual),
                    )

    def test_no_warning_when_fully_specified(self):
        """保护性测试:已经合规的条目不许被规则污染

        合规场景:环保前缀已带 + 规格用硬(非软)→ 没有任何规则触发
        (注:如果用中软/软作规格,新规则 3a+3b 会刻意提问以防误录)
        """
        result = _run_cases([{
            'product_name': '环保纯胶',
            'specification': '1.0黑硬纯胶',
        }])
        self.assertEqual(result[0]['warnings'], [])

    def test_function_extraction_works(self):
        """保证正则抽取没问题(template 改格式时不会静默失效)"""
        func = _extract_get_item_warnings()
        self.assertIn('function getItemWarnings', func)
        self.assertIn('warnings.push(', func)
        # 必须至少有 3 次 push(对应 3 条规则)
        self.assertGreaterEqual(func.count('warnings.push('), 3,
                                'getItemWarnings 应至少有 3 条 warnings.push')

    @unittest.skipUnless(
        Path('tests', 'render_check.js').exists()
        and shutil.which('node') is not None,
        '需要 node + tests/render_check.js 才能跑 DOM 渲染测试'
    )
    def test_render_produces_distinct_warning_blocks(self):
        """DOM 端验证:校验警告搬到明细行下方后,行为正确。

        守护 issues:
        - 历史回归:曾经 .join('；') 让两规则看起来像 1 条,用户报"只触发 1 条"
        - 后续需求:警告从弹框挪到明细行下方,要确保:
          (a) 弹框内不再渲染警告块
          (b) addRowWarning 把警告落到目标 tr 下方,1 行触发 R1+R3b → 2 个独立块
        """
        proc = subprocess.run(
            ['node', 'tests/render_check.js'],
            capture_output=True,
            cwd=str(REPO_ROOT),
        )
        if proc.returncode != 0:
            self.fail(
                f"render_check.js 退出码 {proc.returncode}\n"
                f"STDOUT: {proc.stdout.decode('utf-8', errors='replace')}\n"
                f"STDERR: {proc.stderr.decode('utf-8', errors='replace')}"
            )
        out = proc.stdout.decode('utf-8', errors='replace')
        # 新断言 1: 弹框内不应有警告(已搬走)
        self.assertIn('弹框内警告块数: 0', out,
                      f'期望弹框内无警告块(已搬走),实际输出:\n{out}')
        # 新断言 2: 明细表 addRowWarning 后,1 数据行 + 1 警告行,2 个独立块
        self.assertIn('明细表独立警告块数: 2', out,
                      f'期望明细表 2 个独立警告块,实际输出:\n{out}')
        self.assertIn('✅ render_check 通过', out,
                      f'render_check 未显示通过标记,实际输出:\n{out}')


if __name__ == '__main__':
    unittest.main()
