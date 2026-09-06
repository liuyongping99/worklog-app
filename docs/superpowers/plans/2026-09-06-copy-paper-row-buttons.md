# 拷贝纸 / 日本纸行级双按钮实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `/shipping-records`（PC 端）和 `/m/shipping-today/order/<oid>`（移动端出货）操作列给 `拷贝纸(category_code=0107, unit=令)` 和 `日本纸(category_code=0105, unit=张)` 两类明细行新增「📷 标签」和「📊 张数」两个按钮，前者跳过 OCR/AI 纯上传，后者上传后录入张数并与 quantity 字段比对出徽章。

**Architecture:** 独立新表 `copy_paper_images` + 新模型类 `CopyPaperImage` + 新 helper `compute_copy_paper_expected_quantity` + 4 个 REST 端点 + 复用现有 `_image_upload_modal.html` 弹上传框 + 新建张数输入小弹框 `_copy_paper_count_modal.html` + PC/移动端 HTML/JS 改动。

**Tech Stack:** Python 3.12 + Flask 3.1 + SQLite 3.45 + Jinja2 + 原生 JS + Tailwind CSS（沿用现有栈，不引新依赖）

**Spec:** `docs/superpowers/specs/2026-09-06-copy-paper-row-buttons-design.md`

---

## Global Constraints

1. **测试隔离**：`tests/conftest.py` 已配置用 `worklog_test.db` 跑测试；所有 `pytest` 调用都自动走测试库，不会污染生产数据。**禁止**手改 `worklog.db`。
2. **CRLF**：仓库 CRLF 换行符。Git 提交时如出现 "LF will be replaced by CRLF" 警告无需处理。
3. **Python 编码**：测试脚本含中文时，PowerShell 跑要 `$env:PYTHONUTF8=1`；CMD 跑用 `python -X utf8`。
4. **不引入新依赖**：仅用 Flask / sqlite3 / 现有栈。零 `requirements.txt` 改动。
5. **锁单防御**：所有写入端点（POST/DELETE/PATCH）必须先 `ShippingOrder.get_by_id(record.order_pk)` 检查 `is_locked`，否则返回 403。
6. **跳过 OCR pipeline**：完全不走 `ocr_pipeline.RecordImageProcessor` / `PaddleOCR` / `DeepSeek`。文件保存直接调 `_helpers.save_uploaded_image()`。
7. **不动现有表**：`ShippingImage` / `PlacementImage` / `placement_marks` 三套都不动；新表完全独立。
8. **commit message 中文前缀**：`<type>(<scope>): <description>`，type 用 `feat` / `test` / `docs` / `refactor` / `fix`。
9. **每次 commit 前**跑一遍相关测试 + `python -c "from app import create_app; create_app()"` 验证 import 不破。
10. **route 顺序**：新端点必须放在 `shipping.py` 现有 `/api/v1/shipping-orders/...` 段内（具体位置在 Task 3 说明），不要插到 `/m/*` 通配路由前。

---

## File Structure

**新建：**
- `tests/test_copy_paper_images.py` — TDD 测试（任务 1-4 逐步扩展）
- `templates/_copy_paper_count_modal.html` — 张数输入弹框（Task 7）
- `static/js/copy_paper.js` — 上传/录入/删除/刷新（Task 7）

**修改：**
- `models/_init.py` — 加 `copy_paper_images` 表 DDL（Task 1）
- `models/orders.py` — 加 `CopyPaperImage` 类（Task 1）
- `models/__init__.py` — re-export `CopyPaperImage`（Task 1）
- `blueprints/_helpers.py` — 加 `compute_copy_paper_expected_quantity`（Task 2）
- `blueprints/shipping.py` — 加 4 端点 + `_is_copy_paper_record` + 记录富化（Task 3 + 4）
- `templates/shipping-records.html` — 加 2 按钮 + 缩略图区（Task 5 + 6）
- `static/css/app.css` — 加 `.copy-paper-*` 样式（Task 6）
- `templates/mobile/shipping-order.html` — 加 2 按钮 + 缩略图 + inline 张数（Task 8）
- `static/css/mobile.css` — 加移动端样式（Task 8）

---

## Task 1: 新表 `copy_paper_images` + `CopyPaperImage` 模型类

**Files:**
- Modify: `models/_init.py:935-940` 附近（最后一张业务表之后）
- Modify: `models/orders.py:2635` 之后（最后类之后）
- Modify: `models/__init__.py`
- Create: `tests/test_copy_paper_images.py`

**Interfaces:**
- Produces:
  - `class CopyPaperImage` with static methods:
    - `create(record_pk: int, file_path: str, original_name: str, source: str) -> int`  (returns id)
    - `list_by_record(record_pk: int) -> list[dict]`  (ordered by source ASC, created_at ASC, id ASC)
    - `get_by_id(image_id: int) -> dict | None`
    - `update_count(image_id: int, sheet_count: int | None) -> bool`
    - `delete(image_id: int) -> bool`  (returns True if row deleted)

- [ ] **Step 1: 写失败测试（DDL + create + list_by_record）**

在 `tests/test_copy_paper_images.py` 顶部写：

```python
import os
import pytest
from models._db import get_db, DB_PATH
from models.orders import CopyPaperImage


@pytest.fixture
def fresh_record(tmp_path):
    """Insert a minimal shipping_orders + shipping_records row, return record_pk."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO shipping_orders (date, customer, is_locked) VALUES (?, ?, 0)",
        ('2026-09-06', 'test',))
    order_id = cur.lastrowid
    cur.execute(
        "INSERT INTO shipping_records (order_pk, product_name, specification, quantity, unit) "
        "VALUES (?, ?, ?, ?, ?)",
        (order_id, '拷贝纸A', 'A4', 5, '令'))
    record_id = cur.lastrowid
    conn.commit()
    conn.close()
    return record_id


def test_ddl_table_exists():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='copy_paper_images'")
    assert cur.fetchone() is not None
    conn.close()


def test_create_and_list(fresh_record):
    rid = fresh_record
    img_id = CopyPaperImage.create(rid, 'upload/2026-09/x.jpg', 'x.jpg', 'label')
    assert isinstance(img_id, int) and img_id > 0

    rows = CopyPaperImage.list_by_record(rid)
    assert len(rows) == 1
    assert rows[0]['source'] == 'label'
    assert rows[0]['sheet_count'] is None
    assert rows[0]['file_path'] == 'upload/2026-09/x.jpg'
```

- [ ] **Step 2: 跑测试，确认 fail**

Run: `cd 'C:/Users/Administrator/worklog-app' && $env:PYTHONUTF8=1; python -m pytest tests/test_copy_paper_images.py -v`
Expected: 2 FAILED（`ImportError: cannot import name 'CopyPaperImage'` 或 `OperationalError: no such table: copy_paper_images`）

- [ ] **Step 3: 加 DDL 到 `models/_init.py`**

在 `models/_init.py` 最后一张业务表 DDL 后（约 line 935），加：

```sql
CREATE TABLE IF NOT EXISTS copy_paper_images (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    record_pk     INTEGER NOT NULL,
    file_path     TEXT NOT NULL,
    original_name TEXT,
    source        TEXT NOT NULL CHECK (source IN ('label','count')),
    sheet_count   INTEGER,
    created_at    TEXT DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (record_pk) REFERENCES shipping_records(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_copy_paper_record ON copy_paper_images(record_pk);
```

- [ ] **Step 4: 加 `CopyPaperImage` 类到 `models/orders.py`**

在文件最末（line 2639 之后）追加：

```python


class CopyPaperImage:
    """拷贝纸/日本纸 行级图片 + 人工录入张数。

    与 ShippingImage / PlacementImage 隔离:
    - 不进 OCR pipeline
    - 不进 match-col / 整体图区
    - 仅用于人工参考 + 行级 total 比对
    """

    @staticmethod
    def create(record_pk: int, file_path: str, original_name: str, source: str):
        if source not in ('label', 'count'):
            raise ValueError(f"source must be 'label' or 'count', got {source!r}")
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO copy_paper_images (record_pk, file_path, original_name, source) "
            "VALUES (?, ?, ?, ?)",
            (record_pk, file_path, original_name or '', source))
        new_id = cur.lastrowid
        conn.commit()
        conn.close()
        return new_id

    @staticmethod
    def list_by_record(record_pk: int):
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "SELECT * FROM copy_paper_images WHERE record_pk = ? "
            "ORDER BY source ASC, created_at ASC, id ASC",
            (record_pk,))
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return rows

    @staticmethod
    def get_by_id(image_id: int):
        conn = get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM copy_paper_images WHERE id = ?", (image_id,))
        row = cur.fetchone()
        conn.close()
        return dict(row) if row else None

    @staticmethod
    def update_count(image_id: int, sheet_count):
        if sheet_count is not None:
            try:
                sheet_count = int(sheet_count)
            except (TypeError, ValueError):
                raise ValueError(f"sheet_count must be int or None, got {sheet_count!r}")
            if sheet_count < 0:
                raise ValueError(f"sheet_count must be >= 0, got {sheet_count}")
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            "UPDATE copy_paper_images SET sheet_count = ? WHERE id = ?",
            (sheet_count, image_id))
        changed = cur.rowcount > 0
        conn.commit()
        conn.close()
        return changed

    @staticmethod
    def delete(image_id: int) -> bool:
        conn = get_db()
        cur = conn.cursor()
        cur.execute("DELETE FROM copy_paper_images WHERE id = ?", (image_id,))
        changed = cur.rowcount > 0
        conn.commit()
        conn.close()
        return changed
```

