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
    InboundOrder, InboundRecord, InboundImage, InboundPlacementImage, ProductUnit, PieceConversion, get_db, AuditLog,
    OcrMatchEvent,
    classify_record, CategoryPrompt,
)
from blueprints._helpers import (
    get_upload_dir as get_helpers_upload_dir,
    get_ypp, calc_hint, check_remark, summarize_remarks,
    get_piece_conversion, calc_piece_quantity, check_piece_mismatch,
    validate_image_content, check_uploaded_image,
    match_label_to_row, detect_bg_color,
    apply_user_rotation,
    compute_placement_expected_zhi,
)
from blueprints.ocr_engine import PaddleOCREngine, get_ocr_engine, OCR_MATCH_PROMPT_VERSION, ocr_preprocess_kind
from blueprints.ocr_pipeline import RecordImageProcessor
from blueprints.ocr_log import set_log_context

bp = Blueprint('inbound', __name__)

# 2026-08-26: align shipping placement - sanma regex
SANMA_RE = re.compile(r'(?<![\\d.])(\\d+(?:\\.\\d+)?)\\s*[yY]')

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _get_upload_dir():
    """薄包装，返回 (upload_dir, month_str)。"""
    return get_helpers_upload_dir()


# 行级图 OCR pipeline 共用处理器(入库专用,显式注入 InboundImage)
# 替代旧的 _classify_and_match_image / _run_label_match(2026-08-19 抽取到 ocr_pipeline)。
# ocr_engine_getter 用 lambda 包裹让 monkeypatch 生效。
inbound_processor = RecordImageProcessor(
    InboundImage, ocr_engine_getter=lambda name: get_ocr_engine(name))


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
    total_orders = InboundOrder.count_by_date(today)

    # 2026-08-26:placement images collection (per record)
    placement_by_record = {}
    for grp in groups:
        for rec in grp.get('records', []):
            pimgs = InboundPlacementImage.get_by_record(rec['id'])
            if pimgs:
                placement_by_record[rec['id']] = pimgs

    def _eff_zhi(p):
        mc = p.get('manual_count')
        base = mc if mc is not None else (p.get('n_marks') or 0)
        return -base if p.get('is_unload') else base

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

            # 2026-08-26:placement_match (align shipping semantics)
            _pimgs = placement_by_record.get(item['id']) or []
            _pm = False
            if _pimgs:
                _remark = item.get('remark') or ''
                _total = sum(_eff_zhi(p) for p in _pimgs)
                _loose = sum(p.get('loose_count', 0) for p in _pimgs)
                # 2026-09-03:placement 期望值兜底 — unit='支' + 备注无支数 → 用 quantity
                _exp_zhi, _has_zhi = compute_placement_expected_zhi(
                    _remark, item.get('quantity') or '', item.get('unit') or ''
                )
                _san_m = list(SANMA_RE.finditer(_remark))
                _exp_san = sum(float(x.group(1)) for x in _san_m)
                if _exp_san == int(_exp_san):
                    _exp_san = int(_exp_san)
                _has_san = len(_san_m) > 0
                _matched_zhi = (not _has_zhi) or (abs(_total - _exp_zhi) <= 0.01)
                _matched_san = (not _has_san) or (abs(_loose - _exp_san) <= 0.01)
                if (_has_zhi or _has_san) and _matched_zhi and _matched_san:
                    _pm = True
            item['placement_match'] = _pm
        group.update(summarize_remarks(group['records']))
        group['has_eco'] = any('环保' in r.get('product_name', '') for r in group['records'])
        group['has_jia_mian'] = any(
            '杂胶' in r.get('product_name', '')
            and '加面' in r.get('specification', '')
            for r in group['records']
        )


    # 2026-08-26:placement_groups (group by product_name, same product specs rendered together)
    placement_groups = {}
    for grp in groups:
        group_list = []
        current = None
        for rec in grp.get('records', []):
            pimgs = placement_by_record.get(rec['id']) or []
            if not pimgs:
                continue
            pn = (rec.get('product_name') or '').strip()
            if current is None or current['product_name'] != pn:
                current = {'product_name': pn, 'items': []}
                group_list.append(current)
            loose_total = sum(p.get('loose_count', 0) for p in pimgs)
            remark = rec.get('remark') or ''
            # 2026-09-03:placement 期望值兜底 — unit='支' + 备注无支数 → 用 quantity
            expected_zhi, has_zhi = compute_placement_expected_zhi(
                remark, rec.get('quantity') or '', rec.get('unit') or ''
            )
            san_m = list(SANMA_RE.finditer(remark))
            expected_sanma = sum(float(x.group(1)) for x in san_m)
            if expected_sanma == int(expected_sanma):
                expected_sanma = int(expected_sanma)
            has_sanma = len(san_m) > 0
            current['items'].append({
                'record_id': rec['id'],
                'specification': rec.get('specification') or '',
                'quantity': rec.get('quantity') or '',
                'unit': rec.get('unit') or '',
                'total': sum(_eff_zhi(p) for p in pimgs),
                'loose_total': loose_total,
                'remark': remark,
                'expected_zhi': expected_zhi,
                'has_zhi': has_zhi,
                'expected_sanma': expected_sanma,
                'has_sanma': has_sanma,
                'images': pimgs,
            })
        if group_list:
            placement_groups[grp['id']] = group_list
    # 2026-08-04:match-col 服务端不再渲染,改由 JS `_ensureMatchColumn`
    # 在首次有匹配结果时动态插入。所以这里不再算 record_worst_*_map /
    # has_match —— 那块逻辑下沉到 JS。
    # 2026-09-15:与出货页对齐 —— group.images 排除 source='placement',
    # 让模板主图区只展示普通商品图(upload/copy_paper_label/ai);
    # placement 图只走下方 .placement-area 单独展示,不再重复出现在主图区。
    for grp in groups:
        grp['images'] = [
            img for img in grp.get('images', [])
            if img.get('source') != 'placement'
        ]
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
        total_orders=total_orders,
        start_date=start_date,
        end_date=end_date,
        unit_list=unit_list,
        piece_conversions=piece_conv_list,
        record_by_pk=record_by_pk,
        placement_groups=placement_groups,
        placement_by_record=placement_by_record,
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



