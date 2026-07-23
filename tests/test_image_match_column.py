"""ShippingImage.set_match() + 迁移列 单测（临时 DB）。"""
import os
import sys
import unittest
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models._db as _db


class SetMatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        self._orig = _db.DB_PATH
        _db.DB_PATH = self.tmp.name
        from models import init_db, ShippingOrder, ShippingRecord, ShippingImage
        init_db()
        self.ShippingImage = ShippingImage
        oid = ShippingOrder.create('2026-07-23', 'T')
        rid = ShippingRecord.create('2026-07-23', 'T', '硬加面', '黑色', '10', 'y', '', oid)
        self.image_id = ShippingImage.create(order_pk=oid, file_path='upload/2026-07/a.png',
                                             record_pk=rid)

    def tearDown(self):
        _db.DB_PATH = self._orig
        os.unlink(self.tmp.name)

    def test_set_and_read_back(self):
        self.ShippingImage.set_match(self.image_id, 'green', 92.5)
        img = self.ShippingImage.get_by_id(self.image_id)
        self.assertEqual(img['match_status'], 'green')
        self.assertAlmostEqual(img['match_score'], 92.5, places=1)

    def test_default_null_before_set(self):
        img = self.ShippingImage.get_by_id(self.image_id)
        self.assertIsNone(img['match_status'])


if __name__ == '__main__':
    unittest.main()
