# -*- coding: utf-8 -*-
"""盘点蓝图 —— 阶段 5（PC 单据页）。

盘点是**一项作业**：拿清单去数 → 分几批录 → 最后统一提交。
所以它是**单据**（stock_counts + stock_count_items），不是弹框。

页面:
  GET /stock-count              盘点单列表(状态徽章 / 新建弹框)
  GET /stock-count/<id>         盘点单详情(导出 / 粘贴 / 单行录入 / 提交确认)

REST:
  GET    /api/v1/stock/counts                      盘点单列表
  POST   /api/v1/stock/counts                      建单(4 种范围)
  GET    /api/v1/stock/counts/<id>                 详情(含明细)
  PATCH  /api/v1/stock/counts/<id>                 改名称 / 备注
  DELETE /api/v1/stock/counts/<id>                 删单(**仅未提交的草稿**)
  POST   /api/v1/stock/counts/<id>/items/<pid>     录实盘数(单行,失焦即存)
  DELETE /api/v1/stock/counts/<id>/items/<pid>     删一行(回到「待录」)
  POST   /api/v1/stock/counts/<id>/bulk-input       批量粘贴回填
  GET    /api/v1/stock/counts/<id>/export.csv       导出盘点表
  POST   /api/v1/stock/counts/<id>/submit/preview   提交预览(⭐ A5 四个数)
  POST   /api/v1/stock/counts/<id>/submit           提交(写 adjust 流水)

⚠️ **业务逻辑全在 blueprints/stock_counting.py(服务层)**,
   本文件只做 HTTP 层:参数校验 → 调服务 → 返回 JSON / 渲染页面。
   事务、A1/A2/A4/A5/A8 契约都在服务层,蓝图不碰写库细节。

贯穿全局的约定:
  · **提交后不可撤销** —— 已提交的盘点单不能改、不能删、不能重复提交(409)。
  · **未录的行不参与提交** —— 也不会被启用(A8:启用只走盘点 / 归零)。
  · **改账要留痕(A7)** —— 提交写 audit_log,操作人自动注入 session。
"""
from datetime import datetime

from flask import (Blueprint, Response, abort, current_app, jsonify,
                   render_template, request, session)

from models import ProductStock, StockCount, StockCountItem, Warehouse
from models.audit import AuditLog
from models.products import ProductCategory

from blueprints.stock import (_require_operator, _require_session,
                              _resolve_warehouse)
from blueprints.stock_counting import (CountError, MODE_INVENTORY, MODE_ZERO,
                                       SCOPE_ALL, SCOPE_CATEGORY, SCOPE_SEARCH,
                                       SCOPE_SELECTED, bulk_input, create_count,
                                       export_csv, preview_submit, submit_count)

bp = Blueprint('stock_count', __name__)
bp_api = Blueprint('stock_count_api', __name__, url_prefix='/api/v1')

# 建单范围(前端下拉用)
SCOPES = (
    (SCOPE_ALL, '全仓'),
    (SCOPE_CATEGORY, '按分类'),
    (SCOPE_SEARCH, '按搜索结果'),
    (SCOPE_SELECTED, '勾选商品'),
)

# 盘点状态 → 中文标签
STATUS_LABELS = {'open': '进行中', 'closed': '已提交'}


# ── 参数解析 ──────────────────────────────────────────────────────
def _json_body():
    """取 JSON body;不是合法 JSON 就返回空 dict(由各端点自己判缺字段)。"""
    data = request.get_json(silent=True)
    return data if isinstance(data, dict) else {}


def _count_or_404(count_id):
    """取盘点单,不存在 → 404。"""
    count = StockCount.get_by_id(count_id)
    if not count:
        abort(404, description='盘点单不存在')
    return count


def _require_open(count):
    """盘点单必须还是「进行中」才能改 —— 已提交的单据不可变(行业惯例)。"""
    if count.get('status') != 'open':
        raise CountError('盘点单已提交,不能再改', 409)