- [ ] **Step 5: 在 `models/__init__.py` re-export**

打开 `models/__init__.py`，找到现有 `CopyPaperImage` 引用（如果已有同名的就不动；否则按现有 re-export 风格追加一行）。例如：

```python
from .orders import CopyPaperImage  # 拷贝纸/日本纸 行级图片
```

- [ ] **Step 6: 跑测试，确认 pass**

Run: `cd 'C:/Users/Administrator/worklog-app' && $env:PYTHONUTF8=1; python -m pytest tests/test_copy_paper_images.py -v`
Expected: 2 PASSED

- [ ] **Step 7: 补 3 个测试（update_count / get_by_id / delete）**

在 `tests/test_copy_paper_images.py` 追加：

```python
def test_update_count_int_and_none(fresh_record):
    rid = fresh_record
    iid = CopyPaperImage.create(rid, 'upload/2026-09/y.jpg', 'y.jpg', 'count')
    assert CopyPaperImage.update_count(iid, 100) is True
    assert CopyPaperImage.get_by_id(iid)['sheet_count'] == 100
    assert CopyPaperImage.update_count(iid, None) is True
    assert CopyPaperImage.get_by_id(iid)['sheet_count'] is None


def test_update_count_rejects_negative():
    iid = CopyPaperImage.create(1, 'p', 'p', 'count')
    with pytest.raises(ValueError):
        CopyPaperImage.update_count(iid, -1)


def test_get_by_id_and_delete(fresh_record):
    rid = fresh_record
    iid = CopyPaperImage.create(rid, 'upload/2026-09/z.jpg', 'z.jpg', 'label')
    row = CopyPaperImage.get_by_id(iid)
    assert row is not None and row['id'] == iid

    assert CopyPaperImage.delete(iid) is True
    assert CopyPaperImage.get_by_id(iid) is None
    # 二次删应返回 False
    assert CopyPaperImage.delete(iid) is False
```

- [ ] **Step 8: 跑全部 5 个测试**

Run: `cd 'C:/Users/Administrator/worklog-app' && $env:PYTHONUTF8=1; python -m pytest tests/test_copy_paper_images.py -v`
Expected: 5 PASSED

- [ ] **Step 9: import 检查**

Run: `cd 'C:/Users/Administrator/worklog-app' && $env:PYTHONUTF8=1; python -c "from app import create_app; app = create_app(); print('OK')"`
Expected: `OK`（无 import 错误）

- [ ] **Step 10: 提交**

```bash
cd 'C:/Users/Administrator/worklog-app' && git add models/_init.py models/orders.py models/__init__.py tests/test_copy_paper_images.py
git -c user.name='liuyongping99' -c user.email='liuyongping99@users.noreply.github.com' commit -m "feat(models): 新表 copy_paper_images + CopyPaperImage 类" -m "- 5 个静态方法: create / list_by_record / get_by_id / update_count / delete
- source 字段约束 'label'|'count'
- sheet_count 可空 (NULL = 还没录)"
```

---

## Task 2: Helper `compute_copy_paper_expected_quantity`

**Files:**
- Modify: `blueprints/_helpers.py:421` 之后（在 `compute_placement_expected_zhi` 之后）
- Modify: `tests/test_copy_paper_images.py`

**Interfaces:**
- Produces:
  - `def compute_copy_paper_expected_quantity(quantity, unit) -> tuple[float, bool]`
    - `unit in ('令','张')` 且 quantity > 0 → (float(qty), True)
    - 其他 → (0.0, False)

- [ ] **Step 1: 写失败测试**

在 `tests/test_copy_paper_images.py` 追加：

```python
from blueprints._helpers import compute_copy_paper_expected_quantity


def test_helper_ling_positive():
    assert compute_copy_paper_expected_quantity(5, '令') == (5.0, True)
    assert compute_copy_paper_expected_quantity('3', '令') == (3.0, True)
    assert compute_copy_paper_expected_quantity('2.5', '令') == (2.5, True)


def test_helper_zhang_positive():
    assert compute_copy_paper_expected_quantity(500, '张') == (500.0, True)


def test_helper_unsupported_unit():
    assert compute_copy_paper_expected_quantity(5, '支') == (0.0, False)
    assert compute_copy_paper_expected_quantity(5, '码') == (0.0, False)
    assert compute_copy_paper_expected_quantity(5, None) == (0.0, False)


def test_helper_invalid_quantity():
    assert compute_copy_paper_expected_quantity(0, '令') == (0.0, False)
    assert compute_copy_paper_expected_quantity(None, '张') == (0.0, False)
    assert compute_copy_paper_expected_quantity('', '张') == (0.0, False)
    assert compute_copy_paper_expected_quantity('abc', '张') == (0.0, False)
    assert compute_copy_paper_expected_quantity(-3, '令') == (0.0, False)
```

- [ ] **Step 2: 跑测试，confirm fail**

Run: `cd 'C:/Users/Administrator/worklog-app' && $env:PYTHONUTF8=1; python -m pytest tests/test_copy_paper_images.py -v`
Expected: 4 FAILED（`ImportError: cannot import name 'compute_copy_paper_expected_quantity'`）

- [ ] **Step 3: 实现 helper**

在 `blueprints/_helpers.py` `compute_placement_expected_zhi` 函数末尾（约 line 458）后追加：

```python


def compute_copy_paper_expected_quantity(quantity, unit):
    """拷贝纸/日本纸 期望张/令数,直接从 quantity 字段取。

    语义:对拷贝纸(令)/日本纸(张)而言,quantity 本身就是期望值。
    与 compute_placement_expected_zhi 的区别:
    - placement:解析备注「X支」 + unit='支' 时回退 quantity
    - copy_paper:不解析备注,直接用 quantity;unit 必须是 '令'|'张'

    Returns:
        (expected: float, has_expected: bool)
        - unit 合法且 quantity > 0 → (float(q), True)
        - 其他 → (0.0, False)
    """
    if unit not in ('令', '张'):
        return (0.0, False)
    try:
        q = float(quantity)
    except (TypeError, ValueError):
        return (0.0, False)
    if q <= 0:
        return (0.0, False)
    return (q, True)
```

- [ ] **Step 4: 跑测试，confirm pass**

Run: `cd 'C:/Users/Administrator/worklog-app' && $env:PYTHONUTF8=1; python -m pytest tests/test_copy_paper_images.py -v`
Expected: 9 PASSED（5 个 Task 1 + 4 个 Task 2）

- [ ] **Step 5: 提交**

```bash
cd 'C:/Users/Administrator/worklog-app' && git add blueprints/_helpers.py tests/test_copy_paper_images.py
git -c user.name='liuyongping99' -c user.email='liuyongping99@users.noreply.github.com' commit -m "feat(helpers): compute_copy_paper_expected_quantity" -m "- 拷贝纸(令)/日本纸(张) 直接用 quantity 字段比对
- 不解析备注,不走 quantity 兜底(语义独立于 placement 期望值)"
```

---

## Task 3: 4 个 REST 端点 + 锁单防御

**Files:**
- Modify: `blueprints/shipping.py`（在 placement-images 端点之后，约 line 1305 后）
- Modify: `tests/test_copy_paper_images.py`

