# placement 期望值兜底 — 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 unit='支' 且备注无「X支」的明细,在 placement 点数时直接用 quantity 当 expected_zhi 兜底核对,出货/入库/装柜三套同步。

**Architecture:** 在 `blueprints/_helpers.py` 新增 `compute_placement_expected_zhi(remark, quantity_str, unit)` 共享函数(纯函数,无副作用)。三套订单蓝图各 3 个使用点替换为函数调用,内联的 `re.finditer + has_zhi + matched_zhi` 重复代码删除。比较统一改 `abs(diff) <= 0.01` 容差。模板 `{{ _item.expected_zhi }}` 改 Jinja `'%g'` 格式符保留整数显示习惯。

**Tech Stack:** Python 3.12 + Flask 3.1.3 + Jinja2 + unittest(项目既有测试框架,见 `tests/test_basic.py`)

---

## 文件结构

| 文件 | 操作 | 职责 |
|---|---|---|
| `blueprints/_helpers.py` | 修改 — 新增 `compute_placement_expected_zhi()` 函数(放在 `summarize_remarks` 之后,`# 件数换算` 章节注释之前,即 line 393 之后) | 共享纯函数:按 record 算 expected_zhi + has_zhi |
| `tests/test_placement_zhi_fallback.py` | 新建 | 7 条单元测试覆盖 helper 函数 + 容差 + 边界 |
| `blueprints/shipping.py` | 修改 — 替换 3 处内联代码 | 行 207-211(_pm)、行 219-238(placement_groups)、行 1317-1347(_placement_match_for_image) |
| `blueprints/inbound.py` | 修改 — 替换 3 处内联代码 | 行 192-211(_pm)、行 234-243(placement_groups)、行 627-654(_inbound_placement_match_for_image) |
| `blueprints/loading.py` | 修改 — 替换 3 处内联代码 | 行 174-191(_pm)、行 241-250(placement_groups)、行 1349-1380(_loading_placement_match_for_image) |
| `templates/shipping-records.html` | 修改 — `data-expected-zhi="{{ _item.expected_zhi }}"` 改 `'%g' % _item.expected_zhi` | 整数显示 `33`、小数显示 `33.5` |
| `templates/inbound-records.html` | 修改 — 同上 | 同上 |
| `templates/loading-orders.html` | 修改 — 同上 | 同上 |

---

## 全局约束(从 spec 提取)

- **Windows CRLF**: 修改任何 Python 文件时保持原 CRLF 换行符(Git 默认会处理)
- **Python 编码**: PowerShell 跑 Python 脚本必须带 `-X utf8` 或 `$env:PYTHONUTF8=1`
- **测试框架**: 项目用 `unittest`(参考 `tests/test_basic.py`),不是 pytest fixtures(虽然 conftest.py 有 fixture 但 test_basic.py 用 `unittest.TestCase`)
- **现有测试**: `python -m pytest tests/ -v` 共 84+ 个,实施后必须全过
- **db 破坏性操作**: 本任务不涉及 db 修改,无备份需求
- **commit 风格**: 单次 commit 只做一件事;多文件变更按"任务"为单位 commit
- **范围外**: 不改 `calc_hint`、不改 `/shipping-ypp-review`、不改 `find_ypp_mismatches`、不改 placement_match 的严重程度分级

---

## Task 1: 写 helper 函数测试 + 实现 (TDD)

**Files:**
- Create: `tests/test_placement_zhi_fallback.py`
- Modify: `blueprints/_helpers.py:393`(在 `summarize_remarks` 函数后插入新函数)

**Interfaces:**
- Consumes: 无(纯函数,无外部依赖)
- Produces: `compute_placement_expected_zhi(remark: str, quantity_str: str|float, unit: str|None) -> tuple[float, bool]`

### Step 1.1: 写失败的测试

