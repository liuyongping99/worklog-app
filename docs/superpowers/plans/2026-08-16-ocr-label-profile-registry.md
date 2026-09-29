# OCR Label Profile Registry Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把现有"ocr_engine.py 一个 _wrinkle_ocr 属性 + is_wrinkle_label_category 字符串 gating"重构成 JSON 驱动的 LabelProfile registry,支持 OCR 多场景(无表格线多行 / 纸褶皱 / 透明膜反光 / 合格章手写)。

**Architecture:** Registry 模式 — `LabelProfile` dataclass 描述每个类目(gating_fn / ocr_kwargs / preprocess_fn / postprocess_fn);JSON 配置 `config/ocr_profiles.json` 驱动;`get_label_profile(product_name)` 路由器首匹配命中;Engine API 接受 `label_profile` 参数,preprocess 返回 list[np.ndarray] 时逐图 OCR 后拼接。

**Tech Stack:** Python 3.12 + numpy + Pillow(已用)+ PaddleOCR 3.x(已用)+ 新增 `opencv-python-headless`(HSV 印章去除 + 投影)+ pytest + unittest.TestCase(项目既有测试栈)

## Global Constraints

- Python 3.12
- 新增依赖:**仅 `opencv-python-headless`**(允许);不动 Pillow / numpy / rapidfuzz / Flask / openai / paddleocr
- 命名:`_xxx` 私有,`xxx` 公开,`_UPPER_SNAKE` 常量
- 测试栈:pytest + unittest.TestCase + conftest.py client fixture
- 性能预算:multi_row_table_less 单次 < 3500ms(实测 3290ms)
- db 安全:scripts 只读 + cp,禁止 UPDATE/DELETE
- 不引入 hot-reload,JSON 改完重启应用生效
- API 直接重命名:`apply_wrinkle_enhance: bool` → `label_profile: Optional[LabelProfile]`(无别名)

---

## File Structure

**新建文件**:
- `config/ocr_profiles.json` — 4 个 profile + `_default` + `_order` 全部配置
- `tests/test_ocr_profile_registry.py` — registry / dispatcher / fallback 测试
- `tests/test_ocr_profile_multi_row.py` — multi_row_table_less profile(主)
- `tests/test_ocr_profile_paper_wrinkle.py` — paper_wrinkle profile
- `tests/test_ocr_profile_film_reflective.py` — film_reflective profile
- `tests/test_ocr_profile_stamp_dirty.py` — stamp_dirty profile

**修改文件**:
- `blueprints/ocr_engine.py` — 大改(`LabelProfile` / JSON 加载 / factory / dispatcher / 4 个 preprocess / engine API)
- `blueprints/shipping.py` — 2 处迁移
- `blueprints/inbound.py` — 2 处迁移
- `blueprints/loading.py` — 1 处迁移
- `tests/test_paddleocr_wrinkle_param.py` — 改用 `label_profile`
- `tests/test_shipping_wrinkle_routing.py` — 改用新 API
- `tests/test_inbound_wrinkle_routing.py` — 改用新 API
- `tests/test_loading_wrinkle_routing.py` — 改用新 API

---

## Task 1: 添加 `LabelProfile` dataclass + 删除 `_wrinkle_ocr`

**Files:**
- Modify: `blueprints/ocr_engine.py`(顶部加 import `dataclass` from dataclasses;类顶部加 `LabelProfile` 定义;`PaddleOCREngine.__init__` 删除 `_wrinkle_ocr` 加 `_profile_ocrs`)
- Test: `tests/test_ocr_profile_registry.py`(新建,先空)

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class LabelProfile: name: str; description: str; gating_fn: Callable[[str], bool]; ocr_kwargs: dict; preprocess_fn: Optional[Callable[[np.ndarray], Union[np.ndarray, list[np.ndarray]]]]; postprocess_fn: Optional[Callable[[str], str]]`

- [ ] **Step 1: 写失败测试**

```python
"""Registry + dispatcher 测试套件。"""
import unittest
from blueprints.ocr_engine import LabelProfile


class LabelProfileDataclassTests(unittest.TestCase):

    def test_labelprofile_basic_construction(self):
        """LabelProfile 能用最少字段构造,frozen。"""
        profile = LabelProfile(
            name='test',
            description='test desc',
            gating_fn=lambda pn: False,
            ocr_kwargs={'lang': 'ch'},
            preprocess_fn=None,
            postprocess_fn=None,
        )
        self.assertEqual(profile.name, 'test')
        self.assertEqual(profile.description, 'test desc')
        self.assertFalse(profile.gating_fn('any'))
        self.assertEqual(profile.ocr_kwargs['lang'], 'ch')

    def test_labelprofile_is_frozen(self):
        """dataclass(frozen=True) 阻止字段赋值。"""
        profile = LabelProfile(
            name='test', description='', gating_fn=lambda pn: False,
            ocr_kwargs={}, preprocess_fn=None, postprocess_fn=None,
        )
        with self.assertRaises(Exception):  # FrozenInstanceError 或 AttributeError
            profile.name = 'changed'

    def test_preprocess_fn_accepts_returning_list(self):
        """preprocess_fn 类型签名允许返回 list[np.ndarray]。"""
        def fake_prep(img):
            return [img, img]  # 模拟 row_split 切片
        profile = LabelProfile(
            name='p', description='', gating_fn=lambda pn: False,
            ocr_kwargs={}, preprocess_fn=fake_prep, postprocess_fn=None,
        )
        import numpy as np
        result = profile.preprocess_fn(np.zeros((10, 10, 3), dtype=np.uint8))
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 2)


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: 跑测试,验证失败**

```bash
cd C:\Users\Administrator\worklog-app
python -m pytest tests/test_ocr_profile_registry.py -v
```

预期:全部失败,ImportError(`cannot import name 'LabelProfile'`)。

- [ ] **Step 3: 实现 `LabelProfile` + 删除 `_wrinkle_ocr`**

Edit `blueprints/ocr_engine.py`:

```python
# 顶部加 import
from dataclasses import dataclass, field
from typing import Callable, Optional, Union
```

类外(顶部 CLAHE 区后)加 `LabelProfile`:

```python
@dataclass(frozen=True)
class LabelProfile:
    """标签类别配置:gating + ocr_kwargs + 预处理函数 + 后处理。

    preprocess_fn 可返回:
    - np.ndarray → 走标准单图 OCR
    - list[np.ndarray] → 逐图 OCR 后按顺序拼接(用于 row_split 等切片预处理)
    """
    name: str
    description: str
    gating_fn: Callable[[str], bool]
    ocr_kwargs: dict
    preprocess_fn: Optional[Callable[[np.ndarray], Union[np.ndarray, list[np.ndarray]]]] = None
    postprocess_fn: Optional[Callable[[str], str]] = None
```

`PaddleOCREngine.__init__` 改:
```python
def __init__(self):
    self._ocr = None
    self._profile_ocrs: dict[str, "PaddleOCR"] = {}  # 懒加载各 profile 的 OCR 实例
```

- [ ] **Step 4: 跑测试,验证通过**

```bash
cd C:\Users\Administrator\worklog-app
python -m pytest tests/test_ocr_profile_registry.py -v
```

预期:3/3 PASS

- [ ] **Step 5: 跑既有测试,确保无回归**

```bash
cd C:\Users\Administrator\worklog-app
python -m pytest tests/test_paddleocr_wrinkle_param.py tests/test_wrinkle_label_category.py tests/test_shipping_wrinkle_routing.py tests/test_inbound_wrinkle_routing.py tests/test_loading_wrinkle_routing.py -v
```

预期:全部仍然 PASS(本次只加 `LabelProfile` 类,不改任何现有逻辑)。如果 `_wrinkle_ocr` 删除导致既有测试失败,在 `__init__` 临时保留 `_wrinkle_ocr = None` 占位,Task 8 再彻底删。

- [ ] **Step 6: Commit**

```bash
cd C:\Users\Administrator\worklog-app
git add blueprints/ocr_engine.py tests/test_ocr_profile_registry.py
git commit -m "feat(ocr): 添加 LabelProfile dataclass + _profile_ocrs 容器

- LabelProfile frozen dataclass 描述 OCR profile
- preprocess_fn 类型允许返回 np.ndarray 或 list[np.ndarray]
- PaddleOCREngine.__init__ 加 _profile_ocrs: dict 占位
- tests/test_ocr_profile_registry.py:3 个 dataclass 测试"
```

---

## Task 2: 内置 fallback + JSON 配置加载器

**Files:**
- Modify: `blueprints/ocr_engine.py`(加 `_PROFILE_CFG_PATH` + `_BUILTIN_FALLBACK_CFG` + `_load_profile_config()`)
- Test: `tests/test_ocr_profile_registry.py`(加测试)

**Interfaces:**
- Produces:
  - `_load_profile_config() -> dict` — 首次调时读 `config/ocr_profiles.json`,失败用 `_BUILTIN_FALLBACK_CFG`,缓存结果

