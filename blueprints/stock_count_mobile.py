# -*- coding: utf-8 -*-
"""移动端盘点 —— 阶段 6（站在货堆前用手机录进**同一张盘点单**）。

⭐ **本阶段最重要的一条:移动端不新增写路径** ——
   录数 / 批量 / 提交 / 预览 **全部调用阶段 5 的同一套 REST 端点**
   (`/api/v1/stock/counts/*`),连服务层都是同一个 `blueprints/stock_counting.py`。
   移动端只负责「把同一套 API 用起来」:大按钮 + 失焦即存 + 按分类分组 + 搜索。

   为什么这么定:
     · 完成判据 2「手机录完提交 → 与阶段 5 的提交路径完全一致(同一套 API)」——
       复制一条写路径 = 两套逻辑 = 两套 bug;
       **A5 的差额算法绝不能有两个实现**(基准余额 / 期间变动的切分写错一处,
       账就错了,而且错了很难发现)。
     · A5 差额 / 幂等键 / 单一事务 / A8 启用全在服务层一处,移动端换个 UI 调用它。

页面(都在 `/m/` 前缀下 → 已在 `app.py._AUTH_PUBLIC_PREFIXES` 白名单里,免登录):
  GET  /m/stock-count              盘点单列表(仓库 / 状态过滤 + 新建 + 建单弹框)
  GET  /m/stock-count/<id>         盘点详情(按分类分组 / 搜索 / 快速录数 / 提交)

端点(移动端**只读 + 两个辅助**,不碰库存):
  POST /m/stock-count/identity       选身份(写 session.operator_id)—— A7 要有操作人
  POST /m/stock-count/ocr-prefill    拍照 → OCR 抽数字 → 预填(只预填,不过账)

⚠️ **免登录 ≠ 无痕**:盘点是**改账**操作,A7 要求「谁改的」可追溯。
   所以移动端页面能免登录打开,但**写操作前必须选一次身份**(选人即登录,
   复用 `models.tasks_flow.StaffDB`);选完 session.operator_id 就有了,
   之后所有 `/api/v1/stock/counts/*` 调用都自带操作员(A7 自动注入)。
"""
import io
import re
from datetime import datetime as _datetime

from flask import (Blueprint, abort, current_app, jsonify, render_template,
                   request, session)

from models import StockCount, StockCountItem, Warehouse
from models.products import ProductCategory
from models.tasks_flow import StaffDB

from blueprints.ocr_log import set_log_context
from blueprints.stock import _resolve_warehouse
# 复用阶段 5 的 payload 构造 + 仓库名映射(同一套形状,PC / 手机长得一样)
from blueprints.stock_count import _count_payload, _item_payload, _warehouse_names

bp = Blueprint('stock_count_mobile', __name__)

# 状态 → 中文标签(与 blueprints/stock_count.py 的 STATUS_LABELS 一致)
STATUS_LABELS = {'open': '进行中', 'closed': '已提交'}


# ── 小工具 ──────────────────────────────────────────────────────────
def _current_operator():
    """当前操作员 {id, name, role} 或 None。"""
    op_id = session.get('operator_id')
    if op_id is None:
        return None
    op = StaffDB.get_by_id(int(op_id))
    if not op:
        return None
    return {'id': int(op['id']), 'name': op.get('name') or '',
            'role': op.get('role') or ''}


def _group_items(items):
    """明细行 → 按分类分组(移动端按类翻比按字母翻快)。

    · 没有分类的行(category_id 为空)归到「未分类」组 —— 不能丢。
    · 组顺序:分类 sort_order → 分类名(与库存页的排序口径一致)。
    """
    groups = {}
    for it in items:
        cid = it.get('category_id')
        key = int(cid) if cid is not None else 0
        g = groups.get(key)
        if g is None:
            sort_order = it.get('category_sort_order')
            groups[key] = g = {
                'category_id': key or None,
                'category_name': (it.get('category_name') or '').strip() or '未分类',
                'sort_order': sort_order if sort_order is not None else 9999,
                'items': [],
                'total': 0,
                'counted': 0,
            }
        g['items'].append(it)
        g['total'] += 1
        if it.get('actual_quantity') is not None:
            g['counted'] += 1
    return sorted(groups.values(), key=lambda g: (g['sort_order'], g['category_name']))


def _flat_categories():
    """分类 → 带缩进的一维列表(移动端建单弹框的下拉用)。

    移动端不做分类树交互(点开/折叠太费屏幕),用缩进表达层级就够。
    ⚠️ 父节点不在表里的「孤儿」也要列出来,别静默丢分类。
    """
    rows = ProductCategory.get_all()
    children, roots = {}, []
    for r in rows:
        pid = r.get('parent_id')
        if pid is None:
            roots.append(r)
        else:
            children.setdefault(int(pid), []).append(r)

    out = []

    def _node(r, depth):
        return {
            'id': int(r['id']),
            'name': r.get('category_name') or '',
            'code': r.get('category_code') or '',
            'level': depth,
            'label': ('　' * depth) + (r.get('category_name') or ''),
        }

    def _walk(nodes, depth):
        for r in nodes:
            out.append(_node(r, depth))
            _walk(children.get(int(r['id']), []), depth + 1)

    _walk(roots, 0)
    listed = {c['id'] for c in out}
    for r in rows:                      # 孤儿兜底
        if int(r['id']) not in listed:
            out.append(_node(r, 0))
    return out


