"""行级图接口接 CLAHE 类别门控 集成测试(loading)。

Task 5:loading.py 的行级图片上传**不**跑 OCR(只有 ai-judge fallback 跑),
所以本测试对 ai-judge 端点做断言。record 是褶皱品类(白磅布三文治等)→ True,
否则 → False。

测试 1 个:ai-judge endpoint + record 是「白磅布三文治」→
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


class LoadingWrinkleRoutingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        self._orig = _db.DB_PATH
        _db.DB_PATH = self.tmp.name
        from models import init_db, LoadingOrder, LoadingOrderRecord, LoadingOrderImage, get_db
        init_db()
        # 临时 DB 上 init_db() 的加载柜图片迁移在 CREATE 之前跑,导致 sort_order
        # / source / record_pk 三列未建。补一下(测试-only,不动生产代码)。
        conn = get_db()
        cur = conn.cursor()
        for col_sql in [
            "ALTER TABLE loading_order_images ADD COLUMN source TEXT DEFAULT 'upload'",
            "ALTER TABLE loading_order_images ADD COLUMN record_pk INTEGER DEFAULT NULL",
            "ALTER TABLE loading_order_images ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0",
        ]:
            try:
                cur.execute(col_sql)
                conn.commit()
            except Exception:
                pass
        conn.close()
        # 白磅布三文治(褶皱品类)
        self.oid_wrap = LoadingOrder.create('2026-07-23', 'T')
        self.rid_wrap = LoadingOrderRecord.create(
            '2026-07-23', 'T', '白磅布三文治', '黑色', '10', 'y', '', self.oid_wrap,
        )
        # 关联一张图片(ai-judge 需要 image_id 存在)
        # 使用 PIL 生成最小 PNG,直接落盘以免走上传路径(上传不跑 OCR)
        from tests import fake_png_bytes
        # 保存到 upload/YYYY-MM/ 目录(跟 _save_one_uploaded_file 一致)
        from blueprints._helpers import get_upload_dir as _get_ud
        upload_dir, month_str = _get_ud()
        fname = 'loading_wrinkle_test.png'
        fpath = os.path.join(upload_dir, fname)
        with open(fpath, 'wb') as fh:
            fh.write(fake_png_bytes())
        # 显式传 sort_order=0 跳过 SELECT MAX(sort_order)
        self.image_id = LoadingOrderImage.create(
            order_pk=self.oid_wrap,
            file_path=fpath,
            original_name='label.png',
            source='upload',
            record_pk=self.rid_wrap,
            sort_order=0,
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

    @mock.patch('blueprints.loading.get_ocr_engine')
    def test_ai_judge_uses_clahe_for_wrinkle_category(self, mock_factory):
        """ai-judge endpoint + record 是「白磅布三文治」→ extract_text 收到 apply_wrinkle_enhance=True。

        loading 行级上传不跑 OCR,所以走 ai-judge 端点;stored ocr_text 为空
        会触发 re-OCR,从而走到 extract_text。
        """
        self._login()
        mock_engine = mock.MagicMock()
        mock_engine.extract_text.return_value = '白磅布三文治 黑色 1.5m'
        mock_engine.compare_single_record.return_value = {
            'match_status': 'green',
            'match_score': 95,
            'reason': 'test',
            'prompt_text': '',
            'raw_response': '',
        }
        # 让 AI_KEY 走通的最小 mock:显式设 API_KEY(避免 503)
        mock_engine.API_KEY = 'sk-fake-test-key'
        mock_factory.return_value = mock_engine

        resp = self.client.post(
            f'/api/v1/loading-orders/images/{self.image_id}/ai-judge'
        )
        self.assertEqual(resp.status_code, 200, f'ai-judge 应成功,实际 {resp.get_json()}')

        # 校验工厂被按 'paddleocr' 调用过(re-OCR 路径)
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