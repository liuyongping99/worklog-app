"""voice_llm 测试(monkeypatch openai 客户端)"""
import pytest


def test_load_category_tree_returns_level4_only():
    """只加载 level=4 叶子类目名,排除父节点"""
    from blueprints.voice_llm import load_category_tree
    names = load_category_tree()
    # 必须包含已知的叶子类目
    assert '白磅布三文治' in names
    assert '7P环保杂胶' in names
    assert 'RA弹力胶' in names
    # 不应包含父节点
    assert '商品' not in names
    assert '纸类' not in names  # level=2
    assert '杂胶' not in names  # level=3(中间节点)
    # 应有 100+ 条
    assert len(names) >= 100


def test_build_category_tree_prompt_contains_names():
    from blueprints.voice_llm import build_category_tree_prompt
    prompt = build_category_tree_prompt()
    assert '白磅布三文治' in prompt
    assert '7P环保杂胶' in prompt
    # 不应过长(粗略估 token 上限)
    # 108 条 + 简介 ≈ 1500 字符
    assert len(prompt) < 10000


def test_extract_items_parses_deepseek_json(monkeypatch):
    """mock openai 返回 JSON,验证 extract_items 解析"""
    from unittest.mock import MagicMock
    from blueprints import voice_llm

    fake_response = MagicMock()
    fake_response.choices = [MagicMock()]
    # DeepSeek 应返回 {customer, items[]} dict 形式(按 prompt 规范)
    fake_response.choices[0].message.content = (
        '{"customer":"丰泽","items":['
        '{"phrase_part":"白中磅","spec_part":"1.2","quantity":20,"unit":"支"},'
        '{"phrase_part":"环保杂胶","spec_part":null,"quantity":50,"unit":"个"}'
        ']}'
    )

    monkeypatch.setattr(voice_llm, '_call_deepseek',
                        lambda messages: fake_response.choices[0].message.content)

    result = voice_llm.extract_items('丰泽出货,白中磅1.2二十支,环保杂胶50个')
    assert result['customer'] == '丰泽'
    assert len(result['items']) == 2
    assert result['items'][0]['phrase_part'] == '白中磅'
    assert result['items'][0]['spec_part'] == '1.2'
    assert result['items'][0]['quantity'] == 20
    assert result['items'][0]['unit'] == '支'


def test_extract_items_handles_null_quantity(monkeypatch):
    """无数量兼容"""
    from unittest.mock import MagicMock
    from blueprints import voice_llm

    fake_response = MagicMock()
    fake_response.choices = [MagicMock()]
    fake_response.choices[0].message.content = (
        '[{"phrase_part":"环保杂胶","spec_part":null,"quantity":null,"unit":null}]'
    )
    monkeypatch.setattr(voice_llm, '_call_deepseek',
                        lambda messages: fake_response.choices[0].message.content)

    result = voice_llm.extract_items('环保杂胶')
    assert result['customer'] is None
    assert len(result['items']) == 1
    assert result['items'][0]['quantity'] is None
    assert result['items'][0]['unit'] is None


def test_extract_items_falls_back_to_rules_on_deepseek_error(monkeypatch):
    """DeepSeek 失败时降级到规则切句"""
    from blueprints import voice_llm

    def raise_error(messages):
        raise RuntimeError('DeepSeek timeout')

    monkeypatch.setattr(voice_llm, '_call_deepseek', raise_error)

    result = voice_llm.extract_items('白中磅1.2二十支,环保杂胶50个')
    # 规则切句仍应能切出 2 条
    assert len(result['items']) >= 1
    # 至少包含一个有效 quantity
    has_qty = any(item.get('quantity') is not None for item in result['items'])
    assert has_qty


def test_llm_fallback_selects_from_candidates(monkeypatch):
    """LLM 兜底:给 Top 30 候选,选 1 个"""
    from unittest.mock import MagicMock
    from blueprints import voice_llm

    fake_response = MagicMock()
    fake_response.choices = [MagicMock()]
    fake_response.choices[0].message.content = '{"product_id": 142}'
    monkeypatch.setattr(voice_llm, '_call_deepseek',
                        lambda messages: fake_response.choices[0].message.content)

    candidates = [
        {'product_id': 142, 'product_name': '定做RA白弹大直管',
         'specification': '1.4m*组1.5*足100y', 'score': 60.0},
        {'product_id': 169, 'product_name': '白磅布三文治',
         'specification': '1.2中性', 'score': 55.0},
    ]
    result = voice_llm.llm_fallback_select('RA1.5', '1.5', candidates)
    assert result == 142


def test_llm_fallback_returns_none_on_no_match(monkeypatch):
    """LLM 兜底返回 null → product_id 为 None"""
    from unittest.mock import MagicMock
    from blueprints import voice_llm

    fake_response = MagicMock()
    fake_response.choices = [MagicMock()]
    fake_response.choices[0].message.content = '{"product_id": null}'
    monkeypatch.setattr(voice_llm, '_call_deepseek',
                        lambda messages: fake_response.choices[0].message.content)

    candidates = [{'product_id': 999, 'product_name': '无关品', 'specification': ''}]
    assert voice_llm.llm_fallback_select('完全不存在的短语', None, candidates) is None
