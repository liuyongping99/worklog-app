# 2026-08-16 OCR Label Profile Registry 设计

## 背景

磅布三文治类标签纸+褶皱,导致 PaddleOCR 漏字。前次实现(`docs/superpowers/specs/2026-08-16-ocr-wrinkle-label-preprocessing-design.md`)已用 `_wrinkle_ocr` 实例 + CLAHE 预处理,效果有改善,但**没有根据"纸质+褶皱"这一具体材料特性单独优化**。

实际生产中至少存在 3 种特性不同的标签:
- **纸质 + 褶皱** — 磅布三文治(微阴影,需 CLAHE + 激进检测阈值)
- **透明膜 + 反光 + 褶皱** — 无纺布(高光强反射 + 字符边界模糊,需高光去除 + 更激进的 `unclip_ratio`)
- **纸质 + 合格章** — 杂胶(红/蓝印章字符会被误识别为字,需 HSV 印章去除 + 反向收紧阈值)

另外 `is_wrinkle_label_category` + `_wrinkle_ocr` 这种"一个类目一个函数/属性"的结构,加第 4、第 5 个类目时需要改多处代码,扩展性差。用户明确说后续会继续补充类目。

## 目标

将 OCR 标签类别配置化 + 可扩展:
1. 每个类目(gating 规则、OCR 参数、预处理算法、阈值)用 JSON 配置,改参数不改代码
2. 加新类目 = 改 JSON,**核心代码不变**
3. 3 个生产类目(paper_wrinkle / film_reflective / stamp_dirty)立即可用,参数针对材料特性优化

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
  "paper_wrinkle": {
    "name": "paper_wrinkle",
    "description": "纸质 + 褶皱微阴影(磅布三文治类)",
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
    "description": "透明膜 + 反光高光 + 轻微褶皱(无纺布类)",
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
    "description": "纸质 + 合格章红/蓝印章(杂胶类)",
    "variants": ["杂胶", "7P环保杂胶", "黑杂胶", "白杂胶"],
    "ocr_kwargs": {
      "lang": "ch",
      "use_angle_cls": true,
      "show_log": false,
      "det_db_thresh": 0.25,
      "det_db_box_thresh": 0.45,
      "det_db_unclip_ratio": 1.4,
      "use_dilation": false
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
  "_order": ["paper_wrinkle", "film_reflective", "stamp_dirty"]
}
```

**参数取值依据**:

- **paper_wrinkle**:纸面反射差,褶皱产生微阴影→ **激进的低阈值**(0.10/0.25)+ **slow 打分**(更准确)+ **unclip_ratio=1.8**(扩展框捕获阴影边界外的字)
- **film_reflective**:反光高光破坏字符识别→ **更激进阈值**(0.10/0.20)+ **unclip_ratio=2.0**(反光区字符边界模糊,扩大检测框)
- **stamp_dirty**:**反向策略**(避免印章字符假阳)→ **提高阈值**(0.25/0.45)+ **`use_dilation=False`**(印章笔画不粘连)+ **HSV 印章去除预处理**

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
    preprocess_fn: Optional[Callable[[np.ndarray], np.ndarray]]
    postprocess_fn: Optional[Callable[[str], str]]

def get_label_profile(product_name: str) -> LabelProfile:
    """product_name → 首个匹配的 LabelProfile;都未命中 → _default。"""
```

**`blueprints/ocr_engine.py` 修改的公开符号**:

```python
class PaddleOCREngine:
    def __init__(self): ...  # _wrinkle_ocr 改为 _profile_ocrs: dict[str, PaddleOCR]

    def extract_text(self, image_bytes, label_profile: Optional[LabelProfile] = None) -> str: ...
    def extract_text_with_conf(self, image_bytes, label_profile: Optional[LabelProfile] = None) -> Tuple[str, float]: ...

    def _ensure_profile_ocr(self, profile: LabelProfile) -> PaddleOCR: ...  # 新增
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

- 加载 `config/ocr_profiles.json` 成功,3 个 profile 实例化
- `_load_profile_config()` 缓存(二次调返回同一对象)
- JSON 损坏/缺失 → fallback 到内置配置(`_BUILTIN_FALLBACK_CFG`),不抛异常
- `get_label_profile('白磅布三文治')` → `LabelProfile(name='paper_wrinkle', ...)`
- `get_label_profile('无纺布')` → `LabelProfile(name='film_reflective', ...)`
- `get_label_profile('7P环保杂胶')` → `LabelProfile(name='stamp_dirty', ...)`
- `get_label_profile('未知商品XYZ')` → `_default`
- dispatcher 按 `_order` 顺序遍历(`'磅布三文治无纺布' 这种 hybrid 命中第一个匹配的`)

### 2. paper_wrinkle 专项测试 `tests/test_ocr_profile_paper_wrinkle.py`

- gating 命中 5 个 variant + 5 个 negative
- ocr_kwargs 含 `det_db_score_mode='slow'`(关键)
- 合成褶皱图 → CLAHE 预处理 → 颜色方差改善

### 3. film_reflective 专项测试

- gating 命中「无纺布」,不命中「磅布三文治」
- 合成带高光的图 → `_film_reflection_preprocess` 后高光区域被 inpaint
- ocr_kwargs `unclip_ratio=2.0` 比 paper_wrinkle 大(反光区字符需更大框)

### 4. stamp_dirty 专项测试

- gating 命中「杂胶」「7P环保杂胶」等
- 合成带红印章的图 → `_stamp_removal_preprocess` 后红印章区域变白
- ocr_kwargs `use_dilation=False`(印章笔画不粘连)

### 5. Blueprint 路由测试(改 3 个既有文件)

- 上传行级图 + record 是「白磅布三文治」→ engine 收到 `label_profile=paper_wrinkle`
- 上传行级图 + record 是「无纺布」→ engine 收到 `label_profile=film_reflective`
- 上传行级图 + record 是「7P环保杂胶」→ engine 收到 `label_profile=stamp_dirty`
- 上传行级图 + record 是「未知」→ engine 收到 `label_profile=_default`

### 6. Engine API 单元测试(改 `test_paddleocr_wrinkle_param.py`)

- `label_profile=None` → 走 `self._ocr`
- `label_profile=<paper_wrinkle profile>` → 走 paper_wrinkle 对应 OCR 实例
- `label_profile=<film_reflective profile>` → 走 film_reflective 对应 OCR 实例

### 7. 集成测试 `tests/test_label_profile_integration.py`

- 真实 fixture 跑(可选,需要每类目 5+ 张 fixture)
- 验证 match_status 分布改善 + 延迟预算

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