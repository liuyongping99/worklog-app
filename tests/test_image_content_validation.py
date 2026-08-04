"""图片内容安全校验（M1 stub → 真正校验）。

CLAUDE.md 写:「保存后读取 magic bytes,不是真实图片则删除 + 400 返回」。
实际实现只查扩展名 + 大小,没有真校验。本套件补齐:

- _helpers.validate_image_content(filepath): 落盘后校验
    * 文件存在 + 非空 + ≤10MB
    * magic bytes 与扩展名一致(Pillow 真实能解码)
    * 不是图片 → raise ValueError
- _helpers.check_uploaded_image(file_storage): 上传时即时校验
    * 文件名扩展名白名单 + 大小 ≤10MB
    * 不读全文(避免内存峰值),读前 32 字节 magic 校验
"""
import io
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from blueprints._helpers import (
    check_uploaded_image, validate_image_content,
    _ALLOWED_IMAGE_EXTS, _MAX_IMAGE_BYTES,
)


def _png_bytes():
    from PIL import Image
    buf = io.BytesIO()
    Image.new('RGB', (4, 4), 'white').save(buf, 'PNG')
    return buf.getvalue()


class ValidateImageContentTests(unittest.TestCase):
    """落盘后校验:文件存在 + magic bytes 一致 + Pillow 能解码。"""

    def test_real_png_passes(self):
        path = tempfile.NamedTemporaryFile(suffix='.png', delete=False).name
        try:
            with open(path, 'wb') as f:
                f.write(_png_bytes())
            self.assertTrue(validate_image_content(path))
        finally:
            os.unlink(path)

    def test_text_with_png_ext_rejected(self):
        """伪装:.exe 改后缀为 .png, magic bytes 是 ASCII 文本 → 拒绝。"""
        path = tempfile.NamedTemporaryFile(suffix='.png', delete=False).name
        try:
            with open(path, 'wb') as f:
                f.write(b'NOT_AN_IMAGE_HERE\r\n' * 10)
            with self.assertRaises(ValueError) as cm:
                validate_image_content(path)
            self.assertIn('不是', str(cm.exception))
        finally:
            os.unlink(path)

    def test_truncated_png_rejected(self):
        """截断的 PNG(Pillow 解码失败) → 拒绝。"""
        path = tempfile.NamedTemporaryFile(suffix='.png', delete=False).name
        try:
            # PNG header + 截断
            with open(path, 'wb') as f:
                f.write(b'\x89PNG\r\n\x1a\n' + b'\x00' * 16)
            with self.assertRaises(ValueError):
                validate_image_content(path)
        finally:
            os.unlink(path)

    def test_disallowed_ext_rejected(self):
        path = tempfile.NamedTemporaryFile(suffix='.exe', delete=False).name
        try:
            with open(path, 'wb') as f:
                f.write(_png_bytes())
            with self.assertRaises(ValueError) as cm:
                validate_image_content(path)
            self.assertIn('格式', str(cm.exception))
        finally:
            os.unlink(path)

    def test_empty_file_rejected(self):
        path = tempfile.NamedTemporaryFile(suffix='.png', delete=False).name
        try:
            # open + 立即关 → 0 字节
            with self.assertRaises(ValueError):
                validate_image_content(path)
        finally:
            os.unlink(path)

    def test_oversized_rejected(self):
        path = tempfile.NamedTemporaryFile(suffix='.png', delete=False).name
        try:
            # 写一个 > 10MB 的文件(magic 仍有效 → magic 通过,size 拦截)
            with open(path, 'wb') as f:
                f.write(b'\x89PNG\r\n\x1a\n')
                f.write(b'X' * (_MAX_IMAGE_BYTES + 1))
            with self.assertRaises(ValueError) as cm:
                validate_image_content(path)
            self.assertIn('过大', str(cm.exception))
        finally:
            os.unlink(path)

    def test_missing_file_rejected(self):
        with self.assertRaises(ValueError):
            validate_image_content('Z:/does/not/exist.png')

    def test_supported_exts_and_size_constants(self):
        self.assertIn('.png', _ALLOWED_IMAGE_EXTS)
        self.assertIn('.jpg', _ALLOWED_IMAGE_EXTS)
        self.assertEqual(_MAX_IMAGE_BYTES, 10 * 1024 * 1024)


class CheckUploadedImageTests(unittest.TestCase):
    """上传时校验:扩展名白名单 + 大小 + magic bytes(读前 32 字节即可)。"""

    class _FakeUpload:
        def __init__(self, filename, content):
            self.filename = filename
            self._buf = io.BytesIO(content)

        def seek(self, *args, **kwargs):
            return self._buf.seek(*args, **kwargs)

        def tell(self):
            return self._buf.tell()

        def read(self, n=-1):
            return self._buf.read(n)

    def test_real_png_returns_ext(self):
        f = self._FakeUpload('label.png', _png_bytes())
        ext = check_uploaded_image(f)
        self.assertEqual(ext, '.png')

    def test_disguised_exe_rejected(self):
        """文件名合法扩展名 + 内容是 EXE(MZ 头)→ 拒绝。"""
        f = self._FakeUpload('evil.png', b'MZ\x90\x00\x03\x00\x00\x00' + b'\x00' * 100)
        with self.assertRaises(ValueError):
            check_uploaded_image(f)

    def test_bad_ext_rejected(self):
        f = self._FakeUpload('virus.exe', b'anything')
        with self.assertRaises(ValueError) as cm:
            check_uploaded_image(f)
        self.assertIn('格式', str(cm.exception))

    def test_empty_filename_rejected(self):
        f = self._FakeUpload('', b'')
        with self.assertRaises(ValueError):
            check_uploaded_image(f)


if __name__ == '__main__':
    unittest.main()