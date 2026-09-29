"""summarize_remarks 散码计数 *Yy 隔离规则 — 防回归测试。

行为契约（2026-09-05）:
  - 「X支*Yy」中的 Yy 是「每支码数」(per_piece),不是散码,不应计入散码次数
  - 「X支+Yy」、「裸 Yy」 才是散码,正常计入
  - 备注里的「X支」始终计入 summary_pieces

Bug 现场：9-5 入库订单汇总行「320支 +8支散码」实际应为 5 支散码,
        多出的 3 次来自 21支*40y、6支*40y、1支*40y 中的 *40y。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from blueprints._helpers import summarize_remarks  # noqa: E402


def make_record(remark, unit_hint=''):
    """summarize_remarks 只需 remark + unit_hint 两个字段。"""
    return {'remark': remark, 'unit_hint': unit_hint}


class TestSummarizeRemarksLooseYy:
    """散码计数应剥离 *Yy 形式中的 Yy 部分。"""

    def test_pure_mul_form_no_loose(self):
        """「6支*40y」备注:没有真正的散码,loose=0。"""
        records = [make_record('6支*40y')]
        result = summarize_remarks(records)
        assert result['summary_pieces'] == 6
        assert result['summary_loose_pieces'] == 0, (
            f'「6支*40y」应 loose=0(40y 是 per_piece),实际 {result["summary_loose_pieces"]}'
        )

    def test_single_branch_with_mul_form_no_loose(self):
        """「1支*40y」单支 + per_piece:loose=0。"""
        records = [make_record('1支*40y')]
        result = summarize_remarks(records)
        assert result['summary_pieces'] == 1
        assert result['summary_loose_pieces'] == 0

    def test_mul_form_with_trailing_loose(self):
        """「21支*40y+46y」:46y 才是散码,40y 不是,loose=1。"""
        records = [make_record('21支*40y+46y')]
        result = summarize_remarks(records)
        assert result['summary_pieces'] == 21
        assert result['summary_loose_pieces'] == 1, (
            f'「21支*40y+46y」应 loose=1(只有 46y 是散码),实际 {result["summary_loose_pieces"]}'
        )

    def test_add_form_with_loose_counted(self):
        """「98支+18y」:18y 是散码,loose=1。"""
        records = [make_record('98支+18y')]
        result = summarize_remarks(records)
        assert result['summary_pieces'] == 98
        assert result['summary_loose_pieces'] == 1

    def test_multiple_loose_in_add_form(self):
        """「92支+33y+12y」:33y 和 12y 都是散码,loose=2。"""
        records = [make_record('92支+33y+12y')]
        result = summarize_remarks(records)
        assert result['summary_pieces'] == 92
        assert result['summary_loose_pieces'] == 2

    def test_inbound_2026_09_05_full_group(self):
        """9-5 入库订单 9 行明细:合计 loose 应为 5(不是当前 bug 的 8)。

        原始数据来源:inbound-records.html 2026-09-05 实际订单 9 行 + 汇总行显示
          「320支 +8支散码」(实际 8 是错的,正确是 5)
        """
        records = [
            make_record('98支+18y'),       # loose: 1 (18y)
            make_record('2支'),            # loose: 0
            make_record('92支+33y+12y'),   # loose: 2 (33y + 12y)
            make_record('89支+18y'),       # loose: 1 (18y)
            make_record('8支'),            # loose: 0
            make_record('3支'),            # loose: 0
            make_record('21支*40y+46y'),   # loose: 1 (46y),40y 不是散码
            make_record('6支*40y'),        # loose: 0 (40y 不是散码)
            make_record('1支*40y'),        # loose: 0 (40y 不是散码)
        ]
        result = summarize_remarks(records)
        assert result['summary_pieces'] == 320, f'pieces 应为 320,实际 {result["summary_pieces"]}'
        assert result['summary_loose_pieces'] == 5, (
            f'9-5 单 loose 应为 5,实际 {result["summary_loose_pieces"]} '
            f'(修 bug 前会是 8,因为 *40y 被错误算散码)'
        )
