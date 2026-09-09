# 标签 OCR 预处理设计（按退化类型分派管线）

> 改写自 `2026-08-16-ocr-wrinkle-label-preprocessing-design.md`。
> 原方案把问题定位为「褶皱导致对比度低 → CLAHE 增强」，经实测验证定位错误，本文档为纠正版。
> 关键结论：**磅布三文治 类标签主因是「无表格线 → 行合并/整行漏检」，不是褶皱对比度**；且原方案只覆盖了 3 类退化标签中的 1 类。

## 背景

装柜 / 入库场景的行级商品标签，存在 3 种与「褶皱」无关的、导致 PaddleOCR 漏字 / 污染文字的退化机制。按标签类型分列：

| 标签类型 | 退化机制 | 造成的 OCR 失败 | 近 30 天 yellow/red 体量 |
|---|---|---|---|
| 磅布三文治（含 7P环保 / 黑 / 白 / B级 变体） | 标签 **无表格线**，呈「4 行 × 2 列」自由表单；行距极小、行间无空白间隙 | DB 检测把多行并成 1 个大框，或**整行未被检出** → 漏字 | 24（黑 19 + 白 5） |
| 无纺布 | 标签覆**透明膜**，拍照产生局部反光高光 | 高光区域文字被冲白、丢字 | 5 |
| 杂胶 / 纯胶 | 标签盖有**红色合格章**，压住部分文字 | 红章颜色污染文字区域，检测/识别受干扰 | 杂胶 16 + 纯胶 2 |

3 类合计占近 30 天 91 张 yellow/red 的约 52%。

### 机制实测（坐实，非推测）

对 3 张 磅布三文治 黄/红标签跑原图 PaddleOCR 检测框，证实主因是「无表格线 → 行合并/漏行」，**不是**褶皱对比度：

- 左列 4 个行头（品/厚/手/底）常被并成 1 个跨 4 行的大框 `品厚手底`；
- 右列值行常**整行漏检**（如某张 0.8 硬性原图只剩 `品厚手底`，4 个值全丢）→ DeepSeek 自然找不到品名/厚度 → 判 red；
- 将 `det_db_thresh` 0.2→0.15、`det_db_box_thresh` 0.4→0.30、`use_dilation=True`（原方案的「褶皱调参」）对这 3 张**几乎无变化** → 印证降阈值救不了行合并，CLAHE 也救不了（它只做对比度）。

实测对比（表单分行 OCR 后找回原图漏掉的值）：

| 标签 | 原图整图 OCR（漏字） | 4 行分行后 OCR |
|---|---|---|
| 0.8 硬性 | `品厚手底`（4 个值整行漏检） | 找回 `名：磅布三文治 / 厚度：0.8mm / 感：加硬 / 布：磅布` |
| 0.8 中性 | 缺品名、厚度 | 找回 `品名：磅布三文治` + `厚度：0.8mm` |
| 1.4 硬性 | 值均在但左列被并框 | 4 值行均独立清晰 |

> 关于 磅布三文治 标签「无颜色信息」：标签上压根不印颜色字，此前 DeepSeek 偶有把「黑底」误当「黑色商品」属提示词/判别层问题（由用户自维护的 `category_prompts` 表处理，见非目标），本方案不掺和。

## 目标

针对 3 类退化标签，在 PaddleOCR 文字提取前**按退化类型分派对应预处理管线**，降低漏字 / 文字污染，使 AI 比对结果从 yellow/red 向 green 偏移。

## 非目标

- 不改造整单 OCR 识别路径（Moonshot / DeepSeek 引擎场景）；
- **不调整提示词 / 商品品类提示词系统**：用户已建独立的 `category_prompts` 表并自行补充维护，本方案纯在「图像预处理 + 检测参数」算法层，与提示词层解耦；
- 不引入新的第三方依赖（必要时优先 pin 已在仓库被引用但未锁版本的 `cv2`，而非新增包）；
- 不处理非上述 3 类的其它漏字（本次未反映）。

## 设计概览

**位置**：`blueprints/ocr_engine.py`

**核心思路变更**：原方案的布尔门控 `is_wrinkle_label_category(product_name) -> bool` 只能表达「要不要增强」，无法区分 3 种机制。改为**退化类型分派**：

```
ocr_preprocess_kind(product_name) -> 'form_nolines' | 'glare' | 'redstamp' | None
```

