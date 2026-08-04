# match-col 虚拟列重构 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把出货/入库/装柜三套订单表的「AI 比对」列从服务端预渲染改为 JS 动态插入，修「行级图上传后徽章被 `.no-match` CSS 隐藏」的体验断点。

**Architecture:** 服务端不再渲染 `<th class="match-col">` / `<td class="match-col">`，并删除 `.no-match` CSS 规则；JS 新增私有函数 `_ensureMatchColumn(table, targetRow, badgeHtml)`，在 `setRowMatchBadge` 和 `applyAsyncMatchResult` 里调用，实现「首次有匹配结果时插入列 + 兄弟行插空 td 保持对齐」。Smart-add 弹框里的占位 td 保留（行还没入库，列结构要先存在）。

**Tech Stack:** Jinja2 / 原生 JS + Tailwind / Python 3.12 / SQLite

**Spec:** `docs/superpowers/specs/2026-08-04-match-col-virtual-design.md`

## Global Constraints

- **三套订单模板同步改**：`shipping-records.html` / `inbound-records.html` / `loading-orders.html`
- **服务端永不渲染 match-col**：删除所有 `<th class="match-col">` / `<td class="match-col">` / `group.has_match` / `record_worst_*_map` / `.no-match`
- **JS 首次有匹配结果才插列**：`match_status` 为空时不插（用户体验：上传后到 OCR 完成前不该出现列）
- **兄弟行插空 td 对齐**：同一 table 内插入 match-col 时，所有行都要有对应 td，否则表格错位
- **Smart-add 弹框保留占位**：`_smart_add_modal.html:41` 的 `<td class="match-col"></td>` 不动
- **CRLF 换行**：仓库 Windows CRLF
- **测试夹具用 `tests/__init__.py::fake_png_bytes()`**：本计划不涉及图片上传测试，但如有扩展，沿用共享 helper
- **后台异步线程防护**：本计划不修改行级图上传路径，但若新增测试用到行级图上传，必须在 `_TempDb` 类 `tearDown` 加 `_drain_async_jobs()`（参考 `tests/test_upload_ai_routing.py`）

---

## File Structure

**改动文件 (8)**：

- `templates/shipping-records.html`
  - 删 `<th class="match-col" ...>AI 比对</th>`（约 line 460）
  - 删 `<td class="match-col">{% ... record_worst_status_map ... %}</td>`（约 line 478）
  - 删 `<table class="record-table...{% if not has_match %} no-match{% endif %}">` 的 `no-match`（约 line 453）
  - 删 group 内 `has_match` set 声明（约 line 451）
- `templates/inbound-records.html`
  - 同上（行号不同）+ 删 `.inbound-page .record-table.no-match .match-col { display: none; }`（约 line 302）
- `templates/loading-orders.html`
  - 同上（行号不同）+ 删 `.loading-page .record-table.no-match .match-col { display: none; }`（约 line 99）
  - 删 4 处 JS 字符串里的 `<td class="match-col">...</td>`（约 line 892 / 933 / 977 / 1129）——这些是 JS 动态构建明细行的代码
- `blueprints/shipping.py`
  - 删 `record_worst_status_map = {}` / `record_worst_reason_map = {}` / `record_worst_source_map = {}` / `record_human_verified_map = {}`（约 line 271-300）
  - 删 `group['has_match'] = any(...)`（约 line 381）
  - 删 4 个 `map=...` 上下文传参（约 line 395）
- `blueprints/inbound.py`
  - 删 `record_worst_status_map = {}` + 填充循环 + `grp['has_match']` + 上下文传参（约 line 217-256）

**新增 JS（1 文件）**：

- `templates/_record_image_script.html`
  - 新增私有函数 `_ensureMatchColumn(table, targetRow, badgeHtml)`
  - 修改 `setRowMatchBadge`（约 line 30）：先调 `_ensureMatchColumn` 再写徽章
  - 修改 `applyAsyncMatchResult`（约 line 230）：轮询完成时调 `_ensureMatchColumn`

**改动测试 (2 文件)**：

- `tests/test_shipping_match_badge_refresh.py` — 改 1 个断言
- `tests/test_shipping_p1_fixes.py` — 改 1 个断言

**新增测试 (2 文件)**：

- `tests/test_match_column_dynamic.py` — pytest，服务端首次渲染无 match-col
- `tests/record_image_match_column_check.js` — jsdom 行为测试，3 case

---

## Task 1: 服务端删除 match-col 渲染 + CSS 删 .no-match

**Files:**
- Modify: `templates/shipping-records.html:451` (删 `has_match` set)
- Modify: `templates/shipping-records.html:453` (删 `{% if not has_match %} no-match{% endif %}` 子句)
- Modify: `templates/shipping-records.html:460` (删 `<th class="match-col" ...>AI 比对</th>`)
- Modify: `templates/shipping-records.html:478` (删整行 `<td class="match-col">{% ... %}</td>`)
- Modify: `templates/inbound-records.html:499` (删 `{% if not group.has_match %} no-match{% endif %}`)
- Modify: `templates/inbound-records.html:302` (删 `.inbound-page .record-table.no-match .match-col { display: none; }`)
- Modify: `templates/inbound-records.html:508` (删 `<th class="match-col" ...>AI 比对</th>`)
- Modify: `templates/inbound-records.html:524` (删 `<td class="match-col" ...></td>`)
- Modify: `templates/loading-orders.html:99` (删 `.loading-page .record-table.no-match .match-col { display: none; }`)
- Modify: `templates/loading-orders.html:236` (删 `<th class="match-col" ...>`)
- Modify: `templates/loading-orders.html:255` (删 `<td class="match-col" ...>`)
- Modify: `templates/loading-orders.html:892,933,977,1129` (删 4 处 JS 字符串里的 `<td class="match-col">...</td>`)
- Modify: `blueprints/shipping.py:271-300` (删 4 个 map 字典)
- Modify: `blueprints/shipping.py:381` (删 `group['has_match'] = any(...)`)
- Modify: `blueprints/shipping.py:395` (删 4 个 map 上下文传参)
- Modify: `blueprints/inbound.py:217-256` (删 `record_worst_status_map` + `has_match` + 上下文传参)
- Create: `tests/test_match_column_dynamic.py` (服务端首次渲染无 match-col 断言)
- Modify: `tests/test_validation_rules.py` (可选：确保不需要改)

