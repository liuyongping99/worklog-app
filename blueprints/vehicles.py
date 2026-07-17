"""车辆管理蓝图 (T8)。

车辆档案 CRUD 页 — 跟商品管理同一风格(POST + flash + redirect),
复用 `vehicle-maintenance.html` 的视觉语言(红色主题 / 车牌徽标)。

端点:
- GET  /vehicles                 列表 + 新增表单
- POST /vehicles/add             创建
- POST /vehicles/update/<vid>    更新
- POST /vehicles/delete/<vid>    软删(status='停用')

不强制单独 login gate — 全局 app.py 的 _require_login 已经保护了
受 gate 影响的页面(`/vehicles` 不在白名单里),未登录会自动跳 /login。
"""
from flask import Blueprint, render_template, request, redirect, url_for, flash

from models.tasks_flow import VehicleDB


bp = Blueprint("vehicles", __name__)


# ── 列表 + 新增表单 ──────────────────────────────────────────────
@bp.route("/vehicles")
def vehicles_list():
    """车辆档案主页 — 默认不显示停用的,但顶部有一个开关可看全部。"""
    show_disabled = request.args.get("show_disabled") == "1"
    vehicles = VehicleDB.get_all(include_disabled=show_disabled)
    return render_template(
        "vehicles.html",
        vehicles=vehicles,
        show_disabled=show_disabled,
        page_title="车辆档案",
    )


# ── 创建 ──────────────────────────────────────────────
@bp.route("/vehicles/add", methods=["POST"])
def vehicles_add():
    """新增车辆 — plate_no 必填,其他可选。

    重复 plate_no 由 DB 的 UNIQUE 约束兜住,捕获后 flash 错误,不让入库。
    """
    plate_no = request.form.get("plate_no", "").strip()
    if not plate_no:
        flash("车牌号不能为空", "error")
        return redirect(url_for("vehicles.vehicles_list"))

    payload = _parse_vehicle_form(request.form)
    try:
        vid = VehicleDB.create(plate_no=plate_no, **payload)
        flash(f"已添加车辆: {plate_no} (#{vid})", "success")
    except Exception as e:
        flash(f"添加失败:{e}", "error")
    return redirect(url_for("vehicles.vehicles_list"))


# ── 更新 ──────────────────────────────────────────────
@bp.route("/vehicles/update/<int:vid>", methods=["POST"])
def vehicles_update(vid: int):
    """更新车辆 — plate_no 也允许改(但仍受 UNIQUE 约束)。"""
    existing = VehicleDB.get_by_id(vid)
    if not existing:
        flash("车辆不存在", "error")
        return redirect(url_for("vehicles.vehicles_list"))

    plate_no = request.form.get("plate_no", "").strip()
    if not plate_no:
        flash("车牌号不能为空", "error")
        return redirect(url_for("vehicles.vehicles_list"))

    payload = _parse_vehicle_form(request.form)
    payload["plate_no"] = plate_no
    try:
        VehicleDB.update(vid, **payload)
        flash(f"已更新车辆 #{vid}: {plate_no}", "success")
    except Exception as e:
        flash(f"更新失败:{e}", "error")
    return redirect(url_for("vehicles.vehicles_list"))


# ── 软删 ──────────────────────────────────────────────
@bp.route("/vehicles/delete/<int:vid>", methods=["POST"])
def vehicles_delete(vid: int):
    """停用车辆(软删) — 行不删,status 改为 '停用'。

    已经被指派给人员的车辆也能停用(T7 的 staff.vehicle_id 是 FK,
    ON DELETE SET NULL,但软删不触发 SET NULL,因为行还在)。
    """
    v = VehicleDB.get_by_id(vid)
    if not v:
        flash("车辆不存在", "error")
    else:
        VehicleDB.delete(vid)
        flash(f"已停用车辆: {v['plate_no']}", "success")
    return redirect(url_for("vehicles.vehicles_list"))


# ── 复用恢复(把停用的重新启用) ──────────────────────────────────────────────
@bp.route("/vehicles/enable/<int:vid>", methods=["POST"])
def vehicles_enable(vid: int):
    """恢复停用车辆 → status='启用'。"""
    v = VehicleDB.get_by_id(vid)
    if not v:
        flash("车辆不存在", "error")
    else:
        VehicleDB.update(vid, status="启用")
        flash(f"已启用车辆: {v['plate_no']}", "success")
    return redirect(url_for(
        "vehicles.vehicles_list", show_disabled=request.args.get("show_disabled")
    ))


# ── helpers ──────────────────────────────────────────────
def _parse_vehicle_form(form) -> dict:
    """把 request.form 里 optional 字段解析出来 — 空字符串统一转 None。

    这样 DB 里就是 NULL 而不是 '' 字符串,跟 vehicles.html 渲染一致。
    """
    def _opt(name):
        v = form.get(name, "").strip()
        return v if v else None

    def _num(name):
        v = _opt(name)
        if v is None:
            return None
        try:
            return float(v)
        except (ValueError, TypeError):
            return None

    return {
        "tonnage": _num("tonnage"),
        "length": _num("length"),
        "width": _num("width"),
        "height": _num("height"),
        "inspection_date": _opt("inspection_date"),
        "note": _opt("note"),
    }