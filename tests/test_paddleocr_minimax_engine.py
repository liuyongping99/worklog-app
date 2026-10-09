"""PaddleOCRMiniMaxEngine + MiniMaxEngine.recognize_text 测试(2026-10-04)。

出货页「智能添加 → AI 图片识别」新增「🤖 PaddleOCR + MiniMax(推荐)」选项:
  - 主路径: PaddleOCR 抽文字 → MiniMax M3 结构化
  - 降级路径: MiniMax 异常 → DeepSeek 兜底
  - 都失败 → 返回 MiniMax 错误 + ocr_text 字段

覆盖:
  1. 工厂 / _VALID_ENGINES 注册
  2. 主路径成功(MiniMax 结构化)
  3. MiniMax key 未配置 → 降级 DeepSeek
  4. MiniMax 连接错误 → 降级 DeepSeek
  5. MiniMax 截断 → 降级 DeepSeek
  6. MiniMax 返回 success=False → 降级 DeepSeek
  7. 两边都失败 → 返回 MiniMax 错误 + ocr_text 字段
  8. ocr_text 字段始终存在
  9. MiniMaxEngine.recognize_text 文本路径(单测,不走图片)
"""
import unittest
import unittest.mock as mock
from unittest.mock import MagicMock


def _fake_openai_response(content='', finish_reason='stop'):
    """构造 mock 的 OpenAI chat.completions.create 返回值。"""
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].finish_reason = finish_reason
    resp.choices[0].message.content = content
    return resp


class EngineFactoryTests(unittest.TestCase):
    """get_ocr_engine 工厂支持 paddleocr_minimax + _VALID_ENGINES 注册。"""

    def test_paddleocr_minimax_in_valid_engines(self):
        """'paddleocr_minimax' 必须在 _VALID_ENGINES 中。"""
        from blueprints.ocr_engine import _VALID_ENGINES
        self.assertIn('paddleocr_minimax', _VALID_ENGINES,
            'paddleocr_minimax 必须在 _VALID_ENGINES 合法引擎列表中')

    def test_factory_returns_paddleocr_minimax_engine(self):
        """get_ocr_engine('paddleocr_minimax') 必须返回 PaddleOCRMiniMaxEngine 实例。"""
        from blueprints.ocr_engine import (
            PaddleOCRMiniMaxEngine, get_ocr_engine,
        )
        engine = get_ocr_engine('paddleocr_minimax')
        self.assertIsInstance(engine, PaddleOCRMiniMaxEngine)

    def test_factory_rejects_unknown_engine_lists_all_valid(self):
        """未知引擎名 raise ValueError,错误消息列出全部合法引擎(包括新加的)。"""
        with self.assertRaises(ValueError) as ctx:
            from blueprints.ocr_engine import get_ocr_engine
            get_ocr_engine('nonexistent-engine')
        msg = str(ctx.exception)
        self.assertIn('paddleocr_minimax', msg,
            '错误信息应列出 paddleocr_minimax 在合法列表中')
        self.assertIn('minimax', msg)

    def test_active_models_includes_paddleocr_minimax(self):
        """_ACTIVE_MODELS 防回归参考表也要含新引擎。"""
        from blueprints.ocr_engine import _ACTIVE_MODELS
        self.assertIn('paddleocr_minimax', _ACTIVE_MODELS)
        # value 应该提到 MiniMax(主路径模型)
        self.assertIn('MiniMax', _ACTIVE_MODELS['paddleocr_minimax'])


