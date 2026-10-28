r"""upload 目录备份 —— 补上 CLAUDE.md 备份约定里最大的缺口(只备份了 .db, 没备份图片)。

背景(2026-10-28 事故):
    upload\2026-10 整个目录被误删(3,801 个文件, 其中 1,655 个仍在 DB 被引用)。
    事后排查发现四条恢复途径全部断掉:
      1) $MFT 记录被后续写入整体复用(名字+簇链全丢)
      2) USN 删除事件卡在日志轮转点之前
      3) D: 无任何卷影副本
      4) 空闲空间簇雕刻只能捞回无主图片, 且该批像素已被后续写入覆盖
    根因: 备份约定只覆盖 worklog.db, 3GB 图片从来没被备份过 —— 一次误删即无回退点。

行为:
    1. 增量复制 D:\worklog-app\upload → 目标盘(默认 E:\BAK_upload, 与源盘不同物理盘)
    2. 每天一个独立快照目录 upload_YYYYMMDD, 保留最近 N 天(默认 7)
    3. 只拷新增/变化的(按大小+内容比对), 重复文件不重复占空间
    4. 日志追加写入 D:\BAK\logs\upload_backup_YYYY-MM-DD.log

用法:
    python tools\backup_upload_dir.py                     # 手动跑一次
    python tools\backup_upload_dir.py --dst E:\BAK_upload --keep 7
    python tools\backup_upload_dir.py --verify             # 只校验快照完整性, 不写入

说明: 本脚本【不会】自己注册计划任务。要每天自动跑, 需自行加一条 Windows 计划任务
      (与现有的 FengYuan_DB_Daily_Backup 同风格, 23:30 之后执行即可)。
"""
import argparse
import filecmp
import os
import shutil
import sys
import time
import traceback
from datetime import datetime, timedelta

SRC = r"D:\worklog-app\upload"
DEFAULT_DST = r"E:\BAK_upload"
LOG_DIR = r"D:\BAK\logs"
CHUNK = 4 << 20


def log(msg):
    print(msg, flush=True)


def dir_stats(path):
    n = 0
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            n += 1
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return n, total


def copy_if_changed(src, dst):
    """同大小且内容一致 → 跳过; 否则写 .part 再原子替换。返回 True=实际拷贝。"""
    try:
        if os.path.exists(dst) and os.path.getsize(src) == os.path.getsize(dst):
            if filecmp.cmp(src, dst, shallow=False):
                return False
    except OSError:
        pass
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    tmp = dst + ".part"
    with open(src, 'rb') as fi, open(tmp, 'wb') as fo:
        while True:
            b = fi.read(CHUNK)
            if not b:
                break
            fo.write(b)
    os.replace(tmp, dst)
    return True


def prune(dst_root, keep_days):
    cutoff = datetime.now() - timedelta(days=keep_days)
    removed = []
    if not os.path.isdir(dst_root):
        return removed
    for name in os.listdir(dst_root):
        if not name.startswith('upload_'):
            continue
        try:
            stamp = datetime.strptime(name[len('upload_'):], '%Y%m%d')
        except ValueError:
            continue
        if stamp < cutoff:
            shutil.rmtree(os.path.join(dst_root, name), ignore_errors=True)
            removed.append(name)
    return removed