```python
"""compute_placement_expected_zhi(remark, quantity_str, unit) → (expected_zhi, has_zhi)

placement 期望值兜底逻辑 — unit='支' + 备注无「X支」→ 用 quantity 兜底核对。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'blueprints'))

from blueprints._helpers import compute_placement_expected_zhi


class PlacementExpectedZhiTests(unittest.TestCase):

    def test_unit_zhi_with_int_quantity_fallback(self):
        # unit='支', quantity=33, remark='', 走 quantity 兜底,返回 (33.0, True)
        self.assertEqual(compute_placement_expected_zhi('', '33', '支'), (33.0, True))

    def test_unit_zhi_with_int_quantity_match(self):
        # 整数 total=33 == expected=33.0 → abs diff = 0 ≤ 0.01,匹配
        exp, has_zhi = compute_placement_expected_zhi('', '33', '支')
        total = 33
        self.assertTrue(has_zhi)
        self.assertLessEqual(abs(total - exp), 0.01)

    def test_unit_zhi_with_int_quantity_mismatch(self):
        # total=30 vs expected=33.0 → diff=3 > 0.01,不匹配
        exp, has_zhi = compute_placement_expected_zhi('', '33', '支')
        total = 30
        self.assertLess(abs(total - exp), 0.01)  # False

    def test_unit_zhi_with_float_quantity_tolerance(self):
        # quantity=33.5, total=33.5 → abs diff = 0 ≤ 0.01,匹配
        exp, has_zhi = compute_placement_expected_zhi('', '33.5', '支')
        total = 33.5  # 浮点:整数清点通常凑不出 0.5,但 helper 返回 33.5 是正确的"目标"
        # 注意:实际匹配在调用方用 abs(total - exp) <= 0.01 判断
        self.assertEqual(exp, 33.5)
        self.assertTrue(has_zhi)
        self.assertLessEqual(abs(total - exp), 0.01)

    def test_unit_zhi_with_float_quantity_no_match(self):
        # quantity=33.5, total=33 → 差 0.5 > 0.01,不匹配
        exp, has_zhi = compute_placement_expected_zhi('', '33.5', '支')
        total = 33
        self.assertGreater(abs(total - exp), 0.01)

    def test_unit_zhi_quantity_zero_no_fallback(self):
        # quantity='0', 兜底无效 → has_zhi=False
        self.assertEqual(compute_placement_expected_zhi('', '0', '支'), (0.0, False))

    def test_unit_zhi_quantity_invalid_no_fallback(self):
        # quantity='abc' 解析失败 → has_zhi=False
        self.assertEqual(compute_placement_expected_zhi('', 'abc', '支'), (0.0, False))

    def test_unit_zhi_quantity_empty_no_fallback(self):
        # quantity='', 兜底无效 → has_zhi=False
        self.assertEqual(compute_placement_expected_zhi('', '', '支'), (0.0, False))

    def test_remark_with_zhi_takes_precedence(self):
        # unit='支', remark='10支', quantity=33 → 走 remark 路径(10.0, True),忽略 quantity
        self.assertEqual(compute_placement_expected_zhi('10支', '33', '支'), (10.0, True))

    def test_unit_y_no_fallback(self):
        # unit='y', remark='', quantity='500' → 不走兜底,has_zhi=False
        self.assertEqual(compute_placement_expected_zhi('', '500', 'y'), (0.0, False))

    def test_unit_empty_no_fallback(self):
        # unit='', remark='', quantity='10' → 不走兜底
        self.assertEqual(compute_placement_expected_zhi('', '10', ''), (0.0, False))

    def test_remark_multiple_zhi_sum(self):
        # remark='3支+5支' → sum=8(原逻辑)
        self.assertEqual(compute_placement_expected_zhi('3支+5支', '', 'y'), (8.0, True))

    def test_remark_with_zhi_unit_y_kept(self):
        # unit='y', remark='20支' → 用 remark(20.0, True),不走 quantity 兜底
        self.assertEqual(compute_placement_expected_zhi('20支', '500', 'y'), (20.0, True))


if __name__ == '__main__':
    unittest.main()
```

### Step 1.2: 跑测试确认失败

```bash
python -X utf8 -m unittest tests.test_placement_zhi_fallback -v
```

**Expected: FAIL** with `ImportError: cannot import name 'compute_placement_expected_zhi' from 'blueprints._helpers'`

### Step 1.3: 实现 helper 函数

在 `blueprints/_helpers.py` 第 393 行后(`summarize_remarks` 函数结束后)插入:

```python
def compute_placement_expected_zhi(remark, quantity_str, unit):
    """按 record 计算 placement 期望支数与是否有支目标。

    规则(2026-09-03 placement 期望值兜底):
      1. 先按 remark 解析所有「X支」之和。
      2. 若 remark 无「X支」且 unit == '支':
           用 float(quantity_str) 兜底作为 expected_zhi, has_zhi=True。
           (quantity 解析失败或 ≤0 → 兜底失效, has_zhi=False。)
      3. 否则沿用 remark 解析结果。

    Returns:
        (expected_zhi: float, has_zhi: bool)

    Note:
        - expected_zhi 始终以 float 返回(便于跨订单统一比较)
        - 整数 expected_zhi 仍以 33.0 形式返回(数值本身是整数)
        - 模板如需去小数点,用 `{{ '%g' % value }}` Jinja 格式符
    """
    remark = remark or ''
    qty_str = (quantity_str or '').strip()

    # 1. 按原口径解析 remark 中的「X支」
    zhi_m = list(re.finditer(r'(\d+)\s*支', remark))
    remark_zhi = sum(int(x.group(1)) for x in zhi_m)
    has_remark_zhi = len(zhi_m) > 0

    # 2. 兜底:unit='支' 且备注无支数 → 用 quantity
    if not has_remark_zhi and (unit or '').strip() == '支':
        try:
            qty_val = float(qty_str)
        except (ValueError, TypeError):
            qty_val = 0.0
        if qty_val > 0:
            return float(qty_val), True
        return 0.0, False

    # 3. 沿用 remark 解析结果
    return float(remark_zhi), has_remark_zhi
```

