# -*- coding: utf-8 -*-
"""独立点数工具的数据模型。

与出货订单的 PlacementImage / placement_marks 完全独立(可追溯性要求:
单独建表、单独持久化,不挂在出货订单上)。

表结构(见 models/_init.py 的迁移):
- point_count_sessions  id / user_id(操作员 staff.id) / title / expected_count /
                       unit / remark / status(open|closed) /
                       total_count(缓存的总数) / created_at / closed_at
- point_count_images   id / session_id / file_path / original_name /
                       mark_scale / loose_count / sort_order / created_at
- point_count_marks    id / image_id / seq(从 1 起) / x_ratio(0~1) /
                       y_ratio(0~1) / created_at

可追溯:
- 每个 mark 记 created_at,可以查到"哪一刻按下了计数点"。
- 每个 image 记 created_at,可以查到"何时上传的图"。
- session 记 user_id + created_at,可以查到"谁 / 何时建的点数组"。
- session.total_count = SUM(marks.seq) over all images + SUM(loose_count)。
  写操作(add_mark / delete_last_mark / set_loose_count / set_mark_scale)后
  自动同步刷新。
"""
import os
import json
import shutil
import subprocess
from datetime import datetime

from ._db import get_db


# mavis-trash 路径(在模块加载时解析一次,subprocess 调 .cmd 包装器)。
# 路径越界直接拒删,与 orders.PlacementImage 同一防御。
_MAVIS_TRASH = shutil.which('mavis-trash')
_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_UPLOAD_ROOT = os.path.realpath(os.path.join(_BASE_DIR, 'upload'))


def _safe_remove_file(file_path):
    """删除图片物理文件,走 mavis-trash(可恢复)+ 路径白名单(防越界)。"""
    if not file_path:
        return True
    try:
        real = os.path.realpath(file_path)
        if not real.startswith(_UPLOAD_ROOT + os.sep):
            return False  # 越界拒绝
    except Exception:
        return False
    if not os.path.exists(file_path):
        return True
    if not _MAVIS_TRASH:
        try:
            os.remove(file_path)
            return True
        except Exception:
            return False
    try:
        subprocess.run([_MAVIS_TRASH, file_path], check=False, timeout=10)
        return True
    except Exception:
        return False


def _now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


