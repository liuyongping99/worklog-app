# 入库对齐出货 — 设计文档

- 日期：2026-08-13
- 页面：`/inbound-records`（主）、`/loading-orders`（仅列数持久化 bug 一项）
- 状态：待用户 review

## 1. 背景与目标

### 现状

`shipping-records.html`（出货）作为商品行功能最丰富的基准页，已经历多轮演进：

- 行级图片 + AI 比对（`match-col` 虚拟列由 JS 动态插）
- 加面图标（重点列 + SVG `jia-mian-icon`）
- 环保行底色（`row-eco`）
- 数量异常行底色（`row-out-of-stock` + `qty-badge`）
- 备注校验 `mismatch` warn/info（单支粉/多支红）
- 备注汇总行两列统计（📊 备注支数 + 📊 明细支数）
- `verified_warnings` 折叠按钮（每条警告可 ✓/✗ 手动确认）
- `source_tag` 字段（移动端备货照/装车照/归仓照）
- 图片列数持久化（`img_cols` 存订单表，每订单独立记忆）

`inbound-records.html`（入库）和 `loading-orders.html`（装柜）在历史演进中**只同步了部分功能**（见 2026-07 月 commit `65e8d9f feat(shipping): 行级图 OCR 异步化 + JS 抽取共享模板 + 入库/装柜同步`），共享模板 `_record_image_script.html` 让三套订单共用行级图片/AI 比对基础能力。

但**出货独有的视觉/交互功能**（加面/环保/缺货/警告按钮/source_tag/AI 比对图例）未在入库/装柜上实现。

### 对齐现状（经本次 spec 自查实测代码修正）

> 注意：初始子 agent 报告误判「入库列数持久化 ✅ DB」「装柜 PATCH 无分支」。经逐行查证源码，实际状态如下表。

| 功能 | 出货 | 入库 | 装柜 |
|---|---|---|---|
| 加面图标（重点列） | ✅ | ❌ | ❌ |
| 环保行底色（row-eco） | ✅ | ❌ | ❌ |
| 缺货行底色（row-out-of-stock） | ✅ | ❌ | ❌ |
| 行级图片 has-image 红边框 | ✅ | ⚠️ 模板有 class 无 CSS | ⚠️ 同 |
| AI 比对图例条 | ✅ | ❌ | ❌ |
| verified_warnings 折叠按钮 | ✅ | ⚠️ 模板引用字段但后端没算 | ⚠️ 同 |
| source_tag 字段 | ✅ | ❌（DB 无列） | ❌ |
| 编辑保存"码→y"归一化 | 下拉框物理阻断 | ✅ 已有 JS 归一化 | ✅ 已有 |
| 备注汇总行 / 辅助单位提示 / mismatch | ✅ | ✅ | ✅ |
| 图片列数持久化 | ✅ DB | ❌ **无列 + 无 PATCH 分支 + 无 set_img_cols** | ❌ **无列；set_img_cols + PATCH 分支已存在，但缺列导致 500，前端走 cookie 规避** |

### 目标

1. **入库 `inbound-records.html` 与出货 `shipping-records.html` 像素级一致**——9 项视觉/交互功能对齐（含列数持久化）
2. **修装柜列数持久化 bug**——让 `loading-orders.html` 的列数切换持久化到 DB（与出货/入库一致）

### 不在范围

- 出货 `shipping-records.html` 不动（基准）
- 装柜的其他商品行功能（加面/环保/缺货/警告按钮/source_tag）**不做**（Q1=4）
- 移动端 `mobile_shipping.py` 不动
- `_record_image_script.html` 共享模板**不动**（三套订单共用部分已对齐）
- 后端 AI 比对引擎接入（入库/装柜仍走本地 RapidFuzz，不触发 match-col 插入路径）

## 2. 范围

### 入库范围（9 项）

