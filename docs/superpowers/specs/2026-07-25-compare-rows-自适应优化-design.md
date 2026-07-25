# compare_rows 自适应优化 — 设计文档

- 日期：2026-07-25
- 页面：`/shipping-records` → `POST .../<order_id>/ai-match`
- 路线图定位：阶段 2「AI 验数 + 校对」的持续改进
- 状态：待用户 review

## 1. 背景与目标

### 现状

当前 `DeepSeekEngine.compare_rows(ocr_text, rows)` 的工作方式：

```
标签照片 → PaddleOCR 提字 → DeepSeek v4-flash 语义比对 → 逐行绿/黄/红
```

比对规则（`COMPARE_PROMPT`）是写死的：
- 杂胶/环保等价（7P/15P/18P/21P ≡ 环保）
- 颜色匹配（规格含"黑色" + OCR 含"黑" → 匹配）
- 厚度匹配（规格含"3mm" + OCR 含"3mm" → 匹配）
- 加面/单面冲突检测

### 问题

当前 12 张已比对图片：6 绿 / 1 黄 / **5 红全部已被用户人工确认**——说明 AI 判红过严，这 5 条其实是匹配的。它们集中在**杂胶**品类：

| image_id | 品名 | 规格 | RapidFuzz 分数 | AI 判 | 人工 |
|---|---|---|---|---|---|
| 1994 | 7P环保杂胶 | 0.8单面加面 | 47.8 | red | ✓ |
| 1995 | 7P环保杂胶 | 0.8单面加面 | 47.8 | red | ✓ |
| 1998 | 7P环保杂胶 | 0.8单面加面 | 47.8 | red | ✓ |
| 2038 | 7P环保杂胶 | 1.0中性加面 | 50.3 | red | ✓ |
| 1964 | 无纺布 A料 | A1.5m 200g | 47.8 | red | ✓ |

——**AI 认为不匹配的，人工认为匹配。需要让 AI 学习人工判断标准。**

### 目标

利用人工确认标记（`human_verified=1`）的数据，自适应优化同品类商品的 compare_rows 判断，减少红误报。

### 非目标（明确排除）

- 不发图片给视觉模型（现有 DeepSeek v4-flash 文字模型足够）
- 不自动修正 RapidFuzz 阈值配置
- 不改 `match_label_to_row` 本地匹配逻辑
- 不碰 Moonshot / PaddleOCR 引擎

## 2. 核心机制

两层改进：

**层 1：Few-shot prompt 注入**
- 取被比对的 record 的品名 → 分类到品类（level 3）
- 查该品类的 `human_verified=1` 记录，取最多 3 条作为"参考案例"
- 将参考案例 append 到 `COMPARE_PROMPT` 末尾，让 AI 参照人工确认的案例标准做判断

**层 2：后处理松弛**
- 如果 DeepSeek 仍然判 `red`，但该品类有 `≥1` 条人工确认记录 → 降级为 `yellow`
- 作用：兜底保护——同品类之前有人工确认的匹配案例，说明 AI 对这个品类偏严
- 降级到 `yellow` 而非 `green` → 保留人工 review 的提醒作用
- **不依赖 `match_score`**：`compare_rows()` 不需要查图片分数，仅凭品类 hv 状态判断

### 判定流程

```
compare_rows(ocr_text, rows)
    │
    ├─ classify(product_name) → category
    ├─ get_hv_cases(category) → case_list
    │
    ├─ if cases: append few_shot(cases) to COMPARE_PROMPT
    │
    ├─ DeepSeek API call
    │
    └─ for each result:
         if status=='red' AND category_has_hv:
              status = 'yellow'
```

## 3. 品类分类

### 分类函数 `_classify_product(product_name)`

```
1. 查 product 表：
   SELECT p.product_name, pc.category_code, pc.id
   FROM product p
   JOIN product_categories pc ON p.category_id = pc.id
   WHERE p.product_name = ?
   → 取 category_id → 向上遍历 parent_id 到 level=3
   → 返回 category_code（如"0201"）

2. 如果 product 表无匹配 → CLASS_KEYWORDS 兜底
   → 子串匹配 product_name 中的关键词 → 返回 category_code
```

### 兜底关键词表 `CLASS_FALLBACK_KEYWORDS`

手动维护 11 个大类（覆盖 95%+ 的 shipping_records 品名）：

```python
CLASS_FALLBACK_KEYWORDS = [
    ('0201', ['杂胶']),        # 含"杂胶" → 0201 杂胶
    ('0301', ['纯胶']),
    ('0401', ['回力胶', 'EVA']),
    ('0204', ['无纺布', 'A料', 'B料']),
    ('0701', ['鱼鳞布', 'HA']),
    ('0208', ['潜水胶']),
    ('0601', ['PE板', 'PE']),
    ('0501', ['不织布']),
    ('0205', ['路华里']),
    ('0206', ['路华里']),
    ('0302', ['热熔胶']),
]
```

匹配规则：按列表顺序，第一组关键词在 product_name 中出现的即命中。

> 兜底表可在以后扩展。未命中的品名返回 `None`，走标准 prompt（无注入）。

## 4. Few-shot 参数

| 参数 | 值 | 理由 |
|---|---|---|
| 触发门槛 | 品类 ≥ 1 条 hv 记录 | 立刻生效 |
| 注入上限 | max 3 条 | 控制 prompt 长度 |
| 案例排序 | 最新优先（按 si.id DESC） | 最近的案例最有代表性 |
| 注入位置 | `COMPARE_PROMPT` 末尾 | 不干扰现有规则语义 |

