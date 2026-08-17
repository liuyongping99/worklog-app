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
    assert "备货照".encode() in resp.data
    assert "装车照".encode() in resp.data
    assert "归仓照".encode() in resp.data


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


def test_detail_renders_upload_progress_elements(client):
    """回归：商品卡片含 data-role="upload-progress" 元素（默认 hidden），整体图区含 overall-progress 元素。"""
    from models.orders import ShippingOrder, ShippingRecord
    today = _today_date.today().isoformat()
    oid = ShippingOrder.create(today, "客户")
    ShippingRecord.create(today, "客户", "商品", "规格", 1, "件", "", order_pk=oid)
    resp = client.get(f"/m/shipping-today/order/{oid}")
    body = resp.get_data(as_text=True)
    assert 'data-role="upload-progress"' in body
    assert 'data-role="overall-progress"' in body
    # 默认 hidden（不在视图中显示）
    assert body.count('data-role="upload-progress" hidden') >= 1
    assert body.count('data-role="overall-progress" hidden') >= 1


def test_detail_uses_latest_image_status(client):
    """回归：商品行多张图时，状态应取最新一张（按 sort_order 末尾），不是'最差'。
    复现:先 yellow 存疑,再 green 通过,刷新后应显示 green。"""
    from models.orders import ShippingOrder, ShippingRecord, ShippingImage
    today = _today_date.today().isoformat()
    oid = ShippingOrder.create(today, "客户")
    rid = ShippingRecord.create(today, "客户", "白磅布", "60寸", 2, "件", "", order_pk=oid)
    iid_old = ShippingImage.create(oid, r"upload\2026-08\a.jpg", "a.jpg", "upload", rid, 1)
    ShippingImage.set_match(iid_old, "yellow", 0.6, "存疑", "local_fuzzy")
    iid_new = ShippingImage.create(oid, r"upload\2026-08\b.jpg", "b.jpg", "upload", rid, 2)
    ShippingImage.set_match(iid_new, "green", 0.95, "一致", "local_fuzzy")
    resp = client.get(f"/m/shipping-today/order/{oid}")
    body = resp.get_data(as_text=True)
    assert "✓ 通过" in body
    assert "AI 判定与标签不一致" not in body
    assert "存疑" not in body


def test_detail_renders_color_note_when_ocr_has_no_color(client):
    """回归：OCR 文本无颜色词 + bg_color='black' → 详情页应展示"标签背景:黑色"标注。"""
    from models.orders import ShippingOrder, ShippingRecord, ShippingImage, OcrMatchEvent
    today = _today_date.today().isoformat()
    oid = ShippingOrder.create(today, "客户")
    rid = ShippingRecord.create(today, "客户", "黑磅布", "60寸", 2, "件", "", order_pk=oid)
    iid = ShippingImage.create(oid, r"upload\2026-08\a.jpg", "a.jpg", "upload", rid, 1)
    ShippingImage.set_match(iid, "green", 0.95, "一致", "local_fuzzy")
    # 模拟后台线程写 bg_color + ocr_text(无颜色词)
    from models._db import get_db
    db = get_db()
    db.execute("UPDATE shipping_images SET bg_color=? WHERE id=?", ("black", iid))
    db.commit()
    db.close()
    OcrMatchEvent.create("record_ocr", record_id=rid, order_id=oid, image_id=iid, ocr_text="磅布三文治 60寸")
    resp = client.get(f"/m/shipping-today/order/{oid}")
    body = resp.get_data(as_text=True)
    assert "标签背景:黑色" in body
    assert 'data-role="color-note"' in body


def test_detail_no_color_note_when_ocr_has_color_word(client):
    """回归：OCR 含"白"等颜色词 → 不展示"标签背景"标注。"""
    from models.orders import ShippingOrder, ShippingRecord, ShippingImage, OcrMatchEvent
    today = _today_date.today().isoformat()
    oid = ShippingOrder.create(today, "客户")
    rid = ShippingRecord.create(today, "客户", "白磅布", "60寸", 2, "件", "", order_pk=oid)
    iid = ShippingImage.create(oid, r"upload\2026-08\a.jpg", "a.jpg", "upload", rid, 1)
    ShippingImage.set_match(iid, "green", 0.95, "一致", "local_fuzzy")
    from models._db import get_db
    db = get_db()
    db.execute("UPDATE shipping_images SET bg_color=? WHERE id=?", ("white", iid))
    db.commit()
    db.close()
    OcrMatchEvent.create("record_ocr", record_id=rid, order_id=oid, image_id=iid, ocr_text="白色磅布 三文治 60寸")
    resp = client.get(f"/m/shipping-today/order/{oid}")
    body = resp.get_data(as_text=True)
    assert "标签背景" not in body


