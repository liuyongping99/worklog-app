"""验证:出货订单「同仓库流转」检查函数 — extract_warehouses_from_ocr / is_same_warehouse
(2026-10-21)

背景:
  出货订单默认是芙蓉仓 → 田心仓。AI 识别的订单图片 OCR 文本若同时出现「发货仓库 /
  收货仓库」两个标签且规范化后相等,就是「同仓流转」(没有真实位移),需要拦截。

覆盖:
  - 9 条用例:抽出常见格式 + 误抽防御 + 干扰项处理
  - 5 条 is_same_warehouse 用例:同 / 异 / 缺值 / 大小写 / 标点
  - 1 条端到端:真实 OCR 文本(从 log/202610/ocr-2026-10-16.log:306 同款)
    芙蓉仓 → 田心仓 → 不同 → 允许;改成两芙蓉仓 → 同仓 → 拒绝

依赖:无需网络、无需数据库,纯函数级

用法:
  python tools/verify_warehouse_check.py
"""
import io
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
sys.path.insert(0, r'D:\worklog-app')

from blueprints._helpers import (
    extract_warehouses_from_ocr,
    extract_warehouses_from_result,
    check_same_warehouse,
    is_same_warehouse,
    normalize_warehouse,
)


PASSED = 0
FAILED = 0
ERRORS = []


def check(label, condition, detail=''):
    """assertTrue 的小封装,带累计统计。"""
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f'  ✓ {label}')
    else:
        FAILED += 1
        ERRORS.append(f'{label} — {detail}')
        print(f'  ✗ {label} — {detail}')


# ────────────────────────────────────────────────────────────────────
print('===== extract_warehouses_from_ocr 抽取 =====')

# 用例 1:真实 TD-2026-10-16-004 同款 OCR 文本
sample_ok = """同价调拨单 第1/1页
发货仓库： 芙蓉仓 录单日期： 2026-10-16 15:09:51 单据编号：TD-2026-10-16-004
收货仓库： 田心仓 摘要： 大力3H73M
行号 商品全名 规格 单位 数量 备注
1 无纺布1.6米 BD白200g kg 175 5支
175
制单人：柳永平 发货人： 收货人："""
fw, tw = extract_warehouses_from_ocr(sample_ok)
check('真实 OCR(同价调拨单):芙蓉仓 + 田心仓',
      fw == '芙蓉仓' and tw == '田心仓',
      f'实际 fw={fw!r}, tw={tw!r}')

# 用例 2:同仓流转(用户焦点场景) — 这是要被拦截的输入形态
sample_same = """同价调拨单 第1/1页
发货仓库:芙蓉仓 录单日期:2026-10-21
收货仓库:芙蓉仓 摘要:测试
行号 商品全名 规格 单位 数量 备注
1 PVC桌布 1.2×1.8m kg 100"""
fw, tw = extract_warehouses_from_ocr(sample_same)
check('同仓流转 OCR:抽到 芙蓉仓/芙蓉仓',
      fw == '芙蓉仓' and tw == '芙蓉仓',
      f'实际 fw={fw!r}, tw={tw!r}')

# 用例 3:全半角冒号混用
sample_mix_colon = """发货仓库:芙蓉仓
收货仓库:田心仓"""
fw, tw = extract_warehouses_from_ocr(sample_mix_colon)
check('半角冒号:芙蓉仓 + 田心仓',
      fw == '芙蓉仓' and tw == '田心仓',
      f'实际 fw={fw!r}, tw={tw!r}')

# 用例 4:别名(出货仓库 / 目的仓库 / 调出仓库 / 调入仓库 / ship from / ship to)
sample_alias = """from warehouse: 长沙仓
ship to: 武汉仓"""
fw, tw = extract_warehouses_from_ocr(sample_alias)
check('英文别名 from warehouse / ship to',
      fw == '长沙仓' and tw == '武汉仓',
      f'实际 fw={fw!r}, tw={tw!r}')

# 用例 5:值被引号 / 缩进等包裹
sample_quoted = """【发货仓库】:「芙蓉仓」
（收货仓库）:「田心仓」"""
fw, tw = extract_warehouses_from_ocr(sample_quoted)
check('包装符号(【】「」):芙蓉仓 + 田心仓',
      fw == '芙蓉仓' and tw == '田心仓',
      f'实际 fw={fw!r}, tw={tw!r}')