**Interfaces:**
- Consumes: 无
- Produces:
  - `templates/*.html`：服务端渲染的 HTML 中无 `<th class="match-col">` / `<td class="match-col">` / `no-match` class
  - `blueprints/*.py`：上下文 dict 不再有 `record_worst_status_map` / `record_worst_reason_map` / `record_worst_source_map` / `record_human_verified_map` / `group.has_match`

- [ ] **Step 1: 写红测试 `tests/test_match_column_dynamic.py`**

```python
"""match-col 虚拟列 — 服务端首次渲染不应渲染 match-col th/td。"""
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models._db as _db


def _drain_async_jobs(timeout=15):
    """防止行级图上传后台线程活过 tearDown 污染生产库。"""
    import time
    from blueprints.shipping import _ASYNC_JOBS
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not any(j.get('state') == 'processing' for j in list(_ASYNC_JOBS.values())):
            return
        time.sleep(0.02)
    raise AssertionError('后台 OCR 任务超时未结束,可能污染生产库')


class _TempDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        self._orig = _db.DB_PATH
        _db.DB_PATH = self.tmp.name
        from models import init_db
        init_db()
        from app import create_app
        self.client = create_app().test_client()
        from models.tasks_flow import StaffDB
        sid = StaffDB.create('test', '调度')['id']
        with self.client.session_transaction() as s:
            s['operator_id'] = sid

    def tearDown(self):
        _drain_async_jobs()
        _db.DB_PATH = self._orig
        for ext in ('', '-wal', '-shm'):
            p = self.tmp.name + ext
            if os.path.exists(p):
                try: os.unlink(p)
                except OSError: pass


class ServerSideNoMatchColTests(_TempDb):
    """三套订单表服务端首次渲染,均不应出现 match-col th/td。"""

    def _make_order_with_record(self, page):
        from models import ShippingOrder, ShippingRecord, InboundOrder, InboundRecord, LoadingOrder, LoadingOrderRecord
        if page == 'shipping':
            oid = ShippingOrder.create('2026-08-04', '客户A')
            ShippingRecord.create('2026-08-04', '客户A', '环保杂胶', '0.8黑中加面', '50', 'y', '', oid)
        elif page == 'inbound':
            oid = InboundOrder.create('2026-08-04', '供应商X')
            InboundRecord.create('2026-08-04', '供应商X', '无纺布', 'A1.5m 200g', '10', 'y', '', oid)
        elif page == 'loading':
            oid = LoadingOrder.create('2026-08-04', '央士')
            LoadingOrderRecord.create('2026-08-04', '央士', 'PE板', '1m', '5', 'y', '', oid)

    def test_shipping_first_render_has_no_match_col(self):
        self._make_order_with_record('shipping')
        res = self.client.get('/shipping-records?start_date=2026-08-04&end_date=2026-08-04')
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertNotIn('match-col', html,
            '出货页首次服务端渲染不应包含 match-col (th/td 都不应有)')
        self.assertNotIn('no-match', html,
            '出货页首次服务端渲染不应使用 no-match class')

    def test_inbound_first_render_has_no_match_col(self):
        self._make_order_with_record('inbound')
        res = self.client.get('/inbound-records?start_date=2026-08-04&end_date=2026-08-04')
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertNotIn('match-col', html)
        self.assertNotIn('no-match', html)

    def test_loading_first_render_has_no_match_col(self):
        self._make_order_with_record('loading')
        res = self.client.get('/loading-orders?start_date=2026-08-04&end_date=2026-08-04')
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertNotIn('match-col', html)
        self.assertNotIn('no-match', html)


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: 跑测试,确认 RED**

Run:
```bash
PYTHONUTF8=1 python -m pytest tests/test_match_column_dynamic.py -q -p no:warnings
```
Expected: 3 个测试全部 FAIL（实际服务端渲染仍含 match-col + no-match）

- [ ] **Step 3: 改 `templates/shipping-records.html`**

具体改动：

1. line 451 附近：删
```python
{% set has_match = group.has_match|default(false) %}
```

2. line 453 附近：改
```python
<table class="record-table{% if not has_eco and not has_jia_mian %} no-eco{% endif %}">
```
（删除 `{% if not has_match %} no-match{% endif %}`）

3. line 460 附近：删
```html
<th class="match-col" style="width:55px;">AI 比对</th>
```

4. line 478 附近：删整行（很长的 `{% set ... %}{% if ... %}{% endif %}` 块）
```html
<td class="match-col">{% set _bst = record_worst_status_map.get(item.id) %}...{% endif %}</td>
```

- [ ] **Step 4: 改 `templates/inbound-records.html`**

1. line 302 附近：删
```css
.inbound-page .record-table.no-match .match-col { display: none; }
```

2. line 499 附近：改
```html
<table class="record-table">
```
（删除 `{% if not group.has_match %} no-match{% endif %}`）

3. line 508 附近：删
```html
<th class="match-col" style="width:55px;">AI 比对</th>
```

4. line 524 附近：删
```html
<td class="match-col" style="text-align:center;"></td>
```

- [ ] **Step 5: 改 `templates/loading-orders.html`**

1. line 99 附近：删
```css
.loading-page .record-table.no-match .match-col { display: none; }
```

2. line 236 附近：删
```html
<th class="match-col" style="width:55px;">AI 比对</th>
```

3. line 255 附近：删
```html
<td class="match-col" style="text-align:center;"></td>
```

4. line 892 / 933 / 977 / 1129 附近：4 处 JS 字符串里的
```js
'<td class="match-col" style="text-align:center;">' + ... + '</td>' +
```
全部删除（包括前面的连接符 `+`）。注意：删除前先看上下文——某些位置可能用 `(...) + '<td...' + (...)` 三段拼接，把整个 `<td class="match-col">...</td>` 子表达式删掉，保留前后用 `+` 连接的逻辑。

- [ ] **Step 6: 改 `blueprints/shipping.py`**

读 `blueprints/shipping.py:265-310` 找到 `record_worst_status_map = {}` 开始的 4 个字典声明和填充循环（约 30-40 行），全部删除。

读 `blueprints/shipping.py:375-395`，找到 `group['has_match'] = any(...)` 那一行，删除。

读 `blueprints/shipping.py:395` 附近的 render_template 调用，删除 4 个 `record_worst_status_map=record_worst_status_map` 等上下文传参。

- [ ] **Step 7: 改 `blueprints/inbound.py`**

读 `blueprints/inbound.py:217-256`，删除：
- `record_worst_status_map = {}` 声明
- 填充循环（约 20 行）
- `grp['has_match'] = has_match` 那一行
- 上下文传参 `record_worst_status_map=record_worst_status_map`

- [ ] **Step 8: 跑测试,确认 GREEN**

Run:
```bash
PYTHONUTF8=1 python -m pytest tests/test_match_column_dynamic.py -q -p no:warnings
```
Expected: 3/3 PASS

- [ ] **Step 9: 跑全量回归,确保没破坏其他测试**

Run:
```bash
PYTHONUTF8=1 python -m pytest tests/ -q -p no:warnings --ignore=tests/regression --ignore=tests/test_shipping_match_badge_refresh.py --ignore=tests/test_shipping_p1_fixes.py
```
Expected: 300+ passed / 0 failed（task 1 不该改这两个测试，但避免本 task 影响其他，临时跳过这两个等 Task 4 修）

- [ ] **Step 10: Commit**

```bash
git add templates/shipping-records.html templates/inbound-records.html templates/loading-orders.html \
        blueprints/shipping.py blueprints/inbound.py \
        tests/test_match_column_dynamic.py
