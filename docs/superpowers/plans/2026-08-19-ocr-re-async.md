# OCR 按钮异步提示 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现 `.re-ocr-btn` 后端路由 + 视觉异步反馈（spinner + "OCR中"文字），让用户点 OCR 按钮后立即看到进度，不会觉得"卡"。

**Tech Stack:** Python 3.12 + Flask + 原生 JS + Tailwind CSS；复用项目已有的 `RecordImageProcessor` (ocr_pipeline.py)、`showBtnLoading` (common.js:117) 工具。

## Global Constraints

- 项目用 CRLF 换行符 + GBK 编码：所有 Python 脚本用 `python -X utf8` 执行
- Flask app factory 入口在 `app.py`；蓝图注册：`from blueprints.shipping import bp as shipping_bp` + `app.register_blueprint(shipping_bp)`
- 测试数据库：conftest.py 已自动复制 `worklog.db` 到 `worklog_test.db`，设 `WORKLOG_DB=worklog_test.db`；本计划新建测试不依赖真实数据
- 所有测试用 unittest 风格 + `from unittest import mock`
- 端点路由必须显式方法: `methods=['POST']` (不要 GET 副作用)
- 不要修改非本任务相关代码 (YAGNI)

---

## File Structure

| 文件 | 责任 |
|------|------|
| `blueprints/shipping.py` | 新增 `api_v1_shipping_orders_re_ocr_image` 路由 (~45 行)，复用 `shipping_processor.extract_ocr` |
| `templates/_record_image_script.html` | `bindReOcrButtons` 改用 `showBtnLoading`/`hideBtnLoading` 工具 (~10 行 diff) |
| `static/css/app.css` | 新增 `.re-ocr-btn.loading` + `.re-ocr-btn.loading::before` spinner 规则 (~13 行) |
| `tests/test_shipping_re_ocr.py` | 6 个后端单元测试 (~150 行)，覆盖 200/400/404 + 写事件 + 复用 processor |

---

## Task 1: 后端实现 `/re-ocr` 路由 (TDD)

**Files:**
- Modify: `blueprints/shipping.py:774` 附近（紧接 `api_v1_shipping_orders_fuzzy_match_image`）
- Create: `tests/test_shipping_re_ocr.py`

**Interfaces:**
- 消费: `shipping_processor` (line 65 顶层单例), `ShippingImage.get_by_id`, `ShippingRecord.get_by_id`, `OcrMatchEvent.create`, `match_label_to_row` (from `blueprints._helpers`)
- 产生: `POST /api/v1/shipping-orders/images/<int:image_id>/re-ocr` → 200 + `{success, image_id, match_status, match_score, reason, match_source, bg_color}` / 400 / 404

---

- [ ] **Step 1.1: 写第一个失败测试 (existing image 200)**

`tests/test_shipping_re_ocr.py`:

```python
# -*- coding: utf-8 -*-
"""出货页 re-ocr 端点测试 (2026-08-19)。

覆盖 POST /api/v1/shipping-orders/images/<id>/re-ocr:
  - 200 + 完整 match_status 路径
  - 404 / 400 各 error 分支
  - OcrMatchEvent 'record_ocr' 写库
  - 复用 RecordImageProcessor.extract_ocr (享受缓存 + 4 角背景色)
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests import fake_png_bytes as _PNG


def _stub_paddle(text='OCR_TEXT', conf=0.95):
    paddle = mock.MagicMock()
    paddle.extract_text_with_conf.return_value = (text, conf)
    return paddle


class ReOcrEndpointTests(unittest.TestCase):
    def setUp(self):
        import models._db as _db
        import tempfile
        self.tmp = tempfile.NamedTemporaryFile(suffix='.db', delete=False)
        self.tmp.close()
        self._orig_db = _db.DB_PATH
        _db.DB_PATH = self.tmp.name
        from models import init_db, ShippingOrder, ShippingRecord, ShippingImage
        init_db()
        self.oid = ShippingOrder.create('2026-08-19', 'T')
        self.rid = ShippingRecord.create(
            '2026-08-19', 'T', '黑磅布三文治', '1.2硬性', '182.5', 'y', '', self.oid,
        )
        # 上传一张图 (Pillow 能解码)
        from PIL import Image
        img = Image.new('RGB', (200, 200), 'white')
        import io
        buf = io.BytesIO()
        img.save(buf, 'PNG')
        data = buf.getvalue()
        img_record = ShippingImage.create_upload(
            self.oid, data, 'label.png', record_pk=self.rid, source='upload')
        self.iid = img_record['id']

        from app import create_app
        self.app = create_app()
        self.client = self.app.test_client()

    def tearDown(self):
        import models._db as _db
        _db.DB_PATH = self._orig_db
        os.unlink(self.tmp.name)

    @mock.patch('blueprints.shipping.get_ocr_engine')
    def test_existing_image_returns_200(self, mock_factory):
        """有图 + record + PaddleOCR 返回文字 -> 200 + match_status/y."""
        mock_factory.return_value = _stub_paddle('品名:磅布三文治\n厚度:1.2mm', 0.9)
        resp = self.client.post(f'/api/v1/shipping-orders/images/{self.iid}/re-ocr')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertTrue(data['success'])
        self.assertEqual(data['image_id'], self.iid)
        self.assertIn(data['match_status'], ('green', 'yellow', 'red'))
        self.assertEqual(data['match_source'], 'local_fuzzy')
        self.assertIn('bg_color', data)
```