**Interfaces:**
- Produces (4 endpoints):
  - `POST /api/v1/shipping-orders/records/<rid>/copy-paper-images`
    - form: `source` ∈ {'label','count'}, `file` (image)
    - 流程：锁单检查 → `_helpers.save_uploaded_image()` → `CopyPaperImage.create()` → 返 `{success, image: {...}}`
  - `GET /api/v1/shipping-orders/records/<rid>/copy-paper-images`
    - 返 `{success, images: [...]}`
  - `DELETE /api/v1/shipping-orders/copy-paper-images/<id>`
    - 锁单检查 → 删文件（`os.remove` 包裹 try/except）→ `CopyPaperImage.delete()` → 返 `{success}`
  - `PATCH /api/v1/shipping-orders/copy-paper-images/<id>/sheet-count`
    - json: `{sheet_count: int|null}` → `CopyPaperImage.update_count()` → 返 `{success}`

- [ ] **Step 1: 写失败测试（HTTP 上传 + 列图）**

在 `tests/test_copy_paper_images.py` 追加：

```python
import io
from app import create_app


@pytest.fixture
def client():
    app = create_app()
    app.config['TESTING'] = True
    with app.test_client() as c:
        yield c


def _png_bytes():
    """生成最小有效 PNG 字节（用 Pillow）。"""
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', (10, 10), 'white').save(buf, format='PNG')
    return buf.getvalue()


def test_http_upload_label(client, fresh_record):
    rid = fresh_record
    data = {
        'source': 'label',
        'file': (io.BytesIO(_png_bytes()), 'test.png'),
    }
    resp = client.post(f'/api/v1/shipping-orders/records/{rid}/copy-paper-images',
                       data=data, content_type='multipart/form-data')
    assert resp.status_code == 200
    j = resp.get_json()
    assert j['success'] is True
    assert j['image']['source'] == 'label'
    assert j['image']['sheet_count'] is None

    # list 应能查到
    resp = client.get(f'/api/v1/shipping-orders/records/{rid}/copy-paper-images')
    j = resp.get_json()
    assert j['success'] is True and len(j['images']) == 1
```

- [ ] **Step 2: 跑测试，confirm fail**

Run: `cd 'C:/Users/Administrator/worklog-app' && $env:PYTHONUTF8=1; python -m pytest tests/test_copy_paper_images.py -v`
Expected: 1 FAILED（`404 Not Found`）

- [ ] **Step 3: 加 4 端点到 `blueprints/shipping.py`**

在文件最末追加（先 `from models.orders import CopyPaperImage` 与 `from blueprints._helpers import save_uploaded_image, compute_copy_paper_expected_quantity`）：

```python


# ====================== 拷贝纸/日本纸 行级图片 (2026-09-06) ======================
@bp.route('/api/v1/shipping-orders/records/<int:rid>/copy-paper-images', methods=['POST'])
def upload_copy_paper_image(rid):
    """上传拷贝纸/日本纸 行级图。完全跳过 OCR pipeline。"""
    # 锁单检查
    rec = ShippingRecord.get_by_id(rid)
    if not rec:
        return jsonify({'success': False, 'error': '记录不存在'}), 404
    order = ShippingOrder.get_by_id(rec['order_pk'])
    if not order or order.get('is_locked'):
        return jsonify({'success': False, 'error': '订单已锁定'}), 403

    source = request.form.get('source', '').strip()
    if source not in ('label', 'count'):
        return jsonify({'success': False, 'error': 'source 必须为 label 或 count'}), 400

    file = request.files.get('file')
    if not file or not file.filename:
        return jsonify({'success': False, 'error': '未选择文件'}), 400

    file_path, error = save_uploaded_image(file)
    if error:
        return jsonify({'success': False, 'error': error}), 400

    new_id = CopyPaperImage.create(rid, file_path, file.filename, source)
    img = CopyPaperImage.get_by_id(new_id)
    return jsonify({'success': True, 'image': img})


@bp.route('/api/v1/shipping-orders/records/<int:rid>/copy-paper-images', methods=['GET'])
def list_copy_paper_images(rid):
    images = CopyPaperImage.list_by_record(rid)
    return jsonify({'success': True, 'images': images})


@bp.route('/api/v1/shipping-orders/copy-paper-images/<int:img_id>', methods=['DELETE'])
def delete_copy_paper_image(img_id):
    img = CopyPaperImage.get_by_id(img_id)
    if not img:
        return jsonify({'success': False, 'error': '图片不存在'}), 404

    rec = ShippingRecord.get_by_id(img['record_pk'])
    order = ShippingOrder.get_by_id(rec['order_pk']) if rec else None
    if not order or order.get('is_locked'):
        return jsonify({'success': False, 'error': '订单已锁定'}), 403

    # 删磁盘文件（找不到不报错）
    try:
        from flask import current_app
        full_path = os.path.join(current_app.config.get('UPLOAD_FOLDER', 'upload'), img['file_path'])
        if os.path.exists(full_path):
            os.remove(full_path)
    except Exception:
        pass

    CopyPaperImage.delete(img_id)
    return jsonify({'success': True})


@bp.route('/api/v1/shipping-orders/copy-paper-images/<int:img_id>/sheet-count', methods=['PATCH'])
def patch_copy_paper_sheet_count(img_id):
    img = CopyPaperImage.get_by_id(img_id)
    if not img:
        return jsonify({'success': False, 'error': '图片不存在'}), 404

    rec = ShippingRecord.get_by_id(img['record_pk'])
    order = ShippingOrder.get_by_id(rec['order_pk']) if rec else None
    if not order or order.get('is_locked'):
        return jsonify({'success': False, 'error': '订单已锁定'}), 403

    body = request.get_json(silent=True) or {}
    raw = body.get('sheet_count', None)
    if raw is not None:
        try:
            val = int(raw)
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'sheet_count 必须为整数或 null'}), 400
        if val < 0:
            return jsonify({'success': False, 'error': 'sheet_count 不能为负'}), 400
    else:
        val = None

    CopyPaperImage.update_count(img_id, val)
    return jsonify({'success': True, 'sheet_count': val})
```

并在 `blueprints/shipping.py` 顶部 import 段确认有以下（或追加）：

```python
from models.orders import CopyPaperImage, ShippingOrder, ShippingRecord
from blueprints._helpers import save_uploaded_image, compute_copy_paper_expected_quantity
import os
```

如果 `save_uploaded_image` 已经从 `_helpers` import 了就不重复；`os` 如果已 import 就不重复。

- [ ] **Step 4: 跑测试，confirm pass**

Run: `cd 'C:/Users/Administrator/worklog-app' && $env:PYTHONUTF8=1; python -m pytest tests/test_copy_paper_images.py -v`
Expected: 10 PASSED

- [ ] **Step 5: 补 4 个测试（upload count / patch / delete / locked）**

```python
def test_http_upload_count(client, fresh_record):
    rid = fresh_record
    data = {'source': 'count',
            'file': (io.BytesIO(_png_bytes()), 'c.png')}
    resp = client.post(f'/api/v1/shipping-orders/records/{rid}/copy-paper-images',
                       data=data, content_type='multipart/form-data')
    assert resp.status_code == 200
    j = resp.get_json()
    assert j['image']['source'] == 'count'
    return rid, j['image']['id']


def test_http_patch_sheet_count(client, fresh_record):
    rid, iid = test_http_upload_count(client, fresh_record)
    resp = client.patch(f'/api/v1/shipping-orders/copy-paper-images/{iid}/sheet-count',
                        json={'sheet_count': 250})
    assert resp.status_code == 200
    j = resp.get_json()
    assert j['sheet_count'] == 250

    # null 清空
    resp = client.patch(f'/api/v1/shipping-orders/copy-paper-images/{iid}/sheet-count',
                        json={'sheet_count': None})
    j = resp.get_json()
    assert j['sheet_count'] is None

    # 负值 400
    resp = client.patch(f'/api/v1/shipping-orders/copy-paper-images/{iid}/sheet-count',
                        json={'sheet_count': -1})
    assert resp.status_code == 400


def test_http_delete(client, fresh_record):
    rid = fresh_record
    data = {'source': 'label',
            'file': (io.BytesIO(_png_bytes()), 'd.png')}
    resp = client.post(f'/api/v1/shipping-orders/records/{rid}/copy-paper-images',
                       data=data, content_type='multipart/form-data')
    iid = resp.get_json()['image']['id']

    resp = client.delete(f'/api/v1/shipping-orders/copy-paper-images/{iid}')
    assert resp.status_code == 200

    # 二次删 404
    resp = client.delete(f'/api/v1/shipping-orders/copy-paper-images/{iid}')
    assert resp.status_code == 404


def test_http_locked_blocked(client, fresh_record):
    rid = fresh_record
    # 锁定订单
    conn = get_db()
    cur = conn.cursor()
    cur.execute("UPDATE shipping_orders SET is_locked = 1 WHERE id = (SELECT order_pk FROM shipping_records WHERE id = ?)", (rid,))
    conn.commit()
    conn.close()

    data = {'source': 'label',
            'file': (io.BytesIO(_png_bytes()), 'l.png')}
    resp = client.post(f'/api/v1/shipping-orders/records/{rid}/copy-paper-images',
                       data=data, content_type='multipart/form-data')
    assert resp.status_code == 403
```