# ── 页面 ────────────────────────────────────────────────────────────
@bp.route('/m/stock-count')
def mobile_stock_count_list():
    """移动端盘点单列表:今天要盘的单(按仓库 / 状态过滤)+ 新建入口。"""
    set_log_context(biz='stock_count_mobile')
    status = (request.args.get('status') or '').strip() or None
    try:
        warehouse = _resolve_warehouse(request.args.get('warehouse_id'))
    except ValueError as e:
        abort(400, description=str(e))
    wid = int(warehouse['id'])

    counts = StockCount.list_all(status=status, warehouse_id=wid, limit=200)
    wh_names = _warehouse_names()
    payload = [_count_payload(c, wh_name=wh_names.get(int(c['warehouse_id']), ''))
               for c in counts]

    all_counts = StockCount.list_all(warehouse_id=wid, limit=1000)
    stats = {
        'total': len(all_counts),
        'open': sum(1 for c in all_counts if c.get('status') == 'open'),
        'closed': sum(1 for c in all_counts if c.get('status') == 'closed'),
    }

    return render_template(
        'mobile/stock-count.html',
        page_title='盘点',
        counts=payload,
        stats=stats,
        warehouses=Warehouse.list_all(),
        warehouse=warehouse,
        status=status,
        categories=_flat_categories(),
        operator=_current_operator(),
        now_label=_datetime.now().strftime('%m-%d'),
        staff_list=[{'id': int(s['id']), 'name': s.get('name') or '',
                     'role': s.get('role') or ''}
                    for s in StaffDB.get_active()],
    )


@bp.route('/m/stock-count/<int:count_id>')
def mobile_stock_count_detail(count_id: int):
    """移动端盘点详情:按分类分组 + 搜索 + 快速录数。"""
    set_log_context(biz='stock_count_mobile', count_id=count_id)
    count = StockCount.get_by_id(count_id)
    if not count:
        abort(404, description='盘点单不存在')

    items = []
    for raw in StockCountItem.list_by_count(count_id):
        p = _item_payload(raw)
        # 带上分类字段 → 下面按分类分组(移动端按类翻)
        p['category_id'] = raw.get('category_id')
        p['category_name'] = raw.get('category_name') or ''
        p['category_sort_order'] = raw.get('category_sort_order')
        items.append(p)
    groups = _group_items(items)

    payload = _count_payload(count, items=items)
    wh = Warehouse.get_by_id(int(count['warehouse_id']))

    return render_template(
        'mobile/stock-count-detail.html',
        page_title=f"盘点 · {count.get('title') or count_id}",
        count=payload,
        groups=groups,
        warehouse=wh,
        operator=_current_operator(),
    )


# ── 端点 · 选身份(A7)────────────────────────────────────────────────
@bp.route('/m/stock-count/identity', methods=['POST'])
def mobile_identity():
    """选身份(写 session.operator_id)。

    为什么移动端要这一步:`/m/*` 免登录是「页面能直接打开」的意思,
    但盘点是**改账** —— A7 要求 audit_log 能回答「谁改的」。
    这里复用现成的「选人即登录」模型(和 `/login` 一个口径),
    选完之后所有 `/api/v1/stock/counts/*` 调用都自带操作员。
    """
    data = request.get_json(silent=True) or {}
    # ⚠️ JSON 里的 staff_id 可能是 int → 统一转 str 再 strip
    raw = str(data.get('staff_id') or request.form.get('staff_id') or '').strip()
    if not raw:
        return jsonify({'success': False, 'error': '请选择一个人'}), 400
    try:
        staff_id = int(raw)
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': '无效的人员 id'}), 400

    staff = StaffDB.get_by_id(staff_id)
    if not staff or staff.get('is_active') != 1:
        return jsonify({'success': False, 'error': '该人员不存在或已停用'}), 400

    session['operator_id'] = int(staff['id'])
    session.permanent = True
    return jsonify({'success': True,
                    'operator': {'id': int(staff['id']),
                                 'name': staff.get('name') or '',
                                 'role': staff.get('role') or ''}})


