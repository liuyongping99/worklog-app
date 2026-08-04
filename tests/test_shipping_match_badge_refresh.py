"""出货页 refreshRowMatchBadge + badgeHtml 行为回归。

2026-07-30 bug 修复：
- refreshRowMatchBadge 升级逻辑：红牌 + 已人工确认 → green（既有问题）
- 补：黄牌 + 已人工确认 → green（之前不升级,视觉上看不出变化）
- badgeHtml 输出加 data-match-status 属性（之前缺,与服务端首次渲染不一致,
  导致 refreshRowMatchBadge 重建后 .match-badge[data-match-status] 选择器失效）
"""
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SHIPPING_TPL = REPO_ROOT / 'templates' / 'shipping-records.html'
SHIPPING_INCLUDES = SHIPPING_TPL.parent / '_record_image_script.html'  # 被 shipping-records.html include


class RefreshUpgradeTests(unittest.TestCase):
    """badgeHtml / refreshRowMatchBadge 的源码层断言 —— 避免 jsdom 抽取函数带来的复杂度。"""

    def setUp(self):
        # 合并 shipping-records.html + _record_image_script.html(被它 include 的脚本)
        self.src = SHIPPING_TPL.read_text(encoding='utf-8') + '\n' + SHIPPING_INCLUDES.read_text(encoding='utf-8')

    def _extract_fn(self, name):
        """从模板 <script> 里抽函数体。"""
        m = re.search(r'function\s+' + re.escape(name) + r'\s*\([^)]*\)\s*\{', self.src)
        if not m:
            self.fail(f'找不到 {name} 函数')
        i = m.end()
        depth = 1
        while i < len(self.src) and depth > 0:
            if self.src[i] == '{': depth += 1
            elif self.src[i] == '}': depth -= 1
            i += 1
        return self.src[m.start():i]

    def test_badgeHtml_includes_data_match_status_for_all_three_colors(self):
        """badgeHtml 输出必须含 data-match-status 属性(否则 refreshRowMatchBadge 重建后丢失属性)。"""
        body = self._extract_fn('badgeHtml')
        for color in ('green', 'yellow', 'red'):
            with self.subTest(color=color):
                self.assertIn(f'data-match-status="{color}"', body,
                    f'badgeHtml 必须输出 data-match-status="{color}"')

    def test_refreshRowMatchBadge_upgrades_yellow_to_green_when_verified(self):
        """核心 bug 修复:黄牌 + 已人工确认 → 视为 green（之前只覆盖红牌）。"""
        body = self._extract_fn('refreshRowMatchBadge')
        # 找升级条件:必须同时包含 red 和 yellow
        # 用一个能匹配 if (...) 条件的非贪婪搜索
        m = re.search(
            r"if\s*\(\s*\(?\s*s\s*===\s*'red'\s*\|\|\s*s\s*===\s*'yellow'\s*\)?\s*&&\s*img\.querySelector",
            body)
        self.assertIsNotNone(m,
            'refreshRowMatchBadge 升级条件应同时覆盖 red 与 yellow（黄牌+已确认也升级为 green）')
        # 防回归:确保仍然有 red 路径
        self.assertIn("'red'", body)
        self.assertIn("'yellow'", body)

    def test_refreshRowMatchBadge_uses_marker_count_for_reason(self):
        """reason 文案应说明"含 N 张图已人工确认"而不是仅显示张数。"""
        body = self._extract_fn('refreshRowMatchBadge')
        self.assertIn('manual-verified-marker', body)
        self.assertIn('已人工确认', body)

    def test_first_render_no_match_col_in_template(self):
        """服务端首次渲染不渲染 match-col th/td;match-col 由 JS 动态插入。"""
        # 1) 服务端模板里不应该出现 <td class="match-col">
        self.assertNotRegex(
            self.src,
            r'<td\s+class="match-col"',
            '服务端模板不应再渲染 <td class="match-col">',
        )
        # 2) 服务端模板里不应该出现 <th class="match-col">
        self.assertNotRegex(
            self.src,
            r'<th\s+class="match-col"',
            '服务端模板不应再渲染 <th class="match-col">',
        )
        # 3) 服务端模板里不应该再引用 record_worst_status_map
        self.assertNotIn(
            'record_worst_status_map',
            self.src,
            '服务端模板不应再引用 record_worst_status_map',
        )

    def test_change_comments_mention_2026_07_30_fix(self):
        """留下修复日期锚点,后续维护者能通过 git blame 找到 bug 报告。"""
        self.assertIn('2026-07-30', self.src,
            '应在 badgeHtml / refreshRowMatchBadge 附近留 2026-07-30 修复说明')


if __name__ == '__main__':
    unittest.main()
