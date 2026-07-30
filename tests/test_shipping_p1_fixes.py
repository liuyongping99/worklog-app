"""P1 逻辑缺陷批 回归测试。

覆盖:
- P1-5  复合 PATCH: 仅改备注不改序号; 移组取 MAX+1(排除自身)
- P1-7  OCR 空文本不判红牌(不打徽章)
- P1-8a manual-verify 严格布尔("false"/"0" → 取消确认)
- P1-8b ai-match 重跑清除旧 human_verified
- P1-8c 行级徽章聚合: 突显最差(未确认红牌盖过绿牌)
- P1-9  行级上传响应包含 reason
- P1-10 批量新增原子化 + 报告跳过 + 全无效返回 400
- P1-11 multipart 文件名单点(uuid.png,非 uuid..png)
"""
import io
import os
import re
import sys
import unittest
import tempfile
from html.parser import HTMLParser
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models._db as _db


def rendered_match_statuses(html):
    """只从服务端渲染的 <td class="match-col"> 单元格里取 data-match-status。

    不用纯文本 assertIn / HTMLParser —— 页面 <script> 里有徽章模板字符串,
    且 JS 中 </script> 拆分会让 HTMLParser 的 CDATA 模式提前退出而误解析。
    只匹配真实表格单元格最稳。
    """
    vals = []
    for cell in re.findall(r'<td class="match-col">(.*?)</td>', html, re.S):
        m = re.search(r'data-match-status="(\w+)"', cell)
        if m:
            vals.append(m.group(1))
    return vals


class P1Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        self._orig = _db.DB_PATH
        _db.DB_PATH = self.tmp.name
        from models import init_db
        init_db()
        from app import create_app
        self.client = create_app().test_client()
        self._cleanup_files = []

    def tearDown(self):
        _db.DB_PATH = self._orig
        os.unlink(self.tmp.name)
        for p in self._cleanup_files:
            try:
                os.unlink(p)
            except OSError:
                pass


