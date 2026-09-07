"""拷贝纸/日本纸行级图片 (copy_paper_images 表) 模型层测试。

Task 1 of 2026-09-06-copy-paper-row-buttons:
- DDL: copy_paper_images
- Model: CopyPaperImage (create / list_by_record / get_by_id / update_count / delete)
- 不进 OCR pipeline,不进 match-col,仅供人工参考 + 行级 total 比对
"""
import os
import pytest
from models._db import get_db, DB_PATH
from models.orders import CopyPaperImage


@pytest.fixture(scope="module", autouse=True)
def _ensure_table():
    """确保 copy_paper_images 表存在(conftest 只复制生产 db,
    新 DDL 需要 init_db() 主动跑一次)。module-scope 避免每个测试都跑全量迁移。"""
    from models import init_db
    init_db()
    yield


@pytest.fixture
def fresh_record(tmp_path):
    """Insert a minimal shipping_orders + shipping_records row, return record_pk.

    使用 uuid 后缀避免 UNIQUE(date,customer,order_num) 冲突 — 多次调用 fresh_record
    (跨测试) 不会撞键。
    """
    import uuid as _uuid
    cust = 'test-' + _uuid.uuid4().hex[:8]
    conn = get_db()
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO shipping_orders (date, customer, is_locked, created_at) "
        "VALUES (?, ?, 0, '2026-09-06 12:00:00')",
        ('2026-09-06', cust,))
    order_id = cur.lastrowid
    cur.execute(
        "INSERT INTO shipping_records (order_pk, product_name, specification, quantity, unit, created_at) "
        "VALUES (?, ?, ?, ?, ?, '2026-09-06 12:00:00')",
        (order_id, '拷贝纸A', 'A4', 5, '令'))
    record_id = cur.lastrowid
    conn.commit()
    conn.close()
    return record_id


def test_ddl_table_exists():
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='copy_paper_images'")
    assert cur.fetchone() is not None
    conn.close()


def test_create_and_list(fresh_record):
    rid = fresh_record
    img_id = CopyPaperImage.create(rid, 'upload/2026-09/x.jpg', 'x.jpg', 'label')
    assert isinstance(img_id, int) and img_id > 0

    rows = CopyPaperImage.list_by_record(rid)
    assert len(rows) == 1
    assert rows[0]['source'] == 'label'
    assert rows[0]['sheet_count'] is None
    assert rows[0]['file_path'] == 'upload/2026-09/x.jpg'


def test_update_count_int_and_none(fresh_record):
    rid = fresh_record
    iid = CopyPaperImage.create(rid, 'upload/2026-09/y.jpg', 'y.jpg', 'count')
    assert CopyPaperImage.update_count(iid, 100) is True
    assert CopyPaperImage.get_by_id(iid)['sheet_count'] == 100
    assert CopyPaperImage.update_count(iid, None) is True
    assert CopyPaperImage.get_by_id(iid)['sheet_count'] is None


def test_update_count_rejects_negative(fresh_record):
    rid = fresh_record
    iid = CopyPaperImage.create(rid, 'p', 'p', 'count')
    with pytest.raises(ValueError):
        CopyPaperImage.update_count(iid, -1)


def test_get_by_id_and_delete(fresh_record):
    rid = fresh_record
    iid = CopyPaperImage.create(rid, 'upload/2026-09/z.jpg', 'z.jpg', 'label')
    row = CopyPaperImage.get_by_id(iid)
    assert row is not None and row['id'] == iid

    assert CopyPaperImage.delete(iid) is True
    assert CopyPaperImage.get_by_id(iid) is None
    # 二次删应返回 False
    assert CopyPaperImage.delete(iid) is False
