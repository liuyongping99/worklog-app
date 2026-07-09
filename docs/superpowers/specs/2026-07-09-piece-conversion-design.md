# 件数换算规则 — 设计文档

**日期**: 2026-07-09
**状态**: 待实现

## 概述

实现「件→目标单位」的自动换算系统。用户在明细备注中写"X件"，系统自动计算对应的目标单位数量（如张、只、令），并在辅助提示列显示换算系数，同时校验实际数量是否与件数推算一致。

### 业务场景

| 商品类型 | 换算 | 示例 |
|----------|------|------|
| 日本纸 | 件 → 张 | 1件 1.0规格 = 1200张 |
| 快巴纸 | 件 → 只 | 1件 = 100只 |
| 拷贝纸 | 件 → 令 | 1件 = 10令 |

换算系数由商品名 + 规格决定，15 个日本纸规格各有独立系数。

---

## 数据库

### 新表 `piece_conversions`

```sql
CREATE TABLE piece_conversions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    product_name    TEXT NOT NULL,            -- 商品名，如"日本纸777/700"
    spec_keyword    TEXT DEFAULT NULL,        -- 规格关键词，如"1.0"。NULL=默认行
    model           TEXT DEFAULT NULL,        -- 型号（仅描述用途，不参与匹配）
    units_per_piece REAL NOT NULL,            -- 每件=多少目标单位，如 1200.0
    target_unit     TEXT NOT NULL,            -- '张' | '只' | '令'
    is_active       INTEGER DEFAULT 1,
    created_at      TEXT DEFAULT (datetime('now','localtime')),
    updated_at      TEXT DEFAULT (datetime('now','localtime')),
    UNIQUE(product_name, spec_keyword)        -- model 不在唯一键中
);
```

### 匹配逻辑（两轮）

与 `product_units` 的 `get_match()` 逻辑一致：

1. **精确匹配**：找 `product_name` 相等 + `spec_keyword` 在规格字符串中出现的行
2. **默认兜底**：取 `spec_keyword IS NULL` 的默认行

---

## 模型 `models/piece_conversion.py`

纯静态方法，风格与 `ProductUnit` 一致：

| 方法 | 签名 | 用途 |
|------|------|------|
| `create` | `(product_name, units_per_piece, target_unit, spec_keyword=None, model=None)` | INSERT OR REPLACE |
| `get_all` | `()` | 全表查询，返回 list[dict] |
| `get_match` | `(product_name, spec='')` | 两轮匹配，返回 dict or None |
| `update` | `(id, **kwargs)` | 按 id 更新 |
| `delete` | `(id)` | 按 id 删除 |

### `get_match` 实现

```python
@staticmethod
def get_match(product_name, spec=''):
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
    cursor.execute(
        "SELECT * FROM piece_conversions WHERE product_name = ? AND spec_keyword IS NULL",
        (product_name,)
    )
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None
```

---

## 共享函数 `blueprints/_helpers.py` 新增

### `get_piece_conversion(product_name, spec, cache=None)`

在预加载的 cache 或 DB 中匹配换算规则。

```python
def get_piece_conversion(product_name, spec, cache=None):
    """查询 piece_conversions，返回 dict 或 None"""
    from models import PieceConversion
    if cache is not None:
        return _match_piece_in_cache(product_name, spec, cache)
    return PieceConversion.get_match(product_name, spec)
```

### `_match_piece_in_cache(product_name, spec, cache)`

两轮匹配的缓存版本，与 `_match_unit_in_cache` 逻辑一致。

### `extract_pieces_from_remark(remark)`

```python
def extract_pieces_from_remark(remark):
    """从备注提取件数，如 '3件' → 3，无则返回 None"""
    if not remark:
        return None
    m = re.search(r'(\d+)件', remark)
    return int(m.group(1)) if m else None
```

### `calc_piece_quantity(remark, conversion)`

```python
def calc_piece_quantity(remark, conversion):
    """备注'X件' → 期望数量 X × units_per_piece，返回 float 或 None"""
    pieces = extract_pieces_from_remark(remark)
    if pieces is None or conversion is None:
        return None
    return pieces * conversion['units_per_piece']
```

### `check_piece_mismatch(remark, quantity_str, conversion)`