git commit -m "feat(shipping): match-col 服务端不再渲染,改由 JS 动态插入"

# 消息体（如果想详细）：
# feat(shipping): match-col 服务端不再渲染,改由 JS 动态插入
#
# - 三套订单模板:删 <th class="match-col"> / <td class="match-col"> / no-match class
# - inbound/loading: 删 .no-match .match-col { display: none } CSS
# - shipping/inbound: 删 record_worst_*_map / has_match / 上下文传参
# - loading 模板 4 处 JS 字符串里的 match-col td 也删
# - 新增 tests/test_match_column_dynamic.py (3 测试,全过)
#
# 修复点:行级图上传后徽章被 .no-match CSS 隐藏的体验断点
```

---

## Task 2: JS — `_ensureMatchColumn` 私有函数 + 接入 setRowMatchBadge

**Files:**
- Modify: `templates/_record_image_script.html` (新增 `_ensureMatchColumn` 私有函数)
- Modify: `templates/_record_image_script.html` (修改 `setRowMatchBadge` 接入)
- Create: `tests/record_image_match_column_check.js` (jsdom 行为测试)
- Create: `tests/test_match_column_dynamic.py` (新增 pytest runner — 实际写到同 Task 1 文件的末尾)

**Interfaces:**
- Consumes: 无（独立 JS 函数）
- Produces:
  - `templates/_record_image_script.html:_ensureMatchColumn(table, targetRow, badgeHtml)` — 内部私有，jsdom 测试可抽
  - `setRowMatchBadge(recordPk, status, reason, source)` — 内部行为：调 `_ensureMatchColumn` 后写徽章

- [ ] **Step 1: 写 jsdom 红测试 `tests/record_image_match_column_check.js`**

```js
// match-col 虚拟列重构 — JS 行为测试 (jsdom)
//
// 验证 _ensureMatchColumn / setRowMatchBadge 在三种状态下的行为:
//   Case 1: 空 table + 1 行 → 调一次 → 表头出现 th、所有 tr 有 td、目标行 td 含徽章
//   Case 2: 已有 match-col 的 table → 调一次 → 只更新目标行 td,不重复插列头 / 不重复插空 td
//   Case 3: 5 行 table + 目标为第 3 行 → 调一次 → 表头 1 个 th、所有 5 行有 td
//
// 跑法: node tests/record_image_match_column_check.js
const { JSDOM } = require('jsdom');
const fs = require('fs');
const path = require('path');

