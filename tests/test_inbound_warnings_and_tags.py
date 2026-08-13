"""入库 — AI 比对图例条 + source_tag 标签 + verified_warnings 按钮 + API 端点。

Task 5 of 2026-08-13 入库对齐出货：
- 页面底部渲染 AI 比对图例条 (含 ✓/⚠/✗ + 云端 DeepSeek ⊛ + 👤)
- .img-item-record 渲染 source_tag 标签 (紫色背景)
- 行下方警告区渲染 verified_warnings ✓/✗ 按钮 (addRowWarning 共享函数)
- POST /api/v1/inbound-orders/records/<id>/verify-warning 端点 roundtrip

运行：PYTHONUTF8=1 python -m pytest tests/test_inbound_warnings_and_tags.py -v
"""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models._db as _db


def _drain_async_jobs(timeout=15):
    """出货/入库/装柜的行级图上传走 _ASYNC_JOBS 异步队列；测试期间可能还在跑，
    必须 drain 完才退出，否则 teardown 删 db 会撞 still-open connection。
    """
    try:
        from blueprints.shipping import _ASYNC_JOBS
    except Exception:
        return
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not any(j.get('state') == 'processing' for j in list(_ASYNC_JOBS.values())):
            return
        time.sleep(0.02)
    raise AssertionError('后台 OCR 任务超时未结束')


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

    def tearDown(self):
        _drain_async_jobs()
        _db.DB_PATH = self._orig
        for ext in ('', '-wal', '-shm'):
            p = self.tmp.name + ext
            if os.path.exists(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass


class MatchLegendTests(_TempDb):

    def test_legend_visible(self):
        """AI 比对图例条应在页面渲染。"""
        res = self.client.get('/inbound-records')
        html = res.get_data(as_text=True)
        self.assertIn('AI 比对图例', html, '页面应有 AI 比对图例条')
        self.assertIn('云端 DeepSeek', html, '图例条应含云端 DeepSeek 说明')


class SourceTagRenderTests(_TempDb):

    def test_source_tag_label_in_html(self):
        """行级图片(source_tag != None)应在 .img-item-record 渲染紫色 '📷 标签'。"""
        from models import InboundOrder, InboundRecord, InboundImage
        oid = InboundOrder.create('2026-08-13', '供应商K')
        rid = InboundRecord.create(oid, '品名', '规格', '10', 'y', '')
        InboundImage.create(
            order_pk=oid, record_pk=rid, file_path='/tmp/fake.png',
            source='manual', source_tag='装车照'
        )
        res = self.client.get('/inbound-records?start_date=2026-08-13&end_date=2026-08-13')
        html = res.get_data(as_text=True)
        self.assertIn('装车照', html, 'source_tag 标签应渲染')
        self.assertIn('source-tag-badge', html, '应有 source-tag-badge class')


class VerifyWarningApiTests(_TempDb):

    def test_verify_warning_endpoint_roundtrip(self):
        """POST /api/v1/inbound-orders/records/<id>/verify-warning 写入 verified_warnings。"""
        from models import InboundOrder, InboundRecord
        oid = InboundOrder.create('2026-08-13', '供应商L')
        rid = InboundRecord.create(oid, '品名', '规格', '10', 'y', '')
        res = self.client.post(
            f'/api/v1/inbound-orders/records/{rid}/verify-warning',
            json={'rule_id': 'b_white_300g', 'verified': True}
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data.get('success'))
        self.assertEqual(data.get('verified_warnings', {}).get('b_white_300g'), True)


if __name__ == '__main__':
    unittest.main()
