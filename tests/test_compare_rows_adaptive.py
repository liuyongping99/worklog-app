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
