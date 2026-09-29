"""装柜订单蓝图。

包含：
- /loading-orders 页面（带日期过滤、图片画廊）
- /api/v1/loading-orders/* REST API（订单/明细/图片 CRUD + 移动）
"""
import os
import re
import base64
import uuid
import time
import threading
import contextvars
from datetime import date, timedelta, datetime
from flask import Blueprint, render_template, request, jsonify, current_app, session, abort
from models import (
    LoadingOrder, LoadingOrderRecord, LoadingOrderImage, LoadingPlacementImage, ProductUnit, PieceConversion, get_db, AuditLog,
    OcrMatchEvent,
    classify_record, CategoryPrompt,
)
from blueprints._helpers import (
    get_upload_dir as get_helpers_upload_dir,
    get_ypp, calc_hint, check_remark, summarize_remarks,
    get_piece_conversion, calc_piece_quantity, check_piece_mismatch,
    validate_image_content, check_uploaded_image,
    match_label_to_row, detect_bg_color,
    compute_placement_expected_zhi, apply_user_rotation,
)
from blueprints.ocr_engine import PaddleOCREngine, get_ocr_engine, OCR_MATCH_PROMPT_VERSION, ocr_preprocess_kind
from blueprints.ocr_pipeline import RecordImageProcessor, _OCR_LOCK, _ASYNC_JOBS
from blueprints import _helpers
from models._db import get_db
from blueprints.ocr_log import set_log_context
# 2026-09-09: 拷贝纸/日本纸 共享常量 + 判定函数(对齐出货,出货 9/9 已加)
# 跨 blueprint import:shipping.py 不反向 import loading.py,无循环风险。
from blueprints.shipping import COPY_PAPER_LABEL_SOURCE, _is_copy_paper_item

