"""出货记录蓝图。

包含：
- /shipping-records 页面（带日期过滤、商品单位提示计算）
- /api/v1/shipping-orders/* REST API（订单/明细/图片 CRUD + 移动 + AI 识别）
"""
import os
import uuid
import base64
import re
import json
import threading
import contextvars
import logging
import time
from datetime import date as date_cls, timedelta, datetime
from flask import Blueprint, render_template, request, jsonify, current_app, session
from models import (
    ShippingOrder, ShippingRecord, ShippingImage, ProductUnit, PieceConversion, AuditLog,
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
)
from blueprints.ocr_engine import PaddleOCREngine, get_ocr_engine, OCR_MATCH_PROMPT_VERSION
from blueprints import _helpers
from models._db import get_db
from blueprints.ocr_log import set_log_context


bp = Blueprint('shipping', __name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

logger = logging.getLogger(__name__)

# ── 行级图异步 OCR+AI 处理 ─────────────────────────────────────────────
# 单用户本地部署，用进程内存登记任务进度即可，无需 Redis/Celery。
# _ASYNC_JOBS[image_id] = {'state': 'processing'|'done'|'error', 'started': ts, 'finished': ts}
_ASYNC_JOBS = {}

# PaddleOCR 推理与 DeepSeek 调用都不是严格线程安全的，且 API 有速率限制，
# 统一用一把锁串行化"抽字→比对"过程，避免多图并发上传时互相踩踏。
_OCR_LOCK = threading.Lock()


def _process_record_image_async(image_id, filepath, record, order_id, record_id):
    """后台线程：对单张行级图跑 OCR + 背景色 + AI 比对，并把结果写库。

    调用方(上传端点)在请求线程里完成图片落盘后立即返回 processing:true，
    真正的重活(2~4s PaddleOCR + 可能的 DeepSeek 联网)在这里异步完成，
    前端通过 /images/<id>/match-status 轮询结果并局部刷新。
    """
    try:
        # 仅"抽字→比对"受锁保护；DB 写库在锁外执行，缩短临界区。
        with _OCR_LOCK:
            # 1) PaddleOCR 抽字(单次)
            ocr_text = ''
            try:
                with open(filepath, 'rb') as _f:
                    ocr_text = get_ocr_engine('paddleocr').extract_text(_f.read()) or ''
            except Exception:
                logger.exception('记录 OCR 文本失败(不阻断): %s', filepath)
            # 1.5) 图片背景色检测:对标签不写颜色的品类(如磅布三文治),从背景推断实物颜色
            bg = detect_bg_color(filepath)
            if bg and ocr_text.strip():
                ocr_text = ocr_text + '\n[标签背景: ' + ('黑色' if bg == 'black' else '白色') + ']'
            if bg:
                try:
                    ShippingImage.set_bg_color(image_id, bg)
                except Exception:
                    logger.exception('set_bg_color 失败(不阻断) image_id=%s', image_id)
            # 2) 走新流程:DeepSeek 优先 → fallback 本地
            result = _classify_and_match_image(filepath, record, ocr_text)
        status, score, reason, source_label = (
            result['status'], result['score'], result['reason'], result['source']
        )
        if status:
            ShippingImage.set_match(image_id, status, score, reason, source=source_label)
        # 新增:append-only record_ocr 事件(写入失败不阻断主流程)
        try:
            OcrMatchEvent.create(
                'record_ocr', record_id=record_id, order_id=order_id, image_id=image_id,
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
            logger.exception('record_ocr 事件写库失败(不阻断)')
        # 若 DeepSeek 参与,额外写入 ai_match 事件保留提示词+原始返回(供详情审计)
        if source_label == 'deepseek' and result.get('prompt_text'):
            try:
                OcrMatchEvent.create(
                    'ai_match',
                    record_id=record_id,
                    order_id=order_id,
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
                logger.exception('ai_match 事件写库失败(不阻断)')
        _ASYNC_JOBS[image_id] = {'state': 'done', 'finished': time.time()}
    except Exception:
        logger.exception('行级图异步 OCR+AI 处理失败 image_id=%s', image_id)
        _ASYNC_JOBS[image_id] = {'state': 'error', 'finished': time.time(), 'error': 'processing failed'}


def _spawn_record_image_processing(image_id, filepath, record, order_id, record_id,
                                    rel_path, original_name, sort_order):
    """登记任务并启动后台线程，返回"处理中"图片字典(供上传端点立即返回)。"""
    _ASYNC_JOBS[image_id] = {'state': 'processing', 'started': time.time()}
    # threading.Thread 不会自动传 contextvars —— 必须显式 copy_context().run(...)
    # 把当前请求的 trace_id + 业务上下文带进后台线程,否则 OCR 日志全显示 '-'
    ctx = contextvars.copy_context()
    t = threading.Thread(
        target=ctx.run,
        args=(_process_record_image_async, image_id, filepath, record, order_id, record_id),
        daemon=True,
    )
    t.start()
    return {
        'image_id': image_id,
        'image': rel_path,
        'original_name': original_name,
        'sort_order': sort_order,
        'processing': True,          # 前端据此显示"识别中"并启动轮询
        'match_status': None,
        'match_score': None,
        'reason': '',
        'match_source': None,
        'bg_color': None,
    }


def _run_label_match(image_abspath, record, ocr_text=''):
    """对一张行级图跑本地匹配 (RapidFuzz)，返回 (status, score, reason)。

    2026-07-30 改造:本函数只负责【本地 fallback】逻辑(DeepSeek 调用由
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
        logger.exception('行级图片本地匹配失败（不阻断上传）')
        return '', None, ''


def _classify_and_match_image(filepath, record, ocr_text):
    """2026-07-30 改造:对一张图先 OCR → 优先调 DeepSeek 单 record 比对 → fallback 本地 RapidFuzz。
    2026-07-31:增加 prompt_text / raw_response 返回,供 upload 流程写入 ai_match 事件。

    Returns:
        {
            'status': 'green' | 'yellow' | 'red' | '',  # 空 = OCR 无文字,不打徽章
            'score':  float | None,
            'reason': str,
            'source': 'deepseek' | 'local_fuzzy' | '',
            'prompt_text': str,   # DeepSeek 时有效,调用方写 ai_match 事件用
            'raw_response': str,  # DeepSeek 时有效
        }
    """
    # 1. OCR 文本为空 → 跳过所有引擎,不打徽章(语义:没证据 ≠ 不符)
    if not (ocr_text or '').strip():
        return {'status': '', 'score': None, 'reason': '', 'source': '', 'prompt_text': '', 'raw_response': ''}

    # 2. 优先调 DeepSeek(联网时)
    try:
        ds = get_ocr_engine('deepseek')
        if getattr(ds, 'API_KEY', ''):  # 有 key 才联网
            res = ds.compare_single_record(ocr_text, record)
            ms = (res.get('match_status') or '').lower()
            if ms in ('green', 'yellow', 'red'):
                return {'status': ms, 'score': res.get('score'),
                        'reason': res.get('reason') or '', 'source': 'deepseek',
                        'prompt_text': res.get('prompt_text', ''),
                        'raw_response': res.get('raw_response', '')}
            # ms 不是合法状态:fallback 到本地(LLM 异常返回)
            logger.warning('DeepSeek compare_single_record 返回非合法状态: %r, 改走本地', ms)
        else:
            logger.info('DeepSeek API key 未配置, 行级图上传走本地 RapidFuzz')
    except Exception as e:
        # 联网但调用失败(超时/网络/配额):fallback 本地,并标本地补
        logger.warning('DeepSeek 单 record 比对失败, fall back 本地: %s', e)

    # 3. fallback:本地 RapidFuzz
    status, score, reason = _run_label_match(filepath, record, ocr_text=ocr_text)
    return {'status': status, 'score': score, 'reason': reason, 'source': 'local_fuzzy',
            'prompt_text': '', 'raw_response': ''}


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
    # 2026-08-04:match-col 服务端不再渲染,改由 JS 在首次有匹配结果时通过
    # `_ensureMatchColumn` 动态插入。所以这里不再算 record_worst_*_map /
    # record_human_verified_map / has_match —— 那块逻辑下沉到 JS。
    for grp in groups:
        all_record_imgs = []
        for rec in grp.get('records', []):
            record_imgs = ShippingImage.get_by_record(rec['id'])
            all_record_imgs.extend(record_imgs)
            # 2026-07-27:行级有图 → 模板渲染 🖼️ 按钮加红框 has-image class(刷新不丢)
            rec['has_image'] = bool(record_imgs)
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
            # 2026-07-27:每条明细的 per-rule 核查状态(目前只用到 b_white_300g),
            # 模板里 addRowWarning 据此决定每条警告单独显示/隐藏 + 渲染 ✓/✗ 按钮
            item['verified_warnings'] = ShippingRecord.get_verified_warnings(item['id'])
        group.update(summarize_remarks(group['records']))
        group['has_eco'] = any('环保' in r.get('product_name', '') for r in group['records'])
        group['has_jia_mian'] = any(
            '杂胶' in r.get('product_name', '')
            and '加面' in r.get('specification', '')
            for r in group['records']
        )
        # 2026-08-04:has_match 服务端不再需要 —— match-col 由 JS 动态插入,首次
        # 有匹配结果时整列首次可见,所以「整组无匹配就隐藏整列」的 no-match 思路废弃。

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
            # 只有「换组」才需要重排单号:同组内(日期+客户都没变)保存备注时若照样取 MAX+1,
            # 会把本单单号顶到组尾,用户看到的表现是「改个备注单号就跳了」。
            if new_date == (order.get('date') or '') and new_customer == (order.get('customer') or ''):
                new_order_num = order.get('order_num')
            else:
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
    # 走 create_many:单事务批量插入(中途失败整批回滚,不留半单),
    # 内部统一做 '码'→'y' 归一化、数量 int/str 兼容、空品名或空数量计入 skipped。
    try:
        result = ShippingRecord.create_many(order_id, items)
    except Exception:
        current_app.logger.exception('批量添加明细失败: order_id=%s', order_id)
        return jsonify({'success': False, 'error': '批量添加失败，已整批回滚'}), 500
    records = result['records']
    skipped = result['skipped']

    # 一条都没成功 → 400:让前端知道这次提交完全无效,而不是收一个 201 空响应当成功
    if not records:
        return jsonify({'success': False, 'error': '没有有效明细（品名与数量均不能为空）',
                        'count': 0, 'skipped': skipped, 'records': []}), 400

    AuditLog.log('batch_add_records', 'shipping_order', order_id,
                 detail={'count': len(records), 'skipped': skipped})
    return jsonify({'success': True, 'count': len(records),
                    'skipped': skipped, 'records': records}), 201


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

    # 先清该明细的行级图片,再删明细 —— 顺序不能反:
    # 明细一旦删掉,其 record_pk 图片就成孤儿(页面按现存 record 循环取图不再显示它们,
    # 而订单又因"仍有图片"删不掉)。
    removed = ShippingImage.delete_by_record(record_id)
    ShippingRecord.delete(record_id)
    AuditLog.log('delete_record', 'shipping_order', record['order_pk'], detail={'record_id': record_id, 'product': record.get('product_name', ''), 'removed_images': len(removed)})
    return jsonify({'success': True})


@bp.route('/api/v1/shipping-orders/<int:order_id>/records', methods=['DELETE'])
def api_v1_shipping_orders_delete_all_records(order_id):
    """删除出货单所有明细 → 200"""
    order = ShippingOrder.get_by_id(order_id)
    if not order:
        return jsonify({'success': False, 'error': '订单不存在'}), 404
    if order.get('is_locked'):
        return jsonify({'success': False, 'error': '该订单已锁定，无法操作'}), 403

    # 明细全清 → 其行级图片一并清理;订单级共享图(record_pk IS NULL)保留,
    # 因为订单外壳还在,那些图不属于任何一条明细。
    removed = ShippingImage.delete_record_images_by_order(order_id)
    ShippingRecord.delete_by_order(order_id)
    AuditLog.log('delete_all_records', 'shipping_order', order_id, detail={'removed_images': len(removed)})
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

    # 移动端拍照方向修正：按 rotate_deg 旋转已落盘图片，非法值 400
    rotate_deg = request.form.get('rotate_deg') or (data.get('rotate_deg') if request.is_json else None)
    try:
        filepath = apply_user_rotation(filepath, rotate_deg)
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400

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
    verified = _as_bool(data.get('verified'), default=True)

    # 新增:append-only human_verify 事件(先读 AI 当前状态快照)
    # 写入失败不阻断主流程 — 与 AuditLog 哲学一致
    try:
        ai_snapshot = ShippingImage.get_by_id(image_id)
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

    ShippingImage.set_human_verified(image_id, verified)
    AuditLog.log(
        'manual_verify_image', 'shipping_order', img['order_pk'],
        detail={'image_id': image_id, 'human_verified': 1 if verified else 0},
    )
    return jsonify({'success': True, 'image_id': image_id, 'human_verified': verified})


@bp.route('/api/v1/shipping-orders/images/<int:image_id>/fuzzy-match', methods=['POST'])
def api_v1_shipping_orders_fuzzy_match_image(image_id):
    """对已上传图片重新运行本地 RapidFuzz 模糊匹配 → 返回新的判别结果。
    
    流程: 取图→取关联商品行→取OCR文字→运行 match_label_to_row→更新 shipping_images→返回
    """
    img = ShippingImage.get_by_id(image_id)
    if not img:
        return jsonify({'success': False, 'error': '图片不存在'}), 404
    order = ShippingOrder.get_by_id(img['order_pk'])
    if order and order.get('is_locked'):
        return jsonify({'success': False, 'error': '订单已锁定，无法操作'}), 403
    record_pk = img.get('record_pk')
    if not record_pk:
        return jsonify({'success': False, 'error': '该图片未关联商品行'}), 400
    record = ShippingRecord.get_by_id(record_pk)
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
    ShippingImage.set_match(image_id, status, score or 0, reason, source='local_fuzzy')

    return jsonify({
        'success': True,
        'image_id': image_id,
        'match_status': status,
        'match_score': score,
        'reason': reason,
        'match_source': 'local_fuzzy',
    })


@bp.route('/api/v1/shipping-orders/images/<int:image_id>/ai-judge', methods=['POST'])
def api_v1_shipping_orders_ai_judge_image(image_id):
    """对已上传图片重新运行 DeepSeek AI 比对 → 返回新的判别结果。

    流程: 取图→取关联商品行→取OCR文字→调用 DeepSeek compare_single_record→更新 shipping_images→返回
    """
    set_log_context(biz='shipping', image_id=image_id)
    img = ShippingImage.get_by_id(image_id)
    if not img:
        return jsonify({'success': False, 'error': '图片不存在'}), 404
    order = ShippingOrder.get_by_id(img['order_pk'])
    if order and order.get('is_locked'):
        return jsonify({'success': False, 'error': '订单已锁定，无法操作'}), 403
    record_pk = img.get('record_pk')
    if not record_pk:
        return jsonify({'success': False, 'error': '该图片未关联商品行'}), 400
    record = ShippingRecord.get_by_id(record_pk)
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
            with open(img['file_path'], 'rb') as _f:
                ocr_text = get_ocr_engine('paddleocr').extract_text(_f.read()) or ''
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
    ShippingImage.set_match(image_id, status, score or 0, reason, source=source_label)

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


# ── 商品行级图片(挂在 record 上,合并订单级共享图展示) ──
def _save_one_uploaded_file(file, upload_dir):
    """保存一个上传文件到 upload_dir,返回 (filepath, original_name)。失败抛 ValueError。"""
    if not file or not file.filename:
        raise ValueError('未选择文件')
    ext = check_uploaded_image(file)  # 契约:返回值【自带前导点】,如 '.png'
    # 兼容两种写法,避免拼出 'xxx..png' 这种双点文件名
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


@bp.route('/api/v1/shipping-orders/records/<int:record_id>/images', methods=['POST'])
def api_v1_shipping_orders_record_upload_images(record_id):
    """上传商品行图片(多文件),按上传顺序赋 sort_order → 201"""
    set_log_context(biz='shipping', record_id=record_id)
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
            # 移动端拍照方向修正：按 rotate_deg 旋转已落盘图片，非法值 400
            rotate_deg = request.form.get('rotate_deg') or (data.get('rotate_deg') if request.is_json else None)
            try:
                filepath = apply_user_rotation(filepath, rotate_deg)
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
            # 落盘即返回:真正的 OCR + AI 比对放到后台线程异步跑(见 _process_record_image_async)
            saved.append(_spawn_record_image_processing(
                image_id, filepath, record, record['order_pk'], record_id,
                rel_path, original_name, img['sort_order'],
            ))
        for s in saved:
            AuditLog.log('upload_image', 'shipping_order', record['order_pk'],
                         detail={'filename': s['image'], 'source': source, 'record_id': record_id})
        return jsonify({'success': True, 'images': saved, 'count': len(saved), 'async': True}), 201

    # 单文件 base64(JSON)
    if request.is_json:
        data = request.get_json() or {}
        try:
            filepath, original_name = _save_one_base64_image(data.get('image', ''), upload_dir)
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        # 移动端拍照方向修正：按 rotate_deg 旋转已落盘图片，非法值 400
        rotate_deg = request.form.get('rotate_deg') or data.get('rotate_deg')
        try:
            filepath = apply_user_rotation(filepath, rotate_deg)
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
        # 落盘即返回:OCR + AI 比对放到后台线程异步跑
        saved = [_spawn_record_image_processing(
            image_id, filepath, record, record['order_pk'], record_id,
            rel_path, original_name, img['sort_order'],
        )]
        AuditLog.log('upload_image', 'shipping_order', record['order_pk'],
                     detail={'filename': rel_path, 'source': source, 'record_id': record_id})
        return jsonify({'success': True, 'images': saved, 'count': 1, 'async': True}), 201

    return jsonify({'success': False, 'error': '未提供图片'}), 400


@bp.route('/api/v1/shipping-orders/images/<int:image_id>/match-status', methods=['GET'])
def api_v1_shipping_orders_image_match_status(image_id):
    """查询行级图 OCR+AI 异步处理的进度。

    前端上传后立即拿到 processing:true,随后轮询本端点:
      - processing:true  → 仍在后台跑
      - processing:false → 已结束,image 里带最新 match_status / match_source / bg_color
    超时保护:任务登记超过 150s 仍未标记完成(线程异常/重启),直接回落到 DB 真实状态。
    """
    job = _ASYNC_JOBS.get(image_id)
    if job and job.get('state') == 'processing' and (time.time() - job.get('started', 0)) < 150:
        return jsonify({'success': True, 'processing': True})
    img = ShippingImage.get_by_id(image_id)
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


@bp.route('/api/v1/shipping-orders/records/<int:record_id>/verify-warning', methods=['POST'])
def api_v1_shipping_orders_record_verify_warning(record_id):
    """切换某条规则(目前用到 b_white_300g)的核查状态。

    Body: {rule_id, verified}
    - verified=true → 把 rule_id 写入 verified_warnings(JSON),警告折叠
    - verified=false → 从 verified_warnings 移除 rule_id,警告重新弹出
    与全局 verified 字段并存:verified=1 仍代表"整行所有警告已核查"。
    """
    record = ShippingRecord.get_by_id(record_id)
    if not record:
        return jsonify({'success': False, 'error': '记录不存在'}), 404
    data = request.get_json() or {}
    rule_id = (data.get('rule_id') or '').strip()
    verified = bool(data.get('verified'))
    if not rule_id:
        return jsonify({'success': False, 'error': 'rule_id 不能为空'}), 400
    current = ShippingRecord.set_verified_warning(record_id, rule_id, verified)
    AuditLog.log(
        'verify_warning' if verified else 'unverify_warning',
        'shipping_record',
        record_id,
        detail={'rule_id': rule_id}
    )
    return jsonify({'success': True, 'verified_warnings': current, 'rule_id': rule_id, 'verified': verified})



# ── AI 图片识别（双引擎：Moonshot + PaddleOCR） ──────────
@bp.route('/api/v1/shipping-orders/ai-recognize', methods=['POST'])
def shipping_records_ai_recognize():
    """接收图片，调用 OCR 引擎识别商品表格，返回 JSON。

    Query/Form 参数: engine=moonshot|paddleocr（默认: .env OCR_BACKEND 或 moonshot）
    """
    set_log_context(biz='shipping', evt_src='ai_recognize')
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

    # 扩展名 + 大小校验(与行级上传同一把尺子)。
    # 不校验就直接 read() 的后果:.txt 之类非图会一路送进 OCR 引擎,
    # 引擎内部抛错后被兜成 503「引擎不可用」—— 明明是用户传错文件,却报成服务故障。
    try:
        check_uploaded_image(file)
    except ValueError as e:
        return jsonify({
            'success': False,
            'error': str(e),
            'hint': '请上传 10MB 以内的 png/jpg/jpeg/gif/webp 图片'
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
    set_log_context(biz='shipping', order_id=order_id)
    order = ShippingOrder.get_by_id(order_id)
    if not order:
        return jsonify({'success': False, 'error': '订单不存在'}), 404
    # 锁定单不允许 AI 改写匹配状态 —— 闸门放在 OCR 之前,避免白跑一趟识别
    if order.get('is_locked'):
        return jsonify({'success': False, 'error': '该订单已锁定，无法执行 AI 匹配'}), 403

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

    # 汇总该单所有行级图片的 OCR 文字（单例引擎，与 _run_label_match / 事件记录共用同一实例）
    paddle = get_ocr_engine('paddleocr')
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

    # ── 2026-07-30 自适应提示词:把同品类的 layer2 + 同规格的 layer3 拼到 prompt 末尾 ──
    augmented_rows = []
    for r in rows:
        cls = classify_record(product_name=r['product_name'], specification=r['specification'])
        augmented_rows.append({
            **r,
            '_supplement': CategoryPrompt.compose_for_record(
                category_code=cls['category_code'] if cls else None,
                product_name=r['product_name'],
                specification=r['specification'],
            ),
        })

    try:
        engine = get_ocr_engine('deepseek')
        ai_result = engine.compare_rows(ocr_text, augmented_rows)
        verdicts = ai_result.get('verdicts', []) if isinstance(ai_result, dict) else ai_result
        prompt_payload_text = ai_result.get('prompt', '') if isinstance(ai_result, dict) else ''
    except Exception as e:
        current_app.logger.exception('DeepSeek 比对失败: %s', e)
        return jsonify({'success': False, 'error': f'AI 比对失败:{type(e).__name__}'}), 500

    _SCORE = {'green': 90.0, 'yellow': 70.0, 'red': 30.0}
    # 只认属于本单的 record_id:LLM 可能幻觉出别单的 id,
    # 直接照单回写会越权改写他人订单的匹配状态。
    own_record_ids = {r['id'] for r in records}
    results = []
    skipped_foreign = 0
    for v in verdicts:
        rid = v.get('record_id')
        try:
            rid = int(rid)
        except (TypeError, ValueError):
            skipped_foreign += 1
            continue
        if rid not in own_record_ids:
            skipped_foreign += 1
            current_app.logger.warning(
                'ai-match 跳过不属于本单的 record_id=%s (order_id=%s)', rid, order_id)
            continue
        raw_status = v.get('match_status', 'red')
        # Normalize: lowercase + small synonym map (LLMs drift on casing/abbrev)
        status = {'g': 'green', 'y': 'yellow', 'r': 'red',
                  'green': 'green', 'yellow': 'yellow', 'red': 'red',
                  'ok': 'green', 'yes': 'green', 'no': 'red', 'unknown': 'yellow'}.get(
            str(raw_status).strip().lower(), 'red')
        score = _SCORE.get(status, 30.0)
        reason = v.get('reason', '') or ''
        for img in ShippingImage.get_by_record(rid):
            ShippingImage.set_match(img['id'], status, score, reason, source='deepseek')
            # 重跑 AI = 新一轮裁决,旧的人工确认作废 —— 否则上一轮被人工"放行"的红牌
            # 会继续顶着已核查的绿色外观,新裁决的风险就被静默吞掉了。
            ShippingImage.set_human_verified(img['id'], False)
        results.append({'record_id': rid, 'match_status': status, 'match_score': score,
                        'reason': reason})
        # append-only ai_match 事件(每个 record 一条;image_id 留空,代表聚合 OCR 比对结果)
        try:
            rec_lookup = {r['id']: r for r in records}
            rec = rec_lookup.get(rid, {})
            OcrMatchEvent.create(
                'ai_match',
                record_id=rid, order_id=order_id, image_id=None,
                ocr_text=ocr_text,
                ocr_engine='paddleocr',
                product_name=rec.get('product_name', ''),
                specification=rec.get('specification', ''),
                prompt_payload=prompt_payload_text or None,
                ai_match_status=status,
                ai_match_score=score,
                ai_match_reason=reason or None,
                ai_raw_response=json.dumps(v, ensure_ascii=False) if v else None,
                ai_engine='deepseek',
                prompt_version=OCR_MATCH_PROMPT_VERSION,
            )
        except Exception:
            # OcrMatchEvent.create 内部已 try/except,这里再兜一层,绝不让事件写库失败影响主流程
            current_app.logger.exception('ai_match 事件写库失败 (order=%s, record=%s)', order_id, rid)

    g = sum(1 for r in results if r['match_status'] == 'green')
    y = sum(1 for r in results if r['match_status'] == 'yellow')
    rd = sum(1 for r in results if r['match_status'] == 'red')
    summary = f'{len(results)} 行已核对:{g} ✓ / {y} ⚠ / {rd} ✗'
    return jsonify({'success': True, 'results': results, 'summary': summary})


# ────────────────────────────────────────────────────────────────────
# 类别/规格自适应提示词 (CategoryPrompt) API — 2026-07-30 新增
# 设计:在出货页对黄/红图点"✓ 确认通过"后,图下方按钮调 /generate-prompt-suggestion
# 生成模板化提示词草稿,用户编辑后调 /category-prompts (POST) 保存。下次同 record 比对自动注入。
# ────────────────────────────────────────────────────────────────────

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


@bp.route('/api/v1/shipping-orders/images/<int:image_id>/generate-prompt-suggestion', methods=['POST'])
def api_v1_shipping_orders_generate_prompt_suggestion(image_id):
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
    img = ShippingImage.get_by_id(image_id)
    if not img:
        return jsonify({'success': False, 'error': '图片不存在'}), 404
    record = None
    if img.get('record_pk'):
        record = ShippingRecord.get_by_id(img['record_pk'])
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
    # 溯源链:找这张图最近一条 human_verify 事件,把 id 透传给前端的 category-prompts 保存请求
    # 之前写 img.get('_source_event_id') 是误读 — shipping_images 没有该列,永远 None
    hv_event = OcrMatchEvent.get_latest_by_image(image_id, 'human_verify')
    source_event_id = hv_event['id'] if hv_event else None
    return jsonify({
        'success': True,
        'suggestion': {
            'prompt_text': suggestion_text,
            'scope': 'category',
            'category_code': cc,
            'product_name_keyword': pn or None,
            'spec_pattern': '',
            'source_event_id': source_event_id,
            'source_ocr_text': (img.get('ocr_text') or '')[:200],
            'source_ai_status': img.get('match_status') or '',
            'source_human_status': 'green',
            'existing_prompts': existing,  # {category_prompts: [...], spec_prompts: [...]}
        }
    })


@bp.route('/api/v1/shipping-orders/images/<int:image_id>/ocr-detail', methods=['GET'])
def api_v1_shipping_orders_image_ocr_detail(image_id):
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
    img = ShippingImage.get_by_id(image_id)
    if not img:
        return jsonify({'success': False, 'error': '图片不存在'}), 404
    record = None
    if img.get('record_pk'):
        record = ShippingRecord.get_by_id(img['record_pk'])

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


@bp.route('/api/v1/category-prompts', methods=['POST'])
def api_v1_category_prompts_create():
    """保存用户编辑后的提示词 → 写入 category_prompts 表(scope='category'|'spec')。

    Body 必填 scope + prompt_text;category / spec 分类按 scope 校验。
    """
    data = request.get_json() or {}
    scope = data.get('scope')
    prompt_text = (data.get('prompt_text') or '').strip()
    if scope not in ('category', 'spec'):
        return jsonify({'success': False, 'error': 'scope 必须是 category 或 spec'}), 400
    if not prompt_text:
        return jsonify({'success': False, 'error': 'prompt_text 不能为空'}), 400
    if scope == 'spec':
        if not data.get('category_code') or not data.get('spec_pattern'):
            return jsonify({'success': False, 'error': 'spec scope 必须有 category_code + spec_pattern'}), 400
    if scope == 'category' and not (data.get('product_name_keyword') or data.get('category_code')):
        return jsonify({'success': False, 'error': 'category scope 必须有 product_name_keyword 或 category_code'}), 400

    pid = CategoryPrompt.create(
        scope=scope,
        prompt_text=prompt_text,
        category_code=data.get('category_code') or None,
        spec_pattern=data.get('spec_pattern') or None,
        product_name_keyword=data.get('product_name_keyword') or None,
        source_event_id=data.get('source_event_id') or None,
        source_ocr_text=data.get('source_ocr_text') or None,
        source_ai_status=data.get('source_ai_status') or None,
        source_human_status=data.get('source_human_status') or None,
        status='active',
    )
    AuditLog.log('create_category_prompt', 'category_prompt', pid,
                 detail={'scope': scope, 'category_code': data.get('category_code'),
                         'spec_pattern': data.get('spec_pattern')})
    return jsonify({'success': True, 'id': pid})


@bp.route('/api/v1/category-prompts', methods=['GET'])
def api_v1_category_prompts_list():
    """列出当前活跃提示词(管理 UI 用),可选 scope 过滤。"""
    scope = request.args.get('scope')
    rows = CategoryPrompt.list_active(scope=scope if scope in ('category', 'spec') else None)
    return jsonify({'success': True, 'rows': rows})


@bp.route('/api/v1/category-prompts/<int:prompt_id>', methods=['DELETE'])
def api_v1_category_prompts_delete(prompt_id):
    """软删:把 status 改为 archived,不影响历史拼装记录(只不再注入)。"""
    if CategoryPrompt.archive(prompt_id):
        AuditLog.log('archive_category_prompt', 'category_prompt', prompt_id)
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': '提示词不存在或已被归档'}), 404