| # | 功能 | 出货参照 | 入库补齐内容 |
|---|---|---|---|
| 1 | 加面图标（重点列） | `shipping.py:415` `has_jia_mian`；`shipping-records.html:30,495,512` | 后端算 + 重点列 + SVG jia-mian-icon + CSS |
| 2 | 环保行底色（row-eco） | `shipping.py:414` `has_eco`；`shipping-records.html:24-27,489,507,512` | 后端算 + row-eco 类 + CSS + eco-icon |
| 3 | 缺货行底色（row-out-of-stock） | `shipping.py:394-396` `qty_invalid`；`shipping-records.html:18-22,507,511` | 后端算 + row-out-of-stock 类 + ❌ + qty-badge |
| 4 | 行级图片 has-image 红边框 | `shipping-records.html:144-146`（CSS） | CSS 补 `.record-image-btn.has-image { border: 2px solid #dc2626; }` |
| 5 | AI 比对图例条 | `shipping-records.html:302-330` | 复制图例条 |
| 6 | 编辑保存"码→y"归一化 | 下拉框物理阻断 | 入库已有（`inbound-records.html:1156,1455`），不动 |
| 7 | verified_warnings 折叠按钮 | `shipping.py:412` `verified_warnings`；`shipping-records.html:507-510,961-986` | 后端算 `verified_warnings` + 模板渲染 ✓/✗ 按钮 + JS 交互 |
| 8 | source_tag 字段 | `shipping.py:799-802`；DB `shipping_images.source_tag` | DB 加 `inbound_images.source_tag` + blueprint 入参 + 模板渲染 |
| 9 | 图片列数持久化 | `shipping_orders.img_cols` + `ShippingOrder.set_img_cols` + `shipping.py:493-501` PATCH 分支 | DB 加 `inbound_orders.img_cols` + `InboundOrder.set_img_cols` + `inbound.py` PATCH 分支 |

### 装柜范围（仅 1 项突破）

| # | 功能 | 现状（实测） | 改动 |
|---|---|---|---|
| 10 | 列数持久化 bug | `loading_orders` 无 `img_cols` 列；`LoadingOrder.set_img_cols`(orders.py:1511) + `loading.py:269-277` PATCH 分支已存在但缺列会 500；渲染走 cookie(`loading.py:160`)；前端走 cookie/localStorage | DB 加列 + 渲染改 DB + 前端改共享 setImgCols |

## 3. 设计

### 3.1 加面图标（功能 1）

**后端 `blueprints/inbound.py`**：

在 `for group in groups` 循环里，复制 `shipping.py:414-419` 的逻辑：

```python
group['has_eco'] = any('环保' in r.get('product_name', '') for r in group['records'])
group['has_jia_mian'] = any(
    '杂胶' in r.get('product_name', '')
    and '加面' in r.get('specification', '')
    for r in group['records']
)
```

**模板 `templates/inbound-records.html`**：

1. 表头新增「重点」列（与出货一致）：

```html
{% set has_eco = group.has_eco|default(false) %}
{% set has_jia_mian = group.has_jia_mian|default(false) %}
<table class="record-table{% if not has_eco and not has_jia_mian %} no-eco{% endif %}">
    <thead>
        <tr>
            <th class="eco-col" style="width:35px;">重点</th>
            ...其余列同出货...
```

2. 行内 eco-icon / jia-mian-icon SVG（从 `shipping-records.html:512` 复制完整 SVG，含 `<defs><pattern>` 网状 mesh，实施时逐字复制）：

```html
{% if '环保' in record.product_name %}<span class="eco-icon" title="环保产品">🌿</span>{% endif %}
{% if '杂胶' in record.product_name and '加面' in (record.specification or '') %}
    <svg class="jia-mian-icon" viewBox="0 0 16 16" width="14" height="14">…（完整复制出货 SVG）…</svg>
{% endif %}
```

**CSS**（`templates/inbound-records.html` 内联 `<style>` 块末尾新增，与出货完全相同）：

