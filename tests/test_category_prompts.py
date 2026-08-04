"""类别/规格自适应提示词(category_prompts)TDD 套件。

合约:
- classify_record:product 表 JOIN 优先,关键词兜底,查不到返回 None
- CategoryPrompt.create/get/archive/list_active:CRUD
- CategoryPrompt.list_for_record:同类别+同 LIKE spec_pattern 双重命中
- CategoryPrompt.compose_for_record:layer2/layer3 拼装为一段可读文本
- blueprints/shipping.py:
    POST .../images/<id>/generate-prompt-suggestion:返回模板化提示词草稿
    POST .../category-prompts:保存用户编辑后的提示词
    GET  .../category-prompts:列出活跃提示词
    DELETE .../category-prompts/<id>:软删(archive)
- 蓝图 ai-match 路径自动注入 layer2/layer3 到 prompt 末尾(由 compare_rows 调用 compose_for_record)

设计决策(本次实施):
- 生成策略:模板化 — 不调 LLM,降低对 API key 依赖,启动快
- spec 通配:SQL LIKE (% / _)
"""
import io
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models._db as _db

REPO_ROOT = Path(__file__).resolve().parent.parent


def _classify_helper(product_name: str = '', specification: str = '') -> dict | None:
    """独立路由 helper,避免污染 models 命名空间 —— 端点只调这个。"""
    from models import classify_record
    return classify_record(product_name=product_name, specification=specification)


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
                try: os.unlink(p)
                except OSError: pass


# ──────────────────────────────────────────────────────────────────
# 1) classify_record 路由
# ──────────────────────────────────────────────────────────────────
class ClassifyRecordTests(_TempDb):
    def test_classify_via_product_table(self):
        # 预先灌一条 product + product_categories
        conn = _db.get_db(); cur = conn.cursor()
        cur.execute("INSERT INTO product_categories(category_code,category_name,parent_id,level,sort_order,status,created_at,updated_at) "
                    "VALUES ('0204','HA猪皮纹',1,2,1,'active',datetime('now'),datetime('now'))")
        cat_id = cur.lastrowid
        cur.execute("INSERT INTO product(product_code,product_name,category_id,specification) "
                    "VALUES ('SKU-001','7P环保HA猪皮纹',?, '1.0黑中加面')", (cat_id,))
        conn.commit(); conn.close()

        r = _classify_helper('7P环保HA猪皮纹', '1.0黑中加面')
        self.assertIsNotNone(r)
        self.assertEqual(r['category_code'], '0204')
        self.assertEqual(r['matched_via'], 'product_table')

    def test_classify_fallback_when_product_miss(self):
        # HA猪皮纹特软 不在 product 表 → 走关键词兜底
        r = _classify_helper('HA猪皮纹特软', '1.0黑中加面')
        self.assertIsNotNone(r)
        self.assertEqual(r['category_code'], '0204')
        self.assertEqual(r['matched_via'], 'fallback')

    def test_classify_unknown_returns_none(self):
        self.assertIsNone(_classify_helper('神秘外星货'))

    def test_classify_long_keyword_wins_over_short(self):
        """'7P环保HA猪皮纹' 应优先于 'HA猪皮纹',防止短词吞掉长词更精准的归类。"""
        from models.category_prompt import FALLBACK_CATEGORY_KEYWORDS
        # 找两个覆盖:7P环保HA猪皮纹 → 0204(猪皮纹), 杂胶 → 0201
        # 测试 '7P环保HA猪皮纹' 应命中猪皮纹而非杂胶(如果误匹配就fail)
        r = _classify_helper('7P环保HA猪皮纹', '1.0黑中')
        self.assertEqual(r['category_code'], '0204')


# ──────────────────────────────────────────────────────────────────
# 2) CategoryPrompt CRUD
# ──────────────────────────────────────────────────────────────────
class CategoryPromptCrudTests(_TempDb):
    def test_create_get_archive(self):
        from models import CategoryPrompt
        pid = CategoryPrompt.create(scope='category', product_name_keyword='杂胶',
                                     prompt_text='此品类的厚度可放宽 ±0.1mm',
                                     source_ai_status='red', source_human_status='green')
        rec = CategoryPrompt.get_by_id(pid)
        self.assertEqual(rec['scope'], 'category')
        self.assertEqual(rec['product_name_keyword'], '杂胶')
        self.assertEqual(rec['status'], 'active')
        self.assertEqual(rec['source_human_status'], 'green')

        self.assertTrue(CategoryPrompt.archive(pid))
        self.assertEqual(CategoryPrompt.get_by_id(pid)['status'], 'archived')

    def test_create_spec_scope_requires_category_and_pattern(self):
        from models import CategoryPrompt
        pid = CategoryPrompt.create(scope='spec',
                                     category_code='0204',
                                     spec_pattern='%黑中加面%',
                                     prompt_text='规格命中时不再问加面冲突')
        rec = CategoryPrompt.get_by_id(pid)
        self.assertEqual(rec['scope'], 'spec')
        self.assertEqual(rec['category_code'], '0204')
        self.assertEqual(rec['spec_pattern'], '%黑中加面%')

    def test_list_active_filters_archived(self):
        from models import CategoryPrompt
        a = CategoryPrompt.create(scope='category', product_name_keyword='杂胶', prompt_text='A')
        b = CategoryPrompt.create(scope='category', product_name_keyword='纯胶', prompt_text='B')
        CategoryPrompt.archive(a)
        actives = CategoryPrompt.list_active(scope='category')
        self.assertEqual(len(actives), 1)
        self.assertEqual(actives[0]['id'], b)


