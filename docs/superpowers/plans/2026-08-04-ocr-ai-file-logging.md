# OCR / AI 识别文件日志 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给 OCR / AI 识别加落盘日志，写到项目根 `log/YYYYMM/` 下按天分文件，出问题后可回溯完整 prompt 与原始返回。

**Architecture:** 全项目约 30 处 OCR/AI 调用点全部收敛到 `blueprints/ocr_engine.py` 的 5 个方法，在这 5 处加装饰器即可全覆盖。新增 `logging_setup.py`（自定义 `DailyFolderHandler` + trace_id 基础设施）与 `blueprints/ocr_log.py`（装饰器 + 上下文 + 脱敏）两个文件，现有文件只做极小改动。

**Tech Stack:** Python 3.12 标准库 `logging` + `contextvars`，Flask 3.1.3，pytest。**不引入任何新依赖。**

**Spec:** `docs/superpowers/specs/2026-08-04-ocr-ai-file-logging-design.md`

## Global Constraints

- **不新增第三方依赖。** 项目目前只有 5 个直接依赖，本功能全部用标准库实现。
- **文件编码硬编码 `encoding='utf-8'`。** Windows 默认 GBK，中文日志会 `UnicodeEncodeError`（CLAUDE.md 踩坑点 7）。
- **日志失败绝不阻断业务。** 所有日志代码包 try/except，并设 `logging.raiseExceptions = False`。
- **图片字节 / base64 绝不写入日志。** 只记文件名与字节数。
- **装饰器必须对被包装方法完全透明** —— 返回值原样返回，异常原样抛出。
- **不要用 `Remove-Item -Force` 删任何文件**，需要删时用 `mavis-trash`（CLAUDE.md 踩坑点 4）。
- 测试命令统一 `python -m pytest tests/ -v`；PowerShell 下如遇中文编码问题加 `$env:PYTHONUTF8=1`。

## 相对 Spec 的两处修订

实现前调研发现的问题，已折进本计划：

1. **trace_id 必须用 `contextvars.ContextVar`，不能用 `flask.g`。**
   Spec §6 原写「存入 `flask.g`」。但 `blueprints/shipping.py:126` 的
   `_spawn_record_image_processing` 把行级图片 OCR 丢进 **daemon 线程**执行，那里没有
   Flask 请求上下文，`flask.g` 不可用 —— 而这正是最主要的 OCR 路径。
   改用 `ContextVar`，并在起线程时用 `contextvars.copy_context().run(...)` 显式传播
   （`threading.Thread` 不会自动传播 contextvars）。业务上下文（order_id / record_id）同理。

2. **trace_id 相关代码放 `logging_setup.py` 而非 `ocr_log.py`。**
   Spec §3.3 把 trace_id 划给 `ocr_log.py`。改为放 `logging_setup.py`，因为 root logger 的
   formatter 也要用它，放这里避免 `logging_setup → ocr_log` 的反向依赖。分层更干净：
   `logging_setup` = 基础设施，`ocr_log` = 消费方。

## File Structure

| 文件 | 状态 | 职责 |
|---|---|---|
| `logging_setup.py` | 新建 | `DailyFolderHandler` / `TraceIdFilter` / trace_id ContextVar / `init_logging()` |
| `blueprints/ocr_log.py` | 新建 | `@log_ocr_call` 装饰器 / 业务上下文 ContextVar / 脱敏截断 / 预置 payload 提取器 |
| `tests/test_ocr_logging.py` | 新建 | 全部单元测试 |
| `app.py` | 改 | `create_app()` 内调 `init_logging(app)` |
| `blueprints/ocr_engine.py` | 改 | 5 个方法加装饰器；模块 logger 改名到 `ocr` 树下 |
| `blueprints/shipping.py` | 改 | 后台线程传播 contextvars；3 个 AI 入口设业务上下文 |
| `blueprints/inbound.py` / `loading.py` | 改 | AI 入口设业务上下文 |
| `.gitignore` | 改 | 加 `log/` |
| `.env.example` | 改 | 加 `OCR_LOG_LEVEL` 说明 |

---

### Task 1: DailyFolderHandler —— 按月目录、按天切文件

**Files:**
- Create: `logging_setup.py`
- Test: `tests/test_ocr_logging.py`

**Interfaces:**
- Consumes: 无（本任务是根基）
- Produces:
  - `DailyFolderHandler(prefix: str, log_root: str | None = None)` —— `logging.FileHandler` 子类
  - `LOG_ROOT: str` —— 项目根下 `log/` 的绝对路径
  - 实例方法 `_today() -> datetime.date`（测试通过实例属性覆盖来注入假日期）
  - 实例方法 `_path_for(day: date) -> str`

- [ ] **Step 1: 写失败测试**

创建 `tests/test_ocr_logging.py`：

```python
"""OCR/AI 文件日志测试。"""
import logging
import os
from datetime import date

import pytest

from logging_setup import LOG_ROOT, DailyFolderHandler


def _make_handler(tmp_path, prefix='ocr', day=date(2026, 8, 4)):
    """建一个日期被钉死的 handler，写到 tmp_path。"""
    h = DailyFolderHandler(prefix, log_root=str(tmp_path))
    h._today = lambda: day          # 实例属性遮蔽方法，注入假日期
    h._current_day = day
    h.baseFilename = h._path_for(day)
    return h


def _emit(handler, msg, level=logging.INFO):
    rec = logging.LogRecord('ocr.test', level, __file__, 1, msg, None, None)
    handler.emit(rec)
    handler.flush()


def test_log_root_points_at_project_root_log_dir():
    assert os.path.basename(LOG_ROOT) == 'log'
    # LOG_ROOT 的父目录应含 app.py（项目根）
    assert os.path.isfile(os.path.join(os.path.dirname(LOG_ROOT), 'app.py'))


def test_path_uses_yyyymm_folder_and_dated_filename(tmp_path):
    h = _make_handler(tmp_path)
    path = h._path_for(date(2026, 8, 4))
    assert path == os.path.join(str(tmp_path), '202608', 'ocr-2026-08-04.log')
    h.close()


def test_emit_creates_month_folder_and_writes(tmp_path):
    h = _make_handler(tmp_path)
    _emit(h, 'hello')
    h.close()
    target = tmp_path / '202608' / 'ocr-2026-08-04.log'
    assert target.exists()
    assert 'hello' in target.read_text(encoding='utf-8')


def test_rolls_to_new_file_when_day_changes(tmp_path):
    h = _make_handler(tmp_path)
    _emit(h, 'day-one')
    h._today = lambda: date(2026, 8, 5)      # 跨天
    _emit(h, 'day-two')
    h.close()
    d1 = tmp_path / '202608' / 'ocr-2026-08-04.log'
    d2 = tmp_path / '202608' / 'ocr-2026-08-05.log'
    assert 'day-one' in d1.read_text(encoding='utf-8')
    assert 'day-one' not in d2.read_text(encoding='utf-8')
    assert 'day-two' in d2.read_text(encoding='utf-8')


def test_rolls_into_new_month_folder(tmp_path):
    h = _make_handler(tmp_path)
    _emit(h, 'august')
    h._today = lambda: date(2026, 9, 1)      # 跨月
    _emit(h, 'september')
    h.close()
    assert (tmp_path / '202609' / 'ocr-2026-09-01.log').exists()
    assert 'september' in (tmp_path / '202609' / 'ocr-2026-09-01.log').read_text(encoding='utf-8')


def test_chinese_content_written_as_utf8(tmp_path):
    """Windows 默认 GBK 会炸，必须显式 utf-8（CLAUDE.md 踩坑点 7）。"""
    h = _make_handler(tmp_path)
    _emit(h, '杂胶 加面 1.2mm 环保 ⊛ ✓')
    h.close()
    text = (tmp_path / '202608' / 'ocr-2026-08-04.log').read_text(encoding='utf-8')
    assert '杂胶 加面 1.2mm 环保 ⊛ ✓' in text


def test_delay_true_no_file_until_first_emit(tmp_path):
    h = DailyFolderHandler('ocr', log_root=str(tmp_path))
    # 月目录会被建出来（_path_for 里 makedirs），但日志文件不应存在
    assert not any(tmp_path.rglob('*.log'))
    h.close()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_ocr_logging.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'logging_setup'`

