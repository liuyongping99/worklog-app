"""PaddleOCREngine `apply_wrinkle_enhance` 参数路由单测。

本任务(Task 3)在 PaddleOCREngine 类内加 4 处修改:
  1. __init__ 加 _wrinkle_ocr = None
  2. _ensure_model 加第二个 PaddleOCR(...) 实例化(更激进参数)
  3. extract_text 加 apply_wrinkle_enhance 参数 + 路由
  4. extract_text_with_conf 加 apply_wrinkle_enhance 参数 + 路由

Fix round 1(2026-08-16 评审反馈):
  - Finding #1:CLAHE 真正生效需要先 bytes → numpy 解码。
    测试补丁要同时 mock 掉 PIL.Image.open 和 np.array,否则
    `Image.open(io.BytesIO(b'data'))` 会因 b'data' 不是合法图片而抛异常,
    让所有 CLAHE 路径的测试直接走外层 except 返回空串,根本测不到 CLAHE 调用。
  - Finding #2:`from paddleocr import PaddleOCR` 提升到 _ensure_model 顶部,
    避免 _wrinkle_ocr 单飞时 NameError。新增 test_ensure_model_wrinkle_load_failure_falls_back_to_ocr。
  - Finding #3:`ocr = (self._wrinkle_ocr or self._ocr) if ...` 兜底路由。
    新增 test_init_sets_wrinkle_ocr_to_none 覆盖 __init__ 初始化默认值。

测试要点:
- 用 MagicMock 完全替代 PaddleOCR(避免 200MB 模型初始化)
- 验证参数路由正确(True → _wrinkle_ocr, False → _ocr)
- 验证 CLAHE 调用时机(True → 调, False → 不调)
- 默认 False 保持向后兼容

全局约束:
- apply_wrinkle_enhance=False 为默认值,老调用方零感知
- _wrinkle_ocr 加载失败时路由回 _ocr(在 _ensure_model 内部 try/except 处理)
"""
import sys
import types
import unittest
from unittest.mock import MagicMock, patch

from blueprints.ocr_engine import PaddleOCREngine


def _install_fake_paddleocr():
    """Inject a fake `paddleocr` module into sys.modules so `_ensure_model`'s
    `from paddleocr import PaddleOCR` doesn't trigger the real paddleocr import
    chain (which loads torch → fatal Windows DLL error 0xc0000139 on some
    worktree setups).

    Returns the MagicMock that the patched `PaddleOCR(...)` call will use.
    """
    fake_paddleocr = types.ModuleType('paddleocr')
    fake_paddleocr.PaddleOCR = MagicMock(name='PaddleOCR_class')
    sys.modules['paddleocr'] = fake_paddleocr
    return fake_paddleocr.PaddleOCR


class _ImagePipelinePatch:
    """组合 patch 上下文,把 bytes → numpy 解码路径整体 mock 掉。

    `extract_text` / `extract_text_with_conf` 在调 CLAHE 之前会做:
        np.array(Image.open(io.BytesIO(resized)).convert('RGB'))
    测试里 `_resize_if_needed` 被 mock 返回 b'data',b'data' 不是合法图片,
    PIL 解码会失败,触发外层 except → 静默返回空串,CLAHE 永远不被调用。
    用这个上下文把 Image.open / np.array 替换成 MagicMock,让 numpy 解码成功,
    CLAHE 路径才能被真正测到。
    """

    def __init__(self, np_return_value=None):
        self.np_return_value = np_return_value if np_return_value is not None else MagicMock(name='np_array')
        self._fake_img = MagicMock(name='PIL_Image')
        self._fake_img.convert.return_value = self._fake_img
        self._patches = [
            patch('blueprints.ocr_engine.Image.open', return_value=self._fake_img),
            patch('blueprints.ocr_engine.np.array', return_value=self.np_return_value),
        ]

    def __enter__(self):
        for p in self._patches:
            p.start()
        return self

    def __exit__(self, *args):
        for p in reversed(self._patches):
            p.stop()


