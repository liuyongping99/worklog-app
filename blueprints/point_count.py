# -*- coding: utf-8 -*-
"""独立点数工具蓝图(双蓝图:页面 + REST API)。

- 页面蓝图 bp(url_prefix='/tools/point-count'):
    GET  /tools/point-count/                   会话列表
    GET  /tools/point-count/new                新建会话页(可选;主入口在列表页)
    GET  /tools/point-count/session/<int:id>   会话详情:拍照/上传 + 手指点图计数
- REST API 蓝图 bp_api(url_prefix='/api/v1/point-count'):
    GET    /api/v1/point-count/sessions                  列出最近 N 个会话
    POST   /api/v1/point-count/sessions                  新建会话
    GET    /api/v1/point-count/sessions/<id>             单会话详情(含所有图片 + marks)
    POST   /api/v1/point-count/sessions/<id>/update      改 title/expected/unit/remark
    POST   /api/v1/point-count/sessions/<id>/close       完结
    POST   /api/v1/point-count/sessions/<id>/reopen      重新打开
    DELETE /api/v1/point-count/sessions/<id>             删除整个会话
    GET    /api/v1/point-count/sessions/<id>/images      列出图片
    POST   /api/v1/point-count/sessions/<id>/images      上传(base64 / multipart / rotate_deg)
    GET    /api/v1/point-count/images/<id>               单图详情
    DELETE /api/v1/point-count/images/<id>               删除图(连带 marks)
    POST   /api/v1/point-count/images/<id>/marks          添加一枚计数点
    DELETE /api/v1/point-count/images/<id>/marks/last     撤销最近一枚
    POST   /api/v1/point-count/images/<id>/mark-scale    数字整体缩放
    POST   /api/v1/point-count/images/<id>/loose-count   散码数
    GET    /api/v1/point-count/sessions/<id>/export      完整追溯数据(JSON)

设计要点:
- 完全独立于出货订单,不污染 shipping_images / placement_marks。
- 复用 _helpers 的图片上传工具(save_base64_image / save_uploaded_file /
  apply_user_rotation / check_uploaded_image / validate_image_content)。
- 所有写操作记日志(走 set_log_context),session_id / image_id 写入 biz 上下文。
- 删除图/会话走 mavis-trash(在 models/point_count.py 内部处理)。
"""
import json
import os
import re

from flask import (
    Blueprint, render_template, request, jsonify, session, abort,
)

from models import PointCountSession, PointCountImage
from blueprints._helpers import (
    save_base64_image, save_uploaded_file,
    apply_user_rotation, check_uploaded_image, validate_image_content,
    get_upload_dir,
)
from blueprints.ocr_log import set_log_context


bp = Blueprint('point_count', __name__, url_prefix='/tools/point-count')
bp_api = Blueprint('point_count_api', __name__, url_prefix='/api/v1/point-count')


# ── 工具 ──────────────────────────────────────────────────────
def _current_user_id() -> int:
    op = session.get('operator_id')
    if op is None:
        abort(401)
    return int(op)


def _title_safe(raw: str) -> str:
    raw = (raw or '').strip()
    if not raw:
        return '未命名点数组'
    # 防御 XSS:仅保留中文/英文/数字/常见符号,长度限制 64
    cleaned = re.sub(r'[^\w\u4e00-\u9fff \-_·.,()（）【】:：xX×\/\\]+', '', raw)
    return cleaned[:64].strip() or '未命名点数组'


def _safe_session_id(raw):
    try:
        return int(raw)
    except (TypeError, ValueError):
        abort(404)


# ── 页面路由 ─────────────────────────────────────────────────
@bp.route('/')
def index():
    """会话列表(主页)。"""
    set_log_context(biz='point_count')
    sessions = PointCountSession.list_all(limit=200)
    return render_template(
        'point-count.html',
        page_title='独立点数',
        sessions=sessions,
    )


