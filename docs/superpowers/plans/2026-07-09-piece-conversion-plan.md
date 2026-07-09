# 件数换算规则 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现「件→目标单位」自动换算系统，支持日本纸(件→张)、快巴纸(件→只)、拷贝纸(件→令)等场景。

**Architecture:** 新增 `piece_conversions` 表存储换算规则，模型 `PieceConversion` 提供匹配/CRUD，共享函数 `_helpers.py` 提供换算与校验逻辑，三个订单蓝图注入数据并在模板中实现自动填充+辅助提示+校验告警三层行为。

**Tech Stack:** Python 3.12 + Flask 3.1.3 + SQLite 3.45.3 + 原生 JavaScript + Tailwind CSS

**Spec:** `docs/superpowers/specs/2026-07-09-piece-conversion-design.md`

## Global Constraints

- 匹配逻辑与 product_units 一致（两轮：精确 spec_keyword → 默认行）
- model 字段仅描述用途，不参与匹配，不在 UNIQUE 约束中
- units_per_piece 存储实际值（不乘 100，与 YPP 的 ×100 不同）
- 件换算与 YPP 换算独立共存，不互相替代

---

## File Map

| 文件 | 操作 | 职责 |
|------|------|------|
| `models/_init.py` | 修改 | 添加 piece_conversions 建表语句 |
| `models/piece_conversion.py` | 新建 | PieceConversion 模型（5 个静态方法） |
| `models/__init__.py` | 修改 | re-export PieceConversion |
| `blueprints/_helpers.py` | 修改 | 添加 5 个共享函数 |
| `blueprints/shipping.py` | 修改 | 注入 piece_conversions + 计算 piece_hint / piece_mismatch |
| `blueprints/inbound.py` | 修改 | 同上 |
| `blueprints/loading.py` | 修改 | 同上 |
| `blueprints/products.py` | 修改 | 添加 /piece-conversions 页面路由 + REST API |
| `templates/piece-conversions.html` | 新建 | 件数换算管理页面 |
| `templates/base.html` | 修改 | 导航栏添加「件数换算」链接 |
| `templates/shipping-records.html` | 修改 | JS 函数 + piece_hint 列 + 自动填充 + 校验 |
| `templates/inbound-records.html` | 修改 | 同上 |
| `templates/loading-orders.html` | 修改 | 同上 |

---

### Task 1: 数据库 — piece_conversions 建表

**Files:**
- Modify: `models/_init.py` (在 product_units 建表后插入)

**Interfaces:**
- Produces: `piece_conversions` 表（id, product_name, spec_keyword, model, units_per_piece, target_unit, is_active, created_at, updated_at）

- [ ] **Step 1: 在 `models/_init.py` 的 `init_db()` 中添加建表语句**

在 `product_units` 建表语句（约第 299 行 `''')` 之后）和 product_units 迁移逻辑（约第 302 行）之前插入：

```python
    # 件数换算规则表（件→张/只/令 等）
    # model 字段仅描述用途，不参与匹配，不在唯一约束中
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS piece_conversions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            product_name    TEXT NOT NULL,
            spec_keyword    TEXT DEFAULT NULL,
            model           TEXT DEFAULT NULL,
            units_per_piece REAL NOT NULL,
            target_unit     TEXT NOT NULL,
            is_active       INTEGER DEFAULT 1,
            created_at      TEXT DEFAULT (datetime('now','localtime')),
            updated_at      TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE(product_name, spec_keyword)
        )
    ''')
```

- [ ] **Step 2: 验证建表**

```bash
cd "C:\Users\Administrator\worklog-app" && python -c "from models import init_db; init_db(); import sqlite3; conn = sqlite3.connect('worklog.db'); cursor = conn.cursor(); cursor.execute(\"SELECT sql FROM sqlite_master WHERE name='piece_conversions'\"); print(cursor.fetchone()[0]); conn.close()"
```

Expected: 输出完整的 CREATE TABLE 语句。

- [ ] **Step 3: 验证不影响现有功能**

```bash
cd "C:\Users\Administrator\worklog-app" && python -c "from app import app; client = app.test_client(); r = client.get('/shipping-records'); print('OK' if r.status_code == 200 else 'FAIL: ' + str(r.status_code))"
```

Expected: `OK`

- [ ] **Step 4: 备份并提交**

```bash
cd "C:\Users\Administrator\worklog-app" && cp worklog.db "D:\BAK\worklog_$(date +'%Y%m%d_%H%M').db" && git add models/_init.py && git commit -m "feat: 添加 piece_conversions 建表语句

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: 模型 — PieceConversion 类

**Files:**
- Create: `models/piece_conversion.py`
- Modify: `models/__init__.py`

**Interfaces:**
- Produces:
  - `PieceConversion.create(product_name, units_per_piece, target_unit, spec_keyword=None, model=None)` → None
  - `PieceConversion.get_all()` → list[dict]
  - `PieceConversion.get_match(product_name, spec='')` → dict | None
  - `PieceConversion.update(id, **kwargs)` → None
  - `PieceConversion.delete(id)` → None

- [ ] **Step 1: 创建 `models/piece_conversion.py`**

```python
"""件数换算规则：件→张/只/令 等"""
from ._db import get_db


