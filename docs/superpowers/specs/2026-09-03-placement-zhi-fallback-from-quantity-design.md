# placement 期望值兜底(unit='支' + 备注无支数 → 用 quantity 核对)

> 状态: 设计稿,待用户审阅
> 日期: 2026-09-03
> 范围: 出货/入库/装柜三套订单的 placement_match 路径

## 背景

`/shipping-records` 页面 placement_match 判定(`blueprints/shipping.py:_placement_match_for_image` + 同位置 `_pm` 内联计算 + `placement_groups` 渲染):

- `expected_zhi = sum(int(m.group(1)) for m in re.finditer(r'(\d+)\s*支', remark))` —— 仅从备注解析
- `has_zhi = bool(expected_zhi > 0)`(实际是 `len(matches) > 0`)
- `matched_zhi = (not has_zhi) or (total == expected_zhi)`

**问题场景**: 商品 `unit='支'`、数量 33 支、备注为空(用户已经知道数量就是支数,没必要在备注里再写一遍「33支」)。当前逻辑下 has_zhi=False → matched_zhi=True → placement_match 只看散码,支数永远不参与核对;点数按钮永远不变绿,即便用户点满 33 支也无法确认核对成功。

## 设计目标

- unit='支' + 备注无「X支」 → **直接把 quantity 当 expected_zhi 兜底**,点数按 quantity 核对
- 其他单位(unit='y'/'码'/'kg'/'m'/空) → 保持原行为(只有备注有「X支」才能比对)
- 数值比较改用 0.01 浮点容差(兼容 quantity='33.5' 这种小数支数)
- 三套订单(出货/入库/装柜)同步改,共享同一个 helper 函数,杜绝行为漂移

## 核心规则

每条 record 的 `expected_zhi` / `has_zhi` 在 placement 路径下按以下口径:

```
1. remark 解析:  zhi_m = re.finditer(r'(\d+)\s*支', remark)
                  remark_zhi = sum(int(m.group(1)) for m in zhi_m)
                  has_remark_zhi = len(zhi_m) > 0

2. 兜底:  若 not has_remark_zhi 且 unit == '支':
              qty_val = float(quantity or 0)  # 失败视为 0,不走兜底
              if qty_val > 0:
                  expected_zhi = qty_val
                  has_zhi = True
              else:
                  expected_zhi = 0
                  has_zhi = False
          否则:
              expected_zhi = remark_zhi  # 沿用原值
              has_zhi = has_remark_zhi
```

**比较容差**: `matched_zhi = (not has_zhi) or abs(total - expected_zhi) <= 0.01`
(原来是 `==` 严格相等;quantity 含小数(33.5)时永远 False,改容差后兼容整数/小数两种。)

**placement_match 最终判定**(不变):
```
matched_zhi = (not has_zhi) or abs(total - expected_zhi) <= 0.01
matched_san = (not has_san) or abs(loose - expected_sanma) <= 0.01
placement_match = (has_zhi or has_san) and matched_zhi and matched_san
```

## 实现

### 新增共享函数

在 `blueprints/_helpers.py` 增加 `compute_placement_expected_zhi`:

```python
def compute_placement_expected_zhi(remark, quantity_str, unit):
    """按 record 计算 placement 期望支数与是否有支目标。

    规则(2026-09-03):
      - 先按 remark 解析「X支」之和(has_remark_zhi)。
      - 若 has_remark_zhi=False 且 unit == '支':
          用 float(quantity_str) 兜底作为 expected_zhi, has_zhi=True。
        否则:沿用 remark 解析结果。

    Returns:
        (expected_zhi: float, has_zhi: bool)
        整数 expected_zhi 仍以 float 返回(如 33.0),便于跨订单统一比较。
        模板渲染如需去小数点,显示前自行 `if expected_zhi == int(expected_zhi)` 处理。
    """
    remark = remark or ''
    qty_str = (quantity_str or '').strip()

    zhi_m = list(re.finditer(r'(\d+)\s*支', remark))
    remark_zhi = sum(int(x.group(1)) for x in zhi_m)
    has_remark_zhi = len(zhi_m) > 0

    if not has_remark_zhi and (unit or '').strip() == '支':
        try:
            qty_val = float(qty_str)
        except (ValueError, TypeError):
            qty_val = 0.0
        if qty_val > 0:
            return qty_val, True
        return 0.0, False

    return float(remark_zhi), has_remark_zhi
```

### 三套订单的 8 处替换点

