# 移动端当天出货页面（微信浏览器）

- 状态：设计稿（待用户审阅）
- 日期：2026-08-09
- 作者：Claude（brainstorming → superpowers:writing-plans）

## 1. 背景与目标

丰源工作台目前的出货/入库/装柜都在 PC 端操作。现场拍照与标签核实需要手机端：仓库工或司机打开微信扫一个固定 URL，就能看到当天所有出货订单，逐商品拍照上传，OCR/AI 判别由后端完成并自动回流。

约束：
- 仅拍照与人工确认是现场操作；订单/商品/客户信息在 PC 端预先录入
- 移动端只读订单与商品，**不提供结单按钮**
- 单 URL 访问，无需登录
- 视觉与交互按微信内置浏览器（iOS 微信 + Android 微信）单手操作为准
- 整体图（整体照/堆放/装车）必拍 1 张作整单证据
- OCR/AI 判别对标签糊图、方向倾斜给出明确提示

## 2. 范围

### 包含
- 新蓝图 `blueprints/mobile_shipping.py` 与路由 `/m/shipping-today`、`/m/shipping-today/order/<oid>`
- 新模板 `templates/mobile/shipping-today.html`、`templates/mobile/shipping-order.html`
- 新增样式 `static/css/mobile.css`（与 PC 端 `app.css` 隔离开）
- `static/css/app.css` 末尾追加引用；不污染现有 PC 布局
- 后端对行级/订单级图片上传端点的扩展：
  - `rotate_deg` 参数（Pillow 旋转矫正）
  - 客户端糊图检查（前端 Canvas + Laplacian）
  - 服务端 OCR 置信度 < 0.5 时的"图像可能模糊"提示
  - `need_rotate` 与 `suggested_deg` 响应字段
- 整体图上传：3 按钮（整体照/堆放/装车），`source='upload'`，`record_pk=NULL`
- 人工确认：复用 `POST /images/<id>/manual-verify`
- 自动旋转矫正：Pillow `img.rotate(deg, expand=True)` + `cv2.minAreaRect`/`pytesseract.image_to_osd` 估算主方向
- 测试：单测 + Playwright 渲染测试

### 不包含
- 移动端的订单/商品/客户信息编辑（继续走 PC 端）
- 移动端的入库/装柜订单（仅出货）
- 移动端的结单/锁定/解锁
- 移动端的语音录入（沿用 PC 端，暂不集成）
- 多用户/登录/权限（保持无登录；预留白名单接口）
- WebSocket / SSE（通知机制走短轮询）

## 3. 架构与复用

| 层 | 复用点 | 新增 |
|---|---|---|
| 数据模型 | `ShippingOrder` / `ShippingRecord` / `ShippingImage` / `OcrMatchEvent` | 无 |
| 业务方法 | `ShippingRecord.get_groups` / `ShippingImage.get_by_order` / `get_by_record` / `get_combined_for_record` / `set_match` / `set_human_verified` | 无 |
| API 端点 | `/api/v1/shipping-orders/*` 全部（包含 `manual-verify` / `ai-judge` / `fuzzy-match` / `ocr-detail` / `re-ocr`） | 行级/订单级上传端点接受 `rotate_deg`；返回 `need_rotate` / `current_rotation` |
| OCR 引擎 | `blueprints/ocr_engine.py`（PaddleOCR + DeepSeek + Moonshot） | Pillow 旋转矫正 + OpenCV 清晰度评估 |
| JS 库 | `templates/_record_image_script.html`（23 函数：轮询、徽标、按钮、人工覆盖） | 详情页 include 同一份并设 `api_prefix='/api/v1/shipping-orders'` |
| 共享 JS | `static/js/common.js`（图片预览、confirmDialog） | 移动端糊图检查函数（约 30 行，独立文件） |
| 样式 | 无 | `static/css/mobile.css`（约 250 行，命名空间 `.mobile-*`） |
| 鉴权 | `_AUTH_PUBLIC_PREFIXES` 现有 | 把 `/m/` 加入白名单 |
| 日志 | `6a6e679` 接入的 `_TRACE_ID` contextvars | 移动端路由注入 `set_log_context(biz='mobile_shipping', order_id=oid)` |

## 4. 页面与组件

### 4.1 列表页 `/m/shipping-today`