class PaddleOCRMiniMaxMainPathTests(unittest.TestCase):
    """主路径成功 — PaddleOCR 抽文字 + MiniMax 结构化。"""

    def setUp(self):
        from blueprints.ocr_engine import MiniMaxEngine
        self._minimax_key = mock.patch.object(MiniMaxEngine, 'API_KEY', 'sk-test-fake')
        self._minimax_key.start()

    def tearDown(self):
        self._minimax_key.stop()

    def test_main_path_returns_minimax_result(self):
        """核心:MiniMax 成功时,recognize 返回 MiniMax items + ocr_text。"""
        from blueprints.ocr_engine import DeepSeekEngine, MiniMaxEngine
        ocr_text = 'OCR第1行：杂胶 1.0黑 50y\nOCR第2行：环保三文治 1.2白 30支\n'

        with mock.patch.object(DeepSeekEngine, '_ocr_image', return_value=ocr_text), \
             mock.patch('openai.OpenAI') as MockOpenAI:
            mock_client = MagicMock()
            MockOpenAI.return_value = mock_client
            mock_client.chat.completions.create.return_value = _fake_openai_response(
                content='{"doc_number":"SO-1","customer_name":"川盛","items":['
                        '{"product_name":"杂胶","specification":"1.0黑","quantity":"50","unit":"y","remark":""},'
                        '{"product_name":"环保三文治","specification":"1.2白","quantity":"30","unit":"支","remark":""}'
                        ']}',
                finish_reason='stop')

            from blueprints.ocr_engine import get_ocr_engine
            engine = get_ocr_engine('paddleocr_minimax')
            result = engine.recognize(b'fake-bytes', 'test.png')

        # 成功路径契约
        self.assertTrue(result['success'])
        self.assertEqual(len(result['items']), 2)
        self.assertEqual(result['items'][0]['product_name'], '杂胶')
        self.assertEqual(result['doc_number'], 'SO-1')
        self.assertEqual(result['customer_name'], '川盛')
        # ocr_text 必须返回(前端两栏对照弹框依赖)
        self.assertEqual(result['ocr_text'], ocr_text)
        # 不应该打 fallback 标记
        self.assertNotIn('fallback_minimax_failed', result)

    def test_main_path_uses_text_prompt_not_image(self):
        """核心:MiniMax.recognize_text 路径不发 image_url,只发纯文本 prompt。

        这与 MiniMaxEngine.recognize(多模态)的关键区别。
        """
        from blueprints.ocr_engine import DeepSeekEngine, MiniMaxEngine, get_ocr_engine
        ocr_text = 'OCR第1行：杂胶 1.0黑 50y'

        with mock.patch.object(DeepSeekEngine, '_ocr_image', return_value=ocr_text), \
             mock.patch('openai.OpenAI') as MockOpenAI:
            mock_client = MagicMock()
            MockOpenAI.return_value = mock_client
            mock_client.chat.completions.create.return_value = _fake_openai_response(
                content='{"items":[]}', finish_reason='stop')

            engine = get_ocr_engine('paddleocr_minimax')
            engine.recognize(b'fake', 'test.png')

            call_kwargs = mock_client.chat.completions.create.call_args.kwargs
            # model 必须 MiniMax-M3
            self.assertEqual(call_kwargs['model'], 'MiniMax-M3')
            # messages 必须是纯字符串(不是 multimodal content list)
            messages = call_kwargs['messages']
            content = messages[0]['content']
            self.assertIsInstance(content, str,
                '文本路径必须用 string content,不是 multimodal list')
            self.assertIn(ocr_text, content,
                '纯文本 prompt 应包含 OCR 抽取的文字')

    def test_empty_ocr_returns_empty_items(self):
        """PaddleOCR 抽不到文字时,直接返回 success=True + 空 items(不调 LLM)。"""
        from blueprints.ocr_engine import DeepSeekEngine, get_ocr_engine
        with mock.patch.object(DeepSeekEngine, '_ocr_image', return_value=''), \
             mock.patch('openai.OpenAI') as MockOpenAI:
            mock_client = MagicMock()
            MockOpenAI.return_value = mock_client

            engine = get_ocr_engine('paddleocr_minimax')
            result = engine.recognize(b'fake', 'test.png')

            # 不应该调 LLM
            mock_client.chat.completions.create.assert_not_called()
        self.assertTrue(result['success'])
        self.assertEqual(result['items'], [])
        self.assertEqual(result['ocr_text'], '')


