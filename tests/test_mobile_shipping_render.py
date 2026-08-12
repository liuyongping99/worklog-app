"""移动端出货页面 jsdom 渲染验证。

Task 10:对 /m/shipping-today(列表)与 /m/shipping-today/order/<oid>(详情)做
DOM 结构渲染验证 + iPhone 12 viewport 模拟。

实现说明(brief 偏差):
- brief 假设用 Playwright + 截屏文件做"渲染验证"——本项目无 Playwright 依赖。
- 项目现有的渲染验证走 Node.js + jsdom(参见 tests/render_check.js /
  tests/record_image_match_column_check.js 等),走 subprocess 调 node 脚本。
- 本测试沿用同款风格:Python 端用 Flask test_client 渲染 HTML,把 HTML
  写到临时文件,subprocess 跑 node 脚本用 jsdom 解析 + 断言 DOM 结构。
- viewport 模拟由 jsdom 的 `pretendToBeVisual + viewport:{width,height}`
  选项提供(390x844,iPhone 12);jsdom 不做真实布局,但会通过 window
  报告 innerWidth/innerHeight,作为移动端契约的一部分被断言。
- 无截屏文件:jsdom 不出图,改为验证 DOM 结构 + viewport + 移动端 CSS 资源。
"""
import os
import shutil
import subprocess
import tempfile
from datetime import date as _today_date

import pytest

from app import create_app
from models._db import DB_PATH


_NODE = shutil.which("node")
_SCRIPT_TODAY = os.path.join(os.path.dirname(__file__), "render_mobile_shipping_today.js")
_SCRIPT_ORDER = os.path.join(os.path.dirname(__file__), "render_mobile_shipping_order.js")


@pytest.fixture
def client():
    """独立的 SQLite 临时库 + Flask test_client。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    original = DB_PATH
    import models._db as db_mod
    db_mod.DB_PATH = path
    from models._init import init_db
    init_db()
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c
    db_mod.DB_PATH = original
    try:
        os.unlink(path)
    except OSError:
        pass


def _run_node_script(script_path: str, html_path: str) -> subprocess.CompletedProcess:
    """跑 node + jsdom 渲染验证脚本,断言 HTML 文件路径。"""
    assert _NODE is not None, "需要 node 可执行,请先安装 Node.js"
    # 强制 UTF-8 输出,Windows 默认 GBK 会让中文断言失败
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    env["NODE_NO_WARNINGS"] = "1"
    return subprocess.run(
        [_NODE, script_path, html_path],
        capture_output=True,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        env=env,
    )


@pytest.mark.skipif(_NODE is None, reason="node 不在 PATH 中,跳过 jsdom 渲染验证")
def test_render_today_listing(client):
    """/m/shipping-today:至少 1 张订单卡片 + 客户名 + 进入按钮文案。"""
    from models.orders import ShippingOrder, ShippingRecord, ShippingImage

    today = _today_date.today().isoformat()
    oid = ShippingOrder.create(today, "广隆纸业")
    rid = ShippingRecord.create(today, "广隆纸业", "杂胶海绵", "黑色 5mm", 100, "y", "", order_pk=oid)
    iid = ShippingImage.create(oid, r"upload\2026-08\x.jpg", "x.jpg", "upload", rid, 1)
    ShippingImage.set_match(iid, "green", 0.95, "一致", "local_fuzzy")

    resp = client.get("/m/shipping-today")
    assert resp.status_code == 200

    # 把 HTML 写到临时文件,node 脚本读取后用 jsdom 解析 + 断言
    fd, html_path = tempfile.mkstemp(suffix=".html")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(resp.get_data(as_text=True))

        proc = _run_node_script(_SCRIPT_TODAY, html_path)
        out = proc.stdout.decode("utf-8", errors="replace")
        if proc.returncode != 0:
            pytest.fail(
                f"render_mobile_shipping_today.js 退出码 {proc.returncode}\n"
                f"STDOUT: {out}\nSTDERR: {proc.stderr.decode('utf-8', errors='replace')}"
            )
        assert "通过" in out, f"node 脚本未输出通过标记:\n{out}"
    finally:
        try:
            os.unlink(html_path)
        except OSError:
            pass


@pytest.mark.skipif(_NODE is None, reason="node 不在 PATH 中,跳过 jsdom 渲染验证")
def test_render_order_detail(client):
    """/m/shipping-today/order/<oid>:整体图 3 按钮 + 商品卡片 + 拍照/相册按钮。"""
    from models.orders import ShippingOrder, ShippingRecord

    today = _today_date.today().isoformat()
    oid = ShippingOrder.create(today, "宏昌贸易")
    ShippingRecord.create(today, "宏昌贸易", "日本纸", "A4 120g", 5, "件", "2支", order_pk=oid)

    resp = client.get(f"/m/shipping-today/order/{oid}")
    assert resp.status_code == 200

    fd, html_path = tempfile.mkstemp(suffix=".html")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(resp.get_data(as_text=True))

        proc = _run_node_script(_SCRIPT_ORDER, html_path)
        out = proc.stdout.decode("utf-8", errors="replace")
        if proc.returncode != 0:
            pytest.fail(
                f"render_mobile_shipping_order.js 退出码 {proc.returncode}\n"
                f"STDOUT: {out}\nSTDERR: {proc.stderr.decode('utf-8', errors='replace')}"
            )
        assert "通过" in out, f"node 脚本未输出通过标记:\n{out}"
    finally:
        try:
            os.unlink(html_path)
        except OSError:
            pass


@pytest.mark.skipif(_NODE is None, reason="node 不在 PATH 中,跳过 jsdom 渲染验证")
def test_render_today_empty(client):
    """空库下 /m/shipping-today:jsdom 解析依然成功,断言空文案存在。"""
    resp = client.get("/m/shipping-today")
    assert resp.status_code == 200

    fd, html_path = tempfile.mkstemp(suffix=".html")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(resp.get_data(as_text=True))

        proc = _run_node_script(_SCRIPT_TODAY, html_path)
        out = proc.stdout.decode("utf-8", errors="replace")
        if proc.returncode != 0:
            pytest.fail(
                f"空库 render_mobile_shipping_today.js 退出码 {proc.returncode}\n"
                f"STDOUT: {out}\nSTDERR: {proc.stderr.decode('utf-8', errors='replace')}"
            )
        # 额外断言:HTML 含空状态文案,证明 node 脚本解析到的也是空库渲染结果
        body = resp.get_data(as_text=True)
        assert "没有出货订单" in body
        assert "通过" in out
    finally:
        try:
            os.unlink(html_path)
        except OSError:
            pass