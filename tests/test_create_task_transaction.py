"""create_task 事务 + _clean 测试(M1 Fix 4 + Fix 7)。

覆盖:
- 正常创建:task + items + event 都在事务内,任一失败全部 rollback。
- 空 source_order_no → 不再触发 UNIQUE 约束冲突(被 _clean 转 None)。
- 重复 source_order_no → 400。
- 商品缺 product_name/quantity → 500 + rollback(不留孤儿 task)。

只通过 POST /api/v1/tasks 端点验证,确保事务边界正确。
"""
import os
import sys
import unittest

# 让 tests 能 import app / models
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from app import create_app
from models.tasks_flow import StaffDB, TaskDB, TaskItemDB, TaskEventDB
from models._db import get_db


class CreateTaskTransactionTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.client = self.app.test_client()
        # 单连接清表
        db = get_db()
        for t in ("task_events", "task_images", "task_items", "tasks", "staff", "vehicles"):
            db.execute(f"DELETE FROM {t}")
        db.commit()
        db.close()
        self.dispatcher = StaffDB.create(name="调度员", role="调度")
        with self.client.session_transaction() as sess:
            sess["operator_id"] = self.dispatcher["id"]

    def _post_task(self, body):
        return self.client.post("/api/v1/tasks", json=body)

    def test_create_task_normal_flow(self):
        """正常建单:task + items + event 都落库"""
        r = self._post_task({
            "source_order_no": "SO-001",
            "customer": "AC公司",
            "items": [
                {"product_name": "P1", "quantity": 100, "unit": "y"},
                {"product_name": "P2", "quantity": 50, "unit": "y"},
            ],
        })
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        body = r.get_json()
        self.assertTrue(body["success"])
        tid = body["task_id"]

        # task 行存在
        t = TaskDB.get_by_id(tid)
        self.assertIsNotNone(t)
        self.assertEqual(t["customer"], "AC公司")
        self.assertEqual(t["source_order_no"], "SO-001")
        # items 落库
        items = TaskItemDB.get_by_task(tid)
        self.assertEqual(len(items), 2)
        # event 落库(至少一条 'assign')
        events = TaskEventDB.get_by_task(tid)
        self.assertGreaterEqual(len(events), 1)
        self.assertIn("assign", [e["event_type"] for e in events])

    def test_create_task_empty_source_order_no_normalized_to_none(self):
        """Fix 7:source_order_no 空串 → None,不触发 UNIQUE 约束冲突。"""
        # 第一个空串
        r = self._post_task({
            "source_order_no": "",
            "customer": "AC",
            "items": [{"product_name": "P1", "quantity": 10}],
        })
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        # 第二个也空串(不应冲突,因为都归一化成了 None,而 UNIQUE 不约束 NULL)
        r = self._post_task({
            "source_order_no": "",
            "customer": "AC",
            "items": [{"product_name": "P2", "quantity": 20}],
        })
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))

        # 验证两单的 source_order_no 都是 NULL
        tasks = TaskDB.get_all()
        self.assertEqual(len(tasks), 2)
        for t in tasks:
            self.assertIsNone(t["source_order_no"], "空串应被规范化为 None")

    def test_create_task_duplicate_source_order_no_returns_400(self):
        """真正的重复 source_order_no(非空) → 400,不留下半截事务。"""
        body = {
            "source_order_no": "SO-DUP",
            "customer": "AC",
            "items": [{"product_name": "P1", "quantity": 10}],
        }
        r = self._post_task(body)
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        # 第二次同 source_order_no
        r = self._post_task(body)
        self.assertEqual(r.status_code, 400, r.get_data(as_text=True))
        # 数据库里只有第一单(没有半截事务残留)
        tasks = TaskDB.get_all()
        self.assertEqual(len(tasks), 1)

    def test_create_task_rollback_on_bad_item(self):
        """items 缺必填 quantity → 整个事务 rollback,不留孤儿 task。"""
        before_count = len(TaskDB.get_all())
        # quantity 缺失 → SQLite NOT NULL 失败 → 异常 → rollback
        r = self._post_task({
            "source_order_no": "SO-FAIL",
            "customer": "AC",
            "items": [{"product_name": "P1"}],  # 没 quantity
        })
        # 失败路径应当 500
        self.assertEqual(r.status_code, 500, r.get_data(as_text=True))
        # 没有新 task 落库
        self.assertEqual(len(TaskDB.get_all()), before_count)

    def test_create_task_empty_customer_normalized_to_none(self):
        """空 customer → None(不进库)。"""
        r = self._post_task({
            "source_order_no": "SO-NULL-C",
            "customer": "   ",  # 全空白
            "items": [{"product_name": "P1", "quantity": 10}],
        })
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        tid = r.get_json()["task_id"]
        t = TaskDB.get_by_id(tid)
        self.assertIsNone(t["customer"])


if __name__ == "__main__":
    unittest.main(verbosity=2)