# 拷贝纸 / 日本纸 行级双按钮设计（2026-09-06）

## 背景

`/shipping-records`（PC 端）和 `/m/shipping-today/order/<oid>`（移动端出货）的操作列目前有 5 类按钮：移动 / 编辑 / 删除 / 行级图片（OCR + AI 比对）/ 点数（摆放图点击计数 + 散码）。

`拷贝纸`（category_code=`0107`，单位 `令`）和 `日本纸`（category_code=`0105`，单位 `张`）两类商品的标签是**全手写体**，现有 OCR / AI 比对流水线对手写体基本无效，摆放图的「点击计数 / 散码」交互对纸类（整令 / 整张）也不适用。

需要在操作列给这两类商品的明细行新增两个**专用按钮**：
1. **📷 标签**：跳过 OCR / AI 的纯上传，标签照片作为人工参考
2. **📊 张数**：可反复上传图，每次上传后弹小弹框录入张数（整数），与 `quantity` 字段比对出徽章

## 目标 / 非目标

**目标**：
- 仅在 `拷贝纸` / `日本纸` 行渲染这两个按钮，其他类别不受影响
- 完全脱离现有 `ocr_pipeline.RecordImageProcessor`（不跑 PaddleOCR / DeepSeek）
- 锁单状态下隐藏（沿用 `lock-hide` 模式）
- PC + 移动端出货同步支持
- 数据模型独立，新表 `copy_paper_images`

**非目标**：
- 不入库 / 不装柜移动端（仅 PC + 出货移动端）
- 不做 OCR 文字提取（手写体不可靠）
- 不做备注 `X支` / `X令` / `X张` 解析（直接与 `quantity` 字段比对）
- 不做按图的张数核对（仅行级汇总）
- 不动现有 `PlacementImage` / `ShippingImage` 表

## 数据模型

### 新表 `copy_paper_images`

```sql
CREATE TABLE copy_paper_images (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    record_pk     INTEGER NOT NULL,
    file_path     TEXT NOT NULL,
    original_name TEXT,
    source        TEXT NOT NULL CHECK (source IN ('label','count')),
    sheet_count   INTEGER,
    created_at    TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (record_pk) REFERENCES shipping_records(id) ON DELETE CASCADE
);
CREATE INDEX idx_copy_paper_record ON copy_paper_images(record_pk);
```

字段语义：
- `source='label'`：仅作参考照片，无 `sheet_count`（前端不渲染张数输入框）
- `source='count'`：人工录入的张数（`sheet_count` 为非 NULL 整数时计入行级汇总）

## 后端

### 类别检测（`blueprints/shipping.py`）

```python
def _is_copy_paper_record(item: dict) -> bool:
    """category_code ∈ {'0105','0107'}"""
    code = _get_category_code(item)
    return code in ('0105', '0107')
```

每条 record 进模板前多带字段：
- `item.is_copy_paper: bool`
- `item.copy_paper_images: list[dict]` — `{id, file_path, source, sheet_count, created_at}`
- `item.copy_paper_total: int` — `sum(img.sheet_count for img in images if img.sheet_count is not None)`
- `item.copy_paper_match: 'green' | 'yellow' | None`

### 期望值计算（`blueprints/_helpers.py` 新增）

```python
def compute_copy_paper_expected_quantity(quantity, unit):
    """直接从 quantity 取期望数；unit ∈ {'令','张'}"""
    if unit not in ('令', '张'):
        return (0.0, False)
    try:
        q = float(quantity)
    except (ValueError, TypeError):
        return (0.0, False)
    return (q, q > 0)
```

**不**回退到 `compute_placement_expected_zhi`（语义不同：拷贝纸/日本纸本身以单位为计，quantity 就是期望值）。

### 匹配判定（每行渲染时）

