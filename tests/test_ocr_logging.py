"""OCR/AI 文件日志测试。"""
import logging
import os
from datetime import date

import pytest

from logging_setup import LOG_ROOT, DailyFolderHandler
import contextvars
import threading

from logging_setup import (TraceIdFilter, get_trace_id, init_logging,
                           new_trace_id)
from blueprints.ocr_log import (clear_log_context, get_log_context, scrub,
                                set_log_context, trunc)
from blueprints.ocr_log import (image_payload, items_outcome, log_ocr_call,
                                parsed_outcome, prompt_payload, text_outcome)


def _make_handler(tmp_path, prefix='ocr', day=date(2026, 8, 4)):
    """建一个日期被钉死的 handler，写到 tmp_path。"""
    h = DailyFolderHandler(prefix, log_root=str(tmp_path))
    h._today = lambda: day          # 实例属性遮蔽方法，注入假日期
    h._current_day = day
    h.baseFilename = h._path_for(day)
    return h


def _emit(handler, msg, level=logging.INFO):
    rec = logging.LogRecord('ocr.test', level, __file__, 1, msg, None, None)
    handler.emit(rec)
    handler.flush()


def test_log_root_points_at_project_root_log_dir():
    assert os.path.basename(LOG_ROOT) == 'log'
    # LOG_ROOT 的父目录应含 app.py（项目根）
    assert os.path.isfile(os.path.join(os.path.dirname(LOG_ROOT), 'app.py'))


def test_path_uses_yyyymm_folder_and_dated_filename(tmp_path):
    h = _make_handler(tmp_path)
    path = h._path_for(date(2026, 8, 4))
    assert path == os.path.join(str(tmp_path), '202608', 'ocr-2026-08-04.log')
    h.close()


def test_emit_creates_month_folder_and_writes(tmp_path):
    h = _make_handler(tmp_path)
    _emit(h, 'hello')
    h.close()
    target = tmp_path / '202608' / 'ocr-2026-08-04.log'
    assert target.exists()
    assert 'hello' in target.read_text(encoding='utf-8')


def test_rolls_to_new_file_when_day_changes(tmp_path):
    h = _make_handler(tmp_path)
    _emit(h, 'day-one')
    h._today = lambda: date(2026, 8, 5)      # 跨天
    _emit(h, 'day-two')
    h.close()
    d1 = tmp_path / '202608' / 'ocr-2026-08-04.log'
    d2 = tmp_path / '202608' / 'ocr-2026-08-05.log'
    assert 'day-one' in d1.read_text(encoding='utf-8')
    assert 'day-one' not in d2.read_text(encoding='utf-8')
    assert 'day-two' in d2.read_text(encoding='utf-8')


def test_rolls_into_new_month_folder(tmp_path):
    h = _make_handler(tmp_path)
    _emit(h, 'august')
    h._today = lambda: date(2026, 9, 1)      # 跨月
    _emit(h, 'september')
    h.close()
    assert (tmp_path / '202609' / 'ocr-2026-09-01.log').exists()
    assert 'september' in (tmp_path / '202609' / 'ocr-2026-09-01.log').read_text(encoding='utf-8')


def test_chinese_content_written_as_utf8(tmp_path):
    """Windows 默认 GBK 会炸，必须显式 utf-8（CLAUDE.md 踩坑点 7）。"""
    h = _make_handler(tmp_path)
    _emit(h, '杂胶 加面 1.2mm 环保 ⊛ ✓')
    h.close()
    text = (tmp_path / '202608' / 'ocr-2026-08-04.log').read_text(encoding='utf-8')
    assert '杂胶 加面 1.2mm 环保 ⊛ ✓' in text


def test_delay_true_no_file_until_first_emit(tmp_path):
    h = DailyFolderHandler('ocr', log_root=str(tmp_path))
    # 月目录会被建出来（_path_for 里 makedirs），但日志文件不应存在
    assert not any(tmp_path.rglob('*.log'))
    h.close()


def test_trace_id_defaults_to_dash_when_unset():
    def probe():
        return get_trace_id()
    # 新线程 = 全新 context，ContextVar 取默认值
    box = {}
    t = threading.Thread(target=lambda: box.update(v=probe()))
    t.start(); t.join()
    assert box['v'] == '-'


