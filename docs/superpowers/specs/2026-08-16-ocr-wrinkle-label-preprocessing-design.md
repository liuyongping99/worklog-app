# 2026-08-16 褶皱标签 OCR 预处理设计

## 背景

白磅布三文治(以及所有 磅布三文治 变体:7P环保磅布三文治、黑磅布三文治、B级 磅布三文治)的商品标签因材料特性存在明显褶皱不平整问题。光照下褶皱产生微阴影,导致 PaddleOCR 单一阈值 binarization 在做文字检测时丢字,继而 DeepSeek COMPARE_PROMPT 判别阶段因关键属性缺失被判 yellow / red。

近 30 天 `shipping_images.match_status IN ('yellow', 'red')` 记录中,品名包含「磅布三文治」的有 24 张(黑磅布三文治 19 + 白磅布三文治 5),占总 yellow/red 91 张的 26% — 这是单一品类最大的召回噪音来源。

## 目标

针对 磅布三文治 类商品标签,在 PaddleOCR 文字提取前做 CLAHE 局部对比度增强 + 调整检测参数,显著降低漏字率,使 AI 比对结果从 yellow/red 向 green 偏移。

## 非目标

- 不改造整单 OCR 识别路径(选 Moonshot / DeepSeek 引擎的场景)
- 不处理入库 / 装柜场景的非「磅布三文治」类漏字(本次未反映)
- 不调整自适应提示词 / Few-shot 注入逻辑(那条路对召回率的边际效益已低)
- 不引入新的第三方依赖(保持 requirements.txt 不动)

## 设计概览

**位置**:`blueprints/ocr_engine.py`

**核心思路**:在 `PaddleOCREngine._resize_if_needed()` 之后新增一段「褶皱增强」可选路径,由调用方通过 `apply_wrinkle_enhance: bool` 参数开启。开关仅对 磅布三文治 品类打开。

**数据流**:

```
行级图片上传 / AI 判别按钮
   │
   ▼
[shipping.py / inbound.py / loading.py 任一行级图接口]
   │
   ▼
ShippingRecord.get_by_id(record_pk)   ← 取 product_name
   │
   ▼
ocr_engine.is_wrinkle_label_category(product_name)
   │
   ▼
apply_wrinkle_enhance = True / False
   │
   ▼
PaddleOCREngine.extract_text_with_conf(bytes, apply_wrinkle_enhance=...)
   │
   ▼
_resize_if_needed(bytes)
   │
   ├─ apply_wrinkle_enhance=True → _enhance_wrinkle_label(numpy 灰度图) → CLAHE → RGB
   │
   └─ apply_wrinkle_enhance=False → 原图直接进 PaddleOCR
   │
   ▼
self._ocr.ocr(numpy_array, cls=True)
```

## 关键组件

### 1. CLAHE 实现(numpy 手写)

**位置**:`PaddleOCREngine._enhance_wrinkle_label()` 私有方法

**算法步骤**:
1. 灰度化:`L = 0.299 * R + 0.587 * G + 0.114 * B`
2. 8×8 网格切分
3. 每 tile 算 256-bin 直方图
4. 直方图裁剪:`clip_limit = (像素数 / tile 像素数 / 256) * 2.0`
5. 裁剪后像素均匀重分配到所有 bin
6. 计算 CDF
7. 双线性插值 tile 边界
8. 应用映射到灰度
9. 按原图 R/G/B 通道按灰度缩放比例同步增强,保留色调

**常量**:
```python
_CLAHE_TILE_SIZE = 8
_CLAHE_CLIP_LIMIT = 2.0
_CLAHE_BINS = 256
```

**依赖**:仅 `numpy` + `Pillow`(两者已在 `_resize_if_needed` 中使用)

**性能**:2000×2000 RGB 图在 numpy 中实测 ~30-80ms(本机 CPU)

### 2. PaddleOCR 调参(褶皱增强专用)

新增独立模型实例 `_wrinkle_ocr`,与现有 `_ocr` 并存:

```python
self._wrinkle_ocr = PaddleOCR(
    lang='ch',
    use_angle_cls=True,
    show_log=False,
    det_db_thresh=0.15,        # 现有 0.2 → 更低(CLAHE 后细微笔画更易检出)
    det_db_box_thresh=0.30,    # 现有 0.4 → 更低
    use_dilation=True,
)
```

