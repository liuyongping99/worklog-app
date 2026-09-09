# 拷贝纸/日本纸 标签图存于 shipping_images（实现现状）

> **状态：已实现。**
>
> 本文档为 `docs/superpowers/specs/2026-09-09-copy-paper-label-relocate-design.md` 的落地记录。
> 初版 relocate 计划曾假设标签图仍在 `copy_paper_images` 表（仅改变渲染位置）。
> 实际 2026-09-09 的重构更进一步：**直接废弃 `copy_paper_images` 表**，标签图迁入
> `shipping_images(source='copy_paper_label')`。以下为代码**实际**状态。

**Tech Stack:** Python 3.12 + Flask 3.1 + Jinja2 + 原生 JS（沿用现有栈）

**Spec:** `docs/superpowers/specs/2026-09-09-copy-paper-label-relocate-design.md`

---

## 实际改动（相对初版计划的偏差）

| 初版计划假设 | 实际落地 |
|---|---|
| 标签图仍在 `copy_paper_images` 表，仅渲染位置从 `copy-paper-area` 移到 `order-images-area` | `copy_paper_images` 表**删除**；标签图改为存 `shipping_images`，`source='copy_paper_label'` |
| 删除复用专用 `DELETE /copy-paper-images/<id>` | 删除走通用 `DELETE /api/v1/shipping-orders/images/<id>`（自带锁单防御 + 审计） |
| `copy-paper-area` 仍保留（只渲染 `source='count'`） | `copy-paper-area` 整块删除（张数图已随点数走 placement，无 `source='count'` 图） |
| `CopyPaperImage` 模型 / 4 端点保留 | `CopyPaperImage` 模型删除；仅保留上传端点（改为写 `shipping_images`），其余 3 端点删除 |

## 实际文件改动清单

| 文件 | 实际状态 |
|---|---|
| `blueprints/shipping.py` | 上传端点 `POST /records/<rid>/copy-paper-images` 改为 `ShippingImage.create(source=copy_paper_label, record_pk=rid)`；删除 GET/DELETE/PATCH 三个旧端点；`_enrich_copy_paper_for_item` 产出 `label_images` / `has_label_image`；标签图注入 `order_non_ai` |
| `blueprints/mobile_shipping.py` | 调用 `_enrich_copy_paper_for_item`；OCR 图排除 `('placement', COPY_PAPER_LABEL_SOURCE)` |
| `models/orders.py` / `models/__init__.py` | 删除 `CopyPaperImage` 类及 re-export |
| `models/_init.py` | 移除 `copy_paper_images` DDL |
| `templates/shipping-records.html` | 标签图在普通图区渲染为 `.img-item-copy-paper-label` + `.copy-paper-label-del-btn`；AI 比对列特判为空；删除 stale 注释（原指向已删 `_copy_paper_count_modal.html`） |
| `templates/mobile/shipping-order.html` | `m-copy-paper-label-btn` 上传；`.m-copy-paper-area` 渲染 `rec.label_images`；删除 stale 注释（原指向已删 count-input 监听） |
| `static/js/copy_paper.js` | 仅标签上传 + 删除（删除走通用 `/images/<id>`）；移除张数录入分支 |
| `static/css/app.css` | 移除 PC `.copy-paper-area` / `.copy-paper-badge` / `.copy-paper-thumb` / `.copy-paper-tag` 整块（移动端 `.m-copy-paper-*` 保留）；保留 `.img-item-copy-paper-label` |
| `static/css/mobile.css` | 保留 `.m-copy-paper-area` / `.m-copy-paper-thumb` / `.m-copy-paper-tag` |
| 数据库 | `worklog.db` 中既有 `copy_paper_images` 行迁移至 `shipping_images(source=copy_paper_label)` 后 `DROP TABLE copy_paper_images` |

## 验证

- `tools/verify_copy_paper_label_merge.py`：PC + 移动端渲染校验（标签图无 OCR/AI 按钮、AI 比对列空、无 JS 错误）。
- `tools/verify_label_merge_e2e.py`：上传 → `shipping_images(source='copy_paper_label')`、删除 → 移除 的回环测试。

## 落地点

- 标签图：普通商品图区 `.img-item-copy-paper-label`，删除走通用 `/images/<id>`。
- 张数（点数）：`shipping_images(source='placement')` + `placement_count.js`（清点令数/张数）。
- 锁单防御统一由通用删除端点提供。
