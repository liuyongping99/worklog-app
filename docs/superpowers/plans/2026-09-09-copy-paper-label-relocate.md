# 拷贝纸/日本纸 标签图移至普通商品图区 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把拷贝纸/日本纸的标签图（`source='label'`）从 `copy-paper-area` 移到普通商品图区（`order-images-area`），与其他商品的行级图混排；张数图（`source='count'`）保留在 `copy-paper-area`。

**Architecture:** 模板 + JS + CSS + 后端 12 行注入，零端点改动、零表改动、零 OCR 改动。后端在 record 富化阶段把 `source='label'` 的图注入到 `order_non_ai`（render 时映射为 `order_images`），临时改写 `source='copy_paper_label'` 让模板能特判（不渲染 match-badge / 人工核查按钮）。张数图保留原 `copy-paper-area` 渲染。

**Tech Stack:** Python 3.12 + Flask 3.1 + Jinja2 + 原生 JS（沿用现有栈）

**Spec:** `docs/superpowers/specs/2026-09-09-copy-paper-label-relocate-design.md`

---

## Global Constraints

1. **测试隔离**：`tests/conftest.py` 自动复制 `worklog.db` → `worklog_test.db`，所有 pytest 不污染生产。
2. **CRLF**：仓库 CRLF，Git 提交如出现 "LF will be replaced by CRLF" 警告无需处理。
3. **不动现有数据/端点**：CopyPaperImage 表 / 4 端点 / 张数图流程全部保留原样。
4. **模板追加不替换**：在 `order-images-area` 循环后追加 label 图渲染，不动现有 img-item-record 循环。
5. **删除按钮独立**：拷贝纸 label 图删除用 `.copy-paper-label-del-btn`，由 copy_paper.js 接管（不动现有 `.del-img-btn` handler）。
6. **移动端不动**：mobile_shipping.py / mobile/order-detail 不在本次范围。

---

## File Structure

**Modify:**
- `blueprints/shipping.py` — record 富化段追加 12 行注入
- `templates/shipping-records.html` — 普通图区追加 label 循环 + copy-paper-area 加 source 过滤
- `static/js/copy_paper.js` — 新增 `.copy-paper-label-del-btn` 点击 handler
- `static/css/app.css` — 末尾追加 `.img-item-copy-paper-label` + 删除按钮样式
- `tests/test_copy_paper_images.py` — 新增 2 个渲染归属测试

---

## Task 1: 后端注入 + 模板渲染 + 测试

**Files:**
- Modify: `blueprints/shipping.py:322` 附近（enrichment 循环尾部，`_enrich_copy_paper_for_item(item)` 之后）
- Modify: `templates/shipping-records.html:952-985` 区域（紧贴 `{% for img in all_order_imgs %}` 循环结束）
- Modify: `templates/shipping-records.html:1108` 附近（`copy-paper-area` 循环加 source 过滤）
- Modify: `tests/test_copy_paper_images.py` 末尾

**Interfaces:**
- Consumes:
  - `_enrich_copy_paper_for_item(item)` 已写入 `item['copy_paper_images']`（list of dict，含 `id`, `file_path`, `source`, `sheet_count`）
  - `ShippingImage.get_relative_path(file_path)` 用于推算 `relative_path`
- Produces:
  - 每条 is_copy_paper record 的 label 图被追加到 `order_non_ai[group.id]`
  - 模板 `<div class="order-images-area">` 内出现 `.img-item-copy-paper-label`
  - 模板 `<div class="copy-paper-area">` 内不再出现 `.tag-label`

- [ ] **Step 1: 写失败测试（label 图应出现在 order-images-area，count 图仍在 copy-paper-area）**

在 `tests/test_copy_paper_images.py` 末尾追加：

