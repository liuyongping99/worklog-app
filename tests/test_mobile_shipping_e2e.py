# -*- coding: utf-8 -*-
"""移动端出货：端到端验收 — 移动端拍照上传 → PC 端 /shipping-records 看到状态（Task 11）。

完整链路：移动端上传行级图 → 后端落盘 → OCR/AI 异步跑（mock 跳过）→ match_status=green
→ PC 端 GET /shipping-records?start_date=今天&end_date=今天 返回 200 且 DB 一致。
"""
import io
import os
import tempfile

import pytest
from PIL import Image

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
        from models.orders import ShippingOrder, ShippingRecord
        # /shipping-records 需要登录 —— 注册一个 Staff 并写 session 跳过 _require_login
        from models.tasks_flow import StaffDB
        staff_id = StaffDB.create("测试员", "调度")["id"]
        with c.session_transaction() as s:
            s["operator_id"] = staff_id
        today = _today()
        oid = ShippingOrder.create(today, "客户")
        # 注意:ShippingRecord.create 签名是 (date, customer, product_name,
        # specification, quantity, unit, remark, order_pk=None) —— 用关键字传 order_pk
        rid = ShippingRecord.create(
            today, "客户", "商品", "规格", 1, "件", "", order_pk=oid
        )
        yield c, oid, rid
    db_mod.DB_PATH = original
    # WAL 残留 cleanup(避免 PermissionError)
    for ext in ("", "-wal", "-shm"):
        p = path + ext
        if os.path.exists(p):
            try:
                os.unlink(p)
            except OSError:
                pass


def _today():
    from datetime import date
    return date.today().isoformat()


def _png():
    img = Image.new("RGB", (64, 64), (255, 255, 255))
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def test_e2e_mobile_to_pc_visible(monkeypatch, client):
    """移动端上传行级图(mock OCR/AI 直接 set_match('green'))→ PC 端完整页可见。"""
    c, oid, rid = client
    from blueprints import shipping as shipping_mod
    from models.orders import ShippingImage

    # mock 后台异步处理函数 —— 跳过 PaddleOCR/DeepSeek 调用,直接写库 match=green
    # 真实签名:(image_id, filepath, record, order_id, record_id)
    def fake_process(image_id, filepath, record, order_id, record_id):
        ShippingImage.set_match(image_id, "green", 0.95, "一致", "local_fuzzy")
        shipping_mod._ASYNC_JOBS[image_id] = {
            "state": "done",
            "finished": __import__("time").time(),
        }

    monkeypatch.setattr(shipping_mod.shipping_processor, "process_async", fake_process)

    # 1) 移动端上传行级图
    png = _png()
    resp = c.post(
        f"/api/v1/shipping-orders/records/{rid}/images",
        data={"image": (io.BytesIO(png), "x.png"), "source": "upload"},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 201, resp.get_data(as_text=True)
    body = resp.get_json()
    assert "images" in body and len(body["images"]) >= 1
    iid = body["images"][0]["image_id"]

    # 2) 触发 mock 写库(端点只负责落盘,真实异步处理由后台线程跑 —— mock 已替换)
    # 真实生产环境 _process_record_image_async 会被后台线程调用,
    # 测试里我们直接显式调一下确保 DB 写入可见
    record = {"id": rid, "product_name": "商品", "specification": "规格"}
    shipping_mod.shipping_processor.process_async(
        image_id=iid, filepath="ignored", record=record, order_id=oid, record_id=rid)

    # 3) PC 端完整出货页 —— ?start_date=今天&end_date=今天 应 200
    today = _today()
    pc = c.get(f"/shipping-records?start_date={today}&end_date={today}")
    assert pc.status_code == 200

    # 4) DB 一致性断言 —— ShippingImage.match_status == 'green'
    img = ShippingImage.get_by_id(iid)
    assert img is not None, f"图片 {iid} 不存在"
    assert img["match_status"] == "green", f"match_status: {img['match_status']!r}"
    assert img["match_score"] == 0.95
    assert img["record_pk"] == rid
    assert img["order_pk"] == oid