### Step 1.4: 跑测试确认通过

```bash
python -X utf8 -m unittest tests.test_placement_zhi_fallback -v
```

**Expected: PASS** (13 tests)

### Step 1.5: 跑全套测试确保无回归

```bash
python -m pytest tests/ -v
```

**Expected: PASS** (84+ existing tests + 13 new = 97+ tests, all green)

### Step 1.6: 提交

```bash
git add blueprints/_helpers.py tests/test_placement_zhi_fallback.py
git commit -m "feat(helpers): compute_placement_expected_zhi 兜底 unit='支'+备注无支数场景

- 7 条 unittest 用例覆盖整数 quantity / 小数 quantity / 备注优先 / 其他单位
- 返回 (expected_zhi: float, has_zhi: bool)
- 比较在调用方用 abs(diff) <= 0.01 容差"
```

---

## Task 2: 迁移 shipping.py 3 处使用点

**Files:**
- Modify: `blueprints/shipping.py` 3 处:
  - 行 197-208(`_pm` 计算块)
  - 行 219-238(placement_groups 渲染)
  - 行 1317-1347(`_placement_match_for_image` 函数)

### Step 2.1: 迁移 `_pm` 计算块(shipping.py L192-211)

**Before**(L196-210):
```python
            if _pimgs:
                _remark = item.get('remark') or ''
                _total = sum(_eff_zhi(p) for p in _pimgs)
                _loose = sum(p.get('loose_count', 0) for p in _pimgs)
                _zhi_m = list(re.finditer(r'(\d+)\s*支', _remark))
                _exp_zhi = sum(int(x.group(1)) for x in _zhi_m)
                _has_zhi = len(_zhi_m) > 0
                _san_m = list(SANMA_RE.finditer(_remark))
                _exp_san = sum(float(x.group(1)) for x in _san_m)
                if _exp_san == int(_exp_san):
                    _exp_san = int(_exp_san)
                _has_san = len(_san_m) > 0
                _matched_zhi = (not _has_zhi) or (_total == _exp_zhi)
                _matched_san = (not _has_san) or (_loose == _exp_san)
                if (_has_zhi or _has_san) and _matched_zhi and _matched_san:
                    _pm = True
```

**After**(替换为):
```python
            if _pimgs:
                _remark = item.get('remark') or ''
                _qty = item.get('quantity') or ''
                _unit = item.get('unit') or ''
                _total = sum(_eff_zhi(p) for p in _pimgs)
                _loose = sum(p.get('loose_count', 0) for p in _pimgs)
                _exp_zhi, _has_zhi = compute_placement_expected_zhi(_remark, _qty, _unit)
                _san_m = list(SANMA_RE.finditer(_remark))
                _exp_san = sum(float(x.group(1)) for x in _san_m)
                if _exp_san == int(_exp_san):
                    _exp_san = int(_exp_san)
                _has_san = len(_san_m) > 0
                _matched_zhi = (not _has_zhi) or (abs(_total - _exp_zhi) <= 0.01)
                _matched_san = (not _has_san) or (abs(_loose - _exp_san) <= 0.01)
                if (_has_zhi or _has_san) and _matched_zhi and _matched_san:
                    _pm = True
```

### Step 2.2: 迁移 placement_groups 渲染(shipping.py L207-240)

**Before**(L215-240):
```python
            loose_total = sum(p.get('loose_count', 0) for p in pimgs)
            remark = rec.get('remark') or ''
            # 备注中分别解析「支」(N支之和)与「散码」(Ny之和,可多个累加),
            # 与清点结果分开比较。例:「1支+2y+3y」→ 支=1, 散码=5
            zhi_m = list(re.finditer(r'(\d+)\s*支', remark))
            expected_zhi = sum(int(x.group(1)) for x in zhi_m)
            has_zhi = len(zhi_m) > 0
            san_m = list(SANMA_RE.finditer(remark))
            expected_sanma = sum(float(x.group(1)) for x in san_m)
            if expected_sanma == int(expected_sanma):
                expected_sanma = int(expected_sanma)
            has_sanma = len(san_m) > 0
            current['items'].append({
                'record_id': rec['id'],
                'specification': rec.get('specification') or '',
                'quantity': rec.get('quantity') or '',
                'unit': rec.get('unit') or '',
                'total': sum(_eff_zhi(p) for p in pimgs),
                'loose_total': loose_total,
                'remark': remark,
                'expected_zhi': expected_zhi,
                'has_zhi': has_zhi,
                'expected_sanma': expected_sanma,
                'has_sanma': has_sanma,
                'images': pimgs,
            })
```

