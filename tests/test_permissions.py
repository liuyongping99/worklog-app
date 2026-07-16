from models._permissions import can, Action
from models.tasks_flow import Staff, Task


def make_staff(role, id=1):
    return Staff(id=id, name="x", gender=None, birth_date=None, role=role,
                 staff_code=None, phone=None, is_active=1, vehicle_id=None,
                 login_name=None, password_hash=None, created_at=None, updated_at=None)


def make_task(driver_id=10, status="准备中"):
    return Task(id=1, task_no="TD-1", source_order_no=None, task_type="送货",
                related_task_id=None, source_type=None, customer=None,
                dest_address=None, dest_lat=None, dest_lng=None, dest_poi_name=None,
                status=status, coding_status="无需打码",
                driver_id=driver_id, vehicle_id=None, creator_id=1, operator_id=None,
                coder_id=None, coding_claimed_at=None, coding_done_at=None,
                est_weight_kg=None, est_distance_km=None, depart_at=None, arrive_at=None,
                remark=None, is_cancelled=0, created_at=None, updated_at=None)


def test_driver_can_release_own_before_depart():
    op = make_staff("司机", id=10)
    t = make_task(driver_id=10, status="已装货")
    assert can(op, Action.DRIVER_RELEASE, t) is True


def test_driver_cannot_release_other():
    op = make_staff("司机", id=11)
    t = make_task(driver_id=10, status="已装货")
    assert can(op, Action.DRIVER_RELEASE, t) is False


def test_coder_can_claim():
    op = make_staff("打码", id=20)
    t = make_task(status="准备中")
    assert can(op, Action.CODING_CLAIM, t) is True


def test_non_coder_cannot_claim():
    op = make_staff("司机", id=11)
    t = make_task(status="准备中")
    assert can(op, Action.CODING_CLAIM, t) is False


def test_dispatcher_can_assign():
    op = make_staff("调度", id=30)
    t = make_task(status="准备中", driver_id=None)
    assert can(op, Action.ASSIGN, t) is True


def test_clerk_can_count():
    op = make_staff("文员", id=40)
    t = make_task(status="已装货")
    assert can(op, Action.COUNT, t) is True