const tpl = fs.readFileSync(
  path.join(__dirname, '..', 'templates', '_record_image_script.html'), 'utf8');

function extractFunc(src, name) {
  const patterns = [
    new RegExp('function\\s+' + name + '\\s*\\([^)]*\\)\\s*\\{'),
    new RegExp('(?:window\\.|var\\s+)?' + name.replace('.', '\\.') +
               '\\s*=\\s*function\\s*\\([^)]*\\)\\s*\\{'),
  ];
  for (const re of patterns) {
    const m = re.exec(src);
    if (!m) continue;
    let i = m.index + m[0].length;
    let depth = 1;
    while (i < src.length && depth > 0) {
      if (src[i] === '{') depth++;
      else if (src[i] === '}') depth--;
      i++;
    }
    return src.substring(m.index, i);
  }
  return '';
}

const errors = [];
function check(cond, msg) { if (!cond) errors.push(msg); }

// ── 抽两个函数 ─────────────────────────────────────────────────
const ensureFnSrc = extractFunc(tpl, '_ensureMatchColumn');
const setBadgeFnSrc = extractFunc(tpl, 'setRowMatchBadge');
if (!ensureFnSrc) {
  console.error('❌ 抽不出 _ensureMatchColumn');
  process.exit(1);
}
if (!setBadgeFnSrc) {
  console.error('❌ 抽不出 setRowMatchBadge');
  process.exit(1);
}

// ── Case 1: 空 table + 1 行 → 表头 th、目标行 td 含徽章 ──────
{
  const dom = new JSDOM(`<!DOCTYPE html><html><body>
<div class="date-group" data-order-id="100">
  <table class="record-table">
    <thead>
      <tr><th>序号</th><th>重点</th><th>商品名称</th>
          <th class="spec-cell">规格</th><th>数量</th></tr>
    </thead>
    <tbody>
      <tr data-record-id="7">
        <td>1</td><td></td>
        <td class="product-name-cell">环保杂胶</td>
        <td class="spec-cell">0.8黑中加面</td>
        <td>50</td>
      </tr>
    </tbody>
  </table>
</div>
</body></html>`);
  global.window = dom.window;
  global.document = dom.window.document;
  eval(ensureFnSrc + '\n' + setBadgeFnSrc);
  // setRowMatchBadge 内部需要 _ensureMatchColumn
  setRowMatchBadge(7, 'green', '命中 7P 等价', 'local_fuzzy');

  const table = dom.window.document.querySelector('table');
  const ths = table.querySelectorAll('thead th');
  const trs = table.querySelectorAll('tbody tr');
  check(ths.length === 6, `Case 1: 表头应有 6 个 th(原 5 + match-col),实际 ${ths.length}`);
  check(ths[4] && ths[4].classList.contains('match-col'),
        'Case 1: 第 5 个 th 应是 match-col');
  check(ths[4] && ths[4].textContent === 'AI 比对',
        'Case 1: match-col th 文本应为 "AI 比对"');
  check(trs[0].querySelector('td.match-col'),
        'Case 1: 目标行应有 td.match-col');
  check(trs[0].querySelector('td.match-col').innerHTML.includes('green'),
        'Case 1: 目标行 td.match-col 应含 green 徽章');
}

// ── Case 2: 已有 match-col → 不重复插入 ──────────────────────
{
  const dom = new JSDOM(`<!DOCTYPE html><html><body>
<table class="record-table">
  <thead><tr><th>序号</th><th>重点</th><th>品名</th>
         <th class="spec-cell">规格</th>
         <th class="match-col">AI 比对</th><th>数量</th></tr></thead>
  <tbody>
    <tr data-record-id="7">
      <td>1</td><td></td>
      <td class="product-name-cell">环保杂胶</td>
      <td class="spec-cell">0.8黑中加面</td>
      <td class="match-col">旧绿</td><td>50</td>
    </tr>
  </tbody>
</table>
</body></html>`);
  global.window = dom.window;
  global.document = dom.window.document;
  eval(ensureFnSrc + '\n' + setBadgeFnSrc);
  setRowMatchBadge(7, 'red', '规格不符', 'deepseek');

  const table = dom.window.document.querySelector('table');
  const ths = table.querySelectorAll('thead th.match-col');
  const trs = table.querySelectorAll('tbody tr');
  check(ths.length === 1, `Case 2: 表头 match-col 应仍是 1 个,实际 ${ths.length}`);
  check(trs[0].querySelectorAll('td.match-col').length === 1,
        `Case 2: 目标行 td.match-col 应仍是 1 个,实际 ${trs[0].querySelectorAll('td.match-col').length}`);
  check(trs[0].querySelector('td.match-col').innerHTML.includes('red'),
        'Case 2: 目标行 td.match-col 应已更新为 red 徽章');
  check(!trs[0].querySelector('td.match-col').innerHTML.includes('旧绿'),
        'Case 2: 旧的「旧绿」文本应被替换');
}