# 2026-08-26:inbound placement image API (align shipping / loading)
# Source='placement' marks an image as 'do not OCR / AI match, only manual click counting'.
# The placement_marks table is inbound_placement_marks (FK to inbound_images) to avoid FK conflicts.

def _detect_cylinder_circles(image_path):
    """Detect cylinder circles in placement image (OpenCV HoughCircles). Same heuristic as shipping."""
    try:
        import cv2
        import math
        img = cv2.imread(image_path)
        if img is None:
            return []
        h, w = img.shape[:2]
        scale = min(1.0, 1000.0 / max(w, h))
        small = cv2.resize(img, None, fx=scale, fy=scale) if scale < 1 else img
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        gray = cv2.medianBlur(gray, 5)
        min_r = max(6, int(min(small.shape[:2]) * 0.02))
        max_r = int(max(small.shape[:2]) * 0.48)
        found = []
        for p2 in (18, 28, 42):
            circles = cv2.HoughCircles(
                gray, cv2.HOUGH_GRADIENT, dp=1.2,
                minDist=int(min_r * 1.6), param1=50, param2=p2,
                minRadius=min_r, maxRadius=max_r,
            )
            if circles is None:
                continue
            for c in circles[0]:
                found.append((float(c[0]), float(c[1]), float(c[2])))
        uniq = []
        for (cx, cy, r) in found:
            dup = False
            for (ox, oy, or_) in uniq:
                if math.hypot(cx - ox, cy - oy) < (r + or_) * 0.5:
                    dup = True
                    break
            if not dup:
                uniq.append((cx, cy, r))
        result = []
        for (cx, cy, r) in uniq:
            result.append({
                'x': round(cx / (w * scale), 4),
                'y': round(cy / (h * scale), 4),
                'rx': round(r / (w * scale), 4),
                'ry': round(r / (h * scale), 4),
            })
        return result
    except Exception:
        current_app.logger.exception('cylinder detect failed: %s', image_path)
        return []
# 2026-08-26:inbound placement image API (align shipping / loading)
# Source='placement' marks an image as 'do not OCR / AI match, only manual click counting'.
# The placement_marks table is inbound_placement_marks (FK to inbound_images).