- [ ] **Step 1.2: 跑测试确认失败 (路由不存在 → 404)**

Run: `cd C:\Users\Administrator\worklog-app && python -X utf8 -m pytest tests/test_shipping_re_ocr.py::ReOcrEndpointTests::test_existing_image_returns_200 -v`

Expected: FAIL with `404 NOT FOUND` (路由未注册)

- [ ] **Step 1.3: 实现路由**

`blueprints/shipping.py`,紧接 `api_v1_shipping_orders_fuzzy_match_image` 之后添加:

```python
@bp.route('/api/v1/shipping-orders/images/<int:image_id>/re-ocr', methods=['POST'])
def api_v1_shipping_orders_re_ocr_image(image_id):
    """对一张已上传图片重做 PaddleOCR 识别,自动重跑本地模糊匹配。

    行为:
      - 复用 RecordImageProcessor.extract_ocr (享受缓存/4角背景色/[标签背景:]后缀)
      - 自动跑 match_label_to_row (本地 RapidFuzz, <100ms)
      - 写 append-only record_ocr 事件 (审计可看 OCR 重做演变)
      - 更新 shipping_images.match_*
    """
    img = ShippingImage.get_by_id(image_id)
    if not img:
        return jsonify({'success': False, 'error': '图片不存在'}), 404
    if not img.get('record_pk'):
        return jsonify({'success': False, 'error': '该图片未关联商品行,无法重做 OCR'}), 400
    record = ShippingRecord.get_by_id(img['record_pk'])
    if not record:
        return jsonify({'success': False, 'error': '关联商品行不存在'}), 404

    # 1) 复用 RecordImageProcessor (与上传流程一致)
    extracted = shipping_processor.extract_ocr(
        img['file_path'], record, image_id=image_id, use_cached=False)

    if not extracted['ocr_text'].strip():
        return jsonify({'success': False, 'error': 'OCR 无文字,无法识别'}), 400

    # 2) 自动重跑本地模糊匹配
    status, score, reason = match_label_to_row(
        extracted['ocr_text'],
        record.get('product_name', ''),
        record.get('specification', ''))
    source_label = 'local_fuzzy'
    if status:
        ShippingImage.set_match(image_id, status, score or 0, reason, source=source_label)

    # 3) 写 append-only record_ocr 事件 (审计)
    try:
        OcrMatchEvent.create(
            'record_ocr', record_id=img['record_pk'], order_id=img['order_pk'],
            image_id=image_id, ocr_text=extracted['ocr_text'],
            ocr_engine='paddleocr',
            ai_engine=source_label if status else None,
            ai_match_status=status or None,
            ai_match_score=score, ai_match_reason=reason or None,
            product_name=record.get('product_name', ''),
            specification=record.get('specification', ''))
    except Exception:
        current_app.logger.exception('record_ocr(re-ocr) 事件写库失败(不阻断)')

    return jsonify({
        'success': True,
        'image_id': image_id,
        'match_status': status, 'match_score': score, 'reason': reason,
        'match_source': source_label, 'bg_color': extracted['bg_color'],
    })
```

- [ ] **Step 1.4: 跑测试确认通过**

Run: `cd C:\Users\Administrator\worklog-app && python -X utf8 -m pytest tests/test_shipping_re_ocr.py::ReOcrEndpointTests::test_existing_image_returns_200 -v`

Expected: PASS

- [ ] **Step 1.5: 写其余 5 个测试**

追加到 `tests/test_shipping_re_ocr.py`:

```python
    def test_nonexistent_image_returns_404(self):
        resp = self.client.post('/api/v1/shipping-orders/images/999999/re-ocr')
        self.assertEqual(resp.status_code, 404)
        data = resp.get_json()
        self.assertFalse(data['success'])
        self.assertIn('图片不存在', data['error'])

    @mock.patch('blueprints.shipping.get_ocr_engine')
    def test_image_without_record_pk_returns_400(self, mock_factory):
        """record_pk 为 NULL 的图(订单级图) -> 400."""
        from models import ShippingImage
        # 新建一张订单级图(record_pk 为 None)
        from PIL import Image
        import io
        img = Image.new('RGB', (100, 100), 'white')
        buf = io.BytesIO()
        img.save(buf, 'PNG')
        order_img = ShippingImage.create_upload(
            self.oid, buf.getvalue(), 'order.png', record_pk=None, source='upload')
        resp = self.client.post(f'/api/v1/shipping-orders/images/{order_img["id"]}/re-ocr')
        self.assertEqual(resp.status_code, 400)
        data = resp.get_json()
        self.assertFalse(data['success'])
        self.assertIn('未关联商品行', data['error'])

    @mock.patch('blueprints.shipping.get_ocr_engine')
    def test_empty_ocr_text_returns_400(self, mock_factory):
        """PaddleOCR 返回 '' -> 400 OCR 无文字."""
        mock_factory.return_value = _stub_paddle('', 1.0)
        resp = self.client.post(f'/api/v1/shipping-orders/images/{self.iid}/re-ocr')
        self.assertEqual(resp.status_code, 400)
        data = resp.get_json()
        self.assertFalse(data['success'])
        self.assertIn('OCR 无文字', data['error'])

    @mock.patch('blueprints.shipping.get_ocr_engine')
    def test_writes_record_ocr_event(self, mock_factory):
        """成功后写 1 条 record_ocr 事件."""
        from models import OcrMatchEvent
        mock_factory.return_value = _stub_paddle('品名:磅布三文治', 0.9)
        resp = self.client.post(f'/api/v1/shipping-orders/images/{self.iid}/re-ocr')
        self.assertEqual(resp.status_code, 200)
        events = OcrMatchEvent.get_by_image(self.iid)
        record_ocr = [e for e in events if e['event_type'] == 'record_ocr']
        self.assertEqual(len(record_ocr), 1, '应该写一条 record_ocr 事件')
        self.assertIn('磅布三文治', record_ocr[0]['ocr_text'])
        self.assertEqual(record_ocr[0]['ai_engine'], 'local_fuzzy')

    @mock.patch('blueprints.shipping.shipping_processor')
    def test_uses_record_image_processor(self, mock_processor):
        """验证调用 shipping_processor.extract_ocr (而非直接 get_ocr_engine)."""
        mock_processor.extract_ocr.return_value = {
            'ocr_text': '品名:磅布三文治', 'bg_color': None,
            'avg_conf': 0.9, 'from_cache': False,
        }
        resp = self.client.post(f'/api/v1/shipping-orders/images/{self.iid}/re-ocr')
        self.assertEqual(resp.status_code, 200)
        mock_processor.extract_ocr.assert_called_once()
        kwargs = mock_processor.extract_ocr.call_args.kwargs
        self.assertEqual(kwargs['image_id'], self.iid)
        self.assertFalse(kwargs['use_cached'])
```

- [ ] **Step 1.6: 跑全部测试**

Run: `cd C:\Users\Administrator\worklog-app && python -X utf8 -m pytest tests/test_shipping_re_ocr.py -v`

Expected: 6 PASS

- [ ] **Step 1.7: 跑回归**

Run: `cd C:\Users\Administrator\worklog-app && python -X utf8 -m pytest tests/test_ocr_pipeline.py tests/test_shipping_p1_fixes.py tests/test_ocr_match_event_triggers.py -v`

Expected: 全部 PASS (无回归)

- [ ] **Step 1.8: Commit**

```bash
cd C:\Users\Administrator\worklog-app
git add blueprints/shipping.py tests/test_shipping_re_ocr.py
git commit -m "feat(shipping): /re-ocr 端点 + 复用 RecordImageProcessor"
```

---

## Task 2: 前端 JS 改用 showBtnLoading

**Files:**
- Modify: `templates/_record_image_script.html:1069-1089` (`bindReOcrButtons` 函数体)

**Interfaces:**
- 消费: `showBtnLoading(btn, label)` / `hideBtnLoading(btn)` 来自 `static/js/common.js:117` (已加载全局)
- 产生: `.re-ocr-btn` 点击行为 — 按钮变 `.loading` + spinner + 文字"OCR中"，完成后恢复

---

- [ ] **Step 2.1: 替换 `bindReOcrButtons` 函数体**

`templates/_record_image_script.html:1069-1089`:

```js
// 22.5 bindReOcrButtons — 重做 OCR 按钮
// 重新跑 PaddleOCR 识别该图,重写 OCR 记录并自动重跑本地模糊匹配,刷新判别图标
// 2026-08-19 异步化: 用 showBtnLoading 工具 (common.js:117) 给按钮加 spinner + 文字变化
function bindReOcrButtons() {
    document.querySelectorAll('.re-ocr-btn:not([data-bound])').forEach(function(btn){
        btn.setAttribute('data-bound','1');
        btn.addEventListener('click', function(){
            var imgId = btn.getAttribute('data-re-ocr');
            // 全局工具: 自动保存原文本 + 加 .loading class + disabled
            showBtnLoading(btn, 'OCR中');
            fetch('{{ api_prefix }}/images/' + imgId + '/re-ocr', { method: 'POST' })
                .then(function(r){ return r.json(); })
                .then(function(data){
                    hideBtnLoading(btn);
                    if (!data.success) { alert('OCR 重做失败: ' + (data.error || '')); return; }
                    if (!refreshImageMatchUI(imgId, data.match_status, data.match_score, data.reason, data.match_source, { bgColor: data.bg_color })) {
                        alert('OCR 重做完成, 结果: ' + (data.match_status || '无') + ' (界面未找到元素,请刷新)');
                    }
                })
                .catch(function(){
                    hideBtnLoading(btn);
                    alert('请求失败,请检查网络');
                });
        });
    });
}
```

**改动点**:
- `var self = this;` 删除 — 用闭包直接用 `btn`
- `self.disabled = true; self.textContent = '识别中...';` → `showBtnLoading(btn, 'OCR中');`
- `self.disabled = false; self.textContent = 'OCR';` → `hideBtnLoading(btn);` (出现 3 处: success / error / catch)

- [ ] **Step 2.2: 验证后端在跑**

Run: 浏览器打开 `http://localhost:5050/shipping-records`

Expected: 页面正常加载(没有 JS 报错)。如果服务器没跑,先 `python app.py` 启动。

- [ ] **Step 2.3: 实地浏览器验证 (手动)**

1. 打开 `/shipping-records`
2. 找一个 `.re-ocr-btn` (行级图右下角绿底"OCR")
3. 点击
4. 验证:
   - 按钮立即变 spinner + 文字"OCR中"(虽然此时 CSS 还没加 spinner 规则,但 showBtnLoading 会加 `.loading` class)
   - 按钮 disabled
   - 2-4 秒后恢复(返回结果)
   - 徽章颜色根据 OCR 匹配结果变 green/yellow/red
   - 刷新页面后状态仍保留(已写 DB)

- [ ] **Step 2.4: Commit**

```bash
cd C:\Users\Administrator\worklog-app
git add templates/_record_image_script.html
git commit -m "feat(shipping-records): .re-ocr-btn 改用 showBtnLoading 异步提示"
```

---

## Task 3: CSS spinner 规则

**Files:**
- Modify: `static/css/app.css` (在 `.btn.loading` 规则附近,大约 line 742)

**Interfaces:**
- 消费: `@keyframes spin` (app.css:718 已存在)
- 产生: `.re-ocr-btn.loading` + `.re-ocr-btn.loading::before` spinner 规则

---

- [ ] **Step 3.1: 在 `.btn.loading` 块后追加规则**

`static/css/app.css`,紧接现有 `.btn.loading.btn-sm::before` (line 742) 之后:

```css

/* 行级图 OCR 按钮 spinner (没 .btn class, 单独定义) */
.re-ocr-btn.loading {
    position: relative; pointer-events: none; opacity: 0.85;
    display: inline-flex; align-items: center; gap: 0.3rem;
}
.re-ocr-btn.loading::before {
    content: ''; width: 10px; height: 10px;
    border: 1.5px solid rgba(255,255,255,0.4);
    border-top-color: #fff;
    border-radius: 50%;
    animation: spin 0.6s linear infinite;
    flex-shrink: 0;
}
```

- [ ] **Step 3.2: 验证 CSS 优先级**

`.btn.loading` (line 723) 和 `.re-ocr-btn.loading` 都有 `::before` spinner。`.re-ocr-btn` 没有 `.btn` class,所以 `.btn.loading::before` 不匹配 → 只有 `.re-ocr-btn.loading::before` 应用。验证:

打开浏览器 DevTools → 找到 `.re-ocr-btn` 元素 → 点击 → 在 Elements 面板看 spinner `::before` 是否出现。

- [ ] **Step 3.3: 实地验证 spinner 出现**

