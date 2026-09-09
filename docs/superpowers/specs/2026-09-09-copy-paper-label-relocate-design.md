# 拷贝纸/日本纸 标签图移至普通商品图区（2026-09-09）

## 背景

2026-09-06 上线的「拷贝纸/日本纸 行级双按钮」功能（`docs/superpowers/specs/2026-09-06-copy-paper-row-buttons-design.md`）把标签图（`source='label'`）与张数图（`source='count'`）都渲染在专属的 `<div class="copy-paper-area">` 块，紧贴 `placement-area`（点数区）。

用户反馈：标签图本质是参考照片，混在「点数区」语义不对；应与普通商品的 🖼️ 行级图混排在 `<div class="order-images-area">`。张数图（数据输入）继续留在 `copy-paper-area` 不动。

## 目标 / 非目标

**目标**：
- 拷贝纸/日本纸的标签图（`source='label'`）渲染到普通商品图区
- 张数图（`source='count'`）保留在 `copy-paper-area`
- 完全复用现有 4 端点 + CopyPaperImage 表
- 不引入 OCR/AI 处理

**非目标**：
- 不动张数图流程
- 不动 4 端点
- 不动 CopyPaperImage 模型 / 表结构
- 不动 OCR pipeline
- 不改 mobile_shipping.py（移动端按原样继续在 copy-paper-area）

## 数据流

### 后端：把 `source='label'` 的 CopyPaperImage 注入 `order_images`

在 `blueprints/shipping.py` 的 `shipping_records()` 主循环里（record 富化段，约 line 270-320 区域，enrich 完 `placement_match` 之后），追加：

```python
# 2026-09-09: 拷贝纸/日本纸 label 图移至普通商品图区
# (张数图保留在 copy-paper-area,仅 label 图混排)
if item.get('is_copy_paper'):
    for lbl in (item.get('copy_paper_images') or []):
        if lbl.get('source') != 'label':
            continue
        # 仿 PlacementImage.get_by_record 模式补 relative_path
        if 'relative_path' not in lbl and lbl.get('file_path'):
            lbl['relative_path'] = os.path.basename(lbl['file_path'])
        # 注入到 order_non_ai[order_id] 复用现有渲染管线
        # 标记 source='copy_paper_label' 让模板能识别(非 OCR 图,无需 match-badge)
        lbl['source'] = 'copy_paper_label'
        order_non_ai.setdefault(grp['id'], []).append(lbl)
```

**关键点**：
- 不动 CopyPaperImage 表/类
- `relative_path` 字段缺失时从 `file_path` 推算（PlacementImage.get_by_record 同款）
- 注入到 `order_non_ai` 后自动进入 `order_images.get(group.id)` 模板渲染管线
- `source` 临时改成 `'copy_paper_label'` 让模板可以特判（不渲染 match-badge / 人工核查按钮）

### 前端：模板渲染 + 删除按钮

#### `templates/shipping-records.html`（line 940-985，普通商品图区）

紧贴现有 `{% for img in all_order_imgs %}` 循环之后（不替换），追加：

```jinja2
{# 2026-09-09: 拷贝纸/日本纸 label 图混排在普通区 (source='copy_paper_label') #}
{% for img in all_order_imgs if img.source == 'copy_paper_label' %}
<div class="img-item img-item-record img-item-copy-paper-label"
     data-record-pk="{{ img.record_pk }}" data-image-id="{{ img.id }}">
  <div class="img-photo">
    <img src="/upload/{{ img.relative_path }}" alt="{{ img.original_name or '拷贝纸标签' }}"
         onclick="showImgPreview('/upload/{{ img.relative_path }}')">
    <button class="copy-paper-label-del-btn lock-hide" data-image-id="{{ img.id }}">× 删除</button>
    {# 顶部水印区分 #}
    <div class="img-overlay-name">📋 拷贝纸标签</div>
  </div>
</div>
{% endfor %}
```

#### `templates/shipping-records.html`（line 1091-1117，copy-paper-area）

把现有 `{% for img in item.copy_paper_images %}` 改为只渲染 `source='count'`：