```python
def check_piece_mismatch(remark, quantity_str, conversion):
    """校验备注件数 vs 实际数量，返回 ''/'info'/'warn'
    
    规则（与 check_remark 一致）：
      - 偏差 ≤ 0.01：不告警
      - 1件偏差：info（粉色）
      - 多件偏差：warn（红色）
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

---

## 蓝图集成

### 列表页预加载

在 `shipping.py` / `inbound.py` / `loading.py` 的列表页 GET 路由中，查询全表注入模板：

```python
from models import PieceConversion
piece_conversions = PieceConversion.get_all()
return render_template('...', piece_conversions=piece_conversions, ...)
```

模板中用 `<script>` 变量暴露给 JS（与现有 `UNIT_LIST` / `INBOUND_UNIT_LIST` / `LOADING_UNIT_LIST` 模式一致）。

---

## 前端 JS（三层行为）

### 🅐 自动填充

触发：备注输入框 `oninput`

```
备注输入 "3件"
  → extractPiecesFromRemark(remark) → 3
  → findPieceConversion(productName, spec) → {units_per_piece: 1200, target_unit: '张'}
  → 自动填入 quantity = 3 × 1200 = 3600
  → 同时刷新辅助提示列
```

**细节**：
- 仅当备注为纯 "X件"（不含 `+`/`*` 运算语义）时自动填充
- 自动填充后不锁死数量框，用户可手动修改
- 如果用户手动修改过的数量又被自动填充覆盖 → 用 `data-user-edited` 标记避免覆盖

### 🅑 辅助提示

在现有辅助单位提示列中追加换算信息：

```
getPieceHint(productName, spec, remark):
  命中 piece_conversions → 显示 "{units_per_piece}{target_unit}/件"
  如 "1200张/件"
```

与 YPP 提示共存时，两者都显示（分行或并列）。

### 🅒 校验告警

在现有 mismatch 逻辑旁边增加件数校验：

```
checkPieceMismatch(remark, qty, conversion):
  expected = pieces × units_per_piece
  actual ≠ expected:
    1件偏差 → 行标粉色 (info)
    多件偏差 → 行标红色 (warn)
```

触发时机：页面加载 + 明细编辑保存后 + 备注/数量修改后。

### JS 函数汇总

每个模板需新增的函数（与现有 `findUnit`/`getHint`/`checkMismatch` 同级）：

| 函数 | 用途 |
|------|------|
| `findPieceConversion(productName, spec)` | 匹配换算规则 |
| `extractPiecesFromRemark(remark)` | 提取件数 |
| `getPieceHint(productName, spec)` | 生成辅助提示文本 |
| `getPieceQuantity(remark, conv)` | 计算期望数量 |
| `checkPieceMismatch(remark, qty, conv)` | 校验 mismatch |

---

## 管理页面 `/piece-conversions`

### 路由

- `GET /piece-conversions` — 管理页面
- `GET /api/v1/piece-conversions` — 查询全表
- `POST /api/v1/piece-conversions` — 创建
- `PUT /api/v1/piece-conversions/<id>` — 更新
- `DELETE /api/v1/piece-conversions/<id>` — 删除

### 注册

在 `blueprints/products.py` 中添加路由。

### 导航

`base.html` 导航栏「商品管理」分组 → 添加「件数换算」。

---

## 实现顺序

1. **数据库** — `models/_init.py` 加建表语句
2. **模型** — 新建 `models/piece_conversion.py`（5 个静态方法），`models/__init__.py` re-export
3. **后端共享函数** — `blueprints/_helpers.py` 加 5 个函数
4. **蓝图注入** — shipping/inbound/loading 三个列表页注入 `piece_conversions` 数据
5. **管理页面** — `/piece-conversions` 页面 + REST API + 导航链接
6. **前端 JS** — 三个订单模板加自动填充 + 提示 + 校验逻辑

---

## 与现有系统的关系

| 现有组件 | 关系 |
|----------|------|
| `product_units` | 独立的匹配逻辑，不修改现有表 |
| `calc_hint` / `check_remark` | 新函数并行存在，不替换 |
| 辅助单位提示列 | 追加件换算信息，YPP 和件换算共存 |
| mismatch 校验 | 追加件数校验，与支数码校验独立 |

---

## 扩展性

未来如需支持新的换算方向（如"箱→个"、"包→码"），只需：
1. 在 `piece_conversions` 中新增 `conversion_type` 列区分换算类型
2. 前端 `getPieceHint` / `checkPieceMismatch` 根据 `target_unit` 和 `conversion_type` 调整提示文案

当前设计已预留 `target_unit` 字段承载不同目标单位的显示，无需改表。