class CompoundPatchTests(P1Base):
    def test_note_save_keeps_order_num(self):
        """仅保存备注(日期/客户未变)时,order_num 不得被 +1。"""
        from models import ShippingOrder
        o1 = ShippingOrder.create('2026-07-25', 'C')
        ShippingOrder.create('2026-07-25', 'C')  # 同组第二单,order_num=2
        before = ShippingOrder.get_by_id(o1)['order_num']
        resp = self.client.patch(f'/api/v1/shipping-orders/{o1}',
                                 json={'order_note': 'hi', 'customer': 'C', 'date': '2026-07-25'})
        self.assertEqual(resp.status_code, 200)
        after = ShippingOrder.get_by_id(o1)['order_num']
        self.assertEqual(before, after)
        self.assertEqual(resp.get_json()['order_num'], before)

    def test_move_to_new_group_gets_maxplus1(self):
        """改到新分组时,order_num 取目标组 MAX+1。"""
        from models import ShippingOrder
        o1 = ShippingOrder.create('2026-07-25', 'A')
        ShippingOrder.create('2026-07-26', 'B')  # 目标组已有 order_num=1
        resp = self.client.patch(f'/api/v1/shipping-orders/{o1}',
                                 json={'order_note': 'x', 'customer': 'B', 'date': '2026-07-26'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.get_json()['order_num'], 2)


class ManualVerifyTests(P1Base):
    def _mk_image(self):
        from models import ShippingOrder, ShippingRecord, ShippingImage
        oid = ShippingOrder.create('2026-07-25', 'C')
        rid = ShippingRecord.create('2026-07-25', 'C', 'P', 'S', '1', 'y', '', oid)
        iid = ShippingImage.create(order_pk=oid, file_path='upload/2026-07/v.png', record_pk=rid)
        return oid, rid, iid

    def test_verified_false_string_cancels(self):
        """{'verified': 'false'} 必须被当作取消确认(human_verified=0),不能 bool('false')=True。"""
        from models import ShippingImage
        _, _, iid = self._mk_image()
        ShippingImage.set_human_verified(iid, True)
        resp = self.client.post(f'/api/v1/shipping-orders/images/{iid}/manual-verify',
                                json={'verified': 'false'})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(ShippingImage.get_by_id(iid)['human_verified'], 0)

    def test_verified_zero_cancels(self):
        from models import ShippingImage
        _, _, iid = self._mk_image()
        ShippingImage.set_human_verified(iid, True)
        resp = self.client.post(f'/api/v1/shipping-orders/images/{iid}/manual-verify',
                                json={'verified': 0})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(ShippingImage.get_by_id(iid)['human_verified'], 0)

    def test_default_true(self):
        from models import ShippingImage
        _, _, iid = self._mk_image()
        resp = self.client.post(f'/api/v1/shipping-orders/images/{iid}/manual-verify', json={})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(ShippingImage.get_by_id(iid)['human_verified'], 1)


class AiMatchClearsVerifyTests(P1Base):
    @mock.patch('blueprints.shipping.get_ocr_engine')
    def test_rerun_clears_human_verified(self, mock_get_engine):
        """重跑 AI 匹配应清掉旧的人工确认,让新裁决(仍红)重新暴露风险。"""
        from models import ShippingOrder, ShippingRecord, ShippingImage
        from blueprints.shipping import BASE_DIR
        oid = ShippingOrder.create('2026-07-25', 'C')
        rid = ShippingRecord.create('2026-07-25', 'C', 'P', 'S', '1', 'y', '', oid)
        upload_dir = os.path.join(BASE_DIR, 'upload', '2026-07')
        os.makedirs(upload_dir, exist_ok=True)
        abspath = os.path.join(upload_dir, 'ver.png')
        with open(abspath, 'wb') as f:
            f.write(b'\x89PNG\r\n\x1a\n' + b'0' * 64)
        self._cleanup_files.append(abspath)
        iid = ShippingImage.create(order_pk=oid, file_path='upload/2026-07/ver.png', record_pk=rid)
        ShippingImage.set_human_verified(iid, True)

        fake_paddle = mock.MagicMock()
        fake_paddle.extract_text.return_value = '文字'
        fake = mock.MagicMock()
        fake.compare_rows.return_value = [{'record_id': rid, 'match_status': 'red', 'reason': 'x'}]
        mock_get_engine.side_effect = lambda name: fake_paddle if name == 'paddleocr' else fake

        resp = self.client.post(f'/api/v1/shipping-orders/{oid}/ai-match')
        self.assertEqual(resp.status_code, 200)
        img = ShippingImage.get_by_id(iid)
        self.assertEqual(img['match_status'], 'red')
        self.assertEqual(img['human_verified'], 0)  # 被清掉


class BadgeAggregationTests(P1Base):
    def _login(self):
        from models.tasks_flow import StaffDB
        sid = StaffDB.create('测试员', '调度')['id']
        with self.client.session_transaction() as s:
            s['operator_id'] = sid

    def test_worst_wins_red_over_green(self):
        """一行有 green + red 两张图时,首屏行级徽章应显红(突显最差)。"""
        from models import ShippingOrder, ShippingRecord, ShippingImage
        oid = ShippingOrder.create('2026-07-25', 'C')
        rid = ShippingRecord.create('2026-07-25', 'C', 'P', 'S', '1', 'y', '', oid)
        g = ShippingImage.create(order_pk=oid, file_path='upload/2026-07/g.png', record_pk=rid)
        r = ShippingImage.create(order_pk=oid, file_path='upload/2026-07/r.png', record_pk=rid)
        ShippingImage.set_match(g, 'green', 90.0, 'ok')
        ShippingImage.set_match(r, 'red', 30.0, 'bad')
        self._login()
        html = self.client.get('/shipping-records?start_date=2026-07-25&end_date=2026-07-25').get_data(as_text=True)
        # 该行 match-col 应含红牌(突显最差),不应是绿牌
        statuses = rendered_match_statuses(html)
        self.assertIn('red', statuses)
        self.assertNotIn('green', statuses)

    def test_verified_red_downgraded(self):
        """人工确认过的红牌视为已解决,单张确认红牌 → 显 green。"""
        from models import ShippingOrder, ShippingRecord, ShippingImage
        oid = ShippingOrder.create('2026-07-25', 'C')
        rid = ShippingRecord.create('2026-07-25', 'C', 'P', 'S', '1', 'y', '', oid)
        r = ShippingImage.create(order_pk=oid, file_path='upload/2026-07/r.png', record_pk=rid)
        ShippingImage.set_match(r, 'red', 30.0, 'bad')
        ShippingImage.set_human_verified(r, True)
        self._login()
        html = self.client.get('/shipping-records?start_date=2026-07-25&end_date=2026-07-25').get_data(as_text=True)
        statuses = rendered_match_statuses(html)
        self.assertIn('green', statuses)
        self.assertNotIn('red', statuses)


class RecordUploadTests(P1Base):
    @mock.patch('blueprints.shipping.get_ocr_engine')
    def test_reason_in_response_and_single_dot_filename(self, mock_factory):
        """行级上传响应含 reason;multipart 文件名单点(uuid.png)。"""
        from models import ShippingOrder, ShippingRecord, ShippingImage
        oid = ShippingOrder.create('2026-07-25', 'C')
        rid = ShippingRecord.create('2026-07-25', 'C', '硬加面', '黑色', '1', 'y', '', oid)
        # NP0-1 之后走 get_ocr_engine('paddleocr') 单例;按引擎名分派 fake
        fake_paddle = mock.MagicMock()
        fake_paddle.extract_text.return_value = '硬加面 黑色'
        mock_factory.side_effect = lambda name: fake_paddle if name == 'paddleocr' else mock.DEFAULT

        data = {'image': (io.BytesIO(b'\x89PNG\r\n\x1a\n' + b'0' * 64), 'label.png')}
        resp = self.client.post(f'/api/v1/shipping-orders/records/{rid}/images',
                                data=data, content_type='multipart/form-data')
        self.assertEqual(resp.status_code, 201)
        img = resp.get_json()['images'][0]
        self.assertIn('reason', img)
        self.assertTrue(img['reason'])  # 命中应有 reason

        # 文件名单点校验
        saved = ShippingImage.get_by_record(rid)[0]
        fname = os.path.basename(saved['file_path'])
        self._cleanup_files.append(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            saved['file_path'].replace('/', os.sep)))
        self.assertRegex(fname, r'^[0-9a-f]{32}\.png$')
        self.assertNotIn('..', fname)


