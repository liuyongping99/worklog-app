"""数据库初始化：建表 + 迁移"""
import json
import os
import sqlite3
from datetime import datetime
from ._db import get_db


def _fk_pairs(conn, table):
    """表的外键 (列, 父表) 集合。"""
    return {(r['from'], r['table']) for r in conn.execute(f'PRAGMA foreign_key_list({table})')}


def _rebuild_table_for_fk(conn, cursor, tbl, ddl_tpl, want_fks, indexes=(), triggers=()):
    """给已存在的表补外键约束 —— SQLite 的 ALTER TABLE **加不了**约束,只能重建表。

    重建要过 4 道坎(每一条都踩过坑,别简化):
      ① `PRAGMA foreign_keys` 在**事务内是 no-op** → 必须在 BEGIN 之前关掉;
         所以先 conn.commit() 确保不在事务里。
      ② DROP 一个还被别的表引用的父表(如 stock_postings)→ 要 `PRAGMA legacy_alter_table=ON`,
         否则后面 RENAME 时 SQLite 会重解析整个 schema,发现悬空 FK 直接报错。
      ③ DROP TABLE 会连带删掉**该表的索引和触发器** → 重建后必须一并建回来;
         且索引/触发器要在 RENAME **之后**再建,这样触发器体里的表名指向最终表名。
      ④ 数据搬运要**按列名**写,不能用 SELECT * —— 新旧列顺序可能不同
         (ALTER 追加的列在末尾),按位置对应会错位。

    幂等:目标 FK 已经都在 → 直接跳过,什么都不做。
    返回 True = 真的重建了。
    """
    have = _fk_pairs(conn, tbl)
    if all(fk in have for fk in want_fks):
        return False

    conn.commit()                     # 确保不在事务里(PRAGMA 在事务内是 no-op)
    conn.execute('PRAGMA foreign_keys = OFF')
    conn.execute('PRAGMA legacy_alter_table = ON')
    try:
        tmp = f'{tbl}__fknew'
        cursor.execute(f'DROP TABLE IF EXISTS {tmp}')
        cursor.execute(ddl_tpl.replace('{tbl}', tmp))

        old_cols = [r['name'] for r in conn.execute(f'PRAGMA table_info({tbl})')]
        new_cols = [r['name'] for r in conn.execute(f'PRAGMA table_info({tmp})')]
        lost = [c for c in old_cols if c not in new_cols]
        if lost:
            # 宁可不迁移,也不要静默丢列 → 直接炸,让人来处理
            raise RuntimeError(
                f'重建 {tbl} 会丢列 {lost}(新 DDL 里没有这些列)—— 已中止,请先对齐 DDL')
        common = [c for c in old_cols if c in new_cols]
        collist = ', '.join(common)
        if common:
            cursor.execute(f'INSERT INTO {tmp} ({collist}) SELECT {collist} FROM {tbl}')

        cursor.execute(f'DROP TABLE {tbl}')
        cursor.execute(f'ALTER TABLE {tmp} RENAME TO {tbl}')
        for ix in indexes:
            cursor.execute(ix)
        for tg in triggers:
            cursor.execute(tg)
        conn.commit()
    finally:
        conn.execute('PRAGMA legacy_alter_table = OFF')
        conn.execute('PRAGMA foreign_keys = ON')
    return True


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

    # 车辆维护记录图片（2026-11-02 新增）
    # 存相对路径(month_dir + file_name)而非绝对盘符路径 —— 规避踩坑点 9（换盘要跑迁移）
    # 文件本体落在 upload/<YYYY-MM>/ 下，与出货/入库/装柜图片同目录，纳入 upload 备份脚本覆盖
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS vehicle_maintenance_images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            record_pk INTEGER NOT NULL,
            file_name TEXT NOT NULL,
            month_dir TEXT NOT NULL DEFAULT '',
            original_name TEXT,
            created_at TEXT NOT NULL
        )
    ''')
    try:
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_vehicle_maint_images_record_pk '
            'ON vehicle_maintenance_images(record_pk)'
        )
    except sqlite3.OperationalError:
        pass

    # 消防检查记录（2026-11-06 新增）
    # 例行消防设施检查台账：日期 / 部位 / 检查事项 / 遗留事项 / 状态 / 备注
    # status 由 models/fire_check.py 按「遗留事项是否为空」推导，DB 侧只存不校验
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS fire_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            location TEXT DEFAULT '',
            check_items TEXT NOT NULL,
            outstanding_items TEXT DEFAULT '',
            status TEXT NOT NULL DEFAULT 'open',
            remark TEXT DEFAULT '',
            user_id INTEGER,
            created_at TEXT NOT NULL,
            updated_at TEXT
        )
    ''')
    try:
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_fire_checks_date '
            'ON fire_checks(date)'
        )
    except sqlite3.OperationalError:
        pass

    # 消防检查记录图片（2026-11-08 新增）
    # 与 vehicle_maintenance_images 同款：存相对路径(month_dir + file_name) 而非绝对盘符路径
    # —— 规避踩坑点 9（换盘要跑迁移脚本改写全库）
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS fire_check_images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            record_pk INTEGER NOT NULL,
            file_name TEXT NOT NULL,
            month_dir TEXT NOT NULL DEFAULT '',
            original_name TEXT,
            created_at TEXT NOT NULL
        )
    ''')
    try:
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_fire_check_images_record_pk '
            'ON fire_check_images(record_pk)'
        )
    except sqlite3.OperationalError:
        pass

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
            doc_number TEXT NOT NULL DEFAULT '',   -- ⭐2026-11-16 单据编号(与生产库列序对齐)
            warehouse_id INTEGER NOT NULL DEFAULT 1,
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
            user_id INTEGER DEFAULT NULL,
            doc_number TEXT NOT NULL DEFAULT '',   -- ⭐2026-11-16 单据编号(与生产库列序对齐)
            img_cols INTEGER NOT NULL DEFAULT 5,   -- ⭐2026-11-16 补进建表语句
            warehouse_id INTEGER NOT NULL DEFAULT 1
        )
    ''')

    # 迁移：为已存在的 inbound_orders 表添加 img_cols 列
    try:
        cursor.execute('ALTER TABLE inbound_orders ADD COLUMN img_cols INTEGER NOT NULL DEFAULT 5')
    except Exception:
        pass  # 列已存在

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
            user_id INTEGER DEFAULT NULL,
            doc_number TEXT NOT NULL DEFAULT '',   -- ⭐2026-11-16 单据编号(与生产库列序对齐)
            img_cols INTEGER NOT NULL DEFAULT 5,   -- ⭐2026-11-16 默认值 3→5(与生产库一致)
            warehouse_id INTEGER NOT NULL DEFAULT 1
        )
    ''')

    # 迁移：为已存在的 loading_orders 表添加 img_cols 列
    try:
        cursor.execute('ALTER TABLE loading_orders ADD COLUMN img_cols INTEGER NOT NULL DEFAULT 5')
    except Exception:
        pass  # 列已存在

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

    # 迁移：三张订单表加 doc_number 列（单据编号，例如出货单号）
    # ⭐2026-11-16 修 bug:原来只覆盖 inbound_orders / loading_orders,
    #   **漏了 shipping_orders** → 全新库重建出来的 shipping_orders 没有这一列,
    #   而 models/orders.py 的 ShippingOrder.set_doc_number() 会执行
    #   `UPDATE shipping_orders SET doc_number = ?` → 全新库上直接报错。
    for tbl in ('shipping_orders', 'inbound_orders', 'loading_orders'):
        try:
            cursor.execute(f"ALTER TABLE {tbl} ADD COLUMN doc_number TEXT NOT NULL DEFAULT ''")
        except Exception:
            pass  # 列已存在

    # 迁移：三张订单表添加 user_id 字段（未来多用户用，nullable，单人用时全 NULL）
    for tbl in ('shipping_orders', 'inbound_orders', 'loading_orders'):
        try:
            cursor.execute(f"ALTER TABLE {tbl} ADD COLUMN user_id INTEGER DEFAULT NULL")
        except Exception:
            pass  # 列已存在

    # 迁移：三套订单明细加 verified 列（用户已核查该明细，0/1）
    #   2026-07-30：入库补齐 —— 之前只有出货/装柜有此列，入库页「✓ 全部已核查」点了没法落库。
    for tbl in ('shipping_records', 'inbound_records', 'loading_order_records'):
        try:
            cursor.execute(f"ALTER TABLE {tbl} ADD COLUMN verified INTEGER NOT NULL DEFAULT 0")
        except Exception:
            pass  # 列已存在

    # 迁移：三套订单明细加 verified_warnings(JSON) — 细粒度记录"哪条警告已核查"
    #   与 verified 字段并存:verified=1 仍代表整行已核查(隐藏所有警告),
    #   verified_warnings 用于单独追踪某条规则(如 B白300g)是否被点掉过,
    #   这样用户可以"取消核查"该条让警告重新弹出,而不会触发其他警告的复活。
    #   NOT NULL DEFAULT '{}' 保证存量行不是 NULL —— 模板 |tojson 遇到 None 会输出 null,
    #   前端 JSON.parse 出 null 后 Object 操作会抛错。
    #   2026-07-30：入库/装柜补齐（装柜之前只有 verified，per-rule 折叠区永远是空的）。
    for tbl in ('shipping_records', 'inbound_records', 'loading_order_records'):
        try:
            cursor.execute(f"ALTER TABLE {tbl} ADD COLUMN verified_warnings TEXT NOT NULL DEFAULT '{{}}'")
        except Exception:
            pass  # 列已存在

    # 迁移：出货/装柜图片表加 record_pk + sort_order 列
    #   - record_pk: NULL=订单级共享图,非NULL=某条明细的专属图
    #   - sort_order: 图片显示顺序(ORDER BY sort_order ASC, id ASC)
    for tbl in ('shipping_images', 'loading_order_images'):
        for col, default in [('record_pk', 'INTEGER DEFAULT NULL'),
                            ('sort_order', 'INTEGER NOT NULL DEFAULT 0')]:
            try:
                cursor.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {default}")
            except Exception:
                pass  # 列已存在

    # 迁移：shipping_images 加 match_status/match_score（标签 OCR 匹配徽章）
    for col, default in [('match_status', "TEXT DEFAULT NULL"),
                         ('match_score', 'REAL DEFAULT NULL')]:
        try:
            cursor.execute(f"ALTER TABLE shipping_images ADD COLUMN {col} {default}")
        except Exception:
            pass  # 列已存在

    # 迁移：shipping_images 加 reason（AI 比对详细理由，hover 提示用）
    try:
        cursor.execute("ALTER TABLE shipping_images ADD COLUMN reason TEXT DEFAULT NULL")
    except Exception:
        pass  # 列已存在

    # 迁移：shipping_images 加 human_verified（人工覆盖 AI 红牌:0=未确认,1=已人工确认）
    try:
        cursor.execute("ALTER TABLE shipping_images ADD COLUMN human_verified INTEGER DEFAULT 0")
    except Exception:
        pass  # 列已存在

    # 迁移：loading_order_images 加 match_status/match_score/reason/human_verified
    for tbl in ('loading_order_images',):
        for col, default in [('match_status', "TEXT DEFAULT NULL"),
                             ('match_score', 'REAL DEFAULT NULL')]:
            try:
                cursor.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} {default}")
            except Exception:
                pass
    try:
        cursor.execute("ALTER TABLE loading_order_images ADD COLUMN reason TEXT DEFAULT NULL")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE loading_order_images ADD COLUMN human_verified INTEGER DEFAULT 0")
    except Exception:
        pass

    # 迁移(2026-07-27):出货 OCR 比对事件 append-only 日志,供未来提示词优化用
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS ocr_match_event (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        record_id           INTEGER NOT NULL,
        image_id            INTEGER,
        order_id            INTEGER NOT NULL,
        event_type          TEXT NOT NULL,
        ocr_text            TEXT,
        ocr_engine          TEXT,
        product_name        TEXT,
        specification       TEXT,
        prompt_payload      TEXT,
        ai_match_status     TEXT,
        ai_match_score      REAL,
        ai_match_reason     TEXT,
        ai_raw_response     TEXT,
        ai_engine           TEXT,
        prompt_version      TEXT,
        human_status        TEXT,
        human_reason        TEXT,
        human_verified_by   INTEGER,
        created_at          TEXT NOT NULL,
        FOREIGN KEY (record_id) REFERENCES shipping_records(id) ON DELETE CASCADE
    )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_ocr_match_record ON ocr_match_event(record_id, event_type, created_at DESC)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_ocr_match_order  ON ocr_match_event(order_id, created_at DESC)')

    # ── shipping_images（出货订单图片） ──
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS shipping_images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_pk INTEGER NOT NULL,
            file_path TEXT NOT NULL,
            original_name TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            source TEXT DEFAULT 'upload',
            record_pk INTEGER DEFAULT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            match_status TEXT DEFAULT NULL,
            match_score REAL DEFAULT NULL,
            reason TEXT DEFAULT NULL,
            human_verified INTEGER DEFAULT 0,
            match_source TEXT DEFAULT NULL,        -- 'local_fuzzy' | 'deepseek' | NULL(老记录)
            bg_color TEXT DEFAULT NULL,             -- 自动识别的背景色 'black'|'white'|NULL
            source_tag TEXT DEFAULT NULL,           -- 整体图分类标签: '备货照'|'装车照'|'归仓照'|NULL
            FOREIGN KEY (order_pk) REFERENCES shipping_orders(id)
        )
    ''')
    # 2026-08-15: 摆放图计数点表(供"支"类商品清点数量)
    # 每张摆放图(image_id)可有多枚点击计数点,按 seq 顺序撤销
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS placement_marks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            image_id INTEGER NOT NULL,
            seq INTEGER NOT NULL,          -- 点击序号 1,2,3...
            x_ratio REAL NOT NULL,          -- 相对图片宽度比例 0~1
            y_ratio REAL NOT NULL,          -- 相对图片高度比例 0~1
            created_at TEXT NOT NULL,
            FOREIGN KEY (image_id) REFERENCES shipping_images(id)
        )
    ''')
    try:
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_placement_marks_image_id ON placement_marks(image_id)')
    except Exception:
        pass

    # 2026-08-26:inbound placement_marks table (separate FK to inbound_images)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS inbound_placement_marks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            image_id INTEGER NOT NULL,
            seq INTEGER NOT NULL,
            x_ratio REAL NOT NULL,
            y_ratio REAL NOT NULL,
            mark_r REAL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (image_id) REFERENCES inbound_images(id)
        )
    ''')
    try:
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_inbound_placement_marks_image_id ON inbound_placement_marks(image_id)')
    except Exception:
        pass

    # 2026-09-09:loading placement_marks table (separate FK to loading_order_images)
    # 装柜摆放图计数点与出货/inbound 隔离,避免主键串图;口径与 placement_marks 一致。
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS loading_placement_marks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            image_id INTEGER NOT NULL,
            seq INTEGER NOT NULL,
            x_ratio REAL NOT NULL,
            y_ratio REAL NOT NULL,
            mark_r REAL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (image_id) REFERENCES loading_order_images(id)
        )
    ''')
    try:
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_loading_placement_marks_image_id ON loading_placement_marks(image_id)')
    except Exception:
        pass
    # 2026-07-30: 老表增量加 match_source 列(若已存在则 skip)
    for tbl in ('shipping_images', 'loading_order_images'):
        try:
            cursor.execute(f"ALTER TABLE {tbl} ADD COLUMN match_source TEXT DEFAULT NULL")
        except Exception:
            pass
    # 2026-08-02: 增量加 bg_color 列(自动检测背景色)
    for tbl in ('shipping_images', 'loading_order_images'):
        try:
            cursor.execute(f"ALTER TABLE {tbl} ADD COLUMN bg_color TEXT DEFAULT NULL")
        except Exception:
            pass
    # 2026-08-12: 增量加 source_tag 列(整体图分类:备货照/装车照/归仓照)
    try:
        cursor.execute("ALTER TABLE shipping_images ADD COLUMN source_tag TEXT DEFAULT NULL")
    except Exception:
        pass
    # 2026-08-15: 增量加 circles 列(摆放图用 OpenCV 检测的圆柱端面,JSON 数组)
    try:
        cursor.execute("ALTER TABLE shipping_images ADD COLUMN circles TEXT DEFAULT NULL")
    except Exception:
        pass
    # 2026-08-15: 增量加 mark_r 列(计数点所属圆柱的归一化半径,相对展示区宽度)
    try:
        cursor.execute("ALTER TABLE placement_marks ADD COLUMN mark_r REAL DEFAULT 0")
    except Exception:
        pass
    # 2026-08-16: 增量加 mark_scale 列(摆放图计数数字整体缩放比例,弹框内放大/缩小按钮写入)
    try:
        cursor.execute("ALTER TABLE shipping_images ADD COLUMN mark_scale REAL DEFAULT 1")
    except Exception:
        pass
    # 2026-08-16: 增量加 loose_count 列(摆放图散码数量,点数弹框内「散码」按钮录入)
    try:
        cursor.execute("ALTER TABLE shipping_images ADD COLUMN loose_count INTEGER DEFAULT 0")
    except Exception:
        pass
    # 2026-09-09: 补齐三张图片表的摆放图 / 标签匹配相关列。
    # ⭐ 2026-11-16 阶段 1.6:这段补列**整体挪到了下面**(三张图片表 CREATE 之后)。
    #    原因:ALTER 补列语句**必须排在目标表的 CREATE TABLE 之后**。
    #    排在前面的后果是:存量库没事(表早建好了),**全新库静默失败**(表还不存在)
    #    → 全新库缺列、生产库正常 → 只在「换机重建 / 新人 clone」时才炸,极难察觉。
    #    本阶段实测踩中 3 处:shipping_images 缺 manual_count/is_unload、
    #    loading_order_images 缺 15 列、shipping_orders 缺 doc_number。
    #    统一放一处 → 见文件末尾「图片表列对齐安全网」,别再往这里加补列语句。


    # 2026-08-17: 独立点数工具 -- 独立建表 / 单独持久化(可追溯)
    # 设计: 参考出货页摆放图计数, 但会话/图片/计数点 都新建独立表, 不挂在出货订单上。
    #   point_count_sessions  -- 一个"点数任务"(谁来点/何时/标题/可选期望值)
    #   point_count_images   -- 一张上传的图(物理文件 + 缩放/散码等局部状态)
    #   point_count_marks    -- 图上每枚点击(相对 x/y + seq 顺序号)
    # 可追溯: session 记 user_id+created_at; image 记 created_at;
    #          mark 记 created_at; session 总数 = SUM(marks) + loose_count。
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS point_count_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            expected_count INTEGER DEFAULT NULL,
            unit TEXT NOT NULL DEFAULT '支',
            remark TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'open',
            total_count INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            closed_at TEXT DEFAULT NULL
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS point_count_images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id INTEGER NOT NULL,
            file_path TEXT NOT NULL,
            original_name TEXT NOT NULL DEFAULT '',
            mark_scale REAL NOT NULL DEFAULT 1,
            loose_count INTEGER NOT NULL DEFAULT 0,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES point_count_sessions(id) ON DELETE CASCADE
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS point_count_marks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            image_id INTEGER NOT NULL,
            seq INTEGER NOT NULL,
            x_ratio REAL NOT NULL,
            y_ratio REAL NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (image_id) REFERENCES point_count_images(id) ON DELETE CASCADE
        )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_point_count_images_session ON point_count_images(session_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_point_count_marks_image ON point_count_marks(image_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_point_count_sessions_user ON point_count_sessions(user_id)')

    # ── 2026-08-02: 入库图片补齐 record-level 匹配能力 ──
    # 补齐之前遗漏的 source 列(InboundImage.create 已经写了 source,但建表未声明)
    try:
        cursor.execute("ALTER TABLE inbound_images ADD COLUMN source TEXT DEFAULT 'upload'")
    except Exception:
        pass
    for col, default in [('record_pk', 'INTEGER DEFAULT NULL'),
                         ('sort_order', 'INTEGER NOT NULL DEFAULT 0')]:
        try:
            cursor.execute(f"ALTER TABLE inbound_images ADD COLUMN {col} {default}")
        except Exception:
            pass
    for col, default in [('match_status', "TEXT DEFAULT NULL"),
                         ('match_score', 'REAL DEFAULT NULL')]:
        try:
            cursor.execute(f"ALTER TABLE inbound_images ADD COLUMN {col} {default}")
        except Exception:
            pass
    try:
        cursor.execute("ALTER TABLE inbound_images ADD COLUMN reason TEXT DEFAULT NULL")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE inbound_images ADD COLUMN human_verified INTEGER DEFAULT 0")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE inbound_images ADD COLUMN match_source TEXT DEFAULT NULL")
    except Exception:
        pass
    try:
        cursor.execute("ALTER TABLE inbound_images ADD COLUMN bg_color TEXT DEFAULT NULL")
    except Exception:
        pass
    # 入库图片 record_pk 索引
    try:
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_inbound_images_record_pk ON inbound_images(record_pk)')
    except Exception:
        pass

    # ── inbound_images（入库订单图片） ──
    # 注意:下方 "2026-08-02: 入库图片补齐 record-level 匹配能力" 段落的 ALTER TABLE
    # 对全新 db 不生效(在 CREATE 之前跑,表还不存在,异常被 except 吞了),所以这里把
    # 所有需要的列都直接放进 CREATE TABLE —— 让 init_db() 在全新 db 上也能建出
    # 完整的 schema(供测试用临时 db + monkey-patch DB_PATH)。
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS inbound_images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            order_pk INTEGER NOT NULL,
            file_path TEXT NOT NULL,
            original_name TEXT DEFAULT '',
            source TEXT DEFAULT 'upload',
            record_pk INTEGER DEFAULT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            match_status TEXT DEFAULT NULL,
            match_score REAL DEFAULT NULL,
            reason TEXT DEFAULT NULL,
            human_verified INTEGER DEFAULT 0,
            match_source TEXT DEFAULT NULL,
            bg_color TEXT DEFAULT NULL,
                        source_tag TEXT DEFAULT NULL,
            -- 2026-08-26:inbound enable placement (align shipping)
            circles TEXT DEFAULT NULL,
            mark_scale REAL DEFAULT 1,
            loose_count INTEGER DEFAULT 0,
            manual_count INTEGER DEFAULT NULL,
            is_unload INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (order_pk) REFERENCES inbound_orders(id)
        )
    ''')

    # 迁移：为已存在的 inbound_images 表添加 source_tag 列（移动端备货照/装车照/归仓照）
    try:
        cursor.execute('ALTER TABLE inbound_images ADD COLUMN source_tag TEXT')
    except Exception:
        pass  # 列已存在

    # 2026-08-26:inbound placement cols migration (for existing dbs)
    for col, default in [
        ('circles', 'TEXT DEFAULT NULL'),
        ('mark_scale', 'REAL DEFAULT 1'),
        ('loose_count', 'INTEGER DEFAULT 0'),
        ('manual_count', 'INTEGER DEFAULT NULL'),
        ('is_unload', 'INTEGER DEFAULT 0'),
    ]:
        try:
            cursor.execute(f'ALTER TABLE inbound_images ADD COLUMN {col} {default}')
        except Exception:
            pass  # column already exists

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
            verified INTEGER NOT NULL DEFAULT 0,
            verified_warnings TEXT NOT NULL DEFAULT '{}',
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
            verified INTEGER NOT NULL DEFAULT 0,
            verified_warnings TEXT NOT NULL DEFAULT '{}',
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
            -- ⭐2026-11-16 阶段 1.6:下面这批列原先**只靠 ALTER 补**,
            --   而那些 ALTER 排在本 CREATE 之前 → 全新库建表时全部静默失败,
            --   全新库的 loading_order_images 只有 5 列,装柜的行级标签图 /
            --   摆放图计数 / 卸货取负全用不了(生产库正常,所以一直没人发现)。
            --   现在直接写进建表语句,列序与生产库一致。
            source TEXT DEFAULT 'upload',
            record_pk INTEGER DEFAULT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            match_status TEXT DEFAULT NULL,
            match_score REAL DEFAULT NULL,
            reason TEXT DEFAULT NULL,
            human_verified INTEGER DEFAULT 0,
            match_source TEXT DEFAULT NULL,
            bg_color TEXT DEFAULT NULL,
            circles TEXT DEFAULT NULL,
            mark_scale REAL DEFAULT 1,
            loose_count INTEGER DEFAULT 0,
            manual_count INTEGER DEFAULT NULL,
            is_unload INTEGER DEFAULT 0,
            source_tag TEXT DEFAULT NULL,
            FOREIGN KEY (order_pk) REFERENCES loading_orders(id)
        )
    ''')

    # ── ⭐2026-11-16 阶段 1.6:三张图片表「列对齐安全网」────────────────
    # ⚠️⚠️ 这块**必须放在三张图片表的 CREATE TABLE 之后**。
    #    原因:ALTER 补列语句排在 CREATE 之前 → 存量库没事(表早建好了),
    #    **全新库静默失败**(表还不存在)→ 全新库缺列、生产库正常,
    #    只在「换机重建 / 新人 clone 下来」时才炸,现场极难复现。
    #    本阶段实测踩中 3 处(见上方注释),所以把三张图片表的列集中声明一次。
    # 语义:已有列 → skip;缺列 → ALTER 补上。存量库 / 全新库 行为一致。
    # 验证工具:python tools/check_fresh_db_schema.py
    for _img_tbl in ('shipping_images', 'inbound_images', 'loading_order_images'):
        for _col, _ddl in (
            ('source', "TEXT DEFAULT 'upload'"),
            ('record_pk', 'INTEGER DEFAULT NULL'),
            ('sort_order', 'INTEGER NOT NULL DEFAULT 0'),
            ('match_status', 'TEXT DEFAULT NULL'),
            ('match_score', 'REAL DEFAULT NULL'),
            ('reason', 'TEXT DEFAULT NULL'),
            ('human_verified', 'INTEGER DEFAULT 0'),
            ('match_source', 'TEXT DEFAULT NULL'),
            ('bg_color', 'TEXT DEFAULT NULL'),
            ('source_tag', 'TEXT DEFAULT NULL'),
            ('circles', 'TEXT DEFAULT NULL'),
            ('mark_scale', 'REAL DEFAULT 1'),
            ('loose_count', 'INTEGER DEFAULT 0'),
            ('manual_count', 'INTEGER DEFAULT NULL'),
            ('is_unload', 'INTEGER DEFAULT 0'),
        ):
            _have = {r['name'] for r in cursor.execute(f'PRAGMA table_info({_img_tbl})')}
            if _col not in _have:
                cursor.execute(f'ALTER TABLE {_img_tbl} ADD COLUMN {_col} {_ddl}')

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
    # ── 「品名 + 规格」唯一约束(2026-11-16)────────────────────────────────
    # 为什么要有:库存系统靠「品名+规格」认货(订单明细表没有 product_code 列),
    #   同一个 (品名,规格) 指向两个 SKU 时,引擎只能拒绝匹配 → 落「待人工」,
    #   库存永远对不上。数据库层面直接不让建,比事后发现强。
    # 为什么用 COALESCE(specification,''):
    #   SQLite 里 NULL 互不相同 → 普通 UNIQUE(name, spec) 拦不住「规格为空」的重复
    #   (2026-11-16 清理掉的 3 组里就有 1 组正是这种)。COALESCE 把 NULL 归成 ''。
    # 为什么品名不去空格:
    #   空格在业务上有意义,必须原样保留(用户 2026-11-16 明确要求)。
    #   'A1.5米 200g' 与 'A1.5米200g' 允许并存为两个 SKU;
    #   引擎侧靠「L1 原始精确优先 + 归一键歧义作废」保证不会串货(宁可漏配不可错配)。
    cursor.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uk_product_name_spec "
        "ON product(product_name, COALESCE(specification, ''))"
    )

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

    # ── 类别/规格自适应提示词（compare-rows 比对时拼装 layer2/layer3） ──
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS category_prompts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope TEXT NOT NULL,                  -- 'category' | 'spec'
            category_code TEXT,                   -- NULL 时要求 product_name_keyword 非空(spec 全局匹配)
            spec_pattern TEXT,                    -- LIKE 模式(%, _),scope='spec' 时必填
            product_name_keyword TEXT,            -- 品名包含关键词则匹配(category 分类的依据)
            prompt_text TEXT NOT NULL,
            source_event_id INTEGER,              -- 触发它的 ocr_match_event.id;NULL=手工直接添加
            source_ocr_text TEXT,                 -- 生成时的 OCR 文本快照
            source_ai_status TEXT,                -- 生成时的 AI 裁决快照(green/yellow/red)
            source_human_status TEXT,            -- 人工最终裁决(一般是 green)
            status TEXT NOT NULL DEFAULT 'active',  -- 'active' | 'archived'
            created_at TEXT NOT NULL,
            FOREIGN KEY (source_event_id) REFERENCES ocr_match_event(id)
        )
    ''')
    # 经常按 scope+category_code 查分类级提示词,以及按 category+spec_pattern 查规格级提示词
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_cat_prompts_category ON category_prompts(scope, category_code, status)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_cat_prompts_spec ON category_prompts(scope, category_code, spec_pattern, status)')

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
            verified INTEGER NOT NULL DEFAULT 0,
            verified_warnings TEXT NOT NULL DEFAULT '{}',
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

    # 件数换算规则表（件→张/只/令 等）
    # model 字段仅描述用途，不参与匹配，不在唯一约束中
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS piece_conversions (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            product_name    TEXT NOT NULL,
            spec_keyword    TEXT DEFAULT NULL,
            model           TEXT DEFAULT NULL,
            units_per_piece REAL NOT NULL,
            target_unit     TEXT NOT NULL,
            is_active       INTEGER DEFAULT 1,
            created_at      TEXT DEFAULT (datetime('now','localtime')),
            updated_at      TEXT DEFAULT (datetime('now','localtime')),
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
        'CREATE INDEX IF NOT EXISTS idx_shipping_images_record_pk ON shipping_images(record_pk)',
        'CREATE INDEX IF NOT EXISTS idx_inbound_records_order_pk ON inbound_records(order_pk)',
        'CREATE INDEX IF NOT EXISTS idx_inbound_images_order_pk ON inbound_images(order_pk)',
        'CREATE INDEX IF NOT EXISTS idx_loading_records_order_pk ON loading_order_records(order_pk)',
        'CREATE INDEX IF NOT EXISTS idx_loading_images_order_pk ON loading_order_images(order_pk)',
        'CREATE INDEX IF NOT EXISTS idx_loading_images_record_pk ON loading_order_images(record_pk)',
    ]:
        try:
            cursor.execute(ddl)
        except sqlite3.OperationalError:
            pass  # 表尚未创建(冷启动场景)——索引下次启动再加

    # === 任务流 M1:6 张表(staff / vehicles / tasks / task_items / task_images / task_events) ===
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS staff (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            gender TEXT,
            birth_date TEXT,
            role TEXT NOT NULL CHECK(role IN ('司机','调度','搬运','打码','仓管','文员')),
            staff_code TEXT UNIQUE,
            phone TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            vehicle_id INTEGER,
            login_name TEXT,
            password_hash TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (vehicle_id) REFERENCES vehicles(id) ON DELETE SET NULL
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS vehicles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            plate_no TEXT NOT NULL UNIQUE,
            tonnage REAL,
            length REAL,
            width REAL,
            height REAL,
            inspection_date TEXT,
            status TEXT NOT NULL DEFAULT '启用' CHECK(status IN ('启用','停用')),
            note TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime'))
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_no TEXT NOT NULL UNIQUE,
            source_order_no TEXT UNIQUE,
            task_type TEXT NOT NULL DEFAULT '送货' CHECK(task_type IN ('送货','退货','换货')),
            related_task_id INTEGER,
            source_type TEXT,
            customer TEXT,
            dest_address TEXT,
            dest_lat REAL,
            dest_lng REAL,
            dest_poi_name TEXT,
            status TEXT NOT NULL DEFAULT '准备中' CHECK(status IN ('准备中','已装货','已点数','已到达','已卸货','已完成','已拒收')),
            coding_status TEXT NOT NULL DEFAULT '无需打码' CHECK(coding_status IN ('无需打码','待打码','打码中','打码完成')),
            driver_id INTEGER,
            vehicle_id INTEGER,
            creator_id INTEGER NOT NULL,
            operator_id INTEGER,
            coder_id INTEGER,
            coding_claimed_at TEXT,
            coding_done_at TEXT,
            est_weight_kg REAL,
            est_distance_km REAL,
            depart_at TEXT,
            arrive_at TEXT,
            remark TEXT,
            is_cancelled INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            updated_at TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (driver_id) REFERENCES staff(id) ON DELETE SET NULL,
            FOREIGN KEY (vehicle_id) REFERENCES vehicles(id) ON DELETE SET NULL,
            FOREIGN KEY (creator_id) REFERENCES staff(id) ON DELETE RESTRICT,
            FOREIGN KEY (operator_id) REFERENCES staff(id) ON DELETE SET NULL,
            FOREIGN KEY (coder_id) REFERENCES staff(id) ON DELETE SET NULL,
            FOREIGN KEY (related_task_id) REFERENCES tasks(id) ON DELETE SET NULL
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS task_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            product_name TEXT NOT NULL,
            specification TEXT,
            quantity REAL NOT NULL,
            unit TEXT,
            remark TEXT,
            sort_order INTEGER NOT NULL DEFAULT 0,
            line_type TEXT NOT NULL DEFAULT 'normal' CHECK(line_type IN ('normal','return','exchange_out','exchange_in')),
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS task_images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            stage TEXT NOT NULL CHECK(stage IN ('识别原图','装车照','点数标签照','点数整体照','卸货照','签收单','打码照','退单照','退货照')),
            image_path TEXT NOT NULL,
            sort_order INTEGER NOT NULL DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS task_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id INTEGER NOT NULL,
            event_type TEXT NOT NULL CHECK(event_type IN ('advance','assign','coding_claim','coding_release','coding_done','driver_release','task_cancel','return_create')),
            from_status TEXT,
            to_status TEXT,
            operator_id INTEGER,
            note TEXT,
            created_at TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
            FOREIGN KEY (operator_id) REFERENCES staff(id) ON DELETE SET NULL
        )
    ''')

    cursor.execute('CREATE INDEX IF NOT EXISTS idx_task_events_task_id ON task_events(task_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_task_items_task_id ON task_items(task_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_task_images_task_id ON task_images(task_id)')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS voice_phrase_mapping (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            phrase TEXT NOT NULL,
            product_id INTEGER NOT NULL,
            spec_hint TEXT,
            source TEXT NOT NULL DEFAULT 'user_confirmed',
            use_count INTEGER NOT NULL DEFAULT 1,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            last_used_at TEXT NOT NULL,
            UNIQUE(phrase, product_id),
            FOREIGN KEY (product_id) REFERENCES product(product_id) ON DELETE CASCADE
        )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_voice_phrase ON voice_phrase_mapping(phrase)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_voice_status ON voice_phrase_mapping(status)')

    # 2026-10-26:日终盘点表 —— 用户手工填的「当日出货单总量」,与系统实际录入单数对账。
    # 纸质出货单一天一张张开,单号 TD-YYYY-MM-DD-NNN 本身不连续(实测 09-16 最大 017
    # 但只有 7 单),光看单号编号判断不出少录了几单,所以由用户手填纸面单据总数来对账。
    #   scope   : shipping / inbound / loading(预留,后续入库/装柜页零改表复用)
    #   date    : 业务日期 YYYY-MM-DD,与 shipping_orders.date 同格式
    #   manual_total : 手填的当日单据总量(与系统 count_by_date 的结果比)
    # 列一次写全 —— 全新库上 ALTER 段跑在 CREATE 之前会被 except 吞掉(见本文件上方注释)。
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS daily_order_check (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            scope TEXT NOT NULL DEFAULT 'shipping',
            date TEXT NOT NULL,
            manual_total INTEGER NOT NULL DEFAULT 0,
            remark TEXT NOT NULL DEFAULT '',
            user_id INTEGER DEFAULT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(scope, date)
        )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_daily_order_check_scope_date ON daily_order_check(scope, date)')

    # 2026-10-14:按重量计价的简化方案 — 在 product_units 表加 2 列,
    # 不再单独建重量类别表。同表两轨道(与 is_usingyardforcounting 并列)互不干扰:
    #   - is_usingyardforcounting=1 → 启用 YPP(按码卖)校验
    #   - is_usingweightforcounting=1 → 启用「按重量」单位白名单校验
    # 配置在 /product-unit 页加;校验走 ProductUnit.get_match 双轮匹配。
    try:
        cursor.execute(
            "ALTER TABLE product_units ADD COLUMN "
            "is_usingweightforcounting INTEGER NOT NULL DEFAULT 0"
        )
    except sqlite3.OperationalError:
        pass  # 列已存在
    try:
        cursor.execute(
            "ALTER TABLE product_units ADD COLUMN allowed_units TEXT NOT NULL DEFAULT ''"
        )
    except sqlite3.OperationalError:
        pass  # 列已存在

    # 2026-10-13 老方案遗留:product_weight_categories 表 + product.weight_category_id 列。
    # 2026-10-14 简化方案上线:删表 + 清索引。product.weight_category_id 列 SQLite 12 之前
    # 不支持 DROP COLUMN,保留无副作用(全员 NULL,UI 也不再读它)。
    cursor.execute('DROP TABLE IF EXISTS product_weight_categories')
    cursor.execute('DROP INDEX IF EXISTS idx_pwc_status')

    # 2026-09-09: copy_paper_images 表已废弃(标签图迁入 shipping_images,
    # source='copy_paper_label';点数走 shipping_images(source='placement')),
    # 不再建表;本机迁移时旧表已 DROP,不影响运行。

    # ══════════════════════════════════════════════════════════════════
    # 库存管理(多仓 + 入库过账)—— 阶段 1:表结构 + 模型层地基
    # 2026-11-16。设计文档:docs/superpowers/plans/2026-11-15-库存管理-多仓过账.md §三
    # ══════════════════════════════════════════════════════════════════
    # 几条不能忘的设计约束(改代码前先看这里):
    #   · **仓库是数据不是结构** —— 用 warehouses 表,将来加「田心仓」不用改表结构
    #   · **期初数不导入** —— product_stock 从空表起步,库存靠「盘点」起算
    #     (product.stock_quantity 是目录图上抄的期初数,不是实时库存;
    #      已存档到 D:\BAK\opening_snapshot_20261116.csv,B6 阶段才 DROP 那个列)
    #   · **is_tracking 区分「真 0」和「未启用」** —— 默认 0(未启用,对外按 0 显示),
    #     只有盘点 / 归零才置 1。过账**绝不**自动启用。
    #   · **stock_ledger.UNIQUE(uniq_key)** 是幂等的唯一保障
    #     (inbound:{record_id} / adjust:{seq} / {biz}:{record_id}:rev)
    #   · **红字冲销,不删不改** —— 冲销写一条 quantity 为负的流水,原记录留着
    #   · FK 是开的(get_db 里 PRAGMA foreign_keys = ON)→ product_stock 引用 product,
    #     所以**有库存的商品不允许直接删**(删除会被 FK 拦下)

    # ── 3.1 warehouses 仓库 ─────────────────────────────────────────
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS warehouses (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            code        TEXT    NOT NULL UNIQUE,      -- 'FR' / 'TX'
            name        TEXT    NOT NULL,             -- '芙蓉仓' / '田心仓'
            is_default  INTEGER NOT NULL DEFAULT 0,
            sort_order  INTEGER NOT NULL DEFAULT 0,
            status      INTEGER NOT NULL DEFAULT 1,
            created_at  TEXT DEFAULT (datetime('now','localtime')),
            updated_at  TEXT DEFAULT (datetime('now','localtime'))
        )
    ''')
    # 唯一的种子行:芙蓉仓(id=1)。INSERT OR IGNORE → 幂等,重复跑不报错。
    cursor.execute(
        "INSERT OR IGNORE INTO warehouses (code, name, is_default, sort_order) "
        "VALUES ('FR', '芙蓉仓', 1, 1)"
    )

    # ── 3.2 product_stock 当前库存(仓库 × SKU)──────────────────────
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS product_stock (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            warehouse_id  INTEGER NOT NULL,
            product_id    INTEGER NOT NULL,
            quantity      REAL    NOT NULL DEFAULT 0,   -- 折算到 product.base_unit;is_tracking=1 时才对外显示
            is_tracking   INTEGER NOT NULL DEFAULT 0,   -- 1=已纳入库存计算;0=未纳入(对外按 0 处理)
            tracking_started_at TEXT,                  -- 纳入库存计算的起始时刻
            updated_at    TEXT DEFAULT (datetime('now','localtime')),
            UNIQUE(warehouse_id, product_id),
            FOREIGN KEY (warehouse_id) REFERENCES warehouses(id),
            FOREIGN KEY (product_id)   REFERENCES product(product_id)
        )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_product_stock_wh ON product_stock(warehouse_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_product_stock_product ON product_stock(product_id)')

    # ── 3.3 stock_ledger 库存流水(一切的唯一来源)──────────────────
    # ⭐ 阶段 1.6:DDL 存成模板,「重建表加外键」时用 .replace('{tbl}', ...) 复用同一份定义
    #    —— 避免建表语句和重建语句两处 truth 漂移。
    #    ⚠️ 用 replace 不用 format:DDL 注释里有 `{record_id}` 等花括号,format 会炸。
    DDL_LEDGER = '''
        CREATE TABLE IF NOT EXISTS {tbl} (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            warehouse_id  INTEGER NOT NULL,
            biz_type      TEXT    NOT NULL,   -- 'opening' | 'inbound' | 'adjust' | 'outbound' | 'loading'
            uniq_key      TEXT    NOT NULL,   -- 幂等键: inbound:{record_id} / adjust:{seq} / {biz}:{record_id}:rev
            order_pk      INTEGER NOT NULL DEFAULT 0,
            record_id     INTEGER NOT NULL DEFAULT 0,
            product_id    INTEGER NOT NULL,
            quantity      REAL    NOT NULL,   -- 基本单位数量;红字为负
            unit          TEXT    NOT NULL,   -- 折算后的基本单位
            raw_quantity  REAL    NOT NULL DEFAULT 0,
            raw_unit      TEXT    NOT NULL DEFAULT '',
            factor        REAL    NOT NULL DEFAULT 1,
            is_reversal   INTEGER NOT NULL DEFAULT 0,
            posting_id    INTEGER,
            operator_id   INTEGER,
            memo          TEXT    NOT NULL DEFAULT '',
            occurred_at   TEXT    NOT NULL DEFAULT '',   -- ⭐A4 业务日期(取订单 date);期间汇总/盘点切分一律用它
            balance_after REAL,                         -- ⭐A1 本笔记账后该(仓,SKU)的余额快照
            unit_cost     REAL,                         -- ⭐B档预留:每个基本单位的成本(可空,V1 不写)
            amount        REAL,                         -- ⭐B档预留:本笔金额 = quantity × unit_cost(可空,V1 不写)
            created_at    TEXT DEFAULT (datetime('now','localtime')),  -- 记账时刻(系统时间,不用于期间汇总)
            UNIQUE(uniq_key),
            FOREIGN KEY (warehouse_id) REFERENCES warehouses(id),
            FOREIGN KEY (posting_id)   REFERENCES stock_postings(id)
        )
    '''
    cursor.execute(DDL_LEDGER.replace('{tbl}', 'stock_ledger'))
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_stock_ledger_product ON stock_ledger(product_id, warehouse_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_stock_ledger_posting ON stock_ledger(posting_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_stock_ledger_created ON stock_ledger(created_at)')

    # ── 幂等迁移:给已建好的 stock_ledger 补 A 档 / B 档新列 ──────────
    # 2026-11-16 阶段 1.5。建表语句只对**新建**的库生效;存量库要靠这段 ALTER 补列。
    # ALTER TABLE ADD COLUMN 是追加操作,不重写表、不锁表;表当时全 0 行 → 零数据风险。
    # ⚠️ occurred_at 带 NOT NULL,所以 DEFAULT 必须是 '' 而不是 NULL(SQLite 的硬性要求)。
    # ⚠️⚠️ **这段 ALTER 必须排在下面 CREATE INDEX 之前** ——
    #    存量表还没有 occurred_at 列时,`CREATE INDEX ... ON stock_ledger(occurred_at)`
    #    会直接报 `no such column: occurred_at`,整个 init_db() 崩掉 → Flask 起不来。
    for _col, _ddl in (
        ('occurred_at', "occurred_at TEXT NOT NULL DEFAULT ''"),
        ('balance_after', 'balance_after REAL'),
        ('unit_cost', 'unit_cost REAL'),
        ('amount', 'amount REAL'),
    ):
        _cols = {r['name'] for r in cursor.execute('PRAGMA table_info(stock_ledger)').fetchall()}
        if _col not in _cols:
            cursor.execute(f'ALTER TABLE stock_ledger ADD COLUMN {_ddl}')

    cursor.execute('CREATE INDEX IF NOT EXISTS idx_stock_ledger_occurred ON stock_ledger(occurred_at)')

    # ── ⭐A3 只追加:数据库层禁止改 / 删流水(光靠应用层约定挡不住手工 SQL)──
    # ⚠️ 加了这几个触发器后,流水**永远**不能 UPDATE / DELETE / REPLACE ——
    #    改错只能走红字冲销(写一条 quantity 取负的流水),对账脚本也**不能**改历史流水。
    #
    # ⚠️⚠️ **为什么必须是 3 条**:实测 SQLite 3.45.3,前两条能挡 UPDATE / DELETE、
    #    放行正常 INSERT,但 **`INSERT OR REPLACE` 在默认 `recursive_triggers=0` 下
    #    不触发 DELETE 触发器** → 可无声把流水数量从 10 改成 999。
    #    第 3 条 `BEFORE INSERT ... WHEN EXISTS(...)` 在冲突旧行被删之前触发,
    #    故能同时拦住 INSERT OR REPLACE / ON CONFLICT DO UPDATE / DO NOTHING / OR IGNORE,
    #    **无需开任何 PRAGMA**。代价:幂等写法必须「先查后写」(见 models/inventory.py)。
    # 存成变量,阶段 1.6 重建表时(DROP 会连带删掉触发器)要原样建回来。
    TRG_LEDGER = (
        '''
        CREATE TRIGGER IF NOT EXISTS trg_stock_ledger_no_update
        BEFORE UPDATE ON stock_ledger
        BEGIN SELECT RAISE(ABORT, 'stock_ledger 只可追加;改错请走红字冲销'); END;
    ''',
        '''
        CREATE TRIGGER IF NOT EXISTS trg_stock_ledger_no_delete
        BEFORE DELETE ON stock_ledger
        BEGIN SELECT RAISE(ABORT, 'stock_ledger 只可追加;禁止删除'); END;
    ''',
        '''
        CREATE TRIGGER IF NOT EXISTS trg_stock_ledger_no_reuse
        BEFORE INSERT ON stock_ledger
        WHEN EXISTS (SELECT 1 FROM stock_ledger WHERE uniq_key = NEW.uniq_key)
        BEGIN SELECT RAISE(ABORT, 'uniq_key 不可复用;禁止 REPLACE / UPSERT 改账'); END;
    ''',
    )
    for _t in TRG_LEDGER:
        cursor.execute(_t)

    # ── 3.4 stock_postings 过账批次 ───────────────────────────────
    DDL_POSTINGS = '''
        CREATE TABLE IF NOT EXISTS {tbl} (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            biz_type       TEXT    NOT NULL DEFAULT 'inbound',
            order_pk       INTEGER NOT NULL,
            warehouse_id   INTEGER NOT NULL,
            posted_at      TEXT    NOT NULL,
            operator_id    INTEGER,
            operator_name  TEXT,
            status         TEXT    NOT NULL DEFAULT 'posted',   -- 'posted' | 'reversed'
            reversed_at    TEXT,
            posted_count   INTEGER NOT NULL DEFAULT 0,
            posted_qty     REAL    NOT NULL DEFAULT 0,
            skipped_count  INTEGER NOT NULL DEFAULT 0,
            note           TEXT    NOT NULL DEFAULT '',
            FOREIGN KEY (warehouse_id) REFERENCES warehouses(id)
        )
    '''
    cursor.execute(DDL_POSTINGS.replace('{tbl}', 'stock_postings'))
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_stock_postings_order ON stock_postings(biz_type, order_pk)')

    # ── 3.5 stock_posting_skips 跳过明细(待处理清单)───────────────
    DDL_SKIPS = '''
        CREATE TABLE IF NOT EXISTS {tbl} (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            posting_id     INTEGER NOT NULL,
            order_pk       INTEGER NOT NULL,
            record_id      INTEGER NOT NULL,
            product_name   TEXT,
            specification  TEXT,
            raw_quantity   TEXT,
            raw_unit       TEXT,
            reason_code    TEXT NOT NULL,   -- 'no_product' | 'no_conversion' | 'bad_base_unit' | 'bad_qty' | 'not_tracking'
            reason_text    TEXT NOT NULL,
            resolved       INTEGER NOT NULL DEFAULT 0,
            created_at     TEXT DEFAULT (datetime('now','localtime')),
            FOREIGN KEY (posting_id) REFERENCES stock_postings(id)
        )
    '''
    cursor.execute(DDL_SKIPS.replace('{tbl}', 'stock_posting_skips'))
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_stock_skips_posting ON stock_posting_skips(posting_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_stock_skips_unresolved ON stock_posting_skips(resolved)')

    # ── 3.7 stock_counts / stock_count_items 盘点单(对齐 point_count 会话模式)──
    DDL_COUNTS = '''
        CREATE TABLE IF NOT EXISTS {tbl} (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            warehouse_id  INTEGER NOT NULL DEFAULT 1,   -- 单仓设计:固定 1(芙蓉仓)
            title         TEXT    NOT NULL,              -- '11月全仓盘点'
            remark        TEXT    NOT NULL DEFAULT '',
            status        TEXT    NOT NULL DEFAULT 'open',-- 'open' | 'closed'
            total_items   INTEGER NOT NULL DEFAULT 0,   -- 应盘商品数
            counted_items INTEGER NOT NULL DEFAULT 0,   -- 已录数
            diff_items    INTEGER NOT NULL DEFAULT 0,   -- 有差异数
            created_by    INTEGER,
            created_at    TEXT    NOT NULL,
            as_of         TEXT    NOT NULL DEFAULT '',   -- ⭐A5 盘点基准时点(建单时刻);提交时按它切分期间流水
            closed_at     TEXT,
            FOREIGN KEY (warehouse_id) REFERENCES warehouses(id)
        )
    '''
    cursor.execute(DDL_COUNTS.replace('{tbl}', 'stock_counts'))
    # 幂等迁移:存量库补 as_of(A5)
    _cols = {r['name'] for r in cursor.execute('PRAGMA table_info(stock_counts)').fetchall()}
    if 'as_of' not in _cols:
        cursor.execute("ALTER TABLE stock_counts ADD COLUMN as_of TEXT NOT NULL DEFAULT ''")
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS stock_count_items (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            count_id        INTEGER NOT NULL,
            product_id      INTEGER NOT NULL,
            book_quantity   REAL    NOT NULL DEFAULT 0,  -- 账面数(建单时快照)
            actual_quantity REAL,                        -- 实盘数;NULL = 待录
            counted_at      TEXT,
            UNIQUE(count_id, product_id),
            FOREIGN KEY (count_id) REFERENCES stock_counts(id)
        )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_stock_count_items_count ON stock_count_items(count_id)')

    # ── 3.8 ⭐阶段 1.6:给 4 张库存表补外键约束(B 方案)───────────────
    # 为什么要补:全库 24 张表都建了 FK,库存这 4 张是唯一的例外 → 风格不统一,
    # 而且「流水指向一个不存在的仓 / 跳过明细指向一个已删的批次」这类脏数据没人拦。
    # 为什么能补:SQLite 的 ALTER TABLE 加不了外键约束 → 只能 DROP + CREATE 重建表。
    #             但这 4 张表当时**全 0 行** → 重建零数据损失(窗口期)。
    # 顺序:**被引用方先建**(stock_postings → stock_ledger → stock_posting_skips → stock_counts),
    #       否则重建中间会出现悬空 FK。
    # ⚠️ 仓库引用的 FK 会不会「挡住删仓」?—— 不会:
    #    `product_stock.warehouse_id → warehouses(id)` 早就建了,只要该仓有过库存行就已经拦住了;
    #    且 `warehouses.status` 的存在说明设计上是「停用代替删除」。
    # ⚠️ 但 **FK 拦下来的错在 Flask 里是 500** → 阶段 3 的过账 API 仍要做应用层校验,
    #    把「仓库不存在」翻译成 400 + 中文提示(FK 只当兜底)。
    _rebuild_table_for_fk(
        conn, cursor, 'stock_postings', DDL_POSTINGS,
        want_fks=[('warehouse_id', 'warehouses')],
        indexes=('CREATE INDEX IF NOT EXISTS idx_stock_postings_order '
                 'ON stock_postings(biz_type, order_pk)',),
    )
    _rebuild_table_for_fk(
        conn, cursor, 'stock_ledger', DDL_LEDGER,
        want_fks=[('warehouse_id', 'warehouses'), ('posting_id', 'stock_postings')],
        indexes=('CREATE INDEX IF NOT EXISTS idx_stock_ledger_product '
                 'ON stock_ledger(product_id, warehouse_id)',
                 'CREATE INDEX IF NOT EXISTS idx_stock_ledger_posting ON stock_ledger(posting_id)',
                 'CREATE INDEX IF NOT EXISTS idx_stock_ledger_created ON stock_ledger(created_at)',
                 'CREATE INDEX IF NOT EXISTS idx_stock_ledger_occurred ON stock_ledger(occurred_at)'),
        triggers=TRG_LEDGER,
    )
    _rebuild_table_for_fk(
        conn, cursor, 'stock_posting_skips', DDL_SKIPS,
        want_fks=[('posting_id', 'stock_postings')],
        indexes=('CREATE INDEX IF NOT EXISTS idx_stock_skips_posting ON stock_posting_skips(posting_id)',
                 'CREATE INDEX IF NOT EXISTS idx_stock_skips_unresolved ON stock_posting_skips(resolved)'),
    )
    _rebuild_table_for_fk(
        conn, cursor, 'stock_counts', DDL_COUNTS,
        want_fks=[('warehouse_id', 'warehouses')],
    )

    # ── 3.6 现有表改动:三张订单表加 warehouse_id(默认 1 = 芙蓉仓)──
    # 全新库走上面的 CREATE TABLE(列已直接写进去);
    # 存量库靠这段 ALTER 补列(SQLite 的 ALTER 是追加操作,不重写表)。
    for tbl in ('inbound_orders', 'shipping_orders', 'loading_orders'):
        try:
            cursor.execute(f'ALTER TABLE {tbl} ADD COLUMN warehouse_id INTEGER NOT NULL DEFAULT 1')
        except Exception:
            pass  # 列已存在

    # ── 启动 seed:为管理页要列出的分类节点占位空白提示词 ─────────
    # 2026-08-08 新增:不再依赖 classify_record 自动分类精度,
    # 而是让所有 Level 3 品类先 seed 占位空白行,前端管理页手动微调。
    # 2026-08-09 调整:覆盖范围扩展到 (L3 全部 ∪ L4 无商品纯分类)。
    # 幂等(seed_management_categories 内部 SELECT 后 INSERT),失败 log 不 raise。
    try:
        from models.category_prompt import CategoryPrompt
        inserted = CategoryPrompt.seed_management_categories()
        if inserted:
            import logging as _logging
            _logging.getLogger('worklog.init_db').info(
                f'seeded {inserted} blank category prompts for management categories')
    except Exception:
        import logging as _logging
        _logging.getLogger('worklog.init_db').exception(
            'seed blank category prompts failed (non-fatal)')

    conn.commit()
    conn.close()

