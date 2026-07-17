"""Task 8 — 车辆管理 CRUD。

覆盖范围:
- VehicleDB.create 成功返回 id,且行可被 get_by_id 读到
- plate_no 重复 → IntegrityError(UNIQUE 约束)
- get_all(include_disabled=False) 默认隐藏 status='停用'
- delete 是软删 — 行还在,只是 status 变成 '停用'

测试清表:setUp 用单连接 commit,跟 tests/test_state_machine.py 一致。
"""
import os
import sys
import unittest

# 让 tests 能 import app / models
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from app import create_app
from models.tasks_flow import VehicleDB, StaffDB
from models._db import get_db


class VehiclesCrudTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.client = self.app.test_client()
        # 清表(单连接,确保所有 DELETE 在同一事务内)
        db = get_db()
        for t in ("task_events", "task_images", "task_items", "tasks", "staff", "vehicles"):
            db.execute(f"DELETE FROM {t}")
        db.commit()
        db.close()

        # T6 闸门需要登录态 — 装一个占位 operator
        op = StaffDB.create(name="veh-tester", role="文员", is_active=1)
        with self.client.session_transaction() as sess:
            sess["operator_id"] = op["id"]

    # ── 模型层 ──────────────────────────────────────────────
    def test_create_vehicle(self):
        """create 返回 id,get_by_id 能读到刚插入的行"""
        vid = VehicleDB.create(
            plate_no="粤B-T8-001",
            tonnage=5.0,
            length=4.2,
            width=1.8,
            height=1.9,
            inspection_date="2026-12-31",
            note="测试车",
        )
        self.assertIsInstance(vid, int)
        row = VehicleDB.get_by_id(vid)
        self.assertIsNotNone(row)
        self.assertEqual(row["plate_no"], "粤B-T8-001")
        self.assertEqual(row["tonnage"], 5.0)
        self.assertEqual(row["length"], 4.2)
        self.assertEqual(row["width"], 1.8)
        self.assertEqual(row["height"], 1.9)
        self.assertEqual(row["inspection_date"], "2026-12-31")
        self.assertEqual(row["status"], "启用")
        self.assertEqual(row["note"], "测试车")

    def test_unique_plate(self):
        """plate_no UNIQUE 约束 — 同号插入第二次应抛 IntegrityError"""
        import sqlite3
        VehicleDB.create(plate_no="粤B-DUP-01")
        with self.assertRaises(sqlite3.IntegrityError):
            VehicleDB.create(plate_no="粤B-DUP-01")

    def test_list_with_status_filter(self):
        """get_all 默认不返回停用的;include_disabled=True 才全返回"""
        v1 = VehicleDB.create(plate_no="粤B-A001")
        v2 = VehicleDB.create(plate_no="粤B-A002")
        v3 = VehicleDB.create(plate_no="粤B-A003")
        # 停用 v2
        VehicleDB.delete(v2)

        active_only = VehicleDB.get_all()
        all_v = VehicleDB.get_all(include_disabled=True)

        ids_active = {v["id"] for v in active_only}
        ids_all = {v["id"] for v in all_v}

        self.assertIn(v1, ids_active)
        self.assertNotIn(v2, ids_active)
        self.assertIn(v3, ids_active)
        self.assertEqual(ids_all, {v1, v2, v3})

    def test_soft_delete(self):
        """delete 是软删 — 行还在,只是 status='停用'"""
        vid = VehicleDB.create(plate_no="粤B-SOFT-01")
        # 删前
        row = VehicleDB.get_by_id(vid)
        self.assertEqual(row["status"], "启用")
        # 删
        VehicleDB.delete(vid)
        # 删后 — get_by_id 仍能找到(行没删)
        row_after = VehicleDB.get_by_id(vid)
        self.assertIsNotNone(row_after, "软删后行不应被物理删除")
        self.assertEqual(row_after["status"], "停用")
        # 默认列表里也找不到了
        active = VehicleDB.get_all()
        self.assertFalse(any(v["id"] == vid for v in active))


if __name__ == "__main__":
    unittest.main(verbosity=2)