bp = Blueprint('loading', __name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 2026-09-09: 散码正则(对齐 shipping.py:SANMA_RE)
# 匹配「5y」「5 y」「3Y」等形式,且前面不能是数字/小数点(避免吃掉码数如 3.5y 被误读成 5y)
SANMA_RE = re.compile(r'(?<![\d.])(\d+)\s*[yY]')

# 行级图 OCR pipeline 共用处理器(装柜专用,显式注入 LoadingOrderImage)
# 替代端点内嵌的 OCR + AI 比对代码(2026-08-19 抽取到 ocr_pipeline)。
# ocr_engine_getter 用 lambda 包裹让 monkeypatch 生效。
# 装柜行为:不注入自适应提示词(2026-07-30 起 loading 不拼 CategoryPrompt;
# 后续是否补,看准确率再说),端点显式传 with_supplement=False。
loading_processor = RecordImageProcessor(
    LoadingOrderImage, ocr_engine_getter=lambda name: get_ocr_engine(name))


# 2026-09-18: 行级图异步 OCR+AI 触发器 — 与出货 shipping._spawn_record_image_processing 同源。
# 装柜历史上传是同步落库 + 等用户手动点 fuzzy/ai-judge 才写 match_status;
# 现在对齐出货:上传即后台跑 OCR+AI,前端轮询 /match-status 拿结果。
def _spawn_loading_record_image_processing(image_id, filepath, record, order_id,
                                            record_id, rel_path, original_name, sort_order):
    _ASYNC_JOBS[image_id] = {'state': 'processing', 'started': time.time()}

    def _run_async_in_thread():
        loading_processor.process_async(
            image_id=image_id, filepath=filepath, record=record,
            order_id=order_id, record_id=record_id,
        )

    # threading.Thread 不会自动传 contextvars — 必须显式 copy_context().run(...)
    # 把当前请求的 trace_id + 业务上下文带进后台线程,否则 OCR 日志全显示 '-'
    ctx = contextvars.copy_context()
    t = threading.Thread(
        target=ctx.run,
        args=(_run_async_in_thread,),
        daemon=True,
    )
    t.start()
    return {
        'image_id': image_id,
        'image': rel_path,
        'original_name': original_name,
        'sort_order': sort_order,
        'processing': True,        # 前端据此启动 pollRecordImageMatch
        'match_status': None,
        'match_score': None,
        'match_source': None,
    }


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


def _detect_cylinder_circles(image_path):
    """用 OpenCV HoughCircles 检测圆柱端面(正圆),返回归一化圆列表。

    每个圆: {x, y, rx, ry},其中 (x, y) 为圆心(相对宽/高 0~1),
    rx = 半径/宽, ry = 半径/高。检测不到或出错返回 []。
    仅适用于圆柱端面正对镜头(圆形可见)的摆放照。
    (对齐 shipping.py:_detect_cylinder_circles)
    """
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
        # 多次尝试不同累加阈值,提升召回(避免漏检),最后去重
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
        current_app.logger.exception('圆柱端面检测失败: %s', image_path)
        return []


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

    # 2026-09-09: 摆放图按 record 预加载(数点匹配 + 模板按记录渲染靠它),
    # 与 shipping 一致:用单独的 placement_by_record 而非混入 OCR/AI 图。
    placement_by_record = {}
    for grp in groups:
        for rec in grp.get('records', []):
            placement_by_record[rec['id']] = LoadingPlacementImage.get_by_record(rec['id'])

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

            # 2026-09-09: 数点匹配标记 —— 供操作列「点数」按钮加绿框。
            # 匹配 = 该记录有摆放图,且 支合计==备注支期望, 且(无散码期望 或 散码合计==备注散码期望)
            # 装柜独有 is_unload(卸载货物 → 支数取负),与出货页 _eff_zhi 唯一差异点。
            _pimgs = placement_by_record.get(item['id']) or []
            _pm = False
            if _pimgs:
                _remark = item.get('remark') or ''
                _qty = item.get('quantity') or ''
                _unit = item.get('unit') or ''
                _total = 0
                _loose = 0
                for p in _pimgs:
                    _base = p['manual_count'] if p['manual_count'] is not None else (p.get('n_marks') or 0)
                    _total += -_base if p.get('is_unload') else _base
                    _loose += p.get('loose_count') or 0
                _exp_zhi, _has_zhi = compute_placement_expected_zhi(_remark, _qty, _unit)
                _san_m = list(SANMA_RE.finditer(_remark))
                _exp_san = sum(int(x.group(1)) for x in _san_m)
                _has_san = len(_san_m) > 0
                _matched_zhi = (not _has_zhi) or (abs(_total - _exp_zhi) <= 0.01)
                _matched_san = (not _has_san) or (abs(_loose - _exp_san) <= 0.01)
                if (_has_zhi or _has_san) and _matched_zhi and _matched_san:
                    _pm = True
            item['placement_match'] = _pm
        group.update(summarize_remarks(group['records']))

    order_images = LoadingOrderImage.get_all_by_orders(
        order_ids=[g['order_pk'] for g in groups]
    )
    # 按 source 拆分:非 AI(商品行下方图区) + AI(商品行上方 AI 图区)
    # 2026-09-18: 与出货/入库对齐 —— 主图区排除 source='placement',placement 图只走 .placement-area
    order_non_ai = {}
    ai_images = {}
    for oid, imgs in order_images.items():
        for img in imgs:
            if img.get('record_pk') is not None:
                continue  # record 级图由下面 get_by_record 循环补,避免重复
            if img.get('source') == 'placement':
                continue  # placement 图走 .placement-area,不进主图区
            target = ai_images if img.get('source') == 'ai' else order_non_ai
            target.setdefault(oid, []).append(img)
    # 合并 record 级图
    for grp in groups:
        for rec in grp.get('records', []):
            for img in LoadingOrderImage.get_by_record(rec['id']):
                if img.get('source') == 'placement':
                    continue  # placement 图同上,跳过
                target = ai_images if img.get('source') == 'ai' else order_non_ai
                target.setdefault(grp['order_pk'], []).append(img)
    for oid in order_non_ai:
        order_non_ai[oid].sort(key=lambda x: (x.get('sort_order', 0), x.get('id', 0)))
    for oid in ai_images:
        ai_images[oid].sort(key=lambda x: (x.get('sort_order', 0), x.get('id', 0)))

    # 2026-09-18: 摆放图分组(对齐 shipping/inbound) — server-side 渲染 placement-area
    # 同产品多规格并排,不同产品单规格也能并排。
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
            expected_zhi, has_zhi = compute_placement_expected_zhi(
                remark, rec.get('quantity') or '', rec.get('unit') or ''
            )
            san_m = list(SANMA_RE.finditer(remark))
            expected_sanma = sum(int(x.group(1)) for x in san_m)
            has_sanma = len(san_m) > 0
            current['items'].append({
                'record_id': rec['id'],
                'specification': rec.get('specification') or '',
                'quantity': rec.get('quantity') or '',
                'unit': rec.get('unit') or '',
                # 装柜独有 is_unload(卸载货物 → 支数取负),与出货页 _eff_zhi 等价
                'total': sum((-(p['manual_count'] if p['manual_count'] is not None else (p.get('n_marks') or 0))
                              if p.get('is_unload') else
                              (p['manual_count'] if p['manual_count'] is not None else (p.get('n_marks') or 0)))
                             for p in pimgs),
                'loose_total': loose_total,
                'remark': remark,
                'expected_zhi': expected_zhi,
                'has_zhi': has_zhi,
                'expected_sanma': expected_sanma,
                'has_sanma': has_sanma,
                'images': pimgs,
            })
        if group_list:
            placement_groups[grp['order_pk']] = group_list

    # 不再用 cookie:img_cols 由 Task 1 加的 loading_orders.img_cols 列提供,渲染走 group.img_cols
    return render_template(
        'loading-orders.html',
        groups=groups,
        order_images=order_non_ai,
        ai_images=ai_images,
        placement_groups=placement_groups,  # 2026-09-18: 摆放图 server-side 渲染
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
    # 2026-09-12: 拷贝纸/日本纸行禁止走普通 /images 端点 → 必须用 /copy-paper-images
    # 后端硬拦截,与出货页一致(出货/装柜共用 _is_copy_paper_item 判定)
    if _is_copy_paper_item(record):
        return jsonify({
            'success': False,
            'error': '该明细为拷贝纸/日本纸,请使用 🖼️ 标签按钮上传(不需 OCR)'
        }), 400

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
            # 2026-09-19: 移动端拍照方向修正(90/180/270),落盘后再走 OCR
            rotate_deg = request.form.get('rotate_deg')
            try:
                filepath = apply_user_rotation(filepath, rotate_deg)
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
            # 2026-09-19: 上传即异步跑 OCR + AI 比对(对齐出货);前端靠 processing:true 启动轮询
            saved.append(_spawn_loading_record_image_processing(
                image_id, filepath, record, record['order_pk'], record_id,
                rel_path, original_name, img['sort_order'],
            ))
        for s in saved:
            AuditLog.log('upload_image', 'loading_order', record['order_pk'],
                         detail={'filename': s['image'], 'source': source, 'record_id': record_id})
        return jsonify({'success': True, 'images': saved, 'count': len(saved), 'async': True}), 201

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
        # 2026-09-19: 上传即异步跑 OCR + AI 比对(对齐出货)
        saved_item = _spawn_loading_record_image_processing(
            image_id, filepath, record, record['order_pk'], record_id,
            rel_path, original_name, img['sort_order'],
        )
        AuditLog.log('upload_image', 'loading_order', record['order_pk'],
                     detail={'filename': rel_path, 'source': source, 'record_id': record_id})
        return jsonify({'success': True, 'images': [saved_item], 'count': 1, 'async': True}), 201

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

    2026-08-19 改造:OCR + 背景色提取改用 loading_processor.extract_ocr 共享方法。
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

    extracted = loading_processor.extract_ocr(
        img['file_path'], record, image_id=image_id, use_cached=True)
    if not extracted['ocr_text'].strip():
        return jsonify({'success': False, 'error': 'OCR 无文字，无法匹配'}), 400

    status, score, reason = match_label_to_row(
        extracted['ocr_text'],
        record.get('product_name', ''),
        record.get('specification', ''))
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

    # 2026-08-19 改造:OCR + DeepSeek 调用 + ai_match 事件写库全部走 processor。
    # 装柜端点显式传 with_supplement=False 保留原行为(2026-07-30 起 loading 不注入
    # 自适应提示词)。
    extracted = loading_processor.extract_ocr(
        img['file_path'], record, image_id=image_id, use_cached=True)
    if not extracted['ocr_text'].strip():
        return jsonify({'success': False, 'error': 'OCR 无文字，无法判断'}), 400

    try:
        ds = get_ocr_engine('deepseek')
        if not getattr(ds, 'API_KEY', ''):
            return jsonify({'success': False, 'error': 'DeepSeek API Key 未配置'}), 503
    except Exception:
        return jsonify({'success': False, 'error': 'DeepSeek 引擎初始化失败'}), 503

    result = loading_processor.classify(
        extracted['ocr_text'], record, with_supplement=False)
    if not result['status']:
        return jsonify({'success': False, 'error': 'DeepSeek 返回异常'}), 502
    loading_processor.persist_match(
        image_id=image_id, ocr_text=extracted['ocr_text'],
        result=result, record=record, order_id=img['order_pk'],
        avg_conf=extracted.get('avg_conf', 1.0),
    )

    return jsonify({
        'success': True,
        'image_id': image_id,
        'match_status': result['status'],
        'match_score': result['score'],
        'reason': result['reason'],
        'match_source': result['source'],
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


# ── 摆放图 + 交互式点数清点(对齐出货页 placement 体系,库表用 loading_placement_marks) ──
def _compute_loading_placement_match(record_pk):
    """按装柜记录已上传的所有摆放图,计算支数+散码累计与目标是否吻合(供端点 mutation 即时回传)。"""
    rec = LoadingOrderRecord.get_by_id(record_pk)
    if not rec:
        return False
    images = LoadingPlacementImage.get_by_record(record_pk)
    total = 0
    loose = 0
    for im in images:
        eff = im['manual_count'] if im['manual_count'] is not None else im['n_marks']
        total += -eff if im.get('is_unload') else eff
        loose += im.get('loose_count') or 0
    remark = rec.get('remark') or ''
    exp_zhi, has_zhi = compute_placement_expected_zhi(
        remark, rec.get('quantity') or '', rec.get('unit') or ''
    )
    san_m = list(SANMA_RE.finditer(remark))
    exp_san = sum(int(x.group(1)) for x in san_m)
    has_san = len(san_m) > 0
    matched_zhi = (not has_zhi) or (abs(total - exp_zhi) <= 0.01)
    matched_san = (not has_san) or (abs(loose - exp_san) <= 0.01)
    return (has_zhi or has_san) and matched_zhi and matched_san


def _placement_match_response(img: dict):
    """placement 端点 mutation 后的标准回报字段(供 5 个端点末尾调用)。

    返回 dict,直接 merge 进 jsonify({...})。
    """
    record_pk = img.get('record_pk')
    return {
        'record_id': record_pk,
        'placement_match': _compute_loading_placement_match(record_pk) if record_pk else False,
    }


@bp.route('/api/v1/loading-orders/records/<int:record_id>/placement-images', methods=['POST'])
def api_v1_loading_orders_record_placement_upload(record_id):
    """上传装柜商品行的摆放图(多文件/单 base64),不触发 OCR,source='placement' → 201"""
    set_log_context(biz='loading', record_id=record_id)
    record = LoadingOrderRecord.get_by_id(record_id)
    if not record:
        return jsonify({'success': False, 'error': '记录不存在'}), 404
    order = LoadingOrder.get_by_id(record['order_pk'])
    if order and order.get('is_locked'):
        return jsonify({'success': False, 'error': '该订单已锁定，无法上传摆放图'}), 403

    upload_dir, month_str = _get_upload_dir()
    saved = []
    files = request.files.getlist('image')
    if files:
        for f in files:
            try:
                filepath, original_name = _save_one_uploaded_file(f, upload_dir)
            except ValueError as e:
                return jsonify({'success': False, 'error': str(e)}), 400
            rotate_deg = request.form.get('rotate_deg')
            try:
                filepath = apply_user_rotation(filepath, rotate_deg)
            except ValueError as e:
                return jsonify({'success': False, 'error': str(e)}), 400
            image_id = LoadingPlacementImage.create(
                order_pk=record['order_pk'], record_pk=record_id,
                file_path=filepath, original_name=original_name,
            )
            rel_path = os.path.join(month_str, os.path.basename(filepath)).replace('\\', '/')
            img = LoadingPlacementImage.get_by_id(image_id)
            saved.append({
                'image_id': image_id, 'image': rel_path, 'original_name': original_name,
                'sort_order': img['sort_order'], 'circles': [],
            })
    elif request.is_json:
        data = request.get_json() or {}
        try:
            filepath, original_name = _save_one_base64_image(data.get('image', ''), upload_dir)
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        rotate_deg = data.get('rotate_deg')
        try:
            filepath = apply_user_rotation(filepath, rotate_deg)
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        image_id = LoadingPlacementImage.create(
            order_pk=record['order_pk'], record_pk=record_id,
            file_path=filepath, original_name=original_name,
        )
        rel_path = os.path.join(month_str, os.path.basename(filepath)).replace('\\', '/')
        saved.append({
            'image_id': image_id, 'image': rel_path, 'original_name': original_name,
            'circles': [],
        })
    else:
        return jsonify({'success': False, 'error': '未提供图片'}), 400

    for s in saved:
        AuditLog.log('upload_placement_image', 'loading_order', record['order_pk'],
                     detail={'filename': s['image'], 'record_id': record_id})
    return jsonify({'success': True, 'images': saved, 'count': len(saved)}), 201


@bp.route('/api/v1/loading-orders/records/<int:record_id>/placement-images', methods=['GET'])
def api_v1_loading_orders_record_placement_list(record_id):
    """列出某装柜商品行所有摆放图(含计数点)。"""
    images = LoadingPlacementImage.get_by_record(record_id)
    return jsonify({'success': True, 'images': images})


@bp.route('/api/v1/loading-orders/placement-images/<int:image_id>', methods=['DELETE'])
def api_v1_loading_orders_placement_delete(image_id):
    """删除一张摆放图(连带计数点 + 物理文件)。"""
    ok = LoadingPlacementImage.delete(image_id)
    if not ok:
        return jsonify({'success': False, 'error': '摆放图不存在'}), 404
    return jsonify({'success': True})


@bp.route('/api/v1/loading-orders/placement-images/<int:image_id>', methods=['GET'])
def api_v1_loading_orders_placement_get(image_id):
    """获取单张摆放图(含计数点)。"""
    img = LoadingPlacementImage.get_by_id(image_id)
    if not img:
        return jsonify({'success': False, 'error': '摆放图不存在'}), 404
    return jsonify({'success': True, 'image': img})


@bp.route('/api/v1/loading-orders/placement-images/<int:image_id>/detect', methods=['POST'])
def api_v1_loading_orders_placement_detect(image_id):
    """重新用 OpenCV 检测圆柱端面(可反复重试以提高召回)。返回 circles。"""
    img = LoadingPlacementImage.get_by_id(image_id)
    if not img:
        return jsonify({'success': False, 'error': '摆放图不存在'}), 404
    circles = _detect_cylinder_circles(img['file_path'])
    LoadingPlacementImage.set_circles(image_id, circles)
    return jsonify({'success': True, 'circles': circles})


@bp.route('/api/v1/loading-orders/placement-images/<int:image_id>/mark-scale', methods=['POST'])
def api_v1_loading_orders_placement_mark_scale(image_id):
    """设置该摆放图计数数字的整体系缩放比例(弹框放大/缩小按钮)。body: {scale}。"""
    img = LoadingPlacementImage.get_by_id(image_id)
    if not img:
        return jsonify({'success': False, 'error': '摆放图不存在'}), 404
    data = request.get_json() or {}
    try:
        scale = float(data.get('scale', 1))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'scale 必须是数字'}), 400
    scale = LoadingPlacementImage.set_mark_scale(image_id, scale)
    return jsonify({'success': True, 'scale': scale, 'marks': LoadingPlacementImage.get_marks(image_id), **_placement_match_response(img)})


@bp.route('/api/v1/loading-orders/placement-images/<int:image_id>/loose-count', methods=['POST'])
def api_v1_loading_orders_placement_loose_count(image_id):
    """设置该摆放图的散码数量(点数弹框内「散码」按钮录入)。body: {count}。"""
    img = LoadingPlacementImage.get_by_id(image_id)
    if not img:
        return jsonify({'success': False, 'error': '摆放图不存在'}), 404
    data = request.get_json() or {}
    try:
        count = int(round(float(data.get('count', 0))))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'count 必须是数字'}), 400
    count = LoadingPlacementImage.set_loose_count(image_id, count)
    return jsonify({'success': True, 'count': count, **_placement_match_response(img)})


