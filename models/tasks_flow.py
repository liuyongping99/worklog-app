"""任务流 M1 — 数据模型(占位 dataclass + DB CRUD)。

本文件为任务流(staff / vehicles / tasks / task_items / task_images / task_events)
提供两套能力:

1. **纯 dataclass 占位**(Staff / Task / TaskItem / TaskImage / TaskEvent):
   仅用于给 `models._permissions.can()` 这类纯函数喂数据,字段集与 DB schema
   对齐,不依赖 sqlite。后续 T5/T9 会用真 ORM/Repository 替换。

2. **DB CRUD 工具类**(StaffDB / TaskDB / TaskImageDB / TaskEventDB):
   静态方法,直接走 sqlite3,返回 dict(行式 Row 包装)便于模板/JSON 渲染。
   Task 10 只需要 Task.get_by_id / Task.update / Staff.create / Task.create /
   TaskImage.create / TaskImage.get_by_task 这几个,其它 CRUD 后续任务再补。

为什么不把 CRUD 直接挂在 @dataclass 的 Task 上:
    @dataclass 装饰的类再塞 classmethod 会破坏 dataclass 字段生成的 __init__,
    也跟 T5 的扩展计划(可能改用真正的 ORM)冲突。所以 DB 层单独放一个 *DB 命名空间,
    Task dataclass 保持纯净。测试如果需要 `Task.get_by_id` 用法,改用 `TaskDB.get_by_id`,
    这是与 T4 brief 的微小偏差。
"""
from dataclasses import dataclass
from typing import Optional

from ._db import get_db


# ============================================================
# 1. dataclass 占位（用于 can() 等纯函数喂数据）
# ============================================================

@dataclass
class Staff:
    id: int
    name: str
    gender: Optional[str]
    birth_date: Optional[str]
    role: str
    staff_code: Optional[str]
    phone: Optional[str]
    is_active: int
    vehicle_id: Optional[int]
    login_name: Optional[str]
    password_hash: Optional[str]
    created_at: Optional[str]
    updated_at: Optional[str]


@dataclass
class Task:
    id: int
    task_no: str
    source_order_no: Optional[str]
    task_type: str
    related_task_id: Optional[int]
    source_type: Optional[str]
    customer: Optional[str]
    dest_address: Optional[str]
    dest_lat: Optional[float]
    dest_lng: Optional[float]
    dest_poi_name: Optional[str]
    status: str
    coding_status: str
    driver_id: Optional[int]
    vehicle_id: Optional[int]
    creator_id: Optional[int]
    operator_id: Optional[int]
    coder_id: Optional[int]
    coding_claimed_at: Optional[str]
    coding_done_at: Optional[str]
    est_weight_kg: Optional[float]
    est_distance_km: Optional[float]
    depart_at: Optional[str]
    arrive_at: Optional[str]
    remark: Optional[str]
    is_cancelled: int
    created_at: Optional[str]
    updated_at: Optional[str]


@dataclass
class TaskItem:
    id: int
    task_id: int
    product_name: str
    specification: Optional[str]
    quantity: float
    unit: Optional[str]
    remark: Optional[str]
    sort_order: int
    line_type: str


@dataclass
class TaskImage:
    id: int
    task_id: int
    stage: str
    image_path: str
    sort_order: int
    created_at: Optional[str]


@dataclass
class TaskEvent:
    id: int
    task_id: int
    event_type: str
    from_status: Optional[str]
    to_status: Optional[str]
    operator_id: Optional[int]
    note: Optional[str]
    created_at: Optional[str]


# ============================================================
# 2. DB CRUD（Task 10 用得到的最小集合）
# ============================================================

