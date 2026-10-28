# 误删图片恢复工具箱（2026-10-28 事故沉淀）

事故：`upload\2026-10` 整个目录被误删（3,801 个文件，1,655 个仍在 DB 被引用）。
元数据四条途径全断（详见 `CLAUDE.md` 踩坑点 11），最终靠**空闲空间簇雕刻 + OCR 文本反查**恢复了 251 张。

这套脚本是那次事故的产物，**下次再发生可以直接用**。依赖：`pytsk3`（`pip install pytsk3`）+ PaddleOCR（项目已带）。

## 前提（很重要）

1. **磁盘被写过的越多，能捞回的越少**。删除后每一次写入（日志、备份、编译）都可能覆盖待恢复的簇。
   → **出事后第一件事：停掉所有写 D: 的进程**（本项目 = Flask 服务 + 任何备份任务）。
2. **元数据一旦被复用就找不回来**。$MFT 记录被后续写入复用后，文件名与簇链都没了 → 只能靠雕刻 + 特征识别。

## 用法（按顺序）

```powershell
# 0) 依赖
pip install pytsk3

# 1) 盘点：DB 引用但磁盘缺失的图片（确认损失面 + 找 OCR 原文参考集）
python tools\recovery\assess_missing.py     # 需自备: 见下"附:一次性脚本"

# 2) 定向雕刻：只捞"手机标签照尺寸族"的 JPEG（不要求文件完整，尾部被覆盖也要）
python tools\recovery\carve_family.py
#    → E:\carve_family\  (7,524 张 / 85GB 空闲空间 / 约 10 分钟)
#    想全量雕刻(不限尺寸): carve_sample.py

# 3) OCR 识别 + 文本反查（4 分片并行，约 1.5 小时）
#    每张图的 OCR 文本都会落盘 all_ocr_shard*.jsonl → 后续换参考集重匹配不用再跑 OCR
for %i in (0 1 2 3) do start /b python tools\recovery\ocr_match5.py 0 %i 4

# 4) 离线重匹配（换阈值/换参考集，秒级）
python tools\recovery\re_match.py 0.30 0.07 0.0

# 5) 回填（自动档 ratio≥0.5，并自动补 JPEG 末尾 EOI 标记）
python tools\recovery\restore_final.py --dry-run   # 先看清单
python tools\recovery\restore_final.py --apply

# 6) 校验
python tools\recovery\verify_files.py       # 严格解码 + 母版一致性
python tools\recovery\verify_restored.py    # 用 DB 明细的品名/规格交叉验证
```

## 各脚本作用

| 脚本 | 作用 |
|---|---|
| `carve_family.py` | 扫空闲空间，按 JPEG SOF 段尺寸挑出"标签照尺寸族"，不要求 EOI 完整 |
| `carve_sample.py` | 全量雕刻（不限尺寸，要求 EOI 完整 + 宽 ≥1000px） |
| `ocr_match5.py` | PaddleOCR + 二元组覆盖快筛 + difflib 精算，双层参考集（OCR 原文 / 明细品名规格），**落盘全部 OCR 文本** |
| `re_match.py` | 离线重匹配（读 OCR 文本 dump，不再跑 OCR） |
| `restore_final.py` | 按 ratio 分级回填：≥0.5 自动写回原路径，0.25~0.5 出人工复核清单，<0.25 丢弃；写回时补 EOI |
| `verify_files.py` | 严格解码校验（不开容错）+ 母版副本一致性 |
| `verify_restored.py` | 用 DB 里该图所属**明细的品名/规格**做独立交叉验证 |
| `review_sheet.py` | 给人工复核清单生成缩略图墙（带编号，便于肉眼对号） |

## 关键经验

- **认领靠 OCR 文本**：DB 的 `ocr_match_event.ocr_text` 存着每张图当初的 OCR 原文，是唯一的"指纹"。
  777 张断链图有原文 → 正是靠它把无主图片认回原文件名。
- **同一段 OCR 文本可能对应多个 image_id**（不同订单拍了同一张标签）→ 要按"文本组"分配，不能一对一。
- **残缺 JPEG 也要救**：PIL 开 `LOAD_TRUNCATED_IMAGES` 能读出上半段，标签文字通常还在 → OCR 仍可识别。
  多数被覆盖的图只是**丢了末尾的 EOI 标记**，图像数据基本完好 → 补回 `FFD9` 即成合规 JPEG。
- **误配不等于恢复错**：回填的图内容与它自己 DB 里的 OCR 原文一致；若它指向的明细规格对不上，
  那是**应用自己的 AI 比对**当时就匹配错了，不是恢复环节的问题（本次 251 张里有 22% 属此类）。