def _expand_category(raw):
    """分类 id → 含所有后代的 id 列表(与库存页口径一致)。

    ⚠️ 先验存在性:`descendant_ids` 对不存在的分类会返回 [cid] 自己,
       不校验的话「分类不存在」会被静默当成「该分类下有 1 个商品」。
    """
    if raw is None or str(raw).strip() == '':
        return None
    try:
        cid = int(raw)
    except (TypeError, ValueError):
        raise CountError('category_id 必须是整数')
    if not ProductCategory.get_by_id(cid):
        raise CountError(f'分类不存在(id={cid})')
    ids = ProductCategory.descendant_ids(cid)
    if not ids:
        raise CountError(f'分类不存在(id={cid})')
    return ids


def _parse_mode(raw):
    """提交模式:inventory(盘点) | zero(归零)。"""
    mode = (raw or MODE_INVENTORY).strip() or MODE_INVENTORY
    if mode not in (MODE_INVENTORY, MODE_ZERO):
        raise CountError('mode 只能是 inventory(盘点)或 zero(归零)')
    return mode


def _count_payload(count, items=None, wh_name=None):
    """盘点单 dict → 前端要的形状(补中文标签 + 仓库名)。

    ⚠️ `wh_name` 允许外部传入 —— 列表页有 N 张单,
       每行都 `Warehouse.get_by_id()` 的话就是 N 次开连接(N+1)。
       列表页统一查一次仓库表,按 id 映射传进来。
    """
    wid = int(count['warehouse_id'])
    if wh_name is None:
        wh = Warehouse.get_by_id(wid)
        wh_name = (wh or {}).get('name') or ''
    data = {
        'id': int(count['id']),
        'title': count.get('title') or '',
        'remark': count.get('remark') or '',
        'status': count.get('status') or 'open',
        'status_label': STATUS_LABELS.get(count.get('status') or 'open', '进行中'),
        'warehouse_id': wid,
        'warehouse_name': wh_name,
        'as_of': count.get('as_of') or '',
        'total_items': int(count.get('total_items') or 0),
        'counted_items': int(count.get('counted_items') or 0),
        'diff_items': int(count.get('diff_items') or 0),
        'created_by': count.get('created_by'),
        'created_at': count.get('created_at') or '',
        'closed_at': count.get('closed_at') or '',
    }
    if items is not None:
        data['items'] = items
    return data


def _warehouse_names():
    """{warehouse_id: name} —— 列表页一次性取,避免逐单查。"""
    return {int(w['id']): (w.get('name') or '') for w in Warehouse.list_all()}


def _item_payload(it):
    """盘点明细行 → 前端形状。"""
    book = float(it.get('book_quantity') or 0)
    actual = it.get('actual_quantity')
    diff = None if actual is None else float(actual) - book
    return {
        'product_id': int(it['product_id']),
        'product_code': it.get('product_code') or '',
        'product_name': it.get('product_name') or '',
        'specification': it.get('specification') or '',
        'base_unit': it.get('base_unit') or '',
        'book_quantity': book,
        'actual_quantity': actual,
        'diff': diff,
        'counted_at': it.get('counted_at') or '',
    }


# ── 页面 ──────────────────────────────────────────────────────────
@bp.route('/stock-count')
def stock_count_page():
    """盘点单列表。"""
    status = (request.args.get('status') or '').strip() or None
    try:
        warehouse = _resolve_warehouse(request.args.get('warehouse_id'))
    except ValueError as e:
        # ⚠️ 错误分支也必须把模板用到的每个变量都传全 ——
        #   Jinja2 的 Undefined **属性访问会抛 UndefinedError**，
        #   漏一个 `stats` 就会 500 而不是 400。
        return render_template('stock-count.html', page_title='盘点',
                               error=str(e), counts=[], warehouses=[],
                               warehouses_count=0, warehouse=None,
                               category_tree=[], status=status,
                               stats={'total': 0, 'open': 0, 'closed': 0},
                               scopes=SCOPES), 400

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

    warehouses = Warehouse.list_all()
    return render_template(
        'stock-count.html',
        page_title='盘点',
        counts=payload,
        stats=stats,
        warehouses=warehouses,
        warehouses_count=len(warehouses),
        warehouse=warehouse,
        category_tree=ProductCategory.get_tree(),
        status=status,
        scopes=SCOPES,
    )


