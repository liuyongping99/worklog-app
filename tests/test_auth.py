"""Auth (登录) blueprint 端到端测试 — T6。

业务规则:登录 = 选一个 staff,不需要密码,session 记 operator_id。
- GET  /login  → 渲染登录页(含 active staff 下拉)
- POST /login  → 设置 session['operator_id'],跳转 "/"
- GET  /logout → 清 session,跳转 "/login"
"""
import unittest
from app import create_app
from models.tasks_flow import StaffDB


class AuthTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.client = self.app.test_client()
        # 单连接清表(避免与 test_client 锁竞争 — 已在 E2E 踩过这个坑)
        from models._db import get_db
        db = get_db()
        for t in ("task_events", "task_images", "task_items", "tasks", "staff", "vehicles"):
            db.execute(f"DELETE FROM {t}")
        db.commit()
        # 准备数据:启用 + 停用 各一个
        self.active_driver = StaffDB.create(name="司机甲", role="司机", is_active=1)
        self.active_clerk = StaffDB.create(name="文员乙", role="文员", is_active=1)
        self.inactive = StaffDB.create(name="离职丙", role="司机", is_active=0)

    def _login(self, staff):
        with self.client.session_transaction() as sess:
            sess["operator_id"] = staff["id"]

    def test_login_get_renders_dropdown(self):
        """GET /login 渲染登录页,包含至少一个 active staff"""
        r = self.client.get("/login")
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        # 选人即可,Tailwind 下拉;assert active 名字出现、inactive 不出现(只展示 is_active=1)
        self.assertIn("司机甲", body, "active 司机甲应出现")
        self.assertIn("文员乙", body, "active 文员乙应出现")
        self.assertNotIn("离职丙", body, "inactive 离职丙不应出现")

    def test_login_post_sets_session_and_redirects(self):
        """POST /login 有效 staff_id → session 设置 + 302 → /"""
        r = self.client.post("/login", data={"staff_id": str(self.active_driver["id"])},
                             follow_redirects=False)
        self.assertEqual(r.status_code, 302, "登录成功应重定向")
        self.assertTrue(r.headers["Location"].endswith("/"), f"应跳到 /,实际 {r.headers.get('Location')}")
        # 再用新 session 验证:后续请求能拿到 operator
        with self.client.session_transaction() as sess:
            self.assertEqual(sess.get("operator_id"), self.active_driver["id"])

    def test_logout_clears_session_and_redirects(self):
        """GET /logout 清空 session 并跳 /login"""
        self._login(self.active_clerk)
        r = self.client.get("/logout", follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        self.assertTrue(r.headers["Location"].endswith("/login"),
                        f"应跳 /login,实际 {r.headers.get('Location')}")
        with self.client.session_transaction() as sess:
            self.assertNotIn("operator_id", sess, "operator_id 应被清掉")


if __name__ == "__main__":
    unittest.main()
