"""数据库初始化：建表 + 迁移"""
import json
import os
import sqlite3
from datetime import datetime
from ._db import get_db
def init_db():
    conn = get_db()
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS work_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS error_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            error_type TEXT DEFAULT '',
            solution TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS todo_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            content TEXT NOT NULL,
            done INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS notices (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category TEXT NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT NOT NULL,
            sort_order INTEGER DEFAULT 0
        )
    ''')
    cursor.execute("PRAGMA table_info(notices)")
    notice_cols = [row[1] for row in cursor.fetchall()]
    if 'img_cols' not in notice_cols:
        cursor.execute('ALTER TABLE notices ADD COLUMN img_cols INTEGER NOT NULL DEFAULT 5')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS notice_images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            notice_pk INTEGER NOT NULL,
            file_name TEXT NOT NULL,
            original_name TEXT,
            created_at TEXT NOT NULL
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS vehicle_maintenance (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            vehicle_plate TEXT NOT NULL,
            type TEXT NOT NULL,
            description TEXT NOT NULL,
            cost TEXT DEFAULT '',
            remark TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS shipping_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            customer TEXT NOT NULL,
            order_num INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            is_locked INTEGER NOT NULL DEFAULT 0,
            img_cols INTEGER NOT NULL DEFAULT 1,
            order_note TEXT NOT NULL DEFAULT '',
            user_id INTEGER DEFAULT NULL,
            UNIQUE(date, customer, order_num)
        )
    ''')

    # 入库订单表（之前漏写 CREATE TABLE，靠历史数据存活；现在补全）
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS inbound_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            supplier TEXT NOT NULL,
            order_num INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            is_locked INTEGER NOT NULL DEFAULT 0,
            order_note TEXT NOT NULL DEFAULT '',
            user_id INTEGER DEFAULT NULL
        )
    ''')

    # 装柜订单表（同上）
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS loading_orders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            customer TEXT NOT NULL DEFAULT '',
            order_num INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL,
            is_locked INTEGER NOT NULL DEFAULT 0,
            order_note TEXT NOT NULL DEFAULT '',
            user_id INTEGER DEFAULT NULL
        )
    ''')

    # 迁移：为已存在的表添加 is_locked 列
    try:
        cursor.execute('ALTER TABLE shipping_orders ADD COLUMN is_locked INTEGER NOT NULL DEFAULT 0')
    except Exception:
        pass  # 列已存在

    # 迁移：为已存在的表添加 img_cols 列（图片列数，每订单独立记忆）
    try:
        cursor.execute('ALTER TABLE shipping_orders ADD COLUMN img_cols INTEGER NOT NULL DEFAULT 1')
    except Exception:
        pass  # 列已存在

    # 迁移：为已存在的表添加 order_note 列（订单级备注，显示在商品信息行上方）
    try:
        cursor.execute("ALTER TABLE shipping_orders ADD COLUMN order_note TEXT NOT NULL DEFAULT ''")
    except Exception:
        pass  # 列已存在

    # 迁移：入库订单 order_note
    try:
        cursor.execute("ALTER TABLE inbound_orders ADD COLUMN order_note TEXT NOT NULL DEFAULT ''")
    except Exception:
        pass

    # 迁移：装柜订单 order_note
    try:
        cursor.execute("ALTER TABLE loading_orders ADD COLUMN order_note TEXT NOT NULL DEFAULT ''")
    except Exception:
        pass

    # 迁移：三张订单表添加 user_id 字段（未来多用户用，nullable，单人用时全 NULL）
    for tbl in ('shipping_orders', 'inbound_orders', 'loading_orders'):
        try:
            cursor.execute(f"ALTER TABLE {tbl} ADD COLUMN user_id INTEGER DEFAULT NULL")
        except Exception:
            pass  # 列已存在

    # ── shipping_images（出货订单图片） ──
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS shipping_images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_pk INTEGER NOT NULL,
            file_path TEXT NOT NULL,
            original_name TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (order_pk) REFERENCES shipping_orders(id)
        )
    ''')

    # ── inbound_images（入库订单图片） ──
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS inbound_images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_pk INTEGER NOT NULL,
            file_path TEXT NOT NULL,
            original_name TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (order_pk) REFERENCES inbound_orders(id)
        )
    ''')

    # ── inbound_records（入库订单明细） ──
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS inbound_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_pk INTEGER NOT NULL,
            product_name TEXT NOT NULL,
            specification TEXT NOT NULL,
            quantity TEXT NOT NULL,
            unit TEXT NOT NULL DEFAULT '支',
            remark TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (order_pk) REFERENCES inbound_orders(id)
        )
    ''')

    # ── loading_order_records（装柜订单明细） ──
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS loading_order_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_pk INTEGER NOT NULL,
            product_name TEXT NOT NULL,
            specification TEXT NOT NULL,
            quantity TEXT NOT NULL,
            unit TEXT NOT NULL DEFAULT '支',
            remark TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (order_pk) REFERENCES loading_orders(id)
        )
    ''')

    # ── loading_order_images（装柜订单图片） ──
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS loading_order_images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_pk INTEGER NOT NULL,
            file_path TEXT NOT NULL,
            original_name TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (order_pk) REFERENCES loading_orders(id)
        )
    ''')

    # ── product（产品管理） ──
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS product (
            product_id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_code TEXT NOT NULL,
            product_name TEXT NOT NULL,
            category_id INTEGER NOT NULL,
            specification TEXT,
            model TEXT,
            barcode TEXT,
            cost_price INTEGER,
            base_unit TEXT,
            aux_unit TEXT,
            conversion_rate TEXT,
            stock_quantity INTEGER,
            aux_quantity TEXT,
            preset_price REAL,
            status INTEGER DEFAULT 1,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (category_id) REFERENCES product_categories(id)
        )
    ''')
    cursor.execute('CREATE UNIQUE INDEX IF NOT EXISTS uk_product_code ON product(product_code)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_product_category ON product(category_id)')

    # 操作日志表（未来多用户用——"谁在什么时候改了什么"是刚需）
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS audit_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            action TEXT NOT NULL,
            entity_type TEXT,
            entity_id INTEGER,
            user_id INTEGER,
            detail TEXT,
            created_at TEXT NOT NULL
        )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_audit_log_created_at ON audit_log(created_at)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_audit_log_entity ON audit_log(entity_type, entity_id)')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS shipping_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_pk INTEGER NOT NULL,
            product_name TEXT NOT NULL,
            specification TEXT NOT NULL,
            quantity TEXT NOT NULL,
            unit TEXT NOT NULL DEFAULT '\u652f',
            remark TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            FOREIGN KEY (order_pk) REFERENCES shipping_orders(id)
        )
    ''')

    # 缺货商品表
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS stock_out_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            color TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
    ''')

    # 商品辅助单位表（每支多少码，实际值×100存储以避免浮点误差）
    # 复合唯一键 (product_name, spec_keyword)：同名商品可有默认行 + 多条规格规则
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS product_units (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            product_name TEXT NOT NULL,
            spec_keyword TEXT DEFAULT NULL,
            yards_per_piece INTEGER NOT NULL DEFAULT 0,
            is_usingyardforcounting INTEGER NOT NULL DEFAULT 0,
            UNIQUE(product_name, spec_keyword)
        )
    ''')

    # 迁移：为旧表添加 spec_keyword 列并重建唯一约束
    cursor.execute("PRAGMA table_info(product_units)")
    cols = [r[1] for r in cursor.fetchall()]
    if 'spec_keyword' not in cols:
        cursor.execute('BEGIN')
        try:
            cursor.execute('''
                CREATE TABLE product_units_new (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    product_name TEXT NOT NULL,
                    spec_keyword TEXT DEFAULT NULL,
                    yards_per_piece INTEGER NOT NULL DEFAULT 0,
                    is_usingyardforcounting INTEGER NOT NULL DEFAULT 0,
                    UNIQUE(product_name, spec_keyword)
                )
            ''')
            cursor.execute('''
                INSERT INTO product_units_new (id, product_name, spec_keyword, yards_per_piece, is_usingyardforcounting)
                SELECT id, product_name, NULL, yards_per_piece, is_usingyardforcounting FROM product_units
            ''')
            cursor.execute('DROP TABLE product_units')
            cursor.execute('ALTER TABLE product_units_new RENAME TO product_units')
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    # ── 商品分类：统一为 product_categories（复数）表 ──
    # 首次启动时按目标 schema 建表；旧版 product_category（单数）若存在则自动迁移
    # 注意：`name` 列允许 NULL，因为统一后我们用 `category_name` 替代它
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS product_categories (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT,
            category_code   TEXT NOT NULL,
            category_name   TEXT NOT NULL,
            parent_id       INTEGER DEFAULT NULL,
            level           INTEGER NOT NULL DEFAULT 1,
            sort_order      INTEGER DEFAULT 0,
            status          INTEGER DEFAULT 1,
            created_at      TEXT DEFAULT (datetime('now','localtime')),
            updated_at      TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (parent_id) REFERENCES product_categories(id) ON DELETE CASCADE
        )
    ''')

    # 渐进式 schema 升级：旧版 product_categories 可能只有 id/name/parent_id 三列
    cursor.execute('PRAGMA table_info(product_categories)')
    pc_cols = {row[1] for row in cursor.fetchall()}
    if 'category_code' not in pc_cols:
        cursor.execute("ALTER TABLE product_categories ADD COLUMN category_code TEXT NOT NULL DEFAULT ''")
    if 'category_name' not in pc_cols:
        # 把旧 name 列复制到 category_name（如有 name 列）
        cursor.execute("ALTER TABLE product_categories ADD COLUMN category_name TEXT NOT NULL DEFAULT ''")
        cursor.execute("UPDATE product_categories SET category_name = name WHERE name IS NOT NULL AND name != ''")
    if 'level' not in pc_cols:
        cursor.execute("ALTER TABLE product_categories ADD COLUMN level INTEGER NOT NULL DEFAULT 1")
    if 'sort_order' not in pc_cols:
        cursor.execute("ALTER TABLE product_categories ADD COLUMN sort_order INTEGER DEFAULT 0")
    if 'status' not in pc_cols:
        cursor.execute("ALTER TABLE product_categories ADD COLUMN status INTEGER DEFAULT 1")
    if 'created_at' not in pc_cols:
        cursor.execute("ALTER TABLE product_categories ADD COLUMN created_at TEXT DEFAULT (datetime('now','localtime'))")
    if 'updated_at' not in pc_cols:
        cursor.execute("ALTER TABLE product_categories ADD COLUMN updated_at TEXT DEFAULT (datetime('now','localtime'))")

    # 迁移：把旧 product_category（单数）数据搬到 product_categories
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='product_category'")
    if cursor.fetchone() is not None:
        # 防御：先验证 product_categories 已有所有需要的列（避免 INSERT 失败导致 DROP 半完成）
        cursor.execute('PRAGMA table_info(product_categories)')
        pc_cols_now = {row[1] for row in cursor.fetchall()}
        needed = {'category_code', 'category_name', 'level', 'sort_order', 'status', 'created_at', 'updated_at'}
        if not needed.issubset(pc_cols_now):
            raise RuntimeError(
                f'product_categories 缺少必要列 {needed - pc_cols_now}，拒绝迁移以避免数据丢失。'
            )

        # 把旧表的 category_id → 新表 id（按原 category_id 升序保证 id 一致）
        # 注意：不要用 OR IGNORE，否则 INSERT 失败被吞会导致 DROP 跑掉
        cursor.execute('''
            INSERT INTO product_categories
                (id, category_code, category_name, parent_id, level, sort_order, status, created_at, updated_at)
            SELECT category_id, category_code, category_name, parent_id, level, sort_order,
                   COALESCE(status, 1), created_at, updated_at
            FROM product_category
            ORDER BY category_id
        ''')
        inserted = cursor.rowcount
        old_count = cursor.execute('SELECT COUNT(*) FROM product_category').fetchone()[0]
        if inserted != old_count:
            raise RuntimeError(
                f'迁移行数不匹配：旧表 {old_count} 行，新表写入 {inserted} 行。拒绝 DROP。'
            )
        # 旧表数据已安全落地，删除
        cursor.execute('DROP TABLE product_category')

    # 创建索引（如果不存在不会报错）
    cursor.execute('CREATE UNIQUE INDEX IF NOT EXISTS uk_pc_code ON product_categories(category_code)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_pc_parent ON product_categories(parent_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_pc_level  ON product_categories(level)')

    # ── 迁移：为已存在的明细表添加 sort_order 列 ──
    # 注意:这些迁移必须在 conn.close() 之前完成,否则 cursor 失效。
    for tbl in ('inbound_records', 'shipping_records', 'loading_order_records'):
        try:
            cursor.execute(f'ALTER TABLE {tbl} ADD COLUMN sort_order INTEGER NOT NULL DEFAULT 0')
        except sqlite3.OperationalError:
            pass  # 列已存在（duplicate column name）— 预期内，幂等跳过

    # ── 索引：所有 orders 关联的明细/图片表都按 order_pk 高频查询 ──
    # 数据量小不明显,到几千行后会拖慢;幂等(IF NOT EXISTS)
    for ddl in [
        'CREATE INDEX IF NOT EXISTS idx_shipping_records_order_pk ON shipping_records(order_pk)',
        'CREATE INDEX IF NOT EXISTS idx_shipping_images_order_pk ON shipping_images(order_pk)',
        'CREATE INDEX IF NOT EXISTS idx_inbound_records_order_pk ON inbound_records(order_pk)',
        'CREATE INDEX IF NOT EXISTS idx_inbound_images_order_pk ON inbound_images(order_pk)',
        'CREATE INDEX IF NOT EXISTS idx_loading_records_order_pk ON loading_order_records(order_pk)',
        'CREATE INDEX IF NOT EXISTS idx_loading_images_order_pk ON loading_order_images(order_pk)',
    ]:
        try:
            cursor.execute(ddl)
        except sqlite3.OperationalError:
            pass  # 表尚未创建(冷启动场景)——索引下次启动再加

    conn.commit()
    conn.close()