@bp.route('/new')
def new_session_page():
    """新建会话:简单表单(标题/期望值/单位/备注),提交后跳到详情。"""
    set_log_context(biz='point_count')
    return render_template(
        'point-count.html',
        page_title='新建点数组',
        sessions=PointCountSession.list_all(limit=50),
        show_new_form=True,
    )


@bp.route('/session/<int:session_id>')
def session_detail(session_id: int):
    """详情页:拍照 + 手指点图计数。"""
    set_log_context(biz='point_count', session_id=session_id)
    sess = PointCountSession.get_by_id(session_id)
    if not sess:
        abort(404)
    images = PointCountImage.get_by_session(session_id)
    return render_template(
        'point-count-session.html',
        page_title=f"点数 · {sess['title']}",
        sess=sess,
        images=images,
    )


# ── REST:会话 ───────────────────────────────────────────────
@bp_api.route('/sessions', methods=['GET'])
def api_list_sessions():
    set_log_context(biz='point_count')
    limit = int(request.args.get('limit', 200))
    sessions = PointCountSession.list_all(limit=limit)
    return jsonify({'success': True, 'sessions': sessions})


@bp_api.route('/sessions', methods=['POST'])
def api_create_session():
    set_log_context(biz='point_count')
    user_id = _current_user_id()
    data = request.get_json(silent=True) or request.form
    title = _title_safe(data.get('title', ''))
    expected = data.get('expected_count')
    try:
        expected = int(expected) if expected not in (None, '', 'null') else None
    except (TypeError, ValueError):
        expected = None
    unit = (data.get('unit') or '支').strip()[:8] or '支'
    remark = (data.get('remark') or '').strip()[:500]
    sid = PointCountSession.create(
        user_id=user_id, title=title,
        expected_count=expected, unit=unit, remark=remark,
    )
    return jsonify({
        'success': True,
        'session_id': sid,
        'session': PointCountSession.get_by_id(sid),
    }), 201


@bp_api.route('/sessions/<int:session_id>', methods=['GET'])
def api_get_session(session_id: int):
    set_log_context(biz='point_count', session_id=session_id)
    sess = PointCountSession.get_by_id(session_id)
    if not sess:
        return jsonify({'success': False, 'error': '会话不存在'}), 404
    images = PointCountImage.get_by_session(session_id)
    return jsonify({'success': True, 'session': sess, 'images': images})


@bp_api.route('/sessions/<int:session_id>/update', methods=['POST'])
def api_update_session(session_id: int):
    set_log_context(biz='point_count', session_id=session_id)
    sess = PointCountSession.get_by_id(session_id)
    if not sess:
        return jsonify({'success': False, 'error': '会话不存在'}), 404
    data = request.get_json(silent=True) or request.form
    fields = {}
    if 'title' in data:
        fields['title'] = _title_safe(data['title'])
    if 'expected_count' in data:
        v = data['expected_count']
        try:
            fields['expected_count'] = int(v) if v not in (None, '', 'null') else None
        except (TypeError, ValueError):
            fields['expected_count'] = None
    if 'unit' in data:
        fields['unit'] = (data['unit'] or '支').strip()[:8] or '支'
    if 'remark' in data:
        fields['remark'] = (data['remark'] or '').strip()[:500]
    PointCountSession.update_meta(session_id, **fields)
    return jsonify({'success': True, 'session': PointCountSession.get_by_id(session_id)})


@bp_api.route('/sessions/<int:session_id>/close', methods=['POST'])
def api_close_session(session_id: int):
    set_log_context(biz='point_count', session_id=session_id)
    PointCountSession.close(session_id)
    return jsonify({'success': True, 'session': PointCountSession.get_by_id(session_id)})


@bp_api.route('/sessions/<int:session_id>/reopen', methods=['POST'])
def api_reopen_session(session_id: int):
    set_log_context(biz='point_count', session_id=session_id)
    PointCountSession.reopen(session_id)
    return jsonify({'success': True, 'session': PointCountSession.get_by_id(session_id)})


