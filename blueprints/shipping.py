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
    OcrMatchEvent, PlacementImage, CopyPaperImage,
    classify_record, CategoryPrompt,
)
from blueprints._helpers import (
    get_upload_dir as get_helpers_upload_dir,
    get_ypp, calc_hint, check_remark, summarize_remarks, find_ypp_mismatches,
    get_piece_conversion, calc_piece_quantity, check_piece_mismatch,
    validate_image_content, check_uploaded_image,
    match_label_to_row, detect_bg_color,
    apply_user_rotation,
    compute_placement_expected_zhi,
    compute_copy_paper_expected_quantity,
)
from blueprints.ocr_engine import (
    PaddleOCREngine, get_ocr_engine, OCR_MATCH_PROMPT_VERSION,
    ocr_preprocess_kind,
)
from blueprints.ocr_pipeline import (
    RecordImageProcessor, _OCR_LOCK, _ASYNC_JOBS,
)
from blueprints import _helpers
from models._db import get_db

# 2026-08-19:「散码」口径正则 — 用 (?<![\d.]) 负向后顾排除小数点后的 y,
#   例如 "34.5y" 中的 "5" 不应被识别为独立散码期望,否则会把 160支*34.5y
#   误判成"支=160、散码=5",导致 placement_match 永远为 False。
#   两处使用保持一致:
#     1) line ~202: placement_groups 渲染(每条 record 的备注汇总)
#     2) line ~303: placement_match 判定(决定"点数"按钮是否加绿框)
SANMA_RE = re.compile(r'(?<![\d.])(\d+)\s*[yY]')
from blueprints.ocr_log import set_log_context


