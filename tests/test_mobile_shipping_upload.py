# -*- coding: utf-8 -*-
"""移动端出货：OCR 平均置信度 < 0.5 时 reason 追加"模糊"提示（Task 6）。"""
import io
import os
import tempfile
import time

import pytest
from PIL import Image

from app import create_app


@pytest.fixture
def client():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    import models._db as db_mod
    original = db_mod.DB_PATH
    db_mod.DB_PATH = path
    from models._init import init_db
    init_db()
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        from models.orders import ShippingOrder, ShippingRecord
        oid = ShippingOrder.create("2026-08-09", "客户")
        rid = ShippingRecord.create(
            "2026-08-09", "客户", "杂胶海绵", "黑色 5mm", 100, "y", "",
            order_pk=oid,
        )
        yield c, oid, rid
    db_mod.DB_PATH = original
    os.unlink(path)


def _make_png(width=64, height=64, color=(255, 255, 255)):
    img = Image.new("RGB", (width, height), color)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def _wait_async_done(shipping_mod, image_id, timeout=3.0):
    """行级图走后台线程 OCR+AI；测试要等它跑完才能读 DB。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        info = shipping_mod._ASYNC_JOBS.get(image_id) or {}
        if info.get("state") in ("done", "error"):
            return info
        time.sleep(0.02)
    raise AssertionError(f"async job for image_id={image_id} 没在 {timeout}s 内完成")


def test_blur_reason_appended(monkeypatch, client):
    """avg_conf=0.4 时,reason 应追加 '[图像可能模糊，建议重拍]'。"""
    c, oid, rid = client
    from blueprints import shipping as shipping_mod
    from models.orders import ShippingImage

    def fake_process(image_id, filepath, record, order_id, record_id):
        # 模拟 OCR 完成后写库：先写入基础 match,再追加模糊提示
        ShippingImage.set_match(image_id, "yellow", 0.4, "标签疑似", "local_fuzzy")
        from blueprints.shipping import _append_blur_reason_if_low_conf
        _append_blur_reason_if_low_conf(image_id, avg_conf=0.4)
        shipping_mod._ASYNC_JOBS[image_id] = {"state": "done", "finished": time.time()}

    monkeypatch.setattr(shipping_mod, "_process_record_image_async", fake_process)

    png = _make_png()
    resp = c.post(
        f"/api/v1/shipping-orders/records/{rid}/images",
        data={"image": (io.BytesIO(png), "x.png"), "source": "upload"},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 201, resp.get_data(as_text=True)
    iid = resp.get_json()["images"][0]["image_id"]
    _wait_async_done(shipping_mod, iid)

    img = ShippingImage.get_by_id(iid)
    assert "模糊" in img["reason"], f"reason 中找不到'模糊': {img['reason']!r}"
    assert "图像可能模糊" in img["reason"]


def test_sharp_no_blur_reason(monkeypatch, client):
    """avg_conf=0.9 时,reason 不应追加模糊提示。"""
    c, oid, rid = client
    from blueprints import shipping as shipping_mod
    from models.orders import ShippingImage

    def fake_process(image_id, filepath, record, order_id, record_id):
        ShippingImage.set_match(image_id, "green", 0.95, "一致", "local_fuzzy")
        from blueprints.shipping import _append_blur_reason_if_low_conf
        _append_blur_reason_if_low_conf(image_id, avg_conf=0.9)
        shipping_mod._ASYNC_JOBS[image_id] = {"state": "done", "finished": time.time()}

    monkeypatch.setattr(shipping_mod, "_process_record_image_async", fake_process)

    png = _make_png()
    resp = c.post(
        f"/api/v1/shipping-orders/records/{rid}/images",
        data={"image": (io.BytesIO(png), "x.png"), "source": "upload"},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 201, resp.get_data(as_text=True)
    iid = resp.get_json()["images"][0]["image_id"]
    _wait_async_done(shipping_mod, iid)

    img = ShippingImage.get_by_id(iid)
    assert "模糊" not in img["reason"], f"清晰图不应有模糊提示: {img['reason']!r}"