def _inbound_placement_match_for_image(image_id):
    """Compute whether the record's placement count matches remark expectation."""
    img = InboundPlacementImage.get_by_id(image_id)
    if not img:
        return False, None
    record_pk = img.get('record_pk')
    if not record_pk:
        return False, None
    pimgs = InboundPlacementImage.get_by_record(record_pk)
    if not pimgs:
        return False, record_pk
    rec = InboundRecord.get_by_id(record_pk) or {}
    remark = rec.get('remark') or ''
    total = sum((((p.get('manual_count') if p.get('manual_count') is not None else (p.get('n_marks') or 0)))
                 * (-1 if p.get('is_unload') else 1)) for p in pimgs)
    loose = sum(p.get('loose_count', 0) for p in pimgs)
    # 2026-09-03:placement 期望值兜底 — unit='支' + 备注无支数 → 用 quantity
    exp_zhi, has_zhi = compute_placement_expected_zhi(
        remark, rec.get('quantity') or '', rec.get('unit') or ''
    )
    san_m = list(SANMA_RE.finditer(remark))
    exp_san = sum(float(x.group(1)) for x in san_m)
    if exp_san == int(exp_san):
        exp_san = int(exp_san)
    has_san = len(san_m) > 0
    matched_zhi = (not has_zhi) or (abs(total - exp_zhi) <= 0.01)
    matched_san = (not has_san) or (abs(loose - exp_san) <= 0.01)
    return bool((has_zhi or has_san) and matched_zhi and matched_san), record_pk


def _save_one_placement_upload(file, upload_dir):
    """Save an uploaded file to upload_dir with placement-friendly filename. Same logic as shipping."""
    import uuid as _uuid
    safe_name = re.sub(r'[^A-Za-z0-9._-]', '_', os.path.basename(file.filename or 'image'))
    filename = f"inb_placement_{datetime.now().strftime('%Y%m%d%H%M%S')}_{_uuid.uuid4().hex[:6]}_{safe_name}"
    filepath = os.path.join(upload_dir, filename)
    file.save(filepath)
    try:
        validate_image_content(filepath)
    except ValueError as e:
        try:
            os.remove(filepath)
        except OSError:
            pass
        raise ValueError(str(e))
    return filepath, file.filename or filename


@bp.route('/api/v1/inbound-orders/records/<int:record_id>/placement-images', methods=['POST'])
def api_v1_inbound_orders_record_placement_upload(record_id):
    set_log_context(biz='inbound', record_id=record_id)
    record = InboundRecord.get_by_id(record_id)
    if not record:
        return jsonify({'success': False, 'error': 'record not found'}), 404
    order = InboundOrder.get_by_id(record['order_pk'])
    if order and order.get('is_locked'):
        return jsonify({'success': False, 'error': 'order locked'}), 403
    upload_dir, month_str = _get_upload_dir()
    saved = []
    files = request.files.getlist('image')
    if files:
        for f in files:
            try:
                filepath, original_name = _save_one_placement_upload(f, upload_dir)
            except ValueError as e:
                return jsonify({'success': False, 'error': str(e)}), 400
            rotate_deg = request.form.get('rotate_deg')
            try:
                filepath = apply_user_rotation(filepath, rotate_deg)
            except ValueError as e:
                return jsonify({'success': False, 'error': str(e)}), 400
            image_id = InboundPlacementImage.create(
                order_pk=record['order_pk'], record_pk=record_id,
                file_path=filepath, original_name=original_name,
            )
            rel_path = os.path.join(month_str, os.path.basename(filepath)).replace('\\', '/')
            img = InboundPlacementImage.get_by_id(image_id)
            saved.append({
                'image_id': image_id, 'image': rel_path, 'original_name': original_name,
                'sort_order': img['sort_order'], 'circles': [],
            })
    if not saved:
        return jsonify({'success': False, 'error': 'no image provided'}), 400
    for s in saved:
        AuditLog.log('upload_placement_image', 'inbound_order', record['order_pk'],
                     detail={'filename': s['image'], 'record_id': record_id})
    return jsonify({'success': True, 'images': saved, 'count': len(saved)}), 201


@bp.route('/api/v1/inbound-orders/records/<int:record_id>/placement-images', methods=['GET'])
def api_v1_inbound_orders_record_placement_list(record_id):
    images = InboundPlacementImage.get_by_record(record_id)
    return jsonify({'success': True, 'images': images})


