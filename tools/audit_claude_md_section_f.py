# -*- coding: utf-8 -*-
"""CLAUDE.md 关键落点完整性自检

两个起因:
  1. 2026-10-09 曾发生一次 Edit 覆盖导致 §f 项及配套段落静默丢失(用户先发现)
  2. 同日发现「技术栈 · AI 集成」只列 3 项、漏了 MiniMax / 复合引擎 / 百度语音

所以本脚本把「文档必须写全的关键事实」写成断言,改文档后跑一遍即可发现缺失。
分组:§f 导入即校验(15 项) + AI 集成清单完整性(11 项)。

新增断言时的坑:
  - 正则里别把 markdown 反引号位置写死 —— `**别按 `unit` 分流**` 这类嵌套反引号
    会让 `r'...`unit`...'` 匹配失败,改用宽松模式(r'别按 .*unit.* 分流')
  - a-f 序列检查能发现「某项被 Edit 吞掉」这类静默丢失
"""
import io
import os
import re
import sys

DOC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'CLAUDE.md')

with io.open(DOC, encoding='utf-8') as f:
    lines = f.readlines()
text = ''.join(lines)

# (落点名, 判定:正则 or None=只查存在, 期望最少命中数)
CHECKS = [
    ('概述 §a 订单来源', r'\*\*a\. 订单来源', 1),
    ('概述 §b 两类图片', r'\*\*b\. 每行两类图片', 1),
    ('概述 §c 标签图比对', r'\*\*c\. 标签图 OCR', 1),
    ('概述 §d 摆放图点数', r'\*\*d\. 摆放图点数', 1),
    ('概述 §e 免 AI 比对', r'\*\*e\. 免 AI 比对标签图', 1),
    ('概述 §f 导入即校验', r'\*\*f\. 明细导入即校验', 1),
    ('§12 子节「导入即校验」', r'#### 导入即校验（概述 §f', 1),
    ('§12 两轨对照表', r'\| \*\*YPP（码基）\*\* \|', 1),
    ('§12 「别按 unit 分流」告警', r'别按 .*unit.* 分流', 1),
    ('§12 响应体字段表', r'`mismatch_detail` \| dict', 1),
    ('§12 不落库拍板', r'\*\*不落库（2026-10-09 拍板）\*\*', 1),
    ('§12 前端消费', r'`readImportFlags\(rec\)` —— \*\*优先消费服务端字段\*\*', 1),
    ('§12 三个验证脚本', r'tools/verify_import_validation_js\.js', 1),
    ('§20 口径同源声明', r'与概述 §f / §12 的「导入即校验」\*\*同一套判定口径\*\*', 1),
    ('待办已完成项', r'概述 §f 的"导入即校验"闭环', 1),

    # ── 技术栈「AI 集成」必须列全 5 引擎 + 百度语音 + 语音三档(2026-10-09 补)──
    ('技术栈 moonshot 引擎', r'`moonshot` \| `MoonshotEngine`', 1),
    ('技术栈 paddleocr 引擎', r'`paddleocr` \| `PaddleOCREngine`', 1),
    ('技术栈 deepseek 引擎', r'`deepseek` \| `DeepSeekEngine`', 1),
    ('技术栈 minimax 引擎', r'`minimax` \| `MiniMaxEngine`', 1),
    ('技术栈 复合引擎', r'`paddleocr_minimax` \| `PaddleOCRMiniMaxEngine`', 1),
    ('技术栈 百度语音', r'\*\*百度语音 API\*\*', 1),
    ('技术栈 语音三档管线', r'语音录入三档降级管线', 1),
    ('技术栈 百度不可用降级说明', r'百度不可用自动降级 LLM', 1),
    ('§3 百度勿漏清单告警', r'别把百度语音漏出「AI 集成」清单', 1),
    ('技术栈 依赖只 5 个的说明', r'别被「五个引擎」误导成要装五个包', 1),
    ('技术栈 引擎 key 对照表', r'见「环境变量」表格', 1),
]

fail = 0
print('=' * 74)
print('CLAUDE.md §f 落点完整性自检')
print('=' * 74)
for name, pat, least in CHECKS:
    hits = len(re.findall(pat, text))
    ok = hits >= least
    if not ok:
        fail += 1
    print(f'  {"OK  " if ok else "MISS"} {name:30s} 命中 {hits} 次')

# 编码健康度:不该出现 HTML 实体
print('-' * 74)
ent = re.findall(r'&(?:amp|#x[0-9a-fA-F]+|lt|gt|quot);', text)
if ent:
    fail += 1
    uniq = sorted(set(ent))
    print(f'  FAIL 检出 HTML 实体残留 {len(ent)} 处: {uniq[:10]}')
    for i, ln in enumerate(lines, 1):
        if re.search(r'&(?:amp|#x[0-9a-fA-F]+|lt|gt|quot);', ln):
            print(f'       line {i}: {ln.strip()[:110]}')
else:
    print('  OK   无 HTML 实体残留')

# a-f 必须连续且有序
print('-' * 74)
letters = re.findall(r'^\s*- \*\*([a-f])\. ', text, re.M)
seq = ''.join(letters)
ok = seq == 'abcdef'
if not ok:
    fail += 1
print(f'  {"OK  " if ok else "FAIL"} a-f 序列连续有序  实际: {seq!r}')

print('=' * 74)
print(f'❌ {fail} 处缺失' if fail else f'✅ 全部 {len(CHECKS)} 个落点齐全，编码干净，a-f 有序')
sys.exit(1 if fail else 0)
