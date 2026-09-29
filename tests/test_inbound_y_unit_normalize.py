"""入库智能文本添加·「按 y 卖」自动归一化测试。

覆盖：
  1. compute_total_yards_from_remark helper 的各形式备注解析
  2. check_remark 抽出 expected 逻辑后行为不变（回归）
  3. inbound_ai_recognize 端点对「按 y 卖」商品的 quantity/unit 改写
  4. 端点对「非按 y 卖」/解析不到 X支 的情况不动 item（边界）
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models._db as _db


# ── helper 单元测试（无 DB，纯函数）─────────────────────────────

class HelperParseTests(unittest.TestCase):
    """直接调 _helpers.compute_total_yards_from_remark,覆盖各形式备注。"""

    def setUp(self):
        from blueprints._helpers import compute_total_yards_from_remark
        self.f = compute_total_yards_from_remark

    def test_addition_form_default_ypp(self):
        # "10支+2码" → 10 × ypp + 2
        self.assertEqual(self.f("10支+2码", 48.5), 487.0)
        self.assertEqual(self.f("5支+0.5码", 7.5), 38.0)

    def test_multiplication_form_per_piece_overrides_ypp(self):
        # "3支*48.5y+2y" → 3 × 48.5(per_piece) + 2,ypp=7.5 被忽略
        self.assertEqual(self.f("3支*48.5y+2y", 7.5), 147.5)
        # 反序: "48.5y*3支+2y"
        self.assertEqual(self.f("48.5y*3支+2y", 7.5), 147.5)

    def test_no_loose_yards(self):
        # "5支" 仅算 pieces × ypp
        self.assertEqual(self.f("5支", 7.5), 37.5)
        self.assertEqual(self.f("10支", 0.5), 5.0)

    def test_decimal_yards(self):
        # 小数散码
        self.assertEqual(self.f("2支+1.5码", 10.0), 21.5)
        self.assertEqual(self.f("1支*7.85y", 5.0), 7.85)

    def test_returns_none_when_no_pieces(self):
        # 没有 X支 → 不可解析
        self.assertIsNone(self.f("2码", 7.5))
        self.assertIsNone(self.f("", 7.5))
        self.assertIsNone(self.f("hello", 7.5))

    def test_returns_none_when_ypp_invalid(self):
        # ypp <= 0 → 不适用
        self.assertIsNone(self.f("10支+2码", 0))
        self.assertIsNone(self.f("10支+2码", -1))

    def test_returns_none_when_no_remark(self):
        self.assertIsNone(self.f(None, 7.5))
        self.assertIsNone(self.f("", 7.5))

    def test_quantity_fallback_when_remark_empty(self):
        """2026-09-24 修 bug:DeepSeek 对「环保杂胶 1.2白硬加面 10」不会主动把 X支+Y码
        写进 remark,但 quantity=10 是支数,应按 quantity × ypp 兜底算总码。"""
        self.assertEqual(self.f("", 7.5, quantity_str="10"), 75.0)
        self.assertEqual(self.f("", 48.5, quantity_str="10"), 485.0)
        # 小数也能算
        self.assertEqual(self.f("", 7.5, quantity_str="3.5"), 26.25)

    def test_quantity_fallback_invalid_returns_none(self):
        self.assertIsNone(self.f("", 7.5, quantity_str=""))
        self.assertIsNone(self.f("", 7.5, quantity_str=None))
        self.assertIsNone(self.f("", 7.5, quantity_str="abc"))

    def test_remark_takes_priority_over_quantity(self):
        """remark 含 X支+Y码 时走精确路径,quantity 被忽略(避免 10×ypp 与 10*ypp+2 冲突)。"""
        # remark "10支+2码" → 10*48.5 + 2 = 487
        # quantity="10" 兜底会是 10*48.5 = 485
        self.assertEqual(self.f("10支+2码", 48.5, quantity_str="10"), 487.0)


class CheckRemarkRegressionTests(unittest.TestCase):
    """抽出 expected 计算逻辑后,check_remark 老行为必须不变。"""

    def test_consistent_returns_empty(self):
        from blueprints._helpers import check_remark
        # quantity 与 expected 一致 → '' (无 mismatch)
        self.assertEqual(check_remark("10支+2码", "487", 48.5), '')
        self.assertEqual(check_remark("5支", "37.5", 7.5), '')

    def test_inconsistent_single_piece_info(self):
        from blueprints._helpers import check_remark
        # 单支不一致 → 'info'
        self.assertEqual(check_remark("1支+5码", "100", 7.5), 'info')

    def test_inconsistent_multi_pieces_warn(self):
        from blueprints._helpers import check_remark
        # 多支不一致 → 'warn'
        self.assertEqual(check_remark("10支+2码", "100", 48.5), 'warn')


# ── 端点集成测试（mock DeepSeek,真实打 DB）─────────────────────────

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
        sid = StaffDB.create('test', '文员')['id']
        with self.client.session_transaction() as s:
            s['operator_id'] = sid


    def tearDown(self):
        _db.DB_PATH = self._orig
        for ext in ('', '-wal', '-shm'):
            p = self.tmp.name + ext
            if os.path.exists(p):
                try: os.unlink(p)
                except OSError: pass


class InboundAiRecognizeNormalizeTests(_TempDb):
    """打 inbound_ai_recognize 端点的 text 分支,mock DeepSeek engine.recognize_text。"""

    def _seed_unit(self, name, ypp_int, using_yard=True, spec_kw=''):
        """插一条 product_units 配置。ypp_int 是 yards_per_piece×100 的整数(7.5码→750)。"""
        from models import ProductUnit
        ProductUnit.create(name, ypp_int, is_usingyardforcounting=using_yard, spec_keyword=spec_kw)

    def _patch_deepseek(self, items):
        """mock DeepSeekEngine.recognize_text 返回固定 items。"""
        # inbound.py 内调 get_ocr_engine('deepseek'),在文本分支里调 recognize_text
        class FakeEngine:
            def recognize_text(self, text):
                return {'success': True, 'items': items, 'customer_name': '', 'doc_number': ''}
        # ocr_engine.get_ocr_engine 是单例,需要按 name patch 走旁路
        return mock.patch(
            'blueprints.ocr_engine.get_ocr_engine',
            side_effect=lambda name: FakeEngine() if name == 'deepseek' else None,
        )

    def test_y_unit_product_gets_normalized(self):
        """按 y 卖商品 + 备注 10支+2码 → quantity=10*ypp+2, unit='y'。"""
        # 种子一条「杂胶」按 y 卖,每件 48.5 码(4850 整数),默认行
        self._seed_unit('杂胶', 4850)
        items = [
            {'product_name': '杂胶', 'specification': '黑色', 'quantity': '10', 'unit': '支',
             'remark': '10支+2码'},
        ]
        with self._patch_deepseek(items):
            res = self.client.post(
                '/api/v1/inbound-orders/ai-recognize',
                data={'text': '杂胶 黑色 加面 10支+2码', 'engine': 'deepseek'},
            )
        self.assertEqual(res.status_code, 200)
        body = res.get_json()
        self.assertTrue(body.get('success'))
        self.assertEqual(len(body['items']), 1)
        it = body['items'][0]
        # 数量 = 10 × 48.5 + 2 = 487
        self.assertEqual(float(it['quantity']), 487.0)
        self.assertEqual(it['unit'], 'y')
        # 备注原样保留
        self.assertIn('10支+2码', it.get('remark') or '')

    def test_per_piece_form_in_remark(self):
        """「X支*Yy」形式:per_piece 优先于 ypp。"""
        self._seed_unit('杂胶', 750)  # ypp=7.5
        items = [
            {'product_name': '杂胶', 'specification': '黑色', 'quantity': '3', 'unit': '支',
             'remark': '3支*48.5y+2y'},
        ]
        with self._patch_deepseek(items):
            res = self.client.post(
                '/api/v1/inbound-orders/ai-recognize',
                data={'text': '杂胶 黑色 3支*48.5y+2y'},
            )
        it = res.get_json()['items'][0]
        # 3 × 48.5 + 2 = 147.5
        self.assertEqual(float(it['quantity']), 147.5)
        self.assertEqual(it['unit'], 'y')

    def test_non_y_unit_product_unchanged(self):
        """非按 y 卖商品(is_usingyardforcounting=0)→ 不改写 quantity/unit。"""
        # 不插 product_units,或插一条 using_yard=False
        items = [
            {'product_name': '不存在的商品XYZ', 'specification': '', 'quantity': '10', 'unit': '支',
             'remark': '10支+2码'},
        ]
        with self._patch_deepseek(items):
            res = self.client.post(
                '/api/v1/inbound-orders/ai-recognize',
                data={'text': '不存在的商品XYZ 10支+2码'},
            )
        it = res.get_json()['items'][0]
        # 既不是按 y 卖,也无规则 → 原样
        self.assertEqual(it['quantity'], '10')
        self.assertEqual(it['unit'], '支')

    def test_no_pieces_in_remark_unchanged(self):
        """按 y 卖但 remark 解析不出 X支 → 不改写。"""
        self._seed_unit('杂胶', 4850)
        items = [
            {'product_name': '杂胶', 'specification': '黑色', 'quantity': '487', 'unit': 'y',
             'remark': ''},
        ]
        with self._patch_deepseek(items):
            res = self.client.post(
                '/api/v1/inbound-orders/ai-recognize',
                data={'text': '杂胶 黑色 总 487y'},
            )
        it = res.get_json()['items'][0]
        # 没 X支 可解析 → 保留 AI 原样
        self.assertEqual(it['quantity'], '487')
        self.assertEqual(it['unit'], 'y')

    def test_quantity_fallback_when_remark_empty_bug_repro(self):
        """2026-09-24 bug 复现:用户输入「环保杂胶，1.2白硬加面」,DeepSeek 只给
        quantity=10 unit=支 remark=空(没把支+码写进 remark)。
        老逻辑:compute_total_yards_from_remark('')=None → 跳过 → 不归一化 ❌
        新逻辑:quantity 兜底 → 10 × ypp = 总码数 → 归一化为 'y' ✅"""
        self._seed_unit('环保杂胶', 4850)  # ypp=48.5
        items = [
            {'product_name': '环保杂胶', 'specification': '1.2白硬加面',
             'quantity': '10', 'unit': '支', 'remark': ''},
        ]
        with self._patch_deepseek(items):
            res = self.client.post(
                '/api/v1/inbound-orders/ai-recognize',
                data={'text': '环保杂胶，1.2白硬加面，10'},
            )
        it = res.get_json()['items'][0]
        # 10 × 48.5 = 485
        self.assertEqual(float(it['quantity']), 485.0)
        self.assertEqual(it['unit'], 'y')

    def test_already_y_unit_not_double_converted(self):
        """DeepSeek 已经按 y 给出结果(quantity=487, unit=y) → 不重复换算(否则会
        把 487 当支数 × ypp = 误转)。"""
        self._seed_unit('环保杂胶', 4850)
        items = [
            {'product_name': '环保杂胶', 'specification': '1.2白硬加面',
             'quantity': '487', 'unit': 'y', 'remark': ''},
        ]
        with self._patch_deepseek(items):
            res = self.client.post(
                '/api/v1/inbound-orders/ai-recognize',
                data={'text': '环保杂胶 1.2白硬加面 487y'},
            )
        it = res.get_json()['items'][0]
        # unit 已是 y,跳过换算,保留 AI 原样
        self.assertEqual(it['quantity'], '487')
        self.assertEqual(it['unit'], 'y')

    def test_remark_with_pieces_wins_over_quantity(self):
        """当 remark 含「X支+Y码」,走精确路径;即使 quantity 是另一个数字也不混算。
        模拟:用户写「10支+2码」DeepSeek 误抽 quantity=10 unit=支;
        应产出 quantity=10*ypp+2=487 unit=y(不是 10*ypp=485)。"""
        self._seed_unit('杂胶', 4850)
        items = [
            {'product_name': '杂胶', 'specification': '黑色', 'quantity': '10', 'unit': '支',
             'remark': '10支+2码'},
        ]
        with self._patch_deepseek(items):
            res = self.client.post(
                '/api/v1/inbound-orders/ai-recognize',
                data={'text': '杂胶 黑色 10支+2码'},
            )
        it = res.get_json()['items'][0]
        # 走精确路径 → 10 × 48.5 + 2 = 487
        self.assertEqual(float(it['quantity']), 487.0)
        self.assertEqual(it['unit'], 'y')

    def test_image_branch_not_affected(self):
        """图片分支(没有 text 字段)走原逻辑,不在本次改动范围内 — 此处不重复造轮子。
        仅确保:有 image 但没有 text 的请求,现有逻辑返回 400,不被新代码污染。"""
        res = self.client.post(
            '/api/v1/inbound-orders/ai-recognize',
            data={'engine': 'deepseek'},
            content_type='multipart/form-data',
        )
        # 没 text 又没 image → 现有逻辑返回 400('没有上传图片')
        self.assertEqual(res.status_code, 400)


if __name__ == '__main__':
    unittest.main()