@bp.route('/stock-count/<int:count_id>')
def stock_count_detail_page(count_id: int):
    """盘点单详情:明细表 + 导出 / 粘贴 / 单行录入 / 提交。"""
    count = StockCount.get_by_id(count_id)
    if not count:
        abort(404, description='盘点单不存在')
    items = [_item_payload(it) for it in StockCountItem.list_by_count(count_id)]
    payload = _count_payload(count, items=items)
    wh = Warehouse.get_by_id(int(count['warehouse_id']))

    return render_template(
        'stock-count-session.html',
        page_title=f"盘点 · {count.get('title') or count_id}",
        count=payload,
        items=items,
        warehouse=wh,
    )


# ── REST · 盘点单 CRUD ────────────────────────────────────────────
@bp_api.route('/stock/counts', methods=['GET'])
def api_counts_list():
    """盘点单列表。"""
    guard = _require_session()
    if guard:
        return guard
    status = (request.args.get('status') or '').strip() or None
    try:
        warehouse = _resolve_warehouse(request.args.get('warehouse_id'))
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    wid = int(warehouse['id'])
    counts = StockCount.list_all(status=status, warehouse_id=wid, limit=200)
    wh_names = _warehouse_names()
    return jsonify({
        'success': True,
        'warehouse': warehouse,
        'counts': [_count_payload(c, wh_name=wh_names.get(int(c['warehouse_id']), ''))
                   for c in counts],
        'total': len(counts),
    })


@bp_api.route('/stock/counts', methods=['POST'])
def api_counts_create():
    """建盘点单。

    body:
      title          单据名称(必填)
      remark         备注
      warehouse_id   仓库(缺省默认仓)
      scope          all | category | search | selected
      category_id    scope=category 时的分类 id(自动展开子树)
      q              scope=search 时的搜索词
      product_ids    scope=selected 时的商品 id 列表

    ⭐ `as_of`(盘点基准时点)在建单那一刻写死,之后不可改 ——
       它决定「先数货还是先入库」怎么切分(A5)。
    """
    operator_id, operator_name = _require_operator()
    if operator_id is None:
        return jsonify({'success': False, 'error': '未登录,请先选择身份'}), 401

    data = _json_body()
    try:
        warehouse = _resolve_warehouse(data.get('warehouse_id'))
        scope = (data.get('scope') or SCOPE_ALL).strip() or SCOPE_ALL
        if scope not in dict(SCOPES):
            raise CountError('scope 只能是 all / category / search / selected')
        category_ids = None
        if scope == SCOPE_CATEGORY:
            category_ids = _expand_category(data.get('category_id'))
            if not category_ids:
                raise CountError('按分类建单必须指定分类')
        result = create_count(
            int(warehouse['id']),
            title=data.get('title'),
            remark=data.get('remark') or '',
            scope=scope,
            category_ids=category_ids,
            q=(data.get('q') or '').strip(),
            product_ids=data.get('product_ids') or [],
            operator_id=operator_id,
            operator_name=operator_name,
        )
    except CountError as e:
        return jsonify({'success': False, 'error': e.message}), e.status
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        current_app.logger.exception('建盘点单失败')
        return jsonify({'success': False, 'error': '建单失败,请稍后重试'}), 500

    AuditLog.log('stock_count_create', 'stock_count', result['count_id'], detail={
        'title': result['title'],
        'scope': result['scope'],
        'total_items': result['total_items'],
        'as_of': result['as_of'],
        'warehouse_id': int(warehouse['id']),
    })
    result['success'] = True
    result['message'] = f'已建盘点单,共 {result["total_items"]} 个 SKU'
    return jsonify(result), 201


