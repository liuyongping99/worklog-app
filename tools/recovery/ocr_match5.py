# -*- coding: utf-8 -*-
"""OCR 匹配 v5 —— 双层参考集 + 落盘全部 OCR 文本。

改进(相对 v4):
  1. 参考集两层:
     tier='ocr'  = 断链图当初存下来的 OCR 原文(长且有区分度) → 高置信
     tier='spec' = 断链图所属明细的【品名 + 规格】(100% 含数字)   → 中置信
     优先用 ocr 层匹配, ocr 层匹配不上才退到 spec 层
  2. 每张候选图的 OCR 文本都落盘(all_ocr_shard*.jsonl) → 后续换参考集重匹配
     不用再重跑 OCR(3 小时变 3 分钟)

用法: python ocr_match5.py [最多处理数] [分片号] [总分片]
输出: E:\\carve_family\\matches_shard<i>.jsonl / all_ocr_shard<i>.jsonl
"""
import io
import json
import os
import re
import sqlite3
import sys
import time
import difflib

CARVE_DIR = r'E:\carve_family'
DB = r'D:\worklog-app\worklog.db'
MAX_SIDE = 1600
PUNCT = re.compile(r'[\s,.:;，。：；、_\-/\\()（）\[\]【】"\'`|]+')

# 各层的判定门槛(比 ocr 层更宽松)
THRESH = {
    'ocr':  {'cover': 0.40, 'ratio': 0.12, 'margin': 0.04},
    'spec': {'cover': 0.50, 'ratio': 0.10, 'margin': 0.03},
}


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


def load_refs():
    """返回 (refs_ocr, refs_spec) —— 全部断链图的两种参考文本。"""
    con = sqlite3.connect(f'file:{DB}?mode=ro', uri=True)
    cur = con.cursor()

    missing = []          # (image_id, table, file_path)
    for t in ('shipping_images', 'inbound_images', 'loading_order_images'):
        rows = cur.execute(f'SELECT id, file_path, record_pk FROM {t} '
                           f'WHERE file_path LIKE "%upload%"').fetchall()
        for iid, fp, rpk in rows:
            if not fp or os.path.exists(real_path(fp)):
                continue
            missing.append((iid, t, fp, rpk))
    print(f'  断链图 {len(missing)} 张', flush=True)

    # 明细的品名 + 规格
    spec_of = {}
    for iid, t, fp, rpk in missing:
        if not rpk:
            continue
        rtab = {'shipping_images': 'shipping_records',
                'inbound_images': 'inbound_records',
                'loading_order_images': 'loading_order_records'}[t]
        r = cur.execute(f'SELECT product_name, specification FROM {rtab} WHERE id=?',
                        (rpk,)).fetchone()
        if r and (r[0] or r[1]):
            spec_of[iid] = f'{r[0] or ""} {r[1] or ""}'

    # OCR 原文
    texts_of = {}
    ids = [m[0] for m in missing]
    for k in range(0, len(ids), 500):
        chunk = ids[k:k + 500]
        q = ("SELECT image_id, ocr_text FROM ocr_match_event "
             f"WHERE image_id IN ({','.join(str(i) for i in chunk)}) "
             "AND ocr_text IS NOT NULL AND ocr_text <> ''")
        for iid, txt in cur.execute(q).fetchall():
            texts_of.setdefault(iid, []).append(txt)
    con.close()

    path_of = {m[0]: m[2] for m in missing}
    refs_ocr, refs_spec = [], []
    for iid, txts in texts_of.items():
        best_txt = max(txts, key=len)
        nb = norm(best_txt)
        refs_ocr.append({'image_id': iid, 'path': path_of[iid],
                         'name': os.path.basename(real_path(path_of[iid])),
                         'chars': set(nb), 'bigrams': bigrams(nb), 'norm': nb,
                         'text': best_txt[:200], 'kind': 'ocr'})
    for iid, sp in spec_of.items():
        nb = norm(sp)
        if len(nb) < 4:
            continue
        refs_spec.append({'image_id': iid, 'path': path_of[iid],
                          'name': os.path.basename(real_path(path_of[iid])),
                          'chars': set(nb), 'bigrams': bigrams(nb), 'norm': nb,
                          'text': sp[:200], 'kind': 'spec'})
    print(f'  参考: ocr 层 {len(refs_ocr)} 条, spec 层 {len(refs_spec)} 条', flush=True)
    return refs_ocr, refs_spec


