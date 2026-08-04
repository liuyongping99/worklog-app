"""OCR/AI 调用日志：装饰器 + 业务上下文 + 脱敏截断。

用法见 blueprints/ocr_engine.py —— 5 个引擎方法各加一行装饰器，
全项目约 30 处调用点即全部覆盖。
"""
import functools
import logging
import re
import time
from contextvars import ContextVar

# 业务上下文用 ContextVar 而非 flask.g —— 理由同 trace_id，见 logging_setup.py
_LOG_CTX = ContextVar('worklog_ocr_log_ctx', default=None)

# 形如 sk-xxxxxxxx 的密钥（key 走 header 不走 prompt，这里是防御性兜底）
_KEY_RE = re.compile(r'sk-[A-Za-z0-9\-_]{8,}')


def set_log_context(**kw):
    """把业务字段（order_id / record_id / image_id 等）挂到当前 context。

    累加语义：多次调用会合并，值为 None 的键忽略。
    """
    try:
        current = _LOG_CTX.get() or {}
        merged = dict(current)
        merged.update({k: v for k, v in kw.items() if v is not None})
        _LOG_CTX.set(merged)
    except Exception:
        pass


def get_log_context():
    """返回当前业务上下文的副本。"""
    try:
        return dict(_LOG_CTX.get() or {})
    except Exception:
        return {}


def clear_log_context():
    try:
        _LOG_CTX.set(None)
    except Exception:
        pass


def scrub(text):
    """抹掉疑似 API key。非字符串一律转字符串，None 转空串。"""
    if text is None:
        return ''
    if not isinstance(text, str):
        text = str(text)
    return _KEY_RE.sub('sk-***', text)


def trunc(value, limit=200):
    """超长截断，并标注原始长度，避免 INFO 行被自由文本撑爆。"""
    text = scrub(value)
    if len(text) <= limit:
        return text
    return '%s…<共%d字符>' % (text[:limit], len(text))


# ═══════════════════════════════════════════════════════════════════
# 预置的入参 / 返回值提取器
#
# 约定：都返回 (summary, detail) 两个 dict。
#   summary —— INFO 也写，必须短
#   detail  —— 只在 DEBUG 或失败时写，可以很长
# ═══════════════════════════════════════════════════════════════════