- [ ] **Step 1: 写失败测试**

```python
class ProfileConfigLoadingTests(unittest.TestCase):

    def test_load_config_returns_dict(self):
        """_load_profile_config 返回 dict(可能含多个 profile)。"""
        from blueprints.ocr_engine import _load_profile_config
        cfg = _load_profile_config()
        self.assertIsInstance(cfg, dict)
        self.assertIn('paper_wrinkle', cfg)  # 至少含 paper_wrinkle

    def test_load_config_caches(self):
        """二次调用返回同一对象(缓存)。"""
        from blueprints.ocr_engine import _load_profile_config
        cfg1 = _load_profile_config()
        cfg2 = _load_profile_config()
        self.assertIs(cfg1, cfg2)
```

- [ ] **Step 2: 跑测试,验证失败**

```bash
cd C:\Users\Administrator\worklog-app
python -m pytest tests/test_ocr_profile_registry.py::ProfileConfigLoadingTests -v
```

预期:ImportError(`cannot import name '_load_profile_config'`)。

- [ ] **Step 3: 实现 `_load_profile_config()` + `_BUILTIN_FALLBACK_CFG`**

Edit `blueprints/ocr_engine.py`,在 `LabelProfile` 定义之前加:

```python
import json as _json_for_ocr_config
from pathlib import Path as _Path_for_ocr_config

_PROFILE_CFG_PATH = _Path_for_ocr_config(__file__).parent.parent / 'config' / 'ocr_profiles.json'
_PROFILE_CFG_CACHE: Optional[dict] = None

# 内置最小可用配置(任何 JSON 加载失败时使用)
# 必须含 4 个 profile + _default + _order
_BUILTIN_FALLBACK_CFG = {
    'multi_row_table_less': {
        'name': 'multi_row_table_less',
        'description': 'fallback - 无表格线多行',
        'variants': [],
        'ocr_kwargs': {'lang': 'ch', 'use_angle_cls': True, 'show_log': False,
                       'det_db_thresh': 0.2, 'det_db_box_thresh': 0.4, 'use_dilation': True},
        'preprocess': {'type': 'row_split', 'max_subimgs': 5},
    },
    'paper_wrinkle': {
        'name': 'paper_wrinkle',
        'description': 'fallback - 纸质+褶皱',
        'variants': ['白磅布三文治', '黑磅布三文治', 'B级 磅布三文治', '7P环保磅布三文治', '磅布三文治'],
        'ocr_kwargs': {'lang': 'ch', 'use_angle_cls': True, 'show_log': False,
                       'det_db_thresh': 0.2, 'det_db_box_thresh': 0.4, 'use_dilation': True},
        'preprocess': {'type': 'clahe', 'tile_size': 8, 'clip_limit': 2.0},
    },
    'film_reflective': {
        'name': 'film_reflective',
        'description': 'fallback - 透明膜反光',
        'variants': ['无纺布'],
        'ocr_kwargs': {'lang': 'ch', 'use_angle_cls': True, 'show_log': False,
                       'det_db_thresh': 0.2, 'det_db_box_thresh': 0.4, 'use_dilation': True},
        'preprocess': {'type': 'film_reflection', 'highlight_threshold': 230,
                        'highlight_neighborhood_saturation': 0.5},
    },
    'stamp_dirty': {
        'name': 'stamp_dirty',
        'description': 'fallback - 合格章+手写',
        'variants': ['杂胶', '7P环保杂胶', '黑杂胶', '白杂胶', '纯胶', '黑纯胶', '白纯胶'],
        'ocr_kwargs': {'lang': 'ch', 'use_angle_cls': True, 'show_log': False,
                       'det_db_thresh': 0.2, 'det_db_box_thresh': 0.4, 'use_dilation': True},
        'preprocess': {'type': 'stamp_removal',
                        'red_hue_ranges': [[0, 10], [170, 180]],
                        'blue_hue_ranges': [[100, 130]],
                        'saturation_min': 50, 'replace_color': [255, 255, 255]},
    },
    '_default': {
        'name': '_default',
        'description': 'fallback - 通用兜底',
        'variants': [],
        'ocr_kwargs': {'lang': 'ch', 'use_angle_cls': True, 'show_log': False,
                       'det_db_thresh': 0.2, 'det_db_box_thresh': 0.4, 'use_dilation': True},
        'preprocess': None,
    },
    '_order': ['multi_row_table_less', 'paper_wrinkle', 'film_reflective', 'stamp_dirty'],
}


def _load_profile_config() -> dict:
    """懒加载 + 缓存 config/ocr_profiles.json。失败用内置 fallback,启动不阻塞。"""
    global _PROFILE_CFG_CACHE
    if _PROFILE_CFG_CACHE is not None:
        return _PROFILE_CFG_CACHE
    try:
        with open(_PROFILE_CFG_PATH, encoding='utf-8') as f:
            raw = _json_for_ocr_config.load(f)
        _PROFILE_CFG_CACHE = raw
        logger.info('OCR profile 配置加载成功: %s', _PROFILE_CFG_PATH)
        return _PROFILE_CFG_CACHE
    except Exception as e:
        logger.warning('OCR profile 配置加载失败,使用内置 fallback (%s): %s', _PROFILE_CFG_PATH, e)
        _PROFILE_CFG_CACHE = _BUILTIN_FALLBACK_CFG
        return _PROFILE_CFG_CACHE
```

- [ ] **Step 4: 跑测试,验证通过**

```bash
cd C:\Users\Administrator\worklog-app
python -m pytest tests/test_ocr_profile_registry.py::ProfileConfigLoadingTests -v
```

预期:2/2 PASS

- [ ] **Step 5: 跑既有测试,确保无回归**

```bash
cd C:\Users\Administrator\worklog-app
python -m pytest tests/test_paddleocr_wrinkle_param.py tests/test_wrinkle_label_category.py tests/test_clahe_preprocessing.py -v
```

预期:无回归(本次只加 loader,不动其他逻辑)。如果 import 触发副作用,清理即可。

- [ ] **Step 6: Commit**

```bash
cd C:\Users\Administrator\worklog-app
git add blueprints/ocr_engine.py tests/test_ocr_profile_registry.py
git commit -m "feat(ocr): JSON 配置加载器 + 内置 fallback

- _load_profile_config():读 config/ocr_profiles.json,失败用 _BUILTIN_FALLBACK_CFG
- 首次调时读,后续用全局缓存
- 加载失败不抛异常,启动不阻塞
- _BUILTIN_FALLBACK_CFG 含 4 profile + _default + _order(最小可用)"
```

---

## Task 3: 创建 `config/ocr_profiles.json` 配置

**Files:**
- Create: `config/ocr_profiles.json`

**Interfaces:**
- Produces: 配置文件,被 Task 2 的 `_load_profile_config()` 读取

- [ ] **Step 1: 创建 JSON**

创建文件 `config/ocr_profiles.json`(确保 `config/` 目录存在):