class PaddleOCRMiniMaxFallbackTests(unittest.TestCase):
    """降级路径 — MiniMax 失败 → DeepSeek 兜底。"""

    def setUp(self):
        from blueprints.ocr_engine import DeepSeekEngine, MiniMaxEngine
        # DeepSeek key 给,用作 fallback 调用(也用于 _ocr_image 抽取)
        self._ds_key = mock.patch.object(DeepSeekEngine, 'API_KEY', 'sk-test-fake')
        self._ds_key.start()
        # MiniMax key 默认空,各 test 自管
        self._orig_minimax = self._patch_minimax_key('')

    def tearDown(self):
        self._ds_key.stop()
        self._restore_minimax_key()

    def _patch_minimax_key(self, value):
        from blueprints.ocr_engine import MiniMaxEngine
        self._minimax_key_patch = mock.patch.object(MiniMaxEngine, 'API_KEY', value)
        self._minimax_key_patch.start()
        return value

    def _restore_minimax_key(self):
        self._minimax_key_patch.stop()

    def test_minimax_no_key_falls_back_to_deepseek(self):
        """核心:MiniMax key 未配置 → 降级 DeepSeek(DeepSeek 用 _ocr_image mock 出来的文本)。

        MiniMax recognize_text 内部对空 key 直接返 success=False → 上层触发降级。
        本测试用 mock OpenAI 让 DeepSeek 走通,验证降级链路完整 + fallback 标记正确。
        """
        from blueprints.ocr_engine import DeepSeekEngine, get_ocr_engine
        ocr_text = 'OCR第1行：杂胶 1.0黑 50y'

        with mock.patch.object(DeepSeekEngine, '_ocr_image', return_value=ocr_text), \
             mock.patch('openai.OpenAI') as MockOpenAI:
            mock_client = MagicMock()
            MockOpenAI.return_value = mock_client
            # DeepSeek 这次返回成功(MiniMax 因 key 空,根本不会调到 OpenAI)
            mock_client.chat.completions.create.return_value = _fake_openai_response(
                content='{"items":[{"product_name":"杂胶","specification":"1.0黑","quantity":"50","unit":"y","remark":""}]}',
                finish_reason='stop')

            engine = get_ocr_engine('paddleocr_minimax')
            # MiniMax key 仍为空(MiniMax.recognize_text 会返 success=False)
            result = engine.recognize(b'fake', 'test.png')

        # 降级路径:fallback_minimax_failed 必须为 True
        self.assertTrue(result['fallback_minimax_failed'],
            'MiniMax key 空时必须降级 DeepSeek,带 fallback 标记')
        self.assertEqual(result['original_engine'], 'paddleocr_minimax')
        self.assertEqual(result['ocr_text'], ocr_text)
        self.assertTrue(result['success'], 'DeepSeek 走通时整体应 success')
        self.assertEqual(result['items'][0]['product_name'], '杂胶')

    def test_minimax_connection_error_falls_back_to_deepseek(self):
        """核心:MiniMax 抛 openai.APIConnectionError → 降级 DeepSeek。

        由于 DeepSeek key 已配(setUp),DeepSeek.recognize_text 走通,返回成功。
        """
        from blueprints.ocr_engine import DeepSeekEngine, get_ocr_engine
        # MiniMax key 配
        self._restore_minimax_key()
        self._patch_minimax_key('sk-test-fake')
        ocr_text = 'OCR第1行：杂胶 1.0黑 50y'

        with mock.patch.object(DeepSeekEngine, '_ocr_image', return_value=ocr_text), \
             mock.patch('openai.OpenAI') as MockOpenAI:
            mock_client = MagicMock()
            MockOpenAI.return_value = mock_client
            # 第一次(MiniMax)抛连接错误,第二次(DeepSeek)返回成功
            import openai
            responses = [
                openai.APIConnectionError(request=MagicMock()),  # MiniMax 网络断
                _fake_openai_response(  # DeepSeek 成功
                    content='{"doc_number":"","customer_name":"","items":['
                            '{"product_name":"杂胶","specification":"1.0黑","quantity":"50","unit":"y","remark":""}'
                            ']}',
                    finish_reason='stop'),
            ]
            mock_client.chat.completions.create.side_effect = responses

            engine = get_ocr_engine('paddleocr_minimax')
            result = engine.recognize(b'fake', 'test.png')

        # 降级成功
        self.assertTrue(result['success'])
        self.assertTrue(result['fallback_minimax_failed'])
        self.assertEqual(result['original_engine'], 'paddleocr_minimax')
        self.assertEqual(result['ocr_text'], ocr_text)
        # items 来自 DeepSeek(不是 MiniMax)
        self.assertEqual(len(result['items']), 1)
        self.assertEqual(result['items'][0]['product_name'], '杂胶')

    def test_minimax_truncation_falls_back_to_deepseek(self):
        """核心:MiniMax 响应被截断(finish_reason=length)→ 降级 DeepSeek。"""
        from blueprints.ocr_engine import DeepSeekEngine, get_ocr_engine
        self._restore_minimax_key()
        self._patch_minimax_key('sk-test-fake')
        ocr_text = 'OCR第1行：杂胶'

        with mock.patch.object(DeepSeekEngine, '_ocr_image', return_value=ocr_text), \
             mock.patch('openai.OpenAI') as MockOpenAI:
            mock_client = MagicMock()
            MockOpenAI.return_value = mock_client
            responses = [
                _fake_openai_response(content='', finish_reason='length'),  # MiniMax 截断
                _fake_openai_response(content='{"items":[{"product_name":"A"}]}',
                                       finish_reason='stop'),  # DeepSeek 成功
            ]
            mock_client.chat.completions.create.side_effect = responses

            engine = get_ocr_engine('paddleocr_minimax')
            result = engine.recognize(b'fake', 'test.png')

        self.assertTrue(result['success'])
        self.assertTrue(result['fallback_minimax_failed'])
        self.assertEqual(result['original_engine'], 'paddleocr_minimax')

    def test_minimax_returns_failure_falls_back_to_deepseek(self):
        """核心:MiniMax 返回 success=False(非截断,如 JSON 解析失败)→ 降级 DeepSeek。"""
        from blueprints.ocr_engine import DeepSeekEngine, get_ocr_engine
        self._restore_minimax_key()
        self._patch_minimax_key('sk-test-fake')
        ocr_text = 'OCR第1行：杂胶'

        with mock.patch.object(DeepSeekEngine, '_ocr_image', return_value=ocr_text), \
             mock.patch('openai.OpenAI') as MockOpenAI:
            mock_client = MagicMock()
            MockOpenAI.return_value = mock_client
            responses = [
                _fake_openai_response(content='invalid json{{{', finish_reason='stop'),  # JSON 解析失败
                _fake_openai_response(content='{"items":[{"product_name":"X"}]}',
                                       finish_reason='stop'),  # DeepSeek 成功
            ]
            mock_client.chat.completions.create.side_effect = responses

            engine = get_ocr_engine('paddleocr_minimax')
            result = engine.recognize(b'fake', 'test.png')

        self.assertTrue(result['success'])
        self.assertTrue(result['fallback_minimax_failed'])

    def test_both_fail_returns_minimax_error_with_ocr_text(self):
        """核心:MiniMax 与 DeepSeek 都失败 → 返回 MiniMax 错误 + ocr_text 字段。

        主路径优先(MiniMax 错误更相关),但 ocr_text 字段必须保留(前端能展示原文)。
        """
        from blueprints.ocr_engine import DeepSeekEngine, get_ocr_engine
        self._restore_minimax_key()
        self._patch_minimax_key('sk-test-fake')
        ocr_text = 'OCR第1行：杂胶'

        with mock.patch.object(DeepSeekEngine, '_ocr_image', return_value=ocr_text), \
             mock.patch('openai.OpenAI') as MockOpenAI:
            mock_client = MagicMock()
            MockOpenAI.return_value = mock_client
            import openai
            # MiniMax 抛错 + DeepSeek 也抛错
            mock_client.chat.completions.create.side_effect = [
                openai.APIConnectionError(request=MagicMock()),
                openai.APIConnectionError(request=MagicMock()),
            ]

            engine = get_ocr_engine('paddleocr_minimax')
            result = engine.recognize(b'fake', 'test.png')

        # 失败路径,但 ocr_text 字段必须保留
        self.assertFalse(result['success'])
        self.assertIn('MiniMax', result['error'],
            '主路径优先 — 失败时 error 应来自 MiniMax')
        self.assertEqual(result['ocr_text'], ocr_text,
            '失败路径必须保留 ocr_text,供前端两栏对照展示')

    def test_paddleocr_failure_skips_llm(self):
        """PaddleOCR 抽文字失败时,直接返 success=False,不调任何 LLM。"""
        from blueprints.ocr_engine import DeepSeekEngine, get_ocr_engine
        with mock.patch.object(DeepSeekEngine, '_ocr_image',
                               side_effect=RuntimeError('paddleocr boom')), \
             mock.patch('openai.OpenAI') as MockOpenAI:
            mock_client = MagicMock()
            MockOpenAI.return_value = mock_client

            engine = get_ocr_engine('paddleocr_minimax')
            result = engine.recognize(b'fake', 'test.png')

            mock_client.chat.completions.create.assert_not_called()
        self.assertFalse(result['success'])
        self.assertIn('PaddleOCR', result['error'])
        self.assertEqual(result['ocr_text'], '')


