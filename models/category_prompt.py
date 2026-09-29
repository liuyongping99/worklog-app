"""类别/规格自适应提示词(compare-rows 比对 layer2/layer3)。

设计意图:
  - 用户在出货页对 AI 比对结果(黄/红)点"✓ 确认通过"
  - 之后图下方多出"💡 生成提示词"按钮 → 调接口 /generate-prompt-suggestion
    返回一段模板化优化提示词(基于 OCR 原文 + AI/人裁决快照)
  - 用户编辑后保存为 (scope, category_code, spec_pattern, prompt_text)
  - 下次同 record 比对时,compare_rows() 在 base COMPARE_PROMPT 末尾拼装生效层
    2 大类提示词 + 生效层 3 规格通配符提示词

scope 两种取值:
  - 'category': 只按品类(classify(record) 命中 product_name_keyword)生效;category_code + keyword
  - 'spec':     品类 + 规格通配(用 SQL LIKE % _)同时命中才生效

is_active='active' 才参与拼装;'archived' 表示已被人工废弃。

2026-08-09 改造:兜底分类改为从 product_categories 表 level=3 派生 keyword,
                  不再依赖硬编码 FALLBACK_CATEGORY_KEYWORDS。优点:
                  - 自动跟随 DB 真值,不再有"猪皮纹→0204"这类漂移错配
                  - 加新 Level 3 品类时,自动获得 keyword 能力,无需改源码
"""
import re
from ._db import get_db


def _extract_tokens(name: str) -> list:
    """从 category_name 提取用于匹配的 keyword tokens。

    切分规则:
      - 去掉括号内容(将括号内文字也作为独立 token)
      - 在 ASCII ↔ CJK 边界插入空格,作为词边界
      - 取 CJK 连续 ≥ 2 字、ASCII alphanum 连续 ≥ 2 字符

    示例:
      '猪皮纹HA'      → ['猪皮纹', 'HA']
      '7PPVC人造革'   → ['7PPVC', '人造革']     (单次切分,'7PPVC' 视为整体)
      '高发泡（轻胶）'  → ['高发泡', '轻胶']
      'A级杂胶'       → ['级', '杂胶']           (单字 A 丢弃)
      'PE板'          → ['PE']                    (单字板 丢弃)
      'PVC胶片'       → ['PVC', '胶片']
      'LB鱼鳞布特软'  → ['LB', '鱼鳞布特软']
      'TA 特软'       → ['TA', '特软']
      '七B水'         → []                        (单字七/B 都不够长,丢弃)
    """
    if not name:
        return []
    # 1. 括号替换为空格(保留内文作独立 token)
    cleaned = re.sub(r'[()（）\[\]【】]', ' ', name)
    # 2. 在 ASCII ↔ CJK 边界插空格
    spaced = re.sub(r'([A-Za-z0-9])([一-鿿])', r'\1 \2', cleaned)
    spaced = re.sub(r'([一-鿿])([A-Za-z0-9])', r'\1 \2', spaced)
    # 3. 切分 + 长度过滤
    tokens = []
    for part in spaced.split():
        if not part:
            continue
        if re.match(r'^[一-鿿]+$', part) and len(part) >= 2:
            tokens.append(part)
        elif re.match(r'^[A-Za-z0-9]+$', part) and len(part) >= 2:
            tokens.append(part)
    return tokens


# ── DB 派生 keyword 缓存 ──
# Level=3 品类数量 ~87,推导一次 < 10ms,加 cache 避免每次 classify_record 都查 DB
_LEVEL3_KEYWORDS_CACHE: list | None = None


