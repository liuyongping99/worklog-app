# 拷贝纸 / 日本纸行级标签图 + 点数（实现现状）

> **状态：已实现，并经 2026-09-09 重构为最终形态。**
>
> 本文档为 `docs/superpowers/specs/2026-09-06-copy-paper-row-buttons-design.md` 的落地记录。
> 初版计划曾规划独立表 `copy_paper_images` + `CopyPaperImage` + 张数录入（`sheet_count`）；
> 该方案在 2026-09-09 被废弃，标签图迁入 `shipping_images`（source=`copy_paper_label`），
> 点数统一走 placement 体系。以下为代码**实际**状态，不再保留初版的任务勾选清单。

**Tech Stack:** Python 3.12 + Flask 3.1 + SQLite 3.45 + Jinja2 + 原生 JS（沿用现有栈，零新依赖）

**Spec:** `docs/superpowers/specs/2026-09-06-copy-paper-row-buttons-design.md`

---

## 实际架构

```
拷贝纸(0107,令) / 日本纸(0105,张) 行
   ├─ 🖼️ 标签按钮  → openCopyPaperUpload → POST /records/<rid>/copy-paper-images
   │                  → ShippingImage.create(source='copy_paper_label', record_pk)
   │                  → 渲染: order-images-area 内 .img-item-copy-paper-label
   │                  → 删除: 通用 DELETE /images/<id>
   │
   └─ 点数按钮(placement-add-btn) → openPlacementImageModal → placement 体系
                                      → ShippingImage(source='placement')
                                      → placement_count.js（清点令数/张数, 无散码）
                                      → 锁单后 placement_match 命中显示「✓ 点数」
```

## 实际文件改动清单（相对初版计划，已落地的最终态）

| 文件 | 实际状态 |
|---|---|
| `blueprints/shipping.py` | + `COPY_PAPER_LABEL_SOURCE` 常量 + `_is_copy_paper_item` + `_enrich_copy_paper_for_item`（产出 `is_copy_paper` / `label_images` / `has_label_image`）+ `POST /records/<rid>/copy-paper-images`（写 `shipping_images`，跳过 OCR）+ 标签图注入 `order_non_ai` 复用普通图渲染管线 |
| `blueprints/mobile_shipping.py` | 调用 `_enrich_copy_paper_for_item`；OCR 图排除 `('placement', COPY_PAPER_LABEL_SOURCE)` |
| `blueprints/_helpers.py` | `compute_placement_expected_zhi` 扩展支持 `unit in ('支','令','张')`（无备注时令/张以 quantity 兜底） |
| `templates/shipping-records.html` | 拷贝纸行渲染 `🖼️ 标签` + `点数` 按钮；行 `<tr>` 带 `data-count-unit` / `data-has-loose`；标签图在 order-images-area 渲染为 `.img-item-copy-paper-label`；AI 比对列对 `copy_paper_label` 特判为空；锁单后显示 `✓ 点数` 标记 |
| `templates/mobile/shipping-order.html` | `m-copy-paper-label-btn` 上传标签图；`.m-copy-paper-area` 渲染 `rec.label_images`（relative_path）；点数走独立点数页 `point-entry` |
| `static/js/copy_paper.js` | **仅**标签图上传 + 删除（删除走通用 `/images/<id>`） |
| `static/js/placement_count.js` | placement 计数，参数化 `currentCountUnit` / `currentHasLoose`，弹框显示「清点令数」/「清点张数」 |
| `static/css/app.css` | `.img-item-copy-paper-label` 样式（虚线蓝边框 + 删除按钮） |
| `static/css/mobile.css` | `.m-copy-paper-area` / `.m-copy-paper-thumb` / `.m-copy-paper-tag` 样式 |
| `models/orders.py` / `models/_init.py` / `models/__init__.py` | **无** `CopyPaperImage`、无 `copy_paper_images` DDL（已删除） |

## 已废弃移除（相对初版计划）

- `copy_paper_images` 表 + `CopyPaperImage` 模型类（及 re-export）
- `compute_copy_paper_expected_quantity`、`copy_paper_total` / `copy_paper_match` / `copy_paper_expected`
- 专用 `GET /copy-paper-images`、专用 `DELETE /copy-paper-images/<id>`、`PATCH .../sheet-count`
- `templates/_copy_paper_count_modal.html`
- PC 端 `.copy-paper-area` / `.copy-paper-badge` / `.copy-paper-thumb` / `.copy-paper-tag` 整块 CSS
- `tests/test_copy_paper_images.py`（依赖旧表的测试，已删除）

## 验证

- `tools/verify_copy_paper_label_merge.py`：PC + 移动端渲染校验（标签图无 OCR/AI 按钮、AI 比对列空、无 JS 错误）。
- `tools/verify_label_merge_e2e.py`：上传 → `shipping_images(source='copy_paper_label')`、删除 → 移除 的回环测试。

## 落地点

- 标签图落 `shipping_images(source='copy_paper_label')`，与普通出货图同表同渲染管线。
- 点数落 `shipping_images(source='placement')`，与纯胶等按支商品同源。
- 锁单防御由通用 `DELETE /images/<id>` 端点统一提供（403 + 审计）。
