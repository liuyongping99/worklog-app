"""辅助：缺货登记"""
from datetime import datetime
from ._db import get_db


class StockOutItem:
    """缺货商品"""
    @staticmethod
    def create(name: str, color: str):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            "INSERT INTO stock_out_items (name, color, created_at) VALUES (?, ?, ?)",
            (name, color, datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        )
        conn.commit()
        item_id = cursor.lastrowid
        conn.close()
        return item_id

    @staticmethod
    def get_all():
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM stock_out_items ORDER BY id ASC")
        rows = cursor.fetchall()
        conn.close()
        result = []
        for row in rows:
            result.append(dict(row))
        return result

    @staticmethod
    def delete(item_id: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM stock_out_items WHERE id = ?", (item_id,))
        conn.commit()
        conn.close()
        return True
