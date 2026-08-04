"""match-col 虚拟列 — 服务端首次渲染不应渲染 match-col th/td。

2026-08-04 改造:服务端不再渲染 AI 比对列(match-col),改由 JS 在首次有
匹配结果时通过 `_ensureMatchColumn` 动态插入。Smart-add 弹框的占位 td
单独保留(行尚未入库,列结构先存在)。

覆盖范围(4 个测试):
- shipping 页面首次 GET 不含 match-col / no-match
- inbound 页面首次 GET 不含 match-col / no-match
- loading 页面首次 GET 不含 match-col / no-match
- _ensureMatchColumn / setRowMatchBadge 行为 (jsdom runner,走 subprocess 跑 node)

注:本测试不主动跑行级图上传,但仍保留 `_drain_async_jobs()` 以防
其他后续测试共享 _TempDb 时的污染。
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models._db as _db


def _drain_async_jobs(timeout=15):
    """防止行级图上传后台线程活过 tearDown 污染生产库。"""
    from blueprints.shipping import _ASYNC_JOBS
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not any(j.get('state') == 'processing' for j in list(_ASYNC_JOBS.values())):
            return
        time.sleep(0.02)
    raise AssertionError('后台 OCR 任务超时未结束,可能污染生产库')


def _strip_scripts(html: str) -> str:
    """去掉 <script>...</script> 块。CSS 风格 / 类名引用以 .match-col 字样
    出现在 JS 字符串里是合法的(占位 td),所以测试只看非 script 区域。"""
    return re.sub(r'<script\b[^>]*>.*?</script>', '', html, flags=re.S)


class _TempDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        self._orig = _db.DB_PATH
        _db.DB_PATH = self.tmp.name
        from models import init_db
        init_db()
        # 测试 DB 缺几列(loading_orders.doc_number 等),手动补齐避免蓝图查询炸错。
        # 2026-08-04:loading.py get_grouped 引用 o.doc_number,
        # init_db() 的 CREATE TABLE 未定义该列;生产库是通过 SQL 手工补的。
        conn = _db.get_db()
        cur = conn.cursor()
        for sql in (
            "ALTER TABLE loading_orders ADD COLUMN doc_number TEXT NOT NULL DEFAULT ''",
        ):
            try:
                cur.execute(sql)
            except Exception:
                pass
        conn.commit()
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
                try: os.unlink(p)
                except OSError: pass


class ServerSideNoMatchColTests(_TempDb):
    """三套订单表服务端首次渲染,均不应出现 match-col th/td。"""

    def _make_order_with_record(self, page):
        from models import (
            ShippingOrder, ShippingRecord,
            InboundOrder, InboundRecord,
            LoadingOrder, LoadingOrderRecord,
        )
        if page == 'shipping':
            oid = ShippingOrder.create('2026-08-04', '客户A')
            ShippingRecord.create('2026-08-04', '客户A', '环保杂胶', '0.8黑中加面', '50', 'y', '', oid)
        elif page == 'inbound':
            oid = InboundOrder.create('2026-08-04', '供应商X')
            InboundRecord.create(oid, '无纺布', 'A1.5m 200g', '10', 'y', '')
        elif page == 'loading':
            oid = LoadingOrder.create('2026-08-04', '央士')
            LoadingOrderRecord.create('2026-08-04', '央士', 'PE板', '1m', '5', 'y', '', oid)

    def test_shipping_first_render_has_no_match_col(self):
        self._make_order_with_record('shipping')
        res = self.client.get('/shipping-records?start_date=2026-08-04&end_date=2026-08-04')
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        # 2026-08-04:match-col 服务端不再渲染(th/td)。CSS `.match-col { ... }` 和
        # _smart_add_modal.html 的占位 td 保留(都在 <script> / <style> 里),所以剔
        # script/style 后再看结构性断言。
        body = _strip_scripts(html)
        self.assertNotIn('<th class="match-col"', body,
            '出货页首次服务端渲染不应渲染 <th class="match-col">')
        self.assertNotIn('<td class="match-col"', body,
            '出货页首次服务端渲染不应渲染 <td class="match-col">')
        self.assertNotIn('no-match', body,
            '出货页首次服务端渲染不应使用 no-match class')

    def test_inbound_first_render_has_no_match_col(self):
        self._make_order_with_record('inbound')
        res = self.client.get('/inbound-records?start_date=2026-08-04&end_date=2026-08-04')
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        body = _strip_scripts(html)
        self.assertNotIn('<th class="match-col"', body)
        self.assertNotIn('<td class="match-col"', body)
        self.assertNotIn('no-match', body)

    def test_loading_first_render_has_no_match_col(self):
        self._make_order_with_record('loading')
        res = self.client.get('/loading-orders?start_date=2026-08-04&end_date=2026-08-04')
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        body = _strip_scripts(html)
        self.assertNotIn('<th class="match-col"', body)
        self.assertNotIn('<td class="match-col"', body)
        self.assertNotIn('no-match', body)


class JsdomMatchColumnTests(unittest.TestCase):
    """_ensureMatchColumn / setRowMatchBadge 行为测试(jsdom 抽真实函数跑)。

    2026-08-04 改造:match-col 由 JS 动态插入;这里调 subprocess 跑
    node scripts/record_image_match_column_check.js 验证三 case:
    Case 1 空 table 插列、Case 2 已有列不重复插、Case 3 5 行表兄弟行对齐。
    """

    @unittest.skipUnless(
        Path('tests', 'record_image_match_column_check.js').exists()
        and shutil.which('node') is not None,
        '需要 node + tests/record_image_match_column_check.js 才能跑',
    )
    def test_ensure_match_column_jsdom(self):
        proc = subprocess.run(
            ['node', 'tests/record_image_match_column_check.js'],
            capture_output=True,
        )
        if proc.returncode != 0:
            self.fail(
                f"record_image_match_column_check.js 退出码 {proc.returncode}\n"
                f"STDOUT: {proc.stdout.decode('utf-8', errors='replace')}\n"
                f"STDERR: {proc.stderr.decode('utf-8', errors='replace')}"
            )
        out = proc.stdout.decode('utf-8', errors='replace')
        self.assertIn('✅ record_image_match_column_check 通过', out,
                      f'jsdom 检查脚本未通过:\n{out}')


if __name__ == '__main__':
    unittest.main()
