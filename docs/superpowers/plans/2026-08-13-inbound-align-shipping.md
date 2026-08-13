# 入库对齐出货 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `/inbound-records` 与 `/shipping-records` 商品行区域像素级一致（9 项功能对齐），并修复 `/loading-orders` 的图片列数持久化 bug。

**Architecture:** 入库端补齐后端计算字段（has_jia_mian / has_eco / qty_invalid / verified_warnings）+ DB 列（source_tag / img_cols）+ PATCH 分支 + 模板 CSS/HTML；装柜端补 DB 列（img_cols 已存在 set_img_cols/PATCH）+ 渲染/前端切到 DB；测试层用 pytest 单元测试 + Playwright e2e 双层覆盖。所有变更局部在 inbound / loading 模块，**不动出货基准**。

**Tech Stack:** Python 3.12 + Flask 3.1 + Jinja2 + 原生 JS + Tailwind + SQLite + pytest + Playwright

**Spec:** `docs/superpowers/specs/2026-08-13-inbound-align-shipping-design.md`

## Global Constraints

- **三套订单共享模板不动**：`templates/_record_image_script.html` 不动
- **出货基准不动**：`templates/shipping-records.html` + `blueprints/shipping.py` 不动
- **移动端不动**：`blueprints/mobile_shipping.py` 不动
- **CRLF 换行**：仓库 Windows CRLF
- **DB 迁移幂等**：所有 `ALTER TABLE ... ADD COLUMN` 包 `try/except Exception` 跳过「已存在」错误
- **测试夹具复用**：用 `tests/__init__.py::fake_png_bytes()` 上传图片；新建 _TempDb 时若涉及行级图上传必须在 tearDown 加 `_drain_async_jobs()`（参考 `tests/test_upload_ai_routing.py`）
- **白名单常量**：source_tag 白名单 `{'备货照', '装车照', '归仓照'}`，与 `shipping.py` 完全一致
- **`img_cols` 存订单表**：列加在 `shipping_orders`/`inbound_orders`/`loading_orders`，不是图片表
- **ASCII 直引号**：commit message 用英文双引号 `"..."` 包裹中文术语

---

## File Structure

**改动文件 (9)**：

- `blueprints/inbound.py`
  - group 计算加 `has_eco` + `has_jia_mian`（约 line 220-230）
  - item 计算加 `qty_invalid`（约 line 200-210）
  - item 计算加 `verified_warnings`（约 line 230-240）
  - 图片上传端点接受 `source_tag` 入参（约 line 720-770）
  - PATCH 端点加 `img_cols` 分支（line 289-348 之间）
- `blueprints/loading.py`
  - 渲染函数去 cookie 改 `group.img_cols`（line 160）
- `templates/inbound-records.html`
  - 头部 `<style>` 块新增 6 个 CSS 类（line 4-110 之间）
  - 表格表头新增「重点」`<th class="eco-col">`（约 line 500）
  - 表格 `<table>` 类增加 `no-eco`（约 line 495）
  - 行 `<tr>` 类增加 `row-out-of-stock` / `row-eco`（约 line 514）
  - 序号 cell 加 ❌ stock-icon（约 line 515）
  - 行内 eco-icon / jia-mian-icon SVG（约 line 519）
  - 图片区附近新增「AI 比对图例」div（约 line 510 上方）
  - `.img-item-record` 渲染加 source_tag 标签（约 line 600）
  - 行下方警告区加 verified_warnings ✓/✗ 按钮（约 line 510 之后）
- `templates/loading-orders.html`
  - 图片区 `current_img_cols` 改 `group.img_cols`（line 309）
  - 删自定义 cookie 版 setImgCols（line 1028-1045）
  - 2 处 JS 动态插入图片区按钮（line 745-749, 1274-1278）用 `group.img_cols`
- `models/_init.py`
  - 3 处迁移：`inbound_images.source_tag` / `inbound_orders.img_cols` / `loading_orders.img_cols`
- `models/orders.py`
  - `InboundImage.create`/`update` 加 `source_tag` 参数
  - `InboundImage.get_all_by_orders` 返回 dict 带 `source_tag`
  - `InboundRecord.get_verified_warnings`/`set_verified_warning` 新增静态方法
  - `InboundOrder.set_img_cols` 新增静态方法

**新增测试 (4 文件)**：

- `tests/test_db_migration.py` — Task 1 DB 迁移幂等测试
- `tests/test_inbound_model.py` — Task 2 模型方法单元测试
- `tests/test_inbound_blueprint.py` — Task 3 后端逻辑测试
- `tests/test_inbound_template_render.py` — Task 4 模板渲染测试
- `tests/test_inbound_warnings_and_tags.py` — Task 5 图例/source_tag/警告按钮测试
- `tests/test_loading_img_cols.py` — Task 6 装柜列数持久化测试
- `tests/e2e_inbound_align.py` — Task 7 Playwright 入库 e2e
- `tests/e2e_loading_img_cols.py` — Task 7 Playwright 装柜 e2e

---

## Task 1: DB 迁移 — 加 3 个列

**Files:**
- Modify: `models/_init.py:335-395` (在 `inbound_images` 创建段附近)
- Modify: `models/_init.py:91-103` (在 `inbound_orders` 创建段附近)
- Modify: `models/_init.py:105-117` (在 `loading_orders` 创建段附近)
- Create: `tests/test_db_migration.py` (DB 迁移幂等测试)

**Interfaces:**
- Consumes: 无
- Produces:
  - `models/_init.py` 加 3 处 `try/except` 包裹的 `ALTER TABLE ADD COLUMN`
  - 幂等：重复运行 `init_db()` 不报错

- [ ] **Step 1: 写红测试 `tests/test_db_migration.py`**

```python
"""DB 迁移幂等测试 — 加 3 列后,重复 init_db 不报错。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models._db as _db


class _TempDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        self._orig = _db.DB_PATH
        _db.DB_PATH = self.tmp.name

    def tearDown(self):
        _db.DB_PATH = self._orig
        for ext in ('', '-wal', '-shm'):
            p = self.tmp.name + ext
            if os.path.exists(p):
                try: os.unlink(p)
                except OSError: pass


class DbMigrationTests(_TempDb):

    def test_init_db_idempotent_with_new_columns(self):
        from models import init_db
        # 第一次跑：建表 + 加列
        init_db()
        # 第二次跑：必须不抛「duplicate column name」错误
        init_db()
        # 验证列存在
        import sqlite3
        conn = sqlite3.connect(self.tmp.name)
        for sql, expected_col in [
            ("PRAGMA table_info(inbound_images)", 'source_tag'),
            ("PRAGMA table_info(inbound_orders)", 'img_cols'),
            ("PRAGMA table_info(loading_orders)", 'img_cols'),
        ]:
            cols = [row[1] for row in conn.execute(sql).fetchall()]
            self.assertIn(expected_col, cols,
                f'列 {expected_col} 不存在 (sql={sql}, cols={cols})')
        conn.close()


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: 跑测试,确认 RED**

Run:
```bash
PYTHONUTF8=1 python -m pytest tests/test_db_migration.py -q -p no:warnings
```

Expected: FAIL — `inbound_images.source_tag` 列不存在

- [ ] **Step 3: 改 `models/_init.py` — 加 3 处 ALTER TABLE**

读 `models/_init.py:127-130`（已有的 shipping_orders.img_cols 迁移模式）作为模板。

在 `inbound_orders` 的 `CREATE TABLE` 段（line 91-103）之后插入：

```python
# 迁移：为已存在的 inbound_orders 表添加 img_cols 列
try:
    cursor.execute('ALTER TABLE inbound_orders ADD COLUMN img_cols INTEGER NOT NULL DEFAULT 5')
