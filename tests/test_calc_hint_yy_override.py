"""calc_hint 优先级 0:备注有 *Yy 时,auxHint 用 per_piece 算,不再走商品默认 YPP。

行为契约（2026-09-05 用户拍板）:
  优先级:
    0. 备注有 *Yy           → qty / per_piece     (新增)
    1. unit='支'            → qty 直接当支数
    2. ypp>0 (默认 YPP)      → qty / ypp
    3. 无 YPP, 备注有 X支   → 兜底显示 X支
    4. 都不命中              → ''

目的:让 auxHint 列跟 check_remark 用同一套 per_piece 优先级,消除「auxHint 按
默认 YPP 算但 check_remark 按 *Yy 算」的视觉不一致。

参考 bug 现场:9-5 入库订单 9 行明细中,行 #3/#7/#8/#9 用 *Yy 时旧 auxHint 会跟备注
换算的支数不一致(旧 17 支 vs 备注 21 支)。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from blueprints._helpers import calc_hint  # noqa: E402


class TestCalcHintYyOverride:
    """优先级 0:*Yy per_piece 覆盖默认 YPP。"""

    # ---- 优先级 0:备注 *Yy ----

    def test_per_piece_override_with_mul_x_zhi(self):
        """「6支*40y」+ 240码 + 默认 ypp=50 → 按 40 算 6支(旧:4支+40码按 50)。"""
        assert calc_hint('240', ypp=50, unit='y', remark='6支*40y') == '6支'

    def test_per_piece_override_with_mul_y_x(self):
        """「40y*6支」(顺序反过来)也要识别。"""
        assert calc_hint('240', ypp=50, unit='y', remark='40y*6支') == '6支'

    def test_per_piece_override_with_remainder(self):
        """「21支*40y+46y」+ 886码 + 默认 ypp=50 → 886/40=22.15 → 22支+6.0码(旧:17支+36码)。
        注:6.0 而非 6 是 round(remainder, 2) 浮点渲染,与 calc_hint 优先级 2/3 既有行为一致
        (如旧版「98支+18.0码」也是 .0 渲染)。"""
        assert calc_hint('886', ypp=50, unit='y', remark='21支*40y+46y') == '22支+6.0码'

    def test_single_branch_per_piece_override(self):
        """「1支*40y」+ 40码 + 默认 ypp=50 → 按 40 算 1支(旧:0支+40码)。"""
        assert calc_hint('40', ypp=50, unit='y', remark='1支*40y') == '1支'

    def test_per_piece_overrides_default_ypp(self):
        """*Yy 必须赢过默认 YPP:即使默认 YPP 算起来更「整齐」也不行。"""
        # 默认 ypp=40 与 *Yy 一致不会触发优先级 0;改默认 ypp=60 验证 *Yy 赢
        # 100码 / 默认 60 旧:1支+40码;按 *40 新:2支+20.0码(round 浮点渲染)
        assert calc_hint('100', ypp=60, unit='y', remark='5支*40y') == '2支+20.0码'

    # ---- 优先级 1:unit='支' 直接显示(无 *Yy 时) ----

    def test_unit_zhi_no_remark(self):
        assert calc_hint('500', ypp=0, unit='支', remark='') == '500支'

    def test_unit_zhi_no_remark_float(self):
        assert calc_hint('33.5', ypp=0, unit='支', remark='') == '33.5支'

    # ---- 优先级 1 vs 0:unit='支' 跟 *Yy 同时存在时谁优先?----
    # 既然「数量本身就是支数」最直接,unit='支' 应该优先于 *Yy(没有 *Yy 的意义)。
    # 这条不写测试,记入 README 备注:优先级 1 在前,优先级 0 仅用于 y/码 单位。

    # ---- 优先级 2:ypp>0 无 *Yy(默认 YPP) ----

    def test_default_ypp_no_mul(self):
        """无 *Yy 时按默认 YPP 算。"""
        assert calc_hint('4918', ypp=50, unit='y', remark='98支+18y') == '98支+18.0码'

    def test_default_ypp_clean_division(self):
        assert calc_hint('100', ypp=50, unit='y', remark='2支') == '2支'

    # ---- 优先级 3:无 YPP,从备注提 X支 ----

    def test_no_ypp_fallback_to_remark(self):
        """ypp=0,unit='kg',备注 '3支' → 3支。"""
        assert calc_hint('100', ypp=0, unit='kg', remark='3支') == '3支'

    # ---- 9-5 入库订单整组验证 ----

    def test_inbound_2026_09_05_full_group(self):
        """9-5 入库订单 9 行明细 + 默认 YPP=50 + 按 *Yy 优先级 0 后行为:

        | # | 备注                  | qty  | 旧 auxHint      | 新 auxHint         |
        |---|-----------------------|------|-----------------|--------------------|
        | 1 | 98支+18y              | 4918 | 98支+18.0码     | 同(无 *Yy)        |
        | 2 | 2支                   | 100  | 2支             | 同                 |
        | 3 | 92支+33y+12y          | 4645 | 92支+45.0码     | 同(无 *Yy)        |
        | 4 | 89支+18y              | 4468 | 89支+18.0码     | 同                 |
        | 5 | 8支                   | 400  | 8支             | 同                 |
        | 6 | 3支                   | 150  | 3支             | 同                 |
        | 7 | 21支*40y+46y          | 886  | 17支+36.0码     | 22支+6码           |
        | 8 | 6支*40y               | 240  | 4支+40.0码      | 6支                |
        | 9 | 1支*40y               | 40   | 0支+40.0码      | 1支                |
        """
        cases = [
            # (qty, ypp, unit, remark, expected)
            ('4918', 50, 'y', '98支+18y', '98支+18.0码'),
            ('100', 50, 'y', '2支', '2支'),
            ('4645', 50, 'y', '92支+33y+12y', '92支+45.0码'),
            ('4468', 50, 'y', '89支+18y', '89支+18.0码'),
            ('400', 50, 'y', '8支', '8支'),
            ('150', 50, 'y', '3支', '3支'),
            ('886', 50, 'y', '21支*40y+46y', '22支+6.0码'),
            ('240', 50, 'y', '6支*40y', '6支'),
            ('40', 50, 'y', '1支*40y', '1支'),
        ]
        for qty, ypp, unit, remark, expected in cases:
            actual = calc_hint(qty, ypp=ypp, unit=unit, remark=remark)
            assert actual == expected, (
                f'calc_hint({qty!r}, ypp={ypp}, unit={unit!r}, remark={remark!r}): '
                f'期望 {expected!r},实际 {actual!r}'
            )

    # ---- 既有的 9 行明细汇总:📊 明细支数 应变化 ----
    # 注:这条不在 calc_hint 里,在 summarize_remarks 用 unit_hint 算汇总行,
    # 那里行为变化由 summarize_remarks 测试覆盖。calc_hint 测试只到函数返回值。

    # ---- 优先级 4:空串兜底 ----

    def test_empty_when_no_match(self):
        """ypp=0, 无 X支 备注 → 空串。"""
        assert calc_hint('100', ypp=0, unit='kg', remark='急单') == ''

    def test_empty_on_invalid_qty(self):
        """qty 无法转 float → 空串。"""
        assert calc_hint('abc', ypp=50, unit='y', remark='1支') == ''
