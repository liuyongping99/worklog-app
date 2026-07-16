"""Task 10 — 状态机 + 闸门

测试 advance 端点的状态推进、装货前置条件、证据闸门与权限校验。

注意与 brief verbatim 的两处偏差:
1. `Staff.create` 返回 dict(含 id/name/role),所以 `creator_id=self.dispatcher`
   改为 `creator_id=self.dispatcher["id"]`(原 brief 这两行不一致:
   一个当 int 用、一个当 dict 用,显然是 brief 的笔误,这里统一按 dict 处理)。
2. setUp 的 DELETE 循环用单连接 commit。原 brief 每轮调一次 get_db()
   会产生 6 个独立连接,每次连接 GC 时未 commit 的事务被回滚,导致只有
   最后一张表的 DELETE 生效。这是 brief 的 setUp 写法 bug,不影响测试用例
   的逻辑,改成本文件的实现。
"""
import os
import sys
import unittest

# 让 tests 能 import app / models
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from app import create_app
from models.tasks_flow import StaffDB, TaskDB, TaskImageDB
from models._db import get_db


class StateMachineTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.client = self.app.test_client()
        # 用单连接,确保所有 DELETE 在同一事务内,最后 commit 一次。
        db = get_db()
        for t in ("task_events", "task_images", "task_items", "tasks", "staff", "vehicles"):
            db.execute(f"DELETE FROM {t}")
        db.commit()
        db.close()

        self.dispatcher = StaffDB.create(name="D", role="调度")
        self.driver = StaffDB.create(name="Dr", role="司机")
        # 装一个最小 vehicle 以满足 FK(test 不直接用,但保险起见)
        db = get_db()
        db.execute("INSERT INTO vehicles (plate_no) VALUES (?)", ("TEST-001",))
        db.commit()
        db.close()

        with self.client.session_transaction() as sess:
            sess["operator_id"] = self.dispatcher["id"]

    def _new_task(self, status="准备中", coding="无需打码", driver_id=None):
        tid = TaskDB.create(
            task_no=f"TD-{status}-{os.urandom(2).hex()}",
            creator_id=self.dispatcher["id"],
        )
        TaskDB.update(tid, status=status, coding_status=coding, driver_id=driver_id)
        return tid

    def test_cannot_advance_to_loaded_without_driver(self):
        tid = self._new_task("准备中", "无需打码", driver_id=None)
        r = self.client.post(f"/api/v1/tasks/{tid}/advance", json={"to_status": "已装货"})
        self.assertFalse(r.get_json()["success"])

    def test_cannot_advance_to_loaded_with_coding_pending(self):
        tid = self._new_task("准备中", "待打码", driver_id=self.driver["id"])
        r = self.client.post(f"/api/v1/tasks/{tid}/advance", json={"to_status": "已装货"})
        self.assertFalse(r.get_json()["success"])

    def test_cannot_advance_to_counted_without_loading_photo(self):
        tid = self._new_task("已装货", "无需打码", driver_id=self.driver["id"])
        r = self.client.post(f"/api/v1/tasks/{tid}/advance", json={"to_status": "已点数"})
        self.assertFalse(r.get_json()["success"])

    def test_full_flow_with_photos_succeeds(self):
        tid = self._new_task("准备中", "无需打码", driver_id=self.driver["id"])
        # 司机本人推进装货(advance 的 Action.LOAD 要求 op.id == driver_id)
        with self.client.session_transaction() as sess:
            sess["operator_id"] = self.driver["id"]
        # 上传装车照
        TaskImageDB.create(tid, "装车照", "2026-07/loading.png")
        r = self.client.post(f"/api/v1/tasks/{tid}/advance", json={"to_status": "已装货"})
        self.assertTrue(r.get_json()["success"], r.get_json())
        # 点数:文员操作,需点数标签照
        TaskImageDB.create(tid, "点数标签照", "2026-07/count.png")
        clerk = StaffDB.create(name="C", role="文员")
        with self.client.session_transaction() as sess:
            sess["operator_id"] = clerk["id"]
        r = self.client.post(f"/api/v1/tasks/{tid}/advance", json={"to_status": "已点数"})
        self.assertTrue(r.get_json()["success"], r.get_json())


if __name__ == "__main__":
    unittest.main(verbosity=2)