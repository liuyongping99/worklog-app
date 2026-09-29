"""MiniMaxEngine + DeepSeek 截断 fallback 测试(2026-09-17)。

覆盖:
  1. MiniMaxEngine.recognize 多模态调用(image_url + text)
  2. DeepSeek 截断时自动 fallback 到 MiniMax 重试 1 次
  3. MINIMAX_API_KEY 未配置时 fallback 不启用
  4. MiniMax 也失败时仍返回 DeepSeek 原错误
  5. MiniMaxEngine 注册到引擎工厂
"""
import base64
import os
import unittest
import unittest.mock as mock
from unittest.mock import MagicMock

from blueprints.ocr_engine import (
    DeepSeekEngine,
    DeepSeekResponseTruncated,
    MiniMaxEngine,
    _try_minimax_fallback,
    get_ocr_engine,
)


def _fake_openai_response(content='', finish_reason='stop', prompt_tokens=100, completion_tokens=50):
    """构造 mock 的 OpenAI chat.completions.create 返回值。

    真实 API 返回的对象有 .choices[0].message.content / .finish_reason。
    """
    resp = MagicMock()
    resp.choices = [MagicMock()]
    resp.choices[0].finish_reason = finish_reason
    resp.choices[0].message.content = content
    return resp


class MiniMaxEngineBasicTests(unittest.TestCase):
    """MiniMaxEngine 基础行为 — key 配置 + 多模态调用结构。"""

    def test_api_key_from_env(self):
        """核心:MiniMaxEngine.API_KEY 必须从环境变量 MINIMAX_API_KEY 读(不硬编码)。

        用源码检查而非 reload — reload 会让 class identity 改变、破坏 factory cache,
        进而影响后续测试。源码检查无副作用,且足以验证绑定模式。
        """
        import inspect
        src = inspect.getsource(MiniMaxEngine)
        self.assertIn("os.environ.get('MINIMAX_API_KEY'", src,
            'MiniMaxEngine.API_KEY 必须从 MINIMAX_API_KEY 环境变量读,不能硬编码')
        # 严禁出现 sk-api-UNt... 或其他真实 key 前缀(防止误提交)
        self.assertNotIn('sk-api-UNt', src,
            '禁止把真实 API key 硬编码到源码')

    def test_base_url_and_model(self):
        """核心:base_url 与 model 必须按 docs 固定值(2026-09-17)。"""
        self.assertEqual(MiniMaxEngine.BASE_URL, 'https://api.minimaxi.com/v1')
        self.assertEqual(MiniMaxEngine.MODEL, 'MiniMax-M3')

    def test_no_api_key_returns_clear_error(self):
        """没配 key 时返回明确错误,而不是抛异常炸 caller。"""
        with mock.patch.object(MiniMaxEngine, 'API_KEY', ''):
            engine = MiniMaxEngine()
            result = engine.recognize(b'fake-bytes', 'test.png')
        self.assertFalse(result['success'])
        self.assertIn('MiniMax', result['error'])
        self.assertIn('MINIMAX_API_KEY', result['hint'])

    def test_multimodal_call_uses_image_url_and_text(self):
        """核心:recognize 必须用 multimodal 格式(image_url + text),不走纯文本。

        DeepSeek fallback 场景下,期望 MiniMax 直接看图(比传 OCR 文本更准)。
        """
        with mock.patch.object(MiniMaxEngine, 'API_KEY', 'sk-test-fake'), \
             mock.patch('openai.OpenAI') as MockOpenAI:
            mock_client = MagicMock()
            MockOpenAI.return_value = mock_client
            mock_client.chat.completions.create.return_value = _fake_openai_response(
                content='{"items":[]}', finish_reason='stop')

            engine = MiniMaxEngine()
            engine.recognize(b'fake-image-bytes', 'test.png')

            # 抓 chat.completions.create 的实际调用参数
            call_kwargs = mock_client.chat.completions.create.call_args.kwargs
            # model 必须传 MiniMax-M3
            self.assertEqual(call_kwargs['model'], 'MiniMax-M3')
            # messages 必须包含 image_url(content list 第一项)
            messages = call_kwargs['messages']
            self.assertEqual(len(messages), 1)
            content = messages[0]['content']
            self.assertIsInstance(content, list, 'MiniMax 必须用 multimodal content list 格式')
            self.assertEqual(content[0]['type'], 'image_url')
            self.assertIn('data:image/', content[0]['image_url']['url'])
            # 第二项必须是 text 提示词
            self.assertEqual(content[1]['type'], 'text')
            self.assertIn('OCR', content[1]['text'])  # 含 STRUCT_PROMPT 关键词
            # max_tokens + response_format 必须传
            self.assertEqual(call_kwargs['max_tokens'], MiniMaxEngine.MAX_TOKENS)
            self.assertEqual(call_kwargs['response_format'], {'type': 'json_object'})


