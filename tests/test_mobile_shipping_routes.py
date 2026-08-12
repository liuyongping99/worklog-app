import os
import tempfile
from datetime import date as _today_date
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


def test_shipping_today_shows_order_card(client):
    from models.orders import ShippingOrder, ShippingRecord, ShippingImage
    today = _today_date.today().isoformat()
    oid = ShippingOrder.create(today, "广隆纸业")
    rid = ShippingRecord.create(today, "广隆纸业", "杂胶海绵", "黑色 5mm", 100, "y", "", order_pk=oid)
    iid = ShippingImage.create(oid, r"upload\2026-08\x.jpg", "x.jpg", "upload", rid, 1)
    ShippingImage.set_match(iid, "green", 0.95, "一致", "local_fuzzy")
    resp = client.get("/m/shipping-today")
    assert resp.status_code == 200
    assert "广隆纸业".encode() in resp.data
    assert "进入商品详情".encode() in resp.data
    assert "1/1".encode() in resp.data  # total/has_image


def test_shipping_today_aggregates_status(client):
    from models.orders import ShippingOrder, ShippingRecord, ShippingImage
    today = _today_date.today().isoformat()
    oid = ShippingOrder.create(today, "兴达包装")
    rid1 = ShippingRecord.create(today, "兴达包装", "白磅布", "60寸", 2, "件", "", order_pk=oid)
    rid2 = ShippingRecord.create(today, "兴达包装", "日本纸", "A4", 5, "件", "", order_pk=oid)
    iid = ShippingImage.create(oid, "upload\\2026-08\\x.jpg", "x.jpg", "upload", rid1, 1)
    ShippingImage.set_match(iid, "green", 0.95, "一致", "local_fuzzy")
    resp = client.get("/m/shipping-today")
    assert resp.status_code == 200
    assert "1".encode() in resp.data  # total


def test_shipping_order_overall_section(client):
    from models.orders import ShippingOrder, ShippingRecord
    oid = ShippingOrder.create("2026-08-09", "宏昌贸易")
    ShippingRecord.create("2026-08-09", "宏昌贸易", "日本纸", "A4 120g", 5, "件", "", order_pk=oid)
    resp = client.get(f"/m/shipping-today/order/{oid}")
    assert resp.status_code == 200
    assert "整体图".encode() in resp.data
    assert "整体照".encode() in resp.data
    assert "堆放".encode() in resp.data
    assert "装车".encode() in resp.data


def test_shipping_order_record_cards(client):
    from models.orders import ShippingOrder, ShippingRecord
    oid = ShippingOrder.create("2026-08-09", "宏昌贸易")
    rid = ShippingRecord.create("2026-08-09", "宏昌贸易", "日本纸", "A4 120g", 5, "件", "2支", order_pk=oid)
    resp = client.get(f"/m/shipping-today/order/{oid}")
    assert resp.status_code == 200
    assert "拍照识别".encode() in resp.data
    assert "相册".encode() in resp.data
    assert "5件".encode() in resp.data


def test_detail_includes_mobile_detail_js(client):
    from models.orders import ShippingOrder, ShippingRecord
    today = _today_date.today().isoformat()
    oid = ShippingOrder.create(today, "客户")
    rid = ShippingRecord.create(today, "客户", "商品", "规格", 1, "件", "", order_pk=oid)
    resp = client.get(f"/m/shipping-today/order/{oid}")
    assert resp.status_code == 200
    assert "mobile_detail.js" in resp.get_data(as_text=True)
    assert "mobile_blur.js" in resp.get_data(as_text=True)


def test_detail_uses_data_last_image_id(client):
    """回归保险：mobile_detail.js 在 uploadRecordImage 拿到 imageId 后应写入 card.dataset.lastImageId，
    以便 confirm 按钮 handler 可读取——确保模板渲染出可被 dataset 写入的 product-card 占位。"""
    from models.orders import ShippingOrder, ShippingRecord
    today = _today_date.today().isoformat()
    oid = ShippingOrder.create(today, "客户")
    ShippingRecord.create(today, "客户", "商品", "规格", 1, "件", "", order_pk=oid)
    resp = client.get(f"/m/shipping-today/order/{oid}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    # 模板含 product-card 占位；JS 通过 dataset.lastImageId 写入
    assert 'data-record-id' in body
    assert 'product-card' in body
    # JS 静态文件中应包含关键写入语句
    js_path = os.path.join(os.path.dirname(__file__), "..", "static", "js", "mobile_detail.js")
    with open(js_path, encoding="utf-8") as f:
        js = f.read()
    assert "card.dataset.lastImageId = imageId" in js
    assert 'typeof window.mobileBlurCheck === "function"' in js


def test_detail_renders_initial_image_state(client):
    """回归：详情页渲染时如 record 已有 green 图，徽标与状态行应反映出来，不能硬编码'待拍/尚未拍照'。"""
    from models.orders import ShippingOrder, ShippingRecord, ShippingImage
    today = _today_date.today().isoformat()
    oid = ShippingOrder.create(today, "回归客户")
    rid = ShippingRecord.create(today, "回归客户", "白磅布", "60寸", 2, "件", "", order_pk=oid)
    iid = ShippingImage.create(oid, r"upload\2026-08\a.jpg", "a.jpg", "upload", rid, 1)
    ShippingImage.set_match(iid, "green", 0.95, "一致", "local_fuzzy")
    resp = client.get(f"/m/shipping-today/order/{oid}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "✓ 通过" in body
    assert "标签与规格一致" in body
    assert "尚未拍照" not in body


def test_detail_renders_order_images_grid(client):
    """回归：详情页底部展示整单图片缩略图（含 record 图 + 订单共享图），3 列 grid，点击调用 showImgPreview。"""
    from models.orders import ShippingOrder, ShippingRecord, ShippingImage
    today = _today_date.today().isoformat()
    oid = ShippingOrder.create(today, "客户")
    rid = ShippingRecord.create(today, "客户", "商品", "60寸", 1, "件", "", order_pk=oid)
    iid1 = ShippingImage.create(oid, r"upload\2026-08\a.jpg", "a.jpg", "upload", rid, 1)
    iid2 = ShippingImage.create(oid, r"upload\2026-08\b.jpg", "b.jpg", "upload", rid, 2)
    iid_shared = ShippingImage.create(oid, r"upload\2026-08\c.jpg", "c.jpg", "upload", None, 1)
    resp = client.get(f"/m/shipping-today/order/{oid}")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "order-images" in body  # 整单 grid section
    assert "本单图片" in body
    assert body.count('<img src="/upload/') == 3  # 2 record + 1 shared, 单 grid 内合计 3 张
    assert 'onclick="showImgPreview' in body
