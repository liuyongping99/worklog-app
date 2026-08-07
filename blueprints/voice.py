"""语音录入蓝图:识别 + 确认。"""
import logging
from flask import Blueprint, request, jsonify

# 注意:用模块属性访问 voice_pipeline.recognize/confirm,不走 from import。
# 这样 monkeypatch.setattr(voice_pipeline, 'recognize', ...) 在测试中能生效。
from blueprints import voice_pipeline

logger = logging.getLogger(__name__)

bp = Blueprint('voice', __name__)


@bp.route('/api/v1/voice/recognize', methods=['POST'])
def voice_recognize():
    """接收音频 → 返回候选 JSON"""
    if 'audio' not in request.files:
        return jsonify({
            'success': False,
            'error': '没有上传音频',
            'hint': '请先录音',
        }), 400
    file = request.files['audio']
    if file.filename == '':
        return jsonify({'success': False, 'error': '未选择文件'}), 400

    # 限制音频大小(10MB,与现有图片校验一致)
    audio_bytes = file.read()
    if len(audio_bytes) > 10 * 1024 * 1024:
        return jsonify({
            'success': False,
            'error': '音频过大',
            'hint': '请压缩到 10MB 以内',
        }), 400

    audio_format = 'wav'  # 上游约定:浏览器走 ffmpeg 转 wav(JS 端做)
    # TODO: V1.1 让前端传 audio_format 字段

    try:
        result = voice_pipeline.recognize(audio_bytes, audio_format=audio_format)
        return jsonify(result)
    except RuntimeError as e:
        logger.exception('语音识别失败: %s', e)
        return jsonify({
            'success': False,
            'error': str(e),
            'hint': '请检查 .env 配置或网络',
        }), 500
    except Exception as e:
        logger.exception('语音识别异常: %s', e)
        return jsonify({
            'success': False,
            'error': f'识别异常:{type(e).__name__}',
            'hint': '请稍后重试',
        }), 500


@bp.route('/api/v1/voice/confirm', methods=['POST'])
def voice_confirm():
    """用户确认候选 → 写映射表 + 批量插入明细行"""
    body = request.get_json(silent=True) or {}
    order_id = body.get('order_id')
    items = body.get('items', [])
    if not order_id:
        return jsonify({'success': False, 'error': '缺少 order_id'}), 400
    if not isinstance(items, list) or not items:
        return jsonify({'success': False, 'error': 'items 不能为空'}), 400
    try:
        result = voice_pipeline.confirm(order_id=order_id, items=items)
        return jsonify(result)
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    except Exception as e:
        logger.exception('语音确认失败: %s', e)
        return jsonify({
            'success': False,
            'error': f'确认失败:{type(e).__name__}',
        }), 500