- 顶部 `app-head`：标题"今日出货" + 自动取今天日期（`date_cls.today().isoformat()`）
- 摘要三格：订单数 / 商品数 / 已拍数
- 订单卡片（每个订单一块）：
  - 客户名 + 单号 + 状态徽标（待补/已完成/存在不符）
  - 进度条（已拍 / 总数）
  - ✓/⚠/✕ 统计
  - 「进入商品详情 ›」按钮（链接到 `/m/shipping-today/order/<oid>`）
- 空状态：占位文案 + "今天没有出货订单"提示
- 响应数据：`ShippingRecord.get_groups(today, today)` → 服务端按 `order_pk` 分桶，统计每单的 ✓/⚠/✕ 数量

### 4.2 详情页 `/m/shipping-today/order/<oid>`

- 顶部 `app-head`：返回按钮（`‹ 今日订单`）+ 客户 + 单号 + "整体图 0/1"
- **整体图区**（必拍 1 张）：
  - 标题 "🖼 整体图（必拍）" + 状态徽标 "0/1" 或 "1/1"
  - 三按钮：`📷 整体照` / `📷 堆放` / `📷 装车`（点击直接拉起相机/相册）
  - 三个缩略图槽：未拍灰底；已拍绿底
  - 整体图上传后 OCR/AI 跳过，仅标"已拍"作整单证据
- **商品卡片**（每个商品一块）：
  - 顶部：`序号. 商品名` + 规格（颜色/厚度/支数/单位/备注）+ 状态徽标
  - 中间：`📷 拍照识别` / `🖼 相册`（同一行，两侧等宽）
    - 主按钮 `capture="environment"` 直接调起后置相机
    - 副按钮为系统图片选择
  - 状态行：单行摘要
    - 绿底 ✓ "最近一次识别：标签与规格一致" + 详情链接
    - 黄底 ⚠ "AI 判定与标签不一致" + 详情链接
    - 黄牌 + 置信度 < 0.5 → 状态行追加 "图像可能模糊，建议重拍"
    - 红底 ✗ "OCR 失败/AI 不符" + 详情链接
  - 红/黄牌时显示「✓ 人工确认」按钮 → 调 `POST /images/<id>/manual-verify`
  - 有图状态下主按钮文案变"📷 重新拍照"
- **识别记录折叠区**（页面底部）：
  - 标题"📷 识别记录（N）" + 倒序指示
  - 缩略图 + 商品名 + 相对时间 + 置信度 + 状态徽标
  - 仅商品行参与；整体图不进入此区
- 无结单按钮

### 4.3 拍照与上传

```
点击「拍照识别」/「相册」
   ↓
浏览器：<input type="file" accept="image/*" capture="environment">
   ↓
JS：rotate_deg（默认 0，可选 0/90/180/270）
   ↓
JS：canvas.toDataURL() → gray → cv2.Laplacian.var()
   ↓
var < 80 → 弹"图太糊，请重拍"；return
   ↓
POST /api/v1/shipping-orders/records/<rid>/images
   FormData: image=blob, source='upload', rotate_deg=0
   ↓
后端接收：
   - check_uploaded_image (扩展名 + magic bytes)
   - rotate_deg 校验（0/90/180/270）
   - Pillow img.rotate(rotate_deg, expand=True) → 临时保存
   - ShippingImage.create(...) → image_id
   - ShippingOrder.get_by_id(oid) → 注入 _ASYNC_JOBS[image_id] = 'pending'
   - threading.Thread(target=_process_record_image_async, args=(image_id, oid, rid)).start()
       (用 contextvars.copy_context() 包裹 trace_id)
   ↓
201 {success, images:[{image_id, processing:true, ...}], async:true}
   ↓
前端 pollRecordImageMatch(image_id, record_id)
   - 每 1500ms GET /images/<id>/match-status
   - processing=false → 取 match_status / score / reason / bg_color / human_verified
   - 调 applyAsyncMatchResult() 局部刷新：
     * 状态行更新（绿/黄/红）
     * 状态徽标更新
     * 状态行追加"详情"链接（弹 ocr-detail）
     * 状态行根据 reason 追加"图像可能模糊"提示
     * 红/黄牌时显示"✓ 人工确认"按钮
```

### 4.4 自动旋转（与方案 3 一致）

- 客户端可在提交前点"调整方向"按钮：`rotate_deg` = 0/90/180/270
- 服务端在 `_process_record_image_async` 开头加：
  ```
  def auto_correct_orientation(img_bytes):
      img = Image.open(BytesIO(img_bytes))
      osd = pytesseract.image_to_osd(img, output_type=Output.DICT)
      page_rotation = osd.get('rotate', 0)
      if page_rotation:
          img = img.rotate(-page_rotation, expand=True)
      return img, page_rotation
  ```
