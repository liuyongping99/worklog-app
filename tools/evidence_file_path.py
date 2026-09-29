# -*- coding: utf-8 -*-
"""对照证据：C 盘原始 DB（未被本次迁移改动） vs D 盘 DB（改写后）的 file_path 样本。"""
import sqlite3

def dump(label, dbpath):
    print('=' * 78)
    print(label, '\n  DB:', dbpath)
    try:
        db = sqlite3.connect('file:%s?mode=ro' % dbpath.replace('\\', '/'), uri=True)
    except Exception as e:
        print('  打开失败:', e); return
    cur = db.cursor()
    for t in ('shipping_images', 'inbound_images', 'loading_order_images'):
        print('  --- %s ---' % t)
        # 绝对 vs 相对 计数
        cur.execute("SELECT COUNT(*) FROM %s WHERE file_path LIKE '_:\\%%'" % t)
        n_abs = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM %s WHERE file_path LIKE 'upload/%%' OR file_path LIKE 'upload\\%%'" % t)
        n_rel = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM %s" % t)
        n_all = cur.fetchone()[0]
        print('      绝对路径(盘符:\\\\): %d | 相对路径(upload/..): %d | 其他: %d | 总行: %d'
              % (n_abs, n_rel, n_all - n_abs - n_rel, n_all))
        cur.execute("SELECT id, file_path FROM %s ORDER BY id LIMIT 2" % t)
        for i, p in cur.fetchall():
            print('      id=%-6s %s' % (i, p))
        cur.execute("SELECT id, file_path FROM %s WHERE file_path LIKE 'upload%%' ORDER BY id LIMIT 2" % t)
        for i, p in cur.fetchall():
            print('      [相对] id=%-6s %s' % (i, p))
    db.close()

dump('【A】C 盘原始副本（迁移前状态，未被改写）', r'C:\Users\Administrator\worklog-app\worklog.db')
dump('【B】D 盘当前 DB（已执行路径改写）', r'D:\WORKLOG-APP\worklog.db')