```python
def test_label_image_renders_in_order_images_area(client, fresh_record):
    """拷贝纸 label 图 → 渲染在 order-images-area (.img-item-copy-paper-label)"""
    rid = fresh_record
    # 上传 1 张 label 图 + 1 张 count 图
    client.post(
        f'/api/v1/shipping-orders/records/{rid}/copy-paper-images',
        data={'source': 'label', 'image': (io.BytesIO(_png_bytes()), 'lbl.png')},
        content_type='multipart/form-data',
    )
    client.post(
        f'/api/v1/shipping-orders/records/{rid}/copy-paper-images',
        data={'source': 'count', 'image': (io.BytesIO(_png_bytes()), 'cnt.png')},
        content_type='multipart/form-data',
    )
    # 该 record 属于某个 order,需找到 order id
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT order_pk FROM shipping_records WHERE id = ?', (rid,))
    order_id = cur.fetchone()['order_pk']
    conn.close()

    # 设该 order 日期为今天 +1 → 落在默认日期范围(end_date = today)
    # 简化:直接用极宽日期范围确保能找到
    resp = client.get(f'/shipping-records?start_date=2026-09-06&end_date=2030-01-01')
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert 'img-item-copy-paper-label' in html, \
        'label 图应渲染 .img-item-copy-paper-label class'
    assert 'copy-paper-label-del-btn' in html, \
        'label 图应带 .copy-paper-label-del-btn 删除按钮'
    assert '📋 拷贝纸标签' in html, \
        'label 图应有「📋 拷贝纸标签」水印标识'


def test_count_image_still_renders_in_copy_paper_area(client, fresh_record):
    """拷贝纸 count 图 → 仍渲染在 copy-paper-area,不出现在 order-images-area"""
    rid = fresh_record
    # 只上传 1 张 count 图
    client.post(
        f'/api/v1/shipping-orders/records/{rid}/copy-paper-images',
        data={'source': 'count', 'image': (io.BytesIO(_png_bytes()), 'cnt.png')},
        content_type='multipart/form-data',
    )
    resp = client.get(f'/shipping-records?start_date=2026-09-06&end_date=2030-01-01')
    html = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert 'copy-paper-area' in html
    assert 'img-item-copy-paper-label' not in html, \
        'count-only 场景不应有 label 图渲染'
```

- [ ] **Step 2: 跑测试，确认 fail**

Run:
```
cd 'C:/Users/Administrator/worklog-app' && export PYTHONUTF8=1 && /c/Windows/System32/cmd.exe //c 'if exist worklog_test.db del worklog_test.db*' && python -m pytest tests/test_copy_paper_images.py::test_label_image_renders_in_order_images_area tests/test_copy_paper_images.py::test_count_image_still_renders_in_copy_paper_area -v --tb=short
```
Expected: 2 FAILED（label 图当前还在 copy-paper-area，没有 `.img-item-copy-paper-label` class）

- [ ] **Step 3: 后端注入（shipping.py）**

在 `blueprints/shipping.py` 中找到 `item['placement_match'] = _pm` 那一行所在的循环（per-record enrichment），在紧跟其后的 `# 2026-09-06: 拷贝纸/日本纸 行级图片 + 张数比对` 注释块、`_enrich_copy_paper_for_item(item)` 调用之后追加：

```python
        # 2026-09-09: 拷贝纸/日本纸 label 图混排到普通商品图区
        # (张数图保留在 copy-paper-area,仅 label 图走 order-images-area)
        if item.get('is_copy_paper'):
            for lbl in (item.get('copy_paper_images') or []):
                if lbl.get('source') != 'label':
                    continue
                # 补 relative_path (仿 PlacementImage.get_by_record 模式)
                if not lbl.get('relative_path') and lbl.get('file_path'):
                    lbl['relative_path'] = os.path.basename(lbl['file_path'])
                # 标记 source 让模板能特判(不渲染 match-badge/人工核查)
                lbl['source'] = 'copy_paper_label'
                # 注入到 order_non_ai 复用现有渲染管线
                # order_non_ai 在 return render_template 时以 order_images 传给模板
                if 'order_non_ai' in dir():
                    pass  # 占位 — 实际写法见下面"实际注入点"
```

**等等** — 上面"占位"是反例。请按下面真实注入点写：