⚠️ 参数为推测性初值,实际值需回归验证后微调。设计文档锁定初值,实施时如发现效果不好,可直接调常量不需改设计。

### 3. 类别门控:`is_wrinkle_label_category(product_name) -> bool`

**位置**:`blueprints/ocr_engine.py` 模块级公开函数

**判定逻辑(双轨)**:
1. **子串轨**(主要,覆盖当前数据):
   - 已知 磅布三文治 变体品名集合 = `{'白磅布三文治', '黑磅布三文治', 'B级 磅布三文治', '7P环保磅布三文治', '磅布三文治'}`
   - `product_name in 集合` 或 `集合中任一 ∈ product_name` → True
2. **品类轨**(辅助,覆盖未来数据):
   - 调 `DeepSeekEngine._classify_product(product_name)` → category_code
   - code ∈ `{'0212', '021003', '021201', '021202', '021203'}` → True
3. **任一轨命中 → True**,否则 False

**防御**:
- 任一轨异常(DB / 分类失败)→ 返回 False,走原路径,不阻塞
- `product_name` 为空 / None → False

**已知 5 个 磅布三文治 category_code**(已查 DB 确认):

| category_code | category_name | level |
|---|---|---|
| 0212 | 磅布三文治 | 3 |
| 021003 | 7P环保磅布三文治 | 4 |
| 021201 | 白磅布三文治 | 4 |
| 021202 | 黑磅布三文治 | 4 |
| 021203 | B级 磅布三文治 | 4 |

### 4. 调用方集成

**接入点**(3 蓝图):

| 蓝图 | 端点 | 行为 |
|---|---|---|
| `shipping.py` | 行级图片上传(行 record 关联) | 上传时根据 record.product_name 决定是否走褶皱路径 |
| `shipping.py` | AI 判别按钮(`/ai-judge`) | 同上 |
| `inbound.py` | 行级图片上传 | 同上(共用同一 record 商品名机制) |
| `loading.py` | 行级图片上传 | 同上 |

**集成方式**:每个端点从 record 取得 `product_name`,调 `is_wrinkle_label_category(product_name)`,把结果作为 `apply_wrinkle_enhance` 参数透传给 `extract_text_with_conf()`。

**向后兼容**:`extract_text_with_conf(bytes)` 默认 `apply_wrinkle_enhance=False`,老调用方零感知。

## 错误处理

| 失败点 | 处理 | 影响 |
|---|---|---|
| `is_wrinkle_label_category` 内部 DB 异常 | 返回 False,主流程走原路 | 无 |
| `product_name` 为空 | 返回 False | 无 |
| CLAHE numpy 计算异常 | try/except 包住,return 原图,日志 WARNING | 无(仅该张图走原路) |
| PaddleOCR 模型加载失败 | 与现有行为一致,fallback Moonshot | 已有 |
| `_wrinkle_ocr` 懒加载失败 | 与 `_ensure_model` 同样 catch + 日志 + 用回 `_ocr` | 无 |

CLAHE 不抛异常上抛 — 这是设计硬约束。

## 测试策略

### 1. 单元测试:`tests/test_clahe_preprocessing.py`(新)

- **合成图测试**:生成「白底 + 黑字 + 模拟褶皱」(加入正弦波亮度变化),验证输出图对比度方差显著大于输入图
- **异常输入**:空 bytes、非图像 bytes、超大 numpy 数组、超小 numpy 数组 → 验证不抛异常、走原图
- **单色图**:全白 / 全黑图 → CLAHE 不破坏(直方图均衡化对单色应等价或微变)
- **彩色保留**:输入 RGB 图,输出仍是 RGB,色调不被严重偏移

### 2. 回归测试:`tests/test_wrinkle_label_category.py`(新)

- `_is_wrinkle_label_category('白磅布三文治')` → True
- `_is_wrinkle_label_category('黑磅布三文治')` → True
- `_is_wrinkle_label_category('7P环保磅布三文治')` → True
- `_is_wrinkle_label_category('无纺布')` → False
- `_is_wrinkle_label_category('')` → False
- `_is_wrinkle_label_category(None)` → False
- Mock `_classify_product` 抛异常 → 返回 False,主流程不挂

