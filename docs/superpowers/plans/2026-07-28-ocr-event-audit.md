# OCR 事件审计页 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 `/audit/ocr-events` 页面让用户按 `prompt_version` 维度审视 AI 比对准确率 — 主表聚合一致率 + 同页钻入 record 配对明细 + 过滤 + CSV 导出。

**Architecture:** 新模型 `models/audit_query.py::OcrEventAudit`(3 个只读查询方法)+ 新蓝图 `blueprints/audit.py`(1 页面路由 + 3 个 JSON/CSV API)+ 1 Jinja2 模板 + Tailwind 配色。零迁移、零新依赖。

**Tech Stack:** Python 3.12 + SQLite 3.45.3(窗口函数 `ROW_NUMBER() OVER (...)`)+ Flask 3.1.3 + pytest 9.1。沿用现有 base.html 风格 + Tailwind + vanilla JS。

## Global Constraints

1. **回退路径**:任何 db 破坏性操作前必须 `cp worklog.db D:/BAK/worklog_$(date +%Y%m%d_%H%M).db`(项目 CLAUDE.md 硬规则 6)。本 plan 无新表/无新列,无 db 破坏性操作,但全量回归前仍建议备份。
2. **CRLF**:编辑文件注意 CRLF 换行符,SQL 多行字符串避免替换错位。
3. **Unicode safe**:执行 Python 脚本时设 `PYTHONUTF8=1`。
4. **不引入新依赖**:不装新包;`from ._db import get_db` 走项目已有基础设施。
5. **TDD**:每个 task 严格"写失败测试 → 验证 → 实现 → 验证通过 → commit"。
6. **commit 粒度**:每个 task 独立 commit,message 按 `feat(audit): ...` / `feat(model): ...` / `docs(plan): ...` 风格。
7. **append-only**:本 plan 只读 `ocr_match_event`,不写任何表;不上 prompt 编辑器、不上 A/B 分流、不动 `OcrMatchEvent` 已有方法。
8. **不导出到 GitHub**:所有本地 commit 留在本地(项目惯例,需 VPN 时再推)。
9. **scope 限制**:本 plan 只做审计页(只读);不做图表、不做实时打分、不做权限细分、不做 prompt 编辑器。

---

## File Map

| 文件 | 角色 | 何时改 |
|---|---|---|
| `models/audit_query.py` | 新建 `OcrEventAudit` 类,3 个只读查询方法 | Task 1, 2, 3 |
| `models/__init__.py` | re-export `OcrEventAudit` | Task 4 |
| `blueprints/audit.py` | 新蓝图,4 个路由(主页 + aggregate/records JSON + export.csv) | Task 5 |
| `app.py` | `create_app()` 注册 `audit_bp` | Task 6 |
| `templates/audit-ocr-events.html` | 新 Jinja2 模板,继承 base.html | Task 6 |
| `static/css/app.css` | 加 `.rate-good/.rate-mid/.rate-bad` 三色 | Task 6 |
| `templates/base.html` | 「运营信息」分组加审计入口链接 | Task 6 |
| `tests/test_audit_ocr_events.py` | 7 个测试用例 | Task 1, 2, 3, 5 |

---

## Task 1: `OcrEventAudit.prompt_stats` 聚合查询

**Files:**
- Create: `models/audit_query.py`
- Modify: `models/__init__.py`(暂未 re-export,Task 4 才加)
- Test: `tests/test_audit_ocr_events.py`

**Interfaces:**
- Produces:
  - `models.audit_query.OcrEventAudit` 类
  - `OcrEventAudit.prompt_stats(*, start_date=None, end_date=None, prompt_versions=None) -> list[dict]`
  - 每 dict 字段:`prompt_version, total_pairs, consistent_pairs, inconsistent_pairs, null_pairs, consistency_rate (float|None), last_used_at`
  - 排序:`last_used_at DESC`

- [ ] **Step 1: 写失败测试**

打开 `tests/test_audit_ocr_events.py`(新建),写:

```python
"""OCR 事件审计页 — 单元测试入口"""
import os
import sys
import unittest
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models._db as _db


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        self._orig = _db.DB_PATH
        _db.DB_PATH = self.tmp.name
        from models import init_db, ShippingOrder, ShippingRecord, OcrMatchEvent
        init_db()
        self.oid = ShippingOrder.create('2026-07-25', 'C')
        self.rid = ShippingRecord.create('2026-07-25', 'C', '硬加面', '黑色', '1', 'y', '', self.oid)

    def tearDown(self):
        _db.DB_PATH = self._orig
        for ext in ('', '-wal', '-shm'):
            p = self.tmp.name + ext
            if os.path.exists(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass

    def _seed_pair(self, rid, oid, ai, human):
        """写一对(ai_match, human_verify)事件到同一 record。"""
        from models import OcrMatchEvent
        OcrMatchEvent.create('ai_match', rid, oid,
                             ai_match_status=ai, prompt_version='compare_rows_v1')
        OcrMatchEvent.create('human_verify', rid, oid, human_status=human)


class PromptStatsTests(_Base):
    def test_consistency_rate(self):
        """3 对:2 一致 + 1 不一致 + 1 仅 ai 无人工 → 总配对 3,一致率 2/3 = 66.7%。"""
        from models.audit_query import OcrEventAudit
        from models import ShippingRecord
        rid2 = ShippingRecord.create('2026-07-25', 'C', 'P', 'S', '1', 'y', '', self.oid)
        rid3 = ShippingRecord.create('2026-07-25', 'C', 'Q', 'S', '1', 'y', '', self.oid)
        rid4 = ShippingRecord.create('2026-07-25', 'C', 'R', 'S', '1', 'y', '', self.oid)
        self._seed_pair(self.rid, self.oid, 'green', 'green')   # 一致
        self._seed_pair(rid2, self.oid, 'green', 'green')       # 一致
        self._seed_pair(rid3, self.oid, 'red', 'green')         # 不一致
        self._seed_pair(rid4, self.oid, 'red', None)            # 仅 ai 无人工 → null_pairs=1,不计入分母

        rows = OcrEventAudit.prompt_stats()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r['prompt_version'], 'compare_rows_v1')
        self.assertEqual(r['total_pairs'], 3)
        self.assertEqual(r['consistent_pairs'], 2)
        self.assertEqual(r['inconsistent_pairs'], 1)
        self.assertEqual(r['null_pairs'], 1)
        self.assertAlmostEqual(r['consistency_rate'], 66.7, places=1)

    def test_filters_by_versions(self):
        from models.audit_query import OcrEventAudit
        from models import ShippingRecord
        rid2 = ShippingRecord.create('2026-07-25', 'C', 'P', 'S', '1', 'y', '', self.oid)
        from models import OcrMatchEvent
        OcrMatchEvent.create('ai_match', self.rid, self.oid, ai_match_status='green', prompt_version='v1')
        OcrMatchEvent.create('human_verify', self.rid, self.oid, human_status='green')
        OcrMatchEvent.create('ai_match', rid2, self.oid, ai_match_status='red', prompt_version='v2')
        OcrMatchEvent.create('human_verify', rid2, self.oid, human_status='red')

        rows = OcrEventAudit.prompt_stats(prompt_versions=['v1'])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['prompt_version'], 'v1')

    def test_no_events_returns_empty(self):
        from models.audit_query import OcrEventAudit
        rows = OcrEventAudit.prompt_stats()
        self.assertEqual(rows, [])


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: 跑测试验证失败**

Run: `PYTHONUTF8=1 python -m pytest tests/test_audit_ocr_events.py::PromptStatsTests -v`
Expected: FAIL — `ImportError: cannot import name 'OcrEventAudit' from 'models.audit_query'`

- [ ] **Step 3: 实现 `OcrEventAudit.prompt_stats`**

创建 `models/audit_query.py`:

```python
"""OCR 事件审计只读查询(不写库)。

本模块供 /audit/ocr-events 页面 + 3 个 JSON/CSV 端点使用。
零迁移、零新依赖;仅查询 ocr_match_event + shipping_records + staff。
"""
from ._db import get_db


