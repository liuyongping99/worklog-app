"""装柜订单蓝图。

包含：
- /loading-orders 页面（带日期过滤、图片画廊）
- /api/v1/loading-orders/* REST API（订单/明细/图片 CRUD + 移动）
"""
import os
import re
import base64
from datetime import date, timedelta, datetime
from flask import Blueprint, render_template, request, jsonify, current_app, session, abort
from models import (
    LoadingOrder, LoadingOrderRecord, LoadingOrderImage, ProductUnit, PieceConversion, get_db, AuditLog,
    OcrMatchEvent,
    classify_record, CategoryPrompt,
)
from blueprints._helpers import (
    get_upload_dir as get_helpers_upload_dir,
    get_ypp, calc_hint, check_remark, summarize_remarks,
    get_piece_conversion, calc_piece_quantity, check_piece_mismatch,
    validate_image_content, check_uploaded_image,
    match_label_to_row, detect_bg_color,
)
from blueprints.ocr_engine import PaddleOCREngine, get_ocr_engine, OCR_MATCH_PROMPT_VERSION, is_wrinkle_label_category
from blueprints import _helpers
from models._db import get_db
from blueprints.ocr_log import set_log_context

bp = Blueprint('loading', __name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _as_bool(value, default=True):
    """把 JSON 里可能出现的多种"假值写法"统一成 bool。

    坑:bool('false') / bool('0') 在 Python 里都是 True —— 前端只要把布尔序列化成字符串
    (常见于 FormData、某些 fetch 封装、手写 JSON),取消操作就会被反向解读成"确认"。
    这里显式识别字符串假值,数字 0 与 None 也算假。
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() not in ('false', '0', 'no', 'off', '')
    return bool(value)


def _get_upload_dir():
    """薄包装，返回 (upload_dir, month_str)。"""
    return get_helpers_upload_dir()


def _build_suggestion_text(img: dict, record: dict | None) -> str:
    """模板化生成:基于 OCR 原文 + AI/人裁决,产出可供用户编辑的"补充提示词"草稿。

    不调 LLM(避免 API key 依赖),按行内变量渲染,用户可在前端编辑器再改。
    模板自带 [yellow→green] / [red→green] 上下文 + 品名/规格/部分 OCR 关键词,作为"该案例的判决依据"。
    """
    ai = img.get('match_status') or '?'
    ai_reason = img.get('reason') or ''
    pn = (record or {}).get('product_name', '') or ''
    sp = (record or {}).get('specification', '') or ''

    # 截 OCR 原文前 200 字(避免提示词太长压垮上下文)
    return (
        f'[{ai}→人工确认] 商品「{pn}」规格「{sp}」OCR标签此前判 {ai},'
        f'原因为「{ai_reason[:120]}」,但人工复核与录入明细一致。'
        f'同类案例在后续比对中,即使 OCR 文本中只出现部分关键词(如 7P / 加面 / 厚度)'
        f'也视为该规则命中,判 green。'
    )


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
    # 不再用 cookie:img_cols 由 Task 1 加的 loading_orders.img_cols 列提供,渲染走 group.img_cols
    return render_template(
        'loading-orders.html',
        groups=groups,
        order_images=order_non_ai,
        ai_images=ai_images,
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
    set_log_context(biz='loading', evt_src='ai_recognize')
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
    set_log_context(biz='loading', record_id=record_id)
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


@bp.route('/api/v1/loading-orders/images/<int:image_id>/manual-verify', methods=['POST'])
def api_v1_loading_orders_manual_verify_image(image_id):
    """人工覆盖 AI 比对结果(把红牌标记为已确认)→ 200

    Body (可选): {"verified": true|false}，默认 true。
    副作用:写 loading_order_images.human_verified + 写审计日志。
    """
    img = LoadingOrderImage.get_by_id(image_id)
    if not img:
        return jsonify({'success': False, 'error': '图片不存在'}), 404
    order = LoadingOrder.get_by_id(img['order_pk'])
    if order and order.get('is_locked'):
        return jsonify({'success': False, 'error': '订单已锁定，无法操作'}), 403
    data = request.get_json(silent=True) or {}
    verified = _as_bool(data.get('verified'), default=True)

    # 新增:append-only human_verify 事件(先读 AI 当前状态快照)
    # 写入失败不阻断主流程 — 与 AuditLog 哲学一致
    try:
        ai_snapshot = LoadingOrderImage.get_by_id(image_id)
        ai_status = ai_snapshot.get('match_status') if ai_snapshot else None
        ai_reason = ai_snapshot.get('reason') if ai_snapshot else None
        OcrMatchEvent.create(
            'human_verify',
            record_id=img.get('record_pk'),  # 行级图才有 record_pk,订单级共享图为 None
            order_id=img['order_pk'],
            image_id=image_id,
            ai_match_status=ai_status,
            ai_match_reason=ai_reason,
            human_status='green' if verified else None,  # 取消核查不写状态
            human_verified_by=session.get('operator_id'),
        )
    except Exception:
        current_app.logger.exception('human_verify 事件写库失败(不阻断)')

    LoadingOrderImage.set_human_verified(image_id, verified)
    AuditLog.log(
        'manual_verify_image', 'loading_order', img['order_pk'],
        detail={'image_id': image_id, 'human_verified': 1 if verified else 0},
    )
    return jsonify({'success': True, 'image_id': image_id, 'human_verified': verified})


@bp.route('/api/v1/loading-orders/images/<int:image_id>/fuzzy-match', methods=['POST'])
def api_v1_loading_orders_fuzzy_match_image(image_id):
    """对已上传图片重新运行本地 RapidFuzz 模糊匹配 → 返回新的判别结果。
    
    流程: 取图→取关联商品行→取OCR文字→运行 match_label_to_row→更新 loading_order_images→返回
    """
    img = LoadingOrderImage.get_by_id(image_id)
    if not img:
        return jsonify({'success': False, 'error': '图片不存在'}), 404
    order = LoadingOrder.get_by_id(img['order_pk'])
    if order and order.get('is_locked'):
        return jsonify({'success': False, 'error': '订单已锁定，无法操作'}), 403
    record_pk = img.get('record_pk')
    if not record_pk:
        return jsonify({'success': False, 'error': '该图片未关联商品行'}), 400
    record = LoadingOrderRecord.get_by_id(record_pk)
    if not record:
        return jsonify({'success': False, 'error': '关联商品行不存在'}), 404

    # 1) 取 OCR 文字：优先从 ocr_match_event 历史取，没有则重跑 PaddleOCR
    ocr_text = ''
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            'SELECT ocr_text FROM ocr_match_event '
            'WHERE image_id = ? AND event_type = ? AND ocr_text IS NOT NULL '
            'ORDER BY id DESC LIMIT 1',
            (image_id, 'record_ocr'))
        row = cur.fetchone()
        conn.close()
        if row:
            ocr_text = row['ocr_text'] or ''
    except Exception:
        pass
    if not ocr_text.strip():
        try:
            with open(img['file_path'], 'rb') as _f:
                ocr_text = get_ocr_engine('paddleocr').extract_text(_f.read()) or ''
        except Exception:
            current_app.logger.exception('重跑 PaddleOCR 失败(image_id=%s)', image_id)
    if ocr_text.strip():
        bg = detect_bg_color(img['file_path'])
        if bg:
            ocr_text = ocr_text + '\n[标签背景: ' + ('黑色' if bg == 'black' else '白色') + ']'
    else:
        return jsonify({'success': False, 'error': 'OCR 无文字，无法匹配'}), 400

    # 2) 运行本地模糊匹配
    status, score, reason = match_label_to_row(
        ocr_text, record.get('product_name', ''), record.get('specification', ''))

    # 3) 更新图片匹配结果
    LoadingOrderImage.set_match(image_id, status, score or 0, reason, source='local_fuzzy')

    return jsonify({
        'success': True,
        'image_id': image_id,
        'match_status': status,
        'match_score': score,
        'reason': reason,
        'match_source': 'local_fuzzy',
    })


@bp.route('/api/v1/loading-orders/images/<int:image_id>/ai-judge', methods=['POST'])
def api_v1_loading_orders_ai_judge_image(image_id):
    """对已上传图片重新运行 DeepSeek AI 比对 → 返回新的判别结果。

    流程: 取图→取关联商品行→取OCR文字→调用 DeepSeek compare_single_record→更新 loading_order_images→返回
    """
    set_log_context(biz='loading', image_id=image_id)
    img = LoadingOrderImage.get_by_id(image_id)
    if not img:
        return jsonify({'success': False, 'error': '图片不存在'}), 404
    order = LoadingOrder.get_by_id(img['order_pk'])
    if order and order.get('is_locked'):
        return jsonify({'success': False, 'error': '订单已锁定，无法操作'}), 403
    record_pk = img.get('record_pk')
    if not record_pk:
        return jsonify({'success': False, 'error': '该图片未关联商品行'}), 400
    record = LoadingOrderRecord.get_by_id(record_pk)
    if not record:
        return jsonify({'success': False, 'error': '关联商品行不存在'}), 404

    # 1) 取 OCR 文字
    ocr_text = ''
    try:
        conn = get_db()
        cur = conn.cursor()
        cur.execute(
            'SELECT ocr_text FROM ocr_match_event '
            'WHERE image_id = ? AND event_type = ? AND ocr_text IS NOT NULL '
            'ORDER BY id DESC LIMIT 1',
            (image_id, 'record_ocr'))
        row = cur.fetchone()
        conn.close()
        if row:
            ocr_text = row['ocr_text'] or ''
    except Exception:
        pass
    if not ocr_text.strip():
        try:
            apply_wrinkle_enhance = is_wrinkle_label_category(record.get('product_name', ''))
            with open(img['file_path'], 'rb') as _f:
                ocr_text = get_ocr_engine('paddleocr').extract_text(_f.read(), apply_wrinkle_enhance=apply_wrinkle_enhance) or ''
        except Exception:
            current_app.logger.exception('重跑 PaddleOCR 失败(image_id=%s)', image_id)
    if ocr_text.strip():
        bg = detect_bg_color(img['file_path'])
        if bg:
            ocr_text = ocr_text + '\n[标签背景: ' + ('黑色' if bg == 'black' else '白色') + ']'
    else:
        return jsonify({'success': False, 'error': 'OCR 无文字，无法判断'}), 400

    # 2) 调用 DeepSeek
    try:
        ds = get_ocr_engine('deepseek')
        if not getattr(ds, 'API_KEY', ''):
            return jsonify({'success': False, 'error': 'DeepSeek API Key 未配置'}), 503
        res = ds.compare_single_record(ocr_text, record)
        ms = (res.get('match_status') or '').lower()
        if ms not in ('green', 'yellow', 'red'):
            return jsonify({'success': False, 'error': f'DeepSeek 返回异常: {ms}'}), 502
        status, score, reason = ms, res.get('match_score'), res.get('reason') or ''
        source_label = 'deepseek'
        prompt_text = res.get('prompt_text', '')
        raw_response = res.get('raw_response', '')
    except Exception as e:
        current_app.logger.exception('DeepSeek AI 判断失败(image_id=%s)', image_id)
        return jsonify({'success': False, 'error': f'AI 判断失败: {e}'}), 502

    # 3) 更新图片匹配结果
    LoadingOrderImage.set_match(image_id, status, score or 0, reason, source=source_label)

    # 4) 持久化 AI 判断事件(prompt + 结果),供前端「详情」按钮审计
    try:
        OcrMatchEvent.create(
            'ai_match',
            record_id=record_pk,
            order_id=img['order_pk'],
            image_id=image_id,
            ocr_text=ocr_text,
            ocr_engine='paddleocr',
            prompt_payload=prompt_text,
            ai_engine='deepseek',
            ai_match_status=status,
            ai_match_score=score,
            ai_match_reason=reason or None,
            ai_raw_response=raw_response,
            prompt_version=OCR_MATCH_PROMPT_VERSION,
            product_name=record.get('product_name', ''),
            specification=record.get('specification', ''),
        )
    except Exception:
        current_app.logger.exception('ai_match 事件写库失败(不阻断)')

    return jsonify({
        'success': True,
        'image_id': image_id,
        'match_status': status,
        'match_score': score,
        'reason': reason,
        'match_source': source_label,
    })


@bp.route('/api/v1/loading-orders/images/<int:image_id>/generate-prompt-suggestion', methods=['POST'])
def api_v1_loading_orders_generate_prompt_suggestion(image_id):
    """生成提示词草稿:调人工确认"✓ 确认通过"后,图下方的按钮触发。

    Returns:
      {
        'success': True,
        'suggestion': {
          'prompt_text': str,              # 可编辑草稿
          'scope': 'category',             # 默认大类,前端可改成 spec
          'category_code': str|None,
          'product_name_keyword': str|None,
          'spec_pattern': '',              # spec 时由用户填
          'source_ocr_text': str,          # 摘要
          'source_ai_status': str,
          'source_human_status': 'green',
        }
      }
    """
    img = LoadingOrderImage.get_by_id(image_id)
    if not img:
        return jsonify({'success': False, 'error': '图片不存在'}), 404
    record = None
    if img.get('record_pk'):
        record = LoadingOrderRecord.get_by_id(img['record_pk'])
    if not img.get('human_verified'):
        return jsonify({'success': False, 'error': '仅当 human_verified=1 后才能生成提示词'}), 400

    # 自动分类
    cls = classify_record(product_name=(record or {}).get('product_name', ''),
                          specification=(record or {}).get('specification', ''))
    suggestion_text = _build_suggestion_text(img, record)
    # 查同品类/同规格已有的自定义提示词
    pn = (record or {}).get('product_name', '')
    spec = (record or {}).get('specification', '')
    cc = cls['category_code'] if cls else None
    existing = CategoryPrompt.list_for_record(category_code=cc, product_name=pn, specification=spec)
    return jsonify({
        'success': True,
        'suggestion': {
            'prompt_text': suggestion_text,
            'scope': 'category',
            'category_code': cc,
            'product_name_keyword': pn or None,
            'spec_pattern': '',
            'source_event_id': img.get('_source_event_id'),
            'source_ocr_text': (img.get('ocr_text') or '')[:200],
            'source_ai_status': img.get('match_status') or '',
            'source_human_status': 'green',
            'existing_prompts': existing,  # {category_prompts: [...], spec_prompts: [...]}
        }
    })


@bp.route('/api/v1/loading-orders/images/<int:image_id>/ocr-detail', methods=['GET'])
def api_v1_loading_orders_image_ocr_detail(image_id):
    """图片 AI 比对详情:返回 OCR 原文 + 提示词 + 推理结果(给前端「详情」按钮用)。

    Returns:
      {
        'success': True,
        'detail': {
          'image_id', 'record_id', 'order_id',
          'product_name', 'specification',
          'match_status', 'match_reason',
          'human_verified',
          'ocr':  {ocr_text, ocr_engine, ai_match_status, ai_match_reason, created_at} | None,
          'ai':   {prompt_payload, ai_raw_response, ai_match_reason, ai_match_status,
                    ai_engine, prompt_version, created_at} | None,
          'human':{human_status, human_reason, operator_name, created_at} | None,
        }
      }

    - ocr 段取自最新 record_ocr 事件(同 image_id);
    - ai 段取自最新 ai_match 事件(同 record_id, 跨 image 共享);DeepSeek 调用过才有
    - human 段取自最新 human_verify 事件(同 image_id);有人点过才有
    """
    img = LoadingOrderImage.get_by_id(image_id)
    if not img:
        return jsonify({'success': False, 'error': '图片不存在'}), 404
    record = None
    if img.get('record_pk'):
        record = LoadingOrderRecord.get_by_id(img['record_pk'])

    def _latest_event(event_type: str, image_id_arg=None, record_id_arg=None):
        """取某类型下对应 image/record 的最新事件(没事件返回 None)。"""
        from models import OcrMatchEvent
        if image_id_arg is not None:
            return OcrMatchEvent.get_latest_by_image(image_id_arg, event_type=event_type)
        if record_id_arg is not None:
            return OcrMatchEvent.get_latest_by_record(record_id_arg, event_type=event_type)
        return None

    ocr_event = _latest_event('record_ocr', image_id_arg=image_id)
    ai_event = _latest_event('ai_match', record_id_arg=img.get('record_pk'))
    human_event = _latest_event('human_verify', image_id_arg=image_id)

    def _staff_name(staff_id):
        if not staff_id:
            return None
        conn = get_db()
        cur = conn.cursor()
        cur.execute('SELECT name FROM staff WHERE id=?', (staff_id,))
        row = cur.fetchone()
        conn.close()
        return row['name'] if row else None

    return jsonify({
        'success': True,
        'detail': {
            'image_id': image_id,
            'record_id': img.get('record_pk'),
            'order_id': img.get('order_pk'),
            'product_name': (record or {}).get('product_name', '') or '',
            'specification': (record or {}).get('specification', '') or '',
            'match_status': img.get('match_status') or '',
            'match_reason': img.get('reason') or '',
            'human_verified': bool(img.get('human_verified')),
            # 2026-07-30 UX 一致性:与 refreshRowMatchBadge 升级逻辑对齐
            #   - yellow/red + human_verified=1 → green (人工覆盖)
            #   - 其他情况 → 透传 match_status
            'effective_match_status': (
                'green'
                if img.get('human_verified') and img.get('match_status') in ('yellow', 'red')
                else (img.get('match_status') or '')
            ),
            'ocr': (dict(ocr_event) if ocr_event else None),
            'ai': (dict(ai_event) if ai_event else None),
            'human': ({
                **dict(human_event),
                'operator_name': _staff_name(human_event.get('human_verified_by')),
            } if human_event else None),
        }
    })


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


# ── 移动端（参照 mobile_shipping 的 /m/shipping-today 布局）──────────────
_BOARD_KEYWORDS = ("皮革", "木板")


def _is_loading_board(product_name: str) -> bool:
    """是否为皮革/木板类商品(无需拍照,按"块"计数)。"""
    return any(k in (product_name or "") for k in _BOARD_KEYWORDS)


def _latest_loading_status(images) -> "str | None":
    """取最新一张图片的状态(get_by_record 已按 sort_order ASC, id ASC 排,末尾为最新)。无图返回 None。"""
    if not images:
        return None
    last = images[-1]
    return last.get("match_status") or "green"


def _summarize_loading_group(group: dict) -> dict:
    records = group.get("records", [])
    total = len(records)
    has_image = 0
    stats = {"green": 0, "yellow": 0, "red": 0}
    for r in records:
        imgs = LoadingOrderImage.get_by_record(r["id"])
        if imgs:
            has_image += 1
            # 取最新一张图的状态(与详情页一致:新拍覆盖旧拍)
            st = _latest_loading_status(imgs)
            if st in stats:
                stats[st] += 1
    return {**group, "total": total, "has_image": has_image, "stats": stats}


@bp.route("/m/loading")
def mobile_loading():
    """移动端装柜今日列表(布局参照 /m/shipping-today)。"""
    today = date.today().isoformat()
    groups = LoadingOrderRecord.get_grouped(today, today)
    groups = [_summarize_loading_group(g) for g in groups]
    return render_template("mobile/loading-today.html", today=today, groups=groups)


@bp.route("/m/loading/order/<int:oid>")
def mobile_loading_order(oid: int):
    """移动端装柜订单详情(拍照/识别/详情,布局参照 /m/shipping-today/order/<oid>)。"""
    set_log_context(biz="mobile_loading", order_id=oid)
    order = LoadingOrder.get_by_id(oid)
    if not order:
        abort(404)
    records = LoadingOrderRecord.get_by_order(oid)
    record_states = {}
    for r in records:
        imgs = LoadingOrderImage.get_by_record(r["id"])
        st = _latest_loading_status(imgs) if imgs else None
        record_states[r["id"]] = {
            "status": st,
            "images": imgs,
            "is_board": _is_loading_board(r["product_name"]),
            "color_note": None,
        }
    # 订单级共享图(record_pk IS NULL)
    order_images = [i for i in LoadingOrderImage.get_by_order(oid) if i.get("record_pk") is None]
    return render_template(
        "mobile/loading-order.html",
        order=order,
        records=records,
        record_states=record_states,
        order_images=order_images,
    )


@bp.route("/api/v1/loading-orders/images/<int:image_id>/match-status", methods=["GET"])
def api_v1_loading_orders_image_match_status(image_id):
    """查询行级图匹配状态(移动端轮询用,与出货 /api/v1/shipping-orders/... 同语义)。

    装柜图片识别为同步流程(上传后由前端触发 fuzzy-match / ai-judge 写库),
    故本端点 processing 恒为 false,直接返回 DB 最新状态。
    """
    img = LoadingOrderImage.get_by_id(image_id)
    if not img:
        return jsonify({"success": False, "error": "图片不存在"}), 404
    return jsonify({
        "success": True,
        "processing": False,
        "image": {
            "image_id": image_id,
            "match_status": img.get("match_status") or None,
            "match_score": img.get("match_score"),
            "reason": img.get("reason") or "",
            "match_source": img.get("match_source") or "local_fuzzy",
            "bg_color": img.get("bg_color") or None,
            "human_verified": bool(img.get("human_verified")),
        },
    })
