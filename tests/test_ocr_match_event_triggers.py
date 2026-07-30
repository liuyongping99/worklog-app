"""出货页 record_ocr/ai_match/human_verify 三类事件写库触发点"""
import io
import os
import sys
import unittest
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models._db as _db


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        self._orig = _db.DB_PATH
        _db.DB_PATH = self.tmp.name
        from models import init_db, ShippingOrder, ShippingRecord
        init_db()
        self.oid = ShippingOrder.create('2026-07-25', 'C')
        self.rid = ShippingRecord.create('2026-07-25', 'C', '硬加面', '黑色', '1', 'y', '', self.oid)
        from app import create_app
        self.client = create_app().test_client()
        # 清 paddleocr 缓存防止污染
        try:
            from blueprints.ocr_engine import _engine_cache
            _engine_cache.pop('paddleocr', None)
        except Exception:
            pass

    def tearDown(self):
        _db.DB_PATH = self._orig
        for ext in ('', '-wal', '-shm'):
            p = self.tmp.name + ext
            if os.path.exists(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass


class AiMatchEventTriggerTests(_Base):
    @mock.patch('blueprints.shipping.PaddleOCREngine')
    @mock.patch('blueprints.shipping.get_ocr_engine')
    def test_ai_match_writes_per_record(self, mock_factory, mock_paddle_class):
        """整单 ai-match 给 N 行写 N 条 ai_match 事件(prompt + raw_response + ai_engine=deepseek)。"""
        # 准备:再来一条 record 让本次 ai-match 有 2 行 verdict
        from models import ShippingRecord, ShippingImage
        from blueprints.shipping import BASE_DIR
        rid2 = ShippingRecord.create('2026-07-25', 'C', '别品', '白', '2', 'y', '', self.oid)

        # 上传图(让 ai-match 有图可 OCR)
        upload_dir = os.path.join(BASE_DIR, 'upload', '2026-07')
        os.makedirs(upload_dir, exist_ok=True)
        abspath = os.path.join(upload_dir, 'match.png')
        with open(abspath, 'wb') as f:
            f.write(b'\x89PNG\r\n\x1a\n' + b'0' * 64)
        ShippingImage.create(order_pk=self.oid, file_path='upload/2026-07/match.png', record_pk=self.rid)
        ShippingImage.create(order_pk=self.oid, file_path='upload/2026-07/match.png', record_pk=rid2)

        # mock paddle + deepseek(同时覆盖 clean 基线的 PaddleOCREngine() 直接构造 和 get_ocr_engine 工厂)
        fake_paddle = mock.MagicMock()
        fake_paddle.extract_text.return_value = '硬加面 黑色 别品 白'
        mock_paddle_class.return_value = fake_paddle
        fake_ds = mock.MagicMock()
        # compare_rows 新签名: 返回 {'verdicts': [...], 'prompt': '<完整 prompt 文本>'}
        fake_ds.compare_rows.return_value = {
            'verdicts': [
                {'record_id': self.rid, 'match_status': 'green', 'reason': '命中'},
                {'record_id': rid2,      'match_status': 'red',   'reason': '规格不符'},
            ],
            'prompt': 'COMPARE_PROMPT_PLACEHOLDER\n【OCR文字】\n硬加面 黑色 别品 白\n【明细行】\n...',
        }
        mock_factory.side_effect = lambda name: fake_paddle if name == 'paddleocr' else fake_ds

        from models import OcrMatchEvent
        resp = self.client.post(f'/api/v1/shipping-orders/{self.oid}/ai-match')
        self.assertEqual(resp.status_code, 200, resp.get_json())

        events = OcrMatchEvent.get_by_order(self.oid, event_type='ai_match')
        self.assertEqual(len(events), 2, '每个 record 一条 ai_match')

        statuses = sorted(e['ai_match_status'] for e in events)
        self.assertEqual(statuses, ['green', 'red'])
        # 每条都有 prompt_payload + ai_raw_response + prompt_version
        for e in events:
            self.assertIsNotNone(e['prompt_payload'])
            self.assertIsNotNone(e['ai_raw_response'])
            self.assertEqual(e['ai_engine'], 'deepseek')
            # prompt_version 用源码常量断言(2026-07-30 bump v1→v2 后改成动态引用)
            import blueprints.ocr_engine as _oe
            self.assertEqual(e['prompt_version'], _oe.OCR_MATCH_PROMPT_VERSION)
            # 历史 baseline:本测试在 v1 时代硬编码了 'compare_rows_v1' —— 留这个字符串
            # 作为基线锚点,audit 页面要靠它和 v2 对比一致率。改了常量就要更新这里。
            self.assertIn('compare_rows_v', _oe.OCR_MATCH_PROMPT_VERSION)
            self.assertIn(_oe.OCR_MATCH_PROMPT_VERSION, ('compare_rows_v1', 'compare_rows_v2'),
                '常量已迁移到 v3+?请同步更新历史 baseline:rules/test_compare_prompt_v2_rule1.py 也有断言')

        # 清理磁盘
        try:
            os.unlink(abspath)
        except OSError:
            pass


class RecordOcrTriggerTests(_Base):
    @mock.patch('blueprints.shipping.get_ocr_engine')
    def test_record_ocr_event_on_multipart_upload(self, mock_factory):
        """multipart 行级上传成功后,写一条 record_ocr 事件(image_id+ocr_engine=local_fuzzy)。"""
        fake_paddle = mock.MagicMock()
        fake_paddle.extract_text.return_value = '硬加面 黑色 1.5M'
        mock_factory.side_effect = lambda name: fake_paddle if name == 'paddleocr' else mock.DEFAULT

        from models import OcrMatchEvent, ShippingImage
        from blueprints.shipping import BASE_DIR
        data = {'image': (io.BytesIO(b'\x89PNG\r\n\x1a\n' + b'0' * 64), 'label.png')}
        resp = self.client.post(
            f'/api/v1/shipping-orders/records/{self.rid}/images',
            data=data, content_type='multipart/form-data',
        )
        self.assertEqual(resp.status_code, 201, resp.get_json())

        events = OcrMatchEvent.get_by_record(self.rid)
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual(e['event_type'], 'record_ocr')
        self.assertIsNotNone(e['image_id'])
        self.assertEqual(e['ai_engine'], 'local_fuzzy')
        self.assertEqual(e['ai_match_status'], 'green')  # 文字"硬加面 黑色 1.5M"命中
        self.assertIsNotNone(e['ocr_text'])

        # 清理磁盘上的测试图
        img = ShippingImage.get_by_record(self.rid)[0]
        abspath = os.path.join(BASE_DIR, img['file_path'].replace('/', os.sep))
        try:
            os.unlink(abspath)
        except OSError:
            pass

    @mock.patch('blueprints.shipping.get_ocr_engine')
    def test_record_ocr_event_on_base64_upload(self, mock_factory):
        """base64 行级上传(JSON)成功后,同样写一条 record_ocr 事件。"""
        fake_paddle = mock.MagicMock()
        fake_paddle.extract_text.return_value = ''
        mock_factory.side_effect = lambda name: fake_paddle if name == 'paddleocr' else mock.DEFAULT

        import base64
        from models import OcrMatchEvent
        b64 = base64.b64encode(b'\x89PNG\r\n\x1a\n' + b'0' * 64).decode()
        data_url = f'data:image/png;base64,{b64}'
        resp = self.client.post(
            f'/api/v1/shipping-orders/records/{self.rid}/images',
            json={'image': data_url},
        )
        self.assertEqual(resp.status_code, 201, resp.get_json())

        events = OcrMatchEvent.get_by_record(self.rid)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['event_type'], 'record_ocr')
        # OCR 提取空字符串 → ai_status 应为 NULL(信号"OCR 失败"而非"不符")
        self.assertIsNone(events[0]['ai_match_status'])


