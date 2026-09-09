"""DeepSeekEngine.recognize — 长订单图截断保护。

历史 bug: 17 行订单 OCR 文字含特殊字符(引号/嵌套双引号/备注)时,
DeepSeek v4-flash 用尽 max_tokens=4000 做内部 chain-of-thought reasoning,
visible content 返回空字符串, finish_reason='length'。代码 json.loads('') 失败,
返回通用错误「DeepSeek 返回格式无法解析」—— 实际是「响应被截断」,用户无法
分辨「重试可解决」vs「图片太烂」。

修复方向:
1. 检测 finish_reason='length' → 返回特定错误(可重试/建议减少行数)
2. 检测空 raw → 返回特定错误(图片无文字或模型空响应)
3. 增加 MAX_TOKENS 8000 + 加 response_format=json_object 双重保险

测试用 mock 模拟 openai.OpenAI 响应,无需真实 API key。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fake_openai_response(content='', finish_reason='stop', prompt_tokens=100, completion_tokens=50):
    """构造 mock 的 openai ChatCompletion 响应对象。

    真实 API 返回的对象有 .choices[0].message.content / .finish_reason 和
    .usage.prompt_tokens / .completion_tokens。mock.MagicMock 可直接构造这些属性。
    """
    usage = mock.MagicMock()
    usage.prompt_tokens = prompt_tokens
    usage.completion_tokens = completion_tokens
    choice = mock.MagicMock()
    choice.message.content = content
    choice.finish_reason = finish_reason
    response = mock.MagicMock()
    response.choices = [choice]
    response.usage = usage
    return response


class DeepSeekRecognizeTruncationTests(unittest.TestCase):
    """DeepSeek 响应被 max_tokens 截断时的处理。"""

    def setUp(self):
        # DeepSeekEngine.API_KEY 在 import 时已被绑死(''),patch class 属性绕过
        from blueprints.ocr_engine import DeepSeekEngine
        self._api_key_patch = mock.patch.object(DeepSeekEngine, 'API_KEY', 'sk-test-fake-key')
        self._api_key_patch.start()

    def tearDown(self):
        self._api_key_patch.stop()

    def _call_recognize(self, mock_openai_class, content='', finish_reason='stop'):
        """调 DeepSeekEngine.recognize, mock openai.OpenAI。"""
        from blueprints.ocr_engine import DeepSeekEngine
        # OCR 文本占位(实际是否跑 PaddleOCR 由 mock 控制)
        ocr_text = (
            '1   环保杂胶 0.8黑中加面 50y\n'
            '2   PE板 1m 10y\n'
            '... 共 17 行\n'
        )

        # Mock PaddleOCREngine._ocr_image 返回固定文本
        with mock.patch.object(DeepSeekEngine, '_ocr_image', return_value=ocr_text):
            # Mock openai.OpenAI class
            fake_client = mock.MagicMock()
            fake_client.chat.completions.create.return_value = _fake_openai_response(
                content=content,
                finish_reason=finish_reason,
            )
            mock_openai_class.return_value = fake_client

            engine = DeepSeekEngine()
            return engine.recognize(b'fake-image-bytes', 'test.png')

    def test_truncation_returns_specific_error(self):
        """finish_reason='length' 且 content 为空时,必须返回「截断」特定错误,而非「格式无法解析」。"""
        with mock.patch('openai.OpenAI') as mock_openai_class:
            # 模拟真实 bug 场景:DeepSeek 用尽 max_tokens,content 为空
            result = self._call_recognize(mock_openai_class, content='', finish_reason='length')

        self.assertFalse(result['success'])
        # 必须告诉用户是截断(可重试/减少行数),而不是含糊的「格式无法解析」
        self.assertIn('截断', result['error'],
            f'应明确告知用户响应被截断,而不是「格式无法解析」,实际错误: {result["error"]}')
        self.assertNotIn('格式无法解析', result['error'],
            '截断场景不应说「格式无法解析」(误导用户重试图片)')
        # 必须给可操作 hint
        self.assertIn('hint', result)
        self.assertTrue(len(result['hint']) > 10, 'hint 应给出可操作建议')

    def test_truncation_with_partial_json_also_returns_truncation_error(self):
        """finish_reason='length' 且 content 是半截 JSON 时,也归类为截断错误。"""
        with mock.patch('openai.OpenAI') as mock_openai_class:
            partial = '{"doc_number":"DS","items":[{"product_name":"杂胶",'
            result = self._call_recognize(mock_openai_class, content=partial, finish_reason='length')

        self.assertFalse(result['success'])
        self.assertIn('截断', result['error'])

    def test_empty_content_with_stop_finish_returns_no_text_error(self):
        """finish_reason='stop' 但 content 为空,应返回「无文字」而不是「格式无法解析」。"""
        with mock.patch('openai.OpenAI') as mock_openai_class:
            result = self._call_recognize(mock_openai_class, content='', finish_reason='stop')

        self.assertFalse(result['success'])
        # 空内容应明确说"无文字"而不是含糊的格式错误
        self.assertNotIn('格式无法解析', result['error'],
            f'空内容应明确说「无文字」,而不是「格式无法解析」,实际: {result["error"]}')

    def test_valid_17_row_json_parses_correctly(self):
        """正常 17 行 JSON 响应仍能正确解析(回归测试,修复不能破坏 happy path)。"""
        valid_json = (
            '{"doc_number":"DS-2026-0804","customer_name":"XX公司","items":['
            + ','.join(
                f'{{"product_name":"商品{i}","specification":"规{i}",'
                f'"quantity":"{10+i}","unit":"y","remark":""}}'
                for i in range(1, 18)
            )
            + ']}'
        )
        with mock.patch('openai.OpenAI') as mock_openai_class:
            result = self._call_recognize(mock_openai_class, content=valid_json, finish_reason='stop')

        self.assertTrue(result['success'], f'正常响应应成功,实际: {result}')
        self.assertEqual(len(result['items']), 17)
        self.assertEqual(result['doc_number'], 'DS-2026-0804')
        self.assertEqual(result['customer_name'], 'XX公司')

    def test_malformed_json_with_stop_finish_returns_parse_error(self):
        """finish_reason='stop' 但 JSON 语法错误,应返回「格式无法解析」(原行为保留)。"""
        with mock.patch('openai.OpenAI') as mock_openai_class:
            # 内容是合法字符串但 JSON 语法错(缺右括号)
            bad_json = '{"doc_number":"DS","items":[{"product_name":"杂胶"'
            result = self._call_recognize(mock_openai_class, content=bad_json, finish_reason='stop')

        self.assertFalse(result['success'])
        # stop 状态的语法错 → 真的是格式问题(不是截断)
        self.assertIn('格式无法解析', result['error'])


class DeepSeekTableFormattingTests(unittest.TestCase):
    def test_ocr_rows_keep_table_order(self):
        from blueprints.ocr_engine import DeepSeekEngine

        engine = DeepSeekEngine()
        engine._ocr_engine._ensure_model = mock.MagicMock()
        engine._ocr_engine._resize_if_needed = mock.MagicMock(return_value=b'fake')
        engine._ocr_engine._ocr = mock.MagicMock()
        engine._ocr_engine._ocr.ocr.return_value = [[
            [[[0, 0], [10, 0], [10, 10], [0, 10]], ['行号', 0.99]],
            [[[20, 0], [40, 0], [40, 10], [20, 10]], ['商品全名', 0.99]],
            [[[0, 40], [10, 40], [10, 50], [0, 50]], ['1', 0.99]],
            [[[20, 40], [40, 40], [40, 50], [20, 50]], ['黑磅布三文治', 0.99]],
        ]]
        text = engine._ocr_image(b'fake-image-bytes')
        self.assertIn('OCR第1行：行号 商品全名', text)
        self.assertIn('OCR第2行：1 黑磅布三文治', text)




class AdaptiveRowThresholdTests(unittest.TestCase):
    """2026-08-24 订单 TD-2026-08-24-005:出货单带表格线,相邻行 gap 仅 ~23px。
    原固定 y_threshold=30 把多行合并,导致黑磅布三文治、白磅布三文治串行 + 漏识别。
    本组测试覆盖 _adaptive_y_threshold() 的自适应行为 + _group_into_rows 的还原。