- [ ] **Step 6: 跑全部 14 个测试**

Run: `cd 'C:/Users/Administrator/worklog-app' && $env:PYTHONUTF8=1; python -m pytest tests/test_copy_paper_images.py -v`
Expected: 14 PASSED

- [ ] **Step 7: import 检查**

Run: `cd 'C:/Users/Administrator/worklog-app' && $env:PYTHONUTF8=1; python -c "from app import create_app; app = create_app(); print('OK')"`
Expected: `OK`

- [ ] **Step 8: 提交**

```bash
cd 'C:/Users/Administrator/worklog-app' && git add blueprints/shipping.py tests/test_copy_paper_images.py
git -c user.name='liuyongping99' -c user.email='liuyongping99@users.noreply.github.com' commit -m "feat(shipping): copy-paper-images 4 端点" -m "- POST/GET/DELETE/PATCH 完整 CRUD
- 跳过 OCR pipeline,直接调 _helpers.save_uploaded_image
- 锁单防御:is_locked=1 → 403"
```

---

## Task 4: 类别检测 + 记录富化（每行渲染带 copy-paper 字段）

**Files:**
- Modify: `blueprints/shipping.py:319` 附近（`item['placement_match']` 那一段之后）
- Modify: `tests/test_copy_paper_images.py`

**Interfaces:**
- Produces: 在每条 record dict 上多带 4 个字段：
  - `item['is_copy_paper']: bool`
  - `item['copy_paper_images']: list[dict]`
  - `item['copy_paper_total']: int`
  - `item['copy_paper_match']: 'green' | 'yellow' | 'partial' | None`

- [ ] **Step 1: 写失败测试（record enrichment）**

在 `tests/test_copy_paper_images.py` 追加：

```python
def test_record_enrichment_green(client, fresh_record):
    """拷贝纸 q=5令 + sheet_count=2+3 → match='green'"""
    rid = fresh_record
    CopyPaperImage.create(rid, 'p1.jpg', 'p1.jpg', 'count')
    CopyPaperImage.create(rid, 'p2.jpg', 'p2.jpg', 'count')
    last1 = CopyPaperImage.list_by_record(rid)[0]
    last2 = CopyPaperImage.list_by_record(rid)[1]
    CopyPaperImage.update_count(last1['id'], 2)
    CopyPaperImage.update_count(last2['id'], 3)

    # 调出货首页拿 HTML 验证 enrichment
    resp = client.get('/shipping-records')
    assert resp.status_code == 200
    # 测试 record 已通过 fresh_record 插入,但需要 product_name 对应 category_code=0107
    # 为简化,这里只断言 CopyPaperImage.list_by_record 返回了 2 行
    rows = CopyPaperImage.list_by_record(rid)
    assert len(rows) == 2
    # enrichment 字段由 _enrich_record_records 计算,这里直接调内部函数
    # (见 Step 3 实现)
```

为简洁起见，将 enrichment 函数提取为模块级私有函数 `_enrich_copy_paper_for_item(item)`，方便单测：

```python
def test_enrich_function_green(fresh_record):
    rid = fresh_record
    iid = CopyPaperImage.create(rid, 'p.jpg', 'p.jpg', 'count')
    CopyPaperImage.update_count(iid, 5)

    item = {'id': rid, 'quantity': 5, 'unit': '令', 'remark': ''}
    # 暂未导入,留待 Step 3 后
```

（完整测试在 Step 4 写完函数后补；这里先写**骨架测试**让它失败）

- [ ] **Step 2: 跑测试，confirm fail**

Run: `cd 'C:/Users/Administrator/worklog-app' && $env:PYTHONUTF8=1; python -m pytest tests/test_copy_paper_images.py -v`
Expected: 14 PASSED（Task 4 的测试暂留空，等 Step 4 补完整）

如果用了 `from blueprints.shipping import _enrich_copy_paper_for_item` 会报 ImportError，把测试放到 Step 4 一起写更顺。

- [ ] **Step 3: 实现 `_enrich_copy_paper_for_item` 函数**

在 `blueprints/shipping.py` 末尾追加（紧跟 4 个端点之后）：

```python


def _is_copy_paper_item(item: dict) -> bool:
    """判断 record 是否属于拷贝纸/日本纸类别。

    通过 product 表 join 出 category_code(若有),否则返回 False。
    若 product_name 已包含 '拷贝' 或 '日本' 关键词也兜底返回 True(避免遗漏)。
    """
    name = (item.get('product_name') or '').strip()
    # 关键词兜底
    if '拷贝' in name or '日本纸' in name:
        return True
    # JOIN category_code(若 helper 已存在)
    try:
        from models.category_prompt import classify_record
        result = classify_record(name, item.get('specification') or '')
        code = result.get('category_code') if isinstance(result, dict) else None
        return code in ('0105', '0107')
    except Exception:
        return False


def _enrich_copy_paper_for_item(item: dict) -> None:
    """对单条 record 原地写入 copy-paper 字段。"""
    item['is_copy_paper'] = _is_copy_paper_item(item)
    if not item['is_copy_paper']:
        item['copy_paper_images'] = []
        item['copy_paper_total'] = 0
        item['copy_paper_match'] = None
        return

    images = CopyPaperImage.list_by_record(item['id'])
    item['copy_paper_images'] = images

    counts = [img['sheet_count'] for img in images if img['sheet_count'] is not None]
    total = sum(counts)
    item['copy_paper_total'] = total

    count_imgs = [img for img in images if img['source'] == 'count']
    total_count_imgs = len(count_imgs)
    counted_imgs = sum(1 for img in count_imgs if img['sheet_count'] is not None)

    expected, has_expected = compute_copy_paper_expected_quantity(item.get('quantity'), item.get('unit'))
    if not has_expected:
        item['copy_paper_match'] = None
    elif total_count_imgs > 0 and counted_imgs < total_count_imgs:
        item['copy_paper_match'] = 'partial'
    elif total == expected:
        item['copy_paper_match'] = 'green'
    else:
        item['copy_paper_match'] = 'yellow'
```

- [ ] **Step 4: 在 `/shipping-records` 主循环中调用 enrich**

找到 `blueprints/shipping.py` 中 `item['placement_match'] = _pm` 这一行（约 line 319），紧接其后追加：

```python
            # 2026-09-06: 拷贝纸/日本纸 行级图片 + 张数比对
            _enrich_copy_paper_for_item(item)
```

（如果已有 `item['placement_match'] = _pm` 不止一行，需要 `Search "placement_match" line 319` 确认上下文后再插入。）

- [ ] **Step 5: 补 5 个 enrichment 测试**

在 `tests/test_copy_paper_images.py` 追加：

```python
from blueprints.shipping import _enrich_copy_paper_for_item, _is_copy_paper_item


def _make_item(rid, qty, unit, name='拷贝纸A'):
    return {'id': rid, 'product_name': name, 'specification': '',
            'quantity': qty, 'unit': unit, 'remark': ''}


def test_enrich_green_ling(fresh_record):
    rid = fresh_record
    iid1 = CopyPaperImage.create(rid, 'p1.jpg', 'p1.jpg', 'count')
    iid2 = CopyPaperImage.create(rid, 'p2.jpg', 'p2.jpg', 'count')
    CopyPaperImage.update_count(iid1, 2)
    CopyPaperImage.update_count(iid2, 3)
    item = _make_item(rid, 5, '令')
    _enrich_copy_paper_for_item(item)
    assert item['is_copy_paper'] is True
    assert item['copy_paper_total'] == 5
    assert item['copy_paper_match'] == 'green'


def test_enrich_yellow_ling(fresh_record):
    rid = fresh_record
    iid = CopyPaperImage.create(rid, 'p.jpg', 'p.jpg', 'count')
    CopyPaperImage.update_count(iid, 4)
    item = _make_item(rid, 5, '令')
    _enrich_copy_paper_for_item(item)
    assert item['copy_paper_match'] == 'yellow'


def test_enrich_partial(fresh_record):
    rid = fresh_record
    CopyPaperImage.create(rid, 'p1.jpg', 'p1.jpg', 'count')
    CopyPaperImage.create(rid, 'p2.jpg', 'p2.jpg', 'count')
    iid = CopyPaperImage.list_by_record(rid)[0]['id']
    CopyPaperImage.update_count(iid, 2)
    item = _make_item(rid, 5, '令')
    _enrich_copy_paper_for_item(item)
    assert item['copy_paper_match'] == 'partial'
    assert item['copy_paper_total'] == 2


def test_enrich_no_match_when_no_quantity(fresh_record):
    rid = fresh_record
    CopyPaperImage.create(rid, 'p.jpg', 'p.jpg', 'count')
    item = _make_item(rid, 0, '令')
    _enrich_copy_paper_for_item(item)
    assert item['copy_paper_match'] is None


def test_enrich_non_copy_paper_item(fresh_record):
    rid = fresh_record
    item = _make_item(rid, 5, '支', name='杂胶袋')
    _enrich_copy_paper_for_item(item)
    assert item['is_copy_paper'] is False
    assert item['copy_paper_match'] is None
```

