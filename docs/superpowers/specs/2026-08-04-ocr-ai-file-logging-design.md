# OCR / AI 识别文件日志 — 设计文档

- 日期：2026-08-04
- 状态：已确认，待实现
- 目标：给 OCR 识别与 AI 识别加落盘日志，出问题后可回溯排查

---

## 1. 背景与问题

### 现状

- `blueprints/ocr_engine.py` 顶部有 `logger = logging.getLogger(__name__)`，各蓝图有约 30 处
  `current_app.logger.exception(...)`。
- **但全项目没有任何 FileHandler**。所有日志只进控制台，由 `start_server.bat` 重定向到根目录
  `_server.log`（单文件、覆盖式）。关掉窗口或重启即丢失。
- `ocr_match_event` 表存了 `prompt_payload` / `ai_raw_response`，但：
  - 只覆盖「明细行标签比对」一条链路；
  - **只在成功入库时才写**。AI 报错、返回非 JSON、fallback 到本地模糊匹配这些恰恰最需要排查的场景，
    表里查不到。

### 排查不了的典型问题

1. 某张图 AI 判成红牌，想看当时发给 LLM 的完整 prompt 和原始返回 —— 失败路径没落库。
2. 整单 AI 识别（`/ai-recognize`）漏行 / 多行 —— 全链路无记录。
3. DeepSeek 偶发 429 / governor 401 —— 控制台已滚走。
4. PaddleOCR 某图提不出字 —— `extract_text` 吞异常返回 `''`，调用方只看到空字符串。

---

## 2. 需求确认（已与用户敲定）

| 决策点 | 结论 |
|---|---|
| 日志详细度 | **分级**：INFO 记精简元数据，`OCR_LOG_LEVEL=DEBUG` 时记全量 prompt / 原始返回 |
| 失败路径 | **加强项**：ERROR 无条件记全量，不受 DEBUG 开关约束 |
| 覆盖范围 | OCR/AI 专用日志 + 应用总日志，两类文件分开 |
| 旧日志处理 | 不自动清理。按月分目录，手动用 `mavis-trash` 删整个月目录 |
| 落盘路径 | 项目根 `log/YYYYMM/` 下，每天一个文件 |

---

## 3. 架构

### 3.1 核心洞察：5 个收敛点

全项目约 30 处 OCR/AI 调用点分布在 `shipping.py` / `inbound.py` / `loading.py` / `task_flow.py`，
但**全部收敛到 `ocr_engine.py` 的 5 个方法**：

| 方法 | 覆盖的调用 |
|---|---|
| `MoonshotEngine.recognize` | 云端视觉整单识别 |
| `PaddleOCREngine.extract_text` | 所有行级标签提文字 |
| `PaddleOCREngine.recognize` | 本地整单识别 |
| `DeepSeekEngine._call_api_with_prompt` | `compare_rows` + `compare_single_record` 的实际 HTTP 调用 |
| `DeepSeekEngine.recognize` | PaddleOCR→DeepSeek 组合整单识别 |

**因此在这 5 处加装饰器即可全覆盖，且未来新增调用点自动获得日志。**

### 3.2 方案选择

已评估三个方案：

- **A. 只在 5 个引擎方法加装饰器** —— 全覆盖、改动集中，但缺业务上下文（不知道是哪个订单）。
- **B. 在 30 个蓝图调用点手写 `logger.info`** —— 有业务上下文，但改动分散、易漏、新代码作者必须记得加。
- **C.（采纳）A + 轻量上下文注入** —— 装饰器打底保证全覆盖；另在少数 AI 入口把
  `order_id` / `record_id` 塞进 `flask.g`，装饰器自动带上。兼得覆盖率与可读性。

### 3.3 新增文件

| 文件 | 预估行数 | 职责 |
|---|---|---|
| `logging_setup.py`（项目根） | ~80 | `init_logging()`：建目录、装 handler、按 env 定级别 |
| `blueprints/ocr_log.py` | ~90 | `@log_ocr_call()` 装饰器 + trace_id + 上下文 + 截断/脱敏 |

**放置理由**：`logging_setup.py` 放根目录，因为它是应用级基础设施，由 `app.py` 的 `create_app()`
调用，不属于任何蓝图；`ocr_log.py` 放 `blueprints/`，遵循已有的 `_helpers.py` / `ocr_engine.py`
「非蓝图辅助模块」惯例，且被 `ocr_engine.py` 直接依赖。

### 3.4 现有文件改动