"""

    def test_adaptive_threshold_with_real_order_image_ys(self):
        """订单 TD-2026-08-24-005 实际 Y 坐标,table 行距 22~25px, self.assertLess(threshold, 18, "<" + str(threshold) + ">")
        """
        from blueprints.ocr_engine import _adaptive_y_threshold
        ys = [32.0, 37.2, 64.5, 64.5, 64.5, 64.5, 66.0, 84.5, 84.5, 84.5,
              85.0, 106.8, 107.0, 107.0, 107.0, 107.0, 130.5, 131.0, 131.0,
              131.5, 132.0, 132.0, 154.5, 154.5, 155.0, 155.0, 156.0,
              179.5, 179.5, 179.5, 180.5, 203.0, 203.0, 204.0, 204.0, 204.0,
              226.2, 226.5, 227.5, 228.0, 228.0, 251.8, 277.0, 277.0, 277.5, 301.0]
        threshold = _adaptive_y_threshold(ys)
        self.assertLess(threshold, 18)
        self.assertGreater(threshold, 5)

    def test_group_into_rows_separates_5_25_45(self):
        """相邻行 y=5 / 25 / 45 (gap=20): 自适应阈值应切分为 3 行。
        关键是自适应阈值 (None) 在此场景下能切分。
        """
        from blueprints.ocr_engine import _group_into_rows
        lines = [("A", 5.0, 0), ("B", 25.0, 0), ("C", 45.0, 0)]
        rows = _group_into_rows(lines)
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows, ["A", "B", "C"])

    def test_group_into_rows_separates_dense_table_with_variance(self):
        """模拟订单表格行:每行内部 Y 有 ~2px 抖动,行间 gap ~22px。
        验证自适应阈值能切出 3 行,不会因为 rolling Y 误差合并。
        """
        from blueprints.ocr_engine import _group_into_rows
        lines = [
            ("品名", 5.0, 50), ("单位", 5.5, 200), ("数量", 6.0, 400),
            ("黑磅布三文治", 25.0, 50), ("码", 25.5, 200), ("2555", 26.0, 400),
            ("白磅布三文治", 45.0, 50), ("码", 45.5, 200), ("1825", 46.0, 400),
        ]
        rows = _group_into_rows(lines)
        self.assertEqual(len(rows), 3)

    def test_group_into_rows_handles_real_order_data(self):
        """真实订单数据(完整):5 个商品 + 表头 + 合计 + 签字行。
        应切出 12 行,5 个商品独立成行,白磅布三文治 字面保留(无误识为磷)。
        """
        from blueprints.ocr_engine import _group_into_rows
        lines = [
            ("第1/1页", 32.0, 746), ("同价调拨单", 37.2, 358),
            ("发货仓库:", 64.5, 42), ("芙蓉仓", 64.5, 142),
            ("录单日期:", 64.5, 307), ("单据编号:TD-2026-08-24-005", 64.5, 601),
            ("2026-08-24 15:25:06", 66.0, 393),
            ("收货仓库:", 84.5, 43), ("田心仓", 84.5, 142),
            ("摘要:", 84.5, 307), ("阿海68FG3", 85.0, 395),
            ("商品全名", 106.8, 175), ("行号", 107.0, 64),
            ("规格", 107.0, 404), ("单位", 107.0, 513),
            ("数量", 107.0, 588), ("备注", 107.0, 737),
            ("70支", 130.5, 652), ("0.8中性", 131.0, 392),
            ("码", 131.0, 520), ("黑磅布三文治", 131.5, 186),
            ("1", 132.0, 73), ("2555", 132.0, 587),
            ("1.0中性", 154.5, 391), ("50支", 154.5, 652),
            ("白磅布三文治", 155.0, 187), ("1825", 155.0, 588),
            ("2", 156.0, 74),
            ("皮革木板", 179.5, 202), ("块", 179.5, 520),
            ("2", 179.5, 599), ("3", 180.5, 74),
            ("A级黑", 203.0, 210), ("支", 203.0, 520),
            ("4", 204.0, 75), ("1.4*3.0*46y", 204.0, 376),
            ("4", 204.0, 599),
            ("1.4*1.5*92y", 226.2, 366), ("支", 226.5, 521),
            ("白色回力胶", 227.5, 194), ("5", 228.0, 74),
            ("1", 228.0, 598),
            ("4387", 251.8, 586),
            ("制单人:柳永平", 277.0, 42), ("收货人:", 277.0, 649),
            ("发货人:", 277.5, 221), ("提货人:", 301.0, 449),
        ]
        rows = _group_into_rows(lines)
        product_rows = [r for r in rows if "磅布三文治" in r or "皮革木板" in r or "A级黑" in r or "白色回力胶" in r]
        self.assertEqual(len(product_rows), 5)
        joined = " ".join(rows)
        self.assertIn("白磅布三文治", joined)
        self.assertNotIn("白磷布", joined)

    def test_group_into_rows_explicit_threshold_still_works(self):
        """显式 y_threshold=30 仍可用 (兼容历史测试)。
        极端:把行间 gap 调到 40,确保 30 仍能切。
        """
        from blueprints.ocr_engine import _group_into_rows
        lines = [("A", 0.0, 0), ("B", 40.0, 0), ("C", 80.0, 0)]
        rows = _group_into_rows(lines, y_threshold=30)
        self.assertEqual(len(rows), 3)

    def test_adaptive_threshold_handles_uniform_gaps(self):
        """gap 均匀分布(无明显跳跃)兜底路径。
        """
        from blueprints.ocr_engine import _adaptive_y_threshold
        ys = [0.0, 10.0, 20.0, 30.0, 40.0, 50.0]
        t = _adaptive_y_threshold(ys)
        self.assertGreater(t, 0)
        self.assertLessEqual(t, 35)

    def test_adaptive_threshold_empty_input(self):
        """空数据应返回合理默认值,不抛异常。
        """
        from blueprints.ocr_engine import _adaptive_y_threshold
        self.assertEqual(_adaptive_y_threshold([]), 18.0)
        self.assertEqual(_adaptive_y_threshold([5.0]), 18.0)

if __name__ == '__main__':
    unittest.main()
