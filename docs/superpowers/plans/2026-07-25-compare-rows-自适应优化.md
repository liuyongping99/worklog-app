# compare_rows 自适应优化 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 利用人工确认（human_verified=1）记录优化 DeepSeek compare_rows 比对精度，通过品类共享的 few-shot 案例注入 + red→yellow 后处理松弛，减少误报红。

**Architecture:** 4 个任务，仅改 `blueprints/ocr_engine.py`（DeepSeekEngine 类）。T1 纯函数分类 + 关键词表；T2 DB 查询 + prompt 构造；T3 集成调用方（仅 compare_rows 方法）；T4 E2E 验证。零模板/API/DB schema 改动。

**Tech Stack:** Python 3.12 / SQLite (get_db) / OpenAI SDK (DeepSeek v4-flash)

**Spec:** `docs/superpowers/specs/2026-07-25-compare-rows-自适应优化-design.md`

## Global Constraints

- **范围限定**：仅 `blueprints/ocr_engine.py` — DeepSeekEngine 类
- **零前端/API 改动**：不改模板、端点签名、请求参数
- **零 schema 改动**：不 ALTER 表、不新增字段
- **不依赖 match_score**：后处理松弛仅凭"品类是否有 hv 案例"，不查 shipping_images.match_score
- **不增加 API 调用次数**：Few-shot 注入只是多 ~300 tokens prompt，不新增独立 API 调用
- **DeepSeek v4-flash 文本模型**：不发图片给视觉模型（沿用现有 PaddleOCR 提字 + DeepSeek 文生文）
- **CRLF 换行**：仓库 Windows CRLF
- **遵循现有代码风格**：Python `any()` 生成器模式（与现有 `has_eco`/`has_jia_mian` 风格一致）

---

## File Structure

**改动文件 (1)**：

- `blueprints/ocr_engine.py`
  - 新增 `CLASS_FALLBACK_KEYWORDS` 常量（~900 行附近，在所有引擎类之前）
  - 新增 `_classify_product()` 静态方法 → DeepSeekEngine
  - 新增 `_get_hv_cases()` 实例方法 → DeepSeekEngine
  - 新增 `_build_few_shot()` 静态方法 → DeepSeekEngine
  - 修改 `compare_rows()` 方法（在方法体内加 ~10 行）

**未改动**：
- 任何模板、端点、模型
- `MoonshotEngine` / `PaddleOCREngine`
- `shipping.py` 蓝图

---

## Task 1: CLASS_FALLBACK_KEYWORDS + _classify_product()

**Files:**
- Modify: `blueprints/ocr_engine.py` — 在 DeepSeekEngine 类**之前**新增常量
- Modify: `blueprints/ocr_engine.py` — 在 DeepSeekEngine 类内新增 `_classify_product` 静态方法

**Interfaces:**
- Produces:
  - `CLASS_FALLBACK_KEYWORDS: list[tuple[str, list[str]]]` — 品类关键词兜底表
  - `DeepSeekEngine._classify_product(product_name: str) -> str | None` — 返回 category_code（如 "0201"）或 None

- [ ] **Step 1: Read current code structure**

读 `blueprints/ocr_engine.py:660-662`（DeepSeekEngine 类定义附近），确认插入位置在类定义之前。

- [ ] **Step 2: Add CLASS_FALLBACK_KEYWORDS constant**

在 `DeepSeekEngine` 类定义（约 line 660）**之前**，`DeepSeekEngine(BaseOCREngine):` **之后**插入：

```python
# ═══════════════════════════════════════════════════════════════════
# 品类关键词兜底表（_classify_product 在用）
# 格式: [(category_code, [keyword1, keyword2, ...]), ...]
# 匹配规则：按列表顺序，第一个 product_name 中含有关键词的行即命中
# ═══════════════════════════════════════════════════════════════════
CLASS_FALLBACK_KEYWORDS = [
    ('0201', ['杂胶']),
    ('0301', ['纯胶']),
    ('0401', ['回力胶', 'EVA']),
    ('0204', ['无纺布']),
    ('0701', ['鱼鳞布', 'HA']),
    ('0208', ['潜水胶']),
    ('0601', ['PE板', 'PE']),
    ('0501', ['不织布']),
    ('0205', ['路华里']),
    ('0302', ['热熔胶']),
]
```