class BatchAddTests(P1Base):
    def test_all_invalid_returns_400(self):
        from models import ShippingOrder
        oid = ShippingOrder.create('2026-07-25', 'C')
        resp = self.client.post(f'/api/v1/shipping-orders/{oid}/records/batch',
                                json={'items': [{'product_name': '', 'quantity': ''},
                                                {'product_name': 'X', 'quantity': ''}]})
        self.assertEqual(resp.status_code, 400)
        self.assertEqual(resp.get_json()['skipped'], 2)

    def test_mixed_reports_skipped(self):
        from models import ShippingOrder, ShippingRecord
        oid = ShippingOrder.create('2026-07-25', 'C')
        resp = self.client.post(f'/api/v1/shipping-orders/{oid}/records/batch',
                                json={'items': [
                                    {'product_name': 'A', 'specification': 's', 'quantity': '10', 'unit': 'y'},
                                    {'product_name': '', 'quantity': '5'},  # 跳过
                                    {'product_name': 'B', 'quantity': 3, 'unit': '码'},  # 数量为 int + 码→y
                                ]})
        self.assertEqual(resp.status_code, 201)
        body = resp.get_json()
        self.assertEqual(body['count'], 2)
        self.assertEqual(body['skipped'], 1)
        # 码 → y 归一化
        units = {r['product_name']: r['unit'] for r in body['records']}
        self.assertEqual(units['B'], 'y')

    def test_batch_atomic_no_partial_on_error(self):
        """create_many 内部 INSERT 抛错时应整批回滚,不留半单。"""
        from models import ShippingOrder, ShippingRecord
        oid = ShippingOrder.create('2026-07-25', 'C')
        # 用真实路径验证原子性:第二条 INSERT 抛错,整批应回滚
        import models.orders as mo
        real_get_db = mo.get_db
        call = {'n': 0}

        class BoomConn:
            def __init__(self, real):
                self.real = real
            def cursor(self):
                return BoomCursor(self.real.cursor())
            def commit(self): return self.real.commit()
            def rollback(self): return self.real.rollback()
            def close(self): return self.real.close()

        class BoomCursor:
            def __init__(self, real):
                self.real = real
            def execute(self, sql, params=()):
                if sql.strip().startswith('INSERT'):
                    call['n'] += 1
                    if call['n'] == 2:
                        raise RuntimeError('boom')
                return self.real.execute(sql, params)
            def fetchone(self):
                return self.real.fetchone()
            @property
            def lastrowid(self):
                return self.real.lastrowid

        with mock.patch('models.orders.get_db', lambda: BoomConn(real_get_db())):
            with self.assertRaises(RuntimeError):
                ShippingRecord.create_many(oid, [
                    {'product_name': 'A', 'quantity': '1'},
                    {'product_name': 'B', 'quantity': '2'},
                ])
        # 回滚后应无任何明细
        groups = ShippingRecord.get_groups('2026-07-25', '2026-07-25')
        recs = [r for g in groups for r in g['records']]
        self.assertEqual(len(recs), 0)


class OcrEmptyTextTests(P1Base):
    @mock.patch('blueprints.shipping.PaddleOCREngine')
    def test_empty_ocr_no_badge(self, MockPaddle):
        """OCR 提不出文字时,不得写红牌(match_status 保持空)。"""
        from models import ShippingOrder, ShippingRecord, ShippingImage
        oid = ShippingOrder.create('2026-07-25', 'C')
        rid = ShippingRecord.create('2026-07-25', 'C', '硬加面', '黑色', '1', 'y', '', oid)
        MockPaddle.return_value.extract_text.return_value = '   '  # 空白
        data = {'image': (io.BytesIO(b'\x89PNG\r\n\x1a\n' + b'0' * 64), 'x.png')}
        resp = self.client.post(f'/api/v1/shipping-orders/records/{rid}/images',
                                data=data, content_type='multipart/form-data')
        self.assertEqual(resp.status_code, 201)
        img = resp.get_json()['images'][0]
        self.assertIsNone(img['match_status'])  # 没打徽章
        saved = ShippingImage.get_by_record(rid)[0]
        self._cleanup_files.append(os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            saved['file_path'].replace('/', os.sep)))
        self.assertIsNone(saved['match_status'])


if __name__ == '__main__':
    unittest.main()
