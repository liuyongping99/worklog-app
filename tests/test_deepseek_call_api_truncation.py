"""DeepSeekEngine._call_api_with_prompt 截断 + JSON 解析失败防回归测试(2026-09-17)。

背景:用户 /shipping-records 添加图片订单(走 ai_recognize)时反复看到
"DeepSeek 响应被截断(订单行数过多或备注过长)" 错误。同 7KB 图 30s 成功 /
43s 截断交替(reasoning token 把 8000 budget 吃光)。

根因(2026-09-17 实测 log/202609/ocr-2026-09-17.log:155-158):
  1. _call_api_with_prompt(整单 ai-match / 行级 compare_single_record 都在用)
     不检查 finish_reason='length',JSON 截断时 json.loads 抛 JSONDecodeError
     → 走到 raise openai.APIError(...) — 但 openai.APIError 签名是
     (self, message, request, *, body=None),这里没传 request= → 抛 TypeError
     (实测 traceback: TypeError: APIError.__init__() missing 1 required
     positional argument: 'request'),无法走到 fallback 本地 fuzzy 路径。

修复:
  - 新增 DeepSeekResponseTruncated(RuntimeError) 模块级异常类
  - _call_api_with_prompt 解析前显式检查 finish_reason='length' → 抛
    DeepSeekResponseTruncated,让 ocr_pipeline.classify 走 fallback
  - 把原 2782 的 openai.APIError(...) 替换为 DeepSeekResponseTruncated,
    不再依赖 openai.APIError 的 request= 参数
  - DeepSeekEngine.MAX_TOKENS 8000 → 12000(给 reasoning 留 50% 余量)

防回归覆盖:
  1. MAX_TOKENS 必须 ≥ 12000(防误退回 8000)
  2. finish_reason='length' → 抛 DeepSeekResponseTruncated(不再抛 TypeError)
  3. 解析前显式检查 finish_reason(防误退到 json.loads)
  4. JSON 语法错也归类为 truncated,不再抛 openai.APIError(避免误调用签名)
"""
import inspect
import unittest
import unittest.mock as mock
from unittest.mock import MagicMock

import blueprints.ocr_engine as ocr_engine_mod
from blueprints.ocr_engine import DeepSeekEngine, DeepSeekResponseTruncated