`extract_text_with_conf(bytes, preprocess_kind=...)` 据此选管线。调用方从 record 取 `product_name` 判类型，传入对应 kind（原参数名 `apply_wrinkle_enhance` 保留向后兼容语义，但含义扩展为「走专用预处理」，详见调用方集成）。

**数据流**：

```
行级图片上传 / AI 判别按钮
   │
   ▼
[shipping.py / inbound.py / loading.py 任一行级图接口]
   │
   ▼
ShippingRecord.get_by_id(record_pk)  ← 取 product_name
   │
   ▼
ocr_engine.ocr_preprocess_kind(product_name)
   │
   ▼
kind = 'form_nolines' | 'glare' | 'redstamp' | None
   │
   ▼
PaddleOCREngine.extract_text_with_conf(bytes, preprocess_kind=kind)
   │
   ├─ 'form_nolines' → _extract_form_lines(逐行裁切 OCR + 整图对照择优)   ✅ 已实现
   │
   ├─ 'glare'        → 反光抑制(Retinex/MSRCR 或高光衰减)后再 OCR         🔲 待实现
   │
   ├─ 'redstamp'     → 红域分割掩膜 + 修复(inpaint)后再 OCR               🔲 待实现
   │
   └─ None           → 原图直接进 PaddleOCR（完全向后兼容）
```

## 关键组件

### 1. 退化类型分派：`ocr_preprocess_kind(product_name) -> str | None`

**位置**：`blueprints/ocr_engine.py` 模块级公开函数（取代原 `is_wrinkle_label_category`）。

**判定逻辑（双轨）**：
1. **子串轨**（主要，覆盖当前数据）：
   - `form_nolines`：品名含「磅布三文治」
   - `glare`：品名含「无纺布」
   - `redstamp`：品名含「杂胶」或「纯胶」
2. **品类轨**（辅助，覆盖未来未知变体）：查 `product_categories` 表得 `category_code`，按 code→kind 静态映射。映射建议在 `category_prompts` 同源的配置里维护，避免每次上传再烧一次 LLM（原方案调 `DeepSeekEngine._classify_product` 属浪费，改为查表）。
3. 任一轨命中返回对应 kind，否则 None。

**防御**：任一轨 DB / 查表异常 → 返回 None，走原路径，不阻塞；`product_name` 为空 → None。

### 2. 磅布三文治管线：表单分行 OCR（✅ 已实现）

**位置**：`PaddleOCREngine._form_row_bands()` + `_extract_form_lines()`

**算法步骤**：
1. 灰度化：`L = 0.299*R + 0.587*G + 0.114*B`；
2. 暗像素二值（文本=暗），做水平投影定位文字上下边界；
3. 在文字上下边界内**等分 4 行**（标签为固定 4 行表单：品名/厚度/手感/底布），每行上下各扩 3px 容错；
4. 逐行裁切，分别跑 PaddleOCR 识别；
5. 同时跑一次**整图 OCR** 作为对照；
6. **择优**：行分行结果字符数 ≥ 整图结果时取分行结果，否则取整图（防御分行把跨行字切坏的情况）；
7. 合并去重，返回拼接文本。

**为什么等分 4 行而非按间隙分行**：实测该类标签行距极小、行间几乎无空白间隙，靠"找间隙"会把整张并成 1 带、分行失败。固定 4 行表单用上下边界等分最稳。

**依赖**：仅 `numpy` + `Pillow`（已在 `_resize_if_needed` 中使用）。

**性能**：每张图约 5 次 OCR（4 行 + 1 整图对照），人拍节奏下可接受（< 300ms 预算偏紧，若超时可在「分行结果已含全部字段」时跳过整图对照）。

> 实施时修复的原代码 bug：旧 `apply_wrinkle_enhance` 路径写 `np.array(Image.open(io.BytesIO(resized)))`，但 `_resize_if_needed` 已返回 `ndarray`，导致 enhance 路径每次抛 `BytesIO` 解码错被 `try/except` 吞掉、返回 `''` —— **原 CLAHE 方案此前根本未生效**。新路径直接对 ndarray 操作，已修正。

### 3. 无纺布管线：反光抑制（🔲 待实现）

**退化**：透明膜局部高光把文字冲白。

**算法方向**（非 CLAHE —— CLAHE 会把高光当局部低对比度去拉，反而放大光斑）：
- Retinex / MSRCR 多尺度光照估计，去除非均匀光照；
- 或高光检测（`L > 0.9` 且饱和度低）后做高光衰减 / 局部修复；
- 建议优先用 `cv2`（仓库已引用）实现，必要时 pin 版本。