1. 打开 `/shipping-records`(Ctrl+Shift+R 强制刷新缓存)
2. 点 `.re-ocr-btn`
3. 验证 spinner 旋转 + 文字"OCR中"
4. 完成后恢复

- [ ] **Step 3.4: Commit**

```bash
cd C:\Users\Administrator\worklog-app
git add static/css/app.css
git commit -m "feat(css): .re-ocr-btn.loading spinner 规则"
```

---

## Task 4: 端到端 Playwright 验证

**Files:** 无 (手动验证)

---

- [ ] **Step 4.1: 启动服务器(若未运行)**

Run: `cd C:\Users\Administrator\worklog-app && python app.py`

Expected: 服务器在 127.0.0.1:5050 启动,debug 模式开启

- [ ] **Step 4.2: Playwright 实地测试**

通过浏览器控制台(或 MCP playwright) 执行:

```js
// 1. 找 .re-ocr-btn → 点击 → 验证中间状态 → 验证完成
const btn = document.querySelector('.re-ocr-btn');
const imgId = btn.getAttribute('data-re-ocr');

// 2. 模拟点击
btn.click();

// 3. 立即检查 (按钮 spinner + 文字)
const hasLoadingClass = btn.classList.contains('loading');
const isDisabled = btn.disabled;
const text = btn.textContent.trim();

// 4. 等 4 秒后检查
await new Promise(r => setTimeout(r, 4000));
const finalLoading = btn.classList.contains('loading');
const finalDisabled = btn.disabled;
const finalText = btn.textContent.trim();

// 返回 {hasLoadingClass, isDisabled, text, finalLoading, finalDisabled, finalText}
```

Expected:
- `hasLoadingClass` = true (有 .loading class)
- `isDisabled` = true (按钮 disabled)
- `text` = `"OCR中"` (文字变化)
- `finalLoading` = false (4 秒后 spinner 消失)
- `finalDisabled` = false (按钮恢复)
- `finalText` = `"OCR"` (文字恢复)

- [ ] **Step 4.3: 验证 DB 写入了 record_ocr 事件**

通过 SQLite 查询:

```python
import sqlite3
conn = sqlite3.connect('worklog.db')
recent = conn.execute(
    "SELECT id, image_id, ocr_text, ai_engine FROM ocr_match_event "
    "WHERE event_type='record_ocr' ORDER BY id DESC LIMIT 3"
).fetchall()
# 应有最近 1 条来自我们刚才点的 image
```

Expected: 至少 1 条新的 record_ocr 事件(image_id = 你刚才点的那张图)

- [ ] **Step 4.4: 验证徽章刷新**

1. 浏览器看到行级图徽章(✓/⚠/✗)
2. 点击 OCR 按钮
3. 完成后徽章颜色变化(根据 OCR 结果)
5. 刷新页面 → 徽章仍保留(已持久化到 shipping_images.match_status)

- [ ] **Step 4.5: 完整功能验证清单**

| 检查项 | 期望 |
|--------|------|
| 点 `.re-ocr-btn` 后立即有 spinner | ✓ |
| 文字变成"OCR中" | ✓ |
| 按钮 disabled | ✓ |
| 同一订单其他 OCR 按钮仍可点 | ✓ |
| 2-4 秒后恢复"OCR" | ✓ |
| 徽章按 OCR 结果更新 | ✓ |
| 数据库写 record_ocr 事件 | ✓ |
| 数据库写 shipping_images.match_* | ✓ |
| 网络失败 → 弹 alert + 按钮恢复 | ✓ |
| OCR 无文字(空文本)→ 弹 alert + 按钮恢复 | ✓ |
| 不存在的 image_id → 弹 alert + 按钮恢复(实际是 404) | ✓ |
| 关闭页面再打开 → 状态保留 | ✓ |

---

## Self-Review Checklist

- [x] **Spec 覆盖**: 4 个文件改动都在 tasks 里 (后端/前端/CSS/测试)
- [x] **占位符扫描**: 无 TBD/TODO/"similar to"; 所有代码块完整
- [x] **类型一致**: `extract_ocr` 返回 dict `{ocr_text, bg_color, avg_conf, from_cache}` 在 Task 1 和 spec 一致
- [x] **接口签名一致**: `api_v1_shipping_orders_re_ocr_image(image_id)` 在 Task 1 定义,后续测试/JS 引用一致
- [x] **测试驱动**: Task 1 严格 TDD (test → fail → impl → pass),Task 2-3 因前端无单测机制用手动验证,Task 4 用 Playwright E2E
- [x] **每步可独立验证**: 每步都有明确的 Run 命令 + Expected
- [x] **commit 频率**: Task 1/2/3 各一次 commit,Task 4 不需 commit (手动验证)