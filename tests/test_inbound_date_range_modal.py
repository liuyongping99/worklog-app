"""回归测试:入库页日期范围弹框不应被 3 秒自动隐藏定时器误删。

历史 bug:
  templates/inbound-records.html 第 1267 行的 "3 秒后自动隐藏 flash 消息" 定时器
  选择器是 '.flash-messages .flash, [style*="position: fixed"]'——第二个选择器
  误伤 #dateRangeModal(内联样式带 position:fixed),3 秒后被 el.remove()。
  用户在弹框内点日期输入→ 原生 date picker 还没来得及选→ 弹框被定时器删掉
  → date picker 跟着消失,表现为"日期选择组件展开后来不及选就消失"。

重现关键点:
  原 HTML 内联样式是 'display:none; position:fixed;'(无空格),浏览器没改写过。
  [style*="position: fixed"] 字符串匹配要求有空格 → 不命中。
  当 JS 调用 modal.style.display = 'flex' 后,浏览器会**重序列化**整个内联样式,
  变成 'display: flex; position: fixed; top: 0px; ...'(有空格) → 此时才命中。
  所以重现路径必须是:打开 modal(让样式被重序列化)→ 等定时器触发。

本测试:
  1. 打开 /inbound-records
  2. 立刻点 ⚙️ 日期范围按钮(打开 modal,触发样式重序列化)
  3. 等 4 秒(让 3s 定时器触发 + 300ms 移除延迟走完)
  4. 断言 #dateRangeModal 仍存在 + display:flex + opacity:1 + 内部 input 有值
"""
import subprocess
import sys
import time
import os
import tempfile
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _seed_db():
    """建一份最小可用的测试 db, 写入几条入库记录让页面有内容渲染。"""
    import models._db as _db
    tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
    tmp.close()
    _orig = _db.DB_PATH
    _db.DB_PATH = tmp.name
    try:
        from models import init_db, InboundOrder, InboundRecord
        from models.tasks_flow import StaffDB
        init_db()
        StaffDB.create("回归测试员", "调度")
        oid = InboundOrder.create("2026-08-13", "回归供应商")  # type: ignore[arg-type]
        InboundRecord.create(oid, "环保杂胶", "1m", "10", "y", "")
        return tmp.name
    finally:
        _db.DB_PATH = _orig


def main():
    db_path = _seed_db()
    env = os.environ.copy()
    env["WORKLOG_DB"] = db_path
    (ROOT / "docs/superpowers/regression").mkdir(parents=True, exist_ok=True)

    # 用 wrapper 启 app:debug=True 会起 werkzeug reloader, 子进程不继承
    # WORKLOG_DB,会回落到生产 db。所以显式 debug=False + use_reloader=False。
    # 用 5051 而不是 5050:避免与开发者正在跑的 dev server 冲突。
    _launcher = "import app; app.app.run(host='127.0.0.1', port=5051, debug=False, use_reloader=False)"
    proc = subprocess.Popen(
        [sys.executable, "-c", _launcher],
        cwd=str(ROOT), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    # 等服务器起来(轮询 5051 端口可达,避免时间不够或起不起来)
    import socket
    deadline = time.time() + 15
    while time.time() < deadline:
        with socket.socket() as s:
            s.settimeout(0.5)
            try:
                s.connect(('127.0.0.1', 5051))
                break
            except OSError:
                if proc.poll() is not None:
                    raise RuntimeError(f"test server exited early, code={proc.returncode}")
        time.sleep(0.3)
    else:
        proc.terminate()
        raise RuntimeError("test server failed to start on port 5051 within 15s")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page(viewport={"width": 1280, "height": 800})
            page.goto("http://127.0.0.1:5051/login")
            page.wait_for_load_state("networkidle")
            # 跳过第一个 disabled 占位项(— 请选择 —),选实际 staff
            options = page.locator("select[name=staff_id] option:not([disabled])").all()
            assert options, "登录页找不到可用 staff 选项"
            page.select_option("select[name=staff_id]", value=options[0].get_attribute("value"))
            page.click("button[type=submit]")
            page.wait_for_load_state("networkidle")

            page.goto("http://127.0.0.1:5051/inbound-records")
            page.wait_for_load_state("networkidle")

            # 关键时序:
            # 1) 原 HTML 写的是 `position:fixed;`(无空格),选择器 `[style*="position: fixed"]`(有空格)
            #    不会匹配 —— 直到 JS 调用 modal.style.display = 'flex',浏览器重序列化内联样式为
            #    `position: fixed;`(有空格),此时才命中选择器。
            # 2) 3 秒后定时器触发,把 modal 的 opacity 设为 0,300ms 后 el.remove()。
            # 所以用户场景:页面打开后立刻点 ⚙️ → modal 打开 → 等几秒选日期 → modal 莫名消失。
            page.click("#dateRangeBtn")  # 打开 modal,触发样式重序列化
            time.sleep(4)  # 等定时器(3s)+ 移除延迟(300ms)

            errors = []
            def check(cond, msg):
                if not cond:
                    errors.append(msg)

            # 1. modal 元素仍应在 DOM 里(没被 el.remove() 误删)
            modal_count = page.locator("#dateRangeModal").count()
            check(modal_count == 1, f"#dateRangeModal 不在 DOM (count={modal_count})")

            # 2. modal 应仍处于打开状态(display:flex,不是被 opacity:0 隐藏)
            modal_display = page.evaluate(
                "() => { const m = document.getElementById('dateRangeModal');"
                "        return m ? getComputedStyle(m).display : 'missing'; }"
            )
            check(modal_display == "flex", f"modal 已被关闭 (display={modal_display})")

            modal_opacity = page.evaluate(
                "() => { const m = document.getElementById('dateRangeModal');"
                "        return m ? getComputedStyle(m).opacity : 'missing'; }"
            )
            check(modal_opacity == "1", f"modal 被设 opacity=0 (opacity={modal_opacity})")

            # 3. modal 内 startDate 应有值
            start_value = page.evaluate(
                "() => { const el = document.getElementById('startDate');"
                "        return el ? el.value : null; }"
            )
            check(bool(start_value), f"startDate 没值 (value={start_value!r})")

            screenshot_path = str(ROOT / "docs/superpowers/regression/inbound_date_range_modal.png")
            page.screenshot(path=screenshot_path)
            print(f"截图已保存: {screenshot_path}")

            browser.close()

            if errors:
                print("FAIL:")
                for e in errors:
                    print(f"  - {e}")
                sys.exit(1)
            print("PASS: 4 项回归断言全过 — 日期弹框不再被定时器误删")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        try:
            os.unlink(db_path)
        except OSError:
            pass


if __name__ == "__main__":
    main()