@bp_api.route('/stock/counts/<int:count_id>', methods=['GET'])
def api_counts_detail(count_id: int):
    """盘点单详情(含明细行)。"""
    guard = _require_session()
    if guard:
        return guard
    count = StockCount.get_by_id(count_id)
    if not count:
        return jsonify({'success': False, 'error': '盘点单不存在'}), 404
    items = [_item_payload(it) for it in StockCountItem.list_by_count(count_id)]
    return jsonify({'success': True, 'count': _count_payload(count, items=items)})


@bp_api.route('/stock/counts/<int:count_id>', methods=['PATCH'])
def api_counts_update(count_id: int):
    """改名称 / 备注。

    ⚠️ `as_of` **不给改** —— 它是 A5 的基准时点,改了「先数货还是先入库」
       的切分就失去意义了(上游 §4.3)。
    """
    guard = _require_session()
    if guard:
        return guard
    count = StockCount.get_by_id(count_id)
    if not count:
        return jsonify({'success': False, 'error': '盘点单不存在'}), 404
    if count.get('status') != 'open':
        return jsonify({'success': False, 'error': '盘点单已提交,不能再改'}), 409

    data = _json_body()
    fields = {}
    if 'title' in data:
        title = (data.get('title') or '').strip()
        if not title:
            return jsonify({'success': False, 'error': '盘点单名称不能为空'}), 400
        fields['title'] = title
    if 'remark' in data:
        fields['remark'] = (data.get('remark') or '').strip()
    if not fields:
        return jsonify({'success': False, 'error': '没有要改的字段'}), 400

    StockCount.update_meta(count_id, **fields)
    return jsonify({'success': True, 'count': _count_payload(StockCount.get_by_id(count_id))})


@bp_api.route('/stock/counts/<int:count_id>', methods=['DELETE'])
def api_counts_delete(count_id: int):
    """删盘点单。

    ⚠️ **只允许删未提交的草稿** —— 已提交的单据写过了 adjust 流水,
       删掉单子会让流水失去出处(审计链断),所以直接拒绝。
    """
    guard = _require_session()
    if guard:
        return guard
    count = StockCount.get_by_id(count_id)
    if not count:
        return jsonify({'success': False, 'error': '盘点单不存在'}), 404
    if count.get('status') != 'open':
        return jsonify({'success': False,
                        'error': '盘点单已提交,不能删除(已写入调整流水)'}), 409

    StockCount.delete(count_id)
    AuditLog.log('stock_count_delete', 'stock_count', count_id,
                 detail={'title': count.get('title') or ''})
    return jsonify({'success': True, 'message': '盘点单已删除'})


# ── REST · 录数 ───────────────────────────────────────────────────
@bp_api.route('/stock/counts/<int:count_id>/items/<int:product_id>',
              methods=['POST'])
