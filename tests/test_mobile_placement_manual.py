# -*- coding: utf-8 -*-
"""移动端摆放图手动输入支数(2026-08-19 PC 端同步到移动端)。

- 后端 _placement_compare: 直接输入 manual_count 优先于 n_marks(对齐 PC 端 _eff_zhi)。
- 模板 templates/mobile/placement-count.html: 含手动输入支数弹框。
- mobile_placement.js: API 接入 / 按钮 / 弹框保存+清空 / 点击图自动清空手动模式 / 比对时使用 manual_count。
"""
import os
import re
import tempfile

import pytest

from app import create_app
from models._db import DB_PATH
from models._init import init_db
from models.orders import ShippingOrder, ShippingRecord, PlacementImage


@pytest.fixture
def client():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    import models._db as db_mod
    original = db_mod.DB_PATH
    db_mod.DB_PATH = path
    init_db()
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c
    db_mod.DB_PATH = original
    for ext in ("", "-wal", "-shm"):
        p = path + ext
        if os.path.exists(p):
            try:
                os.unlink(p)
            except OSError:
                pass


def _upload_three_placements(client, rid):
    """上传 3 张摆放图,返回 image_ids。"""
    import io
    from PIL import Image

    def png_bytes():
        img = Image.new("RGB", (32, 32), (255, 255, 255))
        buf = io.BytesIO()
        img.save(buf, "PNG")
        return buf.getvalue()

    png = png_bytes()
    ids = []
    for i in range(3):
        resp = client.post(
            f"/api/v1/shipping-orders/records/{rid}/placement-images",
            data={"image": (io.BytesIO(png), f"x{i}.png"), "source": "placement"},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 201, resp.get_data(as_text=True)
        ids.append(resp.get_json()["images"][0]["image_id"])
    return ids


def _place_result_text(body: str) -> str:
    """提取订单详情页 place-result 块的纯文本(供断言)。"""
    m = re.search(r'<div class="place-result[^"]*">(.*?)</div>', body, re.S)
    return re.sub(r"\s+", " ", m.group(1)) if m else ""


def test_backend_compare_prefers_manual_count(client):
    """zhi_actual = manual_count(image2)=42 + n_marks(image1)=5 = 47。"""
    oid = ShippingOrder.create("2026-08-19", "客户")
    rid = ShippingRecord.create("2026-08-19", "客户", "商品", "规格", 1, "件", "5支+42支", order_pk=oid)
    iid1, iid2, _iid3 = _upload_three_placements(client, rid)
    # img1: 5 个点击点
    for _ in range(5):
        client.post(
            f"/api/v1/shipping-orders/placement-images/{iid1}/marks",
            json={"x_ratio": 0.5, "y_ratio": 0.5, "r": 0},
        )
    # img2: 写入 manual_count=42
    client.post(
        f"/api/v1/shipping-orders/placement-images/{iid2}/manual-count",
        json={"count": 42},
    )
    resp = client.get(f"/m/shipping-today/order/{oid}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    pr = _place_result_text(body)
    assert "支数" in pr and "47" in pr and "备注 47" in pr, f"place-result={pr!r}, full={body!r}"


def test_backend_compare_ignores_n_marks_when_manual_set(client):
    """直输 42 应顶替 n_marks(不是相加)。两份都改 manual 应相加。"""
    oid = ShippingOrder.create("2026-08-19", "客户")
    rid = ShippingRecord.create("2026-08-19", "客户", "商品", "规格", 1, "件", "5支+42支", order_pk=oid)
    iid1, iid2, iid3 = _upload_three_placements(client, rid)
    # img1: 5 marks + 10 manual(覆盖 5 marks),得 10
    for _ in range(5):
        client.post(
            f"/api/v1/shipping-orders/placement-images/{iid1}/marks",
            json={"x_ratio": 0.5, "y_ratio": 0.5, "r": 0},
        )
    client.post(
        f"/api/v1/shipping-orders/placement-images/{iid1}/manual-count",
        json={"count": 10},
    )
    # img2: 42 manual
    client.post(
        f"/api/v1/shipping-orders/placement-images/{iid2}/manual-count",
        json={"count": 42},
    )
    resp = client.get(f"/m/shipping-today/order/{oid}")
    body = resp.get_data(as_text=True)
    pr = _place_result_text(body)
    # 实际应=10+42+0=52
    assert "52" in pr, f"pr={pr!r}"
    # 不应再累加 5 个 marks
    assert "57" not in pr, f"pr={pr!r}"


def test_backend_compare_clears_manual_to_fallback(client):
    """清空 manual_count 后,应回退到 n_marks。"""
    oid = ShippingOrder.create("2026-08-19", "客户")
    rid = ShippingRecord.create("2026-08-19", "客户", "商品", "规格", 1, "件", "5支+42支", order_pk=oid)
    iid1, iid2, _iid3 = _upload_three_placements(client, rid)
    for _ in range(5):
        client.post(
            f"/api/v1/shipping-orders/placement-images/{iid1}/marks",
            json={"x_ratio": 0.5, "y_ratio": 0.5, "r": 0},
        )
    client.post(
        f"/api/v1/shipping-orders/placement-images/{iid2}/manual-count",
        json={"count": 42},
    )
    # 清空 manual
    client.post(
        f"/api/v1/shipping-orders/placement-images/{iid2}/manual-count",
        json={"count": None},
    )
    resp = client.get(f"/m/shipping-today/order/{oid}")
    body = resp.get_data(as_text=True)
    pr = _place_result_text(body)
    # 5 + 0(fallback)+ 0 = 5
    assert "5" in re.sub(r"\D", "", pr), f"pr={pr!r}"
    assert "42" not in re.sub(r"\d+\s*(?=支)|(?<=支)\s*\d+", "", pr), f"pr={pr!r}"


def test_placement_page_renders_manual_modal(client):
    """placement-count.html 应含「输入支数」弹框(移动端手动输入支数)。"""
    oid = ShippingOrder.create("2026-08-19", "客户")
    rid = ShippingRecord.create("2026-08-19", "客户", "商品", "规格", 1, "件", "5支", order_pk=oid)
    resp = client.get(f"/m/shipping-today/order/{oid}/placement/{rid}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "placeManualMask" in body
    assert "输入支数" in body
    assert "manualInput" in body
    assert "manualClear" in body
    assert "manualCancel" in body
    assert "manualSave" in body
    assert "mobile.css" in body


def test_placement_js_manual_api_and_click_override():
    """mobile_placement.js 静态检查:有 manual API / 直输覆盖点击 / 撤销视为清空。"""
    js_path = os.path.join(os.path.dirname(__file__), "..", "static", "js", "mobile_placement.js")
    with open(js_path, encoding="utf-8") as f:
        js = f.read()
    # 1. API endpoint
    assert "manual: (iid) =>" in js
    assert "/manual-count" in js
    # 2. POST manual with count + null clear
    assert 'body: JSON.stringify({ count: count })' in js or '"count": count' in js
    assert 'body: JSON.stringify({ count: null })' in js
    # 3. 点击图先清空 manual_count 再 addMark
    assert "_clearManualThen" in js
    # 4. 撤销时若 manual_count 有值,改为清空
    assert "im.manual_count != null" in js
    # 5. 比对使用 manual_count 优先
    assert "_effZhi" in js
    # 6. 卡片上有「支数」按钮
    assert 'data-action="manual"' in js
    # 7. 默认指示器 + 直接输入标牌
    assert "is-manual" in js
    assert 'data-role="manualLabel"' in js


def test_placement_css_manual_button_and_modal():
    """mobile.css 含手动按钮样式与弹框样式。"""
    css_path = os.path.join(os.path.dirname(__file__), "..", "static", "css", "mobile.css")
    with open(css_path, encoding="utf-8") as f:
        css = f.read()
    assert ".place-card-tools .manual-btn" in css
    assert ".place-manual-mask" in css
    assert ".place-manual-box" in css
    assert ".place-manual-title" in css
    assert ".place-manual-actions .primary" in css