def _load_level3_keywords() -> list:
    """从 product_categories level=3 派生 [(category_code, category_name, [tokens])]。

    Returns:
        list of dict: [{category_code, category_name, tokens}, ...]
        按 category_code 升序,过滤掉 tokens 为空的(无法匹配的品类)。
    """
    global _LEVEL3_KEYWORDS_CACHE
    if _LEVEL3_KEYWORDS_CACHE is not None:
        return _LEVEL3_KEYWORDS_CACHE
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """SELECT category_code, category_name
           FROM product_categories
           WHERE level = 3 AND status = 1
           ORDER BY category_code"""
    )
    rows = cursor.fetchall()
    conn.close()
    cache = []
    for r in rows:
        tokens = _extract_tokens(r['category_name'] or '')
        if tokens:
            cache.append({
                'category_code': r['category_code'],
                'category_name': r['category_name'] or '',
                'tokens': tokens,
            })
    _LEVEL3_KEYWORDS_CACHE = cache
    return cache


def _clear_level3_cache():
    """手动失效缓存。用于 admin 加/改分类树后立刻生效。
    2026-08-09:同时失效祖先链与 level>=3 兜底缓存,保证 classify 即时准确。"""
    global _LEVEL3_KEYWORDS_CACHE, _ANCESTOR_CACHE, _ALL_CATEGORY_KEYWORDS_CACHE
    _LEVEL3_KEYWORDS_CACHE = None
    _ANCESTOR_CACHE = None
    _ALL_CATEGORY_KEYWORDS_CACHE = None


# ── 2026-08-09 修复:祖先链路 + level>=3 最深匹配兜底 ──
_ANCESTOR_CACHE: dict | None = None
_ALL_CATEGORY_KEYWORDS_CACHE: list | None = None


def _build_ancestor_map() -> dict:
    """预计算每个 category_code 的祖先链(含自身,从自身向根)。parent_id 为节点 id。"""
    global _ANCESTOR_CACHE
    if _ANCESTOR_CACHE is not None:
        return _ANCESTOR_CACHE
    conn = get_db()
    cursor = conn.cursor()
    rows = cursor.execute(
        "SELECT id, category_code, parent_id FROM product_categories WHERE status=1").fetchall()
    conn.close()
    nodes = {r['id']: {'code': r['category_code'], 'pid': r['parent_id']} for r in rows}
    code_to_id = {n['code']: i for i, n in nodes.items()}
    cache = {}
    for code in code_to_id:
        cur = code_to_id.get(code)
        seen = []
        guard = 0
        while cur is not None and cur in nodes and guard < 30:
            seen.append(nodes[cur]['code'])
            cur = nodes[cur]['pid']
            guard += 1
        cache[code] = seen
    _ANCESTOR_CACHE = cache
    return cache


def _ancestor_codes(leaf_code: str) -> list:
    """返回 leaf_code 的所有祖先 category_code(含自身),从自身到根。无则 [leaf_code]。"""
    if not leaf_code:
        return []
    return _build_ancestor_map().get(leaf_code, [leaf_code])


def _load_all_category_keywords() -> list:
    """从 product_categories level>=3 派生 [(category_code, category_name, level, tokens)]。
    用于 classify_record 兜底:优先匹配最深(最具体)的品类节点。
    """
    global _ALL_CATEGORY_KEYWORDS_CACHE
    if _ALL_CATEGORY_KEYWORDS_CACHE is not None:
        return _ALL_CATEGORY_KEYWORDS_CACHE
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """SELECT category_code, category_name, level
           FROM product_categories WHERE level >= 3 AND status = 1 ORDER BY category_code""")
    rows = cursor.fetchall()
    conn.close()
    cache = []
    for r in rows:
        tokens = _extract_tokens(r['category_name'] or '')
        if tokens:
            cache.append({
                'category_code': r['category_code'],
                'category_name': r['category_name'] or '',
                'level': r['level'],
                'tokens': tokens,
            })
    _ALL_CATEGORY_KEYWORDS_CACHE = cache
    return cache


def _clear_category_prompt_caches():
    """admin 改分类树后失效所有相关缓存。"""
    global _LEVEL3_KEYWORDS_CACHE, _ANCESTOR_CACHE, _ALL_CATEGORY_KEYWORDS_CACHE
    _LEVEL3_KEYWORDS_CACHE = None
    _ANCESTOR_CACHE = None
    _ALL_CATEGORY_KEYWORDS_CACHE = None


