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
