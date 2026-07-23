# 出货明细图片 OCR 匹配校验 — 设计文档

- 日期：2026-07-23
- 页面：`/shipping-records`
- 路线图定位：阶段 2「AI 验数 + 校对」的一块落地
- 状态：待用户 review

## 1. 背景与目标

出货明细行现在已支持挂"行级图片"（`shipping_images.record_pk` 指向某条 `shipping_records`，
上传/取图 API 已存在）。用户希望在给某一行贴图（多为**手机拍的实物商品标签**）时，
自动判断"这张照片上的文字，跟这一行已录入的品名+规格是不是同一个商品"，用一个徽章提示，
起到**防错 / 证据链**作用。

两类图片来源，识别难度差别很大：

- **整张打印单据**（已有的整单识别流程）：文字规范、识别准。
- **手机拍的实物标签**（本次新增校验对象）：无统一格式、拍摄角度/反光问题、文字顺序不规范。
  例：品名"硬加面"、颜色"黑色"这类**属性 token**，标签上顺序随意。

### 目标（方案 A：只做校验提示，不自动改数据）

给明细行打一个匹配徽章：
- 🟢 绿 ✓ 图文相符
- 🟡 黄 ⚠ 存疑
- 🔴 红 ✗ 可能不符

徽章是**提示**不是判定，误报由人工无视即可。复用现有备注 mismatch 的红/粉行视觉语言。

### 非目标（明确排除，避免范围蔓延）

- 不做自动回填（不拿识别结果去改品名/规格）。
- 不做全表 1000 SKU 检索匹配——只跟"当前这一行已录入的品名+规格"这一个目标比对。
- 本期只做**出货** `/shipping-records`；入库/装柜先不动（record 级图片迁移当前只覆盖 shipping/loading，
  且需求只提了出货）。

## 2. 总体架构：两档匹配，共用一套徽章

```
[行级图片] 上传某明细行的图
   → PaddleOCR 提字（本地）
   → RapidFuzz 本地比对（对"本行 品名+规格"）
   → 写 match_status/match_score
   → 行首徽章 🟢/🟡/🔴
   特性：高频、即时、离线、免费、上传即算

["AI 匹配" 按钮] 作用于整块图片区
   → PaddleOCR 提字
   → DeepSeek API 语义比对（对整单所有明细行）
   → 逐行回填 match_status/match_score
   → 同一套徽章
   特性：低频、手动触发、联网、更准（换更强引擎，产出与行级一致 = 方案 C）
```

两档产出**同一种徽章**，用户无需学习两套语义。

## 3. 匹配算法（核心难点）

难点本质：**文字顺序不规范 + OCR 噪声/漏字**。这否定了现有 `_split_name_spec` 的"子串精确匹配"
（要求字连续且顺序对，OCR 成"加面硬"就漏）。

### 行级：RapidFuzz（新增依赖 `rapidfuzz`，MIT、纯 C++、轻量、离线）

新增纯函数 `match_label_to_row(ocr_text, product_name, specification) -> (status, score)`，
放在 `blueprints/_helpers.py`：

1. **归一化**（两边都做）：全角→半角、去空格/标点、`码`→`y` 统一、大小写统一。
2. **目标串**：把本行 `product_name + specification` 拼成一个**属性 token 集**
   （硬加面 / 黑色 / 尺寸数字…）。中文按字符切。
3. **打分**：
   - `token_set_ratio`（无视顺序，主力，专治"顺序不规范"）
   - `partial_ratio`（容忍目标只是照片文字的一部分）
   - 取两者综合。
4. **加权（已定：品名为主 + 规格加分）**：
   - 品名 token 命中为主要得分；规格 token 命中作加分项。
   - 理由：手机拍标签时规格常被遮挡/拍不全，若强制规格也对上会误报红。
5. **阈值分档（默认值，实拍后可调）**：
   - `score ≥ 85` → 🟢 绿
   - `60 ≤ score < 85` → 🟡 黄
   - `score < 60` → 🔴 红

### 整单："AI 匹配"按钮 → PaddleOCR + DeepSeek

复用现有 `ocr_engine.py` 的 `DeepSeekEngine`（PaddleOCR 提字 → DeepSeek 结构化的能力已就绪），
新增一个"比对型" prompt：给 DeepSeek 传入①OCR 出的整块文字 ②该订单所有明细行的
`(行号, 品名, 规格)` 列表，让它逐行返回 `{行号, match_status, 理由}`。后端据此逐行写回。

