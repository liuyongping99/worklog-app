"""基础记录蓝图。

包含：
- 工作经验 (/experience)
- 错误经验 (/errorlog)
- 待办事项 (/todolist)
- 车辆维护记录 (/vehicle-maintenance)
"""
from flask import Blueprint, render_template, request, redirect, url_for, flash
from models import WorkLog, ErrorLog, TodoItem, VehicleMaintenance

bp = Blueprint('basic_records', __name__)


# ── 工作经验 ──────────────────────────────────────────────
@bp.route('/experience')
def experience():
    logs = WorkLog.get_all()
    return render_template('experience.html', logs=logs, page_title='工作经验')


@bp.route('/experience/add', methods=['POST'])
def experience_add():
    title = request.form.get('title', '').strip()
    content = request.form.get('content', '').strip()
    if title and content:
        WorkLog.create(title, content)
        flash('工作经验已保存!', 'success')
    else:
        flash('标题和内容不能为空', 'error')
    return redirect(url_for('basic_records.experience'))


@bp.route('/experience/delete/<int:log_id>', methods=['POST'])
def experience_delete(log_id):
    WorkLog.delete(log_id)
    flash('已删除', 'info')
    return redirect(url_for('basic_records.experience'))


# ── 错误经验 ──────────────────────────────────────────────
@bp.route('/errorlog')
def errorlog():
    logs = ErrorLog.get_all()
    return render_template('errorlog.html', logs=logs, page_title='错误经验')


@bp.route('/errorlog/add', methods=['POST'])
def errorlog_add():
    title = request.form.get('title', '').strip()
    error_type = request.form.get('error_type', '').strip()
    solution = request.form.get('solution', '').strip()
    if title:
        ErrorLog.create(title, error_type, solution)
        flash('错误记录已保存!', 'success')
    else:
        flash('标题不能为空', 'error')
    return redirect(url_for('basic_records.errorlog'))


@bp.route('/errorlog/delete/<int:log_id>', methods=['POST'])
def errorlog_delete(log_id):
    ErrorLog.delete(log_id)
    flash('已删除', 'info')
    return redirect(url_for('basic_records.errorlog'))


# ── 待办事项 ──────────────────────────────────────────────
@bp.route('/todolist')
def todolist():
    items = TodoItem.get_all()
    return render_template('todolist.html', items=items, page_title='待办事项')


@bp.route('/todolist/add', methods=['POST'])
def todolist_add():
    content = request.form.get('content', '').strip()
    if content:
        TodoItem.create(content)
        flash('待办已添加!', 'success')
    else:
        flash('内容不能为空', 'error')
    return redirect(url_for('basic_records.todolist'))


@bp.route('/todolist/toggle/<int:item_id>', methods=['POST'])
def todolist_toggle(item_id):
    TodoItem.toggle(item_id)
    return redirect(url_for('basic_records.todolist'))


@bp.route('/todolist/delete/<int:item_id>', methods=['POST'])
def todolist_delete(item_id):
    TodoItem.delete(item_id)
    flash('已删除', 'info')
    return redirect(url_for('basic_records.todolist'))


# ── 车辆维护记录 ──────────────────────────────────────────────
@bp.route('/vehicle-maintenance')
def vehicle_maintenance():
    records = VehicleMaintenance.get_all()
    return render_template('vehicle-maintenance.html', records=records, page_title='车辆维护记录')


@bp.route('/vehicle-maintenance/add', methods=['POST'])
def vehicle_maintenance_add():
    date = request.form.get('date', '').strip()
    vehicle_plate = request.form.get('vehicle_plate', '').strip()
    type_ = request.form.get('type', '').strip()
    description = request.form.get('description', '').strip()
    cost = request.form.get('cost', '').strip()
    remark = request.form.get('remark', '').strip()
    if date and vehicle_plate and type_ and description:
        VehicleMaintenance.create(date, vehicle_plate, type_, description, cost, remark)
        flash('车辆维护记录已保存!', 'success')
    else:
        flash('日期、车牌、类型和描述不能为空', 'error')
    return redirect(url_for('basic_records.vehicle_maintenance'))


@bp.route('/vehicle-maintenance/delete/<int:record_id>', methods=['POST'])
def vehicle_maintenance_delete(record_id):
    VehicleMaintenance.delete(record_id)
    flash('已删除', 'info')
    return redirect(url_for('basic_records.vehicle_maintenance'))