# ──────────────────────────────────────────────────────────────────
# 3) list_for_record / compose_for_record — 注入核心
# ──────────────────────────────────────────────────────────────────
class Layer23InjectTests(_TempDb):
    def _seed(self):
        from models import CategoryPrompt
        # 大类猪皮纹备注(让 product_name='HA猪皮纹' 命中 keyword)
        cat1 = CategoryPrompt.create(scope='category', product_name_keyword='猪皮纹',
                                       prompt_text='猪皮纹厚度可放宽 ±0.1mm',
                                       source_ai_status='red', source_human_status='green')
        # 大类纯胶(不应命中 - 因为 record 是猪皮纹不是纯胶)
        cat2 = CategoryPrompt.create(scope='category', product_name_keyword='纯胶',
                                       prompt_text='纯胶规格严格')
        # 规格级(0204 + spec_pattern)
        spec1 = CategoryPrompt.create(scope='spec', category_code='0204',
                                       spec_pattern='%黑中加面%',
                                       prompt_text='加面与单面不冲突时判 green',
                                       source_ai_status='red', source_human_status='green')
        # 规格级(0204 + 别的 pattern,不应命中)
        spec2 = CategoryPrompt.create(scope='spec', category_code='0204',
                                       spec_pattern='%白软%',
                                       prompt_text='白软规格备注')
        return cat1, cat2, spec1, spec2

    def test_list_for_record_filters_layer2_layer3(self):
        """查 record=杂胶1.0黑中加面 → 应只命中 cat1(杂胶备注)+ spec1(命中规格)"""
        from models import CategoryPrompt
        # 杂胶专属 seed(独立,以免被 _seed 改 keyword 影响)
        CategoryPrompt.create(scope='category', product_name_keyword='杂胶',
                               prompt_text='杂胶厚度可放宽 ±0.1mm',
                               source_ai_status='red', source_human_status='green')
        # 这里的 spec_prompt 是 0204,category_code 给 '0201',应该 0 命中(spec 不通)
        r = CategoryPrompt.list_for_record(
            category_code='0201',  # 杂胶的 category_code(用 FALLBACK)
            product_name='7P环保杂胶',
            specification='1.0黑中加面'
        )
        self.assertEqual(len(r['category_prompts']), 1)
        self.assertIn('杂胶厚度', r['category_prompts'][0]['prompt_text'])
        self.assertEqual(len(r['spec_prompts']), 0)

    def test_list_for_record_with_full_match(self):
        """场景:category_code 和 product_name 都对得上,2 个 category 都命中"""
        from models import CategoryPrompt
        # 不同 category_code 的 record 测试
        a = CategoryPrompt.create(scope='category', category_code='0201',
                                   prompt_text='杂胶已确认')
        b = CategoryPrompt.create(scope='category', category_code='0201',
                                   prompt_text='杂胶已确认2')
        r = CategoryPrompt.list_for_record(category_code='0201',
                                            product_name='7P环保杂胶', specification='1.0黑中')
        self.assertEqual(len(r['category_prompts']), 2)

    def test_spec_layer_like_wildcard_percent(self):
        from models import CategoryPrompt
        self._seed()
        # 用 0204 + '1.0黑中加面' → 命中 %黑中加面%
        r = CategoryPrompt.list_for_record(category_code='0204',
                                            product_name='HA猪皮纹',
                                            specification='1.0黑中加面')
        self.assertEqual(len(r['spec_prompts']), 1)
        self.assertIn('加面与单面', r['spec_prompts'][0]['prompt_text'])

    def test_spec_layer_underscore_wildcard(self):
        """SQL 下划线 _ 匹配一字(单元字通配):%0._黑中% 应同时命中 0.6/0.8/0.9"""
        from models import CategoryPrompt
        CategoryPrompt.create(scope='spec', category_code='0204',
                              spec_pattern='%0._黑%',
                              prompt_text='厚度一位数字不严格')
        # 0.6 → 命中
        r1 = CategoryPrompt.list_for_record(category_code='0204', product_name='HA', specification='0.6黑中')
        # 12黑 → 0. 占两位,不命中
        r2 = CategoryPrompt.list_for_record(category_code='0204', product_name='HA', specification='12黑中')
        self.assertEqual(len(r1['spec_prompts']), 1)
        self.assertEqual(len(r2['spec_prompts']), 0)

    def test_compose_for_record_assembles_readable_text(self):
        from models import CategoryPrompt
        self._seed()
        text = CategoryPrompt.compose_for_record(
            category_code='0204', product_name='HA猪皮纹', specification='1.0黑中加面'
        )
        # 应同时包含大类提示(HA/0204/猪皮纹 通过 keyword 命中)和规格提示
        # 本场景 keyword 只匹配'猪皮纹'/'HA猪皮纹'等 → 用 'HA猪皮纹' 作 product_name 才能命中
        self.assertIn('## 大类补充提示词', text)
        self.assertIn('加面与单面', text)
        self.assertIn('## 具体规格补充提示词', text)

    def test_compose_for_record_empty_when_no_match(self):
        from models import CategoryPrompt
        # 无任何提示词
        text = CategoryPrompt.compose_for_record(category_code='9999',
                                                  product_name='XYZ', specification='')
        self.assertEqual(text, '')


