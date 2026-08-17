"""入库记录蓝图。

包含：
- /inbound-records 页面（带日期过滤、商品单位提示计算）
- /api/v1/inbound-orders/* REST API（订单/明细/图片 CRUD + 移动 + AI 识别）
"""
import os
import uuid
import base64
import re
from datetime import date as date_cls, timedelta, datetime
from flask import Blueprint, render_template, request, jsonify, current_app, session
from models import (
    InboundOrder, InboundRecord, InboundImage, ProductUnit, PieceConversion, get_db, AuditLog,
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
from blueprints.ocr_log import set_log_context

bp = Blueprint('inbound', __name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _get_upload_dir():
    """薄包装，返回 (upload_dir, month_str)。"""
    return get_helpers_upload_dir()


def _as_bool(val, default=False):
    """将 JSON 的 verified 字段转 bool(None → default)。"""
    if val is None:
        return default
    return bool(val)


def _save_one_uploaded_file(file, upload_dir):
    """保存一个上传文件到 upload_dir,返回 (filepath, original_name)。失败抛 ValueError。"""
    if not file or not file.filename:
        raise ValueError('未选择文件')
    ext = check_uploaded_image(file)
    if not ext.startswith('.'):
        ext = '.' + ext
    filename = f"{uuid.uuid4().hex}{ext}"
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
    else: ext = 'jpg'
    img_bytes = base64.b64decode(base64_data)
    filename = f"{uuid.uuid4().hex}.{ext}"
    filepath = os.path.join(upload_dir, filename)
    with open(filepath, 'wb') as f:
        f.write(img_bytes)
    validate_image_content(filepath)
    return filepath, 'pasted_image'


def _run_label_match(image_abspath, record, ocr_text=''):
    """对一张行级图跑本地匹配 (RapidFuzz)，返回 (status, score, reason)。

    本函数只负责【本地 fallback】逻辑(DeepSeek 调用由
    _classify_and_match_image 调度)。调用方先抽 OCR文本,如已抽取则复用避免重跑 PaddleOCR。
    """
    try:
        if not ocr_text:
            with open(image_abspath, 'rb') as f:
                img_bytes = f.read()
            ocr_text = get_ocr_engine('paddleocr').extract_text(img_bytes) or ''
        if not ocr_text.strip():
            return '', None, ''
        status, score, reason = match_label_to_row(
            ocr_text, record.get('product_name', ''), record.get('specification', '')
        )
        return status, score, reason
    except Exception:
        current_app.logger.exception('行级图片本地匹配失败（不阻断上传）')
        return '', None, ''


def _classify_and_match_image(filepath, record, ocr_text):
    """对一张图先 OCR → 优先调 DeepSeek 单 record 比对 → fallback 本地 RapidFuzz。

    Returns:
        {
            'status': 'green' | 'yellow' | 'red' | '',
            'score':  float | None,
            'reason': str,
            'source': 'deepseek' | 'local_fuzzy' | '',
            'prompt_text': str,
            'raw_response': str,
        }
    """
    if not (ocr_text or '').strip():
        return {'status': '', 'score': None, 'reason': '', 'source': '', 'prompt_text': '', 'raw_response': ''}

    try:
        ds = get_ocr_engine('deepseek')
        if getattr(ds, 'API_KEY', ''):
            # ── 2026-08-09:把分类后的补充提示词也喂给单 record 比对 ──
            from models.category_prompt import classify_record, CategoryPrompt
            _cls = classify_record(product_name=record.get('product_name', ''),
                                   specification=record.get('specification', ''))
            _cc = _cls['category_code'] if (_cls and _cls.get('category_code')) else None
            _sup = (CategoryPrompt.compose_for_record(
                category_code=_cc,
                product_name=record.get('product_name', ''),
                specification=record.get('specification', ''),
            ) if _cc else '')
            res = ds.compare_single_record(ocr_text, record, supplement_prompt=_sup)
            ms = (res.get('match_status') or '').lower()
            if ms in ('green', 'yellow', 'red'):
                return {
                    'status': ms,
                    'score': res.get('match_score'),
                    'reason': res.get('reason') or '',
                    'source': 'deepseek',
                    'prompt_text': res.get('prompt_text', ''),
                    'raw_response': res.get('raw_response', ''),
                }
    except Exception:
        current_app.logger.exception('DeepSeek 单 record 比对异常，退到本地匹配')

    status, score, reason = _run_label_match(filepath, record, ocr_text=ocr_text)
    return {'status': status or '', 'score': score, 'reason': reason or '', 'source': 'local_fuzzy', 'prompt_text': '', 'raw_response': ''}


def _build_suggestion_text(img: dict, record: dict | None) -> str:
    """模板化生成:基于 OCR 原文 + AI/人裁决,产出可供用户编辑的"补充提示词"草稿。"""
    ai = img.get('match_status') or '?'
    ai_reason = img.get('reason') or ''
    pn = (record or {}).get('product_name', '') or ''
    sp = (record or {}).get('specification', '') or ''
    return (
        f'[{ai}→人工确认] 商品「{pn}」规格「{sp}」OCR标签此前判 {ai},'
        f'原因为「{ai_reason[:120]}」,但人工复核与录入明细一致。'
        f'同类案例在后续比对中,即使 OCR 文本中只出现部分关键词(如 7P / 加面 / 厚度)'
        f'也视为该规则命中,判 green。'
    )


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
            # 数量异常标记:空 或 非数字
            qty_raw = (item.get('quantity') or '').strip()
            try:
                float(qty_raw)
                item['qty_invalid'] = False
            except (TypeError, ValueError):
                item['qty_invalid'] = True
            # 已核查警告 (per-rule verified_warnings)
            item['verified_warnings'] = InboundRecord.get_verified_warnings(item['id'])
        group.update(summarize_remarks(group['records']))
        group['has_eco'] = any('环保' in r.get('product_name', '') for r in group['records'])
        group['has_jia_mian'] = any(
            '杂胶' in r.get('product_name', '')
            and '加面' in r.get('specification', '')
            for r in group['records']
        )

    # 2026-08-04:match-col 服务端不再渲染,改由 JS `_ensureMatchColumn`
    # 在首次有匹配结果时动态插入。所以这里不再算 record_worst_*_map /
    # has_match —— 那块逻辑下沉到 JS。
    for grp in groups:
        for rec in grp.get('records', []):
            record_imgs = [img for img in grp.get('images', []) if img.get('record_pk') == rec['id']]
            rec['has_image'] = bool(record_imgs)

    record_by_pk = {}
    for grp in groups:
        for r in grp.get('records', []):
            record_by_pk[r['id']] = r

    return render_template(
        'inbound-records.html',
        groups=groups,
        page_title='入库记录',
        today=today,
        start_date=start_date,
        end_date=end_date,
        unit_list=unit_list,
        piece_conversions=piece_conv_list,
        record_by_pk=record_by_pk,
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

    if 'img_cols' in data:
        try:
            cols = int(data['img_cols'])
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': '列数必须是整数'}), 400
        if cols < 1 or cols > 5:
            return jsonify({'success': False, 'error': '列数范围为1-5'}), 400
        InboundOrder.set_img_cols(order_id, cols)
        return jsonify({'success': True, 'img_cols': cols})

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
    # 联动清理行级图片
    InboundImage.delete_by_record(record_id)
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
    set_log_context(biz='inbound', evt_src='ai_recognize')
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
    """上传订单级图片 → 201"""
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
    _VALID_SOURCE_TAGS = {'打板照', '装车照', '归仓照'}
    source_tag = request.form.get('source_tag')
    if source_tag not in _VALID_SOURCE_TAGS:
        source_tag = None
    image_id = InboundImage.create(order_id, filepath, file.filename, source, source_tag=source_tag)
    relative_path = f"{date_str}/{filename}"
    AuditLog.log('upload_image', 'inbound_order', order_id, detail={'filename': filename, 'source': source, 'source_tag': source_tag})
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


# ── 行级图片匹配 API（复制出货模式）─────────────────────────────────


@bp.route('/api/v1/inbound-orders/images/<int:image_id>/manual-verify', methods=['POST'])
def api_v1_inbound_orders_manual_verify_image(image_id):
    """人工覆盖 AI 比对结果(把红牌标记为已确认)→ 200

    Body (可选): {"verified": true|false}，默认 true。
    副作用:写 inbound_images.human_verified + 写审计日志。
    """
    img = InboundImage.get_by_id(image_id)
    if not img:
        return jsonify({'success': False, 'error': '图片不存在'}), 404
    order = InboundOrder.get_by_id(img['order_pk'])
    if order and order.get('is_locked'):
        return jsonify({'success': False, 'error': '订单已锁定，无法操作'}), 403
    data = request.get_json(silent=True) or {}
    verified = _as_bool(data.get('verified'), default=True)

    try:
        ai_snapshot = InboundImage.get_by_id(image_id)
        ai_status = ai_snapshot.get('match_status') if ai_snapshot else None
        ai_reason = ai_snapshot.get('reason') if ai_snapshot else None
        OcrMatchEvent.create(
            'human_verify',
            record_id=img.get('record_pk'),
            order_id=img['order_pk'],
            image_id=image_id,
            ai_match_status=ai_status,
            ai_match_reason=ai_reason,
            human_status='green' if verified else None,
            human_verified_by=session.get('operator_id'),
        )
    except Exception:
        current_app.logger.exception('human_verify 事件写库失败(不阻断)')

    InboundImage.set_human_verified(image_id, verified)
    AuditLog.log(
        'manual_verify_image', 'inbound_order', img['order_pk'],
        detail={'image_id': image_id, 'human_verified': 1 if verified else 0},
    )
    return jsonify({'success': True, 'image_id': image_id, 'human_verified': verified})


@bp.route('/api/v1/inbound-orders/images/<int:image_id>/fuzzy-match', methods=['POST'])
def api_v1_inbound_orders_fuzzy_match_image(image_id):
    """对已上传图片重新运行本地 RapidFuzz 模糊匹配 → 返回新的判别结果。

    流程: 取图→取关联商品行→取OCR文字→运行 match_label_to_row→更新 inbound_images→返回
    """
    img = InboundImage.get_by_id(image_id)
    if not img:
        return jsonify({'success': False, 'error': '图片不存在'}), 404
    order = InboundOrder.get_by_id(img['order_pk'])
    if order and order.get('is_locked'):
        return jsonify({'success': False, 'error': '订单已锁定，无法操作'}), 403
    record_pk = img.get('record_pk')
    if not record_pk:
        return jsonify({'success': False, 'error': '该图片未关联商品行'}), 400
    record = InboundRecord.get_by_id(record_pk)
    if not record:
        return jsonify({'success': False, 'error': '关联商品行不存在'}), 404

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

    status, score, reason = match_label_to_row(
        ocr_text, record.get('product_name', ''), record.get('specification', ''))

    InboundImage.set_match(image_id, status, score or 0, reason, source='local_fuzzy')

    return jsonify({
        'success': True,
        'image_id': image_id,
        'match_status': status,
        'match_score': score,
        'reason': reason,
        'match_source': 'local_fuzzy',
    })


@bp.route('/api/v1/inbound-orders/images/<int:image_id>/ai-judge', methods=['POST'])
def api_v1_inbound_orders_ai_judge_image(image_id):
    """对已上传图片重新运行 DeepSeek AI 比对 → 返回新的判别结果。

    流程: 取图→取关联商品行→取OCR文字→调用 DeepSeek compare_single_record→更新 inbound_images→返回
    """
    set_log_context(biz='inbound', image_id=image_id)
    img = InboundImage.get_by_id(image_id)
    if not img:
        return jsonify({'success': False, 'error': '图片不存在'}), 404
    order = InboundOrder.get_by_id(img['order_pk'])
    if order and order.get('is_locked'):
        return jsonify({'success': False, 'error': '订单已锁定，无法操作'}), 403
    record_pk = img.get('record_pk')
    if not record_pk:
        return jsonify({'success': False, 'error': '该图片未关联商品行'}), 400
    record = InboundRecord.get_by_id(record_pk)
    if not record:
        return jsonify({'success': False, 'error': '关联商品行不存在'}), 404

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

    try:
        ds = get_ocr_engine('deepseek')
        if not getattr(ds, 'API_KEY', ''):
            return jsonify({'success': False, 'error': 'DeepSeek API Key 未配置'}), 503
        # ── 2026-08-09:把分类后的补充提示词喂给 AI 判别 ──
        from models.category_prompt import classify_record, CategoryPrompt
        _cls = classify_record(product_name=record.get('product_name', ''),
                               specification=record.get('specification', ''))
        _cc = _cls['category_code'] if (_cls and _cls.get('category_code')) else None
        _sup = (CategoryPrompt.compose_for_record(
            category_code=_cc,
            product_name=record.get('product_name', ''),
            specification=record.get('specification', ''),
        ) if _cc else '')
        res = ds.compare_single_record(ocr_text, record, supplement_prompt=_sup)
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

    InboundImage.set_match(image_id, status, score or 0, reason, source=source_label)

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


@bp.route('/api/v1/inbound-orders/images/<int:image_id>/ocr-detail', methods=['GET'])
def api_v1_inbound_orders_image_ocr_detail(image_id):
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
    """
    img = InboundImage.get_by_id(image_id)
    if not img:
        return jsonify({'success': False, 'error': '图片不存在'}), 404
    record = None
    if img.get('record_pk'):
        record = InboundRecord.get_by_id(img['record_pk'])

    def _latest_event(event_type: str, image_id_arg=None, record_id_arg=None):
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


@bp.route('/api/v1/inbound-orders/images/<int:image_id>/generate-prompt-suggestion', methods=['POST'])
def api_v1_inbound_orders_generate_prompt_suggestion(image_id):
    """生成提示词草稿:调人工确认"✓ 确认通过"后,图下方的按钮触发。"""
    img = InboundImage.get_by_id(image_id)
    if not img:
        return jsonify({'success': False, 'error': '图片不存在'}), 404
    record = None
    if img.get('record_pk'):
        record = InboundRecord.get_by_id(img['record_pk'])
    if not img.get('human_verified'):
        return jsonify({'success': False, 'error': '仅当 human_verified=1 后才能生成提示词'}), 400

    cls = classify_record(product_name=(record or {}).get('product_name', ''),
                          specification=(record or {}).get('specification', ''))
    suggestion_text = _build_suggestion_text(img, record)
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
            'existing_prompts': existing,
        }
    })


# ── 商品行级图片(挂在 record 上,合并订单级共享图展示) ──


@bp.route('/api/v1/inbound-orders/records/<int:record_id>/images', methods=['POST'])
def api_v1_inbound_orders_record_upload_images(record_id):
    """上传商品行图片(多文件),按上传顺序赋 sort_order → 201"""
    set_log_context(biz='inbound', record_id=record_id)
    record = InboundRecord.get_by_id(record_id)
    if not record:
        return jsonify({'success': False, 'error': '记录不存在'}), 404
    order = InboundOrder.get_by_id(record['order_pk'])
    if order and order.get('is_locked'):
        return jsonify({'success': False, 'error': '该订单已锁定，无法上传图片'}), 403

    upload_dir, month_str = _get_upload_dir()
    source = 'upload'
    saved = []

    _VALID_SOURCE_TAGS = {'打板照', '装车照', '归仓照'}
    source_tag = request.form.get('source_tag')
    if source_tag not in _VALID_SOURCE_TAGS:
        source_tag = None

    files = request.files.getlist('image')
    if files:
        for f in files:
            try:
                filepath, original_name = _save_one_uploaded_file(f, upload_dir)
            except ValueError as e:
                return jsonify({'success': False, 'error': str(e)}), 400
            image_id = InboundImage.create(
                order_pk=record['order_pk'],
                file_path=filepath,
                original_name=original_name,
                source=source,
                record_pk=record_id,
                source_tag=source_tag,
            )
            rel_path = os.path.join(month_str, os.path.basename(filepath)).replace('\\', '/')
            img = InboundImage.get_by_id(image_id)
            ocr_text = ''
            try:
                apply_wrinkle_enhance = is_wrinkle_label_category(record.get('product_name', ''))
                with open(filepath, 'rb') as _f:
                    ocr_text = get_ocr_engine('paddleocr').extract_text(_f.read(), apply_wrinkle_enhance=apply_wrinkle_enhance) or ''
            except Exception:
                current_app.logger.exception('记录 OCR 文本失败(不阻断): %s', filepath)
            bg = detect_bg_color(filepath)
            if bg and ocr_text.strip():
                ocr_text = ocr_text + '\n[标签背景: ' + ('黑色' if bg == 'black' else '白色') + ']'
            if bg:
                try:
                    InboundImage.set_bg_color(image_id, bg)
                except Exception:
                    pass
            result = _classify_and_match_image(filepath, record, ocr_text)
            status, score, reason, source_label = (
                result['status'], result['score'], result['reason'], result['source']
            )
            if status:
                InboundImage.set_match(image_id, status, score, reason, source=source_label)
            try:
                OcrMatchEvent.create(
                    'record_ocr', record_id=record_id, order_id=record['order_pk'], image_id=image_id,
                    ocr_text=ocr_text,
                    ocr_engine='paddleocr',
                    ai_engine=source_label or 'local_fuzzy',
                    ai_match_status=status or None,
                    ai_match_score=score,
                    ai_match_reason=reason or None,
                    product_name=record.get('product_name', ''),
                    specification=record.get('specification', ''),
                )
            except Exception:
                current_app.logger.exception('record_ocr 事件写库失败(不阻断)')
            if source_label == 'deepseek' and result.get('prompt_text'):
                try:
                    OcrMatchEvent.create(
                        'ai_match',
                        record_id=record_id,
                        order_id=record['order_pk'],
                        image_id=image_id,
                        ocr_text=ocr_text,
                        ocr_engine='paddleocr',
                        prompt_payload=result['prompt_text'],
                        ai_engine='deepseek',
                        ai_match_status=status,
                        ai_match_score=score,
                        ai_match_reason=reason or None,
                        ai_raw_response=result['raw_response'],
                        prompt_version=OCR_MATCH_PROMPT_VERSION,
                        product_name=record.get('product_name', ''),
                        specification=record.get('specification', ''),
                    )
                except Exception:
                    current_app.logger.exception('ai_match 事件写库失败(不阻断)')
            saved.append({
                'image_id': image_id,
                'image': rel_path,
                'original_name': original_name,
                'sort_order': img['sort_order'],
                'match_status': status or None,
                'match_score': score,
                'reason': reason or '',
                'match_source': source_label,
                'bg_color': bg or None,
            })
        for s in saved:
            AuditLog.log('upload_image', 'inbound_order', record['order_pk'],
                         detail={'filename': s['image'], 'source': source, 'record_id': record_id})
        return jsonify({'success': True, 'images': saved, 'count': len(saved)}), 201

    if request.is_json:
        data = request.get_json() or {}
        try:
            filepath, original_name = _save_one_base64_image(data.get('image', ''), upload_dir)
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        source = data.get('source', 'upload')
        _json_source_tag = data.get('source_tag')
        if _json_source_tag not in _VALID_SOURCE_TAGS:
            _json_source_tag = None
        image_id = InboundImage.create(
            order_pk=record['order_pk'],
            file_path=filepath,
            original_name=original_name,
            source=source,
            record_pk=record_id,
            source_tag=_json_source_tag,
        )
        rel_path = os.path.join(month_str, os.path.basename(filepath)).replace('\\', '/')
        img = InboundImage.get_by_id(image_id)
        ocr_text = ''
        try:
            with open(filepath, 'rb') as _f:
                ocr_text = get_ocr_engine('paddleocr').extract_text(_f.read()) or ''
        except Exception:
            current_app.logger.exception('记录 OCR 文本失败(不阻断): %s', filepath)
        bg = detect_bg_color(filepath)
        if bg and ocr_text.strip():
            ocr_text = ocr_text + '\n[标签背景: ' + ('黑色' if bg == 'black' else '白色') + ']'
        if bg:
            try:
                InboundImage.set_bg_color(image_id, bg)
            except Exception:
                pass
        result = _classify_and_match_image(filepath, record, ocr_text)
        status, score, reason, source_label = (
            result['status'], result['score'], result['reason'], result['source']
        )
        if status:
            InboundImage.set_match(image_id, status, score, reason, source=source_label)
        try:
            OcrMatchEvent.create(
                'record_ocr', record_id=record_id, order_id=record['order_pk'], image_id=image_id,
                ocr_text=ocr_text,
                ocr_engine='paddleocr',
                ai_engine=source_label or 'local_fuzzy',
                ai_match_status=status or None,
                ai_match_score=score,
                ai_match_reason=reason or None,
                product_name=record.get('product_name', ''),
                specification=record.get('specification', ''),
            )
        except Exception:
            current_app.logger.exception('record_ocr 事件写库失败(不阻断)')
        if source_label == 'deepseek' and result.get('prompt_text'):
            try:
                OcrMatchEvent.create(
                    'ai_match',
                    record_id=record_id,
                    order_id=record['order_pk'],
                    image_id=image_id,
                    ocr_text=ocr_text,
                    ocr_engine='paddleocr',
                    prompt_payload=result['prompt_text'],
                    ai_engine='deepseek',
                    ai_match_status=status,
                    ai_match_score=score,
                    ai_match_reason=reason or None,
                    ai_raw_response=result['raw_response'],
                    prompt_version=OCR_MATCH_PROMPT_VERSION,
                    product_name=record.get('product_name', ''),
                    specification=record.get('specification', ''),
                )
            except Exception:
                current_app.logger.exception('ai_match 事件写库失败(不阻断)')
        AuditLog.log('upload_image', 'inbound_order', record['order_pk'],
                     detail={'filename': rel_path, 'source': source, 'record_id': record_id})
        return jsonify({'success': True, 'images': [{
            'image_id': image_id, 'image': rel_path,
            'original_name': original_name, 'sort_order': img['sort_order'],
            'match_status': status or None, 'match_score': score,
            'reason': reason or '',
            'match_source': source_label,
            'bg_color': bg or None,
        }], 'count': 1}), 201

    return jsonify({'success': False, 'error': '未提供图片'}), 400


@bp.route('/api/v1/inbound-orders/records/<int:record_id>/images-area', methods=['GET'])
def api_v1_inbound_orders_record_images_area(record_id):
    """某 record 的合并图片区:该 record 专属图 + 订单共享图,统一排序"""
    record = InboundRecord.get_by_id(record_id)
    if not record:
        return jsonify({'success': False, 'error': '记录不存在'}), 404
    items = InboundImage.get_combined_for_record(record['order_pk'], record_id)
    for it in items:
        it['scope'] = 'order' if it.get('record_pk') is None else 'record'
    return jsonify({'success': True, 'images': items, 'count': len(items)})


# ── 移动端（参照 mobile_shipping 的 /m/shipping-today 布局）──────────────
_BOARD_KEYWORDS = ("皮革", "木板")
_COLOR_KEYWORDS = ("黑", "白", "红", "蓝", "绿", "黄", "棕", "灰", "米", "杏", "紫", "粉", "橙")
_BG_CN = {"black": "黑色", "white": "白色"}


def _inbound_is_board(product_name: str) -> bool:
    """是否为皮革/木板类商品(无需拍照,按"块"计数)。"""
    return any(k in (product_name or "") for k in _BOARD_KEYWORDS)


def _inbound_has_color_word(text: str) -> bool:
    return any(c in text for c in _COLOR_KEYWORDS)


def _inbound_color_note(image: dict) -> str | None:
    bg = image.get("bg_color")
    if not bg:
        return None
    evt = OcrMatchEvent.get_latest_by_image(image["id"], "record_ocr")
    ocr_text = (evt or {}).get("ocr_text") or ""
    if _inbound_has_color_word(ocr_text):
        return None
    return _BG_CN.get(bg, bg)


def _inbound_latest_status(images) -> "str | None":
    """取最新一张图片的状态(get_by_record 已按 sort_order ASC, id ASC 排,末尾为最新)。"""
    if not images:
        return None
    last = images[-1]
    return last.get("match_status") or "green"


def _summarize_inbound_group(group: dict) -> dict:
    records = group.get("records", [])
    regular = [r for r in records if not _inbound_is_board(r["product_name"])]
    boards = [r for r in records if _inbound_is_board(r["product_name"])]
    total = len(regular)
    board_total = len(boards)
    has_image = 0
    stats = {"green": 0, "yellow": 0, "red": 0}
    for r in regular:
        images = InboundImage.get_by_record(r["id"])
        if images:
            has_image += 1
            st = _inbound_latest_status(images)
            if st in stats:
                stats[st] += 1
    return {**group, "total": total, "board_total": board_total, "has_image": has_image, "stats": stats}


@bp.route('/m/inbound')
def mobile_inbound():
    """移动端入库今日列表(布局参照 /m/shipping-today)。"""
    today = date_cls.today().isoformat()
    groups = InboundRecord.get_groups(today, today)
    groups = [_summarize_inbound_group(g) for g in groups]
    return render_template('mobile/inbound-today.html', today=today, groups=groups)


@bp.route('/m/inbound/order/<int:oid>')
def mobile_inbound_order(oid: int):
    """移动端入库订单详情(拍照/识别/详情,布局参照 /m/shipping-today/order/<oid>)。"""
    set_log_context(biz='mobile_inbound', order_id=oid)
    order = InboundOrder.get_by_id(oid)
    if order is None:
        from flask import abort
        abort(404)
    groups = InboundRecord.get_groups(order["date"], order["date"])
    records = next(
        (group.get("records", []) for group in groups if group.get("id") == oid),
        [],
    )

    def _with_rel(imgs):
        return [{**img, "rel_path": InboundImage.get_relative_path(img["file_path"])} for img in imgs]
    record_images = {
        rec["id"]: _with_rel(InboundImage.get_by_record(rec["id"]))
        for rec in records
    }
    order_images = _with_rel(InboundImage.get_by_order(oid))
    record_states = {}
    for rec in records:
        imgs = record_images[rec["id"]]
        record_states[rec["id"]] = {
            "images": imgs,
            "status": _inbound_latest_status(imgs),
            "color_note": _inbound_color_note(imgs[-1]) if imgs else None,
            "is_board": _inbound_is_board(rec["product_name"]),
        }
    # 整体图缩略图初始状态(按 source_tag)
    from datetime import datetime as _dt
    thumb_states = {}
    for tag in ("打板照", "装车照", "归仓照"):
        match = [img for img in order_images if img.get("source_tag") == tag]
        if match:
            last = match[-1]
            try:
                t = _dt.strptime(last["created_at"], "%Y-%m-%d %H:%M:%S")
                ts = f"{t.hour:02d}:{t.minute:02d}"
            except Exception:
                ts = ""
            thumb_states[tag] = {"taken": True, "time_text": ts}
        else:
            thumb_states[tag] = {"taken": False, "time_text": ""}
    return render_template(
        "mobile/inbound-order.html",
        order=order,
        records=records,
        record_states=record_states,
        order_images=order_images,
        thumb_states=thumb_states,
    )


@bp.route('/api/v1/inbound-orders/images/<int:image_id>/match-status', methods=['GET'])
def api_v1_inbound_orders_image_match_status(image_id):
    """查询行级图 OCR+AI 比对结果(移动端上传后轮询)。入库上传为同步处理,上传即出结果。"""
    img = InboundImage.get_by_id(image_id)
    if not img:
        return jsonify({'success': False, 'error': '图片不存在'}), 404
    return jsonify({
        'success': True,
        'processing': False,
        'image': {
            'image_id': image_id,
            'match_status': img.get('match_status') or None,
            'match_score': img.get('match_score'),
            'reason': img.get('reason') or '',
            'match_source': img.get('match_source') or 'local_fuzzy',
            'bg_color': img.get('bg_color') or None,
            'human_verified': bool(img.get('human_verified')),
        },
    })