**After**:
```python
            loose_total = sum(p.get('loose_count', 0) for p in pimgs)
            remark = rec.get('remark') or ''
            # 2026-09-03:placement 期望值兜底 — unit='支' + 备注无支数 → 用 quantity
            expected_zhi, has_zhi = compute_placement_expected_zhi(
                remark, rec.get('quantity') or '', rec.get('unit') or ''
            )
            san_m = list(SANMA_RE.finditer(remark))
            expected_sanma = sum(float(x.group(1)) for x in san_m)
            if expected_sanma == int(expected_sanma):
                expected_sanma = int(expected_sanma)
            has_sanma = len(san_m) > 0
            current['items'].append({
                'record_id': rec['id'],
                'specification': rec.get('specification') or '',
                'quantity': rec.get('quantity') or '',
                'unit': rec.get('unit') or '',
                'total': sum(_eff_zhi(p) for p in pimgs),
                'loose_total': loose_total,
                'remark': remark,
                'expected_zhi': expected_zhi,
                'has_zhi': has_zhi,
                'expected_sanma': expected_sanma,
                'has_sanma': has_sanma,
                'images': pimgs,
            })
```

### Step 2.3: 迁移 `_placement_match_for_image`(shipping.py L1317-1347)

**Before**(L1333-1347):
```python
    rec = ShippingRecord.get_by_id(record_pk) or {}
    remark = rec.get('remark') or ''
    total = sum((((p.get('manual_count') if p.get('manual_count') is not None else (p.get('n_marks') or 0)))
                 * (-1 if p.get('is_unload') else 1)) for p in pimgs)
    loose = sum(p.get('loose_count', 0) for p in pimgs)
    zhi_m = list(re.finditer(r'(\d+)\s*支', remark))
    exp_zhi = sum(int(x.group(1)) for x in zhi_m)
    has_zhi = len(zhi_m) > 0
    san_m = list(SANMA_RE.finditer(remark))
    exp_san = sum(float(x.group(1)) for x in san_m)
    if exp_san == int(exp_san):
        exp_san = int(exp_san)
    has_san = len(san_m) > 0
    matched_zhi = (not has_zhi) or (total == exp_zhi)
    matched_san = (not has_san) or (loose == exp_san)
    return bool((has_zhi or has_san) and matched_zhi and matched_san), record_pk
```

**After**:
```python
    rec = ShippingRecord.get_by_id(record_pk) or {}
    remark = rec.get('remark') or ''
    total = sum((((p.get('manual_count') if p.get('manual_count') is not None else (p.get('n_marks') or 0)))
                 * (-1 if p.get('is_unload') else 1)) for p in pimgs)
    loose = sum(p.get('loose_count', 0) for p in pimgs)
    # 2026-09-03:placement 期望值兜底 — unit='支' + 备注无支数 → 用 quantity
    exp_zhi, has_zhi = compute_placement_expected_zhi(
        remark, rec.get('quantity') or '', rec.get('unit') or ''
    )
    san_m = list(SANMA_RE.finditer(remark))
    exp_san = sum(float(x.group(1)) for x in san_m)
    if exp_san == int(exp_san):
        exp_san = int(exp_san)
    has_san = len(san_m) > 0
    matched_zhi = (not has_zhi) or (abs(total - exp_zhi) <= 0.01)
    matched_san = (not has_san) or (abs(loose - exp_san) <= 0.01)
    return bool((has_zhi or has_san) and matched_zhi and matched_san), record_pk
```

### Step 2.4: 添加 import

确认 `blueprints/shipping.py` 顶部 `from blueprints._helpers import ...` 已包含 `compute_placement_expected_zhi`(若没有就追加)。

### Step 2.5: 跑测试

```bash
python -X utf8 -m unittest tests.test_placement_zhi_fallback -v
python -m pytest tests/ -v
```

**Expected: PASS**

### Step 2.6: 提交

```bash
git add blueprints/shipping.py
git commit -m "refactor(shipping): placement 期望值改用 compute_placement_expected_zhi 兜底

- 3 处使用点(_pm / placement_groups / _placement_match_for_image)统一改 helper 调用
- matched_zhi 改 abs(diff) <= 0.01 容差,兼容 quantity 含小数
- 删除内联 re.finditer + sum(int) 重复代码"
```