@bp.route('/api/v1/loading-orders/placement-images/<int:image_id>/manual-count', methods=['POST'])
def api_v1_loading_orders_placement_manual_count(image_id):
    """设置该摆放图直接输入的支数(点数弹框内「输入支数」按钮录入)。body: {count}。

    count 为 null / 空字符串 → 清除直接输入,回退到点击计数(n_marks);
    为整数(含 0) → 以该值为准,并清空点击计数点(二者互斥)。
    """
    img = LoadingPlacementImage.get_by_id(image_id)
    if not img:
        return jsonify({'success': False, 'error': '摆放图不存在'}), 404
    data = request.get_json() or {}
    raw = data.get('count')
    if raw is None or raw == '':
        count = None
    else:
        try:
            count = int(round(float(raw)))
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'count 必须是数字'}), 400
    count = LoadingPlacementImage.set_manual_count(image_id, count)
    return jsonify({'success': True, 'manual_count': count, **_placement_match_response(img)})


@bp.route('/api/v1/loading-orders/placement-images/<int:image_id>/marks', methods=['POST'])
def api_v1_loading_orders_placement_mark_add(image_id):
    """在摆放图上点一枚计数点。body: {x_ratio, y_ratio, r}(0~1)。返回最新计数点列表。"""
    img = LoadingPlacementImage.get_by_id(image_id)
    if not img:
        return jsonify({'success': False, 'error': '摆放图不存在'}), 404
    data = request.get_json() or {}
    try:
        x = float(data.get('x_ratio'))
        y = float(data.get('y_ratio'))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'x_ratio/y_ratio 必须是数字'}), 400
    if not (0 <= x <= 1) or not (0 <= y <= 1):
        return jsonify({'success': False, 'error': 'x_ratio/y_ratio 需在 0~1 区间'}), 400
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
    LoadingPlacementImage.add_mark(image_id, x, y, mark_r=r)
    return jsonify({'success': True, 'marks': LoadingPlacementImage.get_marks(image_id), **_placement_match_response(img)})


