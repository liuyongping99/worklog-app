"""Pipeline orchestrator 测试(monkeypatch baidu/llm/fuzzy)"""
import pytest
import sqlite3


@pytest.fixture(autouse=True)
def _ensure_test_products():
    """测试用 product 兜底 + 清 voice_phrase_mapping"""
    from models._db import DB_PATH
    try:
        conn = sqlite3.connect(DB_PATH, timeout=10)
        for pid in (100, 143, 169, 170):
            conn.execute(
                'INSERT OR IGNORE INTO product '
                '(product_id, product_code, product_name, category_id, '
                ' specification, base_unit, status, created_at, updated_at) '
                'VALUES (?, ?, ?, 1, "test", "支", 1, '
                'datetime("now","localtime"), datetime("now","localtime"))',
                (pid, f'TEST{pid:04d}', f'测试品{pid}')
            )
        conn.execute('DELETE FROM voice_phrase_mapping')
        conn.commit()
        conn.close()
    except sqlite3.OperationalError:
        pass
    yield


@pytest.fixture
def stub_all(monkeypatch):
    """stub 掉所有外部依赖,只测编排逻辑"""
    from blueprints import voice_pipeline

    # 1) stub BaiduASR.transcribe
    monkeypatch.setattr(
        voice_pipeline, '_transcribe_audio',
        lambda audio_bytes, audio_format='wav': '丰泽出货,白中磅1.2二十支,环保杂胶50个'
    )

    # 2) stub DeepSeek 切句
    monkeypatch.setattr(
        voice_pipeline, 'extract_items',
        lambda text: {
            'customer': '丰泽',
            'items': [
                {'phrase_part': '白中磅', 'spec_part': '1.2', 'quantity': 20, 'unit': '支'},
                {'phrase_part': '环保杂胶', 'spec_part': None, 'quantity': 50, 'unit': '个'},
            ]
        }
    )

    # 3) stub 口语映射表查询(空,fallback 到 fuzzy)
    from models import VoiceMapping
    monkeypatch.setattr(VoiceMapping, 'lookup_phrase', lambda phrase: [])

    # 4) stub fuzzy_match_product 返回受控候选
    def fake_fuzzy(phrase, top_n=5, score_cutoff=30):
        if '白中磅' in phrase:
            return [
                {'product_id': 169, 'product_name': '白磅布三文治',
                 'specification': '1.2中性', 'score': 88},
                {'product_id': 170, 'product_name': '黑磅布三文治',
                 'specification': '1.2黑色', 'score': 70},
            ]
        if '环保杂胶' in phrase:
            return [
                {'product_id': 143, 'product_name': '7P环保杂胶',
                 'specification': '黑色', 'score': 75},
            ]
        return []

    monkeypatch.setattr(voice_pipeline, 'fuzzy_match_product', fake_fuzzy)

    # 5) stub LLM 兜底(未触发)
    monkeypatch.setattr(
        voice_pipeline, 'llm_fallback_select',
        lambda *args, **kwargs: None
    )


def test_recognize_returns_full_json(stub_all):
    from blueprints.voice_pipeline import recognize
    result = recognize(b'fake_audio', audio_format='wav')
    assert result['success'] is True
    assert result['customer'] == '丰泽'
    assert result['recognized_text'] == '丰泽出货,白中磅1.2二十支,环保杂胶50个'
    assert len(result['items']) == 2
    # 第一个 item:白中磅,有 2 个候选
    item1 = result['items'][0]
    assert item1['phrase_part'] == '白中磅'
    assert item1['quantity'] == 20
    assert len(item1['candidates']) >= 1
    # fuzzy 命中 88 分
    top = item1['candidates'][0]
    assert top['product_id'] == 169
    assert top['source'] == 'fuzzy'


def test_recognize_sets_needs_disambiguation_true(stub_all):
    """有 ≥2 候选 → needs_disambiguation=True"""
    from blueprints.voice_pipeline import recognize
    result = recognize(b'fake_audio')
    assert result['needs_disambiguation'] is True