class OcrEventAudit:
    """OCR 比对事件审计查询助手。"""

    @staticmethod
    def prompt_stats(*, start_date=None, end_date=None,
                     prompt_versions=None) -> list:
        """按 prompt_version 聚合一致率 + 配对数 + 最后使用时间。

        配对定义:同一 record 必须同时有 ai_match 和 human_verify 事件,
        各取 latest(created_at DESC, id DESC 兜底)配成一对。
        一致率 = consistent_pairs / (consistent_pairs + inconsistent_pairs);
        null_pairs 不计入分母(取消核查或 ai_status NULL 的对)。

        Args:
          start_date, end_date: 'YYYY-MM-DD' 字符串,闭区间 [start, end+1day)
          prompt_versions: 列表,None 表示不限

        Returns:
          [{prompt_version, total_pairs, consistent_pairs,
            inconsistent_pairs, null_pairs, consistency_rate,
            last_used_at}, ...]
          按 last_used_at DESC 排序。
        """
        params = []
        where_clauses = [
            "e.event_type IN ('ai_match', 'human_verify')",
            "e.prompt_version IS NOT NULL",
        ]
        if start_date:
            where_clauses.append("e.created_at >= ?")
            params.append(f"{start_date} 00:00:00")
        if end_date:
            where_clauses.append("e.created_at < ?")
            params.append(f"{end_date} 23:59:59")  # 含当天
        if prompt_versions:
            placeholders = ','.join('?' for _ in prompt_versions)
            where_clauses.append(f"e.prompt_version IN ({placeholders})")
            params.extend(prompt_versions)

        where_sql = ' AND '.join(where_clauses)

        sql = f"""
        WITH latest AS (
          SELECT
            e.record_id,
            e.prompt_version,
            e.event_type,
            e.ai_match_status,
            e.human_status,
            e.created_at,
            ROW_NUMBER() OVER (
              PARTITION BY e.record_id, e.event_type
              ORDER BY e.created_at DESC, e.id DESC
            ) AS rn
          FROM ocr_match_event e
          WHERE {where_sql}
        ),
        pairs AS (
          SELECT
            ai.record_id,
            ai.prompt_version,
            ai.ai_match_status,
            hv.human_status,
            CASE
              WHEN ai.ai_match_status IS NULL OR hv.human_status IS NULL THEN NULL
              WHEN ai.ai_match_status = hv.human_status THEN 1
              ELSE 0
            END AS is_consistent
          FROM latest ai
          JOIN latest hv ON ai.record_id = hv.record_id
          WHERE ai.event_type = 'ai_match' AND ai.rn = 1
            AND hv.event_type = 'human_verify' AND hv.rn = 1
        )
        SELECT
          p.prompt_version,
          COUNT(*) AS total_pairs,
          SUM(CASE WHEN is_consistent = 1 THEN 1 ELSE 0 END) AS consistent_pairs,
          SUM(CASE WHEN is_consistent = 0 THEN 1 ELSE 0 END) AS inconsistent_pairs,
          SUM(CASE WHEN is_consistent IS NULL THEN 1 ELSE 0 END) AS null_pairs,
          ROUND(
            100.0 * SUM(CASE WHEN is_consistent = 1 THEN 1 ELSE 0 END)
            / NULLIF(SUM(CASE WHEN is_consistent IS NOT NULL THEN 1 ELSE 0 END), 0),
            1
          ) AS consistency_rate,
          MAX(l.created_at) AS last_used_at
        FROM pairs p
        JOIN latest l ON l.record_id = p.record_id AND l.rn = 1
        GROUP BY p.prompt_version
        ORDER BY last_used_at DESC
        """
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]
```

- [ ] **Step 4: 跑测试验证通过**

Run: `PYTHONUTF8=1 python -m pytest tests/test_audit_ocr_events.py::PromptStatsTests -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
cd "C:/Users/Administrator/worklog-app" && git add models/audit_query.py tests/test_audit_ocr_events.py && git commit -m "feat(audit): OcrEventAudit.prompt_stats 按 prompt_version 聚合一致率"
```

---

## Task 2: `OcrEventAudit.record_pairs` 钻入明细

**Files:**
- Modify: `models/audit_query.py`
- Modify: `tests/test_audit_ocr_events.py`

**Interfaces:**
- Produces:
  - `OcrEventAudit.record_pairs(*, prompt_version, start_date=None, end_date=None, limit=500, offset=0) -> tuple[list[dict], int]`
  - 第 1 项:rows 列表,每 dict 字段:
    `record_id, order_id, order_date, customer, product_name, specification, quantity, unit,
     ai_status, ai_reason, ai_time, human_status, human_reason, operator_name, human_time,
     is_consistent (1|0|None)`
    按 `ai_time DESC` 排序
  - 第 2 项:满足条件的总 record 数(`COUNT(*)`,用于分页提示)

- [ ] **Step 1: 写失败测试**

在 `tests/test_audit_ocr_events.py` 追加:

```python
class RecordPairsTests(_Base):
    def test_join_logic(self):
        """同 record 有 ai_match + human_verify 时配对;只有 ai 没有 human_verify 时不出现。"""
        from models.audit_query import OcrEventAudit
        from models import ShippingRecord, ShippingOrder
        rid2 = ShippingRecord.create('2026-07-25', 'C', 'P', 'S', '1', 'y', '', self.oid)
        # rid: 配对一致
        self._seed_pair(self.rid, self.oid, 'green', 'green')
        # rid2: 配对不一致
        self._seed_pair(rid2, self.oid, 'red', 'green')

        rows, total = OcrEventAudit.record_pairs(prompt_version='compare_rows_v1')
        self.assertEqual(total, 2)
        self.assertEqual(len(rows), 2)
        # 按 ai_time DESC 排序:两条同秒插入,按 record_id DESC 兜底
        self.assertEqual(rows[0]['record_id'], rid2)  # 较后插入
        self.assertEqual(rows[1]['record_id'], self.rid)
        self.assertEqual(rows[0]['ai_status'], 'red')
        self.assertEqual(rows[0]['human_status'], 'green')
        self.assertEqual(rows[0]['is_consistent'], 0)
        self.assertEqual(rows[1]['is_consistent'], 1)

    def test_only_ai_no_human_excluded(self):
        """只有 ai_match 没有 human_verify 的 record 不出现在结果里。"""
        from models.audit_query import OcrEventAudit
        from models import OcrMatchEvent
        OcrMatchEvent.create('ai_match', self.rid, self.oid,
                             ai_match_status='red', prompt_version='v1')
        # 没有 human_verify

        rows, total = OcrEventAudit.record_pairs(prompt_version='v1')
        self.assertEqual(total, 0)
        self.assertEqual(rows, [])

    def test_latest_wins_for_human(self):
        """同一 record 多次核查,取 latest human_verify。"""
        from models.audit_query import OcrEventAudit
        from models import OcrMatchEvent
        OcrMatchEvent.create('ai_match', self.rid, self.oid,
                             ai_match_status='green', prompt_version='v1')
        # 第一次核查:红
        OcrMatchEvent.create('human_verify', self.rid, self.oid, human_status='red')
        # 第二次核查(覆盖):绿
        OcrMatchEvent.create('human_verify', self.rid, self.oid, human_status='green')

        rows, total = OcrEventAudit.record_pairs(prompt_version='v1')
        self.assertEqual(total, 1)
        self.assertEqual(rows[0]['human_status'], 'green')  # latest wins
        self.assertEqual(rows[0]['is_consistent'], 1)
