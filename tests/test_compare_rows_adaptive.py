"""Tests for _classify_product + few-shot helpers."""
import os
import sys
import pytest

# Ensure project root on path
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# 这是超前写的 TDD 红测试：对应 plan docs/superpowers/plans/2026-07-25-compare-rows-自适应优化.md
# Task 1(CLASS_FALLBACK_KEYWORDS + _classify_product)尚未实现。
# 用 allow_module_level skip 兜住,否则模块级 ImportError 会中断【整个 pytest collection】,
# 导致全套测试一个都跑不了。Task 1 落地后此 skip 自动失效,测试即刻生效。
try:
    from blueprints.ocr_engine import CLASS_FALLBACK_KEYWORDS  # noqa: F401
except ImportError:
    pytest.skip(
        "CLASS_FALLBACK_KEYWORDS 未实现 — compare-rows 自适应优化 plan Task 1 待做",
        allow_module_level=True,
    )


class TestClassifyProduct:
    """Test _classify_product() against both product table and fallback keywords."""

    @staticmethod
    def _classify(name):
        """Import and call the real function."""
        from blueprints.ocr_engine import DeepSeekEngine
        return DeepSeekEngine._classify_product(name)

    def test_product_table_match_杂胶(self):
        """7P环保杂胶 should match product table → 0210 (actual DB: category 143 → parent 0210)."""
        assert self._classify('7P环保杂胶') == '0210'

    def test_product_table_match_无纺布(self):
        """无纺布 A料 should match product table."""
        assert self._classify('无纺布 A料') == '0204'

    def test_fallback_keyword_纯胶(self):
        """品名不在 product 表但含'纯胶'关键词 → 0301."""
        # Use a name unlikely to be in product table
        assert self._classify('测试纯胶XYZ') == '0301'

    def test_fallback_keyword_回力胶(self):
        """含'EVA'关键词 → 0401."""
        assert self._classify('EVA泡棉') == '0401'

    def test_no_match_returns_none(self):
        """未知品名 → None."""
        assert self._classify('XYZ123不明商品') is None

    def test_empty_string(self):
        """空字符串 → None."""
        assert self._classify('') is None

    def test_none_input(self):
        """None 输入 → None（不抛异常）."""
        assert self._classify(None) is None


class TestGetHvCases:
    """Test _get_hv_cases() — runs against real DB (integration)."""

    def test_returns_list(self):
        from blueprints.ocr_engine import DeepSeekEngine
        cases = DeepSeekEngine._get_hv_cases('0201')
        assert isinstance(cases, list)

    def test_returns_at_most_3(self):
        from blueprints.ocr_engine import DeepSeekEngine
        cases = DeepSeekEngine._get_hv_cases('0201')
        assert len(cases) <= 3

    def test_unknown_category_returns_empty(self):
        from blueprints.ocr_engine import DeepSeekEngine
        cases = DeepSeekEngine._get_hv_cases('9999')
        assert cases == []

    def test_each_case_has_required_keys(self):
        from blueprints.ocr_engine import DeepSeekEngine
        cases = DeepSeekEngine._get_hv_cases('0201')
        for c in cases:
            assert 'product_name' in c
            assert 'specification' in c


class TestBuildFewShot:
    """Test _build_few_shot() — pure string formatting."""

    def test_empty_cases_returns_empty_string(self):
        from blueprints.ocr_engine import DeepSeekEngine
        assert DeepSeekEngine._build_few_shot([]) == ''

    def test_format_includes_product_names(self):
        from blueprints.ocr_engine import DeepSeekEngine
        cases = [
            {'product_name': '7P环保杂胶', 'specification': '0.8单面加面'},
            {'product_name': '无纺布 A料', 'specification': 'A1.5m 200g'},
        ]
        result = DeepSeekEngine._build_few_shot(cases)
        assert '7P环保杂胶' in result
        assert '无纺布 A料' in result
        assert '人工确认' in result

    def test_truncates_to_3_cases(self):
        from blueprints.ocr_engine import DeepSeekEngine
        cases = [
            {'product_name': f'商品{i}', 'specification': f'规格{i}'}
            for i in range(10)
        ]
        result = DeepSeekEngine._build_few_shot(cases)
        assert '商品0' in result
        assert '商品2' in result
        assert '商品9' not in result


class TestCompareRowsIntegration:
    """Test the post-processing relaxation — pure logic, no real API call."""

    def test_relaxation_red_to_yellow_when_hv_exists(self):
        from blueprints.ocr_engine import DeepSeekEngine
        result = DeepSeekEngine._apply_few_shot_and_relaxation([
            {'record_id': 1, 'match_status': 'red', 'reason': 'no match'},
            {'record_id': 2, 'match_status': 'green', 'reason': 'matched'},
        ], '0201', True)
        assert result[0]['match_status'] == 'yellow'  # red → yellow
        assert result[1]['match_status'] == 'green'    # green unchanged

    def test_no_relaxation_when_no_hv(self):
        from blueprints.ocr_engine import DeepSeekEngine
        result = DeepSeekEngine._apply_few_shot_and_relaxation([
            {'record_id': 1, 'match_status': 'red', 'reason': 'no match'},
        ], '0301', False)
        assert result[0]['match_status'] == 'red'  # unchanged

    def test_relaxation_preserves_reason(self):
        from blueprints.ocr_engine import DeepSeekEngine
        result = DeepSeekEngine._apply_few_shot_and_relaxation([
            {'record_id': 1, 'match_status': 'red',
             'reason': 'OCR文字中未找到该商品信息'},
        ], '0201', True)
        assert result[0]['match_status'] == 'yellow'
        assert '未找到' in result[0]['reason']
