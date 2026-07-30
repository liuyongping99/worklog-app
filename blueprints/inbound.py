"""入库记录蓝图。

包含：
- /inbound-records 页面（带日期过滤、商品单位提示计算）
- /api/v1/inbound-orders/* REST API（订单/明细/图片 CRUD + 移动 + AI 识别）
"""
import os
import re
from datetime import date as date_cls, timedelta, datetime
from flask import Blueprint, render_template, request, jsonify, current_app
from models import (
    InboundOrder, InboundRecord, InboundImage, ProductUnit, PieceConversion, get_db, AuditLog
)
from blueprints._helpers import (
    get_upload_dir as get_helpers_upload_dir,
    get_ypp, calc_hint, check_remark, summarize_remarks,
    get_piece_conversion, calc_piece_quantity, check_piece_mismatch,
    validate_image_content, check_uploaded_image,
)

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

    def _check_remark(remark, quantity_str, ypp):
        return check_remark(remark, quantity_str, ypp)

    # 件数换算规则
    piece_convs = PieceConversion.get_all()
    piece_conv_list = [{
        'product_name': pc['product_name'],
        'spec_keyword': pc['spec_keyword'] or '',
        'units_per_piece': pc['units_per_piece'],
        'target_unit': pc['target_unit'],
    } for pc in piece_convs]

    def _get_piece_conv(product_name, spec):
        return get_piece_conversion(product_name, spec, cache=piece_convs)

    def _check_piece(remark, quantity_str, conv):
        return check_piece_mismatch(remark, quantity_str, conv)

    for group in groups:
        for item in group['records']:
            ypp = _get_ypp(item['product_name'], item.get('specification', ''))
            item['unit_hint'] = calc_hint(item['quantity'], ypp, unit=item.get('unit', ''), remark=item.get('remark', ''))
            item['mismatch'] = _check_remark(
                item.get('remark', ''),
                item['quantity'],
                ypp
            )

            # 件数换算
            pc = _get_piece_conv(item['product_name'], item.get('specification', ''))
            if pc:
                item['piece_hint'] = f"{pc['units_per_piece']}{pc['target_unit']}/件"
                item['piece_mismatch'] = _check_piece(
                    item.get('remark', ''),
                    item['quantity'],
                    pc
                )
            else:
                item['piece_hint'] = ''
                item['piece_mismatch'] = ''
        group.update(summarize_remarks(group['records']))

    return render_template(
        'inbound-records.html',
        groups=groups,
        page_title='入库记录',
        today=today,
        start_date=start_date,
        end_date=end_date,
        unit_list=unit_list,
        piece_conversions=piece_conv_list,
    )




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
        from flask import current_app
        current_app.logger.exception('inbound create 失败')
        return jsonify({'success': False, 'error': '服务异常，请稍后重试'}), 500


