"""分类提示词启动 seed + 管理页 API 测试套件。

合约(2026-08-08 新增,2026-08-09 扩展):
- CategoryPrompt.seed_blank_for_level(level=3) — 仅 Level 3 的 seed,幂等
   (保留用于单测;实际启动改调 seed_management_categories)
- CategoryPrompt.seed_management_categories() — 启动 seed,幂等,
   为 (Level 3 全部 ∪ Level 4 无商品纯分类) 各 INSERT 一条占位空白行
- CategoryPrompt.update_text(pid, text) — 仅更新 prompt_text,允许空串
- CategoryPrompt.list_active_by_category(category_code, ...) — 按 code 过滤
- list_management_categories() — 返回 Level 3 全部 + Level 4 无商品纯分类(active)
- blueprints/category_prompts_manage.py:
    GET  /manage/category-prompts → 渲染模板
    GET  /api/v1/manage/category-prompts/list → 列表 + 过滤
    PATCH /api/v1/manage/category-prompts/<id> → 更新 prompt_text
    POST /api/v1/manage/category-prompts/bulk → 批量更新

设计决策:
- seed 用"先 SELECT 后 INSERT"模式,幂等
- seed 跳过用户已填的行(prompt_text != '' 不被覆盖)
- 用户归档占位后下次 seed 会重出一行(已知行为,避免状态机)
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models._db as _db

REPO_ROOT = Path(__file__).resolve().parent.parent


def _get_row_or_fail(pid: int) -> dict:
    """get_by_id 的 None-safe 包装:测试用,失败立刻 fail。"""
    from models.category_prompt import CategoryPrompt
    row = CategoryPrompt.get_by_id(pid)
    assert row is not None, f'get_by_id({pid}) returned None'
    return row


def _insert_level3_category(cursor, code: str, name: str, status: int = 1):
    """辅助:直接 SQL 插一条 Level 3 分类(测试 setup 用)。"""
    cursor.execute(
        """INSERT INTO product_categories
           (category_code, category_name, parent_id, level, sort_order,
            status, created_at, updated_at)
           VALUES (?, ?, 1, 3, 1, ?, datetime('now'), datetime('now'))""",
        (code, name, status)
    )


class _TempDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        self._orig = _db.DB_PATH
        _db.DB_PATH = self.tmp.name
        from models import init_db
        init_db()
        from app import create_app
        self.client = create_app().test_client()

    def tearDown(self):
        _db.DB_PATH = self._orig
        for ext in ('', '-wal', '-shm'):
            p = self.tmp.name + ext
            if os.path.exists(p):
                try:
                    os.unlink(p)
                except OSError:
                    pass

    def _login(self):
        from models.tasks_flow import StaffDB
        sid = StaffDB.create('test', '调度')['id']
        with self.client.session_transaction() as s:
            s['operator_id'] = sid


# ──────────────────────────────────────────────────────────────────
# 1) seed_blank_for_level 行为
# ──────────────────────────────────────────────────────────────────
class SeedBlankForLevelTests(_TempDb):
    def test_seed_creates_blank_for_each_level3(self):
        """每个 Level 3 品类应有 1 条占位空白行 (prompt_text='', scope='category')。"""
        conn = _db.get_db(); cur = conn.cursor()
        _insert_level3_category(cur, '0701', '猪皮纹HA')
        _insert_level3_category(cur, '0204', '无纺布')
        _insert_level3_category(cur, '0301', '喷胶')
        conn.commit(); conn.close()

        from models.category_prompt import CategoryPrompt
        inserted = CategoryPrompt.seed_blank_for_level(level=3)
        self.assertEqual(inserted, 3)

        rows = CategoryPrompt.list_active_by_category(include_empty=True)
        codes = sorted([r['category_code'] for r in rows])
        self.assertEqual(codes, ['0204', '0301', '0701'])
        for r in rows:
            self.assertEqual(r['prompt_text'], '')
            self.assertEqual(r['status'], 'active')
            self.assertEqual(r['scope'], 'category')

    def test_seed_is_idempotent(self):
        """连续调 2 次 seed → 行数不变。"""
        conn = _db.get_db(); cur = conn.cursor()
        _insert_level3_category(cur, '0701', '猪皮纹HA')
        _insert_level3_category(cur, '0204', '无纺布')
        conn.commit(); conn.close()

        from models.category_prompt import CategoryPrompt
        n1 = CategoryPrompt.seed_blank_for_level(level=3)
        n2 = CategoryPrompt.seed_blank_for_level(level=3)
        self.assertEqual(n1, 2)
        self.assertEqual(n2, 0)  # 第二次 0 inserted
        # 总行数仍 = 2
        rows = CategoryPrompt.list_active_by_category(include_empty=True)
        self.assertEqual(len(rows), 2)

    def test_seed_does_not_overwrite_user_filled(self):
        """用户已填的行不被 seed 清空。"""
        conn = _db.get_db(); cur = conn.cursor()
        _insert_level3_category(cur, '0701', '猪皮纹HA')
        conn.commit(); conn.close()

        from models.category_prompt import CategoryPrompt
        CategoryPrompt.seed_blank_for_level(level=3)
        # 用户编辑这一行,填入文本
        rows = CategoryPrompt.list_active_by_category(category_code='0701')
        pid = rows[0]['id']
        CategoryPrompt.update_text(pid, '厚度 ±0.1mm 可放宽')

        # 再次 seed
        n2 = CategoryPrompt.seed_blank_for_level(level=3)
        self.assertEqual(n2, 0)  # 已有占位行,不再插
        # 用户填的内容应保留
        row = _get_row_or_fail(pid)
        self.assertEqual(row['prompt_text'], '厚度 ±0.1mm 可放宽')

    def test_seed_skips_archived_placeholders_known_behavior(self):
        """归档占位行后下次 seed 又会出一行占位 —— 已知行为,记录在 docstring。

        用户想"关闭某个 category 的提示词"应直接 update_text(pid, '') 留空
        而不是 archive;归档专门给"我以后不想再看到这条"的场景。
        """
        conn = _db.get_db(); cur = conn.cursor()
        _insert_level3_category(cur, '0701', '猪皮纹HA')
        conn.commit(); conn.close()

        from models.category_prompt import CategoryPrompt
        CategoryPrompt.seed_blank_for_level(level=3)
        # 归档占位行
        rows = CategoryPrompt.list_active_by_category(category_code='0701')
        pid = rows[0]['id']
        CategoryPrompt.archive(pid)
        # 再 seed
        n2 = CategoryPrompt.seed_blank_for_level(level=3)
        self.assertEqual(n2, 1)  # 旧占位已 archived,新占位会插入
        # DB 现在有 2 行(1 archived + 1 active 占位)
        all_rows = CategoryPrompt.list_active_by_category(category_code='0701')
        self.assertEqual(len(all_rows), 1)  # 只看 active
        # 但 get_by_id 仍能查到 archived 那条
        archived = _get_row_or_fail(pid)
        self.assertEqual(archived['status'], 'archived')

    def test_seed_no_op_when_no_level3(self):
        """Level 3 没有任何 category 时,seed 返回 0,不报错。"""
        from models.category_prompt import CategoryPrompt
        n = CategoryPrompt.seed_blank_for_level(level=3)
        self.assertEqual(n, 0)


# ──────────────────────────────────────────────────────────────────
# 2) update_text 行为
# ──────────────────────────────────────────────────────────────────
class UpdateTextTests(_TempDb):
    def test_update_text_allows_blank(self):
        """管理页"清空"按钮:update_text(pid, '') 不抛错。"""
        conn = _db.get_db(); cur = conn.cursor()
        _insert_level3_category(cur, '0701', '猪皮纹HA')
        conn.commit(); conn.close()

        from models.category_prompt import CategoryPrompt
        CategoryPrompt.seed_blank_for_level(level=3)
        rows = CategoryPrompt.list_active_by_category(category_code='0701')
        pid = rows[0]['id']

        ok = CategoryPrompt.update_text(pid, '')
        self.assertTrue(ok)
        row = _get_row_or_fail(pid)
        self.assertEqual(row['prompt_text'], '')

    def test_update_text_nonexistent_returns_false(self):
        """不存在的 pid 返回 False,rowcount=0。"""
        from models.category_prompt import CategoryPrompt
        ok = CategoryPrompt.update_text(99999, 'x')
        self.assertFalse(ok)


# ──────────────────────────────────────────────────────────────────
# 3) list_active_by_category 行为
# ──────────────────────────────────────────────────────────────────
class ListActiveByCategoryTests(_TempDb):
    def test_filters_by_category_code(self):
        conn = _db.get_db(); cur = conn.cursor()
        _insert_level3_category(cur, '0701', '猪皮纹HA')
        _insert_level3_category(cur, '0204', '无纺布')
        conn.commit(); conn.close()

        from models.category_prompt import CategoryPrompt
        CategoryPrompt.seed_blank_for_level(level=3)

        rows_0701 = CategoryPrompt.list_active_by_category(category_code='0701')
        rows_0204 = CategoryPrompt.list_active_by_category(category_code='0204')
        rows_all = CategoryPrompt.list_active_by_category()
        self.assertEqual(len(rows_0701), 1)
        self.assertEqual(rows_0701[0]['category_code'], '0701')
        self.assertEqual(len(rows_0204), 1)
        self.assertEqual(len(rows_all), 2)

    def test_include_empty_false_filters_blank(self):
        """include_empty=False → 只返回已填的行。"""
        conn = _db.get_db(); cur = conn.cursor()
        _insert_level3_category(cur, '0701', '猪皮纹HA')
        _insert_level3_category(cur, '0204', '无纺布')
        conn.commit(); conn.close()

        from models.category_prompt import CategoryPrompt
        CategoryPrompt.seed_blank_for_level(level=3)
        rows = CategoryPrompt.list_active_by_category(category_code='0701')
        CategoryPrompt.update_text(rows[0]['id'], '已填文本')

        all_rows = CategoryPrompt.list_active_by_category(include_empty=True)
        filled_rows = CategoryPrompt.list_active_by_category(include_empty=False)
        self.assertEqual(len(all_rows), 2)
        self.assertEqual(len(filled_rows), 1)
        self.assertEqual(filled_rows[0]['category_code'], '0701')


# ──────────────────────────────────────────────────────────────────
# 4) list_management_categories 模块函数
# ──────────────────────────────────────────────────────────────────
class ListManagementCategoriesTests(_TempDb):
    def test_returns_all_level3_and_deeper(self):
        conn = _db.get_db(); cur = conn.cursor()
        # Level 3 active
        _insert_level3_category(cur, '0701', '猪皮纹HA')
        _insert_level3_category(cur, '0204', '无纺布')
        # Level 3 inactive (status=0) → 排除
        _insert_level3_category(cur, '0901', '已弃用', status=0)
        # Level 2 → 排除(粒度太粗,不在此维护)
        cur.execute(
            """INSERT INTO product_categories
               (category_code, category_name, parent_id, level, sort_order,
                status, created_at, updated_at)
               VALUES ('01', '纸品类', NULL, 2, 1, 1, datetime('now'), datetime('now'))"""
        )
        # Level 4 纯分类(无商品) → 应包含
        cur.execute(
            """INSERT INTO product_categories
               (category_code, category_name, parent_id, level, sort_order,
                status, created_at, updated_at)
               VALUES ('070101', '猪皮纹子类', 1, 4, 1, 1, datetime('now'), datetime('now'))"""
        )
        # Level 4 商品分类(挂了 product) → 仍应包含(它是分类节点,非实际 SKU 行)
        cur.execute(
            """INSERT INTO product_categories
               (category_code, category_name, parent_id, level, sort_order,
                status, created_at, updated_at)
               VALUES ('070102', '猪皮纹成品', 1, 4, 2, 1, datetime('now'), datetime('now'))"""
        )
        l4_product_row_id = cur.lastrowid
        cur.execute(
            """INSERT INTO product
               (product_code, product_name, category_id, status, created_at, updated_at)
               VALUES ('P070102', '猪皮纹成品A', ?, 1, datetime('now'), datetime('now'))""",
            (l4_product_row_id,)
        )
        # Level 5 深层分类 → 应包含
        cur.execute(
            """INSERT INTO product_categories
               (category_code, category_name, parent_id, level, sort_order,
                status, created_at, updated_at)
               VALUES ('07010201', '猪皮纹成品细分', ?, 5, 1, 1, datetime('now'), datetime('now'))""",
            (l4_product_row_id,)
        )
        conn.commit(); conn.close()

        from models.category_prompt import list_management_categories
        cats = list_management_categories()
        codes = [c['category_code'] for c in cats]
        self.assertIn('0701', codes)
        self.assertIn('0204', codes)
        self.assertIn('070101', codes)    # L4 纯分类 → 包含
        self.assertIn('070102', codes)    # L4 商品分类 → 也包含(新规则)
        self.assertIn('07010201', codes)  # L5 深层 → 包含
        self.assertNotIn('0901', codes)   # inactive 排除
        self.assertNotIn('01', codes)     # Level 2 排除
        # 按 code 升序
        self.assertEqual(codes, sorted(codes))


# ──────────────────────────────────────────────────────────────────
# 5) 管理页 API 端点
# ──────────────────────────────────────────────────────────────────
class ManageApiTests(_TempDb):
    def test_manage_page_renders(self):
        """GET /manage/category-prompts → 200 + 含页面标题。"""
        self._login()
        res = self.client.get('/manage/category-prompts')
        self.assertEqual(res.status_code, 200)
        body = res.get_data(as_text=True)
        self.assertIn('分类提示词', body)

    def test_list_endpoint_returns_rows(self):
        """GET /api/v1/manage/category-prompts/list → 含 seed 数据。"""
        # 先 seed 几条
        conn = _db.get_db(); cur = conn.cursor()
        _insert_level3_category(cur, '0701', '猪皮纹HA')
        _insert_level3_category(cur, '0204', '无纺布')
        conn.commit(); conn.close()

        from models.category_prompt import CategoryPrompt
        CategoryPrompt.seed_blank_for_level(level=3)

        self._login()
        res = self.client.get('/api/v1/manage/category-prompts/list')
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data['success'])
        self.assertGreaterEqual(len(data['rows']), 2)
        codes = {r['category_code'] for r in data['rows']}
        self.assertIn('0701', codes)
        self.assertIn('0204', codes)

    def test_list_endpoint_filters_by_code(self):
        """?category_code=0701 → 只返回 0701。"""
        conn = _db.get_db(); cur = conn.cursor()
        _insert_level3_category(cur, '0701', '猪皮纹HA')
        _insert_level3_category(cur, '0204', '无纺布')
        conn.commit(); conn.close()

        from models.category_prompt import CategoryPrompt
        CategoryPrompt.seed_blank_for_level(level=3)

        self._login()
        res = self.client.get('/api/v1/manage/category-prompts/list?category_code=0701')
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertEqual(len(data['rows']), 1)
        self.assertEqual(data['rows'][0]['category_code'], '0701')

    def test_patch_endpoint_updates_text(self):
        """PATCH /api/v1/manage/category-prompts/<id> → 改 prompt_text。"""
        conn = _db.get_db(); cur = conn.cursor()
        _insert_level3_category(cur, '0701', '猪皮纹HA')
        conn.commit(); conn.close()

        from models.category_prompt import CategoryPrompt
        CategoryPrompt.seed_blank_for_level(level=3)
        rows = CategoryPrompt.list_active_by_category(category_code='0701')
        pid = rows[0]['id']

        self._login()
        res = self.client.patch(
            f'/api/v1/manage/category-prompts/{pid}',
            json={'prompt_text': '环保等级 ±0.1mm 放宽'}
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data['success'])
        # DB 已写入
        row = _get_row_or_fail(pid)
        self.assertEqual(row['prompt_text'], '环保等级 ±0.1mm 放宽')

    def test_patch_endpoint_allows_blank(self):
        """PATCH prompt_text='' 也允许。"""
        conn = _db.get_db(); cur = conn.cursor()
        _insert_level3_category(cur, '0701', '猪皮纹HA')
        conn.commit(); conn.close()

        from models.category_prompt import CategoryPrompt
        CategoryPrompt.seed_blank_for_level(level=3)
        rows = CategoryPrompt.list_active_by_category(category_code='0701')
        pid = rows[0]['id']
        # 先填一点
        CategoryPrompt.update_text(pid, 'foo')
        # 再清空
        self._login()
        res = self.client.patch(
            f'/api/v1/manage/category-prompts/{pid}',
            json={'prompt_text': ''}
        )
        self.assertEqual(res.status_code, 200)
        row = _get_row_or_fail(pid)
        self.assertEqual(row['prompt_text'], '')

    def test_patch_404_on_unknown_id(self):
        """PATCH 不存在的 id → 404。"""
        self._login()
        res = self.client.patch(
            '/api/v1/manage/category-prompts/99999',
            json={'prompt_text': 'x'}
        )
        self.assertEqual(res.status_code, 404)


if __name__ == '__main__':
    unittest.main()
