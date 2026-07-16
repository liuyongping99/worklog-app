from enum import Enum


class Action(str, Enum):
    CREATE_TASK = "create_task"
    EDIT_TASK = "edit_task"
    ASSIGN = "assign"
    CODING_CLAIM = "coding_claim"
    CODING_DONE = "coding_done"
    CODING_RELEASE = "coding_release"
    LOAD = "load"          # 装货(含出发)
    ARRIVE = "arrive"
    UNLOAD = "unload"
    COUNT = "count"
    DRIVER_RELEASE = "driver_release"
    TASK_CANCEL = "task_cancel"
    RETURN_CREATE = "return_create"


def _get_role(operator):
    """兼容 dataclass Staff 实例和 dict 两种 operator 形式。"""
    if operator is None:
        return None
    if isinstance(operator, dict):
        return operator.get("role")
    return getattr(operator, "role", None)


def _get_id(operator):
    """兼容 dataclass Staff 实例和 dict 两种 operator 形式。"""
    if operator is None:
        return None
    if isinstance(operator, dict):
        return operator.get("id")
    return getattr(operator, "id", None)


def _get_driver_id(task):
    if task is None:
        return None
    if isinstance(task, dict):
        return task.get("driver_id")
    return getattr(task, "driver_id", None)


def _get_status(task):
    if task is None:
        return None
    if isinstance(task, dict):
        return task.get("status")
    return getattr(task, "status", None)


def can(operator, action: Action, task=None) -> bool:
    """集中权限判断。operator 是 Staff 实例(或 dict 含 role/id), task 是 Task 实例。"""
    role = _get_role(operator)
    op_id = _get_id(operator)
    if role is None:
        return False

    # 任意登录者(司机/调度/文员/搬运/打码/仓管) 可建单/编辑
    if action in (Action.CREATE_TASK, Action.EDIT_TASK):
        return role in ("司机", "调度", "文员", "搬运", "打码", "仓管")

    # 打码三操作
    if action in (Action.CODING_CLAIM, Action.CODING_DONE, Action.CODING_RELEASE):
        return role == "打码"

    # 指派司机
    if action == Action.ASSIGN:
        return role in ("调度", "文员")

    # 点数
    if action == Action.COUNT:
        return role in ("文员", "仓管")

    # 装/到/卸
    if action in (Action.LOAD, Action.ARRIVE, Action.UNLOAD, Action.RETURN_CREATE):
        if role != "司机":
            return False
        if task is None:
            return False
        return _get_driver_id(task) == op_id

    # 司机退单
    if action == Action.DRIVER_RELEASE:
        if role != "司机" or task is None:
            return False
        return _get_driver_id(task) == op_id and _get_status(task) in ("准备中", "已装货")

    # 任务作废
    if action == Action.TASK_CANCEL:
        if role not in ("调度", "文员") or task is None:
            return False
        return _get_status(task) == "准备中"

    return False


__all__ = ["Action", "can"]