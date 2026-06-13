"""装柜订单蓝图。

包含：
- /loading-orders 页面（带日期过滤、图片画廊）
- /api/v1/loading-orders/* REST API（订单/明细/图片 CRUD + 移动）
"""
import os
import base64
from datetime import date, timedelta, datetime
from flask import Blueprint, render_template, request, jsonify
from models import (
    LoadingOrder, LoadingOrderRecord, LoadingOrderImage, ProductUnit, get_db, AuditLog
)
from blueprints._helpers import get_upload_dir as get_helpers_upload_dir, get_ypp, calc_hint, check_remark, summarize_remarks

bp = Blueprint('loading', __name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── 页面 ──────────────────────────────────────────────
@bp.route('/loading-orders')
def loading_orders():
    """装柜订单页面"""
    start_date = request.args.get('start_date', '').strip()
    end_date = request.args.get('end_date', '').strip()
    if not start_date or not end_date:
        end = date.today()
        start = end - timedelta(days=9)
        start_date = start.isoformat()
        end_date = end.isoformat()
    groups = LoadingOrderRecord.get_grouped(start_date, end_date)

    units = ProductUnit.get_all()
    unit_list = [{
        'product_name': u['product_name'],
        'spec_keyword': u['spec_keyword'] or '',
        'yards_per_piece': u['yards_per_piece'] / 100.0,
        'is_usingyardforcounting': bool(u['is_usingyardforcounting'])
    } for u in units]

    for group in groups:
        for item in group['records']:
            ypp = get_ypp(item['product_name'], item.get('specification', ''), units_cache=units)
            item['unit_hint'] = calc_hint(item['quantity'], ypp)
            item['mismatch'] = check_remark(
                item.get('remark', ''),
                item['quantity'],
                ypp
            )
        group.update(summarize_remarks(group['records']))

    order_images = LoadingOrderImage.get_all_by_orders(
        order_ids=[g['order_pk'] for g in groups]
    )
    current_img_cols = request.cookies.get('loadingImgCols', '3')
    return render_template(
        'loading-orders.html',
        groups=groups,
        order_images=order_images,
        current_img_cols=current_img_cols,
        today=date.today().strftime('%Y-%m-%d'),
        start_date=start_date,
        end_date=end_date,
        page_title='装柜订单',
        unit_list=unit_list,
    )


# ── REST API 订单 CRUD ──────────────────────────────────────────────
@bp.route('/api/v1/loading-orders', methods=['POST'])
def api_v1_loading_orders_create():
    """创建装柜订单 → 201"""
    data = request.get_json() if request.is_json else request.form
    date_val = data.get('date')
    customer = data.get('customer', '')
    if not date_val:
        return jsonify({'success': False, 'error': '日期不能为空'}), 400
    try:
        order_id = LoadingOrder.create(date_val, customer)
        AuditLog.log('create_order', 'loading_order', order_id, detail={'date': date_val, 'customer': customer})
        return jsonify({'success': True, 'id': order_id}), 201
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/v1/loading-orders/<int:order_id>', methods=['DELETE'])
def api_v1_loading_orders_delete(order_id):
    """删除装柜订单 → 200（有明细或图片时拒绝）"""
    order = LoadingOrder.get_by_id(order_id)
    if not order:
        return jsonify({'success': False, 'error': '订单不存在'}), 404
    result = LoadingOrder.delete(order_id)
    if not result.get('success'):
        return jsonify(result), 400
    AuditLog.log('delete_order', 'loading_order', order_id, detail={'customer': order.get('customer')})
    return jsonify({'success': True})


@bp.route('/api/v1/loading-orders/<int:order_id>', methods=['PATCH'])
def api_v1_loading_orders_update(order_id):
    """更新订单（锁定/解锁/备注）→ 200"""
    order = LoadingOrder.get_by_id(order_id)
    if not order:
        return jsonify({'success': False, 'error': '订单不存在'}), 404
    data = request.get_json()
    if data is None:
        return jsonify({'success': False, 'error': '请求体不能为空'}), 400
    if 'is_locked' in data:
        if data['is_locked']:
            LoadingOrder.lock(order_id)
            AuditLog.log('lock_order', 'loading_order', order_id)
        else:
            LoadingOrder.unlock(order_id)
            AuditLog.log('unlock_order', 'loading_order', order_id)
        return jsonify({'success': True, 'is_locked': bool(data['is_locked'])})
    # 同时改备注 + 客户（弹框一次保存两个字段）—— 必须先判断
    if 'order_note' in data and 'customer' in data:
        if order.get('is_locked'):
            return jsonify({'success': False, 'error': '订单已锁定，无法修改'}), 403
        new_customer = str(data['customer']).strip()
        note = str(data['order_note']).strip()
        if not new_customer:
            return jsonify({'success': False, 'error': '客户名称不能为空'}), 400
        result = LoadingOrder.set_customer(order_id, new_customer)
        if not result.get('success'):
            return jsonify(result), 400
        LoadingOrder.set_note(order_id, note)
        return jsonify({'success': True, 'customer': new_customer, 'order_note': note})

    if 'order_note' in data:
        if order.get('is_locked'):
            return jsonify({'success': False, 'error': '订单已锁定，无法修改备注'}), 403
        note = str(data['order_note']).strip()
        LoadingOrder.set_note(order_id, note)
        return jsonify({'success': True, 'order_note': note})

    if 'customer' in data:
        if order.get('is_locked'):
            return jsonify({'success': False, 'error': '订单已锁定，无法修改客户名称'}), 403
        new_customer = str(data['customer']).strip()
        if not new_customer:
            return jsonify({'success': False, 'error': '客户名称不能为空'}), 400
        result = LoadingOrder.set_customer(order_id, new_customer)
        if result.get('success'):
            return jsonify(result)
        return jsonify(result), 400

    return jsonify({'success': False, 'error': '无可更新的字段'}), 400


# ── REST API 明细 CRUD ──────────────────────────────────────────────
@bp.route('/api/v1/loading-orders/<int:order_id>/records', methods=['POST'])
def api_v1_loading_orders_add_record(order_id):
    """向订单添加明细 → 201"""
    order = LoadingOrder.get_by_id(order_id)
    if not order:
        return jsonify({'success': False, 'error': '订单不存在'}), 404
    if order.get('is_locked'):
        return jsonify({'success': False, 'error': '订单已锁定，无法添加'}), 403

    data = request.get_json() if request.is_json else request.form
    product_name = data.get('product_name')
    quantity = data.get('quantity')
    if not product_name or not quantity:
        return jsonify({'success': False, 'error': '品名和数量不能为空'}), 400

    record_id = LoadingOrderRecord.create(
        date='', customer='',
        product_name=product_name,
        specification=data.get('specification', ''),
        quantity=quantity,
        unit=data.get('unit', '支'),
        remark=data.get('remark', ''),
        order_pk=order_id
    )
    record = LoadingOrderRecord.get_by_id(record_id)
    return jsonify({'success': True, 'id': record_id, 'record': record}), 201


@bp.route('/api/v1/loading-orders/<int:order_id>/records/batch', methods=['POST'])
def api_v1_loading_orders_add_records_batch(order_id):
    """批量添加明细 → 201"""
    order = LoadingOrder.get_by_id(order_id)
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
        result_records = []
        for rec in records:
            cursor.execute(
                'INSERT INTO loading_order_records (order_pk,product_name,specification,quantity,unit,remark,created_at) VALUES (?,?,?,?,?,?,?)',
                (order_id, rec.get('product_name', ''), rec.get('specification', ''),
                 str(rec.get('quantity', '')), rec.get('unit', '支'), rec.get('remark', ''),
                 datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
            result_records.append({
                'id': cursor.lastrowid,
                'order_pk': order_id,
                'product_name': rec.get('product_name', ''),
                'specification': rec.get('specification', ''),
                'quantity': str(rec.get('quantity', '')),
                'unit': rec.get('unit', '支'),
                'remark': rec.get('remark', '')
            })
        conn.commit()
        return jsonify({'success': True, 'count': len(result_records), 'records': result_records}), 201
    except Exception as e:
        conn.rollback()
        return jsonify({'success': False, 'error': str(e)}), 500
    finally:
        conn.close()


@bp.route('/api/v1/loading-orders/records/<int:record_id>', methods=['PUT'])
def api_v1_loading_orders_update_record(record_id):
    """更新明细 → 200"""
    record = LoadingOrderRecord.get_by_id(record_id)
    if not record:
        return jsonify({'success': False, 'error': '记录不存在'}), 404
    order = LoadingOrder.get_by_id(record['order_pk'])
    if order and order.get('is_locked'):
        return jsonify({'success': False, 'error': '订单已锁定，无法修改'}), 403

    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': '请求体不能为空'}), 400

    LoadingOrderRecord.update(record_id, data)
    updated = LoadingOrderRecord.get_by_id(record_id)
    return jsonify({'success': True, 'record': updated})


@bp.route('/api/v1/loading-orders/records/<int:record_id>', methods=['DELETE'])
def api_v1_loading_orders_delete_record(record_id):
    """删除明细 → 200"""
    record = LoadingOrderRecord.get_by_id(record_id)
    if not record:
        return jsonify({'success': False, 'error': '记录不存在'}), 404
    order = LoadingOrder.get_by_id(record['order_pk'])
    if order and order.get('is_locked'):
        return jsonify({'success': False, 'error': '订单已锁定，无法删除'}), 403
    LoadingOrderRecord.delete(record_id)
    return jsonify({'success': True})


@bp.route('/api/v1/loading-orders/records/<int:record_id>/move', methods=['PATCH'])
def api_v1_loading_orders_move_record(record_id):
    """移动明细排序 → 200"""
    record = LoadingOrderRecord.get_by_id(record_id)
    if not record:
        return jsonify({'success': False, 'error': '记录不存在'}), 404
    order = LoadingOrder.get_by_id(record['order_pk'])
    if order and order.get('is_locked'):
        return jsonify({'success': False, 'error': '订单已锁定，无法操作'}), 403

    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': '请求体不能为空'}), 400
    direction = data.get('direction', '')

    if direction == 'up':
        ok = LoadingOrderRecord.move_up(record_id)
    elif direction == 'down':
        ok = LoadingOrderRecord.move_down(record_id)
    else:
        return jsonify({'success': False, 'error': 'direction 必须为 up 或 down'}), 400

    if ok:
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': '无法移动（已是首条/末条）'}), 400


# ── REST API 图片 CRUD ──────────────────────────────────────────────
@bp.route('/api/v1/loading-orders/<int:order_id>/images', methods=['POST'])
def api_v1_loading_orders_upload_image(order_id):
    """上传图片 → 201（支持 multipart 文件和 base64 粘贴）"""
    order = LoadingOrder.get_by_id(order_id)
    if not order:
        return jsonify({'success': False, 'error': '订单不存在'}), 404

    upload_dir, date_str = get_helpers_upload_dir()

    # 支持 base64 粘贴
    if request.is_json:
        data = request.get_json()
        image_data = data.get('image')
        if image_data and image_data.startswith('data:image'):
            header, base64_data = image_data.split(',', 1)
            if 'png' in header.lower():
                ext = 'png'
            elif 'gif' in header.lower():
                ext = 'gif'
            elif 'webp' in header.lower():
                ext = 'webp'
            else:
                ext = 'jpg'
            img_bytes = base64.b64decode(base64_data)
            filename = f"loading_{order_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}.{ext}"
            filepath = os.path.join(upload_dir, filename)
            with open(filepath, 'wb') as f:
                f.write(img_bytes)
            image_id = LoadingOrderImage.create(order_id, filepath, 'pasted_image')
            relative_path = f"{date_str}/{filename}"
            return jsonify({'success': True, 'image_id': image_id, 'image': relative_path}), 201

    if 'image' not in request.files:
        return jsonify({'success': False, 'error': '未提供图片'}), 400
    file = request.files['image']
    if file.filename == '':
        return jsonify({'success': False, 'error': '未选择文件'}), 400

    ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else 'png'
    if ext not in ('png', 'jpg', 'jpeg', 'gif', 'webp'):
        ext = 'png'
    safe_name = os.path.basename(file.filename)  # 防路径穿越
    filename = f"loading_{order_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{safe_name}"
    filepath = os.path.join(upload_dir, filename)
    file.save(filepath)

    image_id = LoadingOrderImage.create(order_id, filepath, file.filename)
    relative_path = f"{date_str}/{filename}"
    return jsonify({'success': True, 'image_id': image_id, 'image': relative_path}), 201


@bp.route('/api/v1/loading-orders/images/<int:image_id>', methods=['DELETE'])
def api_v1_loading_orders_delete_image(image_id):
    """删除图片 → 200"""
    if LoadingOrderImage.delete(image_id):
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': '图片不存在'}), 404
