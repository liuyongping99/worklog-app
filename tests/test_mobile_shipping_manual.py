# -*- coding: utf-8 -*-
"""移动端出货：人工确认幂等 + 详情路由注入 set_log_context（Task 9）。"""
import os
import tempfile

import pytest

from app import create_app
from models._db import DB_PATH
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
        iid = ShippingImage.create(oid, "upload\\2026-08\\a.jpg", "a.jpg", "upload", rid, 1)
        ShippingImage.set_match(iid, "yellow", 0.6, "OCR差异", "local_fuzzy")
        yield c, oid, rid, iid
    db_mod.DB_PATH = original
    os.unlink(path)


def test_manual_verify_marks_human(client):
    c, oid, rid, iid = client
    resp = c.post(
        f"/api/v1/shipping-orders/images/{iid}/manual-verify",
        json={"verified": True},
    )
    assert resp.status_code == 200
    img = ShippingImage.get_by_id(iid)
    assert img["human_verified"] == 1


def test_manual_verify_idempotent(client):
    c, oid, rid, iid = client
    for _ in range(2):
        resp = c.post(
            f"/api/v1/shipping-orders/images/{iid}/manual-verify",
            json={"verified": True},
        )
        assert resp.status_code == 200
    img = ShippingImage.get_by_id(iid)
    assert img["human_verified"] == 1


def test_log_context_set_in_mobile_route(client, monkeypatch):
    """访问详情路由不应报错，且应在路由体内调用过 set_log_context
    （具体 biz/order_id 值由实现决定，宽松断言）。"""
    from blueprints import mobile_shipping as mship

    captured = []

    def fake_set(**kw):
        captured.append(kw)

    # 拦截 mobile_shipping 视图内导入的 set_log_context
    monkeypatch.setattr(mship, "set_log_context", fake_set)

    c, oid, rid, iid = client
    resp = c.get(f"/m/shipping-today/order/{oid}")
    assert resp.status_code == 200
    # 至少有一次调用携带了 order_id
    assert any("order_id" in kw for kw in captured), f"未捕获到 set_log_context 调用: {captured}"
    # 至少 biz 标签被注入
    assert any(kw.get("biz") == "mobile_shipping" for kw in captured), f"未注入 biz='mobile_shipping': {captured}"