```css
.inbound-page .jia-mian-icon { display: inline-block; color: #868e96; vertical-align: middle; line-height: 0; }
.inbound-page .eco-icon { color: #2d8c4a; margin-right: 0.25rem; font-size: 0.95rem; vertical-align: middle; }
.inbound-page .eco-icon + .jia-mian-icon { margin-left: 0.15rem; }
.inbound-page .record-table .eco-col { text-align: center; padding: 0.25rem; }
.inbound-page .record-table.no-eco .eco-col { display: none; }
.inbound-page .record-table tr.row-eco:not(.row-out-of-stock):not(.row-warn):not(.row-info) td { background-color: #f0f9f3; }
.inbound-page .record-table tr.row-eco:not(.row-out-of-stock):not(.row-warn):not(.row-info):hover td { background-color: #e3f5e8; }
.inbound-page .record-table tr.row-eco td:first-child { border-left-color: #2d8c4a; }
```

### 3.2 环保行底色（功能 2）

**后端**：同 3.1，`has_eco` 计算。

**模板**：

行 `<tr>` 的 class 加上 `' row-eco'`（仅当品名含「环保」）：

```html
<tr class="{% if record.qty_invalid %}row-out-of-stock{% elif record.mismatch == 'warn' or record.piece_mismatch == 'warn' %}row-warn{% elif record.mismatch == 'info' or record.piece_mismatch == 'info' %}row-info{% endif %}{% if '环保' in record.product_name %} row-eco{% endif %}">
```

**CSS**：见 3.1。

### 3.3 缺货行底色（功能 3）

**后端 `blueprints/inbound.py`**：

在 `for group in groups` 循环里，给每条 record 加 `qty_invalid`：

```python
qty_raw = (item.get('quantity') or '').strip()
try:
    float(qty_raw)
    item['qty_invalid'] = False
except (TypeError, ValueError):
    item['qty_invalid'] = True
```

**模板**：

1. 行 class 增加 `row-out-of-stock`（当 `qty_invalid`）：

```html
<tr class="{% if record.qty_invalid %}row-out-of-stock{% elif ... %}...{% endif %}">
```

2. 序号 cell 加 ❌ stock-icon：

```html
<td style="text-align:center; color:#868e96; font-size:0.85rem;">
    {% if record.qty_invalid %}<span class="stock-icon" title="数量异常">❌</span>{% endif %}
    {{ loop.index }}
</td>
```

**CSS**（与出货完全相同）：

```css
.inbound-page .record-table tr.row-out-of-stock td { background: var(--color-out-of-stock-bg); }
.inbound-page .record-table tr.row-out-of-stock td:first-child { border-left-color: var(--color-out-of-stock); }
.inbound-page .record-table tr.row-out-of-stock:hover td { background: #ffcccc; }
.inbound-page .record-table tr.row-out-of-stock .stock-icon { color: var(--color-out-of-stock); font-size: var(--font-md); margin-right: 2px; vertical-align: middle; }
.inbound-page .record-table tr.row-out-of-stock .qty-badge { background: #fab1a0; color: var(--color-out-of-stock); }
```

### 3.4 行级图片 has-image 红边框（功能 4）

**CSS**（最小改动）：

```css
.inbound-page .record-table .record-image-btn.has-image {
    border: 2px solid #dc2626;
}
```

模板 `<button class="record-image-btn has-image">` 已经有 class（行 526），无需改。

### 3.5 AI 比对图例条（功能 5）

**模板**：从 `shipping-records.html:302-330` 复制图例条 div 到 `inbound-records.html` 合适位置（如页面底部表格区域上方）：

```html
<div class="match-legend" style="margin: var(--space-md) 0; padding: var(--space-sm); background: #f8f9fa; border-radius: 6px; font-size: 0.85rem;">
    <strong>AI 比对图例：</strong>
    <span class="match-badge" style="color:#16a34a;">✓</span> 图文相符
    <span class="match-badge" style="color:#d97706;">⚠</span> 存疑
    <span class="match-badge" style="color:#dc2626;">✗</span> 可能不符
    <span style="margin-left: 1rem;">
        <span class="match-badge match-badge-deepseek" data-match-status="green" style="color:#0d9488;">⊛</span> 云端 DeepSeek
    </span>
    <span class="match-badge" style="color:#10b981;">👤</span> 人工已确认
</div>
```

