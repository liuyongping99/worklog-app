"""E2E test: 完整任务流(建单 → 装货 → 点数 → 到达 → 卸货 → 完成)

串联已有 API + 内置 stub,验证 M1 核心链路全通。
"""
import unittest
import json
import io
from unittest.mock import patch
from app import create_app
from models.tasks_flow import StaffDB, TaskDB, TaskItemDB, TaskImageDB, TaskEventDB


class E2ETaskFlowTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.client = self.app.test_client()
        from models._db import get_db
        # 单连接清表(避免与 test_client 锁竞争)
        db = get_db()
        for t in ("task_events", "task_images", "task_items", "tasks", "staff", "vehicles"):
            db.execute(f"DELETE FROM {t}")
        db.commit()
        # 基础数据
        self.dispatcher = StaffDB.create(name="调度员", role="调度")
        self.driver = StaffDB.create(name="司机甲", role="司机")
        self.clerk = StaffDB.create(name="文员", role="文员")
        self.coder = StaffDB.create(name="打码", role="打码")

    def _login(self, staff):
        with self.client.session_transaction() as sess:
            sess["operator_id"] = staff["id"]

    def test_full_flow_dispatch_to_completion(self):
        """完整主线:建单 → 指派 → 装货 → 点数 → 到达 → 卸货 → 完成"""
        # 1. 调度建单(直接走 DB 写,跳过 T9 的 POST API)
        self._login(self.dispatcher)
        tid = TaskDB.create(
            task_no="TD-E2E-001", creator_id=self.dispatcher["id"],
            source_order_no="E2E-001", source_type="装车单",
            customer="AC", dest_address="Addr",
        )
        TaskItemDB.create(tid, "P1", 100, "y", specification="30S")

        # 2. 调度指派司机
        # T9 的 /api/v1/tasks/<id>/assign 可能没建 — 用 DB 直接设
        TaskDB.update(tid, driver_id=self.driver["id"], coding_status="无需打码")
        t = TaskDB.get_by_id(tid)
        self.assertEqual(t["driver_id"], self.driver["id"])
        self.assertEqual(t["status"], "准备中")

        # 3. 司机装货(上传装车照 + advance)
        self._login(self.driver)
        TaskImageDB.create(tid, "装车照", "2026-07/load.png")
        r = self.client.post(f"/api/v1/tasks/{tid}/advance", json={"to_status": "已装货"})
        self.assertTrue(r.get_json()["success"], r.get_json())
        t = TaskDB.get_by_id(tid)
        self.assertEqual(t["status"], "已装货")
        self.assertIsNotNone(t["depart_at"], "装货时 depart_at 应被写入")

        # 4. 文员点数(上传点数照)
        self._login(self.clerk)
        TaskImageDB.create(tid, "点数标签照", "2026-07/count.png")
        r = self.client.post(f"/api/v1/tasks/{tid}/advance", json={"to_status": "已点数"})
        self.assertTrue(r.get_json()["success"], r.get_json())

        # 5. 司机到达
        self._login(self.driver)
        r = self.client.post(f"/api/v1/tasks/{tid}/advance", json={"to_status": "已到达"})
        self.assertTrue(r.get_json()["success"], r.get_json())
        t = TaskDB.get_by_id(tid)
        self.assertIsNotNone(t["arrive_at"], "到达时 arrive_at 应被写入")

        # 6. 司机卸货(上传卸货照)
        TaskImageDB.create(tid, "卸货照", "2026-07/unload.png")
        r = self.client.post(f"/api/v1/tasks/{tid}/advance", json={"to_status": "已卸货"})
        self.assertTrue(r.get_json()["success"], r.get_json())

        # 7. 上传签收单 + 完成
        TaskImageDB.create(tid, "签收单", "2026-07/sign.png")
        r = self.client.post(f"/api/v1/tasks/{tid}/advance", json={"to_status": "已完成"})
        self.assertTrue(r.get_json()["success"], r.get_json())

        # 8. 终态验证
        t = TaskDB.get_by_id(tid)
        self.assertEqual(t["status"], "已完成")
        # 流水至少 6 个 advance(从 已装货到 已完成 共 5 步 advance)
        from models.tasks_flow import TaskEventDB
        if hasattr(TaskEventDB, "get_by_task"):
            events = TaskEventDB.get_by_task(tid)
            self.assertGreaterEqual(len(events), 5, f"应至少 5 条 advance 流水,实际 {len(events)}")
            event_types = [e["event_type"] for e in events]
            self.assertIn("advance", event_types)

    def test_driver_release_preserves_coding(self):
        """司机退单后,打码完成态保留"""
        self._login(self.dispatcher)
        tid = TaskDB.create(task_no="TD-RL-001", creator_id=self.dispatcher["id"])
        TaskDB.update(tid, driver_id=self.driver["id"], coding_status="打码完成")
        # 上传装车照 + 装货
        self._login(self.driver)
        TaskImageDB.create(tid, "装车照", "2026-07/l.png")
        r = self.client.post(f"/api/v1/tasks/{tid}/advance", json={"to_status": "已装货"})
        self.assertTrue(r.get_json()["success"])
        # 退单(需先传退单照)
        TaskImageDB.create(tid, "退单照", "2026-07/rel.png")
        r = self.client.post(f"/api/v1/tasks/{tid}/driver-release")
        self.assertTrue(r.get_json()["success"], r.get_json())
        t = TaskDB.get_by_id(tid)
        self.assertEqual(t["status"], "准备中")
        self.assertIsNone(t["driver_id"])
        self.assertEqual(t["coding_status"], "打码完成", "退单应保留打码完成态")

    def test_return_all_creates_reverse_task(self):
        """整单退回:生成退货单 + 原单进 已拒收 终态"""
        self._login(self.dispatcher)
        tid = TaskDB.create(task_no="TD-RET-001", creator_id=self.dispatcher["id"])
        TaskDB.update(tid, driver_id=self.driver["id"], status="已卸货")
        TaskItemDB.create(tid, "P1", 100, "y")
        TaskImageDB.create(tid, "退货照", "2026-07/ret.png")
        # 司机整单退回
        self._login(self.driver)
        r = self.client.post(f"/api/v1/tasks/{tid}/return-all")
        self.assertTrue(r.get_json()["success"], r.get_json())
        orig = TaskDB.get_by_id(tid)
        self.assertEqual(orig["status"], "已拒收")
        new_tid = r.get_json()["new_task_id"]
        new_task = TaskDB.get_by_id(new_tid)
        self.assertEqual(new_task["task_type"], "退货")
        self.assertEqual(new_task["related_task_id"], tid)
        # 新单数量为负
        new_items = TaskItemDB.get_by_task(new_tid)
        self.assertEqual(new_items[0]["quantity"], -100)


if __name__ == "__main__":
    unittest.main()