@bp.route('/api/v1/loading-orders/placement-images/<int:image_id>/marks/last', methods=['DELETE'])
def api_v1_loading_orders_placement_mark_undo(image_id):
    """撤销最近一枚计数点(连续可撤销)。返回剩余计数点列表。"""
    img = LoadingPlacementImage.get_by_id(image_id)
    if not img:
        return jsonify({'success': False, 'error': '摆放图不存在'}), 404
    marks = LoadingPlacementImage.delete_last_mark(image_id)
    return jsonify({'success': True, 'marks': marks, **_placement_match_response(img)})


# ─────────────────────────────────────────────────────────
# Task 4 (2026-09-09): 装柜 record 级别拷贝纸/日本纸 上传端点(对齐出货 9/9)
# ─────────────────────────────────────────────────────────

@bp.route('/api/v1/loading-orders/records/<int:rid>/copy-paper-images', methods=['POST'])
def api_v1_loading_orders_record_copy_paper_upload(rid):
    """上传装柜商品行的拷贝纸/日本纸 标签照 → 写 loading_order_images(source='copy_paper_label')。

    完全跳过 OCR pipeline(不做 OCR/AI 比对,仅供人工留档)。
    支持 multipart(字段名 `image`)与 JSON base64(`image` 字段)。
    锁单 → 403;记录不存在 → 404;无图 → 400。
    """
    set_log_context(biz='loading', record_id=rid, evt_src='copy_paper_upload')
    record = LoadingOrderRecord.get_by_id(rid)
    if not record:
        return jsonify({'success': False, 'error': '记录不存在'}), 404
    order = LoadingOrder.get_by_id(record['order_pk'])
    if order and order.get('is_locked'):
        return jsonify({'success': False, 'error': '该订单已锁定,无法上传拷贝纸/日本纸'}), 403

    upload_dir, month_str = _get_upload_dir()

    if request.is_json:
        _payload = request.get_json(silent=True) or {}
        source = (_payload.get('source') or '').strip()
    else:
        source = (request.form.get('source') or '').strip()
    if source not in ('', 'label'):
        return jsonify({'success': False, 'error': 'source 只支持 label'}), 400

    files = request.files.getlist('image') or request.files.getlist('file')
    if files:
        try:
            filepath, original_name = _save_one_uploaded_file(files[0], upload_dir)
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
    elif request.is_json:
        _payload = request.get_json(silent=True) or {}
        try:
            filepath, original_name = _save_one_base64_image(
                _payload.get('image', ''), upload_dir)
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
    else:
        return jsonify({'success': False, 'error': '未提供图片(image)'}), 400

    new_id = LoadingOrderImage.create(
        record['order_pk'], filepath, original_name,
        source=COPY_PAPER_LABEL_SOURCE, record_pk=rid)
    img = LoadingOrderImage.get_by_id(new_id)
    rel_path = os.path.join(month_str, os.path.basename(filepath)).replace('\\', '/')
    img['file_path'] = filepath
    img['relative_path'] = rel_path
    AuditLog.log('upload_copy_paper_image', 'loading_order', record['order_pk'],
                 detail={'filename': rel_path,
                         'source': COPY_PAPER_LABEL_SOURCE, 'record_id': rid})
    return jsonify({'success': True, 'image': img})