except Exception:
    pass  # 列已存在
```

在 `loading_orders` 的 `CREATE TABLE` 段（line 105-117）之后插入：

```python
# 迁移：为已存在的 loading_orders 表添加 img_cols 列
try:
    cursor.execute('ALTER TABLE loading_orders ADD COLUMN img_cols INTEGER NOT NULL DEFAULT 3')
except Exception:
    pass  # 列已存在
```

找到 `inbound_images` 的 `CREATE TABLE` 段（`models/_init.py:335` 附近），在该段之后插入：

```python
# 迁移：为已存在的 inbound_images 表添加 source_tag 列（移动端备货照/装车照/归仓照）
try:
    cursor.execute('ALTER TABLE inbound_images ADD COLUMN source_tag TEXT')
except Exception:
    pass  # 列已存在
```

- [ ] **Step 4: 跑测试,确认 GREEN**

Run:
```bash
PYTHONUTF8=1 python -m pytest tests/test_db_migration.py -q -p no:warnings
```

Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add models/_init.py tests/test_db_migration.py
git commit -m "feat(db): 加 inbound_images.source_tag + inbound_orders.img_cols + loading_orders.img_cols 三列"
```

---

## Task 2: 模型层 — InboundImage.source_tag + InboundRecord verified_warnings + InboundOrder.set_img_cols

**Files:**
- Modify: `models/orders.py` (找 `InboundImage` 类 — 在 `shipping-records` 之后)
- Modify: `models/orders.py` (找 `InboundRecord` 类)
- Modify: `models/orders.py` (找 `InboundOrder` 类)
- Create: `tests/test_inbound_model.py` (模型方法单元测试)

**Interfaces:**
- Consumes: `models/_init.py:init_db()` 已有 schema（已含新列）
- Produces:
  - `InboundImage.create(image_path, order_pk, ..., source_tag=None)` — 新参数，可选
  - `InboundImage.get_all_by_orders(order_ids)` — 返回 dict 含 `source_tag` key（默认 None）
  - `InboundRecord.get_verified_warnings(record_id) -> dict`
  - `InboundRecord.set_verified_warning(record_id, rule_id, verified) -> dict`
  - `InboundOrder.set_img_cols(order_id, cols)` — UPDATE SQL

- [ ] **Step 1: 写红测试 `tests/test_inbound_model.py`**

```python
"""入库模型层 — 新增字段/方法。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models._db as _db


class _TempDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        self._orig = _db.DB_PATH
        _db.DB_PATH = self.tmp.name
        from models import init_db
        init_db()

    def tearDown(self):
        _db.DB_PATH = self._orig
        for ext in ('', '-wal', '-shm'):
            p = self.tmp.name + ext
            if os.path.exists(p):
                try: os.unlink(p)
                except OSError: pass


class InboundModelTests(_TempDb):

    def test_inbound_order_set_img_cols(self):
        from models import InboundOrder
        oid = InboundOrder.create('2026-08-13', '供应商X')
        InboundOrder.set_img_cols(oid, 4)
        # 验证落库
        import sqlite3
        conn = sqlite3.connect(self.tmp.name)
        row = conn.execute('SELECT img_cols FROM inbound_orders WHERE id = ?', (oid,)).fetchone()
        conn.close()
        self.assertEqual(row[0], 4)

    def test_inbound_image_source_tag_roundtrip(self):
        from models import InboundOrder, InboundImage
        oid = InboundOrder.create('2026-08-13', '供应商Y')
        iid = InboundImage.create(
            file_path='/tmp/fake.png', order_pk=oid,
            record_pk=None, source='manual', source_tag='备货照'
        )
        result = InboundImage.get_all_by_orders([oid])
        self.assertEqual(len(result[oid]), 1)
        self.assertEqual(result[oid][0]['source_tag'], '备货照')

    def test_inbound_record_get_verified_warnings_default_empty(self):
        from models import InboundOrder, InboundRecord
        oid = InboundOrder.create('2026-08-13', '供应商Z')
        rid = InboundRecord.create('2026-08-13', '供应商Z', '品名', '规格', '10', 'y', '', oid)
        result = InboundRecord.get_verified_warnings(rid)
        self.assertEqual(result, {})


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: 跑测试,确认 RED**

Run:
```bash
PYTHONUTF8=1 python -m pytest tests/test_inbound_model.py -q -p no:warnings
```

Expected: 3 个测试全部 FAIL（方法未实现）

- [ ] **Step 3: 改 `models/orders.py` — 加 3 个新方法**

先找到 `InboundImage` 类（用 grep `class InboundImage`）。读其 `create()` 方法签名（参考 `ShippingImage.create`），加 `source_tag=None` 参数：

```python
@staticmethod
def create(file_path, order_pk, record_pk=None, source='manual',
           original_name='', sort_order=0, source_tag=None):
    # ...原有 INSERT SQL...
    cursor.execute(
        'INSERT INTO inbound_images (file_path, order_pk, record_pk, source, original_name, sort_order, source_tag) '
        'VALUES (?, ?, ?, ?, ?, ?, ?)',
        (file_path, order_pk, record_pk, source, original_name, sort_order, source_tag)
    )
```

找 `InboundImage.get_all_by_orders` 方法（参考 `ShippingImage.get_all_by_orders`），确认返回的 dict 含 `source_tag`（若 SELECT * FROM inbound_images 则自动含）。无需改 SQL。

找 `InboundRecord` 类（参考 `ShippingRecord.get_verified_warnings` 在 `models/orders.py:443`）：

```python
@staticmethod
def get_verified_warnings(record_id: int) -> dict:
    """读某条入库明细的 verified_warnings(JSON),反序列化为 dict,失败返回空 dict。"""
    return _verified_warnings_get('inbound_records', record_id)

@staticmethod
def set_verified_warning(record_id: int, rule_id: str, verified: bool) -> dict:
    """单条规则的核查切换。"""
    return _verified_warnings_set('inbound_records', record_id, rule_id, verified)
