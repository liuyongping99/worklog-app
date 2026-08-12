# -*- coding: utf-8 -*-
import os
import tempfile

import pytest

from app import create_app
from models._init import init_db
from models.orders import ShippingOrder, ShippingRecord, ShippingImage


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
        rid = ShippingRecord.create(oid, "商品", "规格", 1, "件", "")
        yield c, oid, rid
    db_mod.DB_PATH = original
    os.unlink(path)


def test_replace_old_after_new_green(client):
    c, oid, rid = client
    iid_old = ShippingImage.create(oid, "upload\\2026-08\\a.jpg", "a.jpg", "upload", rid, 1)
    iid_new = ShippingImage.create(oid, "upload\\2026-08\\b.jpg", "b.jpg", "upload", rid, 2)
    ShippingImage.set_match(iid_old, "yellow", 0.6, "模糊", "local_fuzzy")
    ShippingImage.set_match(iid_new, "green", 0.95, "一致", "local_fuzzy")
    resp = c.delete(f"/api/v1/shipping-orders/images/{iid_old}")
    assert resp.status_code == 200
    assert ShippingImage.get_by_id(iid_old) is None
    assert ShippingImage.get_by_id(iid_new) is not None


def test_replace_keep_old_if_new_not_green(client):
    c, oid, rid = client
    iid_old = ShippingImage.create(oid, "upload\\2026-08\\a.jpg", "a.jpg", "upload", rid, 1)
    iid_new = ShippingImage.create(oid, "upload\\2026-08\\b.jpg", "b.jpg", "upload", rid, 2)
    ShippingImage.set_match(iid_old, "green", 0.95, "一致", "local_fuzzy")
    ShippingImage.set_match(iid_new, "yellow", 0.6, "模糊", "local_fuzzy")
    assert ShippingImage.get_by_id(iid_old) is not None
    assert ShippingImage.get_by_id(iid_new) is not None
