"""阶段 1.6:库存表外键一致性 + 全新库补列回归测试。

覆盖三件事:
  1. **全新库从零 init_db()** → 5 组库存 FK 齐全、3 个守卫触发器齐全、
     shipping_images 补齐 manual_count / is_unload(阶段 1.6 修的 bug)。
  2. **幂等** → 连跑两次 init_db(),表清单与每表行数都不变。
  3. **重建路径** → 把库存表改回「无 FK」的旧形态,再跑 init_db(),
     FK 必须被补回来,且**数据一行不丢**、触发器/索引照常重建。

⚠️ 这些用例自己建临时库,**不碰生产库也不碰 worklog_test.db** ——
   跑完 fixture 会把 WORKLOG_DB 环境变量还原。
"""
import importlib
import os
import re
import sqlite3

import pytest

# 5 组库存 FK:表 → [(列, 父表)]
EXPECTED_STOCK_FKS = {
    'product_stock': [('warehouse_id', 'warehouses'), ('product_id', 'product')],
    'stock_ledger': [('warehouse_id', 'warehouses'), ('posting_id', 'stock_postings')],
    'stock_postings': [('warehouse_id', 'warehouses')],
    'stock_posting_skips': [('posting_id', 'stock_postings')],
    'stock_counts': [('warehouse_id', 'warehouses')],
    'stock_count_items': [('count_id', 'stock_counts')],
}

EXPECTED_TRIGGERS = {
    'trg_stock_ledger_no_update',
    'trg_stock_ledger_no_delete',
    'trg_stock_ledger_no_reuse',
}


def _connect(db):
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row
    return conn


def _reopen(conn, db):
    """关掉旧连接重开一个。

    ⚠️ 必须这样做:init_db() 用的是**另一个**连接,它重建了表(DROP + RENAME)。
    旧连接还持着旧 schema 的读快照,PRAGMA foreign_key_list 会读到**重建前**的
    信息(看起来像「重建没生效」)。这是 SQLite 的 schema 缓存,不是重建逻辑的锅。
    """
    conn.close()
    return _connect(db)


def _fks(conn, table):
    return {(r['from'], r['table']) for r in conn.execute(f'PRAGMA foreign_key_list({table})')}


def _cols(conn, table):
    return [r['name'] for r in conn.execute(f'PRAGMA table_info({table})')]


def _strip_fk(sql):
    """把 CREATE TABLE 语句里的 FOREIGN KEY 子句去掉 —— 造出「阶段 1.6 之前」的旧表形态。"""
    out = re.sub(r',\s*FOREIGN KEY\s*\([^)]*\)\s*REFERENCES\s+\w+\([^)]*\)', '', sql)
    out = re.sub(r',(\s*)\)', r'\1)', out)   # 去掉 FK 后可能留下的悬空逗号
    return out


@pytest.fixture
def fresh_db(tmp_path):
    """建一个空库跑 init_db(),结束后还原环境变量。"""
    db = str(tmp_path / 'fresh.db')
    old_env = os.environ.get('WORKLOG_DB')
    os.environ['WORKLOG_DB'] = db
    import models._db as db_mod
    importlib.reload(db_mod)          # DB_PATH 在 import 时读 env → 必须 reload

    from models._init import init_db
    init_db()

    yield db, init_db

    if old_env is None:
        os.environ.pop('WORKLOG_DB', None)
    else:
        os.environ['WORKLOG_DB'] = old_env
    importlib.reload(db_mod)          # 还原成 conftest 设的 worklog_test.db


def test_fresh_db_has_all_stock_fks(fresh_db):
    """全新库从零建 → 5 组 FK 齐全。"""
    db, _ = fresh_db
    conn = _connect(db)
    for table, want in EXPECTED_STOCK_FKS.items():
        got = _fks(conn, table)
        for col, parent in want:
            assert (col, parent) in got, f'{table} 缺 FK {col} → {parent}(实际 {sorted(got)})'
    conn.close()


