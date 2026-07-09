"""件数换算规则：件→张/只/令 等"""
from ._db import get_db


class PieceConversion:
    """件数换算规则模型。

    匹配逻辑与 ProductUnit 一致（两轮）：
    1. product_name 相等 + spec_keyword 在规格中出现
    2. product_name 相等 + spec_keyword IS NULL（默认行）
    """

    @staticmethod
    def create(product_name, units_per_piece, target_unit, spec_keyword=None, model=None):
        """创建或替换换算规则。"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT OR REPLACE INTO piece_conversions "
            "(product_name, spec_keyword, model, units_per_piece, target_unit) "
            "VALUES (?, ?, ?, ?, ?)",
            (product_name, spec_keyword, model, units_per_piece, target_unit)
        )
        conn.commit()
        conn.close()

    @staticmethod
    def get_all():
        """全表查询，按 product_name + spec_keyword 排序。"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM piece_conversions ORDER BY product_name ASC, spec_keyword ASC"
        )
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    @staticmethod
    def get_match(product_name, spec=''):
        """两轮匹配：先按 spec_keyword 精确匹配，未命中取默认行。

        Args:
            product_name: 商品名
            spec: 规格字符串

        Returns:
            dict 或 None
        """
        conn = get_db()
        cursor = conn.cursor()
        if spec:
            cursor.execute(
                "SELECT * FROM piece_conversions WHERE product_name = ? AND spec_keyword IS NOT NULL",
                (product_name,)
            )
            for row in cursor.fetchall():
                kw = row['spec_keyword']
                if kw and kw in spec:
                    conn.close()
                    return dict(row)
        # fallback 到默认行
        cursor.execute(
            "SELECT * FROM piece_conversions WHERE product_name = ? AND spec_keyword IS NULL",
            (product_name,)
        )
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    @staticmethod
    def update(id, **kwargs):
        """按 id 更新字段。"""
        allowed = ['product_name', 'spec_keyword', 'model',
                   'units_per_piece', 'target_unit', 'is_active']
        sets = []
        vals = []
        for k in allowed:
            if k in kwargs:
                sets.append(f"{k} = ?")
                vals.append(kwargs[k])
        if not sets:
            return
        sets.append("updated_at = datetime('now','localtime')")
        vals.append(id)
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            f"UPDATE piece_conversions SET {', '.join(sets)} WHERE id = ?", vals
        )
        conn.commit()
        conn.close()

    @staticmethod
    def delete(id):
        """按 id 删除。"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM piece_conversions WHERE id = ?", (id,))
        conn.commit()
        conn.close()
