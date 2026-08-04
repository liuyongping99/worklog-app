"""出货页商品行 🖼️ 按钮红框提示 —— 关联图片存在时高亮。

覆盖:
- 无图:渲染按钮不含 has-image class
- 有图:渲染按钮含 has-image class
- 上传后再渲染:按钮含 has-image class
- CSS 定义存在(.record-image-btn.has-image)
"""
import io
import os
import re
import sys
import unittest
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import fake_png_stream as _fake_png  # noqa: E402,F401

import models._db as _db


class ImageBtnHighlightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        self._orig = _db.DB_PATH
        _db.DB_PATH = self.tmp.name
        from models import init_db
        init_db()
        from app import create_app
        self.app = create_app()
        self.client = self.app.test_client()
        # 登录(防 _require_login 拦截)
        from models.tasks_flow import StaffDB
        self.staff_id = StaffDB.create('测试员', '调度')['id']
        with self.client.session_transaction() as s:
            s['operator_id'] = self.staff_id
        # 清可能存在的 PaddleOCR 缓存
        try:
            from blueprints.ocr_engine import _engine_cache
            _engine_cache.pop('paddleocr', None)
        except Exception:
            pass

    def tearDown(self):
        _db.DB_PATH = self._orig
        # WAL 残留 cleanup(参考 tests/conftest.py 同模式,避免 PermissionError)
        for ext in ('', '-wal', '-shm'):
            p = self.tmp.name + ext
            if os.path.exists(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass

    def _btn_class_for_record(self, html, record_id):
        """从渲染 HTML 中找出该 record-id 的 .record-image-btn 按钮的 class。"""
        pat = re.compile(
            r'<button[^>]*class="([^"]*record-image-btn[^"]*)"[^>]*data-record-id="' + str(record_id) + '"',
            re.S,
        )
        m = pat.search(html)
        if not m:
            return None
        return m.group(1)

    def test_btn_without_image_lacks_highlight(self):
        """无图的明细行:按钮 class 不含 has-image。"""
        from models import ShippingOrder, ShippingRecord
        oid = ShippingOrder.create('2026-07-25', 'C')
        rid = ShippingRecord.create('2026-07-25', 'C', 'P', 'S', '1', 'y', '', oid)

        html = self.client.get(
            '/shipping-records?start_date=2026-07-25&end_date=2026-07-25'
        ).get_data(as_text=True)
        cls = self._btn_class_for_record(html, rid)
        self.assertIsNotNone(cls, '按钮未渲染')
        self.assertNotIn('has-image', cls)
        self.assertNotIn('（已上传）', html)

    def test_btn_with_image_has_highlight(self):
        """有图的明细行:按钮 class 含 has-image,title 文本带"已上传"。"""
        from models import ShippingOrder, ShippingRecord, ShippingImage
        oid = ShippingOrder.create('2026-07-25', 'C')
        rid = ShippingRecord.create('2026-07-25', 'C', 'P', 'S', '1', 'y', '', oid)
        ShippingImage.create(order_pk=oid, file_path='upload/2026-07/x.png', record_pk=rid)

        html = self.client.get(
            '/shipping-records?start_date=2026-07-25&end_date=2026-07-25'
        ).get_data(as_text=True)
        cls = self._btn_class_for_record(html, rid)
        self.assertIsNotNone(cls, '按钮未渲染')
        self.assertIn('has-image', cls)
        self.assertIn('（已上传）', html)

    @mock.patch('blueprints.shipping.get_ocr_engine')
    def test_btn_highlight_after_record_upload(self, mock_factory):
        """行级图片上传后,该 record 按钮加上 has-image class(下次渲染自然会有,这里直接端到端核验:
        上传成功后 DB 已有该图的 record_pk 关联,渲染时即应含 has-image)。"""
        from models import ShippingOrder, ShippingRecord
        oid = ShippingOrder.create('2026-07-25', 'C')
        rid = ShippingRecord.create('2026-07-25', 'C', 'P', 'S', '1', 'y', '', oid)

        # 上传前:无 has-image
        html0 = self.client.get(
            '/shipping-records?start_date=2026-07-25&end_date=2026-07-25'
        ).get_data(as_text=True)
        self.assertNotIn('has-image', self._btn_class_for_record(html0, rid) or '')

        # mock OCR 让上传顺利通过
        fake_paddle = mock.MagicMock()
        fake_paddle.extract_text.return_value = ''
        mock_factory.side_effect = lambda name: fake_paddle if name == 'paddleocr' else mock.DEFAULT

        data = {'image': (_fake_png(), 'x.png')}
        resp = self.client.post(
            f'/api/v1/shipping-orders/records/{rid}/images',
            data=data, content_type='multipart/form-data',
        )
        self.assertEqual(resp.status_code, 201, resp.get_json())

        # 清理磁盘上的测试图
        from models import ShippingImage
        saved = ShippingImage.get_by_record(rid)
        for s in saved:
            abspath = os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                s['file_path'].replace('/', os.sep),
            )
            try:
                os.unlink(abspath)
            except OSError:
                pass

        # 上传后:必须含 has-image class
        html = self.client.get(
            '/shipping-records?start_date=2026-07-25&end_date=2026-07-25'
        ).get_data(as_text=True)
        cls = self._btn_class_for_record(html, rid)
        self.assertIsNotNone(cls)
        self.assertIn('has-image', cls)

    def test_css_rule_defined(self):
        """页面 <style> 块必须用 border 实现红框(不能用 box-shadow,否则红区会撑大撑厚)。

        2026-07-27 用户要求:红框=普通边框,不要大块红色背景。
        """
        html = self.client.get(
            '/shipping-records?start_date=2026-07-27&end_date=2026-07-27'
        ).get_data(as_text=True)
        m = re.search(r'\.shipping-page\s+\.record-table\s+\.record-image-btn\.has-image\s*\{[^}]*\}', html)
        self.assertIsNotNone(m, '样式 .record-image-btn.has-image 未定义')
        rule = m.group(0).lower()
        self.assertIn('#dc2626', rule, '规则缺失红色 #dc2626')
        self.assertIn('border', rule, '必须用 border 实现红框,不能用 box-shadow/background')
        self.assertNotIn('box-shadow', rule, '禁止 box-shadow —— 会产生大块红区域,违背"边框"语义')
        self.assertNotIn('background:', rule, '禁止改 background —— 覆盖按钮的蓝色背景')


if __name__ == '__main__':
    unittest.main()
