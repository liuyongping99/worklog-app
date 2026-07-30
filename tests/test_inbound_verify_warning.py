"""入库明细「已核查」功能 —— 回归测试。

背景：`/inbound-records` 页面警告区的「✓ 全部已核查」按钮点击无效，根因两层：
  1. `templates/inbound-records.html` 没定义 `window.markRecordVerified`（共享事件代理静默 no-op）
  2. `inbound_records` 表没有 `verified` / `verified_warnings` 列，`InboundRecord.update`
     白名单也不含 `verified` → 即使补上回调也无法持久化

覆盖：
- 迁移：三张明细表都有 verified + verified_warnings 列
- 模型：InboundRecord/LoadingOrderRecord.update 接受 verified；verified=0 联动清空 verified_warnings
- 端点：inbound / loading 的 POST .../records/<id>/verify-warning 写入&移除 rule，锁定订单 403
- 回归：出货同端点行为不变（模型层抽了共享 helper）
- 静态防回归：共享模板不再硬编码出货 URL、提供共享默认回调；入库/装柜模板渲染 data-verified*

运行：python -m pytest tests/test_inbound_verify_warning.py -v
"""
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models._db as _db

REPO_ROOT = Path(__file__).resolve().parent.parent
SMART_MODAL = REPO_ROOT / 'templates' / '_smart_add_modal.html'
INBOUND_TPL = REPO_ROOT / 'templates' / 'inbound-records.html'
LOADING_TPL = REPO_ROOT / 'templates' / 'loading-orders.html'
SHIPPING_TPL = REPO_ROOT / 'templates' / 'shipping-records.html'


class TempDbBase(unittest.TestCase):
    """每个测试一个独立临时 db（照 tests/test_shipping_p1_fixes.py 的做法）。"""

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
        try:
            os.unlink(self.tmp.name)
        except OSError:
            pass


# ── 1. 迁移 ────────────────────────────────────────────────────────────────
class MigrationTests(TempDbBase):
    def test_verified_columns_exist_on_all_three_tables(self):
        conn = _db.get_db()
        for tbl in ('shipping_records', 'inbound_records', 'loading_order_records'):
            cols = {r[1] for r in conn.execute(f'PRAGMA table_info("{tbl}")')}
            self.assertIn('verified', cols, f'{tbl} 缺 verified 列')
            self.assertIn('verified_warnings', cols, f'{tbl} 缺 verified_warnings 列')
        conn.close()

    def test_existing_rows_default_to_empty_json(self):
        """存量行的 verified_warnings 必须是 '{}' 而不是 NULL（模板 |tojson 会输出 null）。"""
        from models import InboundOrder, InboundRecord
        oid = InboundOrder.create('2026-07-30', '供应商A')
        rid = InboundRecord.create(oid, '环保杂胶', '1.0黑', '100', 'y', '')
        rec = InboundRecord.get_by_id(rid)
        self.assertEqual(rec['verified'], 0)
        self.assertEqual(rec['verified_warnings'], '{}')


# ── 2. 模型层 ──────────────────────────────────────────────────────────────
class InboundModelTests(TempDbBase):
    def _make_record(self):
        from models import InboundOrder, InboundRecord
        oid = InboundOrder.create('2026-07-30', '供应商A')
        rid = InboundRecord.create(oid, '环保杂胶', '1.0黑', '100', 'y', '')
        return oid, rid

    def test_update_persists_verified(self):
        from models import InboundRecord
        _, rid = self._make_record()
        InboundRecord.update(rid, {'verified': 1})
        self.assertEqual(InboundRecord.get_by_id(rid)['verified'], 1)

    def test_unverify_clears_verified_warnings(self):
        from models import InboundRecord
        _, rid = self._make_record()
        InboundRecord.set_verified_warning(rid, 'misc_no_jia_mian', True)
        InboundRecord.update(rid, {'verified': 1})
        InboundRecord.update(rid, {'verified': 0})
        rec = InboundRecord.get_by_id(rid)
        self.assertEqual(rec['verified'], 0)
        self.assertEqual(rec['verified_warnings'], '{}')

    def test_set_and_unset_verified_warning(self):
        from models import InboundRecord
        _, rid = self._make_record()
        cur = InboundRecord.set_verified_warning(rid, 'misc_no_jia_mian', True)
        self.assertEqual(cur, {'misc_no_jia_mian': True})
        self.assertEqual(InboundRecord.get_verified_warnings(rid), {'misc_no_jia_mian': True})
        cur = InboundRecord.set_verified_warning(rid, 'misc_no_jia_mian', False)
        self.assertEqual(cur, {})


