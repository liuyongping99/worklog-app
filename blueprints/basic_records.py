"""基础记录蓝图。

包含：
- 工作经验 (/experience)
- 错误经验 (/errorlog)
- 待办事项 (/todolist)
- 车辆维护记录 (/vehicle-maintenance) + 记录图片上传/删除
- 消防检查记录 (/fire-checks)
"""
import os
import uuid

from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify, session
from models import (WorkLog, ErrorLog, TodoItem, VehicleMaintenance, VehicleMaintenanceImage,
                    FireCheck, FireCheckImage)
from blueprints._helpers import (
    check_uploaded_image, validate_image_content, get_upload_dir, backup_to_ebak,
)

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
    # 记录 id → 图片列表（与 notice 页同样的 dict 模式，模板里 vm_images.get(item.id) 取）
    vm_images = {r['id']: VehicleMaintenanceImage.get_by_record(r['id']) for r in records}
    return render_template(
        'vehicle-maintenance.html',
        records=records,
        vm_images=vm_images,
        page_title='车辆维护记录',
    )


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
    # 级联：记录下的图片（DB 行 + 物理文件）一并清掉，见 VehicleMaintenance.delete
    VehicleMaintenance.delete(record_id)
    flash('已删除', 'info')
    return redirect(url_for('basic_records.vehicle_maintenance'))


@bp.route('/vehicle-maintenance/edit/<int:record_id>', methods=['POST'])
def vehicle_maintenance_edit(record_id):
    """修改维护记录（弹框提交 → 表单 POST → flash + redirect，与本页其它表单同风格）。"""
    date = request.form.get('date', '').strip()
    vehicle_plate = request.form.get('vehicle_plate', '').strip()
    type_ = request.form.get('type', '').strip()
    description = request.form.get('description', '').strip()
    cost = request.form.get('cost', '').strip()
    remark = request.form.get('remark', '').strip()
    if not (date and vehicle_plate and type_ and description):
        flash('日期、车牌、类型和描述不能为空', 'error')
    elif not VehicleMaintenance.get_by_id(record_id):
        flash('维护记录不存在', 'error')
    else:
        VehicleMaintenance.update(record_id, date, vehicle_plate, type_, description, cost, remark)
        flash('维护记录已修改!', 'success')
    return redirect(url_for('basic_records.vehicle_maintenance'))


# ── 车辆维护记录图片（上传/删除） ──────────────────────────────
@bp.route('/api/v1/vehicle-maintenance/<int:record_id>/images', methods=['POST'])
def api_vehicle_maintenance_upload_images(record_id):
    """上传维护记录图片。

    multipart，字段名 image，可一次多张（项目约定，见 CLAUDE.md「排列坑」）。
    校验链与订单行级图一致：扩展名白名单 + 10MB 上限 + magic bytes + Pillow 真实解码。
    任一张不合法 → 整体 400，并把本次已落盘的半成品清掉。
    """
    if not session.get('operator_id'):
        return jsonify({'success': False, 'error': '未登录'}), 401

    if not VehicleMaintenance.get_by_id(record_id):
        return jsonify({'success': False, 'error': '维护记录不存在'}), 404

    files = [f for f in (request.files.getlist('image') or request.files.getlist('file'))
             if f and f.filename]
    if not files:
        return jsonify({'success': False, 'error': '未选择图片'}), 400

    upload_dir, month_str = get_upload_dir()
    saved_paths = []      # 已落盘的文件（失败时回滚）
    created_ids = []      # 已建 DB 行的（失败时回滚）
    images = []
    try:
        for f in files:
            ext = check_uploaded_image(f)          # 契约:返回值【自带前导点】,如 '.png'
            if not ext.startswith('.'):
                ext = '.' + ext
            file_name = f"{uuid.uuid4().hex}{ext}"
            filepath = os.path.join(upload_dir, file_name)
            f.save(filepath)
            saved_paths.append(filepath)
            validate_image_content(filepath)        # 落盘后再验: magic + Pillow 真实解码
            backup_to_ebak(filepath)                # 同步一份到 E:\BAK\upload

            image_id = VehicleMaintenanceImage.create(record_id, file_name, month_str, f.filename or '')
            created_ids.append(image_id)
            images.append({
                'id': image_id,
                'url': '/upload/%s/%s' % (month_str, file_name),
                'original_name': f.filename or '',
            })
    except ValueError as e:
        # 回滚：已建的 DB 行 + 已落盘的半成品文件
        for image_id in created_ids:
            if image_id:
                VehicleMaintenanceImage.delete(image_id)
        for path in saved_paths:
            VehicleMaintenanceImage.remove_file(path)
        return jsonify({'success': False, 'error': str(e)}), 400

    return jsonify({'success': True, 'images': images, 'count': len(images)}), 201


@bp.route('/api/v1/vehicle-maintenance/images/<int:image_id>', methods=['DELETE'])
def api_vehicle_maintenance_delete_image(image_id):
    """删除一张维护记录图片（DB 行 + 物理文件，文件进回收站可恢复）。"""
    if not session.get('operator_id'):
        return jsonify({'success': False, 'error': '未登录'}), 401

    if VehicleMaintenanceImage.delete(image_id):
        return jsonify({'success': True, 'id': image_id})
    return jsonify({'success': False, 'error': '图片不存在'}), 404