```jinja2
{% for img in item.copy_paper_images if img.source == 'count' %}
{# 原缩略图 + 张数输入框 + 删除按钮 — 不变 #}
{% endfor %}
```

#### `static/js/copy_paper.js` — 新增 label 删除 handler

```js
// 拷贝纸标签图删除(在普通商品图区,需要单独接管)
document.body.addEventListener('click', function (e) {
    var btn = e.target.closest('.copy-paper-label-del-btn');
    if (!btn) return;
    if (!confirm('删除这张标签图？')) return;
    var iid = btn.getAttribute('data-image-id');
    fetch('/api/v1/shipping-orders/copy-paper-images/' + iid, { method: 'DELETE' })
        .then(function (r) { return r.json(); })
        .then(function (j) {
            if (!j.success) { alert('删除失败'); return; }
            // 简单方案:整行刷新(后续可优化为局部)
            location.reload();
        });
});
```

#### `static/css/app.css` — 新增样式

```css
/* 2026-09-09: 拷贝纸标签图(混排到普通商品图区,虚线边框区分) */
.shipping-page .img-item-copy-paper-label {
    border: 1px dashed #74b9ff;
}
.shipping-page .img-item-copy-paper-label .copy-paper-label-del-btn {
    position: absolute;
    top: 4px;
    right: 4px;
    background: rgba(220, 38, 38, 0.85);
    color: #fff;
    border: none;
    border-radius: 4px;
    padding: 2px 6px;
    font-size: 0.7rem;
    cursor: pointer;
    opacity: 0;
    transition: opacity 0.15s;
}
.shipping-page .img-item-copy-paper-label:hover .copy-paper-label-del-btn {
    opacity: 1;
}
```

## 文件改动清单

| 文件 | 改动 |
|---|---|
| `blueprints/shipping.py` | record 富化段后追加 12 行:label 图注入 order_non_ai |
| `templates/shipping-records.html` | (a) 普通图区追加 label 缩略图循环 (b) copy-paper-area 加 `if img.source == 'count'` 过滤 |
| `static/js/copy_paper.js` | 新增 `.copy-paper-label-del-btn` 点击 handler (~10 行) |
| `static/css/app.css` | 末尾追加 `.img-item-copy-paper-label` + `.copy-paper-label-del-btn` 样式 (~18 行) |
| `tests/test_copy_paper_images.py` | 新增 2 个测试:`test_label_image_renders_in_order_images_area` / `test_count_image_still_renders_in_copy_paper_area` |

## 测试计划

`tests/test_copy_paper_images.py` 新增：

1. **`test_label_image_renders_in_order_images_area`**：
   - 构造 fixture：拷贝纸 record + 1 张 `source='label'` + 1 张 `source='count'` 图
   - 调 `shipping_records()` 主富化逻辑（用 `client.get('/shipping-records')` 触发）
   - 断言：label 图 HTML 出现在 `<div class="order-images-area">` 内
   - 断言：label 图带 `.img-item-copy-paper-label` class

2. **`test_count_image_still_renders_in_copy_paper_area`**：
   - 同样 fixture
   - 断言：count 图 HTML 出现在 `<div class="copy-paper-area">` 内
   - 断言：label 图 **不**出现在 `<div class="copy-paper-area">`

3. **现有 22 个测试保持 PASS**：
   - `test_template_buttons_have_onclick` 不受影响
   - `test_e2e_full_flow` 不受影响
   - 其他 HTTP/CRUD 测试不受影响

## 风险 / 回滚

- 后端仅 1 处注入,易回滚(删 12 行)
- 模板仅追加(不替换),回滚 = 删追加块
- JS/CSS 增量,直接删即可
- 完全无 OCR 风险(标签图从不走 RecordImageProcessor)
- 无数据迁移(数据一直在 CopyPaperImage 表,只是渲染位置换了)

## 范围边界

- ❌ 不做张数图移动(用户明确要求保留在 copy-paper-area)
- ❌ 不做 mobile_shipping.py 同步修改(用户只提 PC 页面,移动端按原样)
- ❌ 不优化 label 删除的局部刷新(简单方案:location.reload())