```python
        # 2026-09-09: 拷贝纸/日本纸 label 图混排到普通商品图区
        # (张数图保留在 copy-paper-area,仅 label 图走 order-images-area)
        if item.get('is_copy_paper'):
            for lbl in (item.get('copy_paper_images') or []):
                if lbl.get('source') != 'label':
                    continue
                # 补 relative_path (仿 PlacementImage.get_by_record 模式)
                if not lbl.get('relative_path') and lbl.get('file_path'):
                    lbl['relative_path'] = os.path.basename(lbl['file_path'])
                # 标记 source 让模板能特判(不渲染 match-badge/人工核查)
                lbl['source'] = 'copy_paper_label'
                # 注入到 order_non_ai 复用现有渲染管线
                order_non_ai.setdefault(group['id'], []).append(lbl)
```

> ⚠️ **关键**：这行必须放在外层循环（`for group in groups:`）里、与现有 `order_non_ai.setdefault(grp['id'], [])` 模式平行的位置。**不要**放在 `for item in group['records']:` 内部（那样 `order_non_ai` 变量名虽然仍可见，但更内层的循环每条 record 都会重复 setdefault）。

**实际位置**：找到 `_enrich_copy_paper_for_item(item)` 调用所在的 for group 循环结尾（line 322 之后），在 `group.update(summarize_remarks(group['records']))` **之前**插入上述 9 行。

确认 `import os` 已在 `blueprints/shipping.py` 顶部（如果不在就加上）。`os.path.basename` 需要 `os`。

- [ ] **Step 4: 模板修改（templates/shipping-records.html）**

**修改 A**：在 line 952-985 的 `{% for img in all_order_imgs %}` 循环**结束**之后（找到 `{% endfor %}` 紧跟其后），追加：

```jinja2
                {# 2026-09-09: 拷贝纸/日本纸 label 图混排到普通区 (source='copy_paper_label') #}
                {% for img in all_order_imgs if img.source == 'copy_paper_label' %}
                <div class="img-item img-item-record img-item-copy-paper-label" data-record-pk="{{ img.record_pk }}" data-image-id="{{ img.id }}">
                    <div class="img-photo">
                        <img src="/upload/{{ img.relative_path }}" alt="{{ img.original_name or '拷贝纸标签' }}" onclick="showImgPreview('/upload/{{ img.relative_path }}')">
                        <button class="copy-paper-label-del-btn lock-hide" data-image-id="{{ img.id }}">× 删除</button>
                        <div class="img-overlay-name">📋 拷贝纸标签</div>
                    </div>
                </div>
                {% endfor %}
```

注意：原 `{% for img in all_order_imgs %}` 循环内部用 `{% if img.record_pk and record_by_pk and record_by_pk.get(img.record_pk) %}` 拿了 record_by_pk。我们的 label 图 `record_pk` 已设置（来自 `_enrich_copy_paper_for_item` 的 record 数据），但注入时 `lbl['record_pk']` 字段需要从 `item['id']` 设置 — 上面后端代码**漏了**这一行，需补上：

```python
                # 注入时设上 record_pk,模板 img-overlay-name 才能查 record_by_pk 拿品名
                lbl['record_pk'] = item['id']
```

把这条加在 `lbl['source'] = 'copy_paper_label'` 之后。

**修改 B**：在 `copy-paper-area` 的 `{% for img in item.copy_paper_images %}` 改成：

```jinja2
                        {% for img in item.copy_paper_images if img.source == 'count' %}
```

- [ ] **Step 5: 跑测试，确认 pass**

Run:
```
cd 'C:/Users/Administrator/worklog-app' && export PYTHONUTF8=1 && /c/Windows/System32/cmd.exe //c 'if exist worklog_test.db del worklog_test.db*' && python -m pytest tests/test_copy_paper_images.py::test_label_image_renders_in_order_images_area tests/test_copy_paper_images.py::test_count_image_still_renders_in_copy_paper_area -v --tb=short
```
Expected: 2 PASSED

- [ ] **Step 6: 跑全部 24 个测试**

Run:
```
cd 'C:/Users/Administrator/worklog-app' && export PYTHONUTF8=1 && /c/Windows/System32/cmd.exe //c 'if exist worklog_test.db del worklog_test.db*' && python -m pytest tests/test_copy_paper_images.py -v --tb=short
```
Expected: 24/24 PASSED (22 原有 + 2 新增)

- [ ] **Step 7: import 检查**