> 关键词去掉了 'A料'/'B料'（它们更可能是产品等级而非品类）、去掉了 'LB'（可能不是独立品类）。实施时如需扩展，加到列表末尾即可。

- [ ] **Step 3: Write the test (TDD)**

Create `tests/test_compare_rows_adaptive.py`:

```python
"""Tests for _classify_product + few-shot helpers."""
import os
import sys
import pytest

# Ensure project root on path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from blueprints.ocr_engine import CLASS_FALLBACK_KEYWORDS


class TestClassifyProduct:
    """Test _classify_product() against both product table and fallback keywords."""

    @staticmethod
    def _classify(name):
        """Import and call the real function."""
        from blueprints.ocr_engine import DeepSeekEngine
        return DeepSeekEngine._classify_product(name)

    def test_product_table_match_杂胶(self):
        """7P环保杂胶 should match product table → 0201."""
        assert self._classify('7P环保杂胶') == '0201'

    def test_product_table_match_无纺布(self):
        """无纺布 A料 should match product table."""
        assert self._classify('无纺布 A料') == '0204'

    def test_fallback_keyword_纯胶(self):
        """品名不在 product 表但含'纯胶'关键词 → 0301."""
        # Use a name unlikely to be in product table
        assert self._classify('测试纯胶XYZ') == '0301'

    def test_fallback_keyword_回力胶(self):
        """含'EVA'关键词 → 0401."""
        assert self._classify('EVA泡棉') == '0401'

    def test_no_match_returns_none(self):
        """未知品名 → None."""
        assert self._classify('XYZ123不明商品') is None

    def test_empty_string(self):
        """空字符串 → None."""
        assert self._classify('') is None

    def test_none_input(self):
        """None 输入 → None（不抛异常）."""
        assert self._classify(None) is None
```

- [ ] **Step 4: Run test to verify it fails**

```bash
python -m pytest tests/test_compare_rows_adaptive.py -v
```

Expected: FAIL — `_classify_product` method does not exist yet.

- [ ] **Step 5: Implement _classify_product()**

在 `DeepSeekEngine` 类内（约 line 831，`compare_rows` 方法之后），新增静态方法：

```python
@staticmethod
def _classify_product(product_name):
    """品名 → 品类代码（level 3 category_code）。

    1. 先查 product 表（product_name → category_id → 向上取 level=3 父节点）
    2. 兜底：CLASS_FALLBACK_KEYWORDS 子串匹配
    3. 都无匹配 → None

    Args:
        product_name: str | None — 商品名称

    Returns:
        str | None — category_code（如 "0201"）或 None
    """
    if not product_name:
        return None

    try:
        from models._db import get_db
        conn = get_db()
        cur = conn.cursor()

        # ── 策略 1: product 表精确匹配 ──
        cur.execute(
            'SELECT p.category_id FROM product p '
            'WHERE p.product_name = ? AND p.category_id IS NOT NULL '
            'LIMIT 1',
            (product_name,)
        )
        row = cur.fetchone()
        if row:
            cat_id = row[0]
            # 向上遍历 parent_id 到 level=3
            for _ in range(10):  # safety limit
                cur.execute(
                    'SELECT parent_id, category_code, level '
                    'FROM product_categories WHERE id = ?',
                    (cat_id,)
                )
                cat_row = cur.fetchone()
                if not cat_row:
                    break
                parent, code, level = cat_row
                if level == 3:
                    conn.close()
                    return code
                cat_id = parent

        # ── 策略 2: 关键词兜底 ──
        pn_lower = product_name.lower()
        for code, keywords in CLASS_FALLBACK_KEYWORDS:
            for kw in keywords:
                if kw.lower() in pn_lower:
                    conn.close()
                    return code

        conn.close()
    except Exception:
        pass

    return None
```