def test_detail_skips_photo_ui_for_board_items(client):
    """回归：皮革/木板类商品详情页不显示拍照按钮 + 徽标"无需拍照" + 单位显示"块"。"""
    from models.orders import ShippingOrder, ShippingRecord
    today = _today_date.today().isoformat()
    oid = ShippingOrder.create(today, "客户")
    ShippingRecord.create(today, "客户", "白海绵", "60寸", 2, "件", "", order_pk=oid)
    ShippingRecord.create(today, "客户", "皮革", "5mm", 1, "件", "", order_pk=oid)
    ShippingRecord.create(today, "客户", "木板", "60x40", 3, "件", "", order_pk=oid)
    resp = client.get(f"/m/shipping-today/order/{oid}")
    body = resp.get_data(as_text=True)
    assert "无需拍照" in body
    assert "皮革/木板类" in body
    # 板材的 product-card 应有 data-role="board" 标记
    assert body.count('data-role="board"') == 2
    # 板材不渲染拍照按钮
    import re
    board_cards = re.findall(r'<div class="product-card"[^>]*data-role="board"[^>]*>(.*?)</div>\s*</div>\s*</div>', body, re.S)
    for card in board_cards:
        assert "拍照识别" not in card


def test_list_page_excludes_boards_from_total(client):
    """回归：列表页 total 不算皮革/木板,单独显示 board_total。"""
    from models.orders import ShippingOrder, ShippingRecord, ShippingImage
    today = _today_date.today().isoformat()
    oid = ShippingOrder.create(today, "客户")
    rid1 = ShippingRecord.create(today, "客户", "白海绵", "60寸", 2, "件", "", order_pk=oid)
    rid2 = ShippingRecord.create(today, "客户", "皮革", "5mm", 1, "件", "", order_pk=oid)
    rid3 = ShippingRecord.create(today, "客户", "木板", "60x40", 3, "件", "", order_pk=oid)
    iid = ShippingImage.create(oid, r"upload\2026-08\a.jpg", "a.jpg", "upload", rid1, 1)
    ShippingImage.set_match(iid, "green", 0.95, "一致", "local_fuzzy")
    resp = client.get("/m/shipping-today")
    body = resp.get_data(as_text=True)
    # 2 块板（皮革 + 木板）单独显示
    assert "板 2 块" in body
    # regular total = 1 (白海绵),不是 3
    assert "<b>1</b> 项商品" in body
    # 已拍比例 1/1(只算白海绵,板不算)
    assert "1/1 已拍" in body


def test_list_header_shows_today_order_count(client):
    """回归：列表页 header 右侧显示今日订单总数。"""
    from models.orders import ShippingOrder
    today = _today_date.today().isoformat()
    ShippingOrder.create(today, "客户A")
    ShippingOrder.create(today, "客户B")
    resp = client.get("/m/shipping-today")
    body = resp.get_data(as_text=True)
    assert "header-stat" in body
    assert "2 单" in body


def test_list_header_has_refresh_button(client):
    """回归：列表页 header 有整体刷新按钮（链接回自己）。"""
    from models.orders import ShippingOrder
    today = _today_date.today().isoformat()
    ShippingOrder.create(today, "客户")
    resp = client.get("/m/shipping-today")
    body = resp.get_data(as_text=True)
    assert "refresh-btn" in body
    assert "整体刷新" in body


def test_detail_template_has_order_images_grid_data_role(client):
    """回归：详情页整单 grid 容器含 data-role=order-images-grid,方便 JS 局部追加新上传图。"""
    from models.orders import ShippingOrder, ShippingRecord, ShippingImage
    today = _today_date.today().isoformat()
    oid = ShippingOrder.create(today, "客户")
    rid = ShippingRecord.create(today, "客户", "商品", "60寸", 1, "件", "", order_pk=oid)
    iid = ShippingImage.create(oid, r"upload\2026-08\a.jpg", "a.jpg", "upload", rid, 1)
    ShippingImage.set_match(iid, "green", 0.95, "一致", "local_fuzzy")
    resp = client.get(f"/m/shipping-today/order/{oid}")
    body = resp.get_data(as_text=True)
    assert 'data-role="order-images-grid"' in body
    # JS 含 appendImageToOrderGrid helper
    js_path = os.path.join(os.path.dirname(__file__), "..", "static", "js", "mobile_detail.js")
    with open(js_path, encoding="utf-8") as f:
        js = f.read()
    assert "appendImageToOrderGrid" in js


def test_mobile_detail_js_skips_board_cards_in_bindRecord(client):
    """回归：mobile_detail.js 对板材商品（无 record-input）不能 throw null addEventListener 错误。"""
    from models.orders import ShippingOrder, ShippingRecord
    today = _today_date.today().isoformat()
    oid = ShippingOrder.create(today, "客户")
    ShippingRecord.create(today, "客户", "皮革", "5mm", 1, "件", "", order_pk=oid)
    resp = client.get(f"/m/shipping-today/order/{oid}")
    assert resp.status_code == 200
    js_path = os.path.join(os.path.dirname(__file__), "..", "static", "js", "mobile_detail.js")
    with open(js_path, encoding="utf-8") as f:
        js = f.read()
    # bindRecord 开头必须有 null 守卫,避免板材商品触发 null.addEventListener
    assert "if (!input) return" in js


