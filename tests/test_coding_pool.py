"""Task 14 — 打码抢单池(M1 占位)视图测试。

覆盖范围:
- GET /coding-pool 仅显示 coding_status='待打码' 的任务
- GET /coding-pool 排除已作废 (is_cancelled=1) 的任务

M1 不接抢单逻辑,只验证过滤逻辑正确。
"""
import os
import sys
import unittest

# 让 tests 能 import app / models
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from app import create_app
from models.tasks_flow import StaffDB, TaskDB
from models._db import get_db


class CodingPoolViewTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.client = self.app.test_client()
        # 单连接清表(避免与 test_client 锁竞争)
        db = get_db()
        for t in ("task_events", "task_images", "task_items", "tasks", "staff", "vehicles"):
            db.execute(f"DELETE FROM {t}")
        db.commit()
        db.close()
        # T6 闸门需要登录态
        op = StaffDB.create(name="打码员", role="打码", is_active=1)
        with self.client.session_transaction() as sess:
            sess["operator_id"] = op["id"]
        self.op = op

    # ── TDD 用例 ──────────────────────────────────────────────
    def test_coding_pool_only_shows_pending(self):
        """GET /coding-pool 只显示 coding_status='待打码' 的任务,排除 '打码完成'"""
        # 一个 待打码(应显示)
        tid_pending = TaskDB.create(
            task_no="TD-CP-PENDING", creator_id=self.op["id"],
            customer="客户A", source_order_no="DOC-PENDING",
            coding_status="待打码",
        )
        # 一个 打码完成(不应显示)
        TaskDB.create(
            task_no="TD-CP-DONE", creator_id=self.op["id"],
            customer="客户B", source_order_no="DOC-DONE",
            coding_status="打码完成",
        )
        r = self.client.get("/coding-pool")
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        # 待打码应出现
        self.assertIn("TD-CP-PENDING", body, "待打码任务应在抢单池中")
        # 打码完成不应出现
        self.assertNotIn("TD-CP-DONE", body, "打码完成任务不应在抢单池中")
        # 标题在
        self.assertIn("打码抢单池", body)

    def test_coding_pool_excludes_cancelled(self):
        """GET /coding-pool 排除已作废的任务(即使 coding_status='待打码')"""
        # 一个 待打码 但作废(不应显示)
        TaskDB.create(
            task_no="TD-CP-CANCELLED", creator_id=self.op["id"],
            customer="客户C", source_order_no="DOC-CANCELLED",
            coding_status="待打码",
            is_cancelled=1,
        )
        # 一个 待打码 未作废(应显示)
        tid_active = TaskDB.create(
            task_no="TD-CP-ACTIVE", creator_id=self.op["id"],
            customer="客户D", source_order_no="DOC-ACTIVE",
            coding_status="待打码",
            is_cancelled=0,
        )
        r = self.client.get("/coding-pool")
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        # 未作废应在
        self.assertIn("TD-CP-ACTIVE", body, "未作废的待打码任务应在抢单池中")
        # 已作废不应在
        self.assertNotIn("TD-CP-CANCELLED", body, "已作废任务不应在抢单池中")


if __name__ == "__main__":
    unittest.main(verbosity=2)