def test_new_trace_id_is_8_hex_chars():
    def run():
        tid = new_trace_id()
        assert len(tid) == 8
        assert all(c in '0123456789abcdef' for c in tid)
    contextvars.copy_context().run(run)


def test_trace_id_stable_within_same_context():
    def run():
        first = new_trace_id()
        assert get_trace_id() == first
        assert get_trace_id() == first
    contextvars.copy_context().run(run)


def test_same_process_trace_ids_share_pid_prefix():
    """高 4 位由 PID 派生，用于区分 Flask reloader 的两个进程。"""
    ids = []
    for _ in range(5):
        contextvars.copy_context().run(lambda: ids.append(new_trace_id()))
    assert len({tid[:4] for tid in ids}) == 1        # 前缀一致
    assert len({tid[4:] for tid in ids}) > 1         # 后缀随机


def test_trace_id_propagates_into_background_thread():
    """行级图片 OCR 跑在后台线程，必须能带上同一个 trace_id。"""
    box = {}

    def outer():
        parent = new_trace_id()
        ctx = contextvars.copy_context()
        t = threading.Thread(target=ctx.run, args=(lambda: box.update(child=get_trace_id()),))
        t.start(); t.join()
        box['parent'] = parent

    contextvars.copy_context().run(outer)
    assert box['child'] == box['parent']


def test_trace_id_filter_injects_attribute():
    rec = logging.LogRecord('ocr.test', logging.INFO, __file__, 1, 'x', None, None)
    assert not hasattr(rec, 'trace_id')
    TraceIdFilter().filter(rec)
    assert rec.trace_id == '-'


def test_init_logging_writes_ocr_and_app_files(tmp_path):
    init_logging(log_root=str(tmp_path))
    logging.getLogger('ocr.deepseek').info('ocr-line')
    logging.getLogger('blueprints.shipping').error('app-line')
    for h in logging.getLogger('ocr').handlers + logging.getLogger().handlers:
        h.flush()

    ocr_files = list(tmp_path.rglob('ocr-*.log'))
    app_files = list(tmp_path.rglob('app-*.log'))
    assert ocr_files and app_files
    ocr_text = ocr_files[0].read_text(encoding='utf-8')
    app_text = app_files[0].read_text(encoding='utf-8')
    assert 'ocr-line' in ocr_text
    assert 'app-line' in app_text
    # propagate=False：OCR 日志不应重复出现在 app 日志里
    assert 'ocr-line' not in app_text


def test_init_logging_is_idempotent(tmp_path):
    """Flask reloader / 重复 create_app 不应叠加 handler 造成重复行。"""
    init_logging(log_root=str(tmp_path))
    init_logging(log_root=str(tmp_path))
    init_logging(log_root=str(tmp_path))
    ocr_logger = logging.getLogger('ocr')
    file_handlers = [h for h in ocr_logger.handlers if isinstance(h, DailyFolderHandler)]
    assert len(file_handlers) == 1


def test_app_log_ignores_info_level(tmp_path):
    """root 设 WARNING，werkzeug 的 INFO 请求日志不该淹没 app 日志。"""
    init_logging(log_root=str(tmp_path))
    logging.getLogger('werkzeug').info('GET /shipping-records 200')
    for h in logging.getLogger().handlers:
        h.flush()
    app_files = list(tmp_path.rglob('app-*.log'))
    text = app_files[0].read_text(encoding='utf-8') if app_files else ''
    assert 'GET /shipping-records' not in text


def test_log_context_roundtrip():
    def run():
        assert get_log_context() == {}
        set_log_context(order_id=610, record_id=88)
        assert get_log_context() == {'order_id': 610, 'record_id': 88}
        set_log_context(image_id=7)
        assert get_log_context()['image_id'] == 7
        assert get_log_context()['order_id'] == 610      # 累加不覆盖
        clear_log_context()
        assert get_log_context() == {}
    contextvars.copy_context().run(run)


def test_log_context_ignores_none_values():
    def run():
        set_log_context(order_id=610, record_id=None)
        assert get_log_context() == {'order_id': 610}
    contextvars.copy_context().run(run)


def test_log_context_returns_copy_not_live_dict():
    def run():
        set_log_context(order_id=610)
        snapshot = get_log_context()
        snapshot['order_id'] = 999
        assert get_log_context()['order_id'] == 610
    contextvars.copy_context().run(run)