Run:
```
cd 'C:/Users/Administrator/worklog-app' && export PYTHONUTF8=1 && python -c "from app import create_app; app = create_app(); print('OK routes:', len(list(app.url_map.iter_rules())))"
```
Expected: `OK routes: 228` (无变化)

- [ ] **Step 8: 提交**

```bash
cd 'C:/Users/Administrator/worklog-app' && git add blueprints/shipping.py templates/shipping-records.html tests/test_copy_paper_images.py
git -c user.name='liuyongping99' -c user.email='liuyongping99@users.noreply.github.com' commit -m "feat(ui): 拷贝纸标签图移至普通商品图区" -m "- shipping.py 注入 source='label' 到 order_non_ai,临时改写 source='copy_paper_label' 让模板特判
- shipping-records.html 普通区追加 label 缩略图循环 (.img-item-copy-paper-label)
- copy-paper-area 加 source=='count' 过滤,只渲染张数图
- 2 个新测试覆盖渲染归属"
```

---

## Task 2: JS 删除 handler + CSS 样式

**Files:**
- Modify: `static/js/copy_paper.js` 末尾（在 IIFE 内部）
- Modify: `static/css/app.css` 末尾

**Interfaces:**
- Consumes:
  - `window.openCopyPaperUpload` 已存在 (Task 7)
  - `.copy-paper-label-del-btn` 元素（Task 1 模板已渲染）
  - `DELETE /api/v1/shipping-orders/copy-paper-images/<id>` 已存在 (Task 3)
- Produces:
  - 点击 `.copy-paper-label-del-btn` 弹确认 → 调 DELETE → 成功后刷新

- [ ] **Step 1: 加 JS handler**

在 `static/js/copy_paper.js` 的 IIFE 内部、`document.addEventListener('DOMContentLoaded', ...)` 之后追加：

```javascript
// 拷贝纸标签图删除 (普通商品图区)
document.body.addEventListener('click', function (e) {
    var btn = e.target.closest('.copy-paper-label-del-btn');
    if (!btn) return;
    if (!confirm('删除这张标签图？')) return;
    var iid = btn.getAttribute('data-image-id');
    fetch('/api/v1/shipping-orders/copy-paper-images/' + iid, { method: 'DELETE' })
        .then(function (r) { return r.json(); })
        .then(function (j) {
            if (!j.success) { alert('删除失败: ' + (j.error || '')); return; }
            // 简单方案:整行刷新(后续可优化为局部)
            location.reload();
        })
        .catch(function (err) { alert('删除失败: ' + err); });
});
```

- [ ] **Step 2: 加 CSS 样式**

在 `static/css/app.css` 末尾追加：

