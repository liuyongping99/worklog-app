"""行级图片上传后自动匹配打分 集成测试（monkeypatch OCR）。"""
import os
import sys
import io
import unittest
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models._db as _db


class RecordUploadMatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        self._orig = _db.DB_PATH
        _db.DB_PATH = self.tmp.name
        from models import init_db, ShippingOrder, ShippingRecord
        init_db()
        self.oid = ShippingOrder.create('2026-07-23', 'T')
        self.rid = ShippingRecord.create(
            '2026-07-23', 'T', '硬加面', '黑色', '10', 'y', '', self.oid,
        )
        from app import create_app
        self.app = create_app()
        self.client = self.app.test_client()

    def tearDown(self):
        _db.DB_PATH = self._orig
        os.unlink(self.tmp.name)

    @mock.patch('blueprints.shipping.get_ocr_engine')
    def test_upload_returns_green_for_matching_label(self, mock_factory):
        """行级上传 → PaddleOCR（走单例工厂） → 本地模糊匹配命中 → 绿牌。

        NP0-1 之后生产代码改走 get_ocr_engine('paddleocr') 单例,原先 mock
        PaddleOCREngine 类的写法已不再拦截（无 PaddleOCREngine() 直接调用）。
        改成 mock 工厂函数返回 fake 引擎,行为一致。
        """
        mock_factory.return_value.extract_text.return_value = '硬加面 黑色 1.5m'
        data = {'image': (io.BytesIO(b'\x89PNG\r\n\x1a\n' + b'0' * 64), 'label.png')}
        resp = self.client.post(
            f'/api/v1/shipping-orders/records/{self.rid}/images',
            data=data, content_type='multipart/form-data'
        )
        self.assertEqual(resp.status_code, 201)
        img = resp.get_json()['images'][0]
        self.assertEqual(img['match_status'], 'green')
        self.assertGreaterEqual(img['match_score'], 85)


if __name__ == '__main__':
    unittest.main()