@bp.route('/api/v1/inbound-orders/placement-images/<int:image_id>', methods=['DELETE'])
def api_v1_inbound_orders_placement_delete(image_id):
    img = InboundPlacementImage.get_by_id(image_id)
    if not img:
        return jsonify({'success': False, 'error': 'placement image not found'}), 404
    order = InboundOrder.get_by_id(img['order_pk'])
    if order and order.get('is_locked'):
        return jsonify({'success': False, 'error': 'order locked'}), 403
    InboundPlacementImage.delete(image_id)
    return jsonify({'success': True})


@bp.route('/api/v1/inbound-orders/placement-images/<int:image_id>', methods=['GET'])
def api_v1_inbound_orders_placement_get(image_id):
    img = InboundPlacementImage.get_by_id(image_id)
    if not img:
        return jsonify({'success': False, 'error': 'placement image not found'}), 404
    return jsonify({'success': True, 'image': img})


@bp.route('/api/v1/inbound-orders/placement-images/<int:image_id>/detect', methods=['POST'])
def api_v1_inbound_orders_placement_detect(image_id):
    img = InboundPlacementImage.get_by_id(image_id)
    if not img:
        return jsonify({'success': False, 'error': 'placement image not found'}), 404
    circles = _detect_cylinder_circles(img['file_path'])
    InboundPlacementImage.set_circles(image_id, circles)
    return jsonify({'success': True, 'circles': circles})


@bp.route('/api/v1/inbound-orders/placement-images/<int:image_id>/mark-scale', methods=['POST'])
def api_v1_inbound_orders_placement_mark_scale(image_id):
    img = InboundPlacementImage.get_by_id(image_id)
    if not img:
        return jsonify({'success': False, 'error': 'placement image not found'}), 404
    data = request.get_json() or {}
    try:
        scale = float(data.get('scale', 1))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'scale must be number'}), 400
    scale = InboundPlacementImage.set_mark_scale(image_id, scale)
    return jsonify({'success': True, 'scale': scale, 'marks': InboundPlacementImage.get_marks(image_id)})


@bp.route('/api/v1/inbound-orders/placement-images/<int:image_id>/loose-count', methods=['POST'])
def api_v1_inbound_orders_placement_loose_count(image_id):
    img = InboundPlacementImage.get_by_id(image_id)
    if not img:
        return jsonify({'success': False, 'error': 'placement image not found'}), 404
    data = request.get_json() or {}
    count = InboundPlacementImage.set_loose_count(image_id, data.get('count', 0))
    pm, record_id = _inbound_placement_match_for_image(image_id)
    return jsonify({'success': True, 'loose_count': count, 'placement_match': pm, 'record_id': record_id})


@bp.route('/api/v1/inbound-orders/placement-images/<int:image_id>/manual-count', methods=['POST'])
def api_v1_inbound_orders_placement_manual_count(image_id):
    img = InboundPlacementImage.get_by_id(image_id)
    if not img:
        return jsonify({'success': False, 'error': 'placement image not found'}), 404
    data = request.get_json() or {}
    raw = data.get('count')
    if raw is None or raw == '':
        count = None
    else:
        try:
            count = int(round(float(raw)))
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'count must be number'}), 400
    count = InboundPlacementImage.set_manual_count(image_id, count)
    pm, record_id = _inbound_placement_match_for_image(image_id)
    return jsonify({'success': True, 'manual_count': count, 'placement_match': pm, 'record_id': record_id})


@bp.route('/api/v1/inbound-orders/placement-images/<int:image_id>/unload', methods=['POST'])
def api_v1_inbound_orders_placement_unload(image_id):
    img = InboundPlacementImage.get_by_id(image_id)
    if not img:
        return jsonify({'success': False, 'error': 'placement image not found'}), 404
    data = request.get_json() or {}
    raw = data.get('unload')
    unload = bool(raw) if raw is not None else False
    flag = InboundPlacementImage.set_unload(image_id, unload)
    pm, record_id = _inbound_placement_match_for_image(image_id)
    return jsonify({'success': True, 'is_unload': bool(flag), 'placement_match': pm, 'record_id': record_id})


