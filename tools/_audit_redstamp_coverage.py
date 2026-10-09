# -*- coding: utf-8 -*-
"""Audit: OCR 红章处理白名单覆盖率。

扫 ocr_match_event 全表 yellow/red AI 比对事件,按品名聚合,
找出 yellow+red >= THRESHOLD(默认 5) 且未在 _REDSTAMP_VARIANTS 白名单里的品名。

用法:
    python tools/_audit_redstamp_coverage.py             # 默认阈值 5
    python tools/_audit_redstamp_coverage.py --min=10    # 仅看严重品名
    python tools/_audit_redstamp_coverage.py --json     # 输出 JSON 供脚本消费

输出:控制台表格 + 红章覆盖率提示,人工决定是否需要把新发现的品名加白名单。

不修改任何代码 — 仅报告。这是 2026-10-07 加的工具,配套 _REDSTAMP_VARIANTS 手动维护。

历史:
- 2026-10-07 创建:首次发现 HA猪皮纹系列(×42 yellow/red)未进白名单,后补上。
"""
import json
import sqlite3
import sys
from collections import defaultdict

DB_PATH = 'D:/worklog-app/worklog.db'
DEFAULT_THRESHOLD = 5

# 必须与 blueprints/ocr_engine.py:_REDSTAMP_VARIANTS 一致。
# 这里手维护一份副本(避免 audit 脚本依赖整个 Flask app 启动) ——
# 加白名单时务必**两处同步更新**。
REDSTAMP_WHITELIST = frozenset({
    '杂胶', '纯胶',
    '环保磅布三文治',
    '鱼鳞布', 'LB特软',
    '环保路华里', '路华里',
    'HA猪皮纹', '环保HA猪皮纹',
})


def is_in_whitelist(product_name: str) -> bool:
    """品名是否被 _REDSTAMP_VARIANTS 覆盖(子串匹配,同 ocr_preprocess_kind 行为)。"""
    if not product_name:
        return False
    return any(v in product_name for v in REDSTAMP_WHITELIST)


def audit(min_threshold: int = DEFAULT_THRESHOLD) -> list:
    """扫描 DB,返回未在白名单的 yellow+red 集中品名列表(按 yellow+red 总数降序)。"""
    conn = sqlite3.connect('file:%s?mode=ro' % DB_PATH.replace('\\', '/'), uri=True)
    cur = conn.cursor()
    cur.execute("""
        SELECT product_name,
               SUM(CASE WHEN ai_match_status='yellow' THEN 1 ELSE 0 END) as yellow,
               SUM(CASE WHEN ai_match_status='red'    THEN 1 ELSE 0 END) as red,
               COUNT(*) as total
        FROM ocr_match_event
        WHERE event_type='ai_match' AND ai_match_status IN ('yellow','red')
        GROUP BY product_name
    """)
    buckets = defaultdict(lambda: {'yellow': 0, 'red': 0, 'total': 0})
    for pn, yellow, red, total in cur.fetchall():
        buckets[pn or '(empty)']['yellow'] += yellow or 0
        buckets[pn or '(empty)']['red'] += red or 0
        buckets[pn or '(empty)']['total'] += total or 0
    conn.close()

    uncovered = []
    for pn, counts in buckets.items():
        y_plus_r = counts['yellow'] + counts['red']
        if y_plus_r < min_threshold:
            continue
        if is_in_whitelist(pn):
            continue
        uncovered.append({
            'product_name': pn,
            'yellow': counts['yellow'],
            'red': counts['red'],
            'total': counts['total'],
            'y_plus_r': y_plus_r,
        })
    uncovered.sort(key=lambda x: -x['y_plus_r'])
    return uncovered


def render_table(rows: list) -> str:
    if not rows:
        return '✅ 所有高频 yellow/red 品名已在 _REDSTAMP_VARIANTS 白名单,无覆盖盲区。'
    lines = ['⚠️ 以下品名 yellow+red >= 阈值但**未进 redstamp 白名单**(可能受红章污染):']
    lines.append('')
    header = '{:24s} {:>7s} {:>5s} {:>7s} {:>5s}'.format('品名', 'yellow', 'red', 'total', 'y+r')
    lines.append(header)
    lines.append('-' * 52)
    for r in rows:
        line = '  {:22s} {:<7d} {:<5d} {:<7d} {:<5d}'.format(
            r['product_name'], r['yellow'], r['red'], r['total'], r['y_plus_r'])
        lines.append(line)
    lines.append('')
    lines.append('建议:对照每个品名找 1-2 张 yellow 图人工核对 — 如确有红章污染,')
    lines.append('      在 blueprints/ocr_engine.py:_REDSTAMP_VARIANTS 加主名/子串。')
    lines.append('      加完记得:① 同步更新本脚本 REDSTAMP_WHITELIST ② 写 tests/test_xxx_redstamp_routing.py')
    return '\n'.join(lines)


def main():
    min_threshold = DEFAULT_THRESHOLD
    as_json = False
    for arg in sys.argv[1:]:
        if arg.startswith('--min='):
            min_threshold = int(arg.split('=', 1)[1])
        elif arg == '--json':
            as_json = True
        elif arg in ('-h', '--help'):
            print(__doc__)
            return

    rows = audit(min_threshold)
    if as_json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        print(render_table(rows))


if __name__ == '__main__':
    main()