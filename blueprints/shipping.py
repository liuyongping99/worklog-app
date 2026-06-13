"""出货记录蓝图。

包含：
- /shipping-records 页面（带日期过滤、商品单位提示计算）
- /api/v1/shipping-orders/* REST API（订单/明细/图片 CRUD + 移动 + AI 识别）
"""
import os
import uuid
import base64
import json
import re
from datetime import date as date_cls, timedelta, datetime
from flask import Blueprint, render_template, request, jsonify, current_app
from models import (
    ShippingOrder, ShippingRecord, ShippingImage, ProductUnit, AuditLog
)
from blueprints._helpers import get_upload_dir as get_helpers_upload_dir, get_ypp, calc_hint, check_remark, summarize_remarks

bp = Blueprint('shipping', __name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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
    # order_pk -> 图片列表
    order_ids = [g['id'] for g in groups]
    order_images = {oid: ShippingImage.get_by_order(oid) for oid in order_ids}

    units = ProductUnit.get_all()
    unit_list = [{
        'product_name': u['product_name'],
        'spec_keyword': u['spec_keyword'] or '',
        'yards_per_piece': u['yards_per_piece'] / 100.0,
        'is_usingyardforcounting': bool(u['is_usingyardforcounting'])
    } for u in units]

    def _get_ypp(product_name, spec):
        return get_ypp(product_name, spec, units_cache=units)

    def _calc_hint(quantity_str, ypp):
        return calc_hint(quantity_str, ypp)

    def _check_remark(remark, quantity_str, ypp):
        return check_remark(remark, quantity_str, ypp)

    for group in groups:
        for item in group['records']:
            ypp = _get_ypp(item['product_name'], item.get('specification', ''))
            item['unit_hint'] = _calc_hint(item['quantity'], ypp)
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
        group.update(summarize_remarks(group['records']))

    return render_template(
        'shipping-records.html',
        groups=groups,
        order_images=order_images,
        page_title='出货记录',
        today=today,
        start_date=start_date,
        end_date=end_date,
        unit_list=unit_list,
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
        cols = int(data['img_cols'])
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
            return jsonify({'success': False, 'error': str(e)}), 400
        conn.close()
        return jsonify({'success': True, 'date': new_date, 'customer': new_customer, 'order_note': note})

    if 'order_note' in data:
        # 备注功能：解锁时才能改（防止误操作）
        if order.get('is_locked'):
            return jsonify({'success': False, 'error': '订单已锁定，无法修改备注'}), 403
        note = str(data['order_note']).strip()
        ShippingOrder.set_note(order_id, note)
        return jsonify({'success': True, 'order_note': note})

    if 'customer' in data:
        if order.get('is_locked'):
            return jsonify({'success': False, 'error': '订单已锁定，无法修改客户名称'}), 403
        new_customer = str(data['customer']).strip()
        if not new_customer:
            return jsonify({'success': False, 'error': '客户名称不能为空'}), 400
        result = ShippingOrder.set_customer(order_id, new_customer)
        if result.get('success'):
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

    ShippingRecord.update(record_id, data)
    updated = ShippingRecord.get_by_id(record_id)
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
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': '无法移动（已是首条/末条）'}), 400


# ── 图片 CRUD ──────────────────────────────────────────────
@bp.route('/api/v1/shipping-orders/<int:order_id>/images', methods=['POST'])
def api_v1_shipping_orders_upload_image(order_id):
    """上传出货单图片 → 201"""
    order = ShippingOrder.get_by_id(order_id)
    if not order:
        return jsonify({'success': False, 'error': '订单不存在'}), 404

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
            original_name = 'pasted_image'
        else:
            return jsonify({'success': False, 'error': '无效的图片数据'}), 400
    elif 'image' in request.files:
        file = request.files['image']
        if file.filename:
            ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else 'png'
            if ext not in ['png', 'jpg', 'jpeg', 'gif', 'webp']:
                ext = 'png'
            filename = f"{uuid.uuid4().hex}.{ext}"
            filepath = os.path.join(upload_dir, filename)
            file.save(filepath)
            original_name = file.filename
        else:
            return jsonify({'success': False, 'error': '未选择文件'}), 400
    else:
        return jsonify({'success': False, 'error': '未提供图片'}), 400

    image_id = ShippingImage.create(order_id, filepath, original_name)
    rel_path = os.path.join(month_str, filename).replace('\\', '/')
    return jsonify({'success': True, 'image_id': image_id, 'image': rel_path, 'file_path': filepath, 'original_name': original_name}), 201


@bp.route('/api/v1/shipping-orders/images/<int:image_id>', methods=['DELETE'])
def api_v1_shipping_orders_delete_image(image_id):
    """删除出货记录图片 → 200"""
    if ShippingImage.delete(image_id):
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': '图片不存在'}), 404


# ── AI 图片识别（Kimi k2.6 Vision） ──────────────────────
# 从 .env 读取，缺失则 AI 识别功能不可用
MOONSHOT_API_KEY = os.environ.get('MOONSHOT_API_KEY', '')
MOONSHOT_VISION_URL = 'https://api.moonshot.cn/v1/chat/completions'

# AI 识别提示词（表格 → JSON 数组）
# 集中放这里方便调整，不影响路由逻辑
AI_RECOGNIZE_PROMPT = '''你是一个商品信息提取助手。请仔细看这张图片，识别其中的商品列表。
图片中可能包含出货单、送货单、报价单、库存表等。
请提取所有商品行，返回 JSON 数组格式。每条商品包含以下字段：
- product_name: 商品名称（必须）
- specification: 规格型号（必须）
- quantity: 数量（必须）
- unit: 单位，如支、kg、y、桶、件、箱等（必须）
- remark: 备注（如有则填，无则空字符串）

注意事项：
1. 如果是表格图片，按行提取；提取表格中的内容，不要加标题，不要序号。不要提取标题，只提取表格中的内容。如果是手写/打印的文字描述，也请尽可能提取
2. 单位请标准化：
   - "支"、"PCS"、"pc"、"个" → "支"
   - "码"、"y"、"Y" → "y"
   - "桶" → "桶"
   - "件" → "件"
   - "箱" → "箱"
   - "张" → "张"
   - "块" → "块"
3. 如果某行只有部分信息，quantity默认为"1"，remark填"信息不完整"
4. 只返回 JSON 数组，不要其他解释文字，不要 markdown 代码块，直接返回纯 JSON
5. 示例格式：[{"product_name":"PVC桌布","specification":"1.2×1.8m","quantity":"50","unit":"支","remark":""},{"product_name":"无纺布","specification":"2m","quantity":"30","unit":"kg","remark":""}]
6. 如果图片中没有任何可识别的商品行，返回空数组：[]
7. 请务必只返回纯 JSON 数组，不要包含任何其他文字说明'''


@bp.route('/api/v1/shipping-orders/ai-recognize', methods=['POST'])
def shipping_records_ai_recognize():
    """接收图片，调用 Kimi k2.6 Vision 识别商品表格，返回 JSON"""
    try:
        import openai
    except ImportError:
        return jsonify({
            'success': False,
            'error': 'openai 包未安装',
            'hint': '请运行: pip install openai'
        }), 500

    # 提前校验 API key 是否配置（避免走到 openai 客户端才报错）
    if not MOONSHOT_API_KEY or MOONSHOT_API_KEY == 'sk-your-moonshot-api-key':
        return jsonify({
            'success': False,
            'error': 'AI 识别功能未配置',
            'hint': '请在 .env 中设置 MOONSHOT_API_KEY（申请地址 https://platform.moonshot.cn）后重启应用'
        }), 503

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
    img_b64 = base64.b64encode(img_bytes).decode('utf-8')

    if file.filename.lower().endswith('.png'):
        mime = 'image/png'
    elif file.filename.lower().endswith(('.jpg', '.jpeg')):
        mime = 'image/jpeg'
    elif file.filename.lower().endswith('.gif'):
        mime = 'image/gif'
    elif file.filename.lower().endswith('.webp'):
        mime = 'image/webp'
    else:
        mime = 'image/jpeg'
    data_url = f'data:{mime};base64,{img_b64}'

    try:
        client = openai.OpenAI(
            api_key=MOONSHOT_API_KEY,
            base_url='https://api.moonshot.cn/v1'
        )
        response = client.chat.completions.create(
            model='kimi-k2.6',
            messages=[{
                'role': 'user',
                'content': [
                    {'type': 'image_url', 'image_url': {'url': data_url}},
                    {'type': 'text', 'text': AI_RECOGNIZE_PROMPT}
                ]
            }],
            max_tokens=4000,
            temperature=1,  # Moonshot kimi-k2.6 限制：temperature 必须为 1
            timeout=60  # 防止 API 挂起导致 Flask 请求永久阻塞
        )
        raw_text = response.choices[0].message.content.strip()
        # 剥离 markdown 围栏:`​`​`json\n...\n​`​`​` 或裸 `​`​`...`​`​`,允许首尾空行
        # 用正则替代 split,避免末尾空行/不规范围栏吃掉数据末字符
        raw_text = re.sub(r'^\s*```[a-zA-Z]*\s*\n?', '', raw_text)
        raw_text = re.sub(r'\n?\s*```\s*$', '', raw_text)
        raw_text = raw_text.strip()
        items = json.loads(raw_text)
        if not isinstance(items, list):
            return jsonify({
                'success': False,
                'error': 'AI 返回的数据格式无法解析',
                'hint': '图片可能不够清晰，建议手动输入或换张图片重试'
            }), 500
        return jsonify({'success': True, 'items': items})

    except openai.AuthenticationError:
        # 401: API key 失效或被作废（最常见）
        return jsonify({
            'success': False,
            'error': 'AI 识别 API key 失效（401）',
            'hint': '请到 Moonshot 控制台（https://platform.moonshot.cn）重新生成 API key，更新 .env 里的 MOONSHOT_API_KEY 后重启应用'
        }), 401
    except openai.RateLimitError:
        # 429: 调用频率过高
        return jsonify({
            'success': False,
            'error': 'AI 调用频率过高（429）',
            'hint': '请稍候几秒后重试'
        }), 429
    except openai.APIConnectionError:
        # 网络问题
        return jsonify({
            'success': False,
            'error': '无法连接 Moonshot 服务',
            'hint': '请检查网络连接，或稍后重试'
        }), 502
    except openai.APIStatusError as e:
        # 其他 4xx/5xx
        return jsonify({
            'success': False,
            'error': f'AI 服务返回异常（{e.status_code}）',
            'hint': '请稍后重试，或检查 Moonshot 平台状态'
        }), 500
    except json.JSONDecodeError:
        return jsonify({
            'success': False,
            'error': 'AI 返回的数据格式无法解析',
            'hint': '图片可能不规范，建议手动输入或换张图片'
        }), 500
    except Exception as e:
        # 兜底：记录原始错误，但只给用户看通用提示
        current_app.logger.exception('AI 识别未预期错误: %s', e)
        return jsonify({
            'success': False,
            'error': f'识别失败：{type(e).__name__}',
            'hint': '请稍后重试；如反复出现请联系管理员查看应用日志'
        }), 500