```

（前提：`_verified_warnings_get` / `_verified_warnings_set` 辅助函数已存在并在出货用，支持传表名作为参数——若不支持，加一层封装。grep 确认。）

找 `InboundOrder` 类（参考 `ShippingOrder.set_img_cols` 在 `models/orders.py:194`）：

```python
@staticmethod
def set_img_cols(order_id: int, cols: int):
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('UPDATE inbound_orders SET img_cols = ? WHERE id = ?', (cols, order_id))
    conn.commit()
    conn.close()
```

- [ ] **Step 4: 跑测试,确认 GREEN**

Run:
```bash
PYTHONUTF8=1 python -m pytest tests/test_inbound_model.py -q -p no:warnings
```

Expected: 3/3 PASS

- [ ] **Step 5: Commit**

```bash
git add models/orders.py tests/test_inbound_model.py
git commit -m "feat(model): InboundImage.source_tag + InboundRecord verified_warnings + InboundOrder.set_img_cols"
```

---

## Task 3: 入库后端 blueprint — group/item 字段计算 + source_tag 入参 + PATCH img_cols 分支

**Files:**
- Modify: `blueprints/inbound.py:172-225` (在 `groups = InboundRecord.get_groups(...)` 之后, `for group in groups` 循环内)
- Modify: `blueprints/inbound.py:289-348` (PATCH 端点加 img_cols 分支)
- Modify: `blueprints/inbound.py` (行级图片上传端点接受 source_tag)
- Create: `tests/test_inbound_blueprint.py` (后端逻辑测试)

**Interfaces:**
- Consumes: `models/orders.py` 的 `InboundImage.create(source_tag=...)` / `InboundOrder.set_img_cols(...)`
- Produces:
  - `group['has_eco']: bool` — 整组是否有「环保」品名
  - `group['has_jia_mian']: bool` — 整组是否有「杂胶+加面」记录
  - `item['qty_invalid']: bool` — 单条记录数量是否非数字
  - `item['verified_warnings']: dict` — 单条记录已核查的警告规则
  - PATCH `/api/v1/inbound-orders/<id>` 接受 `{img_cols: 1-5}` → 落库
  - POST `/api/v1/inbound-orders/records/<id>/images` 接受 `source_tag` → 落库

- [ ] **Step 1: 写红测试 `tests/test_inbound_blueprint.py`**

```python
"""入库蓝图 — has_eco/has_jia_mian/qty_invalid/verified_warnings + img_cols PATCH + source_tag 入参。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models._db as _db


def _drain_async_jobs(timeout=15):
    import time
    from blueprints.shipping import _ASYNC_JOBS
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not any(j.get('state') == 'processing' for j in list(_ASYNC_JOBS.values())):
            return
        time.sleep(0.02)
    raise AssertionError('后台 OCR 任务超时未结束')


class _TempDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        self._orig = _db.DB_PATH
        _db.DB_PATH = self.tmp.name
        from models import init_db
        init_db()
        from app import create_app
        self.client = create_app().test_client()
        from models.tasks_flow import StaffDB
        sid = StaffDB.create('test', '调度')['id']
        with self.client.session_transaction() as s:
            s['operator_id'] = sid

    def tearDown(self):
        _drain_async_jobs()
        _db.DB_PATH = self._orig
        for ext in ('', '-wal', '-shm'):
            p = self.tmp.name + ext
            if os.path.exists(p):
                try: os.unlink(p)
                except OSError: pass


class InboundGroupComputeTests(_TempDb):

    def test_group_has_eco_true_when_product_contains_环保(self):
        from models import InboundOrder, InboundRecord
        oid = InboundOrder.create('2026-08-13', '供应商A')
        InboundRecord.create('2026-08-13', '供应商A', '环保杂胶', '1m', '10', 'y', '', oid)
        res = self.client.get('/inbound-records?start_date=2026-08-13&end_date=2026-08-13')
        html = res.get_data(as_text=True)
        self.assertIn('row-eco', html, '环保行应有 row-eco class')
        self.assertIn('eco-icon', html, '环保行应有 eco-icon span')

    def test_group_has_jia_mian_true_when_杂胶_加面(self):
        from models import InboundOrder, InboundRecord
        oid = InboundOrder.create('2026-08-13', '供应商B')
        InboundRecord.create('2026-08-13', '供应商B', '杂胶', '0.8黑中加面', '50', 'y', '', oid)
        res = self.client.get('/inbound-records?start_date=2026-08-13&end_date=2026-08-13')
        html = res.get_data(as_text=True)
        self.assertIn('jia-mian-icon', html, '加面行应有 jia-mian-icon SVG')
        self.assertIn('eco-col', html, '表头应有 eco-col 列')

    def test_qty_invalid_marks_row_out_of_stock(self):
        from models import InboundOrder, InboundRecord
        oid = InboundOrder.create('2026-08-13', '供应商C')
        InboundRecord.create('2026-08-13', '供应商C', '品名', '规格', '', 'y', '', oid)
        res = self.client.get('/inbound-records?start_date=2026-08-13&end_date=2026-08-13')
        html = res.get_data(as_text=True)
        self.assertIn('row-out-of-stock', html, '空数量行应有 row-out-of-stock class')
        self.assertIn('stock-icon', html, '空数量行应有 stock-icon ❌')


class InboundPatchImgColsTests(_TempDb):

    def test_patch_img_cols_persists(self):
        from models import InboundOrder
        oid = InboundOrder.create('2026-08-13', '供应商D')
        res = self.client.patch(
            f'/api/v1/inbound-orders/{oid}',
            json={'img_cols': 4}
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data.get('success'))
        self.assertEqual(data.get('img_cols'), 4)
        # 验证落库
        import sqlite3
        conn = sqlite3.connect(self.tmp.name)
        row = conn.execute('SELECT img_cols FROM inbound_orders WHERE id = ?', (oid,)).fetchone()
        conn.close()
        self.assertEqual(row[0], 4)

    def test_patch_img_cols_rejects_out_of_range(self):
        from models import InboundOrder
        oid = InboundOrder.create('2026-08-13', '供应商E')
        res = self.client.patch(
            f'/api/v1/inbound-orders/{oid}',
            json={'img_cols': 9}
        )
        self.assertEqual(res.status_code, 400)


class InboundSourceTagTests(_TempDb):

    def test_record_image_upload_accepts_source_tag(self):
        from models import InboundOrder, InboundRecord
        oid = InboundOrder.create('2026-08-13', '供应商F')
        rid = InboundRecord.create('2026-08-13', '供应商F', '品名', '规格', '10', 'y', '', oid)
        from tests import fake_png_bytes
        data = {
            'image': (fake_png_bytes(), 'test.png'),
            'source_tag': '备货照',
        }
        res = self.client.post(
            f'/api/v1/inbound-orders/records/{rid}/images',
            data=data,
            content_type='multipart/form-data'
        )
        self.assertEqual(res.status_code, 200, res.get_data(as_text=True))
        rj = res.get_json()
        self.assertTrue(rj.get('success'))
        # 验证落库 source_tag
        import sqlite3
        conn = sqlite3.connect(self.tmp.name)
        row = conn.execute('SELECT source_tag FROM inbound_images WHERE record_pk = ?', (rid,)).fetchone()
        conn.close()
        self.assertEqual(row[0], '备货照')

    def test_record_image_upload_rejects_invalid_source_tag(self):
        from models import InboundOrder, InboundRecord
        oid = InboundOrder.create('2026-08-13', '供应商G')
        rid = InboundRecord.create('2026-08-13', '供应商G', '品名', '规格', '10', 'y', '', oid)
        from tests import fake_png_bytes
        data = {
            'image': (fake_png_bytes(), 'test.png'),
            'source_tag': '非法值',
        }
        res = self.client.post(
            f'/api/v1/inbound-orders/records/{rid}/images',
            data=data,
            content_type='multipart/form-data'
        )
        self.assertEqual(res.status_code, 200)
        # 非法值被清空 → source_tag 应为 NULL
        import sqlite3
        conn = sqlite3.connect(self.tmp.name)
        row = conn.execute('SELECT source_tag FROM inbound_images WHERE record_pk = ?', (rid,)).fetchone()
        conn.close()
        self.assertIsNone(row[0])


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: 跑测试,确认 RED**

Run:
```bash
PYTHONUTF8=1 python -m pytest tests/test_inbound_blueprint.py -q -p no:warnings
```

Expected: 7 个测试全部 FAIL（后端未计算这些字段、未接受 source_tag、未加 img_cols PATCH）

- [ ] **Step 3: 改 `blueprints/inbound.py` — group/item 计算**

读 `blueprints/inbound.py:172-225` 找到 `for group in groups` 循环。

在该循环里，给每条 item 加：

```python
# 数量异常标记:空 或 非数字
qty_raw = (item.get('quantity') or '').strip()
try:
    float(qty_raw)
    item['qty_invalid'] = False
except (TypeError, ValueError):
    item['qty_invalid'] = True

# 已核查警告 (per-rule verified_warnings)
item['verified_warnings'] = InboundRecord.get_verified_warnings(item['id'])
```

在该循环外（在每个 group 处理完之后），加：

```python
group['has_eco'] = any('环保' in r.get('product_name', '') for r in group['records'])
group['has_jia_mian'] = any(
    '杂胶' in r.get('product_name', '')
    and '加面' in r.get('specification', '')
    for r in group['records']
)
```

- [ ] **Step 4: 改 `blueprints/inbound.py` — PATCH img_cols 分支**

读 `blueprints/inbound.py:289-348` 找到 `api_v1_inbound_orders_update` 函数。

在 `if 'supplier' in data:` 分支之前，加：

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

- [ ] **Step 5: 改 `blueprints/inbound.py` — 图片上传 source_tag 入参**

读 `blueprints/inbound.py` 找到行级图片上传端点（搜索 `/images`）。

在该端点接受 image 的位置，加：

```python
source_tag = (data.get('source_tag') if request.is_json
              else request.form.get('source_tag'))
_VALID_SOURCE_TAGS = {'备货照', '装车照', '归仓照'}
if source_tag not in _VALID_SOURCE_TAGS:
    source_tag = None
```

调 `InboundImage.create(...)` 时把 `source_tag=source_tag` 加上。

订单级图片上传端点（如 `POST /api/v1/inbound-orders/<id>/images`）同样接受 source_tag。

- [ ] **Step 6: 跑测试,确认 GREEN**

Run:
```bash
PYTHONUTF8=1 python -m pytest tests/test_inbound_blueprint.py -q -p no:warnings
```

Expected: 7/7 PASS

- [ ] **Step 7: Commit**

```bash
git add blueprints/inbound.py tests/test_inbound_blueprint.py
git commit -m "feat(inbound): 后端计算 has_eco/has_jia_mian/qty_invalid/verified_warnings + source_tag 入参 + PATCH img_cols 分支"
```

---

## Task 4: 入库模板 — CSS 6 类 + 重点列 th + 行内 SVG/类/❌

**Files:**
- Modify: `templates/inbound-records.html:4-110` (内联 `<style>` 块末尾)
- Modify: `templates/inbound-records.html:495` (`<table>` 类增加 `no-eco`)
- Modify: `templates/inbound-records.html:500` (新增「重点」`<th class="eco-col">`)
- Modify: `templates/inbound-records.html:514-519` (行 `<tr>` 类增加 `row-out-of-stock` / `row-eco` + ❌ stock-icon + eco-icon/jia-mian-icon SVG)
- Create: `tests/test_inbound_template_render.py` (HTML 渲染断言测试)

**Interfaces:**
- Consumes: 后端计算好的 `group.has_eco` / `group.has_jia_mian` / `item.qty_invalid` / `item.verified_warnings`
- Produces: 入库模板视觉与出货对齐

- [ ] **Step 1: 写红测试 `tests/test_inbound_template_render.py`**

```python
"""入库模板 — 像素级对齐出货的视觉/类断言。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models._db as _db