# ── 消防检查记录 ──────────────────────────────────────────────
@bp.route('/fire-checks')
def fire_checks():
    """消防检查台账页。列表按日期倒序,顶部给「待整改 / 已闭环」汇总。"""
    records = FireCheck.get_all()
    stats = FireCheck.stats(records)
    # 记录 id → 图片列表（模板里 fc_images.get(item.id) 取）
    fc_images = {r['id']: FireCheckImage.get_by_record(r['id']) for r in records}
    return render_template(
        'fire-checks.html',
        records=records,
        stats=stats,
        fc_images=fc_images,
        status_label=FireCheck.STATUS_LABEL,
        page_title='消防检查记录',
    )


@bp.route('/fire-checks/add', methods=['POST'])
def fire_checks_add():
    date = request.form.get('date', '').strip()
    location = request.form.get('location', '').strip()
    check_items = request.form.get('check_items', '').strip()
    outstanding_items = request.form.get('outstanding_items', '').strip()
    remark = request.form.get('remark', '').strip()
    if date and check_items:
        FireCheck.create(
            date, check_items, location, outstanding_items, remark,
            user_id=session.get('operator_id'),
        )
        flash('消防检查记录已保存!', 'success')
    else:
        flash('日期和检查事项不能为空', 'error')
    return redirect(url_for('basic_records.fire_checks'))


@bp.route('/fire-checks/edit/<int:record_id>', methods=['POST'])
def fire_checks_edit(record_id):
    """修改消防检查记录（弹框提交 → 表单 POST → flash + redirect，与本页其它表单同风格）。"""
    date = request.form.get('date', '').strip()
    location = request.form.get('location', '').strip()
    check_items = request.form.get('check_items', '').strip()
    outstanding_items = request.form.get('outstanding_items', '').strip()
    remark = request.form.get('remark', '').strip()
    # status 传空 → 后端按遗留事项重新推导；有值 → 以用户手选为准
    status = request.form.get('status', '').strip()
    if not (date and check_items):
        flash('日期和检查事项不能为空', 'error')
    elif not FireCheck.get_by_id(record_id):
        flash('消防检查记录不存在', 'error')
    else:
        FireCheck.update(
            record_id, date, check_items, location, outstanding_items, remark, status,
        )
        flash('消防检查记录已修改!', 'success')
    return redirect(url_for('basic_records.fire_checks'))


@bp.route('/fire-checks/delete/<int:record_id>', methods=['POST'])
def fire_checks_delete(record_id):
    # 级联：记录下的图片（DB 行 + 物理文件）一并清掉，见 FireCheck.delete
    FireCheck.delete(record_id)
    flash('已删除', 'info')
    return redirect(url_for('basic_records.fire_checks'))


# ── 消防检查记录图片（上传/删除） ──────────────────────────────
@bp.route('/api/v1/fire-checks/<int:record_id>/images', methods=['POST'])
def api_fire_check_upload_images(record_id):
    """上传消防检查记录图片。

    multipart，字段名 image，可一次多张（项目约定，见 CLAUDE.md「排列坑」）。
    校验链与车辆维护记录图片一致：扩展名白名单 + 10MB 上限 + magic bytes + Pillow 真实解码。
    任一张不合法 → 整体 400，并把本次已落盘的半成品清掉。
    """
    if not session.get('operator_id'):
        return jsonify({'success': False, 'error': '未登录'}), 401

    if not FireCheck.get_by_id(record_id):
        return jsonify({'success': False, 'error': '消防检查记录不存在'}), 404

    files = [f for f in (request.files.getlist('image') or request.files.getlist('file'))
             if f and f.filename]
    if not files:
        return jsonify({'success': False, 'error': '未选择图片'}), 400

    upload_dir, month_str = get_upload_dir()
    saved_paths = []      # 已落盘的文件（失败时回滚）
    created_ids = []      # 已建 DB 行的（失败时回滚）
    images = []
    try:
        for f in files:
            ext = check_uploaded_image(f)          # 契约:返回值【自带前导点】,如 '.png'
            if not ext.startswith('.'):
                ext = '.' + ext
            file_name = f"{uuid.uuid4().hex}{ext}"
            filepath = os.path.join(upload_dir, file_name)
            f.save(filepath)
            saved_paths.append(filepath)
            validate_image_content(filepath)        # 落盘后再验: magic + Pillow 真实解码
            backup_to_ebak(filepath)                # 同步一份到 E:\BAK\upload

            image_id = FireCheckImage.create(record_id, file_name, month_str, f.filename or '')
            created_ids.append(image_id)
            images.append({
                'id': image_id,
                'url': '/upload/%s/%s' % (month_str, file_name),
                'original_name': f.filename or '',
            })
    except ValueError as e:
        # 回滚：已建的 DB 行 + 已落盘的半成品文件
        for image_id in created_ids:
            if image_id:
                FireCheckImage.delete(image_id)
        for path in saved_paths:
            FireCheckImage.remove_file(path)
        return jsonify({'success': False, 'error': str(e)}), 400

    return jsonify({'success': True, 'images': images, 'count': len(images)}), 201


@bp.route('/api/v1/fire-checks/images/<int:image_id>', methods=['DELETE'])
def api_fire_check_delete_image(image_id):
    """删除一张消防检查记录图片（DB 行 + 物理文件，文件进回收站可恢复）。"""
    if not session.get('operator_id'):
        return jsonify({'success': False, 'error': '未登录'}), 401

    if FireCheckImage.delete(image_id):
        return jsonify({'success': True, 'id': image_id})
    return jsonify({'success': False, 'error': '图片不存在'}), 404
