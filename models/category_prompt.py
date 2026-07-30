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
"""
from ._db import get_db


# ── 兜底关键词(防止 product 表查不到品类时返回 None,小项目常用品名覆盖)─
# 关键词顺序很重要:先试长子串(避免"纯胶"先匹配到"环保纯胶"的下游 case)
FALLBACK_CATEGORY_KEYWORDS = [
    ('0204', ['7P环保HA猪皮纹']),   # 021008 简写:7P环保HA猪皮纹
    ('0204', ['HA猪皮纹', '猪皮纹']),
    ('0204', ['7P环保LB鱼鳞布', 'LB鱼鳞布', '鱼鳞布']),
    ('0201', ['7P环保杂胶', '环保杂胶', '杂胶']),
    ('0208', ['7P环保磅布三文治', '磅布']),
    ('0301', ['7P环保纯胶', '环保纯胶', '纯胶']),
    ('0205', ['7P环保三文治', '环保三文治', '三文治']),
    ('0206', ['路华里', '环保路华里']),
    ('0210', ['无纺布', 'A料', 'B料']),
    ('0209', ['回力胶', 'EVA']),
]


def classify_record(product_name: str = '', specification: str = '') -> dict | None:
    """根据商品名称+规格查 category_code(2 级品类,如 0204 / 0201 之类)。

    Returns:
        {'category_code': '0204', 'category_name': 'HA猪皮纹', 'matched_via': 'product_table|fallback'}
        或 None(查不到时)。

    主路径:product 表 JOIN product_categories(覆盖 7P环保HA猪皮纹/纯胶/杂胶 等主力品名)。
    兜底:关键词最长优先匹配(覆盖 HA猪皮纹特软/牛津布底纯胶 等非 product 表里的别名)。
    """
    conn = get_db()
    cursor = conn.cursor()

    # 1) product 表 JOIN
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
    # 2) 关键词兜底(FALLBACK_CATEGORY_KEYWORDS 已按长词优先,这里命中即返回)
    pn = product_name or ''
    for code, keywords in FALLBACK_CATEGORY_KEYWORDS:
        for kw in keywords:
            if kw in pn:
                return {'category_code': code, 'category_name': kw, 'matched_via': 'fallback'}
    return None


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
                lines.append(f"- [{p.get('source_ai_status','?')}→{p.get('source_human_status','?')}] {p['prompt_text']}")
            parts.append('\n'.join(lines))
        if spec_prompts:
            lines = ['## 具体规格补充提示词(基于此前人工确认的同规格案例)']
            for p in spec_prompts:
                lines.append(f"- [{p.get('source_ai_status','?')}→{p.get('source_human_status','?')}] {p['prompt_text']}")
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
        conn = get_db()
        cursor = conn.cursor()

        # ── Layer 2: 类别级 (scope='category') ──
        # 命中条件:status='active' AND (category_code 匹配 OR product_name_keyword 命中品名)
        cursor.execute(
            """SELECT * FROM category_prompts
               WHERE status='active' AND scope='category'""",
        )
        for r in cursor.fetchall():
            row = dict(r)
            hit = False
            if category_code and row.get('category_code') and row['category_code'] == category_code:
                hit = True
            elif (product_name_keyword := row.get('product_name_keyword')):
                if product_name and product_name_keyword in product_name:
                    hit = True
            if hit:
                result['category_prompts'].append(row)

        # ── Layer 3: 规格级 (scope='spec',同时匹配 category_code 和 LIKE spec_pattern) ──
        if category_code and specification:
            cursor.execute(
                """SELECT * FROM category_prompts
                   WHERE status='active' AND scope='spec'
                     AND (category_code = ? OR category_code IS NULL)
                     AND spec_pattern IS NOT NULL
                     AND ? LIKE spec_pattern""",
                (category_code, specification)
            )
            for r in cursor.fetchall():
                result['spec_prompts'].append(dict(r))

        conn.close()
        return result