- [ ] **Step 3: 实现 DailyFolderHandler**

创建 `logging_setup.py`：

```python
"""应用日志落盘基础设施。

日志写到项目根 log/YYYYMM/ 下，每天一个文件：
    log/202608/ocr-2026-08-04.log   —— OCR/AI 专用
    log/202608/app-2026-08-04.log   —— Flask 其他错误

不用 TimedRotatingFileHandler：它靠「重命名旧文件」轮转，无法把文件分散到
不同月份子目录，跨目录会与命名规则打架。
"""
import logging
import os
from datetime import date

# 项目根下的 log/ —— 本文件就在项目根
LOG_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'log')


class DailyFolderHandler(logging.FileHandler):
    """按天切文件、按月分目录的 FileHandler。

    每次 emit 前比对当前日期，跨天则关闭旧流、建新月目录、开新文件。
    """

    def __init__(self, prefix, log_root=None):
        self.prefix = prefix
        self.log_root = log_root or LOG_ROOT
        day = self._today()
        self._current_day = day
        # delay=True：首次写入时才建文件，避免启动就产生空文件
        super().__init__(self._path_for(day), encoding='utf-8', delay=True)

    def _today(self):
        """独立成方法，方便测试注入假日期。"""
        return date.today()

    def _path_for(self, day):
        folder = os.path.join(self.log_root, day.strftime('%Y%m'))
        os.makedirs(folder, exist_ok=True)
        return os.path.join(folder, '%s-%s.log' % (self.prefix, day.isoformat()))

    def emit(self, record):
        try:
            day = self._today()
            if day != self._current_day:
                self._current_day = day
                if self.stream is not None:
                    self.stream.close()
                    self.stream = None
                self.baseFilename = self._path_for(day)
        except Exception:
            pass          # 切文件失败也要让这条日志尽力写出去
        super().emit(record)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_ocr_logging.py -v`
Expected: PASS（8 passed）

- [ ] **Step 5: 提交**

```bash
git add logging_setup.py tests/test_ocr_logging.py
git commit -m "feat(log): DailyFolderHandler 按月目录按天切文件"
```

---

### Task 2: trace_id 与 init_logging()

**Files:**
- Modify: `logging_setup.py`（追加）
- Test: `tests/test_ocr_logging.py`（追加）

**Interfaces:**
- Consumes: Task 1 的 `DailyFolderHandler`、`LOG_ROOT`
- Produces:
  - `new_trace_id() -> str` —— 生成并写入 ContextVar，返回 8 位十六进制
  - `get_trace_id() -> str` —— 读 ContextVar，未设置返回 `'-'`
  - `TraceIdFilter` —— `logging.Filter` 子类，给无 `trace_id` 属性的 record 补上
  - `init_logging(app=None, log_root=None) -> logging.Logger` —— 幂等；返回 `ocr` logger

**为什么用 ContextVar 而不是 `flask.g`：** 行级图片 OCR 跑在 daemon 线程里（`shipping.py:126`），
那里没有请求上下文。ContextVar 配合 `contextvars.copy_context()` 能跨线程传播，`flask.g` 不能。

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_ocr_logging.py`：

```python
import contextvars
import threading

from logging_setup import (TraceIdFilter, get_trace_id, init_logging,
                           new_trace_id)


def test_trace_id_defaults_to_dash_when_unset():
    def probe():
        return get_trace_id()
    # 新线程 = 全新 context，ContextVar 取默认值
    box = {}
    t = threading.Thread(target=lambda: box.update(v=probe()))
    t.start(); t.join()
    assert box['v'] == '-'


def test_new_trace_id_is_8_hex_chars():
    def run():
        tid = new_trace_id()
        assert len(tid) == 8
        assert all(c in '0123456789abcdef' for c in tid)
    contextvars.copy_context().run(run)


def test_trace_id_stable_within_same_context():
    def run():
        first = new_trace_id()
        assert get_trace_id() == first
        assert get_trace_id() == first
    contextvars.copy_context().run(run)


def test_same_process_trace_ids_share_pid_prefix():
    """高 4 位由 PID 派生，用于区分 Flask reloader 的两个进程。"""
    ids = []
    for _ in range(5):
        contextvars.copy_context().run(lambda: ids.append(new_trace_id()))
    assert len({tid[:4] for tid in ids}) == 1        # 前缀一致
    assert len({tid[4:] for tid in ids}) > 1         # 后缀随机


def test_trace_id_propagates_into_background_thread():
    """行级图片 OCR 跑在后台线程，必须能带上同一个 trace_id。"""
    box = {}

    def outer():
        parent = new_trace_id()
        ctx = contextvars.copy_context()
        t = threading.Thread(target=ctx.run, args=(lambda: box.update(child=get_trace_id()),))
        t.start(); t.join()
        box['parent'] = parent

    contextvars.copy_context().run(outer)
    assert box['child'] == box['parent']


def test_trace_id_filter_injects_attribute():
    rec = logging.LogRecord('ocr.test', logging.INFO, __file__, 1, 'x', None, None)
    assert not hasattr(rec, 'trace_id')
    TraceIdFilter().filter(rec)
    assert rec.trace_id == '-'


def test_init_logging_writes_ocr_and_app_files(tmp_path):
    init_logging(log_root=str(tmp_path))
    logging.getLogger('ocr.deepseek').info('ocr-line')
    logging.getLogger('blueprints.shipping').error('app-line')
    for h in logging.getLogger('ocr').handlers + logging.getLogger().handlers:
        h.flush()

    ocr_files = list(tmp_path.rglob('ocr-*.log'))
    app_files = list(tmp_path.rglob('app-*.log'))
    assert ocr_files and app_files
    ocr_text = ocr_files[0].read_text(encoding='utf-8')
    app_text = app_files[0].read_text(encoding='utf-8')
    assert 'ocr-line' in ocr_text
    assert 'app-line' in app_text
    # propagate=False：OCR 日志不应重复出现在 app 日志里
    assert 'ocr-line' not in app_text