bp = Blueprint('shipping', __name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

logger = logging.getLogger(__name__)

# 行级图 OCR pipeline 共用处理器(出货专用,显式注入 ShippingImage)
# 替代旧的 _classify_and_match_image / _run_label_match / _supplement_for_record /
# _append_blur_reason_if_low_conf / _process_record_image_async 等私有函数
# (2026-08-19 抽取到 blueprints/ocr_pipeline.py)。
#
# ocr_engine_getter 用 lambda 包裹:让 processor 在每次调用时从本模块 globals
# 重新解析 get_ocr_engine —— 这样测试 mock.patch('blueprints.shipping.get_ocr_engine')
# 能正常生效(直接传 get_ocr_engine 引用会被锁在 import 时绑定,patch 不生效)。
shipping_processor = RecordImageProcessor(
    ShippingImage, ocr_engine_getter=lambda name: get_ocr_engine(name))


# ── 行级图异步编排薄壳 ─────────────────────────────────────────────
# _OCR_LOCK 与 _ASYNC_JOBS 由 ocr_pipeline 模块提供(从 Blueprint 内 re-export,
# 保留 history import 路径给其它文件 / 测试)。
# 单用户本地部署,用进程内存登记任务进度即可,无需 Redis/Celery。
# _ASYNC_JOBS[image_id] = {'state': 'processing'|'done'|'error', 'started': ts, 'finished': ts}


def _spawn_record_image_processing(image_id, filepath, record, order_id, record_id,
                                    rel_path, original_name, sort_order):
    """登记任务并启动后台线程,返回"处理中"图片字典(供上传端点立即返回)。

    真正的 OCR + AI 比对逻辑在 ocr_pipeline.RecordImageProcessor.process_async 内部,
    这里只负责:登记 _ASYNC_JOBS、起 threading.Thread + copy_context、调 processor。
    """
    _ASYNC_JOBS[image_id] = {'state': 'processing', 'started': time.time()}

    def _run_async_in_thread():
        # 必须包一层 lambda/wrapper:shipping_processor.process_async 是绑定方法,
        # 而 ctx.run(callable, *args) 会把 args 全传给 callable — 绑定方法再传一遍 self
        # 会得到 6 个参数(process_async(self, image_id, filepath, record, order_id, record_id))
        # 导致 TypeError。
        shipping_processor.process_async(
            image_id=image_id, filepath=filepath, record=record,
            order_id=order_id, record_id=record_id,
        )

    # threading.Thread 不会自动传 contextvars —— 必须显式 copy_context().run(...)
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
        'processing': True,          # 前端据此显示"识别中"并启动轮询
        'match_status': None,
        'match_score': None,
        'reason': '',
        'match_source': None,
        'bg_color': None,
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


# ── 页面 ──────────────────────────────────────────────
@bp.route('/shipping-records')
def shipping_records():
    today = date_cls.today().isoformat()
    start_date = request.args.get('start_date', '').strip()
    end_date = request.args.get('end_date', '').strip()
    # 默认最近2天
    if not start_date or not end_date:
        end = date_cls.today()
        start = end - timedelta(days=1)
        start_date = start.isoformat()
        end_date = end.isoformat()
    groups = ShippingRecord.get_groups(start_date, end_date)
    # order_pk -> 图片字典,按 source 拆分为:
    #   order_non_ai: 订单级图 + record 级图(非 AI,展示在"商品行下方"整体图区)
    #   ai_images:     订单级图 + record 级图(AI 识别图,展示在"商品行上方"AI 图区)
    order_ids = [g['id'] for g in groups]
    order_non_ai = {}  # 订单标题区(商品行下方)显示的图:仅非 AI
    ai_images = {}     # AI 识别图区(商品行上方)显示
    placement_by_record = {}  # 摆放图:record_pk -> [图片(含 marks)],与 OCR 图彻底隔离
    # ShippingImage.get_by_order 返回"订单级 + record 级"全部图;
    # 这里只需要订单级(record_pk IS NULL),record 级的从下面 get_by_record 循环补
    for oid in order_ids:
        order_only = [img for img in ShippingImage.get_by_order(oid) if img.get('record_pk') is None]
        non_ai = [img for img in order_only if img.get('source') not in ('ai', 'placement')]
        ai = [img for img in order_only if img.get('source') == 'ai']
        if non_ai: order_non_ai[oid] = non_ai
        if ai: ai_images[oid] = ai
    # 把每个订单的所有 record 级图也按 source 分到上面两个 dict
    # 2026-08-04:match-col 服务端不再渲染,改由 JS 在首次有匹配结果时通过
    # `_ensureMatchColumn` 动态插入。所以这里不再算 record_worst_*_map /
    # record_human_verified_map / has_match —— 那块逻辑下沉到 JS。
    # 2026-08-15:source='placement' 的摆放图不进上述两区,单独走 placement_by_record。
    for grp in groups:
        all_record_imgs = []
        for rec in grp.get('records', []):
            record_imgs = ShippingImage.get_by_record(rec['id'])
            # 摆放图不计入 OCR 图的 has_image(红框语义=已上传 OCR 图)
            rec['has_image'] = any(i.get('source') != 'placement' for i in record_imgs)
            placement_by_record[rec['id']] = PlacementImage.get_by_record(rec['id'])
            all_record_imgs.extend(i for i in record_imgs if i.get('source') != 'placement')
        for img in all_record_imgs:
            target = ai_images if img.get('source') == 'ai' else order_non_ai
            target.setdefault(grp['id'], []).append(img)
    # 摆放图"支数"口径:直接输入(manual_count)优先,无则回退点击计数点(n_marks)。
    # 与前端 placement_count.js 的 effectiveZhi 保持一致,避免"前端✓、后端不匹配"的割裂。
    def _eff_zhi(p):
        mc = p.get('manual_count')
        return mc if mc is not None else (p.get('n_marks') or 0)
    # 2026-08-16:按 product_name 把有点数图的 record 进行预分组(连续同 product_name 合并),模板里组内并排显示
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
            expected_sanma = sum(int(x.group(1)) for x in san_m)
            has_sanma = len(san_m) > 0
            current['items'].append({
                'record_id': rec['id'],
                'specification': rec.get('specification') or '',
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

    total_orders = ShippingOrder.count_by_date(today)

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

            # 2026-08-16:点数匹配标记 —— 供操作列"点数"按钮加绿框。
            # 匹配 = 该记录有摆放图,且 支合计==备注支期望,且(备注无散码期望 或 散码合计==备注散码期望)
            _pimgs = placement_by_record.get(item['id']) or []
            _pm = False
            if _pimgs:
                _remark = item.get('remark') or ''
                _qty = item.get('quantity') or ''
                _unit = item.get('unit') or ''
                _total = sum(_eff_zhi(p) for p in _pimgs)
                _loose = sum(p.get('loose_count', 0) for p in _pimgs)
                # 2026-09-03:placement 期望值兜底 — unit='支' + 备注无支数 → 用 quantity
                _exp_zhi, _has_zhi = compute_placement_expected_zhi(_remark, _qty, _unit)
                _san_m = list(SANMA_RE.finditer(_remark))
                _exp_san = sum(int(x.group(1)) for x in _san_m)
                _has_san = len(_san_m) > 0
                _matched_zhi = (not _has_zhi) or (abs(_total - _exp_zhi) <= 0.01)
                _matched_san = (not _has_san) or (abs(_loose - _exp_san) <= 0.01)
                if (_has_zhi or _has_san) and _matched_zhi and _matched_san:
                    _pm = True
            item['placement_match'] = _pm

            # 2026-09-06: 拷贝纸/日本纸 行级图片 + 张数比对
            _enrich_copy_paper_for_item(item)
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
        placement_by_record=placement_by_record,  # 2026-08-15:摆放图(计数用),与 OCR 图隔离
        placement_groups=placement_groups,  # 2026-08-16:按 product_name 预分组,组内并排
        page_title='出货记录',
        today=today,
        total_orders=total_orders,
        start_date=start_date,
        end_date=end_date,
        unit_list=unit_list,
        piece_conversions=piece_conv_list,
    )




# ── YPP 规则冲突核查页 ─────────────────────────────────────
@bp.route('/shipping-ypp-review')
def shipping_ypp_review_page():
    """YPP 规则冲突核查页(单页手动扫描,不受日期过滤影响)。

    顶部展示已配置 YPP 的品名数(供用户判断"是不是漏配了"),
    下方点击"开始扫描"按钮触发 POST /api/v1/shipping-orders/ypp-review/scan
    返回所有按 YPP 规则核验出的冲突项。
    """
    try:
        all_units = ProductUnit.get_all() or []
    except Exception:
        all_units = []
    ypp_config_count = sum(
        1 for u in all_units
        if u.get('is_usingyardforcounting') and (u.get('yards_per_piece') or 0) > 0
    )
    return render_template('shipping_ypp_review.html', ypp_config_count=ypp_config_count)


@bp.route('/api/v1/shipping-orders/ypp-review/scan', methods=['POST'])
def api_v1_shipping_orders_ypp_review_scan():
    """扫全表所有出货明细,找出 YPP 规则冲突项。

    Returns:
        {
          'success': True,
          'scanned_total': N,        # 扫了多少条(参与校验的明细)
          'items': [                 # 冲突项,按 warn→info,日期倒序
            {record_id, order_id, date, customer, product_name, specification,
             quantity, unit, remark, pieces, per_piece_yards, loose_yards, ypp,
             expected, actual, diff, severity}
          ]
        }
    """
    try:
        records = ShippingRecord.get_all()
    except Exception as e:
        current_app.logger.exception('ypp-review scan: failed to load records')
        return jsonify({'success': False, 'error': f'加载明细失败: {e}'}), 500
    try:
        items = find_ypp_mismatches(records) or []
    except Exception as e:
        current_app.logger.exception('ypp-review scan: failed to compute mismatches')
        return jsonify({'success': False, 'error': f'扫描失败: {e}'}), 500
    return jsonify({
        'success': True,
        'scanned_total': len(records),
        'items': items,
    })


# ── 订单 CRUD ──────────────────────────────────────────────
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
    source_tag = request.form.get('source_tag') or (data.get('source_tag') if request.is_json else None)
    if source_tag not in ('备货照', '装车照', '归仓照'):
        source_tag = None
    image_id = ShippingImage.create(order_id, filepath, original_name, source, None, None, source_tag)
    rel_path = os.path.join(month_str, filename).replace('\\', '/')
    AuditLog.log('upload_image', 'shipping_order', order_id, detail={'filename': filename, 'source': source})
    return jsonify({'success': True, 'image_id': image_id, 'image': rel_path, 'source_tag': source_tag, 'file_path': filepath, 'original_name': original_name}), 201


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

    流程: 取图→取关联商品行→调用 processor.extract_ocr (缓存优先)→
         跑 match_label_to_row→更新 shipping_images→返回。

    2026-08-19 改造:OCR + 背景色提取改用 shipping_processor.extract_ocr 共享方法,
    消除 ~50 行重复代码。
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

    extracted = shipping_processor.extract_ocr(
        img['file_path'], record, image_id=image_id, use_cached=True)
    if not extracted['ocr_text'].strip():
        return jsonify({'success': False, 'error': 'OCR 无文字，无法匹配'}), 400

    status, score, reason = match_label_to_row(
        extracted['ocr_text'],
        record.get('product_name', ''),
        record.get('specification', ''))
    ShippingImage.set_match(image_id, status, score or 0, reason, source='local_fuzzy')

    return jsonify({
        'success': True,
        'image_id': image_id,
        'match_status': status,
        'match_score': score,
        'reason': reason,
        'match_source': 'local_fuzzy',
    })


@bp.route('/api/v1/shipping-orders/images/<int:image_id>/re-ocr', methods=['POST'])
def api_v1_shipping_orders_re_ocr_image(image_id):
    """对一张已上传图片重做 PaddleOCR 识别,自动重跑本地模糊匹配 (2026-08-19)。

    与 /fuzzy-match 的区别:
      - /fuzzy-match 优先用缓存(免重跑 PaddleOCR),/re-ocr 强制 use_cached=False
        (出货页右下角"OCR"按钮的语义:重新识别,不允许吃旧结果)
      - 额外写一条 append-only 'record_ocr' 事件 (审计可看 OCR 重做演变)

    行为:
      - 复用 RecordImageProcessor.extract_ocr (享受缓存/4角背景色/[标签背景:]后缀)
      - 自动跑 match_label_to_row (本地 RapidFuzz, <100ms)
      - 写 append-only record_ocr 事件 (审计)
      - 更新 shipping_images.match_*
    """
    img = ShippingImage.get_by_id(image_id)
    if not img:
        return jsonify({'success': False, 'error': '图片不存在'}), 404
    if not img.get('record_pk'):
        return jsonify({'success': False, 'error': '该图片未关联商品行，无法重做 OCR'}), 400
    record = ShippingRecord.get_by_id(img['record_pk'])
    if not record:
        return jsonify({'success': False, 'error': '关联商品行不存在'}), 404

    # 1) 复用 RecordImageProcessor (与上传流程一致)
    extracted = shipping_processor.extract_ocr(
        img['file_path'], record, image_id=image_id, use_cached=False)

    if not extracted['ocr_text'].strip():
        return jsonify({'success': False, 'error': 'OCR 无文字，无法识别'}), 400

    # 2) 自动重跑本地模糊匹配
    status, score, reason = match_label_to_row(
        extracted['ocr_text'],
        record.get('product_name', ''),
        record.get('specification', ''))
    source_label = 'local_fuzzy'
    if status:
        ShippingImage.set_match(image_id, status, score or 0, reason, source=source_label)

    # 3) 写 append-only record_ocr 事件 (审计)
    try:
        OcrMatchEvent.create(
            'record_ocr', record_id=img['record_pk'], order_id=img['order_pk'],
            image_id=image_id, ocr_text=extracted['ocr_text'],
            ocr_engine='paddleocr',
            ai_engine=source_label if status else None,
            ai_match_status=status or None,
            ai_match_score=score, ai_match_reason=reason or None,
            product_name=record.get('product_name', ''),
            specification=record.get('specification', ''))
    except Exception:
        current_app.logger.exception('record_ocr(re-ocr) 事件写库失败(不阻断)')

    return jsonify({
        'success': True,
        'image_id': image_id,
        'match_status': status, 'match_score': score, 'reason': reason,
        'match_source': source_label, 'bg_color': extracted['bg_color'],
    })


@bp.route('/api/v1/shipping-orders/images/<int:image_id>/ai-judge', methods=['POST'])
def api_v1_shipping_orders_ai_judge_image(image_id):
    """对已上传图片重新运行 DeepSeek AI 比对 → 返回新的判别结果。

    流程: 取图→取关联商品行→调 processor.extract_ocr (缓存优先)→
         DeepSeek 单 record 比对 (with_supplement)→写 image.match + 写 ai_match 事件→返回。

    2026-08-19 改造:OCR + 背景色提取 + DeepSeek 调用 + ai_match 事件写库全部走 processor,
    消除 ~90 行重复代码。
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

    extracted = shipping_processor.extract_ocr(
        img['file_path'], record, image_id=image_id, use_cached=True)
    if not extracted['ocr_text'].strip():
        return jsonify({'success': False, 'error': 'OCR 无文字，无法判断'}), 400

    # DeepSeek API key 校验(processor.classify 内部会静默 fallback,这里显式 503 让前端知道)
    try:
        ds = get_ocr_engine('deepseek')
        if not getattr(ds, 'API_KEY', ''):
            return jsonify({'success': False, 'error': 'DeepSeek API Key 未配置'}), 503
    except Exception:
        return jsonify({'success': False, 'error': 'DeepSeek 引擎初始化失败'}), 503

    result = shipping_processor.classify(extracted['ocr_text'], record)
    if not result['status']:
        return jsonify({'success': False, 'error': 'DeepSeek 返回异常'}), 502
    shipping_processor.persist_match(
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


# ────────────────────────────────────────────────────────────
# 摆放图 + 计数点(供"支"类商品清点数量)
# 摆放图标记 source='placement',与 OCR/AI 比对图彻底隔离(不上 OCR、不进整体图区)
# ────────────────────────────────────────────────────────────
def _detect_cylinder_circles(image_path):
    """用 OpenCV HoughCircles 检测圆柱端面(正圆),返回归一化圆列表。

    每个圆: {x, y, rx, ry},其中 (x, y) 为圆心(相对宽/高 0~1),
    rx = 半径/宽, ry = 半径/高。检测不到或出错返回 []。
    仅适用于圆柱端面正对镜头(圆形可见)的摆放照。
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


@bp.route('/api/v1/shipping-orders/records/<int:record_id>/placement-images', methods=['POST'])
def api_v1_shipping_orders_record_placement_upload(record_id):
    """上传商品行的摆放图(多文件/单 base64),不触发 OCR,source='placement' → 201"""
    set_log_context(biz='shipping', record_id=record_id)
    record = ShippingRecord.get_by_id(record_id)
    if not record:
        return jsonify({'success': False, 'error': '记录不存在'}), 404
    order = ShippingOrder.get_by_id(record['order_pk'])
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
            image_id = PlacementImage.create(
                order_pk=record['order_pk'], record_pk=record_id,
                file_path=filepath, original_name=original_name,
            )
            rel_path = os.path.join(month_str, os.path.basename(filepath)).replace('\\', '/')
            img = PlacementImage.get_by_id(image_id)
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
        image_id = PlacementImage.create(
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
        AuditLog.log('upload_placement_image', 'shipping_order', record['order_pk'],
                     detail={'filename': s['image'], 'record_id': record_id})
    return jsonify({'success': True, 'images': saved, 'count': len(saved)}), 201


@bp.route('/api/v1/shipping-orders/records/<int:record_id>/placement-images', methods=['GET'])
def api_v1_shipping_orders_record_placement_list(record_id):
    """列出某商品行所有摆放图(含计数点)。"""
    images = PlacementImage.get_by_record(record_id)
    return jsonify({'success': True, 'images': images})


@bp.route('/api/v1/shipping-orders/placement-images/<int:image_id>', methods=['DELETE'])
def api_v1_shipping_orders_placement_delete(image_id):
    """删除一张摆放图(连带计数点 + 物理文件)。"""
    ok = PlacementImage.delete(image_id)
    if not ok:
        return jsonify({'success': False, 'error': '摆放图不存在'}), 404
    return jsonify({'success': True})


@bp.route('/api/v1/shipping-orders/placement-images/<int:image_id>', methods=['GET'])
def api_v1_shipping_orders_placement_get(image_id):
    """获取单张摆放图(含计数点)。"""
    img = PlacementImage.get_by_id(image_id)
    if not img:
        return jsonify({'success': False, 'error': '摆放图不存在'}), 404
    return jsonify({'success': True, 'image': img})


@bp.route('/api/v1/shipping-orders/placement-images/<int:image_id>/detect', methods=['POST'])
def api_v1_shipping_orders_placement_detect(image_id):
    """重新用 OpenCV 检测圆柱端面(可反复重试以提高召回)。返回 circles。"""
    img = PlacementImage.get_by_id(image_id)
    if not img:
        return jsonify({'success': False, 'error': '摆放图不存在'}), 404
    circles = _detect_cylinder_circles(img['file_path'])
    PlacementImage.set_circles(image_id, circles)
    return jsonify({'success': True, 'circles': circles})


@bp.route('/api/v1/shipping-orders/placement-images/<int:image_id>/mark-scale', methods=['POST'])
def api_v1_shipping_orders_placement_mark_scale(image_id):
    """设置该摆放图计数数字的整体系缩放比例(弹框放大/缩小按钮)。body: {scale}。"""
    img = PlacementImage.get_by_id(image_id)
    if not img:
        return jsonify({'success': False, 'error': '摆放图不存在'}), 404
    data = request.get_json() or {}
    try:
        scale = float(data.get('scale', 1))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'scale 必须是数字'}), 400
    scale = PlacementImage.set_mark_scale(image_id, scale)
    return jsonify({'success': True, 'scale': scale, 'marks': PlacementImage.get_marks(image_id)})


@bp.route('/api/v1/shipping-orders/placement-images/<int:image_id>/loose-count', methods=['POST'])
def api_v1_shipping_orders_placement_loose_count(image_id):
    """设置该摆放图的散码数量(点数弹框内「散码」按钮录入)。body: {count}。"""
    img = PlacementImage.get_by_id(image_id)
    if not img:
        return jsonify({'success': False, 'error': '摆放图不存在'}), 404
    data = request.get_json() or {}
    try:
        count = int(round(float(data.get('count', 0))))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'count 必须是数字'}), 400
    count = PlacementImage.set_loose_count(image_id, count)
    return jsonify({'success': True, 'count': count})