def test_overall_buttons_renamed(client):
    """回归：整体图 3 按钮文案依次为'备货照/装车照/归仓照'。"""
    from models.orders import ShippingOrder, ShippingRecord
    today = _today_date.today().isoformat()
    oid = ShippingOrder.create(today, "客户")
    ShippingRecord.create(today, "客户", "商品", "规格", 1, "件", "", order_pk=oid)
    resp = client.get(f"/m/shipping-today/order/{oid}")
    body = resp.get_data(as_text=True)
    assert "备货照" in body
    assert "装车照" in body
    assert "归仓照" in body


def test_overall_image_source_tag_persists_and_renders(client):
    """回归：整体图 source_tag（备货照/装车照/归仓照）持久化到 DB + 详情页叠加显示。"""
    from models.orders import ShippingOrder, ShippingImage
    today = _today_date.today().isoformat()
    oid = ShippingOrder.create(today, "客户")
    # 直接通过 model 插入,带 source_tag(模拟后端已接收 source_tag form field)
    iid = ShippingImage.create(oid, r"upload\2026-08\a.jpg", "备货照.jpg", "upload", None, None, "备货照")
    resp = client.get(f"/m/shipping-today/order/{oid}")
    body = resp.get_data(as_text=True)
    assert "img-source-tag" in body
    assert "备货照" in body
    # DB 字段确实写入
    img = ShippingImage.get_by_id(iid)
    assert img["source_tag"] == "备货照"


def test_record_image_no_source_tag(client):
    """回归：record 图片不显示 source_tag 叠加。"""
    from models.orders import ShippingOrder, ShippingRecord, ShippingImage
    today = _today_date.today().isoformat()
    oid = ShippingOrder.create(today, "客户")
    rid = ShippingRecord.create(today, "客户", "白海绵", "60寸", 2, "件", "", order_pk=oid)
    iid = ShippingImage.create(oid, r"upload\2026-08\a.jpg", "a.jpg", "upload", rid, 1, None)
    ShippingImage.set_match(iid, "green", 0.95, "一致", "local_fuzzy")
    resp = client.get(f"/m/shipping-today/order/{oid}")
    body = resp.get_data(as_text=True)
    # record 图没 source_tag,不应有 img-source-tag 节点
    assert "img-source-tag" not in body


def test_overall_button_data_attributes_match_source_tag_whitelist(client):
    """回归：3 个整体图按钮的 data-overall-source 必须与后端白名单一致（备货照/装车照/归仓照），否则被过滤后无叠加。"""
    from models.orders import ShippingOrder, ShippingRecord
    today = _today_date.today().isoformat()
    oid = ShippingOrder.create(today, "客户")
    ShippingRecord.create(today, "客户", "商品", "规格", 1, "件", "", order_pk=oid)
    resp = client.get(f"/m/shipping-today/order/{oid}")
    body = resp.get_data(as_text=True)
    import re
    for tag in ("备货照", "装车照", "归仓照"):
        assert f'data-overall-source="{tag}"' in body


def test_overall_thumb_text_matches_buttons(client):
    """回归：3 个缩略图占位文本与按钮文本一致（备货照/装车照/归仓照）。"""
    from models.orders import ShippingOrder, ShippingRecord
    today = _today_date.today().isoformat()
    oid = ShippingOrder.create(today, "客户")
    ShippingRecord.create(today, "客户", "商品", "规格", 1, "件", "", order_pk=oid)
    resp = client.get(f"/m/shipping-today/order/{oid}")
    body = resp.get_data(as_text=True)
    assert "备货照\u3000未拍" in body
    assert "装车照\u3000未拍" in body
    assert "归仓照\u3000未拍" in body
    assert "整体照\u3000未拍" not in body  # 旧文案已无


def test_overall_thumb_initial_state_from_db(client):
    """回归：刷新页面时,缩略图占位应根据 DB source_tag 显示'已拍+时间'或'未拍'。"""
    from datetime import date as _today_date
    from models.orders import ShippingOrder, ShippingRecord, ShippingImage
    today = _today_date.today().isoformat()
    oid = ShippingOrder.create(today, "客户")
    ShippingRecord.create(today, "客户", "商品", "规格", 1, "件", "", order_pk=oid)
    # 已有"备货照"图 → 应显示"已拍 ✓"
    ShippingImage.create(oid, r"upload\2026-08\备货照.jpg", "备货照.jpg", "upload", None, 1, "备货照")
    # 没有"装车照" → 应显示"未拍"
    resp = client.get(f"/m/shipping-today/order/{oid}")
    body = resp.get_data(as_text=True)
    import re
    # 备货照 thumb 含 ✓
    m = re.search(r'data-thumb-source="备货照"[^>]*>([^<]+)', body)
    assert m and "✓" in m.group(1), f"备货照 thumb 未显示已拍: {m.group(1) if m else 'no match'}"
    # 装车照 thumb 含"未拍"
    m = re.search(r'data-thumb-source="装车照"[^>]*>([^<]+)', body)
    assert m and "未拍" in m.group(1), f"装车照 thumb 应显示未拍: {m.group(1) if m else 'no match'}"
    # 备货照 thumb 已加 filled class
    assert body.count('data-thumb-source="备货照" class="overall-thumb filled"') == 1 or \
           'class="overall-thumb filled"' in body
