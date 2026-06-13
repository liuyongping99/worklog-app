"""入库记录蓝图。

包含：
- /inbound-records 页面（带日期过滤、商品单位提示计算）
- /inbound-records/* 传统 form 端点（add/add-item/edit/lock/batch-add/move/...）
- /inbound-records/upload-image/<id> / delete-image/<id> HTML 风格图片端点
- /api/v1/inbound-orders/* REST API（订单/明细/图片 CRUD + 移动）
"""
import os
import uuid
import base64
from datetime import date as date_cls, timedelta, datetime
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from models import (
    InboundOrder, InboundRecord, InboundImage, ProductUnit, get_db, AuditLog
)
from blueprints._helpers import get_upload_dir as get_helpers_upload_dir, get_ypp, calc_hint, check_remark, summarize_remarks

bp = Blueprint('inbound', __name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _get_upload_dir():
    """薄包装，返回 (upload_dir, month_str)。"""
    return get_helpers_upload_dir()


# ── 页面 ──────────────────────────────────────────────
@bp.route('/inbound-records')
def inbound_records():
    today = date_cls.today().isoformat()
    start_date = request.args.get('start_date', '').strip()
    end_date = request.args.get('end_date', '').strip()
    if not start_date or not end_date:
        end = date_cls.today()
        start = end - timedelta(days=2)
        start_date = start.isoformat()
        end_date = end.isoformat()
    groups = InboundRecord.get_groups(start_date, end_date)

    units = ProductUnit.get_all()
    unit_list = [{
        'product_name': u['product_name'],
        'spec_keyword': u['spec_keyword'] or '',
        'yards_per_piece': u['yards_per_piece'] / 100.0,
        'is_usingyardforcounting': bool(u['is_usingyardforcounting'])
    } for u in units]

    def _get_ypp(product_name, spec):
        return get_ypp(product_name, spec, units_cache=units)

    def _calc_hint(quantity_str, ypp):
        return calc_hint(quantity_str, ypp)

    def _check_remark(remark, quantity_str, ypp):
        return check_remark(remark, quantity_str, ypp)

    for group in groups:
        for item in group['records']:
            ypp = _get_ypp(item['product_name'], item.get('specification', ''))
            item['unit_hint'] = _calc_hint(item['quantity'], ypp)
            item['mismatch'] = _check_remark(
                item.get('remark', ''),
                item['quantity'],
                ypp
            )
        group.update(summarize_remarks(group['records']))

    return render_template(
        'inbound-records.html',
        groups=groups,
        page_title='入库记录',
        today=today,
        start_date=start_date,
        end_date=end_date,
        unit_list=unit_list,
    )


# ── HTML form 端点 ──────────────────────────────────────────────
@bp.route('/inbound-records/add', methods=['POST'])
def inbound_records_add():
    date = request.form.get('date')
    supplier = request.form.get('supplier')
    if date and supplier:
        InboundOrder.create(date, supplier)
        flash('已创建入库单')
    return redirect(url_for('inbound.inbound_records'))


@bp.route('/inbound-records/add-item', methods=['POST'])
def inbound_records_add_item():
    order_pk = request.form.get('order_pk')
    product_name = request.form.get('product_name')
    specification = request.form.get('specification')
    quantity = request.form.get('quantity')
    unit = request.form.get('unit', '支')
    remark = request.form.get('remark', '')
    if order_pk and product_name and quantity:
        InboundRecord.create(int(order_pk), product_name, specification, quantity, unit, remark)
        flash('已添加明细')
    return redirect(url_for('inbound.inbound_records'))


@bp.route('/inbound-records/delete/<int:record_id>', methods=['POST'])
def inbound_records_delete(record_id):
    record = InboundRecord.get_by_id(record_id)
    if record:
        InboundRecord.delete(record_id)
        flash('已删除明细')
    return redirect(url_for('inbound.inbound_records'))


@bp.route('/inbound-records/edit/<int:record_id>', methods=['POST'])
def inbound_records_edit(record_id):
    record = InboundRecord.get_by_id(record_id)
    if not record:
        return jsonify({'success': False, 'error': '记录不存在'})
    order = InboundOrder.get_by_id(record['order_pk'])
    if order and order.get('is_locked'):
        return jsonify({'success': False, 'error': '该订单已锁定，无法修改'})
    data = request.get_json()
    InboundRecord.update(record_id, data)
    updated = InboundRecord.get_by_id(record_id)
    return jsonify({'success': True, 'record': updated})


@bp.route('/inbound-records/delete-order/<int:order_id>', methods=['POST'])
def inbound_records_delete_order(order_id):
    order = InboundOrder.get_by_id(order_id)
    if not order:
        flash('订单不存在', 'error')
        return redirect(url_for('inbound.inbound_records'))
    if order.get('is_locked'):
        flash('该订单已锁定，无法删除', 'error')
        return redirect(url_for('inbound.inbound_records'))
    supplier = order.get('supplier')
    result = InboundOrder.delete(order_id)
    if not result.get('success'):
        flash(result.get('error', '删除失败'), 'error')
        return redirect(url_for('inbound.inbound_records'))
    AuditLog.log('delete_order', 'inbound_order', order_id, detail={'supplier': supplier})
    flash('已删除入库单')
    return redirect(url_for('inbound.inbound_records'))


@bp.route('/inbound-records/lock/<int:order_id>', methods=['POST'])
def inbound_records_lock(order_id):
    """切换入库订单锁定状态"""
    order = InboundOrder.get_by_id(order_id)
    if not order:
        return jsonify({'success': False, 'error': '订单不存在'})
    if order.get('is_locked', 0):
        InboundOrder.unlock(order_id)
        locked = False
    else:
        InboundOrder.lock(order_id)
        locked = True
    return jsonify({'success': True, 'locked': locked})


@bp.route('/inbound-records/batch-add', methods=['POST'])
def inbound_records_batch_add():
    order_pk = request.form.get('order_pk')
    items_text = request.form.get('items_text', '')

    if order_pk and items_text:
        for line in items_text.strip().split('\n'):
            line = line.strip()
            if not line:
                continue
            if '|' in line:
                parts = line.split('|')
            else:
                parts = line.split()
            product_name = parts[0].strip() if len(parts) > 0 else ''
            specification = parts[1].strip() if len(parts) > 1 else ''
            unit = parts[2].strip() if len(parts) > 2 else '支'
            quantity = parts[3].strip() if len(parts) > 3 else ''
            remark = parts[4].strip() if len(parts) > 4 else ''
            if product_name and quantity:
                InboundRecord.create(int(order_pk), product_name, specification, quantity, unit, remark)
        flash('已批量添加明细')
    return redirect(url_for('inbound.inbound_records'))


@bp.route('/inbound-records/move-up/<int:record_id>', methods=['POST'])
def inbound_records_move_up(record_id):
    record = InboundRecord.get_by_id(record_id)
    if not record:
        return jsonify({'success': False, 'error': '记录不存在'})
    order = InboundOrder.get_by_id(record['order_pk'])
    if order and order.get('is_locked'):
        return jsonify({'success': False, 'error': '订单已锁定'})
    InboundRecord.move_up(record_id)
    return jsonify({'success': True})


@bp.route('/inbound-records/move-down/<int:record_id>', methods=['POST'])
def inbound_records_move_down(record_id):
    record = InboundRecord.get_by_id(record_id)
    if not record:
        return jsonify({'success': False, 'error': '记录不存在'})
    order = InboundOrder.get_by_id(record['order_pk'])
    if order and order.get('is_locked'):
        return jsonify({'success': False, 'error': '订单已锁定'})
    InboundRecord.move_down(record_id)
    return jsonify({'success': True})


# ── HTML 风格图片上传 ──────────────────────────────────────────────
@bp.route('/inbound-records/upload-image/<int:order_pk>', methods=['POST'])
def inbound_upload_image(order_pk):
    """上传图片到指定入库单"""
    upload_dir, month_str = _get_upload_dir()

    if request.is_json:
        data = request.get_json()
        image_data = data.get('image')
        if image_data and image_data.startswith('data:image'):
            header, base64_data = image_data.split(',', 1)
            if 'png' in header:
                ext = 'png'
            elif 'jpeg' in header or 'jpg' in header:
                ext = 'jpg'
            elif 'gif' in header:
                ext = 'gif'
            else:
                ext = 'png'
            filename = f"{uuid.uuid4().hex}.{ext}"
            filepath = os.path.join(upload_dir, filename)
            image_bytes = base64.b64decode(base64_data)
            with open(filepath, 'wb') as f:
                f.write(image_bytes)
            image_id = InboundImage.create(order_pk, filepath, '')
            return jsonify({'success': True, 'image_id': image_id, 'file_path': filepath})

    if 'image' in request.files:
        file = request.files['image']
        if file.filename:
            ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else 'png'
            if ext not in ['png', 'jpg', 'jpeg', 'gif', 'webp']:
                ext = 'png'
            filename = f"{uuid.uuid4().hex}.{ext}"
            filepath = os.path.join(upload_dir, filename)
            file.save(filepath)
            original_name = file.filename
            image_id = InboundImage.create(order_pk, filepath, original_name)
            return jsonify({'success': True, 'image_id': image_id, 'file_path': filepath, 'original_name': original_name})

    return jsonify({'success': False, 'error': 'No image provided'}), 400


@bp.route('/inbound-records/delete-image/<int:image_id>', methods=['POST'])
def inbound_delete_image(image_id):
    if InboundImage.delete(image_id):
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': 'Image not found'}), 404


# ── REST API 订单 CRUD ──────────────────────────────────────────────
@bp.route('/api/v1/inbound-orders', methods=['POST'])
def api_v1_inbound_orders_create():
    """创建入库订单 → 201"""
    data = request.get_json() if request.is_json else request.form
    date_val = data.get('date')
    supplier = data.get('supplier', '')
    if not date_val or not supplier:
        return jsonify({'success': False, 'error': '日期和供应商不能为空'}), 400
    try:
        order_id = InboundOrder.create(date_val, supplier)
        AuditLog.log('create_order', 'inbound_order', order_id, detail={'date': date_val, 'supplier': supplier})
        return jsonify({'success': True, 'id': order_id}), 201
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/v1/inbound-orders/<int:order_id>', methods=['DELETE'])
def api_v1_inbound_orders_delete(order_id):
    """删除入库订单 → 200（有明细或图片时拒绝）"""
    order = InboundOrder.get_by_id(order_id)
    if not order:
        return jsonify({'success': False, 'error': '订单不存在'}), 404
    result = InboundOrder.delete(order_id)
    if not result.get('success'):
        return jsonify(result), 400
    AuditLog.log('delete_order', 'inbound_order', order_id, detail={'supplier': order.get('supplier')})
    return jsonify({'success': True})


@bp.route('/api/v1/inbound-orders/<int:order_id>', methods=['PATCH'])
def api_v1_inbound_orders_update(order_id):
    """更新订单（锁定/解锁/备注）→ 200"""
    order = InboundOrder.get_by_id(order_id)
    if not order:
        return jsonify({'success': False, 'error': '订单不存在'}), 404
    data = request.get_json()
    if data is None:
        return jsonify({'success': False, 'error': '请求体不能为空'}), 400
    if 'is_locked' in data:
        if data['is_locked']:
            InboundOrder.lock(order_id)
            AuditLog.log('lock_order', 'inbound_order', order_id)
        else:
            InboundOrder.unlock(order_id)
            AuditLog.log('unlock_order', 'inbound_order', order_id)
        return jsonify({'success': True, 'is_locked': bool(data['is_locked'])})
    # 同时改备注 + 供应商（弹框一次保存两个字段）—— 必须先判断
    if 'order_note' in data and 'supplier' in data:
        if order.get('is_locked'):
            return jsonify({'success': False, 'error': '订单已锁定，无法修改'}), 403
        new_supplier = str(data['supplier']).strip()
        note = str(data['order_note']).strip()
        if not new_supplier:
            return jsonify({'success': False, 'error': '供应商不能为空'}), 400
        result = InboundOrder.set_supplier(order_id, new_supplier)
        if not result.get('success'):
            return jsonify(result), 400
        InboundOrder.set_note(order_id, note)
        return jsonify({'success': True, 'supplier': new_supplier, 'order_note': note})

    if 'order_note' in data:
        if order.get('is_locked'):
            return jsonify({'success': False, 'error': '订单已锁定，无法修改备注'}), 403
        note = str(data['order_note']).strip()
        InboundOrder.set_note(order_id, note)
        return jsonify({'success': True, 'order_note': note})

    if 'supplier' in data:
        if order.get('is_locked'):
            return jsonify({'success': False, 'error': '订单已锁定，无法修改供应商'}), 403
        new_supplier = str(data['supplier']).strip()
        if not new_supplier:
            return jsonify({'success': False, 'error': '供应商不能为空'}), 400
        result = InboundOrder.set_supplier(order_id, new_supplier)
        if result.get('success'):
            return jsonify(result)
        return jsonify(result), 400

    return jsonify({'success': False, 'error': '无可更新的字段'}), 400


# ── REST API 明细 CRUD ──────────────────────────────────────────────
@bp.route('/api/v1/inbound-orders/<int:order_id>/records', methods=['POST'])
def api_v1_inbound_orders_add_record(order_id):
    """向订单添加明细 → 201"""
    order = InboundOrder.get_by_id(order_id)
    if not order:
        return jsonify({'success': False, 'error': '订单不存在'}), 404
    if order.get('is_locked'):
        return jsonify({'success': False, 'error': '订单已锁定，无法添加'}), 403

    data = request.get_json() if request.is_json else request.form
    product_name = data.get('product_name')
    quantity = data.get('quantity')
    if not product_name or not quantity:
        return jsonify({'success': False, 'error': '品名和数量不能为空'}), 400

    record_id = InboundRecord.create(
        int(order_id),
        product_name=product_name,
        specification=data.get('specification', ''),
        quantity=quantity,
        unit=data.get('unit', '支'),
        remark=data.get('remark', '')
    )
    record = InboundRecord.get_by_id(record_id)
    return jsonify({'success': True, 'id': record_id, 'record': record}), 201


@bp.route('/api/v1/inbound-orders/<int:order_id>/records/batch', methods=['POST'])
def api_v1_inbound_orders_add_records_batch(order_id):
    """批量添加明细 → 201"""
    order = InboundOrder.get_by_id(order_id)
    if not order:
        return jsonify({'success': False, 'error': '订单不存在'}), 404
    if order.get('is_locked'):
        return jsonify({'success': False, 'error': '订单已锁定，无法添加'}), 403

    data = request.get_json()
    records = data.get('records', [])
    if not records:
        return jsonify({'success': False, 'error': '无数据'}), 400

    conn = get_db()
    cursor = conn.cursor()
    try:
        cursor.execute('SELECT COALESCE(MAX(sort_order), 0) FROM inbound_records WHERE order_pk = ?', (order_id,))
        sort_order = cursor.fetchone()[0]
        result_records = []
        for rec in records:
            sort_order += 1
            cursor.execute(
                'INSERT INTO inbound_records (order_pk, product_name, specification, quantity, unit, remark, sort_order, created_at) VALUES (?,?,?,?,?,?,?,?)',
                (order_id, rec.get('product_name', ''), rec.get('specification', ''),
                 str(rec.get('quantity', '')), rec.get('unit', '支'), rec.get('remark', ''),
                 sort_order, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
            result_records.append({
                'id': cursor.lastrowid,
                'order_pk': order_id,
                'product_name': rec.get('product_name', ''),
                'specification': rec.get('specification', ''),
                'quantity': str(rec.get('quantity', '')),
                'unit': rec.get('unit', '支'),
                'remark': rec.get('remark', ''),
                'sort_order': sort_order
            })
        conn.commit()
        return jsonify({'success': True, 'count': len(result_records), 'records': result_records}), 201
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        conn.close()


@bp.route('/api/v1/inbound-orders/records/<int:record_id>', methods=['PUT'])
def api_v1_inbound_orders_update_record(record_id):
    """更新明细 → 200"""
    record = InboundRecord.get_by_id(record_id)
    if not record:
        return jsonify({'success': False, 'error': '记录不存在'}), 404
    order = InboundOrder.get_by_id(record['order_pk'])
    if order and order.get('is_locked'):
        return jsonify({'success': False, 'error': '订单已锁定，无法修改'}), 403

    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': '请求体不能为空'}), 400
    InboundRecord.update(record_id, data)
    updated = InboundRecord.get_by_id(record_id)
    return jsonify({'success': True, 'record': updated})


@bp.route('/api/v1/inbound-orders/records/<int:record_id>', methods=['DELETE'])
def api_v1_inbound_orders_delete_record(record_id):
    """删除明细 → 200"""
    record = InboundRecord.get_by_id(record_id)
    if not record:
        return jsonify({'success': False, 'error': '记录不存在'}), 404
    order = InboundOrder.get_by_id(record['order_pk'])
    if order and order.get('is_locked'):
        return jsonify({'success': False, 'error': '订单已锁定，无法删除'}), 403
    InboundRecord.delete(record_id)
    return jsonify({'success': True})


@bp.route('/api/v1/inbound-orders/records/<int:record_id>/move', methods=['PATCH'])
def api_v1_inbound_orders_move_record(record_id):
    """移动明细排序 → 200"""
    record = InboundRecord.get_by_id(record_id)
    if not record:
        return jsonify({'success': False, 'error': '记录不存在'}), 404
    order = InboundOrder.get_by_id(record['order_pk'])
    if order and order.get('is_locked'):
        return jsonify({'success': False, 'error': '订单已锁定，无法操作'}), 403

    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': '请求体不能为空'}), 400
    direction = data.get('direction', '')

    if direction == 'up':
        ok = InboundRecord.move_up(record_id)
    elif direction == 'down':
        ok = InboundRecord.move_down(record_id)
    else:
        return jsonify({'success': False, 'error': 'direction 必须为 up 或 down'}), 400

    if ok:
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': '无法移动（已是首条/末条）'}), 400


# ── REST API 图片 CRUD ──────────────────────────────────────────────
@bp.route('/api/v1/inbound-orders/<int:order_id>/images', methods=['POST'])
def api_v1_inbound_orders_upload_image(order_id):
    """上传图片 → 201"""
    order = InboundOrder.get_by_id(order_id)
    if not order:
        return jsonify({'success': False, 'error': '订单不存在'}), 404

    if 'image' not in request.files:
        return jsonify({'success': False, 'error': '未提供图片'}), 400
    file = request.files['image']
    if file.filename == '':
        return jsonify({'success': False, 'error': '未选择文件'}), 400

    upload_dir, date_str = _get_upload_dir()

    ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else 'png'
    if ext not in ('png', 'jpg', 'jpeg', 'gif', 'webp'):
        ext = 'png'
    safe_name = os.path.basename(file.filename)  # 防路径穿越
    filename = f"inbound_{order_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{safe_name}"
    filepath = os.path.join(upload_dir, filename)
    file.save(filepath)

    image_id = InboundImage.create(order_id, filepath, file.filename)
    relative_path = f"{date_str}/{filename}"
    return jsonify({'success': True, 'image_id': image_id, 'image': relative_path}), 201


@bp.route('/api/v1/inbound-orders/images/<int:image_id>', methods=['DELETE'])
def api_v1_inbound_orders_delete_image(image_id):
    """删除图片 → 200"""
    if InboundImage.delete(image_id):
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': '图片不存在'}), 404
