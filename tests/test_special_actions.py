"""Task 12 — 司机退单 + 任务作废 + 整单退回

测试 3 个特殊动作端点:
- POST /api/v1/tasks/<tid>/driver-release   司机退单(装货前/装货后均可)
- POST /api/v1/tasks/<tid>/cancel           任务作废(仅准备中)
- POST /api/v1/tasks/<tid>/return-all       整单退回(已卸货后生成退货单)

与 brief verbatim 的偏差:
1. `StaffDB.create()` 返回 dict(含 id/name/role),所以 `creator_id=self.dispatcher`
   改为 `creator_id=self.dispatcher["id"]`(brief 这两行不一致)。
2. setUp 的 DELETE 循环用单连接 commit,避免 brief setUp 的 6 连接 bug。
3. `Task` dataclass 没有 `update`/`get_by_id`/`create`,全用 `TaskDB.*`。
   `TaskImage` 同理 → 用 `TaskImageDB.*`。
4. 新增 `TaskItemDB` dataclass 类(原文件未提供),由本测试间接通过端点验证。
"""
import os
import sys
import unittest

# 让 tests 能 import app / models
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from app import create_app
from models.tasks_flow import StaffDB, TaskDB, TaskImageDB, TaskItemDB
from models._db import get_db


class SpecialActionsTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.client = self.app.test_client()
        # 用单连接 DELETE 全部任务流相关表
        db = get_db()
        for t in ("task_events", "task_images", "task_items", "tasks", "staff", "vehicles"):
            db.execute(f"DELETE FROM {t}")
        db.commit()
        db.close()

        self.driver = StaffDB.create(name="Dr", role="司机")
        self.dispatcher = StaffDB.create(name="D", role="调度")

    def _login(self, who):
        with self.client.session_transaction() as sess:
            sess["operator_id"] = who["id"]

    def _task(self, status="准备中", driver_id=None):
        tid = TaskDB.create(task_no="TD-SP-1", creator_id=self.dispatcher["id"])
        TaskDB.update(tid, status=status, driver_id=driver_id, coding_status="打码完成")
        return tid

    def test_driver_release_preserves_coding(self):
        tid = self._task("已装货", driver_id=self.driver["id"])
        self._login(self.driver)
        # 必传退单照
        TaskImageDB.create(tid, "退单照", "2026-07/r.png")
        r = self.client.post(f"/api/v1/tasks/{tid}/driver-release")
        self.assertTrue(r.get_json()["success"], r.get_json())
        t = TaskDB.get_by_id(tid)
        self.assertEqual(t["status"], "准备中")
        self.assertIsNone(t["driver_id"])
        self.assertEqual(t["coding_status"], "打码完成")  # 保留

    def test_task_cancel_only_in_preparing(self):
        tid = self._task("准备中", driver_id=self.driver["id"])
        self._login(self.dispatcher)
        r = self.client.post(f"/api/v1/tasks/{tid}/cancel")
        self.assertTrue(r.get_json()["success"])
        self.assertEqual(TaskDB.get_by_id(tid)["is_cancelled"], 1)

    def test_return_create_generates_new_task(self):
        tid = self._task("已卸货", driver_id=self.driver["id"])
        TaskItemDB.create(tid, "P1", 100, "y")
        TaskImageDB.create(tid, "退货照", "2026-07/ret.png")
        self._login(self.driver)
        r = self.client.post(f"/api/v1/tasks/{tid}/return-all")
        self.assertTrue(r.get_json()["success"], r.get_json())
        orig = TaskDB.get_by_id(tid)
        self.assertEqual(orig["status"], "已拒收")
        new_tid = r.get_json()["new_task_id"]
        new = TaskDB.get_by_id(new_tid)
        self.assertEqual(new["task_type"], "退货")
        self.assertEqual(new["related_task_id"], tid)
        # 新单数量为负
        self.assertEqual(TaskItemDB.get_by_task(new_tid)[0]["quantity"], -100)


if __name__ == "__main__":
    unittest.main(verbosity=2)