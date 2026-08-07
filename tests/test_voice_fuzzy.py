"""voice_fuzzy RapidFuzzy + 规则切句测试"""


def test_fuzzy_match_category_finds_top_match():
    from blueprints.voice_fuzzy import fuzzy_match_category
    results = fuzzy_match_category('白中磅', top_n=3)
    assert len(results) >= 1
    # 第一个应该是白磅布三文治(类目 fuzzy 命中)
    assert results[0]['name'] == '白磅布三文治'
    assert results[0]['score'] > 50


def test_fuzzy_match_category_handles_no_match():
    from blueprints.voice_fuzzy import fuzzy_match_category
    results = fuzzy_match_category('完全不存在的短语xyz', top_n=3)
    # 仍可能返回,但 score 应该很低
    if results:
        assert results[0]['score'] < 60


def test_fuzzy_match_product_returns_candidates():
    from blueprints.voice_fuzzy import fuzzy_match_product
    results = fuzzy_match_product('白磅布三文治', top_n=5)
    assert len(results) >= 1
    assert all('product_id' in r for r in results)
    assert all('product_name' in r for r in results)
    assert all('specification' in r for r in results)
    assert all('score' in r for r in results)


def test_rule_based_segment_basic():
    """规则切句:逗号分隔 + 数字单位提取"""
    from blueprints.voice_fuzzy import rule_based_segment
    result = rule_based_segment('白中磅1.2二十支,环保杂胶50个')
    assert result['customer'] is None  # 规则切不抽客户
    assert len(result['items']) >= 1
    # 至少有一个 quantity 被识别
    qtys = [item.get('quantity') for item in result['items']]
    assert any(q is not None for q in qtys)


def test_rule_based_segment_chinese_numbers():
    """中文数字转换"""
    from blueprints.voice_fuzzy import rule_based_segment
    result = rule_based_segment('白中磅五十支')
    items = result['items']
    assert len(items) >= 1
    assert any(item.get('quantity') == 50 for item in items)


def test_rule_based_segment_no_numbers():
    """无数量:quantity 为 None"""
    from blueprints.voice_fuzzy import rule_based_segment
    result = rule_based_segment('环保杂胶')
    assert len(result['items']) == 1
    assert result['items'][0]['phrase_part'] == '环保杂胶'
    assert result['items'][0]['quantity'] is None
