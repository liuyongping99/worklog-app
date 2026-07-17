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
import os
import sqlite3
import uuid
import logging
from datetime import datetime

from flask import Blueprint, request, jsonify, session, render_template, abort

from models._permissions import Action, can
from models.tasks_flow import TaskDB, TaskItemDB, TaskImageDB, TaskEventDB


bp = Blueprint("task_flow", __name__)

logger = logging.getLogger(__name__)


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


def _clean(v):
    """空字符串 → None(避免 source_order_no 等 UNIQUE 字段空串撞约束)。Fix 7。"""
    if v is None:
        return None
    s = str(v).strip()
    return s if s else None


def _gen_task_no() -> str:
    """TD-YYYYMMDD-NNNN — NNNN 是当日已建任务数 +1。

    简单乐观锁(N+1):并发极低(单人用户)下足够;并发高了应改 UNIQUE+重试。
    """
    today = datetime.now().strftime("%Y%m%d")
    prefix = f"TD-{today}-"
    conn = __import__("models._db", fromlist=["get_db"]).get_db()
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM tasks WHERE task_no LIKE ?",
        (prefix + "%",),
    ).fetchone()
    return f"{prefix}{(row['c'] if row else 0) + 1:04d}"


# 任务图片允许的 stage 白名单(对应证据链采集节点)
ALLOWED_STAGES = {
    "识别原图", "装车照", "点数标签照", "点数整体照",
    "卸货照", "签收单", "打码照", "退单照", "退货照",
}


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
# 端点:T9b — 建单 / 列表 / 详情 / 上传 / OCR + assign 实现
# ============================================================

@bp.route("/api/v1/tasks", methods=["POST"])
def create_task():
    """建单:Body 包含 items[] + 可选 source_order_no / task_type / source_type /
    customer / dest_address / driver_id / vehicle_id / remark。

    事务(Fix 4):TaskDB.create + 循环 TaskItemDB.create + TaskEventDB.create
    全部 commit=False,最后由本端点统一 commit;失败时 rollback,不留孤儿 task。
    """
    op = _current_operator()
    if not can(op, Action.CREATE_TASK):
        return jsonify(success=False, error="无权限建单"), 403
    data = request.get_json(force=True, silent=True) or {}
    from models._db import get_db
    db = get_db()
    try:
        task_no = data.get("task_no") or _gen_task_no()
        tid = TaskDB.create(
            task_no=task_no,
            creator_id=op["id"],
            source_order_no=_clean(data.get("source_order_no")),  # Fix 7
            task_type=data.get("task_type", "送货"),
            source_type=_clean(data.get("source_type")),
            customer=_clean(data.get("customer")),
            dest_address=_clean(data.get("dest_address")),
            driver_id=data.get("driver_id"),
            vehicle_id=data.get("vehicle_id"),
            remark=_clean(data.get("remark")),
            commit=False,
            conn=db,
        )
    except (ValueError, sqlite3.IntegrityError) as e:
        db.rollback()
        return jsonify(success=False, error=str(e)), 400
    except Exception:
        db.rollback()
        raise

    try:
        for i, item in enumerate(data.get("items", [])):
            TaskItemDB.create(
                task_id=tid,
                product_name=item["product_name"],
                specification=item.get("specification"),
                quantity=item["quantity"],
                unit=item.get("unit", "y"),
                remark=item.get("remark"),
                sort_order=i,
                commit=False,
                conn=db,
            )

        TaskEventDB.create(
            task_id=tid, event_type="assign",
            operator_id=op["id"],
            from_status=None, to_status="准备中",
            note="建单",
            commit=False,
            conn=db,
        )
        db.commit()
    except Exception:
        db.rollback()
        logger.exception("create_task 事务失败,已 rollback")
        return jsonify(success=False, error="建单失败,请重试"), 500

    return jsonify(success=True, task_id=tid, task_no=task_no)


