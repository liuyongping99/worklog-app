# -*- coding: utf-8 -*-
"""移动端当天出货页面：列表 + 订单详情，仅拍照/人工确认。"""
import re
from datetime import date as _date

from flask import Blueprint, render_template

from models.orders import ShippingOrder, ShippingRecord, ShippingImage, PlacementImage, OcrMatchEvent
from blueprints.ocr_log import set_log_context
from blueprints.shipping import _enrich_copy_paper_for_item, COPY_PAPER_LABEL_SOURCE
from blueprints._helpers import parse_loose_yards

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
    """取最新一张 OCR/AI 比对图的状态(排除 placement 摆放图;新拍覆盖旧拍)。
    黄/红 + 已人工确认(human_verified=1) → 视为 green(人工覆盖)。无图返回 None。"""
    ocr = [i for i in images if i.get("source") != "placement"]
    if not ocr:
        return None
    last = ocr[-1]
    st = last.get("match_status") or "green"
    if last.get("human_verified") and st in ("yellow", "red"):
        return "green"
    return st


def _status_badge(status) -> "tuple[str, str]":
    """返回 (badge_class, badge_text);status=None 时 todo/待拍。"""
    if status == "green":
        return ("done", "✓ 通过")
    if status == "yellow":
        return ("warn", "⚠ 待确认")
    if status == "red":
        return ("warn", "✕ 不符")
    return ("todo", "待拍")


def _placement_compare(rec: dict) -> dict:
    """摆放图清点结果:支合计/散码合计 与 备注 分开比较,供移动端订单页点数按钮下方展示。
    2026-08-19:与 PC 端 _eff_zhi 保持一致——直接输入的 manual_count 优先于点击计数 n_marks,
    避免「点数弹框里输入了 X 支,但订单页却按 n_marks 对不上备注」的割裂。
    """
    pimgs = PlacementImage.get_by_record(rec["id"])
    # 摆放图"支数"口径:manual_count 优先,无则回退点击计数点(n_marks)
    def _eff_zhi(p):
        mc = p.get("manual_count")
        return mc if mc is not None else (p.get("n_marks") or 0)
    zhi_actual = sum(_eff_zhi(p) for p in pimgs)
    loose_actual = sum(p.get("loose_count", 0) for p in pimgs)
    remark = rec.get("remark") or ""
    zhi_m = list(re.finditer(r"(\d+)\s*支", remark))
    expected_zhi = sum(int(x.group(1)) for x in zhi_m)
    has_zhi = len(zhi_m) > 0
    # 2026-10-09: 散码解析统一调 parse_loose_yards(先抠掉「X支*Yy」乘法项)
    expected_san, has_san = parse_loose_yards(remark)
    zhi_match = (not has_zhi) or zhi_actual == expected_zhi
    san_match = (not has_san) or loose_actual == expected_san
    return {
        "has_placement": len(pimgs) > 0,
        "zhi_actual": zhi_actual,
        "zhi_expected": expected_zhi,
        "has_zhi": has_zhi,
        "zhi_match": zhi_match,
        "loose_actual": loose_actual,
        "loose_expected": expected_san,
        "has_san": has_san,
        "san_match": san_match,
        "all_match": zhi_match and san_match,
    }


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
            # 取最新一张图的状态(与详情页 _latest_status 一致:新拍覆盖旧拍)
            st = _latest_status(images)
            if st in stats:
                stats[st] += 1
    return {**group, "total": total, "board_total": board_total, "has_image": has_image, "stats": stats}


@bp.route("/m/")
def mobile_index():
    """移动端入口页:聚合今日出货 / 今日入库两个入口。"""
    return render_template("mobile/index.html")


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
    # 2026-08-30: enrich placement 图的 n_marks / signed_count(本单图片网格显示支数/散码 overlay 用),
    # get_by_order 不像 get_by_record 那样 enrich,这里补齐
    for _img in order_images:
        if _img.get("source") == "placement":
            _marks = PlacementImage.get_marks(_img["id"])
            _n = len(_marks)
            _mc = _img.get("manual_count")
            _eff = _mc if _mc is not None else _n
            _img["n_marks"] = _n
            _img["effective_zhi"] = _eff
            _img["signed_count"] = -_eff if _img.get("is_unload") else _eff
    record_states = {}
    for rec in records:
        # 2026-09-06: enrich copy-paper 字段(同步 PC shipping-records 的 _enrich_copy_paper_for_item)
        _enrich_copy_paper_for_item(rec)
        imgs = record_images[rec["id"]]
        # 2026-09-09: 免 AI 比对标签图(source='copy_paper_label')不做 OCR,不算 OCR 图,
        # 否则会被当成"最后一张 OCR 图"影响状态/详情链接
        ocr_imgs = [
            i for i in imgs
            if i.get("source") not in ("placement", COPY_PAPER_LABEL_SOURCE)
        ]
        last_ocr = ocr_imgs[-1] if ocr_imgs else None
        base = (last_ocr.get("match_status") or "green") if last_ocr else None
        human_confirmed = bool(
            last_ocr and last_ocr.get("human_verified") and base in ("yellow", "red")
        )
        record_states[rec["id"]] = {
            "images": imgs,
            "ocr_images": ocr_imgs,
            "status": _latest_status(imgs),
            "human_confirmed": human_confirmed,
            "color_note": _color_note(last_ocr) if last_ocr else None,
            "is_board": _is_board(rec["product_name"]),
            "placement": _placement_compare(rec),
        }
    # 整体图缩略图初始状态(按 source_tag):None=未拍,否则显示时间
    from datetime import datetime as _dt
    thumb_states = {}
    for tag in ("备货照", "装车照", "归仓照"):
        match = [img for img in order_images if img.get("source_tag") == tag]
        if match:
            last = match[-1]
            try:
                t = _dt.strptime(last["created_at"], "%Y-%m-%d %H:%M:%S")
                ts = f"{t.hour:02d}:{t.minute:02d}"
            except Exception:
                ts = ""
            thumb_states[tag] = {"taken": True, "time_text": ts}
        else:
            thumb_states[tag] = {"taken": False, "time_text": ""}
    return render_template(
        "mobile/shipping-order.html",
        order=order,
        records=records,
        record_states=record_states,
        order_images=order_images,
        thumb_states=thumb_states,
    )


@bp.route("/m/shipping-today/order/<int:oid>/placement/<int:record_id>")
def shipping_placement_count(oid: int, record_id: int):
    """移动端点数页:复用 PC 摆放图后端机制,手机拍照 + 手指点击计数。"""
    set_log_context(biz="mobile_shipping", order_id=oid, record_id=record_id)
    order = ShippingOrder.get_by_id(oid)
    rec = ShippingRecord.get_by_id(record_id)
    if order is None or rec is None or rec.get("order_pk") != oid:
        from flask import abort
        abort(404)
    return render_template(
        "mobile/placement-count.html",
        order=order,
        rec=rec,
        oid=oid,
    )
