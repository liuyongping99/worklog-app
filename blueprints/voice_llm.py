"""DeepSeek 切句 + LLM 兜底 + 类目树加载。

切句职责:字面切分 phrase_part / spec_part / quantity / unit。
兜底职责:给 Top 30 候选,选 1 个 product_id。

不打包口语映射表进切句 prompt(V1 简化,V2 看效果)。
"""
import os
import re
import json
import logging
from openai import OpenAI

from models._db import get_db

logger = logging.getLogger(__name__)

_client = None

# 标准单位集合
_VALID_UNITS = {'支', 'y', '码', 'kg', '桶', '件', '箱', '张', '块', '卷', '令', '个'}

# 类目缓存
_category_tree_cache = None


def _get_client():
    global _client
    if _client is None:
        api_key = os.environ.get('DEEPSEEK_API_KEY') or os.environ.get('OPENAI_API_KEY')
        if not api_key:
            raise RuntimeError('DEEPSEEK_API_KEY 未配置')
        base_url = os.environ.get('DEEPSEEK_BASE_URL', 'https://api.deepseek.com')
        _client = OpenAI(api_key=api_key, base_url=base_url)
    return _client


def _call_deepseek(messages: list[dict]) -> str:
    """调 DeepSeek Chat Completions,返回 content 文本"""
    client = _get_client()
    model = os.environ.get('DEEPSEEK_MODEL', 'deepseek-chat')
    resp = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.1,
        max_tokens=1000,
    )
    return resp.choices[0].message.content


def load_category_tree() -> list[str]:
    """从 product_categories 表加载 level=4 叶子类目名(缓存)"""
    global _category_tree_cache
    if _category_tree_cache is not None:
        return _category_tree_cache
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        'SELECT category_name FROM product_categories WHERE level=4 '
        'AND status=1 ORDER BY sort_order, id'
    )
    names = [row[0] for row in cur.fetchall()]
    conn.close()
    _category_tree_cache = names
    return names


def invalidate_category_cache():
    """产品类目变更时调用"""
    global _category_tree_cache
    _category_tree_cache = None


def build_category_tree_prompt() -> str:
    """构造类目树 prompt 片段(给 DeepSeek 作切句基准)"""
    names = load_category_tree()
    lines = '\n'.join(f'  - {n}' for n in names)
    return f'以下是本系统的商品分类树叶子类目(共 {len(names)} 条,作为切句基准):\n{lines}'


def _build_extract_prompt(recognized_text: str) -> list[dict]:
    """切句 prompt 拼装"""
    cat = build_category_tree_prompt()
    system = (
        '你是订单口语切句助手。把用户的口语拆成结构化 JSON。\n\n'
        f'{cat}\n\n'
        '规则:\n'
        '1. phrase_part 必须是上述类目的口语化简写(如「白中磅」对应类目「白磅布三文治」)\n'
        '2. spec_part 仅含规格数字/单位(如 "1.2"、"中性"、"黑色"),无则 null\n'
        '3. quantity 可为 null(用户只报品名未报数量)\n'
        '4. unit 可为 null,标准单位:支/y/码/kg/桶/件/箱/张/块/卷/令/个\n'
        '5. 中文数字转阿拉伯("二十" → 20,"五十" → 50)\n'
        '6. customer 字段填客户名(口语中提到的公司/人名),无则 null\n'
        '7. 严格输出 JSON,不要其他文字'
    )
    user = f'用户说:{recognized_text}\n\n输出 JSON,格式:{{"customer":"...","items":[{{"phrase_part":"...","spec_part":"...","quantity":N,"unit":"..."}},...]}}'
    return [
        {'role': 'system', 'content': system},
        {'role': 'user', 'content': user},
    ]


def _parse_extract_response(content: str) -> dict:
    """解析 DeepSeek 返回的 JSON,容错:可能包在 ```json ``` 里"""
    text = content.strip()
    if text.startswith('```'):
        text = re.sub(r'^```(?:json)?\s*', '', text)
        text = re.sub(r'\s*```$', '', text)
    data = json.loads(text)
    if isinstance(data, list):
        # 旧格式只返回数组,补 customer=null
        return {'customer': None, 'items': data}
    return {
        'customer': data.get('customer'),
        'items': data.get('items', []),
    }


def extract_items(recognized_text: str) -> dict:
    """DeepSeek 切句 → 结构化 {customer, items[]}。失败降级到规则切句。"""
    try:
        messages = _build_extract_prompt(recognized_text)
        content = _call_deepseek(messages)
        return _parse_extract_response(content)
    except Exception as e:
        logger.warning('DeepSeek 切句失败,降级到规则切句: %s', e)
        from blueprints.voice_fuzzy import rule_based_segment
        return rule_based_segment(recognized_text)


def _build_fallback_prompt(phrase_part: str, spec_part: str | None,
                            candidates: list[dict]) -> list[dict]:
    """LLM 兜底 prompt:给 Top 30 候选,选 1 个"""
    items_text = '\n'.join(
        f'  id={c["product_id"]} | {c["product_name"]} | 规格:{c.get("specification", "")}'
        for c in candidates
    )
    system = (
        '你是商品匹配助手。用户口语短语可能不标准,需要在候选清单里选最匹配的。\n'
        '严格输出 JSON:{"product_id": <int 或 null>}\n'
        '如果都不匹配,product_id 设为 null。'
    )
    user = (
        f'口语短语:{phrase_part}\n'
        f'规格数字:{spec_part or "(无)"}\n\n'
        f'候选商品清单:\n{items_text}\n\n'
        '选择最匹配的 product_id:'
    )
    return [
        {'role': 'system', 'content': system},
        {'role': 'user', 'content': user},
    ]


def llm_fallback_select(phrase_part: str, spec_part: str | None,
                          candidates: list[dict]) -> int | None:
    """LLM 兜底:从候选中选 1 个 product_id,失败返回 None"""
    if not candidates:
        return None
    try:
        messages = _build_fallback_prompt(phrase_part, spec_part, candidates)
        content = _call_deepseek(messages)
        text = content.strip()
        if text.startswith('```'):
            text = re.sub(r'^```(?:json)?\s*', '', text)
            text = re.sub(r'\s*```$', '', text)
        data = json.loads(text)
        pid = data.get('product_id')
        if pid is None:
            return None
        # 验证 pid 在候选中
        valid_ids = {c['product_id'] for c in candidates}
        return int(pid) if int(pid) in valid_ids else None
    except Exception as e:
        logger.warning('LLM 兜底失败: %s', e)
        return None
