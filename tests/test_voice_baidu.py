"""BaiduASR 客户端测试(monkeypatch requests)"""
import pytest


@pytest.fixture
def fake_baidu_env(monkeypatch):
    monkeypatch.setenv('BAIDU_API_KEY', 'test_api_key')
    monkeypatch.setenv('BAIDU_SECRET_KEY', 'test_secret')
    monkeypatch.setenv('BAIDU_VOICE_TOKEN_URL',
                       'https://aip.baidubce.com/oauth/2.0/token')
    monkeypatch.setenv('BAIDU_VOICE_API_URL',
                       'https://vop.baidu.com/server_api')


def test_transcribe_returns_text(monkeypatch, fake_baidu_env):
    from blueprints.voice_baidu import BaiduASR

    # mock token 获取
    token_calls = []

    def fake_token_post(url, data=None, timeout=None):
        from unittest.mock import MagicMock
        token_calls.append(url)
        r = MagicMock()
        r.json.return_value = {'access_token': 'fake_token_123'}
        r.raise_for_status = lambda: None
        return r

    # mock 语音识别
    asr_calls = []

    def fake_asr_post(url, data=None, files=None, timeout=None, json=None):
        from unittest.mock import MagicMock
        asr_calls.append({'url': url, 'data': data, 'json': json})
        r = MagicMock()
        r.json.return_value = {'err_no': 0, 'err_msg': 'success',
                                'result': ['丰泽出货白中磅1.2二十支']}
        r.raise_for_status = lambda: None
        return r

    import requests
    call_count = {'n': 0}

    def fake_post_dispatcher(url, **kwargs):
        if 'oauth' in url:
            return fake_token_post(url, **kwargs)
        return fake_asr_post(url, **kwargs)

    monkeypatch.setattr(requests, 'post', fake_post_dispatcher)

    asr = BaiduASR()
    text = asr.transcribe(b'fake_audio_bytes')

    assert text == '丰泽出货白中磅1.2二十支'
    assert len(token_calls) == 1  # 调了一次 token
    # 第二次调用应该走缓存,不再调 token
    text2 = asr.transcribe(b'fake_audio_bytes')
    assert text2 == '丰泽出货白中磅1.2二十支'
    assert len(token_calls) == 1  # 缓存生效


def test_transcribe_raises_on_api_error(monkeypatch, fake_baidu_env):
    from blueprints.voice_baidu import BaiduASR

    def fake_post(url, **kwargs):
        from unittest.mock import MagicMock
        r = MagicMock()
        if 'oauth' in url:
            r.json.return_value = {'access_token': 'tok'}
        else:
            r.json.return_value = {'err_no': 3300, 'err_msg': '音频质量过差'}
        r.raise_for_status = lambda: None
        return r

    import requests
    monkeypatch.setattr(requests, 'post', fake_post)

    asr = BaiduASR()
    with pytest.raises(RuntimeError, match='音频质量过差'):
        asr.transcribe(b'bad_audio')


def test_transcribe_raises_on_missing_credentials(monkeypatch):
    monkeypatch.delenv('BAIDU_API_KEY', raising=False)
    monkeypatch.delenv('BAIDU_SECRET_KEY', raising=False)
    from blueprints.voice_baidu import BaiduASR
    asr = BaiduASR()
    with pytest.raises(RuntimeError, match='BAIDU_API_KEY'):
        asr.transcribe(b'x')