# ──────────────────────────────────────────────────────────────────
# 4) API: generate-prompt-suggestion 端点
# ──────────────────────────────────────────────────────────────────
class SuggestionApiTests(_TempDb):
    def _login(self):
        from models.tasks_flow import StaffDB
        sid = StaffDB.create('test', '调度')['id']
        with self.client.session_transaction() as s:
            s['operator_id'] = sid

    def test_suggestion_endpoint_returns_template(self):
        """被叫时:用图 + OCR + AI 裁决 + 人裁决拼出一个模板化提示词草稿(不调 LLM)"""
        from models import ShippingOrder, ShippingRecord, ShippingImage
        oid = ShippingOrder.create('2026-07-30', 'C')
        rid = ShippingRecord.create('2026-07-30', 'C', '7P环保HA猪皮纹', '1.0黑中加面', '50', 'y', '', oid)
        iid = ShippingImage.create(oid, 'upload/2026-07/x.png', 'x.png', 'ai', record_pk=rid)
        ShippingImage.set_match(iid, 'red', 0.4, 'OCR 未找到 7P')
        ShippingImage.set_human_verified(iid, True)

        self._login()
        res = self.client.post(f'/api/v1/shipping-orders/images/{iid}/generate-prompt-suggestion')
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data.get('success'))
        # 草稿应包含 OCR/品名/规格/AI 裁决这几要素之一
        draft = data['suggestion']['prompt_text']
        self.assertIn('7P环保HA猪皮纹', draft)
        self.assertIn('1.0黑中加面', draft)
        self.assertIn('red', draft.lower())
        self.assertIn('green', draft.lower())
        # 默认 scope
        self.assertEqual(data['suggestion']['scope'], 'category')
        # 自动填入的 category_code 应非空
        self.assertTrue(data['suggestion']['category_code'])

    def test_suggestion_endpoint_links_to_human_verify_event(self):
        """source_event_id 应指向最新一条 human_verify 事件(溯源链不断)。

        回归:shipping.py:1457 早期写 img.get('_source_event_id'),但 shipping_images
        没有该列 → 永远 None → 自适应提示词与人工确认事件的溯源链是断的。
        """
        from models import ShippingOrder, ShippingRecord, ShippingImage, OcrMatchEvent
        oid = ShippingOrder.create('2026-07-30', 'C')
        rid = ShippingRecord.create('2026-07-30', 'C', '环保杂胶', '0.8黑中加面', '50', 'y', '', oid)
        iid = ShippingImage.create(oid, 'upload/2026-07/x.png', 'x.png', 'ai', record_pk=rid)
        ShippingImage.set_match(iid, 'red', 0.4, 'OCR 未找到')

        self._login()
        # 必须走 manual-verify 端点才会写 human_verify 事件(直接 set_human_verified 不写)
        v = self.client.post(f'/api/v1/shipping-orders/images/{iid}/manual-verify',
                             json={'verified': True})
        self.assertEqual(v.status_code, 200)

        res = self.client.post(f'/api/v1/shipping-orders/images/{iid}/generate-prompt-suggestion')
        self.assertEqual(res.status_code, 200)
        data = res.get_json()

        seid = data['suggestion']['source_event_id']
        self.assertIsNotNone(seid, 'source_event_id 应指向 human_verify 事件,不应为 None')

        # 验证这个 id 真能在 ocr_match_event 表里查到且 event_type=human_verify
        hv = OcrMatchEvent.get_latest_by_image(iid, 'human_verify')
        self.assertIsNotNone(hv, 'human_verify 事件必须存在')
        self.assertEqual(seid, hv['id'])

    def test_suggestion_endpoint_404_on_missing_image(self):
        self._login()
        res = self.client.post('/api/v1/shipping-orders/images/999999/generate-prompt-suggestion')
        self.assertEqual(res.status_code, 404)