def test_init_logging_is_idempotent(tmp_path):
    """Flask reloader / 重复 create_app 不应叠加 handler 造成重复行。"""
    init_logging(log_root=str(tmp_path))
    init_logging(log_root=str(tmp_path))
    init_logging(log_root=str(tmp_path))
    ocr_logger = logging.getLogger('ocr')
    file_handlers = [h for h in ocr_logger.handlers if isinstance(h, DailyFolderHandler)]
    assert len(file_handlers) == 1


def test_app_log_ignores_info_level(tmp_path):
    """root 设 WARNING，werkzeug 的 INFO 请求日志不该淹没 app 日志。"""
    init_logging(log_root=str(tmp_path))
    logging.getLogger('werkzeug').info('GET /shipping-records 200')
    for h in logging.getLogger().handlers:
        h.flush()
    app_files = list(tmp_path.rglob('app-*.log'))
    text = app_files[0].read_text(encoding='utf-8') if app_files else ''
    assert 'GET /shipping-records' not in text
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_ocr_logging.py -v`
Expected: FAIL —— `ImportError: cannot import name 'init_logging' from 'logging_setup'`

- [ ] **Step 3: 实现**

追加到 `logging_setup.py`（放在 `DailyFolderHandler` 之后）：

```python
import random
from contextvars import ContextVar

# ═══════════════════════════════════════════════════════════════════
# trace_id：串联一次请求内的多条 OCR 日志
#
# 用 ContextVar 而非 flask.g —— 行级图片 OCR 跑在 daemon 线程里
# (blueprints/shipping.py 的 _spawn_record_image_processing)，那里没有
# Flask 请求上下文。ContextVar 配合 contextvars.copy_context() 可跨线程传播。
# ═══════════════════════════════════════════════════════════════════

_TRACE_ID = ContextVar('worklog_trace_id', default=None)

# 高 4 位由 PID 派生：Flask debug 模式 reloader 会起两个进程写同一文件，
# 靠这个前缀区分是哪个进程写的。
_PID_PREFIX = '%04x' % (os.getpid() & 0xFFFF)


def new_trace_id():
    """生成新 trace_id 并写入当前 context，返回之。"""
    tid = _PID_PREFIX + '%04x' % random.getrandbits(16)
    _TRACE_ID.set(tid)
    return tid


def get_trace_id():
    """读当前 context 的 trace_id；未设置返回 '-'。"""
    try:
        return _TRACE_ID.get() or '-'
    except Exception:
        return '-'


class TraceIdFilter(logging.Filter):
    """给没有 trace_id 属性的 record 补上，否则 formatter 会 KeyError。"""

    def filter(self, record):
        if not hasattr(record, 'trace_id'):
            record.trace_id = get_trace_id()
        return True


# ═══════════════════════════════════════════════════════════════════
# 日志系统初始化
# ═══════════════════════════════════════════════════════════════════

_FORMAT = '%(asctime)s %(levelname)-5s [%(trace_id)s] %(name)-14s %(message)s'
_DATEFMT = '%Y-%m-%d %H:%M:%S'


def _drop_our_handlers(logger):
    """移除本模块之前装过的 handler —— 保证 init_logging 幂等。"""
    for h in list(logger.handlers):
        if isinstance(h, DailyFolderHandler) or getattr(h, '_worklog_console', False):
            logger.removeHandler(h)
            try:
                h.close()
            except Exception:
                pass


def init_logging(app=None, log_root=None):
    """装好文件日志。幂等 —— 重复调用不会叠加 handler。

    返回 'ocr' logger。
    """
    # 日志系统自身出错时不要往 stderr 喷、更不要抛给业务
    logging.raiseExceptions = False

    fmt = logging.Formatter(_FORMAT, datefmt=_DATEFMT)
    trace_filter = TraceIdFilter()

    # --- OCR/AI 专用日志 ---
    level_name = os.environ.get('OCR_LOG_LEVEL', 'INFO').strip().upper()
    ocr_level = getattr(logging, level_name, logging.INFO)
    if not isinstance(ocr_level, int):
        ocr_level = logging.INFO

    ocr_logger = logging.getLogger('ocr')
    _drop_our_handlers(ocr_logger)
    ocr_logger.setLevel(ocr_level)
    ocr_logger.propagate = False        # 不冒泡到 root，避免写进 app 日志重复一份

    ocr_file = DailyFolderHandler('ocr', log_root)
    ocr_file.setFormatter(fmt)
    ocr_file.addFilter(trace_filter)
    ocr_file.setLevel(logging.DEBUG)    # 级别由 logger 控制，handler 全放行
    ocr_logger.addHandler(ocr_file)

    console = logging.StreamHandler()
    console._worklog_console = True
    console.setFormatter(fmt)
    console.addFilter(trace_filter)
    ocr_logger.addHandler(console)      # 开发时终端仍然直接可见

    # --- 应用总日志（Flask/蓝图的 current_app.logger.exception 等）---
    root = logging.getLogger()
    _drop_our_handlers(root)
    root.setLevel(logging.WARNING)      # 挡掉 werkzeug 的 INFO 请求流水
    app_file = DailyFolderHandler('app', log_root)
    app_file.setFormatter(fmt)
    app_file.addFilter(trace_filter)
    app_file.setLevel(logging.WARNING)
    root.addHandler(app_file)

    # --- 每个请求开头生成 trace_id ---
    if app is not None:
        @app.before_request
        def _assign_trace_id():
            new_trace_id()

    return ocr_logger
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_ocr_logging.py -v`
Expected: PASS（17 passed）

- [ ] **Step 5: 提交**

```bash
git add logging_setup.py tests/test_ocr_logging.py
git commit -m "feat(log): trace_id ContextVar + init_logging 双文件日志"
```

---

### Task 3: 业务上下文与脱敏工具

**Files:**
- Create: `blueprints/ocr_log.py`
- Test: `tests/test_ocr_logging.py`（追加）

**Interfaces:**
- Consumes: 无。trace_id 由 `logging_setup.TraceIdFilter` 在 handler 层注入到每条 record，
  本模块不需要读它。
- Produces:
  - `set_log_context(**kw) -> None` —— 写业务上下文到 ContextVar（`None` 值忽略）
  - `get_log_context() -> dict` —— 读当前上下文副本
  - `clear_log_context() -> None`
  - `scrub(text: str) -> str` —— 抹掉形如 `sk-xxxx` 的密钥
  - `trunc(value, limit: int = 200) -> str` —— 超长截断并标注原长度

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_ocr_logging.py`：

