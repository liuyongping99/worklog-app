"""出货记录蓝图。

包含：
- /shipping-records 页面（带日期过滤、商品单位提示计算）
- /api/v1/shipping-orders/* REST API（订单/明细/图片 CRUD + 移动 + AI 识别）
"""
import os
import uuid
import base64
import re
from datetime import date as date_cls, timedelta, datetime
from flask import Blueprint, render_template, request, jsonify, current_app
from models import (
    ShippingOrder, ShippingRecord, ShippingImage, ProductUnit, PieceConversion, AuditLog
)
from blueprints._helpers import (
    get_upload_dir as get_helpers_upload_dir,
    get_ypp, calc_hint, check_remark, summarize_remarks,
    get_piece_conversion, calc_piece_quantity, check_piece_mismatch,
    validate_image_content, check_uploaded_image,
    match_label_to_row,
)
from blueprints.ocr_engine import PaddleOCREngine, get_ocr_engine
from blueprints import _helpers

bp = Blueprint('shipping', __name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_label_match(image_abspath, record):
    """对一张行级图跑 OCR + 本地匹配，返回 (status, score, reason)。失败则 ('', None, '') 不阻断上传。"""
    try:
        with open(image_abspath, 'rb') as f:
            img_bytes = f.read()
        text = PaddleOCREngine().extract_text(img_bytes)
        status, score, reason = match_label_to_row(
            text, record.get('product_name', ''), record.get('specification', '')
        )
        return status, score, reason
    except Exception:
        current_app.logger.exception('行级图片 OCR 匹配失败（不阻断上传）')
        return '', None, ''


def _get_upload_dir():
    """薄包装，返回 (upload_dir, month_str)。"""
    return get_helpers_upload_dir()


# ── 页面 ──────────────────────────────────────────────
@bp.route('/shipping-records')
def shipping_records():
    today = date_cls.today().isoformat()
    start_date = request.args.get('start_date', '').strip()
    end_date = request.args.get('end_date', '').strip()
    # 默认最近3天
    if not start_date or not end_date:
        end = date_cls.today()
        start = end - timedelta(days=2)
        start_date = start.isoformat()
        end_date = end.isoformat()
    groups = ShippingRecord.get_groups(start_date, end_date)
    # order_pk -> 图片字典,按 source 拆分为:
    #   order_non_ai: 订单级图 + record 级图(非 AI,展示在"商品行下方"整体图区)
    #   ai_images:     订单级图 + record 级图(AI 识别图,展示在"商品行上方"AI 图区)
    order_ids = [g['id'] for g in groups]
    order_non_ai = {}  # 订单标题区(商品行下方)显示的图:仅非 AI
    ai_images = {}     # AI 识别图区(商品行上方)显示
    # ShippingImage.get_by_order 返回"订单级 + record 级"全部图;
    # 这里只需要订单级(record_pk IS NULL),record 级的从下面 get_by_record 循环补
    for oid in order_ids:
        order_only = [img for img in ShippingImage.get_by_order(oid) if img.get('record_pk') is None]
        non_ai = [img for img in order_only if img.get('source') != 'ai']
        ai = [img for img in order_only if img.get('source') == 'ai']
        if non_ai: order_non_ai[oid] = non_ai
        if ai: ai_images[oid] = ai
    # 把每个订单的所有 record 级图也按 source 分到上面两个 dict
    # 同时按 record 汇总 match_status 最高分那张的档位(green > yellow > red),
    # 给到前端做「行级徽章」首屏渲染(刷新不丢)。
    _status_rank = {'green': 3, 'yellow': 2, 'red': 1}
    record_best_status_map = {}  # record_id -> 'green'/'yellow'/'red'(行级徽章)
    record_best_reason_map = {}  # record_id -> 该 record 最高分档图对应的 reason(给 hover 用)
    for grp in groups:
        all_record_imgs = []
        for rec in grp.get('records', []):
            record_imgs = ShippingImage.get_by_record(rec['id'])
            all_record_imgs.extend(record_imgs)
            for img in record_imgs:
                status = img.get('match_status') or ''
                if not status:
                    continue
                # 2026-07-24:human_verified=1 的红牌视为已通过(等价 green)
                # 这样:刷新页面时,人工确认过的红牌不会让行级徽章仍然是 ✗
                if status == 'red' and img.get('human_verified'):
                    status = 'green'
                cur = record_best_status_map.get(rec['id'])
                if cur is None or _status_rank.get(status, 0) > _status_rank.get(cur, 0):
                    record_best_status_map[rec['id']] = status
                    record_best_reason_map[rec['id']] = img.get('reason') or ''
        for img in all_record_imgs:
            target = ai_images if img.get('source') == 'ai' else order_non_ai
            target.setdefault(grp['id'], []).append(img)
    # 按 sort_order + id 排
    for oid in order_non_ai:
        order_non_ai[oid].sort(key=lambda x: (x.get('sort_order', 0), x.get('id', 0)))
    for oid in ai_images:
        ai_images[oid].sort(key=lambda x: (x.get('sort_order', 0), x.get('id', 0)))

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

    def _get_ypp(product_name, spec):
        return get_ypp(product_name, spec, units_cache=units)

    def _calc_hint(quantity_str, ypp, unit=None, remark=None):
        return calc_hint(quantity_str, ypp, unit=unit, remark=remark)

    def _check_remark(remark, quantity_str, ypp):
        return check_remark(remark, quantity_str, ypp)

    def _get_piece_conv(product_name, spec):
        return get_piece_conversion(product_name, spec, cache=piece_convs)

    def _check_piece(remark, quantity_str, conv):
        return check_piece_mismatch(remark, quantity_str, conv)

    for group in groups:
        for item in group['records']:
            ypp = _get_ypp(item['product_name'], item.get('specification', ''))
            item['unit_hint'] = _calc_hint(item['quantity'], ypp, unit=item.get('unit', ''), remark=item.get('remark', ''))
            item['mismatch'] = _check_remark(
                item.get('remark', ''),
                item['quantity'],
                ypp
            )
            # 数量异常标记：空 或 非数字（含"货-"、字母、特殊符号等）→ 整行警告
            qty_raw = (item.get('quantity') or '').strip()
            try:
                float(qty_raw)
                item['qty_invalid'] = False
            except (TypeError, ValueError):
                item['qty_invalid'] = True

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
        group['has_eco'] = any('环保' in r.get('product_name', '') for r in group['records'])
        group['has_jia_mian'] = any(
            '杂胶' in r.get('product_name', '')
            and '加面' in r.get('specification', '')
            for r in group['records']
        )
        group['has_match'] = any(record_best_status_map.get(r['id']) for r in group['records'])

    # 2026-07-24: 把所有 group 的 records 合并成 record_by_pk,模板里给图片叠加品名/规格用
    # (服务端渲染,不依赖 JS 加载 — 之前靠 JS append 在某些场景下不稳定)
    record_by_pk = {}
    for grp in groups:
        for r in grp.get('records', []):
            record_by_pk[r['id']] = r

    return render_template(
        'shipping-records.html',
        groups=groups,
        order_images=order_non_ai,  # 模板里变量名仍叫 order_images,但内容已剔 AI
        ai_images=ai_images,
        record_best_status_map=record_best_status_map,
        record_best_reason_map=record_best_reason_map,
        record_by_pk=record_by_pk,   # 2026-07-24 新增:图片叠加文字用的 record 查表
        page_title='出货记录',
        today=today,
        start_date=start_date,
        end_date=end_date,
        unit_list=unit_list,
        piece_conversions=piece_conv_list,
    )


# ── 订单 CRUD ──────────────────────────────────────────────
@bp.route('/api/v1/shipping-orders', methods=['POST'])
def api_v1_shipping_orders_create():
    """创建出货单 → 201"""
    data = request.get_json() if request.is_json else request.form
    date = data.get('date', '').strip()
    customer = data.get('customer', '').strip()
    if not date or not customer:
        return jsonify({'success': False, 'error': '日期和客户不能为空'}), 400
    order_id = ShippingOrder.create(date, customer)
    AuditLog.log('create_order', 'shipping_order', order_id, detail={'date': date, 'customer': customer})
    return jsonify({'success': True, 'order': {'id': order_id, 'date': date, 'customer': customer}}), 201


@bp.route('/api/v1/shipping-orders/<int:order_id>', methods=['DELETE'])
def api_v1_shipping_orders_delete(order_id):
    """删除出货单 → 200（有明细或图片时拒绝）"""
    order = ShippingOrder.get_by_id(order_id)
    if not order:
        return jsonify({'success': False, 'error': '订单不存在'}), 404
    if order.get('is_locked'):
        return jsonify({'success': False, 'error': '订单已锁定，无法删除'}), 403
    result = ShippingOrder.delete(order_id)
    if not result.get('success'):
        return jsonify(result), 400
    AuditLog.log('delete_order', 'shipping_order', order_id, detail={'customer': order.get('customer')})
    return jsonify({'success': True})


@bp.route('/api/v1/shipping-orders/<int:order_id>', methods=['PATCH'])
def api_v1_shipping_orders_update(order_id):
    """更新出货单（锁定/解锁、图片列数）→ 200"""
    order = ShippingOrder.get_by_id(order_id)
    if not order:
        return jsonify({'success': False, 'error': '订单不存在'}), 404
    data = request.get_json()
    if data is None:
        return jsonify({'success': False, 'error': '请求体不能为空'}), 400

    if 'is_locked' in data:
        if data['is_locked']:
            ShippingOrder.lock(order_id)
            AuditLog.log('lock_order', 'shipping_order', order_id)
        else:
            ShippingOrder.unlock(order_id)
            AuditLog.log('unlock_order', 'shipping_order', order_id)
        return jsonify({'success': True, 'locked': bool(data['is_locked'])})

    if 'img_cols' in data:
        try:
            cols = int(data['img_cols'])
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': '列数必须是整数'}), 400
        if cols < 1 or cols > 5:
            return jsonify({'success': False, 'error': '列数范围为1-5'}), 400
        ShippingOrder.set_img_cols(order_id, cols)
        return jsonify({'success': True, 'img_cols': cols})

    # 同时改备注 + 客户 + 日期（弹框一次保存三个字段）—— 必须先判断，否则会落到下面的单字段分支
    if 'order_note' in data and 'customer' in data and 'date' in data:
        if order.get('is_locked'):
            return jsonify({'success': False, 'error': '订单已锁定，无法修改'}), 403
        new_customer = str(data['customer']).strip()
        new_date = str(data['date']).strip()
        note = str(data['order_note']).strip()
        if not new_customer:
            return jsonify({'success': False, 'error': '客户名称不能为空'}), 400
        if not new_date:
            return jsonify({'success': False, 'error': '日期不能为空'}), 400
        # 原子更新：在单个事务里完成 date + customer + order_num + note，避免半状态
        from models import get_db
        conn = get_db()
        cursor = conn.cursor()
        try:
            cursor.execute(
                'SELECT COALESCE(MAX(order_num), 0) + 1 FROM shipping_orders WHERE date = ? AND customer = ?',
                (new_date, new_customer)
            )
            new_order_num = cursor.fetchone()[0]
            cursor.execute(
                'UPDATE shipping_orders SET date = ?, customer = ?, order_num = ?, order_note = ? WHERE id = ?',
                (new_date, new_customer, new_order_num, note, order_id)
            )
            conn.commit()
        except Exception as e:
            conn.rollback()
            conn.close()
            current_app.logger.exception('shipping PATCH复合字段失败: order_id=%s', order_id)
            return jsonify({'success': False, 'error': '服务异常，请稍后重试'}), 400
        conn.close()
        return jsonify({'success': True, 'date': new_date, 'customer': new_customer, 'order_num': new_order_num, 'order_note': note})

    if 'order_note' in data:
        if order.get('is_locked'):
            return jsonify({'success': False, 'error': '订单已锁定，无法修改备注'}), 403
        note = str(data['order_note']).strip()
        ShippingOrder.set_note(order_id, note)
        AuditLog.log('update_note', 'shipping_order', order_id, detail={'note': note[:80]})
        return jsonify({'success': True, 'order_note': note})

    if 'doc_number' in data:
        if order.get('is_locked'):
            return jsonify({'success': False, 'error': '订单已锁定，无法修改单据编号'}), 403
        doc_number = str(data['doc_number']).strip()[:200]
        ShippingOrder.set_doc_number(order_id, doc_number)
        AuditLog.log('update_doc_number', 'shipping_order', order_id, detail={'doc_number': doc_number})
        return jsonify({'success': True, 'doc_number': doc_number})

    if 'customer' in data:
        if order.get('is_locked'):
            return jsonify({'success': False, 'error': '订单已锁定，无法修改客户名称'}), 403
        new_customer = str(data['customer']).strip()
        if not new_customer:
            return jsonify({'success': False, 'error': '客户名称不能为空'}), 400
        result = ShippingOrder.set_customer(order_id, new_customer)
        if result.get('success'):
            AuditLog.log('update_customer', 'shipping_order', order_id, detail={'customer': new_customer})
            return jsonify(result)
        return jsonify(result), 400

    if 'date' in data:
        if order.get('is_locked'):
            return jsonify({'success': False, 'error': '订单已锁定，无法修改日期'}), 403
        new_date = str(data['date']).strip()
        if not new_date:
            return jsonify({'success': False, 'error': '日期不能为空'}), 400
        result = ShippingOrder.set_date(order_id, new_date)
        if result.get('success'):
            AuditLog.log('update_date', 'shipping_order', order_id, detail={'date': new_date})
            return jsonify(result)
        return jsonify(result), 400

    return jsonify({'success': False, 'error': '无可更新的字段'}), 400


# ── 明细 CRUD ──────────────────────────────────────────────
@bp.route('/api/v1/shipping-orders/<int:order_id>/records', methods=['POST'])
def api_v1_shipping_orders_add_record(order_id):
    """向出货单添加明细 → 201"""
    order = ShippingOrder.get_by_id(order_id)
    if not order:
        return jsonify({'success': False, 'error': '订单不存在'}), 404
    if order.get('is_locked'):
        return jsonify({'success': False, 'error': '该订单已锁定，无法添加商品'}), 403

    product_name = request.form.get('product_name', '').strip()
    specification = request.form.get('specification', '').strip()
    quantity = request.form.get('quantity', '').strip()
    unit = request.form.get('unit', '支').strip()
    remark = request.form.get('remark', '').strip()

    if not product_name or not quantity:
        return jsonify({'success': False, 'error': '品名和数量不能为空'}), 400

    record_id = ShippingRecord.create('', '', product_name, specification, quantity, unit, remark, order_pk=order_id)
    AuditLog.log('add_record', 'shipping_order', order_id, detail={'product': product_name, 'qty': quantity, 'unit': unit})
    return jsonify({'success': True, 'id': record_id}), 201


@bp.route('/api/v1/shipping-orders/<int:order_id>/records/batch', methods=['POST'])
def api_v1_shipping_orders_add_records_batch(order_id):
    """批量添加明细 → 201"""
    order = ShippingOrder.get_by_id(order_id)
    if not order:
        return jsonify({'success': False, 'error': '订单不存在'}), 404
    if order.get('is_locked'):
        return jsonify({'success': False, 'error': '该订单已锁定，无法添加商品'}), 403

    data = request.get_json()
    if not data or not data.get('items'):
        return jsonify({'success': False, 'error': '无数据'}), 400

    items = data['items']
    records = []
    for item in items:
        product_name = item.get('product_name', '').strip()
        specification = item.get('specification', '').strip()
        quantity = item.get('quantity', '').strip()
        unit = item.get('unit', '支').strip()
        remark = item.get('remark', '').strip()
        if product_name and quantity:
            record_id = ShippingRecord.create('', '', product_name, specification, quantity, unit, remark, order_pk=order_id)
            records.append({
                'id': record_id, 'product_name': product_name,
                'specification': specification, 'quantity': quantity,
                'unit': unit, 'remark': remark
            })
    if records:
        AuditLog.log('batch_add_records', 'shipping_order', order_id, detail={'count': len(records)})
    return jsonify({'success': True, 'count': len(records), 'records': records}), 201


@bp.route('/api/v1/shipping-orders/records/<int:record_id>', methods=['PUT'])
def api_v1_shipping_orders_update_record(record_id):
    """更新明细 → 200"""
    record = ShippingRecord.get_by_id(record_id)
    if not record:
        return jsonify({'success': False, 'error': '记录不存在'}), 404
    order = ShippingOrder.get_by_id(record['order_pk'])
    if order and order.get('is_locked'):
        return jsonify({'success': False, 'error': '该订单已锁定，无法修改'}), 403

    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': '请求体不能为空'}), 400

    if data.get('unit') == '码':
        data['unit'] = 'y'
    ShippingRecord.update(record_id, data)
    updated = ShippingRecord.get_by_id(record_id)
    AuditLog.log('update_record', 'shipping_order', updated['order_pk'], detail={'record_id': record_id})
    return jsonify({'success': True, 'record': updated})


@bp.route('/api/v1/shipping-orders/records/<int:record_id>', methods=['DELETE'])
def api_v1_shipping_orders_delete_record(record_id):
    """删除单条明细 → 200"""
    record = ShippingRecord.get_by_id(record_id)
    if not record:
        return jsonify({'success': False, 'error': '记录不存在'}), 404
    order = ShippingOrder.get_by_id(record['order_pk'])
    if order and order.get('is_locked'):
        return jsonify({'success': False, 'error': '该订单已锁定，无法删除商品'}), 403

    ShippingRecord.delete(record_id)
    AuditLog.log('delete_record', 'shipping_order', record['order_pk'], detail={'record_id': record_id, 'product': record.get('product_name', '')})
    return jsonify({'success': True})


@bp.route('/api/v1/shipping-orders/<int:order_id>/records', methods=['DELETE'])
def api_v1_shipping_orders_delete_all_records(order_id):
    """删除出货单所有明细 → 200"""
    order = ShippingOrder.get_by_id(order_id)
    if not order:
        return jsonify({'success': False, 'error': '订单不存在'}), 404
    if order.get('is_locked'):
        return jsonify({'success': False, 'error': '该订单已锁定，无法操作'}), 403

    ShippingRecord.delete_by_order(order_id)
    AuditLog.log('delete_all_records', 'shipping_order', order_id)
    return jsonify({'success': True})


@bp.route('/api/v1/shipping-orders/records/<int:record_id>/move', methods=['PATCH'])
def api_v1_shipping_orders_move_record(record_id):
    """移动明细排序 → 200"""
    record = ShippingRecord.get_by_id(record_id)
    if not record:
        return jsonify({'success': False, 'error': '记录不存在'}), 404
    order = ShippingOrder.get_by_id(record['order_pk'])
    if order and order.get('is_locked'):
        return jsonify({'success': False, 'error': '订单已锁定'}), 403

    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': '请求体不能为空'}), 400
    direction = data.get('direction', '')

    if direction == 'up':
        ok = ShippingRecord.move_up(record_id)
    elif direction == 'down':
        ok = ShippingRecord.move_down(record_id)
    else:
        return jsonify({'success': False, 'error': 'direction 必须为 up 或 down'}), 400

    if ok:
        AuditLog.log('move_record', 'shipping_order', record['order_pk'], detail={'record_id': record_id, 'direction': direction})
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': '无法移动（已是首条/末条）'}), 400


# ── 图片 CRUD ──────────────────────────────────────────────
@bp.route('/api/v1/shipping-orders/<int:order_id>/images', methods=['POST'])
def api_v1_shipping_orders_upload_image(order_id):
    """上传出货单图片 → 201"""
    order = ShippingOrder.get_by_id(order_id)
    if not order:
        return jsonify({'success': False, 'error': '订单不存在'}), 404
    if order.get('is_locked'):
        return jsonify({'success': False, 'error': '订单已锁定，无法上传图片'}), 403

    upload_dir, month_str = _get_upload_dir()

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
            filename = f"{uuid.uuid4().hex}.{ext}"
            filepath = os.path.join(upload_dir, filename)
            with open(filepath, 'wb') as f:
                f.write(img_bytes)
            try:
                validate_image_content(filepath)
            except ValueError as e:
                return jsonify({'success': False, 'error': str(e)}), 400
            original_name = 'pasted_image'
        else:
            return jsonify({'success': False, 'error': '无效的图片数据'}), 400
    elif 'image' in request.files:
        file = request.files['image']
        if file.filename:
            try:
                ext = check_uploaded_image(file)
            except ValueError as e:
                return jsonify({'success': False, 'error': str(e)}), 400
            filename = f"{uuid.uuid4().hex}.{ext}"
            filepath = os.path.join(upload_dir, filename)
            file.save(filepath)
            try:
                validate_image_content(filepath)
            except ValueError as e:
                return jsonify({'success': False, 'error': str(e)}), 400
            original_name = file.filename
        else:
            return jsonify({'success': False, 'error': '未选择文件'}), 400
    else:
        return jsonify({'success': False, 'error': '未提供图片'}), 400

    source = request.form.get('source', 'upload')
    if request.is_json:
        source = data.get('source', 'upload')
    if source not in ('upload', 'ai'):
        source = 'upload'
    image_id = ShippingImage.create(order_id, filepath, original_name, source)
    rel_path = os.path.join(month_str, filename).replace('\\', '/')
    AuditLog.log('upload_image', 'shipping_order', order_id, detail={'filename': filename, 'source': source})
    return jsonify({'success': True, 'image_id': image_id, 'image': rel_path, 'file_path': filepath, 'original_name': original_name}), 201


@bp.route('/api/v1/shipping-orders/images/<int:image_id>', methods=['DELETE'])
def api_v1_shipping_orders_delete_image(image_id):
    """删除出货记录图片 → 200"""
    img = ShippingImage.get_by_id(image_id)
    if not img:
        return jsonify({'success': False, 'error': '图片不存在'}), 404
    order = ShippingOrder.get_by_id(img['order_pk'])
    if order and order.get('is_locked'):
        return jsonify({'success': False, 'error': '订单已锁定，无法删除图片'}), 403
    if ShippingImage.delete(image_id):
        AuditLog.log('delete_image', 'shipping_order', img['order_pk'], detail={'image_id': image_id})
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': '图片不存在'}), 404


@bp.route('/api/v1/shipping-orders/images/<int:image_id>/manual-verify', methods=['POST'])
def api_v1_shipping_orders_manual_verify_image(image_id):
    """人工覆盖 AI 比对结果(把红牌标记为已确认)→ 200

    Body (可选): {"verified": true|false}，默认 true。
    副作用:写 shipping_images.human_verified + 写审计日志。
    """
    img = ShippingImage.get_by_id(image_id)
    if not img:
        return jsonify({'success': False, 'error': '图片不存在'}), 404
    order = ShippingOrder.get_by_id(img['order_pk'])
    if order and order.get('is_locked'):
        return jsonify({'success': False, 'error': '订单已锁定，无法操作'}), 403
    data = request.get_json(silent=True) or {}
    verified = bool(data.get('verified', True))
    ShippingImage.set_human_verified(image_id, verified)
    AuditLog.log(
        'manual_verify_image', 'shipping_order', img['order_pk'],
        detail={'image_id': image_id, 'human_verified': 1 if verified else 0},
    )
    return jsonify({'success': True, 'image_id': image_id, 'human_verified': verified})


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
    else: ext = 'jpg'
    img_bytes = base64.b64decode(base64_data)
    filename = f"{uuid.uuid4().hex}.{ext}"
    filepath = os.path.join(upload_dir, filename)
    with open(filepath, 'wb') as f:
        f.write(img_bytes)
    validate_image_content(filepath)
    return filepath, 'pasted_image'


@bp.route('/api/v1/shipping-orders/records/<int:record_id>/images', methods=['POST'])
def api_v1_shipping_orders_record_upload_images(record_id):
    """上传商品行图片(多文件),按上传顺序赋 sort_order → 201"""
    record = ShippingRecord.get_by_id(record_id)
    if not record:
        return jsonify({'success': False, 'error': '记录不存在'}), 404
    order = ShippingOrder.get_by_id(record['order_pk'])
    if order and order.get('is_locked'):
        return jsonify({'success': False, 'error': '该订单已锁定，无法上传图片'}), 403

    upload_dir, month_str = _get_upload_dir()
    source = 'upload'
    saved = []  # [(image_id, rel_path, original_name, sort_order)]

    # 接收两种格式:multipart 多个 'image' 字段,或 JSON 里 images[]/image(单个 base64)
    files = request.files.getlist('image')
    if files:
        # 多个文件:逐个按顺序存,sort_order 自动累加(因 ShippingImage.create 内部 max+1)
        for f in files:
            try:
                filepath, original_name = _save_one_uploaded_file(f, upload_dir)
            except ValueError as e:
                return jsonify({'success': False, 'error': str(e)}), 400
            image_id = ShippingImage.create(
                order_pk=record['order_pk'],
                file_path=filepath,
                original_name=original_name,
                source=source,
                record_pk=record_id,
            )
            rel_path = os.path.join(month_str, os.path.basename(filepath)).replace('\\', '/')
            # 取刚插入的 sort_order
            img = ShippingImage.get_by_id(image_id)
            status, score, reason = _run_label_match(filepath, record)
            if status:
                ShippingImage.set_match(image_id, status, score, reason)
            saved.append({
                'image_id': image_id,
                'image': rel_path,
                'original_name': original_name,
                'sort_order': img['sort_order'],
                'match_status': status or None,
                'match_score': score,
            })
        for s in saved:
            AuditLog.log('upload_image', 'shipping_order', record['order_pk'],
                         detail={'filename': s['image'], 'source': source, 'record_id': record_id})
        return jsonify({'success': True, 'images': saved, 'count': len(saved)}), 201

    # 单文件 base64(JSON)
    if request.is_json:
        data = request.get_json() or {}
        try:
            filepath, original_name = _save_one_base64_image(data.get('image', ''), upload_dir)
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        source = data.get('source', 'upload')
        image_id = ShippingImage.create(
            order_pk=record['order_pk'],
            file_path=filepath,
            original_name=original_name,
            source=source,
            record_pk=record_id,
        )
        rel_path = os.path.join(month_str, os.path.basename(filepath)).replace('\\', '/')
        img = ShippingImage.get_by_id(image_id)
        status, score, reason = _run_label_match(filepath, record)
        if status:
            ShippingImage.set_match(image_id, status, score, reason)
        AuditLog.log('upload_image', 'shipping_order', record['order_pk'],
                     detail={'filename': rel_path, 'source': source, 'record_id': record_id})
        return jsonify({'success': True, 'images': [{
            'image_id': image_id, 'image': rel_path,
            'original_name': original_name, 'sort_order': img['sort_order'],
            'match_status': status or None, 'match_score': score,
        }], 'count': 1}), 201

    return jsonify({'success': False, 'error': '未提供图片'}), 400


@bp.route('/api/v1/shipping-orders/records/<int:record_id>/images-area', methods=['GET'])
def api_v1_shipping_orders_record_images_area(record_id):
    """某 record 的合并图片区:该 record 专属图 + 订单共享图,统一排序"""
    record = ShippingRecord.get_by_id(record_id)
    if not record:
        return jsonify({'success': False, 'error': '记录不存在'}), 404
    items = ShippingImage.get_combined_for_record(record['order_pk'], record_id)
    # 把每条 record_pk=None 的项标 scope=order,非 None 标 scope=record,便于前端 tooltip 决策
    for it in items:
        it['scope'] = 'order' if it.get('record_pk') is None else 'record'
    return jsonify({'success': True, 'images': items, 'count': len(items)})



# ── AI 图片识别（双引擎：Moonshot + PaddleOCR） ──────────
@bp.route('/api/v1/shipping-orders/ai-recognize', methods=['POST'])
def shipping_records_ai_recognize():
    """接收图片，调用 OCR 引擎识别商品表格，返回 JSON。

    Query/Form 参数: engine=moonshot|paddleocr（默认: .env OCR_BACKEND 或 moonshot）
    """
    from blueprints.ocr_engine import get_ocr_engine

    # 确定引擎
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

    # 验证图片
    if 'image' not in request.files:
        return jsonify({
            'success': False,
            'error': '没有上传图片',
            'hint': '请先选择一张图片再点击识别'
        }), 400
    file = request.files['image']
    if file.filename == '':
        return jsonify({
            'success': False,
            'error': '未选择文件',
            'hint': '请先选择一张图片再点击识别'
        }), 400

    img_bytes = file.read()

    # 初始化引擎
    try:
        engine = get_ocr_engine(engine_name)
    except RuntimeError as e:
        return jsonify({
            'success': False,
            'error': '引擎初始化失败',
            'hint': str(e)
        }), 500
    except ValueError as e:
        return jsonify({
            'success': False,
            'error': str(e),
            'hint': '请使用 "moonshot"、"paddleocr" 或 "deepseek"'
        }), 400

    # 委托识别
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
        # 根据错误类型返回合适的 HTTP 状态码
        error_msg = result.get('error', '')
        if 'API key' in error_msg or '未配置' in error_msg:
            return jsonify(result), 503
        if '未安装' in error_msg:
            return jsonify(result), 500
        return jsonify(result), 500


# ── 整单 AI 匹配(PaddleOCR 提字 + DeepSeek 逐行比对) ──────
@bp.route('/api/v1/shipping-orders/<int:order_id>/ai-match', methods=['POST'])
def api_v1_shipping_orders_ai_match(order_id):
    """整单 AI 匹配:OCR 该单所有行级图 → DeepSeek 逐行比对 → 回写 match_status。

    Returns:
        404 - 订单不存在
        400 - 没有可识别的行级图片
        200 - {'success': True, 'results': [...], 'summary': 'N 行已核对:G ✓ / Y ⚠ / R ✗'}
        500 - DeepSeek 调用失败
    """
    order = ShippingOrder.get_by_id(order_id)
    if not order:
        return jsonify({'success': False, 'error': '订单不存在'}), 404

    # 拉取该单所有明细行(ShippingRecord 没有 get_by_order,内联 SQL 最稳)
    from models._db import get_db as _get_db
    conn = _get_db()
    cur = conn.cursor()
    cur.execute(
        'SELECT * FROM shipping_records WHERE order_pk = ? ORDER BY sort_order, id',
        (order_id,)
    )
    records = [dict(r) for r in cur.fetchall()]
    conn.close()

    # 汇总该单所有行级图片的 OCR 文字
    paddle = PaddleOCREngine()
    texts = []
    for rec in records:
        for img in ShippingImage.get_by_record(rec['id']):
            abspath = os.path.join(_helpers.BASE_DIR, 'upload', img['relative_path'])
            try:
                with open(abspath, 'rb') as f:
                    texts.append(paddle.extract_text(f.read()))
            except Exception:
                current_app.logger.exception('AI 匹配读图失败: %s', abspath)
    ocr_text = '\n'.join(t for t in texts if t)
    if not ocr_text:
        return jsonify({'success': False, 'error': '没有可识别的行级图片'}), 400

    rows = [{'record_id': r['id'], 'product_name': r['product_name'],
             'specification': r['specification']} for r in records]
    try:
        engine = get_ocr_engine('deepseek')
        verdicts = engine.compare_rows(ocr_text, rows)
    except Exception as e:
        current_app.logger.exception('DeepSeek 比对失败: %s', e)
        return jsonify({'success': False, 'error': f'AI 比对失败:{type(e).__name__}'}), 500

    _SCORE = {'green': 90.0, 'yellow': 70.0, 'red': 30.0}
    results = []
    for v in verdicts:
        rid = v.get('record_id')
        raw_status = v.get('match_status', 'red')
        # Normalize: lowercase + small synonym map (LLMs drift on casing/abbrev)
        status = {'g': 'green', 'y': 'yellow', 'r': 'red',
                  'green': 'green', 'yellow': 'yellow', 'red': 'red',
                  'ok': 'green', 'yes': 'green', 'no': 'red', 'unknown': 'yellow'}.get(
            str(raw_status).strip().lower(), 'red')
        score = _SCORE.get(status, 30.0)
        reason = v.get('reason', '') or ''
        for img in ShippingImage.get_by_record(rid):
            ShippingImage.set_match(img['id'], status, score, reason)
        results.append({'record_id': rid, 'match_status': status, 'match_score': score,
                        'reason': reason})

    g = sum(1 for r in results if r['match_status'] == 'green')
    y = sum(1 for r in results if r['match_status'] == 'yellow')
    rd = sum(1 for r in results if r['match_status'] == 'red')
    summary = f'{len(results)} 行已核对:{g} ✓ / {y} ⚠ / {rd} ✗'
    return jsonify({'success': True, 'results': results, 'summary': summary})