def test_log_context_propagates_into_background_thread():
    box = {}

    def outer():
        set_log_context(order_id=610, record_id=88)
        ctx = contextvars.copy_context()
        t = threading.Thread(target=ctx.run, args=(lambda: box.update(c=get_log_context()),))
        t.start(); t.join()

    contextvars.copy_context().run(outer)
    assert box['c'] == {'order_id': 610, 'record_id': 88}


def test_scrub_removes_api_keys():
    assert 'sk-abcd1234efgh5678' not in scrub('Bearer sk-abcd1234efgh5678 rest')
    assert 'sk-***' in scrub('Bearer sk-abcd1234efgh5678 rest')
    assert scrub('Bearer sk-abcd1234efgh5678 rest').endswith(' rest')


def test_scrub_leaves_normal_text_alone():
    text = '杂胶 加面 1.2mm sk-短'          # 太短不像 key，不动
    assert scrub(text) == text


def test_scrub_handles_non_string():
    assert scrub(None) == ''
    assert scrub(123) == '123'


def test_trunc_cuts_long_text_and_marks_length():
    out = trunc('x' * 500, 200)
    assert len(out) < 260
    assert out.startswith('x' * 200)
    assert '500' in out              # 标注原始长度


def test_trunc_leaves_short_text_intact():
    assert trunc('hello', 200) == 'hello'


# ═══════════════════════════════════════════════════════════════════
# Task 4: @log_ocr_call 装饰器
# ═══════════════════════════════════════════════════════════════════


@pytest.fixture
def logdir(tmp_path, monkeypatch):
    """每个测试独立的日志目录，返回读取 ocr 日志全文的函数。"""
    monkeypatch.setenv('OCR_LOG_LEVEL', 'INFO')
    init_logging(log_root=str(tmp_path))

    def read():
        for h in logging.getLogger('ocr').handlers:
            h.flush()
        files = list(tmp_path.rglob('ocr-*.log'))
        return files[0].read_text(encoding='utf-8') if files else ''

    return read


@pytest.fixture
def logdir_debug(tmp_path, monkeypatch):
    monkeypatch.setenv('OCR_LOG_LEVEL', 'DEBUG')
    init_logging(log_root=str(tmp_path))

    def read():
        for h in logging.getLogger('ocr').handlers:
            h.flush()
        files = list(tmp_path.rglob('ocr-*.log'))
        return files[0].read_text(encoding='utf-8') if files else ''

    return read


LONG_PROMPT = '请比对以下标签与明细行：' + 'A' * 3000


class _FakeEngine:
    """模拟 5 种被包装方法的行为。"""

    @log_ocr_call('ocr.fake', evt='ok_call', payload=prompt_payload,
                  outcome=parsed_outcome)
    def ok_call(self, prompt_text, multi=False):
        return {'match_status': 'green', 'reason': '完全一致'}

    @log_ocr_call('ocr.fake', evt='raiser', payload=prompt_payload,
                  outcome=parsed_outcome)
    def raiser(self, prompt_text, multi=False):
        raise ValueError('DeepSeek 返回非 JSON')

    @log_ocr_call('ocr.fake', evt='recognize', failed_if=lambda r: not r.get('success'),
                  payload=image_payload, outcome=items_outcome)
    def recognize(self, image_bytes, filename=''):
        return {'success': False, 'error': 'AI 识别功能未配置'}

    @log_ocr_call('ocr.fake', evt='recognize_ok', failed_if=lambda r: not r.get('success'),
                  payload=image_payload, outcome=items_outcome)
    def recognize_ok(self, image_bytes, filename=''):
        return {'success': True, 'items': [{'product_name': '杂胶'}, {'product_name': '纯胶'}]}

    @log_ocr_call('ocr.fake', evt='extract_text', failed_if=lambda r: not r,
                  payload=image_payload, outcome=text_outcome)
    def extract_text(self, image_bytes):
        return ''          # 现实中 PaddleOCR 失败就是返回空串


def test_success_at_info_logs_summary_without_prompt(logdir):
    _FakeEngine().ok_call(LONG_PROMPT)
    text = logdir()
    assert 'evt=ok_call' in text
    assert 'elapsed=' in text
    assert 'AAAA' not in text                 # INFO 不写 prompt 全文