class LoadingModelTests(TempDbBase):
    def test_unverify_clears_verified_warnings(self):
        """装柜之前只有 verified 列、缺联动清空 —— 一并补齐。"""
        from models import LoadingOrder, LoadingOrderRecord
        oid = LoadingOrder.create('2026-07-30', '客户B')
        rid = LoadingOrderRecord.create('2026-07-30', '客户B', '杂胶', '0.8白', '50', 'y', '', order_pk=oid)
        LoadingOrderRecord.set_verified_warning(rid, 'misc_no_jia_mian', True)
        LoadingOrderRecord.update(rid, {'verified': 0})
        rec = LoadingOrderRecord.get_by_id(rid)
        self.assertEqual(rec['verified_warnings'], '{}')


# ── 3. 端点 ────────────────────────────────────────────────────────────────
class InboundEndpointTests(TempDbBase):
    def _make_record(self):
        from models import InboundOrder, InboundRecord
        oid = InboundOrder.create('2026-07-30', '供应商A')
        rid = InboundRecord.create(oid, '环保杂胶', '1.0黑', '100', 'y', '')
        return oid, rid

    def test_put_verified_persists(self):
        _, rid = self._make_record()
        res = self.client.put(
            f'/api/v1/inbound-orders/records/{rid}',
            json={'verified': 1},
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.get_json()['success'])
        from models import InboundRecord
        self.assertEqual(InboundRecord.get_by_id(rid)['verified'], 1)

    def test_put_verified_locked_order_403(self):
        oid, rid = self._make_record()
        conn = _db.get_db()
        conn.execute('UPDATE inbound_orders SET is_locked = 1 WHERE id = ?', (oid,))
        conn.commit()
        conn.close()
        res = self.client.put(
            f'/api/v1/inbound-orders/records/{rid}',
            json={'verified': 1},
        )
        self.assertEqual(res.status_code, 403)

    def test_verify_warning_sets_rule(self):
        _, rid = self._make_record()
        res = self.client.post(
            f'/api/v1/inbound-orders/records/{rid}/verify-warning',
            json={'rule_id': 'misc_no_jia_mian', 'verified': True},
        )
        self.assertEqual(res.status_code, 200)
        data = res.get_json()
        self.assertTrue(data['success'])
        self.assertEqual(data['verified_warnings'], {'misc_no_jia_mian': True})

    def test_verify_warning_unsets_rule(self):
        _, rid = self._make_record()
        self.client.post(
            f'/api/v1/inbound-orders/records/{rid}/verify-warning',
            json={'rule_id': 'misc_no_jia_mian', 'verified': True},
        )
        res = self.client.post(
            f'/api/v1/inbound-orders/records/{rid}/verify-warning',
            json={'rule_id': 'misc_no_jia_mian', 'verified': False},
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()['verified_warnings'], {})

    def test_verify_warning_missing_rule_id_400(self):
        _, rid = self._make_record()
        res = self.client.post(
            f'/api/v1/inbound-orders/records/{rid}/verify-warning',
            json={'verified': True},
        )
        self.assertEqual(res.status_code, 400)

    def test_verify_warning_unknown_record_404(self):
        res = self.client.post(
            '/api/v1/inbound-orders/records/999999/verify-warning',
            json={'rule_id': 'x', 'verified': True},
        )
        self.assertEqual(res.status_code, 404)

    def test_verify_warning_writes_audit_log(self):
        from models import AuditLog
        _, rid = self._make_record()
        before = AuditLog.count()
        self.client.post(
            f'/api/v1/inbound-orders/records/{rid}/verify-warning',
            json={'rule_id': 'misc_no_jia_mian', 'verified': True},
        )
        self.assertEqual(AuditLog.count(), before + 1)


