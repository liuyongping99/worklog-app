"""按重量计价校验单测（2026-10-14 简化方案,复用 product_units）

契约:
  - product_units 中 is_usingweightforcounting=1 的行 → 启用「按重量」单位白名单校验
  - 匹配规则与 YPP 同构(同表两轮匹配):
    1. product_name + spec 命中 spec_keyword 行
    2. 兜底 product_name + spec_keyword IS NULL 默认行
  - 未命中 / is_usingweightforcounting=0 → 跳过校验(向后兼容)
  - 未配置 allowed_units / 配置空 → 跳过校验
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from blueprints._helpers import (
    get_weight_allowed_units,
    validate_weight_unit,
    _parse_allowed_units,
)


class TestParseAllowedUnits:
    """_parse_allowed_units 字段格式兼容。"""

    def test_json_array(self):
        assert _parse_allowed_units('["kg","斤"]') == ['kg', '斤']

    def test_comma_separated(self):
        assert _parse_allowed_units('kg,斤') == ['kg', '斤']

    def test_chinese_comma(self):
        assert _parse_allowed_units('kg、斤') == ['kg', '斤']

    def test_space_separated(self):
        assert _parse_allowed_units('kg 斤') == ['kg', '斤']

    def test_empty(self):
        assert _parse_allowed_units('') is None
        assert _parse_allowed_units('   ') is None
        assert _parse_allowed_units(None) is None

    def test_mixed_separators(self):
        assert _parse_allowed_units('kg,斤、kg 两') == ['kg', '斤', 'kg', '两']


class TestGetWeightAllowedUnits:
    """get_weight_allowed_units 走 product_units cache 双轮匹配。"""

    def test_bound_passes_through(self):
        # 预置:本文件用 ProductUnit fixture
        from models import ProductUnit
        from models._db import get_db
        conn = get_db()
        cur = conn.cursor()
        # 测试用一次性 product_unit 行
        cur.execute("INSERT OR REPLACE INTO product_units "
                    "(product_name, spec_keyword, yards_per_piece, "
                    " is_usingyardforcounting, is_usingweightforcounting, allowed_units) "
                    "VALUES (?, ?, ?, 0, 1, ?)",
                    ('__test_w_bound__', None, 100, '["kg","斤"]'))
        conn.commit()
        conn.close()

        units = get_weight_allowed_units('__test_w_bound__', '')
        assert units == frozenset({'kg', '斤'})

        # 清理
        conn = get_db()
        conn.cursor().execute(
            "DELETE FROM product_units WHERE product_name = ?",
            ('__test_w_bound__',)
        )
        conn.commit()
        conn.close()

    def test_not_bound_returns_none(self):
        from models._db import get_db
        conn = get_db()
        cur = conn.cursor()
        cur.execute("INSERT OR REPLACE INTO product_units "
                    "(product_name, spec_keyword, yards_per_piece, "
                    " is_usingyardforcounting, is_usingweightforcounting, allowed_units) "
                    "VALUES (?, ?, ?, 0, 0, '')",
                    ('__test_w_notbound__', None, 100))
        conn.commit()
        conn.close()

        assert get_weight_allowed_units('__test_w_notbound__', '') is None

        # 清理
        conn = get_db()
        conn.cursor().execute(
            "DELETE FROM product_units WHERE product_name = ?",
            ('__test_w_notbound__',)
        )
        conn.commit()
        conn.close()

    def test_spec_keyword_match_wins(self):
        from models._db import get_db
        conn = get_db()
        cur = conn.cursor()
        # 默认行 kg
        cur.execute("INSERT OR REPLACE INTO product_units "
                    "(product_name, spec_keyword, yards_per_piece, "
                    " is_usingyardforcounting, is_usingweightforcounting, allowed_units) "
                    "VALUES (?, NULL, 100, 0, 1, ?)",
                    ('__test_w_spec__', '["kg"]'))
        # 特殊规格行 斤
        cur.execute("INSERT OR REPLACE INTO product_units "
                    "(product_name, spec_keyword, yards_per_piece, "
                    " is_usingyardforcounting, is_usingweightforcounting, allowed_units) "
                    "VALUES (?, ?, 100, 0, 1, ?)",
                    ('__test_w_spec__', 'A1.5米', '["斤"]'))
        conn.commit()
        conn.close()

        # 命中 spec_keyword 行
        units = get_weight_allowed_units('__test_w_spec__', 'A1.5米 200g')
        assert units == frozenset({'斤'}), f'应命中「斤」,实际 {units}'
        # 兜底默认行
        units_default = get_weight_allowed_units('__test_w_spec__', '')
        assert units_default == frozenset({'kg'}), f'应兜底「kg」,实际 {units_default}'

        # 清理
        conn = get_db()
        conn.cursor().execute(
            "DELETE FROM product_units WHERE product_name = ?",
            ('__test_w_spec__',)
        )
        conn.commit()
        conn.close()


class TestValidateWeightUnit:
    """validate_weight_unit 完整契约。"""

    def test_bound_passes(self):
        from models._db import get_db
        conn = get_db()
        cur = conn.cursor()
        cur.execute("INSERT OR REPLACE INTO product_units "
                    "(product_name, spec_keyword, yards_per_piece, "
                    " is_usingyardforcounting, is_usingweightforcounting, allowed_units) "
                    "VALUES (?, NULL, 100, 0, 1, ?)",
                    ('__test_w_v_pass__', '["kg","斤"]'))
        conn.commit()
        conn.close()

        r = {'product_name': '__test_w_v_pass__', 'specification': '', 'unit': 'kg'}
        result = validate_weight_unit(r)
        assert result['weight_unit_invalid'] is False
        assert sorted(result['allowed_units']) == ['kg', '斤']
        assert result['weight_source'] == 'product_units'

        conn = get_db()
        conn.cursor().execute(
            "DELETE FROM product_units WHERE product_name = ?",
            ('__test_w_v_pass__',))
        conn.commit()
        conn.close()

    def test_bound_rejects_bad_unit(self):
        from models._db import get_db
        conn = get_db()
        cur = conn.cursor()
        cur.execute("INSERT OR REPLACE INTO product_units "
                    "(product_name, spec_keyword, yards_per_piece, "
                    " is_usingyardforcounting, is_usingweightforcounting, allowed_units) "
                    "VALUES (?, NULL, 100, 0, 1, ?)",
                    ('__test_w_v_rej__', '["kg"]'))
        conn.commit()
        conn.close()

        r = {'product_name': '__test_w_v_rej__', 'specification': '', 'unit': 'y'}
        result = validate_weight_unit(r)
        assert result['weight_unit_invalid'] is True
        assert result['allowed_units'] == ['kg']
        assert result['actual_unit'] == 'y'

        conn = get_db()
        conn.cursor().execute(
            "DELETE FROM product_units WHERE product_name = ?",
            ('__test_w_v_rej__',))
        conn.commit()
        conn.close()

    def test_unbound_backward_compat(self):
        """未绑定的商品 → weight_unit_invalid=False,完全跳过(1000+ 老商品零变化)。"""
        r = {'product_name': '__no_such_product_zzz__',
             'specification': '', 'unit': 'y'}
        result = validate_weight_unit(r)
        assert result['weight_unit_invalid'] is False
        assert result['weight_source'] is None

    def test_empty_unit_rejected(self):
        from models._db import get_db
        conn = get_db()
        cur = conn.cursor()
        cur.execute("INSERT OR REPLACE INTO product_units "
                    "(product_name, spec_keyword, yards_per_piece, "
                    " is_usingyardforcounting, is_usingweightforcounting, allowed_units) "
                    "VALUES (?, NULL, 100, 0, 1, ?)",
                    ('__test_w_v_empty__', '["kg"]'))
        conn.commit()
        conn.close()

        r = {'product_name': '__test_w_v_empty__', 'specification': '', 'unit': ''}
        result = validate_weight_unit(r)
        assert result['weight_unit_invalid'] is True

        conn = get_db()
        conn.cursor().execute(
            "DELETE FROM product_units WHERE product_name = ?",
            ('__test_w_v_empty__',))
        conn.commit()
        conn.close()


class TestSeedSanity:
    """防护性:seed 脚本跑过后的实际状态。"""

    def test_wufang_abai_bound(self):
        from models._db import get_db
        conn = get_db()
        conn.row_factory = None  # default
        cur = conn.cursor()
        cur.execute("SELECT product_name, allowed_units FROM product_units "
                    "WHERE is_usingweightforcounting = 1 "
                    "AND product_name IN ('无纺布A白', '无纺布 A白')")
        rows = cur.fetchall()
        conn.close()
        assert len(rows) >= 1, 'seed 脚本应已为「无纺布A白」/「无纺布 A白」创建按重量配置'
        for (pn, au) in rows:
            assert au == '["kg","斤"]', f'允许单位应 kg+斤,实际 {au!r}'
