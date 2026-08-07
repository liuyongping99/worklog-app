"""口语归一化 + RapidFuzzy 匹配 + 规则切句降级。

口语短语归一化(phrase):strip + 留作产品匹配键。
RapidFuzzy 匹配:在 product 表 / level=4 类目名 fuzzy 搜索。
规则切句降级:DeepSeek 失败时按分隔符切句 + 正则提取数字单位。
"""
import re
from rapidfuzz import fuzz, process

from models._db import get_db

# ── 中文数字 → 阿拉伯 ──
_CN_NUM = {
    '零': 0, '〇': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4,
    '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10,
}
_CN_NUM_TENS = {'十': 10, '百': 100, '千': 1000}

# ── 单位集合(从 ocr_engine.py 同步)──
_VALID_UNITS = ('支', 'y', '码', 'kg', 'KG', '桶', '件', '箱',
                '张', '块', '卷', '令', '个', '只')

# 分隔符(中英文标点 + 空白 + 逻辑连词)
_SEPARATORS = ('、', ',', '，', '。', ';', '；', '和', '与', '加', '还有', ' ', '\t')

# 数字+单位正则(支持阿拉伯数字 + 简单中文数字)
_QTY_UNIT_RE = re.compile(
    r'(\d+(?:\.\d+)?|[一二两三四五六七八九十百]+)\s*'
    r'(支|y|码|kg|KG|桶|件|箱|张|块|卷|令|个|只)(?!\w)'
)


def normalize_phrase(s: str) -> str:
    return (s or '').strip()


def _cn_to_int(s: str) -> int | None:
    """简单中文数字转 int:支持 一/十/二十/五十/一百 等"""
    if not s:
        return None
    try:
        return int(s)
    except ValueError:
        pass
    # 处理「二十」/「十五」/「一百」 等
    if s == '十':
        return 10
    total = 0
    if s.startswith('十'):
        s = '一' + s  # 「十五」 → 「一十五」= 15
    for ch in s:
        if ch in _CN_NUM_TENS:
            # 简单处理:「二十」 → 2*10 + 0
            total = total * _CN_NUM_TENS[ch] if total else _CN_NUM_TENS[ch]
        elif ch in _CN_NUM:
            total += _CN_NUM[ch]
        else:
            return None
    return total if total > 0 else None


def fuzzy_match_category(phrase: str, top_n: int = 3) -> list[dict]:
    """在 level=4 叶子类目名 fuzzy 搜,返回 [{name, score}, ...]"""
    from blueprints.voice_llm import load_category_tree
    names = load_category_tree()
    if not names:
        return []
    results = process.extract(phrase, names, scorer=fuzz.WRatio,
                              limit=top_n, score_cutoff=0)
    return [{'name': r[0], 'score': r[1]} for r in results]


def fuzzy_match_product(phrase: str, top_n: int = 5, score_cutoff: int = 30) -> list[dict]:
    """在 product 表 fuzzy 搜,返回 [{product_id, product_name, specification, score}, ...]"""
    conn = get_db()
    cur = conn.cursor()
    cur.execute('SELECT product_id, product_name, specification FROM product '
                'WHERE product_name IS NOT NULL AND product_name != ""')
    rows = cur.fetchall()
    conn.close()
    if not rows:
        return []
    products = [dict(r) for r in rows]
    # 用 product_name + specification 拼一个完整字符串 fuzzy
    choices = {f"{p['product_name']}|{p.get('specification') or ''}": p for p in products}
    results = process.extract(phrase, list(choices.keys()),
                              scorer=fuzz.WRatio, limit=top_n,
                              score_cutoff=score_cutoff)
    return [
        {
            'product_id': choices[r[0]]['product_id'],
            'product_name': choices[r[0]]['product_name'],
            'specification': choices[r[0]].get('specification') or '',
            'score': r[1],
        }
        for r in results
    ]


def rule_based_segment(text: str) -> dict:
    """规则切句降级:按分隔符切 + 正则提取数字单位。

    返回格式:{customer: None, items: [{phrase_part, spec_part, quantity, unit}]}
    规则切句抽不出 customer,固定 None。
    """
    # 按分隔符切句
    parts = re.split(r'[、,，。;；和与加还有]', text)
    items = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        # 提取数量+单位
        m = _QTY_UNIT_RE.search(part)
        quantity = None
        unit = None
        if m:
            qty_str = m.group(1)
            unit = m.group(2).lower() if m.group(2).lower() == 'y' else m.group(2)
            if unit == '码':
                unit = 'y'  # 单位归一化
            quantity = _cn_to_int(qty_str) if not qty_str.replace('.', '').isdigit() else float(qty_str)
            quantity = int(quantity) if quantity == int(quantity) else quantity
            # 剩余部分作为 phrase_part(spec_part 留空,后续 fuzzy 处理)
            phrase_part = (part[:m.start()] + part[m.end():]).strip()
        else:
            phrase_part = part
        if phrase_part:
            items.append({
                'phrase_part': phrase_part,
                'spec_part': None,
                'quantity': quantity,
                'unit': unit,
            })
    return {'customer': None, 'items': items}