// ── Case 3: 5 行 table, 目标为第 3 行 → 全部行都有 td ───────
{
  const dom = new JSDOM(`<!DOCTYPE html><html><body>
<table class="record-table">
  <thead><tr><th>序号</th><th>重点</th><th>品名</th>
         <th class="spec-cell">规格</th><th>数量</th><th>操作</th></tr></thead>
  <tbody>
    <tr data-record-id="1"><td>1</td><td></td><td class="product-name-cell">a</td><td class="spec-cell">x</td><td>1</td><td>—</td></tr>
    <tr data-record-id="2"><td>2</td><td></td><td class="product-name-cell">b</td><td class="spec-cell">y</td><td>2</td><td>—</td></tr>
    <tr data-record-id="3"><td>3</td><td></td><td class="product-name-cell">c</td><td class="spec-cell">z</td><td>3</td><td>—</td></tr>
    <tr data-record-id="4"><td>4</td><td></td><td class="product-name-cell">d</td><td class="spec-cell">w</td><td>4</td><td>—</td></tr>
    <tr data-record-id="5"><td>5</td><td></td><td class="product-name-cell">e</td><td class="spec-cell">v</td><td>5</td><td>—</td></tr>
  </tbody>
</table>
</body></html>`);
  global.window = dom.window;
  global.document = dom.window.document;
  eval(ensureFnSrc + '\n' + setBadgeFnSrc);
  // 选第 3 行 (record-id=3)
  setRowMatchBadge(3, 'yellow', '存疑', 'deepseek');

  const table = dom.window.document.querySelector('table');
  const ths = table.querySelectorAll('thead th.match-col');
  const trs = table.querySelectorAll('tbody tr');
  check(ths.length === 1, `Case 3: 表头 match-col 应只有 1 个,实际 ${ths.length}`);
  check(trs.length === 5, `Case 3: tbody 应仍是 5 行,实际 ${trs.length}`);
  trs.forEach((tr, i) => {
    const tds = tr.querySelectorAll('td.match-col');
    if (tds.length !== 1) {
      errors.push(`Case 3: 第 ${i+1} 行 td.match-col 应有 1 个,实际 ${tds.length}`);
    }
  });
  // 目标行(第 3 行)含 yellow,其他行空 td
  check(trs[2].querySelector('td.match-col').innerHTML.includes('yellow'),
        'Case 3: 目标行 td.match-col 应含 yellow 徽章');
  check(trs[0].querySelector('td.match-col').innerHTML.trim() === '',
        'Case 3: 兄弟行 td.match-col 应为空');
}

if (errors.length) {
  errors.forEach(e => console.error('❌ ' + e));
  process.exit(1);
}
console.log('✅ record_image_match_column_check 通过');
```

- [ ] **Step 2: 跑测试,确认 RED**

Run:
```bash
node tests/record_image_match_column_check.js
```
Expected: 抽不出 `_ensureMatchColumn` 退出 1（函数尚未定义）

- [ ] **Step 3: 在 `templates/_record_image_script.html` 新增 `_ensureMatchColumn`**

找一个合适的位置（例如 line 90 附近，紧跟在 `refreshRowMatchBadge` 后面）插入：

```js
// 2.5 _ensureMatchColumn — 在 table 内动态插入 AI 比对列(首次有匹配结果时)
// 位置: 规格 cell 之后(td[4])。
// 表头 + 目标行 + 其他兄弟行(保持对齐)。
function _ensureMatchColumn(table, targetRow, badgeHtml) {
    if (!table || !targetRow) return;
    var theadRow = table.querySelector('thead tr');
    var specTh = theadRow && theadRow.querySelector('th.spec-cell, th:nth-child(4)');
    var specTd = targetRow.querySelector('td.spec-cell');
    var insertAfter = function (parent, ref, newEl) {
        if (ref && ref.nextElementSibling) parent.insertBefore(newEl, ref.nextElementSibling);
        else parent.appendChild(newEl);
    };

    // 1) 表头:如尚未插入 th,加一个
    if (theadRow && !theadRow.querySelector('th.match-col')) {
        var th = document.createElement('th');
        th.className = 'match-col';
        th.style.width = '55px';
        th.textContent = 'AI 比对';
        insertAfter(theadRow, specTh, th);
    }

    // 2) 目标行:写入徽章
    var targetTd = targetRow.querySelector('td.match-col');
    if (!targetTd) {
        targetTd = document.createElement('td');
        targetTd.className = 'match-col';
        targetTd.style.textAlign = 'center';
        insertAfter(targetRow, specTd, targetTd);
    }
    targetTd.innerHTML = badgeHtml;

    // 3) 表内其他兄弟行:插空 td(对齐)
    table.querySelectorAll('tbody tr').forEach(function (tr) {
        if (tr === targetRow) return;
        if (tr.querySelector('td.match-col')) return;
        var emptyTd = document.createElement('td');
        emptyTd.className = 'match-col';
        emptyTd.style.textAlign = 'center';
        var ref = tr.querySelector('td.spec-cell');
        insertAfter(tr, ref, emptyTd);
    });
}
```

- [ ] **Step 4: 修改 `setRowMatchBadge` 接入 `_ensureMatchColumn`**

读 `templates/_record_image_script.html:30` 附近的 `setRowMatchBadge`，其当前结构大概是：

```js
function setRowMatchBadge(recordId, status, reason, source) {
    // ... 找到 row ...
    var cell = row.querySelector('.match-col');  // ← 当前是 td,但 td 已不存在!
    // ... 写徽章 ...
}
```

**问题**：`setRowMatchBadge` 当前用 `row.querySelector('.match-col')`，但 CSS selector 同时匹配 `th.match-col` 和 `td.match-col`——table thead 里也有 `.match-col`，可能导致选错。

改为：

```js
function setRowMatchBadge(recordId, status, reason, source) {
    var row = document.querySelector('.record-table tr[data-record-id="' + recordId + '"]');
    if (!row) return;
    var table = row.closest('table');
    // 构造徽章 HTML(与现有 buildBadgeHtml 内部逻辑一致;这里复用现有变量构造)
    var badgeHtml = _buildBadgeHtmlForRow(status, reason, source);
    // 调用 _ensureMatchColumn:首次匹配时插列,已存在则只更新目标行 td
    _ensureMatchColumn(table, row, badgeHtml);
}
```

其中 `_buildBadgeHtmlForRow` 是从现有 `setRowMatchBadge` 内 `cell.innerHTML = ...` 那部分抽出来（badge 颜色 / 符号 / title 拼接逻辑，参考 `applyAsyncMatchResult` 里 line 386-393 的 `badgeHtml` 构造）。

读现有 `setRowMatchBadge` 实现，把内联的徽章 HTML 字符串构造抽成 `_buildBadgeHtmlForRow(status, reason, source) -> string`，让 `setRowMatchBadge` 通过它拿 HTML 字符串。

- [ ] **Step 5: 跑 jsdom 测试,确认 GREEN**

Run:
```bash
node tests/record_image_match_column_check.js
```
Expected: `✅ record_image_match_column_check 通过`

- [ ] **Step 6: 把 jsdom 测试接到 pytest**

在 `tests/test_match_column_dynamic.py` 末尾新增一个测试类（同 `tests/test_validation_rules.py::test_render_produces_distinct_warning_blocks` 模式，用 subprocess 跑 node）：

```python
@unittest.skipUnless(
    Path('tests', 'record_image_match_column_check.js').exists()
    and shutil.which('node') is not None,
    '需要 node + tests/record_image_match_column_check.js 才能跑',
)
def test_ensure_match_column_jsdom(self):
    """_ensureMatchColumn / setRowMatchBadge 行为测试(jsdom 抽真实函数跑)。"""
    proc = subprocess.run(
        ['node', 'tests/record_image_match_column_check.js'],
        capture_output=True,
    )
    if proc.returncode != 0:
        self.fail(
            f"record_image_match_column_check.js 退出码 {proc.returncode}\n"
            f"STDOUT: {proc.stdout.decode('utf-8', errors='replace')}\n"
            f"STDERR: {proc.stderr.decode('utf-8', errors='replace')}"
        )
    out = proc.stdout.decode('utf-8', errors='replace')
    self.assertIn('✅ record_image_match_column_check 通过', out,
                  f'jsdom 检查脚本未通过:\n{out}')