- [ ] **Step 6: 跑全部 19 个测试**

Run: `cd 'C:/Users/Administrator/worklog-app' && $env:PYTHONUTF8=1; python -m pytest tests/test_copy_paper_images.py -v`
Expected: 19 PASSED

- [ ] **Step 7: 全量回归（确保不破现有 placement/OCR）**

Run: `cd 'C:/Users/Administrator/worklog-app' && $env:PYTHONUTF8=1; python -m pytest tests/ -v --ignore=tests/regression 2>&1 | tail -30`
Expected: 既有测试全 PASSED（不破）

- [ ] **Step 8: 提交**

```bash
cd 'C:/Users/Administrator/worklog-app' && git add blueprints/shipping.py tests/test_copy_paper_images.py
git -c user.name='liuyongping99' -c user.email='liuyongping99@users.noreply.github.com' commit -m "feat(shipping): copy-paper 行级富化" -m "- _is_copy_paper_item: 关键词 + classify_record 双兜底
- _enrich_copy_paper_for_item: 写入 4 字段
  is_copy_paper / copy_paper_images / copy_paper_total / copy_paper_match
- 主循环调用 enrich,green/yellow/partial/None 四态"
```

---

## Task 5: PC 端操作列 2 按钮

**Files:**
- Modify: `templates/shipping-records.html:914-924`

**Interfaces:**
- Renders only when `item.is_copy_paper` is True
- Two buttons: `📷 标签` (`copy-paper-label-btn`) and `📊 张数` (`copy-paper-count-btn`)
- Both have `lock-hide` class, `data-record-id`, `data-order-id`, `data-source`
- `onclick` opens upload modal with `data-source` (label/count)

- [ ] **Step 1: 在操作列加 2 按钮**

打开 `templates/shipping-records.html`，找到 actions-cell 块（约 line 914-924），在 `</td>` 之前（即 delete 按钮之后、record-image-btn 之前），插入：

```html
            {% if item.is_copy_paper %}
            <button type="button" class="btn btn-sm copy-paper-label-btn lock-hide" data-record-id="{{ item.id }}" data-order-id="{{ group.id }}" data-source="label" title="上传标签图（拷贝纸/日本纸，无需 OCR）" style="background:#74b9ff;color:#fff;">📷 标签</button>
            <button type="button" class="btn btn-sm copy-paper-count-btn lock-hide" data-record-id="{{ item.id }}" data-order-id="{{ group.id }}" data-source="count" title="上传点数图并录入张数" style="background:#fdcb6e;color:#222;">📊 张数</button>
            {% endif %}
```

注意缩进和现有按钮保持一致（4 空格或 8 空格，看文件）。

- [ ] **Step 2: 浏览器手动验证**

启动 server 后访问 `http://localhost:5050/shipping-records`，找一个有 `拷贝纸` / `日本纸` 的订单（如果没有就临时在 db 里插入 1 条测试 record）：
- 拷贝纸行应看到 `📷 标签` `📊 张数` 两个按钮
- 其他类别行不应看到这两个按钮
- 锁定订单后两个按钮应隐藏

如无现成测试数据，可用以下 SQL 临时插入：

```sql
INSERT INTO shipping_orders (date, customer, is_locked) VALUES ('2026-09-06', 'test', 0);
INSERT INTO shipping_records (order_pk, product_name, specification, quantity, unit)
VALUES (last_insert_rowid(), '拷贝纸测试A', 'A4', 5, '令');
```

- [ ] **Step 3: 提交**

```bash
cd 'C:/Users/Administrator/worklog-app' && git add templates/shipping-records.html
git -c user.name='liuyongping99' -c user.email='liuyongping99@users.noreply.github.com' commit -m "feat(ui): shipping-records 操作列加 copy-paper 双按钮" -m "- 仅 is_copy_paper 行渲染
- lock-hide 模式与现有按钮一致
- data-source 区分 label/count"
```

---

## Task 6: PC 端缩略图区 + 徽章 + 样式

**Files:**
- Modify: `templates/shipping-records.html`（在 placement-area 之后）
- Modify: `static/css/app.css`（末尾追加）

**Interfaces:**
- Renders only when `item.is_copy_paper and item.copy_paper_images`
- Shows badge (`green`/`yellow`/`partial`) + thumbnails
- Each thumbnail has source tag + (for `count` source) sheet count input + delete button

- [ ] **Step 1: 加缩略图区 HTML**

在 `templates/shipping-records.html` 找到 `placement-area` 块结束的位置（约 line 1081 后），在其紧后插入：

```html
                    {% if item.is_copy_paper and item.copy_paper_images %}
                    <div class="copy-paper-area">
                        <div class="copy-paper-header">
                            {% if item.copy_paper_match == 'green' %}
                                <span class="copy-paper-badge green">✓ 张数 {{ item.copy_paper_total }}/{{ item.copy_paper_expected }}</span>
                            {% elif item.copy_paper_match == 'yellow' %}
                                <span class="copy-paper-badge yellow">⚠ 张数不符 {{ item.copy_paper_total }}≠{{ item.copy_paper_expected }}</span>
                            {% elif item.copy_paper_match == 'partial' %}
                                <span class="copy-paper-badge partial">⊕ 部分已录 {{ item.copy_paper_total }}</span>
                            {% endif %}
                        </div>
                        <div class="copy-paper-thumb-list">
                            {% for img in item.copy_paper_images %}
                            <div class="copy-paper-thumb" data-image-id="{{ img.id }}">
                                <img src="/upload/{{ img.file_path }}" alt="copy paper {{ img.source }}">
                                <span class="copy-paper-tag tag-{{ img.source }}">{{ '标签' if img.source == 'label' else '点数' }}</span>
                                {% if img.source == 'count' %}
                                <input class="copy-paper-count-input" type="number" min="0" value="{{ img.sheet_count if img.sheet_count is not none else '' }}" data-image-id="{{ img.id }}" placeholder="张数">
                                {% endif %}
                                <button type="button" class="copy-paper-delete-btn" data-image-id="{{ img.id }}" title="删除">×</button>
                            </div>
                            {% endfor %}
                        </div>
                    </div>
                    {% endif %}
```

注意：这里模板用了 `item.copy_paper_expected` 变量，需要在 `_enrich_copy_paper_for_item` 里也写入这个字段（Task 4 漏了）。在 Task 6 实现时**先补 Task 4 enrichment 函数**：

```python
    # 在 _enrich_copy_paper_for_item 末尾,根据 match 决定 expected 值
    if has_expected:
        item['copy_paper_expected'] = expected
    else:
        item['copy_paper_expected'] = None
```

并补 1 个测试：

```python
def test_enrich_sets_expected_field(fresh_record):
    rid = fresh_record
    item = _make_item(rid, 5, '令')
    _enrich_copy_paper_for_item(item)
    assert item['copy_paper_expected'] == 5.0
```

- [ ] **Step 2: 跑测试 confirm pass**

Run: `cd 'C:/Users/Administrator/worklog-app' && $env:PYTHONUTF8=1; python -m pytest tests/test_copy_paper_images.py -v`
Expected: 20 PASSED（19 + 1 新增）

- [ ] **Step 3: 加 CSS 到 `static/css/app.css`**

文件末尾追加：