@bp.route('/api/v1/inbound-orders/<int:order_id>', methods=['DELETE'])
def api_v1_inbound_orders_delete(order_id):
    """删除入库订单 → 200（有明细或图片时拒绝）"""
    order = InboundOrder.get_by_id(order_id)
    if not order:
        return jsonify({'success': False, 'error': '订单不存在'}), 404
    if order.get('is_locked'):
        return jsonify({'success': False, 'error': '订单已锁定，无法删除'}), 403
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
        AuditLog.log('update_supplier_and_note', 'inbound_order', order_id, detail={'supplier': new_supplier})
        return jsonify({'success': True, 'supplier': new_supplier, 'order_num': result.get('order_num'), 'order_note': note})

    if 'order_note' in data:
        if order.get('is_locked'):
            return jsonify({'success': False, 'error': '订单已锁定，无法修改备注'}), 403
        note = str(data['order_note']).strip()
        InboundOrder.set_note(order_id, note)
        AuditLog.log('update_note', 'inbound_order', order_id, detail={'note': note[:80]})
        return jsonify({'success': True, 'order_note': note})

    if 'doc_number' in data:
        if order.get('is_locked'):
            return jsonify({'success': False, 'error': '订单已锁定，无法修改单据编号'}), 403
        doc_number = str(data['doc_number']).strip()[:200]
        InboundOrder.set_doc_number(order_id, doc_number)
        AuditLog.log('update_doc_number', 'inbound_order', order_id, detail={'doc_number': doc_number})
        return jsonify({'success': True, 'doc_number': doc_number})

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
    AuditLog.log('add_record', 'inbound_order', order_id, detail={'product': product_name, 'qty': quantity, 'unit': data.get('unit', '支')})
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
            unit = rec.get('unit', '支')
            if unit == '码':
                unit = 'y'
            cursor.execute(
                'INSERT INTO inbound_records (order_pk, product_name, specification, quantity, unit, remark, sort_order, created_at) VALUES (?,?,?,?,?,?,?,?)',
                (order_id, rec.get('product_name', ''), rec.get('specification', ''),
                 str(rec.get('quantity', '')), unit, rec.get('remark', ''),
                 sort_order, datetime.now().strftime('%Y-%m-%d %H:%M:%S')))
            result_records.append({
                'id': cursor.lastrowid,
                'order_pk': order_id,
                'product_name': rec.get('product_name', ''),
                'specification': rec.get('specification', ''),
                'quantity': str(rec.get('quantity', '')),
                'unit': unit,
                'remark': rec.get('remark', ''),
                'sort_order': sort_order
            })
        conn.commit()
        if result_records:
            AuditLog.log('batch_add_records', 'inbound_order', order_id, detail={'count': len(result_records)})
        return jsonify({'success': True, 'count': len(result_records), 'records': result_records}), 201
    except Exception as e:
        conn.rollback()
        from flask import current_app
        current_app.logger.exception('inbound batch-add 失败: order_id=%s', order_id)
        return jsonify({'success': False, 'error': '服务异常，请稍后重试'}), 500
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
    if data.get('unit') == '码':
        data['unit'] = 'y'
    InboundRecord.update(record_id, data)
    updated = InboundRecord.get_by_id(record_id)
    AuditLog.log('update_record', 'inbound_order', updated['order_pk'], detail={'record_id': record_id})
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
    AuditLog.log('delete_record', 'inbound_order', record['order_pk'], detail={'record_id': record_id, 'product': record.get('product_name', '')})
    return jsonify({'success': True})


@bp.route('/api/v1/inbound-orders/records/<int:record_id>/verify-warning', methods=['POST'])
def api_v1_inbound_orders_record_verify_warning(record_id):
    """切换某条校验规则的核查状态（与出货 /api/v1/shipping-orders/... 同语义）。

    Body: {rule_id, verified}
    - verified=true → 把 rule_id 写入 verified_warnings(JSON),该条警告折叠到下方
    - verified=false → 从 verified_warnings 移除 rule_id,警告重新弹出
    与整行 verified 字段并存:verified=1 仍代表"整行所有警告已核查"。
    """
    record = InboundRecord.get_by_id(record_id)
    if not record:
        return jsonify({'success': False, 'error': '记录不存在'}), 404
    data = request.get_json() or {}
    rule_id = (data.get('rule_id') or '').strip()
    verified = bool(data.get('verified'))
    if not rule_id:
        return jsonify({'success': False, 'error': 'rule_id 不能为空'}), 400
    current = InboundRecord.set_verified_warning(record_id, rule_id, verified)
    AuditLog.log(
        'verify_warning' if verified else 'unverify_warning',
        'inbound_record',
        record_id,
        detail={'rule_id': rule_id}
    )
    return jsonify({'success': True, 'verified_warnings': current, 'rule_id': rule_id, 'verified': verified})


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
        AuditLog.log('move_record', 'inbound_order', record['order_pk'], detail={'record_id': record_id, 'direction': direction})
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': '无法移动（已是首条/末条）'}), 400