### 3.6 编辑保存"码→y"归一化（功能 6）

入库已有（`inbound-records.html:1156,1455`）。不动。

### 3.7 verified_warnings 折叠按钮（功能 7）

**后端 `blueprints/inbound.py`**：

参考出货 `shipping.py:412` `verified_warnings` 计算逻辑。新增 `InboundRecord.get_verified_warnings(record_id)` 方法（参考出货 `ShippingRecord.get_verified_warnings`，`models/orders.py:443`）：

```python
# 在 for group in groups 循环里
for item in group['records']:
    ...  # 已有 unit_hint / mismatch 等计算
    item['verified_warnings'] = InboundRecord.get_verified_warnings(item['id'])
```

**模型 `models/orders.py`**：

新增 `InboundRecord.get_verified_warnings(record_id)` / `InboundRecord.set_verified_warning(record_id, rule_id, verified)` 静态方法（参考 `ShippingRecord` 同款方法，`models/orders.py:443-456`）。底层复用 `_verified_warnings_get` / `_verified_warnings_set` 辅助函数，改表名为 `inbound_records`。

**模板 `templates/inbound-records.html`**：

1. 行已引用 `data-verified-warnings="{{ record.verified_warnings or '{}' }}"`（行 513），保留。
2. 在行下方警告区渲染 ✓/✗ 按钮（参考出货 `shipping-records.html:961-986`）。
3. JS 端需要：
   - `addRowWarning` / `removeRowWarning` 函数（出货有，入库通过 `_record_image_script.html` 共享，应已可用）
   - 已核查 toggle 按钮（共享模板 `_smart_add_modal.html` 已有 `markRecordVerified` / `unmarkRecordVerified`）

**API 端点**：

参考出货的 `/api/v1/shipping-orders/records/<id>/verify-warning`（或类似），在入库蓝图加对应端点 `POST /api/v1/inbound-orders/records/<id>/verify-warning`。

### 3.8 source_tag 字段（功能 8）

**DB 迁移 `models/_init.py`**：

在 `init_db()` 加迁移分支（幂等）：

```python
# 给 inbound_images 加 source_tag 列（白名单：备货照/装车照/归仓照）
try:
    cursor.execute("ALTER TABLE inbound_images ADD COLUMN source_tag TEXT")
except Exception:
    pass  # 列已存在
```

**模型 `models/orders.py`**：

`InboundImage` 类加 `source_tag` 字段：

- `create()` / `update()` 方法接受 `source_tag` 参数
- `get_all_by_orders()` 等查询方法返回字典时包含 `source_tag`

**后端 `blueprints/inbound.py`**：

行级图片上传端点 `POST /api/v1/inbound-orders/records/<id>/images` 接受 `source_tag` 入参：

```python
source_tag = data.get('source_tag') if request.is_json else request.form.get('source_tag')
_VALID_SOURCE_TAGS = {'备货照', '装车照', '归仓照'}
if source_tag not in _VALID_SOURCE_TAGS:
    source_tag = None  # 静默忽略非法值
```

调 `InboundImage.create(..., source_tag=source_tag)` 落库。

订单级图片上传端点 `POST /api/v1/inbound-orders/<id>/images` 同样接受。

**模板 `templates/inbound-records.html`**：

在 `.img-item-record` 渲染时（如适用），显示 source_tag 标签：

```html
{% if image.source_tag %}
    <span class="source-tag-badge" style="background:#7c3aed;color:#fff;padding:2px 6px;border-radius:4px;font-size:0.7rem;">
        📷 {{ image.source_tag }}
    </span>
{% endif %}
```

