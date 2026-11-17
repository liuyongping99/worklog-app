# 丰源工作台项目指南

此文件为 Claude Code (claude.ai/code) 在本仓库中工作时提供指导。

> **数据快照 = 2026-11-06**。本文件所有行数 / 文件数 / 数据行数均为该日实测值。
>
> **局部更新 = 2026-11-02**：车辆维护记录改字段 + 挂图片一节（§26）涉及的条目已重新实测并同步（表数 38、路由 250/252、`models/basic.py` / `blueprints/basic_records.py` / `templates/vehicle-maintenance.html` 行数、测试与工具脚本计数）。**其余条目仍是 2026-10-28 的快照**，与当前代码已有漂移（出货/入库/装柜侧的在改内容未纳入本次对账）。
>
> **局部更新 = 2026-11-08**：消防检查记录挂图片一节（§27）涉及的条目已重新实测并同步（表数 40、`models/fire_check.py` 197 行 / `FireCheckImage`、`blueprints/basic_records.py` 298 行 + 2 个端点、`templates/fire-checks.html` 612 行、测试 `tests/test_fire_check_images.py` 12 用例）。
>
> **工作目录 = `D:\worklog-app`**（唯一真实目录）。数据库备份统一走 `D:\BAK\`，不再用项目内的 `_safe-snapshot`。
>
> 数字会随开发漂移；判断"文档是否与代码一致"时以实测为准，刷新方式见「路由结构」章节的复核命令。

## 项目概述

丰源工作台是一个基于 Flask 的工作日志与订单管理系统，已从「事后记账」演进为「任务流实时管控 + 证据链」系统。核心能力：

- **工作日志**：工作经验、错误经验、待办事项、公司通知、车辆维护
- **订单管理**：出货 / 入库 / 装柜三套平行订单（同一套架构三份实现，端点同构、主键隔离）。一条订单的完整生命周期：
  - **a. 订单来源 —— 单据 OCR + LLM 结构化导入**：装车单 / 同价调拨单等**纸质或截图单据** → PaddleOCR 抽文本 → DeepSeek / MiniMax 等大模型结构化为 JSON → 落 `*_records` 明细行（品名 / 规格 / 单位 / 数量 / 备注）。明细**主要通过智能添加弹框的 OCR + LLM 路径导入**（4 引擎可选：`moonshot` / `paddleocr+minimax组合` / `paddleocr+deepseek组合` / `paddleocr纯本地`，详见 §3），手敲为辅助；OCR 原文与 AI 原始返回存 `ocr_match_event.ocr_text` / `ai_raw_response`，AI 比对相似度存 `ai_match_score`，供人工复核。
  - **b. 商品行每行两类图片 —— 行级图 + 摆放图**：每个商品明细行可挂 **行级图**（`source='upload'` / `'ai'` / `'copy_paper_label'`，OCR + AI 比对或纯存档用）和 **摆放图**（`source='placement'`，清点支数用）。两类图**同表不同 source**，可同时存在、互不干扰。`copy_paper_label` 是不跑 OCR 的「免 AI 比对标签图」（详见 §e + §22），与 upload/ai 共存但走不同端点。行级图见 §6 + 「图片匹配流水线」，摆放图见 §21。
  - **c. 商品行级图 OCR → LLM 与数据库规格比对**：行级图上传后跑 PaddleOCR 取文本（部分商品标签不含颜色信息时还会抽取背景色），再交 DeepSeek / MiniMax 等大模型与该明细行的**数据库规格**（`product_name` / `specification`）做语义比对，产出 `match_status` 与 ✓⚠✗ 徽标；比对**只给结论、不改写品名/规格列**（页面规格永远以数据库为准）。判定源头由 `_helpers.check_remark` / `check_piece_mismatch` 驱动（同一套函数被 §f「导入即校验」复用，**与 YPP 无关**——YPP 是 §d 摆放图的支数换算）。
  - **d. 商品摆放图点数 → 与备注拆出的期望比对**：摆放图用于点击计数、清点散码、卸货取负，得出 `effective_zhi`（口径：`manual_count` 优先 → 回落 `n_marks` → `is_unload=1` 取负），再与**备注拆出的期望值**做比对：① 期望支数 `compute_placement_expected_zhi(remark, qty, unit)`（从备注拆 `X支*Yy` / `X支+Yy`；当 unit ∈ 支/令/张/桶 且备注无支数时**用 quantity 兜底**）；② 期望散码 `parse_loose_yards(remark)`（先抠掉乘法项再加散码）。任一不一致即 `placement_match=False`，前端按绿/红/黄徽章提示。
  - **e. 商品行免 AI 比对标签图（手写/不规则标签的逃生口）**：部分商品的标签是**不规则手写信息**（`product_categories` 0105=日本纸 / 0106=快巴纸 / 0107=拷贝纸 / 0108=**腊光纸**——注：DB 源品类标准叫「腊光纸」，但实际 `shipping_records` 4 行都用「**蜡**光纸」，代码关键字含两个字），既跑不了可靠 OCR，AI 比对必然误判 ✗。这类行改用**「免 AI 比对标签图」按钮**上传：图片正常存档（`source='copy_paper_label'`），但**跳过 OCR 抽取、跳过 LLM 比对**，页面上品名规格仍取自数据库（详见 §22）。
  - **f. 商品明细导入即校验（数量 ↔ 换算规则当场对账）**：明细一落库就按该行匹配到的换算规则做**数量校验**，当场算出期望值与 `quantity` 的偏差，不把问题留到事后：① **按 y（码）卖的商品** → 走 **YPP 规则**（`get_ypp` 取 `product_units.yards_per_piece`／100.0），备注里的 `X支*Yy` / `X支+Yy` 反解出期望码数与 `quantity` 比对；② **纸类按「件」收的商品** → 走**件数换算规则**（`get_piece_conversion` 取 `piece_conversions.units_per_piece`），备注里的 `X件` / `X件+Zs张` 反解出期望张数与 `quantity` 比对；备注用 `*` 显式写了单支/单件码数时以备注为准（`extract_pieces_from_remark` 的 `per_piece_override` 字段）。**不匹配的行立即打错误提示标志**（`mismatch` / `piece_mismatch`），偏差 ≤0.01 视为一致；单件/单支偏差 `info`（粉），多件/多支偏差 `warn`（红）。判定走 `_helpers.annotate_import_validation()`，标志**只在响应体里、不落库**，前端导入后立即显示（详见 §12）。
  - **g. 商品分类提示词**：商品标签上的名称 / 规格与系统里的品名 / 规格常有出入，故另有一套分类提示词系统，把原厂标签映射到本系统的商品名称与规格上；实际映射不上时可随时调提示词，交由 LLM 判别后直接匹配（详见「自适应提示词系统」）。

- **商品资料**：商品单位、商品分类（3 级树）、产品库（1000+ SKU）、件数换算规则
- **任务流**：状态机驱动的送货任务管理（准备中 → 已装货 → 已点数 → 已到达 → 已卸货 → 已完成）+ 退单/退货/作废
- **人员/车辆**：人员档案（6 种角色）、车辆档案、身份选择登录
- **AI/OCR**：五个引擎（Moonshot 云端视觉 / PaddleOCR 本地 / DeepSeek 结构化 / MiniMax 云端多模态 / PaddleOCR+MiniMax 复合）+ 明细行标签 OCR 匹配校验
- **移动端**：`/m/*` 免登录移动端页面（微信浏览器直达，含拍照 OCR + 摆放图点数）
- **工具**：独立点数（会话+拍照+标记+导出 CSV）、语音录入、综合查找

## 技术栈

- **后端**: Python 3.12 + Flask 3.1.3
- **数据库**: SQLite 3.45.3 (文件: `worklog.db`, WAL 模式)
- **前端**: 原生 JavaScript + Tailwind CSS（内联在模板 + `static/css/app.css`）
- **JS 模块**: `static/js/` 下 13 个文件（4,752 行）；`common.js`（151 行）提供公共工具；`product_row_utils.js`（641 行）**目前仅装柜页引入**（出货 / 入库各自内联复制了同名函数，见「模板设计约定」）；其余为专项模块（摆放图 / 免 AI 比对标签图 / 语音 / 独立点数 / 移动端）。各模板末尾另有 `<script>` 内联实现页面级交互
- **AI 集成**:
  - Moonshot Kimi k2.6 Vision API — 云端图片直接识别
  - PaddleOCR 3.x — 本地 CPU OCR 文字提取
  - DeepSeek v4 Flash API — OCR 文字结构化 + 明细行标签比对
- **OCR 架构**: ABC 基类 + 工厂模式（`blueprints/ocr_engine.py`），五引擎可切换
  - **2026-09-18 装车单 OCR 行合并修复**:装车单/出货单等紧凑表格(典型行距 8-12px),PaddleOCR 默认 clamp `[10,35]` 会把相邻 2 行合并为 1 行 → DeepSeek 串行错配。`_adaptive_y_threshold` clamp 已收紧到 **`[5, 20]`**(`blueprints/ocr_engine.py:1220` 起)。装车单等有表格线单据**不要**走 `KIND_FORM_NOLINES`(那是给无表格线磅布标签用的,固定 4 行,反而会把多行压成 4 段)。调试经验见 `~/.claude/projects/D--worklog-app/memory/ocr-threshold-tuning.md`。
- **模糊匹配**: RapidFuzz（本地 FuzzyWuzzy 替代，用于标签行匹配）
- **依赖**: 5 个直接依赖（Flask / openai / python-dotenv / rapidfuzz / Pillow）；rapidfuzz 为本地模糊匹配（标签行匹配），Pillow 为标签背景色检测（黑/白磅布三文治）

## 开发命令

```bash
# 安装依赖
pip install -r requirements.txt

# 启动开发服务器
python app.py

# 运行测试
python -m pytest tests/ -v

# 服务器运行在 http://127.0.0.1:5050，已启用 debug 模式
```

## 环境变量（`.env`，模板见 `.env.example`）

| 变量                                   | 用途                                                                                                          | 未配置时                                                 |
| ------------------------------------ | ----------------------------------------------------------------------------------------------------------- | ---------------------------------------------------- |
| `SECRET_KEY`                         | Flask session 签名密钥                                                                                             | 回落 dev 默认值 `worklog-dev-secret-change-me`（**上线必改**） |
| `MOONSHOT_API_KEY`                   | Moonshot kimi-k2.6 云端视觉                                                                                     | 该引擎不可用                                               |
| `DEEPSEEK_API_KEY`                   | DeepSeek v4-flash 结构化 + `ai-match` 比对                                                                       | 比对整体不可用                                              |
| `MINIMAX_API_KEY`                    | **MiniMax-M3**：`minimax` 引擎 + **默认的 `paddleocr_minimax` 复合引擎**（2026-10-04）。端点 `https://api.minimaxi.com/v1` | `paddleocr_minimax` 本地直接判失败（不发请求）并自动降级 DeepSeek，功能不挂 |
| `BAIDU_API_KEY` / `BAIDU_SECRET_KEY` | 语音录入第一档                                                                                                     | 降级 LLM → 本地 fuzzy                                    |
| `OCR_BACKEND`                        | `ai-recognize` 端点 `engine` 字段为空时的默认引擎（5 选 1）                                                                | 默认 `moonshot`                                        |
| `OCR_LOG_LEVEL`                      | OCR/AI 日志详细度（`DEBUG` 记完整 prompt + 原始返回）                                                                     | 默认 `INFO`（不记完整 prompt / 原始返回）                    |


## 目录结构

```
worklog-app/
├── app.py                  # 应用入口（204 行，工厂模式 + 登录闸门 + 19 个 Blueprint 实例注册）
├── models/                 # 数据模型包（18 个 .py，39 张业务表）
│   ├── __init__.py         # 统一 re-export（46 行）
│   ├── _db.py              # get_db() + DB_PATH（22 行）
│   ├── _init.py            # init_db() 建表 + 迁移（1,092 行）
│   ├── _permissions.py     # 权限系统：Action 枚举（13 个）+ can() 集中校验（100 行）
│   ├── basic.py            # WorkLog / ErrorLog / TodoItem / VehicleMaintenance（134 行）
│   ├── fire_check.py       # FireCheck 消防检查记录 + FireCheckImage 记录图片：日期/部位/检查事项/遗留事项/状态（197 行）★
│   ├── notice.py           # Notice / NoticeImage（144 行）
│   ├── orders.py           # 三套订单 9 模型 + UnifiedSearch + OcrMatchEvent + PlacementImage / InboundPlacementImage / LoadingPlacementImage（3,096 行，最大文件）
│   ├── stock.py            # StockOutItem（40 行）
│   ├── products.py         # ProductUnit / ProductCategory / Product（421 行）
│   ├── piece_conversion.py # PieceConversion 件数换算规则（101 行）
│   ├── point_count.py      # PointCountSession / PointCountImage / PointCountMark + 文件白名单删除（416 行）★
│   ├── tasks_flow.py       # Staff/Task/TaskItem/TaskImage/TaskEvent（492 行）
│   ├── voice_mapping.py    # VoicePhraseMapping 语音短语映射（139 行）★
│   ├── audit.py            # AuditLog（58 行）
│   ├── audit_query.py      # OcrEventAudit：OCR 事件审计只读查询（273 行）
│   ├── category_prompt.py  # CategoryPrompt + classify_record：自适应提示词（647 行）
│   ├── inventory.py        # ⭐ 库存 7 模型：Warehouse / ProductStock / StockLedger / StockPosting / StockPostingSkip / StockCount / StockCountItem（648 行，2026-11-16）
│   └── daily_check.py      # DailyOrderCheck 日终盘点（手填当日单据总量）（95 行）★
├── blueprints/             # 27 个文件（18 个蓝图文件 → 19 个 Blueprint 实例 + 8 个辅助模块 + 包标记），共 15,845 行
│   ├── __init__.py         # 包标记（1 行）
│   ├── _helpers.py         # 辅助：图片校验、YPP 匹配、支数换算、备注校验、导入即校验（annotate_import_validation）、compute_placement_expected_zhi、compute_daily_check_status、apply_user_rotation（1,451 行）
│   ├── ocr_engine.py       # 辅助：OCR 引擎抽象层五引擎（4,065 行，全项目最大文件；含 _wrinkle_ocr / KIND_FORM_NOLINES / KIND_GLARE / KIND_REDSTAMP / _to_grayscale）
│   ├── ocr_pipeline.py     # 辅助：行级图 OCR pipeline 共享层（RecordImageProcessor，三订单共用，437 行）
│   ├── ocr_log.py          # 辅助：业务日志上下文（set_log_context / clear_log_context，242 行）
│   ├── inventory_engine.py # 辅助：库存折算/SKU 匹配引擎（纯函数，无 DB 依赖；L0~L5 级联 + 单位折算 + 重复键作废，259 行）★ 2026-11-16 新增
│   ├── voice_pipeline.py / voice_llm.py / voice_fuzzy.py / voice_baidu.py  # 辅助：语音三档管线（150 / 176 / 135 / 90 行）★
│   ├── auth.py             # 蓝图：登录/登出（67 行）
│   ├── upload.py           # 蓝图：/upload/<path> 静态文件（17 行）
│   ├── basic_records.py    # 蓝图：经验/错误/待办/车辆维护/消防检查 + 改记录/维护记录图片/消防检查图片（298 行）
│   ├── info_pages.py       # 蓝图：首页/价格板/通知/流程/仓库/要点/缺货（87 行）
│   ├── notice.py           # 蓝图：/notice + REST API（220 行）
│   ├── products.py         # 蓝图：商品管理（326 行）
│   ├── shipping.py         # 蓝图：出货页 + YPP 核查 + REST + AI/OCR + 自适应提示词 + placement + copy-paper + _is_no_ai_match_item（2,508 行）
│   ├── inbound.py          # 蓝图：入库页 + 行级图片 + placement + align 复制至出货（1,891 行）
│   ├── loading.py          # 蓝图：装柜页 + 行级图片 + placement + img_cols + copy-paper 后端（1,737 行）
│   ├── search.py           # 蓝图：/unified-search 综合查找（80 行）
│   ├── staff.py            # 蓝图：/staff 人员档案（177 行）
│   ├── vehicles.py         # 蓝图：/vehicles 车辆档案（140 行）
│   ├── task_flow.py        # 蓝图：任务流 REST + 状态机（617 行）
│   ├── voice.py            # 蓝图：/api/v1/voice/{recognize,confirm,mappings/*}（217 行）★
│   ├── category_prompts_manage.py # 蓝图：/manage/category-prompts + REST（117 行）★
│   ├── mobile_shipping.py  # 蓝图：/m/* 移动端当天出货（231 行）★
│   ├── point_count.py      # 蓝图：/tools/point-count 独立点数 + REST（408 行，含 bp+bp_api 两个 Blueprint）★
│   └── audit.py            # 蓝图：OCR 事件审计页 + 聚合/钻取/CSV（132 行）★
├── templates/              # 52 个 Jinja2 模板（44 主目录 + 8 mobile/），共 21,436 行
│   ├── base.html           # 公共布局 + 导航 + Tailwind + 顶栏头像（444 行）
│   ├── _record_image_script.html # 共享行级图片 JS（1,185 行，31 具名 function + 5 箭头函数 + 3 个 window 导出，三订单共用）
│   ├── _smart_add_modal.html    # 共享智能添加弹框（1,741 行）
│   ├── _image_upload_modal.html # 共享图片上传弹框（432 行）
│   ├── _placement_count_modal.html # 共享摆放图清点弹框（102 行，三订单共用）
│   ├── _voice_input_modal.html   # 共享语音录入弹框（129 行，仅出货页引入）
│   ├── _ypp_review_include.html   # 共享 YPP 核查逻辑 + 样式（166 行，三页 YPP 页共用）★
│   ├── 三大订单: shipping-records.html（2,585）/ inbound-records.html（2,456）/ loading-orders.html（1,819）
│   ├── 商品管理: product-units.html（273）/ product-categories.html（449）/ products.html（478）/ piece-conversions.html（125）
│   ├── 信息页: notice.html（428）/ notice-color.html（477）/ priceboard.html（523）/ workflow.html（71）/ warehouse.html（46）
│   ├── 操作要点: count-tips.html（741） / huandan-guide.html（642） / billing-tips.html（569）
│   ├── 基础记录: experience.html（48）/ errorlog.html（72）/ todolist.html（50）/ vehicle-maintenance.html（174）/ fire-checks.html（612）/ stockout.html（220）
│   ├── 任务流: tasks.html（220）/ tasks-new.html（346）/ task-detail.html（301）/ coding-pool.html（101）
│   ├── 管理: staff.html（309）/ vehicles.html（255）/ unified-search.html（235）/ audit-ocr-events.html（233）/ manage-category-prompts.html（817）
│   ├── 独立点数: point-count.html（157） / point-count-session.html（124）
│   ├── YPP 核查三页: shipping_ypp_review.html（17）/ inbound_ypp_review.html（16）/ loading_ypp_review.html（16）
│   │   └── 三页均只是壳，逻辑全部在 _ypp_review_include.html ★
│   └── mobile/             # 移动端页面（8 个文件，867 行）
│       ├── index.html（125）/ shipping-today.html（43）/ shipping-order.html（229）/ placement-count.html（88）
│       ├── inbound-today.html（43）/ inbound-order.html（129）
│       └── loading-today.html（43）/ loading-order.html（167）
├── static/                 # 静态资源（js 13 个 4,752 行；css 3 个 1,170 行）
│   ├── css/app.css（807） / css/mobile.css（259） / css/point-count.css（104）
│   ├── js/common.js（151，公共工具）
│   ├── js/product_row_utils.js（641，商品行工具：件数换算/校验/mismatch/行内编辑/汇总；**仅装柜页引入**，出货/入库内联复制）★
│   ├── js/placement_count.js（744，出货+入库共用摆放图计数）★
│   ├── js/loading_placement_count.js（768，装柜专用摆放图计数）★
│   ├── js/no_ai_label.js（68，免 AI 比对标签图上传，不做 OCR；**出货 + 装柜页引入**）★
│   ├── js/voice_input.js（285）
│   ├── js/point-count.js（313） / point-count-list.js（79）
│   └── js/mobile_*.js（5 个）：mobile_blur（33）/ mobile_detail（416）/ mobile_inbound_detail（385）/ mobile_loading_detail（356）/ mobile_placement（513）
│   ├── mainflow.png / furongflow.png / warehouse.png
│   └── notice/             # 通知图片
├── upload/YYYY-MM/         # 用户上传图片，按月分组（8 个目录 / 10,941 个文件 / ~3 GB）
│                            # ⚠️ 铁律：只增不删。任何脚本/命令都不许删这里面的文件(见「踩坑点 11」)
├── log/                     # 业务日志（log/YYYYMM/ 按天分文件，OCR/AI 落盘）
├── logging_setup.py        # 日志初始化（166 行）：trace_id ContextVar + 线程上下文传播 + 日志格式
├── tests/                  # 测试套件（116 个 .py = 顶层 113 + tests/regression/ 3，19,012 行）
├── tools/                  # 工具脚本（39 个 .py + 2 个 .js，含路径迁移/校验/OCR fixture/回归验证/CLAUDE.md 审计/红章白名单覆盖率审计/前端逻辑验证）
├── docs/superpowers/       # 设计文档：specs/（20）+ plans/（19）+ e2e/ + regression/（截图）
├── sql/                    # SQL 脚本（15 个 = 13 .sql + 1 .json + 1 .txt；new.sql 为商品分类标准源 / new_sqlite.sql 为 SQLite 导入脚本，其余为历史导出与转换产物）
├── pyrightconfig.json      # Pyright 类型检查配置（仅覆盖 app.py + models/_db.py + models/__init__.py + models/_init.py）
├── _safe-snapshot/         # 高风险操作前的数据快照目录（当前存 8 份 product 重写 SQL 备份；**DB 快照统一走 D:\BAK\**）
├── start_server.bat        # Windows 启动脚本
├── setup_startup.ps1       # Windows 自启动 PowerShell 脚本
├── setup_firewall.bat      # 防火墙放行脚本（29 行）
├── README.md               # 面向人看的项目说明（459 行 / 41,818 B）
├── paddle_engine_dump.py   # PaddleOCR 引擎调试转储（518 行）
├── patch_lines.txt         # 临时补丁片段（5 行）
└── scratchpad.md           # 空占位文件
```

## 路由结构（267 个端点方法 / 265 条 rule，按 21 个 Blueprint 实例分布，2026-11-17 更新）

> **口径**：264 = **端点方法数**（同一 rule 支持 GET/POST 两个方法时计 2）；262 = **rule 数**。两者都由 `create_app().url_map` 统计：21 个 Blueprint 实例共 **261 条 rule / 263 个方法**（`auth` 的 `/logout` 与 `info_pages` 的 `/` 各含 2 个方法），加上 Flask 自动注册的 `/static/<path:filename>`（GET）→ **262 条 rule / 264 个方法**。早期版本的 243 是 `@bp.route(...)` 装饰器文本计数，口径不同，现统一以审计脚本为准。

所有端点已拆分到 `blueprints/` 目录下的 19 个蓝图文件（`point_count.py` 与 `stock.py` 各内含 `bp` + `bp_api` 两个 Blueprint → 共 **21 个 Blueprint 实例**）+ 9 个辅助模块 + 1 个包标记 = 29 个 .py，主 `app.py` 只保留工厂、上下文、缓存、登录闸门、蓝图注册。

> 复核命令：`python tools/_audit_claude_md.py`（打印全量 url_map 分组 + 各表行数，用于本文件对账）。
>
> ⚠️ 该脚本会 `create_app()`，**连带触发 `init_db()` 建表/迁移并向 `log/` 落盘**。只想读行数时改用只读连接：`sqlite3.connect("file:D:/worklog-app/worklog.db?mode=ro", uri=True)`。

移动端走 `/m/*` 前缀（**免登录**，见 `app.py._AUTH_PUBLIC_PREFIXES`），便于微信里点链接直接用；登录后路径仍是 `/shipping-records` 等带中文业务的页。

### `blueprints/auth.py` — 身份验证（3 个）

- `GET /login` 登录页（选身份，无密码）· `POST /login` 设 session · `GET|POST /logout` 清 session

### `blueprints/basic_records.py` — 基础记录（22 个）

- `/experience` 工作经验 · `/errorlog` 错误经验 · `/todolist` 待办 · `/vehicle-maintenance` 车辆维护
- **车辆维护记录修改（2026-11-02 新增）**：
  - `POST /vehicle-maintenance/edit/<id>` 改记录（弹框提交 → 表单 POST → flash + redirect，与本页其它表单同风格）
- **车辆维护图片（2026-11-02 新增）**：
  - `POST /api/v1/vehicle-maintenance/<id>/images` 上传（multipart，字段名 `image`，`request.files.getlist` 收多张）
  - `DELETE /api/v1/vehicle-maintenance/images/<image_id>` 删图（DB 行 + 物理文件）
- **消防检查记录（2026-11-06 新增）**：
  - `GET /fire-checks` 台账页（顶部「共 N 次 / 待整改 / 已闭环」汇总 + 「只看待整改」筛选）
  - `POST /fire-checks/add` 新增（日期 + 检查事项必填，部位/遗留事项/备注选填）
  - `POST /fire-checks/edit/<id>` 修改（弹框提交 → 表单 POST → flash + redirect）
  - `POST /fire-checks/delete/<id>` 删除
  - 状态 `status` 由 `models/fire_check.py` 按「遗留事项是否为空」自动推导（有遗留=open / 无遗留=closed），
    修改时可手选覆盖；避免出现「填了遗留事项却标成已闭环」的自相矛盾记录
- **消防检查图片（2026-11-08 新增）**：
  - `POST /api/v1/fire-checks/<id>/images` 上传（multipart，字段名 `image`，`request.files.getlist` 收多张）
  - `DELETE /api/v1/fire-checks/images/<image_id>` 删图（DB 行 + 物理文件）

### `blueprints/info_pages.py` — 信息展示 + stockout（11 个）

- `/` 首页 · `/priceboard` 码数报价 · `/notice-color` 通知彩色版 · `/workflow` 业务流程 · `/warehouse` 仓库布局 · `/count-tips` 点数要点 · `/huandan-guide` 换单要点 · `/billing-tips` 开单要点 · `/stockout` 当前缺货

### `blueprints/notice.py` — 通知（15 个）

- `/notice` CRUD · `/api/v1/notices` REST API · `/api/v1/notice/<id>/images` 图片管理 · `/api/v1/notice/<id>/img-cols` 列数设置

### `blueprints/shipping.py` — 出货（44 个端点 = 2 页面 + 42 API，2026-10-26 盘点）

**页面**：

- `GET /shipping-records` 列表页（日期范围/客户筛选/搜索，支持 `?focus=<record_id>&focus_date=YYYY-MM-DD` 跳转高亮）
- `GET /shipping-ypp-review` YPP 规则冲突核查页（⚖️ YPP 核查，导航栏新增；扫描全表不受日期过滤）

**YPP 核查**：

- `POST /api/v1/shipping-orders/ypp-review/scan` 全表扫描，返回 warn/info 明细列表（前端在 YPP 页点「开始扫描」触发）

**订单 CRUD**（PATCH 同时承载 lock/unlock、img_cols 等字段更新，无独立端点）：

- `POST /api/v1/shipping-orders` 创建订单
- `PATCH /api/v1/shipping-orders/<id>` 改订单（含 `is_locked` 锁定翻转、`img_cols` 图片列数、`order_note` 备注等）
- `DELETE /api/v1/shipping-orders/<id>` 删订单

**明细行操作**：

- `POST /api/v1/shipping-orders/<id>/records` 添加明细行
- `POST /api/v1/shipping-orders/<id>/records/batch` 批量添加
- `PUT /api/v1/shipping-orders/records/<id>` 改明细
- `DELETE /api/v1/shipping-orders/records/<id>` 删明细
- `DELETE /api/v1/shipping-orders/<id>/records` 删整单所有明细
- `PATCH /api/v1/shipping-orders/records/<id>/move` 上移/下移

**订单级图片（OCR 识别图，2026-08-10 增 rotate_deg + source_tag）**：

- `POST /api/v1/shipping-orders/<id>/images` 订单级图片上传，支持 `rotate_deg`（90/180/270，移动端拍照方向修正）+ `source_tag`（备货照/装车照/归仓照 三选一，移动端整体图缩略图按此分组）
- `DELETE /api/v1/shipping-orders/images/<id>` 删除图片

**行级图片（明细标签，2026-08-10 起支持 `rotate_deg`）**：

- `POST /api/v1/shipping-orders/records/<id>/images` 行级图片上传（DeepSeek 优先 + 本地 RapidFuzz fallback；支持 `rotate_deg`）
- `GET /api/v1/shipping-orders/records/<id>/images-area` 行级图片合并查询
- `POST /api/v1/shipping-orders/records/<id>/verify-warning` 标记/取消单条警告核查

**匹配与人工核查**（按图）：

- `POST /api/v1/shipping-orders/images/<id>/manual-verify` 人工覆盖/撤销 AI 判定
- `POST /api/v1/shipping-orders/images/<id>/fuzzy-match` 本地 RapidFuzz 重跑（**优先用缓存 `record_ocr` 事件，命中则不重跑 PaddleOCR**）
- `POST /api/v1/shipping-orders/images/<id>/re-ocr` 重跑 PaddleOCR（**强制 `use_cached=False`**；自动跟随一次本地 fuzzy 重比对；触发按钮带 `showBtnLoading` 异步 spinner）
- `POST /api/v1/shipping-orders/images/<id>/ai-judge` DeepSeek 重比对
- `GET /api/v1/shipping-orders/images/<id>/match-status` 异步轮询匹配状态
- `GET /api/v1/shipping-orders/images/<id>/ocr-detail` OCR 原文/提示词/推理详情

**整单 AI**：

- `POST /api/v1/shipping-orders/ai-recognize` 整单图片 AI 识别（`engine` 参数 5 引擎可选：`moonshot` / `paddleocr` / `deepseek` / `minimax` / `paddleocr_minimax`）
- `POST /api/v1/shipping-orders/<id>/ai-match` 整单 AI 比对（注入自适应提示词）

**摆放图（点数）**——11 个端点（9 个 URL 模式，其中 2 个各含 GET/POST 两条 rule），2026-08 上线：

- `POST /api/v1/shipping-orders/records/<id>/placement-images` 上传摆放图
- `GET /api/v1/shipping-orders/records/<id>/placement-images` 按明细行查摆放图
- `GET /api/v1/shipping-orders/placement-images/<id>` 单图详情
- `DELETE /api/v1/shipping-orders/placement-images/<id>` 删摆放图
- `POST /api/v1/shipping-orders/placement-images/<id>/detect` 自动检测散落点
- `POST /api/v1/shipping-orders/placement-images/<id>/mark-scale` 调整标记点大小
- `POST /api/v1/shipping-orders/placement-images/<id>/loose-count` 录入散码数
- `POST /api/v1/shipping-orders/placement-images/<id>/manual-count` 覆盖手动支数
- `POST /api/v1/shipping-orders/placement-images/<id>/unload` 标记卸货（取负）
- `POST /api/v1/shipping-orders/placement-images/<id>/marks` 追加点击标记
- `DELETE /api/v1/shipping-orders/placement-images/<id>/marks/last` 撤销最后标记

> 三页（出货 / 入库 / 装柜）placement 端点结构完全一致，见 [placement 三页对齐基线](#placement-三页对齐基线2026-09-11-复核)。

**拷贝纸 / 日本纸行级标签图**（2026-09-09 重构后状态）：

- `POST /api/v1/shipping-orders/records/<rid>/copy-paper-images` 上传标签照 → 落 `shipping_images(source='copy_paper_label', record_pk=<明细 id>)`
- 删除走通用 `DELETE /api/v1/shipping-orders/images/<id>`（自带锁单防御 + 审计）；**专表 `copy_paper_images` 与 `CopyPaperImage` 模型已废弃删除**，张数走 placement 体系（`source='placement'`）
- 判定函数 `_is_no_ai_match_item(item)`（2026-10-08 起为唯一谓词，原 `_is_copy_paper_item` 已并入），字段富化 `_enrich_no_ai_label_for_item(item)` 原地写 `is_no_ai_match` / `label_images` / `has_label_image`
- 装柜页有同款上传端点 `POST /api/v1/loading-orders/records/<rid>/copy-paper-images`（复用 shipping 的 `NO_AI_LABEL_SOURCE` / `_is_no_ai_match_item` / `_enrich_no_ai_label_for_item`）；**入库页暂无**

**自适应提示词**（共享端点，category_prompts 表）：

- `GET /api/v1/category-prompts` 列出提示词
- `POST /api/v1/category-prompts` 创建提示词
- `DELETE /api/v1/category-prompts/<id>` 软删提示词
- `POST /api/v1/shipping-orders/images/<id>/generate-prompt-suggestion` 基于图片 OCR 生成提示词建议

**日终盘点**（2026-10-26 新增，手填当日纸面单据总量对账，详见「关键实现细节」§25）：

- `GET /api/v1/shipping-orders/daily-check?start_date=&end_date=` 查区间内每天的盘点数据
- `POST /api/v1/shipping-orders/daily-check` 保存某天的手填单数（`{date, manual_total, remark?}`）
- `DELETE /api/v1/shipping-orders/daily-check/<date>` 清除某天的手填值

**加面图标**: 后端 `has_jia_mian` 计算 + 前端 SVG 渲染（"杂胶+加面"→ 重点列网状图标）

**OCR 糊图标记（2026-08-10 移动端）**：行级图 OCR 平均置信度 `avg_conf < 0.5` 时，由 `ocr_pipeline._append_blur_reason_if_low_conf()` 在 `image.reason` 末尾追加 `[图像可能模糊，建议重拍]`，前端 hover 提示用户重拍（幂等，marker 已存在不重复追加）。

**共享弹框/子页面（`templates/`）**：

- `_image_upload_modal.html`（430 行）—— 订单级 + record 级图片上传复用弹框（粘贴/选文件/旋转 90°，旋转后 Pillow 落盘再走 OCR）
- `_smart_add_modal.html`（1,588 行）—— 智能添加明细：📷 AI 图片识别（`#aiEngine` 四选项：Moonshot / **PaddleOCR+MiniMax（默认，推荐）** / PaddleOCR+DeepSeek / 纯 PaddleOCR）+ 📝 文本输入（DeepSeek 结构化）
- `_record_image_script.html`（1,185 行）—— 行级图片所有交互：匹配徽章列动态插入、异步 OCR+AI 比对、人工 ✓ 确认 / 👤 已确认、re-ocr/fuzzy/ai-judge/ocr-detail 按钮、自适应提示词弹框；出货页初始化会调用 `bindReOcrButtons()` 让「重 OCR」按钮在每次刷新后也能响应。内含 31 个具名 `function` + 5 个箭头函数变量 + 3 个 `window.*` 导出（`setImgCols` / `openImageModalForRecord` / `recordImageUploaded`）
- `_placement_count_modal.html`（102 行）—— 摆放图交互式清点弹框，三订单共用；内部同时引入 `placement_count.js` 与 `loading_placement_count.js` ★
- `_voice_input_modal.html`（129 行）—— 语音录入弹框（**仅出货页引入**，入口 `/api/v1/voice/recognize`/`/confirm`）

### `blueprints/inbound.py` — 入库（39 个端点，2026-10-28 盘点）

- `GET /inbound-records` 列表页 · `GET /m/inbound` 移动端当天列表 · `GET /m/inbound/order/<oid>` 移动端详情
- **REST API（`/api/v1/inbound-orders/*`）**: 订单 CRUD、明细 CRUD、move、图片 CRUD
- **行级图片**: 行级上传/合并查询 + manual-verify/fuzzy-match/ai-judge/ocr-detail/gen-prompt（同出货，**无 `re-ocr`**）
- **摆放图（点数）**: 11 个端点（含 **`/unload`**），与出货/装柜同构，模型 `InboundPlacementImage` + 表 `inbound_placement_marks`
- **交互式点数清点弹框已移植**：`inbound-records.html` 引入 `static/js/placement_count.js`（与出货共用同一文件），模板含 `#placementCountModal`
- **日本纸件数换算**: 按件数×每件张数+散装张数对比（支持 loose 与 * 形式）；placement 期望值走 `compute_placement_expected_zhi` 兜底
- **备注**: 2026-07-16 已清理 11 个 HTML form 死端点，统一为 REST API

### `blueprints/loading.py` — 装柜（36 个端点，2026-10-28 盘点）

- `GET /loading-orders` 列表页 · `GET /m/loading` 移动端当天列表 · `GET /m/loading/order/<oid>` 移动端详情
- **REST API（`/api/v1/loading-orders/*`）**: 订单 CRUD（含 `img_cols` 列数持久化）、明细 CRUD、move、图片 CRUD
- **行级图片**: 行级上传/合并查询 + manual-verify/fuzzy-match/ai-judge/ocr-detail/gen-prompt（同出货，**无 `re-ocr`**）
- **摆放图（点数）**: 11 个端点（含 **`/unload`**），模型 `LoadingPlacementImage` + 表 `loading_placement_marks`
- **拷贝纸 / 日本纸 / 蜡光纸**: `POST /api/v1/loading-orders/records/<rid>/copy-paper-images` 上传标签照（复用 shipping 的 `NO_AI_LABEL_SOURCE` / `_is_no_ai_match_item` / `_enrich_no_ai_label_for_item`，2026-10-15 PC + 移动端已对齐出货页）—— 普通 🖼️ 守卫 `not is_no_ai_match`（隐藏）+ 标签 🖼️ 按钮守卫 `is_no_ai_match`（显示，`source='copy_paper_label'` 不做 OCR）；后端 `loading.py:924` 对 `records/<rid>/images` 端点 0105-0108 仍硬拦截 400
- **共享模板**: `_record_image_script.html`（与出货/入库共用）+ `static/js/loading_placement_count.js`（装柜专用点数 JS）

### `blueprints/products.py` — 商品管理（18 条 rule）

- `/product-units` 商品单位（83 条数据）
- `/product-categories` 商品类型（`product_categories` 表，205 条 = 1 根 + 9 大类 + 分类树，以 `sql/new.sql` 为最终标准源）
- `/products` 产品管理（`product` 表，2026-11-17 为 1,605 条）；商品信息区表头 **10 列**：`序号 | 商品编号 | 品名 | 规格 | 型号 | 主单位 | 辅单位 | 库存 | 辅数量 | 操作`（2026-11-15：「编号」改名「商品编号」、最前加序号列从 1 计数）。序号是**显示态**、不落库：`templates/products.html` 的 `renumberProductRows()` 按 tbody 内 `tr[data-pid]` 当前顺序重排，加载(2)/编辑替换(1)/新增(1)/删除(1) 共 **5 处调用**，`buildProductRow()` 首格为 `<td class="rowno-cell"></td>` 占位，骨架屏同步 `colspan=10` + 10 个占位格。验证：`node tools/_verify_products_rowno.js`（静态列数 + jsdom 增删改序号）、`python tools/_verify_products_render.py`（真实 Jinja 渲染）。
- `/piece-conversions` 件数换算规则（16 条）
- `/api/v1/products` REST API（GET/POST/PUT/DELETE）
- `GET /api/v1/product-categories/<id>/children` 分类树子节点懒加载

### `blueprints/search.py` — 综合查找（2 个）

- `GET /unified-search` 综合查找页面 · `POST /api/v1/unified-search` REST API
- 支持按日期范围、客户、品名、规格跨出货/入库/装柜搜索

### `blueprints/staff.py` — 人员管理（5 个）

- `GET /staff` 列表 + 新增表单（支持 role 过滤 & show_disabled 切换）
- `POST /staff/add` · `POST /staff/update/<sid>` · `POST /staff/delete/<sid>`（软删）
- `POST /staff/enable/<sid>` 恢复启用
- 角色枚举：司机/调度/搬运/打码/仓管/文员

### `blueprints/vehicles.py` — 车辆管理（5 个）

- `GET /vehicles` 列表 + 新增表单
- `POST /vehicles/add` · `POST /vehicles/update/<vid>` · `POST /vehicles/delete/<vid>`（软删）
- `POST /vehicles/enable/<vid>` 恢复启用

### `blueprints/task_flow.py` — 任务流（17 个端点）

- `GET /tasks` 任务列表（tab 过滤：全部/待接单/进行中/已完成/已退单）
- `GET /tasks/new` 新建任务页（带 OCR 弹框）
- `GET /tasks/<tid>` 任务详情页（7 阶段进度条 + 证据图 + 明细 + 事件历史）
- `GET /coding-pool` 打码抢单池（M1 占位，仅显示待打码）
- **REST API**:
  - `POST /api/v1/tasks` 建单（事务保护：Task + Items + Event 三合一）
  - `POST /api/v1/tasks/<tid>/advance` 状态推进（闸门：司机指派 + 打码完成 + 证据照）
  - `POST /api/v1/tasks/<tid>/assign` 指派司机
  - `POST /api/v1/tasks/<tid>/images` 上传证据照（9 种 stage 白名单）
  - `DELETE /api/v1/tasks/images/<iid>` 删除证据照
  - `POST /api/v1/tasks/<tid>/driver-release` 司机退单（退单照闸门）
  - `DELETE /api/v1/tasks/<tid>` 硬删除（测试用，调度/文员）
  - `POST /api/v1/tasks/<tid>/cancel` 任务作废（仅准备中）
  - `POST /api/v1/tasks/<tid>/return-all` 整单退回（生成退货任务，数量负）
  - `POST /api/v1/tasks/ai-recognize` OCR 识别（复用 OCR 引擎）
  - 占位（501）：coding-claim / coding-done / coding-release

### `blueprints/audit.py` — OCR 事件审计（4 个端点，新增）★

- `GET /audit/ocr-events` 审计主页（渲染 `audit-ocr-events.html`，挂导航「📊 OCR 事件审计」）
- `GET /api/v1/audit/ocr-events/aggregate` 按 `prompt_version` 聚合统计（每提示词版本命中 green/yellow/red 数）
- `GET /api/v1/audit/ocr-events/records` 钻取某 `prompt_version` 下的 record↔image 配对明细（支持日期过滤 + 分页）
- `GET /api/v1/audit/ocr-events/export.csv` 导出 CSV（带公式注入防护：`=+-@` 开头强制加 `'` 转文本）

后端走 `models/audit_query.py` 的 `OcrEventAudit`（只读查询：`prompt_stats` / `record_pairs` / `export_rows`）。用途：追踪「自适应提示词」各版本上线后对 AI 比对准确率的实际影响。

### `blueprints/voice.py` — 语音录入（6 个端点，2026-08-06）★

- `POST /api/v1/voice/recognize` 上传音频 → 返回候选文本（顺序：百度语音 → LLM → 本地 fuzzy）
- `POST /api/v1/voice/confirm` 用户确认/修正后回写（用于自适应学习）
- `GET /api/v1/voice/mappings` 列出自定义映射规则
- `POST /api/v1/voice/mappings` 新建映射
- `PATCH /api/v1/voice/mappings/<id>` 修改映射
- `DELETE /api/v1/voice/mappings/<id>` 软删

设计要点：表单 `action` 必须显式指向 `/api/v1/voice/recognize`（不要依赖 button 默认 form action，曾误回到 `/shipping-orders` 返回 HTML）。出货页用 `_voice_input_modal.html` + `static/js/voice_input.js`。

### `blueprints/category_prompts_manage.py` — 分类提示词管理（4 个端点）★

- `GET /manage/category-prompts` 管理页（挂「📚 分类提示词」导航）
- `GET /api/v1/manage/category-prompts/list` 列出全部（含软删）
- `PATCH /api/v1/manage/category-prompts/<id>` 修改
- `POST /api/v1/manage/category-prompts/bulk` 批量导入

### `blueprints/mobile_shipping.py` — 移动端出货（4 个页端点，2026-08-09）★

- `GET /m/` 移动端入口（聚合今日出货/今日入库）
- `GET /m/shipping-today` 当天出货分组列表（每客户一组，按 ⚠ 待确认 / ✕ 不符 / ✓ 通过 聚合统计）
- `GET /m/shipping-today/order/<oid>` 移动端订单详情（含拍照按钮 + 摆放图状态 + 颜色提示）
- `GET /m/shipping-today/order/<oid>/placement/<record_id>` 移动端点数页（复用 PC 摆放图后端，前端走 `mobile_placement.js`）

PC 端的 `/m/inbound` + `/m/inbound/order/<oid>` / `/m/loading` + `/m/loading/order/<oid>` 分别由 `inbound.py` / `loading.py` 内的 `@bp.route('/m/...')` 服务（2026-09 起路由名由 `inbound-today` / `loading-today` 改为 `inbound` / `loading`），模板在 `templates/mobile/`。**所有 `/m/*` 都在 `_AUTH_PUBLIC_PREFIXES` 白名单**，免登录便于微信直接打开。

### `blueprints/point_count.py` — 独立点数（bp + bp_api 两个 Blueprint，19 个端点，2026-08-17）★

`bp`（页面路由，前缀 `/tools/point-count`）：

- `GET /` 会话列表（开/关/全部）
- `GET /new` 新建会话
- `GET /session/<id>` 会话详情（点数画布 + 图片序列）

`bp_api`（REST 前缀 `/api/v1/point-count`）：

- `GET /sessions` · `POST /sessions` · `GET /sessions/<id>` · `POST /sessions/<id>/{update,close,reopen}` · `DELETE /sessions/<id>`
- `GET /sessions/<id>/export` 导出 CSV
- `GET /sessions/<id>/images` · `POST /sessions/<id>/images` 上传
- `GET /images/<id>` · `DELETE /images/<id>`
- `POST /images/<id>/marks` 追加点击标记
- `DELETE /images/<id>/marks/last` 撤销最后标记
- `POST /images/<id>/mark-scale` 调整标记点大小
- `POST /images/<id>/loose-count` 录入散码数

用于"独立点数"场景：临时开会话 → 连续拍照 → 标记支/散码 → 导出 CSV。不挂订单/明细，纯独立工具。

### `blueprints/stock.py` — 库存查询 + 入库过账（bp + bp_api 两个 Blueprint，9 个端点，2026-11-17）★

**阶段 2 · 只读**：不写任何库存数据。
**阶段 3 · 写库**：过账 / 冲销 —— **事务与写入契约在 `blueprints/stock_posting.py`（服务层）**，
本文件只做 HTTP 层（参数校验 → 调服务 → 返回 JSON）。

`bp`（页面路由）：
- `GET /stock` 库存查询页（服务端渲染：仓库标签 + 分类树 + 搜索 + 统计格 + 表格 + 行展开看流水）

`bp_api`（REST 前缀 `/api/v1`）：
- `GET /warehouses` 仓库列表（当前只有芙蓉仓）
- `GET /stock` 库存列表（`warehouse_id` / `category_id` / `q` / `page` / `page_size` / `only_tracking`）
- `GET /stock/<int:product_id>` 单 SKU：基本信息 + 各仓库存 + 最近 20 条流水
- `GET /stock/ledger` 流水明细（`product_id` / `biz_type` / `date_from` / `date_to`；日期筛的是 `occurred_at`）
- `GET /stock/export.csv` 导出 CSV（**带 `=+-@` 公式注入防护**，照抄 `blueprints/audit.py`）
- `POST /inbound-orders/<int:order_id>/post` 📥 **入库过账**（阶段 3）
- `POST /inbound-orders/<int:order_id>/reverse` ↩ **红字冲销**（阶段 3）
- `GET /inbound-orders/<int:order_id>/stock-status` 过账状态 + 跳过清单 + 未启用提示（阶段 4 徽章数据源）

⚠️ `/api/*` 不在登录闸门白名单 → 这套 API **自己判 session**，没登录返回 401 + JSON。
⭐ **改账类端点**（post / reverse）额外走 `_require_operator()`：session 里没有 `operator_id` 直接 401
（A7：改账必须留痕，`audit_log.user_id` 不允许为 NULL）。

### `blueprints/stock_posting.py` — 入库过账服务层（2026-11-17 新增）★

阶段 3 的**核心**。过账要跨 4 张表写在**同一个事务**里，这活儿不该塞在 HTTP 层。

- `post_inbound_order(order_id, operator_id, operator_name)` → 入库过账
- `reverse_inbound_order(order_id, operator_id, operator_name)` → 红字冲销
- `get_stock_status(order_id)` → 过账状态 / 跳过清单 / 未启用提示
- `PostingError(message, status)` → 带 HTTP 状态码的业务异常，蓝图直接转 JSON

⭐ **A2 事务边界**：`_tx_conn()` 把 `conn.isolation_level = None`（Python sqlite3 默认会隐式管事务，
跟显式 `BEGIN IMMEDIATE` 打架），然后 `BEGIN IMMEDIATE` → 四表共用一个连接 → `COMMIT` / `ROLLBACK`。
模型层方法一律传 `conn=conn`（传了就不 commit / 不 close）。

⭐ **三道幂等防线**：① `uniq_key` 明细行级（`inbound:{record_id}`）② `BEGIN IMMEDIATE` 写锁串行化
③ `UNIQUE(uniq_key)` 兜底（IntegrityError → 转成「已过账」跳过，不是 500）。

⭐ **A3 幂等写入契约**：`stock_ledger` 一律**普通 INSERT**；幂等靠**先查后写**
（`StockLedger.get_by_uniq_key()` 查到就跳过），**禁用** `OR REPLACE` / `OR IGNORE` / `ON CONFLICT`。

⭐ **A1 / A4 写入契约**：每条流水写 `occurred_at`（**取订单头 `date`**，不是系统时间）
+ `balance_after`（本笔记账后该 (仓, SKU) 的余额快照）。

⭐ **过账不改变启用状态**：`is_tracking=0` 的商品 —— 流水照记、内部累计照加、**对外显示仍是 0**，
结果里单列 `not_tracking` 清单。
⚠️ `not_tracking` **不进** `stock_posting_skips`（那是「没入账」的待处理清单），
它是「已入账但未启用」的提示清单 —— 两者语义完全不同。

### `blueprints/upload.py` — 静态文件

- `/upload/<path>` 访问 upload 目录下的文件

### 主 `app.py` — 应用入口（204 行，2026-10-28 盘点）

- `create_app()` 工厂函数，**注册 21 个 Blueprint 实例**（19 个蓝图文件；`point_count.py` 与 `stock.py` 各含 `bp` + `bp_api` 两个，含 audit / voice / category_prompts_manage / mobile_shipping）
- `inject_notices` 全局上下文（注入 all_notices 到所有模板）
- `inject_current_operator` 全局上下文（注入当前操作员 Staff dict 到模板；session 失效时主动 `session.pop('operator_id', None)` 避免"什么都没了"假象）
- `_require_login` before_request 闸门（白名单 `_AUTH_PUBLIC_PREFIXES = ("/static", "/api/", `/m/`, "/upload/")` + `_AUTH_PUBLIC_PATHS = ("/login", "/logout", "/favicon.ico")`）
- `add_cache_control_headers` 禁用 HTML 缓存
- `_reset_log_context` after_request 清业务上下文 + `_TRACE_ID`，避免请求间串味
- `init_logging(app)` 在 `init_db()` 之前（建表失败也能落盘到 `log/`）
- `init_db()` 数据库初始化（38 张业务表 DDL + 48 条 try/except ALTER 增量迁移）
- 上传超限 413 友好返回（API 走 JSON，页面走纯文本）

## 数据库（40 张业务表，2026-11-08 更新）

> 库内共 **42 张表** = 40 张业务表 + `sqlite_sequence`（系统自增序列）+ `_cleanup_safety_2026_07_30`（2026-07-30 数据清洗临时表，非业务表，可清理）。
>
> 早期存在的 `product_units_new`（迁移残留）与 `copy_paper_images`（2026-09-09 废弃）**均已删除**。
>
> 另有 48 个索引（38 个手写 + 10 个 `sqlite_autoindex` 隐式）、0 个视图。

### 核心业务表

| 表                           | 用途          | 行数     |
| --------------------------- | ----------- | ------ |
| `work_logs`                 | 工作经验        | 25     |
| `error_logs`                | 错误经验        | 9      |
| `todo_items`                | 待办事项        | 3      |
| `notices` + `notice_images` | 公司通知        | 26 + 5 |
| `vehicle_maintenance`       | 车辆维护        | 4      |
| `vehicle_maintenance_images` | 车辆维护图片（2026-11-02 新增） | 0 |
| `fire_checks` | 消防检查记录（2026-11-06 新增） | 0 |
| `fire_check_images` | 消防检查图片（2026-11-08 新增） | 0 |
| `stock_out_items`           | 缺货登记        | 1      |
| `voice_phrase_mapping`      | 语音短语映射（自学习） | 0      |

### 订单三表结构

三种订单都遵循 **订单 → 记录/明细 → 图片** 三表模式：

**出货** (`shipping_orders` / `shipping_records` / `shipping_images`)

- 1,144 个订单 / 3,116 条记录 / 6,755 张图片

**入库** (`inbound_orders` / `inbound_records` / `inbound_images`)

- 331 个订单 / 1,029 条记录 / 959 张图片

**装柜** (`loading_orders` / `loading_order_records` / `loading_order_images`)

- 25 个订单 / 131 条记录 / 283 张图片

通用字段：

- `is_locked` (0/1) — 锁定订单防止修改
- 订单级字段：`date` / `customer` / `order_num`（出货和装柜）
- 入库订单只有 `date`、`supplier` 和 `is_locked`
- 明细字段：`product_name` / `specification` / `quantity` / `unit` / `remark`
- 图片存储在 `upload/` 文件夹，通过相对路径引用
- **图片表统一带 `source` 字段**（三页口径一致）：`upload`（手动）/ `ai`（AI 识别）/ `placement`（摆放图）/ `copy_paper_label`（免 AI 比对标签图，**出货 + 装柜**，不做 OCR；入库页尚未接入）
  - 出货实测分布：upload 4,165 / placement 1,844 / ai 696 / copy_paper_label 50
  - 入库实测分布：upload 728 / ai 214 / placement 17（**placement 已投入使用**）
  - 装柜实测分布：upload 243 / ai 9 / placement 31 / copy_paper_label 0（**2026-10-15 PC + 移动端 UI 已就绪**,待真机首次上传后回填非零值）
- **摆放图相关列直接挂在图片表上**（不是 marks 表）：`circles`(JSON) / `mark_scale` / `loose_count` / `manual_count` / `is_unload` / `source_tag`

### 摆放图标记表（2026-09 三页齐备）

| 表                         | 归属        | 行数               | 同库摆放图总数                                         |
| ------------------------- | --------- | ---------------- | ----------------------------------------------- |
| `placement_marks`         | 出货摆放图点击标记 | 5,971（覆盖 515 张图） | `shipping_images(source='placement')` 1,844 张   |
| `inbound_placement_marks` | 入库摆放图点击标记 | 15（覆盖 2 张图）      | `inbound_images(source='placement')` 17 张       |
| `loading_placement_marks` | 装柜摆放图点击标记 | 0                | `loading_order_images(source='placement')` 31 张 |

> 装柜 0 行标记**不是 bug**：31 张摆放图全部走 `manual_count` 直接录支数（1-80 支不等，其中 2 张 `is_unload=1`），没有一条点击标记。

三表结构一致：`id / image_id / seq / x_ratio / y_ratio / mark_r / created_at`（坐标为 0-1 比例值）。

### 商品资料

| 表                    | 用途                 | 行数    | 模型类               |
| -------------------- | ------------------ | ----- | ----------------- |
| `product_units`      | 商品单位/规格（YPP 支码换算）  | 83    | `ProductUnit`     |
| `product_categories` | 商品分类（3 级树，带编码）     | 205   | `ProductCategory` |
| `product`            | 产品（品名/规格/条码/价格/库存） | 1,287 | `Product`         |
| `piece_conversions`  | 件数换算规则（件→张/只/令）    | 16    | `PieceConversion` |

### 任务流表

| 表             | 用途                                     | 行数                                                                                     |
| ------------- | -------------------------------------- | -------------------------------------------------------------------------------------- |
| `staff`       | 人员档案（姓名/角色/电话/绑定车辆）                    | 54 = 5 真实员工（阿海/阿祥/刘会/阿荒/调试员，id 2471-2475，2026-11-07 从 7/18 备份恢复）+ 49 测试残留（test/verify/smoke-tester/portal-tester/veh-tester，id 6220+） |
| `vehicles`    | 车辆档案（车牌/吨位/长宽高/年检日期）                   | 1                                                                                      |
| `tasks`       | 任务（task_no/状态/客户/地址/司机/车辆/打码状态）        | 0                                                                                      |
| `task_items`  | 任务明细行（品名/规格/数量/单位/备注）                  | 0                                                                                      |
| `task_images` | 任务证据照（stage 白名单 + image_path）          | 0                                                                                      |
| `task_events` | 任务事件历史（advance/assign/return_create 等） | 0                                                                                      |

### 独立点数表（2026-08-17 新增）

| 表                      | 用途         | 行数 |
| ---------------------- | ---------- | -- |
| `point_count_sessions` | 点数会话（不挂订单） | 1  |
| `point_count_images`   | 会话图片       | 1  |
| `point_count_marks`    | 图片点击标记     | 14 |

### 审计 & OCR 事件

| 表                  | 用途                                                      | 行数     |
| ------------------ | ------------------------------------------------------- | ------ |
| `audit_log`        | 操作审计日志                                                  | 13,693 |
| `ocr_match_event`  | OCR 标签匹配事件记录（record_ocr / ai_match / human_verify 三类事件） | 2,941  |
| `category_prompts` | 自适应提示词（scope=category/spec，注入 AI 比对）                    | 214    |

### 库存（多仓 + 入库过账）★ 2026-11-16 阶段 1 新建

| 表                    | 用途                                             | 行数 |
| --------------------- | ------------------------------------------------ | ---- |
| `warehouses`          | 仓库（当前只有 'FR' 芙蓉仓，架构已支持多仓）      | 1    |
| `product_stock`       | 当前库存（仓库 × SKU），`UNIQUE(warehouse_id, product_id)` | 0 |
| `stock_ledger`        | **库存流水，一切的唯一来源**，`UNIQUE(uniq_key)` 幂等全靠它<br>⭐ 阶段 1.5 加：`occurred_at`(业务日期) / `balance_after`(余额快照) / `unit_cost` / `amount`(成本预留,可空) | 0 |
| `stock_postings`      | 过账批次（一次点「过账」= 一个批次，status: posted/reversed） | 0 |
| `stock_posting_skips` | 过账跳过明细（待处理清单，5 种 reason_code） | 0    |
| `stock_counts`        | 盘点单（status: open/closed；⭐ 阶段 1.5 加 `as_of` 基准时点） | 0    |
| `stock_count_items`   | 盘点明细，`UNIQUE(count_id, product_id)`          | 0    |

**⭐A3 只追加：3 个守卫触发器**（`stock_ledger`）：
`trg_stock_ledger_no_update`（BEFORE UPDATE）· `trg_stock_ledger_no_delete`（BEFORE DELETE）·
`trg_stock_ledger_no_reuse`（BEFORE INSERT + `WHEN EXISTS(uniq_key=NEW.uniq_key)`）
→ 全部只 `SELECT` + `RAISE(ABORT)`，**禁止承载业务计算**（体检脚本扫 `sqlite_master.sql` 自动判）。
⚠️ **第 3 条不能省**：SQLite 默认 `recursive_triggers=0` 时 `INSERT OR REPLACE` **不触发** DELETE 触发器
→ 能无声把流水数量从 10 改成 999。代价是**幂等必须「先查后写」**（`StockLedger.create_if_absent()`）。

**三张订单表加了 `warehouse_id`**（`INTEGER NOT NULL DEFAULT 1` = 芙蓉仓）：
`inbound_orders` / `shipping_orders` / `loading_orders`。

⭐ **阶段 3 过账落地后，这 7 张表在生产库仍是 0 行** —— 过账 API 已就绪但**前端按钮在阶段 4**，
目前还没有任何真实单据被过账。`product_stock` / `stock_ledger` 等从空表起步，库存靠「盘点」起算
（期初数不导入；`product.stock_quantity` 是建档时抄的目录图数字，不是实时库存）。

**`ocr_match_event` 关键字段**：`event_type`（record_ocr / ai_match / human_verify）、`ocr_text`（PaddleOCR 原文）、`prompt_payload`（完整比对 prompt）、`ai_match_status/score/reason`、`ai_raw_response`（DeepSeek 原始返回）、`ai_engine`、`prompt_version`（按提示词版本聚合用）、`human_status/reason/verified_by`（人工裁决）。

### 日终盘点表（2026-10-26 新增）

| 表                  | 用途                                              | 行数 |
| ------------------ | ------------------------------------------------- | ---- |
| `daily_order_check` | 用户手填的「当日单据总量」（日终盘点对账用）             | 3    |

**`daily_order_check` 关键字段**：`scope`（`shipping` / `inbound` / `loading`，预留扩展）、`date`（业务日期 `YYYY-MM-DD`）、`manual_total`（手填的当日单据总量）、`remark`（差异原因备注）、`user_id`、`created_at` / `updated_at`。**`UNIQUE(scope, date)`** —— 一天一条，重复填是 upsert 不是追加。

## 关键实现细节

### 1. 登录与权限（T6 新增）

**登录闸门**：`app.py` 的 `_require_login` before_request 保护除 `/static`、`/api/`、`/login`、`/logout` 外的所有页面。REST API 自己返回 401，由前端引导跳转。

**身份模型**：选人即登录（无密码），适合单人/小团队场景。session 存 `operator_id`（int, Staff.id）。多人化时只需加密码层，gate 不变。

**权限系统**（`models/_permissions.py`）：`Action` 枚举（**13 个**动作：`CREATE_TASK` / `EDIT_TASK` / `ASSIGN` / `CODING_CLAIM` / `CODING_DONE` / `CODING_RELEASE` / `LOAD` / `ARRIVE` / `UNLOAD` / `COUNT` / `DRIVER_RELEASE` / `TASK_CANCEL` / `RETURN_CREATE`）+ `can(operator, action, task)` 集中校验。规则：

- 建单/编辑：任意登录者
- 打码三操作：仅「打码」角色
- 指派司机：调度/文员
- 点数：文员/仓管
- 装货/到达/卸货/退货：仅本单司机
- 司机退单：仅本单司机 + 准备中/已装货
- 任务作废：调度/文员 + 准备中

### 2. 任务流状态机（任务流 M1）

**7 态主线**：准备中 → 已装货 → 已点数 → 已到达 → 已卸货 → 已完成（异常：已拒收、已作废）

**推进闸门**：

- 准备中 → 已装货：必须已指派司机 + coding_status 为「无需打码」或「打码完成」
- 已装货 → 已点数：需装车照
- 已点数 → 已到达：需点数标签照/点数整体照（任一）
- 已到达 → 已卸货：需卸货照
- 已卸货 → 已完成：需签收单
- 推进动作走 `can(op, Action.X, t)` 集中校验

**异常处理**：

- 司机退单：上传退单照 → 回退准备中（解绑司机/车辆）
- 任务作废：调度/文员在准备中状态可作废
- 整单退回：上传退货照 → 自动生成退货任务（数量负）

### 3. OCR 引擎抽象层（五引擎）

`blueprints/ocr_engine.py` 提供统一接口 `recognize(image_bytes, filename) → {"success": bool, "items": [...]}`：

| 引擎 key              | 类                            | 类型     | 适用场景                                                                        |
| ------------------- | ---------------------------- | ------ | --------------------------------------------------------------------------- |
| `moonshot`          | `MoonshotEngine` (kimi-k2.6) | 云端视觉   | 手写单据、复杂排版，直接图片→JSON                                                         |
| `paddleocr`         | `PaddleOCREngine`            | 本地 CPU | 免费离线 OCR 文字提取，打印单据效果好                                                       |
| `deepseek`          | `DeepSeekEngine` (v4-flash)  | 云端结构化  | PaddleOCR 文字 → LLM → JSON，兼顾免费+语义理解                                         |
| `minimax`           | `MiniMaxEngine` (MiniMax-M3) | 云端多模态  | 2026-09-17 加：DeepSeek `finish_reason='length'` 截断时的自动 fallback；也可手动选        |
| `paddleocr_minimax` | `PaddleOCRMiniMaxEngine`     | 复合     | 2026-10-04 加，前端**默认选项**：PaddleOCR 抽文字 → MiniMax 结构化，MiniMax 失败自动降级 DeepSeek |

**引擎工厂**：`get_ocr_engine(name)` 单例缓存，通过 `OCR_BACKEND` 环境变量或前端 `#aiEngine` 下拉选择。合法值白名单在 `ocr_engine._VALID_ENGINES`（5 项）；⚠️ **shipping / inbound / loading 三个 `ai-recognize` 端点各自又硬编码了一份同样的元组**，加新引擎要改 4 处（已知技术债，见「待办」）。

**`PaddleOCRMiniMaxEngine` 契约**（2026-10-04，`blueprints/ocr_engine.py:3698` 起）：

- 流程：`self._deepseek._ocr_image(image_bytes)`（复用 DeepSeekEngine 的 PaddleOCR + `_format_table_rows` 表格行格式化）→ `MiniMaxEngine.recognize_text(ocr_text)` → 失败降级 `DeepSeekEngine.recognize_text`
- 成功返回必含 `ocr_text`（前端 `_smart_add_modal.html` 两栏对照弹框要用）
- 降级成功时额外带 `fallback_minimax_failed=True` + `original_engine='paddleocr_minimax'`
- 两边都失败 → 返回 **MiniMax 的错误**（主路径优先）+ `ocr_text`
- 依赖 `.env` 的 `MINIMAX_API_KEY`；未配置时 `recognize_text` 本地直接失败（不发网络请求）→ 自动落到 DeepSeek，功能不挂

**PaddleOCR 双实例**：`_ocr`（默认阈值）+ `_wrinkle_ocr`（低阈值，专攻褶皱标签）。`form_nolines` kind 自动走后者。\_wrinkle_ocr 加载失败时 `_ensure_model` try/except 回退到 \_ocr。

> ⚠️ `DeepSeekEngine.__init__` 里 `self._ocr_engine = PaddleOCREngine()` 是**新建实例**，不是 `get_ocr_engine('paddleocr')` 单例。所以 `paddleocr` + `deepseek`（或 `paddleocr_minimax`）同时使用会在进程内加载两份 PaddleOCR 模型。

**共享后处理**：`_filter_summary_items()` 安全网过滤 LLM 未能跳过的汇总行（5 种检测：关键词/无品名/异常大数量/remark 汇总/规格汇总）。

**汇总行检测**：关键词匹配 + 数量异常值检测（超过中位数 3 倍的 outlier）+ remark 清理——各引擎统一后处理，双保险防汇总数据混入商品行。

### 4. AI 图片识别（出货）

**整单识别**：`/api/v1/shipping-orders/ai-recognize` 接收图片，调用选定引擎，提取商品信息后返回 JSON（含 doc_number、customer_name）。

**明细行标签 OCR 匹配**（`/api/v1/shipping-orders/<oid>/ai-match`）：

- PaddleOCR 提取标签文字 → DeepSeek COMPARE_PROMPT 比对已录入明细行
- 逐行返回 `{record_id, match_status: green|yellow|red, reason}`
- 前端渲染 ✓/⚠/✗ 徽标 + 图例说明
- **行级图片上传**：上传即自动跑 OCR 匹配，返回 status/score，落库到 `shipping_images.match_status` 等字段

**COMPARE_PROMPT 4 大规则**：

1. 杂胶类「环保」等价词（环保/7P/15P/18P/21P）
2. 颜色匹配（黑/白/红/蓝/绿/黄/棕/灰/米/杏/紫/粉/橙）
3. 厚度匹配（mm 单位数值比对）
4. 加面/单面匹配（双面≡加面，单面≠加面判冲突 red）

**人工核查**：`human_verified` 字段支持人工确认/覆盖 AI 判定。

#### 跳过 AI 比对的品类（`_is_no_ai_match_item`，2026-10-04 新增 / 2026-10-08 扩到标签图入口）

拷贝纸 / 日本纸 / 快巴纸 / 腊光纸这类的**包装贴纸与商品本体不一致**，行级图走 OCR 必然刷误导性红 ✗。它们的**唯一图片入口是「🖼️ 标签图」按钮**（落 `shipping_images(source='copy_paper_label')`，不做 OCR / 不进 AI 比对）。

- 常量（`blueprints/shipping.py:60-71`）：`NO_AI_LABEL_SOURCE = 'copy_paper_label'`（落库的 `source` 字符串）+ `NO_AI_MATCH_CATEGORY_CODES = ('0105','0106','0107','0108')` + `NO_AI_MATCH_KEYWORDS = ('拷贝','日本纸','快巴','腊光','蜡光')`
  > ⚠️ **真实数据用的是「蜡光纸」**（`shipping_records` 命中 4 行，全部是「蜡光纸」；「腊光」0 行）。`蜡光` 同义收录不是防御性冗余，是当前唯一生效的那个字，**别当成错别字容错删掉**。
- 判定函数 `_is_no_ai_match_item(item)`：关键词先短路，未命中再走 `classify_record()` 拿 `category_code`
- **2026-10-08 合并**：此前还有一个 `_is_copy_paper_item`（只覆盖 0105/0106/0107）。两者关键词集/品类码集互相包含，对同一 `(name, spec)` 会跑两次 `classify_record` 查询；腊光纸纳入后完全等价，已合并为单一谓词，`is_copy_paper` 字段一并删除
- 五层消费：
  1. 后端硬拦截 `POST /api/v1/{shipping,loading}-orders/records/<id>/images` → 400（防 DevTools / 粘贴 / 拖拽绕过 UI）
  2. 整单 `ai-match` 在 OCR 前过滤 `records`（覆盖 OCR 循环 / rows / own_record_ids / verdicts 回写四路径）
  3. PC `shipping-records.html`：**标签 🖼️ 按钮**守卫 `if item.is_no_ai_match`（渲染）+ **普通 🖼️ 按钮**守卫 `if not ... is_no_ai_match`（隐藏）→ 一行恰好一个 🖼️
  4. 移动端 `mobile/shipping-order.html`：隐藏「📷 拍照识别 / 相册」（否则点进去吃 400），改渲染「📷 标签」；徽章从永远满足不了的「待拍」改为「标签图」；卡片打 `data-role="label-only"` 结构标记
  5. `_enrich_no_ai_label_for_item`（`shipping.py:2475`）写入 `item['is_no_ai_match']` + `label_images` / `has_label_image`（供 3/4 消费）；`loading.py` 复用同一 helper（需预注入 `record_imgs_cache`，否则会 fallback 查 `ShippingImage` 表拿错图）

> **标签图上传端点本身没有 `is_copy_paper` 守卫**（`shipping.py` / `loading.py` 的 `/records/<rid>/copy-paper-images` 只校验锁单 + 图片有效性），所以腊光纸能直接复用，无需改后端。
>
> 装柜页（`loading-orders.html`）**已于 2026-10-15 接上标签图按钮**（PC + 移动端），端点复用 `POST /api/v1/loading-orders/records/<rid>/copy-paper-images`。**入库页仍未接**（缺端点 + 前端守卫 + 移动端标签按钮，见「待办」）。

### 5. 加面图标（商品明细重点列）

`has_jia_mian` 后端计算：`product_name` 含「杂胶」且 `specification` 含「加面」→ True。

前端渲染：重点列显示 SVG 网状图标 + CSS class `.jia-mian-icon`。

原「环保」列已改名为「重点」列。

### 6. 图片上传流程（含安全校验）

**上传校验**（`blueprints/_helpers.py`，2026-07-15 加固）：

- **文件大小**：单张最大 10 MB（`_MAX_IMAGE_BYTES`，另 `app.py` 整体请求上限 20 MB）
- **扩展名白名单**：`.jpg`, `.jpeg`, `.png`, `.gif`, `.webp`, `.bmp`（`_ALLOWED_IMAGE_EXTS`）
- **文件头校验**：保存后读取 magic bytes，不是真实图片则删除 + 400 返回
- **粘贴图片**：浏览器剪贴板默认 MIME 类型为 `image/png`

**三种上传模式**：

1. **订单级图片**（传统）：上传到订单下，显示在图片区
2. **行级图片**（record_pk）：上传时关联到具体明细行，hover tooltip 预览
3. **AI/非AI 分区**：订单图片按 `source` 字段拆分为上下两区（AI 识别图 / 手动上传图）

**共享弹框**：`_image_upload_modal.html` 和 `_smart_add_modal.html` 被多个模板 include 复用。

**局部刷新**：三套订单均已实现 fetch + DOM 局部更新，无整页 reload。fallback `location.reload()` 仅在 DOM 节点找不到时触发。

### 7. 订单锁定

- 每个订单有 `is_locked` 字段
- 锁定后隐藏添加明细表单，禁用删除按钮
- 锁定切换使用 POST + 表单提交（非 fetch），确保有确认弹窗和页面刷新

### 8. 路由顺序（关键！）

Flask 按定义顺序匹配路由。具体路由如 `/loading-orders/delete/<int:order_id>` 必须定义在通配路由如 `/loading-orders/<int:record_id>` 之前。

### 9. 表单处理

- 旧功能（经验/错误/待办/通知/维护）使用传统 POST + redirect + `flash()`
- 出货/入库/装柜统一使用 REST API（fetch + JSON，无刷新）—— 入库的 HTML form 端点已于 2026-07-16 清理
- 商品/人员/车辆管理用 POST + flash + redirect（传统风格）
- 任务流用 REST API + fetch + JSON

### 10. OCR 行级图 pipeline 共享层（2026-08-19 新增）

`blueprints/ocr_pipeline.py` 把出货/入库/装柜三套订单重复的 OCR 流水

（extract + classify + persist_match）抽到一个共享类：

- `RecordImageProcessor(image_model, *, ocr_engine_getter=None)` — 显式注入 image_model（ShippingImage / LoadingOrderImage / InboundImage），统一封装三个步骤
- 三个核心方法：
  - `extract_ocr(filepath, record, *, image_id=None, use_cached=True)` — PaddleOCR + 背景色 + 平均置信度（可选缓存命中）
  - `classify(ocr_text, record, *, with_supplement=True)` — DeepSeek → 本地 RapidFuzz 降级
  - `persist_match(*, image_id, ocr_text, avg_conf, result, record, order_id)` — 写 image.match\_* + OcrMatchEvent record_ocr + ai_match + blur_reason
- 两个编排方法：
  - `process_full(...)` — 同步全流程（入库用）
  - `process_async(...)` — 锁内 OCR+classify，锁外 persist（出货用；后台线程由调用方起）
- 模块级 `_OCR_LOCK` 与 `_ASYNC_JOBS` 由 `ocr_pipeline` 模块提供，三个蓝图 re-export 共用（用 `lambda name: get_ocr_engine(name)` 注入 `ocr_engine_getter` 让 `mock.patch` monkeypatch 能穿透到 ocr_pipeline）

加新行为（如新预处理 kind / 新事件类型 / 新比对规则）：改 `ocr_pipeline.py` 一处，三套订单自动跟随，行为漂移风险归零。**装柜端点显式传 `with_supplement=False`** 保留 2026-07-30 起的 loading 行为（不注入自适应提示词）。

### 11. 缓存控制

`app.py` 中已禁用开发缓存：

```python
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0
# 所有 HTML 响应添加 Cache-Control 头
```

### 12. 辅助单位提示与备注校验（支数换算）

每个明细行显示"辅助单位提示"列，通过 YPP（Yards Per Piece，码/支）将码数换算为支数。三层逻辑（Python 后端 + JS 前端一致）：

1. **unit='支'**：数量本身就是支数，直接显示 `X支`（无需 YPP 配置）
2. **unit='y'/'码' 且有 YPP 配置**：`qty / ypp` 换算出支数，显示 `X支+Y码`
3. **无 YPP 配置**：从备注中提取 `X支` 作为兜底

**YPP 匹配**（两轮）：

- 第一轮：找 `product_name` 匹配且 `spec_keyword` 在规格中出现的行（精确匹配）
- 第二轮：兜底取没有 `spec_keyword` 的默认行

**备注校验**（`check_remark` / `checkMismatch`）：

- 解析备注中的 `X支*Yy`（乘法语义）和 `X支+Yy`（加法语义）
- 期望值 = pieces × yards_per_piece + loose_yards，与 quantity 比较
- 不一致时：单支标粉色(info)，多支标红色(warn)

#### 导入即校验（概述 §f，2026-10-09 已闭环）

明细落库时按该行匹配到的换算规则，把「备注里的支数/件数」与「`quantity`」当场对账，不等事后人工翻单。**两条轨道**：

| 轨道              | 触发条件                                               | 规则来源                                    | 判定函数                                           | 结果字段             |
| --------------- | -------------------------------------------------- | --------------------------------------- | ---------------------------------------------- | ---------------- |
| **YPP（码基）**     | 商品在 `product_units` 配了 `is_usingyardforcounting=1` | `yards_per_piece`（int×100，消费方 `/100.0`） | `check_remark(remark, quantity, ypp)`          | `mismatch`       |
| **件数换算（纸类按件收）** | 命中 `piece_conversions` 规则（日本纸等）                    | `units_per_piece`                       | `check_piece_mismatch(remark, quantity, conv)` | `piece_mismatch` |

共用口径（两函数刻意对齐，改一处必须同步另一处）：

- 期望值 = `pieces × 每支/每件码数(张数) + loose`；`loose` 是备注里扣掉 `*` 段后剩下的裸码/裸张数之和
- 备注用 `*` 显式写了单支/单件值 → 以备注为准（`per_piece_override`），覆盖规则表配置
- 判定阈值 `abs(expected - actual) <= 0.01` → 一致（返回 `''`，无标志）
- 分级：`pieces == 1` → `info`（粉）；多件 → `warn`（红）
- **无规则 / 备注里没有「支」或「件」字 / quantity 非数字 → 一律跳过，不出标志**（宁可不报也不误报）
- 数量本身非法（空 / 含「货-」等非数字）→ 走独立标志 `qty_invalid`，此时 mismatch 为空（算术比对无从谈起）

> ⚠️ **别按 `unit` 分流**：实测确有 `unit='支'` 但数量实为**码数**的行（**出货 16 行 / 入库 26 行**，如 `露华里 quantity=390码 备注'13支'`），对这些行做 YPP 校验才有意义（13支 × 30码 = 390 ✓）。加 `unit` 守卫会漏掉这批。

**已闭环（2026-10-09）**：`_helpers.annotate_import_validation(records)` 在三页 `records/batch` 端点插入后调用，**原地**给每条 record 打标志并随响应体回传；`summarize_import_validation(records)` 汇总成顶层 `validation`。判定函数与列表页渲染**完全同一套**，已用 `tools/verify_import_validation.py` 断言「annotate 的 mismatch 必须 == 直接调 check_remark」零漂移。

**响应体字段**（字段名与列表页 `item` 同名，前端直接消费）：

| 字段                | 值                          | 说明                    |                                                           |
| ----------------- | -------------------------- | --------------------- | --------------------------------------------------------- |
| `mismatch`        | `''` / `'info'` / `'warn'` | YPP 轨道                |                                                           |
| `piece_mismatch`  | `''` / `'info'` / `'warn'` | 件数换算轨道                |                                                           |
| `qty_invalid`     | bool                       | 数量本身非法                |                                                           |
| `unit_hint`       | str                        | 辅助单位提示（如 `3支+4.5码`）   |                                                           |
| `piece_hint`      | str                        | 件数换算提示（如 `1500.0张/件`） |                                                           |
| `mismatch_detail` | dict                       | None                  | `{rule,label,expected,actual,diff,severity}`，供 hover 显示差额 |

顶层另有 `validation = {total, warn, info, qty_invalid, flagged}`。

**不落库（2026-10-09 拍板）**：mismatch 是「备注 + 数量」两个字段的算术关系，不落库也不丢信息；落库反而带来三页 × 迁移 × 备注一改就过期的成本。刷新时列表页现算即可。实测三张明细表均为 11 列、无 mismatch 字段。

**前端消费**（三页统一入口，`templates/_smart_add_modal.html`）：

- `readImportFlags(rec)` —— **优先消费服务端字段**，仅当 `mismatch`/`piece_mismatch`/`qty_invalid` 三者全 `undefined` 时才回退页面本地 JS 重算（防旧端点/异常降级）。这是「导入那一刻」与「刷新后」结论一致的关键。
- `importFlagPrefix()` / `applyImportFlagClass()` / `importFlagTitle()` —— 序号列图标 + 行底色 + hover 提示，三页共用，优先级 `qty_invalid > warn > info`（与后端 Jinja 模板一致）。
- `notifyImportValidation()` + `showImportToast()` —— 导入后弹一次汇总（只报有问题的情况，全对得上时不打扰）。⚠️ **不复用 `showToast`**：`placement_count.js` / `loading_placement_count.js` 里的 `showToast` 都在 IIFE 内部、不挂 `window`，三页全局取不到，故自建。
- 三页 build 函数改为读 `flags`：`buildShippingRecordRow`（共享模板，出货）/ `buildInboundRecordRow`（`inbound-records.html:1847`）/ `buildLoadingRecordRow`（`loading-orders.html:1314`）；`unit_hint` / `piece_hint` 也优先用服务端值。
- 入库页补了 `invalidQtyIcon()`（原先缺，三页图标语义现已统一：⚠️ 数量异常 / ❌ 数量对不上 / 💡 轻微偏差）。

**验证脚本**（新增 3 个，全部实测通过）：

- `tools/verify_import_validation.py` —— 纯函数级口径验证，用真实 `product_units` / `piece_conversions` 跑 14 条用例 + 6 条 `check_qty_invalid` 断言 + **零漂移不变量**断言。**14/14 通过，零漂移**。
- `tools/verify_import_validation_http.py` —— HTTP 端到端，三页各建临时订单（`order_num='__ZZ_TEMP_f_test__'`）打 batch 验标志，再 `SELECT id` 后按 `id IN (...)` 删（**禁 LIKE 模糊删**），跑完校验零残留。**三页全绿**（每页 13 项断言）。
- `tools/verify_import_validation_js.js` —— 前端逻辑验证，从 `_smart_add_modal.html` 抽出 6 个新函数在 Node `vm` 里实测（无框架依赖）。**29/29 通过**，重点验「**服务端优先**」：本地重算故意返回不同值时，服务端有值则本地调用次数必须为 0。

### 13. 备注汇总行（两列统计）

每个日期组底部有汇总行，两列：

- **📊 备注支数**：汇总备注中 `X支` 的支数 + 散码出现次数
- **📊 明细支数**：汇总辅助单位提示列中提取的支数

### 14. 单位归一化（码 → y）

三层防御（防止 '码' 再次写入）：

1. **模型层**：`InboundRecord.create` / `ShippingRecord.create` / `LoadingOrderRecord.create` 均自动归一化
2. **API 层**：三个蓝图的 update + batch-add 端点均检查 `unit == '码' → 'y'`
3. **前端 JS**：三个订单模板的编辑保存流程均归一化

### 15. 整列隐藏（shipping-records.html）

`data-has-jiamian` 属性标记在行上，通过 CSS 控制：有加面的行显示重点列，无加面的行隐藏该列。CSS class `columns-N`（N 为列数）由服务端 render，保证刷新后列宽正确。

### 16. 登录状态与操作员头像

`base.html` 顶栏右侧：登录后显示当前操作员头像（圆角色色块+姓名首字）+ 下拉菜单（切换身份/退出）；未登录时不显示。`inject_current_operator` 上下文注入 `current_operator` dict 到所有模板。session 里的 `Staff.is_active=0`（已离职）会主动 `session.pop('operator_id')` 避免头像永久消失假象。

### 17. OCR 标签退化预处理双轨（kind: form_nolines / glare / redstamp）

`blueprints/ocr_engine.ocr_preprocess_kind(product_name)` 双轨判定返回 `KIND_FORM_NOLINES`（磅布三文治等无表格线表单） / `KIND_GLARE`（无纺布透明膜反光） / `KIND_REDSTAMP`（杂胶/纯胶红章污染） / `None`：

- 轨 1：子串白名单（`_FORM_NOLINES_VARIANTS` / `_GLARE_VARIANTS` / `_REDSTAMP_VARIANTS`）—— 命中即返回
- 轨 2：品类 code 白名单（`_WRINKLE_CATEGORY_CODES`）—— 覆盖未来未知变体（仅 form_nolines）

每 kind 走不同预处理管线：

- `form_nolines`：调用 `_form_row_bands` 按固定行数（`_FORM_ROWS = 4`）等分图片 + 逐行 OCR + 与整图 OCR 择优；二级兜底 `_text_roi` 用暗像素投影估计文字 ROI
- `glare`：透明膜反光抑制（CLAHE 类前处理）
- `redstamp`：红章掩膜 + 擦除 → **四遍 OCR 取优**（原图 / 红章擦除 / 锐化 / **灰度**）按 `(thickness_pattern, line_count, total_chars, prefix_bonus)` 综合打分；行级修补「winner 缺品名而冒头含厚度」时借 orig 首行回填（防红章擦除误吞 LB 前缀）。**灰度 pass 是 2026-10-07 加的**：直接对原图 `_to_grayscale(img_np)`，去掉红色通道污染；实测订单 1001/6029「0.舌 → 0.6」/ 5952「0.V → 0.6」/ 5887「0.18 → 0.8」三张原 yellow 图全部命中正确厚度。**注意：灰度要用原图，不要喂红章擦除版**（擦除会把小数点一起擦掉变成「06 黑色」）

封装在 `PaddleOCREngine.extract_text(... preprocess_kind=...)`：kind 优先，旧参数 `apply_wrinkle_enhance=True` 等价于 `KIND_FORM_NOLINES`（已废弃）。

> ⚠️ **新增/改动 pass 会同步改 mock 序列**：`extract_text_with_conf` 的 `_pick_best_of_n` 按固定顺序收 pass，加灰度后 mock 的 `ocr.ocr` side_effect 列表必须补第 4 项，否则测试跑的是「pre 胜」而不是真实的打分选择。相关测试：`tests/test_redstamp_grayscale_pass.py`（新）/ `test_redstamp_line_level_fixup.py` / `test_redstamp_two_pass.py`。

**`_REDSTAMP_VARIANTS` 当前 9 条**（2026-10-07 盘点，2026-10-07 加 HA 系列）：杂胶 / 纯胶 / 环保磅布三文治 / 鱼鳞布（覆盖 7P环保LB鱼鳞布/环保LB鱼鳞布/环保HA鱼鳞布/LB鱼鳞布 等） / LB特软 / 环保路华里（覆盖 7P环保路华里/环保路华里） / 路华里（短名兜底） / HA猪皮纹（覆盖 HA猪皮纹特软/7P环保HA猪皮纹/8P环保HA猪皮纹A黑 等） / 环保HA猪皮纹（主名）。**每加一条必须配单测** `tests/test_xxx_redstamp_routing.py`，已有：LB鱼鳞布（`test_lb_fish_scale_redstamp_routing.py`）/ 环保磅布三文治（`test_eco_pangburger_redstamp_routing.py`）/ 环保路华里（`test_eco_luhuali_redstamp_routing.py`）/ HA 猪皮纹（`test_ha_zhupiwen_redstamp_routing.py`）。

**`_REDSTAMP_VARIANTS` 覆盖率审计**：`tools/_audit_redstamp_coverage.py`（2026-10-07 起）。扫 `ocr_match_event` 全表 yellow/red 事件按品名聚合，对比硬白名单副本 `REDSTAMP_WHITELIST`，输出「未覆盖 + 高频」清单。**两处需手动同步**：① `blueprints/ocr_engine.py:_REDSTAMP_VARIANTS` ② `tools/_audit_redstamp_coverage.py:REDSTAMP_WHITELIST`。阈值默认 `--min=5`，高优先级 `--min=10`，机器可读 `--json`。已知「同音近似 / OCR 误识」不算真红章污染（如「露华里」OCR 读「路华里」，AI 比对兼容判定 yellow，是 AI 比对策略问题不是 OCR 红章盲区）。

### 18. 移动端图片旋转（rotate_deg）+ 糊图标记（avg_conf < 0.5）

`blueprints/_helpers.apply_user_rotation(filepath, rotate_deg)`：移动端拍照常把横屏拍成竖屏（90° 旋转）。上传时带 `rotate_deg`（90/180/270），Pillow 落盘后再走 OCR，避免误识。订单级 + 行级上传端点都接受该参数（2026-08-10 起）。

`blueprints/ocr_pipeline._append_blur_reason_if_low_conf(image_model, image_id, avg_conf, threshold=0.5)`：移动端拍照易糊，PaddleOCR 仍会跑出文字但平均置信度偏低。`avg_conf < 0.5` 时在 `image.reason` 末尾追加 `[图像可能模糊，建议重拍]`，前端 hover 提示重拍；幂等，marker 已存在不重复追加。

### 19. match-col 服务端不再渲染 → JS 动态补建

`templates/shipping-records.html` 服务端不再输出 `match-col` 单元格，改由 `initAllMatchColumns()` 在 DOMContentLoaded 后为有图片的 record 动态补建单元格 + 行级徽章（取该 record 所有图片最差一档）。理由：服务端渲染时图片可能还没匹配完，状态会过期；JS 端拿到完整 `ShippingImage.get_by_record(...)` 后再补，状态总是新的。

### 20. 出货 YPP 核查 / focus 跳转

`/shipping-ypp-review` 页（2026-08-22 上线）单页手动扫描，按 warn→info、日期倒序排列结果，点行跳转回 `/shipping-records?focus=<record_id>&focus_date=YYYY-MM-DD` 高亮闪烁目标行（IIFE 轮询 3s，行可能由 `initAllMatchColumns` 异步补建）。日期范围不含目标时弹 toast 提示用户调整。

数据源是 `_helpers.find_ypp_mismatches(records, units_cache)` —— 与概述 §f / §12 的「导入即校验」**同一套判定口径**（内部转调 `check_remark`），区别只是：§f 是**逐行导入当场提示**，这个页是**事后批量扫描 + focus 跳转复查**。改判定逻辑时两处必须同步。

### 21. placement 三页对齐基线（2026-09-11 复核）

三页（出货 / 入库 / 装柜）**各有一组独立的 placement 体系，互不串主键**：

| 页面 | 图片来源表                                      | 标记表                       | 模型类                     | 点数 JS                        |
| -- | ------------------------------------------ | ------------------------- | ----------------------- | ---------------------------- |
| 出货 | `shipping_images(source='placement')`      | `placement_marks`         | `PlacementImage`        | `placement_count.js`（与入库共用）  |
| 入库 | `inbound_images(source='placement')`       | `inbound_placement_marks` | `InboundPlacementImage` | `placement_count.js`         |
| 装柜 | `loading_order_images(source='placement')` | `loading_placement_marks` | `LoadingPlacementImage` | `loading_placement_count.js` |

**端点结构三页完全一致**（9 个 URL 模式 / 11 条 rule，命名空间不同）：

```
POST   /api/v1/<page>-orders/records/<rid>/placement-images
GET    /api/v1/<page>-orders/records/<rid>/placement-images
GET    /api/v1/<page>-orders/placement-images/<id>
DELETE /api/v1/<page>-orders/placement-images/<id>
POST   /api/v1/<page>-orders/placement-images/<id>/detect
POST   /api/v1/<page>-orders/placement-images/<id>/mark-scale
POST   /api/v1/<page>-orders/placement-images/<id>/loose-count
POST   /api/v1/<page>-orders/placement-images/<id>/manual-count
POST   /api/v1/<page>-orders/placement-images/<id>/unload
POST   /api/v1/<page>-orders/placement-images/<id>/marks
DELETE /api/v1/<page>-orders/placement-images/<id>/marks/last
```

> ⚠️ **`/unload` 三页都有**（2026-09-11 实测 url_map 确认），早期文档"仅装柜有 /unload"的说法已失效。

**计数点口径**：`manual_count`（直接输入）优先 → 无则回退点击计数 `n_marks` → `is_unload=1` 时整体取负（`effective_zhi = -n`，用于「卸货」扣减记录总数）。这些字段存在**图片表**上，不在 marks 表上。

**期望值兜底**：`_helpers.compute_placement_expected_zhi(...)`（2026-09-03 上线）处理 `unit='支'` + 备注无支数场景 → 直接用 `quantity` 核对，出货/入库均已切换。

**排列坑**：测三页 placement 时**必传 `record_pk` / `order_pk`**（unload 端点要回写 `record_id`），否则 404；上传 multipart 用 `request.files.getlist('image')` 兼容多文件，单图也走 base64 JSON。

### 22. 免 AI 比对标签图（`source='copy_paper_label'`，2026-09-09 重构 / 2026-10-08 扩到 0108）

#### 术语定义

「**免 AI 比对标签图**」= 明细行下、**不做 OCR 识别、不与 AI 比对**的一类实拍照片图。

- **拍什么**：拷贝纸 / 日本纸 / 快巴纸 / 蜡光纸（品类 0105/0106/0107/0108）这批商品的实物照片。图上通常能看到包装纸或贴纸标签。
- **页面上显示什么**：**仍是数据库 `shipping_records` 里的商品名称和规格**（`product_name` / `specification`），跟普通行级图完全一样。识别结果**不会**回写到品名/规格列 —— 这类图不存在 OCR 文本，也就不存在"识别出的品名"这个中间产物。
- **图文怎么对上**：靠 `record_pk` 直接绑定到明细行 id，不是靠识别内容。用户先在列表里认准这一行的品名/规格，再往这一行上传图。
- **为什么免 AI 比对**：这批商品的包装贴纸描述的是**包装物**（纸/袋/箱），跟明细行的商品本体对不上；且标签多为**不规则手写**，PaddleOCR 抽不出稳定文本，DeepSeek 按常规规则比对必然误判 ✗。所以走独立的落库路径，**不进 OCR 抽取、不进 AI 比对流水线**（对应概述 §e）。
- **和普通行级图的区别**：
  |           | 普通行级图                          | 免 AI 比对标签图          |
  | --------- | ------------------------------ | ------------------- |
  | `source`  | `upload` / `ai`                | `copy_paper_label`  |
  | 跑 OCR     | 是                              | 否                   |
  | 跑 AI 比对   | 是 → 产出 `match_status` / ✓⚠✗ 徽标 | 否 → **不产生任何比对徽标**   |
  | 改写品名/规格   | 否（只产出比对结论）                     | 否（压根不识别）            |
  | 上传后触发耗时操作 | PaddleOCR + DeepSeek           | 无，仅落盘               |
  | 上传入口      | 🖼️ 普通图按钮                      | 🖼️ 标签按钮（移动端 📷 标签） |
- **和摆放图（`source='placement'`）的区别**：两者同属「不做 OCR 比对的图」，容易混。摆放图是用来**清点支数 / 对数量**的（点击计数、散码、卸货取负）；免 AI 比对标签图只是**存档**，不承担任何核对职能。一个明细行可以同时有这两类图。
- **判定**：`shipping.py:_is_no_ai_match_item()`（关键词 `拷贝/日本纸/快巴/腊光/蜡光` + 品类码 0105-0108）—— 同一个谓词也用于硬拦截普通 `/images` 上传端点。

> ⚠️ **代码标识符是历史遗留，别拿它反推中文叫法**：`copy_paper_label` / `copy-paper-images` / `.copy-paper-label-btn` / `no_ai_label.js` 这些名字沿用了最早的单一品类，但实际覆盖 4 个品类（含蜡光纸），拍的是实物照而非标签特写。中文一律以本节术语为准。

#### 实现现状

`copy_paper_images` 专表与 `CopyPaperImage` 模型**已废弃删除**（`models/_init.py:1023` 有留档注释）。改为复用订单图片表：

- 存储：`shipping_images(source='copy_paper_label', record_pk=<明细行 id>)`，与订单图同表、**不做 OCR / AI 比对**（`shipping.py` 的 `record_imgs` 过滤显式排除 `placement` 与 `copy_paper_label`）
- 上传：`POST /api/v1/shipping-orders/records/<rid>/copy-paper-images`（装柜页有同款 `POST /api/v1/loading-orders/records/<rid>/copy-paper-images`，**2026-10-15 PC + 移动端 UI 均已接**）。该端点**没有品类守卫**，只校验锁单 + 图片有效性，任何明细都能传
- 删除：走通用 `DELETE /api/v1/shipping-orders/images/<id>`（自带锁单防御 + 审计）
- 张数：**不在这类图上录入**，统一走 placement 体系（`source='placement'`）；期望张数改由**件数换算规则**算（`get_piece_conversion` / `calc_piece_quantity`），旧的 `compute_copy_paper_expected_quantity` 与 `copy_paper_total` / `copy_paper_match` / `copy_paper_expected` 字段已删除
- 常量与判定：`blueprints/shipping.py` 的 `NO_AI_LABEL_SOURCE`、`NO_AI_MATCH_CATEGORY_CODES`、`_is_no_ai_match_item()`、`_enrich_no_ai_label_for_item()`（原地写 `is_no_ai_match` / `label_images` / `has_label_image`）；装柜 `loading.py` 直接 import 复用
- 前端：`static/js/no_ai_label.js`（68 行，只保留上传/删除；2026-10-09 由 `copy_paper.js` 改名而来）+ 移动端 `mobile_shipping.py` / `loading.py` 同步富化

### 23. 独立点数工具（`point_count`，2026-08-17）

`blueprints/point_count.py` 内两个 Blueprint：`bp`（页面，前缀 `/tools/point-count`，3 个端点）+ `bp_api`（REST，前缀 `/api/v1/point-count`，16 个端点）。

用途：**临时开会话 → 连续拍照 → 标记支/散码 → 导出 CSV**，不挂订单/明细，纯独立工具。模型在 `models/point_count.py`（416 行，含独立文件白名单删除 `_safe_remove_file`）。

与摆放图清点的区别：placement 挂在某条明细行上（用来核对订单数量），point_count 是脱离业务的临时工具。

### 24. 语音录入三档管线（拆分子模块）

`blueprints/voice.py` 只保留 6 个端点（recognize / confirm / mappings CRUD），实现拆到 4 个辅助模块：

| 模块                  | 行数  | 职责              |
| ------------------- | --- | --------------- |
| `voice_pipeline.py` | 150 | 顺序编排三档降级        |
| `voice_baidu.py`    | 90  | 百度语音 API        |
| `voice_llm.py`      | 176 | LLM 纠错/补全       |
| `voice_fuzzy.py`    | 135 | 本地 RapidFuzz 兜底 |

`models/voice_mapping.py`（139 行）承载自学习映射（`voice_phrase_mapping` 表，目前 0 行）。

### 25. 日终盘点（出货页，2026-10-26 新增）

**要解决的问题**：纸质出货单是一天一张张开的，单号 `TD-YYYY-MM-DD-NNN` 本身**不连续** —— 实测 2026-09-16 最大序号是 017 但当天只有 7 单、2026-09-17 的 004 整段没录。所以**光看系统里的单号编号判断不出少录了几单**。日终时由用户把**纸面单据总数手工填进来**，和系统实际录入的单数当场对账。

**数据流**

```
用户在页面填「当日纸面单据总数」
  → POST /api/v1/shipping-orders/daily-check {date, manual_total}
  → models/daily_check.py DailyOrderCheck.upsert()   # UNIQUE(scope,date) → 覆盖不是追加
  → blueprints/_helpers.compute_daily_check_status()   # 算状态
  → 返回重算后的该日对象 → 前端局部刷新顶部卡片 + 日期级 band 两处
```

**判定口径**（`_helpers.compute_daily_check_status()`，纯函数，三页可共用）

| 条件 | status | label |
|---|---|---|
| 没填 | `unfilled` | 未盘点 |
| 系统单数 == 手填 | `ok` | ✓ 对上 |
| 系统单数 < 手填 | `short` | ⚠️ 还差 N 单没录 |
| 系统单数 > 手填 | `extra` | ⚠️ 比手填多 N 单 |

**副提示**（`flags`，不影响主状态）：`empty_records`（录了单没填明细的空壳单）+ `unlocked`（录了但没锁定）。**不做「单号序号断档」检测** —— 实测单号本身不连续，噪音太大。

**系统侧三项指标**（`models/orders.py` → `ShippingOrder.daily_stock_stats(start, end)`）：
- `total` = `COUNT(shipping_orders WHERE date = D)` —— 一张纸质出货单对应一行，可直接和手填的纸面单数比
- `empty_records` = 其中 0 条明细的单数
- `unlocked` = 其中 `is_locked = 0` 的单数

**页面上有两处入口**（同一天的数据必须同步）：
1. **顶部卡片** `#eodCard`「📋 日终盘点」—— 列出所选日期范围内每一天的 手填/系统/状态/提示，底部汇总「范围内 N 天 · 已盘点 M 天 · 对上 X 天」。日期骨架复用已有的 `ShippingOrder.count_by_date_range()`（已把 0 单日期补齐）
2. **日期级 band** `.day-band` —— 挂在订单列表里、该日期第一张单的上方，就地填写

每处都有 **💾 保存** + **✕ 清除** 两个按钮；输入框回车 = 保存。保存/清除后前端用返回的该日对象**同时刷新两处**（`renderEodDay()`）。

> ⚠️ **同一天在页面上有两个输入框**（顶部表格行 + 日期 band）。保存时必须取**按钮所在那一处**的输入框 —— 按 document 顺序全局查会拿到另一个位置的旧值（这个坑踩过一次：点 band 的保存，读到的是顶部表格的值）。见 `findEodInput(dateStr, btn)`。

> ⚠️ **`.date-group` 其实是订单级**（一个 `.date-group` = 一张出货单），同一天会有好几块。日期级 band 只能渲染一次 —— 后端给每个日期**首次出现**的那一单打 `is_day_head` 标记。
> ⚠️ **`get_groups()` 是 `ORDER BY created_at DESC`，日期会交错**（补录 / 跨日下单的单会被排到中间，实测 9 个日期出现 12 次日期切换）。所以必须按「首次出现」判定，不能按相邻两单日期是否相同来判定。

**三个 REST 端点**（`blueprints/shipping.py`）

| 端点 | 行为 |
|---|---|
| `GET /api/v1/shipping-orders/daily-check?start_date=&end_date=` | 返回区间内每天的合并对象 `days:[...]` |
| `POST /api/v1/shipping-orders/daily-check` | `{date, manual_total, remark?}` → upsert → 返回重算后的该日对象；落 `AuditLog.log('set_daily_check', ...)` |
| `DELETE /api/v1/shipping-orders/daily-check/<date>` | 清除手填值 → 回到「未盘点」 |

**扩展性**：表有 `scope` 列（`shipping` / `inbound` / `loading`），后续入库 / 装柜页要接时**零改表**，只需加端点 + 前端（模型层 `DailyOrderCheck` 已按 scope 隔离）。

### 26. 车辆维护记录：改记录 + 挂图片（2026-11-02 新增）

`/vehicle-maintenance` 页每条维护记录可**修改字段**、可挂**多张图片**，**图片显示在该记录下方**（表格里的「图片子行」，跨 7 列）。

**页面结构**

```
操作列: [✏️ 修改] [🗑️ 删除]        ← ✏️ 弹框改字段，🗑️ 删记录（连带删图）
  └─ 图片子行: 📷 图片 N 张 | 缩略图网格 | [📷 添加图片] 磁贴
       └─ 点磁贴 → 「添加图片」弹框（粘贴区 + 选文件 + 待上传队列）
```

**修改记录**：沿用本页其它表单的经典风格（POST + flash + redirect），不是 REST。✏️ 按钮把行内字段写进 `data-*` 属性 → JS 填弹框 → 表单 `action` 改成 `/vehicle-maintenance/edit/<id>` → 提交。后端 `VehicleMaintenance.update()` 逐字段 UPDATE。改记录**不动图片**。

**添加图片弹框（支持直接粘贴）**

| 能力 | 实现 |
|---|---|
| 粘贴截图 | `document` 上的 `paste` 监听（函数名 `vmHandlePaste`，单独命名便于单测）→ 从 `clipboardData.items` 取 `kind=='file'` 且 `type` 以 `image/` 开头的项 → `getAsFile()` 进待上传队列 |
| 粘贴兜底 | `clipboardData.items` 为空时读 `clipboardData.files` |
| 选文件 | 隐藏 `<input type=file multiple>`，队列去重按「同名 + 同大小」 |
| 队列 | 每项渲染缩略图 + 文件名 + `×` 移除；队列空时「上传」按钮禁用 |
| 上传 | 「上传」按钮 → `POST /api/v1/vehicle-maintenance/<id>/images`（multipart，字段名 `image`）→ 成功后缩略图就地追加（不整页刷新） |

> 粘贴监听挂在 `document` 上（不是粘贴区上），所以用户不必先点一下粘贴区也能粘；弹框关闭时不处理粘贴。
>
> ⚠️ 原生 `confirm()` 会阻塞页面，删图时用 `vmDeleteBusy` 全局锁挡住连点 —— 否则第一次删除完成、网格左移之后，第二下会落到「下一张图」的 × 上，误删两张。

**存储设计**

| 项 | 取值 | 理由 |
|---|---|---|
| 文件落点 | `upload/<YYYY-MM>/<uuid>.ext` | 项目统一图片目录；被 `tools/backup_upload_dir.py`（每日快照到 `E:\BAK_upload`）+ `backup_to_ebak()` 覆盖备份 |
| DB 存路径 | **相对路径**（`month_dir` + `file_name` 两列） | 规避**踩坑点 9**（`*_images.file_path` 存绝对盘符路径 → 换盘要跑迁移脚本改写全库）。`notice_images` 同款做法 |
| URL | `/upload/<month_dir>/<file_name>` | `blueprints/upload.py` 提供；`/upload/` 在免登录白名单内 |
| 索引 | `idx_vehicle_maint_images_record_pk` | 沿用 `idx_<table>_<fkcol>` 惯例（`notice_images` 当年漏建，这次补上） |

**校验链**（比 `notice.py` 的通知图片端点严，与订单行级图一致）：扩展名白名单 + 10MB 上限 + magic bytes → 落盘后 Pillow 真实解码。**任一张不合法 → 整体 400**，并把本次已落盘的半成品文件回滚清理。

**删除语义**（符合「upload 只增不删」铁律：只走应用内删除通道，DB 行与物理文件同时清）

- 删单张图 → `VehicleMaintenanceImage.delete()` → `models/orders._safe_remove_file(path, UPLOAD_ROOT)`（路径白名单 + `mavis-trash` 回收站可恢复）
- 删维护记录 → `VehicleMaintenance.delete()` **级联**清其全部图片（DB 行 + 文件）

**测试**：`tests/test_vehicle_maintenance_images.py`（16 项）+ `tools/verify_vehicle_maint_upload_js.js`（24 项前端逻辑断言）。

> ⚠️ **上传目录隔离**（测试铁律）：测试把 `_helpers.BASE_DIR` / `_helpers.EBAK_UPLOAD_ROOT` / `models.basic.UPLOAD_ROOT` / `blueprints.upload.BASE_DIR` 全指向项目内 `tests/.tmp_vm_img_upload/`，跑完逐文件清理 —— 生产 `upload/` 一个字节都不动。`_safe_remove_file` 的白名单也按 `UPLOAD_ROOT` 判定，补丁后即使代码有 bug 指向生产目录也会被挡下。

### 27. 消防检查记录：挂图片（2026-11-08 新增）

`/fire-checks` 台账页每条检查记录可挂**多张图片**，**图片显示在该记录下方**（表格里的「图片子行」，跨 7 列），与 §26 车辆维护页同一套交互。

**页面结构**

```
操作列: [✏️ 修改] [🗑️ 删除]        ← 🗑️ 删记录（连带删图）
  └─ 图片子行: 📷 图片 N 张 | 缩略图网格 | [📷 添加图片] 磁贴
       └─ 点磁贴 → 「添加图片」弹框（粘贴区 + 选文件 + 待上传队列）
```

**前端**：`templates/fire-checks.html` 内 `fc-*` 前缀的一套（`fc-img-grid` / `fc-img-item` / `fc-img-del` / `fc-img-add` / `fcPasteZone` / `fcHandlePaste` / `fcOpenUpload`），与车辆维护页 `vm-*` 同构但**不复用同一套类名**（两页各自内联 CSS/JS，互不耦合）。

| 能力 | 实现 |
|---|---|
| 粘贴截图 | `document` 上的 `paste` 监听（`fcHandlePaste`）→ 从 `clipboardData.items` 取 `kind=='file'` 且 `type` 以 `image/` 开头的项 → `getAsFile()` 进待上传队列 |
| 选文件 | 隐藏 `<input type=file multiple>`，队列去重按「同名 + 同大小」 |
| 上传 | `POST /api/v1/fire-checks/<id>/images`（multipart，字段名 `image`）→ 成功后缩略图就地追加（不整页刷新） |
| 放大 | 点缩略图 → 全局 `showImgPreview(src)`（`static/js/common.js` + `base.html` 的 `#imgPreviewModal`，带旋转按钮） |
| 删图 | 缩略图右上角 `×` → `DELETE /api/v1/fire-checks/images/<image_id>` → 移除 DOM + 刷新「N 张」计数 |

⚠️ 两个坑（与车辆维护页同款，别重复踩）：
> 1. 原生 `confirm()` 会阻塞页面，连点会误删「下一张图」→ 用 `fcDeleteBusy` 全局锁挡住。
> 2. 「只看待整改」筛选按 `#fcTable tbody tr` 隐藏行 —— 图片子行也是 `tr`，会被一起藏掉 → 选择器改成 `tr[data-status]`，且子行也带上 `data-status`。

**存储设计**

| 项 | 取值 | 理由 |
|---|---|---|
| 文件落点 | `upload/<YYYY-MM>/<uuid>.ext` | 项目统一图片目录；被 `tools/backup_upload_dir.py` + `backup_to_ebak()` 覆盖备份 |
| DB 存路径 | **相对路径**（`month_dir` + `file_name` 两列） | 规避**踩坑点 9**；`vehicle_maintenance_images` / `notice_images` 同款做法 |
| 表 | `fire_check_images`（`record_pk` / `file_name` / `month_dir` / `original_name` / `created_at`） | 与 `vehicle_maintenance_images` 同构 |
| 索引 | `idx_fire_check_images_record_pk` | 沿用 `idx_<table>_<fkcol>` 惯例 |

**校验链**：与 §26 一致 —— 扩展名白名单 + 10MB 上限 + magic bytes → 落盘后 Pillow 真实解码。**任一张不合法 → 整体 400**，并把本次已落盘的半成品文件回滚清理。

**删除语义**：删单张图 → `FireCheckImage.delete()`（DB 行 + 物理文件，文件进回收站可恢复）；删检查记录 → `FireCheck.delete()` **级联**清其全部图片。改记录**不动图片**。

**测试**：`tests/test_fire_check_images.py`（12 用例，含上传/回滚/页面渲染/删图/级联）。浏览器实测走真实 UI（选文件 → 上传 → 点开放大 → 删图），实测记录与图片用完即删，生产 `upload/` 无残留。

> ⚠️ **上传目录隔离**（测试铁律）：测试把 `_helpers.BASE_DIR` / `_helpers.EBAK_UPLOAD_ROOT` / `models.fire_check.UPLOAD_ROOT` / `blueprints.upload.BASE_DIR` 全指向项目内 `tests/.tmp_fc_img_upload/`，跑完逐文件清理 —— 生产 `upload/` 一个字节都不动。

### 28. 商品明细行显示商品编号（入库 2026-11-13 / 出货 2026-11-14）

入库页（`/inbound-records`）商品明细区域：**商品编号（`product.product_code`）独立成列，显示在「商品名称」之前**（2026-11-13 接入时是「规格后 `::after` 徽章」，2026-11-14 改为独立列），方便当场核对录的是哪个商品。

**出货页（`/shipping-records`）2026-11-14 已对齐**，同为 10 列、编号在品名前。唯一差异是**未匹配占位**：入库页显示 `—`，出货页显示红色「未匹配」（`.product-code-missing`，`#d63031`）。实现靠页面级钩子 `window.productCodeMissingHtml` —— 共享弹窗 `buildShippingRecordRow` 读它，未定义则回退 `—`，所以装柜页零改动。出货页规格 `th` 也加了 `class="spec-cell"`，否则 `_ensureMatchColumn` 的 `nth-child(4)` fallback 会把 AI 比对列插错位置。

**匹配口径**（`models/products.py` 新增 `Product.get_code_lookup()` / `Product.match_code()`）

- 一次查表 + 内存匹配（不逐行查库），优先级与 `Product.find_by_name_and_spec` 一致：商品名+规格精确 → 同名空规格默认行 → 名称去空格后命中
- **刻意不做「同名任一行」兜底** —— 规格对不上时给编号会误导用户（例：「白磅布三文治 0.6软」在 product 表里只有 0.4中性 / 0.6中性 / 0.8中性…，兜底会显示 02120101「0.4中性」）
- 实测：入库 1,139 条明细按此口径命中 634 条，其余**不显示编号**（宁可空着也不给错编号）

**渲染方式（独立列，为什么不再用 `::after`）**

编号是独立 `<td class="product-code-cell"><span class="product-code-badge">02100131</span></td>`，位于「商品名称」td 之前；匹配不到时显示 `—`。列宽 8%（商品名称 22→19%、规格 18→16%、备注 15→13%、辅助提示 12→11%，百分比合计仍 87%）。编号与规格分属两个 td，`.spec-cell` 的 `textContent` 依然只有规格本身 —— 全站 9 处按 class 读规格的逻辑（YPP/件数换算校验、图片叠加文字、行内编辑取值、placement 身份条、smart-add 重复检测…）不受污染。早期用 `data-product-code` + `::after` 正是为了避开「内嵌 `<span>` 污染文本」，独立列后该约束自然消失。

**列序（改动后，任何按索引取 td 的 JS 都以此为准）**

| 索引 | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 | 9 |
|---|---|---|---|---|---|---|---|---|---|---|
| 列 | 序号 | 重点 | 商品编号 | 商品名称 | 规格 | 数量 | 单位 | 备注 | 辅助单位提示 | 操作 |

行内编辑的可编辑列从 `tds[3]` 起（旧版 `tds[2]`）；保存后用 API 回传的 `r.product_code` 刷新只读的 `tds[2]`。`addRowWarning` 的 colspan 走 `targetTr.children.length` 动态计算，无需改。

| 位置 | 作用 |
|---|---|
| `models/products.py` | `Product.get_code_lookup()`（批量载入映射）+ `Product.match_code()`（按品名+规格取编号） |
| `blueprints/inbound.py` | 列表渲染回填 `record['product_code']`；新增 / 批量新增 / 修改明细的 API 回传 `product_code` |
| `templates/inbound-records.html` | 表头 + 服务端行 + `buildInboundRecordRow` 三处渲染编号列；CSS `.product-code-badge`；两处行内编辑按新列序取值、保存后刷新 `tds[2]`；搜索改按 class 取品名/规格/编号（原 `nth-child(1)/(2)` 实际读的是序号/重点列） |
| `templates/_smart_add_modal.html` | `doBatchAdd` → `buildShippingRecordRow` 三页共用，按目标表头 `th.product-code-col` 决定要不要补编号 td（出货/装柜不受影响） |
| `templates/_record_image_script.html` | `_ensureMatchColumn` 表头定位改为「优先 `th.spec-cell`，再 fallback `th:nth-child(4)`」—— 逗号选择器按 DOM 顺序返回首个匹配，入库规格变第 5 列后会插错位置 |

**测试**：`tests/test_inbound_product_code.py`（11 用例：页面渲染命中/未命中、spec 文本不被污染、三个 API 回传编号、`match_code` 四种匹配口径）。

> ⚠️ 本次**只做入库页**；出货 / 装柜页没有编号显示。共享的 `buildShippingRecordRow` 改动对那两页无副作用（那两页 API 不返回 `product_code` → 属性不会出现）。移动端 `templates/mobile/inbound-order.html` 也未接。

### 29. 入库过账（阶段 3 · 2026-11-17）

`blueprints/stock_posting.py`（服务层）+ `blueprints/stock.py`（HTTP 层）。前端在阶段 4。

**过账流程**（`POST /api/v1/inbound-orders/<id>/post`）

```
闸门① 订单必须 is_locked=1          → 否则 409「请先核查并锁定该订单」
闸门② 订单 warehouse_id 必须有效     → 否则 400「仓库不存在(id=N)」
   ↓
BEGIN IMMEDIATE（单一连接）
  逐明细行:
    ① 先查后写:stock_ledger 有 uniq_key(=inbound:{record_id}) → 幂等跳过
    ② 匹配 SKU:inventory_engine.resolve_product(品名, 规格) → 匹配不到 = no_product
    ③ 数量:非数字 / ≤0 → bad_qty
    ④ 折算:inventory_engine.convert_to_base_unit → 失败按 no_conversion / bad_base_unit
    ⑤ 写流水(普通 INSERT)+ 更新 product_stock 内部累计(不改 is_tracking)
    ⑥ balance_after = 本笔后的 (仓,SKU) 余额;occurred_at = 订单头 date
  写 stock_postings 批次 + stock_posting_skips 待处理清单
COMMIT / ROLLBACK(任何异常 → 四表全回滚)
```

**A1~A8 在本阶段的落点**

| 条 | 本阶段怎么落地 |
|---|---|
| **A1** | 每条流水记 `balance_after`；`tools/verify_stock_balance.py` 对账 0 差异；余额比较一律容差 `1e-6`，**禁止 `== 0`** |
| **A2** | `models/inventory.py` 全部写方法加 `conn=None`（传了就不 commit / 不 close）；过账服务持唯一 `BEGIN IMMEDIATE` 事务 |
| **A3** | `stock_ledger` 一律**普通 INSERT**；幂等走**先查后写**；禁用 `OR REPLACE` / `OR IGNORE` / `ON CONFLICT`（第 3 条触发器会 RAISE） |
| **A4** | `occurred_at` = **订单头 `date`**（补录上周的单归上周）；`created_at` 是记账时刻，只用于「同日先后」判定 |
| **A6** | `blueprints/inbound.py` 的 `is_locked=False` 分支先查 `stock_postings`（status='posted'）→ 有则 **403「该订单已过账,请先红字冲销后再解锁」** |
| **A7** | `models/audit.py` 的 `AuditLog.log()` **自动从 session 注入 `operator_id`**；改账端点走 `_require_operator()`，没登录 401 |

**过账不改变启用状态**（关键约定）

`is_tracking=0` 的商品 → 流水照记、内部累计照加、**对外显示仍是 0** → 结果里单列 `not_tracking` 清单提示「去盘点 / 归零后生效」。
⚠️ `not_tracking` **不进** `stock_posting_skips`（那是「没入账」的待处理清单）。

**跳过行分两类**（处置完全不同）

| reason_code | 修好数据后能否补过账 |
|---|---|
| `no_product` / `no_conversion` / `bad_base_unit` / `bad_qty` | ✅ **能** —— 再点一次过账，只补这些行，已入账的幂等跳过 |
| `not_tracking`（未启用） | ❌ **不能** —— 盘点的实际库存已含这批货，补了重复计算 |

**红字冲销**（`POST /api/v1/inbound-orders/<id>/reverse`）

写一条 `quantity` 取负、`is_reversal=1`、`uniq_key` 加 `:rev` 的流水；原流水**永不删改**；批次 `status='reversed'`。
⚠️ 冲销过的行**不能再正向过账**（正向 uniq_key 仍被原流水占着）—— 要改单据请改源头再重新建单。

**测试**：`tests/test_stock_posting.py`（21 条，覆盖开发计划 §阶段 3 的 11 条完成判据）。

### 30. ⚠️ 生产库 `product` 表的悬空外键（2026-11-17 阶段 3 发现，待修）

生产库 `product` 表带一条外键 `weight_category_id → product_weight_categories(id)`，
而 `product_weight_categories` **在整个代码库里根本不存在**（grep 全库零命中，当前 `models/_init.py` 的 DDL 里也没有）。
→ `PRAGMA foreign_keys=ON` 下 `INSERT INTO product` 直接报 `no such table: main.product_weight_categories`
→ **「产品管理」页新增商品这条路径当前是坏的**。

**根因与阶段 1.6 那三个 bug 同一类**：存量库结构与当前 DDL 漂移。阶段 1.6 是「全新库缺列」，
这次反过来 —— 老库**多**了一条指向空表的 FK。`CREATE TABLE IF NOT EXISTS` 对已存在的表**不会重建**，
所以这条 FK 一直躺着，只有真往 `product` 插行才会炸。

**修法**：`tools/fix_product_dangling_fk.py`（默认**演练**，`--apply` 才落库）
—— 重建 `product` 表，逐列逐行原样搬运，只把「父表不存在」的 FK 子句摘掉；
索引 / 触发器 / 行数 / AUTOINCREMENT 计数全部复原，**列本身保留**（`weight_category_id` 全代码库零引用，
但删列是另一件事，不在本次范围）。

⚠️ 落库前先备份：`python D:\BAK\_backup_now.py pre_fix_product_dangling_fk`

## 出货页面功能流程

### 页面结构

- 新建订单表单（日期 + 客户）→ 日期范围筛选 → 订单分组列表
- 每订单：订单头部（日期/客户/锁定）→ 明细表格 → 图片区
- 每明细行：品名/规格/数量/单位/备注/辅助提示/AI 比对列/操作列

### 图片匹配流水线（`blueprints/ocr_pipeline.RecordImageProcessor` 共享层封装）

```
上传图片（订单级 / 行级 / 摆放图）
  → 移动端可选 rotate_deg（90/180/270） Pillow 落盘后再走 OCR
  → PaddleOCR 提取文字 (~2-4s，ocr_preprocess_kind 按品名走 form_nolines/glare/redstamp 三档)
  → 背景色检测 [标签背景: 黑/白]
  → DeepSeek compare_single_record() 优先（出货 / 入库 自动注入自适应提示词；装柜显式 with_supplement=False）
  → 成功 → match_status(green/yellow/red) + match_score + reason + match_source(deepseek)
  → 失败 → fallback 本地 RapidFuzz → match_source(local_fuzzy)
  → 移动端糊图标记: avg_conf < 0.5 时 _append_blur_reason_if_low_conf() 在 reason 末尾追加 "[图像可能模糊，建议重拍]"
  → 写入 shipping_images.match_status + match_score + reason + match_source
  → 写入 ocr_match_event (record_ocr 事件, audit trail)
  → 若 DeepSeek → 额外写入 ai_match 事件(含 prompt_payload + raw_response)
```

**复用与隔离**：三套订单（出货/入库/装柜）共用 `ocr_pipeline.RecordImageProcessor`，显式注入 `image_model`（ShippingImage / LoadingOrderImage / InboundImage）。装柜端点显式传 `with_supplement=False` 保留旧行为（不注入自适应提示词）。`re-ocr` 端点强制 `use_cached=False`，`fuzzy-match` 端点优先用缓存的 `record_ocr` 事件避免重跑 PaddleOCR。

### 徽章系统

- **match_status**: green(✓/⊛) / yellow(⚠/◇) / red(✗/◆)
- **match_source**: local_fuzzy(✓/⚠/✗) vs deepseek(⊛/◇/◆)
- **human_verified** + yellow/red → 👤（绿，#10b981）
- **行级徽章**: 取该 record 所有图片最差一档
- **bg_color**: ⬛ 黑底 / ⬜ 白底（自动检测标签背景色）

### 每图按钮（img-meta 2 行布局，2026-08-19 起 Row 2 新增 re-ocr）

- **Row 1**: match-badge + manual-confirm(✓确认通过)/manual-undo(👤已确认) + bg-color-tag + gen-prompt(💡生成提示词)
- **Row 2**: fuzzy-match(本地 RapidFuzz，优先缓存) + re-ocr(重跑 PaddleOCR，强制 use_cached=False) + ai-judge(DeepSeek) + ocr-detail(详情弹窗)
- **异步反馈**: re-ocr / fuzzy-match / ai-judge 按钮带 `showBtnLoading()` spinner，调用完成后恢复

### OCR 详情弹窗（3 段）

1. **OCR 原文**（PaddleOCR 抽取）— `ocr_match_event.record_ocr`
2. **提示词**（完整 prompt）— `ocr_match_event.ai_match`
3. **推理结果**（原始 JSON + 人工裁决）— `ocr_match_event.ai_match` + `human_verify`

### 自适应提示词系统

- `CategoryPrompt.compose_for_record()` 拼装 layer2(大类) + layer3(规格)
- 保存到 `category_prompts` 表（scope='category'|'spec'）
- 生成弹窗显示已有提示词（第一条默认加载 + 高亮）
- 支持编辑、删除（软删）

### 共享 JS 模板

`_record_image_script.html`（1,185 行，31 个具名 `function` + 5 个箭头函数变量）：

- 通过 `{% set api_prefix %}` 参数化 URL
- 三套订单共用：shipping / loading / inbound
- 关键函数：`initAllMatchColumns`（服务端不渲染 match-col，由 JS 动态补建）/ `applyAllRecordImageOverlays` / `bindEditDeleteButtons` / `bindManualConfirmButtons` / `bindFuzzyMatchButtons` / `bindAiJudgeButtons` / `bindReOcrButtons` / `bindOcrDetailButtons` / `bindGenPromptButtons`
- 出货页底部会显式调用上述 `bindReOcrButtons()`，确保「重 OCR」按钮在刷新后也能响应

### JS 模块（`static/js/` 全部 13 个文件 / 4,752 行）

- `static/js/common.js`（151 行）—— 公共工具（`escHtml` / `confirmDialog` / `showBtnLoading` 等）
- `static/js/product_row_utils.js`（641 行）—— 商品行工具：件数换算、单位/数量校验、辅助单位提示、备注→数量自动填充、mismatch icon、行内编辑、上移/下移、汇总行（支持 `row-after-table` 与 `tbody-tr` 两种布局）。⚠️ **实测只有 `loading-orders.html` 引入**；出货/入库页各自内联复制了 5-8 个同名函数，改这个文件不会同步到那两页
- `static/js/placement_count.js`（744 行）—— 出货 + 入库共用摆放图：上传/展示/点击计数/撤销/卸货/手动支数/散码
- `static/js/loading_placement_count.js`（768 行）—— 装柜专用摆放图。⚠️ 早期文档称「与上者约 90% 重复、仅 API 前缀不同」，**现已反超** `placement_count.js`（768 > 744），两边各自演进出独立功能，不再是简单前缀差异
- `static/js/no_ai_label.js`（68 行）—— 免 AI 比对标签图上传（**不做 OCR/AI**；2026-10-09 由 `copy_paper.js` 改名而来，出货 + 装柜两页引入）
- `static/js/voice_input.js`（285 行）—— 语音录入弹框逻辑
- `static/js/point-count.js`（313 行） / `point-count-list.js`（79 行）—— 独立点数
- `static/js/mobile_*.js`（5 个 / 1,703 行）—— 移动端 blur(33) / detail(416) / inbound_detail(385) / loading_detail(356) / placement(513)

### 关键 API 模式

- **整单比对**: `/api/v1/shipping-orders/<id>/ai-match`（注入自适应提示词）
- **整单 AI 识别**: `/api/v1/shipping-orders/ai-recognize`（`moonshot` / `paddleocr` / `deepseek` / `minimax` / `paddleocr_minimax` 五选一）
- **行级上传**: `/api/v1/shipping-orders/records/<id>/images`（自动 OCR + 匹配；移动端可传 `rotate_deg`）
- **图片操作**: manual-verify / fuzzy-match / **re-ocr**（2026-08-19 新增）/ ai-judge / ocr-detail / generate-prompt-suggestion
- **YPP 核查**: `/api/v1/shipping-orders/ypp-review/scan`（全表扫描，不受日期过滤；扫描结果点行可携带 `?focus=<record_id>&focus_date=YYYY-MM-DD` 跳转回出货页高亮）
- **异步状态轮询**: `/api/v1/shipping-orders/images/<id>/match-status`（前端轮询后台比对状态）

### 行级功能

- **record-image-btn(🖼️)**: 行级上传图片，`has-image` class
- **match-col**: 行级 AI 比对徽章（取最差）—— **服务端不再渲染**，由 `initAllMatchColumns()` JS 动态补建
- 编辑/删除/移动/批量添加明细行
- 辅助单位提示（支数换算）+ 备注校验
- 加面图标（杂胶 + 加面 → 重点列网状图标）
- **摆放图（点数）**: 11 个端点（placement-images 上传 / detect 自动检测散落点 / mark-scale / loose-count / manual-count / unload 卸货 / marks 点击计数 / marks/last 撤销）；前端 `static/js/placement_count.js` 统一处理；与备注「X支*Yy / X支+Yy」自动比对并出徽章
- **placement 按钮 / ✓点数徽章的显示条件（2026-09-30 起统一按计量单位，不再按商品分类）**：`unit in ('桶','张','令')` 或 `'支' in unit_hint / piece_hint`。`令` 是拷贝纸的计量单位（`piece_hint` 形如「N张/件」不含「支」，只能按单位特判）。三页已对齐：出货 `shipping-records.html`、入库 `inbound-records.html`、装柜 `loading-orders.html`；行上 `data-count-unit` 也直接取 `item.unit`（不再由 `is_copy_paper` 切换）
- **免 AI 比对标签图双按钮**（`static/js/no_ai_label.js`）：出货 + 装柜两页都有（PC + 移动端），**入库页尚未接入**

### 数据流 — 创建订单

1. 用户填日期 + 客户 → `POST /api/v1/shipping-orders`
2. 订单 + 明细行插入 `shipping_orders` + `shipping_records`
3. 页面刷新显示订单组
4. 行级上传图片 → OCR 匹配 → badge 更新

### 数据流 — 图片匹配

1. 上传行级图片（移动端可选 `rotate_deg` Pillow 旋转） → PaddleOCR 提取文字
2. DeepSeek 优先 → fallback 本地 RapidFuzz
3. 写入 match_status/score/reason/source
4. 若 `avg_conf < 0.5` → reason 追加 "[图像可能模糊，建议重拍]"（移动端糊图修复）
5. 前端局部刷新徽章 + 行级徽章
6. 黄/红牌 → 人工确认按钮 → 确认后变 👤 绿色
7. 确认后显示 💡 生成提示词 → 可保存自适应提示词
8. 任何时候可点「重 OCR」调 `/re-ocr` 重跑 PaddleOCR（强制 `use_cached=False`），自动跟随一次本地 fuzzy 重比对

### 移动端配套（`/m/*`，2026-08-09 起免登录）

- `/m/shipping-today` 当天分组列表 + 整体图缩略图（按 `source_tag=备货照/装车照/归仓照` 分组显示 HH:MM 时间）
- `/m/shipping-today/order/<oid>` 订单详情：每行「📷 拍照」按钮 + 颜色提示 + 摆放图状态
- `/m/shipping-today/order/<oid>/placement/<record_id>` 移动端点数页：复用 PC 摆放图后端
- 移动端图片上传端点 `rotate_deg` 参数把横屏拍歪的图转正再走 OCR（避免 90° 标签误识）
- 入库 / 装柜的 `/m/*` 入口在 `inbound.py` / `loading.py` 内部

## 模板设计约定

- 所有模板继承 `base.html`
- 顶栏登录后显示 `current_operator`（头像+姓名首字+下拉：切换登录/退出）；未登录不显示
- 导航分组（6 个下拉 + 6 个单链）：
  - **运营信息**（下拉）— 公司通知、通知彩色版、业务流程、仓库布局、**OCR 事件审计**（📊）
  - **当前缺货** — 单页
  - **商品管理**（下拉）— 商品单位、商品类型、产品管理、件数换算、**分类提示词**（📚）
  - **码数报价** — 单页
  - **操作要点**（下拉）— 开单要点、换单要点、点数要点、**独立点数**（📷 工具型会话）
  - **快速录入**（下拉）— 工作经验、错误经验、待办事项、车辆维护记录
  - **任务流**（下拉）— 任务列表、新建任务、打码抢单池、人员档案、车辆档案
  - **订单操作** — 装柜订单 / 出货记录 / 入库记录 / 综合查找（4 个单链）
  - **⚖️ YPP 核查**（下拉）— 出货 / 入库 / 装柜三页 YPP 规则冲突核查
- 样式以 Tailwind CSS 为主，共享自定义 CSS 在 `static/css/app.css`（807 行）
- 移动端样式独立在 `static/css/mobile.css`（259 行）
- 共享 JS 工具函数在 `static/js/common.js`
- 每个模板底部用 `<script>` 内联实现交互 + `<script src="...">` 引入专项 JS
- 共享 include 关系（2026-10-28 实测 grep）：
  | 组件                                                                          | 出货 | 入库 | 装柜       |
  | --------------------------------------------------------------------------- | -- | -- | -------- |
  | `_record_image_script.html`                                                 | ✅  | ✅  | ✅        |
  | `_placement_count_modal.html`                                               | ✅  | ✅  | ✅        |
  | `_voice_input_modal.html`                                                   | ✅  | —  | —        |
  | `static/js/placement_count.js`                                              | ✅  | ✅  | —（装柜用 loading 版） |
  | `static/js/loading_placement_count.js`                                      | —  | —  | ✅（经清点弹框） |
  | `static/js/no_ai_label.js`                                                  | ✅  | —  | ✅        |
  | `static/js/product_row_utils.js`                                            | —  | —  | ✅        |
  | ⚠️ 最后一行是已知技术债：出货/入库页没有引 `product_row_utils.js`，而是各自内联复制了同名函数，改公共文件不会同步到这两页。 |    |    |          |
- 移动端页面在 `templates/mobile/` 子目录，由 `mobile_shipping.py` / `inbound.py` / `loading.py` 服务

## Windows 环境注意事项

1. **CRLF 换行符**: 代码库使用 Windows CRLF 换行符。注意 SQL 查询中的多行字符串替换。
2. **PowerShell 解析**: 避免在 PowerShell 中使用带有特殊字符（`&`、`)`）的内联 Python。始终先将 Python 脚本写入文件，再执行。
3. **文件路径**: 数据库 `file_path` 列存的是**绝对 Windows 路径**（如 `D:\WORKLOG-APP\upload\2026-05\file.jpg`），不是相对路径。渲染层通过 `get_relative_path()` 转换为 URL 风格供模板使用；但删除/读文件等文件系统操作直接用原值，因此换盘符必须跑迁移脚本（见"踩坑点 9"）。
4. **PowerShell 字符串**: 在 PowerShell 中执行内联 Python 时避免使用双引号字符串包裹含双引号的 SQL/JSON，会被解释器吃掉。优先用脚本文件。
5. **Python 编码**: 在 PowerShell 中执行 Python 脚本时需指定 UTF-8 编码：`python -X utf8 script.py` 或 `$env:PYTHONUTF8=1`。CMD 下执行默认为 GBK，含中文的 Python 脚本会报 `UnicodeDecodeError: 'gbk' codec can't decode ...`。
6. **torch DLL 路径**：`ocr_engine.py` 模块顶层已做 `os.add_dll_directory()` 处理 PaddleOCR → torch → shm.dll 的 Windows DLL 搜索路径问题。

## 添加新功能

### 添加订单类功能（类似出货/入库/装柜）

1. 在 `models/_init.py` 的 `init_db()` 中添加建表语句
2. 在 `models/` 下对应业务子模块中创建模型类(静态方法)：`create` / `get_all` / `get_by_id` / `update` / `delete`,并在 `models/__init__.py` re-export
3. **在 `blueprints/` 下新建蓝图**（参照 `shipping.py` / `inbound.py` / `loading.py`）：
   - 命名建议：`blueprints/{feature}.py`
   - 在蓝图文件里注册所有路由：`bp = Blueprint('feature', __name__)`
   - **HTML 接口**: `GET /feature-name`（列表） · `POST /feature-name/add`（创建） · `POST /feature-name/add-item`（添加明细） · `POST /feature-name/delete/<id>` · `POST /feature-name/lock/<id>` · `POST /feature-name/upload-image/<id>`
   - **REST API**: `POST /api/v1/...` · `PATCH/DELETE /api/v1/.../<id>` · `POST /api/v1/.../<id>/records` · `POST /api/v1/.../<id>/images`
4. 在 `app.py` 的 `create_app()` 里 `from blueprints.{feature} import bp as feature_bp` + `app.register_blueprint(feature_bp)`
5. 创建继承 `base.html` 的模板
6. 在 `templates/base.html` 导航栏添加链接

### 添加管理类功能（类似人员/车辆/件数换算）

参考 `/staff` / `/vehicles` / `/piece-conversions` 的实现：

- 模板用 Tailwind 表单
- 提交用 POST + flash + redirect（传统风格）
- 软删 + 启用切换
- 在 `models/` 下新建模型，在 `blueprints/` 下新建蓝图

## 重要文件参考

### 应用入口

- `app.py` — 204 行（`create_app()` 工厂 + 缓存控制 + 登录闸门 + 19 个 Blueprint 实例注册 + 操作员上下文 + 日志初始化 + log context 清理）


### 蓝图（30 个 .py 文件 → 21 个 Blueprint 实例，15,470 行，按行数排序，2026-11-17 实测）

| 文件                                      | 行数    | 关键内容                                                                                                                                                                     |
| --------------------------------------- | ----- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `blueprints/ocr_engine.py`              | 3,686 | 五引擎（Moonshot/PaddleOCR/DeepSeek/MiniMax/PaddleOCR+MiniMax）+ 后处理安全网 + COMPARE_PROMPT + \_wrinkle_ocr + KIND_FORM_NOLINES/KIND_GLARE/KIND_REDSTAMP 三档预处理 + `_to_grayscale` |
| `blueprints/shipping.py`                | 2,250 | 出货 REST + AI 识别 + OCR 匹配 + 行级图片 + 加面 + 自适应提示词 + placement + copy-paper + `_is_no_ai_match_item` + `/re-ocr` + `/shipping-ypp-review` + YPP scan + 日终盘点 daily-check                          |
| `blueprints/inbound.py`                 | 1,774 | 入库 REST + 行级图片 + placement（11 端点）+ 移动端 /m/* + 日本纸件数换算 + align 复制至出货 + ⭐A6 解锁守卫                                                 |
| `blueprints/loading.py`                 | 1,551 | 装柜 REST + 行级图片 + placement + copy-paper（复用 shipping `_is_no_ai_match_item` 硬拦截）+ img_cols + 移动端 /m/*                                                                     |
| `blueprints/_helpers.py`                | 1,269 | 共享：图片上传校验、YPP 匹配、支数换算、备注校验、导入即校验（annotate_import_validation / summarize_import_validation）、compute_placement_expected_zhi、compute_daily_check_status、apply_user_rotation                                                       |
| `blueprints/task_flow.py`               | 516   | 任务流 REST + 状态机 + 证据闸门 + 建单/推进/退单/退货                                                                                                                                      |
| `blueprints/stock_posting.py`           | 494   | ⭐**入库过账服务层**（阶段 3）：`post_inbound_order` / `reverse_inbound_order` / `get_stock_status` + `PostingError`；A2 单事务 BEGIN IMMEDIATE + A1/A4 写入契约 + 三道幂等防线 ★ 2026-11-17 新增 |
| `blueprints/stock.py`                   | 452   | 库存查询页 + 只读 API + ⭐入库过账/冲销/状态 3 个写端点（阶段 2+3）★                                                                                                                 |
| `blueprints/ocr_pipeline.py`            | 389   | 行级图 OCR pipeline 共享层：RecordImageProcessor（三套订单共用）+ 糊图标记 + 模块级 \_OCR_LOCK/\_ASYNC_JOBS ★                                                                                  |
| `blueprints/point_count.py`             | 353   | 独立点数（bp + bp_api 两个 Blueprint）★                                                                                                                                          |
| `blueprints/inventory_engine.py`        | 259   | 库存折算/SKU 匹配引擎（纯函数，无 Flask/无 DB）：`resolve_product` L0~L5 级联 + `convert_to_base_unit` 三级优先级折算 + 重复键作废 ★ 2026-11-16 新增                                              |
| `blueprints/basic_records.py`           | 298   | 基础记录 + 车辆维护记录改字段/图片上传删除 + 消防检查图片上传删除（2026-11-08 新增）★                                                                                                  |
| `blueprints/products.py`                | 293   | 商品管理                                                                                                                                                                     |
| `blueprints/ocr_log.py`                 | 242   | 业务日志上下文 set_log_context / clear_log_context（订单/记录/操作员）                                                                                                                   |
| `blueprints/mobile_shipping.py`         | 231   | 移动端 /m/* 出货（today/order/placement）+ copy-paper 富化 ★                                                                                                                      |
| `blueprints/notice.py`                  | 220   | 通知 CRUD                                                                                                                                                                  |
| `blueprints/voice.py`                   | 217   | 语音录入端点（实现已拆到 voice\_*.py 四个辅助模块）★                                                                                                                                        |
| `blueprints/staff.py`                   | 177   | 人员档案 CRUD                                                                                                                                                                |
| `blueprints/voice_llm.py`               | 176   | 语音辅助：LLM 纠错/补全 ★                                                                                                                                                         |
| `blueprints/voice_pipeline.py`          | 150   | 语音辅助：三档降级编排 ★                                                                                                                                                            |
| `blueprints/vehicles.py`                | 140   | 车辆档案                                                                                                                                                                     |
| `blueprints/voice_fuzzy.py`             | 135   | 语音辅助：本地 RapidFuzz 兜底 ★                                                                                                                                                   |
| `blueprints/audit.py`                   | 132   | OCR 事件审计：聚合/钻取/CSV 导出 ★                                                                                                                                                  |
| `blueprints/basic_records.py`           | 298   | 基础记录 + 车辆维护记录改字段/图片上传删除 + 消防检查图片上传删除（2026-11-08 新增）★                                                                                                  |
| `blueprints/category_prompts_manage.py` | 117   | 分类提示词管理页 + REST ★                                                                                                                                                        |
| `blueprints/voice_baidu.py`             | 90    | 语音辅助：百度语音 API ★                                                                                                                                                          |
| `blueprints/info_pages.py`              | 87    | 信息展示页                                                                                                                                                                    |
| `blueprints/search.py`                  | 80    | 综合查找                                                                                                                                                                     |
| `blueprints/auth.py`                    | 67    | 登录/登出                                                                                                                                                                    |
| `blueprints/upload.py`                  | 17    | 静态文件服务                                                                                                                                                                   |
| `blueprints/__init__.py`                | 1     | 包标记                                                                                                                                                                      |

### 数据库层（18 个模块，2026-11-06 实测）

| 文件                           | 行数    | 模型数 | 关键类                                                                                                                                                                                                                                                   |
| ---------------------------- | ----- | --- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `models/orders.py`           | 3,096 | 14  | ShippingOrder / ShippingRecord / InboundOrder / InboundRecord / InboundImage / ShippingImage / PlacementImage / LoadingPlacementImage / InboundPlacementImage / OcrMatchEvent / LoadingOrder / LoadingOrderRecord / LoadingOrderImage / UnifiedSearch |
| `models/_init.py`            | 1,067 | —   | 38 张业务表 DDL（41 段 `CREATE TABLE` 含已废弃留档）+ 48 段 try/except ALTER 增量迁移                                                                                                                                                                                   |
| `models/category_prompt.py`  | 647   | 1   | CategoryPrompt + compose_for_record + classify_record：自适应提示词★                                                                                                                                                                                         |
| `models/tasks_flow.py`       | 492   | 11  | Staff/StaffDB/Task/TaskItem/TaskImage/TaskEvent 等                                                                                                                                                                                                     |
| `models/point_count.py`      | 416   | 2   | PointCountSession / PointCountImage / PointCountMark + `_safe_remove_file` 白名单删除★                                                                                                                                                                     |
| `models/products.py`         | 421   | 3   | ProductUnit / ProductCategory / Product                                                                                                                                                                                                               |
| `models/audit_query.py`      | 273   | 1   | OcrEventAudit：OCR 事件只读查询（prompt_stats/record_pairs/export_rows）★                                                                                                                                                                                      |
| `models/notice.py`           | 144   | 2   | Notice / NoticeImage                                                                                                                                                                                                                                  |
| `models/voice_mapping.py`    | 139   | 1   | VoicePhraseMapping 语音短语自学习★                                                                                                                                                                                                                           |
| `models/daily_check.py`      | 95    | 1   | DailyOrderCheck 日终盘点（手填当日单据总量）★                                                                                                                                                                                                            |
| `models/basic.py`            | 277   | 5   | WorkLog / ErrorLog / TodoItem / VehicleMaintenance（+update）/ VehicleMaintenanceImage（车辆维护图片）★                                                                                                                                                 |
| `models/piece_conversion.py` | 101   | 1   | PieceConversion                                                                                                                                                                                                                                       |
| `models/_permissions.py`     | 100   | —   | Action 枚举 + can() 函数                                                                                                                                                                                                                                  |
| `models/audit.py`            | 58    | 1   | AuditLog                                                                                                                                                                                                                                              |
| `models/__init__.py`         | 44    | —   | re-export                                                                                                                                                                                                                                             |
| `models/stock.py`            | 40    | 1   | StockOutItem                                                                                                                                                                                                                                          |
| `models/fire_check.py`       | 197   | 2   | FireCheck 消防检查记录（状态按遗留事项自动推导）+ FireCheckImage 记录图片 ★ |
| `models/_db.py`              | 22    | —   | get_db() + DB_PATH                                                                                                                                                                                                                                    |

### 模板（52 个文件 = 44 主目录 + 8 mobile/，共 21,436 行，按行数排序，2026-11-06 实测）

| 文件                             | 行数    | 说明                                                                         |
| ------------------------------ | ----- | -------------------------------------------------------------------------- |
| `shipping-records.html`        | 2,585 | 出货记录（行级图片 + 摆放图清点弹框 + YPP focus 跳转 + 标签图双按钮 + 日终盘点 + `is_no_ai_match` 守卫） |
| `inbound-records.html`         | 2,456 | 入库记录（行级图片 + **摆放图清点弹框** + 移动端入口）                                           |
| `loading-orders.html`          | 1,819 | 装柜订单（行级图片 + 摆放图清点弹框 + 移动端入口 + **免 AI 比对标签图按钮**）                 |
| `_smart_add_modal.html`        | 1,741 | 共享智能添加弹框（AI 图片识别，默认引擎 `paddleocr_minimax` + 文本批量）                          |
| `_record_image_script.html`    | 1,185 | 共享行级图片 JS（三订单 include 复用，31 具名 function + 5 箭头函数）★                         |
| `manage-category-prompts.html` | 817   | 分类提示词管理页                                                                   |
| `count-tips.html`              | 741   | 点数要点                                                                       |
| `huandan-guide.html`           | 642   | 换单要点                                                                       |
| `billing-tips.html`            | 569   | 开单要点                                                                       |
| `priceboard.html`              | 523   | 码数报价                                                                       |
| `products.html`                | 478   | 产品管理                                                                       |
| `notice-color.html`            | 477   | 通知彩色版                                                                      |
| `product-categories.html`      | 449   | 商品类型                                                                       |
| `base.html`                    | 444   | 公共布局 + 导航（6 下拉 + 6 单链 + 顶栏头像）                                              |
| `_image_upload_modal.html`     | 432   | 共享图片上传弹框（旋转 90° 落盘）                                                        |
| `notice.html`                  | 428   | 通知管理                                                                       |
| `tasks-new.html`               | 346   | 新建任务（OCR）                                                                  |
| `staff.html`                   | 309   | 人员档案                                                                       |
| `task-detail.html`             | 301   | 任务详情                                                                       |
| `vehicles.html`                | 255   | 车辆档案                                                                       |
| `unified-search.html`          | 235   | 综合查找                                                                       |
| `audit-ocr-events.html`        | 233   | OCR 事件审计页                                                                  |
| `product-units.html`           | 273   | 商品单位                                                                       |
| `stockout.html`                | 220   | 当前缺货                                                                       |
| `tasks.html`                   | 220   | 任务列表                                                                       |
| `vehicle-maintenance.html`     | 639   | 车辆维护（✏️ 改记录 + 记录下方挂多张图片 + 添加图片弹框可粘贴，2026-11-02 新增）★ |
| `_ypp_review_include.html`     | 166   | 共享 YPP 核查逻辑 + 样式（三页共用）★                                                    |
| `point-count.html`             | 157   | 独立点数会话列表 ★                                                                 |
| `_voice_input_modal.html`      | 129   | 共享语音录入弹框（仅出货页引入）                                                           |
| `piece-conversions.html`       | 125   | 件数换算                                                                       |
| `point-count-session.html`     | 124   | 独立点数会话详情 ★                                                                 |
| `index.html`                   | 118   | 首页                                                                         |
| `_placement_count_modal.html`  | 102   | 共享摆放图清点弹框（三订单共用）★                                                          |
| `coding-pool.html`             | 101   | 打码抢单池                                                                      |
| `errorlog.html`                | 72    | 错误经验                                                                       |
| `workflow.html`                | 71    | 业务流程                                                                       |
| `login.html`                   | 70    | 登录选身份                                                                      |
| `todolist.html`                | 50    | 待办事项                                                                       |
| `experience.html`              | 48    | 工作经验                                                                       |
| `warehouse.html`               | 46    | 仓库布局                                                                       |
| `shipping_ypp_review.html`     | 17    | 出货 YPP 核查页（壳，逻辑全在 include）★                                                |
| `inbound_ypp_review.html`      | 16    | 入库 YPP 核查页（壳）★                                                             |
| `loading_ypp_review.html`      | 16    | 装柜 YPP 核查页（壳）★                                                             |

- `templates/mobile/`（8 个文件，867 行）：`shipping-order.html`(229) / `inbound-order.html`(129) / `index.html`(125) / `loading-order.html`(167) / `placement-count.html`(88) / `shipping-today.html`(43) / `inbound-today.html`(43) / `loading-today.html`(43)

## 待办 / 待清理

- [x] ~~统一 `product` / `product_category` 命名~~ — 已完成
- [x] ~~图片上传后局部刷新~~ — 已完成
- [x] ~~`app.py` 按业务拆分蓝图（19 蓝图注册）~~ — 已完成
- [x] ~~单位归一化 `码 → y`~~ — 已完成
- [x] ~~辅助单位提示升级~~ — 已完成
- [x] ~~备注汇总行拆分~~ — 已完成
- [x] ~~商品分类树按 category_code 排序~~ — 已完成
- [x] ~~任务流 M1~~ — 基本完工（2026-07-22），17 task 完成 16，仅抢单池占位
- [x] ~~行级图片 OCR pipeline 共享层（`ocr_pipeline.RecordImageProcessor`，出货/入库/装柜共用）~~ — 已完成（2026-08-19）
- [x] ~~移动端当天出货 / 入库 / 装柜（`/m/*`，免登录）~~ — 已完成（2026-08-09）
- [x] ~~语音录入（百度语音 + LLM + 本地 fuzzy 三档）~~ — 已完成（2026-08-06）
- [x] ~~摆放图点数（手动支数 / 散码 / 卸货）~~ — 已完成（2026-08-15）
- [x] ~~独立点数工具（会话 + 拍照 + 标记 + 导出 CSV）~~ — 已完成（2026-08-17）
- [x] ~~出货 YPP 规则冲突核查页（`/shipping-ypp-review`）~~ — 已完成（2026-08-22）
- [x] ~~OCR 糊图标记（`avg_conf < 0.5` → reason 追加"建议重拍"）~~ — 已完成（2026-08-10）
- [x] ~~CLAUDE.md / README.md 数据对账（订单 758/199/22、audit 7023、ocr 1083、placement 2104、staff 7、product_units 77）~~ — 已完成（2026-08-23）
- [x] ~~placement 期望值兜底（`unit='支'` + 备注无支数 → 用 quantity 核对）~~ — 已完成（2026-09-03，`_helpers.compute_placement_expected_zhi`）
- [x] ~~拷贝纸/日本纸 行级双按钮 + 标签图落 `shipping_images(source='copy_paper_label')`~~ — 已完成（2026-09-09，`copy_paper_images` 专表废弃）
- [x] ~~placement 三页对齐（入库补 12 端点 + 清点弹框移植；三页统一带 `/unload`）~~ — 已完成（2026-09-09）
- [x] ~~CLAUDE.md 全量对账（路由 240/238、表 38、行数、文件行数）~~ — 已完成（2026-09-11）
- [x] ~~CLAUDE.md 二次全量对账（路由 243/244、蓝图 19、表 36 业务 / 43 索引、模板 51、JS 4,721 行、行数全表、DB 行数）~~ — 已完成（2026-09-29）
- [x] ~~CLAUDE.md 三次全量对账（路由 246/244、JS 4,720、tests 110、tools 25、ocr_engine 3,991、新增五引擎与 `_is_no_ai_match_item` 章节）~~ — 已完成（2026-10-08）
- [x] ~~CLAUDE.md 四次全量对账（路由 249/247、模板 20,647、JS 4,752、tests 115、tools 35、sql 15、upload 10,941、
      订单 1,144/331/25、标记 5,971、`no_ai_label.js` 改名、装柜标签图入口已落地）~~ — 已完成（2026-10-28）
- [x] ~~**消防检查记录（新功能，2026-11-06）**~~ — 已完成：导航「⌨️ 快速录入」下新增「🚨 消防检查记录」（`/fire-checks`）；
      新表 `fire_checks`（39 张业务表）+ `models/fire_check.py`；要素 = 日期 / 部位 / 检查事项 / 遗留事项 / 状态 / 备注；
      **状态由「遗留事项是否为空」自动推导**（有遗留=open 待整改，无遗留=closed 已闭环），修改时可手选覆盖，
      从根上避免「填了遗留事项却标成已闭环」的自相矛盾记录。回归测试 `tests/test_fire_check.py`（9 用例全绿）
- [x] ~~**消防检查记录挂图片（2026-11-08）**~~ — 已完成：`/fire-checks` 每条记录下方加「图片子行」（缩略图网格 + 添加图片磁贴 + 删除按钮）；
      点缩略图走全局 `showImgPreview` 放大（带旋转），点 `×` 删图；新表 `fire_check_images`（40 张业务表）+ `models/fire_check.py` 的 `FireCheckImage`；
      端点 `POST /api/v1/fire-checks/<id>/images` / `DELETE /api/v1/fire-checks/images/<image_id>`；删记录级联删图。
      回归测试 `tests/test_fire_check_images.py`（12 用例全绿）+ 浏览器实测（上传 → 放大 → 删图 → 级联清理，零残留）
- [x] ~~**腊光纸（0108）行没有行级图片入口**~~ — 已完成（2026-10-08）：`_is_copy_paper_item` 并入 `_is_no_ai_match_item`（0105-0108 单一谓词），PC 标签 🖼️ 按钮按 `is_no_ai_match` 渲染、移动端隐藏拍照识别改出「📷 标签」+ 徽章「标签图」
- [x] ~~**`_is_no_ai_match_item` 与 `_is_copy_paper_item` 重复查库**~~ — 已完成（2026-10-08）：两个谓词合并为 `_is_no_ai_match_item` 单一入口（0105-0108），`_is_copy_paper_item` 已彻底移除（代码/文档 0 引用），`classify_record` 调用次数从每行 2 次降到 1 次
- [x] ~~**明细导入接口不返回 mismatch 标志（概述 §f 的"导入即校验"闭环）**~~ — 已完成（2026-10-09）：`_helpers.annotate_import_validation()` + `summarize_import_validation()` 新增，三页 `records/batch` 端点在插入后调用并回传 `mismatch`/`piece_mismatch`/`qty_invalid`/`unit_hint`/`piece_hint`/`mismatch_detail` + 顶层 `validation`；前端三页 build 函数改走 `readImportFlags()`（**服务端优先**）+ `notifyImportValidation()` 弹汇总。**拍板不落库**（mismatch 由备注+数量完全决定，刷新现算）。验证：`tools/verify_import_validation.py`（14 用例 + 零漂移）+ `_http.py`（三页端到端全绿，临时数据零残留）+ `_js.js`（前端 29/29）
- [x] ~~**upload 目录纳入每日备份**~~ — 已完成（2026-10-28 上线，2026-11-06 复核）：脚本 `tools/backup_upload_dir.py` + Windows 计划任务 **`FengYuan_Upload_Daily_Backup`**（每天 23:45，SYSTEM / StartWhenAvailable，120 分钟超时）。11/05 23:45 实跑：7,249 文件 / 2,233 MB / 0 错误 / `RESULT: OK`，`LastTaskResult=0`
- [ ] **`E:\BAK_upload` 的 `--keep 7` 已把事故基线 `upload_20261028` 裁掉**（2026-11-06 复核）：`prune()` 用 `shutil.rmtree` **硬删**（不可恢复），E 盘现只剩 `upload_20261102`～`upload_20261105` 四个快照。**基线数据未丢**：`D:\BAK\upload_20261028_1120` 仍有 7,141 文件 / 2.2 GB。E 盘剩余 315 GB，按 ~2.2 GB/天 × 满 7 份 ≈ 15 GB，空间安全；要不要把 `--keep` 调大留更长历史，由用户拍板
- [ ] **`D:\worklog-app` 根目录散落 3 个库文件**（2026-11-06 复核）：`worklog.db.bak_20260926_112321`、`worklog.db.bak_20260926_141116`、`worklog_20261028_2330.db`，与真库 `worklog.db` 同目录 → 恢复时容易拿错。属破坏性操作，待用户点头后移到 `D:\BAK\`
- [ ] **pytest 全量基线水位**（2026-11-06 实测，`487 s`）：`37 failed / 858 passed / 4 skipped / 8 errors`。**全是存量欠账，与 2026-11-06 新增的消防检查功能无关**（该功能专项 9/9 全绿）。根因分组：① `ocr_engine` 无 `_enhance_wrinkle_label` 方法而测试在等它（`test_clahe_preprocessing` 7 + `test_paddleocr_wrinkle_param` 6）② CLAHE 性能预算 P95 14.5 s ≫ 300 ms（`test_compare_rows_wrinkle`）③ 测试与模板/前端实现漂移（`test_product_row_alignment` 4 + `test_inbound_verify_warning` 1 + `test_shipping_row_cell_count` 1）④ 页面请求没带 session → 拿到 302 登录跳转（`test_mobile_*` 8）⑤ fixture 缺失导致 ERROR（`test_compare_prompt_order575` 4 + `test_validation_rules` 4）。要治得按组来，不是一把梭

- [ ] **DB 里"引用但文件已丢"的断链图要治本**（2026-10-28 体检发现 **3,142 张**：2026-10 1,655 + **2026-09 1,480** + 2026-07/06 各若干）。九月那批**不是本次事故造成的**，说明历史上也丢过 —— 需查清是"删明细只删文件不删行"还是别的路径导致，并补上自动清理/自愈逻辑
- [ ] **引擎白名单 4 处重复**：`ocr_engine._VALID_ENGINES` 已是唯一真源，但 shipping / inbound / loading 三个 `ai-recognize` 端点各自硬编码了一份 5 元组 → 下次加引擎要改 4 处。建议蓝图改 `from blueprints.ocr_engine import _VALID_ENGINES`（或把 `_` 前缀去掉暴露成 `VALID_ENGINES`）
- [ ] **`PaddleOCRMiniMaxEngine` 靠私有方法 + 第二份 PaddleOCR 模型**：`DeepSeekEngine.__init__` 里 `self._ocr_engine = PaddleOCREngine()` 是新建实例而非 `get_ocr_engine('paddleocr')` 单例，复合引擎复用它的 `_ocr_image` 后，`paddleocr` + `paddleocr_minimax` 同进程会加载两份 PaddleOCR 模型。建议把 `DeepSeekEngine.__init__` 改成取单例
- [ ] **整单 `ai-match` 全单被过滤时返回误导性 400**：`records` 过滤后为空 → `ocr_text` 空 → 返回「没有可识别的行级图片」，实际语义是「本单全部明细都跳过 AI 比对」。建议在过滤后加早退分支给明确文案
- [x] ~~**恢复 5 位真实人员**~~ — 已完成（2026-11-07）：从 `D:\BAK\worklog_20260718_1432_M1_complete_final.db` 恢复 阿海(2471/司机/男/1998-02-18) / 阿祥(2472/调度/男/1988-10-06) / 刘会(2473/文员/女/2000-05-10) / 阿荒(2474/打码/男/1997-04-05) / 调试员(2475/调度)。**沿用备份里的原始 id**（2471-2475 当前无人引用，插入不动 `sqlite_sequence`=6269，保留原始 created_at）；阿海原 `vehicle_id=405` 指向的车在 7 月就已删除 → **置 NULL**，避免悬空外键。恢复前已备份 `D:\BAK\worklog_20261107_0828_before_restore_staff.db`。登录页 `/login` 已能选到这 5 人，`/staff` 档案页正常显示
- [ ] **`staff` 表 49 行测试残留待清理**（id 6220-6268：`test`×2 / `verify` / `verify2` / `smoke-tester`×42 / `portal-tester`×2 / `veh-tester`，全部 `is_active=1`）→ 建议精确 id 软删（`is_active=0`）或物理删，**先备份 `D:\BAK\`**。注意：`veh-tester`(6220) 是当前浏览器会话的操作员，删前先换登录身份
- [ ] **入库 placement 已投入使用但覆盖面很小**（`inbound_images(source='placement')` 17 张图 / `inbound_placement_marks` 仅 15 行、覆盖 2 张图）→ 建议真机再走一遍上传+点数验收
- [x] ~~**装柜拷贝纸/腊光纸前端无入口**~~ — 已完成（2026-10-15）：`loading_orders()` + `mobile_loading_order()` 调 `_enrich_no_ai_label_for_item` 富化 `is_no_ai_match` / `has_label_image` / `label_images`（预注入 `record_imgs_cache` 绕开 helper 内部 fallback 到 `ShippingImage` 的坑）；`templates/loading-orders.html` 操作列加普通 �️ 守卫 `not is_no_ai_match` + 新增 🖼️ 标签按钮（图区追加 `source='copy_paper_label'` 二级循环渲染）；`templates/mobile/loading-order.html` 加 `is_no_ai_match` 分支（徽章「标签图」/ 隐藏拍照识别 / 新增「📷 标签」按钮 + 缩略图 + fetch 脚本）；引入 `static/js/no_ai_label.js`，并修 `_image_upload_modal.html` `_copyPaperUploadSource` → `_noAiLabelUploadSource` 变量名错位(2026-10-09 rename 漏改,导致出货页此前也是半通)
- [ ] **2026-10-09 `copy_paper` → `no_ai_label` rename 残留二次扫尾**（2026-10-17）：上一条修过一波（变量名/后端逻辑），但 CSS 选择器 + 模板水印文案没扫干净，直到 2026-10-17 用户报出货页「日本纸标签图上传后没红框」才暴露：① `templates/shipping-records.html:210` 红框 CSS 选择器仍写 `.copy-paper-label-btn` → 改成 `.no-ai-label-btn`（漏改导致出货页上传标签图后红框不生效）② 两页「统一按钮尺寸」CSS 选择器列表漏 `.no-ai-label-btn`（漏改导致免 AI 比对标签图按钮比普通图按钮大）③ 两页「标签图水印」硬编码 "拷贝纸标签" / "标签图"，而不是显示该 record 的 `品名 · 规格`（漏改导致 0105-0108 行上传后看不出是哪条明细）。三处都已修，但**入库页**没接免 AI 比对入口（`copy-paper-images` 端点都缺，见上一条），意味着上述三类残留一旦入库页接入口，还会再出现一次；扫尾时一并核对入库页 CSS 选择器 / 水印文案是否同步改对
- [ ] **入库页对齐出货/装柜免 AI 比对标签图**：`inbound.py` 缺 `POST /api/v1/inbound-orders/records/<rid>/copy-paper-images` 端点 + `inbound-records.html` 普通 �️ 无 `is_no_ai_match` 守卫 + `mobile/inbound-order.html` 缺标签按钮/缩略图/fetch 脚本;工作量可沿用本次装柜改完归纳的标准模式(后端 copy-paper-images 端点 + 前端双按钮 + mobile handler + enrich helper,单页对齐)
- [ ] **`product_row_utils.js` 只被装柜页引用**（出货/入库内联复制了同名函数）→ 要么给三页都挂上，要么明确放弃共享
- [x] ~~**车辆维护记录只能增删、不能改；也没有图片**~~ — 已完成（2026-11-02）：新增 `vehicle_maintenance_images` 表 + 2 个图片端点（上传多张 / 删图）+ `POST /vehicle-maintenance/edit/<id>` 改记录；页面每条记录下方加「图片子行」（缩略图网格 + 计数 + 「📷 添加图片」磁贴），点磁贴开「添加图片」弹框（**支持 Ctrl+V 直接粘贴截图** + 多选文件 + 待上传队列）；删记录级联删图。详见 §26
- [ ] 推送本地 commits 到 GitHub（需先开梯子，见 [[GitHub 需要梯子]]）
- [x] ~~D 盘数据库备份~~ — 常态执行，脚本 `D:\BAK\_backup_now.py` + `D:\BAK\_verify_backup.py`（最近一份 `worklog_20260927_1927.db`，校验 MATCH）
- [ ] task_flow.py 中 `coding-claim`/`coding-done`/`coding-release` 三个端点 501 占位 → M2 实现
- [ ] OCR Label Profile Registry（`config/ocr_profiles.json`，4 个 profile）—— 当前在 feature 分支，待合并
- [ ] 入库 align 复制至出货：核对入库对齐逻辑能否让出货也用上（2026-08-13 在 inbound.py 加，未在 shipping.py 同步）
- [ ] 入库/装柜**缺 `/re-ocr` 端点**（出货页独占）→ 若需三页一致可补
- [ ] 入库页**缺 copy-paper 上传端点**（出货/装柜已有）
- [ ] **商品编号已接 入库 + 出货 两页**（入库 2026-11-13 / 出货 2026-11-14，均改独立列，见 §28）；**装柜页 `/loading-orders` 与移动端 `templates/mobile/inbound-order.html` 仍未接**。接的时候复用 `Product.get_code_lookup()` + `Product.match_code()`，渲染走独立 `<td class="product-code-cell">`（别内嵌进 `.spec-cell`，会污染其 `textContent`）；目标页若用 `buildShippingRecordRow` 插行，按表头 `th.product-code-col` 补 td，同时同步所有 `tds[n]` 索引与规格 `th.spec-cell`；未匹配占位按页面需求选 `—` 或钩子 `window.productCodeMissingHtml`
- [ ] 重复代码：`placement_count.js`(744) 与 `loading_placement_count.js`(768) 早期称约 90% 重复，但后者已反超 → 需重新评估哪些差异是有意为之，再决定是否参数化成单文件
- [x] ~~蓝图数量命名口径统一~~ — 已完成（2026-09-29）：全文统一为「18 个蓝图文件 / 19 个 Blueprint 实例 / 27 个 .py」

## 长期规划（2026-06-08 拍板）

**核心方向**：把出货/入库/装柜从「事后记账系统」升级为「送货流程的实时管控 + 证据链」。

### 当前进度

**阶段 1：商品库完善** ✅ 基本完成

- `product` 表从 0 行 → 1,287 行
- `product_units` 从 67 → 81 条
- `product_categories` 从 186 → 205 条
- `piece_conversions` 件数换算规则 16 条

**阶段 2：AI 验数 + 校对** ✅ 已完成

- OCR 引擎（Moonshot/PaddleOCR/DeepSeek，后扩到五引擎）+ 汇总行安全网
- 出货明细行标签 OCR 匹配（PaddleOCR + DeepSeek COMPARE_PROMPT）
- AI 比对 reason + human_verified 人工确认
- 辅助单位提示 + 备注 mismatch 红/粉行

**阶段 2.5：OCR + 移动端 + 语音录入 + 独立点数（2026-08 上线）** ✅ 已完成

- 行级图片 OCR pipeline 共享层（`ocr_pipeline.RecordImageProcessor`）：出货/入库/装柜三套共用
- 移动端当天出货/入库/装柜（`/m/*`，免登录）—— 微信里点链接即用
- 语音录入（百度语音 / LLM / 本地 fuzzy 三档），出货页直接弹框录入
- 出货 YPP 规则冲突核查页（`/shipping-ypp-review`），全表扫描 + focus 跳转高亮
- 独立点数工具（`/tools/point-count/`，会话+拍照+标记+导出 CSV）
- OCR 糊图标记 / 重 OCR / 行级图片 rotate_deg / 整体图 source_tag
- OCR 标签退化预处理（kind: form_nolines / glare / redstamp 三档）
- 分类提示词管理页 + OCR 事件审计页（`/audit/ocr-events`）

**阶段 2.7：OCR 引擎扩容 + 非 OCR 类目隔离（2026-09/10）** ✅ 已完成

- `minimax` 引擎（2026-09-17）：DeepSeek 截断自动 fallback
- `paddleocr_minimax` 复合引擎（2026-10-04）：PaddleOCR 抽字 → MiniMax 结构化 → 失败降级 DeepSeek；前端 `_smart_add_modal.html` 默认选项
- redstamp 预处理四遍 OCR（原图 / 擦除 / 锐化 / **灰度**，2026-10-07）+ `_REDSTAMP_VARIANTS` 扩容至 9 条 + `tools/_audit_redstamp_coverage.py` 覆盖率审计
- `_is_no_ai_match_item`（2026-10-04 新增 / 2026-10-08 合并 `_is_copy_paper_item`）：拷贝纸 / 日本纸 / 快巴纸 / 腊光纸（0105–0108）跳过行级 AI 比对，后端硬拦截 + 整单 ai-match 过滤 + PC/移动端按钮守卫；标签图成为这批品类的唯一图片入口
- placement 按钮 / ✓点数徽章显示条件由商品分类改为计量单位（桶/张/令/支 hint），三页对齐（2026-09-30）

**阶段 2.6：placement 三页对齐 + 拷贝纸重构（2026-09 上线）** ✅ 已完成

- placement 期望值兜底 `compute_placement_expected_zhi`（`unit='支'` + 备注无支数 → 用 quantity 核对）
- 入库补全 12 个 placement 端点 + 移植交互式清点弹框 → **三页点数能力齐平**
- 三页统一 `/unload`（卸货取负），早期"仅装柜有"的说法作废
- 拷贝纸/日本纸 行级双按钮；标签图迁入 `shipping_images(source='copy_paper_label')`，专表 `copy_paper_images` 废弃
- 移动端 `shipping-order` 同步 copy-paper 富化；入库/装柜移动端详情页 `mobile_inbound_detail.js` / `mobile_loading_detail.js`
- 环境迁移：项目根目录 `C:\Users\Administrator\worklog-app` → `D:\WORKLOG-APP`，4858 行图片绝对路径已改写（见"踩坑点 9"）

**阶段 3：任务流 M1** ✅ 基本完成

- 7 态状态机 + 证据闸门 + 权限系统
- 人员档案（6 种角色）+ 车辆档案
- 登录闸门（选身份，无密码）
- 打码抢单池占位（M2 接）

**阶段 4：多人化 + 权限** 🚧 M1 已做准备

- 身份选择登录（无密码）→ 登录闸门 → 权限枚举
- 架构已预留密码升级路径（`login_name` / `password_hash` 字段已存在但未启用）
- **移动端形态已落地（2026-08-09 起 `mobile_shipping.py` + 入库/装柜内部 `/m/*` 路由上线）**：当前走「微信浏览器」路径；WebView 套壳为长期规划
  - **近期（先落地）**：同一套 Flask 网页 + 现有响应式（`viewport` + `@media`），微信里直接开 URL 即用。零资质、零上架、发链接即用。
    - 拍照上传 OCR：原生 `<input type=file accept="image/*" capture="camera">` 即可在微信调起相机；如需「从微信聊天选图」再上 JSSDK `wx.chooseImage`（需公众号 AppID/AppSecret + 配 JS 安全域名 + 后端签名）。
    - 注意：iOS 微信原生 file 拍照会存入系统相册；JSSDK 拍照留在微信沙盒不污染个人相册。Android 行为随微信/X5 版本波动，以真机实测为准。
  - **长期（择机）**：WebView 套壳——安卓 Capacitor 打包 APK / Mac Tauri 打包 .dmg，壳内 WebView 指向本服务器 URL（方案 A：网页资源远端加载，壳几乎不更新，业务变动全在服务器改）。
    - 目的：拥有自己的 App 图标 + 调用更多原生能力（蓝牙打印、离线、推送等）；**不是为了"省上架"**——省上架是「直接分发」的副产品，套壳本身不豁免商店审核。
    - 发布：安卓直接发 APK 绕过 Play 审核；Mac 发 .dmg 需 Developer ID 签名 + 公证（跳过 App Store 人工审核，但公证不可省，否则弹「无法验证开发者」）。
    - 触发条件：当微信限制卡脖子（蓝牙/离线/推送/深度相册）时再实施；届时网页代码一行不改，仅外面包一层壳。
  - **关键判断**：微信浏览器 = 借微信的 WebView 壳（受微信约束）；套壳 = 自己的 App（不受微信限）。两者复用同一套网页，套壳时无需重写业务代码。

### 已落地的"司机端预备工作"

- [x] 任务流状态机（7 态推进 + 证据链拍照）
- [x] 权限模型（6 角色 × 13 动作）
- [x] 登录闸门 + session 身份
- [x] 司机退单 / 任务作废 / 整单退回
- [x] 人员/车辆档案管理
- [x] 三套订单辅助单位提示 + mismatch

### 给建议时的原则

- 单人能放宽的（无认证、外网风险）也要为多人用做铺垫，但**不要立刻上**重型的多用户架构
- 业务便利性（效率/防错）和工程性（架构/复用/可扩展）两手都兼顾
- 端起来（落地可用）优先于完美设计——快速出能跑的小工具，远胜长期空想

---

## 在建功能 · 库存管理（2026-11-16 启动）🚧

**目标**：给 `product` 建**按仓库隔离**的库存；入库单**过账**把数量计入对应仓库存；
库存**默认不启用**，盘点或归零后才开始计数（没盘过就没有可信基线）。

**方案文档**（**动手前先读**）：
- `docs/superpowers/plans/2026-11-15-库存管理-多仓过账.md` —— 主方案（数据模型 §三 / 口径 §二 / 决策 §十）
- `docs/superpowers/plans/2026-11-16-库存管理-开发计划.md` —— **现行阶段划分（阶段 0~8）**
- `docs/superpowers/plans/2026-11-16-库存管理-分批执行计划.md` —— ⚠️ **已作废**（B0~B7 排期被阶段划分取代）

**已拍板（2026-11-16，用户）**：
1. 订单明细表**不加** `product_code` 列 → 匹配永远靠「品名+规格」→ 引擎「重复键作废」兜底
2. 盘点**两种都要**：PC 批量粘贴 + 手机现场录入（写同一张盘点单）
3. 出货 / 装柜扣减**整体往后放** → V1 库存**只增不减**
4. 盘点 / 归零**任何登录的人都能做**（不新增权限枚举）→ **但必须写 `audit_log`**

**已落地**：
- `tools/_verify_match_precision.py` —— 口径精度复验 + 重复 SKU 检查（**改级联或建完档后必跑**）
- `D:\BAK\opening_snapshot_20261116.csv` —— 期初存档（1536 行）→ **DROP COLUMN 的唯一退路**

**阶段 0 ✅ / 阶段 1 ✅**（2026-11-16）：
- `blueprints/inventory_engine.py` —— 折算 / 匹配引擎（纯函数，259 行，35 条单测）
- `models/_init.py` —— ⭐ `uk_product_name_spec` 唯一索引：`UNIQUE(product_name, COALESCE(specification,''))`
  （挡「精确重复 + 规格留空重复」；**空格不参与**——业务要求空格原样保留，只差空格的两个 SKU 允许并存）
- `models/products.py` —— `ProductConflictError` + `find_by_name_spec()`；`blueprints/products.py` API 返回 **400 + 中文提示**
- `tools/stock_readiness_check.py` —— 过账就绪度体检（**直接 import 引擎** → 体检口径 = 上线口径）
- `tools/_export_opening_snapshot.py` —— 期初存量存档（只读库，默认演练）
- ⭐ `models/inventory.py` —— 库存 7 模型类（648 行）：`Warehouse` / `ProductStock` / `StockLedger` / `StockPosting` / `StockPostingSkip` / `StockCount` / `StockCountItem`
- ⭐ `models/_init.py` 库存段 —— 7 张表 DDL + 索引 + `warehouses` 种子行（'FR' 芙蓉仓）+ 3 张订单表 `warehouse_id` ALTER 迁移
- ⭐ `tools/stock_schema_check.py` —— 库存表结构体检（列定义 / UNIQUE / FK / 索引 / 种子行 / 存量 warehouse_id；`--idempotent` 在副本上验 init_db 幂等）
- ⭐ `tests/test_inventory_models.py` —— 85 条单测

**阶段 1.5 ✅（2026-11-16 19:45）** —— A 档地基补强（表还全空 → 零迁移成本）：
- ⭐ `models/_init.py` —— `stock_ledger` 加 **4 列**（`occurred_at` / `balance_after` / `unit_cost` / `amount`）、`stock_counts` 加 `as_of`、**3 个守卫触发器**、幂等 ALTER 迁移
- ⭐ `models/inventory.py` —— `StockLedger.create()` 接新列 + **`create_if_absent()`（先查后写的幂等入口，返回 `(ledger_id, created)`）**；`StockCount.create()` 建单即写 `as_of`
- ⭐ `tools/verify_stock_balance.py` —— **新增**（A1 对账：缓存可重算 / `balance_after` 链连续 / `occurred_at` 非空）。**演练默认；`--apply` 只重算 `product_stock.quantity` 缓存，绝不改历史流水**
- ⭐ `tools/stock_schema_check.py` —— 补 5 列预期 + 触发器检查 + **触发器体白名单扫描**
- ⭐ `tests/test_inventory_models.py` —— **85 条全绿**
- 备份：`D:\BAK\worklog_20261116_1940_pre_stage1.5_cost_columns.db`（**MATCH**，49 表）

**阶段 1.5 踩到的 3 个坑（写代码前必读）**：
1. ⚠️⚠️ **ALTER 加列必须排在 CREATE INDEX 之前** ——
   存量表没有 `occurred_at` 时，`CREATE INDEX ... ON stock_ledger(occurred_at)` 直接报
   `no such column` → **整个 `init_db()` 崩掉 → Flask 起不来**（服务直接挂）。
   顺序必须是：CREATE TABLE → **ALTER 补列** → CREATE INDEX → CREATE TRIGGER。
2. ⚠️ **`INSERT OR REPLACE` 绕得过 DELETE 触发器**（`recursive_triggers=0` 时）→ 第 3 条触发器不能省，幂等必须「先查后写」
3. ⚠️ **触发器体白名单扫描要剥掉字符串字面量** —— 否则报错文案里的字会被当成写关键字误判
4. ⚠️ **ALTER 加的列只能追加到表末尾** → 存量库列顺序与文档 CREATE 语句不一致（**无害**，SQLite 无「指定位置插列」语法；新建库逐字一致；体检脚本不校验列顺序）

**成本字段现状（2026-11-16 用户拍板）**：
- ⭐ **成本两列已落 DDL**：`stock_ledger.unit_cost` / `amount`（均可空、**V1 不写**）
  → 理由：流水有禁改触发器，**历史成本单价永远无法回填** → 现在不加列，V2 只能从零起算、期初成本缺失
- `unit_cost` 口径：**每个 `product.base_unit` 的成本**（不是原始单位的价）
- **成本核算方法（移动加权 / FIFO）V2 再定**（V1 不预设）→ 要知道：FIFO 需连带做批次表，移动加权不需
- ⚠️ `product.cost_price` 是**参考成本价**（现有字段，0 行有值），与将来的**实际加权成本**是两回事

**阶段 1.6 ✅（2026-11-16 20:25 ~ 2026-11-17 08:45）** —— FK 一致性（B 方案，用户拍板）+ 修 3 个「全新库缺列」真 bug：
- ⭐ `models/_init.py` —— 4 张库存表 DDL 改成 `{tbl}` 模板 + 各加 FK；
  新增 `_fk_pairs()` / `_rebuild_table_for_fk()`（SQLite 加不了约束 → 只能 DROP + CREATE 重建表）
- ⭐ `models/_init.py` —— `TRG_LEDGER` 触发器元组化（重建表要按原样建回，DROP 会连带删触发器和索引）
- ⭐ `models/_init.py` —— 重建顺序 **stock_postings → stock_ledger → stock_posting_skips → stock_counts**（被引用方先建）
- ⭐ `tools/stock_schema_check.py` —— FK 检查从 3 条补到 **8 条**（原来漏了 5 条，体检形同虚设）
- ⭐ `tools/check_fresh_db_schema.py` —— **新增**：全新库 vs 生产库逐表逐列对比（专治「全新库缺列」这一类 bug）
- ⭐ `tests/test_stage16_fk_rebuild.py` —— **新增 6 条**（全新库建库 / 幂等 / 重建保数据 / 补列回归）
- 备份：`D:\BAK\worklog_20261116_2025_pre_stage1.6_fk_consistency.db`（**MATCH**，49 表）
  ＋ 后置 `D:\BAK\worklog_20261117_0841_post_stage1.6_fk_consistency_and_freshdb_column_fixes.db`（**MATCH**，49 表）
- ⭐ `tools/check_rowcount_stable.py` —— **新增**：改库前后逐表行数比对（证明「这次改动没动数据」）

**阶段 1.6 踩到的坑（写代码前必读）**：
1. ⚠️⚠️ **ALTER 补列语句必须排在目标表的 `CREATE TABLE` 之后** ——
   排在前面的后果：存量库没事（表早建好了），**全新库静默失败**（表还不存在）→ 全新库缺列、生产库正常 →
   只在「换机重建 / 新人 clone 下来」时才炸，**现场极难复现**。
   本阶段实测踩中 3 处：`shipping_images` 缺 `manual_count`/`is_unload`、
   `loading_order_images` **缺 15 列**（全新库只有 5 列）、`shipping_orders` 缺 `doc_number`。
2. ⚠️⚠️ **SQLite 的 `ALTER TABLE` 加不了外键约束** → 只能 DROP + CREATE 重建表，而重建要过 4 道坎：
   ① `PRAGMA foreign_keys` **在事务内是 no-op**（必须先 commit 再关）
   ② DROP 一个还被别的表引用的父表 → 必须 `PRAGMA legacy_alter_table=ON`，否则 RENAME 时重解析 schema 遇悬空 FK 直接报错
   ③ **DROP TABLE 会连带删掉该表的索引和触发器** → 重建后必须按原样建回（且要在 RENAME 之后建）
   ④ 数据搬运**必须按列名**（不能用 `SELECT *`）—— 新旧列顺序可能不同（ALTER 追加的列在末尾）
3. ⚠️ **改表结构后，观察方的连接必须重开** —— 重建表是**另一个连接**做的，旧连接还持着旧 schema 的读快照，
   `PRAGMA foreign_key_list` 会读到**重建前**的信息（看起来像「重建没生效」）。**这是 schema 缓存，不是重建逻辑的锅。**
4. ⚠️ **DDL 模板用 `.replace('{tbl}', ...)` 不用 `.format()`** —— DDL 注释里有 `{record_id}` 等花括号，format 会炸
5. ⚠️ **FK 只当兜底，不当唯一防线** —— 阶段 3 的过账 API **仍要做应用层校验**
   （`Warehouse.get_by_id()` 查不到 → 400 + 中文提示），否则 `IntegrityError` 变成用户看不懂的 500
6. ⚠️ **FK 一上就暴露出旧测试的脏写法** —— `posting_id` 指向不存在的批次现在直接被 DB 挡下（原来能写进去）

**阶段 2 ✅（2026-11-17 10:00）** —— 库存查询（**只读**，不写任何库存数据）：
- ⭐ `blueprints/stock.py` —— **新增**（`bp` 页面 + `bp_api` API 双蓝图，`bp_api` 前缀 `/api/v1`）：
  `GET /stock` · `GET /api/v1/warehouses` · `GET /api/v1/stock` · `GET /api/v1/stock/<pid>` ·
  `GET /api/v1/stock/ledger` · `GET /api/v1/stock/export.csv`
- ⭐ `templates/stock.html` —— **新增**：仓库静态标签「🏭 芙蓉仓」+ 分类树（左栏）+ 搜索 + 状态列 + 内部累计（可折叠）+ 导出 + 行展开看流水
- ⭐ `models/inventory.py` —— `list_stock_view` / `count_stock_view` / `stats_by_warehouse` / `sum_by_warehouse` / `list_ledger` / `count_ledger`
- ⭐ `models/products.py` —— `ProductCategory.descendant_ids()`（分类子树）；`ProductCategory.create()` 改成**返回新建 id**
- `templates/base.html` —— 导航加「📦 库存」（在「商品管理」和「码数报价」之间）
- `tests/test_stock_readonly.py` —— **28 条**

**阶段 2 踩到的 3 个坑（写代码前必读）**：
1. ⚠️⚠️ **库存页主查询必须以 `product` 为驱动表** —— `product_stock` 现在 **0 行**（只在盘点/归零后才有行）。
   从 `product_stock` 出发 `INNER JOIN product` → **库存页上线当天一片空白**。
   而完成判据要求「未启用的商品要显示出来（0 + 灰色未启用标签）」→
   必须 `FROM product p LEFT JOIN product_stock ps ON ps.product_id = p.product_id AND ps.warehouse_id = ?`，
   `ps` 字段用 `COALESCE(..., 0)` 兜底。
2. ⚠️ **「内部累计」不能逐行查** —— `sum_by_product()` 是单 SKU 一次查询，1600 行 = 1600 次（N+1）→
   新增 `sum_by_warehouse()` 按仓一次 `GROUP BY product_id` 聚合。
3. ⚠️ **「CSV 与页面逐行一致」要靠结构保证，不是人工核对** —— 页面和导出**共用**同一个
   `_parse_stock_query()` + `_fetch_stock_rows()`，导出只是不加分页。筛选口径一处定义，不会分叉。
   ⚠️ 唯一差别是 **CSV 注入防护前缀**（品名 `=+-@` 开头会多一个 `'`）—— 传输层保护，不是数据变化。

**阶段 2 顺带定的 2 个口径**：
- **统计格跟随当前筛选**（分类 / 搜索 / 仅看已启用）—— 主流进销存通用做法：
  用户筛了「灰板类」，统计格就该说这批有多少，不能还报全仓的数。旁边给「全仓共 N 个」对照行。
- **分类筛选含子树** —— 选父分类要含所有后代（`ProductCategory.descendant_ids`），
  否则用户点「白坯布」看不到三级子类，会以为货丢了。
- **`/api/*` 不在登录闸门白名单里** → 库存 API **自己判 session**，没登录返回 401 + JSON（不是 500，也不是放行）。
- **`ProductCategory.create()` 原来返回 None** → 改成返回新建的 id（现有调用方忽略返回值，不受影响）。

**阶段 1 的 5 个坑（写代码前必读）**：
1. ⚠️ **SQL 片段必须 `' '.join(sql)`，不能 `''.join(sql)`** —— `list_by_warehouse` 一开始写成 `''`，
   `ORDER BY ... ASC` + `LIMIT ?` 粘成 `ASCLIMIT` → 所有库存列表 500
2. **`stock_ledger.uniq_key` 全局唯一，不按仓分组** —— 一条明细一生只过账一次，换仓也不能再过（防双重计入）
3. **`product_stock` FK → `product` 且 FK 开着** → **有库存行的商品不允许直接删**（阶段 4 做友好报错）
4. **`stock_count_items` FK → `stock_counts` 无 ON DELETE** → 删盘点单必须先删明细
5. **`models/stock.py` 已被「缺货登记 StockOutItem」占用** → 库存模型必须叫 `models/inventory.py`

**关键口径**（2026-11-16 13:30 实测，**库是活的，数字会漂**）：
入库明细 1162 行 / 365 张单 → 可过账 **1045 (89.9%)**，待人工 115，折算失败 1，数量非法 1。
匹配分布：L1 精确 962 · L2 去空格 80 · L4b 规格前缀唯一解 5。

**踩过的坑（写代码前必读）**：
1. **没有「同名任一行」兜底** —— 那 130 行是猜的（候选 SKU 1~96 个），错配比不匹配隐蔽
2. **折算规则按 `product` 表的品名查**，不能按入库单写的品名查（带空格 / 带 7P 前缀会落空）
3. `yards_per_piece = 0` 视为「无规则」（当 0 系数入账会把库存清零）
4. **同名同规格多 SKU → 该键作废**，不许取第一条（15P/21P 建档后必然出现）。
   历史上出现过 3 组（规格串行 2 组 + 规格漏填 1 组），2026-11-16 已由用户清理 → 当前 0 组；
   引擎这条规则是「不加 product_code」决策的**最后一道防线，别删**（数据库索引管不到「只差空格」的变体）
5. 期初数**只存档不采用**（它是目录图抄下来的「期初数」，不是实时库存）；库存靠盘点起算

---

## 项目背景

- **当前用户**：单人使用；同时承担外贸装柜/仓库管理业务 + 维护本工具（用户自述，2026-06-08）
- **业务目标**：确保各环节装货正确、点数正确
- **当前阶段**：任务流 M1 基本完工（2026-07-22）；阶段 2.6「placement 三页对齐 + 拷贝纸重构」已完成（2026-09-09）；项目根目录已迁至 `D:\WORKLOG-APP`（2026-09-11），已从「事后记账」升级为「任务流实时管控 + 证据链」
- **演进路线**：商品库 ✅ → AI 验数 ✅ → OCR/移动端/语音/独立点数 ✅ → 任务流 M1 ✅ → placement 三页对齐 + 拷贝纸 ✅ → 打码抢单池 M2 → 司机端/移动端深化 → 多人化 + 权限
- **影响**：给建议时要兼顾这两点——单人能放宽的（无认证、外网风险）也要为多人用做铺垫（数据隔离、操作日志、权限），但**不要立刻上**重型的多用户架构（避免过度设计）。**端起来优先**于完美设计。
- 详细规划见 [## 长期规划](#长期规划2026-06-08-拍板) 章节

---

## 踩坑点

### 1. Windows 上不要用 `aux.py` / `con.py` / `prn.py` 等保留设备名做模块名

**问题**：拆分 `models/` 包时把缺货登记类放到 `aux.py`，结果 `from models.aux import ...` 永远报 `ModuleNotFoundError`。

**原因**：`AUX`/`CON`/`PRN`/`NUL`/`COM1`/`LPT1` 是 Windows 保留设备名，文件系统层面就拒绝。`os.scandir` 和 PowerShell `Get-ChildItem` 能"看到"文件存在，但 `os.path.exists()` / Python import 系统走 `GetFileAttributes` API，**永远返回 False**。

**解决**：改名 `aux.py` → `stock.py`（业务上 StockOutItem 就是缺货登记）。

**教训**：跨平台项目要避 Windows 保留设备名，或在跨平台模块名单里加 lint 检查。

### 2. Moonshot kimi-k2.6 模型锁死 `temperature=1`

**问题**：把 AI 识别的 `temperature` 从 1 改成 0.2 想让输出更稳定，Moonshot 端返回 400（`invalid temperature: only 1 is allowed for this model`）。

**解决**：保持 `temperature=1`；用 prompt 工程 + max_tokens 调高（4000）来控制输出。

**教训**：Moonshot 的 Kimi 视觉模型跟 OpenAI 不同，温度参数受限；改模型参数前先看官方文档或小流量测试。

**维护提示（2026-07-25）**：Moonshot/DeepSeek/MiniMax 偶尔下线旧模型，出现 400 + "model not found" 时先到对应平台 docs 查当前可用模型清单再改 MODEL 常量。当前活跃模型速查表在 `ocr_engine._ACTIVE_MODELS`（`deepseek-v4-flash` / `kimi-k2.6` / `MiniMax-M3`），改模型常量时同步更新该表。

### 3. 拆分大文件时迁移脚本的"header 模板"容易漏常量

**问题**：把 `models.py` 拆成 `models/` 包时，迁移脚本用 `HEADER_TEMPLATES` 给每个子文件加 import 头，但漏了 `_db.py` 里的 `DB_PATH = os.path.join(...)` 这行常量定义。

**解决**：import 报错后手动补 DB_PATH 到 `_db.py`；回归测试立刻发现。

**教训**：迁移脚本要"完整搬运"——别只搬运类，常量、装饰器、模块级语句都要照搬，或者改用 AST 解析后再生成。

### 4. PowerShell `Remove-Item` 不进回收站，权限层会拦

**问题**：用 `Remove-Item -Force` 删临时文件被权限规则自动拒绝。

**解决**：用 `mavis-trash <path>` 替代，文件进回收站可恢复。

**教训**：Windows 下做"删文件"操作时优先用 `mavis-trash`，避免触发权限告警。

### 5. Flask `from app import app` 必须在应用根目录执行

**问题**：`python /path/to/_check.py` 在别的 cwd 下找不到 `app.py`，报错 `ModuleNotFoundError: No module named 'app'`。

**解决**：所有测试/验证脚本都用 `python -m unittest discover tests` 形式，或在脚本里 `sys.path.insert(0, os.path.dirname(__file__))` 把工作目录加进去。

**教训**：跨目录调用 Flask app 时用相对路径和 `os.path.dirname(__file__)` 定位项目根。

### 6. db 破坏性操作前必须做带时间戳的完整备份（2026-06-09 事故）

**问题**：本项目只有一个 db 文件 `worklog.db`，外加 `.bak`。曾因事故用 `Copy-Item .bak → .db` 覆盖了用户 3 天业务数据（订单/明细/图片/重做的商品分类树），**无法恢复**——SQLite 事务日志（`-journal` / `-wal`）只在事务进行中存在，commit 后立即被合并/删除。

**解决（硬规则）**：

1. 任何涉及 db 的破坏性操作（删行、覆盖、回滚、批量更新）**之前**，必须 `python D:\BAK\_backup_now.py` 完整备份一份到 `D:\BAK\worklog_YYYYMMDD_HHmm.db`（项目内 `_safe-snapshot` 从 2026-09-19 起不再放 db 快照）
2. 清理/批量 SQL **必须**先用 `SELECT id FROM ... WHERE 条件` 看返回，再把 id 列表喂给 `DELETE FROM ... WHERE id IN (...)`，禁止用 `LIKE '%xx%'` 模糊匹配删数据
3. 删文件统一用 `mavis-trash`，不要用 `Remove-Item -Force`（不进回收站，永久删除）

   **教训**：单人小项目没有 DBA 兜底，**任何"覆盖 db"的动作都是高风险操作**——`.bak` 只是 06-06 的快照，跟当前真实状态差 3 天。约定俗成的"覆盖回去"思路在这种场景下就是"丢 3 天数据"。

### 7. 新增：Windows PowerShell 下 Python 脚本中文编码问题

**问题**：在 PowerShell 中执行含中文注释/字符串的 Python 脚本时，默认 GBK 编码报 `UnicodeDecodeError`。

**解决**：CMD 下用 `python -X utf8 script.py`，PowerShell 下设 `$env:PYTHONUTF8=1` 后执行。

**教训**：PowerShell 默认 OEM 代码页与 Python 默认 UTF-8 不一致；跨环境执行带上 `-X utf8`。

### 8. 新增：`import openai` 在 except 子句内会导致 `UnboundLocalError`

**问题**：在 `try` 块中调用 `import openai`，再用 `except openai.XxxError` 捕获——如果 openai 包未安装，Python 会先报 `UnboundLocalError`（因为 `except openai.XxxError` 中的 `openai` 已经被 `try` 块里的 `import openai` 遮蔽，但 `import` 失败所以 `openai` 未绑定）。

**解决**：把 `import openai` 提到模块顶层（`ocr_engine.py` 第 17 行），跟其他顶层 import 一起。

**教训**：`except SomeLibrary.SomeError` 依赖 `SomeLibrary` 已被导入；不要把 `import SomeLibrary` 放在可能抛异常的作用域里。

### 9. 图片表 `file_path` 存的是**绝对路径**，迁移/换盘符必须批量改写（2026-09-11）

**问题**：`*_images.file_path` 列存的是绝对路径（如 `D:\WORKLOG-APP\upload\2026-05\xxx.png`），不是相对路径。全库 4,858 行（shipping 3,950 / inbound 688 / loading 219 / point_count 1）。

**两层行为不同，容易误判**：

- **渲染层不受影响** — `models/orders.py` 的 `get_relative_path()` 用 `split('upload\\')` 把前缀整段切掉，模板一律用 `img.relative_path` 拼 `/upload/<path>`，由 `blueprints/upload.py` 以 `BASE_DIR/upload` 为根 `send_from_directory` → **换盘符图片照常显示**，所以只看页面会以为没事。
- **文件系统层强依赖绝对路径正确** — 这才是真坑：
  - `models/orders.py` 的 `_safe_delete_image_file()` / `models/point_count.py` 的 `_safe_remove_file()`：白名单要求 `realpath(file_path)` 以 `<BASE_DIR>/upload` 开头，否则**静默 return False 拒绝删除**（DB 行删了、磁盘文件成孤儿，且不报错）
  - `blueprints/inbound.py` 的 AI 比对会用 `open(img['file_path'],'rb')` 直读绝对路径

**解决**：`tools/migrate_paths.py`（先 dry-run，再 `--apply`；自动备份到 `D:\BAK`）→ 全库扫描命中旧前缀的列 + `REPLACE(CAST(col AS TEXT), 旧, 新)`，同时处理反斜杠/正斜杠/无尾斜杠三种变体。**不要手改**。

**校验**：`tools/verify_paths_after_move.py` 逐表查「白名单越界 / 物理文件缺失」；`tools/evidence_file_path.py` / `tools/evidence_whitelist.py` 出对照证据。

**教训**：迁移后必须**同时**验「页面能显示」+「删除能生效」，只验前者会漏。

### 10. 表废弃要走"迁数据 + 删表 + 删模型 + 删端点"四步，别只改渲染位置（2026-09-09）

**问题**：免 AI 比对标签图最初建了专表 `copy_paper_images` + `CopyPaperImage` 模型 + 4 个专用端点。后续需求只是"把这批图从 `copy-paper-area` 挪到普通图区"，初版计划以为改渲染位置即可。

**实际落地**：更彻底——专表直接删除，免 AI 比对标签图迁入 `shipping_images(source='copy_paper_label')`，删除走通用 `DELETE /images/<id>`，4 个专用端点裁到 1 个上传端点。

**教训**：废弃一张表时，`models/_init.py` 要留注释说明「已废弃 + 迁往何处」（本项目 `_init.py:1023` 就是这么做的），否则下一个读代码的人会重新加回来。历史上 `product_units_new` 也是同类残留（现已清理）。

### 11. 临时目录里不要给生产目录建 junction（2026-10-28 · 误删 upload\2026-10 整个目录）

> ⛔ **铁律（用户 2026-10-28 明确要求，已写入记忆）**：
> **绝对不要删除 `D:\worklog-app\upload\` 及其任何子目录下的任何内容。**
> upload 是**只增不删**的 —— 删除图片只能走应用内的删除功能（它会同时删 DB 行）。
> 任何脚本、命令、清理操作都不许碰这个目录里的文件。递归删除前先 `dir /AL` 查重解析点（junction）。

**事故**：为对比测试提交，把 git worktree 建在 `D:\tmp\basecheck`，随后执行
`cmd /c mklink /J D:\tmp\basecheck\upload D:\worklog-app\upload` —— **把临时 worktree 的 `upload` 指向了生产图片目录**。
之后 `git worktree remove --force` 递归删除 worktree 时**穿过了 junction**，把真实的 `D:\worklog-app\upload\2026-10`
整个目录删掉：**3,801 个文件，其中 1,655 个仍在 DB 被引用**（页面全成裂图）。

**为什么难恢复**（四条元数据途径全断，这是最贵的部分）：

| 途径 | 结果 |
|---|---|
| `$MFT` 记录（文件名 + 簇链） | ❌ 记录被后续写入整体复用（11:16 那次把 upload 全量复制到 `D:\BAK` 时复用了刚释放的 MFT 记录） |
| USN 日志（删除事件 + FRN） | ❌ 卡在日志轮转点之前 |
| Windows 卷影副本 | ❌ D: 完全没有卷影副本 |
| 空闲空间簇雕刻 | ⚠️ 只能捞回**无主**图片（无文件名），需靠 DB 里残留的 OCR 原文反查 |

**能用的救命稻草**：`ocr_match_event.ocr_text` 里存着每张图的 **OCR 原文**（DB 里 3,142 张断链图中有 777 张带原文），
这才让「雕刻出的残图 → OCR → 文本比对 → 反查原文件名」成为可能。

**教训**：

1. **别给生产目录建 junction**（尤其是在会被递归删除的临时目录里）。要共享数据，用**复制**或**符号链接到只读副本**。
2. **Windows 上 `git worktree remove --force` 会跟随目录联接删到目标**（同理 `rm -rf`、`shutil.rmtree` 对 junction 也危险）。
3. **元数据（账本）没了就只剩雕刻** —— 所以**备份要覆盖图片，不只是 `.db`**（见下方备份约定新增的 upload 目录条款）。
4. **数据库里"引用但文件已丢"的行要定期体检**：`tools/backup_upload_dir.py --verify` 就是干这个的。
   实测本次体检发现 **2026-09 还有 1,480 张断链图**（早于本次事故，说明历史上也丢过）。

## 数据库备份约定（2026-06-10 起）

**专用备份目录**：`D:\BAK\`

**命名规则**：`worklog_YYYYMMDD_HHmm.db`（24 小时制，本地时间）

**典型命令**（推荐走脚本，自带一致性校验）：

```powershell
python D:\BAK\_backup_now.py
python D:\BAK\_verify_backup.py D:\worklog-app\worklog.db D:\BAK\worklog_<新时间戳>.db
```

等价的纯 PowerShell 写法：

```powershell
Copy-Item "D:\worklog-app\worklog.db" "D:\BAK\worklog_$(Get-Date -Format 'yyyyMMdd_HHmm').db" -Force
```

> **工作目录 = `D:\worklog-app`**（唯一真实目录）。`C:\Users\Administrator\worklog-app` 是 2026-09-11 迁移前的旧副本，**2026-09-16 起不再维护、不再改动**。
>
> 备份脚本已就位：`D:\BAK\_backup_now.py`（一键备份，固定 `src=D:\worklog-app\worklog.db`）+ `D:\BAK\_verify_backup.py`（一致性校验）+ `D:\BAK\_list_tables.py`（表清单）+ `D:\BAK\_daily_backup.py`（每日自动备份 wrapper）。

### upload 图片目录备份（2026-10-28 起 · 踩坑点 11 的直接补救）

**为什么必须补**：此前备份约定**只覆盖 `.db`**，`upload/` 下 3 GB 图片从来没备份过 —— 一次误删就彻底没了，
而恢复时 MFT 记录已被复用、USN 日志已轮转、无卷影副本，四条元数据途径全断（详见「踩坑点 11」）。

**脚本**：`tools/backup_upload_dir.py`（增量复制 + 每日快照 + 旧快照清理 + 日志 + `--verify` 体检）

```powershell
# 手动跑一次（备份到 E:，与源盘不同物理盘 —— 同盘备份挡不住磁盘故障）
python tools\backup_upload_dir.py --dst "E:\BAK_upload" --keep 7

# 只校验最新快照与源目录是否一致（不写入）→ 顺带查出"DB 引用但文件已丢"的断链
python tools\backup_upload_dir.py --verify
```

**约定**：

- 目标盘用 **`E:`**（`D:` 是源盘，同盘备份对磁盘故障无意义）；快照目录 `E:\BAK_upload\upload_YYYYMMDD\`
- 保留 **7 天**，每日一份完整快照（增量：只拷新增/变化的文件）
- 日志：`D:\BAK\logs\upload_backup_YYYY-MM-DD.log`
- **脚本不会自己注册计划任务** —— 每天自动跑需自行加一条 Windows 计划任务（与 `FengYuan_DB_Daily_Backup` 同风格，23:30 之后执行）
- ⚠️ **恢复期间禁止往 `D:` 写入任何东西**（日志、备份、编译产物都算）—— 写入会覆盖空闲簇里的待恢复数据


### 每日自动备份（2026-10-28 上线）

**Windows 计划任务**：`FengYuan_DB_Daily_Backup`

| 项 | 值 |
|---|---|
| 触发 | 每天 23:30（`CalendarTrigger` + `DaysInterval=1`） |
| 执行 | `C:\Users\Administrator\AppData\Local\Programs\Python\Python312\python.exe "D:\BAK\_daily_backup.py"` |
| 运行身份 | `SYSTEM`（`ServiceAccount` + `RunLevel=Highest`）→ 无人登录也能跑，无需存密码 |
| 错过后补跑 | `StartWhenAvailable = True` → 关机错过时点，开机后自动补 |
| 其它 | `AllowStartIfOnBatteries` / `DontStopIfGoingOnBatteries` / `MultipleInstances=IgnoreNew` / 超时 30 分钟 |

**`_daily_backup.py` 做什么**：用 `sqlite3` 在线备份 API（`Connection.backup()`，WAL 模式下对运行中的库安全）复制到 `D:\BAK\worklog_YYYYMMDD_HHMM.db` → 对**全部**表逐表比对源库/备份库行数 + `integrity_check` → 结果追加写入 `D:\BAK\logs\backup_YYYY-MM-DD.log` → 清理 30 天前的旧日志（**不动 .db 备份**）。退出码 0 = MATCH。

**查执行结果**：
```powershell
Get-ScheduledTaskInfo -TaskName FengYuan_DB_Daily_Backup   # LastTaskResult=0 即成功
Get-Content "D:\BAK\logs\backup_$(Get-Date -Format 'yyyy-MM-dd').log" -Tail 20
```

> ⚠️ 计划任务产出的 .db 文件属主是 `NT AUTHORITY\SYSTEM`（手工跑的是 `BUILTIN\Administrators`），两者都能正常读/删，不用改。

**触发时机**：

- 补录完一批订单/通知/分类后（用户明确要求时）
- 任何 db 破坏性操作前（见"踩坑点 6"）
- 每次大改 schema / 跑迁移脚本前
- 上述计划任务已覆盖日常场景，手动备份用于"刚做完大批量修改后立刻留档"

**保留策略**：D 盘容量充足，**不主动清理**——保留历史快照作为时间机器；用户主动说"清理旧的"才动手（必须用 `mavis-trash`，不许 `Remove-Item -Force`）。项目目录下的临时备份保留至少 1 份作近期参考即可，不需要全量同步到 D 盘。
>
> 当前 `D:\BAK` 已有 87 份备份（约 440 MB），按每天 19 MB 估算年增约 7 GB，D 盘余量 240 GB+，**暂不需要清理**。

---

## 商品档案批量维护 SOP（2026-11-14 沉淀 · 可复用）

> 适用场景：从**外部台账 / 截图 / Excel** 往 `product`（或其它主数据表）批量灌数据、按分类节点批量删商品、或给明细表**增删列**。
> 实战来源：2026-11-14「141 / RA弹力胶」27 条删除 → 29 条按截图回灌 → 入库页商品编号独立成列。脚本见 §F，既有踩坑点见「踩坑点 6 / 11」。

### A. 动手前先把口径分歧摆到台面上（不要猜）

外部源表和库内数据几乎必然不一致，先逐项对照并**让用户拍板**，再写第一行代码：

| 分歧 | 本次实例 | 必须确认 |
|---|---|---|
| 编号规则 | 源表后 5 条 9 位 `0206004xx`，库里统一 8 位 `020604xx`（历史导入做过归一化） | 按源表原样 / 归一化成库内规则 |
| 字段取值 | 截图库存 `02060401`=2409，删前备份库=2092；`预设售价1` 是 `****` 脱敏 | 按源表可见列 / 按备份库完整字段 / 只填核心列 |
| 空值语义 | 源表基本条码列全空、部分库存为空 | 脱敏与空格一律 `NULL`，**不要填 `0` 或 `''`** |
| 数量口径 | 「源表 29 条 vs 库里 27 条」不等于漏删 | 先查多出的行在**删前**库里是否存在（按编号 / 名称+规格 / 其它分类三路查） |

判据：库里是「编号/品名/规格/型号/主单位/辅单位/库存/辅数量」，源表是「商品编号/商品全名/…/基本条码/预设售价1」——**列头不一致就说明不是本系统页面**，别拿系统模板去套。

### B. 六步流程（删、插、改都走这一条）

1. **只读定位**：`PRAGMA table_info` / `sqlite_master` + 分类树递归 CTE，确认节点**唯一性**（同名节点、level、父链、子节点数）、页面查询口径（`Product.get_by_category()` 用 `WHERE category_id = ?`，**不递归**；叶子节点两种口径才相等）。交叉验证至少三路：按 id / 按编码前缀 / 按名称模糊。
2. **影响面预检**：分「DB 层强制约束」与「业务层软关联」两张表说清楚。前者查 `PRAGMA foreign_key_list`（入向 + 出向）、触发器、视图、`PRAGMA foreign_key_check`；后者查哪些表只存 `product_name`+`specification` 文本快照（**不存 product_id**），删主数据不破坏历史单据但会让 `Product.match_code()` / 语音 fuzzy「失明」。统计受影响的历史行数并明示。
3. **备份**：用 `sqlite3.Connection.backup()` 做 **WAL 安全在线快照** → `D:\BAK\worklog_YYYYMMDD_HHMM.db`；逐表比对行数 + `integrity_check=ok`；把回滚方式写进回复（停 5050 → 覆盖 `worklog.db` → 清 `-wal`/`-shm` → 重启）。别用裸 `Copy-Item`（WAL 下可能拿到不一致快照）。
4. **演练**：把备份载入 `:memory:`，先在副本上跑一遍目标 DELETE / INSERT，实测 FK 行为、rowcount、`integrity_check`、有无新增 FK 违规、分类节点是否仍在。**演练通过才碰生产库**。
5. **写入**：一次性维护连接 + 显式 id / 编号清单 + 事务 + `rowcount` 断言 + 双保险 `WHERE`（如 `id IN (...) AND category_id=141`）；**禁止 `LIKE '%xx%'` 模糊删**。任何断言失败立即 `rollback`，脚本非 0 退出。
6. **独立复核**：换一个进程、换一种查询方式读回**逐字段**比对；`integrity_check`；探活 5050；最后明示「遗留影响 / 未修项 / 完成-部分-未完成」三段状态。

### C. 三个必须记住的硬约束（本次踩出来的）

- ⚠️ **`product` 表有悬空外键**：`models/_init.py:1084-1088` 在 2026-10-14 `DROP TABLE product_weight_categories`，但 `product.weight_category_id` 列上的 `REFERENCES ... ON DELETE SET NULL` 子句留在 DDL 里（当时 SQLite 不支持 DROP COLUMN）。该列全表为 NULL，语义上永不触发，**但 `PRAGMA foreign_keys=ON` 时 SQLite 准备语句仍解析目标表 → 任何 INSERT / DELETE 都报 `no such table: main.product_weight_categories`**。应用 `get_db()` 显式开 FK（`models/_db.py:18`）⇒ **页面「新增商品」「删除商品」目前都是坏的**，`Product.delete()` / `DELETE /api/v1/products/<id>` 也一样。
  - 本次处置：维护连接 `PRAGMA foreign_keys = OFF`，前置断言「`weight_category_id` 非 NULL 行数=0」+「`voice_phrase_mapping` 引用=0」+ 演练通过；**不改 schema、不改应用连接**。真正修要重建 `product` 表（去掉 FK 子句），须单独授权。
- **外部 sqlite 工具默认 `foreign_keys=OFF`**：绕开应用手工删不会触发 `ON DELETE CASCADE`，以后 `voice_phrase_mapping` 有数据就会留孤儿。要删就走应用连接或手工 `PRAGMA foreign_keys=ON`。
- **时间戳口径**：新行走实际库 DDL 的 `DEFAULT CURRENT_TIMESTAMP`（**UTC**），历史行是本地时间；`models/_init.py` 里写的是 `datetime('now','localtime')`，与实际库不一致。属既存 schema 差异，写入时不要擅自「修正」。

### D. 从截图 / 台账导入的识读防错

- **分块放大**：Pillow 按「行组 × 左右列块」裁成多张 3x LANCZOS 图逐行识读，再拼成**硬编码清单**（可核对、可复跑），不要凭整张图直接生成 SQL。
- **找数据内在算术关系做断言**：本类商品满足 `库存数量 = 支数 × 每支码数 + 散码`（每支码数从 `aux_unit` 如 `1支=92码` 解析，支数/散码从 `aux_quantity` 如 `26支17码` 解析）。本次 17 行有数值全部通过（26×92+17=2409、49×100=4900）→ 能有效排除行错位 / 串行 / 数字看错。**以后同类导入必须复用这个校验思路**。
- **编号位数、8/9 位混用、规格写法差异**逐条写进清单并对照库；「看起来差不多」不算核对。
- **本次字段映射**（源表 → `product`）：`型号→model`、`基本单位→base_unit`、**`辅助单位→aux_unit`（值是 `1支=92码`，不是条码列！）**、`库存数量→stock_quantity`、`辅助数量→aux_quantity`；`barcode`/`cost_price`/`conversion_rate`/`preset_price` 留 NULL，`category_id=141`、`status=1`。
- 写入后按 `(product_name, specification)` 反查三页历史单据的编号回填命中率（本次出货 82/93、入库 78/108、装柜 5/5），把「文本快照规格写法不一致」量化出来，别笼统说「不受影响」。

### E. 明细表增删列的连带检查清单

1. 表头 / 服务端渲染行 / JS `buildXxxRow` **三处**同步；列宽百分比合计保持不溢出。
2. **全库 grep `tds[` / `cells[` / `nth-child(` / `children[`**：新增一列会让所有列索引整体 +1（本次行内编辑可编辑列从 `tds[3]` 起，保存后还要回写只读编号列 `tds[2]`）。顺带检查 `colspan` 警告行、搜索取值、行内编辑取值。
3. **共享模板按表头自适应，别写死列数**：`_smart_add_modal.html::buildShippingRecordRow` 三页共用，按目标表 `th.product-code-col` 决定补不补 td → 入库/出货 10 列、装柜 9 列（零回归）。页面差异（如未匹配占位）走 `window.productCodeMissingHtml` 钩子，由目标页定义、共享模板 `typeof` 检测后回退默认值，**别把页面专属文案硬编码进共享模板**。
4. **`_record_image_script.html::_ensureMatchColumn` 的逗号选择器按 DOM 顺序返回首个匹配**，列序变化后会把 AI 比对列插错位置 → 改成「先 `th.spec-cell`、再 fallback `nth-child`」。
5. **锁定旧实现的测试必须同步重写**（本次 `tests/test_inbound_product_code.py` 原来断言 `data-product-code` + CSS `::after`），并补 jsdom 结构验证（各页 td 数、AI 比对列位置）。
6. **改 `CLAUDE.md` 用「末尾追加 + 唯一命中」**，不要跨段落替换；改完跑 `tools/audit_claude_md_section_f.py` + grep 全量核对（见「踩坑点」与项目记忆里的 2026-10-09 事故）。

### F. 可复用脚本（都在 `tools/`，下次直接照抄骨架）

| 脚本 | 骨架 |
|---|---|
| `_delete_cat141_products.py` | 显式 id 列表 → WAL 在线备份 → FK=OFF → 断言链（前置 / rowcount / 行数 / integrity）→ 事务 → 独立复核说明 |
| `_insert_cat141_from_image.py` | 硬编码清单 → **算术自检** → 唯一编号 + 零冲突断言 → 备份 → FK=OFF 单事务插入 → **逐字段读回比对** |
| `_patch_inbound_code_column.py` | CRLF 模板多段替换，**每条替换断言命中次数 = 1**，避免静默漏改 |
| `_verify_inbound_code_column.js` | jsdom 加载模板，验证表头 / JS 建行 / 共享弹窗三页 td 数与 AI 比对列位置 |

**维护脚本通用写法**：二进制读写保 CRLF；`python -X utf8` 执行；含中文的脚本先落文件再跑；一次性脚本命名 `_` 前缀并留在 `tools/` 作审计凭证，不删除。
