"""Task 9c — 任务流前端模板渲染测试。

覆盖范围:
- GET /tasks 返回 200 + 含"任务列表"+ 默认 tab=全部
- GET /tasks?tab=in_progress 仅返回进行中状态的任务
- GET /tasks/<id> 返回 200 + 含 7 状态进度条
- GET /tasks/new 返回 200 + 含 OCR 弹框 include(smartFillModal)
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


class TasksViewTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.client = self.app.test_client()
        # 单连接清表(避免与 test_client 锁竞争 — 已在 E2E 踩过这个坑)
        db = get_db()
        for t in ("task_events", "task_images", "task_items", "tasks", "staff", "vehicles"):
            db.execute(f"DELETE FROM {t}")
        db.commit()
        db.close()
        # T6 闸门需要登录态
        op = StaffDB.create(name="调度员", role="调度", is_active=1)
        with self.client.session_transaction() as sess:
            sess["operator_id"] = op["id"]
        self.op = op

    # ── TDD 用例 ──────────────────────────────────────────────
    def test_tasks_list_renders(self):
        """GET /tasks 返回 200 + 含任务列表标题,默认 tab=全部"""
        # 空表也返回 200(无任务时显示 empty state)
        r = self.client.get("/tasks")
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn("任务列表", body)
        # tab 切换链接存在
        self.assertIn("全部", body)
        self.assertIn("待接单", body)
        self.assertIn("进行中", body)
        self.assertIn("已完成", body)
        self.assertIn("已退单", body)
        # 新建任务按钮
        self.assertIn("新建任务", body)

    def test_tasks_list_tab_filter(self):
        """?tab=in_progress 只返回进行中状态的任务(已装货/已点数/已到达)"""
        # 准备多种状态
        TaskDB.create(task_no="TD-001", creator_id=self.op["id"], status="准备中")
        TaskDB.create(task_no="TD-002", creator_id=self.op["id"], status="已装货")
        TaskDB.create(task_no="TD-003", creator_id=self.op["id"], status="已点数")
        TaskDB.create(task_no="TD-004", creator_id=self.op["id"], status="已到达")
        TaskDB.create(task_no="TD-005", creator_id=self.op["id"], status="已卸货")
        TaskDB.create(task_no="TD-006", creator_id=self.op["id"], status="已完成")

        r = self.client.get("/tasks?tab=in_progress")
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        # 进行中 3 个
        self.assertIn("TD-002", body, "已装货应在 in_progress tab 中")
        self.assertIn("TD-003", body, "已点数应在 in_progress tab 中")
        self.assertIn("TD-004", body, "已到达应在 in_progress tab 中")
        # 非进行中 不在
        self.assertNotIn("TD-001", body, "准备中不应在 in_progress tab 中")
        self.assertNotIn("TD-005", body, "已卸货不应在 in_progress tab 中")
        self.assertNotIn("TD-006", body, "已完成不应在 in_progress tab 中")

    def test_task_detail_renders(self):
        """GET /tasks/<id> 返回 200 + 含 7 状态进度条"""
        tid = TaskDB.create(
            task_no="TD-DETAIL-001", creator_id=self.op["id"],
            customer="AC公司", source_order_no="DOC-001",
            status="已装货",
        )
        r = self.client.get(f"/tasks/{tid}")
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        # 进度条
        self.assertIn("task-progress", body, "应包含进度条容器")
        # 7 个状态文字(6 主态 + 失败态可选;已装货 是中间态,失败态不渲染)
        for stage in ["准备中", "已装货", "已点数", "已到达", "已卸货", "已完成"]:
            self.assertIn(stage, body)
        # 头部信息
        self.assertIn("AC公司", body)
        self.assertIn("DOC-001", body)
        self.assertIn("TD-DETAIL-001", body)

    def test_tasks_new_renders(self):
        """GET /tasks/new 返回 200 + 含 OCR 弹框 include(smartFillModal)"""
        r = self.client.get("/tasks/new")
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        # 新建任务页标题
        self.assertIn("新建任务", body)
        # OCR 弹框 modal 元素(从 _smart_add_modal.html include 进来)
        self.assertIn("smartFillModal", body)
        self.assertIn("aiEngine", body)
        self.assertIn("aiResultBody", body)
        # 主表单输入
        self.assertIn("customerInput", body)
        self.assertIn("sourceOrderNoInput", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)