class StaffDB:
    """staff 表 CRUD。返回 dict(跟 Row 同构)以便 session/JSON 直接用。"""

    @staticmethod
    def create(name: str, role: str, **fields) -> dict:
        """INSERT 一条 staff,返回完整行 dict(含自增 id)。"""
        cols = ["name", "role"]
        vals = [name, role]
        for k, v in fields.items():
            cols.append(k)
            vals.append(v)
        placeholders = ",".join("?" for _ in cols)
        sql = f"INSERT INTO staff ({','.join(cols)}) VALUES ({placeholders})"
        conn = get_db()
        cur = conn.execute(sql, vals)
        conn.commit()
        staff_id = cur.lastrowid
        return StaffDB.get_by_id(staff_id) or {
            "id": staff_id, "name": name, "role": role, **fields
        }

    @staticmethod
    def get_by_id(staff_id: int) -> Optional[dict]:
        conn = get_db()
        row = conn.execute(
            "SELECT * FROM staff WHERE id = ?", (staff_id,)
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def get_active() -> list:
        """返回所有 is_active=1 的 staff,按 role 排序、id 升序兜底。

        登录选择页用 — 离职的(staff.is_active=0)不应出现在下拉里。
        排序规则:role(司机/调度/搬运/打码/仓管/文员)、同 role 按 id 升序。
        """
        conn = get_db()
        rows = conn.execute(
            "SELECT * FROM staff WHERE is_active = 1 "
            "ORDER BY CASE role "
            "  WHEN '司机' THEN 1 "
            "  WHEN '调度' THEN 2 "
            "  WHEN '搬运' THEN 3 "
            "  WHEN '打码' THEN 4 "
            "  WHEN '仓管' THEN 5 "
            "  WHEN '文员' THEN 6 "
            "  ELSE 99 END, id"
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def get_all(role: Optional[str] = None, include_inactive: bool = False) -> list:
        """默认只返回 is_active=1。role 不为空则按 role 过滤。

        T7 人员管理页用 — 比 get_active 更灵活,支持 role 过滤和显示离职。
        排序:按 id 升序(模板里再做展示排序)。
        """
        sql = "SELECT * FROM staff"
        params = []
        where = []
        if not include_inactive:
            where.append("is_active=1")
        if role:
            where.append("role=?")
            params.append(role)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id"
        return [dict(r) for r in get_db().execute(sql, params).fetchall()]

    @staticmethod
    def update(staff_id: int, **fields) -> None:
        """按 fields 增量更新 staff。无字段时 no-op。

        不做白名单限制(staff 表所有字段都可改,除了 id/created_at —
        id 写在 WHERE,created_at 由 DEFAULT 管)。
        """
        if not fields:
            return
        keys = ",".join(f"{k}=?" for k in fields)
        params = list(fields.values()) + [staff_id]
        conn = get_db()
        conn.execute(
            f"UPDATE staff SET {keys}, updated_at=datetime('now','localtime') WHERE id=?",
            params,
        )
        conn.commit()

    @staticmethod
    def delete(staff_id: int) -> None:
        """软删:is_active=0(行不删,跟 vehicles.delete 保持一致)。"""
        StaffDB.update(staff_id, is_active=0)


class TaskDB:
    """tasks 表 CRUD。"""

    @staticmethod
    def create(task_no: str, creator_id: int, **fields) -> int:
        """INSERT 一条 task,返回新行 id(不是 dict,方便做 creator_id 喂参)。"""
        cols = ["task_no", "creator_id"]
        vals = [task_no, creator_id]
        for k, v in fields.items():
            cols.append(k)
            vals.append(v)
        placeholders = ",".join("?" for _ in cols)
        sql = f"INSERT INTO tasks ({','.join(cols)}) VALUES ({placeholders})"
        conn = get_db()
        cur = conn.execute(sql, vals)
        conn.commit()
        return cur.lastrowid

    @staticmethod
    def get_by_id(task_id: int) -> Optional[dict]:
        """SELECT 一条 task,返回 dict;不存在返回 None。"""
        conn = get_db()
        row = conn.execute(
            "SELECT * FROM tasks WHERE id = ?", (task_id,)
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def get_all(include_cancelled: bool = False, status: Optional[str] = None) -> list:
        """列表任务, 默认不返回作废. status 不为空则按状态过滤."""
        sql = "SELECT * FROM tasks"
        where = []
        params = []
        if not include_cancelled:
            where.append("is_cancelled=0")
        if status:
            where.append("status=?")
            params.append(status)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC"
        return [dict(r) for r in get_db().execute(sql, params).fetchall()]

    @staticmethod
    def update(task_id: int, **fields) -> None:
        """按 fields 增量更新 task。无字段时 no-op。"""
        if not fields:
            return
        # 白名单:仅允许更新白名单字段,防止 caller 误改 id/task_no/creator_id
        # 等不可变列。
        allowed = {
            "source_order_no", "task_type", "related_task_id", "source_type",
            "customer", "dest_address", "dest_lat", "dest_lng", "dest_poi_name",
            "status", "coding_status", "driver_id", "vehicle_id", "operator_id",
            "coder_id", "coding_claimed_at", "coding_done_at",
            "est_weight_kg", "est_distance_km", "depart_at", "arrive_at",
            "remark", "is_cancelled",
        }
        safe_fields = {k: v for k, v in fields.items() if k in allowed}
        if not safe_fields:
            return
        set_clause = ",".join(f"{k}=?" for k in safe_fields.keys())
        params = list(safe_fields.values()) + [task_id]
        sql = f"UPDATE tasks SET {set_clause}, updated_at=datetime('now','localtime') WHERE id=?"
        conn = get_db()
        conn.execute(sql, params)
        conn.commit()


class TaskImageDB:
    """task_images 表 CRUD。"""

    @staticmethod
    def create(task_id: int, stage: str, image_path: str, sort_order: int = 0) -> int:
        conn = get_db()
        cur = conn.execute(
            "INSERT INTO task_images (task_id, stage, image_path, sort_order) "
            "VALUES (?, ?, ?, ?)",
            (task_id, stage, image_path, sort_order),
        )
        conn.commit()
        return cur.lastrowid

    @staticmethod
    def get_by_task(task_id: int) -> list:
        conn = get_db()
        rows = conn.execute(
            "SELECT * FROM task_images WHERE task_id = ? ORDER BY sort_order, id",
            (task_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def delete(image_id: int) -> None:
        conn = get_db()
        conn.execute("DELETE FROM task_images WHERE id=?", (image_id,))
        conn.commit()


class TaskEventDB:
    """task_events 表 CRUD。"""

    @staticmethod
    def create(task_id: int, event_type: str, **fields) -> int:
        cols = ["task_id", "event_type"]
        vals = [task_id, event_type]
        for k in ("from_status", "to_status", "operator_id", "note"):
            if k in fields:
                cols.append(k)
                vals.append(fields[k])
        placeholders = ",".join("?" for _ in cols)
        sql = f"INSERT INTO task_events ({','.join(cols)}) VALUES ({placeholders})"
        conn = get_db()
        cur = conn.execute(sql, vals)
        conn.commit()
        return cur.lastrowid

    @staticmethod
    def get_by_task(task_id: int) -> list:
        return [dict(r) for r in get_db().execute(
            "SELECT * FROM task_events WHERE task_id=? ORDER BY created_at, id", (task_id,)
        ).fetchall()]


class VehicleDB:
    """vehicles 表 CRUD。

    删除是软删:`delete(vid)` 只把 status 置为 '停用',行不删。
    默认 `get_all(include_disabled=False)` 不返回停用的,做下拉/列表时更干净;
    T7 的人员管理页下拉直接用默认行为即可。
    """

    @staticmethod
    def create(
        plate_no: str,
        tonnage=None,
        length=None,
        width=None,
        height=None,
        inspection_date=None,
        status: str = "启用",
        note=None,
    ) -> int:
        """INSERT 一条 vehicle,返回新行 id。

        plate_no 是 UNIQUE 约束,重复插入会让 sqlite3.IntegrityError 透传出去
        (上层 blueprint 接住后 flash 错误,不静默吞)。
        """
        conn = get_db()
        cur = conn.execute(
            "INSERT INTO vehicles "
            "(plate_no, tonnage, length, width, height, inspection_date, status, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (plate_no, tonnage, length, width, height, inspection_date, status, note),
        )
        conn.commit()
        return cur.lastrowid

    @staticmethod
    def get_by_id(vehicle_id: int) -> Optional[dict]:
        conn = get_db()
        row = conn.execute(
            "SELECT * FROM vehicles WHERE id = ?", (vehicle_id,)
        ).fetchone()
        return dict(row) if row else None

    @staticmethod
    def get_all(include_disabled: bool = False) -> list:
        """按 plate_no 升序返回车辆列表。默认过滤 status='停用'。"""
        conn = get_db()
        if include_disabled:
            rows = conn.execute(
                "SELECT * FROM vehicles ORDER BY plate_no"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM vehicles WHERE status = '启用' ORDER BY plate_no"
            ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def update(vehicle_id: int, **fields) -> None:
        """按 fields 增量更新 vehicle。无字段时 no-op。

        白名单字段:防止 caller 误改 id/created_at 等不可变列。
        """
        if not fields:
            return
        allowed = {
            "plate_no", "tonnage", "length", "width", "height",
            "inspection_date", "status", "note",
        }
        safe_fields = {k: v for k, v in fields.items() if k in allowed}
        if not safe_fields:
            return
        set_clause = ",".join(f"{k}=?" for k in safe_fields.keys())
        params = list(safe_fields.values()) + [vehicle_id]
        sql = f"UPDATE vehicles SET {set_clause}, updated_at=datetime('now','localtime') WHERE id=?"
        conn = get_db()
        conn.execute(sql, params)
        conn.commit()

    @staticmethod
    def delete(vehicle_id: int) -> None:
        """软删:把 status 置为 '停用',不删行。"""
        VehicleDB.update(vehicle_id, status="停用")


class TaskItemDB:
    """task_items 表 CRUD。Task 12 (整单退回) 用来拷贝明细到退货单。"""

    @staticmethod
    def create(
        task_id: int,
        product_name: str,
        quantity: float,
        unit: Optional[str] = None,
        specification: Optional[str] = None,
        remark: Optional[str] = None,
        sort_order: int = 0,
        line_type: str = "normal",
    ) -> int:
        conn = get_db()
        cur = conn.execute(
            "INSERT INTO task_items "
            "(task_id, product_name, specification, quantity, unit, remark, sort_order, line_type) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (task_id, product_name, specification, quantity, unit, remark, sort_order, line_type),
        )
        conn.commit()
        return cur.lastrowid

    @staticmethod
    def get_by_task(task_id: int) -> list:
        conn = get_db()
        rows = conn.execute(
            "SELECT * FROM task_items WHERE task_id = ? ORDER BY sort_order, id",
            (task_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    @staticmethod
    def update(item_id: int, **fields) -> None:
        if not fields:
            return
        if fields.get("unit") == "码":
            fields["unit"] = "y"
        keys = ",".join(f"{k}=?" for k in fields)
        params = list(fields.values()) + [item_id]
        conn = get_db()
        conn.execute(f"UPDATE task_items SET {keys} WHERE id=?", params)
        conn.commit()

    @staticmethod
    def delete(item_id: int) -> None:
        conn = get_db()
        conn.execute("DELETE FROM task_items WHERE id=?", (item_id,))
        conn.commit()


__all__ = [
    "Staff", "Task", "TaskItem", "TaskImage", "TaskEvent",
    "StaffDB", "TaskDB", "TaskItemDB", "TaskImageDB", "TaskEventDB",
    "VehicleDB",
]