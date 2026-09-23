"""
清理 sql/product.sql：
1. 删除文件中所有 "SET @parent_id = NULL;" 行
2. 删除文件中所有 "SELECT @parent_id := id FROM product_categories WHERE category_code = 'XXXXXX' LIMIT 1;" 行
3. 对每个 INSERT INTO product 块前置：
   SET @parent_id = NULL;
   <空行>
   SELECT @parent_id := id FROM product_categories WHERE category_code = 'XXXXXX' LIMIT 1;
   <空行>
   （XXXXXX 是该块首个 product_code 的去尾 2 位）

不处理表头/CREATE/DROP/INSERT 块之前的所有内容，原样保留。
"""
import re
from pathlib import Path

PRODUCT_SQL = Path('sql/product.sql')
lines = PRODUCT_SQL.read_text(encoding='utf-8').splitlines()

# 1. 扫描所有 INSERT 块
blocks = []  # [(start, end, parent_code, first_product_code)]
i = 0
while i < len(lines):
    if 'INSERT INTO product' in lines[i] and 'VALUES' in lines[i]:
        start = i
        j = i + 1
        first_code = None
        while j < len(lines):
            m = re.match(r"\(\s*'([0-9]+)'", lines[j])
            if m and first_code is None:
                first_code = m.group(1)
            if lines[j].rstrip().endswith(');'):
                break
            j += 1
        end = j
        if first_code:
            blocks.append((start, end, first_code[:-2], first_code))
        i = end + 1
    else:
        i += 1

print(f'找到 {len(blocks)} 个 INSERT 块')

# 2. 删除所有 SET NULL 和 SELECT 行（仅在 INSERT 块之间）
#    标记每个 INSERT 块的前置区（前一块 end+1 到本块 start）：区内的 SET/SELECT 全部删除
#    文件开头（first INSERT 之前）和文件末尾（last INSERT 之后）也按同样规则清

new_lines = []
first_block_start = blocks[0][0] if blocks else len(lines)

# 写文件开头（INSERT 块之前的所有内容，但清除里面的 SET NULL/SELECT）
for k in range(0, first_block_start):
    line = lines[k]
    if 'SET @parent_id = NULL;' in line or 'SELECT @parent_id := id FROM product_categories' in line:
        continue
    new_lines.append(line)

seen_codes = set()  # 记录已发过 SELECT 的 parent_code

for idx, (start, end, parent_code, first_code) in enumerate(blocks):
    # 在块前置区（上一块 end+1 到本块 start 之间）删除孤立 SET/SELECT
    if idx > 0:
        prev_end = blocks[idx-1][1]
        for k in range(prev_end + 1, start):
            line = lines[k]
            if 'SET @parent_id = NULL;' in line or 'SELECT @parent_id := id FROM product_categories' in line:
                continue
            new_lines.append(line)

    # 段尾空行
    if new_lines and new_lines[-1].strip() != '':
        new_lines.append('')

    # 第一次见 parent_code：写 SET NULL + SELECT
    # 后续同 code 块：不写 SET NULL、不写 SELECT（沿用之前块的 @parent_id，避免被 SET NULL 覆盖）
    if parent_code not in seen_codes:
        new_lines.append('SET @parent_id = NULL;')
        new_lines.append('')
        new_lines.append(f"SELECT @parent_id := id FROM product_categories WHERE category_code = '{parent_code}' LIMIT 1;")
        new_lines.append('')
        seen_codes.add(parent_code)
    else:
        # 加一行注释提示共用
        new_lines.append(f"-- 共用 {parent_code} 的 SELECT @parent_id（已在上一块定义）")
        new_lines.append('')

    # 写 INSERT 头 + 数据行
    for k in range(start, end + 1):
        new_lines.append(lines[k])

# 写文件末尾
last_end = blocks[-1][1]
for k in range(last_end + 1, len(lines)):
    line = lines[k]
    if 'SET @parent_id = NULL;' in line or 'SELECT @parent_id := id FROM product_categories' in line:
        continue
    new_lines.append(line)

# 写回
PRODUCT_SQL.write_text('\n'.join(new_lines) + '\n', encoding='utf-8')

# 校验
final_sql = PRODUCT_SQL.read_text(encoding='utf-8')
print()
print('=== 最终状态 ===')
print('SET @parent_id = NULL;:', sum(1 for l in final_sql.splitlines() if 'SET @parent_id = NULL;' in l))
print('SELECT @parent_id := id FROM product_categories:', sum(1 for l in final_sql.splitlines() if 'SELECT @parent_id := id FROM product_categories' in l))
print('INSERT INTO product:', sum(1 for l in final_sql.splitlines() if 'INSERT INTO product' in l))

at_count = len(re.findall(r"\(\s*'[0-9]+'\s*,\s*'[^']*'\s*,\s*@parent_id\s*,", final_sql))
print('@parent_id 数据行:', at_count)

codes = re.findall(r"WHERE category_code = '(\d+)' LIMIT 1;", final_sql)
from collections import Counter
c = Counter(codes)
print('唯一 parent_code 数:', len(set(codes)), ', 总 SELECT 数:', len(codes))
dup = [(k,v) for k,v in c.items() if v > 1]
print('重复 SELECT:', dup if dup else '(无)')

fk_match = re.search(r'FOREIGN KEY \(category_id\) REFERENCES (\w+)\((\w+)\)', final_sql)
if fk_match:
    print('FK: REFERENCES', fk_match.group(1), '(', fk_match.group(2), ')')
