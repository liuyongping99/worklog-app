"""RecordImageProcessor 单元测试(monkeypatch OCR + DeepSeek)。

不依赖真实 PaddleOCR / DeepSeek,通过 _StubImageModel + get_ocr_engine mock
覆盖 extract_ocr / classify / persist_match / process_full 4 个公开方法。
"""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import fake_png_bytes as _PNG

from blueprints.ocr_pipeline import RecordImageProcessor


# ── image_model 桩 ────────────────────────────────────────────────

class _StubImageModel:
    """满足 image_model 接口契约的桩类(set_match / set_bg_color / get_by_id)。"""
    last_set_match = {}
    last_bg_color = {}

    @staticmethod
    def get_by_id(image_id):
        return {'id': image_id, 'match_status': 'yellow', 'match_score': 0,
                'reason': '', 'match_source': 'local_fuzzy'}

    @staticmethod
    def set_match(image_id, status, score, reason, source=None):
        _StubImageModel.last_set_match = {
            'image_id': image_id, 'status': status, 'score': score,
            'reason': reason, 'source': source,
        }

    @staticmethod
    def set_bg_color(image_id, bg_color):
        _StubImageModel.last_bg_color = {
            'image_id': image_id, 'bg_color': bg_color,
        }

    @classmethod
    def reset(cls):
        cls.last_set_match = {}
        cls.last_bg_color = {}


def _stub_paddle(text='OCR_TEXT', conf=0.95):
    """构造一个返回 (text, conf) 的 paddle mock。"""
    paddle = mock.MagicMock()
    paddle.extract_text_with_conf.return_value = (text, conf)
    return paddle


def _stub_deepseek(api_key='', compare_return=None, side_effect=None):
    """构造一个 deepseek mock。"""
    ds = mock.MagicMock()
    ds.API_KEY = api_key
    if side_effect is not None:
        ds.compare_single_record.side_effect = side_effect
    elif compare_return is not None:
        ds.compare_single_record.return_value = compare_return
    return ds


def _engine_factory(paddle_mock, ds_mock):
    """get_ocr_engine 工厂 mock:按 name 返回对应引擎。"""
    def _factory(name):
        if name == 'deepseek':
            return ds_mock
        return paddle_mock
    return _factory


# ── extract_ocr ───────────────────────────────────────────────────

class ExtractOcrTests(unittest.TestCase):
    def setUp(self):
        _StubImageModel.reset()
        self.tmp = tempfile.NamedTemporaryFile(suffix='.png', delete=False)
        self.tmp.write(_PNG())
        self.tmp.close()
        self.processor = RecordImageProcessor(_StubImageModel)

    def tearDown(self):
        os.unlink(self.tmp.name)

    @mock.patch('blueprints.ocr_pipeline.detect_bg_color')
    @mock.patch('blueprints.ocr_pipeline.get_ocr_engine')
    def test_returns_text_and_appends_bg_color_black(self, mock_factory, mock_bg):
        """OCR + bg_color='black' → ocr_text 末尾追加 [标签背景: 黑色]。"""
        mock_factory.return_value = _stub_paddle('磅布三文治 厚度1.0mm', 0.95)
        mock_bg.return_value = 'black'

        result = self.processor.extract_ocr(
            self.tmp.name, record={'product_name': '磅布三文治'})

        self.assertEqual(result['bg_color'], 'black')
        self.assertIn('磅布三文治', result['ocr_text'])
        self.assertIn('[标签背景: 黑色]', result['ocr_text'])
        self.assertAlmostEqual(result['avg_conf'], 0.95, places=2)
        self.assertFalse(result['from_cache'])
        # image_id=None → 不写 bg_color
        self.assertEqual(_StubImageModel.last_bg_color, {})

    @mock.patch('blueprints.ocr_pipeline.detect_bg_color')
    @mock.patch('blueprints.ocr_pipeline.get_ocr_engine')
    def test_writes_bg_color_when_image_id_given(self, mock_factory, mock_bg):
        mock_factory.return_value = _stub_paddle('某商品', 0.8)
        mock_bg.return_value = 'white'

        self.processor.extract_ocr(
            self.tmp.name, record=None, image_id=42)

        self.assertEqual(_StubImageModel.last_bg_color,
                         {'image_id': 42, 'bg_color': 'white'})

    @mock.patch('blueprints.ocr_pipeline._read_cached_ocr')
    @mock.patch('blueprints.ocr_pipeline.detect_bg_color')
    @mock.patch('blueprints.ocr_pipeline.get_ocr_engine')
    def test_uses_cache_when_available(self, mock_factory, mock_bg, mock_cached):
        """缓存命中时不调用 PaddleOCR。"""
        mock_cached.return_value = 'cached_ocr_text'
        mock_bg.return_value = None

        result = self.processor.extract_ocr(
            self.tmp.name, record=None, image_id=99, use_cached=True)

        self.assertTrue(result['from_cache'])
        self.assertEqual(result['ocr_text'], 'cached_ocr_text')
        mock_factory.assert_not_called()

    @mock.patch('blueprints.ocr_pipeline.detect_bg_color')
    @mock.patch('blueprints.ocr_pipeline.get_ocr_engine')
    def test_handles_paddle_exception(self, mock_factory, mock_bg):
        """PaddleOCR 抛异常时返回空 ocr_text + avg_conf=1.0,不阻断。"""
        paddle = mock.MagicMock()
        paddle.extract_text_with_conf.side_effect = RuntimeError('boom')
        mock_factory.return_value = paddle
        mock_bg.return_value = None

        result = self.processor.extract_ocr(self.tmp.name, record=None)

        self.assertEqual(result['ocr_text'], '')
        self.assertEqual(result['avg_conf'], 1.0)
        self.assertFalse(result['from_cache'])