# 用例 6:空 / None / 非字符串
check('None 输入', extract_warehouses_from_ocr(None) == ('', ''))
check('空字符串输入', extract_warehouses_from_ocr('') == ('', ''))
check('整数输入被拒', extract_warehouses_from_ocr(12345) == ('', ''))

# 用例 7:无关文本 — OCR 没识别到任何仓库,识别出空 (不能让 false-positive 拦截)
fw, tw = extract_warehouses_from_ocr('这是一段没有仓库标签的纯文本\n行号 商品全名 规格')
check('非订单文档 OCR 文本 → (空,空) 不误抽',
      fw == '' and tw == '',
      f'实际 fw={fw!r}, tw={tw!r}')

# 用例 8:OCR 断字("芙 蓉 仓")— 上限 8 字 + 不含空白,只取首块;
# 业务上不需要精确提取仓库名,只需 is_same_warehouse 不误判同/异,
# "芙" vs "田" 必然不等 → 同仓流转拦截不会漏过。
sample_with_space = """发货仓库:芙 蓉 仓
收货仓库:田 心 仓"""
fw, tw = extract_warehouses_from_ocr(sample_with_space)
check('OCR 断字(芙 蓉 仓):只取首块 + 后续比较仍正确',
      fw == '芙' and tw == '田' and not is_same_warehouse(fw, tw),
      f'实际 fw={fw!r}, tw={tw!r}')

# 用例 9:大值截断 — 防止 LLM 误把整段 OCR 描述塞进仓库字段
sample_long = """发货仓库:芙蓉仓\n收货仓库:后面跟一大堆不相关的OCR噪音文字xxxxxxx摘要"""
fw, tw = extract_warehouses_from_ocr(sample_long)
check('长度上限 20 字防止抽过头',
      fw == '芙蓉仓' and len(tw) <= 20,
      f'实际 fw={fw!r}, tw={tw!r}')

# ────────────────────────────────────────────────────────────────────
print('\n===== is_same_warehouse 同仓库判断 =====')

check('芙蓉仓 vs 田心仓 → False', is_same_warehouse('芙蓉仓', '田心仓') is False)
check('芙蓉仓 vs 芙蓉仓 → True',
      is_same_warehouse('芙蓉仓', '芙蓉仓') is True,
      f'实际 is_same_warehouse(芙蓉仓, 芙蓉仓) = {is_same_warehouse("芙蓉仓", "芙蓉仓")}')
check('空值 → False(任一为空)',
      is_same_warehouse('', '芙蓉仓') is False
      and is_same_warehouse('芙蓉仓', '') is False
      and is_same_warehouse('', '') is False)

# 用例:大小写 + 全半角 + 空格归一
check('全角字母大小写归一 (「Ａ仓库」 vs 「a仓库」 → True)',
      is_same_warehouse('Ａ仓库', 'a仓库') is True)
check('ASCII 大小写归一 (「A Warehouse」 vs 「a warehouse」 → True)',
      is_same_warehouse('A Warehouse', 'a warehouse') is True)
check('中英文空格抠掉 (「芙蓉仓」 vs 「芙 蓉 仓」 → True)',
      is_same_warehouse('芙蓉仓', '芙 蓉 仓') is True)
check('尾标点抠掉 (「芙蓉仓」 vs 「芙蓉仓。」 → True)',
      is_same_warehouse('芙蓉仓', '芙蓉仓。') is True)

# ────────────────────────────────────────────────────────────────────
print('\n===== 端到端:replay 真实 OCR 文本(芙蓉→田心 允许, 芙蓉→芙蓉 拦截) =====')

fw, tw = extract_warehouses_from_ocr(sample_ok)
check('end-to-end 真实 OCR(不同仓)→ 不拦截',
      not is_same_warehouse(fw, tw),
      f'fw={fw!r}, tw={tw!r}')

fw, tw = extract_warehouses_from_ocr(sample_same)
check('end-to-end 同仓流转 OCR → 拦截',
      is_same_warehouse(fw, tw),
      f'fw={fw!r}, tw={tw!r}')


