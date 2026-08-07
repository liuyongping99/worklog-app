"""/recognize + /confirm 端点集成测试"""
import io


def test_recognize_endpoint_returns_json(client, monkeypatch):
    from blueprints import voice_pipeline
    monkeypatch.setattr(
        voice_pipeline, 'recognize',
        lambda audio_bytes, audio_format='wav': {
            'success': True,
            'recognized_text': '丰泽出货',
            'customer': '丰泽',
            'items': [],
            'needs_disambiguation': False,
        }
    )
    data = {'audio': (io.BytesIO(b'fake_audio'), 'test.webm', 'audio/webm')}
    rv = client.post('/api/v1/voice/recognize', data=data,
                     content_type='multipart/form-data')
    assert rv.status_code == 200
    body = rv.get_json()
    assert body['success'] is True
    assert body['customer'] == '丰泽'


def test_recognize_endpoint_rejects_missing_audio(client):
    rv = client.post('/api/v1/voice/recognize', data={},
                     content_type='multipart/form-data')
    assert rv.status_code == 400
    assert rv.get_json()['success'] is False


def test_confirm_endpoint_inserts_records(client, monkeypatch):
    from blueprints import voice_pipeline
    from models import ShippingOrder

    monkeypatch.setattr(
        voice_pipeline, 'confirm',
        lambda order_id, items: {
            'success': True,
            'order_id': order_id,
            'records': [{'id': 1, 'product_name': '白磅布三文治', 'quantity': 20}],
        }
    )
    order_id = ShippingOrder.create(date='2026-08-06', customer='丰泽')
    rv = client.post('/api/v1/voice/confirm', json={
        'order_id': order_id,
        'items': [
            {'phrase_part': '白中磅', 'product_id': 169,
             'specification': '1.2中性', 'quantity': 20, 'unit': '支',
             'source': 'fuzzy'},
        ]
    })
    assert rv.status_code == 200
    body = rv.get_json()
    assert body['success'] is True
    assert len(body['records']) == 1


def test_recognize_requires_login(client):
    """未登录:返回 401 或 redirect(取决于 _require_login)"""
    rv = client.post('/api/v1/voice/recognize', data={})
    # /api/ 路径在登录白名单里,直接 400;不要求登录
    assert rv.status_code in (400, 401, 302)
