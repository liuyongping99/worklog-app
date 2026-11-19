"""阶段 2 · 库存查询(只读)—— 页面 + API + 导出。

覆盖:
  1. /stock 页面正常渲染(仓库静态标签 / 统计格 / 未启用徽章)
  2. /api/v1/warehouses 仓库列表
  3. /api/v1/stock 主查询 + 筛选(分类含子树 / 搜索 / 仅看已启用)
  4. ⭐ **未启用 ≠ 真 0** —— 未启用的商品对外显示 0,但内部累计照记
  5. /api/v1/stock/<pid> 单 SKU 明细
  6. /api/v1/stock/ledger 流水筛选(业务类型 / 日期)
  7. ⭐ **导出的 CSV 与页面显示逐行一致**
  8. ⭐ **CSV 公式注入防护**(=+-@ 开头强制加前缀 ')
  9. 参数校验:仓库不存在 / 非法 only_tracking / 非法日期 → 400 + 中文提示
 10. 未登录访问 API → 401

跑法:python -m pytest tests/test_stock_readonly.py -q --tb=short -p no:warnings
"""
import csv
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models._db as _db


class StockReadOnlyTests(unittest.TestCase):
    """整个类共用一个临时库 —— 建一次 app,种子一次,跑完删掉。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        cls.tmp.close()
        cls._orig = _db.DB_PATH
        _db.DB_PATH = cls.tmp.name

        from app import create_app
        cls.app = create_app()
        cls.app.config['TESTING'] = True

        from models import ProductStock, StockLedger, Warehouse
        from models.products import Product, ProductCategory
        from models.tasks_flow import StaffDB

        # 登录闸门:session 里的 operator_id 必须对应一条 is_active=1 的 staff,
        # 否则 /stock 会被 302 到 /login(临时库里 staff 表是空的)。
        cls.staff_id = int(StaffDB.create('测试员', '文员', is_active=1)['id'])

        wh = Warehouse.get_default()
        cls.wid = int(wh['id'])
        cls.wh_name = wh['name']

        # 分类:一级 → 二级 → 三级(测「选父含子树」)
        cls.cat_l1 = ProductCategory.create('T1', '测试一级', None, 1, 1)
        cls.cat_l2 = ProductCategory.create('T2', '测试二级', cls.cat_l1, 2, 1)
        cls.cat_l3 = ProductCategory.create('T3', '测试三级', cls.cat_l2, 3, 1)
        cls.cat_other = ProductCategory.create('T9', '别的分类', None, 1, 2)

        # 商品
        cls.p_on = Product.create('SKU001', '已启用布', cls.cat_l3,
                                  specification='1.0米', base_unit='码')
        cls.p_off = Product.create('SKU002', '未启用布', cls.cat_l3,
                                   specification='2.0米', base_unit='码')
        cls.p_plain = Product.create('SKU003', '没建过行的布', cls.cat_other,
                                     specification='3.0米', base_unit='码')
        # 品名带公式前缀的 —— 专测 CSV 注入防护
        cls.p_formula = Product.create('SKU004', '=1+1', cls.cat_other,
                                       specification='4.0米', base_unit='码')

        # 已启用 + 有库存
        ProductStock.set_quantity(cls.wid, cls.p_on, 5)
        ProductStock.set_tracking(cls.wid, cls.p_on, 1)
        # 未启用,但内部累计已经记了 99(流水照记,只是对外显示 0)
        ProductStock.set_quantity(cls.wid, cls.p_off, 99)
        ProductStock.set_tracking(cls.wid, cls.p_off, 0)
        StockLedger.create(cls.wid, 'inbound', 'inbound:1', cls.p_on, 5, '码',
                           occurred_at='2026-11-10')
        StockLedger.create(cls.wid, 'adjust', 'adjust:1', cls.p_off, 99, '码',
                           occurred_at='2026-11-11')

    @classmethod
    def tearDownClass(cls):
        _db.DB_PATH = cls._orig
        for ext in ('', '-wal', '-shm'):
            p = cls.tmp.name + ext
            if os.path.exists(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass

    def setUp(self):
        self.client = self.app.test_client()
        with self.client.session_transaction() as s:
            s['operator_id'] = self.staff_id

    # ── 工具 ─────────────────────────────────────────────────────
    def _json(self, url):
        r = self.client.get(url)
        self.assertEqual(r.status_code, 200, f'{url} → {r.status_code} {r.get_data(as_text=True)[:300]}')
        return r.get_json()

    @staticmethod
    def _page_codes(html):
        """从页面 HTML 里抽出表格里出现过的商品编号(按出现顺序)。"""
        import re
        return re.findall(r'<tr class="stk-row" data-pid="\d+"[^>]*>\s*<td>([^<]*)</td>', html)

    # ── 1. 页面 ───────────────────────────────────────────────────
    def test_page_renders(self):
        r = self.client.get('/stock')
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn('库存', html)
        # 单仓 → 静态标签(不做只有一项的下拉框)
        self.assertIn(f'🏭 {self.wh_name}', html)
        self.assertIn('stk-stat', html)
        # 分类树
        self.assertIn('测试一级', html)

    def test_page_shows_untracked_badge(self):
        html = self.client.get('/stock').get_data(as_text=True)
        self.assertIn('未启用', html)
        self.assertIn('已启用', html)

    # ── 2. 仓库列表 ──────────────────────────────────────────────
    def test_warehouses_api(self):
        d = self._json('/api/v1/warehouses')
        self.assertTrue(d['success'])
        self.assertGreaterEqual(d['total'], 1)
        self.assertEqual(d['warehouses'][0]['name'], self.wh_name)

    # ── 3. 主查询 + 筛选 ──────────────────────────────────────────
    def test_stock_list_returns_all_products(self):
        """⭐ 主查询以 product 为驱动表 —— 没建过 product_stock 行的商品也要出现。

        (product_stock 现在是 0 行;若从它出发 INNER JOIN,库存页会一片空白)
        """
        d = self._json('/api/v1/stock?page_size=500')
        codes = {r['product_code'] for r in d['rows']}
        self.assertIn('SKU003', codes)      # 从没建过行的
        self.assertIn('SKU001', codes)      # 已启用
        self.assertIn('SKU002', codes)      # 未启用

    def test_stock_list_defaults_to_default_warehouse(self):
        d = self._json('/api/v1/stock')
        self.assertEqual(int(d['warehouse']['id']), self.wid)

    def test_only_tracking_filter(self):
        d = self._json('/api/v1/stock?only_tracking=1&page_size=500')
        codes = {r['product_code'] for r in d['rows']}
        self.assertIn('SKU001', codes)
        self.assertNotIn('SKU002', codes)
        self.assertNotIn('SKU003', codes)

        d0 = self._json('/api/v1/stock?only_tracking=0&page_size=500')
        codes0 = {r['product_code'] for r in d0['rows']}
        self.assertNotIn('SKU001', codes0)
        self.assertIn('SKU002', codes0)
        self.assertIn('SKU003', codes0)

    def test_search_by_name(self):
        d = self._json('/api/v1/stock?q=SKU001&page_size=500')
        self.assertEqual(d['total'], 1)
        self.assertEqual(d['rows'][0]['product_code'], 'SKU001')

    def test_category_filter_includes_descendants(self):
        """选父分类 → 含所有后代(主流进销存的通用做法)。"""
        d = self._json(f'/api/v1/stock?category_id={self.cat_l1}&page_size=500')
        codes = {r['product_code'] for r in d['rows']}
        self.assertIn('SKU001', codes)      # 在三级
        self.assertIn('SKU002', codes)
        self.assertNotIn('SKU003', codes)   # 在别的分类

    def test_stats(self):
        d = self._json('/api/v1/stock?page_size=500')
        st = d['stats']
        self.assertEqual(st['tracking'], 1)
        self.assertEqual(st['with_stock'], 1)     # 只有已启用且数量≠0 的算
        self.assertGreaterEqual(st['untracked'], 2)

    def test_stats_follow_filter(self):
        """统计格**跟随筛选** —— 筛了别的分类,统计不该还报全仓的数。

        (主流进销存的通用做法:用户筛了「灰板类」,统计格就该说这批有多少)
        """
        d_all = self._json('/api/v1/stock?page_size=500')
        d_cat = self._json(f'/api/v1/stock?category_id={self.cat_other}&page_size=500')
        self.assertLess(d_cat['stats']['total'], d_all['stats']['total'])
        self.assertEqual(d_cat['stats']['total'], d_cat['total'])
        # 另一个分类 → 统计又变回另一批
        d_l1 = self._json(f'/api/v1/stock?category_id={self.cat_l1}&page_size=500')
        self.assertEqual(d_l1['stats']['total'], 2)   # SKU001 + SKU002

    # ── 4. 未启用 ≠ 真 0 ─────────────────────────────────────────
    def test_untracked_is_zero_but_keeps_internal_total(self):
        """⭐ 完成判据:未启用的商品显示 0 + 灰色「未启用」,但内部累计照记。"""
        d = self._json('/api/v1/stock?q=SKU002&page_size=500')
        row = d['rows'][0]
        self.assertEqual(row['is_tracking'], 0)
        self.assertEqual(row['display_quantity'], 0)    # 对外显示 0
        self.assertEqual(row['quantity'], 99)            # 内部累计保留 99
        self.assertEqual(row['internal_total'], 99.0)
        self.assertEqual(row['is_tracking_label'], '未启用')

    def test_enabled_shows_real_quantity(self):
        d = self._json('/api/v1/stock?q=SKU001&page_size=500')
        row = d['rows'][0]
        self.assertEqual(row['is_tracking'], 1)
        self.assertEqual(row['display_quantity'], 5)
        self.assertEqual(row['is_tracking_label'], '已启用')

    # ── 5. 单 SKU ────────────────────────────────────────────────
    def test_detail_api(self):
        d = self._json(f'/api/v1/stock/{self.p_on}')
        self.assertTrue(d['success'])
        self.assertEqual(d['stock']['display_quantity'], 5)
        self.assertEqual(d['stock']['is_tracking'], 1)
        self.assertEqual(len(d['ledger']), 1)

    def test_detail_api_404(self):
        r = self.client.get('/api/v1/stock/999999')
        self.assertEqual(r.status_code, 404)

    # ── 6. 流水筛选 ──────────────────────────────────────────────
    def test_ledger_filter_by_product(self):
        d = self._json(f'/api/v1/stock/ledger?product_id={self.p_on}')
        self.assertEqual(d['total'], 1)
        self.assertEqual(d['rows'][0]['biz_type'], 'inbound')

    def test_ledger_filter_by_biz_type(self):
        d = self._json('/api/v1/stock/ledger?biz_type=adjust')
        self.assertEqual(d['total'], 1)
        self.assertEqual(d['rows'][0]['biz_type'], 'adjust')

    def test_ledger_filter_by_date_range(self):
        d = self._json('/api/v1/stock/ledger?date_from=2026-11-11&date_to=2026-11-11')
        self.assertEqual(d['total'], 1)
        self.assertEqual(d['rows'][0]['biz_type'], 'adjust')

        d2 = self._json('/api/v1/stock/ledger?date_from=2026-11-12')
        self.assertEqual(d2['total'], 0)

    def test_ledger_bad_biz_type(self):
        r = self.client.get('/api/v1/stock/ledger?biz_type=xxx')
        self.assertEqual(r.status_code, 400)

    # ── 7. CSV 与页面逐行一致 ────────────────────────────────────
    def test_export_matches_page_rows(self):
        """⭐ 完成判据:导出的 CSV 与页面显示逐行一致(同一套筛选 + 同一套查询)。

        ⚠️ 唯一的差别是 **CSV 注入防护前缀**:品名以 `=+-@` 开头时,CSV 里会多一个
           前缀 `'`(防止 Excel 当成公式执行)。那是**传输层保护**,不是数据变化 ——
           所以比较时给 API 侧套同一个防护函数,两边就应当逐字段相等。
        """
        from blueprints.stock import _sanitize_csv_cell
        api = self._json('/api/v1/stock?page_size=500')
        r = self.client.get('/api/v1/stock/export.csv')
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        if body.startswith('﻿'):
            body = body[1:]
        rows = list(csv.DictReader(io.StringIO(body)))
        self.assertEqual(len(rows), len(api['rows']))
        compare_fields = ('product_code', 'product_name', 'specification',
                          'base_unit', 'display_quantity', 'is_tracking_label',
                          'internal_total')
        for csv_row, api_row in zip(rows, api['rows']):
            for k in compare_fields:
                want = api_row[k]
                if isinstance(want, str):
                    want = _sanitize_csv_cell(want)
                self.assertEqual(csv_row[k], str(want),
                                 f'字段 {k} 不一致:CSV={csv_row[k]!r} API={want!r}')

    def test_export_respects_filter(self):
        """导出必须跟页面用同一套筛选 —— 这里只看已启用。"""
        r = self.client.get('/api/v1/stock/export.csv?only_tracking=1')
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        if body.startswith('﻿'):
            body = body[1:]
        rows = list(csv.DictReader(io.StringIO(body)))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['product_code'], 'SKU001')

    def test_export_page_html_same_rows(self):
        """页面表格里的编号顺序 == CSV 里的编号顺序。"""
        html = self.client.get('/stock?page_size=500').get_data(as_text=True)
        page_codes = self._page_codes(html)
        r = self.client.get('/api/v1/stock/export.csv?page_size=500')
        body = r.get_data(as_text=True)
        if body.startswith('﻿'):
            body = body[1:]
        csv_codes = [row['product_code'] for row in csv.DictReader(io.StringIO(body))]
        self.assertTrue(page_codes)
        self.assertEqual(page_codes, csv_codes)

    # ── 8. CSV 公式注入防护 ──────────────────────────────────────
    def test_export_sanitizes_formula(self):
        """品名 = `=1+1` → CSV 里必须变成 `'=1+1`(Excel 不会当成公式执行)。"""
        r = self.client.get('/api/v1/stock/export.csv?page_size=500')
        body = r.get_data(as_text=True)
        self.assertIn("'=1+1", body)
        self.assertNotIn(',=1+1,', body)

    # ── 9. 参数校验 ──────────────────────────────────────────────
    def test_unknown_warehouse_400(self):
        r = self.client.get('/api/v1/stock?warehouse_id=999999')
        self.assertEqual(r.status_code, 400)
        self.assertIn('仓库不存在', r.get_json()['error'])

    def test_bad_only_tracking_400(self):
        r = self.client.get('/api/v1/stock?only_tracking=xx')
        self.assertEqual(r.status_code, 400)

    def test_bad_date_400(self):
        r = self.client.get('/api/v1/stock/ledger?date_from=2026/11/11')
        self.assertEqual(r.status_code, 400)
        self.assertIn('YYYY-MM-DD', r.get_json()['error'])

    def test_bad_category_400(self):
        r = self.client.get('/api/v1/stock?category_id=999999')
        self.assertEqual(r.status_code, 400)

    def test_page_bad_params_returns_400(self):
        r = self.client.get('/stock?warehouse_id=999999')
        self.assertEqual(r.status_code, 400)

    # ── 10. 登录闸门 ─────────────────────────────────────────────
    def test_api_requires_login(self):
        c = self.app.test_client()
        r = c.get('/api/v1/stock')
        self.assertEqual(r.status_code, 401)
        self.assertFalse(r.get_json()['success'])


if __name__ == '__main__':
    unittest.main()
