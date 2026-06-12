"""基础记录：工作经验 / 错误经验 / 待办 / 车辆维护"""
from datetime import datetime
from ._db import get_db
class WorkLog:
    @staticmethod
    def create(title: str, content: str):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO work_logs (title, content, created_at) VALUES (?, ?, ?)',
            (title, content, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        )
        conn.commit()
        conn.close()

    @staticmethod
    def get_all():
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM work_logs ORDER BY id DESC')
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    @staticmethod
    def delete(log_id: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM work_logs WHERE id = ?', (log_id,))
        conn.commit()
        conn.close()


class ErrorLog:
    @staticmethod
    def create(title: str, error_type: str = '', solution: str = ''):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO error_logs (title, error_type, solution, created_at) VALUES (?, ?, ?, ?)',
            (title, error_type, solution, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        )
        conn.commit()
        conn.close()

    @staticmethod
    def get_all():
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM error_logs ORDER BY id DESC')
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    @staticmethod
    def delete(log_id: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM error_logs WHERE id = ?', (log_id,))
        conn.commit()
        conn.close()


class TodoItem:
    @staticmethod
    def create(content: str):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO todo_items (content, done, created_at) VALUES (?, 0, ?)',
            (content, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        )
        conn.commit()
        conn.close()

    @staticmethod
    def get_all():
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM todo_items ORDER BY done ASC, id DESC')
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    @staticmethod
    def toggle(item_id: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'UPDATE todo_items SET done = CASE WHEN done = 0 THEN 1 ELSE 0 END WHERE id = ?',
            (item_id,)
        )
        conn.commit()
        conn.close()

    @staticmethod
    def delete(item_id: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM todo_items WHERE id = ?', (item_id,))
        conn.commit()
        conn.close()


class VehicleMaintenance:
    @staticmethod
    def create(date: str, vehicle_plate: str, type: str, description: str, cost: str = '', remark: str = ''):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO vehicle_maintenance (date, vehicle_plate, type, description, cost, remark, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
            (date, vehicle_plate, type, description, cost, remark, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        )
        conn.commit()
        conn.close()

    @staticmethod
    def get_all():
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM vehicle_maintenance ORDER BY date DESC, id DESC')
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    @staticmethod
    def delete(record_id: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM vehicle_maintenance WHERE id = ?', (record_id,))
        conn.commit()
        conn.close()


