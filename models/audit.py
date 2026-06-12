"""操作日志"""
import json
from datetime import datetime
from ._db import get_db


class AuditLog:
    """操作日志——记录关键业务操作，未来多用户时是"谁动了什么"的依据。

    用法:
        AuditLog.log('create_order', 'shipping_order', order_id, {'customer': 'ACME'})
        AuditLog.log('lock_order', 'shipping_order', order_id)
    """
    @staticmethod
    def log(action: str, entity_type: str = None, entity_id: int = None,
            user_id: int = None, detail: dict = None):
        """写入一条日志。detail 会序列化为 JSON 字符串存储。"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO audit_log (action, entity_type, entity_id, user_id, detail, created_at) VALUES (?, ?, ?, ?, ?, ?)',
            (action, entity_type, entity_id, user_id,
             json.dumps(detail, ensure_ascii=False) if detail else None,
             datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        )
        log_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return log_id

    @staticmethod
    def get_recent(limit: int = 50, entity_type: str = None, entity_id: int = None):
        """查最近日志，可按 entity 过滤。返回按时间倒序的列表。"""
        conn = get_db()
        cursor = conn.cursor()
        sql = 'SELECT * FROM audit_log'
        params = []
        if entity_type and entity_id is not None:
            sql += ' WHERE entity_type = ? AND entity_id = ?'
            params = [entity_type, entity_id]
        elif entity_type:
            sql += ' WHERE entity_type = ?'
            params = [entity_type]
        sql += ' ORDER BY id DESC LIMIT ?'
        params.append(limit)
        cursor.execute(sql, params)
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]

    @staticmethod
    def count() -> int:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM audit_log')
        n = cursor.fetchone()[0]
        conn.close()
        return n
