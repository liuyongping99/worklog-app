# -*- coding: utf-8 -*-
"""库存蓝图 —— 阶段 2 只读查询 + 阶段 3 入库过账。

页面:
  GET /stock                       库存查询页

REST(只读,阶段 2):
  GET /api/v1/warehouses           仓库列表
  GET /api/v1/stock                库存列表(分页 + 分类 + 搜索 + 仅看已启用/未启用)
  GET /api/v1/stock/<product_id>   单 SKU 库存 + 各仓分布 + 最近流水
  GET /api/v1/stock/ledger         流水明细(按 SKU / 业务类型 / 日期)
  GET /api/v1/stock/export.csv     导出 CSV

REST(写库,阶段 3 · 入库过账):
  POST /api/v1/inbound-orders/<id>/post          过账(数量计入库存)
  POST /api/v1/inbound-orders/<id>/reverse       红字冲销(写负数流水,不改原记录)
  GET  /api/v1/inbound-orders/<id>/stock-status  过账状态 + 跳过清单 + 未启用提示

⚠️ 写库的事务与写入契约在 blueprints/stock_posting.py(服务层);
   本文件只做 HTTP 层:参数校验 → 调服务 → 返回 JSON。

三条贯穿全局的约定:
  · **仓库是数据不是结构** —— API 一律接受 warehouse_id,缺省取 is_default=1 的默认仓;
    将来加「田心仓」只插一行 warehouses,后端零改动。
  · **单仓阶段 UI 显示静态标签「🏭 芙蓉仓」**,不做只有一项的下拉框(下拉框很怪)。
  · **未启用 ≠ 真 0** —— is_tracking=0 的商品对外显示 0 + 灰色「未启用」标签;
    is_tracking=1 但数量为 0 才是「真 0」。
"""
import csv
import io
from datetime import datetime

from flask import (Blueprint, Response, abort, current_app, jsonify,
                   render_template, request, session)

from models import ProductStock, StockLedger, Warehouse
from models.audit import AuditLog
from models.products import Product, ProductCategory

from blueprints.stock_posting import (PostingError, get_stock_status,
                                       post_inbound_order,
                                       reverse_inbound_order)

bp = Blueprint('stock', __name__)
bp_api = Blueprint('stock_api', __name__, url_prefix='/api/v1')

# 流水业务类型(与 stock_ledger.biz_type 同口径)
BIZ_TYPES = ('opening', 'inbound', 'adjust', 'outbound', 'loading')

# 导出列(顺序 = CSV 表头顺序)
EXPORT_FIELDS = ('product_code', 'product_name', 'specification', 'base_unit',
                 'category_name', 'display_quantity', 'quantity',
                 'is_tracking_label', 'internal_total', 'updated_at')

# CSV 公式注入防护:这些开头的字符串会被 Excel 等表格软件当成公式执行
_CSV_FORMULA_PREFIXES = '=+-@\t\r\n'


def _sanitize_csv_cell(value):
    """把可能被表格软件解释为公式的字符串强制标记为文本。"""
    if not isinstance(value, str) or not value:
        return value
    if value[0] in _CSV_FORMULA_PREFIXES:
        return "'" + value
    return value


def _qs(**overrides):
    """基于当前请求参数,覆盖部分键,生成新的查询串(给页面上各种链接用)。

    ⚠️ 值为 None / '' 的覆盖键会**删掉**该参数,而不是留下 `?q=`。
       这样「清除分类筛选」「切到全部」这类链接不会带上残留的空参数。
    """
    from urllib.parse import urlencode
    args = {k: v for k, v in request.args.items()}
    for k, v in overrides.items():
        if v is None or v == '':
            args.pop(k, None)
        else:
            args[k] = v
    return urlencode(args)


# ── 参数解析(页面 / API / 导出 **共用同一套**)────────────────────────
def _require_session():
    """/api/* 不在登录闸门白名单里 → 这里自己判 session,返回 401。"""
    if session.get('operator_id') is None:
        return jsonify({'success': False, 'error': '未登录,请先选择身份'}), 401
    return None