class _TempDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        self._orig = _db.DB_PATH
        _db.DB_PATH = self.tmp.name
        from models import init_db
        init_db()
        from app import create_app
        self.client = create_app().test_client()
        from models.tasks_flow import StaffDB
        sid = StaffDB.create('test', '调度')['id']
        with self.client.session_transaction() as s:
            s['operator_id'] = sid

    def tearDown(self):
        _db.DB_PATH = self._orig
        for ext in ('', '-wal', '-shm'):
            p = self.tmp.name + ext
            if os.path.exists(p):
                try: os.unlink(p)
                except OSError: pass


class InboundTemplateCssTests(_TempDb):

    def test_css_classes_present(self):
        """CSS 必须包含 6 个新类(与出货完全相同)。"""
        tpl_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'templates', 'inbound-records.html'
        )
        with open(tpl_path, encoding='utf-8') as f:
            content = f.read()
        for cls in [
            '.jia-mian-icon',
            '.eco-icon',
            '.eco-col',
            '.no-eco .eco-col',
            'tr.row-eco',
            'tr.row-out-of-stock',
            '.qty-badge',
            '.record-image-btn.has-image',
        ]:
            self.assertIn(cls, content,
                f'CSS 缺少 {cls}')

    def test_render_eco_row_has_classes_and_icon(self):
        from models import InboundOrder, InboundRecord
        oid = InboundOrder.create('2026-08-13', '供应商H')
        InboundRecord.create('2026-08-13', '供应商H', '环保杂胶', '1m', '10', 'y', '', oid)
        res = self.client.get('/inbound-records?start_date=2026-08-13&end_date=2026-08-13')
        html = res.get_data(as_text=True)
        self.assertIn('class="eco-col"', html, '应有 eco-col 表头')
        self.assertIn('row-eco', html, '环保行应有 row-eco class')
        self.assertIn('eco-icon', html, '环保行应有 eco-icon')

    def test_render_jia_mian_row_has_svg(self):
        from models import InboundOrder, InboundRecord
        oid = InboundOrder.create('2026-08-13', '供应商I')
        InboundRecord.create('2026-08-13', '供应商I', '杂胶', '0.8黑中加面', '50', 'y', '', oid)
        res = self.client.get('/inbound-records?start_date=2026-08-13&end_date=2026-08-13')
        html = res.get_data(as_text=True)
        self.assertIn('jia-mian-icon', html, '加面行应有 jia-mian-icon SVG')

    def test_render_empty_qty_marks_out_of_stock(self):
        from models import InboundOrder, InboundRecord
        oid = InboundOrder.create('2026-08-13', '供应商J')
        InboundRecord.create('2026-08-13', '供应商J', '品名', '规格', '', 'y', '', oid)
        res = self.client.get('/inbound-records?start_date=2026-08-13&end_date=2026-08-13')
        html = res.get_data(as_text=True)
        self.assertIn('row-out-of-stock', html)
        self.assertIn('stock-icon', html)


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: 跑测试,确认 RED**

