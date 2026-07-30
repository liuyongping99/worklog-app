"""整单 AI 匹配端点 集成测试（monkeypatch OCR 提字 + DeepSeek 比对）。"""
import os
import sys
import unittest
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models._db as _db


class AiMatchEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        self._orig = _db.DB_PATH
        _db.DB_PATH = self.tmp.name
        from models import init_db, ShippingOrder, ShippingRecord, ShippingImage
        init_db()
        # ShippingOrder.create(date, customer) — positional
        self.oid = ShippingOrder.create('2026-07-23', 'T')
        # ShippingRecord.create(date, customer, product, spec, qty, unit, remark, order_pk)
        self.rid = ShippingRecord.create(
            '2026-07-23', 'T', '硬加面', '黑色', '10', 'y', '', self.oid,
        )
        # 端点会把 img['relative_path'] 拼到 BASE_DIR/upload/ 下,所以需要真实文件
        # relative_path = file_path 去掉 'upload/' 前缀 → '2026-07/a.png'
        # 端点拼出: BASE_DIR/upload/2026-07/a.png
        from blueprints.shipping import BASE_DIR
        self._upload_dir = os.path.join(BASE_DIR, 'upload', '2026-07')
        os.makedirs(self._upload_dir, exist_ok=True)
        self._img_abspath = os.path.join(self._upload_dir, 'a.png')
        with open(self._img_abspath, 'wb') as f:
            f.write(b'\x89PNG\r\n\x1a\n' + b'0' * 64)
        ShippingImage.create(order_pk=self.oid, file_path='upload/2026-07/a.png', record_pk=self.rid)
        from app import create_app
        self.client = create_app().test_client()

    def tearDown(self):
        _db.DB_PATH = self._orig
        os.unlink(self.tmp.name)
        try:
            os.unlink(self._img_abspath)
        except OSError:
            pass

    @mock.patch('blueprints.shipping.get_ocr_engine')
    def test_ai_match_updates_rows(self, mock_get_engine):
        """端点应跑 PaddleOCR + DeepSeek,把 verdict 写到 shipping_images.match_status。

        NP0-1 之后 paddleocr 走 get_ocr_engine 单例,需要按引擎名分发 fake 实例。
        """
        fake_paddle = mock.MagicMock()
        fake_paddle.extract_text.return_value = '硬加面 黑色'
        fake_ds = mock.MagicMock()
        fake_ds.compare_rows.return_value = [
            {'record_id': self.rid, 'match_status': 'green', 'reason': '品名规格一致'}
        ]
        mock_get_engine.side_effect = lambda name: fake_paddle if name == 'paddleocr' else fake_ds

        resp = self.client.post(f'/api/v1/shipping-orders/{self.oid}/ai-match')
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        self.assertTrue(body['success'])
        self.assertEqual(body['results'][0]['match_status'], 'green')
        self.assertIn('summary', body)

        from models import ShippingImage
        imgs = ShippingImage.get_by_record(self.rid)
        self.assertEqual(imgs[0]['match_status'], 'green')

    @mock.patch('blueprints.shipping.get_ocr_engine')
    def test_ai_match_normalizes_off_spec_status(self, mock_get_engine):
        """LLM 返回 'GREEN'/'Yellow'/'OK'/'foo' 等不规范值时,端点必须归一为小写并计入 summary。"""
        fake_paddle = mock.MagicMock()
        fake_paddle.extract_text.return_value = '硬加面 黑色'
        fake_ds = mock.MagicMock()
        fake_ds.compare_rows.return_value = [
            {'record_id': self.rid, 'match_status': 'GREEN', 'reason': ''},
            {'record_id': self.rid, 'match_status': 'Yellow', 'reason': ''},
            {'record_id': self.rid, 'match_status': 'OK', 'reason': ''},
            {'record_id': self.rid, 'match_status': 'foo', 'reason': ''},
        ]
        mock_get_engine.side_effect = lambda name: fake_paddle if name == 'paddleocr' else fake_ds

        resp = self.client.post(f'/api/v1/shipping-orders/{self.oid}/ai-match')
        self.assertEqual(resp.status_code, 200)
        body = resp.get_json()
        statuses = [r['match_status'] for r in body['results']]
        self.assertEqual(statuses, ['green', 'yellow', 'green', 'red'])
        # OK→green, foo→red, GREEN→green, Yellow→yellow: 2 ✓ / 1 ⚠ / 1 ✗
        self.assertIn('4 行已核对', body['summary'])
        self.assertIn('2 ✓', body['summary'])
        self.assertIn('1 ⚠', body['summary'])
        self.assertIn('1 ✗', body['summary'])

        from models import ShippingImage
        imgs = ShippingImage.get_by_record(self.rid)
        # 所有 set_match 调用都用规范化值,最后一条是 'red'
        self.assertIn(imgs[0]['match_status'], {'green', 'yellow', 'red'})


if __name__ == '__main__':
    unittest.main()
