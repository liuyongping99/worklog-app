"""消防检查记录回归测试（2026-11-06 新增功能）。

覆盖：
1. 状态推导 —— 有遗留 = open，无遗留 = closed，用户手选优先
2. 增删改查 + user_id 落库
3. 页面 / 导航可达
4. 空值校验（日期 / 检查事项必填）
"""
import unittest

from app import create_app
from models import FireCheck
from models.tasks_flow import StaffDB
from models._db import get_db


def _table_cleanup(db):
    db.execute("DELETE FROM fire_checks")
    db.commit()


class FireCheckModelTest(unittest.TestCase):
    def setUp(self):
        self.app = create_app()
        self.client = self.app.test_client()
        db = get_db()
        _table_cleanup(db)
        db.close()
        op = StaffDB.create(name="fire-tester", role="文员", is_active=1)
        self.op_id = op["id"]
        with self.client.session_transaction() as sess:
            sess["operator_id"] = self.op_id

    def tearDown(self):
        db = get_db()
        _table_cleanup(db)
        db.close()

    # ── 状态推导 ────────────────────────────────────────────
    def test_status_derived_from_outstanding(self):
        self.assertEqual(FireCheck.derive_status("应急灯不亮"), FireCheck.STATUS_OPEN)
        self.assertEqual(FireCheck.derive_status(""), FireCheck.STATUS_CLOSED)
        self.assertEqual(FireCheck.derive_status("   "), FireCheck.STATUS_CLOSED)

    def test_create_derives_status(self):
        rid = FireCheck.create("2026-11-06", "灭火器正常", "成品仓", "应急灯不亮")
        self.assertEqual(FireCheck.get_by_id(rid)["status"], FireCheck.STATUS_OPEN)

        rid2 = FireCheck.create("2026-11-05", "消防栓无遮挡", "车间")
        self.assertEqual(FireCheck.get_by_id(rid2)["status"], FireCheck.STATUS_CLOSED)

    def test_update_manual_status_wins(self):
        rid = FireCheck.create("2026-11-06", "灭火器正常", "成品仓", "应急灯不亮")
        # 传合法 status → 以用户为准，不按遗留事项推导
        FireCheck.update(rid, "2026-11-06", "灭火器正常", "成品仓", "应急灯不亮", "", "closed")
        self.assertEqual(FireCheck.get_by_id(rid)["status"], FireCheck.STATUS_CLOSED)
        # 传空 status → 按遗留事项重新推导
        FireCheck.update(rid, "2026-11-06", "灭火器正常", "成品仓", "仍有遗留", "", "")
        self.assertEqual(FireCheck.get_by_id(rid)["status"], FireCheck.STATUS_OPEN)

    def test_stats(self):
        FireCheck.create("2026-11-06", "a", "成品仓", "遗留1")
        FireCheck.create("2026-11-05", "b", "车间", "")
        st = FireCheck.stats()
        self.assertEqual(st["total"], 2)
        self.assertEqual(st["open"], 1)
        self.assertEqual(st["closed"], 1)

    # ── CRUD ────────────────────────────────────────────────
    def test_crud(self):
        rid = FireCheck.create("2026-11-06", "灭火器压力正常", "成品仓", "3号仓东侧应急灯不亮", "月度例行", user_id=self.op_id)
        rec = FireCheck.get_by_id(rid)
        self.assertEqual(rec["date"], "2026-11-06")
        self.assertEqual(rec["location"], "成品仓")
        self.assertEqual(rec["check_items"], "灭火器压力正常")
        self.assertEqual(rec["outstanding_items"], "3号仓东侧应急灯不亮")
        self.assertEqual(rec["remark"], "月度例行")
        self.assertEqual(rec["user_id"], self.op_id)

        self.assertTrue(FireCheck.update(rid, "2026-11-07", "改后", "车间", "", ""))
        self.assertEqual(FireCheck.get_by_id(rid)["date"], "2026-11-07")

        self.assertTrue(FireCheck.delete(rid))
        self.assertIsNone(FireCheck.get_by_id(rid))

    def test_get_all_sorted_by_date_desc(self):
        FireCheck.create("2026-11-01", "a")
        FireCheck.create("2026-11-06", "b")
        FireCheck.create("2026-11-03", "c")
        dates = [r["date"] for r in FireCheck.get_all()]
        self.assertEqual(dates, ["2026-11-06", "2026-11-03", "2026-11-01"])

    # ── 路由 ────────────────────────────────────────────────
    def test_page_and_nav(self):
        r = self.client.get("/fire-checks")
        self.assertEqual(r.status_code, 200)
        body = r.get_data(as_text=True)
        self.assertIn("消防检查台账", body)

        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("/fire-checks", r.get_data(as_text=True))

    def test_add_edit_delete_routes(self):
        r = self.client.post("/fire-checks/add", data={
            "date": "2026-11-06",
            "location": "成品仓",
            "check_items": "灭火器正常",
            "outstanding_items": "应急灯不亮",
            "remark": "",
        }, follow_redirects=True)
        self.assertEqual(r.status_code, 200)
        recs = FireCheck.get_all()
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["status"], FireCheck.STATUS_OPEN)
        rid = recs[0]["id"]

        self.client.post("/fire-checks/edit/%d" % rid, data={
            "date": "2026-11-06",
            "location": "成品仓A区",
            "check_items": "灭火器正常",
            "outstanding_items": "",
            "remark": "已整改",
            "status": "",
        }, follow_redirects=True)
        rec = FireCheck.get_by_id(rid)
        self.assertEqual(rec["location"], "成品仓A区")
        self.assertEqual(rec["status"], FireCheck.STATUS_CLOSED)

        self.client.post("/fire-checks/delete/%d" % rid, follow_redirects=True)
        self.assertEqual(len(FireCheck.get_all()), 0)

    def test_add_rejects_empty_required(self):
        self.client.post("/fire-checks/add", data={
            "date": "", "check_items": "", "location": "", "outstanding_items": "",
        }, follow_redirects=True)
        self.assertEqual(len(FireCheck.get_all()), 0)


if __name__ == "__main__":
    unittest.main()