class HumanVerifyEventTriggerTests(_Base):
    def test_human_verify_writes_event(self):
        """人工核查后写 human_verify 事件,含 AI 之前怎么说的快照。"""
        from models import ShippingImage, OcrMatchEvent
        from models.tasks_flow import StaffDB
        from blueprints.shipping import BASE_DIR
        # 准备:上传一张图,设 AI=红牌,设人工=绿牌通过
        upload_dir = os.path.join(BASE_DIR, 'upload', '2026-07')
        os.makedirs(upload_dir, exist_ok=True)
        abspath = os.path.join(upload_dir, 'v.png')
        with open(abspath, 'wb') as f:
            f.write(b'\x89PNG\r\n\x1a\n' + b'0' * 64)
        iid = ShippingImage.create(order_pk=self.oid, file_path='upload/2026-07/v.png', record_pk=self.rid)
        ShippingImage.set_match(iid, 'red', 30.0, 'AI 觉得规格冲突')

        # 登录拿到 staff_id
        sid = StaffDB.create('测试员', '调度')['id']
        with self.client.session_transaction() as s:
            s['operator_id'] = sid

        # 触发 manual-verify (verified=true)
        resp = self.client.post(f'/api/v1/shipping-orders/images/{iid}/manual-verify',
                                json={'verified': True})
        self.assertEqual(resp.status_code, 200)

        events = OcrMatchEvent.get_by_record(self.rid, event_type='human_verify')
        self.assertEqual(len(events), 1)
        e = events[0]
        self.assertEqual(e['human_status'], 'green')
        self.assertEqual(e['human_verified_by'], sid)
        # AI 之前的裁决也快照进了事件(便于算 delta)
        self.assertEqual(e['ai_match_status'], 'red')
        self.assertEqual(e['ai_match_reason'], 'AI 觉得规格冲突')
        self.assertEqual(e['image_id'], iid)

        # 清理
        try:
            os.unlink(abspath)
        except OSError:
            pass

    def test_human_verify_unverify_also_records(self):
        """取消核查(verified=false) 也写一条记录。"""
        from models import ShippingImage, OcrMatchEvent
        from models.tasks_flow import StaffDB
        from blueprints.shipping import BASE_DIR
        upload_dir = os.path.join(BASE_DIR, 'upload', '2026-07')
        os.makedirs(upload_dir, exist_ok=True)
        abspath = os.path.join(upload_dir, 'u.png')
        with open(abspath, 'wb') as f:
            f.write(b'\x89PNG\r\n\x1a\n' + b'0' * 64)
        iid = ShippingImage.create(order_pk=self.oid, file_path='upload/2026-07/u.png', record_pk=self.rid)
        sid = StaffDB.create('测试员', '调度')['id']
        with self.client.session_transaction() as s:
            s['operator_id'] = sid

        resp = self.client.post(f'/api/v1/shipping-orders/images/{iid}/manual-verify',
                                json={'verified': False})
        self.assertEqual(resp.status_code, 200)
        events = OcrMatchEvent.get_by_record(self.rid, event_type='human_verify')
        self.assertEqual(len(events), 1)
        self.assertIsNone(events[0]['human_status'])  # 取消核查不指定 human_status

        try:
            os.unlink(abspath)
        except OSError:
            pass


