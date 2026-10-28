# -*- coding: utf-8 -*-
"""交叉验证已回填的 251 张: 用 DB 里该图所属【明细的品名+规格】去核对候选 OCR 文本。

不依赖 OCR 文本匹配本身, 是一条独立证据链:
    候选图 OCR 文本  vs  DB 记录的产品名/规格  →  数字是否吻合
"""
import json
import os
import re
import sqlite3
from collections import Counter

CARVE_DIR = r'E:\carve_family'
DB = r'D:\worklog-app\worklog.db'


def norm(s):
    return re.sub(r'[\s,.:;，。：；、_\-/\\()（）\[\]【】"\'`|]+', '', (s or '')).lower()


def main():
    import sys
    pf = sys.argv[1] if len(sys.argv) > 1 else os.path.join(CARVE_DIR, 'replan.json')
    plan = json.load(open(pf, encoding='utf-8'))
    auto = [p for p in plan if p['ratio'] >= 0.50]
    con = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
    cur = con.cursor()

    def rec_of(t, rpk):
        rtab = {'shipping_images': 'shipping_records',
                'inbound_images': 'inbound_records',
                'loading_order_images': 'loading_order_records'}[t]
        return cur.execute(f'SELECT product_name, specification FROM {rtab} WHERE id=?',
                           (rpk,)).fetchone()

    ok = miss = norpk = 0
    details = []
    for p in auto:
        t = 'shipping_images'
        if 'inbound' in p['orig_path']:
            t = 'inbound_images'
        elif 'loading' in p['orig_path']:
            t = 'loading_order_images'
        row = cur.execute(f'SELECT record_pk FROM {t} WHERE file_path = ?',
                          (p['orig_path'],)).fetchone()
        if not row or not row[0]:
            norpk += 1
            continue
        r = rec_of(t, row[0])
        if not r:
            norpk += 1
            continue
        pn, sp = r
        cand = norm(p.get('text') or '')
        # 从规格里抽数字 token(0.6 / 1.2 / 70 / 100 等)
        toks = set(re.findall(r'\d+(?:\.\d+)?', norm(sp or '')))
        toks |= set(re.findall(r'\d+(?:\.\d+)?', norm(pn or '')))
        toks = {x for x in toks if len(x) >= 1}
        if not toks:
            norpk += 1
            continue
        hit = sum(1 for x in toks if x in cand)
        if hit / len(toks) >= 0.5:
            ok += 1
            details.append((os.path.basename(p['orig_path'].replace('/', '\\')), p['ratio'],
                            round(hit / len(toks), 2),
                            f"{pn}/{sp}", (p.get('text') or '')[:50]))
        else:
            miss += 1
            details.append((os.path.basename(p['orig_path'].replace('/', '\\')), p['ratio'],
                            round(hit / len(toks), 2),
                            f"{pn}/{sp}", (p.get('text') or '')[:50]))
    print(f'已回填 {len(auto)} 张 → 交叉验证: 数字吻合 {ok} / 不吻合 {miss} / 无明细可查 {norpk}')
    print(f'吻合率: {ok / max(ok + miss, 1) * 100:.1f}%')
    print('\n不吻合的样例(前 12, 供人工判断):')
    for d in [x for x in details if x[2] < 0.5][:12]:
        print(f'  ratio {d[1]} 数字吻合 {d[2]} | 记录: {d[3]} | 候选OCR: {d[4]}')
    print('\n吻合的样例(前 6):')
    for d in [x for x in details if x[2] >= 0.5][:6]:
        print(f'  ratio {d[1]} 数字吻合 {d[2]} | 记录: {d[3]} | 候选OCR: {d[4]}')


if __name__ == '__main__':
    main()
