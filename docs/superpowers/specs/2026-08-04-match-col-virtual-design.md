# match-col 虚拟列重构 — 设计文档

- 日期：2026-08-04
- 页面：`/shipping-records`、`/inbound-records`、`/loading-orders` 三套订单表
- 状态：待用户 review

## 1. 背景与目标

### 现状（2026-08-04 前）

出货/入库/装柜三套订单表的明细行都有一列「AI 比对」(`<th class="match-col">` + `<td class="match-col">`)：
- 服务端用 `record_worst_status_map` 在每个 `<td class="match-col">` 里渲染徽章
- 表格初始 class 带 `no-match`（如 `group.has_match = false`），CSS `.no-match .match-col { display: none; }` 隐藏整列
- 一旦任一 record 有匹配结果，服务端下次渲染去掉 `no-match`，列头出现

### 问题

**行级图上传触发匹配后，列不会自动出现——只有刷新页面才看到。**

具体路径：
1. 用户点 🖼️ 按钮 → 上传图 → 后端返回 `processing: true`（异步处理）
2. 用户立即看到图被加到下方图区，**但行表的 AI 比对列仍然是隐藏的**（`table.no-match` 仍存在）
3. 后台线程跑完 → `/images/<id>/match-status` 轮询拿结果 → JS `applyAsyncMatchResult` 调 `setRowMatchBadge`
4. `setRowMatchBadge` 把徽章写进 `<td class="match-col">`，但因为表是 `no-match`，CSS 把 td 也隐藏了
5. 用户看到图、看不到徽章 → 体验断

同一问题也影响「整单 ai-match」端点（`POST /shipping-orders/<id>/ai-match`）：返回后 JS 端刷新徽章，但 `no-match` 仍然罩着。

### 目标

把 AI 比对列**真正做成虚拟列**：
- 服务端**从不**渲染 `<th class="match-col">` 或 `<td class="match-col">`
- JS 在**首次有匹配结果**时动态插列：
  - 表头插 `<th class="match-col">AI 比对</th>`（每个 table 一次）
  - 目标行插带徽章的 `<td class="match-col">`
  - 表内**所有其他行**插空 `<td class="match-col">`（保持列对齐，否则表格错位）
- 不再有 `.no-match` 类和 `.no-match .match-col { display: none }` 规则

## 2. 范围

### 在范围内

| 文件 | 改动 |
|---|---|
| `templates/shipping-records.html` | 删 `<th class="match-col">`、`<td class="match-col">`、`no-match` class |
| `templates/inbound-records.html` | 同上 + 删 `.no-match .match-col { display: none }` 规则 |
| `templates/loading-orders.html` | 同上 + 删 `.no-match .match-col { display: none }` 规则 |
| `templates/_record_image_script.html` | 新增 `_ensureMatchColumn(table, targetRow, badgeHtml)` 私有函数；`setRowMatchBadge` 和 `applyAsyncMatchResult` 调用它 |
| `blueprints/shipping.py` | 删 `record_worst_status_map` / `record_worst_reason_map` / `record_worst_source_map` / `record_human_verified_map` 字典构造；删 `group['has_match']`；删上下文传参 |
| `blueprints/inbound.py` | 删 `record_worst_status_map` + `grp['has_match']` + 上下文传参（loading 不算这些，可不动） |
| `templates/_smart_add_modal.html` | **保留** `<td class="match-col"></td>` 占位（AI 识别预览弹框行还没入库就要有列结构） |
| `static/css/app.css` | 不需改（`.match-col` 选择器本身仍有用） |
| `tests/test_shipping_match_badge_refresh.py` | 改 1 个断言：「服务端**不**渲染 match-col」 |
| `tests/test_shipping_p1_fixes.py` | 改 1 个断言：match_badge_worst_status 改测客户端插入后的 DOM |
| `tests/test_match_column_dynamic.py` | **新增**：服务端首次 GET 无 match-col + JS 插入后列出现 |
| `tests/record_image_match_column_check.js` | **新增**：jsdom 行为测试，验证 `_ensureMatchColumn` 在三种状态下行为 |

### 不在范围

- 入库/装柜的 AI 引擎接入（inbound/loading 仍走本地 RapidFuzz，不触发 match-col 插入路径；本次只动渲染结构）
- `record_worst_status_map` 在其他场景的使用（如 audit 页 / 详情页）——若有，调出范围
- 行级图片上传以外的入口（手动新增明细行 / 编辑规格）——这些操作不产生匹配结果，不触发列插入

