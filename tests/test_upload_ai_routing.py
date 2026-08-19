"""行级图上传后 AI 比对流程 (2026-07-30 改造):
- 可联网 (DeepSeek API key 已配) → 优先调 DeepSeek 单 record 比对,落 deepseek 结果
- 不可联网 (无 key / 调用失败) → fallback 本地 RapidFuzz,落 local_fuzzy 结果
- 数据字段:
  shipping_images.match_source  ∈ {'local_fuzzy', 'deepseek'}  新增
  shipping_images.match_status  ∈ {'green','yellow','red',NULL}
  ocr_match_event.ai_engine     ∈ {'local_fuzzy','deepseek'}
"""
import io
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import models._db as _db


class _TempDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        self._orig = _db.DB_PATH
        _db.DB_PATH = self.tmp.name
        from models import init_db
        init_db()
        from app import create_app
        self.client = create_app().test_client()
        from models.tasks_flow import StaffDB
        sid = StaffDB.create('test', '调度')['id']
        with self.client.session_transaction() as s:
            s['operator_id'] = sid
        # 准备一条 record
        from models import ShippingOrder, ShippingRecord
        self.oid = ShippingOrder.create('2026-07-30', 'C')
        self.rid = ShippingRecord.create('2026-07-30', 'C', '环保杂胶', '0.8黑中加面', '50', 'y', '', self.oid)

    def tearDown(self):
        # 行级图上传是异步的(daemon 线程)。若线程活过 tearDown，DB_PATH 已被
        # 还原成生产库,线程里的 set_match / OcrMatchEvent.create 就会写到
        # worklog.db 上 —— 必须先等干净再还原。
        self._drain_async_jobs()
        _db.DB_PATH = self._orig
        for ext in ('', '-wal', '-shm'):
            p = self.tmp.name + ext
            if os.path.exists(p):
                try: os.unlink(p)
                except OSError: pass

    @staticmethod
    def _drain_async_jobs(timeout=15):
        """等待所有在飞的行级图后台任务结束(测试隔离用)。"""
        from blueprints.shipping import _ASYNC_JOBS
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not any(j.get('state') == 'processing' for j in list(_ASYNC_JOBS.values())):
                return
            time.sleep(0.02)
        raise AssertionError('后台 OCR 任务超时未结束,可能污染生产库')

    def _wait_async(self, image_id, timeout=15):
        """等单张图的后台 OCR+比对跑完,返回终态 'done'|'error'。"""
        from blueprints.shipping import _ASYNC_JOBS
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = _ASYNC_JOBS.get(image_id, {}).get('state')
            if state in ('done', 'error'):
                return state
            time.sleep(0.02)
        raise AssertionError(f'异步任务超时未完成: image_id={image_id}')

    @staticmethod
    def _db_image(image_id):
        """异步化后 match_status/score/reason/source 只在 DB 里,不在上传响应里。"""
        from models import ShippingImage
        return ShippingImage.get_by_id(image_id)


class ShippingImageMatchSourceSchemaTests(_TempDb):
    """DDL 增加 match_source 列。"""

    def test_match_source_column_exists(self):
        import sqlite3
        c = sqlite3.connect(_db.DB_PATH)
        cols = {r[1] for r in c.execute('PRAGMA table_info(shipping_images)')}
        c.close()
        self.assertIn('match_source', cols,
            'shipping_images 表必须新增 match_source 列')

    def test_match_source_default_local_fuzzy(self):
        from models import ShippingImage
        iid = ShippingImage.create(self.oid, 'upload/2026-07/x.png', 'x.png', 'upload', record_pk=self.rid)
        rec = ShippingImage.get_by_id(iid)
        # 默认为空字符串(老记录升级)或 'local_fuzzy'(新行)
        self.assertIn(rec.get('match_source') or '', ('', 'local_fuzzy'))


