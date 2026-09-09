# 拷贝纸 / 日本纸 行级标签图 + 点数（最终设计）

> 本文档描述 `/shipping-records`（PC 端）与 `/m/shipping-today/order/<oid>`（移动端出货）中
> **拷贝纸 / 日本纸** 两类商品行的实际实现状态。
>
> ⚠️ 历史说明：本功能初版（2026-09-06）曾设计独立表 `copy_paper_images` + `CopyPaperImage`
> 模型 + 张数录入（`sheet_count` / `copy_paper_match` 徽章）。经 2026-09-09 两次重构，
> **`copy_paper_images` 表已彻底废弃删除**，标签图直接存 `shipping_images`，点数复用 placement 体系。
> 本文档为重构后的最终口径，不再适用旧表设计。

## 背景

`拷贝纸`（category_code=`0107`，单位 `令`）和 `日本纸`（category_code=`0105`，单位 `张`）两类商品的
标签是**全手写体**，现有 OCR / AI 比对流水线对手写体基本无效；摆放图的「点击计数 / 散码」交互对纸类
（整令 / 整张）也不适用。

因此这两类商品行在出货页有两个专用能力：
1. **🖼️ 标签**：跳过 OCR / AI 的纯上传，标签照片作为人工参考，混排在普通商品图区。
2. **点数**：复用通用的摆放图计数体系（`shipping_images(source='placement')` +
   `placement_count.js`），单位=令/张，无散码。

## 目标 / 非目标

**目标**：
- 仅在 `拷贝纸` / `日本纸` 行渲染专用按钮，其他类别不受影响
- 标签图**完全脱离** OCR pipeline（不跑 PaddleOCR / DeepSeek，不进 AI 比对列）
- 锁单状态下隐藏（沿用 `lock-hide` 模式）
- PC + 移动端出货同步支持
- 标签图与普通出货图**同表** `shipping_images`，靠 `source='copy_paper_label'` 区分
- 点数复用 placement 体系（不要为纸类再造一套计数）

**非目标**：
- 不建独立表（已废弃 `copy_paper_images`）
- 不做 OCR 文字提取（手写体不可靠）
- 不做「张数录入」(`sheet_count`) —— 点数已统一走 placement
- 不动现有 `PlacementImage` / `ShippingImage` 表结构

## 数据模型

**无独立表。** 标签图与普通出货图共存于 `shipping_images`：

```sql
-- shipping_images（既有表，不新增结构）
CREATE TABLE shipping_images (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    order_pk      INTEGER,
    record_pk     INTEGER,          -- 关联到 shipping_records.id
    file_path     TEXT,
    original_name TEXT,
    source        TEXT,             -- 普通图/placement/copy_paper_label/...
    created_at    TEXT DEFAULT CURRENT_TIMESTAMP,
    ...
);
```

区分依据：
- 标签图：`source = 'copy_paper_label'`（常量 `COPY_PAPER_LABEL_SOURCE = 'copy_paper_label'`）
- 点数图：`source = 'placement'`（与普通按支商品共用 placement 体系）

## 后端

### 常量（`blueprints/shipping.py:54`）

```python
COPY_PAPER_LABEL_SOURCE = 'copy_paper_label'
```

### 类别检测（`blueprints/shipping.py`）

```python
def _is_copy_paper_item(item: dict) -> bool:
    """category_code ∈ {'0105','0107'}，或品名含 '拷贝'/'日本纸' 兜底。"""
    ...
```

### 记录富化（`_enrich_copy_paper_for_item`，shipping.py:1999）

每条 record 进模板前多带 3 个字段：

- `item.is_copy_paper: bool`
- `item.label_images: list[dict]` — 该 record 下 `source='copy_paper_label'` 的
  `shipping_images` 行（移动端缩略图用；PC 端由 order-images-area 按 source 直接渲染，不读此字段）
- `item.has_label_image: bool` — 已上传标签图（标签按钮红框反馈）

> 优先复用调用方注入的 `item['_record_imgs_cache']`（PC 端图片分组循环已查好本行图），避免重复查库；
> 否则回退 `ShippingImage.get_by_record(item['id'])`。

### 端点

| Method | Path | 行为 |
|---|---|---|
| `POST` | `/api/v1/shipping-orders/records/<rid>/copy-paper-images` | 上传标签图（multipart 或 JSON base64）；写 `ShippingImage.create(..., source=COPY_PAPER_LABEL_SOURCE, record_pk=rid)`；**跳过 OCR pipeline**；锁单 → 403 |
| `DELETE` | `/api/v1/shipping-orders/images/<id>` | 通用删除端点（自带锁单防御 + 审计），标签图删除走这里（不再有 copy-paper 专用 DELETE 端点） |