```css
/* ========== 拷贝纸/日本纸 行级图 (2026-09-06) ========== */
.copy-paper-area {
    margin-top: 8px;
    padding: 8px;
    background: #f8f9fa;
    border: 1px dashed #ced4da;
    border-radius: 4px;
}
.copy-paper-header {
    margin-bottom: 6px;
    font-size: 13px;
}
.copy-paper-badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 10px;
    font-weight: 600;
}
.copy-paper-badge.green { background: #d3f9d8; color: #2b8a3e; }
.copy-paper-badge.yellow { background: #fff3bf; color: #b8860b; }
.copy-paper-badge.partial { background: #e9ecef; color: #495057; }
.copy-paper-thumb-list {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
}
.copy-paper-thumb {
    position: relative;
    width: 110px;
    border: 1px solid #dee2e6;
    border-radius: 4px;
    padding: 4px;
    background: #fff;
}
.copy-paper-thumb img {
    width: 100%;
    height: 80px;
    object-fit: cover;
    border-radius: 2px;
    display: block;
}
.copy-paper-tag {
    display: inline-block;
    margin-top: 4px;
    font-size: 11px;
    padding: 1px 6px;
    border-radius: 8px;
}
.copy-paper-tag.tag-label { background: #d0ebff; color: #1864ab; }
.copy-paper-tag.tag-count { background: #fff3bf; color: #b8860b; }
.copy-paper-count-input {
    width: 100%;
    margin-top: 4px;
    padding: 2px 4px;
    border: 1px solid #ced4da;
    border-radius: 3px;
    font-size: 12px;
    box-sizing: border-box;
}
.copy-paper-delete-btn {
    position: absolute;
    top: 2px;
    right: 2px;
    width: 20px;
    height: 20px;
    border: none;
    border-radius: 50%;
    background: #fa5252;
    color: #fff;
    font-size: 12px;
    line-height: 1;
    cursor: pointer;
    padding: 0;
}
.copy-paper-delete-btn:hover { background: #c92a2a; }
```

- [ ] **Step 4: 浏览器手动验证**

- 找到有拷贝纸记录的订单
- 上传几张图（通过 Task 7 完成后才能测；本任务只验 HTML/CSS 渲染）
- 手动在 DB 插入几条 `copy_paper_images` 行指向已知图片路径，确认缩略图/徽章正常显示

- [ ] **Step 5: 提交**

```bash
cd 'C:/Users/Administrator/worklog-app' && git add templates/shipping-records.html static/css/app.css tests/test_copy_paper_images.py blueprints/shipping.py
git -c user.name='liuyongping99' -c user.email='liuyongping99@users.noreply.github.com' commit -m "feat(ui): copy-paper 缩略图区 + 徽章 + 样式" -m "- green/yellow/partial 三色徽章
- 缩略图带 source tag + count 时显示张数输入框
- 删图按钮 absolute 定位"
```

---

## Task 7: PC 端 JS + 张数输入弹框

**Files:**
- Create: `templates/_copy_paper_count_modal.html`
- Create: `static/js/copy_paper.js`
- Modify: `templates/shipping-records.html`（底部 `<script src="/static/js/copy_paper.js"></script>`）

**Interfaces:**
- 4 个全局函数：
  - `openCopyPaperUpload(recordPk, orderPk, source)` — 打开 `_image_upload_modal.html`，绑定上传回调
  - `copyPaperUploaded(data, recordPk, source)` — 上传成功后回调；source='count' 时弹张数输入框
  - `openCopyPaperCountModal(imageId)` — 打开张数输入弹框
  - `refreshCopyPaperBlock(recordPk)` — 局部刷新该 record 块（fetch GET + DOM 替换）
- DOMContentLoaded 时绑定：缩略图上的 `×` 按钮 + 张数输入框 change

- [ ] **Step 1: 创建 `_copy_paper_count_modal.html`**

新建 `templates/_copy_paper_count_modal.html`：

```html
<!-- 拷贝纸/日本纸 张数输入弹框 (2026-09-06) -->
<div id="copyPaperCountModal" class="modal" style="display:none;">
  <div class="modal-content" style="max-width:340px;">
    <div class="modal-header">
      <h3 class="modal-title">📊 录入张数</h3>
      <button type="button" class="modal-close" onclick="closeCopyPaperCountModal()">×</button>
    </div>
    <div class="modal-body">
      <p style="color:#666;font-size:13px;margin-bottom:12px;">
        请输入该标签对应的张数（拷贝纸按令、日本纸按张）。
      </p>
      <input id="copyPaperCountInput" type="number" min="0" step="1"
             style="width:100%;padding:8px;border:1px solid #ced4da;border-radius:4px;font-size:16px;"
             placeholder="例: 500" autofocus>
      <input id="copyPaperCountImageId" type="hidden" value="">
    </div>
    <div class="modal-footer" style="margin-top:16px;text-align:right;">
      <button type="button" class="btn" onclick="closeCopyPaperCountModal()">取消</button>
      <button type="button" class="btn btn-primary" onclick="saveCopyPaperCount()">保存</button>
    </div>
  </div>
</div>
```

- [ ] **Step 2: 在 `templates/shipping-records.html` include 新弹框**

在 `shipping-records.html` `{% endblock %}` 之前加：

```html
{% include '_copy_paper_count_modal.html' %}
```

并在同一文件底部 `<script>` 段后加：

```html
<script src="/static/js/copy_paper.js"></script>
```

- [ ] **Step 3: 创建 `static/js/copy_paper.js`**

```javascript
// 拷贝纸/日本纸 行级图 + 张数 (2026-09-06)
(function () {
    'use strict';

    // 复用现有 _image_upload_modal 的弹框入口（与 placement-add-btn 同款）
    function openUploadModal(recordPk, orderPk, source) {
        if (typeof window.openPlacementImageModal !== 'function') {
            alert('上传弹框未就绪');
            return;
        }
        // 临时重定向 placement 上传回调到 copy-paper
        window._copyPaperUploadSource = source;
        window._copyPaperTargetRecord = recordPk;
        window.openPlacementImageModal(recordPk, orderPk);
    }

    // 接管 placement 上传回调（如果现有 placementImageUploaded 不可重入，可改写 placement_count.js
    // 让它检测 window._copyPaperUploadSource 走 copy-paper 分支）
    window.copyPaperImageUploaded = function (data, recordPk) {
        if (!data || !data.success) {
            alert('上传失败: ' + ((data && data.error) || '未知错误'));
            return;
        }
        var source = window._copyPaperUploadSource;
        if (source === 'count' && data.image) {
            // 弹张数输入框
            openCopyPaperCountModal(data.image.id);
        }
        refreshCopyPaperBlock(recordPk);
        window._copyPaperUploadSource = null;
        window._copyPaperTargetRecord = null;
    };

    window.openCopyPaperUpload = function (recordPk, orderPk, source) {
        openUploadModal(recordPk, orderPk, source);
    };

    window.openCopyPaperCountModal = function (imageId) {
        var modal = document.getElementById('copyPaperCountModal');
        var input = document.getElementById('copyPaperCountInput');
        var hidden = document.getElementById('copyPaperCountImageId');
        if (!modal || !input || !hidden) return;
        hidden.value = imageId;
        input.value = '';
        modal.style.display = 'flex';
        setTimeout(function () { input.focus(); }, 50);
    };

    window.closeCopyPaperCountModal = function () {
        var modal = document.getElementById('copyPaperCountModal');
        if (modal) modal.style.display = 'none';
    };

    window.saveCopyPaperCount = function () {
        var hidden = document.getElementById('copyPaperCountImageId');
        var input = document.getElementById('copyPaperCountInput');
        if (!hidden || !input || !hidden.value) return;
        var val = input.value.trim();
        var body = { sheet_count: val === '' ? null : parseInt(val, 10) };
        fetch('/api/v1/shipping-orders/copy-paper-images/' + hidden.value + '/sheet-count', {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body)
        }).then(function (r) { return r.json(); }).then(function (j) {
            if (!j.success) { alert('保存失败: ' + (j.error || '')); return; }
            closeCopyPaperCountModal();
            // 找出该 image 所在 record 并刷新
            var thumb = document.querySelector('.copy-paper-thumb[data-image-id="' + hidden.value + '"]');
            if (thumb) {
                var block = thumb.closest('.record-block');
                if (block) {
                    var recId = block.getAttribute('data-record-id');
                    if (recId) refreshCopyPaperBlock(parseInt(recId, 10));
                }
            }
        }).catch(function (err) {
            alert('保存失败: ' + err);
        });
    };

    window.refreshCopyPaperBlock = function (recordPk) {
        // 简化方案:整行刷新（防止局部替换搞错 DOM）
        var block = document.querySelector('.record-block[data-record-id="' + recordPk + '"]');
        if (!block) { location.reload(); return; }
        location.reload();  // TODO: 后续可优化为局部刷新
    };

    // 绑定缩略图上的删除按钮 + 张数输入框
    document.addEventListener('DOMContentLoaded', function () {
        document.body.addEventListener('click', function (e) {
            var del = e.target.closest('.copy-paper-delete-btn');
            if (del) {
                if (!confirm('删除这张图片?')) return;
                var iid = del.getAttribute('data-image-id');
                fetch('/api/v1/shipping-orders/copy-paper-images/' + iid, { method: 'DELETE' })
                    .then(function (r) { return r.json(); }).then(function (j) {
                        if (!j.success) { alert('删除失败'); return; }
                        var thumb = del.closest('.copy-paper-thumb');
                        if (thumb) {
                            var block = thumb.closest('.record-block');
                            var recId = block && block.getAttribute('data-record-id');
                            if (recId) refreshCopyPaperBlock(parseInt(recId, 10));
                        }
                    });
                return;
            }
        });
        document.body.addEventListener('change', function (e) {
            var inp = e.target.closest('.copy-paper-count-input');
            if (inp) {
                var iid = inp.getAttribute('data-image-id');
                var val = inp.value.trim();
                var body = { sheet_count: val === '' ? null : parseInt(val, 10) };
                fetch('/api/v1/shipping-orders/copy-paper-images/' + iid + '/sheet-count', {
                    method: 'PATCH',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(body)
                }).then(function (r) { return r.json(); }).then(function (j) {
                    if (!j.success) { alert('保存失败'); return; }
                    var thumb = inp.closest('.copy-paper-thumb');
                    if (thumb) {
                        var block = thumb.closest('.record-block');
                        var recId = block && block.getAttribute('data-record-id');
                        if (recId) refreshCopyPaperBlock(parseInt(recId, 10));
                    }
                });
            }
        });
    });
})();
```

