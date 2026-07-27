"""出货页商品行 OCR 比对事件记录 —— 测试入口"""
import os
import sys
import unittest
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models._db as _db


class OcrMatchEventTableTests(unittest.TestCase):
    def test_create_table_idempotent(self):
        """init_db 跑两次不应报错；表存在 + 索引存在。"""
        tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        tmp.close()
        orig = _db.DB_PATH
        _db.DB_PATH = tmp.name
        try:
            from models import init_db
            init_db()
            init_db()  # 第二次必须不报错
            from models._db import get_db
            conn = get_db()
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='ocr_match_event'"
            ).fetchall()
            self.assertEqual(len(rows), 1, '表 ocr_match_event 应存在')
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_ocr_match_%'"
            ).fetchall()
            self.assertEqual(len(rows), 2, '应有 2 个索引')
            conn.close()
        finally:
            _db.DB_PATH = orig
            os.unlink(tmp.name)