### 3. 集成测试:`tests/test_compare_rows_wrinkle.py`(新)

- 准备 5 张真实褶皱标签 fixture(从 `shipping_images.match_status IN ('yellow', 'red')` 且 record 品名含「磅布三文治」中筛,复制到 `tests/fixtures/wrinkle_labels/`)
- 跑 `compare_rows` 两次(`apply_wrinkle_enhance=False` vs `True`),断言:
  - 至少 3/5 的 match_status 从 yellow/red 改善到 green
  - True 路径的 total latency 增量 < 300ms(95% 分位)

### 4. Fixture 抽取脚本:`tools/extract_wrinkle_fixtures.py`(新)

- **一次性脚本**,只读 + cp,**不删任何 db 数据**
- 从 `shipping_images JOIN shipping_records` 筛近 30 天 yellow/red + 品名含「磅布三文治」
- 复制到 `tests/fixtures/wrinkle_labels/`,每张图配一个 `.json` 元数据
- 输出抽取报告(命中数、跳过数、文件大小)

**安全声明**:脚本只 SELECT + shutil.copy,**不执行 UPDATE/DELETE**。代码 review 重点验证这点。

## 实施分块

| 块 | 内容 | 估时 | 风险 |
|---|---|---|---|
| 1 | CLAHE numpy 实现 + 单测 | 1h | 低(纯 numpy,可独立验证) |
| 2 | `is_wrinkle_label_category` + 回归测试 | 30min | 低 |
| 3 | PaddleOCREngine `apply_wrinkle_enhance` 参数 + 调参 | 30min | 低 |
| 4 | 三蓝图调用方接入 | 1h | 中(需 review 三个文件) |
| 5 | Fixture 抽取脚本 | 20min | 低 |
| 6 | 集成测试 + 回归验证 | 1h | 中(参数可能需微调) |

总估时:约 4 小时。

## 风险与回滚

**风险**:
- CLAHE 在干净图上可能引入少量噪声(噪点被放大) — **不影响**,因为路径只对 磅布三文治 触发
- PaddleOCR 调参可能让其它品类的褶皱标签问题恶化 — **不影响**,因为专属模型实例 `_wrinkle_ocr` 只在 `apply_wrinkle_enhance=True` 路径使用
- 类别判定新增 DB 调用 — 每次行级图片上传多一次 `product` 表查询,影响 < 5ms

**回滚**:
1. 设环境变量 `DISABLE_WRINKLE_ENHANCE=1`,模块加载时短路类别门控(强制返回 False)
2. 或在 `is_wrinkle_label_category` 返回值加 hardcoded `False` 临时禁用
3. 代码可整体 revert `blueprints/ocr_engine.py` 该段

## 度量标准

**上线后一周观察**:
1. `shipping_images.match_status='green'` 在 磅布三文治 record 上的占比从基线 X% 提升到 X+Y%(Y > 10%)
2. `match_status='red'` 在 磅布三文治 record 上绝对数下降
3. 行级图上传平均延迟增量 < 200ms(P50)
4. CLAHE numpy 计算抛异常次数 = 0

## 待办

- [ ] CLAHE 实测首轮参数(`clip_limit=2.0`)效果是否足够,不够再调
- [ ] Fixture 抽取脚本运行结果(预期 ~24 张,过滤后 ~10 张可作为高质量 fixture)
- [ ] 上线后第 7 天回顾

## 备选方案对比(为何不选)

| 备选 | 否决理由 |
|---|---|
| 加 OpenCV 依赖用 `cv2.createCLAHE` | 增加 ~80MB 依赖,只为单一功能 |
| 加 scikit-image 依赖 | 同上,体积更大 |
| 多遍 OCR(原图 + CLAHE 后图,union) | 延迟 ~+2-3s,超出 300ms 预算 |
| 动态启发式判断"是否褶皱图" | YAGNI — 用户已确认所有 磅布三文治 都有褶皱 |
| 仅调 PaddleOCR 参数不加预处理 | 不能解决光照不均,只能稍缓解 |