@bp_api.route('/sessions/<int:session_id>', methods=['DELETE'])
def api_delete_session(session_id: int):
    set_log_context(biz='point_count', session_id=session_id)
    sess = PointCountSession.get_by_id(session_id)
    if not sess:
        return jsonify({'success': False, 'error': '会话不存在'}), 404
    PointCountSession.delete(session_id)
    return jsonify({'success': True})


@bp_api.route('/sessions/<int:session_id>/export', methods=['GET'])
def api_export_session(session_id: int):
    """完整追溯导出:谁 / 何时建会话,每张图何时上传,每枚 mark 何时按下 + x/y。"""
    set_log_context(biz='point_count', session_id=session_id)
    sess = PointCountSession.get_by_id(session_id)
    if not sess:
        return jsonify({'success': False, 'error': '会话不存在'}), 404
    images = PointCountImage.get_by_session(session_id)
    payload = {
        'session': sess,
        'images': [
            {
                'id': img['id'],
                'original_name': img['original_name'],
                'relative_path': img['relative_path'],
                'mark_scale': img['mark_scale'],
                'loose_count': img['loose_count'],
                'n_marks': img['n_marks'],
                'created_at': img['created_at'],
                'marks': img['marks'],
            } for img in images
        ],
    }
    return jsonify({'success': True, 'export': payload})


# ── REST:图片上传 ──────────────────────────────────────────
@bp_api.route('/sessions/<int:session_id>/images', methods=['GET'])
def api_list_images(session_id: int):
    set_log_context(biz='point_count', session_id=session_id)
    if not PointCountSession.get_by_id(session_id):
        return jsonify({'success': False, 'error': '会话不存在'}), 404
    images = PointCountImage.get_by_session(session_id)
    return jsonify({'success': True, 'images': images})


@bp_api.route('/sessions/<int:session_id>/images', methods=['POST'])
def api_upload_image(session_id: int):
    """上传一张图(base64 / multipart),支持 rotate_deg 修正方向。

    返回值与出货 placement 上传对齐:{'success': True, 'image_id': ..., 'images': [...]}
    前端 placement_count.js 兼容此契约。
    """
    set_log_context(biz='point_count', session_id=session_id)
    sess = PointCountSession.get_by_id(session_id)
    if not sess:
        return jsonify({'success': False, 'error': '会话不存在'}), 404
    if sess['status'] == 'closed':
        return jsonify({'success': False, 'error': '会话已完结,请先重新打开'}), 400

    upload_dir, _ = get_upload_dir()
    saved = []
    rotate_deg = None  # 移动端拍照方向修正
    if request.is_json:
        data = request.get_json(silent=True) or {}
        img_data = data.get('image') or ''
        rotate_deg = data.get('rotate_deg')
        if img_data.startswith('data:image'):
            try:
                filepath = save_base64_image(img_data)
            except Exception as e:
                return jsonify({'success': False, 'error': f'图片数据无效:{e}'}), 400
        else:
            return jsonify({'success': False, 'error': '未提供图片'}), 400
    elif 'image' in request.files:
        file = request.files['image']
        if not file.filename:
            return jsonify({'success': False, 'error': '未选择文件'}), 400
        try:
            ext = check_uploaded_image(file)
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        # 拼出 .ext 后缀
        filename_root = os.path.splitext(file.filename or 'upload')[0] or 'upload'
        # 沿用 _helpers.save_uploaded_file 的逻辑
        import uuid as _uuid
        new_name = f"{_uuid.uuid4().hex}.{ext.lstrip('.') or 'png'}"
        filepath = os.path.join(upload_dir, new_name)
        try:
            file.save(filepath)
        except Exception as e:
            return jsonify({'success': False, 'error': f'保存失败:{e}'}), 400
    else:
        return jsonify({'success': False, 'error': '未提供图片'}), 400

    # 移动端方向修正(必须在 validate 之前先旋转,否则宽高被识别错)
    try:
        filepath = apply_user_rotation(filepath, rotate_deg)
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    # 内容真实性校验
    try:
        validate_image_content(filepath)
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400

    original_name = ''
    if request.is_json:
        original_name = (request.get_json(silent=True) or {}).get('original_name', '') or ''
    else:
        original_name = file.filename if 'file' in dir() and hasattr(file, 'filename') else ''

    image_id = PointCountImage.create(session_id, filepath, original_name)
    img = PointCountImage.get_by_id(image_id)
    saved.append({
        'image_id': image_id,
        'image': img['relative_path'],
        'original_name': img['original_name'],
    })
    return jsonify({
        'success': True,
        'images': saved,
        'count': len(saved),
        'image_id': image_id,
    }), 201


@bp_api.route('/images/<int:image_id>', methods=['GET'])
def api_get_image(image_id: int):
    set_log_context(biz='point_count', image_id=image_id)
    img = PointCountImage.get_by_id(image_id)
    if not img:
        return jsonify({'success': False, 'error': '图片不存在'}), 404
    return jsonify({'success': True, 'image': img})


@bp_api.route('/images/<int:image_id>', methods=['DELETE'])
def api_delete_image(image_id: int):
    set_log_context(biz='point_count', image_id=image_id)
    img = PointCountImage.get_by_id(image_id)
    if not img:
        return jsonify({'success': False, 'error': '图片不存在'}), 404
    PointCountImage.delete(image_id)
    return jsonify({'success': True})


# ── REST:计数点 ─────────────────────────────────────────────
@bp_api.route('/images/<int:image_id>/marks', methods=['POST'])
def api_add_mark(image_id: int):
    set_log_context(biz='point_count', image_id=image_id)
    img = PointCountImage.get_by_id(image_id)
    if not img:
        return jsonify({'success': False, 'error': '图片不存在'}), 404
    data = request.get_json(silent=True) or {}
    try:
        x = float(data.get('x_ratio', data.get('x', 0)))
        y = float(data.get('y_ratio', data.get('y', 0)))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'x_ratio / y_ratio 必须是数字'}), 400
    result = PointCountImage.add_mark(image_id, x, y)
    return jsonify({
        'success': True,
        'marks': result['marks'],
        'n_marks': len(result['marks']),
    })


@bp_api.route('/images/<int:image_id>/marks/last', methods=['DELETE'])
def api_undo_mark(image_id: int):
    set_log_context(biz='point_count', image_id=image_id)
    img = PointCountImage.get_by_id(image_id)
    if not img:
        return jsonify({'success': False, 'error': '图片不存在'}), 404
    result = PointCountImage.delete_last_mark(image_id)
    return jsonify({
        'success': True,
        'marks': result['marks'],
        'n_marks': len(result['marks']),
    })


@bp_api.route('/images/<int:image_id>/mark-scale', methods=['POST'])
def api_set_mark_scale(image_id: int):
    set_log_context(biz='point_count', image_id=image_id)
    img = PointCountImage.get_by_id(image_id)
    if not img:
        return jsonify({'success': False, 'error': '图片不存在'}), 404
    data = request.get_json(silent=True) or {}
    try:
        scale = float(data.get('scale', 1))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'scale 必须是数字'}), 400
    scale = PointCountImage.set_mark_scale(image_id, scale)
    return jsonify({'success': True, 'scale': scale})


@bp_api.route('/images/<int:image_id>/loose-count', methods=['POST'])
def api_set_loose_count(image_id: int):
    set_log_context(biz='point_count', image_id=image_id)
    img = PointCountImage.get_by_id(image_id)
    if not img:
        return jsonify({'success': False, 'error': '图片不存在'}), 404
    data = request.get_json(silent=True) or {}
    try:
        count = float(data.get('count', 0))
    except (TypeError, ValueError):
        return jsonify({'success': False, 'error': 'count 必须是数字'}), 400
    count = PointCountImage.set_loose_count(image_id, count)
    return jsonify({'success': True, 'count': count})