# ── 会话 ──────────────────────────────────────────────────────────
class PointCountSession:
    """一个点数组(由谁建/何时/标题/可选期望值)。"""

    @staticmethod
    def create(user_id: int, title: str, expected_count: int = None,
               unit: str = '支', remark: str = '') -> int:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            '''INSERT INTO point_count_sessions
               (user_id, title, expected_count, unit, remark, status, total_count, created_at)
               VALUES (?, ?, ?, ?, ?, 'open', 0, ?)''',
            (int(user_id), title.strip(), expected_count, unit.strip() or '支',
             remark.strip(), _now()),
        )
        sid = cursor.lastrowid
        conn.commit()
        conn.close()
        return sid

    @staticmethod
    def get_by_id(session_id: int):
        conn = get_db()
        row = conn.execute(
            'SELECT * FROM point_count_sessions WHERE id = ?', (session_id,)
        ).fetchone()
        conn.close()
        return dict(row) if row else None

    @staticmethod
    def list_all(limit: int = 200):
        """返回最近 limit 条会话,按创建时间倒序。"""
        conn = get_db()
        rows = conn.execute(
            '''SELECT s.*, st.name AS user_name, st.role AS user_role
               FROM point_count_sessions s
               LEFT JOIN staff st ON st.id = s.user_id
               ORDER BY s.id DESC LIMIT ?''',
            (int(limit),),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    @staticmethod
    def update_meta(session_id: int, **fields) -> bool:
        """更新会话级元数据(title / expected_count / unit / remark)。"""
        allowed = {'title', 'expected_count', 'unit', 'remark'}
        sets = []
        values = []
        for k, v in fields.items():
            if k in allowed:
                sets.append(f'{k} = ?')
                values.append(v)
        if not sets:
            return False
        values.append(session_id)
        conn = get_db()
        conn.execute(
            f'UPDATE point_count_sessions SET {", ".join(sets)} WHERE id = ?',
            values,
        )
        conn.commit()
        conn.close()
        return True

    @staticmethod
    def close(session_id: int) -> bool:
        conn = get_db()
        conn.execute(
            '''UPDATE point_count_sessions
               SET status = 'closed', closed_at = ?
               WHERE id = ? AND status = 'open' ''',
            (_now(), session_id),
        )
        changed = conn.total_changes > 0
        conn.commit()
        conn.close()
        return changed

    @staticmethod
    def reopen(session_id: int) -> bool:
        conn = get_db()
        conn.execute(
            '''UPDATE point_count_sessions
               SET status = 'open', closed_at = NULL
               WHERE id = ? AND status = 'closed' ''',
            (session_id,),
        )
        changed = conn.total_changes > 0
        conn.commit()
        conn.close()
        return changed

    @staticmethod
    def delete(session_id: int) -> bool:
        """删除整个会话(连带所有图 / 所有 mark / 物理文件)。"""
        images = PointCountImage.get_by_session(session_id)
        for img in images:
            PointCountImage.delete(img['id'])
        conn = get_db()
        conn.execute('DELETE FROM point_count_sessions WHERE id = ?', (session_id,))
        changed = conn.total_changes > 0
        conn.commit()
        conn.close()
        return changed

    @staticmethod
    def refresh_total(session_id: int) -> int:
        """重算 session.total_count = Σmarks + Σloose_count,返回新值。"""
        conn = get_db()
        row = conn.execute(
            '''SELECT COALESCE(SUM(m.cnt), 0) + COALESCE((
                       SELECT COALESCE(SUM(loose_count), 0)
                       FROM point_count_images
                       WHERE session_id = ?), 0) AS total
               FROM (
                   SELECT pi.session_id, COUNT(pm.id) AS cnt
                   FROM point_count_images pi
                   LEFT JOIN point_count_marks pm ON pm.image_id = pi.id
                   WHERE pi.session_id = ?
                   GROUP BY pi.id
               ) m''',
            (session_id, session_id),
        ).fetchone()
        total = int(row['total'] if row else 0)
        conn.execute(
            'UPDATE point_count_sessions SET total_count = ? WHERE id = ?',
            (total, session_id),
        )
        conn.commit()
        conn.close()
        return total


# ── 图片 ──────────────────────────────────────────────────────────
class PointCountImage:
    """一张上传的图,FK 到会话;每个 mark_scale / loose_count 局部状态。"""

    @staticmethod
    def get_relative_path(file_path: str) -> str:
        """返回相对 upload/ 的路径(用于 URL 暴露)。"""
        if 'upload\\' in file_path:
            return file_path.split('upload\\')[-1].replace('\\', '/')
        if 'upload/' in file_path:
            return file_path.split('upload/', 1)[-1]
        return file_path

    @staticmethod
    def create(session_id: int, file_path: str, original_name: str = '') -> int:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT COALESCE(MAX(sort_order), 0) + 1 FROM point_count_images WHERE session_id = ?',
            (session_id,),
        )
        sort_order = cursor.fetchone()[0]
        cursor.execute(
            '''INSERT INTO point_count_images
               (session_id, file_path, original_name, mark_scale, loose_count, sort_order, created_at)
               VALUES (?, ?, ?, 1, 0, ?, ?)''',
            (session_id, file_path, original_name or '', sort_order, _now()),
        )
        image_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return image_id

    @staticmethod
    def get_by_id(image_id: int):
        conn = get_db()
        row = conn.execute(
            'SELECT * FROM point_count_images WHERE id = ?', (image_id,)
        ).fetchone()
        conn.close()
        if not row:
            return None
        item = dict(row)
        item['relative_path'] = PointCountImage.get_relative_path(item['file_path'])
        item['marks'] = PointCountImage.get_marks(image_id)
        item['n_marks'] = len(item['marks'])
        return item

    @staticmethod
    def get_by_session(session_id: int):
        conn = get_db()
        rows = conn.execute(
            '''SELECT * FROM point_count_images
               WHERE session_id = ?
               ORDER BY sort_order ASC, id ASC''',
            (session_id,),
        ).fetchall()
        conn.close()
        result = []
        for row in rows:
            item = dict(row)
            item['relative_path'] = PointCountImage.get_relative_path(item['file_path'])
            item['marks'] = PointCountImage.get_marks(item['id'])
            item['n_marks'] = len(item['marks'])
            result.append(item)
        return result

    @staticmethod
    def get_marks(image_id: int):
        conn = get_db()
        rows = conn.execute(
            'SELECT * FROM point_count_marks WHERE image_id = ? ORDER BY seq ASC',
            (image_id,),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    @staticmethod
    def delete(image_id: int) -> bool:
        """删除图片(连带 mark + 物理文件)。"""
        conn = get_db()
        row = conn.execute(
            'SELECT session_id, file_path FROM point_count_images WHERE id = ?',
            (image_id,),
        ).fetchone()
        if not row:
            conn.close()
            return False
        session_id = row['session_id']
        file_path = row['file_path']
        # FK CASCADE 会自动删 mark,这里显式删一遍也无害(双重保险)。
        conn.execute('DELETE FROM point_count_marks WHERE image_id = ?', (image_id,))
        conn.execute('DELETE FROM point_count_images WHERE id = ?', (image_id,))
        conn.commit()
        conn.close()
        _safe_remove_file(file_path)
        PointCountSession.refresh_total(session_id)
        return True

    @staticmethod
    def add_mark(image_id: int, x_ratio: float, y_ratio: float) -> dict:
        """在图上加一枚 mark,返回完整 marks 列表。"""
        x = max(0.0, min(1.0, float(x_ratio)))
        y = max(0.0, min(1.0, float(y_ratio)))
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT session_id FROM point_count_images WHERE id = ?',
            (image_id,),
        )
        row = cursor.fetchone()
        if not row:
            conn.close()
            return {'marks': [], 'session_id': None}
        session_id = row['session_id']
        cursor.execute(
            'SELECT COALESCE(MAX(seq), 0) + 1 FROM point_count_marks WHERE image_id = ?',
            (image_id,),
        )
        seq = cursor.fetchone()[0]
        cursor.execute(
            '''INSERT INTO point_count_marks
               (image_id, seq, x_ratio, y_ratio, created_at)
               VALUES (?, ?, ?, ?, ?)''',
            (image_id, seq, x, y, _now()),
        )
        conn.commit()
        marks = [dict(r) for r in conn.execute(
            'SELECT * FROM point_count_marks WHERE image_id = ? ORDER BY seq ASC',
            (image_id,),
        ).fetchall()]
        conn.close()
        PointCountSession.refresh_total(session_id)
        return {'marks': marks, 'session_id': session_id}

    @staticmethod
    def delete_last_mark(image_id: int) -> dict:
        """撤销最近一枚 mark。返回剩余 marks。"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT session_id FROM point_count_images WHERE id = ?',
            (image_id,),
        )
        row = cursor.fetchone()
        if not row:
            conn.close()
            return {'marks': [], 'session_id': None}
        session_id = row['session_id']
        cursor.execute(
            'SELECT MAX(seq) FROM point_count_marks WHERE image_id = ?',
            (image_id,),
        )
        max_seq = cursor.fetchone()[0]
        if max_seq is None:
            conn.close()
            return {'marks': [], 'session_id': session_id}
        cursor.execute(
            'DELETE FROM point_count_marks WHERE image_id = ? AND seq = ?',
            (image_id, max_seq),
        )
        conn.commit()
        marks = [dict(r) for r in conn.execute(
            'SELECT * FROM point_count_marks WHERE image_id = ? ORDER BY seq ASC',
            (image_id,),
        ).fetchall()]
        conn.close()
        PointCountSession.refresh_total(session_id)
        return {'marks': marks, 'session_id': session_id}

    @staticmethod
    def set_mark_scale(image_id: int, scale: float) -> float:
        try:
            scale = float(scale)
        except (TypeError, ValueError):
            scale = 1.0
        scale = max(0.3, min(4.0, scale))
        conn = get_db()
        conn.execute(
            'UPDATE point_count_images SET mark_scale = ? WHERE id = ?',
            (scale, image_id),
        )
        conn.commit()
        conn.close()
        return scale

    @staticmethod
    def set_loose_count(image_id: int, count: int) -> int:
        try:
            count = int(round(float(count)))
        except (TypeError, ValueError):
            count = 0
        if count < 0:
            count = 0
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT session_id FROM point_count_images WHERE id = ?',
            (image_id,),
        )
        row = cursor.fetchone()
        if not row:
            conn.close()
            return 0
        session_id = row['session_id']
        conn.execute(
            'UPDATE point_count_images SET loose_count = ? WHERE id = ?',
            (count, image_id),
        )
        conn.commit()
        conn.close()
        PointCountSession.refresh_total(session_id)
        return count