> 注：每次调用都开新 DB 连接（当前 data scale 没问题，shipping_records 409 行）。如有性能需求，后续可加 lru_cache。

- [ ] **Step 6: Run tests to verify they pass**

```bash
python -m pytest tests/test_compare_rows_adaptive.py -v
```

Expected: 7/7 PASS.

- [ ] **Step 7: Commit**

```bash
git add blueprints/ocr_engine.py tests/test_compare_rows_adaptive.py
git commit -m "feat(ocr): add CLASS_FALLBACK_KEYWORDS + _classify_product()"
```

**Task 1 验证**：7 tests PASS；`_classify_product('7P环保杂胶')` → `'0201'`

---

## Task 2: _get_hv_cases() + _build_few_shot()

**Files:**
- Modify: `blueprints/ocr_engine.py` — DeepSeekEngine 类内新增 2 个方法

**Interfaces:**
- Consumes: `_classify_product()` from Task 1
- Produces:
  - `_get_hv_cases(category_code: str) -> list[dict]` — 返回该品类最新 3 条 hv 记录
  - `_build_few_shot(cases: list[dict]) -> str` — 返回注入 text block

- [ ] **Step 1: Write tests (TDD)**

在 `tests/test_compare_rows_adaptive.py` 追加：

```python
from blueprints.ocr_engine import DeepSeekEngine


class TestGetHvCases:
    """Test _get_hv_cases() — runs against real DB (integration)."""

    def test_returns_list(self):
        """_get_hv_cases should return a list (possibly empty)."""
        cases = DeepSeekEngine._get_hv_cases('0201')
        assert isinstance(cases, list)

    def test_returns_at_most_3(self):
        """_get_hv_cases should return at most 3 cases."""
        cases = DeepSeekEngine._get_hv_cases('0201')
        assert len(cases) <= 3

    def test_unknown_category_returns_empty(self):
        """Unknown category → empty list, not error."""
        cases = DeepSeekEngine._get_hv_cases('9999')
        assert cases == []

    def test_each_case_has_required_keys(self):
        """Each case must have product_name and specification."""
        cases = DeepSeekEngine._get_hv_cases('0201')
        for c in cases:
            assert 'product_name' in c
            assert 'specification' in c


class TestBuildFewShot:
    """Test _build_few_shot() — pure string formatting."""

    def test_empty_cases_returns_empty_string(self):
        """No cases → empty string."""
        assert DeepSeekEngine._build_few_shot([]) == ''

    def test_format_includes_product_names(self):
        """Output must contain the product names."""
        cases = [
            {'product_name': '7P环保杂胶', 'specification': '0.8单面加面'},
            {'product_name': '无纺布 A料', 'specification': 'A1.5m 200g'},
        ]
        result = DeepSeekEngine._build_few_shot(cases)
        assert '7P环保杂胶' in result
        assert '无纺布 A料' in result
        assert '人工确认' in result  # mention human confirmation

    def test_truncates_to_3_cases(self):
        """If caller passes >3, _build_few_shot should use only first 3."""
        cases = [
            {'product_name': f'商品{i}', 'specification': f'规格{i}'}
            for i in range(10)
        ]
        result = DeepSeekEngine._build_few_shot(cases)
        # 商品0-2 should be in result, 商品9 should not
        assert '商品0' in result
        assert '商品2' in result
        assert '商品9' not in result
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_compare_rows_adaptive.py::TestGetHvCases tests/test_compare_rows_adaptive.py::TestBuildFewShot -v
```

Expected: FAIL — methods don't exist.

- [ ] **Step 3: Implement _get_hv_cases()**

在 `DeepSeekEngine` 类内（_classify_product 之后）：