**锁单防御**：写入/删除前查 `ShippingOrder.get_by_id(record.order_pk)` → 若 `is_locked=1` → 返回 403。

## 前端

### PC 端（`templates/shipping-records.html`）

操作列（约 line 920-932）**仅 `is_copy_paper=True` 时**渲染两个按钮：

```html
{% if item.is_copy_paper %}
<button class="btn btn-sm copy-paper-label-btn lock-hide{% if item.has_label_image %} has-label-image{% endif %}"
        data-record-id="{{ item.id }}" data-order-id="{{ group.id }}" data-source="label"
        onclick="openCopyPaperUpload('{{ item.id }}','{{ group.id }}','label')">🖼️</button>
<button class="placement-add-btn lock-hide{% if item.placement_match %} placement-ok{% endif %}"
        data-record-id="{{ item.id }}" data-order-id="{{ group.id }}"
        onclick="openPlacementImageModal('{{ item.id }}','{{ group.id }}')">点数</button>
{% endif %}
```

- 行 `<tr>` 上带 `data-count-unit`（拷贝纸=`item.unit` 即令/张；否则 `支`）与 `data-has-loose`
  （拷贝纸=`0`；否则 `1`），供 `placement_count.js` 参数化弹框。
- 标签图渲染在普通商品图区 `order-images-area`，class 为 `.img-item-copy-paper-label`，
  带 `.copy-paper-label-del-btn` 删除按钮（调用通用 `DELETE /images/<id>`）。
- AI 比对列对 `copy_paper_label` 图**特判为空**（`initRowMatchColumn` 查询排除
  `.img-item-copy-paper-label`），不渲染 match-badge / 人工核查按钮。
- 订单锁定后若 `item.placement_match` 命中，操作列显示 `✓ 点数` 标记（与纯胶行一致）。

### 移动端（`templates/mobile/shipping-order.html`）

- `📷 标签` 按钮（`m-copy-paper-label-btn`）→ 调相机/相册 → `POST .../copy-paper-images`。
- `.m-copy-paper-area` 块渲染 `rec.label_images`，用 `img.relative_path` 拼 `/upload/...` URL。
- 点数走独立点数页：`point-entry` 链接到 `shipping_placement_count(oid, record_id)`。
- `blueprints/mobile_shipping.py:176` 在算 OCR 图时排除 `placement` 与 `copy_paper_label`，
  避免标签图被误当作「最后一张 OCR 图」影响状态/详情。

### JS

- `static/js/copy_paper.js`：**仅**处理标签图上传 + 删除（删除走通用 `/images/<id>`）。
- `static/js/placement_count.js`：点数计数（参数化 `currentCountUnit` / `currentHasLoose`，
  弹框显示「清点令数」/「清点张数」，拷贝纸无散码）。

## 点数（复用 placement 体系）

- 点数图存 `shipping_images(source='placement')`，与普通按支商品**同一条计数管线**。
- `compute_placement_expected_zhi(remark, quantity_str, unit)`（helpers.py:421）已扩展到
  `unit in ('支','令','张')`：无备注「X支」时，令/张直接以 `float(quantity_str)` 兜底为期望值。
- 弹框标题/单位随 `data-count-unit` 动态切换为「清点令数」/「清点张数」。
- 锁单后的「✓ 点数」标记由 `item.placement_match` 驱动，拷贝纸行同样适用。

## 已废弃并移除（勿再引用）

| 旧设计 | 说明 |
|---|---|
| 表 `copy_paper_images` | 2026-09-09 删除（DDL 移除 + DB `DROP TABLE`） |
| 模型 `CopyPaperImage` | 2026-09-09 删除类及导出 |
| `sheet_count` 字段 | 点数改走 placement，该字段恒为 NULL 会渲染误导徽章，已废弃 |
| `copy_paper_total` / `copy_paper_match` / `copy_paper_expected` | 原「张数录入」产物，随点数走 placement 废弃 |
| `compute_copy_paper_expected_quantity` | 2026-09-09 删除 |
| `GET /copy-paper-images`、专用 `DELETE /copy-paper-images/<id>`、`PATCH .../sheet-count` | 2026-09-09 删除；删除改走通用 `DELETE /images/<id>` |
| `templates/_copy_paper_count_modal.html` | 2026-09-09 删除 |
| PC `.copy-paper-area` / `.copy-paper-badge` / `.copy-paper-thumb` / `.copy-paper-tag` 整块 CSS | 2026-09-09 清理（移动端 `.m-copy-paper-*` 仍在用） |

## 验证

`tools/verify_copy_paper_label_merge.py`（PC + 移动端渲染校验，无 JS 错误）与
`tools/verify_label_merge_e2e.py`（上传→`shipping_images`、删→移除 回环测试）覆盖核心链路。
