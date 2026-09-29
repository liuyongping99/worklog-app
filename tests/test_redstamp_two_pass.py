"""PaddleOCREngine `redstamp` 预处理两遍 OCR 取优单测。

背景(2026-08-28 订单 TD-2026-08-28-001):
纯胶 / 0.6白软纯胶 行级图(image 3621)实际标注「软」字,但走 redstamp 预处理后
被 _suppress_red_stamp 误擦成白色 → OCR 漏字。

15 张同类样本验证:60% 在 redstamp 擦除后反而比原图 OCR 漏字更多。
原图 OCR 通常保留更多字段(只是红章盖住的字会被错读),擦除后偶尔能修对
红章盖住的字(2841 案例)但代价是普遍漏字。

修复方向:extract_text / extract_text_with_conf 在 KIND_REDSTAMP 分支里
OCR 两次(原图 + 擦除图),按字段数 / 字符总数 / 已知字段前缀数综合择优。

测试要点:
- mock PaddleOCR 实例,精确控制两次 OCR 的返回
- 验证函数返回的是「字段更多」的一次结果,不是擦除图(漏字)
- 验证擦除图明明字段更多时,优先选擦除图(2841 场景)
"""
import unittest
from unittest.mock import MagicMock, patch

from blueprints.ocr_engine import PaddleOCREngine


def _ocr_return(*texts):
    """构造 PaddleOCR `ocr()` 返回值, 每个 text 是 (text, confidence) 元组。"""
    res = [[[[0, 0], [t, 0.95]] for t in texts]]
    return res


def _patch_pipeline(engine, np_return=None):
    """组合 patch 让 bytes → numpy 解码成功(参考 test_paddleocr_wrinkle_param)。"""
    fake_img = MagicMock(name='PIL_Image')
    fake_img.convert.return_value = fake_img
    np_mock = np_return if np_return is not None else MagicMock(name='np_array')
    return [
        patch('blueprints.ocr_engine.Image.open', return_value=fake_img),
        patch('blueprints.ocr_engine.np.array', return_value=np_mock),
    ]