def classify_record(product_name: str = '', specification: str = '') -> dict | None:
    """根据商品名称+规格查 category_code(L3,如 0204 / 0701)。

    Returns:
        {'category_code', 'category_name', 'matched_via'}
        matched_via ∈ {'product_table', 'level3_keywords'}
        查不到返回 None。

    主路径(2026-08-09 改造前):
        product 表 JOIN product_categories(精确匹配)
    兜底(2026-08-09 改造):
        从 product_categories level=3 的 category_name 自动切 token 匹配
        - 命中规则:该品类的所有 token 中,有多少出现在 product_name 中
        - 取 score 最高者;平局时取命中 token 总字符长度更长者;再平局取 code 升序

    历史硬编码版本(FALLBACK_CATEGORY_KEYWORDS)于 2026-08-09 删除。
    """
    conn = get_db()
    cursor = conn.cursor()

    # 1) product 表精确 JOIN
    cursor.execute(
        """SELECT pc.category_code, pc.category_name
           FROM product p LEFT JOIN product_categories pc ON p.category_id = pc.id
           WHERE p.product_name = ?
           LIMIT 1""",
        (product_name or '',)
    )
    row = cursor.fetchone()
    conn.close()
    if row:
        return {'category_code': row['category_code'],
                'category_name': row['category_name'] or '',
                'matched_via': 'product_table'}

    # 2) DB 派生兜底:从 level>=3 category_name 自动切 token 匹配,优先最深(最具体)节点
    pn = product_name or ''
    if not pn:
        return None
    pairs = _load_all_category_keywords()
    scored = []
    for p in pairs:
        matches = [t for t in p['tokens'] if t in pn]
        if matches:
            scored.append({
                'category_code': p['category_code'],
                'category_name': p['category_name'],
                'level': p['level'],
                'match_score': len(matches),
                'match_total_len': sum(len(m) for m in matches),
            })
    if not scored:
        return None
    # 优先 deepest level(最具体);平局按命中 token 数、命中总长度、code 升序
    scored.sort(key=lambda x: (-x['level'], -x['match_score'], -x['match_total_len'], x['category_code']))
    winner = scored[0]
    return {
        'category_code': winner['category_code'],
        'category_name': winner['category_name'],
        'matched_via': 'category_keywords',
    }


