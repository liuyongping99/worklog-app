"""装柜 — 列数持久化从 cookie 切到 DB,与出货/入库对齐。

Task 6 of 2026-08-13 入库对齐出货:
- 装柜渲染应读 `group.img_cols` (DB 列),不再读 `loadingImgCols` cookie
- PATCH /api/v1/loading-orders/<id> 接受 `img_cols` 入参,刷新后渲染相应 cols-N
- 前端 setImgCols 由共享 _record_image_script.html 提供 (走 DB),删 cookie 版

运行:PYTHONUTF8=1 python -m pytest tests/test_loading_img_cols.py -v
"""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models._db as _db


def _drain_async_jobs(timeout=15):
    """出货/入库/装柜的行级图上传走 _ASYNC_JOBS 异步队列;测试期间可能还在跑,
    必须 drain 完才退出,否则 teardown 删 db 会撞 still-open connection。
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


class LoadingImgColsTests(_TempDb):

    def test_render_uses_db_img_cols(self):
        """装柜渲染不应再有 cookie loadingImgCols 痕迹,DB 值 2 应注入到 date-group。
        精确断言:`data-img-cols="2"` 在 `.date-group` 上(Jinja 渲染时由 `group.img_cols` 注入);
        `cols-2` 是空断言 — CSS/JS 内联常量始终在,与 DB 值无关,故不再用。
        """
        from models import LoadingOrder
        oid = LoadingOrder.create('2026-08-13', '客户X')
        LoadingOrder.set_img_cols(oid, 2)
        res = self.client.get('/loading-orders?start_date=2026-08-13&end_date=2026-08-13')
        html = res.get_data(as_text=True)
        self.assertIn('data-img-cols="2"', html, 'DB 值 2 应注入到 date-group 的 data-img-cols 属性')
        self.assertNotIn('loadingImgCols', html,
            '渲染不应再有 cookie loadingImgCols 痕迹')

    def test_patch_img_cols_roundtrip(self):
        """PATCH /api/v1/loading-orders/<id> img_cols 后端分支 roundtrip:
        1) PATCH 200
        2) 刷新页面 date-group 的 data-img-cols 跟随 DB
        3) DB 列真写入 4
        装柜默认 3,用 4 让断言能区分 default vs DB。
        """
        from models import LoadingOrder
        oid = LoadingOrder.create('2026-08-13', '客户Y')
        res = self.client.patch(
            f'/api/v1/loading-orders/{oid}',
            json={'img_cols': 4}
        )
        self.assertEqual(res.status_code, 200)
        # 刷新页面看 DB 值生效 — 用 date-group 的 data-img-cols 精确断言(非 cols-4 空断言)
        res2 = self.client.get('/loading-orders?start_date=2026-08-13&end_date=2026-08-13')
        html2 = res2.get_data(as_text=True)
        self.assertIn('data-img-cols="4"', html2, 'PATCH 后刷新页面,DB 值 4 应注入到 date-group')
        # 验证 DB 真写了 4(后端链路)
        import sqlite3
        with sqlite3.connect(_db.DB_PATH) as conn:
            row = conn.execute('SELECT img_cols FROM loading_orders WHERE id=?', (oid,)).fetchone()
            self.assertEqual(row[0], 4, 'DB 真写入 4')


if __name__ == '__main__':
    unittest.main()