@bp.route('/api/v1/inbound-orders/placement-images/<int:image_id>/marks', methods=['POST'])
def api_v1_inbound_orders_placement_mark_add(image_id):
    img = InboundPlacementImage.get_by_id(image_id)
    if not img:
        return jsonify({'success': False, 'error': 'placement image not found'}), 404
    data = request.get_json() or {}
    try:
        x = float(data.get('x_ratio'))
        y = float(data.get('y_ratio'))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'x_ratio/y_ratio must be numbers'}), 400
    if not (0 <= x <= 1) or not (0 <= y <= 1):
        return jsonify({'success': False, 'error': 'x_ratio/y_ratio must be 0~1'}), 400
    r = data.get('r')
    if r is not None:
        try:
            r = float(r)
        except (TypeError, ValueError):
            r = 0.0
        if not (0 <= r <= 1):
            r = 0.0
    else:
        r = 0.0
    InboundPlacementImage.add_mark(image_id, x, y, mark_r=r)
    pm, record_id = _inbound_placement_match_for_image(image_id)
    return jsonify({'success': True, 'marks': InboundPlacementImage.get_marks(image_id),
                    'placement_match': pm, 'record_id': record_id})


@bp.route('/api/v1/inbound-orders/placement-images/<int:image_id>/marks/last', methods=['DELETE'])
def api_v1_inbound_orders_placement_mark_undo(image_id):
    img = InboundPlacementImage.get_by_id(image_id)
    if not img:
        return jsonify({'success': False, 'error': 'placement image not found'}), 404
    marks = InboundPlacementImage.delete_last_mark(image_id)
    pm, record_id = _inbound_placement_match_for_image(image_id)
    return jsonify({'success': True, 'marks': marks, 'placement_match': pm, 'record_id': record_id})

