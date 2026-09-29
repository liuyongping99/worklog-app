# -*- coding: utf-8 -*-
"""独立点数工具的模型 + API 端到端测试。

覆盖:
- 模型层:PointCountSession / PointCountImage 创建、计数、撤销、总数自动刷新
- API 层:创建会话、上传图片、加 mark、撤销、缩放、散码、完结、重开、删除、导出
- 追溯:每次 add_mark 后 session.total_count 准确;export 含 marks x/y/seq

测试清表策略:删除测试创建的 session,避免影响其它测试。
"""
import os
import sys
import base64
import unittest

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from app import create_app
from models import PointCountSession, PointCountImage
from models.tasks_flow import StaffDB
from models._db import get_db

# 1x1 PNG,做最小可用图像(避免磁盘写真实文件)
_TINY_PNG = base64.b64decode(
    b"iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
)

def _table_cleanup(db):
    db.execute("DELETE FROM point_count_marks")
    db.execute("DELETE FROM point_count_images")
    db.execute("DELETE FROM point_count_sessions")
    db.commit()


class PointCountModelTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.client = self.app.test_client()
        db = get_db()
        _table_cleanup(db)
        db.close()
        op = StaffDB.create(name="pc-tester", role="仓管", is_active=1)
        with self.client.session_transaction() as sess:
            sess["operator_id"] = op["id"]

    def test_session_crud(self):
        sid = PointCountSession.create(user_id=1, title="测试", expected_count=10, unit="支", remark="备注")
        s = PointCountSession.get_by_id(sid)
        self.assertEqual(s["title"], "测试")
        self.assertEqual(s["expected_count"], 10)
        self.assertEqual(s["status"], "open")
        self.assertEqual(s["total_count"], 0)

        PointCountSession.update_meta(sid, title="改后的标题", expected_count=20, unit="卷")
        s3 = PointCountSession.get_by_id(sid)
        self.assertEqual(s3["title"], "改后的标题")
        self.assertEqual(s3["expected_count"], 20)
        self.assertEqual(s3["unit"], "卷")

        PointCountSession.close(sid)
        s4 = PointCountSession.get_by_id(sid)
        self.assertEqual(s4["status"], "closed")
        self.assertIsNotNone(s4["closed_at"])

        PointCountSession.reopen(sid)
        s5 = PointCountSession.get_by_id(sid)
        self.assertEqual(s5["status"], "open")
        self.assertIsNone(s5["closed_at"])

        sessions = PointCountSession.list_all(limit=10)
        self.assertTrue(any(x["id"] == sid for x in sessions))

    def test_mark_total_refresh(self):
        sid = PointCountSession.create(user_id=1, title="加mark测试")
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tf:
            tf.write(_TINY_PNG)
            img_path = tf.name
        iid = PointCountImage.create(sid, img_path, "tiny.png")
        self.assertIsNotNone(iid)
        for i in range(5):
            PointCountImage.add_mark(iid, 0.1 * i, 0.2)
        s1 = PointCountSession.get_by_id(sid)
        self.assertEqual(s1["total_count"], 5)
        PointCountImage.delete_last_mark(iid)
        s2 = PointCountSession.get_by_id(sid)
        self.assertEqual(s2["total_count"], 4)
        PointCountImage.set_loose_count(iid, 3)
        s3 = PointCountSession.get_by_id(sid)
        self.assertEqual(s3["total_count"], 4 + 3)
        PointCountImage.delete(iid)
        s4 = PointCountSession.get_by_id(sid)
        self.assertEqual(s4["total_count"], 0)
        PointCountSession.delete(sid)
        self.assertIsNone(PointCountSession.get_by_id(sid))


class PointCountApiTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.client = self.app.test_client()
        db = get_db()
        _table_cleanup(db)
        db.close()
        op = StaffDB.create(name="pc-tester", role="仓管", is_active=1)
        with self.client.session_transaction() as sess:
            sess["operator_id"] = op["id"]

    def _create_session(self, **kw):
        payload = {"title": "API测试", "expected_count": 5, "unit": "支", "remark": "auto"}
        payload.update(kw)
        r = self.client.post("/api/v1/point-count/sessions", json=payload)
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        return r.get_json()["session_id"]

    def test_create_session_api(self):
        sid = self._create_session()
        s = PointCountSession.get_by_id(sid)
        self.assertEqual(s["title"], "API测试")
        self.assertEqual(s["expected_count"], 5)
        self.assertEqual(s["unit"], "支")

    def test_upload_image_add_mark_undo_loose_export(self):
        sid = self._create_session()
        img_b64 = base64.b64encode(_TINY_PNG).decode("ascii")
        data_url = "data:image/png;base64," + img_b64
        r = self.client.post(
            f"/api/v1/point-count/sessions/{sid}/images",
            json={"image": data_url},
        )
        self.assertEqual(r.status_code, 201, r.get_data(as_text=True))
        iid = r.get_json()["image_id"]
        r2 = self.client.post(
            f"/api/v1/point-count/images/{iid}/marks",
            json={"x_ratio": 0.1, "y_ratio": 0.2},
        )
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r2.get_json()["n_marks"], 1)
        self.client.post(f"/api/v1/point-count/images/{iid}/marks", json={"x_ratio": 0.3, "y_ratio": 0.4})
        self.client.post(f"/api/v1/point-count/images/{iid}/marks", json={"x_ratio": 0.5, "y_ratio": 0.6})
        r3 = self.client.delete(f"/api/v1/point-count/images/{iid}/marks/last")
        self.assertEqual(r3.get_json()["n_marks"], 2)
        r4 = self.client.post(f"/api/v1/point-count/images/{iid}/mark-scale", json={"scale": 1.5})
        self.assertEqual(r4.get_json()["scale"], 1.5)
        r5 = self.client.post(f"/api/v1/point-count/images/{iid}/loose-count", json={"count": 7})
        self.assertEqual(r5.get_json()["count"], 7)
        sess = PointCountSession.get_by_id(sid)
        self.assertEqual(sess["total_count"], 9)
        r6 = self.client.get(f"/api/v1/point-count/sessions/{sid}/export")
        data = r6.get_json()["export"]
        self.assertEqual(data["session"]["id"], sid)
        self.assertEqual(len(data["images"]), 1)
        self.assertEqual(len(data["images"][0]["marks"]), 2)
        self.assertEqual(data["images"][0]["loose_count"], 7)
        r7 = self.client.post(f"/api/v1/point-count/sessions/{sid}/close")
        self.assertTrue(r7.get_json()["success"])
        self.assertEqual(PointCountSession.get_by_id(sid)["status"], "closed")
        r8 = self.client.post(
            f"/api/v1/point-count/sessions/{sid}/images",
            json={"image": data_url},
        )
        self.assertEqual(r8.status_code, 400)
        self.client.post(f"/api/v1/point-count/sessions/{sid}/reopen")
        self.assertEqual(PointCountSession.get_by_id(sid)["status"], "open")
        self.client.delete(f"/api/v1/point-count/images/{iid}")
        self.assertEqual(PointCountSession.get_by_id(sid)["total_count"], 0)
        self.client.delete(f"/api/v1/point-count/sessions/{sid}")
        self.assertIsNone(PointCountSession.get_by_id(sid))

    def test_list_sessions_includes_user(self):
        sid = self._create_session(title="列出测试")
        r = self.client.get("/api/v1/point-count/sessions")
        data = r.get_json()
        self.assertTrue(data["success"])
        ids = [s["id"] for s in data["sessions"]]
        self.assertIn(sid, ids)

    def test_page_renders(self):
        sid = self._create_session()
        r = self.client.get("/tools/point-count/")
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn("独立点数", body)
        r2 = self.client.get(f"/tools/point-count/session/{sid}")
        self.assertEqual(r2.status_code, 200)
        body2 = r2.get_data(as_text=True)
        self.assertIn("点数", body2)
        self.assertIn("拍照", body2)


if __name__ == "__main__":
    unittest.main()