# -*- coding: utf-8 -*-
"""离线重匹配: 用已落盘的 OCR 文本(all_ocr_shard*.jsonl)重新对号, 不重跑 OCR。

用途: 放宽阈值 / 换参考集 / 换分配策略, 都是秒级的事。
"""
import difflib
import glob
import json
import os
import sqlite3
import sys

CARVE_DIR = r'E:\carve_family'
DB = r'D:\worklog-app\worklog.db'
PUNCT = None

import re
PUNCT = re.compile(r'[\s,.:;，。：；、_\-/\\()（）\[\]【】"\'`|]+')


def norm(s):
    return PUNCT.sub('', (s or '').lower())


def bigrams(s):
    if len(s) < 2:
        return {s} if s else set()
    return {s[i:i + 2] for i in range(len(s) - 1)}


def real_path(fp):
    p = fp.replace('/', '\\')
    while '\\\\' in p:
        p = p.replace('\\\\', '\\')
    return p


def load_refs(min_len=4):
    con = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
    cur = con.cursor()
    info = {}
    for t in ('shipping_images', 'inbound_images', 'loading_order_images'):
        rows = cur.execute(f'SELECT id, file_path, created_at FROM {t} '
                           f'WHERE file_path LIKE "%upload%"').fetchall()
        for iid, fp, created in rows:
            if not fp or os.path.exists(real_path(fp)):
                continue
            info[iid] = (fp, created or '')
    texts = {}
    ids = list(info)
    for k in range(0, len(ids), 500):
        chunk = ids[k:k + 500]
        q = ("SELECT image_id, ocr_text FROM ocr_match_event "
             f"WHERE image_id IN ({','.join(str(i) for i in chunk)}) "
             "AND ocr_text IS NOT NULL AND ocr_text <> ''")
        for iid, txt in cur.execute(q).fetchall():
            if iid in info:
                # 同一张图可能有多条 OCR 事件且文本相同 → 组内 image_id 必须去重
                texts.setdefault(txt, [])
                if iid not in texts[txt]:
                    texts[txt].append(iid)
    con.close()

    # 文本 → image_id 列表
    groups = {}
    for txt, iids in texts.items():
        nb = norm(txt)
        if len(nb) < min_len:
            continue
        groups[txt] = sorted(((i, info[i][0], info[i][1]) for i in iids),
                              key=lambda x: x[2])
    # 扁平参考(每个 image_id 一条, 带文本 bigram)
    flat = []
    for txt, imgs in groups.items():
        nb = norm(txt)
        flat.append({'text': txt, 'norm': nb, 'bigrams': bigrams(nb),
                     'imgs': imgs})
    return groups, flat


def main():
    cover_min = float(sys.argv[1]) if len(sys.argv) > 1 else 0.32
    ratio_min = float(sys.argv[2]) if len(sys.argv) > 2 else 0.09
    margin_min = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0

    groups, flat = load_refs()
    print(f'参考文本组 {len(groups)}, 覆盖缺失图 {sum(len(v) for v in groups.values())} 张')

    cands = []
    for p in glob.glob(os.path.join(CARVE_DIR, 'all_ocr_shard*.jsonl')):
        for line in open(p, encoding='utf-8'):
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except Exception:
                continue
            if d.get('ok') and d.get('text'):
                cands.append(d)
    print(f'已 OCR 候选 {len(cands)} 张')

    # 逐候选找最佳参考文本
    best_for = {}
    for i, c in enumerate(cands):
        cn = norm(c['text'])
        cb = bigrams(cn)
        if len(cb) < 3:
            continue
        scored = []
        for r in flat:
            if not r['bigrams']:
                continue
            cov = len(cb & r['bigrams']) / len(cb)
            if cov >= cover_min:
                scored.append((cov, r))
        if not scored:
            continue
        scored.sort(key=lambda x: -x[0])
        finals = []
        for cov, r in scored[:15]:
            finals.append((difflib.SequenceMatcher(None, cn, r['norm']).ratio(), cov, r))
        finals.sort(key=lambda x: -x[0])
        top = finals[0]
        second = finals[1][0] if len(finals) > 1 else 0.0
        if top[0] >= ratio_min and (top[0] - second) >= margin_min:
            best_for.setdefault(top[2]['text'], []).append(
                {'cand': c['cand'], 'ratio': round(top[0], 3), 'cover': round(top[1], 3),
                 'margin': round(top[0] - second, 3), 'text': c['text'][:200]})
        if (i + 1) % 1000 == 0:
            print(f'  已匹配 {i+1}/{len(cands)}', flush=True)

    print(f'\n有候选的文本组: {len(best_for)}')
    plan = []
    for txt, imgs in groups.items():
        cl = sorted(best_for.get(txt, []), key=lambda x: -x['ratio'])
        if not cl:
            continue
        for i, (iid, fp, created) in enumerate(imgs):
            if i >= len(cl):
                break
            plan.append({'image_id': iid, 'orig_path': fp, **cl[i],
                         'group_size': len(imgs), 'group_idx': i, 'ref_ocr': txt})
    print(f'★ 可回填: {len(plan)} 张缺失图片')
    bm = {}
    for p in plan:
        m = p['orig_path'].replace('/', '\\').split('\\')[-2]
        bm[m] = bm.get(m, 0) + 1
    print('  按月:', bm)
    json.dump(plan, open(os.path.join(CARVE_DIR, 'replan.json'), 'w', encoding='utf-8'),
              ensure_ascii=False, indent=1)
    print('  方案:', os.path.join(CARVE_DIR, 'replan.json'))


if __name__ == '__main__':
    main()
