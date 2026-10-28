# -*- coding: utf-8 -*-
"""最终校验: 已回填的文件能否被标准 JPEG 解码器正常打开(不靠容错)。"""
import os
import json
import sqlite3
from PIL import Image

CARVE_DIR = r'E:\carve_family'
KEEP_DIR = r'E:\recovered_20261028'
DB = r'D:\worklog-app\worklog.db'


def rp(fp):
    p = fp.replace('/', '\\')
    while '\\\\' in p:
        p = p.replace('\\\\', '\\')
    return p


def main():
    plan = json.load(open(os.path.join(CARVE_DIR, 'replan.json'), encoding='utf-8'))
    auto = [p for p in plan if p['ratio'] >= 0.50]
    ok = bad = missing = 0
    sizes = []
    dims = {}
    for p in auto:
        path = rp(p['orig_path'])
        if not os.path.exists(path):
            missing += 1
            continue
        try:
            with Image.open(path) as im:
                im.load()          # 严格解码: 不开 LOAD_TRUNCATED_IMAGES
                w, h = im.size
                dims[(w, h)] = dims.get((w, h), 0) + 1
            ok += 1
            sizes.append(os.path.getsize(path))
        except Exception as e:
            bad += 1
            if bad <= 5:
                print('  ! %s: %s' % (os.path.basename(path), str(e)[:70]))
    print(f'严格解码校验: 正常 {ok} / 损坏 {bad} / 不存在 {missing}')
    if sizes:
        print(f'文件大小: 最小 {min(sizes)//1024} KB, 中位 {sorted(sizes)[len(sizes)//2]//1024} KB, '
              f'最大 {max(sizes)//1024} KB')
    print('尺寸分布 top6:', sorted(dims.items(), key=lambda x: -x[1])[:6])

    # 母版与生产目录一致性
    same = diff = 0
    for p in auto:
        a = rp(p['orig_path'])
        b = os.path.join(KEEP_DIR, os.path.basename(a))
        if not os.path.exists(a) or not os.path.exists(b):
            continue
        if os.path.getsize(a) == os.path.getsize(b):
            same += 1
        else:
            diff += 1
    print(f'母版副本一致性: 一致 {same} / 不一致 {diff}')


if __name__ == '__main__':
    main()