## 4. 数据与展示

### Schema 改动（唯一的破坏性操作，需先备份 DB）

`shipping_images` 增两列（幂等 `ALTER TABLE ADD COLUMN`，放 `models/_init.py` 迁移段）：

```sql
ALTER TABLE shipping_images ADD COLUMN match_status TEXT DEFAULT NULL;  -- 'green'|'yellow'|'red'|NULL
ALTER TABLE shipping_images ADD COLUMN match_score  REAL DEFAULT NULL;  -- 0~100
```

- 存在**图片**上（一行可有多张图，各自算分）。
- **行徽章 = 该行所有图片里的最高分对应的档**（有一张对上就倾向绿）。
- 存库理由：刷新页面徽章不丢。

> 破坏性操作前置：按 CLAUDE.md「踩坑点 6」，跑迁移前先
> `Copy-Item worklog.db D:\BAK\worklog_YYYYMMDD_HHmm.db`。

### 后端挂载点

- 行级：`api_v1_shipping_orders_record_upload_images`（`shipping.py:537`）里，每张图
  `ShippingImage.create` 之后，调 `match_label_to_row(...)`，把 status/score 写回该图，
  并在响应 JSON 里带上（前端即时贴徽章）。
- 整单：新增端点
  `POST /api/v1/shipping-orders/<order_id>/ai-match`，走 DeepSeek，逐行回写后返回汇总。

### 前端（`templates/shipping-records.html`）

- 明细行首加一个徽章位（🟢/🟡/🔴/无），沿用局部刷新，不整页 reload。
- 上传行级图成功 → 读响应里的 status → 即时贴徽章。
- 整块图片区加"AI 匹配"按钮 → 点击转圈 → 回来逐行更新徽章 + 一句汇总
  （如"6 行已核对：5 ✓ / 1 ⚠"）。

## 5. 复用现有资产（大幅缩小工作量）

- ✅ 行级图片：上传 API（`records/<id>/images` POST）、取图区 API（`records/<id>/images-area` GET）、
  `record_pk` 存储、迁移，**全部现成**，不用动。
- ✅ OCR 引擎层 `ocr_engine.py`：PaddleOCR 与 DeepSeek 引擎均已就绪，直接复用。
- 🆕 只需新增：`match_label_to_row()` 纯函数、两列 schema、`ai-match` 端点、行徽章 UI、
  上传端点里的一处调用。

## 6. 风险 / 坑

- 手机拍标签 OCR 质量远低于打印单（反光、角度、字体杂）→ 靠 PaddleOCR `use_angle_cls`
  + 归一化 + fuzzy 容错兜底；徽章定位为"提示"，容忍误判。
- 短品名共用字误判（"硬加面" vs "加硬"）→ `token_set_ratio` + 叠加规格 token 降低误报。
- 新依赖 `rapidfuzz`（可离线 `pip install`，无重型传递依赖）。
- DeepSeek 端点每次一整单一次调用（不是每行一次），成本与延迟可控。
- 迁移前必须备份 DB（见上）。

## 7. 待实现清单（供后续 writing-plans 展开）

1. `pip install rapidfuzz` + 写进 `requirements.txt`。
2. `models/_init.py` 迁移段加两列（幂等）。
3. `blueprints/_helpers.py` 加 `match_label_to_row()` 纯函数 + 单元测试。
4. `blueprints/shipping.py` 行级上传端点挂载匹配 + 响应带 status/score。
5. `blueprints/shipping.py` 新增 `POST .../<order_id>/ai-match`（DeepSeek 比对）+
   `ocr_engine.py` 加比对型 prompt/方法。
6. `templates/shipping-records.html` 行徽章 UI + "AI 匹配"按钮 + 局部刷新接线。
7. 用几张真实标签照片实测，微调阈值（85 / 60）。

## 8. 已拍板决策记录

- 匹配对象：仅当前行的品名+规格（非全表检索）。
- 结果用途：方案 A（只打徽章，不自动改数据）。
- 两档引擎：行级 RapidFuzz 本地即时；整单 PaddleOCR+DeepSeek，产出同一套徽章（方案 C）。
- 加权：品名为主 + 规格加分。
- 徽章持久化：加 `match_status`/`match_score` 两列，存在图片上，行徽章取最高分档。
