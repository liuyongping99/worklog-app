# OCR 按钮异步提示 设计

**日期**：2026-08-19
**作者**：Claude
**状态**：待用户审批

## 1. 背景

`templates/_record_image_script.html:1076` 调 `POST /api/v1/shipping-orders/images/<id>/re-ocr`，但 `blueprints/shipping.py` **没有注册此路由** — 用户点"OCR"按钮 → 404。

即使补上路由，PaddleOCR 2-4 秒/图同步执行,期间:
- 按钮只改文字"识别中..."(无 spinner 动画)
- 同一订单其他 OCR 按钮可点,但用户看不到哪个在跑
- 失败/无文字场景靠 alert(无视觉进度)

**目标**:实现 `/re-ocr` 路由 + 视觉异步反馈。

## 2. 范围

只改 **单图 OCR 按钮**(`.re-ocr-btn`),**不动**:
- `.fuzzy-match-btn` (本地 RapidFuzz < 100ms,不耗时)
- `.ai-judge-btn` (云端 DeepSeek,现有 JS 已 disable + 文字"识别中...")
- 整单 "🤖 AI 匹配" 按钮 (不耗时在用户感知外)
- 行级图上传后自动 OCR (走 `_ASYNC_JOBS` 后台线程,与此独立)

## 4. 设计

### 4.1 后端:实现 `/re-ocr` 路由

文件:`blueprints/shipping.py`(放在 `api_v1_shipping_orders_fuzzy_match_image` 附近)

```python
@bp.route('/api/v1/shipping-orders/images/<int:image_id>/re-ocr', methods=['POST'])
def api_v1_shipping_orders_re_ocr_image(image_id):
    """对一张已上传图片重做 PaddleOCR 识别,自动重跑本地模糊匹配。

    行为:
      - 复用 RecordImageProcessor.extract_ocr (享受缓存/4角背景色/[标签背景:]后缀)
      - 自动跑 match_label_to_row (本地 RapidFuzz, <100ms)
      - 写 append-only record_ocr 事件 (审计可看 OCR 重做演变)
      - 更新 shipping_images.match_*
      - 200 + {success, match_status, match_score, reason, match_source, bg_color}
      - 404 / 400 / 403 各有具体 error
    """
    img = ShippingImage.get_by_id(image_id)
    if not img:
        return jsonify({'success': False, 'error': '图片不存在'}), 404
    if not img.get('record_pk'):
        return jsonify({'success': False, 'error': '该图片未关联商品行,无法重做 OCR'}), 400
    record = ShippingRecord.get_by_id(img['record_pk'])
    if not record:
        return jsonify({'success': False, 'error': '关联商品行不存在'}), 404

    # 1) 复用 RecordImageProcessor (与上传流程一致,享受缓存+4角背景色+[标签背景:]后缀)
    extracted = shipping_processor.extract_ocr(
        img['file_path'], record, image_id=image_id, use_cached=False)

    if not extracted['ocr_text'].strip():
        return jsonify({'success': False, 'error': 'OCR 无文字,无法识别'}), 400

    # 2) 自动重跑本地模糊匹配 (OCR 文本变了,旧匹配已过期)
    status, score, reason = match_label_to_row(
        extracted['ocr_text'],
        record.get('product_name', ''),
        record.get('specification', ''))
    source_label = 'local_fuzzy'
    if status:
        ShippingImage.set_match(image_id, status, score or 0, reason, source=source_label)

    # 3) 写 append-only record_ocr 事件 (审计:OCR 重做演变)
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

**复用 vs 重新实现**:
- ✅ 用 `RecordImageProcessor.extract_ocr` (ocr_pipeline.py:150) 而非直接调 `ocr_engine`
  - 自动 `set_bg_color` 写库
  - 自动 append `[标签背景: 黑色|白色]` 后缀
  - 自动应用 4 角背景色检测 (2026-08-19 修复)
  - 与行级图上传 (`/api/v1/shipping-orders/records/<id>/images`) 行为一致
- ❌ 不复制 `_safe-snapshot/shipping_with_all_changes.py:991` 的旧逻辑 — 旧逻辑不走 RecordImageProcessor,会与新背景色算法不一致

### 4.2 前端:`bindReOcrButtons` 用 `showBtnLoading`

文件:`templates/_record_image_script.html:1069-1089`

```js
function bindReOcrButtons() {
    document.querySelectorAll('.re-ocr-btn:not([data-bound])').forEach(function(btn){
        btn.setAttribute('data-bound','1');
        btn.addEventListener('click', function(){
            var imgId = btn.getAttribute('data-re-ocr');
            // 用全局 showBtnLoading 工具 (common.js:117)
            // 自动: 保存原文本 + 加 .loading class + disabled
            showBtnLoading(btn, 'OCR中');
            fetch('{{ api_prefix }}/images/' + imgId + '/re-ocr', { method: 'POST' })
                .then(function(r){ return r.json(); })
                .then(function(data){
                    hideBtnLoading(btn);
                    if (!data.success) {
                        alert('OCR 重做失败: ' + (data.error || ''));
                        return;
                    }
                    if (!refreshImageMatchUI(imgId, data.match_status, data.match_score, data.reason, data.match_source, { bgColor: data.bg_color })) {
                        alert('OCR 重做完成, 结果: ' + (data.match_status || '无') + ' (界面未找到元素,请刷新)');
                    }
                })
                .catch(function(err){
                    hideBtnLoading(btn);
                    alert('请求失败,请检查网络');
                });
        });
    });
}
```

**改动 diff**:仅替换 `self.disabled = true; self.textContent = '识别中...'` → `showBtnLoading(btn, 'OCR中')` 和恢复时 `self.disabled = false; self.textContent = 'OCR'` → `hideBtnLoading(btn)`,并把 `var self = this` 简化为直接用闭包 `btn`。

### 4.3 CSS: `.re-ocr-btn.loading` spinner

文件:`static/css/app.css`(在 `.btn.loading` 规则附近)

```css
/* OCR 按钮 spinner (行级图按钮没 .btn class, 单独定义) */
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

