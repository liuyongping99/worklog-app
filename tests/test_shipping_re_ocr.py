# -*- coding: utf-8 -*-
"""出货页 re-ocr 端点测试 (2026-08-19)。

覆盖 POST /api/v1/shipping-orders/images/<id>/re-ocr:
  - 200 + 完整 match_status 路径
  - 404 / 400 各 error 分支
  - OcrMatchEvent 'record_ocr' 写库
  - 复用 RecordImageProcessor.extract_ocr (享受缓存 + 4 角背景色)
"""
import os
import sys
import shutil
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import fake_png_bytes as _PNG  # noqa: E402,F401

import models._db as _db  # noqa: E402


def _stub_paddle(text='OCR_TEXT', conf=0.95):
    """构造一个返回 (text, conf) 的 paddle mock。"""
    paddle = mock.MagicMock()
    paddle.extract_text_with_conf.return_value = (text, conf)
    return paddle


class ReOcrEndpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        self._orig_db = _db.DB_PATH
        _db.DB_PATH = self.tmp.name

        from models import init_db, ShippingOrder, ShippingRecord
        init_db()
        self.oid = ShippingOrder.create('2026-08-19', 'T')
        self.rid = ShippingRecord.create(
            '2026-08-19', 'T', '黑磅布三文治', '1.2硬性', '182.5', 'y', '', self.oid,
        )

        # 准备一张图：实际写 PNG 到 tmp dir(PaddleOCR mock 返回固定字符串, 不真读图)
        from PIL import Image  # noqa: E402
        self.img_dir = tempfile.mkdtemp()
        self.img_path = os.path.join(self.img_dir, 'label.png')
        Image.new('RGB', (200, 200), 'white').save(self.img_path, 'PNG')

        # ShippingImage.create 只需 file_path + record_pk, 返回 int(image_id)
        from models import ShippingImage  # noqa: E402
        self.iid = ShippingImage.create(
            order_pk=self.oid, file_path=self.img_path,
            original_name='label.png', source='upload',
            record_pk=self.rid)

        from app import create_app  # noqa: E402
        self.app = create_app()
        self.client = self.app.test_client()

    def tearDown(self):
        _db.DB_PATH = self._orig_db
        try:
            os.unlink(self.tmp.name)
        except OSError:
            pass
        shutil.rmtree(self.img_dir, ignore_errors=True)

    @mock.patch('blueprints.shipping.get_ocr_engine')
    def test_existing_image_returns_200(self, mock_factory):
        """有图 + record + PaddleOCR 返回文字 -> 200 + match_status/y."""
        mock_factory.return_value = _stub_paddle('品名:磅布三文治\n厚度:1.2mm', 0.9)
        resp = self.client.post(f'/api/v1/shipping-orders/images/{self.iid}/re-ocr')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data['success'])
        self.assertEqual(data['image_id'], self.iid)
        self.assertIn(data['match_status'], ('green', 'yellow', 'red'))
        self.assertEqual(data['match_source'], 'local_fuzzy')
        self.assertIn('bg_color', data)

    def test_nonexistent_image_returns_404(self):
        """不存在的图片 -> 404."""
        resp = self.client.post('/api/v1/shipping-orders/images/999999/re-ocr')
        self.assertEqual(resp.status_code, 404)
        data = resp.get_json()
        self.assertFalse(data['success'])
        self.assertIn('图片不存在', data['error'])

    @mock.patch('blueprints.shipping.get_ocr_engine')
    def test_image_without_record_pk_returns_400(self, mock_factory):
        """record_pk 为 NULL 的图(订单级图) -> 400."""
        from models import ShippingImage
        # 新建一张订单级图(record_pk 为 None) -- 复用 setUp 的 self.img_path
        order_img_id = ShippingImage.create(
            order_pk=self.oid, file_path=self.img_path,
            original_name='order.png', source='upload', record_pk=None)
        resp = self.client.post(f'/api/v1/shipping-orders/images/{order_img_id}/re-ocr')
        self.assertEqual(resp.status_code, 400)
        data = resp.get_json()
        self.assertFalse(data['success'])
        self.assertIn('未关联商品行', data['error'])

    @mock.patch('blueprints.shipping.get_ocr_engine')
    def test_empty_ocr_text_returns_400(self, mock_factory):
        """PaddleOCR 返回 '' -> 400 OCR 无文字."""
        mock_factory.return_value = _stub_paddle('', 1.0)
        resp = self.client.post(f'/api/v1/shipping-orders/images/{self.iid}/re-ocr')
        self.assertEqual(resp.status_code, 400)
        data = resp.get_json()
        self.assertFalse(data['success'])
        self.assertIn('OCR 无文字', data['error'])

    @mock.patch('blueprints.shipping.get_ocr_engine')
    def test_writes_record_ocr_event(self, mock_factory):
        """成功后写 1 条 record_ocr 事件."""
        from models import OcrMatchEvent
        mock_factory.return_value = _stub_paddle('品名:磅布三文治', 0.9)
        resp = self.client.post(f'/api/v1/shipping-orders/images/{self.iid}/re-ocr')
        self.assertEqual(resp.status_code, 200)
        events = OcrMatchEvent.get_by_image(self.iid)
        record_ocr = [e for e in events if e['event_type'] == 'record_ocr']
        self.assertEqual(len(record_ocr), 1, '应该写一条 record_ocr 事件')
        self.assertIn('磅布三文治', record_ocr[0]['ocr_text'])
        self.assertEqual(record_ocr[0]['ai_engine'], 'local_fuzzy')

    @mock.patch('blueprints.shipping.shipping_processor')
    def test_uses_record_image_processor(self, mock_processor):
        """验证调用 shipping_processor.extract_ocr (而非直接 get_ocr_engine)."""
        mock_processor.extract_ocr.return_value = {
            'ocr_text': '品名:磅布三文治', 'bg_color': None,
            'avg_conf': 0.9, 'from_cache': False,
        }
        resp = self.client.post(f'/api/v1/shipping-orders/images/{self.iid}/re-ocr')
        self.assertEqual(resp.status_code, 200)
        mock_processor.extract_ocr.assert_called_once()
        kwargs = mock_processor.extract_ocr.call_args.kwargs
        self.assertEqual(kwargs['image_id'], self.iid)
        self.assertFalse(kwargs['use_cached'])


if __name__ == '__main__':
    unittest.main()