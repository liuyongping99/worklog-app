"""综合查找蓝图。

包含：
- /unified-search 页面（跨出货/入库/装柜的搜索）
- POST /api/v1/unified-search REST API
"""
from datetime import date as date_cls, timedelta
from flask import Blueprint, render_template, request, jsonify
from models import UnifiedSearch

bp = Blueprint('search', __name__)

# ── 页面 ──────────────────────────────────────────────

@bp.route('/unified-search')
def unified_search_page():
    """综合查找页面。"""
    # 默认日期范围：最近 30 天
    end = date_cls.today()
    start = end - timedelta(days=29)
    default_start = start.isoformat()
    default_end = end.isoformat()
    return render_template(
        'unified-search.html',
        default_start=default_start,
        default_end=default_end,
    )


# ── REST API ───────────────────────────────────────────

@bp.route('/api/v1/unified-search', methods=['POST'])
def unified_search_api():
    """
    综合搜索 API。
    请求 JSON：
    {
        "scope": ["shipping", "inbound", "loading"],
        "start_date": "2026-06-01",
        "end_date": "2026-07-06",
        "customer": "",
        "product_name": "",
        "specification": ""
    }
    """
    data = request.get_json() or {}

    scope = data.get('scope', [])
    if not scope or not isinstance(scope, list):
        return jsonify({'success': False, 'error': '请至少选择一个查找范围'}), 400

    # 过滤非法 scope
    valid_scopes = {'shipping', 'inbound', 'loading'}
    scope = [s for s in scope if s in valid_scopes]
    if not scope:
        return jsonify({'success': False, 'error': '查找范围无效'}), 400

    start_date = (data.get('start_date') or '').strip() or None
    end_date = (data.get('end_date') or '').strip() or None
    customer = (data.get('customer') or '').strip() or None
    product_name = (data.get('product_name') or '').strip() or None
    specification = (data.get('specification') or '').strip() or None

    try:
        results = UnifiedSearch.search(
            scope=scope,
            start_date=start_date,
            end_date=end_date,
            customer=customer,
            product_name=product_name,
            specification=specification,
        )
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

    return jsonify({
        'success': True,
        'count': len(results),
        'results': results,
    })