---

## Task 3: 迁移 inbound.py 3 处使用点

**Files:**
- Modify: `blueprints/inbound.py` 3 处:
  - 行 192-211(`_pm` 计算块)
  - 行 221-259(placement_groups 渲染)
  - 行 627-654(`_inbound_placement_match_for_image` 函数)

### Step 3.1: 迁移 `_pm` 计算块(inbound.py L192-211)

**Before**(L193-211):
```python
            _pimgs = placement_by_record.get(item['id']) or []
            _pm = False
            if _pimgs:
                _remark = item.get('remark') or ''
                _total = sum(_eff_zhi(p) for p in _pimgs)
                _loose = sum(p.get('loose_count', 0) for p in _pimgs)
                _zhi_m = list(re.finditer(r'(\d+)\s*吜?', _remark))
                _exp_zhi = sum(int(x.group(1)) for x in _zhi_m)
                _has_zhi = len(_zhi_m) > 0
                _san_m = list(SANMA_RE.finditer(_remark))
                _exp_san = sum(float(x.group(1)) for x in _san_m)
                if _exp_san == int(_exp_san):
                    _exp_san = int(_exp_san)
                _has_san = len(_san_m) > 0
                _matched_zhi = (not _has_zhi) or (_total == _exp_zhi)
                _matched_san = (not _has_san) or (_loose == _exp_san)
                if (_has_zhi or _has_san) and _matched_zhi and _matched_san:
                    _pm = True
            item['placement_match'] = _pm
```

**After**(替换为):
```python
            _pimgs = placement_by_record.get(item['id']) or []
            _pm = False
            if _pimgs:
                _remark = item.get('remark') or ''
                _total = sum(_eff_zhi(p) for p in _pimgs)
                _loose = sum(p.get('loose_count', 0) for p in _pimgs)
                # 2026-09-03:placement 期望值兜底
                _exp_zhi, _has_zhi = compute_placement_expected_zhi(
                    _remark, item.get('quantity') or '', item.get('unit') or ''
                )
                _san_m = list(SANMA_RE.finditer(_remark))
                _exp_san = sum(float(x.group(1)) for x in _san_m)
                if _exp_san == int(_exp_san):
                    _exp_san = int(_exp_san)
                _has_san = len(_san_m) > 0
                _matched_zhi = (not _has_zhi) or (abs(_total - _exp_zhi) <= 0.01)
                _matched_san = (not _has_san) or (abs(_loose - _exp_san) <= 0.01)
                if (_has_zhi or _has_san) and _matched_zhi and _matched_san:
                    _pm = True
            item['placement_match'] = _pm
```

### Step 3.2: 迁移 placement_groups 渲染(inbound.py L221-259)

**Before**(L234-256):
```python
            loose_total = sum(p.get('loose_count', 0) for p in pimgs)
            remark = rec.get('remark') or ''
            zhi_m = list(re.finditer(r'(\d+)\s*吜?', remark))
            expected_zhi = sum(int(x.group(1)) for x in zhi_m)
            has_zhi = len(zhi_m) > 0
            san_m = list(SANMA_RE.finditer(remark))
            expected_sanma = sum(float(x.group(1)) for x in san_m)
            if expected_sanma == int(expected_sanma):
                expected_sanma = int(expected_sanma)
            has_sanma = len(san_m) > 0
            current['items'].append({
                ...
                'expected_zhi': expected_zhi,
                'has_zhi': has_zhi,
                ...
            })
```

**After**:
```python
            loose_total = sum(p.get('loose_count', 0) for p in pimgs)
            remark = rec.get('remark') or ''
            # 2026-09-03:placement 期望值兜底
            expected_zhi, has_zhi = compute_placement_expected_zhi(
                remark, rec.get('quantity') or '', rec.get('unit') or ''
            )
            san_m = list(SANMA_RE.finditer(remark))
            expected_sanma = sum(float(x.group(1)) for x in san_m)
            if expected_sanma == int(expected_sanma):
                expected_sanma = int(expected_sanma)
            has_sanma = len(san_m) > 0
            current['items'].append({
                ...
                'expected_zhi': expected_zhi,
                'has_zhi': has_zhi,
                ...
            })
```

### Step 3.3: 迁移 `_inbound_placement_match_for_image`(inbound.py L627-654)

