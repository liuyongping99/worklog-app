"""tests 包初始化 + 共享测试工具"""
import io


def fake_png_bytes():
    """生成最小合法 PNG 字节(过 Pillow verify + magic 校验)。

    2026-08-04:之前测试用 b'\\x89PNG\\r\\n\\x1a\\n' + b'0' * 64 当图,
    头部合法但 Pillow verify 报「Truncated File Read」。改用 Pillow 真生成,
    所有行级图上传测试统一用这个。
    """
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', (4, 4), 'white').save(buf, 'PNG')
    return buf.getvalue()


def fake_png_stream():
    """同上,返回 BytesIO(seek 到 0)。"""
    return io.BytesIO(fake_png_bytes())