| 文件 | 位置 | 现状 | 改法 |
|---|---|---|---|
| `blueprints/shipping.py` | 路由渲染 `_pm`(L329 附近) | 内联 `_exp_zhi/_has_zhi/_matched_zhi` | `_exp_zhi, _has_zhi = compute_placement_expected_zhi(_remark, _qty, _unit)`,`_matched_zhi = (not _has_zhi) or abs(_total - _exp_zhi) <= 0.01` |
| `blueprints/shipping.py` | `placement_groups` 渲染(L207 附近) | 同上 | 同上 |
| `blueprints/shipping.py` | `_placement_match_for_image`(L1317) | 同上 | 同上 |
| `blueprints/inbound.py` | `_pm` 计算(L192-211) | 同上 | 同上 |
| `blueprints/inbound.py` | `placement_groups` 渲染(L221 附近) | 同上 | 同上 |
| `blueprints/inbound.py` | `_inbound_placement_match_for_image`(L627) | 同上 | 同上 |
| `blueprints/loading.py` | `_pm` 计算(L170-191) | 同上 | 同上 |
| `blueprints/loading.py` | `placement_groups` 渲染(L228 附近) | 同上 | 同上 |
| `blueprints/loading.py` | `_loading_placement_match_for_image`(L1349) | 同上 | 同上 |

### 模板渲染兼容性

模板 `{{ _item.expected_zhi }}` 显示该值。Jinja 默认对 float `33.0` 输出 `33.0`,旧逻辑是 int `33` 显示成 `33`,相差一个 `.0`,影响外观。

**最终决定**:
- helper 内部计算全程用 float
- helper 返回前:`if expected_zhi == int(expected_zhi): expected_zhi = float(int(expected_zhi))` —— 整数期望值仍以 `33.0` 形式返回,但数值本身是 `33.0`(整数),与 `33.5` 区分
- 模板渲染时由 Jinja 决定显示策略:
  - **方案 A**(推荐): 模板用 `{{ '%g' % _item.expected_zhi }}` —— 整数显示 `33`,小数显示 `33.5`,Jinja 内置格式符
  - **方案 B**: 保留 `33.0` 显示,接受外观变化
- 计算逻辑不受影响:`abs(total - 33.0) == abs(total - 33)`,比较正确

> 待实现阶段选定 Jinja 渲染策略(方案 A 或 B)。两种都不影响核对逻辑。

## 数据流

```
record {remark, quantity, unit}
    ↓ compute_placement_expected_zhi()
(exp_zhi: float, has_zhi: bool)
    ↓ 比较 total / loose
placement_match: bool
    ↓ 写回 image / record
前端 data-expected-zhi / data-has-zhi 渲染徽章
```

## 不在范围

- **辅助单位提示列** `calc_hint()` 显示不动 —— 用户已明确只改 placement 路径
- **YPP 核查页** `/shipping-ypp-review` 不动 —— 那条路径只看备注 + YPP 规则,不看 quantity
- **严重程度着色** 不动 —— 当前 placement_match 只返回 bool,没有 info/warn 区分

## 测试

新增 `tests/test_placement_zhi_fallback.py`(7 条):

1. unit='支', quantity=33, remark='', total=33 → match
2. unit='支', quantity=33, remark='', total=30 → no match(差 3 支)
3. unit='支', quantity=33.5, remark='', total=34 → match(0.01 容差)
4. unit='支', quantity=33.5, remark='', total=33 → no match(差 0.5)
5. unit='支', quantity='', remark='10支', total=10 → 走原 remark 路径(用 10 而非空 quantity)
6. unit='y', quantity='500', remark='', total=33 → no match(非支,无目标)
7. unit='', quantity='10', remark='', total=10 → no match(unit 不等于 '支',跳过)

## 风险与回滚

- **风险 1**: 模板 `{{ _item.expected_zhi }}` 显示 `33.0` 而非 `33`。需要 Jinja filter 处理,影响美观不影响逻辑。
- **风险 2**: 三套订单行为同步改,如果某条订单习惯不同(比如装柜某类商品 unit 字段就未规范)会突然触发新匹配。需在测试覆盖「unit 字段为空」「unit 为 None」两种边界。
- **回滚**: helper 函数是新加,函数未引用 = 旧行为;helper 引用但旧值不匹配 = 行为变更。回滚时只需恢复 8 处内联代码 + 删除 helper 调用,git revert 即可。

## 验证清单

- [ ] `python -m pytest tests/test_placement_zhi_fallback.py -v` 全过
- [ ] `python -m pytest tests/ -v` 全过(防止破坏现有 84 个测试)
- [ ] 启动开发服务器,手动测:创建一条 unit='支'/quantity=33/remark='' 的出货明细,上传 placement 图,标记 33 个点 → 「点数」按钮变绿;改标 30 个 → 按钮不变绿
- [ ] 移动端 `/m/shipping-today/order/<oid>/placement/<record_id>` 路径同上验证

## 参考

- `blueprints/_helpers.py` 的 `calc_hint()` / `check_remark()` / `summarize_remarks()`(类似的 unit='支' 兜底模式参考)
- `blueprints/ocr_pipeline.py:RecordImageProcessor`(共享层模式参考,三套订单共用)
- `static/js/placement_count.js:251-262`(前端徽章逻辑,无需改动)
- `docs/superpowers/specs/2026-08-04-match-col-virtual-design.md`(类似「服务端不再渲染某列,改 JS 端处理」模式参考)