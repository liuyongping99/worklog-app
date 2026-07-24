"""prompt 回归测试包

用于:
- COMPARE_PROMPT 等 LLM 提示词的迭代验证
- 通过固定 OCR fixture + 预期 verdict,实现"改提示词 → 一键跑回归"
- 当前 baseline: order 575(5 条 record,含 3 条 7P 环保杂胶 + 2 条无纺布反例)
"""