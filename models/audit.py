"""操作日志"""
import json
from datetime import datetime
from ._db import get_db


class AuditLog:
    """操作日志——记录关键业务操作，未来多用户时是"谁动了什么"的依据。

    用法:
        AuditLog.log('create_order', 'shipping_order', order_id, {'customer': 'ACME'})
        AuditLog.log('lock_order', 'shipping_order', order_id)

    ⭐ **A7(上游 §3.9 / §7.8)——操作员自动注入**:
        改账类接口(过账 / 冲销 / 盘点提交 / 归零)的审计必须能回答「谁改的」,
        而现状 audit_log 里有 20067 行 user_id 为 NULL(调用方不传 → 写 NULL)。
        所以 `log()` 内部**自动从 Flask session 取 operator_id**:
          · 调用方显式传了 user_id  → 用传进来的
          · 没传 → 用 session 里的当前操作员
          · 都不行(CLI 工具 / 后台线程,没有请求上下文)→ 留 NULL(不报错)
        改账类 API 另外会在**入口**校验操作员已登录(见 blueprints/stock.py 的
        `_require_operator()`),保证改账一定有痕。
    """

    @staticmethod
    def _current_operator_id():
        """从 Flask session 取当前操作员 id;不在请求上下文里返回 None。

        ⚠️ `session.get()` 在没有 app context 时会抛 RuntimeError
          (后台线程 / CLI 工具 / 单元测试里直接调模型都会踩到)→ 必须 try。
        """
        try:
            from flask import session
            return session.get('operator_id')
        except Exception:
            return None

    @staticmethod
    def log(action: str, entity_type: str = None, entity_id: int = None,
            user_id: int = None, detail: dict = None):
        """写入一条日志。detail 会序列化为 JSON 字符串存储。

        ⭐ user_id 缺省时**自动注入** session 里的 operator_id(A7),见类文档。
        """
        if user_id is None:
            user_id = AuditLog._current_operator_id()
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