```python
from blueprints.ocr_log import (clear_log_context, get_log_context, scrub,
                                set_log_context, trunc)


def test_log_context_roundtrip():
    def run():
        assert get_log_context() == {}
        set_log_context(order_id=610, record_id=88)
        assert get_log_context() == {'order_id': 610, 'record_id': 88}
        set_log_context(image_id=7)
        assert get_log_context()['image_id'] == 7
        assert get_log_context()['order_id'] == 610      # 累加不覆盖
        clear_log_context()
        assert get_log_context() == {}
    contextvars.copy_context().run(run)


def test_log_context_ignores_none_values():
    def run():
        set_log_context(order_id=610, record_id=None)
        assert get_log_context() == {'order_id': 610}
    contextvars.copy_context().run(run)


def test_log_context_returns_copy_not_live_dict():
    def run():
        set_log_context(order_id=610)
        snapshot = get_log_context()
        snapshot['order_id'] = 999
        assert get_log_context()['order_id'] == 610
    contextvars.copy_context().run(run)


def test_log_context_propagates_into_background_thread():
    box = {}

    def outer():
        set_log_context(order_id=610, record_id=88)
        ctx = contextvars.copy_context()
        t = threading.Thread(target=ctx.run, args=(lambda: box.update(c=get_log_context()),))
        t.start(); t.join()

    contextvars.copy_context().run(outer)
    assert box['c'] == {'order_id': 610, 'record_id': 88}


def test_scrub_removes_api_keys():
    assert 'sk-abcd1234efgh5678' not in scrub('Bearer sk-abcd1234efgh5678 rest')
    assert 'sk-***' in scrub('Bearer sk-abcd1234efgh5678 rest')
    assert scrub('Bearer sk-abcd1234efgh5678 rest').endswith(' rest')


def test_scrub_leaves_normal_text_alone():
    text = '杂胶 加面 1.2mm sk-短'          # 太短不像 key，不动
    assert scrub(text) == text


def test_scrub_handles_non_string():
    assert scrub(None) == ''
    assert scrub(123) == '123'


def test_trunc_cuts_long_text_and_marks_length():
    out = trunc('x' * 500, 200)
    assert len(out) < 260
    assert out.startswith('x' * 200)
    assert '500' in out              # 标注原始长度


def test_trunc_leaves_short_text_intact():
    assert trunc('hello', 200) == 'hello'
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_ocr_logging.py -v`
Expected: FAIL —— `ModuleNotFoundError: No module named 'blueprints.ocr_log'`

- [ ] **Step 3: 实现**

创建 `blueprints/ocr_log.py`：

```python
"""OCR/AI 调用日志：装饰器 + 业务上下文 + 脱敏截断。

用法见 blueprints/ocr_engine.py —— 5 个引擎方法各加一行装饰器，
全项目约 30 处调用点即全部覆盖。
"""
import re
from contextvars import ContextVar

# 业务上下文用 ContextVar 而非 flask.g —— 理由同 trace_id，见 logging_setup.py
_LOG_CTX = ContextVar('worklog_ocr_log_ctx', default=None)

# 形如 sk-xxxxxxxx 的密钥（key 走 header 不走 prompt，这里是防御性兜底）
_KEY_RE = re.compile(r'sk-[A-Za-z0-9\-_]{8,}')


def set_log_context(**kw):
    """把业务字段（order_id / record_id / image_id 等）挂到当前 context。

    累加语义：多次调用会合并，值为 None 的键忽略。
    """
    try:
        current = _LOG_CTX.get() or {}
        merged = dict(current)
        merged.update({k: v for k, v in kw.items() if v is not None})
        _LOG_CTX.set(merged)
    except Exception:
        pass


def get_log_context():
    """返回当前业务上下文的副本。"""
    try:
        return dict(_LOG_CTX.get() or {})
    except Exception:
        return {}


def clear_log_context():
    try:
        _LOG_CTX.set(None)
    except Exception:
        pass


def scrub(text):
    """抹掉疑似 API key。非字符串一律转字符串，None 转空串。"""
    if text is None:
        return ''
    if not isinstance(text, str):
        text = str(text)
    return _KEY_RE.sub('sk-***', text)


def trunc(value, limit=200):
    """超长截断，并标注原始长度，避免 INFO 行被自由文本撑爆。"""
    text = scrub(value)
    if len(text) <= limit:
        return text
    return '%s…<共%d字符>' % (text[:limit], len(text))
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_ocr_logging.py -v`
Expected: PASS（26 passed）

- [ ] **Step 5: 提交**

```bash
git add blueprints/ocr_log.py tests/test_ocr_logging.py
git commit -m "feat(log): OCR 业务上下文 ContextVar + 脱敏截断工具"
```

---

### Task 4: @log_ocr_call 装饰器

这是整个功能的核心。**要点：5 个被包装方法里有 4 个吞异常、用返回值表示失败**
（`extract_text` 失败返回 `''`，`recognize` 失败返回 `{'success': False}`），
只有 `_call_api_with_prompt` 真抛异常。所以不能只靠 try/except 判定失败。

**Files:**
- Modify: `blueprints/ocr_log.py`（追加）
- Test: `tests/test_ocr_logging.py`（追加）

**Interfaces:**
- Consumes: Task 3 的 `get_log_context` / `scrub` / `trunc`
- Produces:
  - `log_ocr_call(logger_name, evt, failed_if=None, payload=None, outcome=None)` —— 装饰器工厂
    - `logger_name: str` —— 如 `'ocr.deepseek'`
    - `evt: str` —— 事件名，如 `'extract_text'`
    - `failed_if: Callable[[Any], bool] | None` —— 由返回值判定失败
    - `payload: Callable[..., tuple[dict, dict]] | None` —— 收原方法的 `*args, **kwargs`（含 `self`），返回 `(summary, detail)`
    - `outcome: Callable[[Any], tuple[dict, dict]] | None` —— 收返回值，返回 `(summary, detail)`
  - 预置提取器：
    - `image_payload(self, image_bytes, filename='', *a, **kw)`
    - `prompt_payload(self, prompt_text, *a, **kw)`
    - `text_outcome(result)` —— 给 `extract_text` 用
    - `items_outcome(result)` —— 给 `recognize` 用
    - `parsed_outcome(result)` —— 给 `_call_api_with_prompt` 用

**语义**（对应 spec §5）：

| 场景 | INFO | DEBUG |
|---|---|---|
| 成功 | 只写 summary 单行 | summary + detail（完整 prompt / 原始返回 / OCR 原文） |
| 失败（抛异常 **或** `failed_if` 为真） | **summary + 全部 detail + 堆栈**，级别 ERROR，不受开关约束 | 同左 |

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_ocr_logging.py`：

```python
from blueprints.ocr_log import (image_payload, items_outcome, log_ocr_call,
                                parsed_outcome, prompt_payload, text_outcome)


@pytest.fixture
def logdir(tmp_path, monkeypatch):
    """每个测试独立的日志目录，返回读取 ocr 日志全文的函数。"""
    monkeypatch.setenv('OCR_LOG_LEVEL', 'INFO')
    init_logging(log_root=str(tmp_path))

    def read():
        for h in logging.getLogger('ocr').handlers:
            h.flush()
        files = list(tmp_path.rglob('ocr-*.log'))
        return files[0].read_text(encoding='utf-8') if files else ''

    return read


@pytest.fixture
def logdir_debug(tmp_path, monkeypatch):
    monkeypatch.setenv('OCR_LOG_LEVEL', 'DEBUG')
    init_logging(log_root=str(tmp_path))

    def read():
        for h in logging.getLogger('ocr').handlers:
            h.flush()
        files = list(tmp_path.rglob('ocr-*.log'))
        return files[0].read_text(encoding='utf-8') if files else ''

    return read


