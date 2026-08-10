# -*- coding: utf-8 -*-
"""移动端出货：上传端点接受 rotate_deg 并 Pillow 旋转。"""
import io
import os
import tempfile

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
        # 实际签名: (date, customer, product_name, specification, quantity, unit, remark, order_pk)
        rid = ShippingRecord.create(
            "2026-08-09", "客户", "杂胶海绵", "黑色 5mm", 100, "y", "",
            order_pk=oid,
        )
        yield c, oid, rid
    db_mod.DB_PATH = original
    os.unlink(path)


def _make_png(width=64, height=64, color=(255, 0, 0)):
    img = Image.new("RGB", (width, height), color)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def test_rotate_180_accepted(client, monkeypatch):
    c, oid, rid = client
    # mock 后台 OCR 处理线程，避免真实加载 PaddleOCR
    from blueprints import shipping as shipping_mod
    monkeypatch.setattr(
        shipping_mod, "_process_record_image_async", lambda *a, **k: None
    )
    png = _make_png()
    resp = c.post(
        f"/api/v1/shipping-orders/records/{rid}/images",
        data={
            "image": (io.BytesIO(png), "x.png"),
            "source": "upload",
            "rotate_deg": "180",
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["success"] is True
    assert body["async"] is True
    img = body["images"][0]
    # 落盘后图尺寸与旋转后一致
    from models.orders import ShippingImage
    file_path = ShippingImage.get_by_id(img["image_id"])["file_path"]
    abs_path = os.path.join(os.getcwd(), file_path)
    with Image.open(abs_path) as im:
        assert im.size == (64, 64)  # 180° 旋转尺寸不变


def test_rotate_invalid_returns_400(client):
    c, oid, rid = client
    png = _make_png()
    resp = c.post(
        f"/api/v1/shipping-orders/records/{rid}/images",
        data={
            "image": (io.BytesIO(png), "x.png"),
            "source": "upload",
            "rotate_deg": "45",
        },
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert "方向参数非法" in resp.get_json()["error"]
