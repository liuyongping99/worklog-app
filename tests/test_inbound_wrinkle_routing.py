"""行级图接口接 CLAHE 类别门控 集成测试(inbound)。

Task 5:inbound.py 的两处 PaddleOCR 调用要在调 OCR 前根据 record.product_name
决定 apply_wrinkle_enhance。record 是褶皱品类(白磅布三文治等)→ True,
否则 → False。

测试 1 个:行级图片上传(line 1036)+ record 是「白磅布三文治」 →
extract_text 收到 apply_wrinkle_enhance=True。
"""
import os
import sys
import io
import unittest
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models._db as _db


class InboundWrinkleRoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        self._orig = _db.DB_PATH
        _db.DB_PATH = self.tmp.name
        from models import init_db, InboundOrder, InboundRecord
        init_db()
        # 白磅布三文治(褶皱品类)
        self.oid_wrap = InboundOrder.create('2026-07-23', 'T')
        self.rid_wrap = InboundRecord.create(
            self.oid_wrap, '白磅布三文治', '黑色', '10', 'y', '',
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

    @mock.patch('blueprints.inbound.get_ocr_engine')
    def test_row_image_upload_uses_clahe_for_wrinkle_category(self, mock_factory):
        """行级图上传 + record 是「白磅布三文治」→ extract_text 收到 apply_wrinkle_enhance=True。

        inbound 的行级图片上传是同步跑 OCR(不像 shipping 走 _ASYNC_JOBS),
        所以上传返回后 extract_text 已经被调用过,直接断言即可。
        """
        self._login()
        mock_engine = mock.MagicMock()
        mock_engine.extract_text.return_value = '白磅布三文治 黑色 1.5m'
        # 模拟 deepseek 调用,以免走真实 LLM(测试无关路径)
        mock_engine.compare_single_record.return_value = {
            'match_status': 'green',
            'match_score': 95,
            'reason': 'test',
            'prompt_text': '',
            'raw_response': '',
        }
        mock_factory.return_value = mock_engine

        data = {'image': (self._fake_png(), 'label.png')}
        resp = self.client.post(
            f'/api/v1/inbound-orders/records/{self.rid_wrap}/images',
            data=data, content_type='multipart/form-data'
        )
        self.assertEqual(resp.status_code, 201, f'上传应成功,实际 {resp.get_json()}')

        # 校验工厂被按 'paddleocr' 调用过(主路径)
        called_names = [call.args[0] for call in mock_factory.call_args_list]
        self.assertIn('paddleocr', called_names,
            f'OCR 工厂应按 paddleocr 调用,实际 {called_names}')

        # 校验 extract_text 收到 apply_wrinkle_enhance=True
        kwargs_with_enhance = [
            c.kwargs for c in mock_engine.extract_text.call_args_list
            if c.kwargs.get('apply_wrinkle_enhance') is True
        ]
        self.assertGreaterEqual(len(kwargs_with_enhance), 1,
            f'extract_text 应收到 apply_wrinkle_enhance=True,实际调用:'
            f' {mock_engine.extract_text.call_args_list}')


if __name__ == '__main__':
    unittest.main()