Run:
```bash
PYTHONUTF8=1 python -m pytest tests/test_inbound_template_render.py -q -p no:warnings
```

Expected: 4 个测试 FAIL（CSS/HTML 都还没改）

- [ ] **Step 3: 改 `templates/inbound-records.html` — 内联 CSS 末尾新增 6 类**

读 `templates/inbound-records.html:4-110` 找到现有 `<style>` 块。在该块的**最后一行 `</style>` 之前**，新增：

```css
/* 重点标识列(与出货对齐):整组无环保+无加面时整列隐藏 */
.inbound-page .jia-mian-icon { display: inline-block; color: #868e96; vertical-align: middle; line-height: 0; }
.inbound-page .eco-icon { color: #2d8c4a; margin-right: 0.25rem; font-size: 0.95rem; vertical-align: middle; }
.inbound-page .eco-icon + .jia-mian-icon { margin-left: 0.15rem; }
.inbound-page .record-table .eco-col { text-align: center; padding: 0.25rem; }
.inbound-page .record-table.no-eco .eco-col { display: none; }
/* 环保行底色(与出货对齐,优先级低于 row-out-of-stock / row-warn / row-info) */
.inbound-page .record-table tr.row-eco:not(.row-out-of-stock):not(.row-warn):not(.row-info) td { background-color: #f0f9f3; }
.inbound-page .record-table tr.row-eco:not(.row-out-of-stock):not(.row-warn):not(.row-info):hover td { background-color: #e3f5e8; }
.inbound-page .record-table tr.row-eco td:first-child { border-left-color: #2d8c4a; }
/* 缺货行底色(与出货对齐) */
.inbound-page .record-table tr.row-out-of-stock td { background: var(--color-out-of-stock-bg); }
.inbound-page .record-table tr.row-out-of-stock td:first-child { border-left-color: var(--color-out-of-stock); }
.inbound-page .record-table tr.row-out-of-stock:hover td { background: #ffcccc; }
.inbound-page .record-table tr.row-out-of-stock .stock-icon { color: var(--color-out-of-stock); font-size: var(--font-md); margin-right: 2px; vertical-align: middle; }
.inbound-page .record-table tr.row-out-of-stock .qty-badge { background: #fab1a0; color: var(--color-out-of-stock); }
/* 行级图片 has-image 红边框(与出货对齐) */
.inbound-page .record-table .record-image-btn.has-image {
    border: 2px solid #dc2626;
}
```

- [ ] **Step 4: 改 `templates/inbound-records.html` — table 类 + 重点列 th**

读 `templates/inbound-records.html:495` 附近，找到 `<table class="record-table">`。

改为：

```jinja
{% set has_eco = group.has_eco|default(false) %}
{% set has_jia_mian = group.has_jia_mian|default(false) %}
<table class="record-table{% if not has_eco and not has_jia_mian %} no-eco{% endif %}">
```

读 `<thead><tr>` 段,在第一列前(序号列之前,或最左)插入「重点」`<th>`：

```jinja
<th class="eco-col" style="width:35px;">重点</th>
```

（如果表头本来就有 7 列,加完变成 8 列。确认其他行的 td 数与 th 数一致。）

- [ ] **Step 5: 改 `templates/inbound-records.html` — 行 tr 类 + 序号 cell + 行内 SVG**

读 `templates/inbound-records.html:514` 附近的 `<tr class="...">`。

改为：

```jinja
<tr class="{% if record.qty_invalid %}row-out-of-stock{% elif record.mismatch == 'warn' or record.piece_mismatch == 'warn' %}row-warn{% elif record.mismatch == 'info' or record.piece_mismatch == 'info' %}row-info{% endif %}{% if '环保' in record.product_name %} row-eco{% endif %}"
    data-record-id="{{ record.id }}"
    data-verified-warnings="{{ record.verified_warnings or '{}' }}">
```

读 `templates/inbound-records.html:515` 附近的 `<td class="product-name product-name-cell">` 序号 cell。

在 `{{ record.product_name }}` 前加 eco-icon / jia-mian-icon：

```jinja
{% if '环保' in record.product_name %}<span class="eco-icon" title="环保产品">🌿</span>{% endif %}
{% if '杂胶' in record.product_name and '加面' in (record.specification or '') %}
    <svg class="jia-mian-icon" viewBox="0 0 16 16" width="14" height="14" title="加面工艺">
        <defs>
            <pattern id="mesh-{{ record.id }}" width="3" height="3" patternUnits="userSpaceOnUse">
                <path d="M0,3 L3,0" stroke="currentColor" stroke-width="0.6"/>
                <path d="M0,0 L3,3" stroke="currentColor" stroke-width="0.6"/>
            </pattern>
        </defs>
        <rect width="16" height="16" fill="url(#mesh-{{ record.id }})"/>
    </svg>
{% endif %}
```

（SVG 内容与 `shipping-records.html:512` 出货版完全相同,实施时直接复制整段。）

- [ ] **Step 6: 跑测试,确认 GREEN**

Run:
```bash
PYTHONUTF8=1 python -m pytest tests/test_inbound_template_render.py -q -p no:warnings
```

Expected: 4/4 PASS

- [ ] **Step 7: Commit**

```bash
git add templates/inbound-records.html tests/test_inbound_template_render.py
git commit -m "feat(inbound): 模板 CSS 6 类 + 重点列 + 环保/加面 SVG + 缺货行 ❌"
```

---

## Task 5: 入库模板 — AI 比对图例条 + source_tag 标签 + verified_warnings 按钮 + API 端点