```

- [ ] **Step 2: 跑测试验证失败**

Run: `PYTHONUTF8=1 python -m pytest tests/test_audit_ocr_events.py::RecordPairsTests -v`
Expected: FAIL — `AttributeError: type object 'OcrEventAudit' has no attribute 'record_pairs'`

- [ ] **Step 3: 实现 `record_pairs`**

在 `models/audit_query.py` 的 `OcrEventAudit` 类内追加:

```python
    @staticmethod
    def record_pairs(*, prompt_version, start_date=None, end_date=None,
                     limit=500, offset=0):
        """按 prompt_version 列出每个 record 的配对详情(用于钻入明细)。

        Args:
          prompt_version: 必填,只查该版本下的 ai_match 事件
          start_date, end_date: 'YYYY-MM-DD'
          limit, offset: 分页(默认 500/0,上限 2000 在蓝图层 enforce)

        Returns:
          (rows, total)
          rows: [{record_id, order_id, order_date, customer, product_name,
                  specification, quantity, unit,
                  ai_status, ai_reason, ai_time,
                  human_status, human_reason, operator_name, human_time,
                  is_consistent}, ...]
          total: COUNT(*),供分页提示
        """
        params = [prompt_version]
        where_clauses = ["lai.prompt_version = ?"]
        if start_date:
            where_clauses.append("lai.created_at >= ?")
            params.append(f"{start_date} 00:00:00")
        if end_date:
            where_clauses.append("lai.created_at <= ?")
            params.append(f"{end_date} 23:59:59")
        where_sql = ' AND '.join(where_clauses)

        # total 单独查
        total_sql = f"""
        WITH latest_ai AS (
          SELECT record_id,
            ROW_NUMBER() OVER (PARTITION BY record_id ORDER BY created_at DESC, id DESC) AS rn
          FROM ocr_match_event
          WHERE event_type = 'ai_match' AND prompt_version = ?
            {'AND created_at >= ?' if start_date else ''}
            {'AND created_at <= ?' if end_date else ''}
        )
        SELECT COUNT(*) FROM latest_ai WHERE rn = 1
        """
        total_params = [prompt_version]
        if start_date:
            total_params.append(f"{start_date} 00:00:00")
        if end_date:
            total_params.append(f"{end_date} 23:59:59")

        # rows 查
        rows_sql = f"""
        WITH latest_ai AS (
          SELECT record_id, ai_match_status, ai_match_reason, created_at,
            ROW_NUMBER() OVER (PARTITION BY record_id ORDER BY created_at DESC, id DESC) AS rn
          FROM ocr_match_event
          WHERE event_type = 'ai_match' AND prompt_version = ?
            {'AND created_at >= ?' if start_date else ''}
            {'AND created_at <= ?' if end_date else ''}
        ),
        latest_hv AS (
          SELECT record_id, human_status, human_reason, human_verified_by, created_at,
            ROW_NUMBER() OVER (PARTITION BY record_id ORDER BY created_at DESC, id DESC) AS rn
          FROM ocr_match_event
          WHERE event_type = 'human_verify'
        )
        SELECT
          sr.id AS record_id, sr.order_pk AS order_id,
          so.date AS order_date, so.customer AS customer,
          sr.product_name, sr.specification, sr.quantity, sr.unit,
          lai.ai_match_status AS ai_status,
          lai.ai_match_reason AS ai_reason,
          lai.created_at AS ai_time,
          lhv.human_status AS human_status,
          lhv.human_reason AS human_reason,
          staff.name AS operator_name,
          lhv.created_at AS human_time,
          CASE
            WHEN lai.ai_match_status IS NULL OR lhv.human_status IS NULL THEN NULL
            WHEN lai.ai_match_status = lhv.human_status THEN 1
            ELSE 0
          END AS is_consistent
        FROM latest_ai lai
        JOIN shipping_records sr ON sr.id = lai.record_id AND lai.rn = 1
        JOIN shipping_orders so ON so.id = sr.order_pk
        LEFT JOIN latest_hv lhv ON lhv.record_id = lai.record_id AND lhv.rn = 1
        LEFT JOIN staff ON staff.id = lhv.human_verified_by
        WHERE {where_sql}
        ORDER BY lai.created_at DESC, sr.id DESC
        LIMIT ? OFFSET ?
        """
        rows_params = params + [limit, offset]

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(total_sql, total_params)
        total = cursor.fetchone()[0]
        cursor.execute(rows_sql, rows_params)
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows], total
```

- [ ] **Step 4: 跑测试验证通过**

Run: `PYTHONUTF8=1 python -m pytest tests/test_audit_ocr_events.py::RecordPairsTests -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
cd "C:/Users/Administrator/worklog-app" && git add models/audit_query.py tests/test_audit_ocr_events.py && git commit -m "feat(audit): OcrEventAudit.record_pairs 钻入明细 + 取 latest 配对"
```

---

## Task 3: `OcrEventAudit.export_rows` CSV 导出数据源

**Files:**
- Modify: `models/audit_query.py`
- Modify: `tests/test_audit_ocr_events.py`

**Interfaces:**
- Produces:
  - `OcrEventAudit.export_rows(*, start_date=None, end_date=None, event_types=None, prompt_versions=None) -> list[dict]`
  - 每 dict 字段固定 20 列(顺序见 `_EXPORT_FIELDS`):
    `event_id, created_at, event_type, record_id, order_id, image_id, product_name, specification, ocr_text, ocr_engine, prompt_version, ai_engine, prompt_payload, ai_match_status, ai_match_score, ai_match_reason, ai_raw_response, human_status, human_reason, human_verified_by`
  - 排序:`id ASC`(append-only 顺序)
- Const:
  - 模块级 `_EXPORT_FIELDS = [...]`(蓝图层 import 用于 CSV header)

- [ ] **Step 1: 写失败测试**

在 `tests/test_audit_ocr_events.py` 追加:

```python
class ExportRowsTests(_Base):
    def test_includes_all_three_event_types(self):
        """导出含 record_ocr / ai_match / human_verify 三类全字段。"""
        from models.audit_query import OcrEventAudit
        from models import OcrMatchEvent
        OcrMatchEvent.create('record_ocr', self.rid, self.oid,
                             ocr_text='硬加面', ocr_engine='paddleocr',
                             ai_match_status='green', ai_engine='local_fuzzy',
                             product_name='硬加面', specification='黑色')
        OcrMatchEvent.create('ai_match', self.rid, self.oid,
                             ai_match_status='green', ai_engine='deepseek',
                             prompt_version='v1', prompt_payload='PROMPT',
                             ai_raw_response='{}')
        OcrMatchEvent.create('human_verify', self.rid, self.oid,
                             human_status='green', human_verified_by=None)

        rows = OcrEventAudit.export_rows()
        self.assertEqual(len(rows), 3)
        # 全字段存在
        for r in rows:
            for f in ('event_id', 'created_at', 'event_type', 'record_id', 'order_id',
                      'product_name', 'specification', 'ocr_text', 'ocr_engine',
                      'prompt_version', 'ai_match_status', 'human_status'):
                self.assertIn(f, r)

    def test_filters_by_event_types(self):
        """event_types=['ai_match'] 只返 ai_match。"""
        from models.audit_query import OcrEventAudit
        from models import OcrMatchEvent
        OcrMatchEvent.create('record_ocr', self.rid, self.oid, ocr_text='x')
        OcrMatchEvent.create('ai_match', self.rid, self.oid, ai_match_status='green')

        rows = OcrEventAudit.export_rows(event_types=['ai_match'])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['event_type'], 'ai_match')
