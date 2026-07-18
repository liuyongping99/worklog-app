"""数据库连接 + 路径常量"""
import os
import sqlite3

# 绝对路径，确保 Flask reloader 也能找到同一文件。
# 优先级: 环境变量 WORKLOG_DB > 默认 worklog.db。
# 跑测试时设 WORKLOG_DB=worklog_test.db, 避免清掉生产数据。
_DEFAULT_DB = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'worklog.db'
)
DB_PATH = os.environ.get('WORKLOG_DB', _DEFAULT_DB)


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout = 30000')
    conn.execute('PRAGMA foreign_keys = ON')
    conn.row_factory = sqlite3.Row
    return conn