def test_success_at_debug_logs_full_prompt(logdir_debug):
    _FakeEngine().ok_call(LONG_PROMPT)
    text = logdir_debug()
    assert 'A' * 3000 in text                 # DEBUG 写全文


def test_exception_logs_full_payload_at_info_level(logdir):
    """核心加强项：失败无条件全量，不受 DEBUG 开关约束。"""
    with pytest.raises(ValueError):
        _FakeEngine().raiser(LONG_PROMPT)
    text = logdir()
    assert 'ERROR' in text
    assert 'A' * 3000 in text                 # DEBUG 没开也有全文
    assert 'Traceback' in text
    assert 'DeepSeek 返回非 JSON' in text


def test_success_false_return_treated_as_failure(logdir):
    """recognize 吞异常返回 success=False，必须判为失败。"""
    _FakeEngine().recognize(b'\xff\xd8\xff' * 100, 'a.jpg')
    text = logdir()
    assert 'ERROR' in text
    assert 'AI 识别功能未配置' in text


def test_empty_string_return_treated_as_failure(logdir):
    """extract_text 失败返回空串，必须判为失败。"""
    _FakeEngine().extract_text(b'\xff\xd8\xff' * 100)
    text = logdir()
    assert 'ERROR' in text
    assert 'evt=extract_text' in text


def test_successful_recognize_logged_as_info_with_item_count(logdir):
    _FakeEngine().recognize_ok(b'\xff\xd8\xff' * 100, 'b.jpg')
    text = logdir()
    assert 'ERROR' not in text
    assert 'items=2' in text
    assert 'img=b.jpg' in text


def test_decorator_is_transparent_to_return_value(logdir):
    assert _FakeEngine().ok_call('p') == {'match_status': 'green', 'reason': '完全一致'}
    assert _FakeEngine().recognize_ok(b'x', 'c.jpg')['success'] is True
    assert _FakeEngine().extract_text(b'x') == ''


def test_decorator_reraises_original_exception(logdir):
    with pytest.raises(ValueError, match='DeepSeek 返回非 JSON'):
        _FakeEngine().raiser('p')


def test_image_bytes_never_written_to_log(logdir):
    """图片字节绝不入日志 —— Moonshot 单图 base64 数百 KB。"""
    blob = b'\x89PNG\r\n\x1a\n' + bytes(range(256)) * 40
    _FakeEngine().recognize_ok(blob, 'big.jpg')
    text = logdir()
    assert 'PNG' not in text
    assert 'size=' in text                    # 只记大小
    assert 'img=big.jpg' in text


def test_api_key_scrubbed_from_logged_prompt(logdir_debug):
    _FakeEngine().ok_call('前缀 sk-abcd1234efgh5678ijkl 后缀')
    text = logdir_debug()
    assert 'sk-abcd1234efgh5678ijkl' not in text
    assert 'sk-***' in text


def test_business_context_appears_in_log(logdir):
    def run():
        set_log_context(order_id=610, record_id=88)
        _FakeEngine().ok_call('p')
    contextvars.copy_context().run(run)
    text = logdir()
    assert 'order_id=610' in text
    assert 'record_id=88' in text


def test_logging_failure_does_not_break_business_call(logdir, monkeypatch):
    """日志系统炸了也不能影响业务返回。"""
    def boom(*a, **kw):
        raise RuntimeError('payload 提取炸了')

    class _E:
        @log_ocr_call('ocr.fake', evt='x', payload=boom, outcome=boom)
        def go(self, prompt_text):
            return {'ok': True}

    assert _E().go('p') == {'ok': True}


def test_trace_id_shared_across_calls_in_one_context(logdir):
    def run():
        new_trace_id()
        _FakeEngine().ok_call('p1')
        _FakeEngine().ok_call('p2')
    contextvars.copy_context().run(run)
    lines = [ln for ln in logdir().splitlines() if 'evt=ok_call' in ln]
    assert len(lines) == 2
    trace_ids = {ln.split('[')[1].split(']')[0] for ln in lines}
    assert len(trace_ids) == 1 and '-' not in trace_ids


# ═══════════════════════════════════════════════════════════════════
# Task 5: 5 个引擎收敛点接装饰器
# ═══════════════════════════════════════════════════════════════════


