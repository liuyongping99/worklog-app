# -*- coding: utf-8 -*-
"""移动端当天出货页面：列表 + 订单详情，仅拍照/人工确认。"""
from datetime import date as _date

from flask import Blueprint, render_template

from models.orders import ShippingOrder, ShippingRecord

bp = Blueprint("mobile_shipping", __name__)


@bp.route("/m/shipping-today")
def shipping_today():
    today = _date.today().isoformat()
    groups = ShippingRecord.get_groups(today, today)
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