## 3. 设计

### 3.1 服务端（shipping.py / inbound.py）

**`shipping.py` 第 271-298 行附近**：
- 删 `record_worst_status_map = {}`、`record_worst_reason_map = {}`、`record_worst_source_map = {}`、`record_human_verified_map = {}`
- 删填充这 4 个字典的循环（约 30 行）
- 删 `group['has_match'] = any(...)`（第 381 行附近）
- 删第 395 行附近的 4 个 `map=...` 上下文传参

**`inbound.py` 第 217-256 行附近**：同上结构（`record_worst_status_map` + `has_match` + 上下文传参）。

**不动的部分**：所有 record_worst_* 数据的原始来源（图片 match_status/match_source 本身仍在 DB），只是不再主动聚合到 Jinja 上下文。

### 3.2 模板（三套订单）

**`shipping-records.html`**：
- 第 460 行附近：删 `<th class="match-col" style="width:55px;">AI 比对</th>`
- 第 478 行附近：删整行 `<td class="match-col">{% set _bst = ... %}{% if _bst == 'green' %}...{% endif %}</td>`
- 第 453 行附近：`<table class="record-table{% if not has_eco and not has_jia_mian %} no-eco{% endif %}{% if not has_match %} no-match{% endif %}">` → 删 `{% if not has_match %} no-match{% endif %}`
- 删 group 内 `has_match` 的 set 声明（第 451 行附近）

**`inbound-records.html`**：
- 第 499 行：`<table class="record-table{% if not group.has_match %} no-match{% endif %}">` → 删 no-match
- 第 508 行：删 `<th class="match-col" ...>AI 比对</th>`
- 第 524 行：删 `<td class="match-col" style="text-align:center;"></td>`
- 第 302 行：删 `.inbound-page .record-table.no-match .match-col { display: none; }`

**`loading-orders.html`**：
- 第 236 行：删 `<th class="match-col" ...>`
- 第 255 行：删 `<td class="match-col" style="text-align:center;"></td>`
- 第 99 行：删 `.loading-page .record-table.no-match .match-col { display: none; }`
- 第 892、933、977、1129 行附近的 `'<td class="match-col" ...>...</td>'` JS 字符串：删除（这些是 JS 动态创建明细行的代码，不该再插入 match-col）

### 3.3 JS — `_ensureMatchColumn` 设计

新私有函数加在 `_record_image_script.html`：

