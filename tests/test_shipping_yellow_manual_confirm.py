"""出货页 match_status=yellow 也应渲染「✓ 确认通过」按钮。

历史行为:只有 red (✗) 显示按钮;yellow (⚠) 标题就是"存疑，请人工核对"
却没按钮,与文案矛盾。改完测试覆盖:

- 服务端模板:match_status=='yellow' 时渲染 .manual-confirm-btn
- 服务端模板:match_status=='yellow' 且 human_verified=1 时渲染 .manual-verified-marker
- JS 动态路径(recordImageUploaded → images.forEach) 同样处理 yellow
- 静态防回归:不依赖浏览器即可断言
"""
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models._db as _db

REPO_ROOT = Path(__file__).resolve().parent.parent
SHIPPING_TPL = REPO_ROOT / 'templates' / 'shipping-records.html'
# 被 shipping-records.html include 进来的脚本文件(里面含 JS appendOrderImageArea 等)
SHIPPING_INCLUDES = SHIPPING_TPL.parent / '_record_image_script.html'


class TempDbBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        self._orig = _db.DB_PATH
        _db.DB_PATH = self.tmp.name
        from models import init_db
        init_db()
        from app import create_app
        self.client = create_app().test_client()
        # 绕开登录闸门（同 tests/test_shipping_p1_fixes.py:155）：
        # 创建真实 staff → 写 operator_id session
        from models.tasks_flow import StaffDB
        sid = StaffDB.create('测试员', '调度')['id']
        with self.client.session_transaction() as s:
            s['operator_id'] = sid

    def tearDown(self):
        _db.DB_PATH = self._orig
        try: os.unlink(self.tmp.name)
        except OSError: pass


def _make_yellow_image(suffix='.png'):
    """造一条出货订单 + 一条明细 + 一张 record 级 match_status=yellow 的图。
    必须是 record 级 —— 服务端模板 {% if img.record_pk %} 守卫包住整个 img-meta 块,
    订单级图根本不渲染徽章/按钮/标记,黄牌按钮自然也看不到。
    source 用 'upload'（非 ai）—— 蓝图的 order_non_ai 过滤会剔除 ai 图,不进 img-meta 渲染。
    """
    from models import ShippingOrder, ShippingRecord, ShippingImage
    oid = ShippingOrder.create('2026-07-30', '客户X')
    rid = ShippingRecord.create('2026-07-30', '客户X', '环保纯胶', '1.0黑软', '100', 'y', '',
                                order_pk=oid)
    iid = ShippingImage.create(oid, f'upload/2026-07/yellow-{suffix}', suffix, 'upload',
                               record_pk=rid)
    ShippingImage.set_match(iid, 'yellow', 0.7, "规格中'0.8'与标签'0.6'不一致")
    return oid, iid


# ── 1. 静态防回归（模板） ──────────────────────────────────────────────────
class TemplateGuardTests(unittest.TestCase):
    def setUp(self):
        # 2026-07-31:appendOrderImageArea 等 JS 守卫在 _record_image_script.html
        # (被 shipping-records.html include),合并两文件一起搜
        self.src = SHIPPING_TPL.read_text(encoding='utf-8') + '\n' + SHIPPING_INCLUDES.read_text(encoding='utf-8')

    def test_server_template_renders_button_for_yellow(self):
        """服务端 img-meta 块:yellow 也要渲染 .manual-confirm-btn / .manual-verified-marker。

        用法：找模板里所有 `{% if img.match_status ... %}` 守卫行，
        选「守卫块内含 manual-confirm-btn」的那个（最外层那个）；
        断言守卫条件同时含 red 与 yellow。
        """
        src = self.src  # 合并 shipping-records.html + _record_image_script.html(由 setUp 准备)
        body_start = src.index('</style>')
        body_src = src[body_start:]

        # 收集所有 img.match_status 守卫行（每条单行 {% if %}）
        guards = []
        for m in re.finditer(r"\{%\s*if\s+img\.match_status[^%]*%\}", body_src):
            guards.append((m.start(), m.group(0)))
        self.assertGreater(len(guards), 0, '模板里找不到 img.match_status 守卫行')

        # 找最外层守卫(单行 if-endif,且 if 后面到 manual-confirm-btn 之前没有第二个 {% if)
        guard_block = None
        for pos, line in guards:
            # 检查这条守卫是不是包住 manual-confirm-btn 的最外层
            # 单行 if:从 pos 到下一个 {% endif 之间的内容里必须含 manual-confirm-btn
            # 且中间没有「未配对的 {% if」(即 must close before reaching btn)
            after = body_src[pos:]
            # 找这条守卫最近的 endif
            endif = after.find('{% endif')
            if endif < 0: continue
            block = after[:endif]
            if 'manual-confirm-btn' in block and 'manual-verified-marker' in block:
                guard_block = block
                break
        self.assertIsNotNone(guard_block, '找不到包住 manual-confirm-btn 的守卫块')

        # 守卫条件要同时覆盖 red 与 yellow
        self.assertIn("'yellow'", guard_block,
            '服务端守卫应同时匹配 red 与 yellow（黄牌也要人工确认）')
        self.assertIn("'red'", guard_block, '服务端守卫应仍覆盖 red（防回归）')

    def test_js_dynamic_path_handles_yellow(self):
        """JS 里的 recordImageUploaded/buildRecordImages 块:yellow 也要按钮。"""
        src = SHIPPING_TPL.read_text(encoding='utf-8')
        # 找 (status === 'red' && ...) 守卫 → 改后应当同时含 'yellow'
        m = re.search(
            r"if\s*\(\s*\(?\s*status\s*===\s*'red'\s*\|\|\s*status\s*===\s*'yellow'\s*\)?\s*&&\s*!?(?:img\.)?human_verified\s*\).*?\}\s*else\s*if\s*\(",
            src, re.S)
        # 备选:也可能用 _hv / !_hv(局部变量)
        if not m:
            m = re.search(
                r"if\s*\(\s*\(?\s*status\s*===\s*'red'\s*\|\|\s*status\s*===\s*'yellow'\s*\)?\s*&&\s*!?_hv\s*\).*?\}\s*else\s*if\s*\(",
                src, re.S)
        self.assertIsNotNone(m, 'JS 守卫 if ((status === "red" || status === "yellow") && ...) 应存在')
        chunk = m.group(0)
        self.assertIn("'yellow'", chunk, 'JS 守卫应处理 yellow（黄牌也要人工确认）')
        self.assertIn("'red'", chunk, 'JS 守卫应仍处理 red（防回归）')