class LoadingEndpointTests(TempDbBase):
    def test_verify_warning_writes_own_table_not_shipping(self):
        """装柜的单条核查必须写 loading_order_records，不能脏写 shipping_records。"""
        from models import (LoadingOrder, LoadingOrderRecord,
                            ShippingOrder, ShippingRecord)
        # 造一条 id 相同的出货明细做"污染探针"
        soid = ShippingOrder.create('2026-07-30', '客户X')
        s_rid = ShippingRecord.create('2026-07-30', '客户X', '纯胶', '0.6黑软', '10', 'y', '', order_pk=soid)
        loid = LoadingOrder.create('2026-07-30', '客户B')
        l_rid = LoadingOrderRecord.create('2026-07-30', '客户B', '杂胶', '0.8白', '50', 'y', '', order_pk=loid)

        res = self.client.post(
            f'/api/v1/loading-orders/records/{l_rid}/verify-warning',
            json={'rule_id': 'misc_no_jia_mian', 'verified': True},
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(LoadingOrderRecord.get_verified_warnings(l_rid), {'misc_no_jia_mian': True})
        # 出货那条不受影响
        self.assertEqual(ShippingRecord.get_verified_warnings(s_rid), {})


class ShippingRegressionTests(TempDbBase):
    def test_shipping_verify_warning_still_works(self):
        """模型层抽共享 helper 后，出货端点行为必须不变。"""
        from models import ShippingOrder, ShippingRecord
        oid = ShippingOrder.create('2026-07-30', '客户X')
        rid = ShippingRecord.create('2026-07-30', '客户X', '纯胶', 'B白300', '10', 'y', '', order_pk=oid)
        res = self.client.post(
            f'/api/v1/shipping-orders/records/{rid}/verify-warning',
            json={'rule_id': 'b_white_300g', 'verified': True},
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.get_json()['verified_warnings'], {'b_white_300g': True})
        # 取消整行核查 → per-rule 状态联动清空（既有行为）
        ShippingRecord.update(rid, {'verified': 0})
        self.assertEqual(ShippingRecord.get_verified_warnings(rid), {})


# ── 4. 静态防回归（模板断言，不起服务） ────────────────────────────────────
class TemplateGuardTests(unittest.TestCase):
    def test_smart_modal_has_no_hardcoded_fetch_url(self):
        """共享模板里的 fetch 必须走 _smartAddApi 拼路径，不能硬编码某套订单的绝对 URL。"""
        src = SMART_MODAL.read_text(encoding='utf-8')
        self.assertNotIn(
            "fetch('/api/v1/", src,
            '共享模板不应硬编码订单 URL（装柜/入库会串表脏写），应改用 _smartAddApi'
        )

    def test_smart_modal_defines_shared_callbacks(self):
        src = SMART_MODAL.read_text(encoding='utf-8')
        self.assertIn('window.markRecordVerified', src)
        self.assertIn('window.unmarkRecordVerified', src)

    def test_page_templates_do_not_redefine_callbacks(self):
        """出货/装柜的重复实现已删除，三页统一走共享默认实现。"""
        for tpl in (SHIPPING_TPL, LOADING_TPL):
            src = tpl.read_text(encoding='utf-8')
            self.assertNotIn(
                'window.markRecordVerified =', src,
                f'{tpl.name} 仍在重复定义 markRecordVerified'
            )

    def test_inbound_template_renders_verified_attributes(self):
        src = INBOUND_TPL.read_text(encoding='utf-8')
        self.assertIn('data-verified=', src)
        self.assertIn('data-verified-warnings=', src)

    def test_loading_template_renders_verified_warnings_attribute(self):
        src = LOADING_TPL.read_text(encoding='utf-8')
        self.assertIn('data-verified-warnings=', src)


if __name__ == '__main__':
    unittest.main()