**验证前提**：先取 2–3 张真实反光标签，确认漏字是否确由高光引起，再定算法。

### 4. 杂胶 / 纯胶管线：红章掩膜 + 修复（🔲 待实现）

**退化**：红色合格章压住文字，颜色污染检测/识别。

**算法方向**：
- 在 HSV/Lab 空间取红域（`H` 近红且 `S`/`L` 满足阈值）得到红章掩膜；
- 掩膜内文字区域做 inpaint（`cv2.INPAINT_TELEA` / `NS`）或简单灰度替换；
- 修复后再进原 OCR；
- ⚠️ 红色合格章是颜色污染，CLAHE（仅灰度对比度）碰不到，必须走颜色分割。

### 5. 调用方集成

**接入点**（3 蓝图，同原方案）：

| 蓝图 | 端点 | 行为 |
|---|---|---|
| `shipping.py` | 行级图片上传 / AI 判别 | 按 record.product_name 判 `preprocess_kind` 透传 |
| `inbound.py` | 行级图片上传 | 同上 |
| `loading.py` | 行级图片上传 | 同上 |

**向后兼容**：`extract_text_with_conf(bytes)` 默认 `preprocess_kind=None`，老调用方零感知。`apply_wrinkle_enhance` 旧参数名保留为别名（True 等同 `preprocess_kind='form_nolines'`），待三蓝图改造后逐步废弃。

## 错误处理

| 失败点 | 处理 | 影响 |
|---|---|---|
| `ocr_preprocess_kind` 内部 DB / 查表异常 | 返回 None，主流程走原路 | 无 |
| `product_name` 为空 | 返回 None | 无 |
| 任一分派管线 numpy 计算异常 | try/except 包住，return 原图/原文本，日志 WARNING | 无（仅该张图走原路） |
| PaddleOCR 模型加载失败 | 与现有行为一致，fallback Moonshot | 已有 |
| 分行结果异常短（疑似切坏） | 自动回退取整图对照结果 | 无 |

各预处理不抛异常上抛 —— 设计硬约束。

## 测试策略

### 1. 单元测试（各管线）

- **表单分行** `tests/test_form_row_split.py`：合成「白底 + 黑字 4 行表单（无表格线）」，验证分行 OCR 文本含全部字段，且优于整图 OCR；异常输入（空 bytes / 非图像 / 超小图）不抛异常。
- **反光抑制** `tests/test_glare_suppress.py`（待管线实现）：合成高光叠加图，验证文字还原。
- **红章掩膜** `tests/test_redstamp_mask.py`（待管线实现）：合成红章压字图，验证红域被分离、文字可识别。

### 2. 回归测试 `tests/test_ocr_preprocess_kind.py`

- `ocr_preprocess_kind('白磅布三文治')` → `'form_nolines'`
- `ocr_preprocess_kind('黑磅布三文治')` → `'form_nolines'`
- `ocr_preprocess_kind('无纺布')` → `'glare'`
- `ocr_preprocess_kind('杂胶')` → `'redstamp'`
- `ocr_preprocess_kind('纯胶')` → `'redstamp'`
- `ocr_preprocess_kind('其他品名')` → `None`
- `ocr_preprocess_kind('')` / `None` → `None`
- 查表异常 → `None`，主流程不挂

### 3. 集成测试 `tests/test_compare_rows_preprocess.py`

- 准备真实 fixture：3 类各取若干张 yellow/red 标签，复制到 `tests/fixtures/label_preprocess/`，每张配 `.json` 元数据；
- 断言**按退化类型分别统计**：
  - ① OCR 可救子集（真因是漏字/污染）→ `preprocess_kind` 路径比 `None` 路径 match_status 改善到 green；
  - ② **真实规格不符子集不得变绿**（防止假阳性通过）：fixture 须含确认不符的样本，断言其仍为 yellow/red。
- 延迟：行级图上传平均增量 < 300ms（P50）。

### 4. Fixture 抽取脚本 `tools/extract_label_fixtures.py`（一次性，只读 + cp，不 UPDATE/DELETE）

- 从 `shipping_images JOIN shipping_records` 筛近 30 天 yellow/red，按品名分 3 类复制到 `tests/fixtures/label_preprocess/{form_nolines,glare,redstamp}/`；
- 输出抽取报告（命中数、跳过数、文件大小）。

## 实施分块