# ── 2. 端到端（HTML 渲染） ─────────────────────────────────────────────────
class RenderTests(TempDbBase):
    def _set_match(self, iid, status, reason=''):
        """后续如果需要再补（目前测试只用 set_match 创建时一次性写好）。"""
        pass

    def test_yellow_image_renders_confirm_button(self):
        _, iid = _make_yellow_image()
        res = self.client.get('/shipping-records?start_date=2026-07-30&end_date=2026-07-30')
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        # 找该图所在的 img-meta 块（用 data-image-id 定位）
        m = re.search(
            r'<span class="match-badge[^"]*" data-match-status="yellow".*?'
            r'<button class="manual-confirm-btn[^"]*" data-image-id="' + str(iid) + r'"',
            html, re.S)
        self.assertIsNotNone(m, '黄牌图 img-meta 必须含 .manual-confirm-btn')

    def test_yellow_image_after_confirm_renders_marker(self):
        _, iid = _make_yellow_image()
        from models import ShippingImage
        ShippingImage.set_human_verified(iid, True)
        res = self.client.get('/shipping-records?start_date=2026-07-30&end_date=2026-07-30')
        html = res.get_data(as_text=True)
        m = re.search(
            r'<span class="manual-verified-marker" data-verified-marker="' + str(iid) + r'"',
            html)
        self.assertIsNotNone(m, '黄牌图人工确认后必须渲染 .manual-verified-marker')

    def test_red_image_still_works(self):
        """防回归:red 仍然有按钮(本次不能把红牌按钮弄没)。"""
        from models import ShippingOrder, ShippingRecord, ShippingImage
        oid = ShippingOrder.create('2026-07-30', '客户R')
        rid = ShippingRecord.create('2026-07-30', '客户R', '杂胶', '1.0黑', '50', 'y', '',
                                    order_pk=oid)
        iid = ShippingImage.create(oid, 'upload/2026-07/red.png', 'red.png', 'upload',
                                   record_pk=rid)
        ShippingImage.set_match(iid, 'red', 0.3, '明显不符')
        res = self.client.get('/shipping-records?start_date=2026-07-30&end_date=2026-07-30')
        html = res.get_data(as_text=True)
        m = re.search(
            r'<button class="manual-confirm-btn[^"]*" data-image-id="' + str(iid) + r'"',
            html)
        self.assertIsNotNone(m, '红牌图仍应渲染按钮（防回归）')

    def test_green_image_has_no_button(self):
        from models import ShippingOrder, ShippingRecord, ShippingImage
        oid = ShippingOrder.create('2026-07-30', '客户G')
        rid = ShippingRecord.create('2026-07-30', '客户G', '纯胶', '0.8白', '80', 'y', '',
                                    order_pk=oid)
        iid = ShippingImage.create(oid, 'upload/2026-07/green.png', 'green.png', 'upload',
                                   record_pk=rid)
        ShippingImage.set_match(iid, 'green', 0.95, '一致')
        res = self.client.get('/shipping-records?start_date=2026-07-30&end_date=2026-07-30')
        html = res.get_data(as_text=True)
        m = re.search(
            r'<div class="img-meta">\s*'
            r'<span class="match-badge[^"]*" data-match-status="green".*?'
            r'<button class="manual-confirm-btn[^"]*" data-image-id="' + str(iid) + r'"',
            html, re.S)
        self.assertIsNone(m, '绿牌图不应渲染按钮（未触发任何警告无需确认）')


if __name__ == '__main__':
    unittest.main()
