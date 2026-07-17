"""Task 7 — 人员管理 CRUD。

覆盖范围:
- StaffDB.create 返回 dict(含 id/name/role)
- StaffDB.get_all 默认只返回 is_active=1
- StaffDB.get_all(role='司机') 只返回该角色
- StaffDB.get_all(include_inactive=True) 包含 is_active=0
- StaffDB.update 修改字段成功,id 不变
- StaffDB.delete 是软删 — is_active=0,行还在
- staff.vehicle_id FK 关联正常

测试清表:用单连接 commit,跟 test_vehicles_crud.py 一致。
"""
import os
import sys
import unittest

# 让 tests 能 import app / models
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from app import create_app
from models.tasks_flow import StaffDB, VehicleDB
from models._db import get_db


class StaffCrudTest(unittest.TestCase):
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
        op = StaffDB.create(name="staff-tester", role="文员", is_active=1)
        with self.client.session_transaction() as sess:
            sess["operator_id"] = op["id"]

    # ── TDD 用例 ──────────────────────────────────────────────
    def test_create_staff(self):
        """create 返回 dict(含 id/name/role),get_by_id 可读"""
        row = StaffDB.create(name="张三", role="司机", phone="13800000000")
        self.assertIsInstance(row, dict)
        self.assertEqual(row["name"], "张三")
        self.assertEqual(row["role"], "司机")
        self.assertEqual(row["phone"], "13800000000")
        self.assertEqual(row["is_active"], 1)
        # 默认字段
        self.assertIsNone(row.get("vehicle_id"))
        # get_by_id 也能找到
        same = StaffDB.get_by_id(row["id"])
        self.assertEqual(same["name"], "张三")

    def test_get_all_with_role_filter(self):
        """get_all 默认只返回 active;role='司机' 只返回司机"""
        StaffDB.create(name="A司机", role="司机")
        StaffDB.create(name="B调度", role="调度")
        StaffDB.create(name="C司机", role="司机")
        StaffDB.create(name="D文员", role="文员")

        # 默认只看 active — 5 个都 active(含 setUp 装的 operator 文员),全返回
        all_active = StaffDB.get_all()
        self.assertEqual(len(all_active), 5)

        # role 过滤
        drivers = StaffDB.get_all(role="司机")
        names = {s["name"] for s in drivers}
        self.assertEqual(names, {"A司机", "C司机"})

        dispatchers = StaffDB.get_all(role="调度")
        self.assertEqual({s["name"] for s in dispatchers}, {"B调度"})

        # 不存在的角色 — 空列表
        empty_role = StaffDB.get_all(role="不存在")
        self.assertEqual(empty_role, [])

    def test_soft_delete_marks_inactive(self):
        """delete 是软删 — is_active=0,行还在,get_by_id 仍能找到"""
        row = StaffDB.create(name="E司机", role="司机")
        sid = row["id"]
        # 删前
        self.assertEqual(StaffDB.get_by_id(sid)["is_active"], 1)
        # 软删
        StaffDB.delete(sid)
        # 行还在,只是 is_active=0
        after = StaffDB.get_by_id(sid)
        self.assertIsNotNone(after, "软删后行不应被物理删除")
        self.assertEqual(after["is_active"], 0)
        # 默认列表里没了
        active = StaffDB.get_all()
        self.assertFalse(any(s["id"] == sid for s in active))
        # include_inactive=True 能找到
        all_staff = StaffDB.get_all(include_inactive=True)
        self.assertTrue(any(s["id"] == sid for s in all_staff))

    def test_vehicle_id_assignment(self):
        """staff.vehicle_id FK 关联 — 分配后 get_by_id 能读出 vehicle_id"""
        vid = VehicleDB.create(plate_no="粤B-STAFF-01", tonnage=3.5)
        row = StaffDB.create(name="F司机", role="司机", vehicle_id=vid)
        self.assertEqual(row["vehicle_id"], vid)

        # 通过 get_by_id 再读,FK 值正确
        same = StaffDB.get_by_id(row["id"])
        self.assertEqual(same["vehicle_id"], vid)

        # update 也能改 vehicle_id(比如换车)
        vid2 = VehicleDB.create(plate_no="粤B-STAFF-02")
        StaffDB.update(row["id"], vehicle_id=vid2)
        after = StaffDB.get_by_id(row["id"])
        self.assertEqual(after["vehicle_id"], vid2)


if __name__ == "__main__":
    unittest.main(verbosity=2)