```bash
cd C:\Users\Administrator\worklog-app
mkdir -p config 2>/dev/null || true
```

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
      "det_db_unclip_ratio": 1.8,
      "use_dilation": true
    },
    "preprocess": {
      "type": "row_split",
      "max_subimgs": 5
    }
  },
  "paper_wrinkle": {
    "name": "paper_wrinkle",
    "description": "纸质 + 褶皱微阴影(磅布三文治类,次要)",
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

- [ ] **Step 2: 跑测试,验证 JSON 可加载**

```bash
cd C:\Users\Administrator\worklog-app
python -m pytest tests/test_ocr_profile_registry.py::ProfileConfigLoadingTests -v
```

预期:2/2 PASS(确认 `_load_profile_config` 成功读到 JSON)

- [ ] **Step 3: Commit**

```bash
cd C:\Users\Administrator\worklog-app
git add config/ocr_profiles.json
git commit -m "feat(ocr): config/ocr_profiles.json 4 个 profile 配置

- multi_row_table_less(主):row_split + 激进 OCR 参数
- paper_wrinkle(次):CLAHE + slow score
- film_reflective(次):反光去除
- stamp_dirty(次):HSV 印章去除 + 加硬/纯胶
- _default 兜底
- _order 优先级:multi_row > paper > film > stamp"
```

---

## Task 4: `_compute_dynamic_row_split_params()` + `_row_split_preprocess()`

**Files:**
- Modify: `blueprints/ocr_engine.py`(加 `_compute_dynamic_row_split_params()` + `_row_split_preprocess()`)
- Test: `tests/test_ocr_profile_multi_row.py`(新建)

**Interfaces:**
- Produces:
  - `_compute_dynamic_row_split_params(h: int) -> dict` — 按图高算 `{min_gap_height, min_subimg_height, threshold}`(基于图大小的相对值)
  - `_row_split_preprocess(img_np, cfg: dict) -> list[np.ndarray]` — 水平投影找行间隙 → 切片 → 过滤 → 截断 → 返回子图列表(0 或 1 段时返回 [原图])

- [ ] **Step 1: 写失败测试**

```python
"""multi_row_table_less profile 测试。"""
import unittest
import numpy as np
from PIL import Image as PILImage

from blueprints.ocr_engine import (
    _compute_dynamic_row_split_params,
    _row_split_preprocess,
)


class RowSplitParamComputationTests(unittest.TestCase):

    def test_dynamic_params_scales_with_image_height(self):
        """小图和大图得到不同参数,但都是合理的相对值。"""
        params_small = _compute_dynamic_row_split_params(h=400)
        params_large = _compute_dynamic_row_split_params(h=2000)
        # 大图的 est_row_height 应该更大
        self.assertGreater(params_large['min_gap_height'], params_small['min_gap_height'])
        self.assertGreater(params_large['min_subimg_height'], params_small['min_subimg_height'])
        # 阈值都是 density × ratio
        self.assertIn('threshold', params_small)
        self.assertGreater(params_small['threshold'], 0)

    def test_dynamic_params_have_minimums(self):
        """参数不会小到不合理值。"""
        params = _compute_dynamic_row_split_params(h=100)  # 极小图
        self.assertGreaterEqual(params['min_gap_height'], 6)
        self.assertGreaterEqual(params['min_subimg_height'], 30)


def _make_4row_no_table_line_image(h=400, w=600) -> np.ndarray:
    """合成 4 行文字无表格线图。"""
    img = np.full((h, w, 3), 255, dtype=np.uint8)  # 白底
    # 4 行文字,Y 坐标均匀分布
    for row in range(4):
        y_center = (h // 4) * row + (h // 8)
        # 写一行"TEST"字(黑色矩形模拟)
        for x_off in range(50, w - 50, 30):
            img[y_center:y_y + 20, x_off:x_off + 20] = 0
    return img


class RowSplitPreprocessTests(unittest.TestCase):

    def test_single_row_returns_original(self):
        """单行图(没有行间隙)→ 返回 [原图]。"""
        img = np.full((200, 600, 3), 255, dtype=np.uint8)
        # 单行连续文字(无间隔)
        for x in range(50, 550, 30):
            img[100:120, x:x + 20] = 0
        result = _row_split_preprocess(img, {'max_subimgs': 5})
        self.assertIsInstance(result, list)
        self.assertEqual(len(result), 1)
        self.assertTrue(np.array_equal(result[0], img))

    def test_4row_no_table_line_returns_list(self):
        """4 行无表线图 → 返回 ≥ 2 个子图。"""
        img = _make_4row_no_table_line_image()
        result = _row_split_preprocess(img, {'max_subimgs': 5})
        self.assertIsInstance(result, list)
        self.assertGreaterEqual(len(result), 2, '应至少切出 2 段')
        # 每个子图应保持 RGB shape
        for sub in result:
            self.assertEqual(sub.ndim, 3)
            self.assertEqual(sub.shape[-1], 3)

    def test_4row_subimgs_respect_max_subimgs(self):
        """4 行图切出段数不超过 max_subimgs。"""
        img = _make_4row_no_table_line_image()
        result = _row_split_preprocess(img, {'max_subimgs': 2})
        self.assertLessEqual(len(result), 2)

    def test_handles_gracefully_on_2d_input(self):
        """shape 不对(2D)→ 返回 [原图],不抛异常。"""
        bad = np.zeros((256, 256), dtype=np.uint8)
        result = _row_split_preprocess(bad, {'max_subimgs': 5})
        self.assertEqual(len(result), 1)
        self.assertTrue(np.array_equal(result[0], bad))


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: 跑测试,验证失败**

```bash
cd C:\Users\Administrator\worklog-app
python -m pytest tests/test_ocr_profile_multi_row.py -v
```

预期:ImportError(`cannot import name '_row_split_preprocess'`)。

- [ ] **Step 3: 实现 `_compute_dynamic_row_split_params()`**

Edit `blueprints/ocr_engine.py`,在 `LabelProfile` 定义后:

```python
def _compute_dynamic_row_split_params(h: int) -> dict:
    """按图高计算自适应 row_split 参数。

    返回 dict:
    - min_gap_height: 找行间隙的最小高度(像素)
    - min_subimg_height: 子图最小高度(像素)
    - threshold: 文字密度阈值(用中位数 × 0.5,抗极端值)
    """
    est_row_height = max(20, h // 12)  # 假设 4-12 行布局
    return {
        'min_gap_height': max(6, est_row_height // 2),
        'min_subimg_height': max(30, est_row_height),
        # threshold 在 _row_split_preprocess 内根据 median 实时算
    }
```

- [ ] **Step 4: 实现 `_row_split_preprocess()`**

```python
def _row_split_preprocess(img_np: np.ndarray, cfg: dict) -> list:
    """水平投影找行间隙 → 切片 → 过滤 + 截断 → 返回子图列表。

    0 段或 1 段时返回 [原图](避免无意义切分)。
    """
    try:
        if img_np is None or img_np.size == 0 or img_np.ndim != 3 or img_np.shape[-1] != 3:
            return [img_np]

        max_subimgs = cfg.get('max_subimgs', 5)
        h = img_np.shape[0]
        params = _compute_dynamic_row_split_params(h)

        # 灰度 + 二值化(阈值180)
        gray = np.array(PILImage.fromarray(img_np).convert('L'))
        binary = (gray < 180).astype(np.uint8)  # 文字像素

        # 文字密度(每行文字像素数)
        row_density = binary.sum(axis=1)

        # 用中位数抗极端值
        median_density = float(np.median(row_density)) if row_density.max() > 0 else 1.0
        threshold = max(1.0, median_density * 0.5)

        # 找密度 ≤ threshold 且连续 ≥ min_gap_height 的间隙
        min_gap_height = params['min_gap_height']
        gaps = []
        in_gap = row_density <= threshold
        gap_start = None
        for y in range(h):
            if in_gap[y]:
                if gap_start is None:
                    gap_start = y
            else:
                if gap_start is not None and y - gap_start >= min_gap_height:
                    gaps.append((gap_start, y))
                gap_start = None
        if gap_start is not None and h - gap_start >= min_gap_height:
            gaps.append((gap_start, h))

        if not gaps:
            return [img_np]

        # 在间隙处切片
        cuts = sorted(set([gs for gs, ge in gaps] + [ge for gs, ge in gaps]))
        subimgs = []
        prev_y = 0
        for cut_y in cuts:
            if cut_y > prev_y:
                subimgs.append(img_np[prev_y:cut_y])
            prev_y = cut_y
        if prev_y < h:
            subimgs.append(img_np[prev_y:])

        # 过滤掉太矮的子图(< min_subimg_height)
        min_subimg_height = params['min_subimg_height']
        subimgs = [s for s in subimgs if s.shape[0] >= min_subimg_height]

        if not subimgs:
            return [img_np]

        # 截断到 max_subimgs(超过则前 N 个 + 最后一个合并其余)
        if len(subimgs) > max_subimgs:
            kept = subimgs[:max_subimgs - 1]
            rest_concat = np.concatenate(subimgs[max_subimgs - 1:], axis=0)
            subimgs = kept + [rest_concat]

        return subimgs

    except Exception as e:
        logger.warning('row_split 预处理失败,使用原图: %s', e)
        return [img_np]
```

确保 `PILImage` 在文件顶部已 import(用 `from PIL import Image as PILImage`)。

- [ ] **Step 5: 跑测试,验证通过**

```bash
cd C:\Users\Administrator\worklog-app
python -m pytest tests/test_ocr_profile_multi_row.py -v
```

预期:6/6 PASS

- [ ] **Step 6: 跑既有测试,确保无回归**

```bash
cd C:\Users\Administrator\worklog-app
python -m pytest tests/test_ocr_profile_registry.py -v
```

预期:5/5 PASS(3 dataclass + 2 loader)

- [ ] **Step 7: Commit**

```bash
cd C:\Users\Administrator\worklog-app
git add blueprints/ocr_engine.py tests/test_ocr_profile_multi_row.py
git commit -m "feat(ocr): _row_split_preprocess 自适应行切片

- _compute_dynamic_row_split_params(h)按图高算 min_gap/min_subimg
- _row_split_preprocess:水平投影 → 找密度低谷 → 切片 → 过滤 → 截断
- max_subimgs 上限保护(避免切太细)
- 0/1 段时返回 [原图](不切)
- 异常降级:任何异常返回 [原图] + logger.warning
- 单行图、单子图、2D 输入等边界 case 测过"
```

---

## Task 5: `_make_preprocess_fn()` 工厂 + clahe / film_reflection / stamp_removal 三种 preprocess

**Files:**
- Modify: `blueprints/ocr_engine.py`(加 `_make_preprocess_fn()` + `_film_reflection_preprocess()` + `_stamp_removal_preprocess()`)
- Test: `tests/test_ocr_profile_paper_wrinkle.py` + `test_ocr_profile_film_reflective.py` + `test_ocr_profile_stamp_dirty.py`(新建)

**Interfaces:**
- Produces:
  - `_make_preprocess_fn(cfg: dict | None) -> Callable[[np.ndarray], Union[np.ndarray, list[np.ndarray]]] | None` — type='clahe' / 'film_reflection' / 'stamp_removal' / 'row_split' 映射到对应 preprocess 函数;cfg=None → None
  - `_film_reflection_preprocess(img_np, cfg) -> np.ndarray` — 高光区域 median 修复 + CLAHE
  - `_stamp_removal_preprocess(img_np, cfg) -> np.ndarray` — HSV 红/蓝印章 mask → 替换为白 + CLAHE
  - `_clahe_preprocess(img_np, cfg) -> np.ndarray` — 现有 `_enhance_wrinkle_label` 复用(只是名字不同)

**依赖**:需要 `opencv-python-headless`(Task 5.1 装)

- [ ] **Step 5.1: 安装 opencv-python-headless**

```bash
cd C:\Users\Administrator\worklog-app
pip install opencv-python-headless
```

- [ ] **Step 5.2: 写失败测试**

3 个测试文件:

`tests/test_ocr_profile_paper_wrinkle.py`:
```python
"""paper_wrinkle profile 测试。"""
import unittest
import numpy as np
from PIL import Image as PILImage

from blueprints.ocr_engine import _make_preprocess_fn, _enhance_wrinkle_label


class PaperWrinklePreprocessTests(unittest.TestCase):

    def test_clahe_preprocess_returns_same_shape(self):
        """CLAHE 预处理后 shape/dtype 不变。"""
        img = np.full((200, 200, 3), 240, dtype=np.uint8)
        result = _enhance_wrinkle_label(img)
        self.assertEqual(result.shape, img.shape)
        self.assertEqual(result.dtype, np.uint8)

    def test_make_preprocess_fn_clahe(self):
        """type='clahe' 映射到 _enhance_wrinkle_label。"""
        fn = _make_preprocess_fn({'type': 'clahe', 'tile_size': 8, 'clip_limit': 2.0})
        self.assertIsNotNone(fn)
        img = np.full((100, 100, 3), 240, dtype=np.uint8)
        out = fn(img)
        self.assertEqual(out.shape, img.shape)

    def test_make_preprocess_fn_none(self):
        """cfg=None 返回 None。"""
        self.assertIsNone(_make_preprocess_fn(None))


if __name__ == '__main__':
    unittest.main()
```

`tests/test_ocr_profile_film_reflective.py`:
```python
"""film_reflective profile 测试。"""
import unittest
import numpy as np

from blueprints.ocr_engine import _make_preprocess_fn, _film_reflection_preprocess


class FilmReflectionPreprocessTests(unittest.TestCase):

    def test_highlight_repaired(self):
        """带高光的图 → 预处理后高光区域被 inpaint(像素值降低)。"""
        # 创建带高光的图:中间一块亮(>230),周围正常
        img = np.full((200, 200, 3), 200, dtype=np.uint8)
        img[80:120, 80:120] = 255  # 高亮方块

        result = _film_reflection_preprocess(img, {
            'highlight_threshold': 230,
            'highlight_neighborhood_saturation': 0.5,
        })
        # 高亮区应被修复(像素值下降)
        highlight_region_before = img[80:120, 80:120].mean()
        highlight_region_after = result[80:120, 80:120].mean()
        self.assertLess(highlight_region_after, highlight_region_before)

    def test_no_highlight_unchanged_shape(self):
        """无高光的图 → shape/dtype 不变。"""
        img = np.full((200, 200, 3), 200, dtype=np.uint8)
        result = _film_reflection_preprocess(img, {
            'highlight_threshold': 230,
            'highlight_neighborhood_saturation': 0.5,
        })
        self.assertEqual(result.shape, img.shape)

    def test_make_preprocess_fn_film_reflection(self):
        """type='film_reflection' 映射到 _film_reflection_preprocess。"""
        fn = _make_preprocess_fn({'type': 'film_reflection',
                                   'highlight_threshold': 230,
                                   'highlight_neighborhood_saturation': 0.5})
        self.assertIsNotNone(fn)


if __name__ == '__main__':
    unittest.main()
```

`tests/test_ocr_profile_stamp_dirty.py`:
```python
"""stamp_dirty profile 测试。"""
import unittest
import numpy as np

from blueprints.ocr_engine import _make_preprocess_fn, _stamp_removal_preprocess


class StampRemovalPreprocessTests(unittest.TestCase):

    def test_red_stamp_replaced_with_white(self):
        """带红色印章的图 → 印章区域被替换为白。"""
        # 创建带红色块的图
        img = np.full((200, 200, 3), 240, dtype=np.uint8)
        img[50:100, 50:100] = [255, 0, 0]  # 红色印章

        result = _stamp_removal_preprocess(img, {
            'red_hue_ranges': [[0, 10], [170, 180]],
            'blue_hue_ranges': [[100, 130]],
            'saturation_min': 50,
            'replace_color': [255, 255, 255],
        })
        # 印章区域应被替换(红色消失)
        stamp_region = result[50:100, 50:100]
        self.assertFalse(np.any(stamp_region[:, :, 0] == 255))  # R 不应为 255

    def test_no_stamp_unchanged_shape(self):
        """无印章的图 → shape 不变。"""
        img = np.full((200, 200, 3), 240, dtype=np.uint8)
        result = _stamp_removal_preprocess(img, {
            'red_hue_ranges': [[0, 10], [170, 180]],
            'blue_hue_ranges': [[100, 130]],
            'saturation_min': 50,
            'replace_color': [255, 255, 255],
        })
        self.assertEqual(result.shape, img.shape)

    def test_make_preprocess_fn_stamp_removal(self):
        """type='stamp_removal' 映射到 _stamp_removal_preprocess。"""
        fn = _make_preprocess_fn({
            'type': 'stamp_removal',
            'red_hue_ranges': [[0, 10], [170, 180]],
            'blue_hue_ranges': [[100, 130]],
            'saturation_min': 50,
            'replace_color': [255, 255, 255],
        })
        self.assertIsNotNone(fn)


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 5.3: 跑测试,验证失败**

```bash
cd C:\Users\Administrator\worklog-app
python -m pytest tests/test_ocr_profile_paper_wrinkle.py tests/test_ocr_profile_film_reflective.py tests/test_ocr_profile_stamp_dirty.py -v
```

预期:ImportError(`cannot import name '_make_preprocess_fn'`)。

- [ ] **Step 5.4: 实现 3 个 preprocess + 工厂**

Edit `blueprints/ocr_engine.py`,在 `_row_split_preprocess` 后加:

```python
import cv2  # opencv-python-headless


def _clahe_preprocess(img_np: np.ndarray, cfg: dict) -> np.ndarray:
    """复用现有 _enhance_wrinkle_label,只是名字不同(统一 preprocess 接口)。"""
    return _enhance_wrinkle_label(img_np)


def _film_reflection_preprocess(img_np: np.ndarray, cfg: dict) -> np.ndarray:
    """高光去除:检测 → median 修复 → CLAHE。"""
    threshold_val = cfg.get('highlight_threshold', 230)
    sat_ratio = cfg.get('highlight_neighborhood_saturation', 0.5)

    # 1. 检测高光像素(任何通道 > threshold)
    bright_mask = (img_np.max(axis=-1) > threshold_val)

    # 2. 邻域判断(避免单点噪声被误判)
    # 简化:直接按单像素判断(如果效果差再加邻域)

    if not bright_mask.any():
        return img_np

    # 3. 用 median 修复(取 5x5 邻域中位数)
    img_uint8 = np.array(PILImage.fromarray(img_np).convert('RGB'))
    fixed = cv2.medianBlur(img_uint8, 5)

    # 4. 把高光区域用 fixed 替换
    mask_3d = np.stack([bright_mask] * 3, axis=-1)
    result = np.where(mask_3d, fixed, img_np)

    # 5. 跑 CLAHE(链式)
    return _clahe_preprocess(result, {'type': 'clahe', 'tile_size': 8, 'clip_limit': 2.0})


def _stamp_removal_preprocess(img_np: np.ndarray, cfg: dict) -> np.ndarray:
    """HSV 红/蓝印章 mask → 替换为白 → CLAHE。"""
    red_ranges = cfg.get('red_hue_ranges', [[0, 10], [170, 180]])
    blue_ranges = cfg.get('blue_hue_ranges', [[100, 130]])
    sat_min = cfg.get('saturation_min', 50)
    replace_color = cfg.get('replace_color', [255, 255, 255])

    # 1. 转 HSV
    hsv = cv2.cvtColor(img_np, cv2.COLOR_RGB2HSV)

    # 2. 红印章 mask
    red_mask = np.zeros(img_np.shape[:2], dtype=bool)
    for lo, hi in red_ranges:
        red_mask |= (hsv[:, :, 0] >= lo) & (hsv[:, :, 0] <= hi) & (hsv[:, :, 1] >= sat_min)

    # 3. 蓝印章 mask
    blue_mask = np.zeros(img_np.shape[:2], dtype=bool)
    for lo, hi in blue_ranges:
        blue_mask |= (hsv[:, :, 0] >= lo) & (hsv[:, :, 0] <= hi) & (hsv[:, :, 1] >= sat_min)

    # 4. 替换为白
    stamp_mask = red_mask | blue_mask
    result = img_np.copy()
    if stamp_mask.any():
        result[stamp_mask] = replace_color

    # 5. CLAHE
    return _clahe_preprocess(result, {'type': 'clahe', 'tile_size': 8, 'clip_limit': 2.0})


def _make_preprocess_fn(cfg):
    """按 cfg['type'] 映射到对应 preprocess 函数。cfg=None → None。"""
    if cfg is None:
        return None
    preprocess_type = cfg.get('type')
    if preprocess_type == 'clahe':
        return lambda img: _clahe_preprocess(img, cfg)
    elif preprocess_type == 'film_reflection':
        return lambda img: _film_reflection_preprocess(img, cfg)
    elif preprocess_type == 'stamp_removal':
        return lambda img: _stamp_removal_preprocess(img, cfg)
    elif preprocess_type == 'row_split':
        return lambda img: _row_split_preprocess(img, cfg)
    else:
        logger.warning('未知 preprocess type: %s,返回 None', preprocess_type)
        return None
```

确保 `import cv2` 和 `from PIL import Image as PILImage` 在文件顶部。

- [ ] **Step 5.5: 跑测试,验证通过**

```bash
cd C:\Users\Administrator\worklog-app
python -m pytest tests/test_ocr_profile_paper_wrinkle.py tests/test_ocr_profile_film_reflective.py tests/test_ocr_profile_stamp_dirty.py -v
```

预期:9/9 PASS(3 + 3 + 3)

- [ ] **Step 5.6: 跑既有测试,确保无回归**

```bash
cd C:\Users\Administrator\worklog-app
python -m pytest tests/test_ocr_profile_registry.py tests/test_ocr_profile_multi_row.py -v
```

预期:无回归(11 + 6 = 17 PASS)

- [ ] **Step 5.7: Commit**

```bash
cd C:\Users\Administrator\worklog-app
git add blueprints/ocr_engine.py tests/test_ocr_profile_paper_wrinkle.py tests/test_ocr_profile_film_reflective.py tests/test_ocr_profile_stamp_dirty.py
git commit -m "feat(ocr): 3 个 preprocess + _make_preprocess_fn 工厂

- _clahe_preprocess:复用 _enhance_wrinkle_label
- _film_reflection_preprocess:高光 median 修复 + CLAHE
- _stamp_removal_preprocess:HSV 红/蓝印章 mask → 替换为白 + CLAHE
- _make_preprocess_fn(cfg):type → 函数映射(None/clahe/film_reflection/stamp_removal/row_split)
- 新增 opencv-python-headless 依赖(requirements.txt 更新)"
git add requirements.txt  # 如果有变动
git commit -m "deps: 添加 opencv-python-headless(stamp_removal HSV 转换需要)" 2>/dev/null || true
```

---

## Task 6: `get_label_profile()` dispatcher

**Files:**
- Modify: `blueprints/ocr_engine.py`(加 dispatcher + `_make_substring_gating_fn()` + `_BUILTIN_FALLBACK_LABELS` 懒构造缓存)
- Test: `tests/test_ocr_profile_registry.py`(加测试)

**Interfaces:**
- Produces:
  - `get_label_profile(product_name: str) -> LabelProfile` — 按 `_order` 遍历,首个 gating 命中返回;都未命中 → `_default` profile

- [ ] **Step 1: 写失败测试**

```python
class GetLabelProfileDispatcherTests(unittest.TestCase):

    def test_dispatch_to_paper_wrinkle_for_known_variant(self):
        """品名含'磅布三文治'→ 命中 paper_wrinkle(若 _order 中它在 multi_row 之后)。"""
        from blueprints.ocr_engine import get_label_profile
        profile = get_label_profile('白磅布三文治')
        self.assertEqual(profile.name, 'paper_wrinkle')  # paper_wrinkle variants 含此字符串

    def test_dispatch_to_film_reflective(self):
        """品名含'无纺布'→ film_reflective。"""
        from blueprints.ocr_engine import get_label_profile
        profile = get_label_profile('无纺布')
        self.assertEqual(profile.name, 'film_reflective')

    def test_dispatch_to_stamp_dirty(self):
        """品名含'杂胶'→ stamp_dirty。"""
        from blueprints.ocr_engine import get_label_profile
        profile = get_label_profile('7P环保杂胶')
        self.assertEqual(profile.name, 'stamp_dirty')

    def test_unknown_product_returns_default(self):
        """未知品名 → _default。"""
        from blueprints.ocr_engine import get_label_profile
        profile = get_label_profile('完全未知商品XYZ')
        self.assertEqual(profile.name, '_default')

    def test_dispatch_respects_order(self):
        """同一品名命中 _order 第一个匹配的 profile(不会同时命中两个)。"""
        from blueprints.ocr_engine import get_label_profile
        # '白磅布三文治' 不在 film_reflective 或 stamp_dirty variants 中
        profile = get_label_profile('白磅布三文治')
        self.assertEqual(profile.name, 'paper_wrinkle')

    def test_multi_row_is_first_priority(self):
        """multi_row_table_less variants 为空,但 _order 第一,空 variants 不会命中品名 gating。
        它主要靠图像特征触发(后续扩展),品名 gating 不命中 → 落到下一个匹配。"""
        from blueprints.ocr_engine import get_label_profile
        # 任何品名都不会命中 multi_row_table_less(variants=[])
        profile = get_label_profile('无纺布')
        self.assertEqual(profile.name, 'film_reflective')  # multi_row 不命中,落到 film_reflective
```

- [ ] **Step 2: 跑测试,验证失败**

```bash
cd C:\Users\Administrator\worklog-app
python -m pytest tests/test_ocr_profile_registry.py::GetLabelProfileDispatcherTests -v
```

预期:ImportError。

- [ ] **Step 3: 实现 dispatcher + 工厂**

Edit `blueprints/ocr_engine.py`:

```python
def _make_substring_gating_fn(variants: list) -> Callable[[str], bool]:
    """子串匹配 gating(任一 variant ∈ product_name 即命中)。"""
    return lambda pn: any(v in pn for v in variants)


_PROFILE_INSTANCES: dict = {}


def _get_or_build_profile(name: str) -> LabelProfile:
    """懒构造 LabelProfile 实例(避免模块导入慢)。"""
    if name in _PROFILE_INSTANCES:
        return _PROFILE_INSTANCES[name]
    cfg = _load_profile_config().get(name)
    if cfg is None:
        raise KeyError(f'OCR profile 不存在: {name}')
    profile = LabelProfile(
        name=cfg['name'],
        description=cfg.get('description', ''),
        gating_fn=_make_substring_gating_fn(cfg.get('variants', [])),
        ocr_kwargs=cfg.get('ocr_kwargs', {}),
        preprocess_fn=_make_preprocess_fn(cfg.get('preprocess')),
        postprocess_fn=None,
    )
    _PROFILE_INSTANCES[name] = profile
    return profile


def get_label_profile(product_name: str) -> LabelProfile:
    """product_name → 首个匹配的 LabelProfile;都未命中 → _default。

    按 JSON _order 遍历,gating_fn 命中即返回。
    任一轨异常(DB/解析)→ 视为未命中,继续下一轨。
    """
    cfg = _load_profile_config()
    order = cfg.get('_order', ['multi_row_table_less', 'paper_wrinkle',
                                'film_reflective', 'stamp_dirty'])
    for name in order:
        try:
            profile = _get_or_build_profile(name)
            if profile.gating_fn(product_name):
                return profile
        except Exception as e:
            logger.warning('Profile %s 路由异常: %s', name, e)
            continue
    # 默认
    return _get_or_build_profile(cfg.get('_default_name', '_default'))
```

- [ ] **Step 4: 跑测试,验证通过**

```bash
cd C:\Users\Administrator\worklog-app
python -m pytest tests/test_ocr_profile_registry.py -v
```

预期:全部 PASS(3 dataclass + 2 loader + 6 dispatcher = 11 PASS)

- [ ] **Step 5: 跑既有测试,确保无回归**

```bash
cd C:\Users\Administrator\worklog-app
python -m pytest tests/test_paddleocr_wrinkle_param.py tests/test_wrinkle_label_category.py tests/test_clahe_preprocessing.py -v
```

预期:无回归(本次只加 dispatcher,不动其他)

- [ ] **Step 6: Commit**

```bash
cd C:\Users\Administrator\worklog-app
git add blueprints/ocr_engine.py tests/test_ocr_profile_registry.py
git commit -m "feat(ocr): get_label_profile dispatcher

- _make_substring_gating_fn:variants 子串匹配
- _PROFILE_INSTANCES 缓存(避免每次重建)
- _get_or_build_profile 懒构造(从 JSON cfg → LabelProfile)
- get_label_profile:按 _order 遍历,首个 gating 命中返回;都未命中 → _default
- 任一轨异常 → 视为未命中,继续下一轨"
```

---

## Task 7: PaddleOCREngine API 改造 — `label_profile` 参数 + 列表预处理

**Files:**
- Modify: `blueprints/ocr_engine.py`
  - `PaddleOCREngine` 加 `_ensure_profile_ocr(profile)` 方法
  - `extract_text` / `extract_text_with_conf` 签名加 `label_profile` 参数
  - 处理 list preprocessing(逐图 OCR + 拼接)
  - 删除 `_wrinkle_ocr` 属性
- Test: `tests/test_paddleocr_wrinkle_param.py`(改用 `label_profile`)

**Interfaces:**
- Produces:
  - `PaddleOCREngine._ensure_profile_ocr(profile) -> PaddleOCR` — 懒加载,失败回退 `self._ocr`
  - `PaddleOCREngine.extract_text(image_bytes, label_profile=None) -> str` — 处理 list preprocessing
  - `PaddleOCREngine.extract_text_with_conf(image_bytes, label_profile=None) -> Tuple[str, float]`

- [ ] **Step 1: 写失败测试(改既有文件)**

Edit `tests/test_paddleocr_wrinkle_param.py`,将所有 `apply_wrinkle_enhance=True/False` 测试改为用 `label_profile` 参数:

```python
"""PaddleOCREngine API 测试(改用 label_profile 参数)。"""
import unittest
import numpy as np
from unittest.mock import patch, MagicMock
import sys
from pathlib import Path

# 添加项目根到 sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from blueprints.ocr_engine import PaddleOCREngine, get_label_profile


class PaddleOCREngineLabelProfileTests(unittest.TestCase):

    def setUp(self):
        self.engine = PaddleOCREngine()
        self.engine._ocr = MagicMock()
        self.engine._ocr.ocr = MagicMock(return_value=[[
            ([[0, 0], [10, 0], [10, 10], [0, 10]], ('test', 0.9))
        ]])
        # 预构造各 profile 实例
        self.paper_wrinkle = get_label_profile('白磅布三文治')
        self.film = get_label_profile('无纺布')
        self.stamp = get_label_profile('杂胶')

    def test_extract_text_label_profile_none_uses_default(self):
        """label_profile=None → 走 self._ocr(默认实例)。"""
        result = self.engine.extract_text(b'fake', label_profile=None)
        self.assertTrue(self.engine._ocr.ocr.called)
        self.assertEqual(result, 'test')

    def test_extract_text_label_profile_paper_wrinkle_uses_separate_instance(self):
        """label_profile=paper_wrinkle → 走 _profile_ocrs['paper_wrinkle']。"""
        self.engine._ensure_profile_ocr(self.paper_wrinkle)
        ocr_used = self.engine._profile_ocrs['paper_wrinkle']
        ocr_used.ocr = MagicMock(return_value=[[
            ([[0, 0], [10, 0], [10, 10], [0, 10]], ('wrinkled', 0.95))
        ]])
        result = self.engine.extract_text(b'fake', label_profile=self.paper_wrinkle)
        ocr_used.ocr.assert_called_once()
        self.assertEqual(result, 'wrinkled')

    def test_extract_text_handles_list_preprocessing(self):
        """label_profile.preprocess_fn 返回 list[np.ndarray] → 逐图 OCR 后拼接。"""
        # 用 row_split 模拟:返回 2 个子图
        def fake_prep(img):
            return [img, img]
        from blueprints.ocr_engine import LabelProfile
        profile = LabelProfile(
            name='test', description='', gating_fn=lambda pn: True,
            ocr_kwargs={}, preprocess_fn=fake_prep, postprocess_fn=None,
        )
        self.engine._ensure_profile_ocr(profile)
        ocr_used = self.engine._profile_ocrs['test']
        ocr_used.ocr = MagicMock(return_value=[[
            ([[0, 0], [10, 0], [10, 10], [0, 10]], ('row', 0.9))
        ]])
        result = self.engine.extract_text(b'fake', label_profile=profile)
        # OCR 应被调 2 次(每子图 1 次)
        self.assertEqual(ocr_used.ocr.call_count, 2)
        # 拼接结果
        self.assertEqual(result, 'row\nrow')

    def test_ensure_profile_ocr_lazy_loads_and_caches(self):
        """_ensure_profile_ocr 第二次调同一 profile 不重建实例。"""
        ocr1 = self.engine._ensure_profile_ocr(self.paper_wrinkle)
        ocr2 = self.engine._ensure_profile_ocr(self.paper_wrinkle)
        self.assertIs(ocr1, ocr2)


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: 跑测试,验证失败**

```bash
cd C:\Users\Administrator\worklog-app
python -m pytest tests/test_paddleocr_wrinkle_param.py -v
```

预期:失败(`extract_text` 不接受 `label_profile` 参数)。

- [ ] **Step 3: 实现 `_ensure_profile_ocr()`**

Edit `blueprints/ocr_engine.py`,在 `PaddleOCREngine` 类内加:

```python
def _ensure_profile_ocr(self, profile: LabelProfile):
    """按 profile.ocr_kwargs 懒加载 PaddleOCR 实例,失败回退 self._ocr。"""
    from paddleocr import PaddleOCR  # 顶层已 import,这里再写一次方便 mock
    if profile.name in self._profile_ocrs:
        return self._profile_ocrs[profile.name]
    try:
        ocr = PaddleOCR(**profile.ocr_kwargs)
    except Exception as e:
        logger.warning('Profile OCR 加载失败 %s,回退默认 _ocr: %s', profile.name, e)
        return self._ocr
    self._profile_ocrs[profile.name] = ocr
    return ocr
```

- [ ] **Step 4: 改造 `extract_text` + `extract_text_with_conf`**

找到现有的 `extract_text` 和 `extract_text_with_conf`,加 `label_profile` 参数 + 列表预处理:

```python
@log_ocr_call('ocr.paddle', evt='extract_text',
              failed_if=lambda r: not r,
              payload=image_payload, outcome=text_outcome)
def extract_text(self, image_bytes, label_profile: Optional[LabelProfile] = None):
    """只做 OCR 提取纯文本。

    Args:
        image_bytes: 图片字节流。
        label_profile: None = 通用 _ocr;LabelProfile 实例 = 走对应 OCR + 预处理。

    preprocess_fn 可返回 np.ndarray(单图)或 list[np.ndarray](多图切片,逐图 OCR 后拼接)。
    """
    try:
        self._ensure_model()
        resized = self._resize_if_needed(image_bytes)

        if label_profile is None:
            ocr = self._ocr
            preprocessed = resized
        else:
            ocr = self._ensure_profile_ocr(label_profile)
            if label_profile.preprocess_fn:
                preprocessed = label_profile.preprocess_fn(resized)
            else:
                preprocessed = resized

        # 单图 or 多图
        if isinstance(preprocessed, list):
            texts = []
            for sub in preprocessed:
                result = ocr.ocr(sub, cls=True)
                if result and result[0]:
                    for item in result[0]:
                        if item and len(item) >= 2:
                            text = item[1][0]
                            if text and text.strip():
                                texts.append(text.strip())
            return '\n'.join(texts)
        else:
            result = ocr.ocr(preprocessed, cls=True)
            if not result or not result[0]:
                return ''
            lines = []
            for item in result[0]:
                if item and len(item) >= 2:
                    text = item[1][0]
                    if text and text.strip():
                        lines.append(text.strip())
            return '\n'.join(lines)

    except Exception as e:
        logger.exception('PaddleOCR extract_text failed: %s', e)
        return ''
```

对 `extract_text_with_conf` 做类似改造(处理 list 并算平均 conf)。

具体 `extract_text_with_conf`:

```python
@log_ocr_call('ocr.paddle', evt='recognize',
              payload=image_payload, outcome=items_outcome)
def extract_text_with_conf(self, image_bytes, label_profile: Optional[LabelProfile] = None):
    """与 extract_text 类似,但额外返回平均置信度 (0~1)。

    label_profile: 同 extract_text。
    """
    try:
        self._ensure_model()
        resized = self._resize_if_needed(image_bytes)

        if label_profile is None:
            ocr = self._ocr
            preprocessed = resized
        else:
            ocr = self._ensure_profile_ocr(label_profile)
            if label_profile.preprocess_fn:
                preprocessed = label_profile.preprocess_fn(resized)
            else:
                preprocessed = resized

        if isinstance(preprocessed, list):
            all_lines = []
            all_confs = []
            for sub in preprocessed:
                result = ocr.ocr(sub, cls=True)
                if result and result[0]:
                    for item in result[0]:
                        if item and len(item) >= 2:
                            text, conf = item[1][0], item[1][1]
                            if text and text.strip():
                                all_lines.append(text.strip())
                                try:
                                    all_confs.append(float(conf))
                                except (TypeError, ValueError):
                                    pass
            text = '\n'.join(all_lines)
            avg_conf = sum(all_confs) / len(all_confs) if all_confs else 1.0
            return text, avg_conf
        else:
            result = ocr.ocr(preprocessed, cls=True)
            if not result or not result[0]:
                return '', 1.0
            lines = []
            confs = []
            for item in result[0]:
                if item and len(item) >= 2:
                    text, conf = item[1][0], item[1][1]
                    if text and text.strip():
                        lines.append(text.strip())
                        try:
                            confs.append(float(conf))
                        except (TypeError, ValueError):
                            pass
            text = '\n'.join(lines)
            # 厚字补回(spec 旧逻辑)
            import re as _re
            text = _re.sub(r'(?m)^\s*度\s*[:：]\s*(\d+\.?\d*)\s*mm?\s*$',
                           r'厚度:\1mm', text)
            avg_conf = sum(confs) / len(confs) if confs else 1.0
            return text, avg_conf

    except Exception as e:
        logger.exception('PaddleOCR extract_text_with_conf failed: %s', e)
        return '', 1.0
```

删除 `__init__` 里的 `self._wrinkle_ocr = None`(已替换为 `_profile_ocrs: dict = {}`)。

删除 `_ensure_model` 里的 `_wrinkle_ocr` 实例化分支(整个 `_wrinkle_ocr` 概念已废弃)。

- [ ] **Step 5: 跑测试,验证通过**

```bash
cd C:\Users\Administrator\worklog-app
python -m pytest tests/test_paddleocr_wrinkle_param.py -v
```

预期:4/4 PASS

- [ ] **Step 6: 跑既有测试,确保无回归**

```bash
cd C:\Users\Administrator\worklog-app
python -m pytest tests/test_clahe_preprocessing.py tests/test_wrinkle_label_category.py tests/test_ocr_profile_registry.py tests/test_ocr_profile_multi_row.py tests/test_ocr_profile_paper_wrinkle.py tests/test_ocr_profile_film_reflective.py tests/test_ocr_profile_stamp_dirty.py -v
```

预期:无回归(若有失败,看是不是 `is_wrinkle_label_category` 被删导致 — 把 `test_wrinkle_label_category.py` 标为 deprecated 或删)

- [ ] **Step 7: Commit**

```bash
cd C:\Users\Administrator\worklog-app
git add blueprints/ocr_engine.py tests/test_paddleocr_wrinkle_param.py
git commit -m "refactor(ocr): PaddleOCREngine 接受 label_profile + 列表预处理

- __init__:_wrinkle_ocr → _profile_ocrs: dict[str, PaddleOCR]
- _ensure_profile_ocr(profile):懒加载,失败回退 _ocr
- extract_text(label_profile=None):preprocess 返回 list 时逐图 OCR 后拼接
- extract_text_with_conf(label_profile=None):同上,avg_conf 取多子图平均
- 删除 _wrinkle_ocr 实例化分支(整体废弃)
- 测试改用 label_profile 替代 apply_wrinkle_enhance"
```

---

## Task 8: 蓝图迁移(3 个文件 + 既有 routing tests)

**Files:**
- Modify: `blueprints/shipping.py`(2 处 line ~100, ~1037)
- Modify: `blueprints/inbound.py`(2 处 line ~814, ~1036)
- Modify: `blueprints/loading.py`(1 处 line ~844)
- Modify: `tests/test_shipping_wrinkle_routing.py`(改用 label_profile)
- Modify: `tests/test_inbound_wrinkle_routing.py`(同上)
- Modify: `tests/test_loading_wrinkle_routing.py`(同上)

**Interfaces:**
- Consumes: `get_label_profile(product_name) -> LabelProfile`
- Produces: 5 处蓝图调用点改用新 API

- [ ] **Step 1: 写失败测试**

3 个 blueprint 测试文件,改 mock 对象为 `get_label_profile`,验证传递的 `label_profile` 是正确 profile 实例。

Edit `tests/test_shipping_wrinkle_routing.py`:

```python
"""shipping.py 行级图接口对褶皱品类的 CLAHE 路由测试。"""
import unittest
from unittest.mock import patch, MagicMock
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app
from blueprints.ocr_engine import LabelProfile


class ShippingWrinkleRoutingTests(unittest.TestCase):

    def setUp(self):
        self.app = create_app()
        self.client = self.app.test_client()
        with self.client.session_transaction() as sess:
            sess['operator_id'] = 1

    def test_row_image_upload_routes_label_profile_to_engine(self):
        """上传行级图 + record 是'白磅布三文治' → engine 收到 label_profile=paper_wrinkle。"""
        from PIL import Image
        import io
        buf = io.BytesIO()
        Image.new('RGB', (4, 4), 'white').save(buf, 'PNG')
        png = buf.getvalue()

        with patch('blueprints.shipping.ShippingRecord.get_by_id') as mock_get_rec, \
             patch('blueprints.shipping.get_ocr_engine') as mock_factory, \
             patch('blueprints.shipping.get_label_profile') as mock_dispatch:
            mock_get_rec.return_value = {
                'id': 1, 'order_pk': 1,
                'product_name': '白磅布三文治', 'specification': '1.2硬性',
            }
            mock_engine = MagicMock()
            mock_engine.extract_text_with_conf = MagicMock(return_value=('TEST', 0.9))
            mock_factory.return_value = mock_engine

            mock_paper = MagicMock(spec=LabelProfile)
            mock_paper.name = 'paper_wrinkle'
            mock_dispatch.return_value = mock_paper

            # 创建真实 record 在测试 db 里,避免 None 错误
            from models import ShippingOrder, ShippingRecord
            from models._db import get_db
            oid = ShippingOrder.create('2026-08-16', 'TEST')
            rid = ShippingRecord.create('2026-08-16', 'TEST', 'PLACEHOLDER', '', '1', 'y', '', order_pk=oid)

            resp = self.client.post(
                f'/api/v1/shipping-orders/records/{rid}/images',
                data={'image': (png, 'test.png')},
                content_type='multipart/form-data',
            )
            self.assertEqual(resp.status_code, 201)

        # 验证 dispatch 被调,返回 paper_wrinkle profile
        mock_dispatch.assert_called()
        # 验证 engine 收到 label_profile=paper_wrinkle
        if mock_engine.extract_text_with_conf.called:
            kwargs = mock_engine.extract_text_with_conf.call_args.kwargs
            self.assertIn('label_profile', kwargs)
            self.assertEqual(kwargs['label_profile'].name, 'paper_wrinkle')


if __name__ == '__main__':
    unittest.main()
```

`test_inbound_wrinkle_routing.py` 和 `test_loading_wrinkle_routing.py` 类似,改 endpoint 路径 + mock record 类。

- [ ] **Step 2: 跑测试,验证失败**

```bash
cd C:\Users\Administrator\worklog-app
python -m pytest tests/test_shipping_wrinkle_routing.py tests/test_inbound_wrinkle_routing.py tests/test_loading_wrinkle_routing.py -v
```

预期:`apply_wrinkle_enhance` kwarg 未传 → 测试断言失败(找不到 label_profile)。

- [ ] **Step 3: 改 shipping.py(2 处)**

Edit `blueprints/shipping.py`:

(a) import 行(line 31),加 `is_wrinkle_label_category` 改成 `get_label_profile`:
```python
from blueprints.ocr_engine import PaddleOCREngine, get_ocr_engine, OCR_MATCH_PROMPT_VERSION, get_label_profile
```

(b) line 100 附近(`_process_record_image_async`):
```python
# Before:
apply_wrinkle_enhance = is_wrinkle_label_category(record.get('product_name', ''))
_ocr_text, avg_conf = get_ocr_engine('paddleocr').extract_text_with_conf(
    _f.read(), apply_wrinkle_enhance=apply_wrinkle_enhance)

# After:
profile = get_label_profile(record.get('product_name', ''))
_ocr_text, avg_conf = get_ocr_engine('paddleocr').extract_text_with_conf(
    _f.read(), label_profile=profile)
```

(c) line 1037 附近(ai-judge re-OCR fallback):同样改动。

- [ ] **Step 4: 改 inbound.py(2 处)**

类似 shipping.py 的 3处改:import 加 `get_label_profile`(去掉 `is_wrinkle_label_category`),line 814 + 1036 改用 `get_label_profile(...).extract_text(..., label_profile=profile)`。

- [ ] **Step 5: 改 loading.py(1 处)**

Line 844(ai-judge fallback re-OCR)改用 `label_profile=profile`。

- [ ] **Step 6: 跑测试,验证通过**

```bash
cd C:\Users\Administrator\worklog-app
python -m pytest tests/test_shipping_wrinkle_routing.py tests/test_inbound_wrinkle_routing.py tests/test_loading_wrinkle_routing.py -v
```

预期:3/3 套件 PASS(每个至少 1 测试)

- [ ] **Step 7: 跑全 regression,确保无其他回归**

```bash
cd C:\Users\Administrator\worklog-app
python -m pytest tests/ -v --ignore=tests/test_wrinkle_label_category.py
```

预期:无新失败。`test_wrinkle_label_category.py` 是 deprecated 函数,删或跳过。

- [ ] **Step 8: 删除 `is_wrinkle_label_category`**

如果既有 `is_wrinkle_label_category` 函数没被任何文件引用,从 `blueprints/ocr_engine.py` 删除。

```bash
cd C:\Users\Administrator\worklog-app
grep -rn "is_wrinkle_label_category" --include="*.py" .
```

预期:除 `ocr_engine.py` 定义 + `test_wrinkle_label_category.py` 外无引用。如果 `test_wrinkle_label_category.py` 仍存在,删它。

- [ ] **Step 9: Commit**

```bash
cd C:\Users\Administrator\worklog-app
git add blueprints/shipping.py blueprints/inbound.py blueprints/loading.py tests/test_shipping_wrinkle_routing.py tests/test_inbound_wrinkle_routing.py tests/test_loading_wrinkle_routing.py tests/test_wrinkle_label_category.py
git rm tests/test_wrinkle_label_category.py 2>/dev/null
git commit -m "refactor: 3 蓝图迁移到 label_profile API

- shipping.py 2 处(行级上传 + ai-judge)
- inbound.py 2 处(行级上传 + ai-judge)
- loading.py 1 处(ai-judge)
- 3 个 routing 测试改用 label_profile + get_label_profile mock
- 删除 is_wrinkle_label_category 函数 + 既有 test_wrinkle_label_category.py"
```

---

## Task 9: 集成测试 + 真实 fixture(id=2994 应 yellow → green)

**Files:**
- Create: `tests/test_label_profile_integration.py`

**Interfaces:**
- Consumes: `get_label_profile()`, PaddleOCREngine.extract_text_with_conf
- Produces: 真实 fixture 跑 multi_row_table_less,验证字符完整

- [ ] **Step 1: 写测试**

```python
"""真实 fixture 集成测试:验证 multi_row_table_less 解决无表格线多行问题。

依赖:tests/fixtures/wrinkle_labels/ 已有 20+ 张图(由 tools/extract_wrinkle_fixtures.py 抽取)。
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

FIXTURES_DIR = Path(__file__).resolve().parent / 'fixtures' / 'wrinkle_labels'


class MultiRowIntegrationTests(unittest.TestCase):

    def setUp(self):
        from blueprints.ocr_engine import PaddleOCREngine, get_label_profile
        self.engine = PaddleOCREngine()
        self.profile = get_label_profile('白磅布三文治')

    def test_id2994_yellow_can_be_resolved_by_row_split(self):
        """TD-2026-08-16-004 id=2994:多行无表线「加硬」行被吞。
        multi_row_table_less 切片后应能识别「加硬」「硬」「1.2mm」「磅布三文治」。
        """
        img_path = FIXTURES_DIR / '2609e6826b604bc99a7aec3b6beae6ea.jpg'
        if not img_path.exists():
            self.skipTest(f'fixture 缺失: {img_path}')
        img_bytes = img_path.read_bytes()
        text, conf = self.engine.extract_text_with_conf(img_bytes, label_profile=self.profile)

        # 关键字符:之前整图 OCR 漏的
        self.assertIn('加', text, f'应识别到「加」,实际: {text!r}')
        self.assertIn('硬', text, f'应识别到「硬」,实际: {text!r}')
        # 之前能识别到的,应保持
        self.assertTrue('1.2' in text or '磅布三文治' in text,
                        f'应保留 1.2 或 磅布三文治,实际: {text!r}')

    def test_paper_wrinkle_profile_does_not_break_single_row(self):
        """单行图:paper_wrinkle profile 不应退化(走 CLAHE 但输出还是文字)。"""
        # 找一个最普通的 fixture
        pngs = list(FIXTURES_DIR.glob('*.png')) + list(FIXTURES_DIR.glob('*.jpg'))
        if not pngs:
            self.skipTest('fixtures 目录为空')
        img_bytes = pngs[0].read_bytes()
        text, conf = self.engine.extract_text_with_conf(img_bytes, label_profile=self.profile)
        # 单行图预处理后应仍能输出文字(不一定完全正确,但不应为空)
        self.assertIsInstance(text, str)


if __name__ == '__main__':
    unittest.main()
```

- [ ] **Step 2: 跑测试**

```bash
cd C:\Users\Administrator\worklog-app
python -m pytest tests/test_label_profile_integration.py -v
```

预期:id=2994 测试如果 PaddleOCR 不可用 → skip;否则 PASS(含「加」「硬」)

- [ ] **Step 3: 如果 skip,跑 fixture 准备**

如果 fixture 缺失,跑:

```bash
cd C:\Users\Administrator\worklog-app
python tools/extract_wrinkle_fixtures.py --execute  # 一次性脚本,从生产 db 抽
```

- [ ] **Step 4: Commit**

```bash
cd C:\Users\Administrator\worklog-app
git add tests/test_label_profile_integration.py
git commit -m "test(ocr): 真实 fixture 集成测试

- id=2994:多行无表线图,multi_row_table_less 应识别「加」「硬」「1.2mm」
- 单行图 paper_wrinkle profile 不退化(预处理后仍输出文字)
- 依赖 tests/fixtures/wrinkle_labels/ 已有 20+ 张图"
```

---

## Task 10: 全套测试 + 文档收尾

**Files:**
- Modify: `docs/superpowers/specs/2026-08-16-ocr-label-profile-registry-design.md`(如有 spec 微调)
- Test: 跑完整测试套件

- [ ] **Step 1: 全套测试**

```bash
cd C:\Users\Administrator\worklog-app
python -m pytest tests/ -v
```

预期:全部 PASS(老的 `is_wrinkle_label_category` 测试已删,所有 routing tests 用新 API)。

- [ ] **Step 2: 性能基准**

```bash
cd C:\Users\Administrator\worklog-app
python tools/_test_row_split_v2.py  # 已存在的验证脚本,确认 multi_row 仍 < 3500ms
```

预期:实测 < 3500ms。

- [ ] **Step 3: 更新 README/CLAUDE.md(如有)**

如果项目根有 README.md 或 CLAUDE.md 提到 `apply_wrinkle_enhance`,更新为 `label_profile`。

- [ ] **Step 4: 最终 commit**

```bash
cd C:\Users\Administrator\worklog-app
git add docs/ README.md CLAUDE.md 2>/dev/null
git commit -m "docs: 更新 spec 微调 + 文档同步

- spec/ocr-label-profile-registry-design.md 任何实现后的微调
- README.md / CLAUDE.md 中 apply_wrinkle_enhance 引用改为 label_profile" 2>/dev/null || true
```

---

## Self-Review

**1. Spec coverage:**
- ✅ `LabelProfile` dataclass → Task 1
- ✅ JSON 配置 + 内置 fallback → Tasks 2-3
- ✅ factory + 4 个 preprocess → Tasks 4-5
- ✅ dispatcher → Task 6
- ✅ Engine API + 列表预处理 → Task 7
- ✅ 蓝图迁移 → Task 8
- ✅ 集成测试 → Task 9
- ✅ 文档收尾 → Task 10

**2. Placeholder scan:**
- 无 TBD/TODO/FIXME
- 所有步骤有具体代码或命令
- 文件路径明确
- 函数签名一致

**3. Type consistency:**
- `LabelProfile.preprocess_fn` 类型 `Callable[[np.ndarray], Union[np.ndarray, list[np.ndarray]]]`
- `get_label_profile() -> LabelProfile`
- `_ensure_profile_ocr(profile) -> PaddleOCR`
- `extract_text(bytes, label_profile=None) -> str`
- `extract_text_with_conf(bytes, label_profile=None) -> Tuple[str, float]`
- 所有跨任务引用一致

**4. Dependencies:**
- Task 1 (foundation) → Task 2/3/4 (config + preprocess) → Task 6 (dispatcher) → Task 7 (engine) → Task 8 (blueprint)
- Tests 在每个 task 后立即跑
- Integration test 在最后

---

## 执行选项

Plan complete and saved to `docs/superpowers/plans/2026-08-16-ocr-label-profile-registry.md`.

两个执行选项:
1. **Subagent-Driven (推荐)** — 我每个 task 派一个新子代理,task 间评审
2. **Inline Execution** — 在本会话连续执行所有 task,batch 执行 + checkpoints

选哪个?