def test_fresh_db_has_guard_triggers(fresh_db):
    """全新库从零建 → 3 个「流水只追加」守卫触发器齐全。"""
    db, _ = fresh_db
    conn = _connect(db)
    have = {r['name'] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert EXPECTED_TRIGGERS <= have, f'缺触发器 {EXPECTED_TRIGGERS - have}'
    conn.close()


def test_shipping_images_columns_completed_on_fresh_db(fresh_db):
    """回归:阶段 1.6 修的 bug —— 全新库图片表曾缺列。

    实测踩中 3 处(都是「ALTER 补列排在 CREATE TABLE 之前」导致):
      ① shipping_images 缺 manual_count / is_unload
         → 摆放图「手动覆盖支数」「卸货取负」直接报错
      ② loading_order_images 缺 15 列(全新库只有 5 列)
         → 装柜的行级标签图 / 摆放图计数全用不了
      ③ shipping_orders 缺 doc_number
         → ShippingOrder.set_doc_number() 直接报错
    """
    db, _ = fresh_db
    conn = _connect(db)

    img_cols = ('source', 'record_pk', 'sort_order', 'match_status', 'match_score',
                'reason', 'human_verified', 'match_source', 'bg_color', 'source_tag',
                'circles', 'mark_scale', 'loose_count', 'manual_count', 'is_unload')
    for table in ('shipping_images', 'inbound_images', 'loading_order_images'):
        cols = _cols(conn, table)
        for need in img_cols:
            assert need in cols, f'{table} 缺列 {need}(实际 {cols})'

    for table in ('shipping_orders', 'inbound_orders', 'loading_orders'):
        cols = _cols(conn, table)
        assert 'doc_number' in cols, f'{table} 缺列 doc_number(实际 {cols})'
        assert 'img_cols' in cols, f'{table} 缺列 img_cols(实际 {cols})'

    conn.close()


def test_init_db_is_idempotent(fresh_db):
    """连跑两次 init_db() → 表清单与行数都不变。"""
    db, init_db = fresh_db
    conn = _connect(db)

    def snap():
        tables = [r['name'] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
        return tables, {t: conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                        for t in tables}

    before = snap()
    init_db()
    after = snap()
    assert before[0] == after[0], '表清单变了'
    assert before[1] == after[1], '行数被改动了'
    conn.close()

def test_rebuild_restores_fk_without_losing_data(fresh_db):
    """重建路径:无 FK 的旧表 → init_db() → FK 补回 + 数据一行不丢。

    这条覆盖 `_rebuild_table_for_fk` 的 4 道坎:
    DROP 被引用的父表(legacy_alter_table)、触发器/索引连带删除后重建、
    按列名搬数据(新旧列顺序不同)、重复执行幂等。
    """
    db, init_db = fresh_db
    conn = _connect(db)

    # 造数据:一个过账批次 + 一条流水
    conn.execute("INSERT INTO warehouses (code,name,is_default,sort_order) "
                 "VALUES ('TX','田心仓',0,2)")
    conn.execute("INSERT INTO stock_postings (biz_type,order_pk,warehouse_id,posted_at) "
                 "VALUES ('inbound',123,1,'2026-11-16 10:00:00')")
    pid = conn.execute('SELECT last_insert_rowid()').fetchone()[0]
    conn.execute(
        "INSERT INTO stock_ledger (warehouse_id,biz_type,uniq_key,order_pk,record_id,"
        "product_id,quantity,unit,raw_quantity,raw_unit,factor,occurred_at,posting_id) "
        "VALUES (1,'inbound','inbound:1',1,1,1,5.0,'米',5.0,'米',1.0,'2026-11-16',?)",
        (pid,))
    conn.commit()

    # 把两张表改回「无 FK」形态(行搬回去,模拟旧库)
    for tbl in ('stock_postings', 'stock_ledger'):
        sql = conn.execute("SELECT sql FROM sqlite_master WHERE name=?", (tbl,)).fetchone()[0]
        cols = _cols(conn, tbl)
        collist = ', '.join(cols)
        rows = [tuple(r) for r in conn.execute(f'SELECT {collist} FROM {tbl}')]
        conn.execute('PRAGMA foreign_keys=OFF')
        conn.execute('PRAGMA legacy_alter_table=ON')
        conn.execute(f'DROP TABLE {tbl}')
        conn.execute(_strip_fk(sql))
        if rows:
            conn.executemany(
                f'INSERT INTO {tbl} ({collist}) VALUES ({",".join(["?"] * len(cols))})', rows)
        conn.execute('PRAGMA legacy_alter_table=OFF')
        conn.execute('PRAGMA foreign_keys=ON')
        conn.commit()

    assert not _fks(conn, 'stock_ledger'), '前置条件不成立:stock_ledger 本就没有 FK'

    # 触发重建
    init_db()
    conn = _reopen(conn, db)      # 见 _reopen 的注释:必须重开连接才看得到新 schema

    assert ('warehouse_id', 'warehouses') in _fks(conn, 'stock_postings')
    assert ('warehouse_id', 'warehouses') in _fks(conn, 'stock_ledger')
    assert ('posting_id', 'stock_postings') in _fks(conn, 'stock_ledger')

    # 数据没丢
    assert conn.execute('SELECT COUNT(*) FROM stock_postings').fetchone()[0] == 1
    assert conn.execute('SELECT COUNT(*) FROM stock_ledger').fetchone()[0] == 1

    # 触发器 / 索引回来了
    have = {r['name'] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'")}
    assert EXPECTED_TRIGGERS <= have, f'重建后缺触发器 {EXPECTED_TRIGGERS - have}'
    idx = {r['name'] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_%'")}
    for need in ('idx_stock_ledger_product', 'idx_stock_ledger_posting',
                 'idx_stock_ledger_created', 'idx_stock_ledger_occurred',
                 'idx_stock_postings_order'):
        assert need in idx, f'重建后缺索引 {need}'
    conn.close()


def test_rebuild_is_idempotent(fresh_db):
    """重建之后再跑一次 init_db() → 不再重建(幂等),数据仍不变。"""
    db, init_db = fresh_db
    conn = _connect(db)
    conn.execute("INSERT INTO stock_postings (biz_type,order_pk,warehouse_id,posted_at) "
                 "VALUES ('inbound',9,1,'2026-11-16 10:00:00')")
    conn.commit()
    init_db()
    init_db()
    assert conn.execute('SELECT COUNT(*) FROM stock_postings').fetchone()[0] == 1
    conn.close()
