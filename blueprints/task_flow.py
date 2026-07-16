"""任务流 M1 — REST 蓝图。

当前任务(Task 10)只实现 **advance** 端点（状态机主线 6 态推进 + 闸门）。
后续任务(T9/T11/T12/T13)会补 assign / coding_claim / coding_done /
coding_release / driver_release / task_cancel / return_create / list / detail
等端点。

路由设计:
- POST /api/v1/tasks/<tid>/advance  状态推进

权限 / 证据闸门:
- 推进前先校验 to_status 是否在 ALLOWED_TRANSITIONS 内
- 准备中 → 已装货 需要指派司机 + coding_status 已完成/无需
- 已装货 → 已点数 需要装车照
- 已点数 → 已到达 需要点数标签照/点数整体照(任一)
- 已到达 → 已卸货 需要卸货照
- 已卸货 → 已完成 需要签收单
- 推进动作走 can(op, Action.X, t) 集中校验
"""
from datetime import datetime
from flask import Blueprint, request, jsonify, session

from models._permissions import Action, can
from models.tasks_flow import TaskDB, TaskImageDB, TaskEventDB


bp = Blueprint("task_flow", __name__)


# ============================================================
# 状态机配置
# ============================================================

ALLOWED_TRANSITIONS = {
    "准备中": ["已装货"],
    "已装货": ["已点数"],
    "已点数": ["已到达"],
    "已到达": ["已卸货"],
    "已卸货": ["已完成"],
    # "已拒收" 是终态,不从这里推进
}

# (from_status, to_status) → 必填证据照 stage 列表(任一即可)
EVIDENCE_GATES = {
    ("已装货", "已点数"): ("装车照",),
    ("已点数", "已到达"): ("点数标签照", "点数整体照"),
    ("已到达", "已卸货"): ("卸货照",),
    ("已卸货", "已完成"): ("签收单",),
}

# (from_status → action 映射) — from 当前状态,做这个动作要 can(op, action, t)
GATE_TO_ACTION = {
    "已装货": Action.LOAD,
    "已点数": Action.COUNT,
    "已到达": Action.ARRIVE,
    "已卸货": Action.UNLOAD,
}


# ============================================================
# Helpers
# ============================================================

def _current_operator():
    """从 session 拿当前操作员(单用户环境也是从 session 里取,后续 T13 改造为登录态)。

    session['operator_id'] 是 int(Staff.id)。
    返回 StaffDB.get_by_id(...) 的 dict(找不到时返回 None)。
    """
    op_id = session.get("operator_id")
    if op_id is None:
        return None
    from models.tasks_flow import StaffDB
    return StaffDB.get_by_id(op_id)


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# ============================================================
# 端点
# ============================================================

@bp.route("/api/v1/tasks/<int:tid>/advance", methods=["POST"])
def advance(tid):
    """推进任务状态。Body: {to_status, note?}"""
    op = _current_operator()
    if op is None:
        return jsonify(success=False, error="未登录"), 401

    t = TaskDB.get_by_id(tid)
    if not t:
        return jsonify(success=False, error="任务不存在"), 404

    body = request.get_json(silent=True) or {}
    to = body.get("to_status")
    if not to:
        return jsonify(success=False, error="缺少 to_status"), 400

    # 1. 合法跳转
    if to not in ALLOWED_TRANSITIONS.get(t["status"], []):
        return jsonify(success=False, error=f"非法跳转 {t['status']} → {to}"), 400

    # 2. 装货前置:必须已指派司机 + 打码完成
    if t["status"] == "准备中" and to == "已装货":
        if not t["driver_id"]:
            return jsonify(success=False, error="未指派司机,禁止装货"), 400
        if t["coding_status"] not in ("无需打码", "打码完成"):
            return jsonify(success=False, error="打码未完成,禁止装货"), 400

    # 3. 证据闸门
    required_stages = EVIDENCE_GATES.get((t["status"], to))
    if required_stages:
        imgs = TaskImageDB.get_by_task(tid)
        have_stages = {i["stage"] for i in imgs}
        if not any(s in have_stages for s in required_stages):
            return jsonify(
                success=False,
                error=f"缺证据照(需要 {required_stages} 之一)",
            ), 400

    # 4. 动作权限(由 can() 集中校验)
    # 注:brief 原稿写 `GATE_TO_ACTION.get(t["status"])`(按 from_status 取),
    # 但映射里的 LOAD/COUNT/ARRIVE/UNLOAD 显然是"进入该状态需要的动作",
    # 按 from_status 取会让 文员 无法推进 已装货→已点数(因为 已装货→LOAD→
    # 只允许司机)。这里按 to_status 取,与测试用例语义一致。
    action = GATE_TO_ACTION.get(to)
    if action is not None and not can(op, action, t):
        return jsonify(success=False, error="无权限"), 403

    # 5. 推进 + 副作用时间戳
    extra: dict = {}
    if to == "已装货":
        extra["depart_at"] = _now_str()
    elif to == "已到达":
        extra["arrive_at"] = _now_str()

    TaskDB.update(tid, status=to, **extra)
    TaskEventDB.create(
        tid,
        "advance",
        from_status=t["status"],
        to_status=to,
        operator_id=op["id"],
        note=body.get("note"),
    )
    return jsonify(success=True, task_id=tid, status=to)


# ============================================================
# 占位(后续任务 T9/T11/T12/T13 补全)
# ============================================================

@bp.route("/api/v1/tasks/<int:tid>/assign", methods=["POST"])
def assign(tid):  # noqa: D401 - placeholder
    return jsonify(success=False, error="未实现:assign"), 501


@bp.route("/api/v1/tasks/<int:tid>/coding-claim", methods=["POST"])
def coding_claim(tid):  # noqa: D401 - placeholder
    return jsonify(success=False, error="未实现:coding_claim"), 501


@bp.route("/api/v1/tasks/<int:tid>/coding-done", methods=["POST"])
def coding_done(tid):  # noqa: D401 - placeholder
    return jsonify(success=False, error="未实现:coding_done"), 501


@bp.route("/api/v1/tasks/<int:tid>/coding-release", methods=["POST"])
def coding_release(tid):  # noqa: D401 - placeholder
    return jsonify(success=False, error="未实现:coding_release"), 501


@bp.route("/api/v1/tasks/<int:tid>/driver-release", methods=["POST"])
def driver_release(tid):  # noqa: D401 - placeholder
    return jsonify(success=False, error="未实现:driver_release"), 501


@bp.route("/api/v1/tasks/<int:tid>/cancel", methods=["POST"])
def cancel(tid):  # noqa: D401 - placeholder
    return jsonify(success=False, error="未实现:cancel"), 501