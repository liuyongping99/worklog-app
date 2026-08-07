"""百度智能云短语音识别客户端。

环境变量:
    BAIDU_API_KEY         # 应用 API Key
    BAIDU_SECRET_KEY      # 应用 Secret Key
    BAIDU_VOICE_TOKEN_URL # 默认 https://aip.baidubce.com/oauth/2.0/token
    BAIDU_VOICE_API_URL   # 默认 https://vop.baidu.com/server_api

用法:
    from blueprints.voice_baidu import BaiduASR
    asr = BaiduASR()
    text = asr.transcribe(audio_bytes)  # 返回识别文字
"""
import base64
import os
import time
import requests
import logging

logger = logging.getLogger(__name__)

_TOKEN_TTL_SECONDS = 2592000  # 百度 access_token 默认 30 天,提前续


class BaiduASR:
    def __init__(self):
        self.api_key = os.environ.get('BAIDU_API_KEY')
        self.secret_key = os.environ.get('BAIDU_SECRET_KEY')
        self.token_url = os.environ.get(
            'BAIDU_VOICE_TOKEN_URL',
            'https://aip.baidubce.com/oauth/2.0/token'
        )
        self.asr_url = os.environ.get(
            'BAIDU_VOICE_API_URL',
            'https://vop.baidu.com/server_api'
        )
        self._token = None
        self._token_expires_at = 0

    def _ensure_token(self):
        if self._token and time.time() < self._token_expires_at - 60:
            return
        if not self.api_key or not self.secret_key:
            raise RuntimeError(
                'BAIDU_API_KEY / BAIDU_SECRET_KEY 未配置,请在 .env 中设置'
            )
        r = requests.post(self.token_url, data={
            'grant_type': 'client_credentials',
            'client_id': self.api_key,
            'client_secret': self.secret_key,
        }, timeout=10)
        r.raise_for_status()
        data = r.json()
        if 'access_token' not in data:
            raise RuntimeError(f'百度 token 获取失败: {data}')
        self._token = data['access_token']
        self._token_expires_at = time.time() + _TOKEN_TTL_SECONDS

    def transcribe(self, audio_bytes: bytes, audio_format: str = 'pcm',
                   sample_rate: int = 16000) -> str:
        """调用百度短语音识别 API,返回文字。

        audio_format: 'pcm' / 'wav' / 'amr'(百度要求 16k 16bit 单声道)
        """
        self._ensure_token()
        # 音频长度:百度要求 base64 后 <= 4MB,总时长 <= 60s
        b64 = base64.b64encode(audio_bytes).decode('ascii')
        body = {
            'format': audio_format,
            'rate': sample_rate,
            'channel': 1,
            'cuid': 'worklog-app',
            'token': self._token,
            'speech': b64,
            'len': len(audio_bytes),
        }
        r = requests.post(self.asr_url, json=body, timeout=30)
        r.raise_for_status()
        data = r.json()
        if data.get('err_no', 0) != 0:
            raise RuntimeError(
                f'百度识别失败: err_no={data.get("err_no")}, '
                f'err_msg={data.get("err_msg", "")}'
            )
        result = data.get('result', [])
        if not result:
            raise RuntimeError('百度识别返回空文字')
        # 百度返回多个候选,第一个最准
        return result[0]