@bp.route('/api/v1/loading-orders/placement-images/<int:image_id>/unload', methods=['POST'])
def api_v1_loading_orders_placement_unload(image_id):
    """勾选/取消「卸载货物」:持久化到库并即时重算该记录的比对角标。body: {unload}。"""
    img = LoadingPlacementImage.get_by_id(image_id)
    if not img:
        return jsonify({'success': False, 'error': '摆放图不存在'}), 404
    data = request.get_json() or {}
    flag = bool(data.get('unload', False))
    LoadingPlacementImage.set_unload(image_id, flag)
    record_pk = img.get('record_pk')
    pm = _compute_loading_placement_match(record_pk) if record_pk is not None else False
    return jsonify({'success': True, 'is_unload': flag, 'placement_match': pm, 'record_id': record_pk})


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


# 2026-09-09: 之前的 _migration_site1/2/3 三个 stub 已删除 —
# 数点匹配逻辑已合并到 /loading-orders 主 handler 的 record 富集循环里,
# 摆放图渲染仍由前端 placementImageUploaded → refreshRecordBlock(GET /placement-images) 驱动,
# 无需 server-side placement_groups 预渲染。


# 2026-09-21: 装柜 YPP 核查页 — 与出货/入库同源端点,只换 biz= 参数
# 端点统一在 shipping.py:/api/v1/shipping-orders/ypp-review/scan (接受 biz=shipping|inbound|loading)
@bp.route('/loading-ypp-review')
def loading_ypp_review_page():
    """装柜 YPP 规则冲突核查页(单页手动扫描,不受日期过滤影响)。"""
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
        'loading_ypp_review.html',
        ypp_config_count=ypp_config_count,
    )