@bp.route('/api/v1/shipping-orders/placement-images/<int:image_id>/manual-count', methods=['POST'])
def api_v1_shipping_orders_placement_manual_count(image_id):
    """设置该摆放图直接输入的支数(点数弹框内「输入支数」按钮录入)。body: {count}。

    count 为 null / 空字符串 → 清除直接输入,回退到点击计数(n_marks);
    为整数(含 0) → 以该值为准,并清空点击计数点(二者互斥)。
    """
    img = PlacementImage.get_by_id(image_id)
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
    count = PlacementImage.set_manual_count(image_id, count)
    return jsonify({'success': True, 'manual_count': count})


@bp.route('/api/v1/shipping-orders/placement-images/<int:image_id>/marks', methods=['POST'])
def api_v1_shipping_orders_placement_mark_add(image_id):
    """在摆放图上点一枚计数点。body: {x_ratio, y_ratio, r}(0~1)。返回最新计数点列表。"""
    img = PlacementImage.get_by_id(image_id)
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
    PlacementImage.add_mark(image_id, x, y, mark_r=r)
    return jsonify({'success': True, 'marks': PlacementImage.get_marks(image_id)})


@bp.route('/api/v1/shipping-orders/placement-images/<int:image_id>/marks/last', methods=['DELETE'])
def api_v1_shipping_orders_placement_mark_undo(image_id):
    """撤销最近一枚计数点(连续可撤销)。返回剩余计数点列表。"""
    img = PlacementImage.get_by_id(image_id)
    if not img:
        return jsonify({'success': False, 'error': '摆放图不存在'}), 404
    marks = PlacementImage.delete_last_mark(image_id)
    return jsonify({'success': True, 'marks': marks})


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
# 加载用户已填的分类提示词(直接进编辑框,可微调),保存后调 /category-prompts 或
# /manage/category-prompts/<id> PATCH 写回。下次同 record 比对自动注入。
# ────────────────────────────────────────────────────────────────────