```js
// 13. _ensureMatchColumn — 在 table 内动态插入 AI 比对列（首次有匹配结果时）
// 位置: 规格 cell 之后（td[4]）。
// 表头 + 目标行 + 其他兄弟行（保持对齐）。
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

### 3.4 接入点

**`setRowMatchBadge(recordPk, status, reason, source)`（约 line 30）**：在现有「写徽章到 td」逻辑前先调 `_ensureMatchColumn(table, targetRow, badgeHtml)`。这样 setRowMatchBadge 的 caller（包括 JS 路径里所有刷新点）都会自动触发列插入。

**`applyAsyncMatchResult`（约 line 230，轮询完成后）**：在写徽章前调 `_ensureMatchColumn`。这是异步上传的核心路径——**正是用户当前痛点**。

**`refreshRowMatchBadge`（约 line 47）**：保留现有逻辑（基于已有 td 重算）。它**不**插入新 td——只刷新现有徽章。这是设计意图：refresh 是「更新」不是「创建」，无匹配时不应触发插入。

### 3.5 CSS

不动 `.match-col { text-align: center; padding: ... }` 本身。
删 `.no-match .match-col { display: none; }` 三处（每个模板各 1 处）。

### 3.6 测试

**改 2 个：**

- `tests/test_shipping_match_badge_refresh.py:67-70`：当前断言「服务端 match-col 仍由 record_worst_status_map 渲染」。改成断言「服务端**不**渲染 match-col th/td」。docstring 也要更新。

- `tests/test_shipping_p1_fixes.py:30-37`：当前用正则 `<td class="match-col">(.*?)</td>` 抓服务端渲染的徽章。改成两步：
  1. 先断言服务端 HTML 里**无** `<td class="match-col">`
  2. 再用 jsdom 模拟 `_ensureMatchColumn` 调用，断言新生成的 DOM 含 data-match-status

**新增 2 个：**

- `tests/test_match_column_dynamic.py`（pytest）：
  - `test_first_render_has_no_match_column`：GET `/shipping-records` 渲染的 HTML 不含 `match-col`
  - `test_first_render_has_no_match_column_inbound`：同上对 inbound
  - `test_first_render_has_no_match_column_loading`：同上对 loading

- `tests/record_image_match_column_check.js`（jsdom）：
  - 抽 `_ensureMatchColumn` + `setRowMatchBadge`
  - Case 1：空 table + 1 行 → 调一次 → 表头出现 th、所有 tr 有 td、目标行 td 含徽章
  - Case 2：已有 match-col 的 table → 调一次 → 只更新目标行 td，不重复插列头 / 不重复插空 td
  - Case 3：5 行 table + 目标为第 3 行 → 调一次 → 表头 1 个 th、所有 5 行有 td

由 `tests/test_match_column_dynamic.py` 通过 subprocess 调用（同 `test_validation_rules.py::test_render_produces_distinct_warning_blocks` 模式）。

## 4. 边界 / 异常

| 场景 | 行为 |
|---|---|
| 首次上传图 → 异步处理中（无 match_status） | 不插入列；徽章位空 |
| 异步完成 → match_status='green' | 调 `setRowMatchBadge` → 插入列 + 绿徽章 |
| 整单 ai-match → 一行 red | 调 `setRowMatchBadge` → 插入列 + 红徽章 |
| 手动 manual-verify → 👤 已确认 | 走 `refreshRowMatchBadge`，列已存在就刷新；列不存在则不做事（无人点击过 match-col 不会冒出来） |
| 删除最后一张图 → human_verified 撤销 → 重新 red | 列已存在，`refreshRowMatchBadge` 刷内容 |
| 用户手动 reload 页面 | 服务端不渲染 → 列不出现。**接受 trade-off**：reload 通常是主动行为，用户期待看到真实初始状态 |
| Smart-add 弹框预览行 | 弹框里仍渲染空 `<td class="match-col"></td>` 占位（行还没入库就要有表格结构） |
| 行级图属于共享图（record_pk = NULL） | `setRowMatchBadge` 不被调用（路径不进入），列不出现。OK |

## 5. 验收

- 全量测试 320 passed / 0 failed + 新增 4+ 测试全过
- 手动验证：
  1. `/shipping-records` 首次加载：表格**无**「AI 比对」列
  2. 上传一张行级图到任一明细行：等待 2-4s → 该行表格「AI 比对」列**出现**，且含徽章
  3. 同一订单其他行：自动出现空 td（列对齐）
  4. 列头位置：在「规格」之后、「数量」之前
  5. 刷新页面：列消失（符合 trade-off）

## 6. Out of scope

- 不动 `.no-eco` / `.jia-mian-icon`（与本次无关）
- 不动 `recordImageUploaded` 里「读 product_name / spec / has-image 红框」逻辑（之前已修）
- 不动 `record_worst_*_map` 在 audit 页 / 详情页的潜在使用——本次只删 Jinja 上下文传参
- 不改 inbound/loading 的 AI 引擎逻辑（本次只动渲染结构）
- 不引入新依赖
- 不优化列宽 CSS（已存在 width:55px 直接复用）

## 7. 风险

- **测试 fixture 期望变化**：`tests/test_shipping_match_badge_refresh.py` 和 `tests/test_shipping_p1_fixes.py` 改断言可能引入新 bug——必须先红后绿。
- **Smart-add 弹框一致性**：用户可能期望弹框也跟主表一致（也无 match-col），但弹框行**没有**图，删了会让弹框表格列宽乱。**保守保留占位**。
- **多 table 隔离**：每个订单一个 `.record-table`。`_ensureMatchColumn` 必须在正确的 table 内操作——通过 `targetRow.closest('table')` 限定。

---

## Acceptance (2026-08-04)

**执行报告**: 4 个 commit（145b405 / 1408810 / 24c9ae6 / d6dd4ad），详见 `.superpowers/sdd/2026-08-04-match-col-virtual/`。

### 自动化验证

- 324 tests PASS（含 4 个新增 `tests/test_match_column_dynamic.py`）
- 0 regression on existing tests
- DB 漂移: 0

### 待跟进（非阻塞）

- StaffDB 连接泄漏（models/tasks_flow.py:132/142）—— Task 4 reviewer 标 Minor M1，另开 task 修
- `_extract_match_badges` no-op 占位 —— Task 4 reviewer 标 Minor M2
- `_server_has_match_col_html` regex 对 `<script type="module">` self-closing 失效风险 —— Task 4 reviewer 标 Minor M3