| 文件 | 改动 |
|---|---|
| `app.py` | `create_app()` 内调用 `init_logging(app)`（1 行 + 1 import） |
| `blueprints/ocr_engine.py` | 5 个方法各加 1 行 `@log_ocr_call(...)` + 1 import |
| `blueprints/shipping.py` / `inbound.py` / `loading.py` | 在 AI 入口调 `set_log_context(...)`，每文件 2~3 处 |
| `.gitignore` | 加 `log/` |
| `.env.example` | 加 `OCR_LOG_LEVEL` 说明 |

---

## 4. 落盘结构

```
log/
├── 202608/
│   ├── ocr-2026-08-04.log     ← OCR/AI 专用
│   ├── app-2026-08-04.log     ← Flask 其他错误
│   ├── ocr-2026-08-05.log
│   └── app-2026-08-05.log
└── 202609/
    └── ...
```

### DailyFolderHandler

自定义 handler，继承 `logging.FileHandler`：

- 每次 `emit()` 前比对当前日期字符串，跨天则关闭旧流、`os.makedirs` 新月目录、打开新文件。
- **不使用 `TimedRotatingFileHandler`** —— 它靠「重命名旧文件」实现轮转，无法把文件分散到不同的
  月份子目录，跨目录会与命名规则打架。
- `encoding='utf-8'` 硬编码（见 CLAUDE.md 踩坑点 7：Windows 下默认 GBK 会让中文日志报
  `UnicodeEncodeError`）。
- `delay=True`，首次写入时才创建文件，避免启动就产生空文件。

### Logger 与 Handler 绑定

| Logger 名 | Handler | 级别 |
|---|---|---|
| `ocr`（含 `ocr.moonshot` / `ocr.paddle` / `ocr.deepseek` 子 logger） | `ocr-YYYY-MM-DD.log` | `OCR_LOG_LEVEL`，默认 INFO |
| root（Flask `app.logger` 挂在其下） | `app-YYYY-MM-DD.log` | WARNING |

`ocr` logger 设 `propagate = False`，避免 OCR 日志同时写进 app 总日志造成重复。

控制台 handler 保留（开发时直接看终端仍然方便）。

---

## 5. 日志级别语义

| 场景 | 默认（INFO） | `OCR_LOG_LEVEL=DEBUG` |
|---|---|---|
| 调用成功 | 一行摘要：引擎 / 模型 / 图片 / 耗时 / 结果状态 | 摘要 + 完整 prompt + 原始返回 + OCR 原文 |
| **调用失败** | **完整 prompt + 原始返回 + 堆栈（无条件）** | 同左 |

**失败路径不受 DEBUG 开关约束**，这是相对纯「分级」方案的关键加强：偶发问题（如 DeepSeek 间歇性
429、某张图偶尔判错）往往不可复现，若要求「先开 DEBUG 再复现」就等于抓不到。

### 5.1 失败的判定（重要）

现有 5 个方法中有 3 个**吞掉异常、用返回值表示失败**，装饰器不能只靠 `try/except` 判断：

| 方法 | 失败表现 |
|---|---|
| `PaddleOCREngine.extract_text` | `except Exception` 后 `return ''` —— 空字符串即失败 |
| `PaddleOCREngine.recognize` | 返回 `{'success': False, 'error': ...}` |
| `MoonshotEngine.recognize` | 返回 `{'success': False, 'error': ...}` |
| `DeepSeekEngine._call_api_with_prompt` | **抛异常**（`openai.APIStatusError` 等） |
| `DeepSeekEngine.recognize` | 返回 `{'success': False, ...}` |

因此装饰器需要一个 `failure_check` 参数，按方法传入不同的返回值判定函数：

```python
@log_ocr_call('ocr.paddle', evt='extract_text',
              failed_if=lambda r: not r)               # 空串 = 失败
@log_ocr_call('ocr.moonshot', evt='recognize',
              failed_if=lambda r: not r.get('success'))  # success=False = 失败
```

异常与「返回值表示失败」两条路径都走 ERROR 全量记录。

---

## 6. trace_id 串联

一次行级图片上传会产生多条日志（PaddleOCR 提文字 → DeepSeek 比对 → 可能的本地 fallback），
需要串起来看。

- 每个请求生成 8 位十六进制 ID，存入 `flask.g`。
- 请求上下文外（如脚本直接调引擎）回退到 `-`。
- 高 4 位由进程 PID 派生、低 4 位随机，因此**同一进程的 ID 共享前缀**，可据此区分 Flask debug
  模式 reloader 起的两个进程（它们写同一文件）。格式仍是 8 位十六进制，不额外加后缀。