def test_recognize_mapping_hit_takes_priority(stub_all, monkeypatch):
    """口语映射表命中 → candidates 第一个 source=mapping, score=100"""
    from blueprints import voice_pipeline
    from models import VoiceMapping

    monkeypatch.setattr(
        VoiceMapping, 'lookup_phrase',
        lambda phrase: [{
            'id': 1, 'phrase': '白中磅', 'product_id': 169,
            'spec_hint': '中性', 'source': 'user_confirmed',
            'use_count': 5, 'status': 'active',
            'created_at': '', 'last_used_at': ''
        }] if '白中磅' in phrase else []
    )

    result = voice_pipeline.recognize(b'fake_audio')
    item1 = result['items'][0]
    # 第一个候选应该是 mapping 命中
    assert item1['candidates'][0]['source'] == 'mapping'
    assert item1['candidates'][0]['score'] == 100
    assert item1['candidates'][0]['product_id'] == 169


def test_recognize_triggers_llm_fallback_when_fuzzy_empty(stub_all, monkeypatch):
    """fuzzy 空 → 走 LLM 兜底"""
    from blueprints import voice_pipeline

    monkeypatch.setattr(
        voice_pipeline, 'fuzzy_match_product',
        lambda phrase, top_n=5, score_cutoff=30: []
    )
    monkeypatch.setattr(
        voice_pipeline, 'llm_fallback_select',
        lambda phrase_part, spec_part, candidates: 142  # 假装 LLM 选了 id=142
    )

    result = voice_pipeline.recognize(b'fake_audio')
    item1 = result['items'][0]
    assert item1['candidates'][0]['source'] == 'llm_fallback'
    assert item1['candidates'][0]['product_id'] == 142


def test_confirm_writes_mapping_and_inserts_records(stub_all):
    """confirm:写映射表 + 批量插入明细行"""
    from blueprints.voice_pipeline import confirm

    # 先创建一个出货订单(用模型 API)
    from models import ShippingOrder
    order_id = ShippingOrder.create(date='2026-08-06', customer='丰泽')

    items = [
        {'phrase_part': '白中磅', 'product_id': 169, 'specification': '1.2中性',
         'quantity': 20, 'unit': '支', 'source': 'fuzzy'},
        {'phrase_part': '环保杂胶', 'product_id': 143, 'specification': '黑色',
         'quantity': 50, 'unit': '个', 'source': 'fuzzy'},
    ]
    result = confirm(order_id=order_id, items=items)

    assert result['success'] is True
    assert len(result['records']) == 2
    # 用直接 SQL 验证(stub_all 把 VoiceMapping.lookup_phrase stub 成 [],不能用它)
    from models._db import get_db
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM voice_phrase_mapping "
                "WHERE phrase IN ('白中磅', '环保杂胶')")
    count = cur.fetchone()[0]
    conn.close()
    assert count >= 2


def test_confirm_use_count_threshold_activates(stub_all):
    """confirm 第二次 → use_count >= 2 → status 变 active"""
    from blueprints.voice_pipeline import confirm
    from models import ShippingOrder

    # 第一次 confirm
    o1 = ShippingOrder.create(date='2026-08-06', customer='丰泽1')
    confirm(order_id=o1, items=[
        {'phrase_part': '白中磅', 'product_id': 169, 'specification': '1.2中性',
         'quantity': 10, 'unit': '支', 'source': 'fuzzy'}
    ])
    # 第二次 confirm(同 phrase + product)
    o2 = ShippingOrder.create(date='2026-08-06', customer='丰泽2')
    confirm(order_id=o2, items=[
        {'phrase_part': '白中磅', 'product_id': 169, 'specification': '1.2中性',
         'quantity': 20, 'unit': '支', 'source': 'fuzzy'}
    ])

    # 用直接 SQL 验证(绕过 stub_all 的 stub)
    from models._db import get_db
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT use_count, status FROM voice_phrase_mapping "
                "WHERE phrase='白中磅' AND product_id=169")
    row = cur.fetchone()
    conn.close()
    assert row is not None
    use_count, status = row[0], row[1]
    assert use_count == 2
    assert status == 'active'
