# 拷贝纸/日本纸 标签图存于 shipping_images（最终设计）

> 本功能在 2026-09-06 初版曾把标签图与张数图都存进独立表 `copy_paper_images`
> （`source='label'` / `'count'`），渲染在专属 `copy-paper-area` 块。
> 2026-09-09 重构后：**`copy_paper_images` 表已废弃删除**，标签图直接存
> `shipping_images(source='copy_paper_label')`，与普通出货图同表；张数图随点数统一走
> `shipping_images(source='placement')`。
>
> 本文档描述重构后的**最终**形态：标签图如何存储、如何渲染到普通商品图区。

## 背景

标签图本质是参考照片，应对照普通商品的 🖼️ 行级图，混排在 `<div class="order-images-area">`
（普通商品图区），而不是挤在「点数区」语义里。重构后它和普通出货图一样存 `shipping_images`，
靠 `source='copy_paper_label'` 区分，且**不进 OCR / AI 比对流水线**。

## 目标 / 非目标

**目标**：
- 标签图存 `shipping_images`，`source='copy_paper_label'`，`record_pk` 关联明细行
- 标签图混排到普通商品图区（`order-images-area`），class 为 `.img-item-copy-paper-label`
- AI 比对列对该类图特判为空（不渲染 match-badge / 人工核查）
- 标签图删除复用通用 `DELETE /api/v1/shipping-orders/images/<id>`（自带锁单防御 + 审计）
- 完全不引入 OCR/AI 处理

**非目标**：
- 不动 placement / 点数流程（点数图仍 `source='placement'`）
- 不动 OCR pipeline
- 不重建 `copy_paper_images` 表

## 存储

标签图行示例（`shipping_images`）：

```text
id=4185
order_pk=955
record_pk=2668          ← 拷贝纸明细行
source='copy_paper_label'
file_path='upload/2026-09/xxxx.jpg'
relative_path='2026-09/xxxx.jpg'
```

常量定义：`COPY_PAPER_LABEL_SOURCE = 'copy_paper_label'`（`blueprints/shipping.py:54`）。

## 后端

### 上传（`blueprints/shipping.py:1914`）

```python
@bp.route('/api/v1/shipping-orders/records/<int:rid>/copy-paper-images', methods=['POST'])
def api_v1_shipping_orders_record_copy_paper_upload(rid):
    # 锁单检查 → 保存文件 → ShippingImage.create(
    #     order_pk, file_path, original_name, source=COPY_PAPER_LABEL_SOURCE, record_pk=rid)
    # 跳过 OCR pipeline; AuditLog.log('upload_copy_paper_image', ...)
```

支持 multipart（`image` 字段）与 JSON base64（`image` 字段，需 `data:image/...;base64,` 前缀）。

### 富化（`_enrich_copy_paper_for_item`，shipping.py:1999）

```python
item['is_copy_paper'] = _is_copy_paper_item(item)
if not item['is_copy_paper']:
    item['label_images'] = []
    item['has_label_image'] = False
    return
imgs = item.get('_record_imgs_cache') or ShippingImage.get_by_record(item['id'])
labels = [i for i in imgs if i.get('source') == COPY_PAPER_LABEL_SOURCE]
item['label_images'] = labels
item['has_label_image'] = bool(labels)
```

### 删除

走通用端点 `DELETE /api/v1/shipping-orders/images/<id>`（shipping.py:789），对 `shipping_images`
任意行生效，自带锁单防御 + 审计日志。**不存在** copy-paper 专用 DELETE 端点。

## 前端

### PC 端（`templates/shipping-records.html`）

- 操作列 `🖼️ 标签` 按钮（`copy-paper-label-btn`，上传后加 `has-label-image` 红框类）→
  `openCopyPaperUpload(rid, oid, 'label')` → 复用上传弹框 → `POST .../copy-paper-images`。
- 标签图渲染在普通商品图区 `order-images-area`，class 为 `.img-item-copy-paper-label`，
  带 `.copy-paper-label-del-btn`（调用通用 `DELETE /images/<id>`）。
- AI 比对列 `initRowMatchColumn`（`_record_image_script.html`）查询时排除
  `.img-item-copy-paper-label`，对该类图不渲染比对结果。
- 渲染分流：主循环中把 `source='copy_paper_label'` 的图注入 `order_non_ai`，
  模板按 source 特判渲染（仅普通图区显示，无 match-badge）。

### 移动端（`templates/mobile/shipping-order.html`）

- `m-copy-paper-label-btn` 调相机/相册 → `POST .../copy-paper-images`。
- `.m-copy-paper-area` 块渲染 `rec.label_images`，用 `img.relative_path` 拼 `/upload/...`。
- 点数走独立点数页 `point-entry` → `shipping_placement_count(oid, record_id)`。

## 文件改动清单（相对初版 relocate 计划，已落地的最终态）

| 文件 | 实际状态 |
|---|---|
| `blueprints/shipping.py` | 上传端点写 `shipping_images(source=copy_paper_label)`；`_enrich_copy_paper_for_item` 产出 `label_images` / `has_label_image`；标签图注入 `order_non_ai` 复用普通图渲染 |
| `templates/shipping-records.html` | 标签图在普通图区渲染为 `.img-item-copy-paper-label` + `.copy-paper-label-del-btn`；AI 比对列特判为空 |
| `templates/mobile/shipping-order.html` | `m-copy-paper-area` 渲染 `rec.label_images`（relative_path） |
| `static/js/copy_paper.js` | 仅标签上传 + 删除（删除走通用 `/images/<id>`） |
| `static/css/app.css` | `.img-item-copy-paper-label` 样式（虚线蓝边框 + 删除按钮） |
| `static/css/mobile.css` | `.m-copy-paper-area` / `.m-copy-paper-thumb` / `.m-copy-paper-tag` 样式 |

## 已废弃

- `copy_paper_images` 表（DDL 移除 + DB `DROP TABLE`）
- `CopyPaperImage` 模型类及导出
- `templates/_copy_paper_count_modal.html`
- 专用 `GET /copy-paper-images`、专用 `DELETE /copy-paper-images/<id>`、`PATCH .../sheet-count`
- PC `.copy-paper-area` / `.copy-paper-badge` / `.copy-paper-thumb` / `.copy-paper-tag` 整块 CSS

## 验证

`tools/verify_copy_paper_label_merge.py`（PC + 移动端渲染校验，无 JS 错误）、
`tools/verify_label_merge_e2e.py`（上传→`shipping_images`、删→移除 回环）。
