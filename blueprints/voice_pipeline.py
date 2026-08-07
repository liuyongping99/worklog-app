"""Voice pipeline orchestrator。

recognize(audio_bytes):音频 → 候选 JSON。
confirm(order_id, items):用户确认候选 → 写映射表 + 批量插入明细行。
"""
import logging
from blueprints.voice_baidu import BaiduASR
from blueprints.voice_llm import extract_items, llm_fallback_select
from blueprints.voice_fuzzy import fuzzy_match_product
from models import VoiceMapping, ShippingRecord, ShippingOrder

logger = logging.getLogger(__name__)


def _transcribe_audio(audio_bytes: bytes, audio_format: str = 'wav') -> str:
    """包一层 BaiduASR.transcribe,便于测试 monkeypatch"""
    asr = BaiduASR()
    return asr.transcribe(audio_bytes, audio_format=audio_format)


def recognize(audio_bytes: bytes, audio_format: str = 'wav') -> dict:
    """完整 pipeline:音频 → 候选 JSON"""
    # 1) 百度识别
    text = _transcribe_audio(audio_bytes, audio_format)

    # 2) DeepSeek 切句(失败降级规则)
    extracted = extract_items(text)
    customer = extracted.get('customer')

    items_out = []
    for item in extracted.get('items', []):
        phrase_part = (item.get('phrase_part') or '').strip()
        spec_part = item.get('spec_part')
        quantity = item.get('quantity')
        unit = item.get('unit')

        candidates = []

        # 3) 口语映射表查询
        mappings = VoiceMapping.lookup_phrase(phrase_part) if phrase_part else []
        for m in mappings:
            candidates.append({
                'product_id': m['product_id'],
                'product_name': '',  # 前端不依赖 name,弹框自己查
                'specification': m.get('spec_hint') or '',
                'score': 100,  # mapping 直接命中,score=100
                'source': 'mapping',
            })

        # 4) RapidFuzzy product 表
        fuzzy_results = fuzzy_match_product(phrase_part, top_n=5) if phrase_part else []
        for r in fuzzy_results:
            # 避免重复:若 mapping 已命中同 product_id,跳过
            if any(c['product_id'] == r['product_id'] for c in candidates):
                continue
            candidates.append({
                'product_id': r['product_id'],
                'product_name': r['product_name'],
                'specification': r['specification'],
                'score': r['score'],
                'source': 'fuzzy',
            })

        # 5) LLM 兜底(无候选时)
        if not candidates and phrase_part:
            # fuzzy 已空,直接传空 list 给 LLM(LLM 可能基于语义猜)
            fallback_pid = llm_fallback_select(phrase_part, spec_part, fuzzy_results)
            if fallback_pid:
                candidates.append({
                    'product_id': fallback_pid,
                    'product_name': '',
                    'specification': '',
                    'score': 95,
                    'source': 'llm_fallback',
                })

        items_out.append({
            'phrase_part': phrase_part,
            'spec_part': spec_part,
            'quantity': quantity,
            'unit': unit,
            'candidates': candidates,
        })

    needs_disambiguation = any(len(it['candidates']) >= 2 for it in items_out)

    return {
        'success': True,
        'recognized_text': text,
        'customer': customer,
        'items': items_out,
        'needs_disambiguation': needs_disambiguation,
    }


def confirm(order_id: int, items: list[dict]) -> dict:
    """用户确认候选 → 写映射表 + 批量插入明细行。

    items 格式:[{phrase_part, product_id, specification, quantity, unit, source}]
    ShippingRecord.create 需要 date/customer(自动归一化逻辑),从订单查出来传入。
    """
    # 取订单的 date/customer(ShippingRecord.create 必填)
    from models._db import get_db
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT date, customer FROM shipping_orders WHERE id=?', (order_id,))
    row = cur.fetchone()
    conn.close()
    if not row:
        raise ValueError(f'订单 {order_id} 不存在')
    date_str, customer_str = row[0], row[1]

    inserted = []
    for item in items:
        # 1) 写映射表
        source = item.get('source') or 'user_confirmed'
        if source not in ('user_confirmed', 'llm_fallback'):
            source = 'user_confirmed'
        spec_hint = item.get('specification') or None
        try:
            VoiceMapping.upsert(
                phrase=item['phrase_part'],
                product_id=item['product_id'],
                spec_hint=spec_hint,
                source=source,
            )
        except Exception as e:
            logger.warning('写口语映射失败(不阻断): %s', e)

        # 2) 插入明细行(ShippingRecord.create 自动归一化 unit=码→y)
        rec_id = ShippingRecord.create(
            date=date_str,
            customer=customer_str,
            order_pk=order_id,
            product_name='',  # voice 不带 product_name,留空(可后续按 product_id 回填)
            specification=item.get('specification') or '',
            quantity=str(item.get('quantity') or 0),
            unit=item.get('unit') or '支',
            remark='',
        )
        inserted.append({'id': rec_id, 'product_name': '',
                         'specification': item.get('specification') or '',
                         'quantity': item.get('quantity') or 0,
                         'unit': item.get('unit') or '支'})

    return {
        'success': True,
        'order_id': order_id,
        'records': inserted,
    }
