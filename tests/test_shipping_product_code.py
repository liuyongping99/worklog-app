"""出货明细行 —— 商品编号独立成列,未匹配显示红色「未匹配」。

背景(2026-11-14):对齐入库页,/shipping-records 商品明细区域新增商品编号列,
位置在「商品名称」之前。与入库页的唯一差异:匹配不到编号时,入库页显示「—」,
出货页显示红色「未匹配」字样(由页面级钩子 window.productCodeMissingHtml 决定)。

口径:编号由 Product.match_code() 按 (商品名 + 规格) 到 product 表匹配,
查不到返回空串 —— 宁可显示未匹配,也不给错编号。
"""
import io
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
        from app import create_app
        self.client = create_app().test_client()
        from models.tasks_flow import StaffDB
        sid = StaffDB.create('test', '调度')['id']
        with self.client.session_transaction() as s:
            s['operator_id'] = sid

    def tearDown(self):
        _db.DB_PATH = self._orig
        for ext in ('', '-wal', '-shm'):
            p = self.tmp.name + ext
            if os.path.exists(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass

    def _category_id(self):
        if getattr(self, '_cat_id', None) is None:
            self._cat_id = self._insert_category()
        return self._cat_id

    def _insert_category(self):
        from models import get_db
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("INSERT INTO product_categories (category_code, category_name, parent_id, level) "
                       "VALUES ('T' || ?, '测试大类', NULL, 1)",
                       (str(self._cat_seq()),))
        conn.commit()
        cid = cursor.lastrowid
        conn.close()
        return cid

    def _cat_seq(self):
        self._cat_counter = getattr(self, '_cat_counter', 0) + 1
        return self._cat_counter

    def _product(self, code, name, spec, category_id=None):
        from models import Product
        if category_id is None:
            category_id = self._category_id()
        Product.create(code, name, category_id, specification=spec, base_unit='y')
        return code

    def _render_one(self, customer, name, spec, with_product=True):
        from models import ShippingRecord
        if with_product:
            self._product('02100131', name, spec)
        ShippingRecord.create('2026-08-13', customer, name, spec, '10', 'y', '')
        return self.client.get(
            '/shipping-records?start_date=2026-08-13&end_date=2026-08-13'
        ).get_data(as_text=True)

    def _read_template(self, rel):
        path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), rel)
        with io.open(path, encoding='utf-8') as f:
            return f.read()