@bp.route("/tasks", methods=["GET"])
def list_view():
    """列表视图(渲染模板,M2 才有 tasks.html,现在仅靠 status 过滤 task 列表)。"""
    op = _current_operator()
    tab = request.args.get("tab", "all")
    status_map = {
        "pending":     ["准备中", "待打码", "打码中"],     # 待接单
        "in_progress": ["已装货", "已点数", "已到达"],     # 进行中
        "done":        ["已卸货", "已完成"],
        # cancelled 走 include_cancelled=True
    }
    if tab == "cancelled":
        tasks = TaskDB.get_all(include_cancelled=True)
        tasks = [t for t in tasks if t["is_cancelled"] == 1]
    elif tab == "all":
        tasks = TaskDB.get_all(include_cancelled=False)
    else:
        tasks = [
            t for t in TaskDB.get_all(include_cancelled=False)
            if t["status"] in status_map.get(tab, [])
        ]
    # 模板已就绪 → 直接 render,任何 Jinja 错误应让 Flask 抛 500(Fix 3)。
    return render_template("tasks.html", tasks=tasks, tab=tab, op=op)


@bp.route("/tasks/new", methods=["GET"])
def new_task_view():
    """新建任务页面(渲染 tasks-new.html,带 OCR 弹框复用)。"""
    op = _current_operator()
    # 模板已就绪 → 直接 render(Fix 3)。
    return render_template("tasks-new.html", op=op)


# ============================================================
# Task 14 — 打码抢单池(M1 占位,仅显示待打码)
# ============================================================

@bp.route("/coding-pool", methods=["GET"])
def coding_pool_view():
    """打码抢单池列表视图。

    M1: 仅显示 coding_status='待打码' 且未作废的任务。
    M2: 接入抢单 / 完成 / 释放真实逻辑(POST 端点已在下方占位)。
    """
    op = _current_operator()
    pool = [
        t for t in TaskDB.get_all()
        if t["coding_status"] == "待打码" and not t["is_cancelled"]
    ]
    return render_template("coding-pool.html", pool=pool, op=op)


@bp.route("/tasks/<int:tid>", methods=["GET"])
def detail_view(tid):
    """详情视图。"""
    op = _current_operator()
    t = TaskDB.get_by_id(tid)
    if not t:
        abort(404)
    items = TaskItemDB.get_by_task(tid)
    events = TaskEventDB.get_by_task(tid)
    images = TaskImageDB.get_by_task(tid)
    # 模板已就绪 → 直接 render(Fix 3)。
    return render_template(
        "task-detail.html",
        task=t, items=items, events=events, images=images, op=op,
    )


@bp.route("/api/v1/tasks/<int:tid>/images", methods=["POST"])
def upload_image(tid):
    """上传任务图片(multipart, field=image, 表单 field=stage)。"""
    op = _current_operator()
    if not op:
        return jsonify(success=False, error="未登录"), 401
    t = TaskDB.get_by_id(tid)
    if not t:
        return jsonify(success=False, error="任务不存在"), 404

    f = request.files.get("image")
    stage = request.form.get("stage")
    if stage not in ALLOWED_STAGES:
        return jsonify(success=False, error=f"非法 stage {stage}"), 400
    if not f or not f.filename:
        return jsonify(success=False, error="无文件"), 400

    # Fix 5:复用 _helpers.check_uploaded_image(扩展名 + 大小白名单)
    from blueprints._helpers import get_upload_dir, check_uploaded_image
    try:
        ext = check_uploaded_image(f)
    except ValueError as e:
        return jsonify(success=False, error=str(e)), 400

    # 复用 _helpers.get_upload_dir()(返回 (dir, month) 元组)
    up_dir, month = get_upload_dir()
    name = f"task_{tid}_{uuid.uuid4().hex[:8]}{ext}"
    f.save(os.path.join(up_dir, name))
    rel = f"{month}/{name}"
    iid = TaskImageDB.create(tid, stage, rel)
    return jsonify(success=True, image=rel, image_id=iid)