# ── AI 图片识别（双引擎：Moonshot + PaddleOCR + DeepSeek）──────
@bp.route('/api/v1/inbound-orders/ai-recognize', methods=['POST'])
def inbound_ai_recognize():
    """接收图片，调用 OCR 引擎识别商品表格，返回 JSON。"""
    from blueprints.ocr_engine import get_ocr_engine

    engine_name = (
        request.form.get('engine') or
        request.args.get('engine') or
        os.environ.get('OCR_BACKEND', 'moonshot')
    )

    if engine_name not in ('moonshot', 'paddleocr', 'deepseek'):
        return jsonify({
            'success': False,
            'error': f'不支持的识别引擎: {engine_name}',
            'hint': '请使用 "moonshot"、"paddleocr" 或 "deepseek"'
        }), 400

    if 'image' not in request.files:
        return jsonify({
            'success': False, 'error': '没有上传图片',
            'hint': '请先选择一张图片再点击识别'
        }), 400
    file = request.files['image']
    if file.filename == '':
        return jsonify({
            'success': False, 'error': '未选择文件',
            'hint': '请先选择一张图片再点击识别'
        }), 400

    img_bytes = file.read()

    try:
        engine = get_ocr_engine(engine_name)
    except RuntimeError as e:
        return jsonify({'success': False, 'error': '引擎初始化失败', 'hint': str(e)}), 500
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e),
                        'hint': '请使用 "moonshot"、"paddleocr" 或 "deepseek"'}), 400

    try:
        result = engine.recognize(img_bytes, file.filename)
    except Exception as e:
        current_app.logger.exception('AI 识别运行时错误: %s', e)
        return jsonify({
            'success': False,
            'error': f'识别引擎运行异常：{type(e).__name__}',
            'hint': '请稍后重试，或切换到其他识别引擎'
        }), 500

    if result.get('success'):
        return jsonify(result)
    else:
        error_msg = result.get('error', '')
        if 'API key' in error_msg or '未配置' in error_msg:
            return jsonify(result), 503
        return jsonify(result), 500


# ── REST API 图片 CRUD ──────────────────────────────────────────────
@bp.route('/api/v1/inbound-orders/<int:order_id>/images', methods=['POST'])
def api_v1_inbound_orders_upload_image(order_id):
    """上传图片 → 201"""
    order = InboundOrder.get_by_id(order_id)
    if not order:
        return jsonify({'success': False, 'error': '订单不存在'}), 404
    if order.get('is_locked'):
        return jsonify({'success': False, 'error': '订单已锁定，无法上传图片'}), 403

    if 'image' not in request.files:
        return jsonify({'success': False, 'error': '未提供图片'}), 400
    file = request.files['image']
    if file.filename == '':
        return jsonify({'success': False, 'error': '未选择文件'}), 400

    try:
        ext = check_uploaded_image(file)
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400

    upload_dir, date_str = _get_upload_dir()

    safe_name = re.sub(r'[^A-Za-z0-9._-]', '_', os.path.basename(file.filename))
    filename = f"inbound_{order_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{safe_name}"
    filepath = os.path.join(upload_dir, filename)
    file.save(filepath)
    try:
        validate_image_content(filepath)
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400

    source = request.form.get('source', 'upload')
    if source not in ('upload', 'ai'):
        source = 'upload'
    image_id = InboundImage.create(order_id, filepath, file.filename, source)
    relative_path = f"{date_str}/{filename}"
    AuditLog.log('upload_image', 'inbound_order', order_id, detail={'filename': filename, 'source': source})
    return jsonify({'success': True, 'image_id': image_id, 'image': relative_path}), 201


@bp.route('/api/v1/inbound-orders/images/<int:image_id>', methods=['DELETE'])
def api_v1_inbound_orders_delete_image(image_id):
    """删除图片 → 200"""
    img = InboundImage.get_by_id(image_id)
    if not img:
        return jsonify({'success': False, 'error': '图片不存在'}), 404
    order = InboundOrder.get_by_id(img['order_pk'])
    if order and order.get('is_locked'):
        return jsonify({'success': False, 'error': '订单已锁定，无法删除图片'}), 403
    if InboundImage.delete(image_id):
        AuditLog.log('delete_image', 'inbound_order', img['order_pk'], detail={'image_id': image_id})
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': '图片不存在'}), 404
