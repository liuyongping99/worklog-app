"""验证: 用 TD-2026-10-16-004 的真实 OCR 文字,跑 DeepSeek,
期望 STRUCT_PROMPT 加强版能阻止 LLM 把「1.6米 BD白200g」幻觉改写为「无纺布 A白 / A1.5米 200g」。

依赖: .env 有 DEEPSEEK_API_KEY,无则跳过。
"""
import os
import sys
import io

# utf-8 输出 + 路径
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.path.insert(0, r'D:\worklog-app')

# dotenv
from dotenv import load_dotenv
load_dotenv(r'D:\worklog-app\.env', override=True)

if not os.environ.get('DEEPSEEK_API_KEY', '').startswith('sk-'):
    print('DEEPSEEK_API_KEY 未配置,跳过网络验证 (改测试本地字符串拼接是否正确即可)')
    sys.exit(0)

from blueprints.ocr_engine import DeepSeekEngine
eng = DeepSeekEngine()

# 真实 OCR 文字(从 log/202610/ocr-2026-10-16.log:306)
ocr_text = """同价调拨单 第1/1页
发货仓库： 芙蓉仓 录单日期： 2026-10-16 15:09:51 单据编号：TD-2026-10-16-004
收货仓库： 田心仓 摘要： 大力3H73M
行号 商品全名 规格 单位 数量 备注
1 无纺布1.6米 BD白200g kg 175 5支
175
制单人：柳永平 发货人： 收货人：
提货人："""

print('===== 注入 STRUCT_PROMPT 加强版 + 真实 OCR 文字 =====')
print(f'OCR文字第一行: {ocr_text.splitlines()[4]}')
print()
res = eng.recognize_text(ocr_text)
if res.get('success'):
    items = res['items']
    print(f'返回 {len(items)} 条:')
    for it in items:
        print(f"  - product_name = {it.get('product_name')!r}")
        print(f"    specification = {it.get('specification')!r}")
        print(f"    quantity = {it.get('quantity')!r}, unit = {it.get('unit')!r}")
        print(f"    remark = {it.get('remark')!r}")
    print()
    # 校验
    pn = items[0].get('product_name', '') if items else ''
    sp = items[0].get('specification', '') if items else ''
    expected_pn_contains = '1.6'  # OCR 原文含「1.6米」,必须保留
    expected_fantasy_a = 'A白'  # 旧错误模式
    expected_fantasy_15 = '1.5米'  # 旧错误模式
    print('===== 校验 =====')
    if expected_pn_contains in pn or expected_pn_contains in sp:
        print(f'✓ OK: 原文「1.6」已保留 (pn={pn!r}, sp={sp!r})')
    else:
        print(f'✗ FAIL: 原文「1.6」丢失 (pn={pn!r}, sp={sp!r})')
    if expected_fantasy_a in pn:
        print(f'✗ FAIL: 仍然出现幻觉「A白」(套用数据库范式)')
    else:
        print(f'✓ OK: 未出现「A白」范式套用')
    if expected_fantasy_15 in sp:
        print(f'✗ FAIL: 仍然出现幻觉「1.5米」(覆盖原文「1.6米」)')
    else:
        print(f'✓ OK: 未出现「1.5米」覆盖')
else:
    print('FAILED:', res)