# ── classify ──────────────────────────────────────────────────────

class ClassifyTests(unittest.TestCase):
    def setUp(self):
        self.processor = RecordImageProcessor(_StubImageModel)

    @mock.patch('blueprints.ocr_pipeline.get_ocr_engine')
    def test_returns_local_fuzzy_when_no_deepseek_key(self, mock_factory):
        """DeepSeek API_KEY 为空 → 走本地 RapidFuzz。"""
        ds = _stub_deepseek(api_key='')
        mock_factory.side_effect = _engine_factory(_stub_paddle(), ds)

        result = self.processor.classify(
            '磅布三文治 厚度1.0mm',
            {'product_name': '磅布三文治', 'specification': '1.0mm'})

        self.assertEqual(result['source'], 'local_fuzzy')
        self.assertEqual(result['prompt_text'], '')
        self.assertEqual(result['raw_response'], '')
        self.assertIn(result['status'], ('green', 'yellow', 'red'))

    @mock.patch('blueprints.ocr_pipeline.get_ocr_engine')
    def test_falls_back_to_local_on_deepseek_exception(self, mock_factory):
        """DeepSeek.compare_single_record 抛异常 → fallback 到本地。"""
        ds = _stub_deepseek(api_key='sk-test',
                            side_effect=RuntimeError('network down'))
        mock_factory.side_effect = _engine_factory(_stub_paddle(), ds)

        result = self.processor.classify(
            '磅布三文治 厚度1.0mm',
            {'product_name': '磅布三文治', 'specification': '1.0mm'})

        self.assertEqual(result['source'], 'local_fuzzy')

    @mock.patch('blueprints.ocr_pipeline.get_ocr_engine')
    def test_uses_deepseek_when_available(self, mock_factory):
        """DeepSeek 返回 green → 走 deepseek 分支,prompt/raw_response 都带。"""
        ds = _stub_deepseek(api_key='sk-test', compare_return={
            'match_status': 'green',
            'match_score': 95.0,
            'reason': '品名命中',
            'prompt_text': 'PROMPT_TEXT',
            'raw_response': '{"ok": true}',
        })
        mock_factory.side_effect = _engine_factory(_stub_paddle(), ds)

        result = self.processor.classify(
            '磅布三文治 厚度1.0mm',
            {'product_name': '磅布三文治', 'specification': '1.0mm'})

        self.assertEqual(result['source'], 'deepseek')
        self.assertEqual(result['status'], 'green')
        self.assertEqual(result['prompt_text'], 'PROMPT_TEXT')

    @mock.patch('blueprints.ocr_pipeline.get_ocr_engine')
    def test_returns_empty_when_ocr_text_blank(self, mock_factory):
        """OCR 为空 → 跳过所有引擎,不打徽章。"""
        result = self.processor.classify(
            '', {'product_name': 'X', 'specification': 'Y'})

        self.assertEqual(result['status'], '')
        self.assertEqual(result['source'], '')
        mock_factory.assert_not_called()


