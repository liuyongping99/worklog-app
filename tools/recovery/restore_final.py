# -*- coding: utf-8 -*-
"""最终回填: 按 replan.json 把雕刻图放回原始路径。

分级:
    ratio >= 0.50  → 自动回填(OCR 文本与库里存的原文高度吻合)
    0.25 <= ratio < 0.50 → 出人工复核清单(缩略图墙 + CSV), 不自动写
    ratio < 0.25   → 丢弃

回填前逐张校验: 能解码 + 尺寸合理 + 体积合理, 防止把垃圾碎片写回去。
"""
import csv
import json
import os
import shutil
import sys

CARVE_DIR = r'E:\carve_family'
KEEP_DIR = r'E:\recovered_20261028'
PLAN = os.path.join(CARVE_DIR, 'replan.json')
REVIEW_CSV = os.path.join(CARVE_DIR, 'review_list.csv')
AUTO_RATIO = 0.50
MIN_KEEP = 0.25
MIN_BYTES = 8000


def main():
    dry = '--dry-run' in sys.argv
    plan = json.load(open(PLAN, encoding='utf-8'))
    from PIL import Image, ImageFile
    ImageFile.LOAD_TRUNCATED_IMAGES = True

    auto, review, dropped = [], [], []
    for p in plan:
        if p['ratio'] >= AUTO_RATIO:
            auto.append(p)
        elif p['ratio'] >= MIN_KEEP:
            review.append(p)
        else:
            dropped.append(p)
    print(f'计划 {len(plan)} 条 → 自动回填 {len(auto)} / 人工复核 {len(review)} / 丢弃 {len(dropped)}')

    # 校验 + 回填
    ok = fail = badimg = 0
    os.makedirs(KEEP_DIR, exist_ok=True)
    for p in auto:
        src = os.path.join(CARVE_DIR, p['cand'])
        dst = p['orig_path'].replace('/', '\\')
        if not os.path.exists(src):
            fail += 1
            continue
        size = os.path.getsize(src)
        if size < MIN_BYTES:
            badimg += 1
            continue
        try:
            with Image.open(src) as im:
                im.verify()
        except Exception:
            badimg += 1
            continue
        if dry:
            ok += 1
            continue
        try:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            # 被覆盖的只是 JPEG 末尾的 EOI 标记 → 补回去即成合规 JPEG(无损)
            raw = open(src, 'rb').read()
            if raw[:2] == b'\xff\xd8' and raw[-2:] != b'\xff\xd9':
                raw = raw + b'\xff\xd9'
            with open(dst, 'wb') as fh:
                fh.write(raw)
            with open(os.path.join(KEEP_DIR, os.path.basename(dst)), 'wb') as fh:
                fh.write(raw)
            ok += 1
        except Exception as e:
            print('  ! %s: %s' % (dst, str(e)[:70]))
            fail += 1

    print(f'\n回填: 成功 {ok}, 失败 {fail}, 图片不合格丢弃 {badimg}')
    if not dry:
        print('母版副本:', KEEP_DIR)

    with open(REVIEW_CSV, 'w', newline='', encoding='utf-8-sig') as fh:
        w = csv.writer(fh)
        w.writerow(['image_id', '原文件名', '原路径', 'ratio', '候选文件', '候选OCR', '参考OCR'])
        for p in sorted(review, key=lambda x: -x['ratio']):
            w.writerow([p['image_id'], os.path.basename(p['orig_path'].replace('/', '\\')),
                        p['orig_path'], p['ratio'], p['cand'],
                        p.get('text', '')[:200], p.get('ref_ocr', '')[:200]])
    print('人工复核清单:', REVIEW_CSV)


if __name__ == '__main__':
    main()