def _require_operator():
    """⭐ **A7**:改账类 API 必须有登录操作员。

    返回 (operator_id, operator_name);未登录返回 (None, None)。
    「改账类」= 会动账面数的接口:过账 / 冲销(阶段 3)、盘点提交 / 归零(阶段 5/7)。
    现状 audit_log 里有 20067 行 user_id 为 NULL —— 改账不留痕等于没账。
    """
    op_id = session.get('operator_id')
    if op_id is None:
        return None, None
    name = ''
    try:
        from models.tasks_flow import StaffDB
        op = StaffDB.get_by_id(op_id)
        name = (op or {}).get('name') or ''
    except Exception:
        name = ''
    return int(op_id), name


def _resolve_warehouse(raw):
    """解析 warehouse_id → 仓库 dict。缺省取默认仓。

    ⚠️ 阶段 1.6 定下的口径:**FK 只当兜底,应用层必须校验** ——
       仓库不存在要在这里翻译成 400 + 中文提示,而不是等 DB 报 IntegrityError(→ 500)。
    """
    if raw is None or str(raw).strip() == '':
        wh = Warehouse.get_default()
        if not wh:
            abort(400, description='没有可用仓库(warehouses 表是空的)')
        return wh
    try:
        wid = int(raw)
    except (TypeError, ValueError):
        raise ValueError('warehouse_id 必须是整数')
    wh = Warehouse.get_by_id(wid)
    if not wh:
        raise ValueError(f'仓库不存在(id={wid})')
    return wh


def _parse_tracking(raw):
    """only_tracking: '1' → 只看已启用;'0' → 只看未启用;空 → 全部。"""
    if raw is None:
        return None
    s = str(raw).strip()
    if s == '' or s == 'all':
        return None
    if s in ('1', 'true', 'yes', 'on'):
        return True
    if s in ('0', 'false', 'no', 'off'):
        return False
    raise ValueError('only_tracking 只能是 1 / 0 / 空')


def _parse_paging():
    """(page, page_size) —— page 从 1 开始。"""
    try:
        page = int(request.args.get('page', 1))
    except (TypeError, ValueError):
        raise ValueError('page 必须是整数')
    try:
        page_size = int(request.args.get('page_size', 50))
    except (TypeError, ValueError):
        raise ValueError('page_size 必须是整数')
    page = max(1, page)
    page_size = min(max(1, page_size), 500)
    return page, page_size


def _parse_category(raw):
    """category_id → 展开成「含所有后代」的 id 列表。

    选父分类要含子树(主流进销存的通用做法)——
    否则用户点「白坯布」看不到它的三级子类,会以为货丢了。
    """
    if raw is None or str(raw).strip() == '':
        return None
    try:
        cid = int(raw)
    except (TypeError, ValueError):
        raise ValueError('category_id 必须是整数')
    # ⚠️ 先验存在性 —— descendant_ids 对不存在的分类会返回 [cid] 自己,
    #   不校验的话「分类不存在」会被静默当成「该分类下有 1 个商品」。
    if not ProductCategory.get_by_id(cid):
        raise ValueError(f'分类不存在(id={cid})')
    ids = ProductCategory.descendant_ids(cid)
    if not ids:
        raise ValueError(f'分类不存在(id={cid})')
    return ids


def _parse_date(raw, label):
    """日期参数校验 YYYY-MM-DD。空 → None(不过滤)。"""
    if raw is None or str(raw).strip() == '':
        return None
    s = str(raw).strip()
    try:
        datetime.strptime(s, '%Y-%m-%d')
    except ValueError:
        raise ValueError(f'{label} 格式错误,应为 YYYY-MM-DD')
    return s