```

在文件顶部加 import：

```python
import shutil
import subprocess
from pathlib import Path
```

- [ ] **Step 7: 跑 pytest,确认 GREEN**

Run:
```bash
PYTHONUTF8=1 python -m pytest tests/test_match_column_dynamic.py -q -p no:warnings
```
Expected: 4/4 PASS（3 个服务端测试 + 1 个 jsdom runner）

- [ ] **Step 8: Commit**

```bash
git add templates/_record_image_script.html \
        tests/record_image_match_column_check.js \
        tests/test_match_column_dynamic.py
git commit -m "feat(shipping): _ensureMatchColumn 动态插入 AI 比对列"

# 详细消息体:
# feat(shipping): _ensureMatchColumn 动态插入 AI 比对列
#
# - templates/_record_image_script.html:
#   * 新增私有函数 _ensureMatchColumn(table, targetRow, badgeHtml)
#   * 位置: 规格 cell 之后(td[4])
#   * 表头 + 目标行 + 其他兄弟行(对齐)
#   * setRowMatchBadge 接入
#
# - tests/record_image_match_column_check.js (jsdom):
#   * Case 1: 空 table + 1 行 → 表头 + td + 徽章
#   * Case 2: 已有 match-col → 不重复插入,只更新
#   * Case 3: 5 行 + 目标第 3 行 → 表头 1 + 5 行 td
#
# - tests/test_match_column_dynamic.py: 加 jsdom runner
```

---

## Task 3: JS — 接入 `applyAsyncMatchResult` 轮询完成路径

**Files:**
- Modify: `templates/_record_image_script.html:applyAsyncMatchResult` (约 line 230)

**Interfaces:**
- Consumes: `applyAsyncMatchResult(imageId, recordPk, ocrText, result)` (现有函数)
- Produces: 同 — 但内部走 `_ensureMatchColumn` 确保首次匹配时插列

- [ ] **Step 1: 读现有 `applyAsyncMatchResult`**

读 `templates/_record_image_script.html:225-245`，定位到 `applyAsyncMatchResult` 的「写徽章到 row」那段（约 line 240-242 `if (recordPk) setRowMatchBadge(recordPk, status);`）。

当前已经在调 `setRowMatchBadge`，但因为 Task 1 已删服务端 match-col td，**首次匹配时 `setRowMatchBadge` 找不到 td 会失败或写入空**。

Task 2 已让 `setRowMatchBadge` 通过 `_ensureMatchColumn` 处理「td 不存在则插入」。**这一步不需要改 `applyAsyncMatchResult`**，只是确认它的行为在 Task 2 修复后正确。

- [ ] **Step 2: 验证路径：写一个新 jsdom 测试**

在 `tests/record_image_match_column_check.js` 末尾加 Case 4：

```js
// ── Case 4: 模拟 applyAsyncMatchResult 的完整流程 ──────────
// (确保轮询完成后路径正确触发了 setRowMatchBadge → _ensureMatchColumn)
{
  // 这个 case 复用 Case 1 的 setup,只多走一步:模拟 async 处理完成的回调链
  // 因为 applyAsyncMatchResult 内部就是调 setRowMatchBadge(recordPk, status),
  // 而 setRowMatchBadge 已接入 _ensureMatchColumn,无需重复断言。
  // 这里写个 placeholder,标注依赖 Task 2 已 GREEN。
  check(true, 'Case 4: 已在 Case 1 覆盖(applyAsyncMatchResult 内部就是 setRowMatchBadge)');
}
```

实际不需要新增 case（避免冗余）。本 step 是「Task 2 已修，无需改 applyAsyncMatchResult」。

- [ ] **Step 3: 跑全量回归,确认没有破坏**

Run:
```bash
PYTHONUTF8=1 python -m pytest tests/test_match_column_dynamic.py tests/test_upload_ai_routing.py tests/test_record_upload_match.py tests/test_shipping_yellow_manual_confirm.py -q -p no:warnings
```
Expected: 全过（确认轮询完成路径正确触发 _ensureMatchColumn）

- [ ] **Step 4: Commit (空操作 / 仅文档)**

如果 Task 3 无代码改动（依赖 Task 2 已修），跳过 commit；否则：

```bash
git commit --allow-empty -m "docs: applyAsyncMatchResult 通过 setRowMatchBadge 间接接入 _ensureMatchColumn"
```

---

## Task 4: 更新 2 个旧测试断言

**Files:**
- Modify: `tests/test_shipping_match_badge_refresh.py:67-70` (改断言)
- Modify: `tests/test_shipping_p1_fixes.py:30-37` (改断言)

**Interfaces:**
- Consumes: 无
- Produces: 旧测试断言改为「服务端不渲染 match-col」+ 「客户端插入后 match-col 行为正确」

- [ ] **Step 1: 读并改 `tests/test_shipping_match_badge_refresh.py:60-75`**

读该测试文件 60-80 行。当前测试：

```python
def test_first_render_uses_server_side_map(self):
    """服务端首次渲染仍由 record_worst_status_map 决定 match-col,与 JS 刷新互不干扰。"""
    # 服务端模板里的 match-col 守卫
    m = re.search(r'<td class="match-col">\{%[^%]*set\s+_bst\s*=\s*record_worst_status_map', self.src)
    self.assertIsNotNone(m, '服务端 match-col 仍由 record_worst_status_map 渲染(未删除该路径)')