**Files:**
- Modify: `templates/inbound-records.html` (页面底部加 AI 比对图例条)
- Modify: `templates/inbound-records.html` (`.img-item-record` 渲染加 source_tag 标签)
- Modify: `templates/inbound-records.html` (行下方警告区加 verified_warnings 按钮)
- Modify: `blueprints/inbound.py` (加 `/api/v1/inbound-orders/records/<id>/verify-warning` 端点)
- Create: `tests/test_inbound_warnings_and_tags.py`

**Interfaces:**
- Consumes: `image.source_tag` / `record.verified_warnings` 后端字段
- Produces:
  - 页面可见的 AI 比对图例条
  - `.img-item-record` 旁的 `📷 备货照` 标签
  - 行下方警告折叠 ✓/✗ 按钮
  - PATCH `/api/v1/inbound-orders/records/<id>/verify-warning` 端点

- [ ] **Step 1: 写红测试 `tests/test_inbound_warnings_and_tags.py`**

```python
"""入库 — AI 比对图例条 + source_tag 标签 + verified_warnings 按钮 + API 端点。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models._db as _db


def _drain_async_jobs(timeout=15):
    import time
    from blueprints.shipping import _ASYNC_JOBS
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not any(j.get('state') == 'processing' for j in list(_ASYNC_JOBS.values())):
            return
        time.sleep(0.02)
    raise AssertionError('后台 OCR 任务超时未结束')


class _TempDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        self._orig = _db.DB_PATH
        _db.DB_PATH = self.tmp.name
        from models import init_db
        init_db()
        from app import create_app
        self.client = create_app().test_client()
        from models.tasks_flow import StaffDB
        sid = StaffDB.create('test', '调度')['id']
        with self.client.session_transaction() as s:
            s['operator_id'] = sid

    def tearDown(self):
        _drain_async_jobs()
        _db.DB_PATH = self._orig
        for ext in ('', '-wal', '-shm'):
            p = self.tmp.name + ext
            if os.path.exists(p):
                try: os.unlink(p)
                except OSError: pass


class MatchLegendTests(_TempDb):

    def test_legend_visible(self):
        """AI 比对图例条应在页面渲染。"""
        res = self.client.get('/inbound-records')
        html = res.get_data(as_text=True)
        self.assertIn('AI 比对图例', html, '页面应有 AI 比对图例条')
        self.assertIn('云端 DeepSeek', html, '图例条应含云端 DeepSeek 说明')


class SourceTagRenderTests(_TempDb):

    def test_source_tag_label_in_html(self):
        from models import InboundOrder, InboundRecord, InboundImage
        oid = InboundOrder.create('2026-08-13', '供应商K')
        rid = InboundRecord.create('2026-08-13', '供应商K', '品名', '规格', '10', 'y', '', oid)
        InboundImage.create(
            file_path='/tmp/fake.png', order_pk=oid, record_pk=rid,
            source='manual', source_tag='装车照'
        )
        res = self.client.get('/inbound-records?start_date=2026-08-13&end_date=2026-08-13')
        html = res.get_data(as_text=True)
        self.assertIn('装车照', html, 'source_tag 标签应渲染')
        self.assertIn('source-tag-badge', html, '应有 source-tag-badge class')


class VerifyWarningApiTests(_TempDb):

    def test_verify_warning_endpoint_roundtrip(self):
        from models import InboundOrder, InboundRecord
        oid = InboundOrder.create('2026-08-13', '供应商L')
        rid = InboundRecord.create('2026-08-13', '供应商L', '品名', '规格', '10', 'y', '', oid)
        res = self.client.post(
            f'/api/v1/inbound-orders/records/{rid}/verify-warning',
            json={'rule_id': 'b_white_300g', 'verified': True}
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data.get('success'))
        self.assertEqual(data.get('verified_warnings', {}).get('b_white_300g'), True)


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: 跑测试,确认 RED**

Run:
```bash
PYTHONUTF8=1 python -m pytest tests/test_inbound_warnings_and_tags.py -q -p no:warnings
```

Expected: 3 个测试 FAIL（图例/标签/端点都没做）

- [ ] **Step 3: 改 `templates/inbound-records.html` — AI 比对图例条**

读 `templates/inbound-records.html` 找到页面底部(在所有 `<table>` 之后或合适位置)。

新增 div:

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

- [ ] **Step 4: 改 `templates/inbound-records.html` — source_tag 标签渲染**

读 `templates/inbound-records.html` 找到 `.img-item-record` 渲染块。

在每个图区元素中加：

```jinja
{% if image.source_tag %}
    <span class="source-tag-badge" style="background:#7c3aed;color:#fff;padding:2px 6px;border-radius:4px;font-size:0.7rem;">
        📷 {{ image.source_tag }}
    </span>
{% endif %}
```

(位置:在 `.img-photo` div 内或 `.img-meta` 内,具体位置取决于现有 img-item-record 结构;以视觉合理为准。)

- [ ] **Step 5: 改 `templates/inbound-records.html` — verified_warnings 按钮**

读 `templates/inbound-records.html` 找到行下方警告渲染区(参考 `shipping-records.html:961-986` 的 `addRowWarning` JS 函数和它在模板里生成 ✓/✗ 按钮的部分)。

将 `addRowWarning` / `removeRowWarning` JS 函数从共享模板 `_record_image_script.html` 引入（已经引入,确认变量 `record_pk` 在闭包内可访问）。

如出货模板里有 `verified-warnings-btn` 类,在入库模板同样位置加相同的 ✓/✗ 按钮 HTML(参考出货完整复制)。

- [ ] **Step 6: 改 `blueprints/inbound.py` — verify-warning 端点**

读 `blueprints/inbound.py` 末尾找到合适位置加新端点:

```python
@bp.route('/api/v1/inbound-orders/records/<int:record_id>/verify-warning', methods=['POST'])
def api_v1_inbound_records_verify_warning(record_id):
    """单条警告的核查切换(per-rule verified_warnings)。"""
    data = request.get_json()
    if not data or 'rule_id' not in data:
        return jsonify({'success': False, 'error': '缺少 rule_id 参数'}), 400
    rule_id = str(data['rule_id']).strip()
    verified = bool(data.get('verified', False))
    current = InboundRecord.set_verified_warning(record_id, rule_id, verified)
    return jsonify({'success': True, 'verified_warnings': current,
                    'rule_id': rule_id, 'verified': verified})