# ── 端点 · 拍照辅助(OCR 预填)────────────────────────────────────────
# 数量候选的抽取规则:
#   强候选 = 带单位(120支 / 35码)或带关键词(数量:88 / 合计 120)
#   弱候选 = 独立整数
# ⚠️ **先挖掉规格噪声再抽**(顺序不能反):
#   「A1.5米 200g」里的 1.5 是**幅宽**、200 是**克重** —— 都是规格不是数量。
#   本行货的规格就是靠这两类数字写的,认成数量等于凭空多算一堆。
#   所以先把 `数字+规格单位` 整段挖掉,剩下的数字才有资格当数量。
# ⚠️ **只预填,绝不过账** —— OCR 认错了也只是把数字填进输入框,
#   人还能改;要是自动过账,一个错字就是一笔假账。
_COUNT_KEYWORD_RE = re.compile(
    r'(?:数量|件数|合计|共计|总共|总数|实数|实盘|支数|码数)\s*[:：]?\s*(\d+(?:\.\d+)?)')
# 数量单位:支 / 码 / 件 …(**米不算** —— 本行货「米」永远是幅宽规格)
_COUNT_UNIT_RE = re.compile(
    r'(\d+(?:\.\d+)?)\s*(支|码|件|匹|令|卷|包|条|台|公斤|千克|kg)')
# 规格噪声:这些数字是规格的一部分,不是数量(幅宽 / 克重 / 尺寸 / 乘法式)
_SPEC_NOISE_RE = re.compile(
    r'\d+(?:\.\d+)?\s*(?:mm|cm|kg|克重|克|米|m|平方|g|\*|x|×|码宽)')


def _extract_count_candidates(text):
    """OCR 文本 → 数量候选列表(带来源标签,前端让人点一下选)。

    ⚠️ 「米」在量词表里**故意没有**:本行货的规格写成「A1.5米」,
       把 1.5 当成数量就是凭空多算。规格噪声正则会先把「1.5米」整段挖掉。
    """
    if not text:
        return []
    # ① 先挖规格噪声
    masked = _SPEC_NOISE_RE.sub(' ', text)
    strong, weak, seen = [], [], set()

    def _push(bucket, value, label):
        try:
            val = float(value)
        except (TypeError, ValueError):
            return
        if val < 0:
            return
        key = ('%g' % val)
        if key in seen:
            return
        seen.add(key)
        bucket.append({'value': val, 'label': label})

    for m in _COUNT_KEYWORD_RE.finditer(masked):
        _push(strong, m.group(1), m.group(0).strip())
    for m in _COUNT_UNIT_RE.finditer(masked):
        _push(strong, m.group(1), m.group(0).strip())

    # ② 弱候选:剩下的独立整数
    for m in re.finditer(r'(?<![\d.])\d+(?![\d.])', masked):
        _push(weak, m.group(0), m.group(0))

    return strong + weak


@bp.route('/m/stock-count/ocr-prefill', methods=['POST'])
def mobile_ocr_prefill():
    """拍照 → OCR 抽文字 → 抽出数量候选 → 回给前端预填。

    ⚠️ **只做预填,不做自动过账**(计划 §阶段 6 · 6.4 明确要求)。
    ⚠️ 用**本地 PaddleOCR** 抽文字(免费 / 离线 / 不外传),
       云端引擎(moonshot / deepseek)要花钱、要有网,现场盘点用不上。
    ⚠️ PaddleOCR 首次调用要加载模型(~200MB,十几秒)→ 之后走单例缓存。
       识别失败**不阻塞流程** —— 返回错误文案,用户手输即可。
    """
    set_log_context(biz='stock_count_mobile')
    f = request.files.get('file') or request.files.get('image')
    if not f or not f.filename:
        return jsonify({'success': False, 'error': '未选择图片'}), 400

    try:
        from blueprints._helpers import check_uploaded_image
        check_uploaded_image(f)
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:  # pragma: no cover - 校验器内部异常
        current_app.logger.exception('图片校验失败')
        return jsonify({'success': False, 'error': f'图片校验失败: {e}'}), 400

    data = f.read()
    # 内存里再做一次真实解码(Pillow)—— 防止伪装文件
    try:
        from PIL import Image, UnidentifiedImageError
        with Image.open(io.BytesIO(data)) as im:
            im.verify()
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError):
        return jsonify({'success': False, 'error': '图片无法解码,请换一张'}), 400
    except Exception:
        return jsonify({'success': False, 'error': '图片无法解码,请换一张'}), 400

    text = ''
    engine_name = 'paddleocr'
    try:
        from blueprints.ocr_engine import get_ocr_engine
        engine = get_ocr_engine(engine_name)
        text = engine.extract_text(data) or ''
    except Exception as e:
        current_app.logger.warning('OCR 预填失败(本地引擎): %s', e)
        return jsonify({
            'success': False,
            'error': '拍照识别不可用,请手动输入数量',
            'detail': f'{type(e).__name__}: {e}',
        }), 200

    candidates = _extract_count_candidates(text)
    return jsonify({
        'success': True,
        'engine': engine_name,
        'candidates': candidates,
        'quantity': candidates[0]['value'] if candidates else None,
        'ocr_text': text[:2000],
    })
