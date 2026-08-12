# -*- coding: utf-8 -*-
"""移动端当天出货页面：列表 + 订单详情，仅拍照/人工确认。"""
from datetime import date as _date

from flask import Blueprint, render_template

from models.orders import ShippingOrder, ShippingRecord, ShippingImage, OcrMatchEvent
from blueprints.ocr_log import set_log_context

bp = Blueprint("mobile_shipping", __name__)

_STATUS = {"green": "✓", "yellow": "⚠", "red": "✕"}
_BG_CN = {"black": "黑色", "white": "白色"}
# 颜色关键词（与 PC 端 COMPARE_PROMPT 一致）
_COLOR_KEYWORDS = ("黑", "白", "红", "蓝", "绿", "黄", "棕", "灰", "米", "杏", "紫", "粉", "橙")
_BOARD_KEYWORDS = ("皮革", "木板")


def _is_board(product_name: str) -> bool:
    """是否为皮革/木板类商品(无需拍照,按"块"计数)。"""
    return any(k in (product_name or "") for k in _BOARD_KEYWORDS)


def _has_color_word(text: str) -> bool:
    """OCR 文本是否含颜色关键词。"""
    return any(c in text for c in _COLOR_KEYWORDS)


def _color_note(image: dict) -> str | None:
    """若该图 OCR 没颜色词且背景色被识别,返回中文颜色('黑色'/'白色');否则 None。"""
    bg = image.get("bg_color")
    if not bg:
        return None
    evt = OcrMatchEvent.get_latest_by_image(image["id"], "record_ocr")
    ocr_text = (evt or {}).get("ocr_text") or ""
    if _has_color_word(ocr_text):
        return None
    return _BG_CN.get(bg, bg)


def _latest_status(images) -> "str | None":
    """取最新一张图片的状态(get_by_record 已按 sort_order ASC, id ASC 排,末尾为最新)。无图返回 None。"""
    if not images:
        return None
    last = images[-1]
    return last.get("match_status") or "green"


def _status_badge(status) -> "tuple[str, str]":
    """返回 (badge_class, badge_text);status=None 时 todo/待拍。"""
    if status == "green":
        return ("done", "✓ 通过")
    if status == "yellow":
        return ("warn", "⚠ 待确认")
    if status == "red":
        return ("warn", "✕ 不符")
    return ("todo", "待拍")


def _summarize_group(group: dict) -> dict:
    records = group.get("records", [])
    regular = [r for r in records if not _is_board(r["product_name"])]
    boards = [r for r in records if _is_board(r["product_name"])]
    total = len(regular)
    board_total = len(boards)
    has_image = 0
    stats = {"green": 0, "yellow": 0, "red": 0}
    for r in regular:
        images = ShippingImage.get_by_record(r["id"])
        if images:
            has_image += 1
            # 取最差档（红 > 黄 > 绿）
            worst = "green"
            for img in images:
                st = img.get("match_status") or "green"
                if st == "red":
                    worst = "red"
                    break
                if st == "yellow" and worst == "green":
                    worst = "yellow"
            if worst in stats:
                stats[worst] += 1
    return {**group, "total": total, "board_total": board_total, "has_image": has_image, "stats": stats}


@bp.route("/m/shipping-today")
def shipping_today():
    today = _date.today().isoformat()
    groups = ShippingRecord.get_groups(today, today)
    groups = [_summarize_group(g) for g in groups]
    return render_template("mobile/shipping-today.html", today=today, groups=groups)


@bp.route("/m/shipping-today/order/<int:oid>")
def shipping_order_detail(oid: int):
    set_log_context(biz="mobile_shipping", order_id=oid)
    order = ShippingOrder.get_by_id(oid)
    if order is None:
        from flask import abort
        abort(404)
    groups = ShippingRecord.get_groups(order["date"], order["date"])
    records = next(
        (group.get("records", []) for group in groups if group.get("id") == oid),
        [],
    )
    # 服务端拉取每个 record 的图片(仅 record 自身,不含共享)用于初始状态渲染
    def _with_rel(imgs):
        return [{**img, "rel_path": ShippingImage.get_relative_path(img["file_path"])} for img in imgs]
    record_images = {
        rec["id"]: _with_rel(ShippingImage.get_by_record(rec["id"]))
        for rec in records
    }
    # 整单所有图片(共享 + record),展示一次。_with_rel 已在 record_images 块内复用
    order_images = _with_rel(ShippingImage.get_by_order(oid))
    record_states = {}
    for rec in records:
        imgs = record_images[rec["id"]]
        record_states[rec["id"]] = {
            "images": imgs,
            "status": _latest_status(imgs),
            "color_note": _color_note(imgs[-1]) if imgs else None,
            "is_board": _is_board(rec["product_name"]),
        }
    return render_template(
        "mobile/shipping-order.html",
        order=order,
        records=records,
        record_states=record_states,
        order_images=order_images,
    )