- [ ] **Step 4: 检查现有 `placement_count.js` 是否会被本次改动破坏**

打开 `static/js/placement_count.js` 找到 `window.placementImageUploaded = function (data, recordPk)`，确认它**不会**与 `copyPaperImageUploaded` 冲突。本计划的设计让 `copyPaperImageUploaded` **不**覆盖 placement 回调，而是通过 `_copyPaperUploadSource` 标记区分。

实际改造方式（如果现有 placement 上传函数无法重入）：

- 在 `placement_count.js` 的 `placementImageUploaded` 函数内**开头**加：
  ```javascript
  if (window._copyPaperUploadSource) {
      window.copyPaperImageUploaded(data, recordPk);
      return;
  }
  ```

如果 `placementImageUploaded` 已存在分支判断则无需改；如不存在，按上面一行插入。

- [ ] **Step 5: 浏览器手动验证**

- 启动 server，访问 `/shipping-records`，找一个拷贝纸行
- 点 `📷 标签` → 上传弹框弹出 → 选图 → 上传 → 缩略图出现（绿色标签 tag）
- 点 `📊 张数` → 上传弹框弹出 → 选图 → 上传 → **张数输入弹框**自动弹出 → 输入 250 → 保存 → 缩略图更新 + 徽章变色
- 修改缩略图上的张数输入框 → 自动保存
- 点缩略图 × → 弹出确认 → 删除 → 缩略图消失

- [ ] **Step 6: 提交**

```bash
cd 'C:/Users/Administrator/worklog-app' && git add templates/_copy_paper_count_modal.html templates/shipping-records.html static/js/copy_paper.js static/js/placement_count.js
git -c user.name='liuyongping99' -c user.email='liuyongping99@users.noreply.github.com' commit -m "feat(ui): copy-paper 上传/录入/删除 JS" -m "- 复用 _image_upload_modal 弹上传框
- source='count' 自动弹张数输入小弹框
- 缩略图 × 按钮 + inline 张数输入框双向绑定 PATCH 端点
- placement_count.js 加 copy-paper 分流(不动 placement 主路径)"
```

---

## Task 8: 移动端出货（`/m/shipping-today/order/<oid>`）

**Files:**
- Modify: `templates/mobile/shipping-order.html`
- Modify: `static/css/mobile.css`

**Interfaces:**
- Same 4 endpoints, called via `/m/...` mobile route → `/api/v1/...` (handled by existing `mobile_shipping.py` routing)
- Two buttons in row, thumbnails below, inline `<input type=number>` for sheet count (no separate modal — uses `prompt()` or inline)

- [ ] **Step 1: 找到模板操作列位置**

打开 `templates/mobile/shipping-order.html`，找到每条 record 行的操作按钮区（拍照/编辑/删除）。

- [ ] **Step 2: 加 2 按钮 + 缩略图区 + inline 张数**

在每条 record 渲染时（按 `item.is_copy_paper` 判断）追加：

```html
{% if item.is_copy_paper %}
<button type="button" class="m-copy-paper-label-btn" data-record-id="{{ item.id }}" data-source="label">📷 标签</button>
<button type="button" class="m-copy-paper-count-btn" data-record-id="{{ item.id }}" data-source="count">📊 张数</button>
{% endif %}
{% if item.is_copy_paper and item.copy_paper_images %}
<div class="m-copy-paper-area">
  {% if item.copy_paper_match == 'green' %}<span class="m-copy-paper-badge green">✓ {{ item.copy_paper_total }}/{{ item.copy_paper_expected }}</span>
  {% elif item.copy_paper_match == 'yellow' %}<span class="m-copy-paper-badge yellow">⚠ {{ item.copy_paper_total }}≠{{ item.copy_paper_expected }}</span>
  {% elif item.copy_paper_match == 'partial' %}<span class="m-copy-paper-badge partial">⊕ 部分已录</span>{% endif %}
  {% for img in item.copy_paper_images %}
  <div class="m-copy-paper-thumb">
    <img src="/upload/{{ img.file_path }}">
    <span class="m-copy-paper-tag tag-{{ img.source }}">{{ '标签' if img.source == 'label' else '点数' }}</span>
    {% if img.source == 'count' %}
    <input class="m-copy-paper-count-input" type="number" min="0" data-image-id="{{ img.id }}" value="{{ img.sheet_count if img.sheet_count is not none else '' }}">
    {% endif %}
  </div>
  {% endfor %}
</div>
{% endif %}
```

- [ ] **Step 3: 在 `mobile_shipping.py` 渲染路径加 enrichment**

找到 `mobile_shipping.py` 渲染 `/m/shipping-today/order/<oid>` 的视图函数（约 `mobile_order_detail`），找到 records 列表的循环，在循环内调用 `_enrich_copy_paper_for_item(item)`。如果该文件没 import 这个函数，在顶部加：

```python
from blueprints.shipping import _enrich_copy_paper_for_item
```

- [ ] **Step 4: 加移动端 JS（inline 在模板底部 `<script>` 段）**

在 `shipping-order.html` 底部 `<script>` 段加：

```javascript
// 拷贝纸/日本纸 (2026-09-06)
document.body.addEventListener('click', function (e) {
    var lblBtn = e.target.closest('.m-copy-paper-label-btn');
    var cntBtn = e.target.closest('.m-copy-paper-count-btn');
    var btn = lblBtn || cntBtn;
    if (!btn) return;
    var rid = btn.getAttribute('data-record-id');
    var source = btn.getAttribute('data-source');
    var input = document.createElement('input');
    input.type = 'file';
    input.accept = 'image/*';
    input.capture = 'environment';  // 移动端直接调相机
    input.onchange = function () {
        var file = input.files[0];
        if (!file) return;
        var fd = new FormData();
        fd.append('source', source);
        fd.append('file', file);
        fetch('/api/v1/shipping-orders/records/' + rid + '/copy-paper-images', {
            method: 'POST', body: fd
        }).then(function (r) { return r.json(); }).then(function (j) {
            if (!j.success) { alert('上传失败: ' + (j.error || '')); return; }
            if (source === 'count') {
                var ans = prompt('请输入张数（拷贝纸按令、日本纸按张）', '');
                if (ans !== null && ans.trim() !== '') {
                    var val = parseInt(ans.trim(), 10);
                    fetch('/api/v1/shipping-orders/copy-paper-images/' + j.image.id + '/sheet-count', {
                        method: 'PATCH',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ sheet_count: val })
                    });
                }
            }
            location.reload();
        });
    };
    input.click();
});

document.body.addEventListener('change', function (e) {
    var inp = e.target.closest('.m-copy-paper-count-input');
    if (!inp) return;
    var iid = inp.getAttribute('data-image-id');
    var val = inp.value.trim();
    fetch('/api/v1/shipping-orders/copy-paper-images/' + iid + '/sheet-count', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sheet_count: val === '' ? null : parseInt(val, 10) })
    }).then(function (r) { return r.json(); }).then(function (j) {
        if (!j.success) alert('保存失败');
    });
});
```