- 若 OCR 后文本行平均置信度 < 0.55 或检测到方向异常，响应里 `need_rotate=true` + `suggested_deg=90/180/270`
- 前端收到 `need_rotate` → 弹"调整方向"小提示 + 90°/180°/270°/0° 四个按钮
- 用户选择后只对当前图片生效（重新上传带新 `rotate_deg`）

### 4.5 整体图上传

- 走 `POST /api/v1/shipping-orders/<oid>/images`，`source='upload'`，`record_pk=NULL`
- 三按钮（整体照/堆放/装车）只是 `original_name` 加前缀，便于在缩略图槽中区分
- 上传后 OCR 跳过（与现有订单级图片一致），仅标"已拍"
- 整体图与"识别记录"折叠区**不展示**整体图（避免混淆），仅商品行参与

### 4.6 人工确认与状态回流

- 红/黄牌时显示「✓ 人工确认」按钮 → `POST /api/v1/shipping-orders/images/<id>/manual-verify {verified:true}`
- 后端写 `human_verified=1` + 写 `ocr_match_event` 的 `human_verify` 事件
- 前端轮询拿新 `human_verified=1` → 状态行变绿 + 显示 👤 标记
- 幂等：重复点确认直接返回 success

### 4.7 通知机制（短轮询）

- 上传完成 → 后端立即返回 `image_id`（`processing=true`）
- 前端 `pollRecordImageMatch` 每 1500ms 拉 `GET /images/<id>/match-status`
- 后端先查 `_ASYNC_JOBS[image_id]`：存在 → `{processing:true}`；否则查 DB 返回 `match_status` 等字段
- 150s 超时回落：前端 `pollRecordImageMatch` 自带超时回落（沿用现有实现）
- 完成回调：`applyAsyncMatchResult(imageId, img, recordPk)` 局部刷新状态行 + 徽标 + 详情链接 + 人工按钮

### 4.8 同 record 第二张图（成功后替换）

- 用户重新拍照 → 第二张 `image_id` 与第一张并存
- 前端轮询两张图分别处理
- 当新图 `processing=false` 且 `match_status=green`：
  - 调 `DELETE /api/v1/shipping-orders/images/<old_id>` 删除旧图
  - 状态行刷新（保留新图徽标）
- 失败策略：新图非 green → 旧图保留，新图状态行也显示，不删

## 5. 数据流（端到端）

```
[移动端]
用户 → /m/shipping-today (GET) → ShippingRecord.get_groups(today, today)
   ↓
列表渲染（订单卡片）
   ↓
用户点击「进入商品详情」→ /m/shipping-today/order/<oid> (GET)
   → ShippingOrder.get_by_id + ShippingRecord.get_by_order + ShippingImage.get_by_order
   → 渲染详情
   ↓
用户点击「📷 拍照识别」/「🖼 相册」
   → 客户端清晰度检查（Canvas + Laplacian）
   → 失败：弹"图太糊，请重拍"
   ↓
POST /api/v1/shipping-orders/records/<rid>/images
   {image, source='upload', rotate_deg}
   ↓
[后端] 接收 → check_uploaded_image → Pillow rotate → 落盘 → ShippingImage.create
   → _ASYNC_JOBS[image_id] = 'pending' → threading.Thread(...).start()
   ↓
201 {success, image_id, processing:true, async:true}
   ↓
[移动端] pollRecordImageMatch(image_id)
   GET /api/v1/shipping-orders/images/<id>/match-status
   ↓
[后端] _process_record_image_async 后台运行:
   - auto_correct_orientation（用 pytesseract OSD）
   - PaddleOCR 提取文字
   - detect_bg_color
   - _classify_and_match_image（注入 supplement_prompt）
   - 计算 OCR 平均置信度
     - < 0.5 → reason 追加 "[图像可能模糊，建议重拍]"
   - ShippingImage.set_match(...)
   - OcrMatchEvent.append('record_ocr')
   - 若 deepseek 参与 → OcrMatchEvent.append('ai_match')
   - _ASYNC_JOBS[image_id] = 'done'
   ↓
[移动端] pollRecordImageMatch 拿到 processing=false
   - 更新状态行（绿/黄/红）
   - 更新状态徽标
   - 追加"详情"链接（弹 ocr-detail）
   - reason 含"模糊"时 → 状态行追加"图像可能模糊，建议重拍"
   - 红/黄牌 → 显示"✓ 人工确认"按钮
   ↓
[可选] 用户点"✓ 人工确认" → POST /manual-verify
   → ShippingImage.set_human_verified(image_id, 1) + OcrMatchEvent.append('human_verify')
   → 前端轮询拿新 human_verified=1 → 状态行变绿 + 👤
```