def api_count_set_item(count_id: int, product_id: int):
    """录实盘数(单行)。前端**失焦即存**。

    body: {quantity: 12.5}  —— 传 null = 清空,回到「待录」。
          {quantity: 12.5, client_counted_at: '2026-11-21 10:00:00'}（可选）

    ⭐ **并发写同单(阶段 6 · 6.6)**:`client_counted_at` 是客户端**上一次看到的**
       这一行的 `counted_at`。服务端比一下:
         · 不同 → 说明**别人在你之前改过这一行** → 仍然照你的值写入
           (「最后写入生效」,不硬锁、不报错),但响应里带 `conflict=True` +
           `conflict_at`(对方改的时间)→ 前端弹一条提示。
         · 相同 / 没传 → 正常写入,不带 conflict 字段。
       ⚠️ `counted_at` 是**秒级**精度 → 同一秒内的并发改动检测不到
         (行级粒度 + 秒级,这是本方案的已知边界,写在计划 §阶段 6 风险里)。
    """
    guard = _require_session()
    if guard:
        return guard
    count = StockCount.get_by_id(count_id)
    if not count:
        return jsonify({'success': False, 'error': '盘点单不存在'}), 404
    if count.get('status') != 'open':
        return jsonify({'success': False, 'error': '盘点单已提交,不能再改'}), 409

    data = _json_body()
    if 'quantity' not in data:
        return jsonify({'success': False, 'error': '缺少 quantity'}), 400

    raw = data.get('quantity')
    if raw is None or raw == '':
        qty = None
    else:
        try:
            qty = float(raw)
        except (TypeError, ValueError):
            return jsonify({'success': False, 'error': '实盘数必须是数字'}), 400
        if qty < 0:
            return jsonify({'success': False, 'error': '实盘数不能是负数'}), 400

    # ── 并发冲突检测(先读旧值,再写)──
    conflict = False
    conflict_at = None
    if 'client_counted_at' in data:
        current = StockCountItem.get(count_id, product_id) or {}
        current_at = current.get('counted_at') or ''
        client_at = data.get('client_counted_at') or ''
        if str(client_at) != str(current_at):
            conflict = True
            conflict_at = current_at or None

    try:
        item = StockCountItem.set_actual(count_id, product_id, qty)
    except Exception:
        current_app.logger.exception('录数失败: count=%s pid=%s', count_id, product_id)
        return jsonify({'success': False, 'error': '保存失败,请稍后重试'}), 500
    if not item:
        return jsonify({'success': False, 'error': '保存失败,请稍后重试'}), 500

    stats = StockCount.refresh_stats(count_id)
    payload = {
        'success': True,
        'item': _item_payload(item),
        'stats': stats,
    }
    if conflict:
        payload['conflict'] = True
        payload['conflict_at'] = conflict_at
    return jsonify(payload)


@bp_api.route('/stock/counts/<int:count_id>/items/<int:product_id>',
              methods=['DELETE'])
def api_count_delete_item(count_id: int, product_id: int):
    """删一行(回到「待录」)。"""
    guard = _require_session()
    if guard:
        return guard
    count = StockCount.get_by_id(count_id)
    if not count:
        return jsonify({'success': False, 'error': '盘点单不存在'}), 404
    if count.get('status') != 'open':
        return jsonify({'success': False, 'error': '盘点单已提交,不能再改'}), 409

    StockCountItem.delete(count_id, product_id)
    stats = StockCount.refresh_stats(count_id)
    return jsonify({'success': True, 'stats': stats})


@bp_api.route('/stock/counts/<int:count_id>/bulk-input', methods=['POST'])
def api_count_bulk_input(count_id: int):
    """批量粘贴回填。

    body: {text: "02050127\\t20\\n白磅布 A1 15"}  —— 每行「编号<TAB或空格>实盘数」。

    返回摘要:识别 N 行 / 成功 N / 未匹配 N / 跳过 N + 逐行明细。
    """
    guard = _require_session()
    if guard:
        return guard
    count = StockCount.get_by_id(count_id)
    if not count:
        return jsonify({'success': False, 'error': '盘点单不存在'}), 404
    if count.get('status') != 'open':
        return jsonify({'success': False, 'error': '盘点单已提交,不能再改'}), 409

    data = _json_body()
    text = data.get('text')
    if text is None:
        text = request.form.get('text') or ''
    if not str(text).strip():
        return jsonify({'success': False, 'error': '粘贴内容为空'}), 400

    try:
        result = bulk_input(count_id, str(text))
    except CountError as e:
        return jsonify({'success': False, 'error': e.message}), e.status
    except Exception:
        current_app.logger.exception('批量粘贴失败: count=%s', count_id)
        return jsonify({'success': False, 'error': '粘贴失败,请稍后重试'}), 500

    result['success'] = True
    result['message'] = (
        f'识别 {result["parsed"]} 行,成功 {result["matched"]},'
        f'未匹配 {result["unmatched"]},跳过 {result["skipped"]}'
    )
    return jsonify(result)