**Before**(L637-654):
```python
    rec = InboundRecord.get_by_id(record_pk) or {}
    remark = rec.get('remark') or ''
    total = sum((((p.get('manual_count') if p.get('manual_count') is not None else (p.get('n_marks') or 0)))
                 * (-1 if p.get('is_unload') else 1)) for p in pimgs)
    loose = sum(p.get('loose_count', 0) for p in pimgs)
    zhi_m = list(re.finditer(r'(\d+)\s*支', remark))
    exp_zhi = sum(int(x.group(1)) for x in zhi_m)
    has_zhi = len(zhi_m) > 0
    san_m = list(SANMA_RE.finditer(remark))
    exp_san = sum(float(x.group(1)) for x in san_m)
    if exp_san == int(exp_san):
        exp_san = int(exp_san)
    has_san = len(san_m) > 0
    matched_zhi = (not has_zhi) or (total == exp_zhi)
    matched_san = (not has_san) or (loose == exp_san)
    return bool((has_zhi or has_san) and matched_zhi and matched_san), record_pk
```

**After**:
```python
    rec = InboundRecord.get_by_id(record_pk) or {}
    remark = rec.get('remark') or ''
    total = sum((((p.get('manual_count') if p.get('manual_count') is not None else (p.get('n_marks') or 0)))
                 * (-1 if p.get('is_unload') else 1)) for p in pimgs)
    loose = sum(p.get('loose_count', 0) for p in pimgs)
    # 2026-09-03:placement 期望值兜底
    exp_zhi, has_zhi = compute_placement_expected_zhi(
        remark, rec.get('quantity') or '', rec.get('unit') or ''
    )
    san_m = list(SANMA_RE.finditer(remark))
    exp_san = sum(float(x.group(1)) for x in san_m)
    if exp_san == int(exp_san):
        exp_san = int(exp_san)
    has_san = len(san_m) > 0
    matched_zhi = (not has_zhi) or (abs(total - exp_zhi) <= 0.01)
    matched_san = (not has_san) or (abs(loose - exp_san) <= 0.01)
    return bool((has_zhi or has_san) and matched_zhi and matched_san), record_pk
```

### Step 3.4: 添加 import

确认 `blueprints/inbound.py` 顶部已 import `compute_placement_expected_zhi`(若没有就追加)。

### Step 3.5: 跑测试

```bash
python -X utf8 -m unittest tests.test_placement_zhi_fallback -v
python -m pytest tests/ -v
```

**Expected: PASS**

### Step 3.6: 提交

```bash
git add blueprints/inbound.py
git commit -m "refactor(inbound): placement 期望值改用 compute_placement_expected_zhi 兜底"
```

---

## Task 4: 迁移 loading.py 3 处使用点

**Files:**
- Modify: `blueprints/loading.py` 3 处:
  - 行 174-191(`_pm` 计算块)
  - 行 241-260(placement_groups 渲染)
  - 行 1349-1380(`_loading_placement_match_for_image` 函数)

### Step 4.1: 迁移 `_pm` 计算块(loading.py L170-191)

**Before**(L173-191):
```python
            _pimgs = placement_by_record.get(item['id']) or []
            _pm = False
            if _pimgs:
                _remark = item.get('remark') or ''
                _total = sum(_loading_eff_zhi(p) for p in _pimgs)
                _loose = sum(p.get('loose_count', 0) for p in _pimgs)
                _zhi_m = list(re.finditer(r'(\d+)\s*支', _remark))
                _exp_zhi = sum(int(x.group(1)) for x in _zhi_m)
                _has_zhi = len(_zhi_m) > 0
                _san_m = list(SANMA_RE.finditer(_remark))
                _exp_san = sum(float(x.group(1)) for x in _san_m)
                if _exp_san == int(_exp_san):
                    _exp_san = int(_exp_san)
                _has_san = len(_san_m) > 0
                _matched_zhi = (not _has_zhi) or (_total == _exp_zhi)
                _matched_san = (not _has_san) or (_loose == _exp_san)
                if (_has_zhi or _has_san) and _matched_zhi and _matched_san:
                    _pm = True
            item['placement_match'] = _pm
```

**After**:
```python
            _pimgs = placement_by_record.get(item['id']) or []
            _pm = False
            if _pimgs:
                _remark = item.get('remark') or ''
                _total = sum(_loading_eff_zhi(p) for p in _pimgs)
                _loose = sum(p.get('loose_count', 0) for p in _pimgs)
                # 2026-09-03:placement 期望值兜底
                _exp_zhi, _has_zhi = compute_placement_expected_zhi(
                    _remark, item.get('quantity') or '', item.get('unit') or ''
                )
                _san_m = list(SANMA_RE.finditer(_remark))
                _exp_san = sum(float(x.group(1)) for x in _san_m)
                if _exp_san == int(_exp_san):
                    _exp_san = int(_exp_san)
                _has_san = len(_san_m) > 0
                _matched_zhi = (not _has_zhi) or (abs(_total - _exp_zhi) <= 0.01)
                _matched_san = (not _has_san) or (abs(_loose - _exp_san) <= 0.01)
                if (_has_zhi or _has_san) and _matched_zhi and _matched_san:
                    _pm = True
            item['placement_match'] = _pm
```