```

- [ ] **Step 2: 跑测试验证失败**

Run: `PYTHONUTF8=1 python -m pytest tests/test_audit_ocr_events.py::ExportRowsTests -v`
Expected: FAIL — `AttributeError: type object 'OcrEventAudit' has no attribute 'export_rows'`

- [ ] **Step 3: 实现 `export_rows` + `_EXPORT_FIELDS`**

在 `models/audit_query.py` 顶部加常量:

```python
_EXPORT_FIELDS = [
    'event_id', 'created_at', 'event_type',
    'record_id', 'order_id', 'image_id',
    'product_name', 'specification',
    'ocr_text', 'ocr_engine',
    'prompt_version', 'ai_engine', 'prompt_payload',
    'ai_match_status', 'ai_match_score', 'ai_match_reason', 'ai_raw_response',
    'human_status', 'human_reason', 'human_verified_by',
]
```

在 `OcrEventAudit` 类内追加:

```python
    @staticmethod
    def export_rows(*, start_date=None, end_date=None,
                    event_types=None, prompt_versions=None):
        """扁平返回每个事件全字段,供 CSV 导出。

        不走配对,逐事件返。
        """
        params = []
        where_clauses = []
        if start_date:
            where_clauses.append("e.created_at >= ?")
            params.append(f"{start_date} 00:00:00")
        if end_date:
            where_clauses.append("e.created_at <= ?")
            params.append(f"{end_date} 23:59:59")
        if event_types:
            placeholders = ','.join('?' for _ in event_types)
            where_clauses.append(f"e.event_type IN ({placeholders})")
            params.extend(event_types)
        if prompt_versions:
            placeholders = ','.join('?' for _ in prompt_versions)
            where_clauses.append(f"e.prompt_version IN ({placeholders})")
            params.extend(prompt_versions)

        where_sql = ('WHERE ' + ' AND '.join(where_clauses)) if where_clauses else ''

        sql = f"""
        SELECT
          e.id AS event_id, e.created_at, e.event_type,
          e.record_id, e.order_id, e.image_id,
          e.product_name, e.specification,
          e.ocr_text, e.ocr_engine,
          e.prompt_version, e.ai_engine, e.prompt_payload,
          e.ai_match_status, e.ai_match_score, e.ai_match_reason, e.ai_raw_response,
          e.human_status, e.human_reason, e.human_verified_by
        FROM ocr_match_event e
        {where_sql}
        ORDER BY e.id ASC
        """
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]
```

- [ ] **Step 4: 跑测试验证通过**

Run: `PYTHONUTF8=1 python -m pytest tests/test_audit_ocr_events.py::ExportRowsTests -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
cd "C:/Users/Administrator/worklog-app" && git add models/audit_query.py tests/test_audit_ocr_events.py && git commit -m "feat(audit): OcrEventAudit.export_rows 扁平导出全字段"
```

---

## Task 4: `models/__init__.py` re-export `OcrEventAudit`

**Files:**
- Modify: `models/__init__.py`

**Interfaces:**
- Produces: `from models import OcrEventAudit` 可用

- [ ] **Step 1: 修改 `models/__init__.py`**

打开 `models/__init__.py`,找到现有 re-export 段(约 line 7-20),在 `.audit` import 之后加:

```python
from .audit_query import OcrEventAudit
```

并在 `__all__` 列表(约 line 22-33)加 `'OcrEventAudit',`(按字母顺序,放在 `'OcrMatchEvent'` 之后或 `'PieceConversion'` 之前)。

- [ ] **Step 2: 验证 import 成功**

Run: `PYTHONUTF8=1 python -c "from models import OcrEventAudit; print(OcrEventAudit)"`
Expected: `<class 'models.audit_query.OcrEventAudit'>` 打印

- [ ] **Step 3: Commit**

```bash
cd "C:/Users/Administrator/worklog-app" && git add models/__init__.py && git commit -m "feat(model): re-export OcrEventAudit"
```

---

## Task 5: `blueprints/audit.py` 4 个路由

**Files:**
- Create: `blueprints/audit.py`
- Test: `tests/test_audit_ocr_events.py`

**Interfaces:**
- Produces:
  - `blueprints.audit.bp` Blueprint 实例
  - 路由:
    - `GET /audit/ocr-events` — 渲染模板
    - `GET /api/v1/audit/ocr-events/aggregate` — JSON 聚合
    - `GET /api/v1/audit/ocr-events/records` — JSON 钻入明细
    - `GET /api/v1/audit/ocr-events/export.csv` — CSV 文件流

- [ ] **Step 1: 写失败测试(端点层)**

在 `tests/test_audit_ocr_events.py` 追加:

```python
class AuditEndpointTests(_Base):
    def setUp(self):
        super().setUp()
        from app import create_app
        self.client = create_app().test_client()

    def test_aggregate_endpoint(self):
        """aggregate 端点返 JSON,rows 至少 1 行。"""
        self._seed_pair(self.rid, self.oid, 'green', 'green')
        resp = self.client.get('/api/v1/audit/ocr-events/aggregate')
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body['success'])
        self.assertEqual(len(body['rows']), 1)
        self.assertEqual(body['rows'][0]['prompt_version'], 'compare_rows_v1')

    def test_records_endpoint_requires_prompt_version(self):
        resp = self.client.get('/api/v1/audit/ocr-events/records')
        self.assertEqual(resp.status_code, 400)
        body = resp.get_json()
        self.assertFalse(body['success'])
        self.assertIn('prompt_version', body['error'])

    def test_records_endpoint_returns_pairs(self):
        self._seed_pair(self.rid, self.oid, 'green', 'green')
        resp = self.client.get('/api/v1/audit/ocr-events/records?prompt_version=compare_rows_v1')
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body['success'])
        self.assertEqual(body['total'], 1)
        self.assertEqual(len(body['rows']), 1)
        self.assertEqual(body['rows'][0]['record_id'], self.rid)
        self.assertEqual(body['rows'][0]['is_consistent'], 1)

    def test_export_csv_returns_csv_file(self):
        self._seed_pair(self.rid, self.oid, 'green', 'green')
        resp = self.client.get('/api/v1/audit/ocr-events/export.csv')
        self.assertEqual(resp.status_code, 200)
        self.assertIn('text/csv', resp.headers['Content-Type'])
        # BOM
        body = resp.data
        self.assertTrue(body.startswith(b'\xef\xbb\xbf'))
        text = body.decode('utf-8-sig')
        # header 行
        self.assertIn('event_id', text)
        self.assertIn('ai_match_status', text)
```

- [ ] **Step 2: 跑测试验证失败**

Run: `PYTHONUTF8=1 python -m pytest tests/test_audit_ocr_events.py::AuditEndpointTests -v`
Expected: 4 failed (404 not found)

- [ ] **Step 3: 实现蓝图**

创建 `blueprints/audit.py`:

```python
"""OCR 事件审计页蓝图。

提供:
  GET  /audit/ocr-events                   渲染审计主页
  GET  /api/v1/audit/ocr-events/aggregate JSON 聚合数据
  GET  /api/v1/audit/ocr-events/records   JSON 钻入明细
  GET  /api/v1/audit/ocr-events/export.csv CSV 文件流
"""
import csv
import io
from datetime import datetime
from flask import Blueprint, render_template, request, jsonify, Response, current_app

from models import OcrEventAudit
from models.audit_query import _EXPORT_FIELDS

bp = Blueprint('audit', __name__)


def _parse_common_filters():
    """解析 start_date / end_date / prompt_versions 公共参数。

    Raises:
      ValueError: 日期格式错 / 开始 > 结束
    """
    start = request.args.get('start_date', '').strip() or None
    end = request.args.get('end_date', '').strip() or None
    pv_csv = request.args.get('prompt_versions', '').strip()
    prompt_versions = [v for v in pv_csv.split(',') if v] or None
    for label, val in (('start_date', start), ('end_date', end)):
        if val:
            try:
                datetime.strptime(val, '%Y-%m-%d')
            except ValueError:
                raise ValueError(f'{label} 格式错误,应为 YYYY-MM-DD')
    if start and end and start > end:
        raise ValueError('开始日期不能晚于结束日期')
    return {'start_date': start, 'end_date': end, 'prompt_versions': prompt_versions}


@bp.route('/audit/ocr-events')
def audit_ocr_events():
    """渲染审计主页(Jinja2 模板 + base.html)"""
    return render_template('audit-ocr-events.html')


