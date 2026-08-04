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


if __name__ == '__main__':
    unittest.main()