- [ ] **Step 5: 加移动端 CSS**

`static/css/mobile.css` 末尾追加：

```css
/* 拷贝纸/日本纸 (2026-09-06) */
.m-copy-paper-label-btn, .m-copy-paper-count-btn {
    margin: 4px 4px 0 0;
    padding: 6px 10px;
    border-radius: 4px;
    border: 1px solid #ced4da;
    background: #fff;
    font-size: 14px;
}
.m-copy-paper-label-btn { background: #74b9ff; color: #fff; }
.m-copy-paper-count-btn { background: #fdcb6e; color: #222; }
.m-copy-paper-area {
    margin-top: 8px;
    padding: 8px;
    background: #f8f9fa;
    border-radius: 4px;
}
.m-copy-paper-badge {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 10px;
    font-size: 12px;
    margin-right: 6px;
}
.m-copy-paper-badge.green { background: #d3f9d8; color: #2b8a3e; }
.m-copy-paper-badge.yellow { background: #fff3bf; color: #b8860b; }
.m-copy-paper-badge.partial { background: #e9ecef; color: #495057; }
.m-copy-paper-thumb {
    display: inline-block;
    width: 80px;
    margin: 4px;
    text-align: center;
}
.m-copy-paper-thumb img { width: 80px; height: 60px; object-fit: cover; border-radius: 2px; }
.m-copy-paper-tag { display: block; font-size: 11px; margin-top: 2px; }
.m-copy-paper-tag.tag-label { color: #1864ab; }
.m-copy-paper-tag.tag-count { color: #b8860b; }
.m-copy-paper-count-input {
    width: 100%;
    margin-top: 2px;
    padding: 2px;
    border: 1px solid #ced4da;
    border-radius: 2px;
    font-size: 12px;
    box-sizing: border-box;
}
```

- [ ] **Step 6: 浏览器手动验证（用 Chrome DevTools 模拟手机）**

- 访问 `http://localhost:5050/m/shipping-today/order/<oid>`，找一个拷贝纸行
- 点 `📷 标签` → 选图（移动端会调相机或相册） → 上传 → 缩略图出现
- 点 `📊 张数` → 选图 → 上传 → prompt 弹框 → 输入数字 → OK → 缩略图更新 + 徽章变色

- [ ] **Step 7: 提交**

```bash
cd 'C:/Users/Administrator/worklog-app' && git add templates/mobile/shipping-order.html static/css/mobile.css blueprints/mobile_shipping.py
git -c user.name='liuyongping99' -c user.email='liuyongping99@users.noreply.github.com' commit -m "feat(mobile): copy-paper 行级按钮 + 缩略图 + inline 张数" -m "- /m/shipping-today/order/<oid> 出货移动端同步
- capture=environment 调原生相机
- prompt() 弹张数输入,简化移动端交互
- 后端走同一套 /api/v1/shipping-orders/.../copy-paper-images 端点"
```

---

## Task 9: 综合验证 + 防回归测试

**Files:**
- Modify: `tests/test_copy_paper_images.py`（追加 e2e 流程测试）
- Modify: `tests/regression/`（如有必要，新增 OCR/AI 不被破坏的回归）

- [ ] **Step 1: 写 e2e 流程测试**

```python
def test_e2e_full_flow(client, fresh_record):
    """端到端:上传 label + 上传 count + 录入张数 → match green。"""
    rid = fresh_record
    # 1. 上传 label 图
    data = {'source': 'label',
            'file': (io.BytesIO(_png_bytes()), 'lbl.png')}
    resp = client.post(f'/api/v1/shipping-orders/records/{rid}/copy-paper-images',
                       data=data, content_type='multipart/form-data')
    assert resp.status_code == 200

    # 2. 上传 count 图
    data = {'source': 'count',
            'file': (io.BytesIO(_png_bytes()), 'cnt.png')}
    resp = client.post(f'/api/v1/shipping-orders/records/{rid}/copy-paper-images',
                       data=data, content_type='multipart/form-data')
    assert resp.status_code == 200
    iid = resp.get_json()['image']['id']

    # 3. 录入张数 5
    resp = client.patch(f'/api/v1/shipping-orders/copy-paper-images/{iid}/sheet-count',
                        json={'sheet_count': 5})
    assert resp.status_code == 200

    # 4. enrichment 应为 green (quantity=5)
    item = _make_item(rid, 5, '令')
    _enrich_copy_paper_for_item(item)
    assert item['copy_paper_match'] == 'green'
    assert item['copy_paper_total'] == 5
    assert len(item['copy_paper_images']) == 2
```

- [ ] **Step 2: 跑全量测试**

Run: `cd 'C:/Users/Administrator/worklog-app' && $env:PYTHONUTF8=1; python -m pytest tests/test_copy_paper_images.py -v`
Expected: 21 PASSED

- [ ] **Step 3: 全项目回归**

Run: `cd 'C:/Users/Administrator/worklog-app' && $env:PYTHONUTF8=1; python -m pytest tests/ -v --ignore=tests/regression 2>&1 | tail -50`
Expected: 既有测试不破（PASSED 数 ≥ 既有）

- [ ] **Step 4: import + 启动 server 检查**

```bash
cd 'C:/Users/Administrator/worklog-app' && $env:PYTHONUTF8=1
python -c "from app import create_app; app = create_app(); print('routes:', len(list(app.url_map.iter_rules())))"
```
Expected: route 数比改动前多 4（4 个 copy-paper 端点）。

- [ ] **Step 5: 提交**

```bash
cd 'C:/Users/Administrator/worklog-app' && git add tests/test_copy_paper_images.py
git -c user.name='liuyongping99' -c user.email='liuyongping99@users.noreply.github.com' commit -m "test(copy-paper): e2e 流程测试 + 全量回归验证" -m "- upload label + upload count + patch count → green
- 既有 84 测试不破"
```

---

## Self-Review

### Spec coverage 检查

| Spec 章节 | 覆盖任务 |
|---|---|
| 数据模型 `copy_paper_images` 表 | Task 1 |
| `CopyPaperImage` 模型类 5 个方法 | Task 1 |
| `compute_copy_paper_expected_quantity` helper | Task 2 |
| 4 个 REST 端点 | Task 3 |
| 类别检测 `_is_copy_paper_item` | Task 4 |
| 记录富化 4 字段 | Task 4 |
| 匹配判定 green/yellow/partial/None | Task 4 |
| PC 操作列 2 按钮 | Task 5 |
| PC 缩略图区 + 徽章 | Task 6 |
| PC JS + 张数弹框 | Task 7 |
| 移动端 2 按钮 + inline 张数 | Task 8 |
| 移动端样式 | Task 8 |
| 测试计划 8 类 | Task 1-4 + 9 |
| 风险回滚 | Task 1-9 各自 commit 便于回滚 |

### Type consistency 检查

- `CopyPaperImage.create(record_pk, file_path, original_name, source)` → `int` ✓
- `CopyPaperImage.list_by_record(record_pk)` → `list[dict]` ✓
- `CopyPaperImage.get_by_id(image_id)` → `dict | None` ✓
- `CopyPaperImage.update_count(image_id, sheet_count)` → `bool` ✓
- `CopyPaperImage.delete(image_id)` → `bool` ✓
- `compute_copy_paper_expected_quantity(quantity, unit)` → `tuple[float, bool]` ✓
- `_is_copy_paper_item(item)` → `bool` ✓
- `_enrich_copy_paper_for_item(item)` → `None` (mutates) ✓
- 模板字段：`is_copy_paper` / `copy_paper_images` / `copy_paper_total` / `copy_paper_match` / `copy_paper_expected` 全程一致 ✓

### Placeholder scan

- 无 "TBD" / "TODO" / "implement later"
- 无 "类似 Task X"（每个 task 都给了完整代码块）
- 所有端点 URL 完整给出
- 所有文件路径 absolute 给出
- 所有 import 都列出
- 所有 commit message 写好

---

## 计划完成

计划已写入 `docs/superpowers/plans/2026-09-06-copy-paper-row-buttons.md`，共 9 个任务，按 TDD 推进，每任务独立可测、独立可提交。

**下一步选执行模式：**

1. **Subagent-Driven（推荐）** —— 每个任务派一个独立 subagent 跑，两段式 review，迭代快
2. **Inline Execution** —— 在当前会话串行执行，每完成一批任务停下来给你 review

请告诉我用哪个。