@bp.route('/api/v1/audit/ocr-events/aggregate')
def api_audit_aggregate():
    try:
        params = _parse_common_filters()
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    try:
        rows = OcrEventAudit.prompt_stats(**params)
    except Exception:
        current_app.logger.exception('prompt_stats 失败')
        return jsonify({'success': False, 'error': '查询失败'}), 500
    return jsonify({'success': True, 'rows': rows})


@bp.route('/api/v1/audit/ocr-events/records')
def api_audit_records():
    prompt_version = request.args.get('prompt_version', '').strip()
    if not prompt_version:
        return jsonify({'success': False, 'error': 'prompt_version 必填'}), 400
    try:
        common = _parse_common_filters()
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    limit = min(int(request.args.get('limit', 500)), 2000)
    offset = max(int(request.args.get('offset', 0)), 0)
    try:
        rows, total = OcrEventAudit.record_pairs(
            prompt_version=prompt_version,
            start_date=common['start_date'],
            end_date=common['end_date'],
            limit=limit, offset=offset,
        )
    except Exception:
        current_app.logger.exception('record_pairs 失败')
        return jsonify({'success': False, 'error': '查询失败'}), 500
    return jsonify({
        'success': True, 'rows': rows, 'total': total,
        'limit': limit, 'offset': offset,
    })


@bp.route('/api/v1/audit/ocr-events/export.csv')
def api_audit_export_csv():
    start = request.args.get('start_date', '').strip() or None
    end = request.args.get('end_date', '').strip() or None
    et_csv = request.args.get('event_types', '').strip()
    pv_csv = request.args.get('prompt_versions', '').strip()
    event_types = [t for t in et_csv.split(',') if t] or None
    prompt_versions = [v for v in pv_csv.split(',') if v] or None
    try:
        rows = OcrEventAudit.export_rows(
            start_date=start, end_date=end,
            event_types=event_types, prompt_versions=prompt_versions,
        )
    except Exception:
        current_app.logger.exception('export_rows 失败')
        return jsonify({'success': False, 'error': '导出失败'}), 500

    fname = f'ocr_events_{datetime.now().strftime("%Y%m%d_%H%M")}.csv'
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_EXPORT_FIELDS)
    writer.writeheader()
    for r in rows:
        writer.writerow({k: (r.get(k) if r.get(k) is not None else '') for k in _EXPORT_FIELDS})
    body = buf.getvalue().encode('utf-8-sig')
    return Response(
        body,
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename="{fname}"'},
    )
```

- [ ] **Step 4: 跑测试验证通过**

Run: `PYTHONUTF8=1 python -m pytest tests/test_audit_ocr_events.py::AuditEndpointTests -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
cd "C:/Users/Administrator/worklog-app" && git add blueprints/audit.py tests/test_audit_ocr_events.py && git commit -m "feat(audit): 4 个审计路由(主页 + aggregate/records/export.csv)"
```

---

## Task 6: app.py 注册 + 模板 + CSS + 导航入口

**Files:**
- Modify: `app.py`
- Create: `templates/audit-ocr-events.html`
- Modify: `static/css/app.css`(追加配色)
- Modify: `templates/base.html`(加导航链接)

**Interfaces:**
- Produces: 浏览器访问 `/audit/ocr-events` 看到完整审计页(过滤栏 + 聚合表 + 钻入空态)

- [ ] **Step 1: `app.py` 注册蓝图**

打开 `app.py`,在现有 `from blueprints.xxx import bp as xxx_bp` 段附近(约 line 30-45)加:

```python
from blueprints.audit import bp as audit_bp
```

并在 `app.register_blueprint(...)` 列表(约 line 50-70)加:

```python
    app.register_blueprint(audit_bp)
```

- [ ] **Step 2: 创建 `templates/audit-ocr-events.html`**

```html
{% extends 'base.html' %}
{% block content %}
<h1 class="text-2xl font-bold mb-4">OCR 比对事件审计</h1>
<p class="text-sm text-gray-600 mb-4">按 prompt_version 维度审视 AI 与人工裁决的一致率,为优化比对提示词提供数据。</p>

<!-- 过滤栏 -->
<section id="filters" class="bg-white p-4 rounded shadow mb-4 flex flex-wrap gap-4 items-end">
  <label class="block">
    <span class="text-sm text-gray-700">开始日期</span>
    <input type="date" id="start_date" class="mt-1 block border rounded px-2 py-1">
  </label>
  <label class="block">
    <span class="text-sm text-gray-700">结束日期</span>
    <input type="date" id="end_date" class="mt-1 block border rounded px-2 py-1">
  </label>
  <label class="block">
    <span class="text-sm text-gray-700">事件类型</span>
    <select multiple id="event_types" size="3" class="mt-1 block border rounded px-2 py-1">
      <option value="record_ocr" selected>record_ocr</option>
      <option value="ai_match" selected>ai_match</option>
      <option value="human_verify" selected>human_verify</option>
    </select>
  </label>
  <label class="block">
    <span class="text-sm text-gray-700">prompt 版本(逗号分隔)</span>
    <input type="text" id="prompt_versions" placeholder="v1,v2" class="mt-1 block border rounded px-2 py-1">
  </label>
  <button id="btn_apply" class="px-4 py-2 bg-blue-600 text-white rounded">应用</button>
  <button id="btn_export" class="px-4 py-2 bg-gray-200 text-gray-800 rounded">导出 CSV</button>
</section>

<!-- 聚合主表 -->
<section id="aggregate_panel" class="bg-white rounded shadow mb-4">
  <table id="aggregate_table" class="w-full">
    <thead class="bg-gray-100">
      <tr>
        <th class="px-3 py-2 text-left">prompt 版本</th>
        <th class="px-3 py-2 text-right">配对数</th>
        <th class="px-3 py-2 text-right">一致</th>
        <th class="px-3 py-2 text-right">不一致</th>
        <th class="px-3 py-2 text-right">人工未核</th>
        <th class="px-3 py-2 text-right">一致率</th>
        <th class="px-3 py-2 text-left">最后使用</th>
        <th class="px-3 py-2 text-center">操作</th>
      </tr>
    </thead>
    <tbody></tbody>
  </table>
  <p id="aggregate_empty" class="p-4 text-gray-500" hidden>暂无数据,试试放宽过滤或先在出货页跑几次 OCR / AI 匹配 / 人工核查。</p>