## 6. 错误处理与边界

### 6.1 上传阶段
- 文件超 10MB / 非白名单扩展名 / magic bytes 校验失败 → 后端 400，前端弹"图片格式不支持/太大"
- 网络断开 → fetch 抛错 → toast "网络中断，请重试"；不丢失当前已拍商品状态
- `rotate_deg` 越界（不在 0/90/180/270）→ 后端 400，提示 "方向参数非法"

### 6.2 糊图检测（1 + 3 组合）
- **客户端**：拍照/选图后 Canvas → 灰度 → `cv2.Laplacian.var()` < 80 → 弹"图太糊，请重拍"，**不上传**  
  - 兜底：若客户端 Canvas/Pillow 不可用（极少数微信版本），跳过客户端检查直接上传
- **服务端**：PaddleOCR 平均置信度 < 0.5 → `match_status='yellow'`，`reason` 字段追加 `[图像可能模糊，建议重拍]`  
  - 状态行底部显示该提示，触发"📷 重新拍照"按钮高亮

### 6.3 OCR/AI 异步处理
- PaddleOCR 抛异常（图片损坏）→ `set_match(image_id, 'red', 0, 'OCR失败', 'local_fuzzy')`；前端轮询拿到 red，显示"重新拍照"按钮
- DeepSeek 调用失败（key 缺 / 网络）→ 自动 fallback 本地 RapidFuzz；状态行同 yellow
- 自动方向矫正失败（OSD 估不出）→ 不阻塞，继续 OCR；`ocr_match_event.prompt_payload` 记 `auto_rotate=skipped`
- `need_rotate=true` 时前端弹"调整方向"小提示，按钮：90°/180°/270°/0°；用户选择后只对当前图片生效

### 6.4 并发 / 数据一致性
- `_OCR_LOCK`（`blueprints/shipping.py:49`）串行化 OCR+AI 调用；移动端用户基本不并发，安全
- 同一 record 在 30s 内连拍两张 → 第二张 `image_id` 与第一张并存，前端轮询分别处理；新图成功后"成功后替换"逻辑删旧图
- 删除旧图失败（已被人删/物理文件丢失）→ 后端返回 success 但前端忽略，状态行继续显示成功

### 6.5 整体图
- 上传成功但 `record_pk=NULL`，OCR 跳过（与现有订单级图片一致）  
- 同一 order 已存在整体图时：**沿用"成功后替换"** 策略；不强制 1 张，缩略图槽仍 3 个都显示
- 整体图与"识别记录"折叠区：**不展示**整体图

### 6.6 人工确认
- 重复点"✓ 人工确认" → 后端幂等（已有 `human_verified=1` 直接返回 success）
- 用户撤销人工确认 → 后端 200 但 `human_verified=0`；前端状态行回退到原 AI 判定

### 6.7 登录/权限
- 移动端走 `/m/` 前缀无登录；如未来加登录，只把 `_AUTH_PUBLIC_PREFIXES` 里 `/m/` 移走即可

### 6.8 PC 端兼容
- 现有 PC 出货页对同一 `image_id` 的"行级图片"是同一份数据；移动端拍照/OCR 后，PC 端打开能看到同样的 `match_status`；人工确认也是同一接口

### 6.9 可观测性
- 沿用 `6a6e679` 的 logging 接入：移动端路由注入 `set_log_context(biz='mobile_shipping', order_id=oid)`
- 移动端特有错误（拍照失败 / 自动旋转判断 / 糊图提示）统一走 `audit_log` 写 `error_log` 行

## 7. 测试与验证

### 7.1 单元测试（`tests/`）
- `test_mobile_shipping_routes.py`：GET 列表 + 详情 200；空当天数据兜底
- `test_mobile_shipping_upload.py`：行级上传 → 轮询 5 次拿到非 processing 状态；糊图 mock 平均置信度 0.4 → 状态行 reason 含"模糊"
- `test_mobile_shipping_rotate.py`：`rotate_deg=180` 上传 → OCR 文本对比正确；`need_rotate=true` 路径
- `test_mobile_shipping_overall.py`：整体图上传 → `record_pk` 留空；OCR 跳过
- `test_mobile_shipping_manual.py`：黄牌调 `manual-verify` → `human_verified=1`；行内徽标变绿
- `test_mobile_shipping_replace.py`：同 record 第二张图成功后调 DELETE 旧图
- `test_mobile_shipping_blur_client.py`：客户端 Laplacian.var < 80 弹"图太糊"

