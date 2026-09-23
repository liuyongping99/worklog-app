"""
把 sql/product.sql 从 MySQL 语法转换为 SQLite 语法：
1. CREATE TABLE:
   - BIGINT → INTEGER
   - AUTO_INCREMENT → AUTOINCREMENT
   - COMMENT '...' 列注释 → 删除
   - UNIQUE KEY uk_xxx (col) → 删除（独立 CREATE UNIQUE INDEX）
   - INDEX idx_xxx (col) → 删除（独立 CREATE INDEX）
   - FOREIGN KEY (col) REFERENCES tbl(col) → 保留（SQLite 支持）
   - ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='...' → 整行删除
2. SET @parent_id = NULL; → 删除
3. SELECT @parent_id := id FROM product_categories WHERE category_code = 'XXXX' LIMIT 1;
   → 删除（不依赖 MySQL 变量）
4. -- 共用 XXX 的 SELECT @parent_id ... → 删除
5. INSERT INTO product (col1, ...) VALUES
   ('xxx', 'yyy', @parent_id, ...)
   →
   INSERT INTO product (col1, ...) SELECT 'xxx', 'yyy',
     (SELECT id FROM product_categories WHERE category_code = 'XXXX')  -- XXXX = 'xxx' 去尾 2 位
     ...
"""
import re
from pathlib import Path

SRC = Path('sql/product.sql')
DST = Path('sql/product_sqlite.sql')

src_lines = SRC.read_text(encoding='utf-8').splitlines()
out_lines = []