class PaddleOCRWrinkleParamTests(unittest.TestCase):
    """PaddleOCREngine `apply_wrinkle_enhance` 参数路由 — 8 项覆盖。

    所有测试用 MagicMock 替代真实 PaddleOCR 实例,只验证路由逻辑,不实际跑 OCR。
    """

    def _make_engine(self):
        """构造 PaddleOCREngine 实例,塞入两个 MagicMock 替代真实 _ocr / _wrinkle_ocr。"""
        engine = PaddleOCREngine.__new__(PaddleOCREngine)
        engine._ocr = MagicMock(name='_ocr')
        engine._wrinkle_ocr = MagicMock(name='_wrinkle_ocr')
        return engine

    # ── 新增(Fix round 1)────────────────────────────────────────

    def test_init_sets_wrinkle_ocr_to_none(self):
        """Finding #3 的初始化默认值保护:__init__ 必须设 _wrinkle_ocr = None。

        使用真正的 __init__(不绕过 __new__),防止以后误改默认值悄悄失效。
        """
        engine = PaddleOCREngine()
        # 必须显式断言 — 别用 `is` 但允许 None
        self.assertIsNone(engine._wrinkle_ocr,
            'PaddleOCREngine.__init__ 必须把 _wrinkle_ocr 初始化为 None,'
            '否则 extract_text 的 `or self._ocr` 兜底判断失效')
        self.assertIsNone(engine._ocr,
            '_ocr 也应该是 None(避免后续修改误动默认初始值)')

    def test_ensure_model_wrinkle_load_failure_falls_back_to_ocr(self):
        """Finding #2 修复验证:`from paddleocr import PaddleOCR` 提升后,
        _wrinkle_ocr 实例化失败时,仍能回退到 _ocr(而不是 NameError 被吞)。

        关键:之前版本 `_ocr` 实例化时 PaddleOCR 名字只在第一个 if 块内绑定。
        如果只让 _wrinkle_ocr 单飞(预先加载 _ocr),第二个块用 `PaddleOCR(...)`
        会 NameError → 被 except 吞 → self._wrinkle_ocr 仍是 None。
        修复后 import 提升,第二次调用能正常进入 except 分支并回退。

        Patch 方式:用 `_install_fake_paddleocr()` 把 fake 模块塞到 sys.modules,
        让 `from paddleocr import PaddleOCR` 走 fake,不触发真 paddleocr import chain
        (该 chain 会拉 torch DLL,在某些 Windows worktree 触发 0xc0000139)。
        """
        mock_paddle_cls = _install_fake_paddleocr()
        engine = PaddleOCREngine()
        # 预填 _ocr,模拟"_ocr 已加载,只重新走 _wrinkle_ocr 路径"的极端情况。
        # 这是提升 import 之前 NameError 的触发路径。
        fake_ocr_instance = MagicMock(name='_ocr_instance')
        # 第一次调用(已有 _ocr 不会触发,但保险起见)— 返回正常实例
        # 第二次调用(_wrinkle_ocr 分支)— 抛异常,触发 except 回退
        mock_paddle_cls.side_effect = [fake_ocr_instance, RuntimeError('模拟加载失败')]
        engine._ensure_model()
        # 关键断言:_wrinkle_ocr 必须回退到 _ocr(不能是 None,也不能是 MagicMock)
        self.assertIs(engine._wrinkle_ocr, engine._ocr,
            'PaddleOCR 第二次实例化失败时,'
            'engine._wrinkle_ocr 必须回退到 engine._ocr;'
            f'实际 _wrinkle_ocr={engine._wrinkle_ocr!r} _ocr={engine._ocr!r}')
        self.assertIs(engine._ocr, fake_ocr_instance,
            '_ocr 应是第一次 PaddleOCR() 的返回值(因为已经走了 _wrinkle_ocr 回退分支)')

    # ── 原 6 项(已更新以兼容 bytes → numpy 解码)──────────────────

    def test_extract_text_accepts_default_param(self):
        """无参调用不抛 TypeError — apply_wrinkle_enhance 应有默认值 False。"""
        engine = self._make_engine()
        # 不传 apply_wrinkle_enhance — 必须不抛 TypeError
        with patch.object(engine, '_ensure_model'), \
             patch.object(engine, '_resize_if_needed', return_value=b'data'), \
             patch.object(engine, '_enhance_wrinkle_label') as mock_enhance:
            engine._ocr.ocr.return_value = [[[[0, 0], ['hello', 0.99]]]]
            result = engine.extract_text(b'fake_image_bytes')
        self.assertEqual(result, 'hello',
            '无参调用应走默认 apply_wrinkle_enhance=False 路径, 正常返回 OCR 文本')
        # 默认 False → 不应调 CLAHE
        mock_enhance.assert_not_called()

    def test_extract_text_routes_to_wrinkle_ocr_when_true(self):
        """apply_wrinkle_enhance=True → _wrinkle_ocr.ocr 被调, _ocr.ocr 不被调。"""
        engine = self._make_engine()
        with patch.object(engine, '_ensure_model'), \
             patch.object(engine, '_resize_if_needed', return_value=b'data'), \
             patch.object(engine, '_enhance_wrinkle_label', return_value='mock_enhanced'), \
             _ImagePipelinePatch():
            engine._wrinkle_ocr.ocr.return_value = [[[[0, 0], ['wrinkle', 0.95]]]]
            engine._ocr.ocr.return_value = [[[[0, 0], ['normal', 0.95]]]]
            result = engine.extract_text(b'fake', apply_wrinkle_enhance=True)
        self.assertEqual(result, 'wrinkle',
            'apply_wrinkle_enhance=True 时应返回 _wrinkle_ocr 的结果')
        engine._wrinkle_ocr.ocr.assert_called_once()
        engine._ocr.ocr.assert_not_called()

    def test_extract_text_routes_to_normal_ocr_when_false(self):
        """apply_wrinkle_enhance=False → _ocr.ocr 被调, _wrinkle_ocr.ocr 不被调。"""
        engine = self._make_engine()
        with patch.object(engine, '_ensure_model'), \
             patch.object(engine, '_resize_if_needed', return_value=b'data'), \
             patch.object(engine, '_enhance_wrinkle_label') as mock_enhance:
            engine._ocr.ocr.return_value = [[[[0, 0], ['normal', 0.95]]]]
            result = engine.extract_text(b'fake', apply_wrinkle_enhance=False)
        self.assertEqual(result, 'normal')
        engine._ocr.ocr.assert_called_once()
        engine._wrinkle_ocr.ocr.assert_not_called()
        # False → 不应调 CLAHE
        mock_enhance.assert_not_called()

    def test_extract_text_with_conf_routes_correctly(self):
        """extract_text_with_conf 也支持参数 — True/False 各路由一次。"""
        engine = self._make_engine()
        # True 路径
        with patch.object(engine, '_ensure_model'), \
             patch.object(engine, '_resize_if_needed', return_value=b'data'), \
             patch.object(engine, '_enhance_wrinkle_label', return_value='mock_enhanced'), \
             _ImagePipelinePatch():
            engine._wrinkle_ocr.ocr.return_value = [[[[0, 0], ['wrinkle', 0.95]]]]
            text, conf = engine.extract_text_with_conf(b'fake', apply_wrinkle_enhance=True)
        self.assertEqual(text, 'wrinkle')
        self.assertAlmostEqual(conf, 0.95)
        engine._wrinkle_ocr.ocr.assert_called_once()
        engine._ocr.ocr.assert_not_called()

        # 重置 mock, False 路径
        engine._wrinkle_ocr.reset_mock()
        engine._ocr.reset_mock()
        with patch.object(engine, '_ensure_model'), \
             patch.object(engine, '_resize_if_needed', return_value=b'data'), \
             patch.object(engine, '_enhance_wrinkle_label') as mock_enhance:
            engine._ocr.ocr.return_value = [[[[0, 0], ['normal', 0.92]]]]
            text, conf = engine.extract_text_with_conf(b'fake', apply_wrinkle_enhance=False)
        self.assertEqual(text, 'normal')
        self.assertAlmostEqual(conf, 0.92)
        engine._ocr.ocr.assert_called_once()
        engine._wrinkle_ocr.ocr.assert_not_called()
        mock_enhance.assert_not_called()

    def test_extract_text_with_enhance_runs_clahe(self):
        """apply_wrinkle_enhance=True 时 _enhance_wrinkle_label 必须被调一次。

        Fix round 1 调整:bytes → numpy 解码现在由 extract_text 自己完成,
        所以 _enhance_wrinkle_label 收到的是 numpy 数组(不再是 bytes)。
        """
        engine = self._make_engine()
        np_mock = MagicMock(name='np_array_after_decode')
        with patch.object(engine, '_ensure_model'), \
             patch.object(engine, '_resize_if_needed', return_value=b'data'), \
             patch.object(engine, '_enhance_wrinkle_label', return_value='mock_enhanced') as mock_enhance, \
             _ImagePipelinePatch(np_return_value=np_mock):
            engine._wrinkle_ocr.ocr.return_value = [[[[0, 0], ['x', 0.99]]]]
            engine.extract_text(b'fake', apply_wrinkle_enhance=True)
        # 关键:CLAHE 收到的应是 numpy 数组(从 bytes 解码而来),不是 bytes 本身
        mock_enhance.assert_called_once_with(np_mock)

    def test_extract_text_without_enhance_skips_clahe(self):
        """apply_wrinkle_enhance=False 时 _enhance_wrinkle_label 不被调(默认 False 也同样)。"""
        engine = self._make_engine()
        with patch.object(engine, '_ensure_model'), \
             patch.object(engine, '_resize_if_needed', return_value=b'data'), \
             patch.object(engine, '_enhance_wrinkle_label') as mock_enhance:
            engine._ocr.ocr.return_value = [[[[0, 0], ['x', 0.99]]]]
            engine.extract_text(b'fake', apply_wrinkle_enhance=False)
        mock_enhance.assert_not_called()


if __name__ == '__main__':
    unittest.main()