LONG_PROMPT = '请比对以下标签与明细行：' + 'A' * 3000


class _FakeEngine:
    """模拟 5 种被包装方法的行为。"""

    @log_ocr_call('ocr.fake', evt='ok_call', payload=prompt_payload,
                  outcome=parsed_outcome)
    def ok_call(self, prompt_text, multi=False):
        return {'match_status': 'green', 'reason': '完全一致'}

    @log_ocr_call('ocr.fake', evt='raiser', payload=prompt_payload,
                  outcome=parsed_outcome)
    def raiser(self, prompt_text, multi=False):
        raise ValueError('DeepSeek 返回非 JSON')

    @log_ocr_call('ocr.fake', evt='recognize', failed_if=lambda r: not r.get('success'),
                  payload=image_payload, outcome=items_outcome)
    def recognize(self, image_bytes, filename=''):
        return {'success': False, 'error': 'AI 识别功能未配置'}

    @log_ocr_call('ocr.fake', evt='recognize_ok', failed_if=lambda r: not r.get('success'),
                  payload=image_payload, outcome=items_outcome)
    def recognize_ok(self, image_bytes, filename=''):
        return {'success': True, 'items': [{'product_name': '杂胶'}, {'product_name': '纯胶'}]}

    @log_ocr_call('ocr.fake', evt='extract_text', failed_if=lambda r: not r,
                  payload=image_payload, outcome=text_outcome)
    def extract_text(self, image_bytes):
        return ''          # 现实中 PaddleOCR 失败就是返回空串


def test_success_at_info_logs_summary_without_prompt(logdir):
    _FakeEngine().ok_call(LONG_PROMPT)
    text = logdir()
    assert 'evt=ok_call' in text
    assert 'elapsed=' in text
    assert 'AAAA' not in text                 # INFO 不写 prompt 全文


def test_success_at_debug_logs_full_prompt(logdir_debug):
    _FakeEngine().ok_call(LONG_PROMPT)
    text = logdir_debug()
    assert 'A' * 3000 in text                 # DEBUG 写全文


def test_exception_logs_full_payload_at_info_level(logdir):
    """核心加强项：失败无条件全量，不受 DEBUG 开关约束。"""
    with pytest.raises(ValueError):
        _FakeEngine().raiser(LONG_PROMPT)
    text = logdir()
    assert 'ERROR' in text
    assert 'A' * 3000 in text                 # DEBUG 没开也有全文
    assert 'Traceback' in text
    assert 'DeepSeek 返回非 JSON' in text


def test_success_false_return_treated_as_failure(logdir):
    """recognize 吞异常返回 success=False，必须判为失败。"""
    _FakeEngine().recognize(b'\xff\xd8\xff' * 100, 'a.jpg')
    text = logdir()
    assert 'ERROR' in text
    assert 'AI 识别功能未配置' in text


def test_empty_string_return_treated_as_failure(logdir):
    """extract_text 失败返回空串，必须判为失败。"""
    _FakeEngine().extract_text(b'\xff\xd8\xff' * 100)
    text = logdir()
    assert 'ERROR' in text
    assert 'evt=extract_text' in text


def test_successful_recognize_logged_as_info_with_item_count(logdir):
    _FakeEngine().recognize_ok(b'\xff\xd8\xff' * 100, 'b.jpg')
    text = logdir()
    assert 'ERROR' not in text
    assert 'items=2' in text
    assert 'img=b.jpg' in text


def test_decorator_is_transparent_to_return_value(logdir):
    assert _FakeEngine().ok_call('p') == {'match_status': 'green', 'reason': '完全一致'}
    assert _FakeEngine().recognize_ok(b'x', 'c.jpg')['success'] is True
    assert _FakeEngine().extract_text(b'x') == ''


def test_decorator_reraises_original_exception(logdir):
    with pytest.raises(ValueError, match='DeepSeek 返回非 JSON'):
        _FakeEngine().raiser('p')


def test_image_bytes_never_written_to_log(logdir):
    """图片字节绝不入日志 —— Moonshot 单图 base64 数百 KB。"""
    blob = b'\x89PNG\r\n\x1a\n' + bytes(range(256)) * 40
    _FakeEngine().recognize_ok(blob, 'big.jpg')
    text = logdir()
    assert 'PNG' not in text
    assert 'size=' in text                    # 只记大小
    assert 'img=big.jpg' in text


def test_api_key_scrubbed_from_logged_prompt(logdir_debug):
    _FakeEngine().ok_call('前缀 sk-abcd1234efgh5678ijkl 后缀')
    text = logdir_debug()
    assert 'sk-abcd1234efgh5678ijkl' not in text
    assert 'sk-***' in text


def test_business_context_appears_in_log(logdir):
    def run():
        set_log_context(order_id=610, record_id=88)
        _FakeEngine().ok_call('p')
    contextvars.copy_context().run(run)
    text = logdir()
    assert 'order_id=610' in text
    assert 'record_id=88' in text


def test_logging_failure_does_not_break_business_call(logdir, monkeypatch):
    """日志系统炸了也不能影响业务返回。"""
    def boom(*a, **kw):
        raise RuntimeError('payload 提取炸了')

    class _E:
        @log_ocr_call('ocr.fake', evt='x', payload=boom, outcome=boom)
        def go(self, prompt_text):
            return {'ok': True}

    assert _E().go('p') == {'ok': True}


def test_trace_id_shared_across_calls_in_one_context(logdir):
    def run():
        new_trace_id()
        _FakeEngine().ok_call('p1')
        _FakeEngine().ok_call('p2')
    contextvars.copy_context().run(run)
    lines = [ln for ln in logdir().splitlines() if 'evt=ok_call' in ln]
    assert len(lines) == 2
    trace_ids = {ln.split('[')[1].split(']')[0] for ln in lines}
    assert len(trace_ids) == 1 and '-' not in trace_ids
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_ocr_logging.py -v`
Expected: FAIL —— `ImportError: cannot import name 'log_ocr_call'`

- [ ] **Step 3: 实现**

追加到 `blueprints/ocr_log.py`：

```python
import functools
import logging
import time

# ═══════════════════════════════════════════════════════════════════
# 预置的入参 / 返回值提取器
#
# 约定：都返回 (summary, detail) 两个 dict。
#   summary —— INFO 也写，必须短
#   detail  —— 只在 DEBUG 或失败时写，可以很长
# ═══════════════════════════════════════════════════════════════════


