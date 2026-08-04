"""任务图片 DELETE 端点测试(M1 Fix 2)。

覆盖范围:
- DELETE 不登录 → 401
- DELETE 不存在的图片 → 404
- DELETE 存在图片 → DB 行删除 + 文件删除(走真实 upload 流程)
"""
import io
import os
import sys
import unittest

# 让 tests 能 import app / models
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from app import create_app
from models.tasks_flow import StaffDB, TaskDB, TaskImageDB
from models._db import get_db


from tests import fake_png_bytes as _MINIMAL_PNG  # 2026-08-04:用 Pillow 真生成,过 verify 校验


class TaskImagesDeleteTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.client = self.app.test_client()
        # 单连接清表
        db = get_db()
        for t in ("task_events", "task_images", "task_items", "tasks", "staff", "vehicles"):
            db.execute(f"DELETE FROM {t}")
        db.commit()
        db.close()
        # 基础数据:调度 + 任务
        self.dispatcher = StaffDB.create(name="D", role="调度")
        self.tid = TaskDB.create(task_no="TD-DEL-IMG", creator_id=self.dispatcher["id"])

    def _login(self, who):
        with self.client.session_transaction() as sess:
            sess["operator_id"] = who["id"]

    def test_delete_requires_login(self):
        """未登录 → 401"""
        iid = TaskImageDB.create(self.tid, "装车照", "2026-07/fake.jpg")
        r = self.client.delete(f"/api/v1/tasks/images/{iid}")
        self.assertEqual(r.status_code, 401)
        self.assertFalse(r.get_json()["success"])

    def test_delete_nonexistent_image_returns_404(self):
        """删除不存在的图片 → 404"""
        self._login(self.dispatcher)
        r = self.client.delete("/api/v1/tasks/images/99999")
        self.assertEqual(r.status_code, 404)
        self.assertFalse(r.get_json()["success"])

    def test_delete_existing_image_removes_db_row(self):
        """删除 → DB 行被删 + 文件不存在时仍正常(OSError 被吞)"""
        iid = TaskImageDB.create(self.tid, "装车照", "2026-07/never_existed.jpg")
        self._login(self.dispatcher)
        r = self.client.delete(f"/api/v1/tasks/images/{iid}")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        self.assertTrue(r.get_json()["success"])
        self.assertEqual(len(TaskImageDB.get_by_task(self.tid)), 0)

    def test_delete_after_real_upload_removes_disk_file(self):
        """通过真实 upload 接口写文件 + DELETE 应同时清理磁盘文件"""
        from blueprints._helpers import BASE_DIR
        self._login(self.dispatcher)
        # 1) 上传图片(走 multipart)
        r = self.client.post(
            f"/api/v1/tasks/{self.tid}/images",
            data={"stage": "装车照", "image": (io.BytesIO(_MINIMAL_PNG()), "test.png")},
            content_type="multipart/form-data",
        )
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        rel = r.get_json()["image"]  # "2026-07/task_xxx.png"
        iid = r.get_json()["image_id"]
        # 文件应已落盘:BASE_DIR/upload/<rel>
        full_path = os.path.join(BASE_DIR, "upload", rel)
        self.assertTrue(os.path.exists(full_path), f"上传后文件应存在:{full_path}")

        # 2) DELETE
        r = self.client.delete(f"/api/v1/tasks/images/{iid}")
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True))
        # DB 行 + 文件都被删
        self.assertEqual(len(TaskImageDB.get_by_task(self.tid)), 0)
        self.assertFalse(os.path.exists(full_path), f"DELETE 后文件应被删:{full_path}")


if __name__ == "__main__":
    unittest.main(verbosity=2)