### 3.9 图片列数持久化（功能 9，入库）

> `img_cols` 存**订单表**（`shipping_orders` / `inbound_orders` / `loading_orders`），不是图片表。

**DB 迁移 `models/_init.py`**：

```python
# 给 inbound_orders 加 img_cols 列（与 shipping_orders 一致）
try:
    cursor.execute("ALTER TABLE inbound_orders ADD COLUMN img_cols INTEGER NOT NULL DEFAULT 5")
except Exception:
    pass
```

**模型 `models/orders.py`**：

新增 `InboundOrder.set_img_cols(order_id, cols)` 静态方法（参照 `ShippingOrder.set_img_cols`，`models/orders.py:194-197`）：

```python
@staticmethod
def set_img_cols(order_id: int, cols: int):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('UPDATE inbound_orders SET img_cols = ? WHERE id = ?', (cols, order_id))
    conn.commit()
    conn.close()
```

**后端 `blueprints/inbound.py`**：

PATCH 端点 `/api/v1/inbound-orders/<id>`（`inbound.py:289-348`）加 `img_cols` 分支（参照 `shipping.py:493-501`）：

```python
if 'img_cols' in data:
    try:
        cols = int(data['img_cols'])
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': '列数必须是整数'}), 400
    if cols < 1 or cols > 5:
        return jsonify({'success': False, 'error': '列数范围为1-5'}), 400
    InboundOrder.set_img_cols(order_id, cols)
    return jsonify({'success': True, 'img_cols': cols})
```

**模板 `templates/inbound-records.html`**：

无需改（模板已用 `group.img_cols | default(5)` + 共享 `setImgCols` fetch PATCH）。加列后 `SELECT * FROM inbound_orders` 自动带出 `img_cols` 键。

### 3.10 装柜列数持久化（功能 10）

> 实测：`LoadingOrder.set_img_cols`(`orders.py:1511`) + `loading.py:269-277` PATCH 分支**已存在**，缺的只有 DB 列 + 渲染/前端走 cookie。

**DB 迁移 `models/_init.py`**：

```python
# 给 loading_orders 加 img_cols 列（与 shipping_orders 一致）
try:
    cursor.execute("ALTER TABLE loading_orders ADD COLUMN img_cols INTEGER NOT NULL DEFAULT 3")
except Exception:
    pass
```

**后端 `blueprints/loading.py`**：

渲染函数 `loading.py:160` 从 cookie 改为 DB：

```python
# 原：current_img_cols = request.cookies.get('loadingImgCols', '3')
# 新：不再需要 current_img_cols，模板直接用 group.img_cols
```

（`LoadingOrder.set_img_cols` + PATCH 分支已存在，无需新增。）

**模板 `templates/loading-orders.html`**：

1. 图片区 `cols-{{ current_img_cols | default('3') }}` 改为 `cols-{{ group.img_cols | default(3) }}`（行 309）
2. 删自定义 cookie 版 `setImgCols`（行 1028-1045），改用共享 `_record_image_script.html` 的 `setImgCols`（fetch PATCH `/api/v1/loading-orders/<id>` `{img_cols: cols}`）
3. 前端动态插入图片区时（行 745-749, 1274-1278）用 `group.img_cols` 替代 `savedCols`（cookie 值）

## 4. 文件变更清单