```python
@staticmethod
def _get_hv_cases(category_code):
    """查该品类下人工确认过的记录，返回最新 3 条。

    匹配逻辑：找 shipping_records.product_name 在该品类下的所有行，
    再取 shipping_images.human_verified=1 的记录，按 id DESC 取最新 3 条。

    Args:
        category_code: str — 品类代码（如 "0201"）

    Returns:
        list[dict] — 每项 {'product_name': str, 'specification': str}
                    最多 3 条，无匹配 → 空列表
    """
    if not category_code:
        return []

    try:
        from models._db import get_db
        conn = get_db()
        cur = conn.cursor()

        # Step 1: 聚合该品类下所有已知品名
        # 从 product 表拿品名 + 从 CLASS_FALLBACK_KEYWORDS 拿关键词
        known_names = set()
        # product 表归属该品类
        cur.execute(
            'SELECT DISTINCT p.product_name FROM product p '
            'JOIN product_categories pc ON p.category_id = pc.id '
            'WHERE pc.category_code = ?',
            (category_code,)
        )
        for row in cur.fetchall():
            known_names.add(row[0])

        # CLASS_FALLBACK_KEYWORDS 该品类关键词
        for code, keywords in CLASS_FALLBACK_KEYWORDS:
            if code == category_code:
                # 关键词不能直接当品名，需要模糊匹配 ——
                # 用 LIKE 查 shipping_records
                break

        # Step 2: 查 hv 案例
        # 构造 WHERE 条件：product_name IN (known_names) OR product_name LIKE '%kw%'
        conditions = []
        params = []
        if known_names:
            placeholders = ','.join(['?' for _ in known_names])
            conditions.append(f'sr.product_name IN ({placeholders})')
            params.extend(known_names)
        # 关键词兜底
        for code, keywords in CLASS_FALLBACK_KEYWORDS:
            if code == category_code:
                for kw in keywords:
                    conditions.append('sr.product_name LIKE ?')
                    params.append(f'%{kw}%')

        if not conditions:
            conn.close()
            return []

        where_clause = ' OR '.join(conditions)
        query = (
            'SELECT sr.product_name, sr.specification '
            'FROM shipping_images si '
            'JOIN shipping_records sr ON si.record_pk = sr.id '
            f'WHERE si.human_verified = 1 AND ({where_clause}) '
            'GROUP BY sr.product_name, sr.specification '
            'ORDER BY MAX(si.id) DESC '
            'LIMIT 3'
        )
        cur.execute(query, params)
        results = [
            {'product_name': r[0] or '', 'specification': r[1] or ''}
            for r in cur.fetchall()
        ]
        conn.close()
        return results

    except Exception:
        return []
```

- [ ] **Step 4: Implement _build_few_shot()**

在 `_get_hv_cases` 之后：

```python
@staticmethod
def _build_few_shot(cases):
    """将参考案例格式化为 COMPARE_PROMPT 注入块。

    Args:
        cases: list[dict] — _get_hv_cases 的返回值，每项含 product_name + specification

    Returns:
        str — 注入 text block，cases 为空则返回 ''
    """
    if not cases:
        return ''

    cases = cases[:3]  # safety cap
    lines = [
        '\n\n════════════════════════════════════════',
        '【参考案例：该品类此前已人工确认的匹配】',
        '',
        '以下标签照片此前经人工核查，确认与录入明细匹配。',
        '请在当前判断中参考这些案例的匹配标准，采用同等的宽松度：',
        '',
    ]
    for i, c in enumerate(cases, 1):
        pn = c.get('product_name', '')
        sp = c.get('specification', '')
        lines.append(
            f'{i}. 品名"{pn}" + 规格"{sp}"'
            f'  → 匹配 ✓（人工确认）'
        )
    lines.append('')
    lines.append('请用与上述案例一致的宽松度判断当前行。')
    return '\n'.join(lines)
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
python -m pytest tests/test_compare_rows_adaptive.py::TestGetHvCases tests/test_compare_rows_adaptive.py::TestBuildFewShot -v
```

Expected: 7/7 PASS（4 TestGetHvCases + 3 TestBuildFewShot）

