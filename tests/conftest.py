"""Pytest 配置: 让测试用独立的 worklog_test.db, 不污染生产数据.

行为:
- 第一个测试开始前: 从 worklog.db 复制一份到 worklog_test.db (保留 schema)
- 整个测试 session 期间: WORKLOG_DB=worklog_test.db
- 最后一个测试结束后: 删 worklog_test.db (清理)

如果想保留测试 db 方便调试, 设环境变量 KEEP_TEST_DB=1.
"""
import os
import shutil
import sys
from pathlib import Path

# 项目根目录
ROOT = Path(__file__).resolve().parent.parent
PROD_DB = ROOT / 'worklog.db'
TEST_DB = ROOT / 'worklog_test.db'


def pytest_configure(config):
    """Pytest 启动时执行一次。"""
    if not PROD_DB.exists():
        # 生产 db 不存在 — 让测试用自己的 schema 初始化
        TEST_DB.touch()
    else:
        # 复制一份作为测试起始状态
        shutil.copy2(PROD_DB, TEST_DB)
    # 让 _db.py 优先用这个
    os.environ['WORKLOG_DB'] = str(TEST_DB)
    # 关键: _db.py 在 import 时就读取了 DB_PATH, 重设 env var 不会生效.
    # 必须 reload _db 模块, 让它重新读 env var.
    if 'models._db' in sys.modules:
        import importlib
        importlib.reload(sys.modules['models._db'])
        # 同步 reload 依赖 _db 的子模块 (它们 import 时绑定了 DB_PATH)
        for name in list(sys.modules):
            if name.startswith('models.') and name != 'models._db':
                try:
                    importlib.reload(sys.modules[name])
                except Exception:
                    pass


def pytest_unconfigure(config):
    """所有测试跑完清理。"""
    if TEST_DB.exists() and not os.environ.get('KEEP_TEST_DB'):
        try:
            TEST_DB.unlink()
        except OSError:
            pass
        # WAL 模式的副作用文件
        for ext in ('-wal', '-shm'):
            aux = TEST_DB.with_name(TEST_DB.name + ext)
            if aux.exists():
                try: aux.unlink()
                except OSError: pass