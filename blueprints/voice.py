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
    """接收音频 → 返回候选 JSON
    浏览器送 webm → 服务端 ffmpeg 转 wav → 百度 ASR
    """
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

    # ffmpeg 转码: webm → wav(pcm 16k 16bit 单声道,百度要求)
    wav_bytes, audio_format = _convert_to_baidu_format(audio_bytes, file.filename)
    if wav_bytes is None:
        return jsonify({
            'success': False,
            'error': '音频转码失败',
            'hint': '检查 FFMPEG_PATH 是否配置正确',
        }), 500

    try:
        result = voice_pipeline.recognize(wav_bytes, audio_format=audio_format)
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


def _convert_to_baidu_format(audio_bytes: bytes, filename: str) -> tuple[bytes | None, str]:
    """浏览器录音 webm → 百度 ASR 要求的 pcm (16k 16bit 单声道 raw)。
    返回 (pcm_bytes, format)。失败返回 (None, '')。
    用 PCM raw 而非 WAV(避开 WAV header 兼容性陷阱)
    """
    import os
    import subprocess
    import tempfile

    ffmpeg_path = os.environ.get('FFMPEG_PATH', 'ffmpeg')
    fmt = 'pcm'  # 用 raw PCM,百度更稳

    # 写临时文件,ffmpeg 处理
    with tempfile.NamedTemporaryFile(suffix='.webm', delete=False) as src:
        src.write(audio_bytes)
        src_path = src.name
    dst_path = src_path + '.pcm'

    try:
        # -ar 16000 -ac 1 -f s16le -acodec pcm_s16le
        # s16le = signed 16-bit little-endian raw PCM(百度短语音要求,无 header)
        proc = subprocess.run(
            [ffmpeg_path, '-y', '-i', src_path,
             '-ar', '16000', '-ac', '1',
             '-acodec', 'pcm_s16le',
             '-f', 's16le', dst_path],
            capture_output=True, timeout=30,
        )
        if proc.returncode != 0:
            logger.error('ffmpeg 失败: %s', proc.stderr.decode('utf-8', 'ignore')[:500])
            return None, ''
        with open(dst_path, 'rb') as f:
            pcm_bytes = f.read()
        # 百度短语音要求最少 1 秒,16k 16bit mono = 32000 字节/秒
        # < 16000 字节(< 0.5 秒)直接拒,避免无效请求
        if len(pcm_bytes) < 16000:
            logger.warning('音频过短: %d 字节(< 0.5 秒),百度可能拒', len(pcm_bytes))
            return None, ''
        logger.info('音频转码 OK: webm %d 字节 → pcm %d 字节 (~%.1f 秒)',
                    len(audio_bytes), len(pcm_bytes), len(pcm_bytes) / 32000)
        return pcm_bytes, fmt
    except FileNotFoundError:
        logger.error('ffmpeg 未找到: %s(请检查 FFMPEG_PATH)', ffmpeg_path)
        return None, ''
    except subprocess.TimeoutExpired:
        logger.error('ffmpeg 超时(>30s)')
        return None, ''
    finally:
        for p in (src_path, dst_path):
            try:
                os.unlink(p)
            except OSError:
                pass


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


# ────────────────────────────────────────────────────────────
# 口语短语映射管理(CRUD)— 给产品页 tab 用
# ────────────────────────────────────────────────────────────
from models import VoiceMapping  # 避免循环:放最后


@bp.route('/api/v1/voice/mappings', methods=['GET'])
def voice_mappings_list():
    """管理页用:列出口语映射"""
    search = request.args.get('search')
    mappings = VoiceMapping.list_all(search=search)
    return jsonify({'success': True, 'mappings': mappings})


@bp.route('/api/v1/voice/mappings', methods=['POST'])
def voice_mappings_create():
    """新增口语映射"""
    body = request.get_json(silent=True) or {}
    phrase = body.get('phrase', '').strip()
    product_id = body.get('product_id')
    spec_hint = body.get('spec_hint')
    if not phrase or not product_id:
        return jsonify({'success': False, 'error': '缺少 phrase 或 product_id'}), 400
    try:
        mapping = VoiceMapping.upsert(
            phrase=phrase,
            product_id=product_id,
            spec_hint=spec_hint,
            source='user_confirmed',
        )
    except ValueError as e:
        return jsonify({'success': False, 'error': str(e)}), 400
    return jsonify({'success': True, 'mapping': mapping})


@bp.route('/api/v1/voice/mappings/<int:mapping_id>', methods=['PATCH'])
def voice_mappings_patch(mapping_id):
    """编辑/启用/停用"""
    body = request.get_json(silent=True) or {}
    status = body.get('status')
    if status:
        try:
            mapping = VoiceMapping.set_status(mapping_id, status)
        except ValueError as e:
            return jsonify({'success': False, 'error': str(e)}), 400
        if not mapping:
            return jsonify({'success': False, 'error': '映射不存在'}), 404
        return jsonify({'success': True, 'mapping': mapping})
    return jsonify({'success': False, 'error': '暂未实现编辑接口'}), 400


@bp.route('/api/v1/voice/mappings/<int:mapping_id>', methods=['DELETE'])
def voice_mappings_delete(mapping_id):
    """删除口语映射"""
    VoiceMapping.delete(mapping_id)
    return jsonify({'success': True})