```

- [ ] **Step 7: 跑测试,确认 GREEN**

Run:
```bash
PYTHONUTF8=1 python -m pytest tests/test_inbound_warnings_and_tags.py -q -p no:warnings
```

Expected: 3/3 PASS

- [ ] **Step 8: Commit**

```bash
git add templates/inbound-records.html blueprints/inbound.py tests/test_inbound_warnings_and_tags.py
git commit -m "feat(inbound): AI 比对图例条 + source_tag 标签 + verified_warnings 按钮 + API 端点"
```

---

## Task 6: 装柜列数持久化 — DB 列已有,改渲染 + 前端

**Files:**
- Modify: `blueprints/loading.py:160` (去 cookie 改 DB)
- Modify: `templates/loading-orders.html:309` (图片区 `group.img_cols`)
- Modify: `templates/loading-orders.html:745-749, 1274-1278` (JS 动态插入按钮用 DB 值)
- Modify: `templates/loading-orders.html:1028-1045` (删自定义 cookie setImgCols,改用共享)
- Create: `tests/test_loading_img_cols.py` (装柜列数持久化测试)

**Interfaces:**
- Consumes: `loading_orders.img_cols` 列(Task 1 已加) + `LoadingOrder.set_img_cols`(`models/orders.py:1511` 已存在) + 共享 `setImgCols`(`_record_image_script.html:222`)
- Produces:
  - 装柜 `loading-orders.html` 渲染时用 `group.img_cols` 而非 cookie
  - 前端 `setImgCols` 调共享版本(PATCH 后端),而非 cookie 版
  - PATCH 后端已有(`loading.py:269-277`),无需新增

- [ ] **Step 1: 写红测试 `tests/test_loading_img_cols.py`**

```python
"""装柜列数持久化 — DB 而非 cookie。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models._db as _db


class _TempDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        self._orig = _db.DB_PATH
        _db.DB_PATH = self.tmp.name
        from models import init_db
        init_db()
        from app import create_app
        self.client = create_app().test_client()
        from models.tasks_flow import StaffDB
        sid = StaffDB.create('test', '调度')['id']
        with self.client.session_transaction() as s:
            s['operator_id'] = sid

    def tearDown(self):
        _db.DB_PATH = self._orig
        for ext in ('', '-wal', '-shm'):
            p = self.tmp.name + ext
            if os.path.exists(p):
                try: os.unlink(p)
                except OSError: pass


class LoadingImgColsTests(_TempDb):

    def test_render_uses_db_img_cols(self):
        """装柜渲染应用 group.img_cols,非 cookie。"""
        from models import LoadingOrder
        oid = LoadingOrder.create('2026-08-13', '客户X')
        LoadingOrder.set_img_cols(oid, 2)
        res = self.client.get('/loading-orders?start_date=2026-08-13&end_date=2026-08-13')
        html = res.get_data(as_text=True)
        self.assertIn('cols-2', html, '渲染应用 DB 的 cols-2')
        self.assertNotIn('loadingImgCols', html,
            '渲染不应再有 cookie loadingImgCols 痕迹')

    def test_patch_img_cols_roundtrip(self):
        from models import LoadingOrder
        oid = LoadingOrder.create('2026-08-13', '客户Y')
        res = self.client.patch(
            f'/api/v1/loading-orders/{oid}',
            json={'img_cols': 3}
        )
        self.assertEqual(res.status_code, 200)
        # 刷新页面看 DB 值生效
        res2 = self.client.get('/loading-orders?start_date=2026-08-13&end_date=2026-08-13')
        self.assertIn('cols-3', res2.get_data(as_text=True))


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: 跑测试,确认 RED**

Run:
```bash
PYTHONUTF8=1 python -m pytest tests/test_loading_img_cols.py -q -p no:warnings
```

Expected: 2 个测试 FAIL（cookie 版仍生效,DB 列加列后未接渲染）

- [ ] **Step 3: 改 `blueprints/loading.py:160`**

读 `blueprints/loading.py:160`:

```python
current_img_cols = request.cookies.get('loadingImgCols', '3')
```

改为:

```python
# 不再用 cookie:img_cols 由 Task 1 加的 loading_orders.img_cols 列提供,渲染走 group.img_cols
```

(`LoadingOrder.get_groups` 已用 `SELECT * FROM loading_orders`,新列自动带出。)

读 `blueprints/loading.py:161-173` 的 `render_template` 调用,删 `current_img_cols=current_img_cols` 参数。

- [ ] **Step 4: 改 `templates/loading-orders.html:309`**

读 `templates/loading-orders.html:309`:

```html
<div class="img-area cols-{{ current_img_cols | default('3') }}" id="imgArea{{ group.order_pk }}">
```

改为:

```html
<div class="img-area cols-{{ group.img_cols | default(3) }}" id="imgArea{{ group.order_pk }}">
```

读 `templates/loading-orders.html:311-315` 的列数按钮:

```html
<button class="img-col-btn {% if current_img_cols == '1' %}active{% endif %}" onclick="setImgCols(this, 1)">1</button>
```

改为（循环判断从 DB 值读）:

```html
<button class="img-col-btn {% if group.img_cols == 1 %}active{% endif %}" onclick="setImgCols(this, 1)">1</button>
```

5 个按钮都改。

- [ ] **Step 5: 改 `templates/loading-orders.html:1028-1045`**

读该段自定义 cookie 版 setImgCols:

```javascript
window.setImgCols = function(btn, n) {
    ...
    try { document.cookie = 'loadingImgCols=' + n + '; path=/; max-age=31536000'; } catch (e) {}
}
```

**删掉整段**(`window.setImgCols = function(btn, n) {...}` 共约 17 行)。共享模板 `_record_image_script.html:222` 的 `window.setImgCols` 已被 `{% include %}` 引入,会自动接管。

- [ ] **Step 6: 改 `templates/loading-orders.html:745-749, 1274-1278`**

这两段是 JS 动态插入图片区按钮的字符串。

找变量 `savedCols` 的来源(可能是 cookie 读),改为读 `group.img_cols`(具体变量名根据实际代码定)。

如: `'<button ... ' + (savedCols === '1' ? ' active' : '') + '...`

改为(假设 `group` 在闭包可访问): `'<button ... ' + ({{ group.img_cols | default(3) }} === 1 ? ' active' : '') + '...`

(具体语法根据 Jinja 嵌入 JS 字符串的位置调整;原则是渲染时把 DB 值嵌入 JS。)

- [ ] **Step 7: 跑测试,确认 GREEN**

Run:
```bash
PYTHONUTF8=1 python -m pytest tests/test_loading_img_cols.py -q -p no:warnings
```

Expected: 2/2 PASS

- [ ] **Step 8: Commit**

```bash
git add blueprints/loading.py templates/loading-orders.html tests/test_loading_img_cols.py
git commit -m "fix(loading): 列数持久化从 cookie 切到 DB,共用 _record_image_script.html setImgCols"
```

---

## Task 7: Playwright e2e — 视觉对比截图 + 交互断言

**Files:**
- Create: `tests/e2e_inbound_align.py`
- Create: `tests/e2e_loading_img_cols.py`
- Create: `docs/superpowers/e2e/inbound_align.png` (实施时生成)

**Interfaces:**
- Consumes: 启动后的 Flask dev server (http://127.0.0.1:5050)
- Produces: 8 项入库断言 + 2 项装柜断言

- [ ] **Step 1: 写 Playwright e2e `tests/e2e_inbound_align.py`**

```python
"""入库对齐出货 — Playwright 端到端(8 项断言)。"""
import subprocess
import time
import sys
import os
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _seed_db():
    """种入测试数据:1 个环保订单 + 1 个加面订单 + 1 个空数量订单。"""
    import tempfile
    import models._db as _db
    tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp.close()
    _orig = _db.DB_PATH
    _db.DB_PATH = tmp.name
    try:
        from models import init_db, InboundOrder, InboundRecord
        init_db()
        oid = InboundOrder.create('2026-08-13', '供应商E2E')
        InboundRecord.create('2026-08-13', '供应商E2E', '环保杂胶', '1m', '10', 'y', '', oid)
        oid2 = InboundOrder.create('2026-08-13', '供应商E2E')
        InboundRecord.create('2026-08-13', '供应商E2E', '杂胶', '0.8黑中加面', '50', 'y', '', oid2)
        oid3 = InboundOrder.create('2026-08-13', '供应商E2E')
        InboundRecord.create('2026-08-13', '供应商E2E', '品名', '规格', '', 'y', '', oid3)
        return tmp.name
    finally:
        _db.DB_PATH = _orig


def main():
    db_path = _seed_db()
    env = os.environ.copy()
    env['WORKLOG_DB'] = db_path

    # 启动 Flask
    proc = subprocess.Popen(
        [sys.executable, 'app.py'],
        cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    time.sleep(3)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={'width': 1280, 'height': 800})
            page.goto('http://127.0.0.1:5050/login')
            page.fill('input[name="operator"]', 'test')
            page.click('button[type="submit"]')

            page.goto('http://127.0.0.1:5050/inbound-records?start_date=2026-08-13&end_date=2026-08-13')

            errors = []
            def check(cond, msg):
                if not cond:
                    errors.append(msg)

            # 1. 加面图标
            check(page.locator('.jia-mian-icon').count() >= 1, 'jia-mian-icon SVG 未渲染')
            # 2. 环保行底色
            check(page.locator('tr.row-eco').count() >= 1, 'row-eco 行未出现')
            check(page.locator('.eco-icon').count() >= 1, 'eco-icon 未渲染')
            # 3. 缺货行底色
            check(page.locator('tr.row-out-of-stock').count() >= 1, 'row-out-of-stock 行未出现')
            check(page.locator('.stock-icon').count() >= 1, 'stock-icon 未渲染')
            # 4. AI 比对图例条
            check(page.locator('text=AI 比对图例').count() >= 1, '图例条未渲染')
            # 5. 重点列 th
            check(page.locator('th.eco-col').count() >= 1, 'eco-col 表头未出现')

            # 截图保存
            page.screenshot(path=str(ROOT / 'docs/superpowers/e2e/inbound_align.png'), full_page=True)

            browser.close()

            if errors:
                print('❌ FAIL:')
                for e in errors:
                    print(f'  - {e}')
                sys.exit(1)
            print('✅ PASS: 8 项视觉断言全过')
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        try: os.unlink(db_path)
        except OSError: pass


if __name__ == '__main__':
    main()
```

- [ ] **Step 2: 装柜 e2e `tests/e2e_loading_img_cols.py`**

```python
"""装柜列数持久化 — Playwright e2e(2 项断言)。"""
import subprocess
import time
import sys
import os
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _seed_db():
    import tempfile
    import models._db as _db
    tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
    tmp.close()
    _orig = _db.DB_PATH
    _db.DB_PATH = tmp.name
    try:
        from models import init_db, LoadingOrder
        init_db()
        oid = LoadingOrder.create('2026-08-13', '客户E2E')
        return tmp.name, oid
    finally:
        _db.DB_PATH = _orig


def main():
    db_path, oid = _seed_db()
    env = os.environ.copy()
    env['WORKLOG_DB'] = db_path

    proc = subprocess.Popen(
        [sys.executable, 'app.py'],
        cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    time.sleep(3)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto('http://127.0.0.1:5050/login')
            page.fill('input[name="operator"]', 'test')
            page.click('button[type="submit"]')

            page.goto(f'http://127.0.0.1:5050/loading-orders?start_date=2026-08-13&end_date=2026-08-13')

            errors = []
            # 点击列数 2 按钮
            page.locator(f'#imgArea{oid} .img-col-btn').nth(1).click()
            time.sleep(0.5)
            # 刷新页面验证 DB 持久化
            page.reload()
            time.sleep(0.5)
            active_count = page.locator(f'#imgArea{oid} .img-col-btn.active').count()
            if active_count != 1:
                errors.append(f'刷新后无 active 按钮 (count={active_count})')
            active_text = page.locator(f'#imgArea{oid} .img-col-btn.active').text_content()
            if active_text != '2':
                errors.append(f'刷新后 active 按钮应为 2,实际 {active_text}')

            browser.close()

            if errors:
                print('❌ FAIL:')
                for e in errors:
                    print(f'  - {e}')
                sys.exit(1)
            print('✅ PASS: 列数刷新后保留')
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        try: os.unlink(db_path)
        except OSError: pass


if __name__ == '__main__':
    main()
```

- [ ] **Step 3: 跑 e2e,确认 GREEN**

Run:
```bash
PYTHONUTF8=1 python tests/e2e_inbound_align.py
PYTHONUTF8=1 python tests/e2e_loading_img_cols.py
```

Expected: 两个都 PASS

如失败:看 stdout/stderr 输出,定位到具体哪个断言失败,对照 Task 1-6 修补。

- [ ] **Step 4: 跑全量 pytest,确保不破坏现有**

Run:
```bash
PYTHONUTF8=1 python -m pytest tests/ -q -p no:warnings
```

Expected: 全部 PASS（包含 Task 1-6 的新测试 + 所有旧测试）

- [ ] **Step 5: Commit**

```bash
git add tests/e2e_inbound_align.py tests/e2e_loading_img_cols.py docs/superpowers/e2e/inbound_align.png
git commit -m "test(e2e): 入库 9 项视觉断言 + 装柜列数持久化 Playwright"
```

---

## Definition of Done (跨所有 task)

- [ ] Task 1-7 全部 commit 完成
- [ ] `python -m pytest tests/ -v` 全部通过
- [ ] `tests/e2e_inbound_align.py` 8 项视觉断言全过
- [ ] `tests/e2e_loading_img_cols.py` 2 项断言全过
- [ ] 人工截图对比:`inbound_align.png` 与 `shipping-records` 同区域像素级一致
- [ ] DB 迁移幂等(重复 `init_db()` 不报错)
- [ ] 装柜刷新后列数保留