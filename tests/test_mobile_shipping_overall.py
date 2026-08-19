# -*- coding: utf-8 -*-
import io
import os
import tempfile

import pytest
from PIL import Image

from app import create_app
from models._init import init_db
from models.orders import ShippingOrder, ShippingImage


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
        oid = ShippingOrder.create("2026-08-09", "客户")
        yield c, oid
    db_mod.DB_PATH = original
    os.unlink(path)


def _png():
    img = Image.new("RGB", (64, 64), (200, 200, 200))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def test_overall_image_no_ocr(monkeypatch, client):
    c, oid = client
    from blueprints import shipping as shipping_mod
    called = {"flag": False}

    def fake_process(*a, **k):
        called["flag"] = True

    monkeypatch.setattr(shipping_mod.shipping_processor, "process_async", fake_process)
    resp = c.post(
        f"/api/v1/shipping-orders/{oid}/images",
        data={"image": (io.BytesIO(_png()), "整体照.png"), "source": "upload", "original_name": "整体照-1.jpg"},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 201
    iid = resp.get_json()["image_id"]
    img = ShippingImage.get_by_id(iid)
    assert img["record_pk"] is None
    assert img["match_status"] is None
    assert called["flag"] is False


def test_overall_thumb_label_distinguishes_sources(client):
    c, oid = client
    resp = c.post(
        f"/api/v1/shipping-orders/{oid}/images",
        data={"image": (io.BytesIO(_png()), "堆放.png"), "source": "upload", "original_name": "堆放-2.jpg"},
        content_type="multipart/form-data",
    )
    iid = resp.get_json()["image_id"]
    img = ShippingImage.get_by_id(iid)
    assert "堆放" in img["original_name"]