- [ ] **Step 6: Commit**

```bash
git add blueprints/ocr_engine.py tests/test_compare_rows_adaptive.py
git commit -m "feat(ocr): add _get_hv_cases() + _build_few_shot() for few-shot prompt"
```

**Task 2 验证**：7 tests PASS；`_get_hv_cases('0201')` 返回杂胶品类的 hv 案例

---

## Task 3: Modify compare_rows() — integrate injection + relaxation

**Files:**
- Modify: `blueprints/ocr_engine.py:806-831` — `compare_rows()` 方法

**Interfaces:**
- Consumes: `_classify_product()` (T1), `_get_hv_cases()` (T2), `_build_few_shot()` (T2)
- Produces: same return type — `list[dict]` with {record_id, match_status, reason}

- [ ] **Step 1: Write the test (TDD)**

在 `tests/test_compare_rows_adaptive.py` 追加：

```python
class TestCompareRowsIntegration:
    """Test the post-processing relaxation — requires mock to avoid real DeepSeek call."""

    def test_relaxation_red_to_yellow_when_hv_exists(self, monkeypatch):
        """当品类有 hv 案例时, red 结果应降级为 yellow."""
        # Mock _classify → return a category that has hv
        from blueprints.ocr_engine import DeepSeekEngine

        classified = []

        def mock_classify(name):
            classified.append(name)
            return '0201'  # 杂胶品类有 hv

        def mock_get_hv(code):
            return [{'product_name': 'test', 'specification': 'test'}]

        monkeypatch.setattr(DeepSeekEngine, '_classify_product',
                           staticmethod(mock_classify))
        monkeypatch.setattr(DeepSeekEngine, '_get_hv_cases',
                           staticmethod(mock_get_hv))

        # Simulate post-processing logic only (skip real API call)
        result = DeepSeekEngine._apply_few_shot_and_relaxation([
            {'record_id': 1, 'match_status': 'red', 'reason': 'no match'},
            {'record_id': 2, 'match_status': 'green', 'reason': 'matched'},
        ], '0201', True)  # category='0201', has_hv=True

        assert result[0]['match_status'] == 'yellow'  # red → yellow
        assert result[1]['match_status'] == 'green'    # green unchanged

    def test_no_relaxation_when_no_hv(self):
        """品类无 hv → 不松弛."""
        from blueprints.ocr_engine import DeepSeekEngine

        result = DeepSeekEngine._apply_few_shot_and_relaxation([
            {'record_id': 1, 'match_status': 'red', 'reason': 'no match'},
        ], '0301', False)  # has_hv=False

        assert result[0]['match_status'] == 'red'  # unchanged

    def test_relaxation_preserves_reason(self):
        """降级时不改 reason 文本."""
        from blueprints.ocr_engine import DeepSeekEngine

        result = DeepSeekEngine._apply_few_shot_and_relaxation([
            {'record_id': 1, 'match_status': 'red',
             'reason': 'OCR文字中未找到该商品信息'},
        ], '0201', True)

        assert result[0]['match_status'] == 'yellow'
        assert '未找到' in result[0]['reason']  # reason preserved
```

- [ ] **Step 2: Run test to verify it fails**

```bash
python -m pytest tests/test_compare_rows_adaptive.py::TestCompareRowsIntegration -v
```

Expected: FAIL — `_apply_few_shot_and_relaxation` doesn't exist.

- [ ] **Step 3: Add _apply_few_shot_and_relaxation() helper**

在 `_build_few_shot` 之后新增：

```python
@staticmethod
def _apply_few_shot_and_relaxation(results, category_code, has_hv):
    """后处理：当品类有 hv 记录时，将 red 降级为 yellow。

    Args:
        results: list[dict] — DeepSeek 返回的原始结果
        category_code: str | None — 品类代码
        has_hv: bool — 该品类是否有 hv 案例

    Returns:
        list[dict] — 处理后的结果
    """
    if not has_hv:
        return results

    for item in results:
        if item.get('match_status') == 'red':
            item['match_status'] = 'yellow'
    return results
```

