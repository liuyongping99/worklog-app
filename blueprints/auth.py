"""登录选身份蓝图(T6)。

设计:
- **无密码**:本地单用户/小团队场景,选人即可登录,不引入 bcrypt/session token。
  后续多人化(阶段 4)再考虑密码 + 权限升级。
- session 存 `operator_id`(int,Staff.id),模板/blueprint 都从这里读操作员。
- /login 自己不被全局 before_request gate 拦截(在 app.py 里白名单)。

端点:
- GET  /login   渲染登录页(下拉列 active staff)
- POST /login   校验 staff_id,设 session,跳 /
- GET  /logout  清 session,跳 /login
"""
from flask import (
    Blueprint, render_template, request, redirect, url_for, session, flash,
)

from models.tasks_flow import StaffDB


bp = Blueprint("auth", __name__)


@bp.route("/login", methods=["GET"])
def login():
    """渲染登录页 — 不需要登录就能访问(否则套娃)。"""
    # 已登录的用户访问 /login 时,直接跳 /
    if session.get("operator_id"):
        return redirect(url_for("info_pages.index"))
    staff_list = StaffDB.get_active()
    return render_template(
        "login.html",
        staff_list=staff_list,
        page_title="登录",
    )


@bp.route("/login", methods=["POST"])
def login_post():
    """提交登录表单:校验 staff_id(必须 is_active=1),设 session,跳 /"""
    raw = request.form.get("staff_id", "").strip()
    if not raw:
        flash("请选择一个人", "error")
        return redirect(url_for("auth.login"))
    try:
        staff_id = int(raw)
    except (ValueError, TypeError):
        flash("无效的人员 id", "error")
        return redirect(url_for("auth.login"))

    staff = StaffDB.get_by_id(staff_id)
    if not staff or staff.get("is_active") != 1:
        flash("该人员不存在或已停用", "error")
        return redirect(url_for("auth.login"))

    # 写入 session,后续请求的 inject_current_operator 会自动读取
    session["operator_id"] = staff["id"]
    session.permanent = True
    return redirect(url_for("info_pages.index"))


@bp.route("/logout", methods=["GET", "POST"])
def logout():
    """清 session,跳回登录页。GET 用于 <a> 直链,POST 用于下拉菜单 fetch。"""
    session.pop("operator_id", None)
    flash("已退出登录", "success")
    return redirect(url_for("auth.login"))