def test_all_five_engine_methods_are_decorated():
    """5 个收敛点必须都挂上装饰器 —— 漏一个就有整条链路没日志。"""
    from blueprints.ocr_engine import (DeepSeekEngine, MoonshotEngine,
                                       PaddleOCREngine)
    targets = [
        (MoonshotEngine, 'recognize'),
        (PaddleOCREngine, 'extract_text'),
        (PaddleOCREngine, 'recognize'),
        (DeepSeekEngine, '_call_api_with_prompt'),
        (DeepSeekEngine, 'recognize'),
    ]
    for cls, name in targets:
        fn = getattr(cls, name)
        assert getattr(fn, '__wrapped__', None) is not None, \
            '%s.%s 没有加 @log_ocr_call' % (cls.__name__, name)


def test_engine_logger_lives_under_ocr_tree():
    """ocr_engine 的模块 logger 必须在 'ocr.' 树下，否则日志跑去 app 文件。"""
    from blueprints import ocr_engine
    assert ocr_engine.logger.name.startswith('ocr')


def test_moonshot_unconfigured_key_logged_as_error(logdir, monkeypatch):
    """未配 key 时 recognize 返回 success=False —— 应记 ERROR。"""
    from blueprints.ocr_engine import MoonshotEngine
    monkeypatch.setattr(MoonshotEngine, 'API_KEY', '')
    MoonshotEngine().recognize(b'\xff\xd8\xff', 'x.jpg')
    text = logdir()
    assert 'ERROR' in text
    assert 'evt=recognize' in text
    assert 'AI 识别功能未配置' in text


# ═══════════════════════════════════════════════════════════════════
# Task 6: 把日志接进 app — init_logging、线程传播、业务上下文
# ═══════════════════════════════════════════════════════════════════


def test_create_app_installs_file_logging(tmp_path, monkeypatch):
    monkeypatch.setenv('OCR_LOG_LEVEL', 'INFO')
    import logging_setup
    monkeypatch.setattr(logging_setup, 'LOG_ROOT', str(tmp_path))
    from app import create_app
    create_app()
    handlers = logging.getLogger('ocr').handlers
    assert any(isinstance(h, DailyFolderHandler) for h in handlers)


def test_request_gets_trace_id(tmp_path, monkeypatch):
    monkeypatch.setenv('OCR_LOG_LEVEL', 'INFO')
    import logging_setup
    monkeypatch.setattr(logging_setup, 'LOG_ROOT', str(tmp_path))
    from app import create_app
    app = create_app()
    seen = {}

    # 用 /api/ 前缀绕开登录闸门(app.py 的 _require_login 把 /api/ 当公开)
    @app.route('/api/__trace_probe')
    def _probe():
        seen['tid'] = get_trace_id()
        return 'ok'

    app.test_client().get('/api/__trace_probe')
    assert seen['tid'] != '-'
    assert len(seen['tid']) == 8


def test_background_thread_spawn_propagates_context(monkeypatch):
    """行级图片 OCR 跑在 daemon 线程，trace_id 与业务上下文必须带进去。

    行为测试：真调一次 _spawn_record_image_processing，把它要起的目标函数
    换成探针，断言后台线程里读到的 trace_id / 业务上下文与主线程一致。
    不断言源码里有没有 'copy_context' 字样 —— 换等价写法不该让测试误报。
    """
    import blueprints.shipping as sh

    done = threading.Event()
    box = {}

    def _probe(image_id, filepath, record, order_id, record_id):
        box['trace'] = get_trace_id()
        box['ctx'] = get_log_context()
        done.set()

    monkeypatch.setattr(sh, '_process_record_image_async', _probe)

    def outer():
        box['parent_trace'] = new_trace_id()
        set_log_context(order_id=610, record_id=88)
        sh._spawn_record_image_processing(
            image_id=1, filepath='x.jpg', record={}, order_id=610,
            record_id=88, rel_path='2026-08/x.jpg', original_name='x.jpg',
            sort_order=0)

    contextvars.copy_context().run(outer)
    assert done.wait(timeout=5), '后台线程没跑起来'
    assert box['trace'] == box['parent_trace'], \
        'trace_id 没传进后台线程 —— 检查是否用了 contextvars.copy_context()'
    assert box['ctx'] == {'order_id': 610, 'record_id': 88}, \
        '业务上下文没传进后台线程'


def test_gitignore_excludes_log_dir():
    with open('.gitignore', encoding='utf-8') as f:
        assert 'log/' in f.read()
