"""商品管理蓝图。

包含：
- /product-units (页面 + CRUD)
- /product-categories (页面 + CRUD)
- /products (页面)
- /api/v1/products (RESTful JSON API)
"""
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from models import ProductUnit, ProductCategory, Product

bp = Blueprint('products', __name__)


# ── 商品单位 ──────────────────────────────────────────────
@bp.route('/product-units')
def product_units():
    units = ProductUnit.get_all()
    return render_template('product-units.html', units=units, page_title='商品单位')


@bp.route('/product-units/add', methods=['POST'])
def product_units_add():
    name = request.form.get('product_name', '').strip()
    yards_str = request.form.get('yards_per_piece', '').strip()
    use_yard = request.form.get('is_usingyardforcounting') == '1'
    spec_kw = request.form.get('spec_keyword', '').strip() or None
    if name and yards_str:
        try:
            yards_val = int(round(float(yards_str) * 100))
            ProductUnit.create(name, yards_val, use_yard, spec_kw)
            label = f'{name}' + (f'（{spec_kw}）' if spec_kw else '')
            flash(f'已添加商品单位: {label}', 'success')
        except (ValueError, TypeError):
            flash('请输入有效的码数值', 'error')
    return redirect(url_for('products.product_units'))


@bp.route('/product-units/edit', methods=['POST'])
def product_units_edit():
    product_name = request.form.get('product_name', '')
    old_spec_kw = request.form.get('old_spec_keyword', '')
    old_spec_kw = old_spec_kw.strip() if old_spec_kw else None
    yards_str = request.form.get('yards_per_piece', '').strip()
    use_yard = request.form.get('is_usingyardforcounting') == '1'
    if product_name and yards_str:
        try:
            yards_val = int(round(float(yards_str) * 100))
            ProductUnit.update(product_name, yards_val, use_yard, old_spec_kw)
            flash(f'已更新 {product_name}', 'success')
        except (ValueError, TypeError):
            flash('请输入有效的码数值', 'error')
    return redirect(url_for('products.product_units'))


@bp.route('/product-units/delete', methods=['POST'])
def product_units_delete():
    product_name = request.form.get('product_name', '')
    spec_keyword = request.form.get('spec_keyword', '')
    spec_keyword = spec_keyword.strip() if spec_keyword else None
    if product_name:
        ProductUnit.delete(product_name, spec_keyword)
        flash(f'已删除 {product_name}', 'success')
    return redirect(url_for('products.product_units'))


# ── 商品类型（分类） ──────────────────────────────────────────────
@bp.route('/product-categories')
def product_categories():
    tree = ProductCategory.get_tree()
    all_cats = ProductCategory.get_all()
    levels = ProductCategory.get_level_options()
    return render_template('product-categories.html',
                           tree=tree, all_cats=all_cats, levels=levels, page_title='商品类型')


@bp.route('/product-categories/add', methods=['POST'])
def product_categories_add():
    code = request.form.get('category_code', '').strip()
    name = request.form.get('category_name', '').strip()
    parent_id_str = request.form.get('parent_id', '').strip()
    level_str = request.form.get('level', '').strip()
    sort_order_str = request.form.get('sort_order', '0').strip()
    if not code or not name:
        flash('编码和名称不能为空', 'error')
        return redirect(url_for('products.product_categories'))
    parent_id = int(parent_id_str) if parent_id_str else None
    level = int(level_str) if level_str else 1
    sort_order = int(sort_order_str) if sort_order_str else 0
    ProductCategory.create(code, name, parent_id, level, sort_order)
    flash(f'已添加分类: {name}', 'success')
    return redirect(url_for('products.product_categories'))


@bp.route('/product-categories/edit/<int:category_id>', methods=['POST'])
def product_categories_edit(category_id):
    code = request.form.get('category_code', '').strip()
    name = request.form.get('category_name', '').strip()
    sort_order_str = request.form.get('sort_order', '0').strip()
    status_str = request.form.get('status', '1').strip()
    if not code or not name:
        flash('编码和名称不能为空', 'error')
        return redirect(url_for('products.product_categories'))
    ProductCategory.update(
        category_id,
        category_code=code,
        category_name=name,
        sort_order=int(sort_order_str) if sort_order_str else 0,
        status=int(status_str) if status_str else 1
    )
    flash(f'已更新分类: {name}', 'success')
    return redirect(url_for('products.product_categories'))


@bp.route('/product-categories/delete/<int:category_id>', methods=['POST'])
def product_categories_delete(category_id):
    cat = ProductCategory.get_by_id(category_id)
    if cat:
        ProductCategory.delete(category_id)
        flash(f'已删除分类及其子分类: {cat["category_name"]}', 'success')
    return redirect(url_for('products.product_categories'))


# ── 产品管理 ──────────────────────────────────────────────
@bp.route('/products')
def products():
    """产品管理页面"""
    categories = ProductCategory.get_tree()
    return render_template('products.html', categories=categories, page_title='产品管理')


# ── 产品 API (RESTful v1) ──────────────────────────────────────────────
@bp.route('/api/v1/products', methods=['GET'])
def api_v1_products_list():
    """按分类查询产品列表"""
    category_id = request.args.get('category_id', '').strip()
    if category_id:
        products = Product.get_by_category(int(category_id))
    else:
        products = Product.get_all()
    return jsonify({'success': True, 'products': products})


@bp.route('/api/v1/products', methods=['POST'])
def api_v1_products_create():
    """创建产品 → 201"""
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': '请求体不能为空'}), 400
    product_code = data.get('product_code', '').strip()
    product_name = data.get('product_name', '').strip()
    category_id = data.get('category_id')
    if not product_code or not product_name or not category_id:
        return jsonify({'success': False, 'error': '产品编号、品名和分类不能为空'}), 400
    try:
        product_id = Product.create(
            product_code=product_code,
            product_name=product_name,
            category_id=int(category_id),
            specification=data.get('specification', ''),
            model=data.get('model', ''),
            barcode=data.get('barcode', ''),
            cost_price=data.get('cost_price'),
            base_unit=data.get('base_unit', ''),
            aux_unit=data.get('aux_unit', ''),
            conversion_rate=data.get('conversion_rate', ''),
            stock_quantity=data.get('stock_quantity'),
            aux_quantity=data.get('aux_quantity', ''),
            preset_price=data.get('preset_price'),
            status=data.get('status', 1)
        )
        product = Product.get_by_id(product_id)
        return jsonify({'success': True, 'id': product_id, 'product': product}), 201
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@bp.route('/api/v1/products/<int:product_id>', methods=['PUT'])
def api_v1_products_update(product_id):
    """更新产品 → 200"""
    product = Product.get_by_id(product_id)
    if not product:
        return jsonify({'success': False, 'error': '产品不存在'}), 404
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': '请求体不能为空'}), 400
    Product.update(product_id, data)
    updated = Product.get_by_id(product_id)
    return jsonify({'success': True, 'product': updated})


@bp.route('/api/v1/products/<int:product_id>', methods=['DELETE'])
def api_v1_products_delete(product_id):
    """删除产品 → 200"""
    product = Product.get_by_id(product_id)
    if not product:
        return jsonify({'success': False, 'error': '产品不存在'}), 404
    Product.delete(product_id)
    return jsonify({'success': True})
