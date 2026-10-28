"""日终盘点功能测试(2026-10-26 新增)。

覆盖:
1. 模型 DailyOrderCheck 的 upsert / get / get_map / delete(含 UNIQUE 幂等)
2. 状态判定 compute_daily_check_status 的 5 个分支
3. ShippingOrder.daily_stock_stats 与手写 SQL 的零漂移
4. REST 端点 GET / POST / DELETE 的完整链路 + 错误路径
5. 页面渲染:顶部卡片 + 日期级 band 两处都在,且状态一致

背景:纸质出货单一天一张张开,单号 TD-YYYY-MM-DD-NNN 本身不连续
(实测 09-16 最大序号 017 但当天只有 7 单、09-17 的 004 整段没录),
光看单号编号判断不出少录了几单 —— 用户手工填纸面单据总数来对账。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from blueprints._helpers import compute_daily_check_status
from models.daily_check import DailyOrderCheck
from models.orders import ShippingOrder


# ── 1. 模型 ──────────────────────────────────────────────

class TestDailyOrderCheckModel:
    """DailyOrderCheck 的读写语义。"""

    def test_upsert_then_get(self):
        date = '2026-01-02'
        DailyOrderCheck.delete(DailyOrderCheck.SCOPE_SHIPPING, date)
        row_id = DailyOrderCheck.upsert(
            DailyOrderCheck.SCOPE_SHIPPING, date, 7, '纸质 7 张')
        assert row_id is not None
        row = DailyOrderCheck.get(DailyOrderCheck.SCOPE_SHIPPING, date)
        assert row is not None
        assert row['manual_total'] == 7
        assert row['remark'] == '纸质 7 张'
        assert row['scope'] == 'shipping'
        assert row['date'] == date

    def test_upsert_is_idempotent_per_day(self):
        """同一天重复填是覆盖,不是追加(表上有 UNIQUE(scope, date))。"""
        date = '2026-01-03'
        DailyOrderCheck.delete(DailyOrderCheck.SCOPE_SHIPPING, date)
        DailyOrderCheck.upsert(DailyOrderCheck.SCOPE_SHIPPING, date, 3)
        DailyOrderCheck.upsert(DailyOrderCheck.SCOPE_SHIPPING, date, 9)
        row = DailyOrderCheck.get(DailyOrderCheck.SCOPE_SHIPPING, date)
        assert row['manual_total'] == 9
        # 只有一行
        m = DailyOrderCheck.get_map(DailyOrderCheck.SCOPE_SHIPPING, date, date)
        assert list(m.keys()) == [date]

    def test_get_map_covers_range(self):
        DailyOrderCheck.delete(DailyOrderCheck.SCOPE_SHIPPING, '2026-02-01')
        DailyOrderCheck.delete(DailyOrderCheck.SCOPE_SHIPPING, '2026-02-03')
        DailyOrderCheck.upsert(DailyOrderCheck.SCOPE_SHIPPING, '2026-02-01', 1)
        DailyOrderCheck.upsert(DailyOrderCheck.SCOPE_SHIPPING, '2026-02-03', 3)
        m = DailyOrderCheck.get_map(
            DailyOrderCheck.SCOPE_SHIPPING, '2026-02-01', '2026-02-05')
        assert set(m.keys()) == {'2026-02-01', '2026-02-03'}
        assert m['2026-02-03']['manual_total'] == 3

    def test_delete_restores_unfilled(self):
        date = '2026-02-10'
        DailyOrderCheck.upsert(DailyOrderCheck.SCOPE_SHIPPING, date, 5)
        assert DailyOrderCheck.delete(DailyOrderCheck.SCOPE_SHIPPING, date) is True
        assert DailyOrderCheck.get(DailyOrderCheck.SCOPE_SHIPPING, date) is None

    def test_scope_isolation(self):
        """不同 scope 的同一天互不干扰(为后续入库/装柜页预留)。"""
        date = '2026-02-20'
        DailyOrderCheck.delete(DailyOrderCheck.SCOPE_SHIPPING, date)
        DailyOrderCheck.delete(DailyOrderCheck.SCOPE_INBOUND, date)
        DailyOrderCheck.upsert(DailyOrderCheck.SCOPE_SHIPPING, date, 4)
        DailyOrderCheck.upsert(DailyOrderCheck.SCOPE_INBOUND, date, 6)
        assert DailyOrderCheck.get(
            DailyOrderCheck.SCOPE_SHIPPING, date)['manual_total'] == 4
        assert DailyOrderCheck.get(
            DailyOrderCheck.SCOPE_INBOUND, date)['manual_total'] == 6
        DailyOrderCheck.delete(DailyOrderCheck.SCOPE_INBOUND, date)


# ── 2. 状态判定 ──────────────────────────────────────────

class TestComputeDailyCheckStatus:
    """手填单数 vs 系统单数的判定分支。"""

    def test_unfilled(self):
        r = compute_daily_check_status(7, None)
        assert r['status'] == 'unfilled'
        assert r['gap'] == 0
        assert '未盘点' in r['label']

    def test_ok(self):
        r = compute_daily_check_status(8, 8)
        assert r['status'] == 'ok'
        assert r['gap'] == 0

    def test_short(self):
        """系统比手填少 → 还差几单没录。"""
        r = compute_daily_check_status(5, 8)
        assert r['status'] == 'short'
        assert r['gap'] == 3
        assert '3' in r['label']

    def test_extra(self):
        """系统比手填多 → 多录了几单。"""
        r = compute_daily_check_status(10, 8)
        assert r['status'] == 'extra'
        assert r['gap'] == 2

    def test_flags_do_not_change_status(self):
        """无明细 / 未锁定只是副提示,不影响主状态。"""
        r = compute_daily_check_status(8, 8, empty_records=2, unlocked=1)
        assert r['status'] == 'ok'
        assert 'empty_records' in r['flags']
        assert 'unlocked' in r['flags']

    def test_flags_present_on_short(self):
        r = compute_daily_check_status(5, 8, empty_records=1, unlocked=2)
        assert r['status'] == 'short'
        assert set(r['flags']) == {'empty_records', 'unlocked'}

    def test_no_flags_when_clean(self):
        r = compute_daily_check_status(8, 8, empty_records=0, unlocked=0)
        assert r['flags'] == []


# ── 3. 系统侧统计零漂移 ──────────────────────────────────

class TestDailyStockStats:
    """daily_stock_stats 必须和逐单重算完全一致。"""

    def test_matches_manual_recount(self):
        from models._db import get_db
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT MIN(date), MAX(date) FROM shipping_orders')
        row = cursor.fetchone()
        conn.close()
        if not row or not row[0]:
            pytest.skip('库里没有出货单,跳过')

        start, end = row[0], row[1]
        stats = {s['date']: s for s in ShippingOrder.daily_stock_stats(start, end)}

        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT o.id, o.date, o.is_locked,
                   (SELECT COUNT(*) FROM shipping_records x
                    WHERE x.order_pk = o.id) AS n
            FROM shipping_orders o
            WHERE o.date >= ? AND o.date <= ?
        ''', (start, end))
        rows = cursor.fetchall()
        conn.close()

        expected = {}
        for r in rows:
            d = expected.setdefault(
                r['date'], {'total': 0, 'empty_records': 0, 'unlocked': 0})
            d['total'] += 1
            if r['n'] == 0:
                d['empty_records'] += 1
            if r['is_locked'] == 0:
                d['unlocked'] += 1

        assert set(stats.keys()) == set(expected.keys())
        for d, exp in expected.items():
            got = stats[d]
            assert got['total'] == exp['total'], f'{d} 总单数不一致'
            assert got['empty_records'] == exp['empty_records'], f'{d} 空壳单数不一致'
            assert got['unlocked'] == exp['unlocked'], f'{d} 未锁定单数不一致'

    def test_total_matches_count_by_date(self):
        """系统单数口径必须和既有的 count_by_date 一致(页面头部那个数字)。"""
        from models._db import get_db
        conn = get_db()
        cursor = conn.cursor()
        cursor.execute('SELECT DISTINCT date FROM shipping_orders LIMIT 5')
        dates = [r[0] for r in cursor.fetchall()]
        conn.close()
        for d in dates:
            stats = ShippingOrder.daily_stock_stats(d, d)
            assert len(stats) == 1
            assert stats[0]['total'] == ShippingOrder.count_by_date(d)


# ── 4. REST 端点 ─────────────────────────────────────────

class TestDailyCheckApi:
    """三个端点的完整链路 + 错误路径。"""

    def test_get_returns_days(self, client):
        resp = client.get(
            '/api/v1/shipping-orders/daily-check'
            '?start_date=2026-09-16&end_date=2026-09-18')
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['success'] is True
        assert len(body['days']) == 3
        for day in body['days']:
            assert set(('date', 'system_total', 'empty_records', 'unlocked',
                        'manual_total', 'status', 'label', 'gap',
                        'flags', 'flags_text')).issubset(day.keys())

    def test_post_then_get_roundtrip(self, client):
        date = '2026-09-16'
        resp = client.post('/api/v1/shipping-orders/daily-check',
                            json={'date': date, 'manual_total': 99})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['success'] is True
        assert body['day']['manual_total'] == 99
        assert body['day']['status'] == 'short'   # 系统 8 单,手填 99
        assert body['day']['gap'] == 91

        resp = client.get(
            '/api/v1/shipping-orders/daily-check'
            f'?start_date={date}&end_date={date}')
        day = resp.get_json()['days'][0]
        assert day['manual_total'] == 99
        assert day['status'] == 'short'

        resp = client.delete(f'/api/v1/shipping-orders/daily-check/{date}')
        assert resp.status_code == 200
        assert resp.get_json()['day']['status'] == 'unfilled'

    def test_post_chinese_remark(self, client):
        """中文备注要能正常落库(前端 fetch 是真 UTF-8)。"""
        date = '2026-09-17'
        resp = client.post('/api/v1/shipping-orders/daily-check',
                            json={'date': date, 'manual_total': 6,
                                  'remark': '纸质有 6 张'})
        assert resp.status_code == 200
        body = resp.get_json()
        assert body['success'] is True
        assert body['day']['remark'] == '纸质有 6 张'

    def test_post_bad_date_rejected(self, client):
        for bad in ['bad-date', '2026/09/17', '20260917', '', '2026-13-45']:
            resp = client.post('/api/v1/shipping-orders/daily-check',
                                json={'date': bad, 'manual_total': 3})
            assert resp.status_code == 400, f'{bad!r} 应被拒'
            assert resp.get_json()['success'] is False

    def test_post_bad_total_rejected(self, client):
        for bad in ['abc', None, -1]:
            resp = client.post('/api/v1/shipping-orders/daily-check',
                                json={'date': '2026-09-17', 'manual_total': bad})
            assert resp.status_code == 400, f'{bad!r} 应被拒'
            assert resp.get_json()['success'] is False

    def test_post_empty_body_rejected(self, client):
        resp = client.post('/api/v1/shipping-orders/daily-check', json={})
        assert resp.status_code == 400
        assert resp.get_json()['success'] is False

    def test_delete_bad_date_rejected(self, client):
        resp = client.delete('/api/v1/shipping-orders/daily-check/not-a-date')
        assert resp.status_code == 400
        assert resp.get_json()['success'] is False


# ── 5. 页面渲染 ──────────────────────────────────────────

class TestDailyCheckPage:
    """出货页两处入口都要在,且状态一致。"""

    @pytest.fixture
    def logged_client(self):
        """出货页有登录闸门(app.py._require_login),要建个 staff 登录。"""
        from app import create_app
        from models.tasks_flow import StaffDB
        clerk = StaffDB.create(name="日终盘点测试员", role="文员")
        c = create_app().test_client()
        with c.session_transaction() as sess:
            sess["operator_id"] = clerk["id"]
        return c

    def test_page_has_card_and_bands(self, logged_client):
        resp = logged_client.get('/shipping-records?start_date=2026-09-16&end_date=2026-09-18')
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        # 顶部卡片
        assert 'id="eodCard"' in html
        assert '📋 日终盘点' in html
        # 日期级 band
        assert 'day-band' in html
        assert 'data-eod-band="2026-09-16"' in html

    def test_each_date_gets_exactly_one_band(self, logged_client):
        """同一天可能有多张出货单(多个 .date-group),band 只能有一个。"""
        resp = logged_client.get('/shipping-records?start_date=2026-09-16&end_date=2026-09-18')
        html = resp.get_data(as_text=True)
        for d in ('2026-09-16', '2026-09-17', '2026-09-18'):
            assert html.count(f'data-eod-band="{d}"') == 1, f'{d} 的 band 数量不对'

    def test_clear_button_present_in_both_places(self, logged_client):
        """每个日期的顶部表格行和日期级 band 都要有「清除」按钮。"""
        resp = logged_client.get('/shipping-records?start_date=2026-09-16&end_date=2026-09-18')
        html = resp.get_data(as_text=True)
        for d in ('2026-09-16', '2026-09-17', '2026-09-18'):
            # 顶部表格行 1 个 + 日期 band 1 个
            assert html.count(f'data-eod-clear="{d}"') == 2, f'{d} 的清除按钮数量不对'

    def test_manual_value_rendered_in_both_places(self, logged_client):
        date = '2026-09-16'
        DailyOrderCheck.upsert(DailyOrderCheck.SCOPE_SHIPPING, date, 42)
        try:
            resp = logged_client.get(
                '/shipping-records?start_date=2026-09-16&end_date=2026-09-18')
            html = resp.get_data(as_text=True)
            # 顶部表格 + 日期 band 各一个输入框,都带这个值
            assert html.count('data-eod-input="2026-09-16"') == 2
            assert 'value="42"' in html
            # 状态也跟着变
            assert '还差' in html
        finally:
            DailyOrderCheck.delete(DailyOrderCheck.SCOPE_SHIPPING, date)

    def test_page_status_matches_api(self, logged_client):
        """页面渲染的 status 必须和 API 返回的一致(同一套判定,不能分叉)。"""
        date = '2026-09-17'
        system_total = ShippingOrder.count_by_date(date)
        DailyOrderCheck.upsert(
            DailyOrderCheck.SCOPE_SHIPPING, date, system_total + 2)
        try:
            api_day = logged_client.get(
                '/api/v1/shipping-orders/daily-check'
                f'?start_date={date}&end_date={date}').get_json()['days'][0]
            resp = logged_client.get(
                '/shipping-records?start_date=2026-09-17&end_date=2026-09-17')
            html = resp.get_data(as_text=True)
            assert f'eod-{api_day["status"]}' in html
            assert api_day['label'].replace('&', '&amp;') in html
        finally:
            DailyOrderCheck.delete(DailyOrderCheck.SCOPE_SHIPPING, date)
