# -*- coding: utf-8 -*-
"""库存引擎单测 —— blueprints/inventory_engine.py(B1 批次产出)。

覆盖:
  · 单位别名表(y→码 等)
  · aux_unit 三种格式解析 + 脏值不炸
  · YPP=0 陷阱(0 系数会把库存清零)
  · 规格级 product_units 优先于品名级 aux_unit
  · 匹配级联 L0 / L1 / L2 / L3 / L4 / L4b / L4c / L5
  · 折算:同单位 1:1 / 支→码 / 件→目标单位 / 缺规则报错

纯函数,不需要数据库。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from blueprints.inventory_engine import (          # noqa: E402
    UNIT_ALIAS, NON_COUNT_BASE_UNITS,
    norm_unit, sq, strip_7p, norm_spec, parse_aux_rules,
    get_ypp, get_piece, convert_to_base_unit,
    build_product_index, resolve_product,
    REASON_NO_CONVERSION, REASON_BAD_BASE_UNIT,
)


def _p(pid, name, spec='', base='码', aux=None, code=None):
    return {
        'product_id': pid, 'product_code': code or f'C{pid:05d}',
        'product_name': name, 'specification': spec,
        'base_unit': base, 'aux_unit': aux,
    }


class TestNorm(unittest.TestCase):
    def test_unit_alias(self):
        self.assertEqual(norm_unit('y'), '码')
        self.assertEqual(norm_unit('Y'), '码')
        self.assertEqual(norm_unit(' 码 '), '码')
        self.assertEqual(norm_unit('kg'), 'kg')
        # 未知单位原样返回,不炸
        self.assertEqual(norm_unit('xyz'), 'xyz')
        self.assertEqual(norm_unit(None), '')

    def test_sq(self):
        self.assertEqual(sq('绿精灵 特规'), '绿精灵特规')
        self.assertEqual(sq('绿精灵\u3000特规'), '绿精灵特规')
        self.assertEqual(sq(None), '')

    def test_strip_7p(self):
        # 只剥 7P
        self.assertEqual(strip_7p('7P环保杂胶'), '环保杂胶')
        self.assertEqual(strip_7p('7P环保磅布三文治'), '环保磅布三文治')
        # 15P / 21P 不剥 —— 那是将来的独立商品
        self.assertEqual(strip_7p('15P环保杂胶'), '15P环保杂胶')
        self.assertEqual(strip_7p('21P环保杂胶'), '21P环保杂胶')
        # 不带前缀的原样返回
        self.assertEqual(strip_7p('环保杂胶'), '环保杂胶')
        self.assertEqual(strip_7p(None), '')

    def test_norm_spec(self):
        self.assertEqual(norm_spec(' 1.2 黑双 '), '1.2黑双')
        self.assertEqual(norm_spec('120#20kg'), '120-20kg')
        self.assertEqual(norm_spec('0.14'), '014')

    def test_non_count_base_units(self):
        # 费用行 / 误解析 —— 不是货物计量单位
        self.assertIn('元', NON_COUNT_BASE_UNITS)
        self.assertIn('色', NON_COUNT_BASE_UNITS)
        # 扎是纸业真实单位,不算非法
        self.assertNotIn('扎', NON_COUNT_BASE_UNITS)


class TestAuxUnit(unittest.TestCase):
    def test_eq_format(self):
        self.assertEqual(parse_aux_rules('1支=46码'), {'支': 46.0, '码': 1.0})

    def test_decimal_eq_format(self):
        self.assertEqual(parse_aux_rules('1支=36.5码'), {'支': 36.5, '码': 1.0})

    def test_only_format(self):
        self.assertEqual(parse_aux_rules('1码'), {'码': 1.0})

    def test_garbage(self):
        self.assertEqual(parse_aux_rules(''), {})
        self.assertEqual(parse_aux_rules(None), {})
        self.assertEqual(parse_aux_rules('不是规则'), {})
        # 左边为 0 → 无效
        self.assertEqual(parse_aux_rules('0支=46码'), {})


class TestYppAndPiece(unittest.TestCase):
    def test_ypp_spec_keyword_priority(self):
        unit_map = {'杂胶': [
            {'spec_keyword': None, 'yards_per_piece': 36.5},
            {'spec_keyword': '0.4', 'yards_per_piece': 43.0},
        ]}
        # 规格命中关键字 → 用 43/100
        self.assertAlmostEqual(get_ypp('杂胶', '0.4黑', unit_map), 0.43)
        # 规格没命中 → 用默认行
        self.assertAlmostEqual(get_ypp('杂胶', '0.8黑', unit_map), 0.365)

    def test_ypp_zero_is_no_rule(self):
        """yards_per_piece=0 是占位行,必须当「无规则」,否则库存会被清零。"""
        unit_map = {'0.4无毒平纹': [
            {'spec_keyword': None, 'yards_per_piece': 0},
        ]}
        self.assertIsNone(get_ypp('0.4无毒平纹', 'xxx', unit_map))

    def test_piece_conversion(self):
        pm = {'日本纸': [
            {'spec_keyword': None, 'units_per_piece': 500, 'target_unit': '张'},
        ]}
        self.assertEqual(get_piece('日本纸', '777/700', pm), (500, '张'))
        self.assertEqual(get_piece('不存在', '', pm), (None, None))


class TestConvert(unittest.TestCase):
    def test_same_unit_is_1to1(self):
        p = _p(1, '令布', 'A', base='令')
        ok, factor, code, text = convert_to_base_unit(3, '令', p)
        self.assertTrue(ok)
        self.assertEqual(factor, 1.0)

    def test_y_to_code_via_alias(self):
        """入库写 y,基本单位是码 —— 归一后同单位,1:1。"""
        p = _p(1, '三文治', '1.2黑双', base='码')
        ok, factor, _, _ = convert_to_base_unit(100, 'y', p)
        self.assertTrue(ok)
        self.assertEqual(factor, 1.0)

    def test_zhi_to_code_via_aux(self):
        p = _p(2, '环保杂胶', '0.4', base='码', aux='1支=46码')
        ok, factor, _, _ = convert_to_base_unit(10, '支', p)
        self.assertTrue(ok)
        self.assertEqual(factor, 46.0)

    def test_spec_level_rule_beats_name_level(self):
        """规格级 product_units 优先于品名级 aux_unit。"""
        p = _p(3, '杂胶', '0.4', base='码', aux='1支=34.5码')
        unit_map = {'杂胶': [
            {'spec_keyword': '0.4', 'yards_per_piece': 36.5},
        ]}
        ok, factor, _, _ = convert_to_base_unit(10, '支', p, unit_map)
        self.assertTrue(ok)
        self.assertAlmostEqual(factor, 0.365)   # 36.5/100,不是 34.5

    def test_piece_to_target_unit(self):
        p = _p(4, '日本纸', '777/700', base='张')
        pm = {'日本纸': [{'spec_keyword': None, 'units_per_piece': 500, 'target_unit': '张'}]}
        ok, factor, _, _ = convert_to_base_unit(2, '件', p, {}, pm)
        self.assertTrue(ok)
        self.assertEqual(factor, 500)

    def test_missing_rule_is_no_conversion(self):
        p = _p(5, '进口新闻纸', '板装', base='kg', aux='1kg')
        ok, factor, code, text = convert_to_base_unit(3, '件', p)
        self.assertFalse(ok)
        self.assertEqual(code, REASON_NO_CONVERSION)

    def test_bad_base_unit(self):
        p = _p(6, '税金', '', base='元')
        ok, _, code, _ = convert_to_base_unit(3, '元', p)
        self.assertFalse(ok)
        self.assertEqual(code, REASON_BAD_BASE_UNIT)

    def test_empty_base_unit(self):
        p = _p(7, '万能胶', '16kg', base=None)
        ok, _, code, _ = convert_to_base_unit(3, '桶', p)
        self.assertFalse(ok)
        self.assertEqual(code, REASON_BAD_BASE_UNIT)


class TestResolve(unittest.TestCase):
    def setUp(self):
        self.index = build_product_index([
            _p(100, '环保杂胶', '0.4黑', code='02100101'),
            _p(101, '环保杂胶', '0.6黑', code='02100102'),
            _p(102, '绿精灵 特规', '足800（900*1260）', base='张', code='0105'),
            _p(103, '三文治', '1.2 黑双', code='020205'),
            _p(104, '环保磅布三文治', '0.8黑双 中性', code='02100303'),
            _p(105, '白磅布三文治', '0.8中性', code='02120103'),
            _p(106, '白磅布三文治', '0.8软性', code='02120108'),
            _p(107, '白磅布三文治', '0.8硬性', code='02120113'),
            _p(108, '唯一货', '', base='桶', code='0999'),
        ])

    def test_l0_by_code(self):
        p, how = resolve_product('随便写的品名', '随便写的规格', '02100101', self.index)
        self.assertIsNotNone(p)
        self.assertEqual(how, 'L0-编号')
        self.assertEqual(p['product_id'], 100)

    def test_l1_exact(self):
        p, how = resolve_product('环保杂胶', '0.4黑', None, self.index)
        self.assertEqual(how, 'L1-精确')
        self.assertEqual(p['product_id'], 100)

    def test_l2_whitespace_both_sides(self):
        """品名和规格的空格差异都归一。"""
        p, how = resolve_product('绿精灵特规', '足800（900*1260）', None, self.index)
        self.assertEqual(how, 'L2-去空格')
        self.assertEqual(p['product_id'], 102)

    def test_l2_spec_space_only(self):
        p, how = resolve_product('三文治', '1.2黑双', None, self.index)
        self.assertEqual(how, 'L2-去空格')
        self.assertEqual(p['product_id'], 103)

    def test_l3_strip_7p(self):
        p, how = resolve_product('7P环保杂胶', '0.4黑', None, self.index)
        self.assertIsNotNone(p)
        self.assertEqual(p['product_id'], 100)
        self.assertTrue(how.startswith(('L1', 'L2', 'L3')))

    def test_l3_strip_7p_with_spec_space(self):
        """7P 前缀 + 规格空格差异,两层归一叠加。"""
        p, how = resolve_product('7P环保磅布三文治', '0.8黑双中性', None, self.index)
        self.assertIsNotNone(p)
        self.assertEqual(p['product_id'], 104)

    def test_l4_spec_normalized_unique(self):
        """规格归一:'#'→'-' / '0.14'→'014',同族唯一才接受。"""
        idx = build_product_index([
            _p(200, '电油', '120#20kg', base='桶', code='C00200'),
            _p(201, '薄料', '0.14', base='kg', code='C00201'),
        ])
        p, how = resolve_product('电油', '120-20kg', None, idx)
        self.assertIsNotNone(p)
        self.assertEqual(p['product_id'], 200)
        self.assertEqual(how, 'L4-规格归一唯一解')

        p, how = resolve_product('薄料', '014', None, idx)
        self.assertIsNotNone(p)
        self.assertEqual(p['product_id'], 201)
        self.assertEqual(how, 'L4-规格归一唯一解')

    def test_l4b_spec_prefix_unique(self):
        """规格缩写:'0.8中' → '0.8中性'(同族唯一)。"""
        p, how = resolve_product('白磅布三文治', '0.8中', None, self.index)
        self.assertIsNotNone(p)
        self.assertEqual(p['product_id'], 105)
        self.assertIn('L4b', how)

    def test_l4b_ambiguous_prefix_rejected(self):
        """'0.8' 同时是 '0.8中性'/'0.8软性'/'0.8硬性' 的前缀 → 歧义,必须跳过。"""
        p, how = resolve_product('白磅布三文治', '0.8', None, self.index)
        self.assertIsNone(p)
        self.assertEqual(how, 'L5-无解')

    def test_l4c_unique_name_without_spec(self):
        p, how = resolve_product('唯一货', '', None, self.index)
        self.assertIsNotNone(p)
        self.assertEqual(p['product_id'], 108)

    def test_l5_no_solution(self):
        p, how = resolve_product('完全不存在的东西', '规格', None, self.index)
        self.assertIsNone(p)
        self.assertEqual(how, 'L5-无解')

    def test_no_same_name_fallback(self):
        """同名多 SKU 时**不猜** —— 同名任一行兜底已废除。"""
        # '环保杂胶' 有 0.4黑 / 0.6黑 两行,规格对不上就跳过
        p, how = resolve_product('环保杂胶', '0.9白', None, self.index)
        self.assertIsNone(p)
        self.assertEqual(how, 'L5-无解')

    def test_duplicate_key_is_ambiguous_not_first_row(self):
        """同名同规格多 SKU → 该键作废,**不能取第一条**。

        背景:订单明细表没有 product_code 列(用户决定不加),
        所以引擎挑错一行 = 静默错配,库存错了要等盘点才发现。
        """
        idx = build_product_index([
            _p(300, '无纺布 A白', 'A1.5米 200g', base='kg', code='C00300'),
            _p(301, '无纺布 A白', 'A1.5米 200g', base='kg', code='C00301'),
        ])
        p, how = resolve_product('无纺布 A白', 'A1.5米 200g', None, idx)
        self.assertIsNone(p, '重复键不能命中任何一行')
        self.assertEqual(how, 'L5-无解')
        # 索引里要留痕,便于排查
        self.assertIn(('无纺布 A白', 'A1.5米 200g'), idx.ambiguous_ns)
        self.assertEqual(len(idx.ambiguous_ns[('无纺布 A白', 'A1.5米 200g')]), 2)

    def test_duplicate_key_after_space_normalize_is_also_ambiguous(self):
        """去空格后撞成同一个键 → 走「去空格匹配」这条路必须拒绝。

        构造要点:两个 SKU 的**原始品名都不能等于归一形态**,
        否则 L1 会用「去空格品名」再查原始索引直接命中,根本走不到歧义判定。
          '无纺布 A白' + '无纺布A 白'  → 归一都是 '无纺布A白'
        """
        idx = build_product_index([
            _p(310, '无纺布 A白', 'A1.5米 200g', base='kg', code='C00310'),
            _p(311, '无纺布A 白', 'A1.5米 200g', base='kg', code='C00311'),
        ])
        # 归一形态查不到原始键 → 只能走 L2 的归一索引 → 而归一索引撞车 → 不许挑
        p, how = resolve_product('无纺布A白', 'A1.5米 200g', None, idx)
        self.assertIsNone(p, '归一后撞车的键不能命中任何一行')
        self.assertEqual(how, 'L5-无解')
        self.assertIn(('无纺布A白', 'A1.5米200g'), idx.ambiguous_ns_sq)

    def test_duplicate_raw_key_is_ambiguous(self):
        """原始键就重复 → L1 也不许取第一条。"""
        idx = build_product_index([
            _p(320, '鱼鳞布纯胶', '0.6黑中软', base='码', code='C00320'),
            _p(321, '鱼鳞布纯胶', '0.6黑中软', base='码', code='C00321'),
        ])
        p, how = resolve_product('鱼鳞布纯胶', '0.6黑中软', None, idx)
        self.assertIsNone(p)
        self.assertEqual(how, 'L5-无解')
        self.assertIn(('鱼鳞布纯胶', '0.6黑中软'), idx.ambiguous_ns)


if __name__ == '__main__':
    unittest.main()