@bp.route("/api/v1/tasks/images/<int:iid>", methods=["DELETE"])
def delete_task_image(iid):
    """删除任务图片(详情页证据图可改)。删除文件 + DB 行。

    TaskImageDB 没有 get_by_id,直接 SQL 查 image_path 以便清掉文件。
    """
    op = _current_operator()
    if op is None:
        return jsonify(success=False, error="未登录"), 401

    from models._db import get_db
    row = get_db().execute(
        "SELECT image_path FROM task_images WHERE id=?", (iid,)
    ).fetchone()
    if not row:
        return jsonify(success=False, error="图片不存在"), 404

    # 删文件(可能已不存在 → 容错)
    # image_path 形如 "2026-07/task_xxx.png",文件位于 BASE_DIR/upload/<image_path>
    try:
        from blueprints._helpers import BASE_DIR
        os.remove(os.path.join(BASE_DIR, "upload", row["image_path"]))
    except OSError:
        pass

    TaskImageDB.delete(iid)
    return jsonify(success=True)


@bp.route("/api/v1/tasks/ai-recognize", methods=["POST"])
def recognize():
    """OCR 识别(直接复用出货页 #aiEngine 下拉的引擎)。

    端点本身是 /api/ 公网前缀 (无需登录), OCR 是预览/工具, 不破坏数据,
    所以不卡 can(op, CREATE_TASK) — 真正落库 (POST /api/v1/tasks) 才有 can() 校验。
    """
    f = request.files.get("image")
    if not f:
        return jsonify(success=False, error="无文件"), 400
    engine_name = request.form.get("engine") or os.environ.get("OCR_BACKEND", "moonshot")

    # Fix 6:分层 catch — 无效引擎 → 400,识别失败 → 500 + 记日志(对齐 shipping)
    try:
        from blueprints.ocr_engine import get_ocr_engine
        engine = get_ocr_engine(engine_name)
    except ValueError as e:
        return jsonify(success=False, error=f"无效引擎: {engine_name}", hint=str(e)), 400
    try:
        result = engine.recognize(f.read(), f.filename)
    except Exception:
        logger.exception("OCR 识别失败 (engine=%s)", engine_name)
        return jsonify(success=False, error="OCR 识别失败,请重试"), 500
    return jsonify(result)


@bp.route("/api/v1/tasks/<int:tid>/assign", methods=["POST"])
def assign(tid):
    """指派司机:仅调度/文员;driver_id 必须为司机角色;自动写入 vehicle_id。"""
    op = _current_operator()
    t = TaskDB.get_by_id(tid)
    if not t:
        return jsonify(success=False, error="任务不存在"), 404
    if not can(op, Action.ASSIGN, t):
        return jsonify(success=False, error="无权限"), 403
    data = request.get_json(silent=True) or {}
    driver_id = data.get("driver_id")
    if not driver_id:
        return jsonify(success=False, error="需 driver_id"), 400
    from models.tasks_flow import StaffDB
    driver = StaffDB.get_by_id(driver_id)
    if not driver or driver["role"] != "司机":
        return jsonify(success=False, error="driver_id 必须为司机"), 400
    vehicle_id = driver.get("vehicle_id")
    TaskDB.update(tid, driver_id=driver_id, vehicle_id=vehicle_id)
    TaskEventDB.create(
        tid, "assign",
        operator_id=op["id"],
        from_status=None, to_status=None,
        note=f"指派司机 #{driver_id}",
    )
    return jsonify(success=True, vehicle_id=vehicle_id)


# ============================================================
# 占位(后续任务 T11 补全 — 打码三操作)
# ============================================================

@bp.route("/api/v1/tasks/<int:tid>/coding-claim", methods=["POST"])
def coding_claim(tid):  # noqa: D401 - placeholder
    return jsonify(success=False, error="未实现:coding_claim"), 501


@bp.route("/api/v1/tasks/<int:tid>/coding-done", methods=["POST"])
def coding_done(tid):  # noqa: D401 - placeholder
    return jsonify(success=False, error="未实现:coding_done"), 501


@bp.route("/api/v1/tasks/<int:tid>/coding-release", methods=["POST"])
def coding_release(tid):  # noqa: D401 - placeholder
    return jsonify(success=False, error="未实现:coding_release"), 501


# ============================================================
# Task 12 — 司机退单 + 任务作废 + 整单退回
# ============================================================

