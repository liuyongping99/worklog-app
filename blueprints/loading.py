"""装柜订单蓝图。

包含：
- /loading-orders 页面（带日期过滤、图片画廊）
- /api/v1/loading-orders/* REST API（订单/明细/图片 CRUD + 移动）
"""
import os
import re
import base64
from datetime import date, timedelta, datetime
from flask import Blueprint, render_template, request, jsonify, current_app
from models import (
    LoadingOrder, LoadingOrderRecord, LoadingOrderImage, ProductUnit, PieceConversion, get_db, AuditLog
)
from blueprints._helpers import (
    get_upload_dir as get_helpers_upload_dir,
    get_ypp, calc_hint, check_remark, summarize_remarks,
    get_piece_conversion, calc_piece_quantity, check_piece_mismatch,
    validate_image_content, check_uploaded_image,
)

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
            ypp = get_ypp(item['product_name'], item.get('specification', ''), units_cache=units)
            item['unit_hint'] = calc_hint(item['quantity'], ypp, unit=item.get('unit', ''), remark=item.get('remark', ''))
            item['mismatch'] = check_remark(
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

    order_images = LoadingOrderImage.get_all_by_orders(
        order_ids=[g['order_pk'] for g in groups]
    )
    # 按 source 拆分:非 AI(商品行下方图区) + AI(商品行上方 AI 图区)
    # get_all_by_orders 返回"订单级 + record 级"全部图;这里只要订单级(record_pk IS NULL)
    order_non_ai = {}
    ai_images = {}
    for oid, imgs in order_images.items():
        for img in imgs:
            if img.get('record_pk') is not None:
                continue  # record 级图由下面 get_by_record 循环补,避免重复
            target = ai_images if img.get('source') == 'ai' else order_non_ai
            target.setdefault(oid, []).append(img)
    # 合并 record 级图
    for grp in groups:
        for rec in grp.get('records', []):
            for img in LoadingOrderImage.get_by_record(rec['id']):
                target = ai_images if img.get('source') == 'ai' else order_non_ai
                target.setdefault(grp['order_pk'], []).append(img)
    for oid in order_non_ai:
        order_non_ai[oid].sort(key=lambda x: (x.get('sort_order', 0), x.get('id', 0)))
    for oid in ai_images:
        ai_images[oid].sort(key=lambda x: (x.get('sort_order', 0), x.get('id', 0)))
    current_img_cols = request.cookies.get('loadingImgCols', '3')
    return render_template(
        'loading-orders.html',
        groups=groups,
        order_images=order_non_ai,
        ai_images=ai_images,
        current_img_cols=current_img_cols,
        today=date.today().strftime('%Y-%m-%d'),
        start_date=start_date,
        end_date=end_date,
        page_title='装柜订单',
        unit_list=unit_list,
        piece_conversions=piece_conv_list,
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
        from flask import current_app
        current_app.logger.exception('loading create 失败')
        return jsonify({'success': False, 'error': '服务异常，请稍后重试'}), 500


@bp.route('/api/v1/loading-orders/<int:order_id>', methods=['DELETE'])
def api_v1_loading_orders_delete(order_id):
    """删除装柜订单 → 200（有明细或图片时拒绝）"""
    order = LoadingOrder.get_by_id(order_id)
    if not order:
        return jsonify({'success': False, 'error': '订单不存在'}), 404
    if order.get('is_locked'):
        return jsonify({'success': False, 'error': '订单已锁定，无法删除'}), 403
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
        AuditLog.log('update_customer_and_note', 'loading_order', order_id, detail={'customer': new_customer})
        return jsonify({'success': True, 'customer': new_customer, 'order_num': result.get('order_num'), 'order_note': note})

    if 'order_note' in data:
        if order.get('is_locked'):
            return jsonify({'success': False, 'error': '订单已锁定，无法修改备注'}), 403
        note = str(data['order_note']).strip()
        LoadingOrder.set_note(order_id, note)
        AuditLog.log('update_note', 'loading_order', order_id, detail={'note': note[:80]})
        return jsonify({'success': True, 'order_note': note})

    if 'doc_number' in data:
        if order.get('is_locked'):
            return jsonify({'success': False, 'error': '订单已锁定，无法修改单据编号'}), 403
        doc_number = str(data['doc_number']).strip()[:200]
        LoadingOrder.set_doc_number(order_id, doc_number)
        AuditLog.log('update_doc_number', 'loading_order', order_id, detail={'doc_number': doc_number})
        return jsonify({'success': True, 'doc_number': doc_number})

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

    if 'img_cols' in data:
        try:
            cols = int(data['img_cols'])
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': '列数必须是整数'}), 400
        if cols < 1 or cols > 5:
            return jsonify({'success': False, 'error': '列数范围为1-5'}), 400
        LoadingOrder.set_img_cols(order_id, cols)
        return jsonify({'success': True, 'img_cols': cols})

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
    AuditLog.log('add_record', 'loading_order', order_id, detail={'product': product_name, 'qty': quantity, 'unit': data.get('unit', '支')})
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
            unit = rec.get('unit', '支')
            if unit == '码':
                unit = 'y'  # 三层防御：批加端也归一化（与单条 add、PUT 端点保持一致）
            cursor.execute(
                'INSERT INTO loading_order_records (order_pk,product_name,specification,quantity,unit,remark,created_at) VALUES (?,?,?,?,?,?,?)',
                (order_id, rec.get('product_name', ''), rec.get('specification', ''),
                 str(rec.get('quantity', '')), unit, rec.get('remark', ''),
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
        from flask import current_app
        current_app.logger.exception('loading batch-add 失败: order_id=%s', order_id)
        return jsonify({'success': False, 'error': '服务异常，请稍后重试'}), 500
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

    if data.get('unit') == '码':
        data['unit'] = 'y'
    LoadingOrderRecord.update(record_id, data)
    updated = LoadingOrderRecord.get_by_id(record_id)
    AuditLog.log('update_record', 'loading_order', updated['order_pk'], detail={'record_id': record_id})
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
    AuditLog.log('delete_record', 'loading_order', record['order_pk'], detail={'record_id': record_id, 'product': record.get('product_name', '')})
    return jsonify({'success': True})


@bp.route('/api/v1/loading-orders/records/<int:record_id>/verify-warning', methods=['POST'])
def api_v1_loading_orders_record_verify_warning(record_id):
    """切换某条校验规则的核查状态（与出货 /api/v1/shipping-orders/... 同语义）。

    Body: {rule_id, verified}
    2026-07-30 新增:共享模板原来把这个请求硬编码发到出货端点,装柜页点「✓ 核查」
    会拿装柜 record_id 去改 shipping_records（串表脏写）。现在共享模板走 _smartAddApi,
    装柜必须有自己的端点。
    """
    record = LoadingOrderRecord.get_by_id(record_id)
    if not record:
        return jsonify({'success': False, 'error': '记录不存在'}), 404
    data = request.get_json() or {}
    rule_id = (data.get('rule_id') or '').strip()
    verified = bool(data.get('verified'))
    if not rule_id:
        return jsonify({'success': False, 'error': 'rule_id 不能为空'}), 400
    current = LoadingOrderRecord.set_verified_warning(record_id, rule_id, verified)
    AuditLog.log(
        'verify_warning' if verified else 'unverify_warning',
        'loading_record',
        record_id,
        detail={'rule_id': rule_id}
    )
    return jsonify({'success': True, 'verified_warnings': current, 'rule_id': rule_id, 'verified': verified})


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
        ok = LoadingOrderRecord.move_down(record_id)
    else:
        return jsonify({'success': False, 'error': 'direction 必须为 up 或 down'}), 400

    if ok:
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': '无法移动（已是首条/末条）'}), 400


# ── AI 图片识别（双引擎：Moonshot + PaddleOCR + DeepSeek）──────
@bp.route('/api/v1/loading-orders/ai-recognize', methods=['POST'])
def loading_ai_recognize():
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
@bp.route('/api/v1/loading-orders/<int:order_id>/images', methods=['POST'])
def api_v1_loading_orders_upload_image(order_id):
    """上传图片 → 201（支持 multipart 文件和 base64 粘贴）"""
    order = LoadingOrder.get_by_id(order_id)
    if not order:
        return jsonify({'success': False, 'error': '订单不存在'}), 404
    if order.get('is_locked'):
        return jsonify({'success': False, 'error': '订单已锁定，无法上传图片'}), 403

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
            try:
                validate_image_content(filepath)
            except ValueError as e:
                return jsonify({'success': False, 'error': str(e)}), 400
            source = request.form.get('source', 'upload') if not request.is_json else data.get('source', 'upload')
            if source not in ('upload', 'ai'):
                source = 'upload'
            image_id = LoadingOrderImage.create(order_id, filepath, 'pasted_image', source)
            relative_path = f"{date_str}/{filename}"
            return jsonify({'success': True, 'image_id': image_id, 'image': relative_path}), 201

    if 'image' not in request.files:
        return jsonify({'success': False, 'error': '未提供图片'}), 400
    file = request.files['image']
    if file.filename == '':
        return jsonify({'success': False, 'error': '未选择文件'}), 400

    try:
        ext = check_uploaded_image(file)
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400

    # 防路径穿越 + 防文件名 XSS：白名单 [A-Za-z0-9._-]，其他字符替换为 _
    safe_name = re.sub(r'[^A-Za-z0-9._-]', '_', os.path.basename(file.filename))
    filename = f"loading_{order_id}_{datetime.now().strftime('%Y%m%d%H%M%S')}_{safe_name}"
    filepath = os.path.join(upload_dir, filename)
    file.save(filepath)
    try:
        validate_image_content(filepath)
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400

    source = request.form.get('source', 'upload')
    if source not in ('upload', 'ai'):
        source = 'upload'
    image_id = LoadingOrderImage.create(order_id, filepath, file.filename, source)
    relative_path = f"{date_str}/{filename}"
    AuditLog.log('upload_image', 'loading_order', order_id, detail={'filename': filename, 'source': source})
    return jsonify({'success': True, 'image_id': image_id, 'image': relative_path}), 201


@bp.route('/api/v1/loading-orders/images/<int:image_id>', methods=['DELETE'])
def api_v1_loading_orders_delete_image(image_id):
    """删除图片 → 200"""
    img = LoadingOrderImage.get_by_id(image_id)
    if not img:
        return jsonify({'success': False, 'error': '图片不存在'}), 404
    order = LoadingOrder.get_by_id(img['order_pk'])
    if order and order.get('is_locked'):
        return jsonify({'success': False, 'error': '订单已锁定，无法删除图片'}), 403
    if LoadingOrderImage.delete(image_id):
        AuditLog.log('delete_image', 'loading_order', img['order_pk'], detail={'image_id': image_id})
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': '图片不存在'}), 404


# ── 商品行级图片(挂在 record 上,合并订单级共享图展示) ──
def _save_one_uploaded_file(file, upload_dir):
    """保存一个上传文件到 upload_dir,返回 (filepath, original_name)。失败抛 ValueError。"""
    if not file or not file.filename:
        raise ValueError('未选择文件')
    ext = check_uploaded_image(file)
    filename = f"{uuid.uuid4().hex}.{ext}"
    filepath = os.path.join(upload_dir, filename)
    file.save(filepath)
    validate_image_content(filepath)
    return filepath, file.filename


def _save_one_base64_image(data_url, upload_dir):
    """保存一个 data:image base64 到 upload_dir。失败抛 ValueError。"""
    if not data_url or not data_url.startswith('data:image'):
        raise ValueError('无效的图片数据')
    header, base64_data = data_url.split(',', 1)
    hl = header.lower()
    if 'png' in hl: ext = 'png'
    elif 'gif' in hl: ext = 'gif'
    elif 'webp' in hl: ext = 'webp'
    else:
        ext = 'jpg'
    img_bytes = base64.b64decode(base64_data)
    filename = f"{uuid.uuid4().hex}.{ext}"
    filepath = os.path.join(upload_dir, filename)
    with open(filepath, 'wb') as f:
        f.write(img_bytes)
    validate_image_content(filepath)
    return filepath, 'pasted_image'


@bp.route('/api/v1/loading-orders/records/<int:record_id>/images', methods=['POST'])
def api_v1_loading_orders_record_upload_images(record_id):
    """上传装柜商品行图片(多文件),按上传顺序赋 sort_order → 201"""
    record = LoadingOrderRecord.get_by_id(record_id)
    if not record:
        return jsonify({'success': False, 'error': '记录不存在'}), 404
    order = LoadingOrder.get_by_id(record['order_pk'])
    if order and order.get('is_locked'):
        return jsonify({'success': False, 'error': '该订单已锁定，无法上传图片'}), 403

    upload_dir, month_str = _get_upload_dir()
    source = 'upload'
    saved = []

    files = request.files.getlist('image')
    if files:
        for f in files:
            try:
                filepath, original_name = _save_one_uploaded_file(f, upload_dir)
            except ValueError as e:
                return jsonify({'success': False, 'error': str(e)}), 400
            image_id = LoadingOrderImage.create(
                order_pk=record['order_pk'],
                file_path=filepath,
                original_name=original_name,
                source=source,
                record_pk=record_id,
            )
            rel_path = os.path.join(month_str, os.path.basename(filepath)).replace('\\', '/')
            img = LoadingOrderImage.get_by_id(image_id)
            saved.append({
                'image_id': image_id,
                'image': rel_path,
                'original_name': original_name,
                'sort_order': img['sort_order'],
            })
        for s in saved:
            AuditLog.log('upload_image', 'loading_order', record['order_pk'],
                         detail={'filename': s['image'], 'source': source, 'record_id': record_id})
        return jsonify({'success': True, 'images': saved, 'count': len(saved)}), 201

    if request.is_json:
        data = request.get_json() or {}
        try:
            filepath, original_name = _save_one_base64_image(data.get('image', ''), upload_dir)
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        source = data.get('source', 'upload')
        image_id = LoadingOrderImage.create(
            order_pk=record['order_pk'],
            file_path=filepath,
            original_name=original_name,
            source=source,
            record_pk=record_id,
        )
        rel_path = os.path.join(month_str, os.path.basename(filepath)).replace('\\', '/')
        img = LoadingOrderImage.get_by_id(image_id)
        AuditLog.log('upload_image', 'loading_order', record['order_pk'],
                     detail={'filename': rel_path, 'source': source, 'record_id': record_id})
        return jsonify({'success': True, 'images': [{
            'image_id': image_id, 'image': rel_path,
            'original_name': original_name, 'sort_order': img['sort_order'],
        }], 'count': 1}), 201

    return jsonify({'success': False, 'error': '未提供图片'}), 400


@bp.route('/api/v1/loading-orders/records/<int:record_id>/images-area', methods=['GET'])
def api_v1_loading_orders_record_images_area(record_id):
    """某 record 的合并图片区:该 record 专属图 + 订单共享图,统一排序"""
    record = LoadingOrderRecord.get_by_id(record_id)
    if not record:
        return jsonify({'success': False, 'error': '记录不存在'}), 404
    items = LoadingOrderImage.get_combined_for_record(record['order_pk'], record_id)
    for it in items:
        it['scope'] = 'order' if it.get('record_pk') is None else 'record'
    return jsonify({'success': True, 'images': items, 'count': len(items)})
