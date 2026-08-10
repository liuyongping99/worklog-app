# -*- coding: utf-8 -*-
"""移动端当天出货页面：列表 + 订单详情，仅拍照/人工确认。"""
from datetime import date as _date

from flask import Blueprint, render_template

from models.orders import ShippingOrder, ShippingRecord, ShippingImage

bp = Blueprint("mobile_shipping", __name__)

_STATUS = {"green": "✓", "yellow": "⚠", "red": "✕"}


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
    order = ShippingOrder.get_by_id(oid)
    if order is None:
        from flask import abort
        abort(404)
    groups = ShippingRecord.get_groups(order["date"], order["date"])
    records = next(
        (group.get("records", []) for group in groups if group.get("id") == oid),
        [],
    )
    return render_template(
        "mobile/shipping-order.html",
        order=order,
        records=records,
    )