@bp.route("/api/v1/tasks/<int:tid>/driver-release", methods=["POST"])
def driver_release(tid):
    """司机退单(从已装货退回准备中,vehicle/driver 解绑,coding_status 保留)。

    权限:仅本单司机在 准备中/已装货 状态。
    闸门:必须已有退单照(stage='退单照')。
    """
    op = _current_operator()
    if op is None:
        return jsonify(success=False, error="未登录"), 401

    t = TaskDB.get_by_id(tid)
    if not t:
        return jsonify(success=False, error="任务不存在"), 404

    if not can(op, Action.DRIVER_RELEASE, t):
        return jsonify(success=False, error="无权限或状态不允许"), 403

    # 必传退单照
    imgs = TaskImageDB.get_by_task(tid)
    if not any(i["stage"] == "退单照" for i in imgs):
        return jsonify(success=False, error="请先上传退单照"), 400

    from_status = t["status"]
    TaskDB.update(tid, driver_id=None, vehicle_id=None, status="准备中")
    TaskEventDB.create(
        tid, "driver_release",
        operator_id=op["id"],
        from_status=from_status, to_status="准备中",
        note="司机退单",
    )
    return jsonify(success=True)


@bp.route("/api/v1/tasks/<int:tid>/cancel", methods=["POST"])
def cancel(tid):
    """任务作废(仅准备中)。权限:调度/文员。设置 is_cancelled=1。"""
    op = _current_operator()
    if op is None:
        return jsonify(success=False, error="未登录"), 401

    t = TaskDB.get_by_id(tid)
    if not t:
        return jsonify(success=False, error="任务不存在"), 404

    if not can(op, Action.TASK_CANCEL, t):
        return jsonify(success=False, error="无权限或状态不允许"), 403

    TaskDB.update(tid, is_cancelled=1)
    TaskEventDB.create(
        tid, "task_cancel",
        operator_id=op["id"],
        from_status=t["status"], to_status=t["status"],
        note="任务作废",
    )
    return jsonify(success=True)


@bp.route("/api/v1/tasks/<int:tid>/return-all", methods=["POST"])
def return_all(tid):
    """整单退回(已卸货后):原单进 已拒收,生成新退货单(数量负)。

    权限:仅本单司机在 已卸货/已到达 状态(can() 中 RETURN_CREATE 仅允许
    司机且是本单 driver_id,与 Action.DRIVER_RELEASE 在范围上略有差异;
    此处保持 brief 语义不变)。
    闸门:必须已有退货照(stage='退货照')。
    """
    op = _current_operator()
    if op is None:
        return jsonify(success=False, error="未登录"), 401

    t = TaskDB.get_by_id(tid)
    if not t:
        return jsonify(success=False, error="任务不存在"), 404

    if not can(op, Action.RETURN_CREATE, t):
        return jsonify(success=False, error="无权限或状态不允许"), 403

    # 必传退货照
    imgs = TaskImageDB.get_by_task(tid)
    if not any(i["stage"] == "退货照" for i in imgs):
        return jsonify(success=False, error="请先上传退货照"), 400

    # 生成退货单:数量全负
    new_tid = TaskDB.create(
        task_no=f"TD-RET-{datetime.now().strftime('%Y%m%d%H%M%S')}",
        creator_id=op["id"],
        source_order_no=None,
        task_type="退货",
        related_task_id=tid,
        source_type=t.get("source_type"),
        customer=t.get("customer"),
        dest_address=t.get("dest_address"),
        driver_id=op["id"],  # 自动给该司机
    )
    for i, it in enumerate(TaskItemDB.get_by_task(tid)):
        TaskItemDB.create(
            new_tid, it["product_name"], -it["quantity"], it["unit"],
            it["specification"], it["remark"], i, "return",
        )

    TaskDB.update(tid, status="已拒收")
    TaskEventDB.create(
        tid, "return_create",
        operator_id=op["id"],
        from_status=t["status"], to_status="已拒收",
        note="整单退回",
    )
    TaskEventDB.create(
        new_tid, "assign",
        operator_id=op["id"],
        from_status=None, to_status="准备中",
        note="系统代建退货单",
    )
    return jsonify(success=True, new_task_id=new_tid)