```python
total = sum(img['sheet_count'] for img in images if img['sheet_count'] is not None)
expected, has_expected = compute_copy_paper_expected_quantity(item['quantity'], item['unit'])
if not has_expected:
    match = None        # 没填 quantity → 不出徽章
elif total == int(expected):
    match = 'green'     # 一致
else:
    match = 'yellow'    # 不一致
```

### 端点（4 个，全部 `blueprints/shipping.py`）

| Method | Path | 行为 |
|---|---|---|
| `POST` | `/api/v1/shipping-orders/records/<rid>/copy-paper-images` | 上传图（multipart）。body: `source ∈ {'label','count'}`；走 `_helpers.save_uploaded_image()`；跳过 OCR pipeline；锁单检查 |
| `GET` | `/api/v1/shipping-orders/records/<rid>/copy-paper-images` | 列图（按 source 分组、created_at 升序） |
| `DELETE` | `/api/v1/shipping-orders/copy-paper-images/<id>` | 删图 + 删磁盘文件；锁单检查 |
| `PATCH` | `/api/v1/shipping-orders/copy-paper-images/<id>/sheet-count` | `{sheet_count: int\|null}`；null = 清空 |

**锁单防御**：进入每个端点前查 `ShippingOrder.get_by_id(record.order_pk)` → 若 `is_locked=1` → 返回 403。

**移动端**：4 个端点路径不变（`/m/shipping-today/order/<oid>` 也走 `/api/v1/...`），由 `mobile_shipping.py` 内部已经处理好的路由分发。

## 前端

### PC 端（`templates/shipping-records.html`）

操作列（lines 914-924）内**仅 `is_copy_paper=True` 时**渲染：

```html
{% if item.is_copy_paper %}
  <button class="copy-paper-label-btn lock-hide"
          data-record-id="{{ item.id }}" data-order-id="{{ group.id }}"
          title="上传标签图（拷贝纸/日本纸，无需 OCR）"
          onclick="openCopyPaperUpload('{{ item.id }}', '{{ group.id }}', 'label')">📷 标签</button>
  <button class="copy-paper-count-btn lock-hide"
          data-record-id="{{ item.id }}" data-order-id="{{ group.id }}"
          title="上传点数图并录入张数"
          onclick="openCopyPaperUpload('{{ item.id }}', '{{ group.id }}', 'count')">📊 张数</button>
{% endif %}
```

每行底部新增缩略图区（紧接现有 placement-area 之后；仅在有图时渲染）：

```html
{% if item.is_copy_paper and item.copy_paper_images %}
  <div class="copy-paper-area">
    {% if item.copy_paper_match == 'green' %}
      <span class="copy-paper-badge green">✓ 张数 {{ total }}/{{ expected }}</span>
    {% elif item.copy_paper_match == 'yellow' %}
      <span class="copy-paper-badge yellow">⚠ 张数不符 {{ total }}≠{{ expected }}</span>
    {% endif %}
    {% for img in item.copy_paper_images %}
      <div class="copy-paper-thumb" data-image-id="{{ img.id }}">
        <img src="/upload/{{ img.file_path }}">
        <span class="copy-paper-tag tag-{{ img.source }}">
          {{ '标签' if img.source == 'label' else '点数' }}
        </span>
        {% if img.source == 'count' %}
          <input class="copy-paper-count-input" type="number" min="0"
                 value="{{ img.sheet_count if img.sheet_count is not none else '' }}"
                 data-image-id="{{ img.id }}" placeholder="张数">
        {% endif %}
        <button class="copy-paper-delete-btn" data-image-id="{{ img.id }}">×</button>
      </div>
    {% endfor %}
  </div>
{% endif %}
```

### 弹框流程（`static/js/copy_paper.js` + `templates/_copy_paper_count_modal.html`）