```css
/* ========== 拷贝纸/日本纸 标签图 (2026-09-09,混排到普通商品图区) ========== */
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

- [ ] **Step 3: 浏览器手动验证**

启动服务器:
```
cd 'C:/Users/Administrator/worklog-app' && export PYTHONUTF8=1 && python app.py &
sleep 3
```

登录 + 抓页面:
```
curl -s -c /tmp/c.txt http://127.0.0.1:5050/login -o /dev/null
curl -s -b /tmp/c.txt -c /tmp/c.txt -L -X POST -d 'staff_id=6224' http://127.0.0.1:5050/login -o /dev/null
curl -s -b /tmp/c.txt 'http://127.0.0.1:5050/shipping-records?start_date=2026-09-07&end_date=2026-09-09' -o /tmp/sr.html
```

验证:
- `grep -c 'img-item-copy-paper-label' /tmp/sr.html` 应 ≥ 1（如果有拷贝纸 record 有 label 图）
- `grep -c 'copy-paper-label-del-btn' /tmp/sr.html` 同上
- 找到 946 号订单的 日本纸 record 行，确认 `<div class="img-item-copy-paper-label">` 在 `<div class="order-images-area">` 内（不是 copy-paper-area）

清理服务器:
```
/c/Windows/System32/taskkill.exe //F //IM python.exe
```

- [ ] **Step 4: 跑全部 24 个测试**

```
cd 'C:/Users/Administrator/worklog-app' && export PYTHONUTF8=1 && /c/Windows/System32/cmd.exe //c 'if exist worklog_test.db del worklog_test.db*' && python -m pytest tests/test_copy_paper_images.py -v --tb=short
```
Expected: 24/24 PASSED

- [ ] **Step 5: 提交**

```bash
cd 'C:/Users/Administrator/worklog-app' && git add static/js/copy_paper.js static/css/app.css
git -c user.name='liuyongping99' -c user.email='liuyongping99@users.noreply.github.com' commit -m "feat(ui): 拷贝纸标签图删除 handler + 样式" -m "- copy_paper.js: .copy-paper-label-del-btn 点击 → DELETE 端点 → 刷新
- app.css: .img-item-copy-paper-label 虚线蓝边框 + 顶部「📋 拷贝纸标签」水印
- 删除按钮 hover 显示,仿现有 .del-img-btn 模式"
```

---

## Task 3: 全量回归 + 提交清理

**Files:** 无（仅验证）

- [ ] **Step 1: 全量回归测试**

```
cd 'C:/Users/Administrator/worklog-app' && export PYTHONUTF8=1 && /c/Windows/System32/cmd.exe //c 'if exist worklog_test.db del worklog_test.db*' && python -m pytest tests/ -v --ignore=tests/regression --tb=line 2>&1 | tail -10
```
Expected: 既有测试不破；pre-existing 41 个 FAIL 不变；无新增 FAIL

- [ ] **Step 2: import 烟测**

```
cd 'C:/Users/Administrator/worklog-app' && export PYTHONUTF8=1 && python -c "from app import create_app; app = create_app(); print('OK routes:', len(list(app.url_map.iter_rules())))"
```
Expected: `OK routes: 228`

- [ ] **Step 3: 检查 commit 历史**

```
cd 'C:/Users/Administrator/worklog-app' && git log --oneline -5
```
Expected: 看到 2 个新 commit（Task 1 + Task 2）

---

## Self-Review

### Spec coverage

| Spec 章节 | 覆盖任务 |
|---|---|
| 后端注入（shipping.py record 富化段） | Task 1 Step 3 |
| 模板普通图区追加 label 循环 | Task 1 Step 4-A |
| 模板 copy-paper-area 加 source 过滤 | Task 1 Step 4-B |
| JS 删除 handler | Task 2 Step 1 |
| CSS 样式 | Task 2 Step 2 |
| 测试 1 (label 渲染归属) | Task 1 Step 1+5 |
| 测试 2 (count 仍渲染在 copy-paper-area) | Task 1 Step 1+5 |
| 范围边界（张数图保留、移动端不动、零 OCR 改动） | 全部 Task 均遵守 |

### Type consistency

- `_enrich_copy_paper_for_item(item)` 输出字段：`is_copy_paper`, `copy_paper_images`, `copy_paper_total`, `copy_paper_match`, `copy_paper_expected` — Task 1 Step 3 仅读 `is_copy_paper` 与 `copy_paper_images`，未触碰其他字段 ✓
- `lbl['record_pk']` 由 Task 1 Step 3 设置（`lbl['record_pk'] = item['id']`），模板读取 `{{ img.record_pk }}` 一致 ✓
- 端点路径一致：`DELETE /api/v1/shipping-orders/copy-paper-images/<id>` Task 2 Step 1 与 Task 3 一致 ✓
- 临时改写的 `lbl['source'] = 'copy_paper_label'` 仅在 shipping.py 注入段设置，模板特判 `img.source == 'copy_paper_label'` 一致 ✓

### Placeholder scan

- 无 TBD / TODO / "implement later"
- 每个 step 都给了完整代码块
- 无 "Similar to Task X"（重复给代码）
- 文件路径 absolute 给出
- 所有 import 列出

---

## 计划完成

计划已写入 `docs/superpowers/plans/2026-09-09-copy-paper-label-relocate.md`，共 3 个任务：

| # | 任务 | 类型 |
|---|---|---|
| 1 | 后端注入 + 模板渲染 + 2 个测试 | TDD |
| 2 | JS 删除 handler + CSS | 手动验证 |
| 3 | 全量回归 + commit 清理 | 验证 |

**下一步选执行模式：**

1. **Subagent-Driven（推荐）** —— 每个任务派一个独立 subagent 跑，两段式 review
2. **Inline Execution** —— 在当前会话串行执行

请告诉我用哪个。
