# 出货 OCR 比对历史记录库 — 设计文档

- 日期：2026-07-27
- 页面：`/shipping-records`
- 路线图定位：阶段 2「AI 验数 + 校对」的**评测数据沉淀层**——把每次 OCR/AI 比对/人工核查裁决
  全链路留痕，为以后"用 AI 系统性优化比对提示词"提供素材
- 状态：待用户 review

## 1. 背景与目标

出货明细行已支持 OCR + AI 匹配 + 人工核查三档产出（绿/黄/红徽章）。
但目前**只存"最新一次"裁决**于 `shipping_images.match_status / reason / human_verified`，
缺历史轨迹。本设计目标：

> **建立 append-only 历史表，每次关键裁决事件都留一笔，方便日后离线批分析。**

### 1.1 用途（已与用户确认）

未来用途是用 AI 系统性优化比对提示词：
- 给定过去的人工裁决与对应 prompt / OCR / AI 输出，批量找出"AI 经常错在哪类行"
- 用这些样本 prompt 工程 → 微调提示词
- 评估新 prompt 时回放 prompt_payload + ai_raw_response 做离线打分

### 1.2 目标 / 非目标

**目标**：
- 三类事件各写一行 append-only：`record_ocr` / `ai_match` / `human_verify`
- 完整保留 prompt/输出/裁决快照，按 prompt_version 切分版本
- 不破坏现有 shipping_images.match_status 等列（共生产用，前端依赖）
- 不要求新增 UI（仅记录 + 提供查询接口给后续脚本/dashboard）

**非目标（明确排除，避免范围蔓延）**：
- 不做 backfill（已与用户确认从迁移后开始记）
- 不在出货页前端加任何 UI
- 不做 prompt 编辑器、不做 A/B 分流、不做实时打分
- 不引入新的迁移工具（Alembic 之类）——沿用 `models/_init.py` 增量 SQL

## 2. 总体架构

```
[行级图片上传]
   └→ ShippingImage.create
   └→ _run_label_match (PaddleOCR + 本地 fuzzy)
   └→ ShippingImage.set_match
   └→ OcrMatchEvent.create(event_type='record_ocr', image_id=img_id, ai_engine='local_fuzzy', ...)
   [ai-match 整单]
   └→ PaddleOCR 聚合 OCR
   └→ DeepSeek compare_rows
   └→ ShippingImage.set_match (循环逐 record)
   └→ OcrMatchEvent.create(event_type='ai_match', record_id=rid, image_id=NULL, prompt_payload, ai_raw_response, ...)
   [人工核查 manual-verify]
   └→ ShippingImage.set_human_verified
   └→ OcrMatchEvent.create(event_type='human_verify', image_id=img_id,
                            human_status, human_verified_by,
                            ai_match_status=<读 shipping_images 取 AI 之前说>)
```

事件写入是**审计性质**，写入失败只 logger.error 不阻断主流程（与现有 AuditLog 哲学一致）。

## 3. 数据模型

### 3.1 新表 `ocr_match_event`

```sql
CREATE TABLE IF NOT EXISTS ocr_match_event (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id           INTEGER NOT NULL,
    image_id            INTEGER,
    order_id            INTEGER NOT NULL,
    event_type          TEXT NOT NULL,         -- 'record_ocr' | 'ai_match' | 'human_verify'
    -- 输入侧
    ocr_text            TEXT,
    ocr_engine          TEXT,                  -- 'paddleocr'
    product_name        TEXT,                  -- 快照
    specification       TEXT,                  -- 快照
    -- AI 处理侧
    prompt_payload      TEXT,
    ai_match_status     TEXT,
    ai_match_score      REAL,
    ai_match_reason     TEXT,
    ai_raw_response     TEXT,
    ai_engine           TEXT,                  -- 'deepseek' / 'local_fuzzy'
    prompt_version      TEXT,                  -- 调 COMPARE_PROMPT 时填入,后续分析按版本切分
    -- 人工侧(仅 human_verify 填)
    human_status        TEXT,
    human_reason        TEXT,
    human_verified_by   INTEGER,
    -- 时间戳
    created_at          TEXT NOT NULL,
    FOREIGN KEY (record_id) REFERENCES shipping_records(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_ocr_match_record ON ocr_match_event(record_id, event_type, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_ocr_match_order  ON ocr_match_event(order_id, created_at DESC);
```

**字段决策依据**：
- **`order_id` 冗余**：因为 `image_id` 在人工删图后变 NULL，从 `record_id` 也能 JOIN 到 order，
  但冗余存让"按订单时间线看"少一次 JOIN；分析维度更灵活
- **`product_name` / `specification` 快照**：后续用户改明细不应回溯影响已记录的裁决
- **`prompt_payload` + `ai_raw_response` 关键**：日后重跑/微调 prompt 时复现 LLM 输入输出
- **`prompt_version`**：当 COMPARE_PROMPT 升级时手 bump，常量 import 同源——保证版本一致
- **FOREIGN KEY ... ON DELETE CASCADE**：明细删除时事件记录同删（与用户确认）

### 3.2 新模型类 `OcrMatchEvent`

