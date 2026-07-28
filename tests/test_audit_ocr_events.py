"""OCR 事件审计页 — 单元测试入口"""
import os
import sys
import unittest
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models._db as _db


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        self._orig = _db.DB_PATH
        _db.DB_PATH = self.tmp.name
        from models import init_db, ShippingOrder, ShippingRecord, OcrMatchEvent
        init_db()
        self.oid = ShippingOrder.create('2026-07-25', 'C')
        self.rid = ShippingRecord.create('2026-07-25', 'C', '硬加面', '黑色', '1', 'y', '', self.oid)

    def tearDown(self):
        _db.DB_PATH = self._orig
        for ext in ('', '-wal', '-shm'):
            p = self.tmp.name + ext
            if os.path.exists(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass

    def _seed_pair(self, rid, oid, ai, human):
        """写一对(ai_match, human_verify)事件到同一 record。"""
        from models import OcrMatchEvent
        OcrMatchEvent.create('ai_match', rid, oid,
                             ai_match_status=ai, prompt_version='compare_rows_v1')
        OcrMatchEvent.create('human_verify', rid, oid, human_status=human)


class PromptStatsTests(_Base):
    def test_consistency_rate(self):
        """3 对:2 一致 + 1 不一致 + 1 仅 ai 无人工 → 总配对 3,一致率 2/3 = 66.7%。"""
        from models.audit_query import OcrEventAudit
        from models import ShippingRecord
        rid2 = ShippingRecord.create('2026-07-25', 'C', 'P', 'S', '1', 'y', '', self.oid)
        rid3 = ShippingRecord.create('2026-07-25', 'C', 'Q', 'S', '1', 'y', '', self.oid)
        rid4 = ShippingRecord.create('2026-07-25', 'C', 'R', 'S', '1', 'y', '', self.oid)
        self._seed_pair(self.rid, self.oid, 'green', 'green')   # 一致
        self._seed_pair(rid2, self.oid, 'green', 'green')       # 一致
        self._seed_pair(rid3, self.oid, 'red', 'green')         # 不一致
        self._seed_pair(rid4, self.oid, 'red', None)            # 仅 ai 无人工 → null_pairs=1,不计入分母

        rows = OcrEventAudit.prompt_stats()
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r['prompt_version'], 'compare_rows_v1')
        self.assertEqual(r['total_pairs'], 3)
        self.assertEqual(r['consistent_pairs'], 2)
        self.assertEqual(r['inconsistent_pairs'], 1)
        self.assertEqual(r['null_pairs'], 1)
        self.assertAlmostEqual(r['consistency_rate'], 66.7, places=1)

    def test_filters_by_versions(self):
        from models.audit_query import OcrEventAudit
        from models import ShippingRecord
        rid2 = ShippingRecord.create('2026-07-25', 'C', 'P', 'S', '1', 'y', '', self.oid)
        from models import OcrMatchEvent
        OcrMatchEvent.create('ai_match', self.rid, self.oid, ai_match_status='green', prompt_version='v1')
        OcrMatchEvent.create('human_verify', self.rid, self.oid, human_status='green')
        OcrMatchEvent.create('ai_match', rid2, self.oid, ai_match_status='red', prompt_version='v2')
        OcrMatchEvent.create('human_verify', rid2, self.oid, human_status='red')

        rows = OcrEventAudit.prompt_stats(prompt_versions=['v1'])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['prompt_version'], 'v1')

    def test_no_events_returns_empty(self):
        from models.audit_query import OcrEventAudit
        rows = OcrEventAudit.prompt_stats()
        self.assertEqual(rows, [])


if __name__ == '__main__':
    unittest.main()