### Step 4.2: 迁移 placement_groups 渲染(loading.py L228-262)

**Before**(L241-260):
```python
            loose_total = sum(p.get('loose_count', 0) for p in pimgs)
            remark = rec.get('remark') or ''
            zhi_m = list(re.finditer(r'(\d+)\s*支', remark))
            expected_zhi = sum(int(x.group(1)) for x in zhi_m)
            has_zhi = len(zhi_m) > 0
            san_m = list(SANMA_RE.finditer(remark))
            expected_sanma = sum(float(x.group(1)) for x in san_m)
            if expected_sanma == int(expected_sanma):
                expected_sanma = int(expected_sanma)
            has_sanma = len(san_m) > 0
            current['items'].append({
                ...
                'expected_zhi': expected_zhi,
                'has_zhi': has_zhi,
                ...
            })
```

**After**:
```python
            loose_total = sum(p.get('loose_count', 0) for p in pimgs)
            remark = rec.get('remark') or ''
            # 2026-09-03:placement 期望值兜底
            expected_zhi, has_zhi = compute_placement_expected_zhi(
                remark, rec.get('quantity') or '', rec.get('unit') or ''
            )
            san_m = list(SANMA_RE.finditer(remark))
            expected_sanma = sum(float(x.group(1)) for x in san_m)
            if expected_sanma == int(expected_sanma):
                expected_sanma = int(expected_sanma)
            has_sanma = len(san_m) > 0
            current['items'].append({
                ...
                'expected_zhi': expected_zhi,
                'has_zhi': has_zhi,
                ...
            })
```

### Step 4.3: 迁移 `_loading_placement_match_for_image`(loading.py L1349-1380)

**Before**(L1359-1380):
```python
    rec = LoadingOrderRecord.get_by_id(record_pk) or {}
    remark = rec.get('remark') or ''
    total = sum((((p.get('manual_count') if p.get('manual_count') is not None else (p.get('n_marks') or 0)))
                 * (-1 if p.get('is_unload') else 1)) for p in pimgs)
    loose = sum(p.get('loose_count', 0) for p in pimgs)
    zhi_m = list(re.finditer(r'(\d+)\s*支', remark))
    exp_zhi = sum(int(x.group(1)) for x in zhi_m)
    has_zhi = len(zhi_m) > 0
    san_m = list(SANMA_RE.finditer(remark))
    exp_san = sum(float(x.group(1)) for x in san_m)
    if exp_san == int(exp_san):
        exp_san = int(exp_san)
    has_san = len(san_m) > 0
    matched_zhi = (not has_zhi) or (total == exp_zhi)
    matched_san = (not has_san) or (loose == exp_san)
    return bool((has_zhi or has_san) and matched_zhi and matched_san), record_pk
```

**After**:
```python
    rec = LoadingOrderRecord.get_by_id(record_pk) or {}
    remark = rec.get('remark') or ''
    total = sum((((p.get('manual_count') if p.get('manual_count') is not None else (p.get('n_marks') or 0)))
                 * (-1 if p.get('is_unload') else 1)) for p in pimgs)
    loose = sum(p.get('loose_count', 0) for p in pimgs)
    # 2026-09-03:placement 期望值兜底
    exp_zhi, has_zhi = compute_placement_expected_zhi(
        remark, rec.get('quantity') or '', rec.get('unit') or ''
    )
    san_m = list(SANMA_RE.finditer(remark))
    exp_san = sum(float(x.group(1)) for x in san_m)
    if exp_san == int(exp_san):
        exp_san = int(exp_san)
    has_san = len(san_m) > 0
    matched_zhi = (not has_zhi) or (abs(total - exp_zhi) <= 0.01)
    matched_san = (not has_san) or (abs(loose - exp_san) <= 0.01)
    return bool((has_zhi or has_san) and matched_zhi and matched_san), record_pk
```

### Step 4.4: 添加 import

确认 `blueprints/loading.py` 顶部已 import `compute_placement_expected_zhi`(若没有就追加)。

### Step 4.5: 跑测试

```bash
python -X utf8 -m unittest tests.test_placement_zhi_fallback -v
python -m pytest tests/ -v
```

**Expected: PASS**

### Step 4.6: 提交

```bash
git add blueprints/loading.py
git commit -m "refactor(loading): placement 期望值改用 compute_placement_expected_zhi 兜底"
```