class WriteFailureResilienceTests(_Base):
    """防御测试:即便 OcrMatchEvent.create 抛错(模拟 DB 故障/重构漏 try/except),
    主端点仍按正常路径返 200/201,事件丢失是审计可接受的代价。"""

    @mock.patch('blueprints.shipping.get_ocr_engine')
    def test_record_ocr_event_db_failure_doesnt_block(self, mock_factory):
        """multipart 上传时事件写库失败,主端点仍返 201。"""
        # 1) 让 OCR 跑成功
        fake_paddle = mock.MagicMock()
        fake_paddle.extract_text.return_value = '硬加面 黑色'
        mock_factory.side_effect = lambda name: fake_paddle if name == 'paddleocr' else mock.DEFAULT

        # 2) 在事件写库时让 OcrMatchEvent.create 抛 DB 错误
        from models import OcrMatchEvent, ShippingImage
        from blueprints.shipping import BASE_DIR
        def boom(*a, **kw):
            raise RuntimeError('db connection lost')
        with mock.patch.object(OcrMatchEvent, 'create', side_effect=boom):
            data = {'image': (io.BytesIO(b'\x89PNG\r\n\x1a\n' + b'0' * 64), 'x.png')}
            resp = self.client.post(
                f'/api/v1/shipping-orders/records/{self.rid}/images',
                data=data, content_type='multipart/form-data',
            )
        self.assertEqual(resp.status_code, 201, '主端点不应被审计失败阻断')

        # 清理
        try:
            imgs = ShippingImage.get_by_record(self.rid)
            for i in imgs:
                abspath = os.path.join(BASE_DIR, i['file_path'].replace('/', os.sep))
                try:
                    os.unlink(abspath)
                except OSError:
                    pass
        except Exception:
            pass


class CascadeTests(_Base):
    """FK ON DELETE CASCADE 验证:删 shipping_record 时,关联 ocr_match_event 自动清掉。"""

    def test_delete_record_cascades_to_events(self):
        from models import ShippingRecord, OcrMatchEvent
        # 先写 3 条
        OcrMatchEvent.create('record_ocr', self.rid, self.oid, ai_match_status='green')
        OcrMatchEvent.create('ai_match', self.rid, self.oid, ai_match_status='red')
        OcrMatchEvent.create('human_verify', self.rid, self.oid, human_status='green')
        self.assertEqual(len(OcrMatchEvent.get_by_record(self.rid)), 3)

        # 删 record
        ShippingRecord.delete(self.rid)

        # 应为 0
        self.assertEqual(len(OcrMatchEvent.get_by_record(self.rid)), 0,
                         'FK CASCADE 应自动清理事件')


if __name__ == '__main__':
    unittest.main()
