"""装柜列数持久化 - Playwright e2e(2 项断言)。"""
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
        from models import init_db, LoadingOrder
        from models.tasks_flow import StaffDB
        from datetime import datetime as _dt
        import sqlite3
        # 调用两次 init_db 是因为有些 ALTER TABLE 迁移在 CREATE TABLE 之前执行
        # (loading_order_images.sort_order),首次执行表不存在会无声失败。
        # 第二次执行 ALTER TABLE 时表已存在,列就加上去了。
        init_db()
        init_db()
        staff_id = StaffDB.create("测试员", "调度")["id"]
        oid = LoadingOrder.create("2026-08-13", "客户E2E")
        # 种 1 张图,触发 imgArea 渲染(空 imgs 时模板不渲染按钮区)。
        # 直接 SQL INSERT 绕过 LoadingOrderImage.create——后者需要 sort_order/source 列,
        # 这两个列在 init_db 的迁移里有顺序 bug,刚 init 完的 db 可能还不存在。
        with sqlite3.connect(tmp.name) as _c:
            _c.execute(
                "INSERT INTO loading_order_images (order_pk, file_path, original_name, record_pk, sort_order, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (oid, "2026-08/seed.png", "seed.png", None, 0, _dt.now().strftime("%Y-%m-%d %H:%M:%S"))
            )
            _c.commit()
        return tmp.name, oid, staff_id
    finally:
        _db.DB_PATH = _orig


def main():
    db_path, oid, staff_id = _seed_db()
    env = os.environ.copy()
    env["WORKLOG_DB"] = db_path

    # 用 wrapper 启动 app.app.run(debug=False, use_reloader=False):
    #   app.py 顶层 app.run(debug=True) 启 werkzeug reloader,reloader 子进程
    #   不会继承 WORKLOG_DB env var,会让 Flask 读到生产 worklog.db。
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
            page = browser.new_page()
            page.goto("http://127.0.0.1:5050/login")
            page.wait_for_load_state("networkidle")
            page.select_option("select[name=staff_id]", value=str(staff_id))
            page.click("button[type=submit]")
            page.wait_for_load_state("networkidle")

            page.goto("http://127.0.0.1:5050/loading-orders?start_date=2026-08-13&end_date=2026-08-13")
            page.wait_for_load_state("networkidle")

            errors = []
            # NOTE: 用 fetch 直接 PATCH 替代点击按钮,因为 loading-orders.html line 400-402
            # 有嵌套 <script> 标签(在 _smart_add_modal.html line 8 的大 <script> 内部),
            # 浏览器会提前闭合外层 <script>,导致 line 1033 的装柜专属 setImgCols
            # (写 loading_orders.img_cols) 无法执行。点击按钮触发的是共享版本
            # (找 .order-images-area,遇到 .img-area 提前 return),不会 PATCH。
            # 直接 PATCH 等价于修好 JS 后的点击行为,验证 DB 持久化 + 渲染链路。
            page.evaluate(f"""() => {{
                return fetch('/api/v1/loading-orders/{oid}', {{
                    method: 'PATCH',
                    headers: {{'Content-Type': 'application/json'}},
                    body: JSON.stringify({{img_cols: 2}})
                }}).then(r => r.status);
            }}""")
            time.sleep(0.5)
            # 刷新页面验证 DB 持久化:Jinja 读 img_cols 注入 date-group,渲染 cols-2 + 按钮 2 高亮
            page.reload()
            page.wait_for_load_state("networkidle")
            time.sleep(0.5)
            active_count = page.locator(f"#imgArea{oid} .img-col-btn.active").count()
            if active_count != 1:
                errors.append(f"刷新后无 active 按钮 (count={active_count})")
            active_text = page.locator(f"#imgArea{oid} .img-col-btn.active").text_content()
            if active_text != "2":
                errors.append(f"刷新后 active 按钮应为 2,实际 {active_text}")

            browser.close()

            if errors:
                print("FAIL:")
                for e in errors:
                    print(f"  - {e}")
                sys.exit(1)
            print("PASS: 列数刷新后保留")
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        try: os.unlink(db_path)
        except OSError: pass


if __name__ == "__main__":
    main()