class PieceConversion:
    """件数换算规则模型。

    匹配逻辑与 ProductUnit 一致（两轮）：
    1. product_name 相等 + spec_keyword 在规格中出现
    2. product_name 相等 + spec_keyword IS NULL（默认行）
    """

    @staticmethod
    def create(product_name, units_per_piece, target_unit, spec_keyword=None, model=None):
        """创建或替换换算规则。"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO piece_conversions "
            "(product_name, spec_keyword, model, units_per_piece, target_unit) "
            "VALUES (?, ?, ?, ?, ?)",
            (product_name, spec_keyword, model, units_per_piece, target_unit)
        )
        conn.commit()
        conn.close()

    @staticmethod
    def get_all():
        """全表查询，按 product_name + spec_keyword 排序。"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM piece_conversions ORDER BY product_name ASC, spec_keyword ASC"
        )
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    @staticmethod
    def get_match(product_name, spec=''):
        """两轮匹配：先按 spec_keyword 精确匹配，未命中取默认行。

        Args:
            product_name: 商品名
            spec: 规格字符串

        Returns:
            dict 或 None
        """
        conn = get_db()
        cursor = conn.cursor()
        if spec:
            cursor.execute(
                "SELECT * FROM piece_conversions WHERE product_name = ? AND spec_keyword IS NOT NULL",
                (product_name,)
            )
            for row in cursor.fetchall():
                kw = row['spec_keyword']
                if kw and kw in spec:
                    conn.close()
                    return dict(row)
        # fallback 到默认行
        cursor.execute(
            "SELECT * FROM piece_conversions WHERE product_name = ? AND spec_keyword IS NULL",
            (product_name,)
        )
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    @staticmethod
    def update(id, **kwargs):
        """按 id 更新字段。"""
        allowed = ['product_name', 'spec_keyword', 'model',
                   'units_per_piece', 'target_unit', 'is_active']
        sets = []
        vals = []
        for k in allowed:
            if k in kwargs:
                sets.append(f"{k} = ?")
                vals.append(kwargs[k])
        if not sets:
            return
        sets.append("updated_at = datetime('now','localtime')")
        vals.append(id)
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE piece_conversions SET {', '.join(sets)} WHERE id = ?", vals
        )
        conn.commit()
        conn.close()

    @staticmethod
    def delete(id):
        """按 id 删除。"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM piece_conversions WHERE id = ?", (id,))
        conn.commit()
        conn.close()
```

- [ ] **Step 2: 修改 `models/__init__.py` 添加 re-export**

在 `from .products import ...` 行之后添加：

```python
from .piece_conversion import PieceConversion
```

在 `__all__` 列表中添加 `'PieceConversion',`（在 `'ProductUnit', 'ProductCategory', 'Product',` 之后）。

- [ ] **Step 3: 验证**

```bash
cd "C:\Users\Administrator\worklog-app" && python -c "from models import PieceConversion; print('import OK'); r = PieceConversion.get_all(); print('get_all OK, rows:', len(r)); m = PieceConversion.get_match('日本纸777/700', '1.0'); print('get_match:', m)"
```

Expected: `import OK` / `get_all OK, rows: 0` / `get_match: None`

- [ ] **Step 4: 提交**

```bash
cd "C:\Users\Administrator\worklog-app" && git add models/piece_conversion.py models/__init__.py && git commit -m "feat: 添加 PieceConversion 模型（5 个静态方法）

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: 后端共享函数 — blueprints/_helpers.py

**Files:**
- Modify: `blueprints/_helpers.py` (在 `summarize_remarks` 函数之后追加)

**Interfaces:**
- Produces:
  - `extract_pieces_from_remark(remark)` → int | None
  - `get_piece_conversion(product_name, spec, cache=None)` → dict | None
  - `_match_piece_in_cache(product_name, spec, cache)` → dict | None
  - `calc_piece_quantity(remark, conversion)` → float | None
  - `check_piece_mismatch(remark, quantity_str, conversion)` → '' | 'info' | 'warn'

- [ ] **Step 1: 在 `blueprints/_helpers.py` 末尾追加 5 个函数**

```python

# =====================================================================
#  件数换算（件 → 张/只/令）
# =====================================================================

def extract_pieces_from_remark(remark):
    """从备注提取件数，如 '3件' → 3，无则返回 None。"""
    if not remark:
        return None
    m = re.search(r'(\d+)件', remark)
    return int(m.group(1)) if m else None


def _match_piece_in_cache(product_name, spec, cache):
    """在预加载的 piece_conversions 列表中两轮匹配。

    与 _match_unit_in_cache 行为一致：
    1. 优先匹配 spec_keyword 在 spec 中出现的行
    2. 兜底取 spec_keyword 为空的默认行
    """
    spec_lower = (spec or '').lower()
    if spec_lower:
        for pc in cache:
            if pc['product_name'] != product_name:
                continue
            kw = pc['spec_keyword']
            if kw and kw.lower() in spec_lower:
                return pc
    for pc in cache:
        if pc['product_name'] != product_name:
            continue
        if not pc['spec_keyword']:
            return pc
    return None


def get_piece_conversion(product_name, spec, cache=None):
    """查询件数换算规则。

    Args:
        product_name: 商品名
        spec: 规格字符串
        cache: 预加载的 piece_conversions 列表（避免循环查 DB），
               元素需有 product_name / spec_keyword / units_per_piece / target_unit 字段

    Returns:
        dict 或 None
    """
    from models import PieceConversion
    if cache is not None:
        return _match_piece_in_cache(product_name, spec, cache)
    return PieceConversion.get_match(product_name, spec)


def calc_piece_quantity(remark, conversion):
    """备注'X件' → 期望数量 X × units_per_piece。

    Args:
        remark: 备注字符串
        conversion: get_piece_conversion 返回的 dict

    Returns:
        float 期望数量，或 None（备注无件数或无换算规则）
    """
    pieces = extract_pieces_from_remark(remark)
    if pieces is None or conversion is None:
        return None
    return pieces * conversion['units_per_piece']


def check_piece_mismatch(remark, quantity_str, conversion):
    """校验备注件数 vs 实际数量。

    规则（与 check_remark 一致）：
    - 偏差 ≤ 0.01 → ''
    - 1 件偏差 → 'info'（粉色）
    - 多件偏差 → 'warn'（红色）

    Returns:
        '' / 'info' / 'warn'
    """
    pieces = extract_pieces_from_remark(remark)
    if pieces is None or conversion is None:
        return ''
    expected = pieces * conversion['units_per_piece']
    try:
        actual = float(quantity_str)
    except (ValueError, TypeError):
        return ''
    if abs(expected - actual) <= 0.01:
        return ''
    return 'info' if pieces == 1 else 'warn'
```

- [ ] **Step 2: 验证导入**

```bash
cd "C:\Users\Administrator\worklog-app" && python -c "from blueprints._helpers import extract_pieces_from_remark, get_piece_conversion, calc_piece_quantity, check_piece_mismatch; print('extract:', extract_pieces_from_remark('3件')); print('no remark:', extract_pieces_from_remark('hello')); print('no conv qty:', calc_piece_quantity('2件', None))"
```

Expected: `extract: 3` / `no remark: None` / `no conv qty: None`

- [ ] **Step 3: 验证 check_piece_mismatch**

```bash
cd "C:\Users\Administrator\worklog-app" && python -c "from blueprints._helpers import check_piece_mismatch; conv = {'units_per_piece': 1200}; print('match:', repr(check_piece_mismatch('1件', '1200', conv))); print('info:', repr(check_piece_mismatch('1件', '1000', conv))); print('warn:', repr(check_piece_mismatch('3件', '3000', conv))); print('no remark:', repr(check_piece_mismatch('', '1200', conv)))"
```

Expected: `match: ''` / `info: 'info'` / `warn: 'warn'` / `no remark: ''`

- [ ] **Step 4: 提交**

```bash
cd "C:\Users\Administrator\worklog-app" && git add blueprints/_helpers.py && git commit -m "feat: 添加件数换算共享函数（5 个）

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: 蓝图注入 — 三个订单蓝图注入 piece_conversions

**Files:**
- Modify: `blueprints/shipping.py`
- Modify: `blueprints/inbound.py`
- Modify: `blueprints/loading.py`

**Interfaces:**
- Consumes: `PieceConversion.get_all()`, `get_piece_conversion()`, `calc_piece_quantity()`, `check_piece_mismatch()`
- Produces: 模板变量 `piece_conversions`（JS 全局变量），每条记录的新字段 `piece_hint` / `piece_mismatch`

- [ ] **Step 1: 修改 `blueprints/shipping.py`**

（以下展示所有修改点，inline diff 风格）

**1a. Import 修改（第 14-17 行附近）**

```python
from models import (
    ShippingOrder, ShippingRecord, ShippingImage, ProductUnit, PieceConversion, AuditLog
)
from blueprints._helpers import (
    get_upload_dir as get_helpers_upload_dir,
    get_ypp, calc_hint, check_remark, summarize_remarks,
    get_piece_conversion, calc_piece_quantity, check_piece_mismatch,
)
```

**1b. 加载 piece_conversions 并注入模板（第 46-75 行区域）**

在 `units = ProductUnit.get_all()` 块之后添加：

```python
    # 件数换算规则
    piece_convs = PieceConversion.get_all()
    piece_conv_list = [{
        'product_name': pc['product_name'],
        'spec_keyword': pc['spec_keyword'] or '',
        'units_per_piece': pc['units_per_piece'],
        'target_unit': pc['target_unit'],
    } for pc in piece_convs]

    def _get_piece_conv(product_name, spec):
        return get_piece_conversion(product_name, spec, cache=piece_convs)

    def _calc_piece_qty(remark, conv):
        return calc_piece_quantity(remark, conv)

    def _check_piece(remark, quantity_str, conv):
        return check_piece_mismatch(remark, quantity_str, conv)
```

**1c. 在 `for group in groups:` 循环中计算 piece_hint / piece_mismatch**

在现有的 `item['unit_hint'] = ...` / `item['mismatch'] = ...` 块之后添加：

```python
            # 件数换算
            pc = _get_piece_conv(item['product_name'], item.get('specification', ''))
            if pc:
                item['piece_hint'] = f"{pc['units_per_piece']}{pc['target_unit']}/件"
                item['piece_mismatch'] = _check_piece(
                    item.get('remark', ''),
                    item['quantity'],
                    pc
                )
            else:
                item['piece_hint'] = ''
                item['piece_mismatch'] = ''
```

**1d. 在 `render_template` 中添加 `piece_conversions`**

在 `render_template('shipping-records.html', ...)` 调用中添加：

```python
        piece_conversions=piece_conv_list,
```

- [ ] **Step 2: 修改 `blueprints/inbound.py`**

与 shipping.py 相同的 4 个修改点：

**2a. Import：** 添加 `PieceConversion` 到 models import，添加 `get_piece_conversion, calc_piece_quantity, check_piece_mismatch` 到 helpers import。

**2b. 数据加载：** 在 `units = ProductUnit.get_all()` 后添加相同的 piece_convs 加载逻辑。

**2c. 字段计算：** 在 `for group in groups:` 循环中，`item['unit_hint']` 计算之后添加 piece_hint / piece_mismatch。

**2d. 模板注入：** 在 `render_template` 中添加 `piece_conversions=piece_conv_list,`。

- [ ] **Step 3: 修改 `blueprints/loading.py`**

与 shipping.py 相同的 4 个修改点。注：loading.py 的 import 结构和循环模式有所不同，需按实际结构调整。

**Checklist for loading.py:**
- Import PieceConversion + 3 helpers
- 加载 piece_convs 列表
- 在 for group/for item 循环中计算 piece_hint / piece_mismatch
- 注入到 render_template

- [ ] **Step 4: 验证服务器启动 + 数据注入**

```bash
cd "C:\Users\Administrator\worklog-app" && python -c "from app import app; client = app.test_client(); r = client.get('/shipping-records'); print('shipping:', r.status_code); r2 = client.get('/inbound-records'); print('inbound:', r2.status_code); r3 = client.get('/loading-orders'); print('loading:', r3.status_code); print('All OK!' if all(s == 200 for s in [r.status_code, r2.status_code, r3.status_code]) else 'FAIL')"
```

Expected: `shipping: 200` / `inbound: 200` / `loading: 200` / `All OK!`

- [ ] **Step 5: 备份并提交**

```bash
cd "C:\Users\Administrator\worklog-app" && cp worklog.db "D:\BAK\worklog_$(date +'%Y%m%d_%H%M').db" && git add blueprints/shipping.py blueprints/inbound.py blueprints/loading.py && git commit -m "feat: 三个订单蓝图注入 piece_conversions 数据

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 5: 管理页面 — /piece-conversions

**Files:**
- Modify: `blueprints/products.py` (添加路由)
- Create: `templates/piece-conversions.html`
- Modify: `templates/base.html` (导航添加链接)

**Interfaces:**
- Consumes: `PieceConversion.get_all()`, `PieceConversion.create()`, `PieceConversion.update()`, `PieceConversion.delete()`
- Produces: 管理页面 `/piece-conversions`，REST API `/api/v1/piece-conversions`

- [ ] **Step 1: 在 `blueprints/products.py` 中添加管理页面路由**

在文件末尾（`api_v1_products_delete` 路由之后）添加：

```python

# ── 件数换算 ──────────────────────────────────────────────
@bp.route('/piece-conversions')
def piece_conversions():
    """件数换算管理页面"""
    from models import PieceConversion
    conversions = PieceConversion.get_all()
    return render_template('piece-conversions.html',
                           conversions=conversions,
                           page_title='件数换算')


@bp.route('/piece-conversions/add', methods=['POST'])
def piece_conversions_add():
    from models import PieceConversion
    product_name = request.form.get('product_name', '').strip()
    units_str = request.form.get('units_per_piece', '').strip()
    target_unit = request.form.get('target_unit', '').strip()
    spec_kw = request.form.get('spec_keyword', '').strip() or None
    model = request.form.get('model', '').strip() or None
    if not product_name or not units_str or not target_unit:
        flash('商品名、每件单位数和目标单位不能为空', 'error')
        return redirect(url_for('products.piece_conversions'))
    try:
        units_val = float(units_str)
        PieceConversion.create(product_name, units_val, target_unit, spec_kw, model)
        label = f'{product_name}' + (f'（{spec_kw}）' if spec_kw else '')
        flash(f'已添加件数换算: {label}（1件={units_val}{target_unit}）', 'success')
    except (ValueError, TypeError):
        flash('请输入有效的每件单位数', 'error')
    return redirect(url_for('products.piece_conversions'))


@bp.route('/piece-conversions/edit/<int:id>', methods=['POST'])
def piece_conversions_edit(id):
    from models import PieceConversion
    units_str = request.form.get('units_per_piece', '').strip()
    target_unit = request.form.get('target_unit', '').strip()
    if not units_str or not target_unit:
        flash('每件单位数和目标单位不能为空', 'error')
        return redirect(url_for('products.piece_conversions'))
    try:
        units_val = float(units_str)
        PieceConversion.update(id,
                               units_per_piece=units_val,
                               target_unit=target_unit,
                               product_name=request.form.get('product_name', '').strip(),
                               spec_keyword=request.form.get('spec_keyword', '').strip() or None,
                               model=request.form.get('model', '').strip() or None)
        flash(f'已更新件数换算 #{id}', 'success')
    except (ValueError, TypeError):
        flash('请输入有效的每件单位数', 'error')
    return redirect(url_for('products.piece_conversions'))


@bp.route('/piece-conversions/delete/<int:id>', methods=['POST'])
def piece_conversions_delete(id):
    from models import PieceConversion
    PieceConversion.delete(id)
    flash(f'已删除件数换算 #{id}', 'success')
    return redirect(url_for('products.piece_conversions'))
```

- [ ] **Step 2: 创建 `templates/piece-conversions.html`**

参照 `product-units.html` 的结构，字段调整为：商品名、规格关键词、型号、每件单位数、目标单位。包含添加表单、列表表格、行内编辑、删除。

完整模板内容（约 180 行）：

```html
{% extends "base.html" %}
{% block title %}件数换算 — 丰源工作台{% endblock %}
{% block content %}
<style>
.page-card { max-width: 900px; margin: 0 auto; }
.add-form { display: grid; grid-template-columns: 1fr 100px 100px 80px 70px auto; gap: 0.5rem; align-items: end; margin-bottom: 1.5rem; }
.add-form input, .add-form select { padding: 0.4rem 0.5rem; border: 1px solid #dfe6e9; border-radius: 6px; font-size: 0.85rem; }
.add-form button { padding: 0.4rem 0.6rem; background: #00b894; color: #fff; border: none; border-radius: 6px; font-size: 0.85rem; cursor: pointer; font-weight: 600; }
.data-table { width: 100%; border-collapse: collapse; font-size: 0.88rem; }
.data-table th { background: #f8f9fa; padding: 0.5rem 0.6rem; text-align: left; border-bottom: 2px solid #dfe6e9; color: #636e72; font-weight: 600; }
.data-table td { padding: 0.4rem 0.6rem; border-bottom: 1px solid #f1f3f5; }
.data-table tr:hover { background: #f8f9fa; }
.inline-edit-form { display: contents; }
.inline-edit-form input, .inline-edit-form select { width: 100%; padding: 0.25rem 0.35rem; border: 1px solid #74b9ff; border-radius: 4px; font-size: 0.82rem; box-sizing: border-box; }
.btn-sm { padding: 0.2rem 0.5rem; border: none; border-radius: 4px; font-size: 0.78rem; cursor: pointer; }
.btn-edit { background: #74b9ff; color: #fff; }
.btn-delete { background: #e74c3c; color: #fff; }
.btn-save { background: #00b894; color: #fff; }
.btn-cancel { background: #b2bec3; color: #fff; }
</style>

<div class="page-card">
    <h2 class="card-title">🧮 件数换算</h2>
    <p style="color:#636e72; font-size:0.85rem; margin-bottom:1rem;">
        配置商品「件→目标单位」的换算规则。日本纸：件→张，快巴纸：件→只，拷贝纸：件→令。
    </p>

    <!-- 添加表单 -->
    <form method="POST" action="/piece-conversions/add" class="add-form">
        <input type="text" name="product_name" placeholder="商品名" required>
        <input type="text" name="spec_keyword" placeholder="规格关键词">
        <input type="text" name="model" placeholder="型号（可选）">
        <input type="number" name="units_per_piece" placeholder="每件单位数" step="0.1" required>
        <select name="target_unit" required>
            <option value="">目标单位</option>
            <option value="张">张</option>
            <option value="只">只</option>
            <option value="令">令</option>
        </select>
        <button type="submit">➕ 添加</button>
    </form>

    <!-- 列表 -->
    <table class="data-table">
        <thead>
            <tr>
                <th style="width:35px;">ID</th>
                <th>商品名</th>
                <th style="width:100px;">规格关键词</th>
                <th style="width:80px;">型号</th>
                <th style="width:100px;">每件单位数</th>
                <th style="width:70px;">目标单位</th>
                <th style="width:110px;">操作</th>
            </tr>
        </thead>
        <tbody>
            {% for c in conversions %}
            <tr id="row-{{ c.id }}">
                <td style="color:#868e96;">{{ c.id }}</td>
                <td class="cell-product_name">{{ c.product_name }}</td>
                <td class="cell-spec_keyword">{{ c.spec_keyword or '—' }}</td>
                <td class="cell-model">{{ c.model or '—' }}</td>
                <td class="cell-units">{{ c.units_per_piece }}</td>
                <td class="cell-target">{{ c.target_unit }}</td>
                <td>
                    <button class="btn-sm btn-edit" onclick="startEdit({{ c.id }})">✏️</button>
                    <form method="POST" action="/piece-conversions/delete/{{ c.id }}" style="display:inline;" onsubmit="return confirm('确定删除「{{ c.product_name }}{% if c.spec_keyword %}（{{ c.spec_keyword }}）{% endif %}」？')">
                        <button class="btn-sm btn-delete">🗑️</button>
                    </form>
                </td>
            </tr>
            {% endfor %}
        </tbody>
    </table>
    {% if not conversions %}
    <p style="text-align:center; color:#b2bec3; padding:2rem;">暂无件数换算规则</p>
    {% endif %}
</div>

<script>
// 行内编辑
function startEdit(id) {
    var row = document.getElementById('row-' + id);
    var cells = {
        product_name: row.querySelector('.cell-product_name').textContent,
        spec_keyword: row.querySelector('.cell-spec_keyword').textContent,
        model: row.querySelector('.cell-model').textContent,
        units: row.querySelector('.cell-units').textContent,
        target: row.querySelector('.cell-target').textContent,
    };
    // 规格关键词和型号的"—"转为空
    ['spec_keyword', 'model'].forEach(function(k) {
        if (cells[k] === '—') cells[k] = '';
    });

    row.innerHTML =
        '<td style="color:#868e96;">' + id + '</td>' +
        '<td><form id="editForm-' + id + '" method="POST" action="/piece-conversions/edit/' + id + '" class="inline-edit-form">' +
        '<input type="text" name="product_name" value="' + escHtml(cells.product_name) + '" required></td>' +
        '<td><input type="text" name="spec_keyword" value="' + escHtml(cells.spec_keyword) + '"></td>' +
        '<td><input type="text" name="model" value="' + escHtml(cells.model) + '"></td>' +
        '<td><input type="number" name="units_per_piece" value="' + cells.units + '" step="0.1" required></td>' +
        '<td><select name="target_unit" required>' +
            '<option value="张"' + (cells.target === '张' ? ' selected' : '') + '>张</option>' +
            '<option value="只"' + (cells.target === '只' ? ' selected' : '') + '>只</option>' +
            '<option value="令"' + (cells.target === '令' ? ' selected' : '') + '>令</option>' +
        '</select></td>' +
        '<td>' +
            '<button type="submit" class="btn-sm btn-save">💾</button> ' +
            '<button type="button" class="btn-sm btn-cancel" onclick="location.reload()">✖️</button>' +
        '</td></form>';
}

function escHtml(str) {
    return String(str).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}
</script>
{% endblock %}
```

- [ ] **Step 3: 在 `templates/base.html` 导航栏添加链接**

在「商品管理」分组中（`/product-units`、`/product-categories`、`/products` 附近）添加：

```html
<a href="/piece-conversions" class="{% if request.endpoint == 'products.piece_conversions' %}active{% endif %}">🧮 件数换算</a>
```

- [ ] **Step 4: 验证页面**

```bash
cd "C:\Users\Administrator\worklog-app" && python app.py &
sleep 2
# 手动测试：访问 http://127.0.0.1:5050/piece-conversions
# 添加一条测试数据：日本纸777/700, spec=1.0, units=1200, target=张
# 编辑、删除也测一遍
# 然后删除测试数据
```

- [ ] **Step 5: 提交**

```bash
cd "C:\Users\Administrator\worklog-app" && git add blueprints/products.py templates/piece-conversions.html templates/base.html && git commit -m "feat: 添加件数换算管理页面 /piece-conversions

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 6: 前端 JS — 三个订单模板添加三层行为

**Files:**
- Modify: `templates/shipping-records.html`
- Modify: `templates/inbound-records.html`
- Modify: `templates/loading-orders.html`

**Interfaces:**
- Consumes: 模板变量 `piece_conversions`（JS 全局变量 `PIECE_CONVERSIONS`）
- Produces: 5 个 JS 函数 + piece_hint 列显示 + piece_mismatch CSS 类 + 备注输入框自动填充

- [ ] **Step 1: 修改 `templates/shipping-records.html`**

**1a. 在 `<style>` 块中添加 piece_mismatch CSS：**

在现有的 `.row-warn` / `.row-info` 样式附近确认这些样式已存在（它们应该已经有了），无需额外添加。因为 piece_mismatch 复用相同的 `'warn'` / `'info'` 字符串和相同的 CSS 类。

**1b. 在 `<thead>` 中修改表头，「辅助单位提示」列改为两行或更宽，添加件换算提示：**

在表头 `辅助单位提示` 下方添加说明文字（可选），或在现有提示列后追加显示。模板中辅助单位提示列是 `<th>辅助单位提示</th>`，在对应的 `<td class="unit-hint-cell">` 中追加 piece_hint：

在模板的 Jinja2 循环中（`{% for item in group.records %}`），修改辅助单位提示单元格：

```html
<td class="unit-hint-cell" style="font-size:0.85rem;color:#e17055;">
    {{ item.unit_hint or '—' }}
    {% if item.piece_hint %}
    <br><span style="color:#0984e3;">{{ item.piece_hint }}</span>
    {% endif %}
</td>
```

**1c. 修改 `<tr>` 行 CSS 类，追加 piece_mismatch：**

```html
<tr class="{% if item.qty_invalid %}row-out-of-stock{% elif item.mismatch == 'warn' or item.piece_mismatch == 'warn' %}row-warn{% elif item.mismatch == 'info' or item.piece_mismatch == 'info' %}row-info{% endif %}">
```

**1d. 在 JS 部分添加 PIECE_CONVERSIONS 全局变量（在 `var UNIT_LIST = ...` 之后）：**

```javascript
var PIECE_CONVERSIONS = {{ piece_conversions | tojson }};
```

**1e. 添加 5 个 JS 函数（在 `function findUnit` 之后）：**

```javascript
// 查找件数换算规则（两轮匹配）
function findPieceConversion(productName, spec) {
    if (spec) {
        for (var i = 0; i < PIECE_CONVERSIONS.length; i++) {
            var pc = PIECE_CONVERSIONS[i];
            if (pc.product_name === productName && pc.spec_keyword && spec.indexOf(pc.spec_keyword) !== -1) {
                return pc;
            }
        }
    }
    for (var i = 0; i < PIECE_CONVERSIONS.length; i++) {
        var pc = PIECE_CONVERSIONS[i];
        if (pc.product_name === productName && !pc.spec_keyword) {
            return pc;
        }
    }
    return null;
}

// 从备注提取件数
function extractPiecesFromRemark(remark) {
    if (!remark) return null;
    var m = remark.match(/(\d+)件/);
    return m ? parseInt(m[1]) : null;
}

// 获取件换算提示文本
function getPieceHint(productName, spec) {
    var pc = findPieceConversion(productName, spec);
    if (pc) {
        return pc.units_per_piece + pc.target_unit + '/件';
    }
    return '';
}

// 从备注件数计算期望数量
function calcPieceQuantity(remark, productName, spec) {
    var pieces = extractPiecesFromRemark(remark);
    if (pieces === null) return null;
    var pc = findPieceConversion(productName, spec);
    if (!pc) return null;
    return pieces * pc.units_per_piece;
}

// 校验件数 vs 实际数量（返回 '' / 'info' / 'warn'）
function checkPieceMismatch(remark, qty, productName, spec) {
    var pieces = extractPiecesFromRemark(remark);
    if (pieces === null) return '';
    var pc = findPieceConversion(productName, spec);
    if (!pc) return '';
    var expected = pieces * pc.units_per_piece;
    var actual = parseFloat(qty);
    if (isNaN(actual)) return '';
    if (Math.abs(expected - actual) <= 0.01) return '';
    return pieces === 1 ? 'info' : 'warn';
}
```

**1f. 更新 `renderAiResults` 中动态生成的行**（约第 1198 行），追加 piece_hint 和 piece_mismatch：

在 `renderAiResults` 中添加行的代码中（`data.records.forEach(function(item, idx) {...}`），在计算 `hint` 和 `mismatch` 后添加：

```javascript
var pieceHint = getPieceHint(item.product_name, item.specification || '');
var pieceMismatch = checkPieceMismatch(item.remark || '', item.quantity, item.product_name, item.specification || '');
```

在 `tr.className` 中追加 pieceMismatch 判断（紧随 `var bad = isInvalidQty(item.quantity);` 之后）：

```javascript
if (!bad && (mismatch === 'warn' || pieceMismatch === 'warn')) { tr.className = 'row-warn'; }
else if (!bad && (mismatch === 'info' || pieceMismatch === 'info')) { tr.className = 'row-info'; }
if (bad) { tr.className = 'row-out-of-stock'; }
```

在渲染的 `<td>` 中追加 piece_hint：

```javascript
'<td class="unit-hint-cell" style="font-size:0.85rem;color:#e17055;">' + (hint || '—') + (pieceHint ? '<br><span style="color:#0984e3;">' + pieceHint + '</span>' : '') + '</td>' +
```

**1g. 同样更新 `batchAddRecords` 渲染逻辑**（约第 1292 行），追加相同的 piece_hint 和 piece_mismatch 计算。

**1h. 添加备注自动填充事件**（在 AI 识别结果表格的 remark 输入框上）：

在 `renderAiResults` 函数中，给 remark input 添加 `oninput` 属性：

```javascript
'<td><input type="text" value="' + escHtml(item.remark || '') + '" data-field="remark" oninput="autoFillQtyFromRemark(this)"></td>'
```

添加全局 `autoFillQtyFromRemark` 函数：

```javascript
// 备注件数自动填充数量
function autoFillQtyFromRemark(inputEl) {
    var row = inputEl.closest('tr');
    var remark = inputEl.value.trim();
    var pieces = extractPiecesFromRemark(remark);
    if (pieces === null) return;
    // 仅当备注为纯件数（不含 +/* 运算语义）时自动填充
    if (/[+*]/.test(remark)) return;
    var productName = (row.querySelector('[data-field="product_name"]') || {}).value || '';
    var spec = (row.querySelector('[data-field="specification"]') || {}).value || '';
    var pc = findPieceConversion(productName, spec);
    if (!pc) return;
    var qtyInput = row.querySelector('[data-field="quantity"]');
    if (!qtyInput) return;
    // 仅当数量为空或未被手动修改时才自动填充
    if (!qtyInput.dataset.userEdited) {
        qtyInput.value = pieces * pc.units_per_piece;
    }
}

// 标记数量框被手动修改
document.addEventListener('input', function(e) {
    if (e.target.dataset && e.target.dataset.field === 'quantity') {
        e.target.dataset.userEdited = '1';
    }
});
```

- [ ] **Step 2: 修改 `templates/inbound-records.html`**

与 shipping 相同的修改模式：
- 添加 `var PIECE_CONVERSIONS = {{ piece_conversions | tojson }};`
- 添加 5 个 JS 函数（`findInboundPieceConversion` 或命名保持一致的 `findPieceConversion`）
- 在表格的 Jinja2 渲染中添加 `piece_hint` 显示
- 在 `tr` class 中追加 `piece_mismatch` 判断
- 在动态渲染行的 JS 代码中追加 piece_hint / piece_mismatch
- 添加自动填充逻辑

**注意：** inbound 模板的 JS 函数命名已有前缀惯例（如 `findInboundUnit`），件换算函数可相应命名为 `findPieceConversion`（统一命名，不区分前缀，因为三模板共享相同的函数名更易维护）。如果 inbound 模板已有冲突的函数名，使用 `findPieceConversion` 即可。

- [ ] **Step 3: 修改 `templates/loading-orders.html`**

与 shipping/inbound 相同的修改模式。注意 loading 模板的 JS 函数模式（如 `findLoadingUnit`），同样使用统一的 `findPieceConversion` 命名。

- [ ] **Step 4: E2E 验证**

```bash
cd "C:\Users\Administrator\worklog-app" && python app.py &
# 1. 在管理页面添加测试数据: 日本纸777/700, spec=1.0, units=1200, target=张
# 2. 打开发货记录页，确认 PIECE_CONVERSIONS 已注入（console 输入 PIECE_CONVERSIONS 应看到数组）
# 3. 在已有日本纸明细行确认辅助单位提示列显示 "1200张/件"
# 4. 通过AI识别或批量添加表单，在备注输入"3件"，验证数量框自动填充 3600
# 5. 手动改数量为 3000，确认行标红（warn）
# 6. 清理测试数据
kill %1
```

- [ ] **Step 5: 备份并提交**

```bash
cd "C:\Users\Administrator\worklog-app" && cp worklog.db "D:\BAK\worklog_$(date +'%Y%m%d_%H%M').db" && git add templates/shipping-records.html templates/inbound-records.html templates/loading-orders.html && git commit -m "feat: 三个订单模板添加件数换算三层行为（自动填充+提示+校验）

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## 完成检查

全部 6 个 Task 完成后：

1. 启动服务器 `python app.py`
2. 访问 `/piece-conversions` 添加 1-2 条测试规则
3. 访问 `/shipping-records` 验证：
   - 日本纸行的辅助单位提示列显示 `1200张/件`
   - 备注写"2件"后数量自动填充 2400
   - 数量不符时行标粉/红
4. `/inbound-records` 和 `/loading-orders` 同样验证
5. 删除测试数据
6. `git log --oneline -6` 确认 6 个 commit 完整
