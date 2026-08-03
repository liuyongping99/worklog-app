"""图片 OCR/prompt/raw 详情端点测试。

需求:
- 出货页每张商品行级别图,在 AI 判据图标(⚠/✓/✗)右侧加「详情」按钮
- 点击 → 模态显示 3 段信息:
  1) OCR 原文  (来自 record_ocr event)
  2) 提示词     (来自 ai_match event 的 prompt_payload,全 record 共用一份)
  3) 推理结果   (来自 ai_match event 的 ai_raw_response + ai_match_reason)
- 如有人工核查 → 显示 human_status / human_reason
"""
import os
import sys
import tempfile
import unittest

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
        from models.tasks_flow import StaffDB
        from app import create_app
        self.client = create_app().test_client()
        sid = StaffDB.create('test', '调度')['id']
        with self.client.session_transaction() as s:
            s['operator_id'] = sid

    def tearDown(self):
        _db.DB_PATH = self._orig
        for ext in ('', '-wal', '-shm'):
            p = self.tmp.name + ext
            if os.path.exists(p):
                try: os.unlink(p)
                except OSError: pass


class ImageOcrDetailApiTests(_TempDb):
    def _seed(self):
        """造一张图 + 三类 ocr_match_event 事件。"""
        from models import (ShippingOrder, ShippingRecord, ShippingImage,
                            OcrMatchEvent)
        oid = ShippingOrder.create('2026-07-30', 'C')
        rid = ShippingRecord.create('2026-07-30', 'C', '环保杂胶', '0.8黑中加面', '50', 'y', '', oid)
        iid = ShippingImage.create(oid, 'upload/2026-07/x.png', 'x.png', 'ai', record_pk=rid)
        # 1. record_ocr: OCR 文字 + 本地模糊匹配
        OcrMatchEvent.create(
            'record_ocr', record_id=rid, order_id=oid, image_id=iid,
            ocr_text='环保杂胶 0.8mm 加面 黑色',
            ocr_engine='paddleocr',
            ai_engine='local_fuzzy',
            ai_match_status='red',
            ai_match_reason='本地模糊匹配 50分',
            product_name='环保杂胶', specification='0.8黑中加面',
        )
        # 2. ai_match: DeepSeek 调用(同 record 共享)
        OcrMatchEvent.create(
            'ai_match', record_id=rid, order_id=oid, image_id=None,
            ocr_text='环保杂胶 0.8mm 加面 黑色',
            ocr_engine='paddleocr',
            prompt_payload='COMPARE_PROMPT compare_rows_v2: ...【明细行】...',
            ai_raw_response='{"record_id":%d,"match_status":"yellow","reason":"OCR中部分命中"}' % rid,
            ai_match_status='yellow',
            ai_match_reason='OCR中部分命中',
            ai_engine='deepseek',
            prompt_version='compare_rows_v2',
            product_name='环保杂胶', specification='0.8黑中加面',
        )
        # 3. human_verify
        OcrMatchEvent.create(
            'human_verify', record_id=rid, order_id=oid, image_id=iid,
            ai_match_status='yellow',
            ai_match_reason='OCR中部分命中',
            human_status='green',
            human_reason='人工:实际是 0.8 加面,AI 把 0.8 误读为 0.6',
            human_verified_by=1,
        )
        return oid, rid, iid

    def test_returns_404_on_missing_image(self):
        res = self.client.get('/api/v1/shipping-orders/images/999999/ocr-detail')
        self.assertEqual(res.status_code, 404)

    def test_returns_image_meta_and_event_snapshots(self):
        oid, rid, iid = self._seed()
        res = self.client.get(f'/api/v1/shipping-orders/images/{iid}/ocr-detail')
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data.get('success'))
        d = data['detail']
        # 图片基本元信息
        self.assertEqual(d['image_id'], iid)
        self.assertEqual(d['record_id'], rid)
        self.assertEqual(d['order_id'], oid)
        self.assertEqual(d['product_name'], '环保杂胶')
        self.assertEqual(d['specification'], '0.8黑中加面')
        # OCR 段:取 record_ocr 最新事件
        self.assertEqual(d['ocr']['ocr_text'], '环保杂胶 0.8mm 加面 黑色')
        self.assertEqual(d['ocr']['ocr_engine'], 'paddleocr')
        # AI 段:取 ai_match 最新事件(同 record)
        self.assertEqual(d['ai']['ai_engine'], 'deepseek')
        self.assertEqual(d['ai']['prompt_version'], 'compare_rows_v2')
        self.assertEqual(d['ai']['ai_match_status'], 'yellow')
        self.assertIn('COMPARE_PROMPT compare_rows_v2', d['ai']['prompt_payload'])
        self.assertIn('OCR中部分命中', d['ai']['ai_raw_response'])
        # 人工段:取 human_verify 最新事件
        self.assertEqual(d['human']['human_status'], 'green')
        self.assertEqual(d['human']['human_reason'], '人工:实际是 0.8 加面,AI 把 0.8 误读为 0.6')

    def test_effective_match_status_yellow_with_human_verified_becomes_green(self):
        """2026-07-30 UX 一致性修复:
        shipping_images.match_status='yellow' + human_verified=1 →
        effective_match_status 应升级为 green,与出货页行 AI 比对列徽章一致。
        """
        from models import (ShippingOrder, ShippingRecord, ShippingImage,
                            OcrMatchEvent)
        oid = ShippingOrder.create('2026-07-30', 'A')
        rid = ShippingRecord.create('2026-07-30', 'A', '杂胶', '0.6', '50', 'y', '', oid)
        iid = ShippingImage.create(oid, 'upload/2026-07/y.png', 'y.png', 'ai', record_pk=rid)
        ShippingImage.set_match(iid, 'yellow', 0.5, 'OCR 部分命中')
        ShippingImage.set_human_verified(iid, True)

        res = self.client.get(f'/api/v1/shipping-orders/images/{iid}/ocr-detail')
        d = res.get_json()['detail']
        self.assertEqual(d['match_status'], 'yellow', '原始 AI 裁决保留为 yellow (审计可追溯)')
        self.assertEqual(d['effective_match_status'], 'green',
            'effective_match_status 应升级为 green (与行升级一致)')
        self.assertTrue(d['human_verified'])

    def test_effective_match_status_red_with_human_verified_becomes_green(self):
        """red + human_verified=1 → effective_match_status 也升级为 green。"""
        from models import (ShippingOrder, ShippingRecord, ShippingImage)
        oid = ShippingOrder.create('2026-07-30', 'B')
        rid = ShippingRecord.create('2026-07-30', 'B', '杂胶', '0.8', '50', 'y', '', oid)
        iid = ShippingImage.create(oid, 'upload/2026-07/r.png', 'r.png', 'ai', record_pk=rid)
        ShippingImage.set_match(iid, 'red', 0.3, 'OCR 不匹配')
        ShippingImage.set_human_verified(iid, True)
        res = self.client.get(f'/api/v1/shipping-orders/images/{iid}/ocr-detail')
        d = res.get_json()['detail']
        self.assertEqual(d['match_status'], 'red')
        self.assertEqual(d['effective_match_status'], 'green')

    def test_effective_match_status_yellow_without_human_verified_stays_yellow(self):
        """yellow + human_verified=0 → effective_match_status 仍为 yellow。"""
        from models import (ShippingOrder, ShippingRecord, ShippingImage)
        oid = ShippingOrder.create('2026-07-30', 'C')
        rid = ShippingRecord.create('2026-07-30', 'C', '纯胶', '0.6', '50', 'y', '', oid)
        iid = ShippingImage.create(oid, 'upload/2026-07/p.png', 'p.png', 'ai', record_pk=rid)
        ShippingImage.set_match(iid, 'yellow', 0.5, 'OCR 部分命中')
        res = self.client.get(f'/api/v1/shipping-orders/images/{iid}/ocr-detail')
        d = res.get_json()['detail']
        self.assertEqual(d['match_status'], 'yellow')
        self.assertEqual(d['effective_match_status'], 'yellow')
        self.assertFalse(d['human_verified'])

    def test_effective_match_status_green_stays_green(self):
        """green + human_verified=0/1 → 都仍是 green。"""
        from models import (ShippingOrder, ShippingRecord, ShippingImage)
        oid = ShippingOrder.create('2026-07-30', 'D')
        rid = ShippingRecord.create('2026-07-30', 'D', '绿胶', '0.6', '50', 'y', '', oid)
        iid = ShippingImage.create(oid, 'upload/2026-07/g.png', 'g.png', 'ai', record_pk=rid)
        ShippingImage.set_match(iid, 'green', 0.95, 'OCR 完全匹配')
        res = self.client.get(f'/api/v1/shipping-orders/images/{iid}/ocr-detail')
        d = res.get_json()['detail']
        self.assertEqual(d['effective_match_status'], 'green')

    def test_returns_nulls_when_no_events(self):
        """图存在但 ocr_match_event 无任何事件 → 三段都为 null(不报错)。"""
        from models import ShippingOrder, ShippingRecord, ShippingImage
        oid = ShippingOrder.create('2026-07-30', 'C2')
        rid = ShippingRecord.create('2026-07-30', 'C2', '纯胶', '0.6白', '10', 'y', '', oid)
        iid = ShippingImage.create(oid, 'upload/2026-07/y.png', 'y.png', 'upload', record_pk=rid)
        res = self.client.get(f'/api/v1/shipping-orders/images/{iid}/ocr-detail')
        self.assertEqual(res.status_code, 200)
        d = res.get_json()['detail']
        self.assertIsNone(d['ocr'])
        self.assertIsNone(d['ai'])
        self.assertIsNone(d['human'])

    def test_picks_latest_event_per_type(self):
        """同类型多次事件时取最新一条(按 created_at DESC, id DESC 兜底)。"""
        from models import (ShippingOrder, ShippingRecord, ShippingImage,
                            OcrMatchEvent)
        oid = ShippingOrder.create('2026-07-30', 'C3')
        rid = ShippingRecord.create('2026-07-30', 'C3', '杂胶', '0.8', '50', 'y', '', oid)
        iid = ShippingImage.create(oid, 'upload/2026-07/z.png', 'z.png', 'ai', record_pk=rid)
        # 两条 record_ocr: 第一条旧,第二条新
        OcrMatchEvent.create('record_ocr', record_id=rid, order_id=oid, image_id=iid,
                             ocr_text='OLD OCR', ocr_engine='paddleocr', ai_engine='local_fuzzy',
                             product_name='杂胶', specification='0.8')
        # 让时间戳差:第二条延后 1 秒
        import time; time.sleep(1.1)
        OcrMatchEvent.create('record_ocr', record_id=rid, order_id=oid, image_id=iid,
                             ocr_text='NEW OCR', ocr_engine='paddleocr', ai_engine='local_fuzzy',
                             product_name='杂胶', specification='0.8')
        res = self.client.get(f'/api/v1/shipping-orders/images/{iid}/ocr-detail')
        d = res.get_json()['detail']
        self.assertEqual(d['ocr']['ocr_text'], 'NEW OCR')


if __name__ == '__main__':
    unittest.main()