### Few-shot 注入格式

```
### 参考案例：该品类此前已人工确认的匹配

以下标签照片此前经人工核查，确认与录入明细匹配。
请在当前判断中参考这些案例的匹配标准：

1. 品名"7P环保杂胶" + 规格"0.8mm单面加面"
   OCR 文字中包含 "7P", "环保", "加面", "0.8mm" 等关键词
   → 匹配 ✓（人工确认）

2. 品名"7P环保杂胶" + 规格"1.0mm中性加面"
   OCR 文字中包含 "7P", "杂胶", "加面", "1.0mm" 等关键词
   → 匹配 ✓（人工确认）

请用与上述案例一致的宽松度判断当前行。
```

### 后处理松弛阈值

| 捕获条件 | 动作 |
|---|---|
| DeepSeek 判 `red` + 品类有 `≥1` 条 hv 案例 | `red → yellow` |
| 其他 | 保持原判 |

## 5. 数据依赖

### 查询人工确认案例 `_get_hv_cases(category_code)`

```sql
SELECT sr.product_name, sr.specification,
       si.match_score, si.human_verified
FROM shipping_images si
JOIN shipping_records sr ON si.record_pk = sr.id
WHERE si.human_verified = 1
  AND sr.product_name IN (
    -- 该品类下所有已知品名(从 product 表 + 从 CLASS_FALLBACK_KEYWORDS)
    ...
  )
ORDER BY si.id DESC
LIMIT 3
```

> 性能：单次查询 ~5-10ms（shipping_images 仅 474 行，shipping_records 409 行）

### 不需要落盘

- 不掉 DB 表（不持久化分类结果）
- 不再新增字段
- 工作方式：每次 `compare_rows()` 调用时实时查 DB + 实时分类

## 6. 改动清单

| # | 文件 | 改动 | 类型 |
|---|---|---|---|
| 1 | `blueprints/ocr_engine.py` | 新增 `CLASS_FALLBACK_KEYWORDS` 常量 | 配置 |
| 2 | `blueprints/ocr_engine.py` | 新增 `_classify_product(name) → str\|None` | Python |
| 3 | `blueprints/ocr_engine.py` | 新增 `_get_hv_cases(category) → list` | Python |
| 4 | `blueprints/ocr_engine.py` | 新增 `_build_few_shot(cases) → str` | Python |
| 5 | `blueprints/ocr_engine.py` | 修改 `compare_rows()` — 注入 + 松弛 | Python |

**无**模板/API/DB schema 改动。总数 ~100 行 Python。

## 7. 边界情况

| 场景 | 行为 |
|---|---|
| 品名不在 product 表 + 不在关键词表 | 返回 `None`，不注入 prompt，走标准逻辑 |
| 品类有 hv 案例但 match_score 不存在（旧记录） | 后处理松弛跳过该行（不变） |
| 品类有 10+ 条 hv 案例 | 取最新 3 条，prompt 不膨胀 |
| 同一品类多次调用 compare_rows | 每次实时查 DB（无缓存），案例列表实时更新 |
| DeepSeek API 调用失败（超时/429） | 后处理松弛不执行（无 status 可判断） |
| `rows` 参数为空列表 | 标准错误处理不变，不触发分类/注入 |

## 8. 效果预期

以当前数据估计：

| 阶段 | 预期 |
|---|---|
| 当前 | 杂胶品类 0 次参照（无历史数据可用） |
| 杂胶积累 ≥1 条 hv 后 | 下次杂胶比对时注入案例 → AI 判断更宽松 |
| 其他品类（纯胶/回力胶等）| 无 hv 记录 → 走标准 prompt（不影响） |
| 长期 | hv 记录跨品类扩散 → 各品类都有自适应的判断标准 |

## 9. 测试策略

### 单元测试（可自动化）

| # | 测试 | 方法 |
|---|---|---|
| 1 | `_classify_product("7P环保杂胶") → "0201"` | mock product 表查询 |
| 2 | `_classify_product("未知品名XYZ") → None` | 无匹配场景 |
| 3 | `_get_hv_cases("0201")` 返回 5 条中的最新 3 条 | 真实 DB 查询 |
| 4 | `_build_few_shot(cases)` 格式正确性 | 字符串断言 |
| 5 | 后处理松弛：red + hv + score=48 → yellow | mock DeepSeek 返回 |
| 6 | 后处理松弛：red + 无 hv → 保持 red | mock |
| 7 | 后处理松弛：red + hv + score=40（<45） → 保持 red | 阈值下限 |

### E2E 测试（手动）

1. 创建杂胶 + 加面的测试明细
2. 拍实物标签，上传为行级图片 → 调用 `compare_rows`
3. 手动确认该图片（`human_verified=1`）
4. **再次上传**同品类的另一张标签图片 → 调用 `compare_rows`
5. 对比两次结果的差异——第二次应该更偏向 yellow 而非 red

## 10. 后续扩展（不在本期）

- 品类内按规格区间细分（"加面" vs "不加面"子群）
- 定时离线分析 hv 数据 → 生成永久 prompt 规则更新
- 统计面板："哪个品类误报率最高"
- 跨品类迁移学习（A 品类的判断标准能否帮到 B 品类）