def _parse_stock_query():
    """页面 / API / 导出共用的查询参数解析。

    返回 dict:
        warehouse(仓库 dict) / only_tracking / category_ids / q
        page / page_size / offset / total
    """
    warehouse = _resolve_warehouse(request.args.get('warehouse_id'))
    only_tracking = _parse_tracking(request.args.get('only_tracking'))
    category_ids = _parse_category(request.args.get('category_id'))
    q = (request.args.get('q') or '').strip() or None
    page, page_size = _parse_paging()
    wid = int(warehouse['id'])
    total = ProductStock.count_stock_view(
        wid, only_tracking=only_tracking, category_ids=category_ids, q=q)
    return {
        'warehouse': warehouse,
        'only_tracking': only_tracking,
        'category_ids': category_ids,
        'q': q,
        'page': page,
        'page_size': page_size,
        'offset': (page - 1) * page_size,
        'total': total,
        'pages': max(1, (total + page_size - 1) // page_size),
    }


def _fetch_stock_rows(params, limit=None, offset=None):
    """按参数取库存行,并把「内部累计」合并进去。

    ⚠️ 内部累计走 **按仓一次聚合**(sum_by_warehouse),不是逐行 sum_by_product ——
       那是 N+1:1600 行 = 1600 次查询。
    """
    wid = int(params['warehouse']['id'])
    lim = params['page_size'] if limit is None else limit
    off = params['offset'] if offset is None else offset
    rows = ProductStock.list_stock_view(
        wid,
        only_tracking=params['only_tracking'],
        category_ids=params['category_ids'],
        q=params['q'],
        limit=lim, offset=off,
    )
    totals = StockLedger.sum_by_warehouse(wid)
    for r in rows:
        r['internal_total'] = totals.get(int(r['product_id']), 0.0)
        r['is_tracking_label'] = '已启用' if r.get('is_tracking') else '未启用'
    return rows


# ── 页面 ──────────────────────────────────────────────────────
@bp.route('/stock')
def stock_page():
    """库存查询页(服务端渲染 —— 表格用同一套查询,导出才能保证逐行一致)。"""
    try:
        params = _parse_stock_query()
    except ValueError as e:
        return render_template('stock.html', page_title='库存',
                               error=str(e), warehouses=[], warehouses_count=0,
                               warehouse=None, category_tree=[], rows=[],
                               stats={'total': 0, 'tracking': 0,
                                      'untracked': 0, 'with_stock': 0},
                               stats_all={'total': 0, 'tracking': 0,
                                          'untracked': 0, 'with_stock': 0},
                               total=0, page=1, pages=1, page_size=50,
                               only_tracking=None, q='', category_id=None,
                               biz_types=BIZ_TYPES, qs=_qs), 400

    rows = _fetch_stock_rows(params)
    wid = int(params['warehouse']['id'])
    # 统计格跟随当前筛选(主流进销存做法);另给一个全仓总数做对照
    stats = ProductStock.stats_by_warehouse(
        wid, only_tracking=params['only_tracking'],
        category_ids=params['category_ids'], q=params['q'])
    stats_all = ProductStock.stats_by_warehouse(wid)
    warehouses = Warehouse.list_all()

    return render_template(
        'stock.html',
        page_title='库存',
        warehouses=warehouses,
        warehouses_count=len(warehouses),
        warehouse=params['warehouse'],
        category_tree=ProductCategory.get_tree(),
        rows=rows,
        stats=stats,
        stats_all=stats_all,
        total=params['total'],
        page=params['page'],
        pages=params['pages'],
        page_size=params['page_size'],
        only_tracking=params['only_tracking'],
        q=params['q'] or '',
        category_id=request.args.get('category_id', ''),
        biz_types=BIZ_TYPES,
        qs=_qs,
    )


# ── REST ──────────────────────────────────────────────────────
@bp_api.route('/warehouses', methods=['GET'])
def api_warehouses():
    """仓库列表(当前只有芙蓉仓;加仓时只插一行 warehouses,这里自动多一项)。"""
    guard = _require_session()
    if guard:
        return guard
    rows = Warehouse.list_all()
    return jsonify({'success': True, 'warehouses': rows, 'total': len(rows)})


@bp_api.route('/stock', methods=['GET'])
def api_stock_list():
    """库存列表。"""
    guard = _require_session()
    if guard:
        return guard
    try:
        params = _parse_stock_query()
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    rows = _fetch_stock_rows(params)
    wid = int(params['warehouse']['id'])
    stats = ProductStock.stats_by_warehouse(
        wid, only_tracking=params['only_tracking'],
        category_ids=params['category_ids'], q=params['q'])
    return jsonify({
        'success': True,
        'warehouse': params['warehouse'],
        'rows': rows,
        'stats': stats,
        'total': params['total'],
        'page': params['page'],
        'page_size': params['page_size'],
        'pages': params['pages'],
    })


@bp_api.route('/stock/<int:product_id>', methods=['GET'])
def api_stock_detail(product_id: int):
    """单个 SKU:基本信息 + 各仓库存 + 最近流水。"""
    guard = _require_session()
    if guard:
        return guard
    product = Product.get_by_id(product_id)
    if not product:
        return jsonify({'success': False, 'error': '商品不存在'}), 404

    try:
        warehouse = _resolve_warehouse(request.args.get('warehouse_id'))
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    wid = int(warehouse['id'])

    row = ProductStock.get(wid, product_id)
    ledger = StockLedger.list_ledger(wid, product_id=product_id, limit=20)
    totals = StockLedger.sum_by_warehouse(wid)
    return jsonify({
        'success': True,
        'product': product,
        'warehouse': warehouse,
        'stock': {
            'quantity': float(row['quantity']) if row else 0.0,
            'is_tracking': int(row['is_tracking']) if row else 0,
            'tracking_started_at': row['tracking_started_at'] if row else None,
            'updated_at': row['updated_at'] if row else None,
            'display_quantity': float(row['quantity']) if (row and row['is_tracking']) else 0.0,
        },
        'internal_total': totals.get(product_id, 0.0),
        'ledger': ledger,
    })


@bp_api.route('/stock/ledger', methods=['GET'])
def api_stock_ledger():
    """流水明细(按 SKU / 业务类型 / 日期)。"""
    guard = _require_session()
    if guard:
        return guard
    try:
        warehouse = _resolve_warehouse(request.args.get('warehouse_id'))
        biz_type = (request.args.get('biz_type') or '').strip() or None
        if biz_type and biz_type not in BIZ_TYPES:
            raise ValueError(f'biz_type 只能是 {"/".join(BIZ_TYPES)}')
        date_from = _parse_date(request.args.get('date_from'), 'date_from')
        date_to = _parse_date(request.args.get('date_to'), 'date_to')
        if date_from and date_to and date_from > date_to:
            raise ValueError('开始日期不能晚于结束日期')
        product_id = request.args.get('product_id')
        if product_id:
            product_id = int(product_id)
        page, page_size = _parse_paging()
    except (ValueError, TypeError) as e:
        return jsonify({'success': False, 'error': str(e)}), 400

    wid = int(warehouse['id'])
    rows = StockLedger.list_ledger(
        wid, product_id=product_id, biz_type=biz_type,
        date_from=date_from, date_to=date_to,
        limit=page_size, offset=(page - 1) * page_size)
    total = StockLedger.count_ledger(
        wid, product_id=product_id, biz_type=biz_type,
        date_from=date_from, date_to=date_to)
    return jsonify({
        'success': True,
        'warehouse': warehouse,
        'rows': rows,
        'total': total,
        'page': page,
        'page_size': page_size,
        'pages': max(1, (total + page_size - 1) // page_size),
    })


@bp_api.route('/stock/export.csv', methods=['GET'])
def api_stock_export_csv():
    """导出 CSV。

    ⚠️ 与页面**逐行一致**的结构保证:导出复用 `_parse_stock_query()` + `_fetch_stock_rows()`,
       只把分页去掉(导出全部匹配行)。筛选口径一处定义,不会分叉。
    """
    guard = _require_session()
    if guard:
        return guard
    try:
        params = _parse_stock_query()
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400

    rows = _fetch_stock_rows(params, limit=params['total'] or 0, offset=0)

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(EXPORT_FIELDS))
    writer.writeheader()
    for r in rows:
        writer.writerow({
            k: _sanitize_csv_cell(r.get(k) if r.get(k) is not None else '')
            for k in EXPORT_FIELDS
        })
    body = buf.getvalue().encode('utf-8-sig')
    fname = f'stock_{datetime.now().strftime("%Y%m%d_%H%M")}.csv'
    try:
        current_app.logger.info('库存导出 %s 行,仓库 %s', len(rows), params['warehouse']['name'])
    except Exception:
        pass
    return Response(
        body,
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename="{fname}"'},
    )


# ── REST · 入库过账(阶段 3)──────────────────────────────────────
# ⚠️ 事务与写入契约在 blueprints/stock_posting.py —— 这里只做 HTTP 层。
#    幂等三道防线、先查后写、A1 balance_after / A4 occurred_at 都由服务层保证。


@bp_api.route('/inbound-orders/<int:order_id>/post', methods=['POST'])
def api_inbound_post(order_id: int):
    """📥 入库过账 —— 数量计入对应仓的库存。

    返回:
      posted_count / posted_qty  本次入账的行数与数量(基本单位)
      skipped[]                  没入账的行 + 原因(品名匹配不到 / 缺换算规则 / 数量非法)
      not_tracking[]             已入账但商品未启用库存计算 → 对外显示仍是 0
      already_posted             true = 本单已过账,这次一笔新的都没入账(幂等命中)

    ⚠️ **重复点过账不报错** —— 幂等命中时返回 200 + already_posted=true,
       前端弹「本单已过账」,而不是甩一个 500 给用户(开发计划 4.5)。
    """
    operator_id, operator_name = _require_operator()
    if operator_id is None:
        return jsonify({'success': False, 'error': '未登录,请先选择身份'}), 401
    try:
        result = post_inbound_order(order_id, operator_id=operator_id,
                                    operator_name=operator_name)
    except PostingError as e:
        return jsonify({'success': False, 'error': e.message}), e.status
    except Exception as e:
        current_app.logger.exception('入库过账失败: order_id=%s', order_id)
        return jsonify({'success': False, 'error': '过账失败,数据已回滚,请稍后重试'}), 500

    AuditLog.log('stock_post', 'inbound_order', order_id, detail={
        'posting_id': result['posting_id'],
        'posted_count': result['posted_count'],
        'posted_qty': result['posted_qty'],
        'skipped_count': result['skipped_count'],
        'already_posted_count': result['already_posted_count'],
        'warehouse_id': result['warehouse']['id'],
        'occurred_at': result['occurred_at'],
    })
    result['success'] = True
    result['message'] = _post_message(result)
    return jsonify(result)


def _post_message(result):
    """给前端弹窗的一句话摘要。"""
    if result['already_posted']:
        if result['order_reversed']:
            return '本单已冲销,不能重复过账'
        return '本单已过账,没有新增入账'
    bits = [f'已入账 {result["posted_count"]} 行 / {result["posted_qty"]:g}']
    if result['skipped_count']:
        bits.append(f'{result["skipped_count"]} 行未入账')
    if result['not_tracking']:
        bits.append(f'{len(result["not_tracking"])} 个商品未启用库存计算(对外显示 0)')
    return ' · '.join(bits)


@bp_api.route('/inbound-orders/<int:order_id>/reverse', methods=['POST'])
def api_inbound_reverse(order_id: int):
    """↩ 红字冲销 —— 写一条数量取负的流水,原记录**永不删改**。

    ⚠️ 流水表有禁改触发器(A3),冲销是唯一的「改错」途径。
    ⚠️ 冲销过的行**不能再正向过账**(正向 uniq_key 仍被原流水占着)——
       要改单据请改源头再重新建单。
    """
    operator_id, operator_name = _require_operator()
    if operator_id is None:
        return jsonify({'success': False, 'error': '未登录,请先选择身份'}), 401
    try:
        result = reverse_inbound_order(order_id, operator_id=operator_id,
                                       operator_name=operator_name)
    except PostingError as e:
        return jsonify({'success': False, 'error': e.message}), e.status
    except Exception as e:
        current_app.logger.exception('入库冲销失败: order_id=%s', order_id)
        return jsonify({'success': False, 'error': '冲销失败,数据已回滚,请稍后重试'}), 500

    AuditLog.log('stock_reverse', 'inbound_order', order_id, detail={
        'reversed_count': result['reversed_count'],
        'reversed_qty': result['reversed_qty'],
        'posting_ids': result['posting_ids'],
    })
    result['success'] = True
    result['message'] = (f'已冲销 {result["reversed_count"]} 行 / '
                          f'{result["reversed_qty"]:g},余额回到过账前')
    return jsonify(result)


@bp_api.route('/inbound-orders/<int:order_id>/stock-status', methods=['GET'])
def api_inbound_stock_status(order_id: int):
    """过账状态 + 跳过清单 + 未启用提示(阶段 4 的徽章数据源)。"""
    guard = _require_session()
    if guard:
        return guard
    try:
        result = get_stock_status(order_id)
    except PostingError as e:
        return jsonify({'success': False, 'error': e.message}), e.status
    except Exception as e:
        current_app.logger.exception('查过账状态失败: order_id=%s', order_id)
        return jsonify({'success': False, 'error': '服务异常,请稍后重试'}), 500
    result['success'] = True
    return jsonify(result)
