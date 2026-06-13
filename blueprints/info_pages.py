"""信息展示页蓝图（静态页面 + 简单 stockout 板）。

包含：
- 首页 /
- 码数报价 /priceboard
- 通知彩色版 /notice-color
- 业务流程 /workflow
- 仓库布局 /warehouse
- 点数要点 /count-tips
- 换单要点 /huandan-guide
- 开单要点 /billing-tips
- 当前缺货 /stockout
"""
from flask import Blueprint, render_template, request, jsonify
from models import StockOutItem

bp = Blueprint('info_pages', __name__)


@bp.route('/')
def index():
    return render_template('index.html', page_title='首页')


# ── 码数报价 ──────────────────────────────────────────────
@bp.route('/priceboard')
def priceboard():
    return render_template('priceboard.html', page_title='码数报价')


# ── 通知彩色版 ──────────────────────────────────────────────
@bp.route('/notice-color')
def notice_color():
    return render_template('notice-color.html', page_title='通知彩色版')


# ── 业务流程 ──────────────────────────────────────────────
@bp.route('/workflow')
def workflow():
    return render_template('workflow.html', page_title='业务流程')


# ── 仓库布局 ──────────────────────────────────────────────
@bp.route('/warehouse')
def warehouse():
    return render_template('warehouse.html', page_title='仓库布局')


# ── 点数要点 ──────────────────────────────────────────────
@bp.route('/count-tips')
def count_tips():
    return render_template('count-tips.html', page_title='点数要点')


# ── 换单要点 ──────────────────────────────────────────────
@bp.route('/huandan-guide')
def huandan_guide():
    return render_template('huandan-guide.html', page_title='换单要点')


# ── 开单要点 ──────────────────────────────────────────────
@bp.route('/billing-tips')
def billing_tips():
    return render_template('billing-tips.html', page_title='开单要点')


# ── 当前缺货 ──────────────────────────────────────────────
@bp.route('/stockout')
def stockout():
    items = StockOutItem.get_all()
    return render_template('stockout.html', items=items, page_title='当前缺货')


@bp.route('/stockout/add', methods=['POST'])
def stockout_add():
    name = request.form.get('name', '').strip()
    color = request.form.get('color', '').strip()
    if not name:
        return jsonify({'success': False, 'error': '商品名称不能为空'}), 400
    StockOutItem.create(name, color)
    return jsonify({'success': True})


@bp.route('/stockout/delete/<int:item_id>', methods=['POST', 'DELETE'])
def stockout_delete(item_id):
    StockOutItem.delete(item_id)
    return jsonify({'success': True})