def image_payload(self, image_bytes, filename='', *a, **kw):
    """给 recognize / extract_text 用：只记文件名和字节数，绝不记图片内容。"""
    try:
        size = len(image_bytes) if image_bytes else 0
    except Exception:
        size = 0
    return ({'img': filename or '-', 'size': '%dKB' % (size // 1024)}, {})


def prompt_payload(self, prompt_text, *a, **kw):
    """给 _call_api_with_prompt 用：summary 只记长度，detail 记全文。"""
    text = prompt_text if isinstance(prompt_text, str) else str(prompt_text)
    return ({'prompt_chars': len(text)}, {'prompt': text})


def text_outcome(result):
    """给 extract_text 用：返回值是 OCR 纯文本。"""
    text = result or ''
    return ({'chars': len(text)}, {'ocr_text': text})


def items_outcome(result):
    """给 recognize 用：返回值是 {'success', 'items', 'error', ...}。"""
    if not isinstance(result, dict):
        return ({}, {'raw': result})
    summary = {'items': len(result.get('items') or [])}
    detail = {}
    if not result.get('success'):
        summary['error'] = trunc(result.get('error'), 120)
        detail['hint'] = result.get('hint') or ''
    detail['items_json'] = result.get('items')
    return (summary, detail)


def parsed_outcome(result):
    """给 _call_api_with_prompt / compare 用：返回值是解析后的 JSON。"""
    summary = {}
    if isinstance(result, dict):
        if result.get('match_status'):
            summary['result'] = result['match_status']
        if result.get('score') is not None:
            summary['score'] = result['score']
    elif isinstance(result, list):
        summary['rows'] = len(result)
    return (summary, {'parsed': result})


def _default_payload(*a, **kw):
    return ({}, {})


def _default_outcome(result):
    return ({}, {})


# ═══════════════════════════════════════════════════════════════════
# 格式化
# ═══════════════════════════════════════════════════════════════════


def _fmt_summary(d):
    parts = []
    for k, v in d.items():
        if v is None or v == '':
            continue
        parts.append('%s=%s' % (k, trunc(v, 200)))
    return ' '.join(parts)


def _fmt_detail(d):
    """detail 每项单独一行缩进，方便肉眼扫和 grep -A。"""
    lines = []
    for k, v in d.items():
        if v is None or v == '':
            continue
        lines.append('\n    %s=%s' % (k, scrub(v)))
    return ''.join(lines)


def _safe(fn, *a, **kw):
    """调提取器；它自己炸了不能连累业务，返回空。"""
    try:
        got = fn(*a, **kw)
        if isinstance(got, tuple) and len(got) == 2:
            return got
    except Exception:
        pass
    return ({}, {})


# ═══════════════════════════════════════════════════════════════════
# 装饰器
# ═══════════════════════════════════════════════════════════════════


def log_ocr_call(logger_name, evt, failed_if=None, payload=None, outcome=None):
    """给 OCR/AI 引擎方法加日志。

    参数:
        logger_name — 'ocr.deepseek' / 'ocr.paddle' / 'ocr.moonshot'
        evt         — 事件名，出现在日志的 evt= 字段
        failed_if   — fn(result) -> bool。被包装的方法多半吞异常、用返回值表示失败
                      （extract_text 返回 ''、recognize 返回 {'success': False}），
                      光靠 try/except 抓不到，必须靠这个判定。
        payload     — fn(*args, **kwargs) -> (summary, detail)，提取入参
        outcome     — fn(result) -> (summary, detail)，提取返回值

    语义:
        成功 + INFO  → 只写 summary
        成功 + DEBUG → summary + detail（完整 prompt / 原始返回）
        失败(异常或 failed_if) → ERROR，summary + 全部 detail + 堆栈，
                                 不受 DEBUG 开关约束（偶发问题往往不可复现）

    对被包装方法完全透明：返回值原样返回，异常原样抛出。
    """
    payload = payload or _default_payload
    outcome = outcome or _default_outcome

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            log = logging.getLogger(logger_name)
            in_sum, in_det = _safe(payload, *args, **kwargs)
            t0 = time.perf_counter()
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:
                _emit(log, evt, t0, in_sum, in_det, {}, {}, failed=True, exc=exc)
                raise                       # 原样抛出，保持透明
            try:
                out_sum, out_det = _safe(outcome, result)
                failed = bool(failed_if(result)) if failed_if else False
                _emit(log, evt, t0, in_sum, in_det, out_sum, out_det, failed=failed)
            except Exception:
                pass                        # 日志失败绝不阻断业务
            return result

        return wrapper

    return deco


def _emit(log, evt, t0, in_sum, in_det, out_sum, out_det, failed=False, exc=None):
    try:
        elapsed = time.perf_counter() - t0
        summary = {'evt': evt}
        summary.update(get_log_context())
        summary.update(in_sum)
        summary.update(out_sum)
        summary['elapsed'] = '%.2fs' % elapsed
        head = _fmt_summary(summary)

        if failed or exc is not None:
            # 失败：全量 detail + 堆栈，无条件
            detail = {}
            detail.update(in_det)
            detail.update(out_det)
            if exc is not None:
                detail['exception'] = '%s: %s' % (type(exc).__name__, exc)
            log.error('%s%s', head, _fmt_detail(detail), exc_info=exc is not None)
        elif log.isEnabledFor(logging.DEBUG):
            detail = {}
            detail.update(in_det)
            detail.update(out_det)
            log.debug('%s%s', head, _fmt_detail(detail))
        else:
            log.info('%s', head)
    except Exception:
        pass
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_ocr_logging.py -v`
Expected: PASS（39 passed）

- [ ] **Step 5: 提交**

```bash
git add blueprints/ocr_log.py tests/test_ocr_logging.py
git commit -m "feat(log): @log_ocr_call 装饰器 — 失败无条件记全量"
```

---

### Task 5: 接到 5 个引擎方法上

**Files:**
- Modify: `blueprints/ocr_engine.py`
  - 第 26 行 `logger = logging.getLogger(__name__)`
  - `MoonshotEngine.recognize`（约 450 行）
  - `PaddleOCREngine.extract_text`（约 604 行）
  - `PaddleOCREngine.recognize`（约 623 行）
  - `DeepSeekEngine.compare_rows`（约 830 行）—— 走 `_call_api_with_prompt`
  - `DeepSeekEngine._call_api_with_prompt`（约 1088 行）
  - `DeepSeekEngine.recognize`（约 1158 行）
- Test: `tests/test_ocr_logging.py`（追加）

**Interfaces:**
- Consumes: Task 4 的 `log_ocr_call` 及全部预置提取器
- Produces: 5 个被装饰的引擎方法，行为不变但产出日志

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_ocr_logging.py`：

```python
def test_all_five_engine_methods_are_decorated():
    """5 个收敛点必须都挂上装饰器 —— 漏一个就有整条链路没日志。"""
    from blueprints.ocr_engine import (DeepSeekEngine, MoonshotEngine,
                                       PaddleOCREngine)
    targets = [
        (MoonshotEngine, 'recognize'),
        (PaddleOCREngine, 'extract_text'),
        (PaddleOCREngine, 'recognize'),
        (DeepSeekEngine, '_call_api_with_prompt'),
        (DeepSeekEngine, 'recognize'),
    ]
    for cls, name in targets:
        fn = getattr(cls, name)
        assert getattr(fn, '__wrapped__', None) is not None, \
            '%s.%s 没有加 @log_ocr_call' % (cls.__name__, name)


def test_engine_logger_lives_under_ocr_tree():
    """ocr_engine 的模块 logger 必须在 'ocr.' 树下，否则日志跑去 app 文件。"""
    from blueprints import ocr_engine
    assert ocr_engine.logger.name.startswith('ocr')


def test_moonshot_unconfigured_key_logged_as_error(logdir, monkeypatch):
    """未配 key 时 recognize 返回 success=False —— 应记 ERROR。"""
    from blueprints.ocr_engine import MoonshotEngine
    monkeypatch.setattr(MoonshotEngine, 'API_KEY', '')
    MoonshotEngine().recognize(b'\xff\xd8\xff', 'x.jpg')
    text = logdir()
    assert 'ERROR' in text
    assert 'evt=recognize' in text
    assert 'AI 识别功能未配置' in text
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_ocr_logging.py -k engine -v`
Expected: FAIL —— `AssertionError: MoonshotEngine.recognize 没有加 @log_ocr_call`

- [ ] **Step 3: 加装饰器**

3a. 改 `blueprints/ocr_engine.py` 第 26 行：

```python
# 改前
logger = logging.getLogger(__name__)

# 改后 —— 挂到 'ocr' 树下，日志才会进 ocr-*.log 而非 app-*.log
logger = logging.getLogger('ocr.engine')
```

3b. 在 `import openai` 那行下面（约第 15 行）加导入：

```python
from blueprints.ocr_log import (image_payload, items_outcome, log_ocr_call,
                                parsed_outcome, prompt_payload, text_outcome)
```

3c. 给 `MoonshotEngine.recognize` 加装饰器（约 450 行，`def recognize` 正上方）：

```python
    @log_ocr_call('ocr.moonshot', evt='recognize',
                  failed_if=lambda r: not (r or {}).get('success'),
                  payload=image_payload, outcome=items_outcome)
    def recognize(self, image_bytes, filename=''):
```

3d. 给 `PaddleOCREngine.extract_text` 加装饰器（约 604 行）：

```python
    @log_ocr_call('ocr.paddle', evt='extract_text',
                  failed_if=lambda r: not r,          # 失败时吞异常返回空串
                  payload=image_payload, outcome=text_outcome)
    def extract_text(self, image_bytes):
```

3e. 给 `PaddleOCREngine.recognize` 加装饰器（约 623 行）：

```python
    @log_ocr_call('ocr.paddle', evt='recognize',
                  failed_if=lambda r: not (r or {}).get('success'),
                  payload=image_payload, outcome=items_outcome)
    def recognize(self, image_bytes, filename=''):
```

3f. 给 `DeepSeekEngine._call_api_with_prompt` 加装饰器（约 1088 行）。
**这一处覆盖 `compare_rows` 与 `compare_single_record` 两条比对链路** —— 它们都经由此方法发 HTTP：

```python
    @log_ocr_call('ocr.deepseek', evt='call_api',
                  payload=prompt_payload, outcome=parsed_outcome)
    def _call_api_with_prompt(self, prompt_text, multi: bool):
```

（此方法真抛异常，故不需要 `failed_if`。）

3g. 给 `DeepSeekEngine.recognize` 加装饰器（约 1158 行）。
注意它内部的 `_ocr_image` 是 `extract_text` 的副本实现、并不调用 `extract_text`，
所以必须单独装饰才有日志：

```python
    @log_ocr_call('ocr.deepseek', evt='recognize',
                  failed_if=lambda r: not (r or {}).get('success'),
                  payload=image_payload, outcome=items_outcome)
    def recognize(self, image_bytes, filename=''):
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_ocr_logging.py -v`
Expected: PASS（43 passed）

- [ ] **Step 5: 跑全量测试确认无回归**

Run: `python -m pytest tests/ -v`
Expected: 全部通过。特别关注 `test_ocr_extract_text.py` / `test_label_match.py` /
`test_deepseek_ai_recognize.py` / `test_ai_match_endpoint.py` —— 它们直接调这几个方法，
是装饰器透明性的最好检验。

若有失败，先确认是不是装饰器改了返回值或吞了异常，而非测试本身过期。

- [ ] **Step 6: 提交**

```bash
git add blueprints/ocr_engine.py tests/test_ocr_logging.py
git commit -m "feat(log): 5 个引擎收敛点挂 @log_ocr_call，全覆盖 ~30 处调用"
```

---

### Task 6: 接进应用 —— init_logging、线程传播、业务上下文

**Files:**
- Modify: `app.py`
- Modify: `blueprints/shipping.py:126-136`（`_spawn_record_image_processing`）+ 3 个 AI 入口
- Modify: `blueprints/inbound.py` / `blueprints/loading.py` 的 AI 入口
- Modify: `.gitignore`、`.env.example`
- Test: `tests/test_ocr_logging.py`（追加）

**Interfaces:**
- Consumes: Task 2 的 `init_logging`、Task 3 的 `set_log_context`
- Produces: 跑起来的应用会往 `log/YYYYMM/` 写日志

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_ocr_logging.py`：

```python
def test_create_app_installs_file_logging(tmp_path, monkeypatch):
    monkeypatch.setenv('OCR_LOG_LEVEL', 'INFO')
    import logging_setup
    monkeypatch.setattr(logging_setup, 'LOG_ROOT', str(tmp_path))
    from app import create_app
    create_app()
    handlers = logging.getLogger('ocr').handlers
    assert any(isinstance(h, DailyFolderHandler) for h in handlers)


def test_request_gets_trace_id(tmp_path, monkeypatch):
    monkeypatch.setenv('OCR_LOG_LEVEL', 'INFO')
    import logging_setup
    monkeypatch.setattr(logging_setup, 'LOG_ROOT', str(tmp_path))
    from app import create_app
    app = create_app()
    seen = {}

    @app.route('/__trace_probe')
    def _probe():
        seen['tid'] = get_trace_id()
        return 'ok'

    app.test_client().get('/__trace_probe')
    assert seen['tid'] != '-'
    assert len(seen['tid']) == 8


def test_background_thread_spawn_propagates_context():
    """行级图片 OCR 跑在 daemon 线程，trace_id 与业务上下文必须带进去。"""
    import blueprints.shipping as sh
    src = __import__('inspect').getsource(sh._spawn_record_image_processing)
    assert 'copy_context' in src, \
        '_spawn_record_image_processing 必须用 contextvars.copy_context() 传播上下文'


def test_gitignore_excludes_log_dir():
    with open('.gitignore', encoding='utf-8') as f:
        assert 'log/' in f.read()
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_ocr_logging.py -k "create_app or trace_probe or background or gitignore" -v`
Expected: FAIL

- [ ] **Step 3a: app.py 调 init_logging**

在 `app.py` 的 import 区（`from models import init_db, Notice` 下面）加：

```python
from logging_setup import init_logging
```

在 `create_app()` 里、`init_db()` **之前**插入（这样建表过程出错也能落盘）：

```python
    # === 初始化日志（log/YYYYMM/ 下按天分文件）===
    init_logging(app)
```

- [ ] **Step 3b: shipping.py 后台线程传播 contextvars**

`threading.Thread` **不会**自动传播 contextvars，必须显式 `copy_context().run(...)`，
否则后台线程里的 OCR 日志 trace_id 全是 `-`，跟主请求串不起来。

在 `blueprints/shipping.py` 顶部 import 区加：

```python
import contextvars
```

改 `_spawn_record_image_processing`（约 126 行）里起线程那段：

```python
# 改前
    t = threading.Thread(
        target=_process_record_image_async,
        args=(image_id, filepath, record, order_id, record_id),
        daemon=True,
    )

# 改后 —— 把当前 context（trace_id + 业务上下文）快照带进后台线程
    ctx = contextvars.copy_context()
    t = threading.Thread(
        target=ctx.run,
        args=(_process_record_image_async, image_id, filepath, record, order_id, record_id),
        daemon=True,
    )
```

- [ ] **Step 3c: 在 AI 入口设业务上下文**

在下列每个视图函数**开头第一行**加 `set_log_context(...)`。先在各文件 import 区加：

```python
from blueprints.ocr_log import set_log_context
```

`blueprints/shipping.py`：

| 函数（约行号） | 加入的调用 |
|---|---|
| `api_v1_shipping_orders_record_upload_images`(993) | `set_log_context(biz='shipping', record_id=record_id)` |
| `api_v1_shipping_orders_ai_match`(1228) | `set_log_context(biz='shipping', order_id=order_id)` |
| `shipping_records_ai_recognize`(1137) | `set_log_context(biz='shipping', evt_src='ai_recognize')` |
| `api_v1_shipping_orders_ai_judge_image`(858) | `set_log_context(biz='shipping', image_id=image_id)` |

`blueprints/inbound.py`（注意：入库**没有** ai_match 端点，只有 3 个）：

| 函数（行号） | 加入的调用 |
|---|---|
| `api_v1_inbound_orders_record_upload_images`(945) | `set_log_context(biz='inbound', record_id=record_id)` |
| `inbound_ai_recognize`(516) | `set_log_context(biz='inbound', evt_src='ai_recognize')` |
| `api_v1_inbound_orders_ai_judge_image`(737) | `set_log_context(biz='inbound', image_id=image_id)` |

`blueprints/loading.py`（同样没有 ai_match，只有 3 个）：

| 函数（行号） | 加入的调用 |
|---|---|
| `api_v1_loading_orders_record_upload_images`(630) | `set_log_context(biz='loading', record_id=record_id)` |
| `loading_ai_recognize`(451) | `set_log_context(biz='loading', evt_src='ai_recognize')` |
| `api_v1_loading_orders_ai_judge_image`(805) | `set_log_context(biz='loading', image_id=image_id)` |

行号是撰写计划时的快照，若对不上用函数名搜索定位。

- [ ] **Step 3d: .gitignore 与 .env.example**

`.gitignore` 在「业务数据」段末尾追加：

```
# 运行日志（log/YYYYMM/ 下按天分文件）
log/
```

`.env.example` 末尾追加：

```
# ===== 可选：OCR/AI 日志级别 =====
# INFO （默认）只记调用摘要：引擎/图片/耗时/结果
# DEBUG        额外记完整 prompt 与 LLM 原始返回，排查判定错误时打开
# 注意：调用失败时无论哪个级别都会记全量 prompt + 堆栈
# OCR_LOG_LEVEL=INFO
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_ocr_logging.py -v`
Expected: PASS（47 passed）

- [ ] **Step 5: 跑全量测试**

Run: `python -m pytest tests/ -v`
Expected: 全部通过，无回归。

- [ ] **Step 6: 确认 log/ 没被 git 跟踪**

Run: `git status --short`
Expected: 输出中**不含** `log/`。若出现，说明 `.gitignore` 没写对。

- [ ] **Step 7: 提交**

```bash
git add app.py blueprints/shipping.py blueprints/inbound.py blueprints/loading.py \
        .gitignore .env.example tests/test_ocr_logging.py
git commit -m "feat(log): 接入应用 — init_logging + 线程上下文传播 + 业务上下文"
```

---

### Task 7: 端到端人工验收

自动化测试用的是假引擎，这一步用真实服务器 + 真实图片验证整条链路。
**注意：本任务只读日志、不改数据，但仍建议先备份数据库**（CLAUDE.md 踩坑点 6）。

**Files:** 不改代码，仅验证。

- [ ] **Step 1: 备份数据库**

```powershell
Copy-Item "C:\Users\Administrator\worklog-app\worklog.db" "D:\BAK\worklog_$(Get-Date -Format 'yyyyMMdd_HHmm').db" -Force
```

- [ ] **Step 2: 启动服务器**

```bash
python app.py
```

- [ ] **Step 3: 验证 INFO 摘要与 trace_id 串联**

浏览器打开 `http://127.0.0.1:5050/shipping-records`，对任一明细行点 🖼️ 上传一张标签图。

检查 `log/202608/ocr-2026-08-04.log`（文件名按当天日期）：

- [ ] 出现 `evt=extract_text`（PaddleOCR）与 `evt=call_api`（DeepSeek）两条 INFO
- [ ] 两条的 `[trace_id]` **相同** —— 证明 contextvars 成功跨线程传播
- [ ] 含 `record_id=` 业务上下文
- [ ] **不含** prompt 全文（INFO 级别不该有）
- [ ] 含 `elapsed=` 耗时

- [ ] **Step 4: 验证失败路径无条件记全量**

停服务器，把 `.env` 里 `DEEPSEEK_API_KEY` 改成 `sk-invalid-key-for-test`，重启，再传一张图。

- [ ] 日志出现 `ERROR` 行
- [ ] **含完整 prompt**（`OCR_LOG_LEVEL` 仍是默认 INFO —— 这是本设计的关键加强项）
- [ ] 含 `Traceback`
- [ ] 日志中的 key 显示为 `sk-***` 而非明文

**验完把 `.env` 的 key 改回去。**

- [ ] **Step 5: 验证 DEBUG 全量**

`.env` 加 `OCR_LOG_LEVEL=DEBUG`，重启，传图。

- [ ] 成功调用的日志里出现完整 prompt 与 LLM 原始返回
- [ ] 验完把 `OCR_LOG_LEVEL` 改回 `INFO` 或注释掉

- [ ] **Step 6: 验证 app 总日志**

访问一个会报错的端点（如 `http://127.0.0.1:5050/api/v1/shipping-orders/999999/ai-match`）。

- [ ] `log/202608/app-2026-08-04.log` 收到蓝图的 `current_app.logger.exception` 输出
- [ ] 该文件里**没有** werkzeug 的 `GET /... 200` 请求流水（root 级别 WARNING 挡掉了）
- [ ] OCR 日志**没有**重复出现在 app 日志里（`propagate=False` 生效）

- [ ] **Step 7: 确认工作区干净**

```bash
git status --short
```

Expected: 不含 `log/`，不含 `.env`。

---

## 验收标准（对应 spec §10）

- [ ] 一次行级图片上传在 `ocr-YYYY-MM-DD.log` 产出 PaddleOCR + DeepSeek 两条 INFO，trace_id 相同
- [ ] key 改错触发失败时，日志含 ERROR + 完整 prompt + 堆栈（DEBUG 未开）
- [ ] `OCR_LOG_LEVEL=DEBUG` 时成功调用也记完整 prompt
- [ ] `app-YYYY-MM-DD.log` 收到蓝图 `current_app.logger.exception` 输出
- [ ] `git status` 不出现 `log/`
- [ ] `python -m pytest tests/ -v` 全绿，现有测试无回归
- [ ] 未引入任何新依赖（`requirements.txt` 未改）
