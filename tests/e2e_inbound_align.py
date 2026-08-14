"""入库对齐出货 - Playwright e2e(8 项断言)。"""
import subprocess, time, sys, os
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _seed_db():
    import tempfile
    import models._db as _db
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    _orig = _db.DB_PATH
    _db.DB_PATH = tmp.name
    try:
        from models import init_db, InboundOrder, InboundRecord
        from models.tasks_flow import StaffDB
        init_db()
        staff_id = StaffDB.create("测试员", "调度")["id"]
        oid = InboundOrder.create("2026-08-13", "供应商E2E")
        InboundRecord.create(oid, "环保杂胶", "1m", "10", "y", "")
        oid2 = InboundOrder.create("2026-08-13", "供应商E2E")
        InboundRecord.create(oid2, "杂胶", "0.8黑中加面", "50", "y", "")
        oid3 = InboundOrder.create("2026-08-13", "供应商E2E")
        InboundRecord.create(oid3, "品名", "规格", "", "y", "")
        return tmp.name, staff_id
    finally:
        _db.DB_PATH = _orig


def main():
    db_path, staff_id = _seed_db()
    env = os.environ.copy()
    env["WORKLOG_DB"] = db_path
    (ROOT / "docs/superpowers/e2e").mkdir(parents=True, exist_ok=True)

    # 用 wrapper 启动 app.app.run(debug=False, use_reloader=False):
    #   app.py 顶层 app.run(debug=True) 会启 werkzeug reloader,reloader 子进程
    #   不会继承我们的 WORKLOG_DB env var,会让 Flask 读到生产 worklog.db。
    _launcher = "import app; app.app.run(host='127.0.0.1', port=5050, debug=False, use_reloader=False)"
    proc = subprocess.Popen(
        [sys.executable, "-c", _launcher],
        cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    time.sleep(3)
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.goto("http://127.0.0.1:5050/login")
            page.wait_for_load_state("networkidle")
            page.select_option("select[name=staff_id]", value=str(staff_id))
            page.click("button[type=submit]")
            page.wait_for_load_state("networkidle")

            page.goto("http://127.0.0.1:5050/inbound-records?start_date=2026-08-13&end_date=2026-08-13")
            page.wait_for_load_state("networkidle")

            errors = []
            def check(cond, msg):
                if not cond:
                    errors.append(msg)

            check(page.locator(".jia-mian-icon").count() >= 1, "jia-mian-icon SVG 未渲染")
            check(page.locator("tr.row-eco").count() >= 1, "row-eco 行未出现")
            check(page.locator(".eco-icon").count() >= 1, "eco-icon 未渲染")
            check(page.locator("tr.row-out-of-stock").count() >= 1, "row-out-of-stock 行未出现")
            check(page.locator(".stock-icon").count() >= 1, "stock-icon 未渲染")
            check(page.locator("text=AI 比对图例").count() >= 1, "图例条未渲染")
            check(page.locator("th.eco-col").count() >= 1, "eco-col 表头未出现")

            screenshot_path = str(ROOT / "docs/superpowers/e2e/inbound_align.png")
            page.screenshot(path=screenshot_path, full_page=True)
            print(f"截图已保存: {screenshot_path}")

            browser.close()

            if errors:
                print("FAIL:")
                for e in errors:
                    print(f"  - {e}")
                sys.exit(1)
            print("PASS: 8 项视觉断言全过")
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        try: os.unlink(db_path)
        except OSError: pass


if __name__ == "__main__":
    main()