def verify(src_root, snap_root):
    """比对源目录与快照: 缺失/大小不一致的列出来。"""
    src_files = {}
    for root, _d, files in os.walk(src_root):
        for f in files:
            p = os.path.join(root, f)
            try:
                src_files[os.path.relpath(p, src_root)] = os.path.getsize(p)
            except OSError:
                pass
    miss = []
    diff = []
    for rel, size in src_files.items():
        p = os.path.join(snap_root, rel)
        if not os.path.exists(p):
            miss.append(rel)
        elif os.path.getsize(p) != size:
            diff.append(rel)
    return len(src_files), miss, diff


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dst', default=DEFAULT_DST)
    ap.add_argument('--keep', type=int, default=7)
    ap.add_argument('--verify', action='store_true', help='只校验最新快照, 不写入')
    ap.add_argument('--no-prune', action='store_true', help='本次不清理旧快照(纯增量, 不删除任何东西)')
    args = ap.parse_args()

    os.makedirs(LOG_DIR, exist_ok=True)
    ts = datetime.now().strftime('%Y%m%d')
    dst = os.path.join(args.dst, 'upload_%s' % ts)
    logpath = os.path.join(LOG_DIR, 'upload_backup_%s.log' % datetime.now().strftime('%Y-%m-%d'))
    lines = []

    def rec(m):
        lines.append(m)
        log(m)

    rec('=' * 64)
    rec('[%s] upload 备份%s' % (datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                                '(校验模式)' if args.verify else ''))
    rec('  SRC = %s' % SRC)
    rec('  DST = %s' % dst)

    rc = 0
    try:
        if not os.path.isdir(SRC):
            rec('  FAIL: 源目录不存在')
            rc = 1
        elif args.verify:
            if not os.path.isdir(dst):
                rec('  FAIL: 快照不存在 %s' % dst)
                rc = 1
            else:
                total, miss, diff = verify(SRC, dst)
                rec('  源 %d 个文件 | 快照缺失 %d | 大小不一致 %d' % (total, len(miss), len(diff)))
                for m in miss[:20]:
                    rec('    缺: %s' % m)
                for m in diff[:20]:
                    rec('    差异: %s' % m)
                rec('RESULT: %s' % ('OK' if not miss and not diff else 'MISMATCH'))
                rc = 0 if (not miss and not diff) else 1
        else:
            prev = None
            if os.path.isdir(args.dst):
                snaps = sorted(n for n in os.listdir(args.dst)
                               if n.startswith('upload_') and n != os.path.basename(dst))
                if snaps:
                    prev = os.path.join(args.dst, snaps[-1])
            copied = skipped = 0
            bytes_new = 0
            errs = 0
            for root, _dirs, files in os.walk(SRC):
                for f in files:
                    s = os.path.join(root, f)
                    rel = os.path.relpath(s, SRC)
                    d = os.path.join(dst, rel)
                    if prev:
                        p = os.path.join(prev, rel)
                        try:
                            if os.path.exists(p) and os.path.getsize(p) == os.path.getsize(s) \
                                    and filecmp.cmp(s, p, shallow=False):
                                skipped += 1
                                continue
                        except OSError:
                            pass
                    try:
                        if copy_if_changed(s, d):
                            copied += 1
                            bytes_new += os.path.getsize(s)
                    except OSError as e:
                        errs += 1
                        if errs <= 20:
                            rec('  ! %s: %s' % (rel, e))
            n, total = dir_stats(dst)
            rec('  新增/变化 %d 个 (%.1f MB), 未变跳过 %d 个, 错误 %d 个'
                % (copied, bytes_new / 1024 / 1024, skipped, errs))
            rec('  快照: %d 个文件 / %.1f MB' % (n, total / 1024 / 1024))
            removed = [] if args.no_prune else prune(args.dst, args.keep)
            if removed:
                rec('  清理旧快照: %s' % ', '.join(removed))
            rec('RESULT: %s' % ('OK' if errs == 0 else 'ERROR'))
            rc = 0 if errs == 0 else 1
    except Exception:
        rec('  异常:\n' + traceback.format_exc())
        rec('RESULT: ERROR')
        rc = 1

    rec('  结束时间: %s' % datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    rec('=' * 64)
    try:
        with open(logpath, 'a', encoding='utf-8') as fh:
            fh.write('\n'.join(lines) + '\n')
    except OSError as e:
        log('写日志失败: %s' % e)
    return rc


if __name__ == '__main__':
    raise SystemExit(main())