| 块 | 内容 | 状态 | 估时 |
|---|---|---|---|
| 1 | 表单分行 OCR（_form_row_bands + _extract_form_lines）+ 单测 | ✅ 已完成 | 已实现 |
| 2 | `ocr_preprocess_kind` 取代 `is_wrinkle_label_category` + 回归测试 | 🔲 | 30min |
| 3 | `extract_text_with_conf` 参数改为 `preprocess_kind` + 向后兼容别名 | 🔲 | 30min |
| 4 | 三蓝图调用方接入 | 🔲 | 1h |
| 5 | 无纺布反光抑制管线 | 🔲 | 1h |
| 6 | 杂胶/纯胶红章掩膜管线 | 🔲 | 1h |
| 7 | Fixture 抽取脚本 + 集成测试 | 🔲 | 1h |

## 风险与回滚

**风险**：
- 表单分行对「非固定 4 行」标签可能切坏 → 有整图对照择优 + 异常回退兜底；
- 反光/红章管线若算法选错反而劣化 → 各自独立 `_wrinkle_ocr` 式专属实例，仅在该 kind 路径生效，不影响其它品类；
- 类别判定查表新增 DB 调用 → < 5ms，且失败即走原路。

**回滚**：
1. 环境变量 `DISABLE_LABEL_PREPROCESS=1` 模块加载时短路分派（强制返回 None）；
2. 或在 `ocr_preprocess_kind` 返回值硬编码 `None` 临时禁用；
3. 整段 `blueprints/ocr_engine.py` 该段可整体 revert。

## 度量标准

**上线后一周观察**（基线须先填，按退化类型分别统计）：

1. `form_nolines` 类：磅布三文治 record 的 green 占比从基线 **X%** 提升到 X+Y%（Y > 10%）；
2. `glare` 类：无纺布 green 占比提升（待管线实现后定基线）；
3. `redstamp` 类：杂胶/纯胶 green 占比提升（待管线实现后定基线）；
4. 行级图上传平均延迟增量 < 300ms（P50）；
5. 各预处理 numpy 计算抛异常次数 = 0。

> ⚠️ 基线 X% 上线前必填；且 B 类（提示词层颜色歧义，已确认不属本方案）不计收益。

## 待办

- [ ] 表单分行 OCR 已落地，补单测 + 集成测试（含「真不符不得变绿」断言）
- [ ] 把类别门控从 `is_wrinkle_label_category` 重命名为 `ocr_preprocess_kind`（覆盖 3 类）
- [ ] 无纺布反光抑制管线实现 + 真实反光图验证机制
- [ ] 杂胶/纯胶红章掩膜管线实现 + 真实红章图验证机制
- [ ] Fixture 抽取脚本运行（预期 磅布三文治 ~24 / 无纺布 ~5 / 杂胶 ~16 / 纯胶 ~2）
- [ ] 上线后第 7 天回顾，按退化类型分别填基线

## 备选方案对比（为何不选 / 调整）

| 备选 | 结论 |
|---|---|
| 加 OpenCV 依赖用 `cv2.createCLAHE` 做「褶皱增强」 | **原方案采纳的 CLAHE 打错地方**（主因是无表格线漏行，非对比度）；且当前仓库已引用 `cv2`（虽未 pin），反光/红章管线将直接使用 `cv2` |
| 多遍 OCR（原图 + 增强图 union） | 延迟 ~+2-3s，超预算；现方案用「分行 + 整图对照择优」替代，增量可控 |
| 动态启发式判断「是否褶皱图」 | YAGNI — 用户确认该类标签无表格线，直接按品类分派 |
| 仅调 PaddleOCR 参数不加预处理 | 实测降阈值对行合并无效，必须做版面重建（分行）或颜色分割 |

## 附录：原方案为何需重写（复盘）

1. **问题定位错误**：原方案假设「褶皱微阴影 → binarization 丢字」，实测为「无表格线 → 行合并/整行漏检」。CLAHE 治对比度，对此几乎无效（降阈值也无效）。
2. **范围只覆盖 1/3**：原方案只处理 磅布三文治，漏掉无纺布（反光）16+5 张、杂胶/纯胶（红章）占第二大桶的退化机制，且这两类需完全不同的算法（CLAHE 碰不到反光高光与红章颜色污染）。
3. **原实现是死代码**：旧 `apply_wrinkle_enhance` 路径对 ndarray 误调 `Image.open(io.BytesIO(...))`，每图抛错被吞、返回空串，CLAHE 从未真正运行过 —— 印证「只改文档不验实效」的风险。
