"""按 5 个 commit 阶段切分 /tmp/full.patch。

每个阶段用一个独立 patch 文件,只包含该阶段的 hunk。git apply --cached + commit。
"""
import os
import re
import subprocess

PATCH = 'C:/Users/Administrator/AppData/Local/Temp/full.patch'
SPLIT_DIR = 'C:/Users/Administrator/AppData/Local/Temp/split_patches'
os.makedirs(SPLIT_DIR, exist_ok=True)

# 读全 patch,按 file 分组,每个 file 内按 hunk 切
with open(PATCH, encoding='utf-8') as f:
    content = f.read()

# Split by file
file_blocks = re.split(r'(?=^diff --git )', content, flags=re.MULTILINE)
file_blocks = [b for b in file_blocks if b.strip()]

# 每个 commit 包含的文件+hunk 列表
# 编号对应 hunk 索引
COMMITS = {
    1: {
        'name': 'refactor 列(eco+match+tds)',
        'files_hunks': {
            'blueprints/shipping.py': [1, 2, 3],   # has_eco + has_match + record_lookup start
            'templates/shipping-records.html': [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13],
        }
    },
    2: {
        'name': 'feat reason hover (DB+UI)',
        'files_hunks': {
            'models/_init.py': [1],  # reason col
            'models/orders.py': [1],  # set_match reason
            'blueprints/shipping.py': [4],  # best_reason_map + ai-match reason
            'templates/shipping-records.html': [],  # title attr 在大 hunk 里
        }
    },
    3: {
        'name': 'feat RapidFuzz reason',
        'files_hunks': {
            'blueprints/_helpers.py': [1, 2],  # match_label_to_row reason
            'blueprints/shipping.py': [5, 6],  # _run_label_match + upload
            'tests/test_label_match.py': [1],  # reason field tests
        }
    },
    4: {
        'name': 'feat 人工确认 + 叠加结构 + 行级联动',
        'files_hunks': {
            'models/_init.py': [2],  # human_verified col
            'models/orders.py': [2],  # set_human_verified
            'blueprints/shipping.py': [7, 8, 9],  # manual-verify + record_lookup + best status human_verified
            'templates/shipping-records.html': [],  # img-photo + manual confirm + refresh in big hunks
        }
    },
    5: {
        'name': 'fix .shipping-page wrapper',
        'files_hunks': {
            'templates/shipping-records.html': [],  # 2 lines, in hunk 1
        }
    },
}


def parse_hunks(file_block):
    """返回 [(header_line, content), ...] 列表"""
    lines = file_block.split('\n')
    # 前 5 行是 diff --git / index / --- / +++
    header_end = 0
    for i, line in enumerate(lines):
        if line.startswith('@@'):
            header_end = i
            break
    header = '\n'.join(lines[:header_end]) + '\n'
    body = '\n'.join(lines[header_end:])
    # 按 @@ 切
    hunks = re.split(r'(?=^@@ )', body, flags=re.MULTILINE)
    hunks = [h for h in hunks if h.strip()]
    return header, hunks


def main():
    # 按 file 切,记录每 file 的 hunks
    file_hunks = {}
    for block in file_blocks:
        m = re.search(r'^diff --git a/(\S+)', block, re.MULTILINE)
        if not m:
            continue
        path = m.group(1)
        header, hunks = parse_hunks(block)
        file_hunks[path] = (header, hunks)

    # 为每个 commit 生成 patch
    for n, info in COMMITS.items():
        out = []
        for path, indices in info['files_hunks'].items():
            if path not in file_hunks:
                continue
            header, hunks = file_hunks[path]
            selected = [hunks[i-1] for i in indices if 0 < i <= len(hunks)]
            if not selected:
                continue
            out.append(header + '\n'.join(selected) + '\n')
        out_path = f'{SPLIT_DIR}/commit_{n}.patch'
        with open(out_path, 'w', encoding='utf-8') as f:
            f.write('\n'.join(out))
        n_hunks = sum(len(v[1]) for v in [file_hunks.get(p, (None, [])) for p in info['files_hunks']])
        print(f'Commit {n}: {info["name"]} -> {out_path} ({len(out)} file blocks)')

    # 列所有 file 的 hunks 供调试
    print('\n=== file -> hunk count ===')
    for path, (header, hunks) in file_hunks.items():
        print(f'  {path}: {len(hunks)} hunks')


if __name__ == '__main__':
    main()