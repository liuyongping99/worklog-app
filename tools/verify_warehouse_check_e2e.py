"""验证:三页 ai-recognize 同仓库拦截(2026-10-21)

mock engine.recognize 返回带仓库信息的结果,走 HTTP 端到端验证:
- 出货 / 入库 / 装柜三页都拦截
- 两条取值路径都覆盖:LLM 直吐 from_warehouse/to_warehouse(moonshot 路径)
  vs ocr_text 正则(deepseek / paddleocr_minimax / paddleocr 路径)
- 放行情形:异仓 / 无仓库字段 / 只有单边标签

用法:
  python tools/verify_warehouse_check_e2e.py
"""
import io
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.path.insert(0, r'D:\worklog-app')

from dotenv import load_dotenv
load_dotenv(r'D:\worklog-app\.env', override=True)

from app import create_app
app = create_app()
client = app.test_client()

# 1x1 PNG,只过扩展名 + 文件头校验,引擎被 mock 不读图片内容
_PNG_BYTES = (
    b'\x89PNG\r\n\x1a\n'
    b'\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02'
    b'\x00\x00\x00\x90wS\xde'
    b'\x00\x00\x00\x0cIDAT\x08\x99c\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xa3]\x9a}'
    b'\x00\x00\x00\x00IEND\xaeB`\x82'
)

# 三页各自的 ai-recognize 端点
ENDPOINTS = [
    ('出货', '/api/v1/shipping-orders/ai-recognize'),
    ('入库', '/api/v1/inbound-orders/ai-recognize'),
    ('装柜', '/api/v1/loading-orders/ai-recognize'),
]

# 路径 A:LLM 直吐字段(moonshot 走这条)
RESULT_LLM_FIELDS_SAME = {
    'success': True,
    'items': [],
    'doc_number': 'TD-2026-10-21-001',
    'customer_name': '测试客户',
    'from_warehouse': '芙蓉仓',
    'to_warehouse': '芙蓉仓',
}
RESULT_LLM_FIELDS_DIFF = {
    'success': True,
    'items': [{'product_name': 'PVC桌布', 'specification': '1.2x1.8m',
               'quantity': '50', 'unit': '支', 'remark': ''}],
    'doc_number': 'TD-2026-10-21-002',
    'customer_name': '调拨客户',
    'from_warehouse': '芙蓉仓',
    'to_warehouse': '田心仓',
}

# 路径 B:ocr_text 正则(deepseek / paddleocr_minimax / paddleocr 走这条)
_OCR_SAME = '同价调拨单\n发货仓库：芙蓉仓\n收货仓库：芙蓉仓\n行号 商品全名'
_OCR_DIFF = '同价调拨单\n发货仓库：芙蓉仓\n收货仓库：田心仓\n行号 商品全名'
RESULT_OCR_SAME = {
    'success': True, 'items': [], 'doc_number': 'TD-2026-10-21-003',
    'customer_name': '测试', 'ocr_text': _OCR_SAME,
}
RESULT_OCR_DIFF = {
    'success': True, 'items': [], 'doc_number': 'TD-2026-10-21-004',
    'customer_name': '测试', 'ocr_text': _OCR_DIFF,
}
RESULT_NO_WAREHOUSE = {
    'success': True, 'items': [], 'doc_number': 'TD-2026-10-21-005',
    'customer_name': '测试', 'from_warehouse': '', 'to_warehouse': '',
}
RESULT_ONE_SIDE_ONLY = {
    'success': True, 'items': [], 'doc_number': 'TD-2026-10-21-006',
    'customer_name': '测试',
    'ocr_text': '发货仓库：芙蓉仓\n(单据没有收货仓库)',
}


def _post(api_path, fake_result):
    """patch ocr_engine.get_ocr_engine 返回假引擎,POST 一次再还原。"""
    import blueprints.ocr_engine as ocr_mod
    original = ocr_mod.get_ocr_engine

    class FakeEngine:
        def __init__(self, name):
            self.name = name
        def recognize(self, img, fn):
            return dict(fake_result)

    ocr_mod.get_ocr_engine = lambda name: FakeEngine(name)
    try:
        return client.post(
            api_path,
            data={'image': (io.BytesIO(_PNG_BYTES), 'fake.png')},
            content_type='multipart/form-data',
        )
    finally:
        ocr_mod.get_ocr_engine = original


PASSED = 0
FAILED = 0
ERRORS = []


def check(label, cond, detail=''):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print(f'  ✓ {label}')
    else:
        FAILED += 1
        ERRORS.append(f'{label} — {detail}')
        print(f'  ✗ {label} — {detail}')


for page_name, api_path in ENDPOINTS:
    print(f'\n===== {page_name}页 {api_path} =====')

    # --- 拦截:LLM 直吐字段,两个仓库相同 ---
    r = _post(api_path, RESULT_LLM_FIELDS_SAME)
    d = r.get_json() or {}
    check(f'{page_name} · LLM字段同仓 → 400',
          r.status_code == 400,
          f'status={r.status_code} data={d}')
    check(f'{page_name} · 响应体 same_warehouse=True + 双仓库名',
          d.get('success') is False
          and d.get('same_warehouse') is True
          and d.get('from_warehouse') == '芙蓉仓'
          and d.get('to_warehouse') == '芙蓉仓'
          and page_name in (d.get('error') or ''),
          f'data={d}')

    # --- 拦截:ocr_text 正则,两个仓库相同 ---
    r = _post(api_path, RESULT_OCR_SAME)
    check(f'{page_name} · ocr_text正则同仓 → 400',
          r.status_code == 400,
          f'status={r.status_code} data={r.get_json()}')

    # --- 放行:LLM 字段不同仓 ---
    r = _post(api_path, RESULT_LLM_FIELDS_DIFF)
    check(f'{page_name} · LLM字段异仓 → 200',
          r.status_code == 200,
          f'status={r.status_code} data={r.get_json()}')

    # --- 放行:ocr_text 不同仓 ---
    r = _post(api_path, RESULT_OCR_DIFF)
    check(f'{page_name} · ocr_text异仓 → 200',
          r.status_code == 200,
          f'status={r.status_code} data={r.get_json()}')

    # --- 放行:完全没有仓库字段 ---
    r = _post(api_path, RESULT_NO_WAREHOUSE)
    check(f'{page_name} · 无仓库字段 → 200 放行',
          r.status_code == 200,
          f'status={r.status_code} data={r.get_json()}')

    # --- 放行:只有单边标签 ---
    r = _post(api_path, RESULT_ONE_SIDE_ONLY)
    check(f'{page_name} · 单边标签 → 200 放行',
          r.status_code == 200,
          f'status={r.status_code} data={r.get_json()}')


# ──────────────────────────────────────────────────────────────
print(f'\n===== 通过 {PASSED},失败 {FAILED} =====')
if FAILED:
    print('失败用例:')
    for e in ERRORS:
        print(f'  - {e}')
    sys.exit(1)
else:
    print('全部通过 ✓')