"""人员管理蓝图 (T7)。

人员档案 CRUD 页 — 跟 T8 车辆档案同一风格(POST + flash + redirect,
inline 编辑行、软删 + 启用、show_disabled 切换)。

端点:
- GET  /staff                  列表 + 新增表单(支持 ?role= 过滤 & show_disabled=1)
- POST /staff/add              创建
- POST /staff/update/<sid>     更新
- POST /staff/delete/<sid>     软删(is_active=0)
- POST /staff/enable/<sid>     恢复(is_active=1)

受全局 _require_login gate 保护 — 未登录会被踢去 /login。
"""
from flask import Blueprint, render_template, request, redirect, url_for, flash

from models.tasks_flow import StaffDB, VehicleDB


bp = Blueprint("staff", __name__)


# 角色枚举(schema CHECK 约束的 6 个值,跟 StaffDB.get_active 的排序一致)
ROLE_OPTIONS = ["司机", "调度", "搬运", "打码", "仓管", "文员"]


# ── 列表 + 新增表单 ──────────────────────────────────────────────
@bp.route("/staff")
def staff_list():
    """人员档案主页 — 支持 role 过滤 + show_disabled 切换。

    注入:
    - staff_list: 过滤后的 staff 列表(默认只看 active)
    - vehicle_list: 全启用车辆(给表单 vehicle_id 下拉用)
    - role_options: 6 个固定角色
    """
    show_disabled = request.args.get("show_disabled") == "1"
    filter_role = (request.args.get("role") or "").strip()

    staff_list = StaffDB.get_all(
        role=filter_role or None,
        include_inactive=show_disabled,
    )
    # 车辆下拉:用 get_all 默认行为(只看启用)— 跟 T8 一致
    vehicle_list = VehicleDB.get_all()

    return render_template(
        "staff.html",
        staff_list=staff_list,
        vehicle_list=vehicle_list,
        role_options=ROLE_OPTIONS,
        filter_role=filter_role,
        show_disabled=show_disabled,
        page_title="人员档案",
    )


# ── 创建 ──────────────────────────────────────────────
@bp.route("/staff/add", methods=["POST"])
def staff_add():
    """新增人员 — name + role 必填,其他可选。

    role 不在 6 个枚举里 → 拒掉,不让入库。
    """
    name = request.form.get("name", "").strip()
    role = request.form.get("role", "").strip()
    if not name:
        flash("姓名不能为空", "error")
        return redirect(url_for("staff.staff_list"))
    if role not in ROLE_OPTIONS:
        flash(f"角色必须是 {ROLE_OPTIONS} 之一", "error")
        return redirect(url_for("staff.staff_list"))

    payload = _parse_staff_form(request.form)
    payload["is_active"] = 1
    try:
        row = StaffDB.create(name=name, role=role, **payload)
        flash(f"已添加人员: {name} (#{row['id']}, {role})", "success")
    except Exception as e:
        flash(f"添加失败:{e}", "error")
    return redirect(url_for("staff.staff_list"))


# ── 更新 ──────────────────────────────────────────────
@bp.route("/staff/update/<int:sid>", methods=["POST"])
def staff_update(sid: int):
    """更新人员 — name + role 都允许改,但 role 仍必须在 6 选 1。"""
    existing = StaffDB.get_by_id(sid)
    if not existing:
        flash("人员不存在", "error")
        return redirect(url_for("staff.staff_list"))

    name = request.form.get("name", "").strip()
    role = request.form.get("role", "").strip()
    if not name:
        flash("姓名不能为空", "error")
        return redirect(url_for("staff.staff_list"))
    if role not in ROLE_OPTIONS:
        flash(f"角色必须是 {ROLE_OPTIONS} 之一", "error")
        return redirect(url_for("staff.staff_list"))

    payload = _parse_staff_form(request.form)
    payload["name"] = name
    payload["role"] = role
    try:
        StaffDB.update(sid, **payload)
        flash(f"已更新人员 #{sid}: {name}", "success")
    except Exception as e:
        flash(f"更新失败:{e}", "error")
    return redirect(url_for("staff.staff_list"))


# ── 软删 ──────────────────────────────────────────────
@bp.route("/staff/delete/<int:sid>", methods=["POST"])
def staff_delete(sid: int):
    """停用人员(软删)— is_active=0,行不删。

    注意:即使该人员正在被某 task 的 driver_id/operator_id/coder_id 引用,
    软删不会触发 FK ON DELETE SET NULL(因为行还在)。
    """
    s = StaffDB.get_by_id(sid)
    if not s:
        flash("人员不存在", "error")
    else:
        StaffDB.delete(sid)
        flash(f"已停用人员: {s['name']}", "success")
    return redirect(url_for(
        "staff.staff_list",
        show_disabled=request.args.get("show_disabled"),
    ))


# ── 复用恢复(把停用的重新启用) ──────────────────────────────────────────────
@bp.route("/staff/enable/<int:sid>", methods=["POST"])
def staff_enable(sid: int):
    """恢复停用人员 → is_active=1。"""
    s = StaffDB.get_by_id(sid)
    if not s:
        flash("人员不存在", "error")
    else:
        StaffDB.update(sid, is_active=1)
        flash(f"已启用人员: {s['name']}", "success")
    return redirect(url_for(
        "staff.staff_list",
        show_disabled=request.args.get("show_disabled"),
    ))


# ── helpers ──────────────────────────────────────────────
def _parse_staff_form(form) -> dict:
    """把 request.form 里 optional 字段解析出来 — 空字符串统一转 None。

    这样 DB 里就是 NULL 而不是 '' 字符串。
    vehicle_id:空值/无 → None(没绑车);非空 → int。
    """

    def _opt(name):
        v = form.get(name, "").strip()
        return v if v else None

    def _int_or_none(name):
        v = _opt(name)
        if v is None:
            return None
        try:
            return int(v)
        except (ValueError, TypeError):
            return None

    return {
        "gender": _opt("gender"),
        "birth_date": _opt("birth_date"),
        "staff_code": _opt("staff_code"),
        "phone": _opt("phone"),
        "vehicle_id": _int_or_none("vehicle_id"),
        "login_name": _opt("login_name"),
    }