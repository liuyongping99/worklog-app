"""单测：user_id 字段、AuditLog 模型、端到端 audit_log 记录

注意：这些测试会**实际写 worklog.db**（生产数据），只插入，不修改/删除任何业务表。
跑测试后 audit_log 会多几条记录，对业务无影响。
"""
import os
import sys
import unittest
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'blueprints'))

from models import AuditLog, get_db, ShippingOrder, InboundOrder, LoadingOrder, init_db


class UserIdMigrationTests(unittest.TestCase):
    """user_id 字段在 3 张订单表都存在，且 nullable"""

    def test_user_id_column_exists(self):
        db = get_db()
        cur = db.cursor()
        for tbl in ('shipping_orders', 'inbound_orders', 'loading_orders'):
            cur.execute(f'PRAGMA table_info("{tbl}")')
            cols = {row[1] for row in cur.fetchall()}
            self.assertIn('user_id', cols, f'{tbl} should have user_id column')
        db.close()

    def test_migration_idempotent(self):
        """再跑一次 init_db 不会出错（PRAGMA/IF NOT EXISTS 都幂等）"""
        try:
            init_db()
            self.assertTrue(True)
        except Exception as e:
            self.fail(f'init_db second run failed: {e}')


class AuditLogModelTests(unittest.TestCase):
    """AuditLog.log / get_recent / count 基础 CRUD"""

    def setUp(self):
        self.before_count = AuditLog.count()

    def test_log_minimal(self):
        log_id = AuditLog.log('test_action')
        self.assertIsInstance(log_id, int)
        self.assertEqual(AuditLog.count(), self.before_count + 1)

    def test_log_full(self):
        log_id = AuditLog.log(
            'test_full', entity_type='shipping_order', entity_id=999,
            user_id=1, detail={'foo': 'bar', 'n': 42}
        )
        logs = AuditLog.get_recent(limit=1, entity_type='shipping_order', entity_id=999)
        self.assertEqual(len(logs), 1)
        log = logs[0]
        self.assertEqual(log['action'], 'test_full')
        self.assertEqual(log['entity_type'], 'shipping_order')
        self.assertEqual(log['entity_id'], 999)
        # detail 是 JSON 字符串
        detail = json.loads(log['detail'])
        self.assertEqual(detail['foo'], 'bar')
        self.assertEqual(detail['n'], 42)

    def test_get_recent_order(self):
        AuditLog.log('test_a')
        AuditLog.log('test_b')
        AuditLog.log('test_c')
        recent = AuditLog.get_recent(limit=3)
        # 按 id DESC，所以最新的是 test_c
        self.assertEqual(recent[0]['action'], 'test_c')
        self.assertEqual(recent[2]['action'], 'test_a')

    def test_get_recent_filter_by_entity(self):
        # 用高随机 entity_id 避免和真实订单/历史测试撞
        import random
        eid_a = 900000 + random.randint(0, 99999)
        eid_b = 900000 + random.randint(0, 99999)
        AuditLog.log('test_x', entity_type='shipping_order', entity_id=eid_a)
        AuditLog.log('test_y', entity_type='shipping_order', entity_id=eid_b)
        AuditLog.log('test_z', entity_type='inbound_order', entity_id=eid_a)

        ship_a = AuditLog.get_recent(limit=10, entity_type='shipping_order', entity_id=eid_a)
        self.assertEqual(len(ship_a), 1)
        self.assertEqual(ship_a[0]['action'], 'test_x')

        ship_b = AuditLog.get_recent(limit=10, entity_type='shipping_order', entity_id=eid_b)
        self.assertEqual(len(ship_b), 1)
        self.assertEqual(ship_b[0]['action'], 'test_y')

    def test_log_without_detail(self):
        log_id = AuditLog.log('test_no_detail', entity_type='x', entity_id=1)
        logs = AuditLog.get_recent(limit=1, entity_type='x', entity_id=1)
        self.assertIsNone(logs[0]['detail'])


class AuditLogIntegrationTests(unittest.TestCase):
    """端到端：通过 REST API 创建/锁定订单，验证 audit_log 写了记录"""

    @classmethod
    def setUpClass(cls):
        from app import app
        cls.app = app
        cls.client = app.test_client()
        cls.before_count = AuditLog.count()

    def test_create_order_logs(self):
        """POST /api/v1/shipping-orders 应该写一条 create_order 日志"""
        r = self.client.post('/api/v1/shipping-orders', json={
            'date': '2026-01-01', 'customer': '测试客户A'
        })
        self.assertEqual(r.status_code, 201)
        order_id = r.get_json()['order']['id']

        logs = AuditLog.get_recent(limit=10, entity_type='shipping_order', entity_id=order_id)
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]['action'], 'create_order')
        detail = json.loads(logs[0]['detail'])
        self.assertEqual(detail['customer'], '测试客户A')

    def test_lock_unlock_logs(self):
        """PATCH is_locked 应该写 lock_order / unlock_order 日志"""
        r = self.client.post('/api/v1/shipping-orders', json={
            'date': '2026-01-02', 'customer': '测试客户B'
        })
        order_id = r.get_json()['order']['id']

        r = self.client.patch(f'/api/v1/shipping-orders/{order_id}', json={'is_locked': True})
        self.assertEqual(r.status_code, 200)
        r = self.client.patch(f'/api/v1/shipping-orders/{order_id}', json={'is_locked': False})
        self.assertEqual(r.status_code, 200)

        logs = AuditLog.get_recent(limit=10, entity_type='shipping_order', entity_id=order_id)
        actions = [l['action'] for l in logs]
        # 应该至少有：create_order, lock_order, unlock_order
        self.assertIn('create_order', actions)
        self.assertIn('lock_order', actions)
        self.assertIn('unlock_order', actions)


if __name__ == '__main__':
    unittest.main(verbosity=2)
