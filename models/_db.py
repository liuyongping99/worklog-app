"""数据库连接 + 路径常量"""
import os
import sqlite3

# 绝对路径，确保 Flask reloader 也能找到同一文件
DB_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'worklog.db')

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30, check_same_thread=False)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout = 30000')
    conn.row_factory = sqlite3.Row
    return conn


