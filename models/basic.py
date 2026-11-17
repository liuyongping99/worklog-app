"""基础记录：工作经验 / 错误经验 / 待办 / 车辆维护"""
import os
from datetime import datetime
from ._db import get_db
from .orders import _safe_remove_file

# 上传根目录（与 blueprints/_helpers.get_upload_dir() 的 BASE_DIR/upload 同一处）
# 做成模块级常量：测试可把它指向临时目录，避免测试文件落到生产 upload/ 目录
UPLOAD_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'upload')

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
    def create(date: str, vehicle_plate: str, type: str, description: str, cost: str = '', remark: str = '') -> int:
        """新建维护记录，返回记录 id（供后续绑定图片用）。"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO vehicle_maintenance (date, vehicle_plate, type, description, cost, remark, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)',
            (date, vehicle_plate, type, description, cost, remark, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        )
        record_id = cursor.lastrowid
        conn.commit()
        conn.close()
        # INSERT 成功后 lastrowid 必为整数；0 表示异常（AUTOINCREMENT 从 1 起）
        return int(record_id) if record_id is not None else 0

    @staticmethod
    def get_all():
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM vehicle_maintenance ORDER BY date DESC, id DESC')
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    @staticmethod
    def get_by_id(record_id: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM vehicle_maintenance WHERE id = ?', (record_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    @staticmethod
    def update(record_id: int, date: str, vehicle_plate: str, type: str,
               description: str, cost: str = '', remark: str = '') -> bool:
        """修改维护记录，返回是否真的改动了（记录不存在时为 False）。"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'UPDATE vehicle_maintenance SET date = ?, vehicle_plate = ?, type = ?, '
            'description = ?, cost = ?, remark = ? WHERE id = ?',
            (date, vehicle_plate, type, description, cost, remark, record_id)
        )
        changed = conn.total_changes > 0
        conn.commit()
        conn.close()
        return changed

    @staticmethod
    def delete(record_id: int):
        """删除维护记录 —— 级联删除其图片（DB 行 + 物理文件）。"""
        # 先清图片（文件走 _safe_remove_file：upload/ 白名单 + mavis-trash 回收站）
        VehicleMaintenanceImage.delete_by_record(record_id)
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM vehicle_maintenance WHERE id = ?', (record_id,))
        conn.commit()
        conn.close()


class VehicleMaintenanceImage:
    """车辆维护记录图片。

    存相对路径（month_dir + file_name）而非绝对盘符路径 —— 规避踩坑点 9
    （`*_images.file_path` 存绝对路径，换盘符必须跑迁移脚本改写全库）。

    物理文件落在 <BASE_DIR>/upload/<YYYY-MM>/<file_name>，URL 为
    /upload/<month_dir>/<file_name>（由 blueprints/upload.py 提供服务）。
    """

    @staticmethod
    def create(record_pk: int, file_name: str, month_dir: str = '', original_name: str = '') -> int:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO vehicle_maintenance_images (record_pk, file_name, month_dir, original_name, created_at) '
            'VALUES (?, ?, ?, ?, ?)',
            (record_pk, file_name, month_dir, original_name, datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
        )
        image_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return int(image_id) if image_id is not None else 0

    @staticmethod
    def get_by_record(record_pk: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'SELECT * FROM vehicle_maintenance_images WHERE record_pk = ? ORDER BY id ASC', (record_pk,)
        )
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    @staticmethod
    def get_by_id(image_id: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM vehicle_maintenance_images WHERE id = ?', (image_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    @staticmethod
    def get_all():
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM vehicle_maintenance_images ORDER BY id ASC')
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    @staticmethod
    def build_url(img: dict) -> str:
        """DB 行 → 可访问的 URL（/upload/<YYYY-MM>/<file_name>）。"""
        month = (img.get('month_dir') or '').strip().strip('/')
        name = (img.get('file_name') or '').strip().strip('/')
        return '/upload/%s/%s' % (month, name) if month else '/upload/%s' % name

    @staticmethod
    def abs_path(img: dict) -> str:
        """DB 行 → 物理文件绝对路径。"""
        month = (img.get('month_dir') or '').strip().strip('/')
        name = (img.get('file_name') or '').strip().strip('/')
        return os.path.join(UPLOAD_ROOT, month, name)

    @staticmethod
    def delete(image_id: int) -> bool:
        """删除一张图片：DB 行 + 物理文件（文件进回收站，可恢复）。"""
        img = VehicleMaintenanceImage.get_by_id(image_id)
        if not img:
            return False
        # 文件先删（走白名单校验），再删 DB 行 —— 两边都清掉才叫干净
        _safe_remove_file(VehicleMaintenanceImage.abs_path(img), UPLOAD_ROOT)
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM vehicle_maintenance_images WHERE id = ?', (image_id,))
        conn.commit()
        conn.close()
        return True

    @staticmethod
    def delete_by_record(record_pk: int) -> int:
        """级联删除某条维护记录的全部图片，返回删除数量。"""
        images = VehicleMaintenanceImage.get_by_record(record_pk)
        for img in images:
            _safe_remove_file(VehicleMaintenanceImage.abs_path(img), UPLOAD_ROOT)
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM vehicle_maintenance_images WHERE record_pk = ?', (record_pk,))
        conn.commit()
        conn.close()
        return len(images)

    @staticmethod
    def remove_file(filepath: str) -> bool:
        """删除一个已落盘但尚未入库的上传文件（上传中途失败时的回滚）。

        走与删除图片同一条通道：路径白名单校验 + mavis-trash 回收站（可恢复）。
        """
        return _safe_remove_file(filepath, UPLOAD_ROOT)


