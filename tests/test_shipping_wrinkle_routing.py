"""行级图接口接 CLAHE 类别门控 集成测试。

Task 4:shipping.py 的两处 PaddleOCR 调用要在调 OCR 前根据 record.product_name
决定 apply_wrinkle_enhance。record 是褶皱品类(白磅布三文治等)→ True,
否则 → False。
"""
import os
import sys
import io
import unittest
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models._db as _db


class ShippingWrinkleRoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        self._orig = _db.DB_PATH
        _db.DB_PATH = self.tmp.name
        from models import init_db, ShippingOrder, ShippingRecord
        init_db()
        # 白磅布三文治(褶皱品类)
        self.oid_wrap = ShippingOrder.create('2026-07-23', 'T')
        self.rid_wrap = ShippingRecord.create(
            '2026-07-23', 'T', '白磅布三文治', '黑色', '10', 'y', '', self.oid_wrap,
        )
        # 无纺布(非褶皱品类)
        self.oid_other = ShippingOrder.create('2026-07-23', 'T2')
        self.rid_other = ShippingRecord.create(
            '2026-07-23', 'T2', '无纺布', '白色', '10', 'y', '', self.oid_other,
        )
        from app import create_app
        self.app = create_app()
        self.client = self.app.test_client()

    def tearDown(self):
        _db.DB_PATH = self._orig
        os.unlink(self.tmp.name)

    def _login(self):
        with self.client.session_transaction() as sess:
            sess['operator_id'] = 1

    def _fake_png(self):
        from tests import fake_png_stream
        return fake_png_stream()

    @mock.patch('blueprints.shipping.get_ocr_engine')
    def test_row_image_upload_uses_clahe_for_wrinkle_category(self, mock_factory):
        """行级图上传 + record 是「白磅布三文治」→ extract_text_with_conf 收到 apply_wrinkle_enhance=True。"""
        self._login()
        # mock 工厂返回的引擎:extract_text_with_conf / extract_text 都要拦截(异步线程会再调一次)
        mock_engine = mock.MagicMock()
        mock_engine.extract_text_with_conf.return_value = ('白磅布三文治 黑色 1.5m', 0.95)
        mock_engine.extract_text.return_value = '白磅布三文治 黑色 1.5m'
        mock_factory.return_value = mock_engine

        data = {'image': (self._fake_png(), 'label.png')}
        resp = self.client.post(
            f'/api/v1/shipping-orders/records/{self.rid_wrap}/images',
            data=data, content_type='multipart/form-data'
        )
        self.assertEqual(resp.status_code, 201, f'上传应成功,实际 {resp.get_json()}')
        img = resp.get_json()['images'][0]
        # 异步线程在调,所以要等
        from blueprints.shipping import _ASYNC_JOBS
        import time
        iid = img['image_id']
        deadline = time.time() + 5
        while time.time() < deadline and _ASYNC_JOBS.get(iid, {}).get('state') == 'processing':
            time.sleep(0.05)
        # 校验工厂被按 'paddleocr' 调用过(异步路径)
        called_names = [call.args[0] for call in mock_factory.call_args_list]
        self.assertIn('paddleocr', called_names,
            f'OCR 工厂应按 paddleocr 调用,实际 {called_names}')
        # 校验 extract_text_with_conf 收到 apply_wrinkle_enhance=True
        kwargs_with_enhance = [
            c.kwargs for c in mock_engine.extract_text_with_conf.call_args_list
            if c.kwargs.get('apply_wrinkle_enhance') is True
        ]
        self.assertGreaterEqual(len(kwargs_with_enhance), 1,
            f'extract_text_with_conf 应收到 apply_wrinkle_enhance=True,实际调用:'
            f' {mock_engine.extract_text_with_conf.call_args_list}')

    @mock.patch('blueprints.shipping.get_ocr_engine')
    def test_row_image_upload_skips_clahe_for_other_category(self, mock_factory):
        """行级图上传 + record 是「无纺布」→ apply_wrinkle_enhance=False(默认路径)。"""
        self._login()
        mock_engine = mock.MagicMock()
        mock_engine.extract_text_with_conf.return_value = ('无纺布 白色 1.5m', 0.95)
        mock_engine.extract_text.return_value = '无纺布 白色 1.5m'
        mock_factory.return_value = mock_engine

        data = {'image': (self._fake_png(), 'label.png')}
        resp = self.client.post(
            f'/api/v1/shipping-orders/records/{self.rid_other}/images',
            data=data, content_type='multipart/form-data'
        )
        self.assertEqual(resp.status_code, 201, f'上传应成功,实际 {resp.get_json()}')
        img = resp.get_json()['images'][0]
        from blueprints.shipping import _ASYNC_JOBS
        import time
        iid = img['image_id']
        deadline = time.time() + 5
        while time.time() < deadline and _ASYNC_JOBS.get(iid, {}).get('state') == 'processing':
            time.sleep(0.05)
        # 校验 extract_text_with_conf 收到 apply_wrinkle_enhance=False(或没传,默认 False)
        # 我们至少要保证没有任何 True 调用
        enhance_args = [
            c.kwargs.get('apply_wrinkle_enhance')
            for c in mock_engine.extract_text_with_conf.call_args_list
        ]
        self.assertNotIn(True, enhance_args,
            f'extract_text_with_conf 不应收到 apply_wrinkle_enhance=True,实际 {enhance_args}')


if __name__ == '__main__':
    unittest.main()