### 输出样例

```
14:23:09 INFO  [a3f91c2e] ocr.paddle   evt=extract_text img=2026-08/a.jpg size=482KB
                                       chars=37 elapsed=2.31s
14:23:11 INFO  [a3f91c2e] ocr.deepseek evt=compare_single model=deepseek-v4-flash
                                       order=610 record=88 result=green elapsed=1.84s
14:25:03 ERROR [b7c02d19] ocr.deepseek evt=compare_single model=deepseek-v4-flash
                                       order=611 record=92
                                       APIStatusError: DeepSeek 服务异常(429): ...
                                       prompt=<全文 3.2KB>
                                       raw=<全文>
                                       Traceback (most recent call last): ...
```

---

## 7. 安全与边界

| 项 | 处理 |
|---|---|
| 图片 base64 | **绝不入日志**。Moonshot 走 data-url，单图 base64 数百 KB，写进去日志立刻膨胀。只记文件名 + 字节数 |
| API key | 输出前用正则 scrub `sk-[A-Za-z0-9\-_]{8,}` 兜底（key 走 header 不走 prompt，但防御性处理） |
| 日志写入失败 | handler 异常必须**不能阻断业务**。`logging.raiseExceptions = False`，且装饰器内部对日志调用本身包 try/except |
| 字段截断 | INFO 级别**完全不输出** prompt / 原始返回（见 §5），仅对确实要记的自由文本字段（如 OCR 原文、AI reason）截断到 200 字符。DEBUG 与 ERROR 全部不截断 |
| `log/` 入库 | 加入 `.gitignore` |
| 并发交错 | Flask 默认 threaded，多请求日志会交错 —— 靠 trace_id 区分，不加锁 |

---

## 8. 配置

`.env` 新增（可选，不配则用默认值）：

```
# OCR/AI 日志级别：INFO（默认，只记摘要）| DEBUG（记完整 prompt 与原始返回）
OCR_LOG_LEVEL=INFO
```

---

## 9. 测试计划

新增 `tests/test_ocr_logging.py`：

**DailyFolderHandler**
1. 路径计算正确：`2026-08-04` → `log/202608/ocr-2026-08-04.log`
2. 跨天自动切文件（注入假日期，不依赖真实时钟）
3. 新月份自动创建子目录
4. 中文内容写入不报 `UnicodeEncodeError`

**装饰器行为**
5. 成功 + INFO → 只有摘要，日志中**不含** prompt 全文
6. 成功 + DEBUG → 含 prompt 与原始返回全文
7. 抛异常 + INFO → **仍含**全量 prompt / 返回 / 堆栈（验证「ERROR 无条件全量」）
8. 返回 `{'success': False}` + INFO → 判定为失败，记 ERROR 全量
9. `extract_text` 返回 `''` + INFO → 判定为失败，记 ERROR
10. 装饰器不改变原方法返回值（透明性）
11. 日志系统抛异常时业务调用不受影响

**安全**
12. 形如 `sk-xxxx` 的字符串被 scrub
13. 图片字节 / base64 不出现在日志中

**集成**
14. trace_id 在同一请求的多条日志中一致

---

## 10. 验收标准

- [ ] 启动应用后触发一次行级图片上传，`log/202608/ocr-2026-08-04.log` 出现该次调用的
      PaddleOCR 与 DeepSeek 两条 INFO 摘要，trace_id 相同
- [ ] 故意把 `DEEPSEEK_API_KEY` 改错触发失败，日志中出现 ERROR + 完整 prompt + 堆栈（DEBUG 未开）
- [ ] 设 `OCR_LOG_LEVEL=DEBUG` 重启，成功调用的日志中出现完整 prompt
- [ ] `app-YYYY-MM-DD.log` 能收到蓝图里 `current_app.logger.exception` 的输出
- [ ] `git status` 中不出现 `log/`
- [ ] 全部测试通过：`python -m pytest tests/ -v`
- [ ] 现有测试无回归

---

## 11. 明确不做（YAGNI）

- 日志自动清理 / 按大小切块 —— 用户选择手动删月目录
- 日志查看 Web 界面 —— 直接看文件即可，已有 `/audit/ocr-events` 页面覆盖统计需求
- 结构化 JSON 日志 —— 单人排查场景下人类可读优先
- 异步日志写入 —— OCR 调用本身秒级，同步写盘开销可忽略
- 把日志内容也写进数据库 —— `ocr_match_event` 表已覆盖成功路径的审计需求，不重复