**为什么不用 `.btn` class**:`.re-ocr-btn` 模板用的是 inline `style` + 自己的 class,加 `.btn` 会与现有规则冲突(`.btn` 默认 padding 0.7rem 1.5rem),单独定义 spinner 规则更稳。

## 5. 数据流

```
用户点 .re-ocr-btn
    │
    ├─ showBtnLoading(btn, 'OCR中')  ← 加 .loading class + spinner + disabled
    │
    └─ fetch POST /api/v1/shipping-orders/images/<id>/re-ocr
         │
         ├─ ShippingImage.get_by_id(image_id) → 404 if not found
         ├─ ShippingRecord.get_by_id(record_pk) → 404 if not found
         ├─ record_pk is None → 400
         │
         ├─ shipping_processor.extract_ocr(filepath, record, image_id, use_cached=False)
         │    │
         │    ├─ 缓存命中 (use_cached=True) → 跳过 PaddleOCR
         │    └─ 缓存未命中 → PaddleOCR (numpy RGB + form_nolines 预) + 4 角背景色
         │        + [标签背景: 黑色|白色] 后缀 (ocr_pipeline.py:196)
         │
         ├─ match_label_to_row(ocr_text, product_name, specification) → (status, score, reason)
         ├─ ShippingImage.set_match(image_id, status, score, reason, source='local_fuzzy')
         │
         ├─ OcrMatchEvent.create('record_ocr', ...) (append-only, 写失败不阻断)
         │
         └─ 返回 {success, image_id, match_status, match_score, reason, match_source, bg_color}
    │
    ├─ hideBtnLoading(btn)  ← 移除 .loading class + 恢复 disabled/text
    │
    ├─ refreshImageMatchUI(imgId, ...) → 局部刷新徽章 + bg_color 标签
    │
    └─ 失败 → alert 显示具体 error
```

## 6. 错误处理

| 场景 | HTTP | error 字段 | 前端行为 |
|------|------|-----------|---------|
| image_id 不存在 | 404 | "图片不存在" | alert + 恢复 |
| 没关联 record_pk | 400 | "该图片未关联商品行,无法重做 OCR" | alert + 恢复 |
| record 被删 | 404 | "关联商品行不存在" | alert + 恢复 |
| OCR 无文字 | 400 | "OCR 无文字,无法识别" | alert + 恢复 |
| PaddleOCR 抛异常 | 200 + success: false | 由 `extract_ocr` 内部吞掉,返回空 ocr_text → 走上一行分支 | alert + 恢复 |
| 网络失败 (catch) | — | "请求失败,请检查网络" | alert + 恢复 |

