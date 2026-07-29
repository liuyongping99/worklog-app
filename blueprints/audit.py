"""OCR 事件审计页蓝图。

提供:
  GET  /audit/ocr-events                   渲染审计主页
  GET  /api/v1/audit/ocr-events/aggregate JSON 聚合数据
  GET  /api/v1/audit/ocr-events/records   JSON 钻入明细
  GET  /api/v1/audit/ocr-events/export.csv CSV 文件流
"""
import csv
import io
from datetime import datetime
from flask import Blueprint, render_template, request, jsonify, Response, current_app

from models import OcrEventAudit
from models.audit_query import _EXPORT_FIELDS

bp = Blueprint('audit', __name__)


def _parse_common_filters():
    """解析 start_date / end_date / prompt_versions 公共参数。

    Raises:
      ValueError: 日期格式错 / 开始 > 结束
    """
    start = request.args.get('start_date', '').strip() or None
    end = request.args.get('end_date', '').strip() or None
    pv_csv = request.args.get('prompt_versions', '').strip()
    prompt_versions = [v for v in pv_csv.split(',') if v] or None
    for label, val in (('start_date', start), ('end_date', end)):
        if val:
            try:
                datetime.strptime(val, '%Y-%m-%d')
            except ValueError:
                raise ValueError(f'{label} 格式错误,应为 YYYY-MM-DD')
    if start and end and start > end:
        raise ValueError('开始日期不能晚于结束日期')
    return {'start_date': start, 'end_date': end, 'prompt_versions': prompt_versions}


@bp.route('/audit/ocr-events')
def audit_ocr_events():
    """渲染审计主页(Jinja2 模板 + base.html)"""
    return render_template('audit-ocr-events.html')


@bp.route('/api/v1/audit/ocr-events/aggregate')
def api_audit_aggregate():
    try:
        params = _parse_common_filters()
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    try:
        rows = OcrEventAudit.prompt_stats(**params)
    except Exception:
        current_app.logger.exception('prompt_stats 失败')
        return jsonify({'success': False, 'error': '查询失败'}), 500
    return jsonify({'success': True, 'rows': rows})


@bp.route('/api/v1/audit/ocr-events/records')
def api_audit_records():
    prompt_version = request.args.get('prompt_version', '').strip()
    if not prompt_version:
        return jsonify({'success': False, 'error': 'prompt_version 必填'}), 400
    try:
        common = _parse_common_filters()
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    limit = min(int(request.args.get('limit', 500)), 2000)
    offset = max(int(request.args.get('offset', 0)), 0)
    try:
        rows, total = OcrEventAudit.record_pairs(
            prompt_version=prompt_version,
            start_date=common['start_date'],
            end_date=common['end_date'],
            limit=limit, offset=offset,
        )
    except Exception:
        current_app.logger.exception('record_pairs 失败')
        return jsonify({'success': False, 'error': '查询失败'}), 500
    return jsonify({
        'success': True, 'rows': rows, 'total': total,
        'limit': limit, 'offset': offset,
    })


@bp.route('/api/v1/audit/ocr-events/export.csv')
def api_audit_export_csv():
    start = request.args.get('start_date', '').strip() or None
    end = request.args.get('end_date', '').strip() or None
    et_csv = request.args.get('event_types', '').strip()
    pv_csv = request.args.get('prompt_versions', '').strip()
    event_types = [t for t in et_csv.split(',') if t] or None
    prompt_versions = [v for v in pv_csv.split(',') if v] or None
    try:
        rows = OcrEventAudit.export_rows(
            start_date=start, end_date=end,
            event_types=event_types, prompt_versions=prompt_versions,
        )
    except Exception:
        current_app.logger.exception('export_rows 失败')
        return jsonify({'success': False, 'error': '导出失败'}), 500

    fname = f'ocr_events_{datetime.now().strftime("%Y%m%d_%H%M")}.csv'
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_EXPORT_FIELDS)
    writer.writeheader()
    for r in rows:
        writer.writerow({k: (r.get(k) if r.get(k) is not None else '') for k in _EXPORT_FIELDS})
    body = buf.getvalue().encode('utf-8-sig')
    return Response(
        body,
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename="{fname}"'},
    )