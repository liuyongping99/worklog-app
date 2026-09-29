# -*- coding: utf-8 -*-
"""把 WorkBuddy 客户端状态库里的旧工作区路径对齐到新目录。

作用对象：C:\\Users\\Administrator\\.workbuddy\\workbuddy.db
  - workspaces 表：补上 D:\\worklog-app 并置顶（客户端「最近工作区」用）
  - sessions 表  ：把 cwd 为旧 C 盘路径的会话改指到 D:\\worklog-app

用法：
  python tools/fix_workbuddy_workspace.py            # dry-run，只打印
  python tools/fix_workbuddy_workspace.py --apply    # 执行（先自动备份）
"""
import os
import sys
import shutil
import sqlite3
import time
from datetime import datetime

DB = r'C:\Users\Administrator\.workbuddy\workbuddy.db'
OLD_CWD = r'c:\Users\Administrator\worklog-app'
NEW_CWD = r'D:\worklog-app'          # 与 16a36028 会话已有取值保持同形
BACKUP_DIR = r'D:\BAK'


def main():
    apply = '--apply' in sys.argv
    print('DB      :', DB)
    print('OLD cwd :', OLD_CWD)
    print('NEW cwd :', NEW_CWD)
    print('MODE    :', 'APPLY' if apply else 'DRY-RUN')
    print('-' * 70)

    db = sqlite3.connect(DB, timeout=15)
    cur = db.cursor()

    cur.execute("SELECT COUNT(*) FROM sessions WHERE lower(cwd) = lower(?)", (OLD_CWD,))
    n_sess = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM workspaces WHERE lower(path) = lower(?)", (NEW_CWD,))
    has_ws = cur.fetchone()[0]

    print('sessions 绑定旧路径 :', n_sess, '行')
    print('workspaces 已有新路径:', '是' if has_ws else '否')
    print('-' * 70)

    if not apply:
        print('[DRY-RUN] 未修改。加 --apply 执行。')
        db.close()
        return

    os.makedirs(BACKUP_DIR, exist_ok=True)
    bak = os.path.join(BACKUP_DIR, 'workbuddy_db_%s.db' % datetime.now().strftime('%Y%m%d_%H%M'))
    # WAL 模式下用 VACUUM INTO 拿到一致快照
    try:
        db.execute('VACUUM INTO ?', (bak,))
    except Exception:
        db.close()
        shutil.copy2(DB, bak)
        db = sqlite3.connect(DB, timeout=15)
        cur = db.cursor()
    print('已备份 ->', bak, '(%d bytes)' % os.path.getsize(bak))

    # 1) sessions.cwd 对齐
    cur.execute("UPDATE sessions SET cwd = ? WHERE lower(cwd) = lower(?)", (NEW_CWD, OLD_CWD))
    print('  sessions.cwd 更新 :', cur.rowcount, '行')

    # 2) workspaces 补新路径并置顶
    now_ms = int(time.time() * 1000)
    if has_ws:
        cur.execute("UPDATE workspaces SET last_opened_at = ? WHERE lower(path) = lower(?)", (now_ms, NEW_CWD))
    else:
        cur.execute("INSERT INTO workspaces(path, last_opened_at) VALUES(?, ?)", (NEW_CWD, now_ms))
    print('  workspaces   更新 :', cur.rowcount, '行')

    db.commit()

    print('-' * 70)
    print('复核 —— sessions cwd 分布:')
    for cwd, n in cur.execute('SELECT cwd, COUNT(*) FROM sessions GROUP BY cwd ORDER BY 2 DESC'):
        print('   %-45s %d' % (cwd, n))
    print('复核 —— workspaces:')
    for p, ts in cur.execute('SELECT path, last_opened_at FROM workspaces ORDER BY last_opened_at DESC'):
        print('   %-45s %s' % (p, datetime.fromtimestamp(ts / 1000).strftime('%Y-%m-%d %H:%M:%S')))
    db.close()


if __name__ == '__main__':
    main()