def _resolve_existing_prompt(existing: dict | None, product_name: str) -> dict | None:
    """从 list_for_record 的结果里挑出要加载进编辑框的那条已有提示词。

    优先级:品名关键字精确命中 > 任一 category 提示词 > spec 提示词。
    返回该行(dict,含 id / prompt_text)或 None。
    """
    if not existing:
        return None
    cats = existing.get('category_prompts') or []
    if cats:
        if product_name:
            for r in cats:
                if r.get('product_name_keyword') and r['product_name_keyword'] == product_name:
                    return r
        return cats[0]
    specs = existing.get('spec_prompts') or []
    if specs:
        return specs[0]
    return None


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
    # 查同品类/同规格已有的自定义提示词,直接加载用户已填内容进编辑框
    # (2026-08-09 简化 + 本次修复:无则空,让用户从头填;弹框 placeholder 已说明)
    pn = (record or {}).get('product_name', '')
    spec = (record or {}).get('specification', '')
    cc = cls['category_code'] if cls else None
    existing = CategoryPrompt.list_for_record(category_code=cc, product_name=pn, specification=spec)
    # 解析要加载进编辑框的提示词:优先品名精确命中,其次任一 category 提示词,再次 spec 提示词
    existing_prompt = _resolve_existing_prompt(existing, pn)
    prompt_text = existing_prompt['prompt_text'] if existing_prompt else ''
    # 溯源链:找这张图最近一条 human_verify 事件,把 id 透传给前端的 category-prompts 保存请求
    # 之前写 img.get('_source_event_id') 是误读 — shipping_images 没有该列,永远 None
    hv_event = OcrMatchEvent.get_latest_by_image(image_id, 'human_verify')
    source_event_id = hv_event['id'] if hv_event else None
    return jsonify({
        'success': True,
        'suggestion': {
            'prompt_text': prompt_text,
            'existing_prompt_id': existing_prompt['id'] if existing_prompt else None,
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


# ────────────────────────────────────────────────────────────
# 拷贝纸/日本纸 行级图片(2026-09-06)
# 与 OCR/AI 比对图彻底隔离:不进 pipeline、不进 match-col;
# 仅供人工参考 + 行级 total 比对用。
# 4 端点:POST 上传、GET 列表、DELETE 删、PATCH 录入张数
# ────────────────────────────────────────────────────────────

@bp.route('/api/v1/shipping-orders/records/<int:rid>/copy-paper-images', methods=['POST'])
def api_v1_shipping_orders_record_copy_paper_upload(rid):
    """上传拷贝纸/日本纸 行级图(标签照/张数照)。完全跳过 OCR pipeline。

    支持 multipart(字段名 `image`)与 JSON base64(`image` 字段)。
    source ∈ {'label','count'};锁单 → 403;记录不存在 → 404;无图 → 400。
    """
    set_log_context(biz='shipping', record_id=rid, evt_src='copy_paper_upload')
    record = ShippingRecord.get_by_id(rid)
    if not record:
        return jsonify({'success': False, 'error': '记录不存在'}), 404
    order = ShippingOrder.get_by_id(record['order_pk'])
    if order and order.get('is_locked'):
        return jsonify({'success': False, 'error': '该订单已锁定,无法上传拷贝纸/日本纸'}), 403

    upload_dir, _month_str = _get_upload_dir()

    # 解析 source
    if request.is_json:
        _payload = request.get_json(silent=True) or {}
        source = (_payload.get('source') or '').strip()
    else:
        source = (request.form.get('source') or '').strip()
    if source not in ('label', 'count'):
        return jsonify({'success': False, 'error': 'source 必须为 label 或 count'}), 400

    # 两种格式都支持:multipart 多个 `image` 字段,或 JSON 单 base64
    files = request.files.getlist('image')
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

    new_id = CopyPaperImage.create(rid, filepath, original_name, source)
    img = CopyPaperImage.get_by_id(new_id)
    rel_path = os.path.join(_month_str, os.path.basename(filepath)).replace('\\', '/')
    img['file_path'] = filepath  # 全路径(与 PlacementImage 一致)
    img['relative_path'] = rel_path
    AuditLog.log('upload_copy_paper_image', 'shipping_order', record['order_pk'],
                 detail={'filename': rel_path, 'source': source, 'record_id': rid})
    return jsonify({'success': True, 'image': img})


@bp.route('/api/v1/shipping-orders/records/<int:rid>/copy-paper-images', methods=['GET'])
def api_v1_shipping_orders_record_copy_paper_list(rid):
    """列出某 record 下所有拷贝纸/日本纸行级图。"""
    images = CopyPaperImage.list_by_record(rid)
    # 补上 relative_path,便于前端直接展示
    for img in images:
        if img.get('file_path'):
            img['relative_path'] = os.path.basename(img['file_path'])
    return jsonify({'success': True, 'images': images})


@bp.route('/api/v1/shipping-orders/copy-paper-images/<int:img_id>', methods=['DELETE'])
def api_v1_shipping_orders_copy_paper_delete(img_id):
    """删除一张拷贝纸/日本纸行级图(连带物理文件)。"""
    img = CopyPaperImage.get_by_id(img_id)
    if not img:
        return jsonify({'success': False, 'error': '拷贝纸/日本纸图片不存在'}), 404

    # 锁单防御:沿 record → order 查 is_locked
    rec = ShippingRecord.get_by_id(img['record_pk']) if img.get('record_pk') else None
    order = ShippingOrder.get_by_id(rec['order_pk']) if rec else None
    if order and order.get('is_locked'):
        return jsonify({'success': False, 'error': '该订单已锁定,无法删除'}), 403

    # 删磁盘文件(找不到不报错,避免脏数据卡住流程)
    try:
        if img.get('file_path') and os.path.exists(img['file_path']):
            os.remove(img['file_path'])
    except Exception:
        current_app.logger.exception('删拷贝纸/日本纸图片文件失败: %s', img.get('file_path'))

    CopyPaperImage.delete(img_id)
    AuditLog.log('delete_copy_paper_image', 'shipping_order',
                 order['id'] if order else None,
                 detail={'image_id': img_id, 'record_id': img.get('record_pk')})
    return jsonify({'success': True})


@bp.route('/api/v1/shipping-orders/copy-paper-images/<int:img_id>/sheet-count', methods=['PATCH'])
def api_v1_shipping_orders_copy_paper_sheet_count(img_id):
    """录入/清空 拷贝纸/日本纸 张数。

    body: `{sheet_count: int | null}`
    - 正数(含 0) → 写入
    - null       → 清空
    - 负数       → 400
    """
    img = CopyPaperImage.get_by_id(img_id)
    if not img:
        return jsonify({'success': False, 'error': '拷贝纸/日本纸图片不存在'}), 404

    rec = ShippingRecord.get_by_id(img['record_pk']) if img.get('record_pk') else None
    order = ShippingOrder.get_by_id(rec['order_pk']) if rec else None
    if order and order.get('is_locked'):
        return jsonify({'success': False, 'error': '该订单已锁定,无法修改'}), 403

    body = request.get_json(silent=True) or {}
    raw = body.get('sheet_count', None)
    if raw is None:
        val = None
    else:
        try:
            val = int(raw)
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': 'sheet_count 必须为整数或 null'}), 400
        if val < 0:
            return jsonify({'success': False, 'error': 'sheet_count 不能为负'}), 400

    CopyPaperImage.update_count(img_id, val)
    AuditLog.log('update_copy_paper_sheet_count', 'shipping_order',
                 order['id'] if order else None,
                 detail={'image_id': img_id, 'sheet_count': val})
    return jsonify({'success': True, 'sheet_count': val})


# ─────────────────────────────────────────────────────────
# Task 4 (2026-09-06): record 级别拷贝纸/日本纸 富化
# ─────────────────────────────────────────────────────────

def _is_copy_paper_item(item: dict) -> bool:
    """判断 record 是否属于拷贝纸/日本纸类别。

    双兜底:
    1. 关键词匹配(product_name 含 '拷贝' 或 '日本纸')
    2. classify_record 查 product_categories 拿到 category_code,
       是 '0105'(日本纸)或 '0107'(拷贝纸)时返回 True
    """
    name = (item.get('product_name') or '').strip()
    # 关键词兜底
    if '拷贝' in name or '日本纸' in name:
        return True
    # JOIN category_code(若 helper 已存在)
    try:
        from models.category_prompt import classify_record
        result = classify_record(name, item.get('specification') or '')
        code = result.get('category_code') if isinstance(result, dict) else None
        return code in ('0105', '0107')
    except Exception:
        return False


def _enrich_copy_paper_for_item(item: dict) -> None:
    """对单条 record 原地写入 copy-paper 字段。

    写入字段:
      is_copy_paper / copy_paper_images / copy_paper_total / copy_paper_match
      copy_paper_expected (float 或 None;qty/unit 算不出时为 None)

    copy_paper_match 四态:
      - None      → 期望值算不出来(qty=0 / unit 非法)
      - 'partial' → 有 count 图但 sheet_count 未全部录入
      - 'green'   → 录入合计 == 期望值
      - 'yellow'  → 录入合计 != 期望值
    """
    item['is_copy_paper'] = _is_copy_paper_item(item)
    if not item['is_copy_paper']:
        item['copy_paper_images'] = []
        item['copy_paper_total'] = 0
        item['copy_paper_match'] = None
        item['copy_paper_expected'] = None
        return

    images = CopyPaperImage.list_by_record(item['id'])
    item['copy_paper_images'] = images

    counts = [img['sheet_count'] for img in images if img['sheet_count'] is not None]
    total = sum(counts)
    item['copy_paper_total'] = total

    count_imgs = [img for img in images if img['source'] == 'count']
    total_count_imgs = len(count_imgs)
    counted_imgs = sum(1 for img in count_imgs if img['sheet_count'] is not None)

    expected, has_expected = compute_copy_paper_expected_quantity(
        item.get('quantity'), item.get('unit'))
    # Task 6 (2026-09-06): 模板要用 expected 渲染徽章文案,Task 4 漏写,这里补上。
    item['copy_paper_expected'] = expected if has_expected else None
    if not has_expected:
        item['copy_paper_match'] = None
    elif total_count_imgs > 0 and counted_imgs < total_count_imgs:
        item['copy_paper_match'] = 'partial'
    elif total == expected:
        item['copy_paper_match'] = 'green'
    else:
        item['copy_paper_match'] = 'yellow'
