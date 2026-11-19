"""入库明细行 —— 商品编号(product.product_code)独立成列。

背景(2026-11-13 接入, 2026-11-14 改版):入库页商品明细区域把商品编号
独立成一列,显示在「商品名称」之前(早期是挂在规格 cell 的 ::after 徽章)。
编号按 (商品名 + 规格) 到 product 表匹配,口径与 Product.find_by_name_and_spec
一致;匹配不到就显示「—」(宁可空着也不给错编号)。
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
                try: os.unlink(p)
                except OSError: pass

    # ── helpers ────────────────────────────────────────────────
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


class InboundProductCodeRenderTests(_TempDb):
    """页面渲染:商品编号独立成列,位于「商品名称」之前。"""

    def _render_one(self, supplier, name, spec, with_product=True):
        from models import InboundOrder, InboundRecord
        if with_product:
            self._product('02100131', name, spec)
        oid = InboundOrder.create('2026-08-13', supplier)
        InboundRecord.create(oid, name, spec, '10', 'y', '')
        return self.client.get(
            '/inbound-records?start_date=2026-08-13&end_date=2026-08-13'
        ).get_data(as_text=True)

    def test_code_column_header_before_product_name(self):
        html = self._render_one('供应商A', '环保杂胶', '1.0白中')
        code_th = html.find('<th class="product-code-col"')
        name_th = html.find('>商品名称</th>')
        self.assertGreater(code_th, -1, '应有「商品编号」表头')
        self.assertGreater(name_th, -1, '应有「商品名称」表头')
        self.assertLess(code_th, name_th, '商品编号列必须在商品名称列之前')

    def test_code_cell_before_name_cell(self):
        html = self._render_one('供应商A2', '环保杂胶', '1.0白中')
        code_td = ('<td class="product-code-cell">'
                   '<span class="product-code-badge">02100131</span></td>')
        self.assertIn(code_td, html, '编号应渲染在独立 cell 里')
        name_td = '<td class="product-name-cell"'
        self.assertGreater(html.find(name_td), -1, '应有商品名称 cell')
        self.assertLess(html.find(code_td), html.find(name_td),
                        '编号 cell 必须在商品名称 cell 之前')

    def test_no_product_code_shows_dash(self):
        html = self._render_one('供应商B', '没建档的商品', '1.0白中',
                                with_product=False)
        self.assertIn('<td class="product-code-cell">—</td>', html,
            'product 表里查不到该商品时编号列显示「—」')

    def test_spec_text_not_polluted_by_code(self):
        """编号在独立 td, .spec-cell 的 textContent 仍只有规格本身
        (全站有多处读 .spec-cell 文本做校验/图片叠加)。"""
        html = self._render_one('供应商C', '环保杂胶', '1.0白中')
        self.assertIn('<td class="spec-cell">1.0白中</td>', html,
            '规格 cell 的文本内容应只有规格本身')
        self.assertNotIn('data-product-code="02100131"', html,
            '不应再把编号挂在规格 cell 的 data 属性上')

    def test_js_row_builder_and_inline_edit_use_new_column_order(self):
        """JS 建行 + 两处行内编辑都按新列序: [2]编号 [3]品名 [4]规格 ... [8]辅助提示。"""
        tpl_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            'templates', 'inbound-records.html'
        )
        with io.open(tpl_path, encoding='utf-8') as f:
            content = f.read()
        self.assertIn('function productCodeCellHtml(', content,
            '应有共用的编号 cell 构造函数')
        self.assertIn('productCodeCellHtml(rec.product_code) +', content,
            'buildInboundRecordRow 应在品名前插入编号列')
        self.assertEqual(
            content.count('tds[2].innerHTML = productCodeInnerHtml(r.product_code);'), 2,
            '两处行内编辑保存后都要按服务端回传值刷新编号列')
        self.assertIn("tds[3].textContent.trim()", content,
            '可编辑列应从 tds[3] 起(旧版是 tds[2])')
        self.assertIn("tds[8].innerHTML", content,
            '辅助单位提示应落在 tds[8]')


class InboundProductCodeApiTests(_TempDb):
    """REST:新增 / 批量新增 / 修改 明细都要回传编号。"""

    def test_add_record_returns_product_code(self):
        from models import InboundOrder
        self._product('02100131', '环保杂胶', '1.0白中')
        oid = InboundOrder.create('2026-08-13', '供应商D')
        res = self.client.post(
            f'/api/v1/inbound-orders/{oid}/records',
            json={'product_name': '环保杂胶', 'specification': '1.0白中',
                  'quantity': '10', 'unit': 'y', 'remark': ''},
        )
        self.assertEqual(res.status_code, 201)
        data = res.get_json()
        self.assertTrue(data['success'])
        self.assertEqual(data['record']['product_code'], '02100131',
            '新增明细应回传商品编号')

    def test_batch_add_returns_product_code(self):
        from models import InboundOrder
        self._product('02100131', '环保杂胶', '1.0白中')
        oid = InboundOrder.create('2026-08-13', '供应商E')
        res = self.client.post(
            f'/api/v1/inbound-orders/{oid}/records/batch',
            json={'records': [{'product_name': '环保杂胶', 'specification': '1.0白中',
                               'quantity': '10', 'unit': 'y', 'remark': ''}]},
        )
        self.assertEqual(res.status_code, 201)
        data = res.get_json()
        self.assertTrue(data['success'])
        self.assertEqual(data['records'][0]['product_code'], '02100131',
            '批量新增应回传商品编号')

    def test_update_record_recomputes_product_code(self):
        from models import InboundOrder, InboundRecord
        self._product('02100131', '环保杂胶', '1.0白中')
        self._product('02100137', '环保杂胶', '1.0白中加面')
        oid = InboundOrder.create('2026-08-13', '供应商F')
        rid = InboundRecord.create(oid, '环保杂胶', '1.0白中', '10', 'y', '')
        res = self.client.put(
            f'/api/v1/inbound-orders/records/{rid}',
            json={'product_name': '环保杂胶', 'specification': '1.0白中加面',
                  'quantity': '10', 'unit': 'y', 'remark': ''},
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data['success'])
        self.assertEqual(data['record']['product_code'], '02100137',
            '改规格后应重算商品编号')


class ProductMatchCodeTests(_TempDb):
    """Product.match_code 的匹配口径。"""

    def test_exact_match(self):
        from models import Product
        self._product('02100131', '环保杂胶', '1.0白中')
        self.assertEqual(Product.match_code('环保杂胶', '1.0白中'), '02100131')

    def test_default_row_used_when_spec_mismatch(self):
        from models import Product
        self._product('02010001', '露华里', '')
        self.assertEqual(Product.match_code('露华里', '0.4黑双'), '02010001',
            '同名 + 空规格默认行应兜底')

    def test_name_whitespace_tolerated(self):
        from models import Product
        self._product('01010101', '无纺布 A白', '200g')
        self.assertEqual(Product.match_code('无纺布A白', '200g'), '01010101',
            '名称只差空格应能命中')

    def test_no_loose_fallback_to_other_spec(self):
        """规格对不上时不给编号 —— 同名任一行兜底会给出错误编号。"""
        from models import Product
        self._product('02120101', '白磅布三文治', '0.4中性')
        self.assertEqual(Product.match_code('白磅布三文治', '0.6软'), '',
            '规格不匹配时不应回退到同名其它规格的编号')


if __name__ == '__main__':
    unittest.main()
