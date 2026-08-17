# Task 4 Brief — shipping.py 行级图接口接入门控

**Plan 文件**: `docs/superpowers/plans/2026-08-16-ocr-wrinkle-label-preprocessing.md`

**项目背景**: 丰源工作台(Flask + SQLite + PaddleOCR)。Task 1-3 已加 CLAHE + 类别门控 + PaddleOCR 调参。本任务把 shipping.py 的 2 个行级图接口接入门控,真正在生产路径上触发 CLAHE。

**本任务位置**: 7 个任务中的第四个。Task 5 复用本任务的接入模式(inbound/loading)。

## 全局约束(必须遵守)
- Python 3.12,不加新依赖
- 默认行为不变:record 不是褶皱品类 → 完全走原路径
- 异常处理:门控函数(`is_wrinkle_label_category`)的 DB 异常已内部处理,这里只需信任返回值
- 命名规范:`_xxx` 私有,`xxx` 公开
- 不动 conftest.py / requirements.txt / worklog.db

## 接口契约(来自 Task 1-3)

`blueprints/ocr_engine` 暴露:
- `is_wrinkle_label_category(product_name: Optional[str]) -> bool` — 任务门控
- `get_ocr_engine(name: str)` — 工厂函数,从模块级 import
- 引擎实例的 `extract_text(bytes, apply_wrinkle_enhance=False) -> str`
- 引擎实例的 `extract_text_with_conf(bytes, apply_wrinkle_enhance=False) -> Tuple[str, float]`

## 必须产出的修改

**`blueprints/shipping.py` 改 2 处**(都是对称的 3 行改动):

### 位置 A:行级图片上传异步处理 `_process_record_image_async`

在 `_process_record_image_async(image_id, filepath, record, order_id, record_id)` 函数内,**line 100 附近**(已有 `get_ocr_engine('paddleocr').extract_text_with_conf(_f.read())` 调用),在调 OCR **之前**加:

```python
apply_wrinkle_enhance = is_wrinkle_label_category(record.get('product_name', ''))
```

然后修改 `_ocr_text, avg_conf = get_ocr_engine('paddleocr').extract_text_with_conf(_f.read())` 为:

```python
_ocr_text, avg_conf = get_ocr_engine('paddleocr').extract_text_with_conf(
    _f.read(), apply_wrinkle_enhance=apply_wrinkle_enhance)
```

注意 `record` 已经传进函数(参数之一),不需要额外查 db。

### 位置 B:`ai-judge` 端点的 re-OCR fallback

`api_v1_shipping_orders_image_ai_judge` 函数(line ~1008 起),**line 1033 附近**(已有 `ocr_text = get_ocr_engine('paddleocr').extract_text(_f.read()) or ''`),在 `record = ShippingRecord.get_by_id(record_pk)` 之后、调 OCR **之前**加:

```python
apply_wrinkle_enhance = is_wrinkle_label_category(record.get('product_name', ''))
```

然后修改 `ocr_text = get_ocr_engine('paddleocr').extract_text(_f.read()) or ''` 为:

```python
ocr_text = get_ocr_engine('paddleocr').extract_text(_f.read(), apply_wrinkle_enhance=apply_wrinkle_enhance) or ''
```

**注意**:ai-judge 端点的 OCR 仅在 `ocr_match_event` 没存 ocr_text 时触发(优先读 stored),但 fallback 路径仍需带 CLAHE 保证一致性。

## 必须产出的测试

**新建 `tests/test_shipping_wrinkle_routing.py`**:

用 Flask test_client + MagicMock,验证:
1. `test_row_image_upload_uses_clahe_for_wrinkle_category` — 上传行级图 + record 是「白磅布三文治」→ mock 的 extract_text_with_conf 被调用且 `apply_wrinkle_enhance=True`
2. `test_row_image_upload_skips_clahe_for_other_category` — 上传行级图 + record 是「无纺布」→ `apply_wrinkle_enhance=False`

**关键 patch 路径**:`patch('blueprints.shipping.get_ocr_engine')`(因为 shipping.py 已经 `from blueprints.ocr_engine import get_ocr_engine`,patch 源模块的 get_ocr_engine 没用 — `from X import Y` 在 importer 命名空间创建独立绑定)。实证:`tests/test_record_upload_match.py:36` 用同样的 patch 路径工作。所有三个蓝图(shipping/inbound/loading)都从 `blueprints.ocr_engine` import `get_ocr_engine`,所以 patch 路径必须是 `blueprints.<blueprint_module>.get_ocr_engine`。

