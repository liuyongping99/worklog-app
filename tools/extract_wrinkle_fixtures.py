"""一次性 fixture 抽取脚本:从近 30 天 yellow/red 的磅布三文治图片中筛高价值样本。

用法:
    python tools/extract_wrinkle_fixtures.py            # 干跑(只显示将复制哪些文件)
    python tools/extract_wrinkle_fixtures.py --execute  # 实际执行(复制)

**安全保证**:脚本只读 db + cp 文件,**不 UPDATE/DELETE**。
"""
import argparse
import json
import os
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = ROOT / 'tests' / 'fixtures' / 'wrinkle_labels'


def find_candidates(conn) -> list:
    """查近 30 天 yellow/red 且品名含「磅布三文治」的图片。"""
    cur = conn.cursor()
    cur.execute("""
        SELECT si.id, si.file_path AS image_path, sr.product_name, sr.specification,
               si.match_status, si.match_score, si.reason AS match_reason
        FROM shipping_images si
        JOIN shipping_records sr ON si.record_pk = sr.id
        WHERE si.match_status IN ('yellow', 'red')
          AND si.created_at >= datetime('now', '-30 days')
          AND sr.product_name LIKE '%磅布三文治%'
        ORDER BY si.id DESC
    """)
    return [dict(row) for row in cur.fetchall()]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--execute', action='store_true', help='实际执行(默认干跑)')
    args = parser.parse_args()

    os.environ.setdefault('WORKLOG_DB', str(ROOT / 'worklog.db'))
    sys.path.insert(0, str(ROOT))
    from models._db import get_db
    conn = get_db()

    candidates = find_candidates(conn)
    conn.close()

    print(f'找到 {len(candidates)} 张候选 fixture')

    if not args.execute:
        print('干跑模式:不复制文件。运行 `python tools/extract_wrinkle_fixtures.py --execute` 实际执行。')
        for c in candidates[:5]:
            print(f"  - {c['product_name']} ({c['match_status']}) -> {c['image_path']}")
        if len(candidates) > 5:
            print(f'  ... 共 {len(candidates)} 张')
        return 0

    FIXTURES_DIR.mkdir(parents=True, exist_ok=True)
    copied = 0
    skipped = 0
    for c in candidates:
        src = Path(c['image_path'])
        if not src.exists():
            # 路径可能是相对路径(相对项目根)
            src = ROOT / c['image_path']
        if not src.exists():
            print(f'  跳过(源文件不存在): {src}', file=sys.stderr)
            skipped += 1
            continue
        dst = FIXTURES_DIR / src.name
        meta_dst = FIXTURES_DIR / (src.stem + '.json')
        shutil.copy2(src, dst)
        meta_dst.write_text(json.dumps(c, ensure_ascii=False, indent=2),
                            encoding='utf-8')
        copied += 1

    print(f'复制完成: {copied} 张,跳过 {skipped} 张 -> {FIXTURES_DIR}')
    return 0


if __name__ == '__main__':
    sys.exit(main())