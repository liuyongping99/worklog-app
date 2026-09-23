"""
批量改写 sql/product.sql 中所有 INSERT INTO product 的 category_id 字段。

规则：product_code 去尾 2 位（即 product_code[:-2]）→ 查 product_categories 表
      中对应 code 的 id → 用 SELECT @parent_id 形式。
      product_code 长度不固定：8 位 → 截 6 位；6 位 → 截 4 位。
对照源：worklog.db 的 product_categories 表（生产库）。
"""
import re
import sqlite3
from pathlib import Path

PRODUCT_SQL = Path('sql/product.sql')
DB_PATH = Path('worklog.db')

# 1. 读生产库 product_categories 表
con = sqlite3.connect(DB_PATH)
cur = con.cursor()
cur.execute("SELECT category_code, id FROM product_categories")
cat_map = {row[0]: str(row[1]) for row in cur.fetchall()}
con.close()
print(f'生产库 product_categories 总数: {len(cat_map)}')

# 2. 读 product.sql
lines = PRODUCT_SQL.read_text(encoding='utf-8').splitlines()

# 3. 一次性重建
new_lines = []
hits = 0
missing_codes = set()
warnings = []
i = 0
while i < len(lines):
    line = lines[i]
    if 'INSERT INTO product' in line and 'VALUES' in line:
        start = i
        j = i + 1
        rows = []
        while j < len(lines):
            rl = lines[j]
            m = re.match(r"\(\s*'([0-9]+)'\s*,\s*'([^']*)'\s*,\s*(\d+|@parent_id)\s*,", rl)
            if m:
                rows.append((j, m.group(1), m.group(2), m.group(3)))
            if rl.rstrip().endswith(');'):
                break
            j += 1
        end = j

        if rows:
            # 新规则：去尾 2 位
            full_code = rows[0][1]
            parent_code = full_code[:-2]
            expected = cat_map.get(parent_code)

            if expected is None:
                missing_codes.add(parent_code)
                for k in range(start, end + 1):
                    new_lines.append(lines[k])
                i = end + 1
                continue

            new_lines.append(
                f"SELECT @parent_id := id FROM product_categories "
                f"WHERE category_code = '{parent_code}' LIMIT 1;"
            )
            cur_cats = {r[3] for r in rows}
            if cur_cats != {expected} and cur_cats != {'@parent_id'}:
                warnings.append((parent_code, rows[0][2], start + 1, end + 1,
                                 f'原 cat_id 与规则值 {expected} 不一致，按规则已改写'))
            hits += 1
            for k in range(start, end + 1):
                rl = lines[k]
                mm = re.match(r"(\(\s*'[0-9]+'\s*,\s*'[^']*'\s*,\s*)(\d+|@parent_id)(\s*,.*)", rl)
                if mm and mm.group(2) != '@parent_id':
                    rl = mm.group(1) + '@parent_id' + mm.group(3)
                new_lines.append(rl)
            i = end + 1
            continue

        for k in range(start, end + 1):
            new_lines.append(lines[k])
        i = end + 1
    else:
        new_lines.append(line)
        i += 1

PRODUCT_SQL.write_text('\n'.join(new_lines) + '\n', encoding='utf-8')

print(f'\n=== 已改写 {hits} 个 INSERT 块（走 SELECT @parent_id）===')
print(f'=== 缺失 parent_code（生产表找不到）: {sorted(missing_codes)} ===')
print(f'\n警告 {len(warnings)} 个：')
for w in warnings[:20]:
    print(f'  code={w[0]} 名称={w[1]} 行{w[2]}-{w[3]} {w[4]}')
if len(warnings) > 20:
    print(f'  ... 还有 {len(warnings)-20} 个未显示')
