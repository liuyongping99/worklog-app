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