# ────────────────────────────────────────────────────────────────────
print('\n===== normalize_warehouse 内部一致性 =====')

# 同一仓库不同写法都得到同一规范化值
expected = normalize_warehouse('芙蓉仓')
candidates = ['芙蓉仓', '芙 蓉 仓', '芙蓉　仓', '芙蓉仓。', '芙蓉仓，', '芙蓉 仓']
for c in candidates:
    got = normalize_warehouse(c)
    check(f'normalization: {c!r} → {got!r}',
          got == expected,
          f'期望 {expected!r}')

check('normalize_warehouse("芙蓉仓") 为空字符串',
      normalize_warehouse('') == ''
      and normalize_warehouse(None) == '')


# ────────────────────────────────────────────────────────────────────
print('\n===== extract_warehouses_from_result 统一取值入口 =====')

# 路径 A:LLM 直吐字段(moonshot)优先
check('LLM 字段优先于 ocr_text',
      extract_warehouses_from_result({
          'from_warehouse': '芙蓉仓', 'to_warehouse': '田心仓',
          'ocr_text': '发货仓库：错误仓\n收货仓库：另一个仓',
      }) == ('芙蓉仓', '田心仓'),
      f'实际 {extract_warehouses_from_result({
          "from_warehouse": "芙蓉仓", "to_warehouse": "田心仓",
          "ocr_text": "发货仓库：错误仓"})!r}')

# 路径 B:LLM 字段缺失 → 回落 ocr_text 正则
check('LLM 字段缺失回落 ocr_text',
      extract_warehouses_from_result({
          'ocr_text': '发货仓库：芙蓉仓\n收货仓库：田心仓',
      }) == ('芙蓉仓', '田心仓'))

# 单边混合:LLM 给了 from,ocr_text 给了 to → 拼起来
check('LLM 给 from + ocr_text 给 to → 合并',
      extract_warehouses_from_result({
          'from_warehouse': '芙蓉仓',
          'ocr_text': '发货仓库：占位\n收货仓库：田心仓',
      }) == ('芙蓉仓', '田心仓'))

check('非 dict 输入 → (空,空)', extract_warehouses_from_result(None) == ('', ''))
check('空 dict → (空,空)', extract_warehouses_from_result({}) == ('', ''))


# ────────────────────────────────────────────────────────────────────
print('\n===== check_same_warehouse 端到端判定 =====')

_same_ocr = '同价调拨单\n发货仓库：芙蓉仓\n收货仓库：芙蓉仓\n行号 商品全名'
_diff_ocr = '同价调拨单\n发货仓库：芙蓉仓\n收货仓库：田心仓\n行号 商品全名'

for label in ('出货订单', '入库单', '装柜单'):
    r = check_same_warehouse({'ocr_text': _same_ocr}, label)
    ok = r is not None and len(r) == 4 \
        and label in r[0] and '芙蓉仓' in r[0] \
        and r[2] == '芙蓉仓' and r[3] == '芙蓉仓'
    check(f'{label}:ocr_text 同仓 → 命中且文案带业务名', ok, f'实际 {r!r}')

    r = check_same_warehouse({'from_warehouse': '田心仓', 'to_warehouse': '田心仓'}, label)
    ok = r is not None and r[2] == '田心仓' and r[3] == '田心仓'
    check(f'{label}:LLM 字段同仓 → 命中', ok, f'实际 {r!r}')

    check(f'{label}:异仓 → None 放行',
          check_same_warehouse({'ocr_text': _diff_ocr}, label) is None)
    check(f'{label}:无仓库字段 → None 放行',
          check_same_warehouse({'items': []}, label) is None)
    check(f'{label}:单边标签 → None 放行',
          check_same_warehouse({'from_warehouse': '芙蓉仓'}, label) is None)

check('默认 biz_label = 出货订单',
      '出货订单' in (check_same_warehouse({'ocr_text': _same_ocr}) or ('',))[0])


# ────────────────────────────────────────────────────────────────────
print(f'\n===== 通过 {PASSED},失败 {FAILED} =====')
if FAILED:
    print('失败用例:')
    for e in ERRORS:
        print(f'  - {e}')
    sys.exit(1)
else:
    print('全部通过 ✓')