测试需要 `from app import create_app` 用 test_client,`with self.client.session_transaction() as sess: sess['operator_id'] = 1` 设登录态。

## 步骤

### Step 1: 写失败测试

新建 `tests/test_shipping_wrinkle_routing.py`(见上方测试要求),2 个测试。

### Step 2: 跑测试,验证失败

```bash
cd C:\Users\Administrator\worklog-app
python -m pytest tests/test_shipping_wrinkle_routing.py -v
```

预期:2 个失败(`apply_wrinkle_enhance` 没传,kwargs 检查失败)。

### Step 3: 修改 shipping.py

按上方"必须产出的修改"两处修改。import 已存在的 `is_wrinkle_label_category` 即可:

```python
from blueprints.ocr_engine import is_wrinkle_label_category
```

如果已有 `from blueprints.ocr_engine import ...` 在文件顶部,直接在那个 import 行加这个函数名即可(避免重复 import 行)。

### Step 4: 跑测试,验证通过

```bash
cd C:\Users\Administrator\worklog-app
python -m pytest tests/test_shipping_wrinkle_routing.py -v
```

预期:2/2 PASS

### Step 5: 跑全套,确保没回归

```bash
cd C:\Users\Administrator\worklog-app
python -m pytest tests/test_clahe_preprocessing.py tests/test_wrinkle_label_category.py tests/test_paddleocr_wrinkle_param.py tests/test_shipping_wrinkle_routing.py -v
```

预期:7 + 6 + 8 + 2 = 23 全过

### Step 6: Commit

```bash
cd C:\Users\Administrator\worklog-app
git add blueprints/shipping.py tests/test_shipping_wrinkle_routing.py
git commit -m "feat(shipping): 行级图接口接 CLAHE 类别门控

- 行级图片上传 + AI 判别按钮两处对称接入门控
- 调用前 record.product_name → is_wrinkle_label_category()
- apply_wrinkle_enhance=True 时走 CLAHE 预处理 + 褶皱 OCR
- 测试覆盖:磅布三文治触发 + 无纺布不触发"
```

## 报告

完成后写报告到:`.superpowers/sdd/2026-08-16-ocr-wrinkle-label-preprocessing/task-4-report.md`

报告内容(短):
- 状态:DONE / DONE_WITH_CONCERNS / NEEDS_CONTEXT / BLOCKED
- commits:base `32bb10a` 到 HEAD
- 测试摘要:2/2 PASS
- 任何关切(可选):找具体行号的难度、调用方理解等

## 自查清单

- [ ] shipping.py 的 2 处都加了 `apply_wrinkle_enhance = is_wrinkle_label_category(record.get('product_name', ''))`
- [ ] 2 处 OCR 调用都传了 `apply_wrinkle_enhance=apply_wrinkle_enhance`
- [ ] import 行不重复(合并到现有 from ... import 行)
- [ ] 2 个测试全过
- [ ] 既有测试(7 + 6 + 8)无回归
- [ ] commit message 与 plan 一致

## 注意

- 工作目录:`C:\Users\Administrator\worklog-app`(主分支)
- **shipping.py 有 1577 行**,先用 grep 找调用点,别盲改
- `from blueprints.ocr_engine import ...` 已在 line 31,合并到那里
- PowerShell 中文编码:用 `python -X utf8`
- 不触碰 worklog.db

## 可参考的代码上下文

- `blueprints/shipping.py:31` — 现有 `from blueprints.ocr_engine import PaddleOCREngine, get_ocr_engine, OCR_MATCH_PROMPT_VERSION`
- `blueprints/shipping.py:85-100` — `_process_record_image_async` 函数开头
- `blueprints/shipping.py:206` — `_run_label_match`(本地 fallback,不需要改)
- `blueprints/shipping.py:967` — fuzzy-match 端点 fallback re-OCR(不在本任务范围)
- `blueprints/shipping.py:1033` — ai-judge 端点 fallback re-OCR(本任务改)
- `blueprints/shipping.py:1644` — ai-match 整单比对(不在本任务范围,deferred minor)

## 不要改

- 不要触碰 `shipping.py:967` 的 fuzzy-match fallback(优先级读 stored,生产很少触发)
- 不要触碰 `shipping.py:1644` 的 ai-match 整单比对(deferred minor)
- 不要触碰 `_run_label_match`(共享 record 信息,已通过传入的 ocr_text 继承 CLAHE 效果)
- 不要触碰任何非行级图相关的 OCR 调用