| 文件 | 改动 |
|---|---|
| `blueprints/inbound.py` | group 加 `has_jia_mian` / `has_eco`；item 加 `qty_invalid` / `verified_warnings`；图片上传接受 `source_tag`；PATCH 端点加 `img_cols` 分支 |
| `blueprints/loading.py` | 渲染函数从 cookie 改为 `group.img_cols`（`loading.py:160`） |
| `templates/inbound-records.html` | ①重点列 + eco-icon + jia-mian-icon SVG；② row-eco / row-out-of-stock 行类；③ ❌ stock-icon + qty-badge；④ AI 比对图例条；⑤ verified_warnings 按钮；⑥ source_tag 标签渲染 |
| `templates/inbound-records.html` 内联 CSS | 补 `.jia-mian-icon` / `.row-eco` / `.row-out-of-stock` / `.qty-badge` / `.record-image-btn.has-image` / `.eco-icon` 样式 |
| `templates/loading-orders.html` | 图片区 `group.img_cols`；删自定义 cookie setImgCols，改用共享 setImgCols |
| `models/_init.py` | DB 迁移：`inbound_images.source_tag` + `inbound_orders.img_cols` + `loading_orders.img_cols` |
| `models/orders.py` | `InboundImage` 加 `source_tag` 字段；`InboundRecord.get_verified_warnings` / `set_verified_warning` 静态方法；`InboundOrder.set_img_cols` 静态方法 |
| `tests/test_inbound_align.py` (新) | 入库 9 项功能单元测试 + Playwright e2e |
| `tests/test_loading_img_cols.py` (新) | 装柜列数持久化 e2e |

## 5. 数据流

### source_tag 数据流（功能 8）

```
桌面端 inbound-records.html 上传图片
   ↓ POST /api/v1/inbound-orders/records/<id>/images
   ↓ 入参 {image: file, source_tag: '备货照'}
   ↓
blueprints/inbound.py：解析 source_tag → 白名单校验 → 落库
   ↓
inbound_images.source_tag = '备货照'
   ↓
下次 GET /inbound-records 渲染时模板读取并显示标签
```

### 入库列数持久化数据流（功能 9）

```
用户点击列数按钮 3 → 共享 setImgCols(btn, 3)
   ↓
fetch PATCH /api/v1/inbound-orders/<oid> {img_cols: 3}
   ↓
blueprints/inbound.py PATCH 端点（新增分支）：校验 1-5 → InboundOrder.set_img_cols
   ↓
inbound_orders.img_cols = 3
   ↓
下次 GET /inbound-records 渲染：SELECT * 带出 group.img_cols=3
```

### 装柜列数持久化数据流（功能 10）

```
用户点击列数按钮 3 → 共享 setImgCols(btn, 3)（替代 cookie 版）
   ↓
fetch PATCH /api/v1/loading-orders/<oid> {img_cols: 3}
   ↓
blueprints/loading.py PATCH 端点（已存在）：校验 1-5 → LoadingOrder.set_img_cols（已存在）
   ↓
loading_orders.img_cols = 3
   ↓
下次 GET /loading-orders 渲染：SELECT * 带出 group.img_cols=3
```

## 6. 错误处理

| 情况 | 处理 |
|---|---|
| `source_tag` 不在白名单 | 静默忽略（不报错），`source_tag = None`，保持现有 `source='manual'` 默认 |
| DB 迁移列已存在 | `try/except Exception` 跳过 |
| 已存在入库订单历史图片 | `source_tag` 默认为 NULL，模板不显示标签 |
| `img_cols` 不在 1-5 | 返回 400「列数范围为1-5」 |
| `img_cols` 非整数 | 返回 400「列数必须是整数」 |
| `qty_invalid` 计算异常 | `try/except (TypeError, ValueError)` → `qty_invalid = True` |
| `verified_warnings` 查询失败 | 返回空对象 `{}`，不影响主流程 |

## 7. 测试策略（Q4=B Playwright e2e）

### 端到端 `tests/e2e_inbound_align.py`

Playwright 加载 `/inbound-records`，断言以下视觉/交互：

1. 上传加面规格入库订单图 → 「重点列」出现 + SVG `jia-mian-icon` 显示
2. 创建含「环保」品名的明细 → `row-eco` 淡绿底
3. 创建数量异常明细（空数量） → `row-out-of-stock` 红底 + ❌
4. 上传行级图片 → 🖼️ 红边框（`border: 2px solid #dc2626`）
5. 触发 AI 比对后 → 图例条可见
6. 触发 `verified_warnings` → ✓/✗ 按钮可点击折叠
7. 上传带 `source_tag='备货照'` 的图 → 标签显示
8. 切换列数 → 刷新页面后保留 DB 值（入库列数持久化）

