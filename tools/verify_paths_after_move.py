# -*- coding: utf-8 -*-
"""迁移后校验：DB 里的 file_path 是否 (1) 落在本项目 upload/ 白名单内 (2) 物理文件存在。"""
import os
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
DB = os.path.join(PROJ, 'worklog.db')
UPLOAD_ROOT = os.path.realpath(os.path.join(PROJ, 'upload'))

db = sqlite3.connect(DB)
cur = db.cursor()

print('UPLOAD_ROOT :', UPLOAD_ROOT)
print('-' * 70)

TABLES = ['shipping_images', 'inbound_images', 'loading_order_images', 'point_count_images']

grand_ok = grand_bad = grand_miss = 0
for t in TABLES:
    try:
        cur.execute('SELECT file_path FROM "%s" WHERE file_path IS NOT NULL AND file_path <> ""' % t)
    except Exception as e:
        print('%-22s SKIP (%s)' % (t, e))
        continue
    rows = [r[0] for r in cur.fetchall()]
    bad = []      # 不在白名单 -> 删除会被拒绝
    miss = []     # 白名单通过但文件不存在
    for p in rows:
        try:
            real = os.path.realpath(p)
        except Exception:
            bad.append(p); continue
        if not real.startswith(UPLOAD_ROOT + os.sep):
            bad.append(p)
        elif not os.path.exists(p):
            miss.append(p)
    ok = len(rows) - len(bad) - len(miss)
    grand_ok += ok; grand_bad += len(bad); grand_miss += len(miss)
    print('%-22s 共 %5d | 白名单通过且存在 %5d | 越界 %3d | 缺失 %3d'
          % (t, len(rows), ok, len(bad), len(miss)))
    for p in bad[:3]:
        print('     [越界]', p)
    for p in miss[:3]:
        print('     [缺失]', p)

print('-' * 70)
print('合计: 正常 %d | 越界 %d | 缺失 %d' % (grand_ok, grand_bad, grand_miss))

# 抽样：模拟 _safe_delete_image_file 的白名单判定
print('\n抽样白名单判定（模拟 models/orders.py 逻辑）:')
cur.execute("SELECT file_path FROM shipping_images WHERE file_path LIKE '%upload%' ORDER BY id DESC LIMIT 3")
for (p,) in cur.fetchall():
    real = os.path.realpath(p)
    passed = real.startswith(UPLOAD_ROOT + os.sep)
    print('  %s  ->  %s  | exists=%s' % ('PASS' if passed else 'REJECT', real, os.path.exists(p)))

db.close()
