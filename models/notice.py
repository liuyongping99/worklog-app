"""通知：通知主体 + 通知图片"""
import os
from datetime import datetime
from ._db import get_db
class Notice:
    @staticmethod
    def create(category: str, content: str):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT COALESCE(MAX(sort_order), 0) + 1 FROM notices WHERE category = ?', (category,))
        next_order = cursor.fetchone()[0]
        created_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        cursor.execute(
            'INSERT INTO notices (category, content, sort_order, created_at) VALUES (?, ?, ?, ?)',
            (category, content, next_order, created_at)
        )
        conn.commit()
        conn.close()

    @staticmethod
    def get_all():
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM notices ORDER BY category, sort_order')
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    @staticmethod
    def delete(notice_id: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM notices WHERE id = ?', (notice_id,))
        conn.commit()
        conn.close()

    @staticmethod
    def update(notice_id: int, category: str, content: str):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('UPDATE notices SET category = ?, content = ? WHERE id = ?', (category, content, notice_id))
        conn.commit()
        conn.close()

    @staticmethod
    def move_up(notice_id: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT category, sort_order FROM notices WHERE id = ?', (notice_id,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return
        cat, order = row[0], row[1]
        cursor.execute(
            'SELECT id, sort_order FROM notices WHERE category = ? AND sort_order < ? ORDER BY sort_order DESC LIMIT 1',
            (cat, order)
        )
        above = cursor.fetchone()
        if above:
            cursor.execute('UPDATE notices SET sort_order = ? WHERE id = ?', (order, above[0]))
            cursor.execute('UPDATE notices SET sort_order = ? WHERE id = ?', (above[1], notice_id))
            conn.commit()
        conn.close()

    @staticmethod
    def move_down(notice_id: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT category, sort_order FROM notices WHERE id = ?', (notice_id,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return
        cat, order = row[0], row[1]
        cursor.execute(
            'SELECT id, sort_order FROM notices WHERE category = ? AND sort_order > ? ORDER BY sort_order ASC LIMIT 1',
            (cat, order)
        )
        below = cursor.fetchone()
        if below:
            cursor.execute('UPDATE notices SET sort_order = ? WHERE id = ?', (order, below[0]))
            cursor.execute('UPDATE notices SET sort_order = ? WHERE id = ?', (below[1], notice_id))
            conn.commit()
        conn.close()

    @staticmethod
    def set_img_cols(notice_id: int, cols: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('UPDATE notices SET img_cols = ? WHERE id = ?', (cols, notice_id))
        conn.commit()
        conn.close()


class NoticeImage:
    @staticmethod
    def create(notice_pk: int, file_name: str, original_name: str = '') -> int:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO notice_images (notice_pk, file_name, original_name, created_at) VALUES (?, ?, ?, ?)',
            (notice_pk, file_name, original_name, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        )
        image_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return image_id

    @staticmethod
    def get_by_notice(notice_pk: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM notice_images WHERE notice_pk = ? ORDER BY id ASC', (notice_pk,))
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    @staticmethod
    def delete(image_id: int) -> bool:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT file_name FROM notice_images WHERE id = ?', (image_id,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return False
        file_name = row[0]
        cursor.execute('DELETE FROM notice_images WHERE id = ?', (image_id,))
        conn.commit()
        conn.close()
        # 删除文件
        try:
            import os
            file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'notice', file_name)
            if os.path.exists(file_path):
                os.remove(file_path)
        except Exception:
            pass
        return True