class ShippingImageSetMatchTests(_TempDb):
    """set_match 应支持 source 参数。"""

    def test_set_match_with_deepseek_source(self):
        from models import ShippingImage
        iid = ShippingImage.create(self.oid, 'upload/2026-07/d.png', 'd.png', 'ai', record_pk=self.rid)
        ShippingImage.set_match(iid, 'green', 95.0, '云端匹配', source='deepseek')
        rec = ShippingImage.get_by_id(iid)
        self.assertEqual(rec['match_status'], 'green')
        self.assertEqual(rec['match_source'], 'deepseek')
        self.assertEqual(rec['match_score'], 95.0)
        self.assertEqual(rec['reason'], '云端匹配')

    def test_set_match_default_source_local_fuzzy(self):
        from models import ShippingImage
        iid = ShippingImage.create(self.oid, 'upload/2026-07/l.png', 'l.png', 'upload', record_pk=self.rid)
        ShippingImage.set_match(iid, 'yellow', 60.0, '本地匹配')
        rec = ShippingImage.get_by_id(iid)
        self.assertEqual(rec['match_source'], 'local_fuzzy')


class RecordUploadImageDeepSeekFallbackTests(_TempDb):
    """POST /records/<id>/images 多文件路径 + 单 base64 路径:
    - 当 DeepSeekEngine.API_KEY 配了 → 调用云端单 record 比对
    - 当没配 / 失败 → fallback 本地 RapidFuzz
    - shipping_images.match_source 正确反映来源
    """

    def _fake_upload_file(self, name='x.png'):
        from PIL import Image
        buf = io.BytesIO()
        Image.new('RGB', (8, 8), 'white').save(buf, 'PNG')
        buf.seek(0)
        return (buf, name)

    def _post_upload(self, files_dict, with_api_key=True, deepseek_status='green'):
        """MOCK PaddleOCR + DeepSeek + 本地匹配,验证路由选择。"""
        # 清 paddleocr 缓存
        from blueprints.ocr_engine import _engine_cache
        _engine_cache.pop('paddleocr', None)
        _engine_cache.pop('deepseek', None)

        from unittest import mock
        with mock.patch('blueprints.shipping.get_ocr_engine') as mock_factory:
            # mock paddleocr: 提取文字
            fake_paddle = mock.MagicMock()
            fake_paddle.extract_text.return_value = '环保杂胶 0.8mm 加面 黑色'
            # 2026-08-10 Task 6:同步 mock extract_text_with_conf
            fake_paddle.extract_text_with_conf.return_value = ('环保杂胶 0.8mm 加面 黑色', 0.95)

            # mock deepseek (在 compare_single_record 时被调用)
            fake_ds = mock.MagicMock()
            fake_ds.API_KEY = 'sk-test' if with_api_key else ''
            fake_ds.compare_single_record.return_value = {
                'match_status': deepseek_status,
                'match_score': 95.0,
                'reason': '云端比对理由',
            }

            def side(name):
                return fake_paddle if name == 'paddleocr' else fake_ds
            mock_factory.side_effect = side

            resp = self.client.post(
                f'/api/v1/shipping-orders/records/{self.rid}/images',
                data=files_dict,
                content_type='multipart/form-data',
            )
            # 2026-07-31 改造:上传立即返回,OCR+比对在后台线程。必须在 mock
            # 仍然生效的作用域内等它跑完,否则线程会去调真的 PaddleOCR。
            if resp.status_code == 201:
                for im in resp.get_json().get('images', []):
                    if im.get('image_id'):
                        self._wait_async(im['image_id'])
            return resp

    def test_with_api_key_uses_deepseek(self):
        png = self._fake_upload_file('deepseek.png')
        # DeepSeek API key 配 → 走云端
        with mock.patch('blueprints.ocr_pipeline.match_label_to_row') as mock_local:
            res = self._post_upload({'image': png}, with_api_key=True, deepseek_status='green')
            self.assertEqual(res.status_code, 201)
            data = res.get_json()
            self.assertTrue(data['success'])
            self.assertTrue(data['images'][0]['processing'],
                '上传端点应立即返回 processing=True(比对在后台线程)')
            rec = self._db_image(data['images'][0]['image_id'])
            self.assertEqual(rec['match_status'], 'green')
            self.assertEqual(rec['match_source'], 'deepseek',
                '联网时 match_source 应为 deepseek')
            self.assertEqual(rec['reason'], '云端比对理由')
            mock_local.assert_not_called()  # 关键:本地匹配不应被调用

    def test_without_api_key_falls_back_to_local(self):
        png = self._fake_upload_file('local.png')
        with mock.patch('blueprints.ocr_pipeline.match_label_to_row',
                        return_value=('yellow', 60.0, '本地匹配理由')) as mock_local:
            res = self._post_upload({'image': png}, with_api_key=False, deepseek_status='green')
            self.assertEqual(res.status_code, 201)
            data = res.get_json()
            rec = self._db_image(data['images'][0]['image_id'])
            self.assertEqual(rec['match_status'], 'yellow')
            self.assertEqual(rec['match_source'], 'local_fuzzy',
                '未联网时 match_source 应为 local_fuzzy')
            self.assertEqual(rec['reason'], '本地匹配理由')
            mock_local.assert_called_once()  # 本地匹配必须被调用

    def test_deepseek_failure_falls_back_to_local(self):
        """联网但 DeepSeek 抛异常 → fall back 本地 + match_source=local_fuzzy。"""
        png = self._fake_upload_file('fail.png')
        from unittest import mock
        from blueprints.ocr_engine import _engine_cache
        _engine_cache.clear()
        with mock.patch('blueprints.shipping.get_ocr_engine') as mock_factory:
            fake_paddle = mock.MagicMock()
            fake_paddle.extract_text.return_value = '环保杂胶 0.8mm 加面 黑色'
            # 2026-08-10 Task 6:同步 mock extract_text_with_conf
            fake_paddle.extract_text_with_conf.return_value = ('环保杂胶 0.8mm 加面 黑色', 0.95)
            fake_ds = mock.MagicMock()
            fake_ds.API_KEY = 'sk-test'
            fake_ds.compare_single_record.side_effect = Exception('云端超时')
            mock_factory.side_effect = lambda n: fake_paddle if n == 'paddleocr' else fake_ds
            with mock.patch('blueprints.ocr_pipeline.match_label_to_row',
                            return_value=('green', 88.0, '本地补算理由')) as mock_local:
                res = self.client.post(
                    f'/api/v1/shipping-orders/records/{self.rid}/images',
                    data={'image': png}, content_type='multipart/form-data',
                )
                self.assertEqual(res.status_code, 201)
                data = res.get_json()
                self._wait_async(data['images'][0]['image_id'])
                rec = self._db_image(data['images'][0]['image_id'])
                self.assertEqual(rec['match_status'], 'green')
                self.assertEqual(rec['match_source'], 'local_fuzzy',
                    'DeepSeek 失败时 match_source 应为 local_fuzzy(fallback)')
                self.assertEqual(rec['reason'], '本地补算理由')
                mock_local.assert_called_once()

    def test_ocr_empty_text_skips_both_engines(self):
        """PaddleOCR 抽不出文字时,不调 DeepSeek 也不调本地匹配(match_status NULL)。"""
        from unittest import mock
        from blueprints.ocr_engine import _engine_cache
        _engine_cache.clear()
        with mock.patch('blueprints.shipping.get_ocr_engine') as mock_factory:
            fake_paddle = mock.MagicMock()
            fake_paddle.extract_text.return_value = ''  # OCR 空
            # 2026-08-10 Task 6:同步 mock extract_text_with_conf
            fake_paddle.extract_text_with_conf.return_value = ('', 1.0)
            fake_ds = mock.MagicMock()
            fake_ds.API_KEY = 'sk-test'
            fake_ds.compare_single_record.return_value = {'match_status': 'green'}
            mock_factory.side_effect = lambda n: fake_paddle if n == 'paddleocr' else fake_ds
            with mock.patch('blueprints.ocr_pipeline.match_label_to_row') as mock_local:
                res = self.client.post(
                    f'/api/v1/shipping-orders/records/{self.rid}/images',
                    data={'image': self._fake_upload_file('empty.png')},
                    content_type='multipart/form-data',
                )
                self.assertEqual(res.status_code, 201)
                data = res.get_json()
                # 必须等后台线程跑完再断言"没被调用",否则线程还没起步,
                # assert_not_called 会假通过。
                self._wait_async(data['images'][0]['image_id'])
                rec = self._db_image(data['images'][0]['image_id'])
                self.assertIsNone(rec['match_status'],
                    'OCR 空时 match_status 应为 None(不打徽章)')
                # DeepSeek / 本地都不该被调
                fake_ds.compare_single_record.assert_not_called()
                mock_local.assert_not_called()


