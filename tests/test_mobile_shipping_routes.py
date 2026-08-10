import os
import tempfile
import pytest

from app import create_app
from models._db import DB_PATH


@pytest.fixture
def client():
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
    os.unlink(path)


def test_shipping_today_renders_200(client):
    resp = client.get("/m/shipping-today")
    assert resp.status_code == 200
    assert "今日出货".encode() in resp.data


def test_shipping_order_detail_renders_200(client):
    from models.orders import ShippingOrder, ShippingRecord
    oid = ShippingOrder.create("2026-08-09", "测试客户")
    ShippingRecord.create("2026-08-09", "测试客户", "杂胶海绵", "黑色 5mm", 100, "y", "2支", order_pk=oid)
    resp = client.get(f"/m/shipping-today/order/{oid}")
    assert resp.status_code == 200
    assert "测试客户".encode() in resp.data
    assert "杂胶海绵".encode() in resp.data


def test_shipping_today_no_login_required(client):
    # 移动端不要求登录（白名单前缀 /m/）
    resp = client.get("/m/shipping-today")
    assert resp.status_code != 302


def test_shipping_today_empty_today(client):
    # 数据库里没有今天订单时，渲染空状态
    resp = client.get("/m/shipping-today")
    assert resp.status_code == 200
    assert "没有出货订单".encode() in resp.data
