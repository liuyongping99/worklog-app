"""单测 helpers 纯函数 + 关键路由

运行方法（在项目根目录）：
    python -m unittest tests.test_basic -v

不需要安装 pytest，用 Python 内置 unittest。
"""
import os
import sys
import unittest
from datetime import datetime

# 让 python 找到 blueprints/、models.py、app.py
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'blueprints'))

from blueprints._helpers import (
    calc_hint, check_remark, summarize_remarks, get_upload_dir
)


class CalcHintTests(unittest.TestCase):
    """calc_hint(quantity_str, ypp) → 支数提示"""

    def test_exact_division(self):
        self.assertEqual(calc_hint('50', 2.0), '25支')

    def test_with_remainder(self):
        self.assertEqual(calc_hint('50', 1.5), '33支+0.5码')

    def test_zero_ypp_skips(self):
        self.assertEqual(calc_hint('50', 0), '')

    def test_negative_ypp_skips(self):
        self.assertEqual(calc_hint('50', -1.5), '')

    def test_invalid_quantity(self):
        self.assertEqual(calc_hint('abc', 1.5), '')

    def test_none_quantity(self):
        self.assertEqual(calc_hint(None, 1.5), '')

    def test_zero_quantity(self):
        self.assertEqual(calc_hint('0', 1.5), '0支')

    def test_small_remainder_below_tolerance(self):
        """余数 < 0.01 视为 0，返回纯支数"""
        self.assertEqual(calc_hint('10.005', 1.0), '10支')


class CheckRemarkTests(unittest.TestCase):
    """check_remark(remark, quantity_str, ypp) → '' / 'info' / 'warn'"""

    def test_consistent(self):
        # 33 支 × 1.5 = 49.5
        self.assertEqual(check_remark('33支', '49.5', 1.5), '')

    def test_consistent_with_loose(self):
        # 33 支 × 1.5 + 0.5 码 = 50
        self.assertEqual(check_remark('33支+0.5码', '50', 1.5), '')

    def test_inconsistent_warn(self):
        # 33 支 × 1.5 = 49.5，但实际数量 100，明显不一致
        self.assertEqual(check_remark('33支', '100', 1.5), 'warn')

    def test_inconsistent_info_single_piece(self):
        # 1 支 × 1.5 = 1.5，但实际 5（轻微不一致 → info）
        self.assertEqual(check_remark('1支', '5', 1.5), 'info')

    def test_empty_remark(self):
        self.assertEqual(check_remark('', '50', 1.5), '')

    def test_zero_ypp(self):
        self.assertEqual(check_remark('33支', '50', 0), '')

    def test_no_pieces_in_remark(self):
        # 备注里没有"X支"，视为不适用
        self.assertEqual(check_remark('50码', '50', 1.5), '')


class SummarizeRemarksTests(unittest.TestCase):
    """summarize_remarks(records) → {summary_pieces, summary_loose_pieces}"""

    def test_multiple_records(self):
        records = [
            {'remark': '10支'},
            {'remark': '20支+0.5码'},
            {'remark': '5支'},
        ]
        result = summarize_remarks(records)
        self.assertEqual(result['summary_pieces'], 35)
        # 两条备注里有码数（20支+0.5码 1 条 + 5支 0 条）= 1
        self.assertEqual(result['summary_loose_pieces'], 1)

    def test_empty_remarks(self):
        records = [{'remark': ''}, {'remark': None}]
        result = summarize_remarks(records)
        self.assertEqual(result['summary_pieces'], 0)
        self.assertEqual(result['summary_loose_pieces'], 0)

    def test_no_remark_field(self):
        records = [{}, {'remark': '3支'}]
        result = summarize_remarks(records)
        self.assertEqual(result['summary_pieces'], 3)
        self.assertEqual(result['summary_loose_pieces'], 0)


class GetUploadDirTests(unittest.TestCase):
    """get_upload_dir() → (dir, 'YYYY-MM')"""

    def test_returns_tuple(self):
        upload_dir, month_str = get_upload_dir()
        self.assertTrue(isinstance(upload_dir, str))
        self.assertTrue(isinstance(month_str, str))
        self.assertEqual(month_str, datetime.now().strftime('%Y-%m'))
        self.assertTrue(month_str in upload_dir)

    def test_explicit_month(self):
        upload_dir, month_str = get_upload_dir('2025-01')
        self.assertEqual(month_str, '2025-01')
        self.assertTrue('2025-01' in upload_dir)

    def test_dir_exists(self):
        upload_dir, _ = get_upload_dir()
        self.assertTrue(os.path.isdir(upload_dir))


class RouteSmokeTests(unittest.TestCase):
    """主要页面/路由的 smoke 测试（GET 必须返回 200）"""

    @classmethod
    def setUpClass(cls):
        from app import app
        cls.app = app
        cls.client = app.test_client()

    def test_home_page(self):
        r = self.client.get('/')
        self.assertEqual(r.status_code, 200)

    def test_experience_page(self):
        r = self.client.get('/experience')
        self.assertEqual(r.status_code, 200)

    def test_shipping_page(self):
        r = self.client.get('/shipping-records')
        self.assertEqual(r.status_code, 200)

    def test_inbound_page(self):
        r = self.client.get('/inbound-records')
        self.assertEqual(r.status_code, 200)

    def test_loading_page(self):
        r = self.client.get('/loading-orders')
        self.assertEqual(r.status_code, 200)

    def test_404_page(self):
        r = self.client.get('/this-route-does-not-exist')
        self.assertEqual(r.status_code, 404)


if __name__ == '__main__':
    unittest.main(verbosity=2)