class CategoryPrompt:
    """类别/规格自适应提示词 CRUD + 拼装用的查询。"""

    @staticmethod
    def create(*, scope: str, prompt_text: str,
               category_code: str = None,
               spec_pattern: str = None,
               product_name_keyword: str = None,
               source_event_id: int = None,
               source_ocr_text: str = None,
               source_ai_status: str = None,
               source_human_status: str = None,
               status: str = 'active') -> int:
        """插入一条提示词。返回新 id。

        入参约束:
          scope='category': 必须有 product_name_keyword 至少一项(category_code 也可同时给)
          scope='spec':     必须有 category_code AND spec_pattern
        """
        from datetime import datetime
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            '''INSERT INTO category_prompts
               (scope, category_code, spec_pattern, product_name_keyword,
                prompt_text, source_event_id, source_ocr_text,
                source_ai_status, source_human_status,
                status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (scope, category_code, spec_pattern, product_name_keyword,
             prompt_text, source_event_id, source_ocr_text,
             source_ai_status, source_human_status,
             status, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        )
        conn.commit()
        new_id = cursor.lastrowid
        conn.close()
        return new_id

    @staticmethod
    def get_by_id(prompt_id: int) -> dict | None:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM category_prompts WHERE id=?', (prompt_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    @staticmethod
    def archive(prompt_id: int) -> bool:
        """软删:把 status 设为 'archived',拼装时不再生效。"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE category_prompts SET status='archived' WHERE id=?",
            (prompt_id,)
        )
        conn.commit()
        n = cursor.rowcount
        conn.close()
        return n > 0

    @staticmethod
    def list_active(*, scope: str = None, limit: int = 200) -> list:
        """列出当前活跃的提示词,可选按 scope 过滤。给前端管理页用。"""
        conn = get_db()
        cursor = conn.cursor()
        if scope:
            cursor.execute(
                "SELECT * FROM category_prompts WHERE status='active' AND scope=? "
                "ORDER BY created_at DESC LIMIT ?",
                (scope, limit)
            )
        else:
            cursor.execute(
                "SELECT * FROM category_prompts WHERE status='active' "
                "ORDER BY created_at DESC LIMIT ?",
                (limit,)
            )
        rows = [dict(r) for r in cursor.fetchall()]
        conn.close()
        return rows

    @staticmethod
    def compose_for_record(*, category_code: str = None, product_name: str = '',
                            specification: str = '') -> str:
        """该 record 比对时要追加的 layer2 + layer3 提示词拼装结果。

        拼装格式:
          ## 大类补充提示词
          - (category 提示词 1, 若多条以编号列出)
          - (category 提示词 2)
          ## 具体规格补充提示词
          - (spec 提示词 1)
          - (spec 提示词 2)

        返回空串 = 没有任何补充提示词(拼装层空着不影响)。
        """
        # 复用 list_for_record 同样的查询逻辑,但走单 SQL 一次获得所有结果
        result = CategoryPrompt.list_for_record(
            category_code=category_code,
            product_name=product_name,
            specification=specification
        )
        parts = []
        cat_prompts = result.get('category_prompts') or []
        spec_prompts = result.get('spec_prompts') or []
        if cat_prompts:
            lines = ['## 大类补充提示词(基于此前人工确认的同品类案例)']
            for p in cat_prompts:
                txt = (p.get('prompt_text') or '').strip()
                if txt:
                    lines.append(f"- {txt}")
            parts.append('\n'.join(lines))
        if spec_prompts:
            lines = ['## 具体规格补充提示词(基于此前人工确认的同规格案例)']
            for p in spec_prompts:
                txt = (p.get('prompt_text') or '').strip()
                if txt:
                    lines.append(f"- {txt}")
            parts.append('\n'.join(lines))
        return '\n\n'.join(parts)

    @staticmethod
    def list_for_record(*, category_code: str = None, product_name: str = None,
                        specification: str = None) -> dict:
        """取该 record 比对时要拼装的所有 layer2/layer3 提示词。

        Returns:
          {
            'category_prompts': [...],   # scope='category' + 命中 keyword
            'spec_prompts':     [...],   # scope='spec' + 品类 + spec LIKE 命中
          }
        同类/同规格可能多条(用户可能积累多条对同一品类的不同侧面备注,全部返回)
        """
        result = {'category_prompts': [], 'spec_prompts': []}
        if not category_code and not product_name:
            return result
        # 祖先链(含自身):提示词挂在任意祖先层级都对当前商品行生效(2026-08-09 修复)
        anc = set(_ancestor_codes(category_code)) if category_code else set()
        conn = get_db()
        cursor = conn.cursor()

        # ── Layer 2: 类别级 (scope='category') ──
        # 命中条件:status='active' AND (category_code 命中祖先链 OR product_name_keyword 命中品名)
        cursor.execute(
            """SELECT * FROM category_prompts
               WHERE status='active' AND scope='category'""",
        )
        for r in cursor.fetchall():
            row = dict(r)
            hit = False
            if category_code and row.get('category_code') and row['category_code'] in anc:
                hit = True
            elif (product_name_keyword := row.get('product_name_keyword')):
                if product_name and product_name_keyword in product_name:
                    hit = True
            if hit:
                result['category_prompts'].append(row)

        # ── Layer 3: 规格级 (scope='spec',category_code 命中祖先链或为空,且 LIKE spec_pattern) ──
        if category_code and specification:
            cursor.execute(
                """SELECT * FROM category_prompts
                   WHERE status='active' AND scope='spec'
                     AND spec_pattern IS NOT NULL
                     AND ? LIKE spec_pattern""",
                (specification,)
            )
            for r in cursor.fetchall():
                row = dict(r)
                cc = row.get('category_code')
                if cc is None or cc in anc:
                    result['spec_prompts'].append(row)

        conn.close()
        return result

    # ─────────────────────────────────────────────────────────────
    # 启动 seed + 管理页查询(2026-08-08 新增)
    # 设计:不依赖自动分类器精度,而是把所有 Level 3 品类先占位为空白
    #      提示词,让前端管理页手动微调。
    # ─────────────────────────────────────────────────────────────

    @staticmethod
    def seed_blank_for_level(level: int = 3) -> int:
        """启动时调用:为所有 Level N 商品分类各 seed 一条空白提示词。

        幂等:对每个 category_code,先查 (scope='category' AND status='active')
              是否已存在该 code 的行。存在则跳过(不论 prompt_text 是空还是
              用户已填——只要 active 就不重复)。

        Returns: 实际新插入的行数。

        已知行为:用户归档占位行后,下次启动又会重新 seed 一条新的占位
        —— 这是有意的,避免引入"seed 自带 archived"状态机。如果用户
        想彻底关闭某个 category 的提示词,请用 update_text(pid, '') 留空
        而不是 archive。
        """
        from datetime import datetime
        conn = get_db()
        cursor = conn.cursor()

        # 1. 取所有 Level N + status=1 的 category_codes
        cursor.execute(
            "SELECT category_code, category_name FROM product_categories "
            "WHERE level = ? AND status = 1 ORDER BY category_code",
            (level,)
        )
        cats = cursor.fetchall()

        if not cats:
            conn.close()
            return 0

        # 2. 一次性查已存在的 active 行(任何 active 都算"已覆盖",跳过)
        cursor.execute(
            """SELECT category_code FROM category_prompts
               WHERE scope='category' AND status='active'"""
        )
        existing = {r['category_code'] for r in cursor.fetchall()}

        inserted = 0
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        for cat in cats:
            code = cat['category_code']
            if not code or code in existing:
                continue
            cursor.execute(
                '''INSERT INTO category_prompts
                   (scope, category_code, prompt_text, status, created_at)
                   VALUES ('category', ?, '', 'active', ?)''',
                (code, now)
            )
            inserted += 1

        conn.commit()
        conn.close()
        return inserted

    @staticmethod
    def seed_management_categories() -> int:
        """启动时调用:为管理页要列出的所有分类节点(level>=3,含 L3/L4/更深)
        各 seed 一条空白提示词。幂等,逻辑同 seed_blank_for_level,但覆盖
        (level>=3 全部分类节点) 这一集合(2026-08-09 调整)。

        "实际商品"指 product 表的 SKU 行;分类节点即便挂着商品也照常纳入,
        故不再做"L4 无商品"过滤。

        Returns: 实际新插入的行数。
        """
        from datetime import datetime
        conn = get_db()
        cursor = conn.cursor()

        cursor.execute(
            """SELECT category_code, category_name FROM product_categories
               WHERE status = 1 AND level >= 3
               ORDER BY category_code"""
        )
        cats = cursor.fetchall()
        if not cats:
            conn.close()
            return 0

        cursor.execute(
            """SELECT category_code FROM category_prompts
               WHERE scope='category' AND status='active'"""
        )
        existing = {r['category_code'] for r in cursor.fetchall()}

        inserted = 0
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        for cat in cats:
            code = cat['category_code']
            if not code or code in existing:
                continue
            cursor.execute(
                '''INSERT INTO category_prompts
                   (scope, category_code, prompt_text, status, created_at)
                   VALUES ('category', ?, '', 'active', ?)''',
                (code, now)
            )
            inserted += 1

        conn.commit()
        conn.close()
        return inserted

    @staticmethod
    def update_text(prompt_id: int, prompt_text: str) -> bool:
        """仅更新 prompt_text;允许空串(管理页"清空"按钮)。

        与 CategoryPrompt.create() 的非空约束不同:管理页主动编辑流程
        需要"清空"语义(create 端点仍校验非空,生成式流程不应存空白)。

        Returns: 是否更新到一行。
        """
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE category_prompts SET prompt_text = ? WHERE id = ?",
            (prompt_text or '', prompt_id)
        )
        conn.commit()
        n = cursor.rowcount
        conn.close()
        return n > 0

    @staticmethod
    def list_active_by_category(*, scope: str | None = 'category',
                                category_code: str | None = None,
                                include_empty: bool = True,
                                limit: int = 500) -> list:
        """管理页主查询:按 category_code 过滤活跃提示词。

        Args:
            scope: 'category' | 'spec' | None(都返回)
            category_code: 精确过滤 category_code 列;None = 不过滤
            include_empty: True = 含 prompt_text='' 占位行;False = 只要已填
            limit: SQL LIMIT
        """
        clauses = ["status = 'active'"]
        params: list = []
        if scope:
            clauses.append("scope = ?")
            params.append(scope)
        if category_code:
            clauses.append("category_code = ?")
            params.append(category_code)
        if not include_empty:
            clauses.append("prompt_text != ''")
        where = ' AND '.join(clauses)
        sql = (f"SELECT * FROM category_prompts WHERE {where} "
               f"ORDER BY category_code, id DESC LIMIT ?")
        params.append(limit)
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(sql, params)
        rows = [dict(r) for r in cursor.fetchall()]
        conn.close()
        return rows

    # ── 2026-09-22 「🤖 智能文本」补充提示词(全局注入)──────────────────
    # 用户在「🤖 智能文本」tab 累积的口语→结构化 业务约束,挂在 category_prompts
    # 表 scope=NEW ('text_recognize') 下,所有激活提示词每次解析都注入。
    # 跟 category/spec scope 的差别是 text_recognize 没有品类绑定 — 它是「全局业务语义」,
    # 比如「环保≠7P」「白软≠中软」「白杂胶 = 杂胶 0.6白软加面」等。
    #
    # 设计取舍:
    #   - 复用同表而非新建表,避免 schema 膨胀 + 跟现有「分类提示词」管理页生态保持
    #   - 不分关键词触发:绝大多数 text_recognize 提示词是横向业务约束,按关键词触发
    #     会让维护成本翻倍(text_recognize 提示词数预期 < 30 条,全量注入可控)
    #   - 软删(status='archived'):误加的可禁用,不直接 delete 留作 audit
    #
    # 数据形状:
    #   scope='text_recognize', category_code='', spec_pattern='',
    #   product_name_keyword='', prompt_text='...', source_ocr_text='' (可选,本条目作"现场补充")
    @staticmethod
    def list_active_text_recognize_supplements(limit: int = 50) -> list:
        """返回所有 text_recognize scope 的活跃补充提示词,按 id ASC(注入顺序稳定)。"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM category_prompts "
            "WHERE status = 'active' AND scope = 'text_recognize' "
            "AND prompt_text != '' "
            "ORDER BY id ASC LIMIT ?",
            (limit,)
        )
        rows = [dict(r) for r in cursor.fetchall()]
        conn.close()
        return rows


def list_management_categories() -> list:
    """返回管理页要列出的分类节点快照(供 /manage/category-prompts 渲染)。

    规则(2026-08-09 明确):纳入所有 level>=3 AND status=1 的商品分类节点,
    包括 Level 3 / Level 4 / 更深层。

    判定口径:"实际商品"指 product 表中的 SKU 行(带规格的那一条条记录),
    而不是 product_categories 里的分类节点。因此**只要一个节点是分类节点
    (哪怕它下面直接挂了商品),就纳入本页**——例如「7PPVC人造革」(L3) 及其
    下层「7P环保杂胶」等(L4,虽挂着 20+ 条 SKU)都属于"商品分类",都展示。

    不纳入的是 level<=2 的大类(如"纸品类"/"塑料类"),它们粒度太粗,不在此
    维护逐品类提示词。
    """
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute(
        """SELECT id, parent_id, category_code, category_name, level, sort_order
           FROM product_categories
           WHERE status = 1 AND level >= 3
           ORDER BY category_code"""
    )
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows
