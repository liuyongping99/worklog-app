# 2026-08-16 OCR Label Profile Registry 设计

## 背景

磅布三文治类标签 OCR 漏字。前次实现(`docs/superpowers/specs/2026-08-16-ocr-wrinkle-label-preprocessing-design.md`)聚焦于"褶皱"假设,但实测发现:

**真正主因(2026-08-16 实测确认)**:标签内**多行属性无表格线**(品名/厚度/手感/底色 4 行紧贴无分隔),PaddleOCR 默认检测将多行合并成一个文本块,导致部分行(如"加硬")的字符**被吞掉**。"褶皱"反而不是关键问题。

材质特征(纸/膜/印章)是次要因素,之前针对不同材质做的 OCR 参数微调效果有限。本次实测 paper_wrinkle vs DEFAULT 对真实 yellow case 输出几乎相同("硬性"/"加硬"都识别不出来)。

新增 profile 类型:
- **多行无表格线**(主) — `multi_row_table_less`,水平投影找行边界 → 切片 OCR
- 材质特征(次) — `paper_wrinkle` / `film_reflective` / `stamp_dirty`,保留作为场景专用

## 目标

1. **新 profile 类型 `multi_row_table_less`**:水平投影找行边界 + 切片 OCR,解决"无表格线多行 label 字符被吞"主问题
2. 材质 profile 保留作为**次要补充**(生产中有特定场景仍可能受益)
3. Registry 模式:JSON 配置文件驱动,加新类目不改代码
4. Engine 支持 preprocess 返回**多个子图**(新),按行逐个 OCR 后拼接

## 非目标

- 不引入 Pillow / numpy / rapidfuzz / Flask / openai 之外的新依赖(允许 `opencv-python-headless`)
- 不做 hot-reload(JSON 改完重启应用生效,YAGNI)
- 不动 COMPARE_PROMPT / DeepSeek 比对逻辑(本次只优化 PaddleOCR 这一段)
- 不改 Moonshot 引擎(只针对本地 CPU OCR)
- 不解决"印章盖住字根本识别不出"的根本问题(只减少印章字符的假阳)

## 设计概览

**架构**:`blueprints/ocr_engine.py` 内定义 `LabelProfile` dataclass + `LABEL_PROFILES` 字典 + dispatcher 函数。配置集中在 `config/ocr_profiles.json`。3 个生产类目 + 1 个通用兜底。Engine API 改名 `apply_wrinkle_enhance: bool` → `label_profile: Optional[LabelProfile]`(直接重命名,不保留别名)。

**核心组件**:
- `LabelProfile` dataclass(gating_fn / ocr_kwargs / preprocess_fn / postprocess_fn)
- `_load_profile_config()` 懒加载 JSON + 内置 fallback
- `get_label_profile(product_name)` 路由函数(首个命中返回)
- `_make_preprocess_fn(cfg)` 工厂函数(type → 函数映射)
- `_PaddleOCREngine` 改造:支持 `label_profile` 参数,懒加载各 profile 的 OCR 实例

## 数据流

```
shipping.py / inbound.py / loading.py 行级图接口
    │
    ▼
record = ShippingRecord.get_by_id(record_pk)
    │
    ▼
profile = get_label_profile(record.get('product_name', ''))
    │   ├─ 加载 config/ocr_profiles.json(首次调时)
    │   ├─ 按 _order 遍历:paper_wrinkle → film_reflective → stamp_dirty
    │   └─ 首个 gating_fn 命中返回 LabelProfile;都未命中 → _default
    ▼
text, conf = engine.extract_text_with_conf(bytes, label_profile=profile)
    │
    ▼
_PaddleOCREngine._resize_if_needed(bytes) → numpy
    │
    ├─ profile is None → ocr = self._ocr(通用)
    │
    └─ profile is not None
        │
        ├─ profile.preprocess_fn(resized) → enhanced numpy(若有)
        │
        └─ ocr = self._ensure_profile_ocr(profile)
            │   ├─ profile.ocr_kwargs 构造 PaddleOCR(首次调时)
            │   └─ 失败 → logger.warning + 回退 self._ocr
            │
            ▼
        result = ocr.ocr(resized, cls=True)
            │
            ▼
        text + avg_conf 返回
```

## 必须产出的文件

**新建**:
- `config/ocr_profiles.json` — 3 个 profile + `_default` + `_order` 全部配置
- `tests/test_ocr_profile_registry.py` — registry 测试(JSON 加载、dispatcher 路由、内置 fallback)
- `tests/test_ocr_profile_paper_wrinkle.py` — paper_wrinkle gating + OCR kwargs + CLAHE 预处理
- `tests/test_ocr_profile_film_reflective.py` — film_reflective gating + 高光去除预处理
- `tests/test_ocr_profile_stamp_dirty.py` — stamp_dirty gating + HSV 印章去除预处理

**修改**:
- `blueprints/ocr_engine.py`:
  - 新增 `LabelProfile` dataclass
  - 新增 `_load_profile_config()` + 内置 fallback
  - 新增 `_make_substring_gating_fn()` + `_make_preprocess_fn()` 工厂
  - 新增 `_film_reflection_preprocess()` + `_stamp_removal_preprocess()` 函数
  - 改造 `extract_text()` / `extract_text_with_conf()` 签名:加 `label_profile: Optional[LabelProfile] = None` 参数
  - 改造 `_PaddleOCREngine.__init__`:用 `_profile_ocrs: dict[str, PaddleOCR]` 替代 `_wrinkle_ocr`
  - 新增 `_ensure_profile_ocr(profile)` 方法
  - 删除 `_wrinkle_ocr` 属性(直接重命名,语义已不准)
  - 删除 `is_wrinkle_label_category()` 公开函数(替换为 `get_label_profile`)
  - 保留 `_WRINKLE_VARIANTS` / `_WRINKLE_CATEGORY_CODES` 作为 paper_wrinkle profile 的内部数据(通过 JSON 注入,但 profile 构建时读 JSON)
- `blueprints/shipping.py`:2 处(行级上传异步处理 line ~100 + ai-judge fallback line ~1037)
- `blueprints/inbound.py`:2 处(行级上传 line ~814 + ai-judge fallback line ~1036)
- `blueprints/loading.py`:1 处(ai-judge fallback line ~844)
- `tests/test_shipping_wrinkle_routing.py` / `test_inbound_wrinkle_routing.py` / `test_loading_wrinkle_routing.py` — 改用新 API
- `tests/test_paddleocr_wrinkle_param.py` — 改 `apply_wrinkle_enhance=True` → `label_profile=<paper_wrinkle>`
- `tests/test_wrinkle_label_category.py` — 删除(被 `test_ocr_profile_registry.py` 替代)或改为测试 dispatcher

**新增依赖**:`opencv-python-headless`(stamp_removal 需要 HSV 转换 + mask)。无其它新增。

## 全局约束(从 plan 沿用)

- Python 3.12
- 不引入 Pillow / numpy / rapidfuzz / Flask / openai 之外的新依赖(允许 `opencv-python-headless`)
- 命名:`_xxx` 私有,`xxx` 公开,`_UPPER_SNAKE` 常量
- 测试栈:pytest + unittest.TestCase + conftest.py client fixture
- 性能预算:PaddleOCR 单次 + 预处理增量 < 300ms (P95) — 三个 profile 都要满足
- db 安全:scripts 只读 + cp,禁止 UPDATE/DELETE

## 配置: `config/ocr_profiles.json`

```json
{
  "_comment": "所有 OCR profile 的可调参数。改这里调优,无需改代码。重启应用生效。",
  "multi_row_table_less": {
    "name": "multi_row_table_less",
    "description": "无表格线多行 label(主 profile)— 水平投影找行边界切片 OCR",
    "variants": [],
    "ocr_kwargs": {
      "lang": "ch",
      "use_angle_cls": true,
      "show_log": false,
      "det_db_thresh": 0.10,
      "det_db_box_thresh": 0.25,
      "unclip_ratio": 1.8,
      "use_dilation": true
    },
    "preprocess": {
      "type": "row_split",
      "row_gap_threshold_ratio": 0.3,
      "min_gap_height": 8
    },
    "row_split_priority": "primary"
  },
  "paper_wrinkle": {
    "name": "paper_wrinkle",
    "description": "纸质 + 褶皱微阴影(磅布三文治类,次要 — 多行无表线是主因)",
    "variants": ["白磅布三文治", "黑磅布三文治", "B级 磅布三文治", "7P环保磅布三文治", "磅布三文治"],
    "ocr_kwargs": {
      "lang": "ch",
      "use_angle_cls": true,
      "show_log": false,
      "det_db_thresh": 0.10,
      "det_db_box_thresh": 0.25,
      "det_db_unclip_ratio": 1.8,
      "use_dilation": true,
      "det_db_score_mode": "slow"
    },
    "preprocess": { "type": "clahe", "tile_size": 8, "clip_limit": 2.0 }
  },
  "film_reflective": {
    "name": "film_reflective",
    "description": "透明膜 + 反光高光 + 轻微褶皱(无纺布类,次要)",
    "variants": ["无纺布"],
    "ocr_kwargs": {
      "lang": "ch",
      "use_angle_cls": true,
      "show_log": false,
      "det_db_thresh": 0.10,
      "det_db_box_thresh": 0.20,
      "det_db_unclip_ratio": 2.0,
      "use_dilation": true
    },
    "preprocess": {
      "type": "film_reflection",
      "highlight_threshold": 230,
      "highlight_neighborhood_saturation": 0.5
    }
  },
  "stamp_dirty": {
    "name": "stamp_dirty",
    "description": "纸质 + 合格章/手写改写厚度(杂胶 + 纯胶类,次要)",
    "variants": ["杂胶", "7P环保杂胶", "黑杂胶", "白杂胶", "纯胶", "黑纯胶", "白纯胶"],
    "ocr_kwargs": {
      "lang": "ch",
      "use_angle_cls": true,
      "show_log": false,
      "det_db_thresh": 0.20,
      "det_db_box_thresh": 0.40,
      "det_db_unclip_ratio": 1.6,
      "use_dilation": true,
      "det_db_score_mode": "slow"
    },
    "preprocess": {
      "type": "stamp_removal",
      "red_hue_ranges": [[0, 10], [170, 180]],
      "blue_hue_ranges": [[100, 130]],
      "saturation_min": 50,
      "replace_color": [255, 255, 255]
    }
  },
  "_default": {
    "name": "_default",
    "description": "通用兜底(无匹配 profile 时)",
    "variants": [],
    "ocr_kwargs": {
      "lang": "ch",
      "use_angle_cls": true,
      "show_log": false,
      "det_db_thresh": 0.2,
      "det_db_box_thresh": 0.4,
      "use_dilation": true
    },
    "preprocess": null
  },
  "_order": ["multi_row_table_less", "paper_wrinkle", "film_reflective", "stamp_dirty"]
}
```

**参数取值依据**:

- **multi_row_table_less(主)**:无表格线多行 → 水平投影找行边界切片 → 逐行 OCR。`row_gap_threshold_ratio=0.3` (文字密度低于均值 30% 视为行间隙)+ `min_gap_height=8` 像素。OCR 参数用 paper_wrinkle 的激进低阈值(0.10/0.25)+ unclip_ratio=1.8(每行单独 OCR 时捕获更多字符)
- **paper_wrinkle(次)**:纸面反射差,褶皱产生微阴影→ 激进低阈值(0.10/0.25)+ slow 打分 + unclip_ratio=1.8。**实测对无表格线多行问题改善有限** — paper_wrinkle 只是字符级参数,救不了行级布局问题
- **film_reflective(次)**:反光高光破坏字符识别→ 更激进阈值(0.10/0.20)+ unclip_ratio=2.0
- **stamp_dirty(次)**:需同时应对印章字符假阳 + 手写粗细不均 → 折中阈值(0.20/0.40)+ use_dilation=True + slow 打分 + unclip_ratio=1.6 + HSV 印章去除

## 必须产出的接口

**`blueprints/ocr_engine.py` 新增公开符号**:

```python
@dataclass(frozen=True)
class LabelProfile:
    """标签类别配置:gating + OCR kwargs + 预处理函数。"""
    name: str
    description: str
    gating_fn: Callable[[str], bool]
    ocr_kwargs: dict
    # preprocess_fn 可返回 单个 np.ndarray 或 list[np.ndarray]
    # 单图 → 走标准 OCR
    # 多图(行切片)→ 逐图 OCR 后拼接
    preprocess_fn: Optional[Callable[[np.ndarray], Union[np.ndarray, list[np.ndarray]]]]
    postprocess_fn: Optional[Callable[[str], str]]

def get_label_profile(product_name: str) -> LabelProfile:
    """product_name → 首个匹配的 LabelProfile;都未命中 → _default。"""
```

**新增预处理函数**:`_row_split_preprocess(img, cfg) → list[np.ndarray]`
- 灰度 → 二值化(阈值180)
- 沿 Y 轴求文字密度(每行文字像素和)
- 找局部密度低谷(密度 < 均值 × `row_gap_threshold_ratio` 且连续 ≥ `min_gap_height` 像素)
- 在低谷处切片 → 返回 list[np.ndarray]
- 若只找到 1 个低谷 → 返回 [原图](避免无意义切分)

**`blueprints/ocr_engine.py` 修改的公开符号**:

```python
class PaddleOCREngine:
    def __init__(self): ...  # _wrinkle_ocr 改为 _profile_ocrs: dict[str, PaddleOCR]

    def extract_text(self, image_bytes, label_profile: Optional[LabelProfile] = None) -> str:
        # 改造:preprocess_fn 可能返回 list[numpy],逐个 OCR 后拼接
        ...
    def extract_text_with_conf(self, image_bytes, label_profile: Optional[LabelProfile] = None) -> Tuple[str, float]:
        # 同上
        ...

    def _ensure_profile_ocr(self, profile: LabelProfile) -> PaddleOCR: ...  # 新增

def _run_ocr_on_image(self, ocr: PaddleOCR, img_np: np.ndarray) -> str:
    """对单张图跑 OCR,返回拼接文本。extract_text / extract_text_with_conf 共用。"""
    ...
```

**删除的公开符号**(直接删除,不保留别名):
- `_PaddleOCREngine._wrinkle_ocr` 属性
- `is_wrinkle_label_category()` 函数

## Engine API 迁移

**Before**:
```python
apply_wrinkle_enhance = is_wrinkle_label_category(record.get('product_name', ''))
text, conf = engine.extract_text_with_conf(bytes, apply_wrinkle_enhance=apply_wrinkle_enhance)
```

**After**:
```python
profile = get_label_profile(record.get('product_name', ''))
text, conf = engine.extract_text_with_conf(bytes, label_profile=profile)
```

**5 处修改**:`shipping.py` 2 处 + `inbound.py` 2 处 + `loading.py` 1 处 + 既有 wrinkle routing tests 同步改

## 测试策略

### 1. Registry 单元测试 `tests/test_ocr_profile_registry.py`

- 加载 `config/ocr_profiles.json` 成功,4 个 profile 实例化(含 multi_row_table_less)
- `_load_profile_config()` 缓存(二次调返回同一对象)
- JSON 损坏/缺失 → fallback 到内置配置(`_BUILTIN_FALLBACK_CFG`),不抛异常
- dispatcher 按 `_order` 顺序遍历:`multi_row_table_less` 第一(优先级最高)

### 2. multi_row_table_less 专项测试 `tests/test_ocr_profile_multi_row.py`(新,主测试)

- **核心**:真实无表格线多行 fixture → 验证能识别所有行(包括之前被吞的"加硬"/"硬性"行)
- 合成多行图(无表格线) → `_row_split_preprocess` 找到 ≥ 2 个低谷 → 返回 list[np.ndarray]
- 单行图 → `_row_split_preprocess` 返回 [原图]
- Engine 处理 list 返回:逐图 OCR → 拼接结果

### 3. paper_wrinkle 专项测试 `tests/test_ocr_profile_paper_wrinkle.py`

- gating 命中 5 个 variant + 5 个 negative
- ocr_kwargs 含 `det_db_score_mode='slow'`(关键)
- 合成褶皱图 → CLAHE 预处理 → 颜色方差改善
- **注**:多行无表线问题不在本 profile 范围(被 multi_row_table_less 处理)

### 4. film_reflective 专项测试

- gating 命中「无纺布」,不命中「磅布三文治」
- 合成带高光的图 → `_film_reflection_preprocess` 后高光区域被 inpaint

### 5. stamp_dirty 专项测试

- gating 命中「杂胶」「7P环保杂胶」「纯胶」「黑纯胶」「白纯胶」
- 合成带红印章的图 → `_stamp_removal_preprocess` 后红印章区域变白
- 合成手写笔迹(粗细不均)→ ocr_kwargs `use_dilation=True` + `score_mode='slow'` 验证
- ocr_kwargs 折中值:`det_db_thresh=0.20 / box_thresh=0.40`(印章不假阳 + 手写不漏)

### 6. Blueprint 路由测试(改 3 个既有文件)

- 上传行级图 + record 是「白磅布三文治」→ engine 收到 `label_profile` 命中第一个匹配的 profile
- 上传行级图 + record 是「无纺布」→ 命中顺序取决于 _order
- 上传行级图 + record 是「未知」→ engine 收到 `label_profile=_default`

### 7. Engine API 单元测试(改 `test_paddleocr_wrinkle_param.py`)

- `label_profile=None` → 走 `self._ocr`
- `label_profile=<paper_wrinkle profile>` → 走 paper_wrinkle 对应 OCR 实例
- `label_profile=<multi_row_table_less profile>` → preprocess 返回 list,逐图 OCR 后拼接

### 8. 集成测试 `tests/test_label_profile_integration.py`

- **核心**:真实无表格线多行 fixture(5+ 张)→ 验证 match_status 改善 + 字符完整
- TD-2026-08-16-004 中 id=2994 这张图(原本 yellow "加硬"漏字)→ multi_row_table_less profile 应能让 yellow → green
- 延迟预算:< 500ms (multi_row 跑两遍 OCR,比 single 行级稍慢)

## 回滚方案

3 档降级,任一档出问题都可回退:

1. **JSON 配置错误**:JSON 加载失败 → 内置 `_BUILTIN_FALLBACK_CFG`(最小可用配置,启动不阻塞)
2. **profile OCR 加载失败**:`_ensure_profile_ocr()` 抛异常 → logger.warning + 返回 `self._ocr`(通用实例)
3. **整个 profile 不可用**:JSON 里把某 profile 删掉 → dispatcher 跳过 → 命中下一个或 `_default`

**最坏情况**:JSON 删光 + 内置 fallback 都没 → `get_label_profile()` 异常 → 3 蓝图加 `try/except` 返回 None → engine 当 None 处理走 `_ocr`

## 性能预算

- 3 个 OCR 实例懒加载,启动时只加载 `_default`(通用)
- 每个 profile 第一次触发时加载 ~200MB,后续复用缓存
- profile 切换延迟:`_ensure_profile_ocr()` 首次 2-3s(模型加载),后续 < 50ms(查缓存)
- 单次 OCR + 预处理增量 < 300ms(P95)对所有 profile 成立(CLAHE 现有 700ms cold,但 warm 后 < 100ms;film_reflection / stamp_removal 增量 < 200ms)

## 待办

- [ ] 抽每类目 5 张真实 yellow/red fixture(从生产 yellow/red 30天筛选,扩到 3 个类目)
- [ ] 用 fixture 调各 profile 的 OCR kwargs 初值(`det_db_thresh` / `box_thresh` / `unclip_ratio` 等是猜测,实测后微调)
- [ ] 上线一周后回顾:green 占比变化、各 profile 命中率、CLAHE 失败率

## 备选方案对比(为何不选)

| 备选 | 否决理由 |
|---|---|
| Python dict config(`ocr_profiles_config.py`) | 用户明确要 JSON(配置化) |
| hot-reload(mtime 检测) | 用户明确不要(YAGNI) |
| 保留 `apply_wrinkle_enhance` 别名 | 语义已不准 + 3 调用方都在仓库内,直接改名更干净 |
| OpenCV 内置 `cv2.createCLAHE` 替代 numpy 手写 | numpy 实现已稳定,8x8 grid 已 25x 提速(cv2 替代会让依赖更重,不值) |
| 把 `preprocess_fn` 也配置化(JSON 写函数) | 函数不可序列化,type 字段做映射是合理的边界 |
| 单 profile(只 paper_wrinkle)改参数 | 不解决无纺布 / 杂胶的漏字问题 |

## 与已有 plan/spec 的关系

- 前次 plan `2026-08-16-ocr-wrinkle-label-preprocessing.md` 的 CLAHE 实现保留,变成 paper_wrinkle profile 的 preprocess
- 前次 spec 的 `_wrinkle_ocr` 参数 + CLAHE 实现 + 双轨门控 → 被 registry 替代
- 前次的 26 + 1 tests 多数保留(测试 paper_wrinkle 的子集需要迁移到新 API)
- `_wrinkle_label_category.py` 测试 → 删除或重写为 `test_ocr_profile_registry.py`
- 旧 fixture `tests/fixtures/wrinkle_labels/` 仍可用(都是 paper_wrinkle 样本),新增 film/stamp fixture