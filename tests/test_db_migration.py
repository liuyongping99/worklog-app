"""DB 迁移幂等测试 — 加 3 列后,重复 init_db 不报错。"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models._db as _db


class _TempDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        self._orig = _db.DB_PATH
        _db.DB_PATH = self.tmp.name

    def tearDown(self):
        _db.DB_PATH = self._orig
        for ext in ('', '-wal', '-shm'):
            p = self.tmp.name + ext
            if os.path.exists(p):
                try: os.unlink(p)
                except OSError: pass


class DbMigrationTests(_TempDb):

    def test_init_db_idempotent_with_new_columns(self):
        from models import init_db
        # 第一次跑：建表 + 加列
        init_db()
        # 第二次跑：必须不抛「duplicate column name」错误
        init_db()
        # 验证列存在
        import sqlite3
        conn = sqlite3.connect(self.tmp.name)
        for sql, expected_col in [
            ("PRAGMA table_info(inbound_images)", 'source_tag'),
            ("PRAGMA table_info(inbound_orders)", 'img_cols'),
            ("PRAGMA table_info(loading_orders)", 'img_cols'),
        ]:
            cols = [row[1] for row in conn.execute(sql).fetchall()]
            self.assertIn(expected_col, cols,
                f'列 {expected_col} 不存在 (sql={sql}, cols={cols})')
        conn.close()


if __name__ == '__main__':
    unittest.main()