`models/orders.py` 中新增：

| 方法 | 用途 |
|---|---|
| `create(event_type, record_id, order_id, image_id=None, **fields)` | 写一条事件；不传 event_type 抛 ValueError |
| `get_by_record(record_id, event_type=None)` | 查某明细行的所有事件(老→新) |
| `get_by_order(order_id, event_type=None)` | 查某订单所有事件(老→新) |
| `get_ai_human_delta(record_id)` | 返回同 record_id 的 (event_type, ai_status, human_status, ...) list，便于将来算一致率 |

每个方法独立 `get_db()/commit()/close()`，与现有 `ShippingRecord` 等风格一致。

## 4. 写库触发点

均沿用现有端点事务边界；不另开事务。

| 事件类型 | 端点 | shipping.py 大致行 | 触发时机 |
|---|---|---|---|
| `record_ocr` | `POST /shipping-orders/records/<rid>/images` (multipart) | 651-664（saved 数组构建末） | 每张图 set_match 后 |
| `record_ocr` | 同上 (base64 单图分支) | 686-695 | 同上 |
| `ai_match` | `POST /shipping-orders/<oid>/ai-match` | 866-870（逐 record 写库循环内） | 每条 record verdict 写库后 |
| `human_verify` | `POST /shipping-orders/images/<iid>/manual-verify` | 578-583（set_human_verified 前） | 人工裁决前先读 shipping_images 取 `ai_match_status/reason` 快照 |

**关键点**：
- `human_verify` 事件写入前要 `SELECT match_status, reason FROM shipping_images WHERE id=image_id`
  ——把"AI 之前怎么说"快照进事件，将来删图后仍能算 delta
- `prompt_version` 用一个模块级常量 `OCR_MATCH_PROMPT_VERSION = 'compare_rows_v1'`
  —— 改 `ocr_engine.py:COMPARE_PROMPT` 时同一处 bump

## 5. 错误处理

- `OcrMatchEvent.create` 内部 `try/except`：捕获异常后 `logger.error` 并 `return None`，**不**抛回上层
- 主端点不感知审计失败，仍按正常路径返 200/201 ——事件丢失 ≤ 写库失败的可能性可接受
- 端点层 catch 任何写入异常只 log 不抛：保证 OCR/AI/核查功能因审计失败而整体拒绝用户操作
- 与现有 `AuditLog.log` 的不抛行为保持一致

## 6. 迁移

`models/_init.py` 现有"幂等 CREATE + 增量 ALTER"模式（175-191 行）继续沿用。
新增一段：

```python
cursor.execute('''
CREATE TABLE IF NOT EXISTS ocr_match_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id INTEGER NOT NULL,
    image_id INTEGER,
    order_id INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    ocr_text TEXT, ocr_engine TEXT,
    product_name TEXT, specification TEXT,
    prompt_payload TEXT,
    ai_match_status TEXT, ai_match_score REAL,
    ai_match_reason TEXT, ai_raw_response TEXT,
    ai_engine TEXT, prompt_version TEXT,
    human_status TEXT, human_reason TEXT, human_verified_by INTEGER,
    created_at TEXT NOT NULL,
    FOREIGN KEY (record_id) REFERENCES shipping_records(id) ON DELETE CASCADE
)
''')
cursor.execute('CREATE INDEX IF NOT EXISTS idx_ocr_match_record ON ocr_match_event(record_id, event_type, created_at DESC)')
cursor.execute('CREATE INDEX IF NOT EXISTS idx_ocr_match_order  ON ocr_match_event(order_id, created_at DESC)')
```

裸 SQL、幂等、不引入新工具。

## 7. 测试

`tests/test_ocr_match_event.py`：

| 用例 | 验证 |
|---|---|
| `test_create_table_idempotent` | init_db 跑两次不报错 |
| `test_record_ocr_event_on_upload` | 行级上传成功后有 1 条 `record_ocr` 事件（image_id 非空，ai_engine='local_fuzzy'） |
| `test_record_ocr_ocr_text_empty` | OCR 提不到字（mock 返回 ''）时仍写一行，ai_status=NULL（信号"OCR 失败"≠"不符"） |
| `test_ai_match_writes_per_record` | ai-match 给 N 行写 N 条 ai_match 事件，含 prompt_payload + ai_raw_response |
| `test_human_verify_writes_event` | 人工核查后写 human_verify，含 ai_vs_human 对照 |
| `test_query_helpers_ordering` | get_by_record / get_by_order 时间升序 |
| `test_write_failure_doesnt_block` | monkeypatch OcrMatchEvent.create 抛错，端点仍 200/201 |
| `test_cascade_on_record_delete` | 删 record 时事件被清 |

**不测的**（YAGNI）：
- prompt 内容断言（依赖金标，超范围）
- 并发安全（项目单用户）
- 性能（N<10k 规模不优化）

## 8. 不在范围 / 待办

- 把现有 `shipping_images.match_status/reason` 列迁出（不迁；现状能工作）
- Backfill 历史数据（用户已拒绝）
- 出货页前端 UI 看历史（用户暂未要求）
- Dashboard / CSV 导出（数据有了再说）