### 端到端 `tests/e2e_loading_img_cols.py`

Playwright 加载 `/loading-orders`，断言：

1. 切换列数 1 → 2 → 3 → 4 → 5 → 刷新页面 → 列数保留 DB 值
2. 切换列数后 cookie/localStorage 不再生效（彻底切走）

### 单元测试 `tests/test_inbound_align.py`

- `has_jia_mian` 计算正确性（杂胶+加面 → True，其他 → False）
- `has_eco` 计算正确性（品名含环保 → True）
- `qty_invalid` 计算正确性（空数量 / 非数字 → True）
- `verified_warnings` 计算正确性（空对象默认）
- `source_tag` 白名单校验（合法值落库，非法值清空）
- `InboundOrder.set_img_cols(1-5)` 落库正确

### 单元测试 `tests/test_loading_img_cols.py`

- `LoadingOrder.set_img_cols(1-5)` 落库正确
- PATCH 端点接受合法值，拒绝非法值

## 8. 实施顺序

依赖关系决定实施顺序：

1. **DB 迁移先行**（`models/_init.py`）：`inbound_images.source_tag` + `inbound_orders.img_cols` + `loading_orders.img_cols`
2. **模型层**（`models/orders.py`）：`InboundImage.source_tag` + `InboundRecord.get_verified_warnings`/`set_verified_warning` + `InboundOrder.set_img_cols`
3. **后端蓝图**（`blueprints/inbound.py` + `blueprints/loading.py`）：渲染计算 + PATCH 分支 + source_tag 入参
4. **模板层**（`templates/inbound-records.html` + `templates/loading-orders.html`）：CSS + HTML + JS
5. **测试**：单元测试 + Playwright e2e

## 9. 验收标准（Definition of Done）

- [ ] `python -m pytest tests/ -v` 全部通过
- [ ] Playwright e2e `tests/e2e_inbound_align.py` 8 项断言全过
- [ ] Playwright e2e `tests/e2e_loading_img_cols.py` 2 项断言全过
- [ ] 视觉对比截图：入库 `/inbound-records` 与出货 `/shipping-records` 重点区域像素级一致（人工 review）
- [ ] 入库 `/inbound-records` 列数切换刷新后保留 DB 值
- [ ] 装柜 `/loading-orders` 列数切换刷新后保留 DB 值
- [ ] DB 迁移幂等（重复执行 `init_db()` 不报错）

## 10. 风险与回滚

| 风险 | 缓解 |
|---|---|
| DB 迁移破坏现有数据 | `try/except Exception` 幂等；列加 NULL/默认值，不影响现有行 |
| 模板改动量大引入 bug | 实施时**只改 `inbound-records.html` + `loading-orders.html` 列数部分**，不动出货 |
| 装柜列数改走 DB 后老用户的 cookie 值丢失 | 加列默认 3，覆盖旧 cookie 默认值；可接受 |
| source_tag 白名单过严导致合法值被拒 | 白名单仅 3 个值（备货照/装车照/归仓照），与出货完全一致 |
| `LoadingOrder.set_img_cols` 加列前调用报 500 | 实施顺序 DB 迁移先行，加列后再触发 PATCH |

回滚：git revert 即可。所有变更在 `inbound-records.html` + `inbound.py` + `loading.py` + `loading-orders.html` + `models/` 局部，**不动出货基准**。

## 11. 不做的事

- 不做入库/装柜的 AI 引擎切换（仍走本地 RapidFuzz）
- 不做装柜的加面/环保/缺货/警告按钮/source_tag（Q1=4 边界保持）
- 不动出货 `shipping-records.html` + `shipping.py`（基准）
- 不动移动端 `mobile_shipping.py`
- 不动共享模板 `_record_image_script.html`
- 不引入新依赖
- 不重构现有代码结构