"""出货页 AI→添加 行 cell 数与表头列数对齐回归测试。

Bug 历史:2026-08-22 用户报告"AI 识别后添加商品,数量列看似丢失,后续列被挤到一起"。
根因:`buildShippingRecordRow()` 硬塞 `<td class="match-col"></td>`,而 server 模板表头
没 match-col 列(由 `_ensureMatchColumn()` 首次有匹配结果时动态插入),导致 AI 添加行
10 cells vs 表头 9 列,数量及之后列左移错位。

修复:
1. `buildShippingRecordRow` 不再硬塞 match-col,由 `_ensureMatchColumn` 按需补建
2. `_ensureMatchColumn` 跳过警告行(record-warn-row),避免 colspan tr 被追加空 td
"""

import os
import re
import subprocess
import textwrap

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _extract_js_block(template_path: str, function_name: str) -> str:
    """从一个 Jinja2 模板里抽出 `function NAME(...)` 的完整源码(大括号配对)。"""
    with open(template_path, encoding='utf-8') as f:
        src = f.read()
    pattern = re.compile(
        rf'(function\s+{re.escape(function_name)}\s*\([^)]*\)\s*\{{)',
        re.MULTILINE,
    )
    m = pattern.search(src)
    if not m:
        raise RuntimeError(f'{function_name} not found in {template_path}')
    start = m.start()
    i = m.end() - 1  # 当前在第一个 '{' 上
    depth = 0
    while i < len(src):
        c = src[i]
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
        i += 1
    raise RuntimeError(f'未匹配到 {function_name} 闭合大括号')


# 服务端模板表头列数(shipping-records.html:712-720 9 个 <th>)。
# buildShippingRecordRow 生成的 td 数必须等于此值,否则列错位。
EXPECTED_HEADER_COLUMNS = 9


# ── jsdom + 函数 stub 模板 ──────────────────────────────────────────
JSDOM_SHELL = r'''
const { JSDOM } = require('jsdom');
const dom = new JSDOM('<!DOCTYPE html><html><body><table class="record-table">' +
    '<thead><tr>' +
    '<th>序号</th>' +
    '<th class="eco-col">重点</th>' +
    '<th>商品名称</th>' +
    '<th class="spec-cell">规格</th>' +
    '<th>数量</th>' +
    '<th>单位</th>' +
    '<th>备注</th>' +
    '<th>辅助单位提示</th>' +
    '<th>操作</th>' +
    '</tr></thead><tbody></tbody></table></body></html>');
const document = dom.window.document;
const window = dom.window;
function escHtml(s) { return String(s == null ? '' : s); }
'''


def _run_node(js_body: str) -> str:
    full = textwrap.dedent(JSDOM_SHELL) + '\n' + js_body
    result = subprocess.run(
        ['node', '-e', full],
        capture_output=True, text=True, cwd=REPO_ROOT, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f'node 执行失败:\nSTDOUT:{result.stdout}\nSTDERR:{result.stderr}'
        )
    return result.stdout.strip()


class TestShippingRowCellCount:
    """AI→添加新建行 cell 数应等于 server 表头列数(9)。"""

    def test_buildShippingRecordRow_cell_count_matches_header(self):
        """buildShippingRecordRow 应生成 9 个 td,与 server 表头 9 列对齐。

        修复前(2026-08-22 前):硬塞 match-col → 10 cells → 数量及之后列错位。
        修复后:_ensureMatchColumn 按需补建,新建行初始 9 cells。
        """
        js = _extract_js_block(
            os.path.join(REPO_ROOT, 'templates', '_smart_add_modal.html'),
            'buildShippingRecordRow',
        )
        output = _run_node(js + '''
            const tbody = document.querySelector('tbody');
            const tr = buildShippingRecordRow({
                id: 1, product_name: 'PVC桌布', specification: '1.2x1.8m',
                quantity: '50', unit: '支', remark: '',
                verified: 0, verified_warnings: {}
            }, tbody);
            process.stdout.write('CELL_COUNT=' + tr.children.length);
        ''')
        m = re.search(r'CELL_COUNT=(\d+)', output)
        assert m, f'未找到 CELL_COUNT 输出: {output!r}'
        actual = int(m.group(1))
        assert actual == EXPECTED_HEADER_COLUMNS, (
            f'AI→添加新建行 cell 数={actual},表头列数={EXPECTED_HEADER_COLUMNS},'
            f'差 {actual - EXPECTED_HEADER_COLUMNS} 列会导致数量及之后列错位'
        )


class TestEnsureMatchColumnSkipsWarnRow:
    """_ensureMatchColumn 给兄弟行补 match-col 时,应跳过警告行。

    警告行 `<tr class="record-warn-row">` 带 colspan,被插入空 td 会破坏 colspan 布局。
    """

    def test_warn_row_not_appended_match_col(self):
        js = _extract_js_block(
            os.path.join(REPO_ROOT, 'templates', '_record_image_script.html'),
            '_ensureMatchColumn',
        )
        output = _run_node(js + '''
            const tbody = document.querySelector('tbody');
            const dataRow = document.createElement('tr');
            dataRow.innerHTML = '<td class="seq-cell"></td><td class="eco-col"></td>' +
                '<td class="product-name-cell">PVC桌布</td><td class="spec-cell">1.2x1.8m</td>' +
                '<td class="qty-cell"><span class="qty-badge">50</span></td>' +
                '<td class="unit-cell"><span class="unit-badge">支</span></td>' +
                '<td class="remark-cell">—</td>' +
                '<td class="unit-hint-cell">—</td>' +
                '<td class="actions-cell"></td>';
            tbody.appendChild(dataRow);
            const warnRow = document.createElement('tr');
            warnRow.className = 'record-warn-row';
            warnRow.innerHTML = '<td colspan="9">⚠ 警告</td>';
            tbody.appendChild(warnRow);
            const table = document.querySelector('table.record-table');
            _ensureMatchColumn(table, dataRow, '');
            process.stdout.write('WARN_CELL_COUNT=' + warnRow.children.length +
                                 ' DATA_CELL_COUNT=' + dataRow.children.length);
        ''')
        m_warn = re.search(r'WARN_CELL_COUNT=(\d+)', output)
        m_data = re.search(r'DATA_CELL_COUNT=(\d+)', output)
        assert m_warn and m_data, f'输出不完整: {output!r}'
        warn_n = int(m_warn.group(1))
        data_n = int(m_data.group(1))
        assert warn_n == 1, (
            f'警告行被 _ensureMatchColumn 追加了 match-col td,'
            f'当前 {warn_n} 个 cell(应保持 1,colspan 不被破坏)'
        )
        # 数据行应有 match-col(由 _ensureMatchColumn 在 spec-cell 后插入)
        assert data_n == EXPECTED_HEADER_COLUMNS + 1, (
            f'数据行 cell 数={data_n},预期 {EXPECTED_HEADER_COLUMNS + 1} '
            f'(9 + match-col = 10)'
        )