def image_payload(self, image_bytes, filename='', *a, **kw):
    """给 recognize / extract_text 用：只记文件名和字节数，绝不记图片内容。"""
    try:
        size = len(image_bytes) if image_bytes else 0
    except Exception:
        size = 0
    return ({'img': filename or '-', 'size': '%dKB' % (size // 1024)}, {})


def prompt_payload(self, prompt_text, *a, **kw):
    """给 _call_api_with_prompt 用：summary 只记长度，detail 记全文。"""
    text = prompt_text if isinstance(prompt_text, str) else str(prompt_text)
    return ({'prompt_chars': len(text)}, {'prompt': text})


def text_outcome(result):
    """给 extract_text 用：返回值是 OCR 纯文本。"""
    text = result or ''
    return ({'chars': len(text)}, {'ocr_text': text})


def items_outcome(result):
    """给 recognize 用：返回值是 {'success', 'items', 'error', ...}。"""
    if not isinstance(result, dict):
        return ({}, {'raw': result})
    summary = {'items': len(result.get('items') or [])}
    detail = {}
    if not result.get('success'):
        summary['error'] = trunc(result.get('error'), 120)
        detail['hint'] = result.get('hint') or ''
    detail['items_json'] = result.get('items')
    return (summary, detail)


def parsed_outcome(result):
    """给 _call_api_with_prompt / compare 用：返回值是解析后的 JSON。"""
    summary = {}
    if isinstance(result, dict):
        if result.get('match_status'):
            summary['result'] = result['match_status']
        if result.get('score') is not None:
            summary['score'] = result['score']
    elif isinstance(result, list):
        summary['rows'] = len(result)
    return (summary, {'parsed': result})


def _default_payload(*a, **kw):
    return ({}, {})


def _default_outcome(result):
    return ({}, {})


# ═══════════════════════════════════════════════════════════════════
# 格式化
# ═══════════════════════════════════════════════════════════════════


def _fmt_summary(d):
    parts = []
    for k, v in d.items():
        if v is None or v == '':
            continue
        parts.append('%s=%s' % (k, trunc(v, 200)))
    return ' '.join(parts)


def _fmt_detail(d):
    """detail 每项单独一行缩进，方便肉眼扫和 grep -A。"""
    lines = []
    for k, v in d.items():
        if v is None or v == '':
            continue
        lines.append('\n    %s=%s' % (k, scrub(v)))
    return ''.join(lines)


def _safe(fn, *a, **kw):
    """调提取器；它自己炸了不能连累业务，返回空。"""
    try:
        got = fn(*a, **kw)
        if isinstance(got, tuple) and len(got) == 2:
            return got
    except Exception:
        pass
    return ({}, {})


# ═══════════════════════════════════════════════════════════════════
# 装饰器
# ═══════════════════════════════════════════════════════════════════


def log_ocr_call(logger_name, evt, failed_if=None, payload=None, outcome=None):
    """给 OCR/AI 引擎方法加日志。

    参数:
        logger_name — 'ocr.deepseek' / 'ocr.paddle' / 'ocr.moonshot'
        evt         — 事件名，出现在日志的 evt= 字段
        failed_if   — fn(result) -> bool。被包装的方法多半吞异常、用返回值表示失败
                      （extract_text 返回 ''、recognize 返回 {'success': False}），
                      光靠 try/except 抓不到，必须靠这个判定。
        payload     — fn(*args, **kwargs) -> (summary, detail)，提取入参
        outcome     — fn(result) -> (summary, detail)，提取返回值

    语义:
        成功 + INFO  → 只写 summary
        成功 + DEBUG → summary + detail（完整 prompt / 原始返回）
        失败(异常或 failed_if) → ERROR，summary + 全部 detail + 堆栈，
                                 不受 DEBUG 开关约束（偶发问题往往不可复现）

    对被包装方法完全透明：返回值原样返回，异常原样抛出。
    """
    payload = payload or _default_payload
    outcome = outcome or _default_outcome

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            log = logging.getLogger(logger_name)
            in_sum, in_det = _safe(payload, *args, **kwargs)
            t0 = time.perf_counter()
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:
                _emit(log, evt, t0, in_sum, in_det, {}, {}, failed=True, exc=exc)
                raise                       # 原样抛出，保持透明
            try:
                out_sum, out_det = _safe(outcome, result)
                failed = bool(failed_if(result)) if failed_if else False
                _emit(log, evt, t0, in_sum, in_det, out_sum, out_det, failed=failed)
            except Exception:
                pass                        # 日志失败绝不阻断业务
            return result

        return wrapper

    return deco


def _emit(log, evt, t0, in_sum, in_det, out_sum, out_det, failed=False, exc=None):
    try:
        elapsed = time.perf_counter() - t0
        summary = {'evt': evt}
        summary.update(get_log_context())
        summary.update(in_sum)
        summary.update(out_sum)
        summary['elapsed'] = '%.2fs' % elapsed
        head = _fmt_summary(summary)

        if failed or exc is not None:
            # 失败：全量 detail + 堆栈，无条件
            detail = {}
            detail.update(in_det)
            detail.update(out_det)
            if exc is not None:
                detail['exception'] = '%s: %s' % (type(exc).__name__, exc)
            log.error('%s%s', head, _fmt_detail(detail), exc_info=exc is not None)
        elif log.isEnabledFor(logging.DEBUG):
            detail = {}
            detail.update(in_det)
            detail.update(out_det)
            log.debug('%s%s', head, _fmt_detail(detail))
        else:
            log.info('%s', head)
    except Exception:
        pass
