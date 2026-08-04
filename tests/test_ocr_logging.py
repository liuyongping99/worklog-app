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