class ShippingProductCodeRenderTests(_TempDb):
    """页面渲染:编号列在品名前;未匹配显示红色「未匹配」。"""

    def test_code_column_header_before_product_name(self):
        html = self._render_one('客户A', '环保杂胶', '1.0白中')
        code_th = html.find('<th class="product-code-col"')
        name_th = html.find('>商品名称</th>')
        self.assertGreater(code_th, -1, '应有「商品编号」表头')
        self.assertGreater(name_th, -1, '应有「商品名称」表头')
        self.assertLess(code_th, name_th, '商品编号列必须在商品名称列之前')

    def test_spec_th_has_class_for_ai_match_column(self):
        html = self._render_one('客户A2', '环保杂胶', '1.0白中')
        self.assertIn('<th class="spec-cell"', html,
            '规格 th 需带 spec-cell,_ensureMatchColumn 才能把 AI 比对列插在规格之后')

    def test_matched_code_rendered_before_name_cell(self):
        html = self._render_one('客户B', '环保杂胶', '1.0白中')
        code_td = ('<td class="product-code-cell">'
                   '<span class="product-code-badge">02100131</span></td>')
        name_td = '<td class="product-name-cell"'
        self.assertIn(code_td, html, '编号应渲染在独立 cell 的徽章里')
        self.assertGreater(html.find(name_td), -1, '应有商品名称 cell')
        self.assertLess(html.find(code_td), html.find(name_td),
                        '编号 cell 必须在商品名称 cell 之前')

    def test_unmatched_shows_red_missing_marker(self):
        html = self._render_one('客户C', '没建档的商品', '1.0白中',
                                with_product=False)
        self.assertIn(
            '<span class="product-code-missing" title="商品档案中未找到匹配的编号">未匹配</span>',
            html, 'product 表里查不到该商品时应显示红色「未匹配」字样')
        # 页面 HTML 内含 JS 源码(productCodeInnerHtml 里有同名字符串),
        # 不能整页断言不存在 —— 只看真正渲染到行上的编号 cell。
        import re
        cells = re.findall(r'<td class="product-code-cell">(.*?)</td>', html)
        self.assertTrue(cells, '至少应渲染一个编号 cell')
        self.assertFalse(any('product-code-badge' in c for c in cells),
            '未匹配时编号 cell 内不能出现编号徽章')
        tpl = self._read_template('templates/shipping-records.html')
        css = tpl.split('.shipping-page .record-table .product-code-missing {', 1)[1].split('}', 1)[0]
        self.assertIn('color: #d63031', css, '「未匹配」必须是红色')

    def test_spec_text_not_polluted_by_code(self):
        html = self._render_one('客户D', '环保杂胶', '1.0白中')
        self.assertNotIn('data-product-code=', html,
            '编号不能挂在规格 cell 的 data 属性上')
        self.assertIn('1.0白中', html)

    def test_template_js_helper_and_page_hook(self):
        tpl = self._read_template('templates/shipping-records.html')
        self.assertIn('function productCodeInnerHtml(', tpl,
            '应有共用的编号 cell 内容构造函数')
        self.assertIn('window.productCodeMissingHtml = function()', tpl,
            '应暴露页面级钩子供共享弹窗取未匹配占位')
        self.assertIn("codeTd.innerHTML = productCodeInnerHtml(r.product_code);", tpl,
            '行内编辑保存后要按服务端回传值刷新编号列')
        modal = self._read_template('templates/_smart_add_modal.html')
        self.assertIn('window.productCodeMissingHtml', modal,
            '共享弹窗必须读钩子,否则出货页新增行会显示入库页的「—」')


class ShippingProductCodeApiTests(_TempDb):
    """REST:批量新增 / 修改 明细都要回传编号,供前端新建行/行内编辑刷新。"""

    def test_batch_add_returns_product_code(self):
        from models import ShippingOrder
        self._product('02100131', '环保杂胶', '1.0白中')
        oid = ShippingOrder.create('2026-08-13', '客户E')
        res = self.client.post(
            '/api/v1/shipping-orders/%s/records/batch' % oid,
            json={'items': [{'product_name': '环保杂胶', 'specification': '1.0白中',
                             'quantity': '10', 'unit': 'y', 'remark': ''}]},
        )
        self.assertEqual(res.status_code, 201)
        data = res.get_json()
        self.assertTrue(data['success'])
        self.assertEqual(data['records'][0]['product_code'], '02100131',
            '批量新增应回传商品编号')

    def test_batch_add_unmatched_returns_empty_code(self):
        from models import ShippingOrder
        oid = ShippingOrder.create('2026-08-13', '客户F')
        res = self.client.post(
            '/api/v1/shipping-orders/%s/records/batch' % oid,
            json={'items': [{'product_name': '没建档的商品', 'specification': '1.0白中',
                             'quantity': '10', 'unit': 'y', 'remark': ''}]},
        )
        self.assertEqual(res.status_code, 201)
        data = res.get_json()
        self.assertEqual(data['records'][0]['product_code'], '',
            '未匹配时回传空串,前端据此渲染红色「未匹配」')

    def test_update_record_recomputes_product_code(self):
        from models import ShippingOrder, ShippingRecord
        self._product('02100131', '环保杂胶', '1.0白中')
        self._product('02100137', '环保杂胶', '1.0白中加面')
        oid = ShippingOrder.create('2026-08-13', '客户G')
        rid = ShippingRecord.create('2026-08-13', '客户G', '环保杂胶',
                                    '1.0白中', '10', 'y', '', order_pk=oid)
        res = self.client.put(
            '/api/v1/shipping-orders/records/%s' % rid,
            json={'product_name': '环保杂胶', 'specification': '1.0白中加面',
                  'quantity': '10', 'unit': 'y', 'remark': ''},
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data['success'])
        self.assertEqual(data['record']['product_code'], '02100137',
            '改规格后应重算商品编号')


if __name__ == '__main__':
    unittest.main(verbosity=2)
