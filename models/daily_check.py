"""日终盘点:用户手工填的「当日出货单总量」。

背景:纸质出货单是一天一张张开单,单号 TD-YYYY-MM-DD-NNN 本身不连续
(实测 09-16 最大序号 017 但当天只有 7 单、09-17 的 004 整段没录),
光看系统里的单号编号判断不出少录了几单。日终时把纸面单据总数手工填进来,
和系统实际录入的单数当场对账,差多少直接报出来。

表结构见 models/_init.py 的 daily_order_check:
    scope / date / manual_total / remark / user_id / created_at / updated_at
    UNIQUE(scope, date) —— 一天一条,重复填是 upsert 不是追加

scope 列是给后续入库/装柜页预留的:同表不同 scope,零改表即可复用。
本次只接出货页(scope='shipping')。
"""
from datetime import datetime
from typing import Optional

from ._db import get_db


class DailyOrderCheck:
    """日终盘点记录模型。"""

    SCOPE_SHIPPING = 'shipping'
    SCOPE_INBOUND = 'inbound'
    SCOPE_LOADING = 'loading'

    @staticmethod
    def get(scope: str, date: str):
        """读某天的一条手填记录,没有则返回 None。"""
        if not date:
            return None
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT * FROM daily_order_check WHERE scope = ? AND date = ?',
            (scope, date))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    @staticmethod
    def get_map(scope: str, start_date: str, end_date: str) -> dict:
        """读区间内全部手填记录 → {date: row}。没有记录时返回空 dict。"""
        if not start_date or not end_date:
            return {}
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT * FROM daily_order_check WHERE scope = ? AND date >= ? AND date <= ?',
            (scope, start_date, end_date))
        rows = cursor.fetchall()
        conn.close()
        return {row['date']: dict(row) for row in rows}

    @staticmethod
    def upsert(scope: str, date: str, manual_total: int,
               remark: str = '', user_id: Optional[int] = None) -> Optional[int]:
        """写/覆盖某天的手填值,返回该行 id。同一天重复填是覆盖不是追加。"""
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO daily_order_check '
            '(scope, date, manual_total, remark, user_id, created_at, updated_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?) '
            'ON CONFLICT(scope, date) DO UPDATE SET '
            'manual_total = excluded.manual_total, '
            'remark = excluded.remark, '
            'user_id = excluded.user_id, '
            'updated_at = excluded.updated_at',
            (scope, date, int(manual_total), remark or '', user_id, now, now))
        conn.commit()
        # ON CONFLICT 走 DO UPDATE 分支时 cursor.lastrowid 不可靠(是陈旧值),
        # 统一回查一次,保证返回的 id 一定是本行的。
        cursor.execute(
            'SELECT id FROM daily_order_check WHERE scope = ? AND date = ?',
            (scope, date))
        found = cursor.fetchone()
        row_id = found['id'] if found else None
        conn.close()
        return row_id

    @staticmethod
    def delete(scope: str, date: str) -> bool:
        """清除某天的手填值(恢复「未盘点」)。"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'DELETE FROM daily_order_check WHERE scope = ? AND date = ?',
            (scope, date))
        changed = conn.total_changes > 0
        conn.commit()
        conn.close()
        return changed