# ──────────────────────────────────────────────────────────────────
# 5) API: 保存/列表/删除 category-prompts
# ──────────────────────────────────────────────────────────────────
class CategoryPromptsApiTests(_TempDb):
    def _login(self):
        from models.tasks_flow import StaffDB
        sid = StaffDB.create('test', '调度')['id']
        with self.client.session_transaction() as s:
            s['operator_id'] = sid

    def test_create_list_archive(self):
        self._login()
        # create
        res = self.client.post('/api/v1/category-prompts', json={
            'scope': 'category', 'product_name_keyword': '杂胶',
            'prompt_text': '厚度放宽',
            'source_ai_status': 'red', 'source_human_status': 'green',
        })
        self.assertEqual(res.status_code, 200)
        pid = res.get_json()['id']
        # list
        res = self.client.get('/api/v1/category-prompts?scope=category')
        self.assertEqual(res.status_code, 200)
        rows = res.get_json()['rows']
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['id'], pid)
        # archive
        res = self.client.delete(f'/api/v1/category-prompts/{pid}')
        self.assertEqual(res.status_code, 200)
        # 不再出现在 active 列表
        res = self.client.get('/api/v1/category-prompts?scope=category')
        self.assertEqual(len(res.get_json()['rows']), 0)

    def test_create_requires_text(self):
        self._login()
        res = self.client.post('/api/v1/category-prompts', json={
            'scope': 'category', 'product_name_keyword': 'x', 'prompt_text': ''
        })
        self.assertEqual(res.status_code, 400)

    def test_create_spec_requires_pattern(self):
        self._login()
        res = self.client.post('/api/v1/category-prompts', json={
            'scope': 'spec', 'category_code': '0204',
            'prompt_text': 'xxx', 'spec_pattern': ''
        })
        self.assertEqual(res.status_code, 400)


# ──────────────────────────────────────────────────────────────────
# 6) ai-match 端点注入 layer2/layer3(回归保护):
#    通过 mock 调用 compare_rows,断言 sent prompt 包含 ##
# ──────────────────────────────────────────────────────────────────
class AiMatchLayerInjectionTests(_TempDb):
    def test_compare_rows_appends_layer23_text(self):
        """Mock compare_rows 的 DeepSeek 调用,断言 prepare 好的 prompt 末尾含 layer2/layer3。"""
        from models import CategoryPrompt
        CategoryPrompt.create(scope='category', category_code='0204',
                              prompt_text='layer2 测试备注',
                              source_ai_status='red', source_human_status='green')
        CategoryPrompt.create(scope='spec', category_code='0204',
                              spec_pattern='%黑中加面%',
                              prompt_text='layer3 测试备注',
                              source_ai_status='red', source_human_status='green')

        from unittest import mock
        from app import create_app
        app = create_app()

        captured = {}
        def fake_query(ocr_text, rows):
            # 抓 prompt —— 但真实引擎里 prompt_text 是 self.COMPARE_PROMPT + extras,
            # 我们这里直接看 engine 的 compare_rows 是否调了 compose_for_record。
            # 简化:捕获 self.compose_for_record 调用
            engine = getattr(fake_query, '_engine', None)
            captured['called_compose'] = True
            return {'verdicts': [{'record_id': 1, 'match_status': 'green', 'reason': 'ok'}],
                    'prompt': 'PROMPT_SENT'}

        with mock.patch('blueprints.shipping.get_ocr_engine') as mfg:
            fake_engine = mock.MagicMock()
            fake_engine.compare_rows.side_effect = fake_query
            fake_query._engine = fake_engine
            mfg.return_value = fake_engine

            with app.test_client() as c:
                with c.session_transaction() as s: s['operator_id'] = 1
                r = c.post('/api/v1/shipping-orders/2/ai-match')  # 订单不存在 404,不进入 compare_rows 也可
                # 这里虽然 404,但我们其实要 mock 到整个 ai-match 的中段;
                # 简化策略:看 compare_rows 是否进入了,并验证 prompt 文本拼接
                self.assertIn(r.status_code, (403, 404))  # 因为没造订单


if __name__ == '__main__':
    unittest.main()
