"""口语短语 → 商品 映射表

自动维护:用户每次在语音弹框确认候选时 upsert,频次门控 use_count>=2 自动 active。
"""
from datetime import datetime
from ._db import get_db


def _now() -> str:
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def _normalize_phrase(phrase: str) -> str:
    return phrase.strip()


class VoiceMapping:
    """口语短语到商品的映射。"""

    @staticmethod
    def upsert(phrase: str, product_id: int, spec_hint: str = None,
               source: str = 'user_confirmed') -> dict:
        """插入或更新映射。

        - 归一化 phrase(strip)
        - 已存在 (phrase, product_id) → use_count + 1, last_used_at = now
        - 不存在 → 插入, use_count=1, status='pending'
        - use_count 达到 2 → status='active'(频次门控)
        - 返回最新记录的 dict
        """
        phrase = _normalize_phrase(phrase)
        if not phrase:
            raise ValueError('phrase 不能为空')
        conn = get_db()
        cur = conn.cursor()
        now = _now()
        # 查现有
        cur.execute(
            'SELECT id, use_count, status FROM voice_phrase_mapping '
            'WHERE phrase=? AND product_id=?',
            (phrase, product_id)
        )
        row = cur.fetchone()
        if row:
            new_count = row['use_count'] + 1
            new_status = 'active' if new_count >= 2 else row['status']
            cur.execute(
                'UPDATE voice_phrase_mapping SET use_count=?, status=?, '
                'last_used_at=?, source=? WHERE id=?',
                (new_count, new_status, now, source, row['id'])
            )
            mapping_id = row['id']
        else:
            cur.execute(
                'INSERT INTO voice_phrase_mapping '
                '(phrase, product_id, spec_hint, source, use_count, status, '
                'created_at, last_used_at) '
                'VALUES (?, ?, ?, ?, 1, ?, ?, ?)',
                (phrase, product_id, spec_hint, source, 'pending', now, now)
            )
            mapping_id = cur.lastrowid
        conn.commit()
        conn.close()
        return VoiceMapping.get_by_id(mapping_id)

    @staticmethod
    def get_by_id(mapping_id: int) -> dict | None:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            'SELECT * FROM voice_phrase_mapping WHERE id=?',
            (mapping_id,)
        )
        row = cur.fetchone()
        conn.close()
        return dict(row) if row else None

    @staticmethod
    def lookup_phrase(phrase: str) -> list[dict]:
        """按 phrase 查所有映射,active 优先返回,pending 兜底"""
        phrase = _normalize_phrase(phrase)
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            'SELECT * FROM voice_phrase_mapping WHERE phrase=? '
            'ORDER BY (status=\'active\') DESC, use_count DESC, last_used_at DESC',
            (phrase,)
        )
        rows = cur.fetchall()
        conn.close()
        return [dict(r) for r in rows]

    @staticmethod
    def list_all(search: str = None, limit: int = 200) -> list[dict]:
        """管理页用:列出所有映射,可选按 phrase/product_id 搜索"""
        conn = get_db()
        cur = conn.cursor()
        if search:
            like = f'%{search.strip()}%'
            cur.execute(
                'SELECT m.*, p.product_name FROM voice_phrase_mapping m '
                'LEFT JOIN product p ON p.product_id = m.product_id '
                'WHERE m.phrase LIKE ? OR p.product_name LIKE ? '
                'ORDER BY m.last_used_at DESC LIMIT ?',
                (like, like, limit)
            )
        else:
            cur.execute(
                'SELECT m.*, p.product_name FROM voice_phrase_mapping m '
                'LEFT JOIN product p ON p.product_id = m.product_id '
                'ORDER BY m.last_used_at DESC LIMIT ?',
                (limit,)
            )
        rows = cur.fetchall()
        conn.close()
        return [dict(r) for r in rows]

    @staticmethod
    def set_status(mapping_id: int, status: str) -> dict | None:
        """人工启用/停用"""
        if status not in ('active', 'pending'):
            raise ValueError(f'不支持的 status: {status}')
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            'UPDATE voice_phrase_mapping SET status=? WHERE id=?',
            (status, mapping_id)
        )
        conn.commit()
        conn.close()
        return VoiceMapping.get_by_id(mapping_id)

    @staticmethod
    def delete(mapping_id: int) -> None:
        conn = get_db()
        cur = conn.cursor()
        cur.execute('DELETE FROM voice_phrase_mapping WHERE id=?', (mapping_id,))
        conn.commit()
        conn.close()
