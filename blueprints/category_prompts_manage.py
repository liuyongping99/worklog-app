"""分类提示词管理页蓝图。

提供:
  GET   /manage/category-prompts                          渲染管理主页
  GET   /api/v1/manage/category-prompts/list              列表查询(按 code/空填过滤)
  PATCH /api/v1/manage/category-prompts/<int:id>          仅更新 prompt_text
  POST  /api/v1/manage/category-prompts/bulk              批量更新多个 id

设计:
- 与 shipping.py 内的 POST /api/v1/category-prompts(create) 并存不冲突——
  本蓝图走独立前缀 /api/v1/manage/category-prompts,语义是"管理页维护"。
- update_text() 允许空串(管理页"清空"按钮);create() 仍要求非空(生成式流程)。
"""
from flask import Blueprint, render_template, request, jsonify, current_app

from models.category_prompt import CategoryPrompt, list_management_categories

bp = Blueprint('manage_category_prompts', __name__)


@bp.route('/manage/category-prompts')
def manage_page():
    """渲染管理主页,模板底部 JS 通过 fetch 异步拉数据。"""
    cats = list_management_categories()
    return render_template(
        'manage-category-prompts.html',
        categories=cats,
    )


@bp.route('/api/v1/manage/category-prompts/list', methods=['GET'])
def api_list():
    """列表查询,支持按 category_code / include_empty 过滤。

    Query:
      category_code: 精确过滤(可选)
      include_empty: '1'(默认)/'0' → 0 = 只返回已填
      scope: 'category'(默认)/'spec'/'all'
    """
    try:
        category_code = request.args.get('category_code', '').strip() or None
        include_empty = request.args.get('include_empty', '1') != '0'
        scope_arg = request.args.get('scope', 'category').strip()
        scope = scope_arg if scope_arg in ('category', 'spec') else None
        rows = CategoryPrompt.list_active_by_category(
            scope=scope,
            category_code=category_code,
            include_empty=include_empty,
        )
        return jsonify({'success': True, 'rows': rows, 'count': len(rows)})
    except Exception as e:
        current_app.logger.exception('manage list failed')
        return jsonify({'success': False, 'error': f'查询失败: {e}'}), 500


@bp.route('/api/v1/manage/category-prompts/<int:prompt_id>', methods=['PATCH'])
def api_patch(prompt_id):
    """仅更新 prompt_text;允许空串。"""
    data = request.get_json(silent=True) or {}
    text = data.get('prompt_text')
    if text is None or not isinstance(text, str):
        return jsonify({'success': False, 'error': 'prompt_text 必须是字符串'}), 400

    row = CategoryPrompt.get_by_id(prompt_id)
    if not row:
        return jsonify({'success': False, 'error': f'提示词 #{prompt_id} 不存在'}), 404

    try:
        CategoryPrompt.update_text(prompt_id, text)
        return jsonify({'success': True, 'id': prompt_id,
                        'prompt_text': text,
                        'status': 'updated'})
    except Exception as e:
        current_app.logger.exception('manage patch failed')
        return jsonify({'success': False, 'error': f'更新失败: {e}'}), 500


@bp.route('/api/v1/manage/category-prompts/bulk', methods=['POST'])
def api_bulk():
    """批量把同一段文本写入多个 prompt id。

    Body:
      {ids: [int, int, ...], prompt_text: str}
    """
    data = request.get_json(silent=True) or {}
    ids = data.get('ids') or []
    text = data.get('prompt_text')
    if not isinstance(ids, list) or not ids:
        return jsonify({'success': False, 'error': 'ids 必须是非空数组'}), 400
    if text is None or not isinstance(text, str):
        return jsonify({'success': False, 'error': 'prompt_text 必须是字符串'}), 400

    updated = 0
    skipped = 0
    failed_ids = []
    for pid in ids:
        try:
            if not isinstance(pid, int):
                pid = int(pid)
            row = CategoryPrompt.get_by_id(pid)
            if not row:
                skipped += 1
                continue
            CategoryPrompt.update_text(pid, text)
            updated += 1
        except (ValueError, TypeError):
            failed_ids.append(pid)
        except Exception:
            current_app.logger.exception(f'bulk update failed for id={pid}')
            failed_ids.append(pid)

    return jsonify({
        'success': True,
        'updated': updated,
        'skipped': skipped,
        'failed_ids': failed_ids,
    })
