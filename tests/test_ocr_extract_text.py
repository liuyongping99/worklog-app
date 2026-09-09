"""PaddleOCREngine.extract_text() 单测：mock 掉 paddle 模型，只验证拼接逻辑。"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from blueprints.ocr_engine import PaddleOCREngine, _filter_summary_items


class ExtractTextTests(unittest.TestCase):
    def test_joins_lines_from_ocr_result(self):
        eng = PaddleOCREngine()
        # 伪造 PaddleOCR 3.x 结果结构：[[ [bbox,[text,conf]], ... ]]
        fake_result = [[
            [[[0, 0], [1, 0], [1, 1], [0, 1]], ['硬加面', 0.99]],
            [[[0, 2], [1, 2], [1, 3], [0, 3]], ['黑色 1.5m', 0.95]],
        ]]
        eng._ensure_model = mock.MagicMock()
        eng._resize_if_needed = mock.MagicMock(return_value=b'x')
        eng._ocr = mock.MagicMock()
        eng._ocr.ocr = mock.MagicMock(return_value=fake_result)

        text = eng.extract_text(b'fake-bytes')
        self.assertIn('硬加面', text)
        self.assertIn('黑色 1.5m', text)

    def test_empty_result_returns_empty(self):
        eng = PaddleOCREngine()
        eng._ensure_model = mock.MagicMock()
        eng._resize_if_needed = mock.MagicMock(return_value=b'x')
        eng._ocr = mock.MagicMock()
        eng._ocr.ocr = mock.MagicMock(return_value=[[]])
        self.assertEqual(eng.extract_text(b'x'), '')

    def test_ocr_exception_returns_empty(self):
        """PaddleOCR 抛异常时应被吞掉、记日志并返回空串，不向上传播。"""
        eng = PaddleOCREngine()
        eng._ensure_model = mock.MagicMock()
        eng._resize_if_needed = mock.MagicMock(return_value=b'x')
        eng._ocr = mock.MagicMock()
        eng._ocr.ocr = mock.MagicMock(side_effect=RuntimeError('paddle boom'))
        self.assertEqual(eng.extract_text(b'x'), '')

    def test_form_candidates_fill_missing_band_result_from_full_ocr(self):
        """整图 OCR 能补回被切带边界截断的厚度字段。"""
        eng = PaddleOCREngine()
        lines, confs = eng._merge_form_candidates(
            ['品名：磅布三文治', '', '手感：中性', '底布：磅布'],
            [0.99, 1.0, 0.99, 0.99],
            ['品名：磅布三文治', '厚度：0.8mm', '手感：中性', '底布：磅布'],
            [0.99, 0.99, 0.99, 0.99],
        )
        self.assertEqual(lines, [
            '品名：磅布三文治', '厚度：0.8mm', '手感：中性', '底布：磅布'])
        self.assertEqual(len(confs), 4)


class RedStampSuppressionTests(unittest.TestCase):
    """2026-08-19 改造:验证 _suppress_red_stamp 不再擦掉红章下叠加的黑色文字。

    关键不变量:对于「红章压黑字」的合成图(红墨+黑字→暗红/棕色),
    函数应只擦除高 V 的纯红像素(真正印章),保留低 V 的暗像素(黑字)。
    """

    def test_preserves_dark_pixels_under_red_stamp(self):
        """暗红/棕色(红章下黑字)不应被擦成白底,只擦亮红。"""
        import numpy as np
        from blueprints.ocr_engine import _suppress_red_stamp

        # 构造一张 10x10 图: 中心 4x4 是「暗红」(模拟红章盖在黑字上)
        rgb = np.zeros((10, 10, 3), dtype=np.uint8)
        rgb[:] = (255, 255, 255)                       # 白底
        rgb[3:7, 3:7] = (95, 75, 75)                   # 暗红=R95 G75 B75 (V=95, ≤100, 模拟红章压黑字)
        out = _suppress_red_stamp(rgb)

        # 暗红区不应被擦 (V≈110 满足 V>100,但 S≈36% < 60,不应命中)
        self.assertFalse((out[3:7, 3:7] == 255).all(),
                         "暗红像素被错误擦成白底")
        # 暗红区不应有变化(因为 S<60 + R-G=40 >30 但 S 不足)
        self.assertTrue(np.array_equal(out[3:7, 3:7], rgb[3:7, 3:7]),
                        "暗红像素未被保留")

    def test_erases_bright_pure_red(self):
        """亮红(纯印章)应被擦成白底。"""
        import numpy as np
        from blueprints.ocr_engine import _suppress_red_stamp

        rgb = np.zeros((10, 10, 3), dtype=np.uint8)
        rgb[:] = (255, 255, 255)
        rgb[3:7, 3:7] = (200, 60, 60)                  # 亮红, V=200, S=70%, R-G=140
        out = _suppress_red_stamp(rgb)

        self.assertTrue((out[3:7, 3:7] == 255).all(),
                        "亮红未擦除")

    def test_preserves_pure_black_text(self):
        """纯黑文字(R=G=B≈0)完全不被擦。"""
        import numpy as np
        from blueprints.ocr_engine import _suppress_red_stamp

        rgb = np.zeros((10, 10, 3), dtype=np.uint8)
        rgb[:] = (255, 255, 255)
        rgb[3:7, 3:7] = (10, 10, 10)                   # 纯黑
        out = _suppress_red_stamp(rgb)

        self.assertTrue(np.array_equal(out[3:7, 3:7], rgb[3:7, 3:7]),
                        "纯黑文字被错误擦除")

    def test_preserves_white_background(self):
        """白底(R=G=B≈255)完全不被擦。"""
        import numpy as np
        from blueprints.ocr_engine import _suppress_red_stamp

        rgb = np.full((10, 10, 3), 255, dtype=np.uint8)
        rgb[3:7, 3:7] = (200, 60, 60)                  # 只在中间放亮红
        out = _suppress_red_stamp(rgb)

        # 周围白底不变
        self.assertTrue(np.array_equal(out[0:3, :], rgb[0:3, :]))
        self.assertTrue(np.array_equal(out[7:, :], rgb[7:, :]))

    def test_erases_faded_pink_stamp_ink(self):
        """2026-08-21 TD-2026-08-21-002 案例:原阈值漏掉浅粉红墨,1.0 被误识为 10。
        新阈值应能抹掉 R-G≈20 的浅粉红章(红章晕染/淡墨),但仍保留黑字与白底。"""
        import numpy as np
        from blueprints.ocr_engine import _suppress_red_stamp
    
        rgb = np.full((10, 10, 3), 255, dtype=np.uint8)
        # 浅粉红(R-G≈16,S≈60):典型合格章压字时的晕染/淡墨层
        rgb[3:7, 3:7] = (215, 198, 198)              # R=215 G=198 B=198
        out = _suppress_red_stamp(rgb)
    
        self.assertTrue((out[3:7, 3:7] == 255).all(),
                        "浅粉红墨未被擦除")
        # 周围白底不变
        self.assertTrue(np.array_equal(out[0:3, :], rgb[0:3, :]))
        self.assertTrue(np.array_equal(out[7:, :], rgb[7:, :]))
    
    def test_preserves_cream_background(self):
        """米色/奶油背景(H~15-25,S~40,R-G≈5)不应被擦。"""
        import numpy as np
        from blueprints.ocr_engine import _suppress_red_stamp
    
        rgb = np.zeros((10, 10, 3), dtype=np.uint8)
        # 米色背景:R=210 G=200 B=180 (R-G=10, S~50, 接近浅红但缺明显红偏)
        rgb[:] = (210, 200, 180)
        out = _suppress_red_stamp(rgb)
    
        self.assertTrue(np.array_equal(out, rgb),
                        "米色背景被误擦除")



class DeepSeekErrorWrappingTests(unittest.TestCase):
    """2026-08-19 修复:openai SDK 2.x 升级后 APIConnectionError 构造签名变更。

    验证网络异常时包装成 openai.APIConnectionError 不再抛 TypeError,
    确保上层 except 能正确 fallback 到本地匹配。
    """

    def test_api_connection_error_wraps_httpx_error_without_typeerror(self):
        """httpx.HTTPError → openai.APIConnectionError 不应触发 TypeError。

        验证 SDK 升级到 2.x 后包装逻辑正确,网络异常能被上层 except
        捕获并 fallback 到本地匹配(不再触发 TypeError)。
        """
        import httpx
        import openai
        import blueprints.ocr_engine as oe

        # 找 DeepSeekEngine 类(其 _call_api_with_prompt 实例方法)
        DeepSeekEngine = getattr(oe, 'DeepSeekEngine', None)
        self.assertIsNotNone(DeepSeekEngine, 'DeepSeekEngine 类未导出')

        eng = DeepSeekEngine.__new__(DeepSeekEngine)   # 跳过 __init__,不真连 API
        eng.API_KEY = 'sk-test'
        eng.BASE_URL = 'https://example.invalid'
        eng.MODEL = 'fake'
        eng.MAX_TOKENS = 100
        eng.TIMEOUT = 5

        fake_request = httpx.Request('POST', 'https://example.invalid/v1/chat/completions')
        with mock.patch('httpx.Client') as mock_client:
            mock_client.return_value.__enter__.return_value.post.side_effect = (
                httpx.ConnectError('boom', request=fake_request)
            )
            try:
                with self.assertRaises(openai.APIConnectionError) as ctx:
                    eng._call_api_with_prompt('test prompt', multi=False)
                # 关键断言:异常对象构造完整,message 包含原始错误信息
                self.assertIn('boom', str(ctx.exception))
            except TypeError as e:
                self.fail(f'APIConnectionError 包装仍触发 TypeError: {e}')



class FilterSummaryItemsRegressionTests(unittest.TestCase):
    """2026-09-03 复盘:订单 TD-2026-09-03-004 漏录「露华里 2400 码」。

    根因:_filter_summary_items 的「中位数×3 离群值」检测把正常商品行
    误判为汇总行。修复后只信赖品名关键词 / 纯数字名 / 备注清理。
    详见 blueprints/ocr_engine.py:_filter_summary_items docstring。
    """

    def test_keeps_yard_product_with_2400_among_small_items(self):
        """3 行码基商品 [1, 300, 2400],不应把 2400 当成汇总行误删。"""
        items = [
            {"product_name": "环保磅布三文治", "specification": "1.0黑双中性", "quantity": "300", "unit": "y", "remark": "6支"},
            {"product_name": "露华里",          "specification": "足0.6面料黑", "quantity": "2400", "unit": "y", "remark": "80支"},
            {"product_name": "皮革木板",         "specification": "",            "quantity": "1",   "unit": "块", "remark": "2701"},
        ]
        clean, removed = _filter_summary_items(items)
        names = [it["product_name"] for it in clean]
        self.assertEqual(removed, 0, f"不该被过滤: {names}")
        self.assertEqual(len(clean), 3)
        self.assertIn("露华里", names)
        self.assertIn("环保磅布三文治", names)
        self.assertIn("皮革木板", names)

    def test_still_filters_summary_keyword_in_product_name(self):
        """product_name 显式含「合计」仍要过滤(安全网不能退化成空)。"""
        items = [
            {"product_name": "环保磅布三文治", "specification": "", "quantity": "300", "unit": "y", "remark": "6支"},
            {"product_name": "合计",             "specification": "", "quantity": "2701", "unit": "y", "remark": ""},
        ]
        clean, removed = _filter_summary_items(items)
        self.assertEqual(removed, 1)
        self.assertEqual([it["product_name"] for it in clean], ["环保磅布三文治"])

    def test_still_filters_pure_numeric_name(self):
        """product_name 是纯数字(如 LLM 误把合计值当品名)仍要过滤。"""
        items = [
            {"product_name": "环保磅布三文治", "specification": "", "quantity": "300", "unit": "y", "remark": ""},
            {"product_name": "2701",             "specification": "", "quantity": "2701", "unit": "y", "remark": ""},
        ]
        clean, removed = _filter_summary_items(items)
        self.assertEqual(removed, 1)
        self.assertEqual([it["product_name"] for it in clean], ["环保磅布三文治"])

    def test_still_cleans_summary_text_in_remark(self):
        """备注里塞了「合计300支」之类仍要清空,保留商品行。"""
        items = [
            {"product_name": "环保磅布三文治", "specification": "", "quantity": "300", "unit": "y", "remark": "合计300支"},
        ]
        clean, removed = _filter_summary_items(items)
        self.assertEqual(removed, 0)
        self.assertEqual(clean[0]["remark"], "")

    def test_empty_items_returns_empty(self):
        clean, removed = _filter_summary_items([])
        self.assertEqual(clean, [])
        self.assertEqual(removed, 0)


if __name__ == '__main__':
    unittest.main()