class MiniMaxEngineTextPathTests(unittest.TestCase):
    """MiniMaxEngine.recognize_text 文本路径(独立单测)。

    验证 PaddleOCRMiniMaxEngine 调用的 MiniMax 文本路径返回结构正确,
    与 DeepSeekEngine.recognize_text 同构。
    """

    def setUp(self):
        from blueprints.ocr_engine import MiniMaxEngine
        self._key = mock.patch.object(MiniMaxEngine, 'API_KEY', 'sk-test-fake')
        self._key.start()

    def tearDown(self):
        self._key.stop()

    def test_no_api_key_returns_clear_error(self):
        """没配 key 时返明确错误(供 PaddleOCRMiniMaxEngine 上层触发降级)。"""
        from blueprints.ocr_engine import MiniMaxEngine
        with mock.patch.object(MiniMaxEngine, 'API_KEY', ''):
            engine = MiniMaxEngine()
            result = engine.recognize_text('some ocr text')
        self.assertFalse(result['success'])
        self.assertIn('MINIMAX_API_KEY', result['hint'])

    def test_empty_input_returns_clear_error(self):
        """空文本返 success=False,不让 PaddleOCRMiniMaxEngine 拿到无效数据。"""
        from blueprints.ocr_engine import MiniMaxEngine
        engine = MiniMaxEngine()
        result = engine.recognize_text('')
        self.assertFalse(result['success'])
        self.assertIn('为空', result['error'])

    def test_successful_recognize_text_returns_structured_items(self):
        """成功路径返回与 DeepSeekEngine.recognize_text 同构的 dict。"""
        from blueprints.ocr_engine import MiniMaxEngine
        text = 'OCR第1行：杂胶 1.0黑 50y'
        with mock.patch('openai.OpenAI') as MockOpenAI:
            mock_client = MagicMock()
            MockOpenAI.return_value = mock_client
            mock_client.chat.completions.create.return_value = _fake_openai_response(
                content='{"doc_number":"T1","customer_name":"测试客户","items":['
                        '{"product_name":"杂胶","specification":"1.0黑","quantity":"50","unit":"y","remark":""}'
                        ']}',
                finish_reason='stop')

            engine = MiniMaxEngine()
            result = engine.recognize_text(text)

        # 与 DeepSeekEngine.recognize_text 同构的字段
        self.assertTrue(result['success'])
        self.assertEqual(result['items'][0]['product_name'], '杂胶')
        self.assertEqual(result['doc_number'], 'T1')
        self.assertEqual(result['customer_name'], '测试客户')
        self.assertEqual(result['ocr_text'], text,
            '成功路径必须回填 ocr_text,供上层 PaddleOCRMiniMaxEngine 透传前端')

    def test_truncation_returns_clear_error_for_fallback(self):
        """截断返 success=False(让 PaddleOCRMiniMaxEngine 触发降级)。"""
        from blueprints.ocr_engine import MiniMaxEngine
        with mock.patch('openai.OpenAI') as MockOpenAI:
            mock_client = MagicMock()
            MockOpenAI.return_value = mock_client
            mock_client.chat.completions.create.return_value = _fake_openai_response(
                content='', finish_reason='length')

            engine = MiniMaxEngine()
            result = engine.recognize_text('some text')

        self.assertFalse(result['success'])
        self.assertIn('截断', result['error'])
        # hint 应暗示上层会自动降级,降低用户焦虑
        self.assertIn('DeepSeek', result.get('hint', ''))

    def test_authentication_error_returns_clear_message(self):
        """401 错误返明确提示,跟 recognize() 同款。"""
        from blueprints.ocr_engine import MiniMaxEngine
        with mock.patch('openai.OpenAI') as MockOpenAI:
            mock_client = MagicMock()
            MockOpenAI.return_value = mock_client
            import openai
            mock_client.chat.completions.create.side_effect = openai.AuthenticationError(
                'invalid key', response=MagicMock(), body=None)

            engine = MiniMaxEngine()
            result = engine.recognize_text('some text')

        self.assertFalse(result['success'])
        self.assertIn('401', result['error'])
        self.assertIn('MINIMAX_API_KEY', result['hint'])


if __name__ == '__main__':
    unittest.main()
