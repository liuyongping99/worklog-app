"""MoonshotEngine MAX_TOKENS 8000 + finish_reason='length' 检查防回归测试(2026-09-15)。

背景:订单 995 (货拉拉-AFU126,22 条明细) 装车单 1afbb6123..png (16 行/页)
用户用 moonshot 引擎识别时,响应被截断。

根因:
  1. MoonshotEngine.MAX_TOKENS=4000 对 16 行+长备注不够装完整 JSON 输出
     → finish_reason='length' 截断 → json.loads 抛 JSONDecodeError → 用户看到"格式无法解析"。
  2. 旧版 MoonshotEngine.recognize 没有显式 finish_reason='length' 检查,
     直接 json.loads(raw_text),截断后只在 except JSONDecodeError 兜底返回通用错误。

修复:
  - MoonshotEngine.MAX_TOKENS 从 4000 提到 8000(对齐 DeepSeek)
  - MoonshotEngine.recognize 加 finish_reason='length' 显式检查,返回明确错误信息

防回归覆盖:
  1. MAX_TOKENS 必须 ≥ 8000(防误退回 4000)
  2. finish_reason='length' 路径必须返回 success=False + 明确 hint
"""
import inspect
import unittest
import unittest.mock as mock
from unittest.mock import MagicMock

from blueprints.ocr_engine import MoonshotEngine


class MoonshotMaxTokensFinishReasonTests(unittest.TestCase):
    """MoonshotEngine MAX_TOKENS + finish_reason='length' + response_format — 3 项核心覆盖。"""

    def test_max_tokens_at_least_8000(self):
        """核心:MAX_TOKENS 必须 ≥ 24000,防止退回 8000/12000 导致 reasoning 截断。

        【2026-09-17】8000 时 30s 成功 / 43s 截断交替;12000 时仍偶发;
        加倍到 24000 给 reasoning 留 2x 余量(reasoning 与 output 共用 max_tokens)。
        阈值叫 "at_least_8000" 是历史命名,实际值应跟随当前常量,不能 < 24000。
        """
        self.assertGreaterEqual(MoonshotEngine.MAX_TOKENS, 24000,
            f'MoonshotEngine.MAX_TOKENS={MoonshotEngine.MAX_TOKENS},'
            f'reasoning 把 budget 吃光会 finish_reason=length 截断。必须 ≥ 24000')
        # TIMEOUT 必须 ≥ 120(理由同 MAX_TOKENS,否则 24K token reasoning 会撞网络超时)
        self.assertGreaterEqual(MoonshotEngine.TIMEOUT, 120,
            f'MoonshotEngine.TIMEOUT={MoonshotEngine.TIMEOUT},'
            f'24K token reasoning 在慢网络下可能跑 90s+,必须 ≥ 120s')

    def test_recognize_handles_length_finish_reason(self):
        """核心:recognize() 必须显式处理 finish_reason='length' → success=False + 明确 hint。

        旧版直接 json.loads(raw_text),截断时只在 JSONDecodeError 兜底,错误信息模糊。
        修复后:检测 finish_reason='length' → 返回 'Moonshot 响应被截断(订单行数过多或备注过长)'。
        """
        # 反射取源码看 finish_reason 检查是否在 json.loads 之前
        src = inspect.getsource(MoonshotEngine.recognize)
        # 必须出现 finish_reason 字符串 + length 关键字
        self.assertIn('finish_reason', src,
            'MoonshotEngine.recognize 没读 finish_reason 字段')
        # 必须有针对 length 的判断分支
        self.assertIn("finish_reason == 'length'", src,
            'MoonshotEngine.recognize 缺 finish_reason=="length" 显式检查')

        # 模拟 API 响应:finish_reason='length',raw_text 是不完整 JSON
        with mock.patch.object(MoonshotEngine, 'API_KEY', 'sk-test-fake-key'), \
             mock.patch('openai.OpenAI') as MockOpenAI:
            mock_client = MagicMock()
            MockOpenAI.return_value = mock_client
            mock_response = MagicMock()
            mock_response.choices = [MagicMock()]
            mock_response.choices[0].finish_reason = 'length'
            mock_response.choices[0].message.content = '{"items":[{"product_name":"x'  # 故意截断
            mock_client.chat.completions.create.return_value = mock_response

            engine = MoonshotEngine()
            result = engine.recognize(b'fake', 'fake.png')

            self.assertFalse(result.get('success'),
                '截断响应不应 success=True')
            self.assertIn('Moonshot', result.get('error', ''),
                '错误信息应明确指出 Moonshot')
            self.assertIn('截断', result.get('error', ''),
                '错误信息应明确说出截断')
            # 【2026-09-17】hint 不再误导用户去拆单/删备注——真因是 reasoning
            # token 吃光 budget,与订单大小无关。改为"重试/换引擎/换图"。
            self.assertIn('推理', result.get('hint', ''),
                'hint 应说明真因是 AI 推理消耗 token,而非订单大小')
            self.assertIn('重试', result.get('hint', ''),
                'hint 应给出"重试一次"作为首选操作(reasoning 长度随机,下次可能短)')
            # 防误退回旧的误导文案
            self.assertNotIn('拆分订单图片', result.get('hint', ''),
                'hint 不应再说"拆分订单图片"——那对 reasoning 截断无效')
            self.assertNotIn('简化', result.get('hint', ''),
                'hint 不应再说"简化备注"——那对 reasoning 截断无效')

    def test_recognize_uses_response_format_json_object(self):
        """【2026-09-15】核心:必须给 chat.completions.create 传 response_format={'type':'json_object'}。

        不传这个,Kimi k2.6 内部 reasoning 消耗 max_tokens 把 budget 烧光,
        即使只有 3 行商品也会 finish_reason='length' 截断(JSON 还没写完)。
        对齐 DeepSeekEngine.recognize:2850。
        """
        src = inspect.getsource(MoonshotEngine.recognize)
        self.assertIn("response_format", src,
            'MoonshotEngine.recognize 没传 response_format 给 API,'
            'Kimi k2.6 reasoning 会烧光 max_tokens(3 行商品也会被截断)')
        self.assertIn("'json_object'", src,
            "MoonshotEngine.recognize 没传 response_format={'type':'json_object'}")