class DeepSeekEngineSingleRecordTests(_TempDb):
    """DeepSeekEngine.compare_single_record(ocr_text, record) 应只比单 record, 不发全单。"""

    def test_compare_single_record_uses_single_record_prompt(self):
        from unittest import mock
        from blueprints.ocr_engine import DeepSeekEngine
        eng = DeepSeekEngine()
        with mock.patch.object(eng, '_call_api_with_prompt') as mock_api:
            mock_api.return_value = {"match_status":"green","reason":"OK"}
            eng.compare_single_record('OCR文字', {'product_name': 'A', 'specification': 'B'})
            # 验证调用时 prompt 只含 1 行(不是 [{...}, {...}])
            call_args = mock_api.call_args
            # kwargs 或 args 取 prompt
            prompt = call_args.kwargs.get('prompt') if call_args.kwargs else None
            if prompt is None:
                prompt = call_args.args[0] if call_args.args else ''
            # 提示词里只描述 1 条记录
            self.assertIn('【明细行】', prompt)
            # 明细行 JSON 段里只有 1 条 record(因为是单 record 比对)
            # COMPARE_PROMPT 模板本身含 'record_id' 关键字,不能直接 count
            # 改成:从【明细行】后截 JSON 段,只解到第 1 个数组,验证 list len
            import json as _json
            after_marker = prompt[prompt.index('【明细行】') + len('【明细行】'):]
            # 截到下一个空行或文件末尾之前(避免吞掉自适应 layer 段)
            detail_part = after_marker.split('\n\n', 1)[0].strip()
            detail_rows = _json.loads(detail_part)
            self.assertIsInstance(detail_rows, dict, '单 record 时【明细行】段是单条 dict 而非数组')
            self.assertIn('product_name', detail_rows)
            self.assertIn('specification', detail_rows)

    def test_compare_single_record_parses_ai_status(self):
        from unittest import mock
        from blueprints.ocr_engine import DeepSeekEngine
        eng = DeepSeekEngine()
        with mock.patch.object(eng, '_call_api_with_prompt') as mock_api:
            mock_api.return_value = {"match_status":"YELLOW","reason":"黄色"}
            result = eng.compare_single_record('OCR', {'product_name': 'X', 'specification': 'Y'})
            self.assertEqual(result['match_status'], 'yellow')
            self.assertEqual(result['reason'], '黄色')

    def test_compare_single_record_no_supplement_keeps_prompt_clean(self):
        """supplement_prompt='' (默认) → 不应出现【自适应提示词】段标识。"""
        from unittest import mock
        from blueprints.ocr_engine import DeepSeekEngine
        eng = DeepSeekEngine()
        with mock.patch.object(eng, '_call_api_with_prompt') as mock_api:
            mock_api.return_value = {"match_status":"green","reason":"OK"}
            result = eng.compare_single_record('OCR', {'product_name': 'X', 'specification': 'Y'})
            self.assertNotIn('【自适应提示词', result['prompt_text'])
            self.assertNotIn('同品类 / 同规格历史人工案例', result['prompt_text'])

    def test_compare_single_record_with_supplement_appends_block(self):
        """supplement_prompt 非空 → prompt_text 末尾追加「自适应提示词」段(与 compare_rows 同格式)。"""
        from unittest import mock
        from blueprints.ocr_engine import DeepSeekEngine
        eng = DeepSeekEngine()
        supplement = '## 大类补充提示词\n- [red→人工确认] 厚度 ±0.1mm 可放宽'
        with mock.patch.object(eng, '_call_api_with_prompt') as mock_api:
            mock_api.return_value = {"match_status":"yellow","reason":"OK"}
            result = eng.compare_single_record('OCR文字', {'product_name': 'A', 'specification': 'B'},
                                              supplement_prompt=supplement)
            self.assertIn('【自适应提示词', result['prompt_text'])
            self.assertIn(supplement, result['prompt_text'])
            # 自适应段应在明细行之后
            self.assertGreater(result['prompt_text'].index('【自适应提示词'),
                               result['prompt_text'].index('【明细行】'))

    def test_compare_single_record_empty_supplement_string_skips_block(self):
        """supplement_prompt='' 显式传 → 与不传一致。"""
        from unittest import mock
        from blueprints.ocr_engine import DeepSeekEngine
        eng = DeepSeekEngine()
        with mock.patch.object(eng, '_call_api_with_prompt') as mock_api:
            mock_api.return_value = {"match_status":"green","reason":"OK"}
            result = eng.compare_single_record('OCR', {'product_name': 'X', 'specification': 'Y'},
                                              supplement_prompt='')
            self.assertNotIn('【自适应提示词', result['prompt_text'])


if __name__ == '__main__':
    unittest.main()