class DeepSeekCallApiTruncationTests(unittest.TestCase):
    """DeepSeekEngine._call_api_with_prompt 截断保护 — 4 项核心覆盖。"""

    def test_max_tokens_at_least_12000(self):
        """核心:MAX_TOKENS 必须 ≥ 24000,防止退回 8000/12000 导致 reasoning 截断。

        【2026-09-17】8000 时同 7KB 图反复 30s 成功 / 43s 截断交替;
        12000 仍偶发;24000 给 reasoning 留 2x 余量(reasoning 与 output 共用
        max_tokens,DeepSeek v4-flash reasoning 长度对同图随机波动)。
        阈值叫 "at_least_12000" 是历史命名,实际值应跟随当前常量,不能 < 24000。
        """
        self.assertGreaterEqual(DeepSeekEngine.MAX_TOKENS, 24000,
            f'DeepSeekEngine.MAX_TOKENS={DeepSeekEngine.MAX_TOKENS},'
            f'reasoning 把 budget 吃光会 finish_reason=length 截断。必须 ≥ 24000')
        # TIMEOUT 必须 ≥ 120(理由同 MAX_TOKENS,否则 24K token reasoning 会撞网络超时)
        self.assertGreaterEqual(DeepSeekEngine.TIMEOUT, 120,
            f'DeepSeekEngine.TIMEOUT={DeepSeekEngine.TIMEOUT},'
            f'24K token reasoning 在慢网络下可能跑 90s+,必须 ≥ 120s')

    def test_truncation_raises_typed_exception_not_typeerror(self):
        """核心:finish_reason='length' 必须抛 DeepSeekResponseTruncated(可被上层
        fallback 捕获),不再抛 TypeError(openai.APIError 缺 request= 参数)。

        旧行为:json.loads 截断 JSON → JSONDecodeError → except → raise
        openai.APIError(f'...') → TypeError(APIError.__init__() missing
        1 required positional argument: 'request')。fallback 路径根本走不到。
        """
        # 反射取源码:确认 DeepSeekResponseTruncated 是显式 raise 的异常
        src = inspect.getsource(DeepSeekEngine._call_api_with_prompt)
        self.assertIn('DeepSeekResponseTruncated', src,
            '_call_api_with_prompt 没引用 DeepSeekResponseTruncated,'
            '截断时无法走 fallback 路径')
        self.assertIn("finish_reason == 'length'", src,
            "_call_api_with_prompt 必须在 json.loads 之前检查 finish_reason='length'")
        # 必须先 finish_reason 检查,再 json.loads
        idx_check = src.index("finish_reason == 'length'")
        idx_load = src.index('json.loads(raw)')
        self.assertLess(idx_check, idx_load,
            'finish_reason 检查必须在 json.loads 之前,否则截断会落到 JSONDecodeError')

        # 跑一遍实际方法:mock httpx 返回 finish_reason='length',半截 JSON
        with mock.patch.object(DeepSeekEngine, 'API_KEY', 'sk-test-fake'), \
             mock.patch('httpx.Client') as MockClient:
            mock_cli = MagicMock()
            MockClient.return_value.__enter__.return_value = mock_cli
            mock_resp = MagicMock()
            mock_resp.raise_for_status.return_value = None
            mock_resp.json.return_value = {
                'choices': [{
                    'finish_reason': 'length',
                    'message': {'content': '{"items":[{"product_name":"x'},  # 半截
                }],
            }
            mock_cli.post.return_value = mock_resp

            engine = DeepSeekEngine()
            with self.assertRaises(DeepSeekResponseTruncated,
                msg='截断响应必须抛 DeepSeekResponseTruncated,而非 TypeError 或 JSONDecodeError'):
                engine._call_api_with_prompt('fake prompt', multi=True)

    def test_truncated_response_does_not_call_json_loads(self):
        """核心:finish_reason='length' 时不应走到 json.loads(避免无谓解析 + 抛 JSONDecodeError)。

        通过 mock json.loads 后抛异常来验证:截断路径不应触发 json.loads。
        """
        with mock.patch.object(DeepSeekEngine, 'API_KEY', 'sk-test-fake'), \
             mock.patch('httpx.Client') as MockClient, \
             mock.patch('blueprints.ocr_engine.json.loads',
                        side_effect=AssertionError('json.loads 不应在截断时被调用')) as mock_loads:
            mock_cli = MagicMock()
            MockClient.return_value.__enter__.return_value = mock_cli
            mock_resp = MagicMock()
            mock_resp.raise_for_status.return_value = None
            mock_resp.json.return_value = {
                'choices': [{
                    'finish_reason': 'length',
                    'message': {'content': ''},  # 空 content,典型截断
                }],
            }
            mock_cli.post.return_value = mock_resp

            engine = DeepSeekEngine()
            with self.assertRaises(DeepSeekResponseTruncated):
                engine._call_api_with_prompt('fake prompt', multi=False)
            mock_loads.assert_not_called()

    def test_malformed_json_with_stop_finish_also_raises_truncated(self):
        """核心:finish_reason='stop' 但 JSON 语法错时,旧代码走 raise
        openai.APIError(...)(缺 request= 参数,抛 TypeError)。
        修复后统一归类为 DeepSeekResponseTruncated,让上层 fallback。
        """
        with mock.patch.object(DeepSeekEngine, 'API_KEY', 'sk-test-fake'), \
             mock.patch('httpx.Client') as MockClient:
            mock_cli = MagicMock()
            MockClient.return_value.__enter__.return_value = mock_cli
            mock_resp = MagicMock()
            mock_resp.raise_for_status.return_value = None
            mock_resp.json.return_value = {
                'choices': [{
                    'finish_reason': 'stop',
                    'message': {'content': '{"items": [{"product_name": "x'},  # 坏 JSON
                }],
            }
            mock_cli.post.return_value = mock_resp

            engine = DeepSeekEngine()
            with self.assertRaises(DeepSeekResponseTruncated):
                engine._call_api_with_prompt('fake prompt', multi=True)

    def test_exception_class_is_runtimeerror_subclass(self):
        """DeepSeekResponseTruncated 必须是 RuntimeError 子类 — 上层
        ocr_pipeline.classify 的 except Exception 能捕获,但 isinstance(...,RuntimeError)
        也能区分(便于未来加特殊处理)。
        """
        self.assertTrue(issubclass(DeepSeekResponseTruncated, RuntimeError))
        self.assertTrue(issubclass(DeepSeekResponseTruncated, Exception))
        # 模块级导出(便于其他模块直接 import)
        self.assertIs(ocr_engine_mod.DeepSeekResponseTruncated, DeepSeekResponseTruncated)