1. 点击 `📷 标签` 或 `📊 张数` → 复用 `_image_upload_modal.html` 弹上传框
2. 上传 `source='count'` 的图成功后 → 自动打开新建的 `_copy_paper_count_modal.html`（单数字输入 + 保存按钮）
3. 上传 `source='label'` 的图 → 直接关闭弹框，不弹张数输入
4. 缩略图上的 `×` 按钮 → DELETE，刷新该行
5. 缩略图上的张数输入框 `change` → PATCH 写回，刷新该行徽章

### 移动端（`templates/mobile/shipping-order.html`）

仅 `/m/shipping-today/order/<oid>` 加同样的两个按钮 + 缩略图区。
张数输入用 `prompt()` 或 inline `<input type=number>`（避免再造 mobile modal）。
后端走同一套 `/api/v1/shipping-orders/...` 端点。

## 文件改动清单

| 文件 | 改动 |
|---|---|
| `models/_init.py` | + `copy_paper_images` 表 DDL + 索引 |
| `models/orders.py` | + `CopyPaperImage` 模型类（`create` / `get_by_record` / `list_by_record` / `delete` / `update_count`） |
| `models/__init__.py` | re-export `CopyPaperImage` |
| `blueprints/_helpers.py` | + `compute_copy_paper_expected_quantity(quantity, unit)` |
| `blueprints/shipping.py` | + 4 端点 + `_is_copy_paper_record` + 每行渲染时附加 4 字段 |
| `templates/shipping-records.html` | + 2 按钮 + 缩略图区 + 引用 `copy_paper.js` |
| `templates/_copy_paper_count_modal.html` | **新建**：张数输入小弹框 |
| `static/js/copy_paper.js` | **新建**：上传 / 录入 / 删除 / 徽章刷新 |
| `static/css/app.css` | + `.copy-paper-*` 样式 |
| `templates/mobile/shipping-order.html` | + 2 按钮 + 缩略图区 + inline 张数输入 |
| `static/css/mobile.css` | + 移动端样式 |
| `tests/test_copy_paper_images.py` | **新建**：表 CRUD + 4 端点 + 比较函数 + 锁单测试 |

## 测试计划

`tests/test_copy_paper_images.py` 覆盖：

1. **DDL**：表创建后字段齐全（断言 `pragma table_info`）
2. **`CopyPaperImage.create`**：插入后 `id` 自增、`source` 校验
3. **`CopyPaperImage.get_by_record` / `list_by_record`**：返回按 source 分组 / created_at 排序
4. **`CopyPaperImage.update_count`**：接受 int 与 null
5. **`CopyPaperImage.delete`**：级联清理磁盘文件（用 tmpdir fixture）
6. **`compute_copy_paper_expected_quantity`**：
   - `unit='令', q=5 → (5.0, True)`
   - `unit='张', q=500 → (500.0, True)`
   - `unit='支' → (0.0, False)`（不适用）
   - `q=0 / None / 'abc' → (0.0, False)`
7. **HTTP 端点**（用 `app.test_client`）：
   - 上传 `source='label'` → 201，列图能查到
   - 上传 `source='count'` → 201
   - PATCH `sheet_count=100` → 行级 total=100
   - 锁单后再上传 → 403
   - DELETE → 200，行级缩略图消失
8. **匹配判定**（fixture 构造 record + images）：
   - 拷贝纸 (q=5令) + 3 张图 sheet_count=2+2+1 → match='green'
   - 拷贝纸 (q=5令) + 1 张图 sheet_count=4 → match='yellow'
   - 日本纸 (q=500张) + 无图 → match=None
   - 拷贝纸 (q=0) + 有图 → match=None

## 风险 / 回滚

- 新表 `copy_paper_images` 失败 → 删除 DDL 即可（无依赖）
- 端点命名冲突 → 4 个路径都带 `copy-paper-images` 后缀，足够区分
- 若 `classify_record()` 对 `拷贝纸/日本纸` 检测遗漏 → 用直接 JOIN `product_categories` 兜底
- 回滚：删除 `_helpers.compute_copy_paper_expected_quantity`、4 端点、模板按钮、JS、CSS、新表