# ── persist_match ─────────────────────────────────────────────────

class PersistMatchTests(unittest.TestCase):
    def setUp(self):
        _StubImageModel.reset()
        self.processor = RecordImageProcessor(_StubImageModel)

    @mock.patch('blueprints.ocr_pipeline.OcrMatchEvent')
    def test_writes_image_and_two_events_for_deepseek(self, mock_event):
        mock_event.create = mock.MagicMock()

        result = {
            'status': 'green', 'score': 95.0, 'reason': '品名命中',
            'source': 'deepseek', 'prompt_text': 'PROMPT_TEXT',
            'raw_response': '{"ok": true}',
        }
        self.processor.persist_match(
            image_id=10, ocr_text='OCR_TEXT', result=result,
            record={'id': 5, 'product_name': 'P', 'specification': 'S'},
            order_id=20,
        )

        # 1. image.set_match
        self.assertEqual(_StubImageModel.last_set_match['image_id'], 10)
        self.assertEqual(_StubImageModel.last_set_match['status'], 'green')
        # 2. record_ocr + ai_match 两条事件
        self.assertEqual(mock_event.create.call_count, 2)
        first_call = mock_event.create.call_args_list[0]
        self.assertEqual(first_call[0][0], 'record_ocr')
        self.assertEqual(first_call[1]['image_id'], 10)
        second_call = mock_event.create.call_args_list[1]
        self.assertEqual(second_call[0][0], 'ai_match')
        self.assertEqual(second_call[1]['prompt_payload'], 'PROMPT_TEXT')

    @mock.patch('blueprints.ocr_pipeline.OcrMatchEvent')
    def test_only_record_ocr_event_for_local_fallback(self, mock_event):
        """本地 fallback 时只写 record_ocr,不写 ai_match。"""
        mock_event.create = mock.MagicMock()

        result = {
            'status': 'green', 'score': 90.0, 'reason': '本地命中',
            'source': 'local_fuzzy', 'prompt_text': '', 'raw_response': '',
        }
        self.processor.persist_match(
            image_id=11, ocr_text='OCR_TEXT', result=result,
            record={'id': 6, 'product_name': 'P', 'specification': 'S'},
            order_id=21,
        )

        self.assertEqual(mock_event.create.call_count, 1)
        self.assertEqual(mock_event.create.call_args[0][0], 'record_ocr')


# ── process_full 编排 ────────────────────────────────────────────

class ProcessFullTests(unittest.TestCase):
    def setUp(self):
        _StubImageModel.reset()
        self.tmp = tempfile.NamedTemporaryFile(suffix='.png', delete=False)
        self.tmp.write(_PNG())
        self.tmp.close()
        self.processor = RecordImageProcessor(_StubImageModel)

    def tearDown(self):
        os.unlink(self.tmp.name)

    @mock.patch('blueprints.ocr_pipeline.OcrMatchEvent')
    @mock.patch('blueprints.ocr_pipeline.detect_bg_color')
    @mock.patch('blueprints.ocr_pipeline.get_ocr_engine')
    def test_process_full_runs_all_three_steps(self, mock_factory, mock_bg,
                                               mock_event):
        """process_full 一次性走完 extract + classify + persist。"""
        ds = _stub_deepseek(api_key='')  # 走本地
        mock_factory.side_effect = _engine_factory(
            _stub_paddle('磅布三文治', 0.9), ds)
        mock_bg.return_value = 'black'
        mock_event.create = mock.MagicMock()

        out = self.processor.process_full(
            image_id=1, filepath=self.tmp.name,
            record={'id': 2, 'product_name': '磅布三文治', 'specification': '1.0mm'},
            order_id=3, record_id=2,
        )

        self.assertEqual(out['source'], 'local_fuzzy')
        self.assertIn('磅布三文治', out['ocr_text'])
        self.assertEqual(out['bg_color'], 'black')
        self.assertEqual(_StubImageModel.last_set_match['image_id'], 1)
        self.assertEqual(mock_event.create.call_args[0][0], 'record_ocr')


if __name__ == '__main__':
    unittest.main()