## 7. 测试

### 7.1 后端单元测试 (新建 `tests/test_shipping_re_ocr.py`)

```python
class ReOcrEndpointTests(unittest.TestCase):
    def setUp(self): ...  # 独立 tmp db
    
    @mock.patch('blueprints.shipping.get_ocr_engine')
    def test_existing_image_returns_200(self, mock_factory):
        """存在图 + record + PaddleOCR 返回文字 → 200 + match_status"""
    
    @mock.patch('blueprints.shipping.get_ocr_engine')
    def test_image_without_record_pk_returns_400(self, mock_factory):
        """record_pk 为 NULL → 400"""
    
    def test_nonexistent_image_returns_404(self):
        """image_id=999999 → 404"""
    
    @mock.patch('blueprints.shipping.get_ocr_engine')
    def test_empty_ocr_text_returns_400(self, mock_factory):
        """PaddleOCR 返回 '' → 400"""
    
    @mock.patch('blueprints.shipping.get_ocr_engine')
    def test_writes_record_ocr_event(self, mock_factory):
        """成功后写 1 条 record_ocr 事件"""
    
    @mock.patch('blueprints.shipping.get_ocr_engine')
    def test_uses_record_image_processor(self, mock_factory):
        """验证调用 RecordImageProcessor.extract_ocr (与上传行为一致)"""
```

### 7.2 前端手动验证 (Playwright)

```python
1. 打开 /shipping-records
2. 找 .re-ocr-btn → 点击
3. 验证:
   - 按钮 disabled + 文字 "OCR中" + 有 .loading class (spinner 可见)
   - 2-4 秒后恢复
   - 徽章状态根据 OCR 结果变 green/yellow/red
   - record_ocr 事件写入 DB
```

### 7.3 不动其他测试

`tests/test_shipping_p1_fixes.py`、`test_ocr_pipeline.py`、`test_ocr_match_event_triggers.py` 都涉及上传流程,但**不涉及** `/re-ocr` 端点,无需修改。

## 8. 风险点

| 风险 | 缓解 |
|------|------|
| RecordImageProcessor 与上传行为不一致 | 复用同一函数,行为天然一致 |
| 多图并发点 OCR 触发同订单锁竞争 | `RecordImageProcessor.extract_ocr` 内部有 `_OCR_LOCK`(单进程串行),无锁竞争风险 |
| PaddleOCR 单例模型崩溃 (None) | conftest.py:62 已清缓存,生产有 _ensure_model() 兜底 |
| 旧前端 `disabled`/`textContent` 直接写会绕过 `showBtnLoading` 的 spinner | 已替换为 `showBtnLoading`/`hideBtnLoading` |
| CSS `.loading::before` 与现有 `.btn.loading::before` 冲突 | 单独定义 `.re-ocr-btn.loading::before`,优先级高于 `.btn.loading`(更具体选择器) |
| OCR 期间用户点错其他按钮 | 仅这张图按钮 disabled,其他图/订单按钮仍可点 |

## 9. 不在本设计范围 (YAGNI)

- ❌ 真实异步任务队列 (需新增 `ocr_jobs` 表 + 状态查询 + SSE/WebSocket)— 改动太大,2-4 秒同步可接受
- ❌ 多图批量 OCR (一个按钮触发整单所有图)— 与"🤖 AI 匹配"功能重叠
- ❌ OCR 进度百分比 (前端不知道后端跑到哪)— 同步调用没有进度信号
- ❌ `.fuzzy-match-btn`/`.ai-judge-btn` 异步化 — 不耗时,无需改

## 10. 文件改动汇总

| 文件 | 改动 |
|------|------|
| `blueprints/shipping.py` | 新增 `api_v1_shipping_orders_re_ocr_image` 路由 (~45 行) |
| `templates/_record_image_script.html` | `bindReOcrButtons` 用 `showBtnLoading`/`hideBtnLoading` (~10 行 diff) |
| `static/css/app.css` | 新增 `.re-ocr-btn.loading` spinner 规则 (~13 行) |
| `tests/test_shipping_re_ocr.py` | 新增 6 个测试 (~100 行) |

总改动 ~170 行,跨 4 个文件,与用户请求"OCR 按钮改为异步提示"完全对应。