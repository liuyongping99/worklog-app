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
    OcrMatchEvent, PlacementImage,
    InboundRecord, LoadingOrderRecord,  # 2026-09-21: ypp-review scan 支持入库/装柜
    classify_record, CategoryPrompt,
)
from blueprints._helpers import (
    get_upload_dir as get_helpers_upload_dir,
    get_ypp, calc_hint, check_remark, summarize_remarks, find_ypp_mismatches,
    get_piece_conversion, calc_piece_quantity, check_piece_mismatch,
    annotate_import_validation, summarize_import_validation,
    validate_image_content, check_uploaded_image,
    match_label_to_row, detect_bg_color,
    apply_user_rotation,
    compute_placement_expected_zhi, parse_loose_yards,
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

# 2026-10-09: 散码解析统一调 _helpers.parse_loose_yards(先抠掉「X支*Yy」乘法项)。
#   旧 SANMA_RE = (?<![\d.])(\d+)\s*[yY] 已被删除 —— 它把 105支*32y+37y 里的
#   32y(每支码数)误算进散码,导致 expected_sanma=69(应=37)。
#   新函数同时支持小数 + 中英 y/Y/码,与 check_remark 的「先抠乘法项」口径一致。

# 2026-09-09: 拷贝纸/日本纸 标签图在 shipping_images 里的 source 值。
# 与 OCR 图('upload'/'ai')、摆放图('placement')并列,靠它区分:
#   - 模板渲染:独立循环,不渲染 OCR/AI 按钮与 match-badge
#   - has_image / ocr_images 统计:排除(不做 OCR,不算已核对图)
# 2026-10-09 重命名:常量名 COPY_PAPER_LABEL_SOURCE → NO_AI_LABEL_SOURCE,
#   语义对齐 CLAUDE.md §22 术语「免 AI 比对标签图」;字符串值 'copy_paper_label'
#   **保持不变** —— DB 字段值 / 已有 28 行存量数据 / 任何将来的数据回查都依赖它。
NO_AI_LABEL_SOURCE = 'copy_paper_label'
# 2026-10-04: 跳过商品行 AI 比对的品类(拷贝纸/日本纸/快巴纸 + 腊光/蜡光)。
# 2026-10-08: 这批品类的**唯一**图片入口是「🖼️ 标签图」按钮(source='copy_paper_label',不做 OCR),
#   腊光纸(0108)也纳入 —— 它跟拷贝纸一样,贴纸与商品本体不一致,走 OCR 只会刷误导性红 ✗。
# 四处消费都走同一个谓词 _is_no_ai_match_item:
#   - 后端硬拦截 POST /records/<id>/images → 400(逼用户改走标签图按钮)
#   - 整单 ai-match 过滤 records → OCR 循环 / rows / own_record_ids / verdicts 回写全跳过
#   - 模板:普通 🖼️ 按钮守卫 not is_no_ai_match(隐藏);标签 🖼️ 按钮守卫 is_no_ai_match(显示)
#   - 移动端:隐藏「📷 拍照识别」(否则点进去吃 400),徽章改为「标签图」
# 与 NO_AI_LABEL_SOURCE 不同:后者是落库的 source 字符串,这里是判定用的品类集合。
NO_AI_MATCH_CATEGORY_CODES = ('0105', '0106', '0107', '0108')
NO_AI_MATCH_KEYWORDS = ('拷贝', '日本纸', '快巴', '腊光', '蜡光')
from blueprints.ocr_log import set_log_context


# 2026-09-29: 皮革/木板 类商品按"块"计数,无需拍照,商品行不应显示🖼️ 上传按钮。
# 与 mobile_shipping.py / inbound.py / loading.py 三个端点的同名检测保持一致,
# 模板 `{% if not item.is_board %}` 守卫依赖此函数 + item['is_board'] 上下文。
# 关键检测:'皮革' 与 '木板' 任一关键字出现即视为 board,避免『皮革木板』字面量
# 检查漏掉『皮革』单独存在(2026-08-30 用户报告 bug)。
_BOARD_KEYWORDS = ("皮革", "木板")


def _is_board(product_name) -> bool:
    """商品名是否属于皮革/木板类(按"块"计数,行级不显示图片按钮)。"""
    if not product_name:
        return False
    return any(k in product_name for k in _BOARD_KEYWORDS)


# 摆放图"支数"口径:直接输入(manual_count)优先,无则回退点击计数点(n_marks)。
# 与前端 placement_count.js 的 effectiveZhi 保持一致,避免"前端✓、后端不匹配"的割裂。
# 2026-09-09:卸载货物(is_unload=1)时取负,从记录总数扣减(对齐装柜,装柜后端早就有这段)。
def _eff_zhi(p: dict):
    mc = p.get('manual_count')
    base = mc if mc is not None else (p.get('n_marks') or 0)
    return -base if p.get('is_unload') else base


def compute_placement_match(record: dict, pimgs: list) -> bool:
    """单条 record 的 placement 匹配判定(供主页渲染 + 各 placement mutation 端点回报)。

    匹配 = 该记录有摆放图,且(支匹配 且 散码匹配);支/散码若无期望则该项跳过。
    容差 0.01 容忍浮点累计误差。
    """
    if not pimgs:
        return False
    remark = record.get('remark') or ''
    qty = record.get('quantity') or ''
    unit = record.get('unit') or ''
    total = sum(_eff_zhi(p) for p in pimgs)
    loose = sum(p.get('loose_count', 0) for p in pimgs)
    exp_zhi, has_zhi = compute_placement_expected_zhi(remark, qty, unit)
    exp_san, has_san = parse_loose_yards(remark)
    matched_zhi = (not has_zhi) or (abs(total - exp_zhi) <= 0.01)
    matched_san = (not has_san) or (abs(loose - exp_san) <= 0.01)
    return (has_zhi or has_san) and matched_zhi and matched_san


def _placement_match_response(img: dict):
    """placement 端点 mutation 后的标准回报字段(供 6 个端点末尾调用)。

    返回 dict,直接 merge 进 jsonify({...})。
    """
    record_id = img.get('record_pk')
    if not record_id:
        return {'record_id': None, 'placement_match': False}
    record = ShippingRecord.get_by_id(record_id)
    pimgs = PlacementImage.get_by_record(record_id)
    return {
        'record_id': record_id,
        'placement_match': compute_placement_match(record or {}, pimgs),
    }


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
            # 2026-09-09: 本行图缓存给 _enrich_no_ai_label_for_item 复用(避免重复查库)
            rec['_record_imgs_cache'] = record_imgs
            # 摆放图不计入 OCR 图的 has_image(红框语义=已上传 OCR 图);
            # 免 AI 比对标签图(source='copy_paper_label')也不算 —— 它不做 OCR/AI
            rec['has_image'] = any(
                i.get('source') not in ('placement', 'copy_paper_label') for i in record_imgs)
            placement_by_record[rec['id']] = PlacementImage.get_by_record(rec['id'])
            all_record_imgs.extend(i for i in record_imgs if i.get('source') != 'placement')
        for img in all_record_imgs:
            target = ai_images if img.get('source') == 'ai' else order_non_ai
            target.setdefault(grp['id'], []).append(img)
    # 摆放图"支数"口径:直接输入(manual_count)优先,无则回退点击计数点(n_marks)。
    # 函数已抽到模块级(_eff_zhi / compute_placement_match),供 6 个 placement 端点复用。
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
            expected_sanma, has_sanma = parse_loose_yards(remark)
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
                # 2026-09-09: 计数单位参数化(支/令/张) + 拷贝纸无散码
                'unit': rec.get('unit') or '',
                # 2026-10-08: placement 区已按计量单位渲染(_item.unit),此字段当前不消费;
                # 保留便于后续按品类过滤 placement 显示。
                'is_no_ai_match': _is_no_ai_match_item(rec),
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
    locked_orders = ShippingOrder.count_locked_by_date(today)  # 2026-09-17 新增

    # 2026-09-29: 顶部趋势图数据 —— 最近 7 天(按日) + 最近 6 个月(按月)
    _today_d = date_cls.today()
    _week_start = (_today_d - timedelta(days=6)).isoformat()  # 含今天共 7 天
    trend_daily = ShippingOrder.count_by_date_range(_week_start, today)
    # 最近 6 个月:用相对今天往前推 5 个月 + 当月,共 6 个月
    _six_months_ago_y = _today_d.year
    _six_months_ago_m = _today_d.month - 5
    while _six_months_ago_m <= 0:
        _six_months_ago_m += 12
        _six_months_ago_y -= 1
    _month_start = f'{_six_months_ago_y:04d}-{_six_months_ago_m:02d}'
    _month_end = f'{_today_d.year:04d}-{_today_d.month:02d}'
    trend_monthly = ShippingOrder.count_by_month_range(_month_start, _month_end)

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
            # 2026-09-29: 皮革/木板类不显示行级图片按钮 —— 模板守卫 `{% if not item.is_board %}`
            # 必须依赖此字段;若未设,模板里 item.is_board 为 undefined/falsy,按钮照旧显示。
            item['is_board'] = _is_board(item.get('product_name', ''))
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

            # 2026-09-09:点数匹配标记 —— 委托模块级 compute_placement_match,
            # 与 6 个 placement mutation 端点保持同一判定,避免"前端绿框"判定分裂。
            _pimgs = placement_by_record.get(item['id']) or []
            item['placement_match'] = compute_placement_match(item, _pimgs)

            # 2026-09-06: 拷贝纸/日本纸 行级图片 + 张数比对
            _enrich_no_ai_label_for_item(item)
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
        locked_orders=locked_orders,  # 2026-09-17 新增:当日已锁单总数
        start_date=start_date,
        end_date=end_date,
        trend_daily=trend_daily,  # 2026-09-29: 最近 7 天每日出货单数
        trend_monthly=trend_monthly,  # 2026-09-29: 最近 6 个月每月出货单数
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
    """扫全表某业务线所有明细,找出 YPP 规则冲突项。

    Query 参数 biz=shipping|inbound|loading,默认 shipping。
    三页('/shipping-ypp-review' / '/inbound-records' / '/loading-orders')
    共用同一端点,统一走 find_ypp_mismatches(record 字段一致)。

    Returns:
        {
          'success': True,
          'biz': 'shipping|inbound|loading',
          'scanned_total': N,        # 扫了多少条(参与校验的明细)
          'items': [                 # 冲突项,按 warn→info,日期倒序
            {record_id, order_id, date, customer, product_name, specification,
             quantity, unit, remark, pieces, per_piece_yards, loose_yards, ypp,
             expected, actual, diff, severity}
          ]
        }
    """
    biz = (request.args.get('biz') or request.form.get('biz') or 'shipping').strip().lower()
    try:
        if biz == 'inbound':
            # InboundRecord 没有 get_all,用 get_groups 拉全部日期范围
            from datetime import date as _date, timedelta as _td
            _today = _date.today()
            groups = InboundRecord.get_groups(
                (_today - _td(days=365*3)).isoformat(),
                _today.isoformat(),
            )
            records = []
            for g in groups:
                for r in g.get('records', []):
                    r['order_pk'] = g.get('id') or g.get('order_pk')
                    r['date'] = g.get('date')
                    r['customer'] = g.get('supplier') or g.get('customer') or ''
                    records.append(r)
        elif biz == 'loading':
            # LoadingOrderRecord.get_all 已包含 order_pk/date/customer 字段
            records = LoadingOrderRecord.get_all()
        else:
            records = ShippingRecord.get_all()
    except Exception as e:
        current_app.logger.exception('ypp-review scan: failed to load records biz=%s', biz)
        return jsonify({'success': False, 'error': f'加载明细失败: {e}'}), 500
    try:
        items = find_ypp_mismatches(records) or []
    except Exception as e:
        current_app.logger.exception('ypp-review scan: failed to compute mismatches')
        return jsonify({'success': False, 'error': f'扫描失败: {e}'}), 500
    return jsonify({
        'success': True,
        'biz': biz,
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

    # 2026-10-09 概述 §f:导入即校验 —— 原地打上 mismatch/piece_mismatch/qty_invalid
    # 等标志回传前端，让红/粉标在导入那一刻就显示，不等刷新。
    # 不落库(2026-10-09 拍板):这些标志完全由 备注+数量 两个字段决定,刷新时现算即可。
    # 判定函数与列表页渲染时完全相同(check_remark/check_piece_mismatch),杜绝两套口径。
    try:
        annotate_import_validation(records)
        validation = summarize_import_validation(records)
    except Exception:
        current_app.logger.exception('导入校验失败(不影响已插入的明细): order_id=%s', order_id)
        validation = None

    resp = {'success': True, 'count': len(records), 'skipped': skipped, 'records': records}
    if validation:
        resp['validation'] = validation
    return jsonify(resp), 201


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
    # 2026-09-12: 拷贝纸/日本纸行禁止走普通 /images 端点 → 必须用 /copy-paper-images
    # 2026-10-04: 扩展到腊光纸(0108)—— 包装贴纸与商品本身不一致,OCR 误识刷红牌
    # 后端硬拦截,避免 UI 隐藏被绕过(DevTools / 粘贴 / 拖拽)误触发 OCR 流水线产生误导性红 ✗
    # 触发于出货订单 986 / record_id=2740(日本纸777/700)的 image_id=4370 误识别案例
    if _is_no_ai_match_item(record):
        return jsonify({
            'success': False,
            'error': '该明细为非 OCR 类(拷贝纸/日本纸/快巴纸/腊光纸),不需 OCR 比对'
        }), 400

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
    return jsonify({'success': True, 'scale': scale, 'marks': PlacementImage.get_marks(image_id), **_placement_match_response(img)})


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
    return jsonify({'success': True, 'count': count, **_placement_match_response(img)})


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
    return jsonify({'success': True, 'manual_count': count, **_placement_match_response(img)})


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
    return jsonify({'success': True, 'marks': PlacementImage.get_marks(image_id), **_placement_match_response(img)})


@bp.route('/api/v1/shipping-orders/placement-images/<int:image_id>/marks/last', methods=['DELETE'])
def api_v1_shipping_orders_placement_mark_undo(image_id):
    """撤销最近一枚计数点(连续可撤销)。返回剩余计数点列表。"""
    img = PlacementImage.get_by_id(image_id)
    if not img:
        return jsonify({'success': False, 'error': '摆放图不存在'}), 404
    marks = PlacementImage.delete_last_mark(image_id)
    return jsonify({'success': True, 'marks': marks, **_placement_match_response(img)})


@bp.route('/api/v1/shipping-orders/placement-images/<int:image_id>/unload', methods=['POST'])
def api_v1_shipping_orders_placement_unload(image_id):
    """勾选 / 取消「卸载货物」(点数弹框内卸载 checkbox)。body: {unload: bool}。

    is_unload=1 时,record 总数扣减(模型层 signed_count 取负);影响 placement_match。
    """
    img = PlacementImage.get_by_id(image_id)
    if not img:
        return jsonify({'success': False, 'error': '摆放图不存在'}), 404
    data = request.get_json() or {}
    flag = bool(data.get('unload', False))
    is_unload = PlacementImage.set_unload(image_id, flag)
    return jsonify({'success': True, 'is_unload': is_unload, **_placement_match_response(img)})


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

    if engine_name not in ('moonshot', 'paddleocr', 'deepseek', 'minimax', 'paddleocr_minimax'):
        return jsonify({
            'success': False,
            'error': f'不支持的识别引擎: {engine_name}',
            'hint': '请使用 "moonshot"、"paddleocr"、"deepseek"、"minimax" 或 "paddleocr_minimax"'
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
            'hint': '请使用 "moonshot"、"paddleocr"、"deepseek"、"minimax" 或 "paddleocr_minimax"'
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
        # 【2026-09-15】重复检测:对照订单已有明细,对重复项标 duplicate=true,
        # 前端智能添加弹框渲染红色 ⚠️ 警示,让用户手动核对而不是盲目批量入库。
        # 场景:订单 1011 (KILA GROUP装柜) AI 误把 3 条数量(3504/2001/3483)
        # 串行分配到错的品名+规格,新增的 2853/2854/2855 与原 2827/2828/2829 数量一致。
        order_id_str = (request.form.get('order_id') or request.args.get('order_id') or '').strip()
        if order_id_str and result.get('items'):
            try:
                order_id_int = int(order_id_str)
                existing_records = ShippingRecord.get_by_order(order_id_int) if hasattr(ShippingRecord, 'get_by_order') else []
                if not existing_records:
                    from models._db import get_db as _get_db
                    _conn = _get_db(); _cur = _conn.cursor()
                    _cur.execute('SELECT id, product_name, specification, quantity, unit FROM shipping_records WHERE order_pk = ? ORDER BY sort_order, id', (order_id_int,))
                    existing_records = [dict(r) for r in _cur.fetchall()]
                    _conn.close()
                for item in result['items']:
                    if not isinstance(item, dict): continue
                    qty = (item.get('quantity') or '').strip()
                    pn = (item.get('product_name') or '').strip()
                    sp = (item.get('specification') or '').strip()
                    if not qty: continue
                    # 三层重复: 数量完全相同(强); 品名+规格+数量 三项一致(最强)
                    same_qty = [r for r in existing_records if str(r.get('quantity') or '').strip() == qty]
                    same_full = [r for r in same_qty if str(r.get('product_name') or '').strip() == pn and str(r.get('specification') or '').strip() == sp]
                    if same_full:
                        item['duplicate'] = 'full'
                        item['duplicate_existing_id'] = same_full[0]['id']
                    elif same_qty:
                        item['duplicate'] = 'quantity_only'
                        item['duplicate_existing_ids'] = [r['id'] for r in same_qty]
                    if item.get('duplicate'):
                        current_app.logger.info(
                            'AI-识别 标记重复: order=%s pn=%s sp=%s qty=%s kind=%s',
                            order_id_int, pn, sp, qty, item['duplicate'])
            except (ValueError, Exception) as e:
                current_app.logger.warning('ai-recognize duplicate check 失败: %s', e)
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
    # 2026-10-04: 拷贝纸/日本纸/快巴纸/腊光纸行直接跳过整轮 AI 比对
    # 单点切片覆盖四路径:
    #   - OCR 循环不遍历腊光纸图(省 PaddleOCR)
    #   - rows 不含腊光纸 → DeepSeek 不知道存在
    #   - own_record_ids 不含腊光纸 → 即便 LLM 幻觉出 id,foreign-id 兜底会跳过
    #   - verdicts 循环不会调 set_match / set_human_verified(False) / OcrMatchEvent.create
    records = [r for r in records if not _is_no_ai_match_item(r)]

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
# 拷贝纸/日本纸 标签图(2026-09-06 创建 / 2026-09-09 重构)
#
# **2026-09-09**:原 copy_paper_images 表已废弃 —— 标签图直接存
# `shipping_images(source='copy_paper_label', record_pk=<明细 id>)`,与订单图同表、
# 靠 source 区分,不再单独建表:
#   - 渲染:自动进入 order_non_ai(见本文件图片分组循环),模板按 source 特判
#     (走独立循环,不渲染 OCR / 模糊匹配 / AI判别 / match-badge)
#   - 删除:复用通用端点 DELETE /api/v1/shipping-orders/images/<id>
#     (已带锁单防御 + 审计),不再单独提供 DELETE 端点
#   - 点数:统一走 placement 体系(shipping_images(source='placement'))
# ────────────────────────────────────────────────────────────

@bp.route('/api/v1/shipping-orders/records/<int:rid>/copy-paper-images', methods=['POST'])
def api_v1_shipping_orders_record_no_ai_label_upload(rid):
    """上传拷贝纸/日本纸 标签照 → 写 shipping_images(source='copy_paper_label')。

    完全跳过 OCR pipeline(不做 OCR/AI 比对,仅供人工留档)。
    支持 multipart(字段名 `image`)与 JSON base64(`image` 字段)。
    锁单 → 403;记录不存在 → 404;无图 → 400。
    """
    set_log_context(biz='shipping', record_id=rid, evt_src='copy_paper_upload')
    record = ShippingRecord.get_by_id(rid)
    if not record:
        return jsonify({'success': False, 'error': '记录不存在'}), 404
    order = ShippingOrder.get_by_id(record['order_pk'])
    if order and order.get('is_locked'):
        return jsonify({'success': False, 'error': '该订单已锁定,无法上传拷贝纸/日本纸'}), 403

    upload_dir, _month_str = _get_upload_dir()

    # source 兼容:历史前端会传 'label'('count' 已随张数路径废弃),统一忽略
    if request.is_json:
        _payload = request.get_json(silent=True) or {}
        source = (_payload.get('source') or '').strip()
    else:
        source = (request.form.get('source') or '').strip()
    if source not in ('', 'label'):
        return jsonify({'success': False, 'error': 'source 只支持 label'}), 400

    # 两种格式都支持:multipart(字段名 `image`,移动端历史用 `file`)或 JSON base64
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

    # 2026-09-09: 直接落 shipping_images,source='copy_paper_label'
    new_id = ShippingImage.create(
        record['order_pk'], filepath, original_name,
        source=NO_AI_LABEL_SOURCE, record_pk=rid)
    img = ShippingImage.get_by_id(new_id)
    rel_path = os.path.join(_month_str, os.path.basename(filepath)).replace('\\', '/')
    img['file_path'] = filepath  # 全路径(与 PlacementImage 一致)
    img['relative_path'] = rel_path
    AuditLog.log('upload_copy_paper_image', 'shipping_order', record['order_pk'],
                 detail={'filename': rel_path,
                         'source': NO_AI_LABEL_SOURCE, 'record_id': rid})
    return jsonify({'success': True, 'image': img})



# ─────────────────────────────────────────────────────────
# Task 4 (2026-09-06): record 级别拷贝纸/日本纸 富化
# ─────────────────────────────────────────────────────────

def _is_no_ai_match_item(item: dict) -> bool:
    """判断 record 是否属于「只走标签图、不做 OCR/AI 比对」的品类。

    覆盖 拷贝纸 / 日本纸 / 快巴纸 / 腊光纸(0105/0106/0107/0108)—— 这类的包装
    贴纸与商品本体对不上,行级图走 OCR 只会刷误导性红 ✗。它们的图片入口只有
    「🖼️ 标签图」按钮(source='copy_paper_label',落 shipping_images,不做 OCR)。

    双兜底:
    1. 关键词匹配(product_name 含 NO_AI_MATCH_KEYWORDS 任意一个)
       '蜡光' 是 '腊光' 的常见错别字,同义,防止错字漏判
    2. classify_record 查 product_categories 拿到 category_code,
       是 NO_AI_MATCH_CATEGORY_CODES 任一时返回 True

    **2026-10-08**:原 `_is_copy_paper_item`(只覆盖 0105/0106/0107)已并入本函数,
    二者关键词集/品类码集互相包含,合并后 classify_record 每行只查一次。
    """
    name = (item.get('product_name') or '').strip()
    if any(k in name for k in NO_AI_MATCH_KEYWORDS):
        return True
    try:
        from models.category_prompt import classify_record
        result = classify_record(name, item.get('specification') or '')
        code = result.get('category_code') if isinstance(result, dict) else None
        return code in NO_AI_MATCH_CATEGORY_CODES
    except Exception:
        return False


def _enrich_no_ai_label_for_item(item: dict) -> None:
    """对单条 record 原地写入 copy-paper 字段。

    **2026-09-09 重构**:点数已统一走 placement 体系
    (存 `shipping_images(source='placement')`,复用 placement_count.js 与全部端点);
    标签图也迁入 `shipping_images(source='copy_paper_label')`,**copy_paper_images 表已废弃**。

    写入字段:
      is_no_ai_match      → 是否 拷贝纸/日本纸/快巴纸/腊光纸(0105-0108)。
                            模板据此:标签 🖼️ 按钮**显示**、普通 🖼️ 按钮**隐藏**
      label_images        → 该 record 的标签图列表(移动端缩略图用;
                            PC 端由 order-images-area 按 source 直接渲染,不读此字段)
      has_label_image     → 是否已上传标签图(标签按钮红框反馈)

    已废弃并移除(勿再引用):
      is_copy_paper       → 2026-10-08 删除,谓词已合并进 _is_no_ai_match_item
      copy_paper_images / copy_paper_total / copy_paper_match / copy_paper_expected
      —— 原为「张数录入」(sheet_count)服务,该路径随点数走 placement 而废弃;
         sheet_count 恒为 NULL 导致 match 恒为 'yellow',会渲染误导性的
         "⚠ 张数不符 0≠300" 徽章,与新 placement 点数体系语义冲突。
    """
    item['is_no_ai_match'] = _is_no_ai_match_item(item)
    if not item['is_no_ai_match']:
        item['label_images'] = []
        item['has_label_image'] = False
        return

    # 优先复用调用方已查好的本行图缓存(PC 端图片分组循环注入),否则现查
    _cache = item.get('_record_imgs_cache')
    imgs = _cache if _cache is not None else ShippingImage.get_by_record(item['id'])
    labels = [i for i in imgs if i.get('source') == NO_AI_LABEL_SOURCE]
    item['label_images'] = labels
    # 标签按钮已上传反馈(红框),与点数状态无关
    item['has_label_image'] = bool(labels)