# ── AI 图片识别（双引擎：Moonshot + PaddleOCR + DeepSeek）──────
@bp.route('/api/v1/inbound-orders/ai-recognize', methods=['POST'])
def inbound_ai_recognize():
    """接收图片，调用 OCR 引擎识别商品表格，返回 JSON。

    2026-09-22:支持「🤖 智能文本」tab 走 text 分支 —— 无 image、有 text 时
    直接调 DeepSeekEngine.recognize_text(text),跳过 PaddleOCR,解析用户
    自由输入的商品描述为结构化明细行。返回 schema 与图片路径完全一致。
    """
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

    # ── 文本分支(2026-09-22 入库页「🤖 智能文本」tab)───────────────
    # 优先级:有 text 字段就走 text 分支,即使同时附了图片(image)也被忽略
    # —— 自由文本路径只调 DeepSeek,不调 OCR。
    raw_text = (request.form.get('text') or request.args.get('text') or '').strip()
    if raw_text:
        try:
            engine = get_ocr_engine('deepseek')
        except RuntimeError as e:
            return jsonify({'success': False, 'error': '引擎初始化失败', 'hint': str(e)}), 500
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e), 'hint': '请配置 DEEPSEEK_API_KEY'}), 400
        try:
            result = engine.recognize_text(raw_text)
        except Exception as e:
            current_app.logger.exception('AI 文本解析运行时错误: %s', e)
            return jsonify({
                'success': False,
                'error': f'解析引擎运行异常：{type(e).__name__}',
                'hint': '请稍后重试,或把文本改写得更结构化'
            }), 500
        # 2026-09-24:入库智能文本添加·「按 y 卖」自动归一化
        # 若商品在 product_units 配置了 is_usingyardforcounting = 1,则把
        # DeepSeek 抽出的 quantity 从「支」换成「总码数」、unit 从「支」换成 'y';
        # 否则保持原样。备注列原样保留(X支+Y码)便于用户核对。
        #
        # 优先级:
        #   - 已 unit='y' → 信任 AI 数量,跳过(防 double-convert)
        #   - remark 含「X支+(Y码|Y码*X支|...)」 → 用 remark 解析,精确(带散码)
        #   - 否则 quantity 兜底按 quantity × ypp 算总码
        if result.get('success') and result.get('items'):
            from models import ProductUnit
            from blueprints._helpers import get_ypp, compute_total_yards_from_remark
            try:
                units_cache = ProductUnit.get_all() or []
            except Exception:
                units_cache = []
            normalized = 0
            for item in result['items']:
                pn = item.get('product_name') or ''
                sp = item.get('specification') or ''
                ypp = get_ypp(pn, sp, units_cache=units_cache)
                if ypp <= 0:
                    continue
                # 已按 y 计量 → 信任 AI 给的 quantity,跳过(避免误转)
                if (item.get('unit') or '').strip() == 'y':
                    continue
                total_y = compute_total_yards_from_remark(
                    item.get('remark') or '',
                    ypp,
                    quantity_str=item.get('quantity'),
                )
                if total_y is None:
                    continue
                item['quantity'] = str(total_y)
                item['unit'] = 'y'
                normalized += 1
            if normalized:
                current_app.logger.info(
                    'ai-recognize text: 已对 %d 条「按 y 卖」明细做归一化 (ypp 命中)', normalized
                )
        if result.get('success'):
            return jsonify(result)
        error_msg = result.get('error', '')
        if 'API key' in error_msg or '未配置' in error_msg:
            return jsonify(result), 503
        return jsonify(result), 500

    # ── 图片分支(原有逻辑)────────────────────────────────────────
    if 'image' not in request.files:
        return jsonify({
            'success': False, 'error': '没有上传图片',
            'hint': '请先选择一张图片,或在「🤖 智能文本」tab 输入商品描述'
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
            preprocess_kind = ocr_preprocess_kind(record.get('product_name', ''))
            with open(img['file_path'], 'rb') as _f:
                ocr_text = get_ocr_engine('paddleocr').extract_text(_f.read(), preprocess_kind=preprocess_kind) or ''
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
            # 2026-08-19:OCR + AI 比对 + 落库 + 写事件统一交给 inbound_processor
            result = inbound_processor.process_full(
                image_id=image_id, filepath=filepath,
                record=record, order_id=record['order_pk'], record_id=record_id,
            )
            saved.append({
                'image_id': image_id,
                'image': rel_path,
                'original_name': original_name,
                'sort_order': img['sort_order'],
                'match_status': result.get('status') or None,
                'match_score': result.get('score'),
                'reason': result.get('reason') or '',
                'match_source': result.get('source'),
                'bg_color': result.get('bg_color'),
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
        # 2026-08-19:OCR + AI 比对 + 落库 + 写事件统一交给 inbound_processor
        result = inbound_processor.process_full(
            image_id=image_id, filepath=filepath,
            record=record, order_id=record['order_pk'], record_id=record_id,
        )
        AuditLog.log('upload_image', 'inbound_order', record['order_pk'],
                     detail={'filename': rel_path, 'source': source, 'record_id': record_id})
        return jsonify({'success': True, 'images': [{
            'image_id': image_id, 'image': rel_path,
            'original_name': original_name, 'sort_order': img['sort_order'],
            'match_status': result.get('status') or None,
            'match_score': result.get('score'),
            'reason': result.get('reason') or '',
            'match_source': result.get('source'),
            'bg_color': result.get('bg_color'),
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


# 2026-09-21: 入库 YPP 核查页 — 与出货/装柜同源端点,只换 biz= 参数
# 端点统一在 shipping.py:/api/v1/shipping-orders/ypp-review/scan (接受 biz=shipping|inbound|loading)
@bp.route('/inbound-ypp-review')
def inbound_ypp_review_page():
    """入库 YPP 规则冲突核查页(单页手动扫描,不受日期过滤影响)。"""
    try:
        from models import ProductUnit
        all_units = ProductUnit.get_all() or []
    except Exception:
        all_units = []
    ypp_config_count = sum(
        1 for u in all_units
        if u.get('is_usingyardforcounting') and (u.get('yards_per_piece') or 0) > 0
    )
    return render_template(
        'inbound_ypp_review.html',
        ypp_config_count=ypp_config_count,
    )


# ═════════════════════════════════════════════════════════════════════
# 「🤖 智能文本」补充提示词管理(2026-09-22)
# 挂载在 inbound.py 是因为本提示词是入库页「🤖 智能文本」tab 用的,
# 数据存于 category_prompts 表(scope='text_recognize')。所有 CRUD 走
# JSON API,前端弹框调,响应 {success, ...}。
# 设计:仅管理员能取(软删/编辑/启用);前端调用前要确认已登录。
# ═════════════════════════════════════════════════════════════════════

@bp.route('/api/v1/text-recognize-supplements', methods=['GET'])
def text_recognize_supplements_list():
    """列出所有 text_recognize 提示词(含 archived)。"""
    include_archived = (request.args.get('include_archived') == '1')
    try:
        from models._db import get_db as _get_db
        conn = _get_db()
        cur = conn.cursor()
        if include_archived:
            cur.execute(
                "SELECT * FROM category_prompts WHERE scope = 'text_recognize' "
                "ORDER BY id ASC"
            )
        else:
            cur.execute(
                "SELECT * FROM category_prompts WHERE scope = 'text_recognize' "
                "AND status = 'active' ORDER BY id ASC"
            )
        rows = [dict(r) for r in cur.fetchall()]
        conn.close()
        return jsonify({'success': True, 'items': rows})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/v1/text-recognize-supplements', methods=['POST'])
def text_recognize_supplements_create():
    """新增一条 text_recognize 提示词。
    Body: {prompt_text: str, source_ocr_text?: str}
    """
    from models._db import get_db as _get_db
    data = request.get_json(silent=True) or {}
    prompt_text = (data.get('prompt_text') or '').strip()
    if not prompt_text:
        return jsonify({'success': False, 'error': 'prompt_text 不能为空'}), 400
    if len(prompt_text) > 500:
        return jsonify({'success': False, 'error': '提示词长度不超过 500 字'}), 400
    source_ocr_text = (data.get('source_ocr_text') or '').strip()[:4000]
    try:
        conn = _get_db()
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO category_prompts "
            "(scope, category_code, spec_pattern, product_name_keyword, "
            "prompt_text, source_ocr_text, status, created_at) "
            "VALUES ('text_recognize', '', '', '', ?, ?, 'active', "
            "datetime('now','localtime'))",
            (prompt_text, source_ocr_text)
        )
        new_id = cur.lastrowid
        conn.commit()
        conn = _get_db()
        cur = conn.cursor()
        cur.execute("SELECT * FROM category_prompts WHERE id = ?", (new_id,))
        row = dict(cur.fetchone())
        conn.close()
        return jsonify({'success': True, 'item': row})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/v1/text-recognize-supplements/<int:item_id>', methods=['PATCH'])
def text_recognize_supplements_update(item_id):
    """编辑提示词文本或切换 active/archived。仅更新非空字段。"""
    from models._db import get_db as _get_db
    data = request.get_json(silent=True) or {}
    fields = []
    params = []
    if 'prompt_text' in data:
        pt = (data.get('prompt_text') or '').strip()
        if not pt:
            return jsonify({'success': False, 'error': 'prompt_text 不能为空'}), 400
        if len(pt) > 500:
            return jsonify({'success': False, 'error': '提示词长度不超过 500 字'}), 400
        fields.append('prompt_text = ?')
        params.append(pt)
    if 'status' in data:
        st = data['status']
        if st not in ('active', 'archived'):
            return jsonify({'success': False, 'error': 'status 必须 active/archived'}), 400
        fields.append('status = ?')
        params.append(st)
    if not fields:
        return jsonify({'success': False, 'error': '没有可更新的字段'}), 400
    try:
        conn = _get_db()
        cur = conn.cursor()
        params.append(item_id)
        cur.execute(f"UPDATE category_prompts SET {', '.join(fields)} WHERE id = ? AND scope = 'text_recognize'", params)
        if cur.rowcount == 0:
            conn.close()
            return jsonify({'success': False, 'error': '提示词不存在或已删除'}), 404
        conn.commit()
        cur.execute("SELECT * FROM category_prompts WHERE id = ?", (item_id,))
        row = dict(cur.fetchone())
        conn.close()
        return jsonify({'success': True, 'item': row})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/v1/text-recognize-supplements/<int:item_id>', methods=['DELETE'])
def text_recognize_supplements_delete(item_id):
    """软删:status='archived'(保留 audit,前端默认不显示 archived)。"""
    from models._db import get_db as _get_db
    try:
        conn = _get_db()
        cur = conn.cursor()
        cur.execute(
            "UPDATE category_prompts SET status = 'archived' "
            "WHERE id = ? AND scope = 'text_recognize'",
            (item_id,)
        )
        if cur.rowcount == 0:
            conn.close()
            return jsonify({'success': False, 'error': '提示词不存在'}), 404
        conn.commit()
        conn.close()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500