class RedstampTwoPassTests(unittest.TestCase):
    """redstamp 路径两遍 OCR 取优 — 3 项核心覆盖。"""

    def _make_engine(self):
        engine = PaddleOCREngine.__new__(PaddleOCREngine)
        engine._ocr = MagicMock(name='_ocr')
        engine._wrinkle_ocr = MagicMock(name='_wrinkle_ocr')
        return engine

    def test_redstamp_picks_more_lines_when_stamping_removes_field(self):
        """场景 1:擦除图漏字段(0.6白色软性 → 0.6白色),应保留原图(0.6白色软性)。

        模拟:
        - ocr.ocr(img_np, cls=True) → ['品名：高弹纯胶', '规格：0.6白色软性']  (原图完整)
        - ocr.ocr(擦除图, cls=True) → ['品名：高弹纯胶', '规格：0.6白色']    (  (漏「软性」)

        期望 extract_text_with_conf 返回「0.6白色软性」(原图版本)。
        """
        engine = self._make_engine()
        # 第一次调用 ocr() 返回原图结果,第二次返回擦除图结果
        engine._ocr.ocr.side_effect = [
            _ocr_return('品名：高弹纯胶', '规格：0.6白色软性'),
            _ocr_return('品名：高弹纯胶', '规格：0.6白色'),
        ]
        np_mock = MagicMock(name='np_array_orig')
        np_mock.__eq__ = lambda self, other: True  # 让 np.all(pre==img) 之类比较失败
        # 让 _suppress_red_stamp 返回值 ≠ 输入(否则擦除没效果)
        # _suppress_red_stamp 会真跑,这里让它返回原图(因为 mock 了 cv2)
        # 实际测试里我们要的是「两次 ocr 返回不同结果」

        patches = _patch_pipeline(engine, np_mock)
        with patch.object(engine, '_ensure_model'), \
             patch.object(engine, '_resize_if_needed', return_value=b'data'), \
             patches[0], patches[1]:
            # _suppress_red_stamp 是真实函数,会基于 numpy 返回新图。
            # 它对 magic mock 数组做 cv2 操作会抛异常,被 except 返回原图
            # → 两次 ocr 都会得到 side_effect[0]
            # 这里我们改用 patch _suppress_red_stamp
            pass

        # 重新设计: 直接 patch _suppress_red_stamp 返回不同 numpy mock
        np_orig = MagicMock(name='np_orig')
        np_pre = MagicMock(name='np_pre')
        with patch.object(engine, '_ensure_model'), \
             patch.object(engine, '_resize_if_needed', return_value=b'data'), \
             patch('blueprints.ocr_engine.Image.open') as mock_open, \
             patch('blueprints.ocr_engine.np.array', side_effect=[np_orig, np_pre]), \
             patch('blueprints.ocr_engine._suppress_red_stamp',
                   side_effect=lambda *a, **kw: np_pre):
            # 【2026-09-02】升 3-pass: 原图 + 擦除图 + 锐化图(同构于 extract_text)
            # 锐化版 mock 等同 orig 版本 — 本测试焦点在 2-pass 取优保留原图逻辑。
            engine._ocr.ocr.side_effect = [
                _ocr_return('品名：高弹纯胶', '规格：0.6白色软性'),  # orig
                _ocr_return('品名：高弹纯胶', '规格：0.6白色'),      # pre(擦除)
                _ocr_return('品名：高弹纯胶', '规格：0.6白色软性'),  # sharp(等同 orig)
            ]
            text, conf = engine.extract_text_with_conf(b'fake', preprocess_kind='redstamp')

        self.assertIn('软性', text,
            '原图 OCR 含「软性」,擦除图漏字段,合并应保留原图结果')
        self.assertEqual(text.count('\n') + 1, 2)

    def test_redstamp_picks_pre_when_pre_has_more_lines(self):
        """场景 2:擦除图字段更多(2841 案例,1.2白色从无到有),应选擦除图。

        模拟:
        - ocr.ocr(原图) → ['品名：环保杂胶', '规格：', '白色', '手感', '中性']  (4 行)
        - ocr.ocr(擦除图) → ['品名：环保杂胶', '规格：', '1.2白色', '手感', '中性'] (4 行,但 '1.2白色' 字段更全)

        期望保留 '1.2白色'。
        """
        engine = self._make_engine()
        np_orig = MagicMock(name='np_orig')
        np_pre = MagicMock(name='np_pre')
        with patch.object(engine, '_ensure_model'), \
             patch.object(engine, '_resize_if_needed', return_value=b'data'), \
             patch('blueprints.ocr_engine.Image.open'), \
             patch('blueprints.ocr_engine.np.array', side_effect=[np_orig, np_pre]), \
             patch('blueprints.ocr_engine._suppress_red_stamp',
                   side_effect=lambda *a, **kw: np_pre):
            # 【2026-09-02】升 3-pass: 原图 + 擦除图 + 锐化图
            # 锐化版 mock 等同擦除版本(保留 1.2 字段)—— 本测试焦点是
            # "擦除图多字段时应选擦除"的择优逻辑。
            engine._ocr.ocr.side_effect = [
                _ocr_return('品名：环保杂胶', '规格：', '白色', '手感', '中性'),  # orig(漏 1.2)
                _ocr_return('品名：环保杂胶', '规格：', '1.2白色', '手感', '中性'),  # pre(有 1.2)
                _ocr_return('品名：环保杂胶', '规格：', '1.2白色', '手感', '中性'),  # sharp(等同 pre)
            ]
            text, conf = engine.extract_text_with_conf(b'fake', preprocess_kind='redstamp')

        self.assertIn('1.2白色', text,
            '擦除图 OCR 多出 1.2 厚度字段,合并应保留擦除图结果')

    def test_non_redstamp_path_does_not_call_twice(self):
        """场景 3:非 redstamp 路径不调两次 OCR(避免性能回归)。

        验证 KIND_GLARE / preprocess_kind=None 路径只调一次 _ocr.ocr()。
        KIND_FORM_NOLINES 内部按 4-row + 6-row 多带切分,会调多次 ocr — 不在
        本测试范围(性能约束已在 form_nolines 自带测试覆盖)。
        """
        engine = self._make_engine()
        for kind in (None, 'glare'):
            engine._ocr.ocr.reset_mock()
            engine._ocr.ocr.return_value = _ocr_return('hello')
            np_mock = MagicMock(name='np_array')
            with patch.object(engine, '_ensure_model'), \
                 patch.object(engine, '_resize_if_needed', return_value=b'data'), \
                 patch('blueprints.ocr_engine.Image.open'), \
                 patch('blueprints.ocr_engine.np.array', return_value=np_mock):
                engine.extract_text_with_conf(b'fake', preprocess_kind=kind)
            self.assertEqual(engine._ocr.ocr.call_count, 1,
                f'preprocess_kind={kind!r} 应只调一次 ocr, 实际 {engine._ocr.ocr.call_count} 次')


if __name__ == '__main__':
    unittest.main()