</section>

<!-- 钻入面板 -->
<section id="drill_panel" class="bg-white rounded shadow" hidden>
  <h2 class="text-lg font-semibold px-4 py-3 border-b">明细:<span id="drill_pv" class="font-mono"></span></h2>
  <div class="overflow-x-auto">
    <table id="records_table" class="w-full text-sm">
      <thead class="bg-gray-100">
        <tr>
          <th class="px-2 py-1">record_id</th>
          <th class="px-2 py-1">订单</th>
          <th class="px-2 py-1">日期</th>
          <th class="px-2 py-1">客户</th>
          <th class="px-2 py-1">品名</th>
          <th class="px-2 py-1">规格</th>
          <th class="px-2 py-1">数量</th>
          <th class="px-2 py-1">AI 判定</th>
          <th class="px-2 py-1">AI 理由</th>
          <th class="px-2 py-1">AI 时间</th>
          <th class="px-2 py-1">人工判定</th>
          <th class="px-2 py-1">人工理由</th>
          <th class="px-2 py-1">操作员</th>
          <th class="px-2 py-1">人工时间</th>
          <th class="px-2 py-1">一致?</th>
        </tr>
      </thead>
      <tbody></tbody>
    </table>
  </div>
  <p id="records_pagination" class="p-3 text-sm text-gray-500"></p>
</section>

<script>
function escapeHtml(s) {
  if (s == null) return '';
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}

function buildParams() {
  const p = new URLSearchParams();
  const start = document.querySelector('#start_date').value;
  const end = document.querySelector('#end_date').value;
  if (start) p.set('start_date', start);
  if (end) p.set('end_date', end);
  const pv = document.querySelector('#prompt_versions').value.trim();
  if (pv) p.set('prompt_versions', pv);
  // event_types 用于 CSV 导出;聚合端点不用
  window._eventTypes = Array.from(document.querySelector('#event_types').selectedOptions).map(o => o.value);
  return p.toString();
}

async function loadAggregate() {
  const resp = await fetch('/api/v1/audit/ocr-events/aggregate?' + buildParams());
  const data = await resp.json();
  if (!data.success) { alert(data.error); return; }
  renderAggregateTable(data.rows);
  // 钻入面板隐藏(主表刷新后,明细需重 fetch)
  document.querySelector('#drill_panel').hidden = true;
}

function renderAggregateTable(rows) {
  const tbody = document.querySelector('#aggregate_table tbody');
  tbody.innerHTML = '';
  if (!rows.length) {
    document.querySelector('#aggregate_empty').hidden = false;
    return;
  }
  document.querySelector('#aggregate_empty').hidden = true;
  rows.forEach(r => {
    const tr = document.createElement('tr');
    tr.className = 'border-t';
    const rate = r.consistency_rate;
    let cls = '';
    if (rate != null) cls = rate >= 80 ? 'rate-good' : rate >= 60 ? 'rate-mid' : 'rate-bad';
    tr.innerHTML = `
      <td class="px-3 py-2 font-mono">${escapeHtml(r.prompt_version)}</td>
      <td class="px-3 py-2 text-right">${r.total_pairs}</td>
      <td class="px-3 py-2 text-right">${r.consistent_pairs}</td>
      <td class="px-3 py-2 text-right">${r.inconsistent_pairs}</td>
      <td class="px-3 py-2 text-right">${r.null_pairs}</td>
      <td class="px-3 py-2 text-right ${cls}">${rate == null ? '-' : rate.toFixed(1) + '%'}</td>
      <td class="px-3 py-2 text-gray-600">${escapeHtml(r.last_used_at || '')}</td>
      <td class="px-3 py-2 text-center">
        <button class="btn-drill px-2 py-1 text-sm bg-gray-100 hover:bg-gray-200 rounded"
                data-pv="${escapeHtml(r.prompt_version)}">明细</button>
      </td>
    `;
    tbody.appendChild(tr);
  });
}

async function drillDown(pv) {
  const p = buildParams();
  const url = '/api/v1/audit/ocr-events/records?prompt_version=' + encodeURIComponent(pv) + '&' + p;
  const resp = await fetch(url);
  const data = await resp.json();
  if (!data.success) { alert(data.error); return; }
  document.querySelector('#drill_pv').textContent = pv;
  renderRecordsTable(data.rows, data.total);
  document.querySelector('#drill_panel').hidden = false;
  document.querySelector('#drill_panel').scrollIntoView({behavior: 'smooth'});
}

function renderRecordsTable(rows, total) {
  const tbody = document.querySelector('#records_table tbody');
  tbody.innerHTML = '';
  rows.forEach(r => {
    const tr = document.createElement('tr');
    tr.className = 'border-t';
    const cls = r.is_consistent === 1 ? 'rate-good' : r.is_consistent === 0 ? 'rate-bad' : 'text-gray-400';
    const mark = r.is_consistent === 1 ? '✓' : r.is_consistent === 0 ? '✗' : '-';
    tr.innerHTML = `
      <td class="px-2 py-1 font-mono">${r.record_id}</td>
      <td class="px-2 py-1 font-mono">${r.order_id}</td>
      <td class="px-2 py-1">${escapeHtml(r.order_date)}</td>
      <td class="px-2 py-1">${escapeHtml(r.customer)}</td>
      <td class="px-2 py-1">${escapeHtml(r.product_name)}</td>
      <td class="px-2 py-1">${escapeHtml(r.specification)}</td>
      <td class="px-2 py-1">${escapeHtml(r.quantity)} ${escapeHtml(r.unit)}</td>
      <td class="px-2 py-1">${escapeHtml(r.ai_status)}</td>
      <td class="px-2 py-1 text-gray-600">${escapeHtml(r.ai_reason)}</td>
      <td class="px-2 py-1 text-gray-500">${escapeHtml(r.ai_time)}</td>
      <td class="px-2 py-1">${escapeHtml(r.human_status)}</td>
      <td class="px-2 py-1 text-gray-600">${escapeHtml(r.human_reason)}</td>
      <td class="px-2 py-1">${escapeHtml(r.operator_name || '-')}</td>
      <td class="px-2 py-1 text-gray-500">${escapeHtml(r.human_time || '')}</td>
      <td class="px-2 py-1 text-center ${cls}">${mark}</td>
    `;
    tbody.appendChild(tr);
  });
  document.querySelector('#records_pagination').textContent =
    `显示 ${rows.length} / 共 ${total} 条`;
}

