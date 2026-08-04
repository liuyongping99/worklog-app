"""P0 回归测试:

1. 删除明细/清空明细时清理其行级图片,防孤儿图导致订单删不掉。
2. 整单 AI 匹配加闸门:锁定单拒绝 + 只回写属于本单的 record_id(防越权)。
"""
import os
import sys
import unittest
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import fake_png_bytes as _PNG  # noqa: E402,F401

import models._db as _db


class ShippingOrphanImageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        self._orig = _db.DB_PATH
        _db.DB_PATH = self.tmp.name
        from models import init_db
        init_db()
        from app import create_app
        self.client = create_app().test_client()

    def tearDown(self):
        _db.DB_PATH = self._orig
        os.unlink(self.tmp.name)

    def _mk_order_with_record_image(self, customer='T'):
        from models import ShippingOrder, ShippingRecord, ShippingImage
        oid = ShippingOrder.create('2026-07-25', customer)
        rid = ShippingRecord.create('2026-07-25', customer, '硬加面', '黑色', '10', 'y', '', oid)
        img_id = ShippingImage.create(order_pk=oid, file_path='upload/2026-07/x.png', record_pk=rid)
        return oid, rid, img_id

    def test_delete_by_record_clears_images(self):
        """模型层 delete_by_record 应删掉该明细的所有行级图片。"""
        from models import ShippingImage
        _, rid, _ = self._mk_order_with_record_image()
        self.assertEqual(len(ShippingImage.get_by_record(rid)), 1)
        ShippingImage.delete_by_record(rid)
        self.assertEqual(len(ShippingImage.get_by_record(rid)), 0)

    def test_delete_record_endpoint_removes_orphan_then_order_deletable(self):
        """删除明细端点应连带清掉行级图片,之后空订单可被删除(不再被'仍有图片'挡住)。"""
        from models import ShippingImage
        oid, rid, _ = self._mk_order_with_record_image()

        resp = self.client.delete(f'/api/v1/shipping-orders/records/{rid}')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(len(ShippingImage.get_by_record(rid)), 0)
        # 图片也从整单查询里消失(不是孤儿)
        self.assertEqual(len(ShippingImage.get_by_order(oid)), 0)

        # 订单现在应可删除
        resp2 = self.client.delete(f'/api/v1/shipping-orders/{oid}')
        self.assertEqual(resp2.status_code, 200)
        self.assertTrue(resp2.get_json()['success'])

    def test_delete_all_records_keeps_order_level_image(self):
        """清空明细端点应清行级图,但保留订单级共享图(record_pk 为空)。"""
        from models import ShippingImage
        oid, rid, _ = self._mk_order_with_record_image()
        # 再加一张订单级共享图
        shared_id = ShippingImage.create(order_pk=oid, file_path='upload/2026-07/shared.png')

        resp = self.client.delete(f'/api/v1/shipping-orders/{oid}/records')
        self.assertEqual(resp.status_code, 200)

        remaining = ShippingImage.get_by_order(oid)
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]['id'], shared_id)
        self.assertIsNone(remaining[0]['record_pk'])


class AiMatchGateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        self._orig = _db.DB_PATH
        _db.DB_PATH = self.tmp.name
        from models import init_db, ShippingOrder, ShippingRecord, ShippingImage
        init_db()
        self.oid = ShippingOrder.create('2026-07-25', 'T')
        self.rid = ShippingRecord.create('2026-07-25', 'T', '硬加面', '黑色', '10', 'y', '', self.oid)
        from blueprints.shipping import BASE_DIR
        self._upload_dir = os.path.join(BASE_DIR, 'upload', '2026-07')
        os.makedirs(self._upload_dir, exist_ok=True)
        self._img_abspath = os.path.join(self._upload_dir, 'gate.png')
        with open(self._img_abspath, 'wb') as f:
            f.write(_PNG())
        ShippingImage.create(order_pk=self.oid, file_path='upload/2026-07/gate.png', record_pk=self.rid)
        from app import create_app
        self.client = create_app().test_client()

    def tearDown(self):
        _db.DB_PATH = self._orig
        os.unlink(self.tmp.name)
        try:
            os.unlink(self._img_abspath)
        except OSError:
            pass

    def test_ai_match_rejects_locked_order(self):
        """锁定订单不允许 AI 匹配改写匹配状态 → 403。"""
        from models import ShippingOrder
        ShippingOrder.lock(self.oid)
        resp = self.client.post(f'/api/v1/shipping-orders/{self.oid}/ai-match')
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(resp.get_json()['success'])

    @mock.patch('blueprints.shipping.get_ocr_engine')
    def test_ai_match_ignores_foreign_record_id(self, mock_get_engine):
        """AI 幻觉出别单的 record_id 时,端点必须跳过,不得改写它的图片状态。"""
        from models import ShippingOrder, ShippingRecord, ShippingImage
        # 另建一个订单 + 明细 + 图片,作为"受害者"
        other_oid = ShippingOrder.create('2026-07-25', 'OTHER')
        other_rid = ShippingRecord.create('2026-07-25', 'OTHER', '别单货', '白色', '5', 'y', '', other_oid)
        other_img = ShippingImage.create(order_pk=other_oid, file_path='upload/2026-07/other.png', record_pk=other_rid)

        fake_paddle = mock.MagicMock()
        fake_paddle.extract_text.return_value = '硬加面 黑色'
        fake_ds = mock.MagicMock()
        # LLM 幻觉:返回别单的 record_id,试图把它标成 red
        fake_ds.compare_rows.return_value = [
            {'record_id': self.rid, 'match_status': 'green', 'reason': 'ok'},
            {'record_id': other_rid, 'match_status': 'red', 'reason': 'hallucination'},
        ]
        mock_get_engine.side_effect = lambda name: fake_paddle if name == 'paddleocr' else fake_ds

        resp = self.client.post(f'/api/v1/shipping-orders/{self.oid}/ai-match')
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        # 结果里只应有本单的 record
        rids = [r['record_id'] for r in body['results']]
        self.assertIn(self.rid, rids)
        self.assertNotIn(other_rid, rids)

        # 受害者订单的图片状态没有被改写
        victim = ShippingImage.get_by_id(other_img)
        self.assertIsNone(victim['match_status'])

    @mock.patch('blueprints.shipping.get_ocr_engine')
    def test_ai_match_writes_match_source_deepseek(self, mock_get_engine):
        """ai-match 整单跑完后,本单图片 match_source 应写 deepseek(云端图标 ⊛/◇/◆)。

        回归:shipping.py:1350 早期漏传 source=,默认成 local_fuzzy,徽章显示
        成本地图标 ✓/⚠/✗,跟实际跑的引擎对不上。
        """
        from models import ShippingImage
        # 已有 1 张图(setup 里创建的),再加 1 张让两个 record 都覆盖到
        ShippingImage.create(order_pk=self.oid,
                             file_path='upload/2026-07/gate-2.png', record_pk=self.rid)

        fake_paddle = mock.MagicMock()
        fake_paddle.extract_text.return_value = '硬加面 黑色'
        fake_ds = mock.MagicMock()
        fake_ds.compare_rows.return_value = [
            {'record_id': self.rid, 'match_status': 'yellow', 'reason': '存疑'},
        ]
        mock_get_engine.side_effect = lambda name: fake_paddle if name == 'paddleocr' else fake_ds

        resp = self.client.post(f'/api/v1/shipping-orders/{self.oid}/ai-match')
        self.assertEqual(resp.status_code, 200)

        imgs = ShippingImage.get_by_record(self.rid)
        self.assertEqual(len(imgs), 2)
        for img in imgs:
            self.assertEqual(img['match_source'], 'deepseek',
                f'ai-match 写库后 match_source 应为 deepseek,实际 {img["match_source"]}')
            self.assertEqual(img['match_status'], 'yellow')


if __name__ == '__main__':
    unittest.main()
