"""通知蓝图。

包含：
- /notice（页面 + CRUD form 提交）
- /api/v1/notice/<id>/images（图片上传/删除）
- /api/v1/notices（REST API 完整 CRUD）
"""
import os
import uuid
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from models import Notice, NoticeImage, get_db

bp = Blueprint('notice', __name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ── 页面路由 ──────────────────────────────────────────────
@bp.route('/notice')
def notice():
    notices = Notice.get_all()
    notice_images = {n['id']: NoticeImage.get_by_notice(n['id']) for n in notices}
    return render_template('notice.html', notices=notices, notice_images=notice_images, page_title='公司通知')


@bp.route('/notice/add', methods=['POST'])
def notice_add():
    category = request.form.get('category', '').strip()
    content = request.form.get('content', '').strip()
    if category and content:
        Notice.create(category, content)
        flash('通知已发布!', 'success')
    return redirect(url_for('notice.notice'))


@bp.route('/notice/delete/<int:notice_id>', methods=['POST'])
def notice_delete(notice_id):
    Notice.delete(notice_id)
    flash('已删除', 'info')
    return redirect(url_for('notice.notice'))


@bp.route('/notice/edit/<int:notice_id>', methods=['POST'])
def notice_edit(notice_id):
    category = request.form.get('category', '').strip()
    content = request.form.get('content', '').strip()
    if category and content:
        Notice.update(notice_id, category, content)
        flash('已修改', 'success')
    return redirect(url_for('notice.notice'))


@bp.route('/notice/move_up/<int:notice_id>', methods=['POST'])
def notice_move_up(notice_id):
    Notice.move_up(notice_id)
    return redirect(url_for('notice.notice'))


@bp.route('/notice/move_down/<int:notice_id>', methods=['POST'])
def notice_move_down(notice_id):
    Notice.move_down(notice_id)
    return redirect(url_for('notice.notice'))


# ── 图片上传/删除（通知图片存在 static/notice/，不走 upload 目录） ──
@bp.route('/api/v1/notice/<int:notice_id>/images', methods=['POST'])
def api_notice_upload_image(notice_id):
    """上传通知图片 → static/notice/<uuid>.<ext>"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT id FROM notices WHERE id = ?', (notice_id,))
    if not cursor.fetchone():
        conn.close()
        return jsonify({'success': False, 'error': '通知不存在'}), 404
    conn.close()

    if 'image' not in request.files:
        return jsonify({'success': False, 'error': '未提供图片'}), 400
    file = request.files['image']
    if file.filename == '':
        return jsonify({'success': False, 'error': '未选择文件'}), 400

    static_dir = os.path.join(BASE_DIR, 'static', 'notice')
    os.makedirs(static_dir, exist_ok=True)

    ext = file.filename.rsplit('.', 1)[-1].lower() if '.' in file.filename else 'png'
    if ext not in ('jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp'):
        ext = 'png'
    file_name = f"{uuid.uuid4().hex}.{ext}"
    file_path = os.path.join(static_dir, file_name)
    file.save(file_path)

    image_id = NoticeImage.create(notice_id, file_name, file.filename)
    return jsonify({'success': True, 'image_id': image_id, 'file_name': file_name}), 201


@bp.route('/api/v1/notice/images/<int:image_id>', methods=['DELETE'])
def api_notice_delete_image(image_id):
    """删除通知图片"""
    if NoticeImage.delete(image_id):
        return jsonify({'success': True})
    return jsonify({'success': False, 'error': '图片不存在'}), 404


@bp.route('/api/v1/notice/<int:notice_id>/img-cols', methods=['PATCH'])
def api_notice_set_img_cols(notice_id):
    """设置通知图片显示列数（1-5）"""
    data = request.get_json()
    if not data or 'img_cols' not in data:
        return jsonify({'success': False, 'error': '缺少 img_cols 参数'}), 400
    try:
        cols = int(data['img_cols'])
    except (ValueError, TypeError):
        return jsonify({'success': False, 'error': 'img_cols 必须是整数'}), 400
    if cols < 1 or cols > 5:
        return jsonify({'success': False, 'error': '列数范围 1-5'}), 400
    Notice.set_img_cols(notice_id, cols)
    return jsonify({'success': True, 'img_cols': cols})


# ── RESTful JSON API ──
@bp.route('/api/v1/notices', methods=['GET'])
def api_v1_notices_list():
    """获取所有通知（按分类和排序）"""
    notices = Notice.get_all()
    notice_images_map = {n['id']: NoticeImage.get_by_notice(n['id']) for n in notices}
    result = []
    for n in notices:
        n['images'] = notice_images_map.get(n['id'], [])
        result.append(n)
    return jsonify({'success': True, 'notices': result, 'count': len(result)})


@bp.route('/api/v1/notices', methods=['POST'])
def api_v1_notices_create():
    """创建通知 → 201"""
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': '请求体不能为空'}), 400
    category = data.get('category', '').strip()
    content = data.get('content', '').strip()
    if not category or not content:
        return jsonify({'success': False, 'error': '分类和内容不能为空'}), 400
    new_id = Notice.create(category, content)
    return jsonify({'success': True, 'id': new_id, 'category': category, 'content': content}), 201


@bp.route('/api/v1/notices/<int:notice_id>', methods=['GET'])
def api_v1_notices_get(notice_id):
    """获取单条通知"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM notices WHERE id = ?', (notice_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return jsonify({'success': False, 'error': '通知不存在'}), 404
    n = dict(row)
    n['images'] = NoticeImage.get_by_notice(notice_id)
    return jsonify({'success': True, 'notice': n})


@bp.route('/api/v1/notices/<int:notice_id>', methods=['PUT'])
def api_v1_notices_update(notice_id):
    """更新通知 → 200"""
    data = request.get_json()
    if not data:
        return jsonify({'success': False, 'error': '请求体不能为空'}), 400
    category = data.get('category', '').strip()
    content = data.get('content', '').strip()
    if not category or not content:
        return jsonify({'success': False, 'error': '分类和内容不能为空'}), 400
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT id FROM notices WHERE id = ?', (notice_id,))
    if not cursor.fetchone():
        conn.close()
        return jsonify({'success': False, 'error': '通知不存在'}), 404
    conn.close()
    Notice.update(notice_id, category, content)
    return jsonify({'success': True, 'notice': {'id': notice_id, 'category': category, 'content': content}})


@bp.route('/api/v1/notices/<int:notice_id>', methods=['DELETE'])
def api_v1_notices_delete(notice_id):
    """删除通知（级联删除图片和文件）"""
    conn = get_db()
    cursor = conn.cursor()
    cursor.execute('SELECT id FROM notices WHERE id = ?', (notice_id,))
    if not cursor.fetchone():
        conn.close()
        return jsonify({'success': False, 'error': '通知不存在'}), 404
    # 删除关联图片文件
    images = NoticeImage.get_by_notice(notice_id)
    for img in images:
        try:
            fpath = os.path.join(BASE_DIR, 'static', 'notice', img['file_name'])
            if os.path.exists(fpath):
                os.remove(fpath)
        except Exception:
            pass
    cursor.execute('DELETE FROM notice_images WHERE notice_pk = ?', (notice_id,))
    conn.commit()
    conn.close()
    Notice.delete(notice_id)
    return jsonify({'success': True, 'id': notice_id})


@bp.route('/api/v1/notices/<int:notice_id>/move', methods=['PATCH'])
def api_v1_notices_move(notice_id):
    """移动通知（上下）"""
    data = request.get_json() or {}
    direction = data.get('direction', '').lower()
    if direction == 'up':
        Notice.move_up(notice_id)
        return jsonify({'success': True, 'direction': 'up', 'id': notice_id})
    elif direction == 'down':
        Notice.move_down(notice_id)
        return jsonify({'success': True, 'direction': 'down', 'id': notice_id})
    return jsonify({'success': False, 'error': 'direction 必须为 up 或 down'}), 400