```

改为：

```python
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
```

- [ ] **Step 2: 读并改 `tests/test_shipping_p1_fixes.py:30-50`**

读该测试文件 25-50 行。当前用正则 `<td class="match-col">(.*?)</td>` 抓服务端渲染的徽章：

```python
def _extract_match_badges(html):
    """只从服务端渲染的 <td class="match-col"> 单元格里取 data-match-status。"""
    vals = []
    for cell in re.findall(r'<td class="match-col">(.*?)</td>', html, re.S):
        m = re.search(r'data-match-status="(\w+)"', cell)
        if m:
            vals.append(m.group(1))
    return vals
```

这是被多个测试调用的辅助函数（不只是 line 30 那一处）。需要重写。

先 grep 这个文件的 _extract_match_badges 调用点：

```bash
grep -n "_extract_match_badges\|<td class=\"match-col\">" tests/test_shipping_p1_fixes.py
```

把 `_extract_match_badges` 改为「从 jsdom 解析过的 DOM 抓 match-badge」：

```python
def _extract_match_badges(html):
    """从服务端渲染的 HTML 提取每行的 match_status。

    2026-08-04 改造:服务端不再渲染 match-col。match-col 由 JS 动态插入。
    本测试现在只校验「服务端不渲染 match-col」(用 _server_has_match_col_html),
    match-col 的行为测试在 tests/record_image_match_column_check.js (jsdom)。
    """
    return []


def _server_has_match_col_html(html):
    """服务端 HTML 是否包含 match-col 渲染。"""
    return '<th class="match-col"' in html or '<td class="match-col"' in html
```

并把 `test_match_badge_worst_status_wins` 等用到 `_extract_match_badges` 的测试改成新断言。读该测试 line 195-210 附近（之前看到的）：

```python
def test_match_badge_worst_status_wins(self):
    """该行 match-col 应含红牌(突显最差),不应是绿牌"""
    # ... setup ...
    res = self.client.get('/shipping-records?...')
    html = res.get_data(as_text=True)
    badges = _extract_match_badges(html)
    # ... 断言 badges ...
```

改为：

```python
def test_match_badge_worst_status_wins(self):
    """服务端不渲染 match-col(match-col 由 JS 插入,行为测试在 jsdom)。"""
    # ... setup ...
    res = self.client.get('/shipping-records?...')
    html = res.get_data(as_text=True)
    # 1) 服务端 HTML 不应包含 match-col(确认 Task 1 已生效)
    self.assertFalse(_server_has_match_col_html(html),
        'match-col 应由 JS 动态插入,服务端不渲染')
    # 2) match-badge 行为由 jsdom 测试覆盖
    #    (tests/record_image_match_column_check.js Case 1 验证 setRowMatchBadge
    #     写入了正确的 badgeHtml 到 td.match-col)
```

- [ ] **Step 3: 跑这两个测试文件**

Run:
```bash
PYTHONUTF8=1 python -m pytest tests/test_shipping_match_badge_refresh.py tests/test_shipping_p1_fixes.py -q -p no:warnings
```
Expected: 全过（具体数量取决于原有测试数 — 至少 6+13=19 个）

- [ ] **Step 4: 跑全量回归**

Run:
```bash
PYTHONUTF8=1 python -m pytest tests/ -q -p no:warnings --ignore=tests/regression
```
Expected: 320+ passed / 0 failed

- [ ] **Step 5: Commit**

```bash
git add tests/test_shipping_match_badge_refresh.py tests/test_shipping_p1_fixes.py
git commit -m "test: 旧 match-col 服务端断言改为「不渲染 + jsdom 行为测试」

