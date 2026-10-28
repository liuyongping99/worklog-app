# -*- coding: utf-8 -*-
"""统计 DB 引用但磁盘上真缺失的图片(含路径格式脏数据修正)。"""
import os
import sqlite3
from collections import Counter

con = sqlite3.connect('file:D:/worklog-app/worklog.db?mode=ro', uri=True)
cur = con.cursor()


def real_path(fp):
    """把 'D:\\worklog-app/upload/\\2026-08\\x.jpg' 这种混合分隔符修正成真实路径。"""
    p = fp.replace('/', '\\')
    while '\\\\' in p:
        p = p.replace('\\\\', '\\')
    return p


missing = []
dirty = 0
present = 0
for t in ('shipping_images', 'inbound_images', 'loading_order_images',
          'notice_images', 'task_images', 'point_count_images'):
    cols = [c[1] for c in cur.execute(f'PRAGMA table_info({t})')]
    if 'file_path' not in cols:
        continue
    for (fp,) in cur.execute(f'SELECT file_path FROM {t} WHERE file_path LIKE "%upload%"'):
        if not fp:
            continue
        rp = real_path(fp)
        if os.path.exists(rp):
            present += 1
            if rp != fp:
                dirty += 1
        else:
            missing.append((t, fp, rp))

print('DB 引用且磁盘存在:', present, '(其中路径分隔符脏数据 %d 条, 修正后能找到)' % dirty)
print('DB 引用但磁盘真缺失:', len(missing))
mc = Counter(m[2].split('\\')[-2] if '\\' in m[2] else '?' for m in missing)
print('缺失分布(按月):', dict(mc))
tc = Counter(m[0] for m in missing)
print('缺失分布(按表):', dict(tc))