# ── REST · 导出 ───────────────────────────────────────────────────
@bp_api.route('/stock/counts/<int:count_id>/export.csv', methods=['GET'])
def api_count_export_csv(count_id: int):
    """导出盘点表:编号 / 品名 / 规格 / 单位 / 账面数 / 实盘数(空) / 差异(空)。

    ⚠️ 与页面**逐行一致**——两者共用 `stock_counting.export_rows()`。
    """
    guard = _require_session()
    if guard:
        return guard
    count = StockCount.get_by_id(count_id)
    if not count:
        return jsonify({'success': False, 'error': '盘点单不存在'}), 404

    try:
        body = export_csv(count_id)
    except CountError as e:
        return jsonify({'success': False, 'error': e.message}), e.status

    safe_title = ''.join(ch for ch in (count.get('title') or '盘点')
                         if ch.isalnum() or ch in '-_')[:30] or 'count'
    fname = f'盘点_{safe_title}_{datetime.now().strftime("%Y%m%d_%H%M")}.csv'
    return Response(
        body.encode('utf-8-sig'),
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename="{fname}"'},
    )


# ── REST · 提交(⭐ A5)─────────────────────────────────────────────
@bp_api.route('/stock/counts/<int:count_id>/submit/preview', methods=['POST'])
def api_count_submit_preview(count_id: int):
    """提交预览 —— 弹框要显示的「账面 → 实盘 → 期间变动 → 基准余额 → 调整额」。

    ⭐ **只读**,不改任何数据。用户先看清楚要写多少条 adjust 流水再决定。
    """
    guard = _require_session()
    if guard:
        return guard
    count = StockCount.get_by_id(count_id)
    if not count:
        return jsonify({'success': False, 'error': '盘点单不存在'}), 404
    if count.get('status') != 'open':
        return jsonify({'success': False, 'error': '盘点单已提交,不能再改'}), 409

    data = _json_body()
    try:
        mode = _parse_mode(data.get('mode'))
        result = preview_submit(count_id, mode=mode)
    except CountError as e:
        return jsonify({'success': False, 'error': e.message}), e.status

    result['success'] = True
    return jsonify(result)


@bp_api.route('/stock/counts/<int:count_id>/submit', methods=['POST'])
def api_count_submit(count_id: int):
    """提交盘点单 —— 写 adjust 流水、启用商品、关单。

    ⭐ **A5**:差额 = 实盘 − **基准余额** − **期间变动**(不是「实盘 − 当前余额」)。
    ⭐ **A7**:写 audit_log,操作人自动注入 session。
    ⭐ **重复提交 → 409**。

    body: {mode: 'inventory'|'zero', reason: '归零原因(归零必填)'}
    """
    operator_id, operator_name = _require_operator()
    if operator_id is None:
        return jsonify({'success': False, 'error': '未登录,请先选择身份'}), 401

    count = StockCount.get_by_id(count_id)
    if not count:
        return jsonify({'success': False, 'error': '盘点单不存在'}), 404
    if count.get('status') != 'open':
        return jsonify({'success': False, 'error': '盘点单已提交,不能重复提交'}), 409

    data = _json_body()
    try:
        mode = _parse_mode(data.get('mode'))
        result = submit_count(
            count_id, mode=mode, operator_id=operator_id,
            operator_name=operator_name,
            reason=data.get('reason') or '',
        )
    except CountError as e:
        return jsonify({'success': False, 'error': e.message}), e.status
    except Exception as e:
        current_app.logger.exception('盘点提交失败: count=%s', count_id)
        return jsonify({'success': False,
                        'error': '提交失败,数据已回滚,请稍后重试'}), 500

    AuditLog.log('stock_count_submit', 'stock_count', count_id, detail={
        'mode': result['mode'],
        'adjusted': result['adjusted'],
        'unchanged': result['unchanged'],
        'updated': result['updated'],
        'as_of': result['as_of'],
        'occurred_at': result['occurred_at'],
        'warehouse_id': int(count['warehouse_id']),
    })
    result['success'] = True
    result['message'] = (
        f'已提交:调整 {result["adjusted"]} 个商品 / 未变 {result["unchanged"]} 个'
    )
    return jsonify(result)
