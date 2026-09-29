# -*- coding: utf-8 -*-
"""工作目录迁移后：把 DB 里存量的旧绝对路径 C:\\Users\\Administrator\\worklog-app\\
统一替换为新根目录（默认 D:\\WORKLOG-APP\\）。

用法：
  python tools/migrate_paths.py            # 只扫描（dry-run），打印将受影响的行数
  python tools/migrate_paths.py --apply    # 执行替换
"""
import os
import sys
import sqlite3
import shutil
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
DB = os.path.join(PROJ, 'worklog.db')

OLD_ROOT = r'C:\Users\Administrator\worklog-app'
NEW_ROOT = PROJ                      # 新根目录 = 当前项目所在目录
OLD_SEP = OLD_ROOT + '\\'
NEW_SEP = NEW_ROOT + '\\'
# 正斜杠变体（少数记录可能用 / 分隔）
OLD_FWD = OLD_ROOT.replace('\\', '/') + '/'
NEW_FWD = NEW_ROOT.replace('\\', '/') + '/'


def scan(cur):
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [r[0] for r in cur.fetchall()]
    hits = []
    for t in tables:
        cur.execute('PRAGMA table_info(%s)' % t)
        for (_, col, *_rest) in cur.fetchall():
            try:
                cur.execute(
                    'SELECT COUNT(*) FROM "%s" WHERE CAST("%s" AS TEXT) LIKE ?'
                    % (t, col), ('%' + OLD_ROOT + '%',))
                n = cur.fetchone()[0]
                if n:
                    hits.append((t, col, n))
            except Exception:
                pass
    return hits


def main():
    apply = '--apply' in sys.argv
    print('DB     :', DB)
    print('OLD    :', OLD_ROOT)
    print('NEW    :', NEW_ROOT)
    print('MODE   :', 'APPLY' if apply else 'DRY-RUN')
    print('-' * 60)

    db = sqlite3.connect(DB)
    cur = db.cursor()
    hits = scan(cur)
    if not hits:
        print('未发现旧绝对路径，无需处理。')
        db.close()
        return

    print('受影响 表.列 (行数):')
    total = 0
    for t, c, n in hits:
        print('  %-28s %-18s %d' % (t, c, n))
        total += n
    print('-' * 60)
    print('合计行数:', total)

    if not apply:
        print('\n[DRY-RUN] 未做任何修改。加 --apply 执行。')
        db.close()
        return

    # 备份
    bakdir = r'D:\BAK'
    os.makedirs(bakdir, exist_ok=True)
    bak = os.path.join(bakdir, 'worklog_%s_prepathmig.db' % datetime.now().strftime('%Y%m%d_%H%M'))
    shutil.copy2(DB, bak)
    print('已备份 ->', bak, '(%d bytes)' % os.path.getsize(bak))

    changed_total = 0
    for t, c, _n in hits:
        # 反斜杠 与 正斜杠 两种变体都替换
        cur.execute(
            'UPDATE "%s" SET "%s" = REPLACE(CAST("%s" AS TEXT), ?, ?) '
            'WHERE CAST("%s" AS TEXT) LIKE ?' % (t, c, c, c),
            (OLD_SEP, NEW_SEP, '%' + OLD_SEP + '%'))
        n1 = cur.rowcount
        cur.execute(
            'UPDATE "%s" SET "%s" = REPLACE(CAST("%s" AS TEXT), ?, ?) '
            'WHERE CAST("%s" AS TEXT) LIKE ?' % (t, c, c, c),
            (OLD_FWD, NEW_FWD, '%' + OLD_FWD + '%'))
        n2 = cur.rowcount
        # 兜底：仅根目录（无尾斜杠）的情况
        cur.execute(
            'UPDATE "%s" SET "%s" = REPLACE(CAST("%s" AS TEXT), ?, ?) '
            'WHERE CAST("%s" AS TEXT) LIKE ?' % (t, c, c, c),
            (OLD_ROOT, NEW_ROOT, '%' + OLD_ROOT + '%'))
        print('  %-28s %-18s -> %d 行' % (t, c, n1 + n2))
        changed_total += n1 + n2
    db.commit()

    # 复核
    left = scan(cur)
    print('-' * 60)
    print('已修改行数:', changed_total)
    print('残留旧路径:', left if left else '无')
    db.close()


if __name__ == '__main__':
    main()