---

## Task 5: 模板 expected_zhi 显示格式(避免 `33.0`)

**Files:**
- Modify: `templates/shipping-records.html` — `data-expected-zhi="{{ _item.expected_zhi }}"` → 用 `'%g'` 格式符
- Modify: `templates/inbound-records.html` — 同上
- Modify: `templates/loading-orders.html` — 同上

### Step 5.1: 改 shipping-records.html 模板

找到 `data-expected-zhi="{{ _item.expected_zhi }}"`(约 1-2 处,行 1032 附近)。

**Before**:
```html
data-expected-zhi="{{ _item.expected_zhi }}"
```

**After**:
```html
data-expected-zhi="{{ '%g' % _item.expected_zhi }}"
```

### Step 5.2: 改 inbound-records.html

类似改动(用 Grep 找 `data-expected-zhi=` 定位)

```html
data-expected-zhi="{{ '%g' % _item.expected_zhi }}"
```

### Step 5.3: 改 loading-orders.html

类似改动。

### Step 5.4: 启动 dev server 验证

```bash
# PowerShell
$env:PYTHONUTF8=1
python app.py
# 浏览器访问 http://localhost:5050/shipping-records
```

**Expected**:
- 现有订单(unit='支', quantity='33', remark='33支') → 期望显示「33 支」(不变)
- 新订单(unit='支', quantity='33', remark='') → 期望显示「33 支」(原本显示「备注无支数」,现在显示「已齐 33 支」)
- 整数 33 不会显示成「33.0」

### Step 5.5: 提交

```bash
git add templates/shipping-records.html templates/inbound-records.html templates/loading-orders.html
git commit -m "fix(templates): expected_zhi 用 '%g' 格式符,整数去小数点显示"
```

---

## Task 6: 全栈烟雾测试 + 提交最终验证

### Step 6.1: 跑完整测试套件

```bash
python -X utf8 -m unittest tests.test_placement_zhi_fallback -v
python -m pytest tests/ -v
```

**Expected: ALL PASS**

### Step 6.2: 手动浏览器验证

启动 server + 浏览器:
1. 访问 `/shipping-records`,打开一条 unit='支' 数量 33 的明细
2. 上传一张 placement 摆放图
3. 点 33 下点击计数点 → 「点数」按钮变绿
4. 改点 30 下 → 「点数」按钮变红/不变绿
5. 浏览器 console 无报错

### Step 6.3: 移动端验证

访问 `/m/shipping-today/order/<oid>/placement/<record_id>`(同一条订单):
- 同样点 33 下 → 变绿
- 点 30 下 → 不变绿

### Step 6.4: 入库/装柜同上验证

访问 `/inbound-records` 和 `/loading-orders`,重复 Step 6.2 的流程(若有 unit='支' 数据;若无,临时新建一条用于验证)。

### Step 6.5: 最终提交

如果 Step 6.1-6.4 全过,本任务无新代码 commit(所有 commit 已在 Task 1-5 完成)。如有调整,按需补 commit。

### Step 6.6: 推送(可选)

```bash
# 按用户记忆 [[github-needs-vpn]] 操作:开梯子 → push
git push origin main
```

---

## 验证清单(完成时打勾)

- [ ] Task 1: helper 函数 + 13 条 unittest 通过
- [ ] Task 2: shipping.py 3 处替换,测试通过
- [ ] Task 3: inbound.py 3 处替换,测试通过
- [ ] Task 4: loading.py 3 处替换,测试通过
- [ ] Task 5: 模板 `'%g'` 格式符生效,整数显示 `33` 不显示 `33.0`
- [ ] Task 6: 浏览器手动验证 三套订单 + 移动端 点数正常
- [ ] 全套 pytest 通过(84+ 现有 + 13 新 = 97+)

---

## 回滚策略

每 Task 独立 commit,回滚 `git revert <commit-hash>` 即可。

按任务顺序回滚:
1. 模板(`'%g'` → `{{ _item.expected_zhi }}`)
2. loading / inbound / shipping(任一蓝图 revert,不影响其他蓝图)
3. helper 函数 revert(同时 8 处调用变回内联)

---

## 参考

- spec: `docs/superpowers/specs/2026-09-03-placement-zhi-fallback-from-quantity-design.md`
- 现有测试模式参考: `tests/test_basic.py:CalcHintTests` / `CheckRemarkTests` / `SummarizeRemarksTests`
- 共享层抽象参考: `blueprints/ocr_pipeline.py:RecordImageProcessor`(三套订单共用)
- Jinja `'%g'` 格式符文档: Python `printf-style String Formatting`,整数去小数点(33 → "33"),小数保留(33.5 → "33.5")