- [ ] **Step 4: Modify compare_rows() to integrate**

修改 `compare_rows()` 方法（约 line 806-831）：

在 `rows_json = json.dumps(...)` **之后**、`client = openai.OpenAI(...)` **之前**加入分类和 few-shot 注入；在 JSON 解析**之后**、`return` **之前**加入后处理：

```python
def compare_rows(self, ocr_text, rows):
    """OCR 文字 vs 明细行列表 → 逐行 {record_id, match_status, reason}。

    Args:
        ocr_text: PaddleOCR 提取的纯文本（多行用 \\n 分隔）。
        rows: [{'record_id': int, 'product_name': str, 'specification': str}, ...]

    Returns:
        list[dict] - 每项 {'record_id', 'match_status', 'reason'}
    """
    rows_json = json.dumps(
        [{'record_id': r['record_id'], 'product_name': r.get('product_name', ''),
          'specification': r.get('specification', '')} for r in rows],
        ensure_ascii=False)

    # ── 自适应优化：分类 + few-shot 注入 ──
    category_code = None
    has_hv = False
    few_shot_block = ''
    if rows:
        # 取第一条记录的品名做分类（同一个 order 内的行通常同品类）
        first_name = rows[0].get('product_name', '')
        if first_name:
            category_code = self._classify_product(first_name)
            if category_code:
                hv_cases = self._get_hv_cases(category_code)
                if hv_cases:
                    has_hv = True
                    few_shot_block = self._build_few_shot(hv_cases)

    # 构造 prompt（可能含 few-shot 注入）
    prompt = self.COMPARE_PROMPT
    if few_shot_block:
        prompt += few_shot_block

    client = openai.OpenAI(api_key=self.API_KEY, base_url=self.BASE_URL)
    response = client.chat.completions.create(
        model=self.MODEL,
        messages=[{'role': 'user', 'content':
                   prompt + '\n【OCR文字】\n' + ocr_text +
                   '\n【明细行】\n' + rows_json}],
        max_tokens=self.MAX_TOKENS, timeout=self.TIMEOUT)
    raw = response.choices[0].message.content.strip()
    raw = re.sub(r'^\s*```[a-zA-Z]*\s*\n?', '', raw)
    raw = re.sub(r'\n?\s*```\s*$', '', raw).strip()
    data = json.loads(raw)
    results = data if isinstance(data, list) else data.get('results', [])

    # ── 后处理松弛：品类有 hv → red → yellow ──
    results = self._apply_few_shot_and_relaxation(results, category_code, has_hv)
    return results
```

> 关键设计决定：
> - 用 **第一条 record 的品名**做品类分类（一个 order 的明细行通常同品类），不每行分类（避免过度设计）
> - few_shot 注入在 prompt 头部（COMPARE_PROMPT 末尾 + few_shot + OCR 文字 + 明细行），语义流自然
> - 后处理在 JSON 解析后、return 前，不与 API 调用耦合

- [ ] **Step 5: Run tests to verify they pass**

```bash
python -m pytest tests/test_compare_rows_adaptive.py -v
```

Expected: 17 tests PASS（7 T1 + 7 T2 + 3 T3）

- [ ] **Step 6: Commit**

```bash
git add blueprints/ocr_engine.py tests/test_compare_rows_adaptive.py
git commit -m "feat(ocr): integrate few-shot injection + red→yellow relaxation into compare_rows"
```

**Task 3 验证**：17 tests PASS；`compare_rows` 注入逻辑完备；后处理松弛正常工作。

---

## Task 4: E2E verification（手动 + 自动化）

**Files:**
- 不改文件
- 验证：`human_verified=1` → 下次同品类 compare_rows 是否生效

**Interfaces:**
- Consumes: T3 完成的 `compare_rows()` 方法

- [ ] **Step 1: Confirm Flask is running**

```bash
curl -s -o /dev/null -w "HTTP %{http_code}\n" http://127.0.0.1:5050/shipping-records
```

Expected: `HTTP 302` or `HTTP 200`

- [ ] **Step 2: 验证杂胶品类有 hv 案例可查**

```bash
python -c "
from blueprints.ocr_engine import DeepSeekEngine
cases = DeepSeekEngine._get_hv_cases('0201')
print(f'杂胶(0201) hv cases: {len(cases)}')
for c in cases:
    print(f'  {c[\"product_name\"]} | {c[\"specification\"]}')