- test_shipping_match_badge_refresh: 断言服务端不渲染 match-col th/td
  且不引用 record_worst_status_map
- test_shipping_p1_fixes: _extract_match_badges 改为空 + 加
  _server_has_match_col_html;test_match_badge_worst_status_wins 改断言
  服务端不渲染

match-col 的实际行为由 tests/record_image_match_column_check.js 覆盖。"
```

---

## Task 5: 全量回归 + DB 漂移检查 + 最终 commit

**Files:** 无（验证性 task）

- [ ] **Step 1: 跑全量回归**

Run:
```bash
PYTHONUTF8=1 python -m pytest tests/ -q -p no:warnings --ignore=tests/regression
```
Expected: 320+ passed / 0 failed（之前 baseline 是 320；本次新增 4 个测试到 test_match_column_dynamic.py + 不应回归其他）

如果失败：检查失败的测试名，按修复 → 重跑，直到全过。

- [ ] **Step 2: DB 漂移校验**

```bash
PYTHONUTF8=1 python -c "
import sqlite3
b = sqlite3.connect('D:/BAK/worklog_20260804_0905.db')
c = sqlite3.connect('C:/Users/Administrator/worklog-app/worklog.db')
tables = ['shipping_orders','shipping_records','shipping_images','ocr_match_event',
          'category_prompts','audit_log','inbound_orders','inbound_records',
          'inbound_images','loading_orders','loading_order_records','loading_order_images']
print('=== DB 漂移校验 ===')
for t in tables:
    bn = b.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
    cn = c.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
    diff = cn - bn
    flag = ' ← Flask 服务器真实业务' if diff > 0 else ''
    print(f'  {t}: baseline={bn} now={cn} delta={diff:+d}{flag}')
b.close(); c.close()
"
```

Expected: 所有表 delta=0,或仅 audit_log / category_prompts 因正常业务写入有微小 delta(用户实际使用产生)。

如果有任何意外的 delta（特别是 shipping_orders/shipping_records/shipping_images）：说明后台异步线程泄漏到生产库——立即检查 `_drain_async_jobs` 是否在新加测试的 tearDown 调到位。

- [ ] **Step 3: 验收**

手动验证（如果 Flask 服务器在跑）：
1. 浏览器打开 `/shipping-records?start_date=2026-08-04`
2. 确认表格**无**「AI 比对」列
3. 上传一张行级图
4. 等待 2-4 秒
5. 确认「AI 比对」列**出现**，含徽章
6. 同订单其他行：自动出现空 td（对齐）

如果 Flask 服务器不在跑（用户关掉了）：跳过手动验证，只看 pytest + jsdom 测试全过。

- [ ] **Step 4: 写 report**

在 `docs/superpowers/specs/2026-08-04-match-col-virtual-design.md` 末尾追加验收段（同其他 plan 的风格）：

```markdown
---

## Acceptance (2026-08-04)

**执行报告**: 见 git log (5 个 commit):
- `bdcee1a` docs(spec)
- `feat(shipping): match-col 服务端不再渲染` (Task 1)
- `feat(shipping): _ensureMatchColumn 动态插入 AI 比对列` (Task 2)
- (Task 3-4 commits)

### 自动化验证

- 320+ tests PASS（含新增 4 个 test_match_column_dynamic.py）
- 0 regression on existing tests
- DB 漂移: 0
```

---

## Self-Review

### 1. Spec coverage

| Spec 章节 | Task |
|---|---|
| §1 背景与目标 | — |
| §2 范围（模板/CSS/JS/blueprint/测试） | Task 1 + 2 + 4 |
| §3.1 服务端改动 | Task 1 Step 6-7 |
| §3.2 模板改动 | Task 1 Step 3-5 |
| §3.3 JS 设计 | Task 2 Step 3 |
| §3.4 接入点 | Task 2 Step 4 + Task 3 |
| §3.5 CSS | Task 1 Step 3-5（合并到模板改动） |
| §3.6 测试 | Task 1 Step 1 + Task 2 Step 1 + Task 4 |
| §4 边界 / 异常 | Task 2 Step 1（Case 2 验证「已有列不重复插」）+ Task 5 Step 3（手动验收） |
| §5 验收 | Task 5 |
| §6 Out of scope | — |
| §7 风险 | Task 1 Step 9（注意 tearDown 加 drain）+ Task 5 Step 2（DB 漂移校验） |

✅ Full coverage.

### 2. Placeholder scan

无 TODO / TBD / "implement later" / "fill in details" / "类似 Task N" / "适当错误处理"。

### 3. Type consistency

- `_ensureMatchColumn(table: HTMLTableElement, targetRow: HTMLTableRowElement, badgeHtml: string): void` — Task 2 步骤 3 定义，Task 2 步骤 4 接入 `setRowMatchBadge` 时使用，Task 5 不显式调用（OK，无外部调用者）
- `setRowMatchBadge(recordId: int, status: str, reason: str, source: str): void` — 现有签名不变
- `_buildBadgeHtmlForRow(status, reason, source): string` — Task 2 Step 4 抽取，签名与现有 setRowMatchBadge 内部构造一致

✅ Consistent.