class MiniMaxEngineTruncationTests(unittest.TestCase):
    """MiniMaxEngine 自己截断的处理 — 与 DeepSeek 同构但独立判断。"""

    def test_truncation_returns_clear_error(self):
        """MiniMax 截断时返回 success=False + 明确 hint,不抛异常。"""
        with mock.patch.object(MiniMaxEngine, 'API_KEY', 'sk-test-fake'), \
             mock.patch('openai.OpenAI') as MockOpenAI:
            mock_client = MagicMock()
            MockOpenAI.return_value = mock_client
            mock_client.chat.completions.create.return_value = _fake_openai_response(
                content='', finish_reason='length')

            engine = MiniMaxEngine()
            result = engine.recognize(b'fake', 'test.png')

        self.assertFalse(result['success'])
        self.assertIn('截断', result['error'])
        self.assertIn('MiniMax', result['error'])


class DeepSeekTruncationFallbackTests(unittest.TestCase):
    """DeepSeek 截断时自动 fallback 到 MiniMax 重试 1 次 — 2026-09-17 新增。"""

    def setUp(self):
        """确保 DeepSeek key 有,MiniMax key 默认空(各 test 自管)。"""
        from blueprints.ocr_engine import DeepSeekEngine
        self._ds_patch = mock.patch.object(DeepSeekEngine, 'API_KEY', 'sk-test-fake')
        self._ds_patch.start()
        # 备份 + 清空 MiniMax key,各 test 按需放开
        self._orig_key = os.environ.get('MINIMAX_API_KEY')
        os.environ['MINIMAX_API_KEY'] = ''

    def tearDown(self):
        self._ds_patch.stop()
        if self._orig_key is None:
            os.environ.pop('MINIMAX_API_KEY', None)
        else:
            os.environ['MINIMAX_API_KEY'] = self._orig_key

    def test_fallback_skipped_when_minimax_key_empty(self):
        """核心:MiniMax key 未配置时,fallback 直接返回 None(不调 MiniMax)。"""
        # MINIMAX_API_KEY 已在 setUp 置空
        result = _try_minimax_fallback(b'fake', 'test.png')
        self.assertIsNone(result, 'key 未配置时 fallback 必须返回 None')

    def test_fallback_skipped_when_minimax_key_placeholder(self):
        """占位符 key(sk-your-...)也算未配置,防误用模板 key。"""
        os.environ['MINIMAX_API_KEY'] = 'sk-your-replace-me'
        result = _try_minimax_fallback(b'fake', 'test.png')
        self.assertIsNone(result)

    def test_fallback_succeeds_when_minimax_returns_items(self):
        """核心:DeepSeek 截断 → MiniMax 返回 items → 上层拿到 MiniMax 结果 + 标记。

        这是用户场景的核心修复:DeepSeek 截断时不再直接报错,而是用 MiniMax 多模态
        重试。成功路径必须带 fallback_minimax=True 标记便于审计。
        """
        os.environ['MINIMAX_API_KEY'] = 'sk-test-real-key'
        with mock.patch('openai.OpenAI') as MockOpenAI:
            mock_client = MagicMock()
            MockOpenAI.return_value = mock_client
            # MiniMax 这次返回成功(DeepSeek 那次已模拟为截断,但 fallback 不调用 DeepSeek)
            mock_client.chat.completions.create.return_value = _fake_openai_response(
                content='{"doc_number":"MM-2026-09-17","customer_name":"","items":['
                        '{"product_name":"杂胶","specification":"1.0黑","quantity":"50","unit":"y","remark":""}'
                        ']}',
                finish_reason='stop')

            result = _try_minimax_fallback(b'fake-bytes', 'test.png')

        self.assertIsNotNone(result, 'MiniMax 成功时 fallback 必须返回 dict,不能 None')
        self.assertTrue(result['success'])
        self.assertEqual(len(result['items']), 1)
        self.assertEqual(result['items'][0]['product_name'], '杂胶')
        self.assertEqual(result['doc_number'], 'MM-2026-09-17')
        # 审计标记必须打上
        self.assertTrue(result.get('fallback_minimax'))
        self.assertEqual(result.get('original_engine'), 'deepseek')

    def test_fallback_returns_none_when_minimax_also_fails(self):
        """MiniMax 也失败时 fallback 返回 None,让上层继续走 DeepSeek 原报错。

        不能用 MiniMax 的错误覆盖 DeepSeek 的错误(用户场景下 DeepSeek 报错更相关)。
        MiniMax 是静默 fallback,失败时返回 None 让 DeepSeek 自己的提示给用户。
        """
        os.environ['MINIMAX_API_KEY'] = 'sk-test-real-key'
        with mock.patch('openai.OpenAI') as MockOpenAI:
            mock_client = MagicMock()
            MockOpenAI.return_value = mock_client
            # MiniMax 也截断
            mock_client.chat.completions.create.return_value = _fake_openai_response(
                content='', finish_reason='length')

            result = _try_minimax_fallback(b'fake', 'test.png')

        # 【2026-09-17】MiniMax 失败时 fallback 必须返回 None,不让 MiniMax 错误泄漏
        self.assertIsNone(result, 'MiniMax 失败时 fallback 必须返回 None')

    def test_fallback_exception_does_not_propagate(self):
        """fallback 自身抛异常时必须 swallow,不能让主流程(DeepSeek 报错)受影响。"""
        os.environ['MINIMAX_API_KEY'] = 'sk-test-real-key'
        with mock.patch('openai.OpenAI', side_effect=RuntimeError('boom')):
            result = _try_minimax_fallback(b'fake', 'test.png')
        self.assertIsNone(result, 'fallback 异常必须返回 None,不能传播给调用方')

    def test_deepseek_recognize_calls_fallback_on_truncation(self):
        """核心:DeepSeekEngine.recognize 截断分支必须调 fallback,而非直接报错。

        这是端到端验证:DeepSeek 截断 → fallback 跑通 → 用户拿到 MiniMax 结果。
        """
        os.environ['MINIMAX_API_KEY'] = 'sk-test-real-key'
        from blueprints.ocr_engine import DeepSeekEngine
        ocr_text = '1   杂胶 1.0黑 50y\n'  # PaddleOCR mock 返回值

        with mock.patch('openai.OpenAI') as MockOpenAI:
            mock_client = MagicMock()
            MockOpenAI.return_value = mock_client
            # 第一次(DeepSeek)返回截断,第二次(MiniMax)返回成功
            responses = [
                _fake_openai_response(content='', finish_reason='length'),  # DeepSeek 截断
                _fake_openai_response(content='{"items":[{"product_name":"A"}]}',
                                       finish_reason='stop'),  # MiniMax 成功
            ]
            mock_client.chat.completions.create.side_effect = responses

            with mock.patch.object(DeepSeekEngine, '_ocr_image', return_value=ocr_text):
                engine = DeepSeekEngine()
                result = engine.recognize(b'fake', 'test.png')

        # 关键:用户拿到的不是 DeepSeek 错误,而是 MiniMax 结果
        self.assertTrue(result['success'], f'fallback 应成功,实际: {result}')
        self.assertEqual(len(result['items']), 1)
        self.assertTrue(result.get('fallback_minimax'),
            'DeepSeek fallback 路径必须打 fallback_minimax=True 标记')


class EngineFactoryTests(unittest.TestCase):
    """get_ocr_engine 工厂支持 minimax + 校验。"""

    def test_minimax_in_valid_engines(self):
        """'minimax' 必须在 _VALID_ENGINES 中(否则工厂 raise ValueError)。"""
        from blueprints.ocr_engine import _VALID_ENGINES
        self.assertIn('minimax', _VALID_ENGINES)

    def test_factory_returns_minimax_engine(self):
        """get_ocr_engine('minimax') 必须返回 MiniMaxEngine 实例。"""
        engine = get_ocr_engine('minimax')
        self.assertIsInstance(engine, MiniMaxEngine)

    def test_factory_rejects_unknown_engine(self):
        """未知引擎名仍 raise ValueError,带 minimax 在合法列表里。"""
        with self.assertRaises(ValueError) as ctx:
            get_ocr_engine('nonexistent-engine')
        self.assertIn('minimax', str(ctx.exception),
            '错误信息应列出 minimax 在合法列表中')