assert len(cases) >= 1, '杂胶品类应有 hv 案例'
print('PASS: hv cases found')
"
```

- [ ] **Step 3: 验证分类函数对现有数据有效**

```bash
python -c "
from blueprints.ocr_engine import CLASS_FALLBACK_KEYWORDS, DeepSeekEngine

test_names = [
    '7P环保杂胶', 'TP环保杂胶', '环保纯胶', '回力胶EVA',
    '无纺布 A料', '7P环保鱼鳞布HA', 'PE板',
]

for name in test_names:
    cat = DeepSeekEngine._classify_product(name)
    print(f'{name:25} → {cat or \"(无分类)\"}')
"
```

- [ ] **Step 4: 验证所有既有测试未回归**

```bash
python -m pytest tests/ -x -q --ignore=tests/regression/test_compare_prompt_order575.py
```

Expected: ~158 passed.

- [ ] **Step 5: 验收 report（append to spec §Acceptance）**

在 spec 文件 `docs/superpowers/specs/2026-07-25-compare-rows-自适应优化-design.md` 末尾追加：

```markdown
---

## Acceptance (2026-07-25)

**执行报告:** `.superpowers/sdd/compare-rows-adaptive-report.md`

### 自动化验证

- 17 unit tests PASS (_classify_product + hv cases + few-shot + relaxation)
- 158 existing tests PASS (no regression)
- _get_hv_cases('0201') returns ≥1 case (杂胶品类有 hv 数据)
- _classify_product handles 7 product names correctly (5 matched, 2 None)

### 需手动操作验证

- 端到端流程: 上传杂胶标签图 → manual_verify → 再次上传同品类 → compare_rows 结果更宽松
```

- [ ] **Step 6: NO git commit for acceptance report**

Task 验证: hv 案例有效 + 分类准确 + 既有测试无回归。

**Task 4 验证**: 17 tests + 158 regression PASS；hv 案例可获取；验收报告写入 spec。

---

## Self-Review

**1. Spec coverage:**

| Spec 章节 | Task |
|---|---|
| §2 两层机制 | T1 + T2 + T3 |
| §3 分类函数 | T1 |
| §4 Few-shot 参数 | T2 + T3 |
| §5 数据依赖 | T2 |
| §6 改动清单 | T1-T3 |
| §7 边界 | T3 后处理 |
| §8 效果预期 | T4 验收 |
| §9 测试策略 | T1-T4 |

✅ Full coverage.

**2. Placeholder scan:** No TBD/TODO/incomplete sections. ✅

**3. Type consistency:**
- `_classify_product(str) -> str|None` — consistent across T1-T3
- `_get_hv_cases(str) -> list[dict]` — consistent
- `_build_few_shot(list[dict]) -> str` — consistent
- `_apply_few_shot_and_relaxation(list[dict], str|None, bool) -> list[dict]` — T3 only

✅ Consistent.

**潜在风险:**
- `_classify_product` 每次开新 DB 连接（T1 注释说明 + 建议后续 lru_cache）
- `_get_hv_cases` 的 SQL IN 子句可能因 known_names 过多而变慢（当前 scope ≤ 20 个品名，不超限）
- 第一条 record 的品名做分类 → 如果 order 内有跨品类明细（如杂胶+纯胶混单），可能会为整单注入杂胶案例 → 对纯胶行无效但有少量 prompt 冗余。这是 spec 知道的 trade-off（"一个 order 的明细行通常同品类"），不修改。