def match_one(cn, refs, th):
    cb = bigrams(cn)
    if len(cb) < 3:
        return None
    scored = []
    for r in refs:
        if not r['bigrams']:
            continue
        cover = len(cb & r['bigrams']) / len(cb)
        if cover >= th['cover']:
            scored.append((cover, r))
    if not scored:
        return None
    scored.sort(key=lambda x: -x[0])
    finals = []
    for cover, r in scored[:12]:
        finals.append((difflib.SequenceMatcher(None, cn, r['norm']).ratio(), cover, r))
    finals.sort(key=lambda x: -x[0])
    top = finals[0]
    second = finals[1][0] if len(finals) > 1 else 0.0
    if top[0] >= th['ratio'] and (top[0] - second) >= th['margin']:
        return {'ratio': round(top[0], 3), 'cover': round(top[1], 3),
                'margin': round(top[0] - second, 3), 'ref': top[2]}
    return None


def salvage(path):
    from PIL import Image, ImageFile
    ImageFile.LOAD_TRUNCATED_IMAGES = True
    raw = open(path, 'rb').read()
    im = Image.open(io.BytesIO(raw))
    w, h = im.size
    if max(w, h) > MAX_SIDE:
        r = MAX_SIDE / max(w, h)
        im = im.resize((max(1, int(w * r)), max(1, int(h * r))))
    buf = io.BytesIO()
    im.convert('RGB').save(buf, format='JPEG', quality=92)
    return buf.getvalue()


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    shard = int(sys.argv[2]) if len(sys.argv) > 2 else 0
    shards = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    OUT = os.path.join(CARVE_DIR, f'matches_shard{shard}.jsonl')
    ALL_OCR = os.path.join(CARVE_DIR, f'all_ocr_shard{shard}.jsonl')

    refs_ocr, refs_spec = load_refs()
    cands = json.load(open(os.path.join(CARVE_DIR, 'index.json'), encoding='utf-8'))
    cands = cands[shard::shards]
    if limit:
        cands = cands[:limit]
    print(f'本分片候选 {len(cands)} 张', flush=True)

    done = set()
    if os.path.exists(ALL_OCR):
        for line in open(ALL_OCR, encoding='utf-8'):
            try:
                done.add(json.loads(line)['cand'])
            except Exception:
                pass
    todo = [c for c in cands if c['file'] not in done]
    print(f'待处理 {len(todo)} (已完成 {len(done)})', flush=True)

    import torch  # noqa: F401
    from paddleocr import PaddleOCR
    ocr = PaddleOCR(lang='ch', use_angle_cls=False, show_log=False,
                    det_db_thresh=0.15, det_db_box_thresh=0.30, use_dilation=True)
    print('PaddleOCR 已加载', flush=True)

    t0 = time.time()
    hits = 0
    with open(OUT, 'a', encoding='utf-8') as fout, open(ALL_OCR, 'a', encoding='utf-8') as fa:
        for i, c in enumerate(todo):
            path = os.path.join(CARVE_DIR, c['file'])
            try:
                small = salvage(path)
                res = ocr.ocr(small)
            except Exception:
                fa.write(json.dumps({'cand': c['file'], 'text': '', 'ok': False}) + '\n')
                continue
            texts = []
            try:
                for line in (res[0] or []):
                    if line and len(line) >= 2:
                        texts.append(line[1][0])
            except Exception:
                pass
            joined = ' '.join(texts)
            fa.write(json.dumps({'cand': c['file'], 'text': joined[:600], 'ok': True}) + '\n')
            cn = norm(joined)
            if len(cn) < 4:
                continue
            m = match_one(cn, refs_ocr, THRESH['ocr'])
            tier = 'ocr'
            if m is None:
                m = match_one(cn, refs_spec, THRESH['spec'])
                tier = 'spec'
            if m is None:
                continue
            hits += 1
            r = m['ref']
            fout.write(json.dumps({
                'cand': c['file'], 'cand_size': c['size'], 'cand_w': c['w'], 'cand_h': c['h'],
                'image_id': r['image_id'], 'orig_name': r['name'], 'orig_path': r['path'],
                'tier': tier, 'ratio': m['ratio'], 'cover': m['cover'], 'margin': m['margin'],
                'cand_ocr': joined[:250], 'ref_ocr': r['text'],
            }, ensure_ascii=False) + '\n')
            fout.flush()
            if tier == 'ocr':
                print(f'  ★[ocr] {c["file"]} → {r["name"]} 相似 {m["ratio"]:.2f}', flush=True)
            else:
                print(f'  ·[spec] {c["file"]} → {r["name"]} 相似 {m["ratio"]:.2f}', flush=True)
            if (i + 1) % 50 == 0:
                el = time.time() - t0
                print(f'   {i+1}/{len(todo)} 用时 {el:.0f}s ({el/(i+1):.2f}s/张) 命中 {hits}', flush=True)
    print(f'\n完成: {len(todo)} 张, 命中 {hits}, 用时 {time.time()-t0:.0f}s')


if __name__ == '__main__':
    main()
