"""口语短语归一化测试"""


def test_normalize_strips_whitespace():
    from blueprints.voice_fuzzy import normalize_phrase
    assert normalize_phrase('  环保杂胶 ') == '环保杂胶'
    assert normalize_phrase('\t白中磅\n') == '白中磅'


def test_normalize_handles_empty():
    from blueprints.voice_fuzzy import normalize_phrase
    assert normalize_phrase('') == ''
    assert normalize_phrase('   ') == ''