i = 0
while i < len(src_lines):
    line = src_lines[i]

    # 跳过 SET @parent_id = NULL;
    if 'SET @parent_id = NULL;' in line:
        i += 1
        continue

    # 跳过 SELECT @parent_id ... 行
    if 'SELECT @parent_id := id FROM product_categories' in line:
        i += 1
        continue

    # 跳过 "共用 XXX 的 SELECT @parent_id" 注释行
    if line.strip().startswith('--') and '共用' in line and 'SELECT @parent_id' in line:
        i += 1
        continue

    # CREATE TABLE 块内的特殊处理
    if 'CREATE TABLE product' in line:
        # 累积到行尾 ');'（注意源文件第 42 行以 ';' 结尾但不是 ');'，
        # 所以同时检测 ENGINE 行或 ';' 后跟空行）
        table_block = [line]
        j = i + 1
        while j < len(src_lines):
            table_block.append(src_lines[j])
            rstripped = src_lines[j].rstrip()
            # CREATE TABLE 结束条件：含 ');'，或 ';' 结尾且下一行是空行/注释
            if rstripped.endswith(');'):
                break
            if rstripped.endswith(';') and (
                j + 1 >= len(src_lines)
                or src_lines[j + 1].strip() == ''
                or src_lines[j + 1].strip().startswith('--')
            ):
                break
            j += 1

        # 处理 table_block 每一行
        prev_was_kept = True  # 上一行是否被保留（用来处理末尾逗号）
        kept_lines = []
        for tb_line in table_block:
            new_line = tb_line
            # 跳过纯空行
            if new_line.strip() == '':
                prev_was_kept = False
                continue
            # BIGINT → INTEGER
            new_line = re.sub(r'\bBIGINT\b', 'INTEGER', new_line)
            # AUTO_INCREMENT → AUTOINCREMENT
            new_line = re.sub(r'\bAUTO_INCREMENT\b', 'AUTOINCREMENT', new_line)
            # ON UPDATE CURRENT_TIMESTAMP → 删（SQLite 不支持）
            new_line = re.sub(r'\s+ON\s+UPDATE\s+CURRENT_TIMESTAMP', '', new_line)
            # 去掉列尾的 COMMENT '...'
            new_line = re.sub(r"\s*COMMENT\s+'[^']*'", '', new_line)
            # UNIQUE KEY uk_xxx (col) → 删（在表外独立 CREATE）
            if re.match(r'\s*UNIQUE KEY\s+\w+\s*\(', new_line):
                prev_was_kept = False
                continue
            # INDEX idx_xxx (col) → 删（在表外独立 CREATE）
            if re.match(r'\s*INDEX\s+\w+\s*\(', new_line):
                prev_was_kept = False
                continue
            # ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='...' → 保留 ')'，删其他
            if 'ENGINE=' in new_line or 'DEFAULT CHARSET' in new_line:
                m_close = re.match(r'^(\s*\))', new_line)
                if m_close:
                    kept_lines.append(m_close.group(1))
                    prev_was_kept = True
                else:
                    prev_was_kept = False
                continue
            # 如果上一行被删，且当前行不是 ')'：去掉当前行尾的逗号
            if not prev_was_kept and not new_line.rstrip().endswith(');'):
                new_line = new_line.rstrip()
                if new_line.endswith(','):
                    new_line = new_line[:-1]
            kept_lines.append(new_line)
            prev_was_kept = True
        out_lines.extend(kept_lines)

        # 表建完后，加 CREATE INDEX
        out_lines.append('CREATE UNIQUE INDEX uk_product_code ON product(product_code);')
        out_lines.append('CREATE INDEX idx_product_category ON product(category_id);')

        i = j + 1
        continue

    # DROP TABLE IF EXISTS product; 保留（兼容 SQLite）
    # FOREIGN KEY 行保留（SQLite 支持）

    # INSERT INTO product 行 + 数据行
    if 'INSERT INTO product' in line and 'VALUES' in line:
        # 找到数据行结束（行末为 ');'）
        insert_block = [line]
        j = i + 1
        first_product_code = None
        while j < len(src_lines):
            insert_block.append(src_lines[j])
            # 取首个 product_code
            if first_product_code is None:
                m = re.match(r"\(\s*'([0-9]+)'", src_lines[j])
                if m:
                    first_product_code = m.group(1)
            if src_lines[j].rstrip().endswith(');'):
                break
            j += 1

        # 转换 INSERT 头
        header = insert_block[0]
        # 把 @parent_id 占位 → 子查询 (SELECT id FROM product_categories WHERE category_code='XXXX')
        # 但每个数据行的 product_code 不同（8 位），不能用一个 parent_code 替换所有
        # → 用各行的 product_code 去尾 2 位查
        # INSERT INTO product (...) VALUES
        #   ('01010101', ..., @parent_id, ...),
        #   ('01010102', ..., @parent_id, ...),
        # →
        # INSERT INTO product (...) VALUES
        #   ('01010101', ..., (SELECT id FROM product_categories WHERE category_code='010101'), ...),
        #   ('01010102', ..., (SELECT id FROM product_categories WHERE category_code='010101'), ...),

        # 取列名（括号内）
        m = re.match(r"INSERT INTO product \(([^)]+)\) VALUES", header)
        if m:
            columns = [c.strip() for c in m.group(1).split(',')]
            category_idx = columns.index('category_id')
        else:
            category_idx = 2  # 默认第 3 列

        # 写 INSERT 头（保留 VALUES）
        out_lines.append(header.rstrip())

        # 处理每行数据
        data_rows = insert_block[1:]
        for ri, row in enumerate(data_rows):
            if not row.strip():
                continue
            # 数据行格式: ('xxx', 'yyy', @parent_id, 'val', ..., 'val2', NULL),
            # 或最后一行: ('xxx', 'yyy', @parent_id, ..., NULL);
            m2 = re.match(r"\(\s*(.*?)\s*\)(,|;)?$", row)
            if not m2:
                out_lines.append(row)
                continue
            inner = m2.group(1)
            end = m2.group(2) or ''

            # 把 inner 按 , 切分（注意字符串内的 , 不切——手工 split）
            parts = []
            cur = ''
            in_quote = False
            for ch in inner:
                if ch == "'" and (not cur or cur[-1] != '\\'):
                    in_quote = not in_quote
                if ch == ',' and not in_quote:
                    parts.append(cur.strip())
                    cur = ''
                else:
                    cur += ch
            if cur.strip():
                parts.append(cur.strip())

            # 取该数据行自己的 product_code
            this_code = None
            if parts:
                m_code = re.match(r"'([0-9]+)'", parts[0])
                if m_code:
                    this_code = m_code.group(1)
            # 替换 @parent_id
            if this_code and category_idx < len(parts):
                subq = f"(SELECT id FROM product_categories WHERE category_code = '{this_code[:-2]}')"
                parts[category_idx] = subq

            # 重新组装
            new_inner = ', '.join(parts)
            # 给每行加逗号或分号（保留原 end）
            out_lines.append(f"  ({new_inner}){end}")

        i = j + 1
        continue

    out_lines.append(line)
    i += 1

# 写回
DST.write_text('\n'.join(out_lines) + '\n', encoding='utf-8')
print(f'已转换: {SRC} → {DST}')
print(f'源行数: {len(src_lines)}, 输出行数: {len(out_lines)}')