### 7.2 Playwright 渲染测试（`tests/render_*.js`）
- `render_mobile_shipping_today.js`：Playwright headless + iPhone 12 viewport（390×844）打开 `http://127.0.0.1:5050/m/shipping-today`，截今日订单列表
- `render_mobile_shipping_order.js`：进入第一条订单，截图整体图区 + 商品卡片 + 识别记录
- 验收标准：所有按钮可点、状态行/徽标/缩略图与设计图一致

### 7.3 PC 端到端
- Playwright 在 PC 浏览器 390px 窗口 + 微信 UA 验证：
  1. 访问 `/m/shipping-today` 直接进入（无登录页）  
  2. 列表首单 → 进入详情  
  3. 模拟文件选择上传 → 看到轮询 + 状态回流  
  4. 切到 PC 完整 `/shipping-records?start=today&end=today` 同一订单 → 移动端拍的照片/状态同步显示  

### 7.4 回归
- `pytest -v tests/test_shipping_p1_fixes.py tests/test_shipping_p2_p0.py tests/test_shipping_yellow_manual_confirm.py tests/test_shipping_image_btn_highlight.py tests/test_shipping_match_badge_refresh.py tests/test_shipping_orphan_images.py tests/test_ai_match_endpoint.py tests/test_record_upload_match.py`

### 7.5 手动 PC 验收
- `python app.py` → 打开浏览器窗口缩到 390px → 访问 `/m/shipping-today` → 验视觉 + 拍照（用 PC 文件选择器模拟）  
- 后端日志看 `set_log_context(biz='mobile_shipping', order_id=...)` 注入正确  
- DB 检查 `shipping_images` 的 `match_status` / `match_source` / `human_verified` 与 PC 端一致  

## 8. 验收标准

- [ ] `pytest -v tests/test_mobile_shipping_*.py` 全部通过
- [ ] `pytest -v` 现有 32 个测试文件无回归
- [ ] Playwright 渲染测试在 iPhone 12 viewport 下截屏与设计图一致
- [ ] PC 浏览器 390px 窗口 + 微信 UA 模拟访问 `/m/shipping-today` 走通完整流程
- [ ] 移动端拍照上传一张 → PC 端 `/shipping-records?start=today&end=today` 看到同一张图与状态
- [ ] 客户端糊图检查生效（方差 < 80 拦截上传）
- [ ] 服务端 OCR 置信度 < 0.5 时 reason 含"模糊"
- [ ] 自动旋转：手动 `rotate_deg=90/180/270` 上传 → OCR 文本对比正确
- [ ] 整体图上传 → OCR 跳过（`match_status=NULL`），仅"已拍"标识
- [ ] 红/黄牌 → 人工确认按钮可用；确认后状态行变绿 + 👤
- [ ] 同 record 第二张图成功后旧图被删（成功后替换）

## 9. 风险与限制

1. **微信内置浏览器兼容性**：Canvas + OpenCV.js（用 `@techstark/opencv-js`）需在微信内置浏览器验证；兜底直接上传
2. **`_ASYNC_JOBS` 进程内 dict**：单用户本地部署 OK；移动端并发 ≤ 5 张图安全
3. **OCR 引擎单例常驻显存**（PaddleOCR ~200MB+）：与 PC 端共用同一进程
4. **PC 端 `set_human_verified(False)` 复位**（`shipping.py:1334`）：PC 端整单重 AI 匹配会清掉所有人工确认；移动端不直接触发"AI 匹配"，但用户后续在 PC 端点"AI 匹配"时会丢移动端做的人工确认——文档提醒，不在移动端加按钮
5. **微信浏览器后台杀进程**：轮询中断后用户切回页面会丢部分回调；需提供"刷新全部图状态"按钮（已沿用现有 `GET /records/<rid>/images-area`）
6. **整体图强制 1 张**的设计仅限初版；后续如需多张，按"成功后替换"策略扩展
7. **导航入口**：初版不暴露在 `base.html` 顶栏；用户通过直接 URL 访问；后续可加二维码或菜单项
