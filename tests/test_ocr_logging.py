"""OCR/AI 文件日志测试。"""
import logging
import os
from datetime import date

import pytest

from logging_setup import LOG_ROOT, DailyFolderHandler
import contextvars
import threading

from logging_setup import (TraceIdFilter, get_trace_id, init_logging,
                           new_trace_id)


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