document.querySelector('#btn_export').onclick = () => {
  const params = buildParams();
  let url = '/api/v1/audit/ocr-events/export.csv?' + params;
  if (window._eventTypes && window._eventTypes.length) {
    url += '&event_types=' + window._eventTypes.join(',');
  }
  window.location = url;
};

window.addEventListener('DOMContentLoaded', () => {
  loadAggregate();
  document.querySelector('#btn_apply').onclick = loadAggregate;
  document.addEventListener('click', e => {
    if (e.target.classList.contains('btn-drill')) {
      drillDown(e.target.dataset.pv);
    }
  });
});
</script>
{% endblock %}
```

- [ ] **Step 3: `static/css/app.css` 加配色**

打开 `static/css/app.css`,在文件末尾追加:

```css
/* OCR 事件审计:一致率配色 */
.rate-good { color: #16a34a; font-weight: 600; }
.rate-mid  { color: #ca8a04; font-weight: 600; }
.rate-bad  { color: #dc2626; font-weight: 600; }
```

- [ ] **Step 4: `templates/base.html` 加导航入口**

打开 `templates/base.html`,找到「运营信息」分组(通常是个 dropdown),在现有「公司通知」「通知彩色版」「业务流程」「仓库布局」「综合查找」等链接附近加:

```html
<a href="{{ url_for('audit.audit_ocr_events') }}" class="...">OCR 事件审计</a>
```

(具体 class 与同分组其他链接保持一致。)

- [ ] **Step 5: 跑全套测试 + 启动服务器冒烟**

Run: `PYTHONUTF8=1 python -m pytest tests/test_audit_ocr_events.py -v`
Expected: 14 passed(3 + 3 + 2 + 4 = 12 个主测试 + 4 个 _Base helper 实例化,实际 12 个 assert)

然后冒烟启动:
```bash
cd "C:/Users/Administrator/worklog-app" && python -X utf8 app.py &
sleep 3
curl -sS http://127.0.0.1:5050/audit/ocr-events | head -c 200
kill %1 2>/dev/null
```
Expected: HTML 输出含 `<h1` 和 `OCR 比对事件审计`

- [ ] **Step 6: Commit**

```bash
cd "C:/Users/Administrator/worklog-app" && git add app.py templates/audit-ocr-events.html static/css/app.css templates/base.html && git commit -m "feat(audit): 模板 + CSS 配色 + 导航入口 + 蓝图注册"
```

---

## Task 7: 全量回归 + 备份

**Files:** 无(只跑测试 + 备份)

**Interfaces:** 无(验证性质)

- [ ] **Step 1: 备份 db(惯例)**

```bash
cp "C:/Users/Administrator/worklog.db" "D:/BAK/worklog_$(date +%Y%m%d_%H%M).db"
```

- [ ] **Step 2: 全量回归**

Run: `PYTHONUTF8=1 python -m pytest tests/ -q --ignore=tests/regression --ignore=tests/test_compare_rows_adaptive.py`
Expected: 通过数 ≥ 之前 baseline(plan 2026-07-27 全套 commit 后是 31 failed / 181 passed);新增 12 个 audit 测试全过,0 新失败

- [ ] **Step 3: 检查 commit 历史**

Run: `cd "C:/Users/Administrator/worklog-app" && git log --oneline -7`
Expected: 6 个新 audit commit 紧接 spec/plan commit

- [ ] **Step 4: 更新 progress.md + plan report(可选,但推荐)**

在 `.superpowers/sdd/2026-07-28-ocr-event-audit/` 建 progress.md,记录 7 task 完成情况,跟之前 plan 的 report 风格一致。

- [ ] **Step 5: 最终 commit(仅 progress + report)**

```bash
cd "C:/Users/Administrator/worklog-app" && git add .superpowers/sdd/ && git commit -m "docs(progress): OCR 事件审计 7 task 全完成"
```

---

## Self-Review Checklist

- [x] **Spec coverage**:
  - §1 目标 ✓ Task 5/6
  - §3.1 `OcrEventAudit` 3 方法 ✓ Task 1/2/3
  - §5 蓝图 4 路由 ✓ Task 5
  - §6 前端 + 配色 + 导航 ✓ Task 6
  - §7 错误处理 ✓ Task 5(端点 try/except + 400/500 JSON)
  - §9 测试 ✓ Task 1/2/3/5
  - §11 待实现清单 ✓ Task 1-7
- [x] **Placeholder scan**: 无 TBD / TODO / "implement later"
- [x] **Type consistency**: `OcrEventAudit.prompt_stats(prompt_version, start, end, ...)` 在 Task 1 定义,Task 5 调用一致;`_EXPORT_FIELDS` Task 3 定义、Task 5 import 用
- [x] **Signature check**: `_parse_common_filters` 只在 Task 5 定义并使用,签名稳定
- [x] **No mock pattern issues**: Task 1/2/3 直接 `from ._db import get_db`,测试端 patch 路径为 `models.audit_query.OcrEventAudit.prompt_stats`(或根本不需 patch — 测试用真实 DB + 临时文件)

---

## Known Constraints Within Plan

1. **Task 5 端点测试需要 `app = create_app()`**:走的是 `tests/test_audit_ocr_events.py` 里 `from app import create_app`,跟 shipping/inbound/loading 端点测试一致
2. **`_EXPORT_FIELDS` 跨模块引用**:Task 3 在 `models/audit_query.py` 定义,Task 5 `blueprints/audit.py` `from models.audit_query import _EXPORT_FIELDS`(模块级常量,稳定)
3. **CSV 大文件**:SQLite 全量拉取后 Python 端 DictWriter 写,内存用量 = 一行 dict 大小 × N(N<10k 行 dict ≈ 几 MB,可接受)
4. **CSV BOM**:用 `utf-8-sig` 编码,Excel 双击自动识别 UTF-8(项目已用此方式于其他导出)
5. **CSS 配色阈值 80/60**:写死,后续有需要再让用户调(spec §6.2)
6. **drill-down 同页**:点「明细」按钮触发同页下半部分 fetch + scrollIntoView,无路由跳转(方案 A)
7. **前端不测**:spec §9 明确不测模板,Step 5 的 `curl` 冒烟只是 sanity check(允许失败但不阻塞 commit)