"""placement_match_toggle 前端同步测试 — jsdom 抽真实函数跑 5 个 case。

2026-09-10 修复「✓ 点数」时有时无:
  服务端模板(shipping-records.html:937 / inbound-records.html:622 / loading-orders.html:480)
  按 placement_match=True 渲染「✓ 点数」徽章(.placement-badge),但模板只在整页加载时跑一次。
  若用户不刷新页面就操作 placement,会让「✓ 点数」徽章(模板)与「点数」按钮绿框(前端 toggle)
  出现不同步 —— 比如「按钮绿框消失但 ✓ 点数还在」。
  前端 updatePlacementMatch() 现在 pm=True 时补建徽章(模板没渲染),pm=False 时主动移除
  徽章(覆盖模板残留),保证两边始终同步。

覆盖范围(5 个 case):
  Case 1: pm=False + 模板已渲染 .placement-badge → 徽章被移除(按钮 placement-ok 也移除)
  Case 2: pm=True + 模板未渲染 + 有 placement-add-btn(外层条件满足) → 补建徽章
  Case 3: pm=True + 模板未渲染 + 无 placement-add-btn(外层条件不满足) → 不创建
  Case 4: pm=True + 模板已渲染 .placement-badge → 不重复创建(保留原徽章)
  Case 5: 反复切换(模拟撤销 mark 后又点 mark) → 状态保持同步

注:加载到 placement_count.js IIFE 内的 updatePlacementMatch 函数仅用 DOM API,
可直接 extract + eval 跑(无需模拟整个 IIFE 环境)。
"""
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class JsdomPlacementMatchToggleTests(unittest.TestCase):
    """placement_count.js + loading_placement_count.js 里的 updatePlacementMatch 双向同步「✓ 点数」徽章。"""

    @unittest.skipUnless(
        Path('tests', 'placement_match_toggle_check.js').exists()
        and shutil.which('node') is not None,
        '需要 node + tests/placement_match_toggle_check.js 才能跑',
    )
    def test_placement_match_toggle_jsdom(self):
        proc = subprocess.run(
            ['node', 'tests/placement_match_toggle_check.js'],
            capture_output=True,
        )
        if proc.returncode != 0:
            self.fail(
                f"placement_match_toggle_check.js 退出码 {proc.returncode}\n"
                f"STDOUT: {proc.stdout.decode('utf-8', errors='replace')}\n"
                f"STDERR: {proc.stderr.decode('utf-8', errors='replace')}"
            )
        out = proc.stdout.decode('utf-8', errors='replace')
        self.assertIn('✅ placement_match_toggle_check 通过', out,
                      f'jsdom 检查脚本未通过:\n{out}')

    @unittest.skipUnless(
        Path('tests', 'placement_match_toggle_check.js').exists()
        and shutil.which('node') is not None,
        '需要 node + tests/placement_match_toggle_check.js 才能跑',
    )
    def test_loading_placement_count_js_has_same_fix(self):
        """loading_placement_count.js 的 updatePlacementMatch 也必须包含同样的修复(防止双 js 不同步)。"""
        loading_src_path = Path('static', 'js', 'loading_placement_count.js')
        self.assertTrue(loading_src_path.exists(),
                        'loading_placement_count.js 必须存在')
        loading_src = loading_src_path.read_text(encoding='utf-8')
        # 抽 updatePlacementMatch 函数体
        import re
        m = re.search(
            r'function\s+updatePlacementMatch\s*\([^)]*\)\s*\{',
            loading_src,
        )
        self.assertIsNotNone(m, 'loading_placement_count.js 必须定义 updatePlacementMatch')
        start = m.end()
        depth = 1
        i = start
        while i < len(loading_src) and depth > 0:
            if loading_src[i] == '{':
                depth += 1
            elif loading_src[i] == '}':
                depth -= 1
            i += 1
        fn_body = loading_src[start:i]
        # 必须包含 .placement-badge 处理 + 「btn 存在才补建」的判断
        self.assertIn('.placement-badge', fn_body,
                      'loading_placement_count.js updatePlacementMatch 必须包含 .placement-badge 处理')
        self.assertIn('!serverBadge && td && btn', fn_body,
                      'loading_placement_count.js updatePlacementMatch 必须包含「btn 存在才补建」的判断(防外层条件不满足时误创建徽章)')


if __name__ == '__main__':
    unittest.main()
