# -*- coding: utf-8 -*-
"""移动端当天出货页面：列表 + 订单详情，仅拍照/人工确认。"""
from datetime import date as _date

from flask import Blueprint, render_template

from models.orders import ShippingOrder, ShippingRecord, ShippingImage
from blueprints.ocr_log import set_log_context

bp = Blueprint("mobile_shipping", __name__)

_STATUS = {"green": "✓", "yellow": "⚠", "red": "✕"}


def _worst_status(images) -> "str | None":
    """取最差档:红 > 黄 > 绿;无图返回 None。"""
    if not images:
        return None
    worst = "green"
    for img in images:
        st = img.get("match_status") or "green"
        if st == "red":
            return "red"
        if st == "yellow" and worst == "green":
            worst = "yellow"
    return worst


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
    total = len(records)
    has_image = 0
    stats = {"green": 0, "yellow": 0, "red": 0}
    for r in records:
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
        # 无图不计入统计
    return {**group, "total": total, "has_image": has_image, "stats": stats}


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
    record_states = {
        rec["id"]: {
            "images": record_images[rec["id"]],
            "status": _worst_status(record_images[rec["id"]]),
        }
        for rec in records
    }
    # 整单所有图片(共享 + record),展示一次
    order_images = _with_rel(ShippingImage.get_by_order(oid))
    return render_template(
        "mobile/shipping-order.html",
        order=order,
        records=records,
        record_states=record_states,
        order_images=order_images,
    )
