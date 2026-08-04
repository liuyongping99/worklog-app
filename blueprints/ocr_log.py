"""OCR/AI 调用日志：装饰器 + 业务上下文 + 脱敏截断。

用法见 blueprints/ocr_engine.py —— 5 个引擎方法各加一行装饰器，
全项目约 30 处调用点即全部覆盖。
"""
import re
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
