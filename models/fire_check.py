"""消防检查记录:仓库/车间消防设施的例行检查台账。

要素:
    date              检查日期
    location          检查部位(仓库/车间/办公区/宿舍…)
    check_items       检查事项(本次查了什么、结果如何)
    outstanding_items 遗留事项(查出来还没整改的问题)
    status            状态:open=有遗留待整改 / closed=已整改闭环
    remark            备注

status 由后端按「遗留事项是否为空」自动推导(有遗留=open,无遗留=closed),
不用用户手选——避免出现「填了遗留事项却标成已整改」这种自相矛盾的记录。
用户也可以手动改状态,手动改过之后以用户为准(见 update 的 force_status)。

表结构见 models/_init.py 的 fire_checks。
"""
import os
from datetime import datetime
from typing import Optional

from ._db import get_db
from .orders import _safe_remove_file

# 上传根目录（与 blueprints/_helpers.get_upload_dir() 的 BASE_DIR/upload 同一处）
# 做成模块级常量：测试可把它指向临时目录，避免测试文件落到生产 upload/ 目录
UPLOAD_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'upload')


class FireCheck:
    """消防检查记录模型。"""

    STATUS_OPEN = 'open'
    STATUS_CLOSED = 'closed'

    STATUS_LABEL = {
        STATUS_OPEN: '待整改',
        STATUS_CLOSED: '已闭环',
    }

    @staticmethod
    def derive_status(outstanding_items: str) -> str:
        """按遗留事项推导状态:留了遗留 → open,没留 → closed。"""
        return FireCheck.STATUS_OPEN if (outstanding_items or '').strip() else FireCheck.STATUS_CLOSED

    @staticmethod
    def create(date: str, check_items: str, location: str = '',
               outstanding_items: str = '', remark: str = '',
               status: str = '', user_id: Optional[int] = None) -> int:
        """新建一条消防检查记录,返回记录 id。"""
        now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        st = status if status in (FireCheck.STATUS_OPEN, FireCheck.STATUS_CLOSED) \
            else FireCheck.derive_status(outstanding_items)
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO fire_checks '
            '(date, location, check_items, outstanding_items, status, remark, user_id, created_at, updated_at) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (date, location or '', check_items or '', outstanding_items or '',
             st, remark or '', user_id, now, now))
        record_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return int(record_id) if record_id is not None else 0

    @staticmethod
    def get_all():
        """全部记录,按日期倒序(同日期按 id 倒序)。"""
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM fire_checks ORDER BY date DESC, id DESC')
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    @staticmethod
    def get_by_id(record_id: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM fire_checks WHERE id = ?', (record_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    @staticmethod
    def update(record_id: int, date: str, check_items: str, location: str = '',
               outstanding_items: str = '', remark: str = '',
               status: str = '') -> bool:
        """修改一条记录,返回是否真的改动了(记录不存在时为 False)。

        status 传空 → 按遗留事项重新推导;传了合法值 → 以用户为准。
        """
        st = status if status in (FireCheck.STATUS_OPEN, FireCheck.STATUS_CLOSED) \
            else FireCheck.derive_status(outstanding_items)
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'UPDATE fire_checks SET date = ?, location = ?, check_items = ?, '
            'outstanding_items = ?, status = ?, remark = ?, updated_at = ? WHERE id = ?',
            (date, location or '', check_items or '', outstanding_items or '',
             st, remark or '', datetime.now().strftime('%Y-%m-%d %H:%M:%S'), record_id))
        changed = conn.total_changes > 0
        conn.commit()
        conn.close()
        return changed

    @staticmethod
    def delete(record_id: int) -> bool:
        """删除一条记录 —— 级联删除其图片（DB 行 + 物理文件）。"""
        # 先清图片（文件走 _safe_remove_file：upload/ 白名单 + mavis-trash 回收站）
        FireCheckImage.delete_by_record(record_id)
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM fire_checks WHERE id = ?', (record_id,))
        changed = conn.total_changes > 0
        conn.commit()
        conn.close()
        return changed

    @staticmethod
    def stats(records=None) -> dict:
        """汇总:总条数 / 有遗留待整改数 / 已闭环数。"""
        records = records if records is not None else FireCheck.get_all()
        open_n = sum(1 for r in records if r.get('status') == FireCheck.STATUS_OPEN)
        closed_n = sum(1 for r in records if r.get('status') == FireCheck.STATUS_CLOSED)
        return {'total': len(records), 'open': open_n, 'closed': closed_n}


class FireCheckImage:
    """消防检查记录图片。

    存相对路径(month_dir + file_name)而非绝对盘符路径 —— 规避踩坑点 9
    (`*_images.file_path` 存绝对路径,换盘符必须跑迁移脚本改写全库)。

    物理文件落在 <BASE_DIR>/upload/<YYYY-MM>/<file_name>,URL 为
    /upload/<month_dir>/<file_name>(由 blueprints/upload.py 提供服务)。
    """

    @staticmethod
    def create(record_pk: int, file_name: str, month_dir: str = '', original_name: str = '') -> int:
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute(
            'INSERT INTO fire_check_images (record_pk, file_name, month_dir, original_name, created_at) '
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
            'SELECT * FROM fire_check_images WHERE record_pk = ? ORDER BY id ASC', (record_pk,)
        )
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    @staticmethod
    def get_by_id(image_id: int):
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM fire_check_images WHERE id = ?', (image_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    @staticmethod
    def get_all():
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM fire_check_images ORDER BY id ASC')
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    @staticmethod
    def build_url(img: dict) -> str:
        """DB 行 → 可访问的 URL(/upload/<YYYY-MM>/<file_name>)。"""
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
        """删除一张图片:DB 行 + 物理文件(文件进回收站,可恢复)。"""
        img = FireCheckImage.get_by_id(image_id)
        if not img:
            return False
        # 文件先删(走白名单校验),再删 DB 行 —— 两边都清掉才叫干净
        _safe_remove_file(FireCheckImage.abs_path(img), UPLOAD_ROOT)
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM fire_check_images WHERE id = ?', (image_id,))
        conn.commit()
        conn.close()
        return True

    @staticmethod
    def delete_by_record(record_pk: int) -> int:
        """级联删除某条检查记录的全部图片,返回删除数量。"""
        images = FireCheckImage.get_by_record(record_pk)
        for img in images:
            _safe_remove_file(FireCheckImage.abs_path(img), UPLOAD_ROOT)
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM fire_check_images WHERE record_pk = ?', (record_pk,))
        conn.commit()
        conn.close()
        return len(images)

    @staticmethod
    def remove_file(filepath: str) -> bool:
        """删除一个已落盘但尚未入库的上传文件(上传中途失败时的回滚)。"""
        return _safe_remove_file(filepath, UPLOAD_ROOT)
