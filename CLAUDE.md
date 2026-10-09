# 丰源工作台项目指南

此文件为 Claude Code (claude.ai/code) 在本仓库中工作时提供指导。

> **数据快照 = 2026-10-08**。本文件所有行数 / 文件数 / 数据行数均为该日实测值。
>
> **工作目录 = `D:\worklog-app`**（唯一真实目录）。数据库备份统一走 `D:\BAK\`，不再用项目内的 `_safe-snapshot`。
>
> 数字会随开发漂移；判断"文档是否与代码一致"时以实测为准，刷新方式见「路由结构」章节的复核命令。

## 项目概述

丰源工作台是一个基于 Flask 的工作日志与订单管理系统，已从「事后记账」演进为「任务流实时管控 + 证据链」系统。核心能力：

- **工作日志**：工作经验、错误经验、待办事项、公司通知、车辆维护
- **订单管理**：出货 / 入库 / 装柜三套平行订单（同一套架构三份实现，端点同构、主键隔离）。一条订单的完整生命周期：
  - **a. 订单来源 —— 单据 OCR + LLM 结构化导入**：装车单 / 同价调拨单等**纸质或截图单据** → PaddleOCR 抽文本 → DeepSeek 结构化成 JSON → 落 `*_records` 明细行（品名 / 规格 / 颜色 / 数量 / 支数 / 备注）。商品明细**不是手敲的**，是 OCR + LLM 从单据反解出来的；识别置信度与原文一并留存供人工复核。
  - **b. 每行两类图片 —— 标签图 + 摆放图**：每个商品明细行可挂 **标签图片**（`source='upload'` / `'ai'`，判定商品本体规格用）和 **摆放图片**（`source='placement'`，清点支数用）。两类图**同表不同 source**，可同时存在、互不干扰（标签图见 §6 + 「图片匹配流水线」，摆放图见 §21）。
  - **c. 标签图 OCR → LLM 与数据库规格比对**：标签图上传后跑 PaddleOCR 取文本，再交 DeepSeek 与该明细行的**系统规格信息**（`product_name` / `specification` / 颜色 / 厚度）做语义比对，产出 `match_status` 与 ✓⚠✗ 徽标；比对**只给结论，不改写品名/规格列**（页面规格永远以数据库为准）。源头与口径统一由 `_helpers.py` 驱动（YPP 等）。
  - **d. 摆放图点数 → 数量比对**：摆放图用于点击计数、清点散码、卸货取负，得出 `effective_zhi`（口径：`manual_count` 优先 → 回落 `n_marks` → `is_unload=1` 取负），再与商品行的 `quantity`（支数 × 单支码数 + 散码）做**总数比对**，不符处标红/提示。
  - **e. 免 AI 比对标签图（手写/不规则标签的逃生口）**：部分商品的标签是**不规则手写信息**（拷贝纸 / 日本纸 / 快巴纸 / 蜡光纸，品类 0105-0108），既跑不了可靠 OCR，AI 比对必然误判 ✗。这类行改&#x7528;**「免 AI 比对标签图」按钮**上传：图片正常存档，但**跳过 OCR 抽取、跳过 LLM 比对**，页面上品名规格仍取自数据库（详见 §22）。
  - **f. 明细导入即校验（数量 ↔ 换算规则当场对账）**：明细一落库就按该行匹配到的换算规则做**数量校验**，当场算出期望值与 `quantity` 的偏差，不把问题留到事后：① **按 y（码）卖的商品** → 走 **YPP 规则**（`get_ypp` 取 `product_units.yards_per_piece`／100.0），备注里的 `X支*Yy` / `X支+Yy` 反解出期望码数与 `quantity` 比对；② **纸类按「件」收的商品** → 走**件数换算规则**（`get_piece_conversion` 取 `piece_conversions.units_per_piece`），备注里的 `X件` / `X件+Zs张` 反解出期望张数与 `quantity` 比对；备注用 `*` 显式写了单支/单件码数时以备注为准（`per_piece_override`）。**不匹配的行立即打错误提示标志**（`mismatch` / `piece_mismatch`），偏差 ≤0.01 视为一致；单件/单支偏差 `info`（粉），多件/多支偏差 `warn`（红）。判定走 `_helpers.annotate_import_validation()`，标志**只在响应体里、不落库**，前端导入后立即显示（详见 §12）。
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
- **JS 模块**: `static/js/` 下 13 个文件（4,720 行）；`common.js`（151 行）提供公共工具；`product_row_utils.js`（640 行）**目前仅装柜页引入**（出货 / 入库各自内联复制了同名函数，见「模板设计约定」）；其余为专项模块（摆放图 / 拷贝纸 / 语音 / 独立点数 / 移动端）。各模板末尾另有 `<script>` 内联实现页面级交互
- **AI 集成**:
  - Moonshot Kimi k2.6 Vision API — 云端图片直接识别
  - PaddleOCR 3.x — 本地 CPU OCR 文字提取
  - DeepSeek v4 Flash API — OCR 文字结构化 + 明细行标签比对
- **OCR 架构**: ABC 基类 + 工厂模式（`blueprints/ocr_engine.py`），五引擎可切换
  - **2026-09-18 装车单 OCR 行合并修复**:装车单/出货单等紧凑表格(典型行距 8-12px),PaddleOCR 默认 clamp `[10,35]` 会把相邻 2 行合并为 1 行 → DeepSeek 串行错配。`_adaptive_y_threshold` clamp 已收紧到 **`[5, 20]`**(`blueprints/ocr_engine.py:1042/1054`)。装车单等有表格线单据**不要**走 `KIND_FORM_NOLINES`(那是给无表格线磅布标签用的,固定 4 行,反而会把多行压成 4 段)。调试经验见 `~/.claude/projects/D--worklog-app/memory/ocr-threshold-tuning.md`。
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
| `MOONSHOT_API_KEY`                   | Moonshot kimi-k2.6 云端视觉                                                                                     | 该引擎不可用                                               |
| `DEEPSEEK_API_KEY`                   | DeepSeek v4-flash 结构化 + `ai-match` 比对                                                                       | 比对整体不可用                                              |
| `MINIMAX_API_KEY`                    | **MiniMax-M3**：`minimax` 引擎 + **默认的 `paddleocr_minimax` 复合引擎**（2026-10-04）。端点 `https://api.minimaxi.com/v1` | `paddleocr_minimax` 本地直接判失败（不发请求）并自动降级 DeepSeek，功能不挂 |
| `BAIDU_API_KEY` / `BAIDU_SECRET_KEY` | 语音录入第一档                                                                                                     | 降级 LLM → 本地 fuzzy                                    |
| `OCR_BACKEND`                        | `ai-recognize` 端点 `engine` 字段为空时的默认引擎（5 选 1）                                                                | 默认 `moonshot`                                        |
| `OCR_LOG_LEVEL`                      | OCR/AI 日志详细度（`DEBUG` 记完整 prompt + 原始返回）                                                                     | 只记失败详情                                               |


## 目录结构

```
worklog-app/
├── app.py                  # 应用入口（204 行，工厂模式 + 登录闸门 + 19 个 Blueprint 实例注册）
├── models/                 # 数据模型包（16 个文件，36 张业务表）
│   ├── __init__.py         # 统一 re-export（42 行）
│   ├── _db.py              # get_db() + DB_PATH（22 行）
│   ├── _init.py            # init_db() 建表 + 迁移（999 行）
│   ├── _permissions.py     # 权限系统：Action 枚举（13 个）+ can() 集中校验（100 行）
│   ├── basic.py            # WorkLog / ErrorLog / TodoItem / VehicleMaintenance（134 行）
│   ├── notice.py           # Notice / NoticeImage（144 行）
│   ├── orders.py           # 三套订单 9 模型 + UnifiedSearch + OcrMatchEvent + PlacementImage / InboundPlacementImage / LoadingPlacementImage（2,967 行，最大文件）
│   ├── stock.py            # StockOutItem（40 行）
│   ├── products.py         # ProductUnit / ProductCategory / Product（302 行）
│   ├── piece_conversion.py # PieceConversion 件数换算规则（101 行）
│   ├── point_count.py      # PointCountSession / PointCountImage / PointCountMark + 文件白名单删除（416 行）★
│   ├── tasks_flow.py       # Staff/Task/TaskItem/TaskImage/TaskEvent（492 行）
│   ├── voice_mapping.py    # VoicePhraseMapping 语音短语映射（139 行）★
│   ├── audit.py            # AuditLog（58 行）
│   ├── audit_query.py      # OcrEventAudit：OCR 事件审计只读查询（273 行）
│   └── category_prompt.py  # CategoryPrompt + classify_record：自适应提示词（647 行）
├── blueprints/             # 27 个文件（18 个蓝图文件 → 19 个 Blueprint 实例 + 8 个辅助模块 + 包标记）
│   ├── __init__.py         # 包标记（1 行）
│   ├── _helpers.py         # 辅助：图片校验、YPP 匹配、支数换算、备注校验、compute_placement_expected_zhi、compute_copy_paper_expected_quantity、apply_user_rotation（898 行）
│   ├── ocr_engine.py       # 辅助：OCR 引擎抽象层五引擎（3,991 行，全项目最大文件；含 _wrinkle_ocr / KIND_FORM_NOLINES / KIND_GLARE / KIND_REDSTAMP / _to_grayscale）
│   ├── ocr_pipeline.py     # 辅助：行级图 OCR pipeline 共享层（RecordImageProcessor，三订单共用，437 行）
│   ├── ocr_log.py          # 辅助：业务日志上下文（set_log_context / clear_log_context，242 行）
│   ├── voice_pipeline.py / voice_llm.py / voice_fuzzy.py / voice_baidu.py  # 辅助：语音三档管线（150 / 176 / 135 / 90 行）★
│   ├── auth.py             # 蓝图：登录/登出（67 行）
│   ├── upload.py           # 蓝图：/upload/<path> 静态文件（17 行）
│   ├── basic_records.py    # 蓝图：经验/错误/待办/车辆维护（126 行）
│   ├── info_pages.py       # 蓝图：首页/价格板/通知/流程/仓库/要点/缺货（87 行）
│   ├── notice.py           # 蓝图：/notice + REST API（220 行）
│   ├── products.py         # 蓝图：商品管理（302 行）
│   ├── shipping.py         # 蓝图：出货页 + YPP 核查 + REST + AI/OCR + 自适应提示词 + placement + copy-paper + _is_no_ai_match_item（2,210 行）
│   ├── inbound.py          # 蓝图：入库页 + 行级图片 + placement + align 复制至出货（1,772 行）
│   ├── loading.py          # 蓝图：装柜页 + 行级图片 + placement + img_cols + copy-paper 后端（1,614 行）
│   ├── search.py           # 蓝图：/unified-search 综合查找（80 行）
│   ├── staff.py            # 蓝图：/staff 人员档案（177 行）
│   ├── vehicles.py         # 蓝图：/vehicles 车辆档案（140 行）
│   ├── task_flow.py        # 蓝图：任务流 REST + 状态机（617 行）
│   ├── voice.py            # 蓝图：/api/v1/voice/{recognize,confirm,mappings/*}（217 行）★
│   ├── category_prompts_manage.py # 蓝图：/manage/category-prompts + REST（117 行）★
│   ├── mobile_shipping.py  # 蓝图：/m/* 移动端当天出货（231 行）★
│   ├── point_count.py      # 蓝图：/tools/point-count 独立点数 + REST（408 行，含 bp+bp_api 两个 Blueprint）★
│   └── audit.py            # 蓝图：OCR 事件审计页 + 聚合/钻取/CSV（132 行）★
├── templates/              # 51 个 Jinja2 模板（43 主目录 + 8 mobile/），共 19,639 行
│   ├── base.html           # 公共布局 + 导航 + Tailwind + 顶栏头像（444 行）
│   ├── _record_image_script.html # 共享行级图片 JS（1,185 行，31 具名 function + 5 箭头函数 + 3 个 window 导出，三订单共用）
│   ├── _smart_add_modal.html    # 共享智能添加弹框（1,584 行）
│   ├── _image_upload_modal.html # 共享图片上传弹框（430 行）
│   ├── _placement_count_modal.html # 共享摆放图清点弹框（102 行，三订单共用）
│   ├── _voice_input_modal.html   # 共享语音录入弹框（129 行，仅出货页引入）
│   ├── _ypp_review_include.html   # 共享 YPP 核查逻辑 + 样式（166 行，三页 YPP 页共用）★
│   ├── 三大订单: shipping-records.html（2,252）/ inbound-records.html（2,193）/ loading-orders.html（1,683）
│   ├── 商品管理: product-units.html（221）/ product-categories.html（449）/ products.html（478）/ piece-conversions.html（125）
│   ├── 信息页: notice.html（428）/ notice-color.html（477）/ priceboard.html（523）/ workflow.html（71）/ warehouse.html（46）
│   ├── 操作要点: count-tips.html（741） / huandan-guide.html（642） / billing-tips.html（569）
│   ├── 基础记录: experience.html（48）/ errorlog.html（72）/ todolist.html（50）/ vehicle-maintenance.html（174）/ stockout.html（220）
│   ├── 任务流: tasks.html（220）/ tasks-new.html（346）/ task-detail.html（301）/ coding-pool.html（101）
│   ├── 管理: staff.html（309）/ vehicles.html（255）/ unified-search.html（235）/ audit-ocr-events.html（233）/ manage-category-prompts.html（817）
│   ├── 独立点数: point-count.html（157） / point-count-session.html（124）
│   ├── YPP 核查三页: shipping_ypp_review.html（17）/ inbound_ypp_review.html（16）/ loading_ypp_review.html（16）
│   │   └── 三页均只是壳，逻辑全部在 _ypp_review_include.html ★
│   └── mobile/             # 移动端页面（8 个文件，815 行）
│       ├── index.html（125）/ shipping-today.html（43）/ shipping-order.html（217）/ placement-count.html（88）
│       ├── inbound-today.html（43）/ inbound-order.html（129）
│       └── loading-today.html（43）/ loading-order.html（117）
├── static/                 # 静态资源（js 13 个 4,720 行；css 3 个 1,169 行）
│   ├── css/app.css（807） / css/mobile.css（259） / css/point-count.css（104）
│   ├── js/common.js（151，公共工具）
│   ├── js/product_row_utils.js（641，商品行工具：件数换算/校验/mismatch/行内编辑/汇总；**仅装柜页引入**，出货/入库内联复制）★
│   ├── js/placement_count.js（734，出货+入库共用摆放图计数）★
│   ├── js/loading_placement_count.js（753，装柜专用摆放图计数）★
│   ├── js/copy_paper.js（62，拷贝纸/日本纸标签图上传，不做 OCR；**仅出货页引入**）★
│   ├── js/voice_input.js（285）
│   ├── js/point-count.js（313） / point-count-list.js（79）
│   └── js/mobile_*.js（5 个）：mobile_blur（33）/ mobile_detail（416）/ mobile_inbound_detail（385）/ mobile_loading_detail（356）/ mobile_placement（513）
│   ├── mainflow.png / furongflow.png / warehouse.png
│   └── notice/             # 通知图片
├── upload/YYYY-MM/         # 用户上传图片，按月分组（7 个月份目录 / 8,631 个文件 / ~2.5 GB）
├── log/                     # 业务日志（log/YYYYMM/ 按天分文件，OCR/AI 落盘）
├── logging_setup.py        # 日志初始化（166 行）：trace_id ContextVar + 线程上下文传播 + 日志格式
├── tests/                  # 测试套件（110 个 .py，17,528 行）
├── tools/                  # 工具脚本（25 个 .py，含路径迁移/校验/OCR fixture/回归验证/CLAUDE.md 审计/红章白名单覆盖率审计）
├── docs/superpowers/       # 设计文档：specs/（20）+ plans/（19）+ e2e/ + regression/（截图）
├── sql/                    # SQL 脚本（2 个）
│   ├── new.sql             # 商品分类标准源（MySQL 语法）
│   └── new_sqlite.sql      # 商品分类导入脚本（SQLite 语法）
├── pyrightconfig.json      # Pyright 类型检查配置（仅覆盖 app.py + models/_db.py + models/__init__.py + models/_init.py）
├── _safe-snapshot/         # 高风险操作前的数据快照目录（当前存 8 份 product 重写 SQL 备份；**DB 快照统一走 D:\BAK\**）
├── start_server.bat        # Windows 启动脚本
├── setup_startup.ps1       # Windows 自启动 PowerShell 脚本
├── setup_firewall.bat      # 防火墙放行脚本（29 行）
├── README.md               # 面向人看的项目说明（607 行 / 36,641 B）
├── paddle_engine_dump.py   # PaddleOCR 引擎调试转储（518 行）
├── patch_lines.txt         # 临时补丁片段（5 行）
└── scratchpad.md           # 空占位文件
```

## 路由结构（246 个端点方法 / 244 条 rule，按 19 个 Blueprint 实例分布，2026-10-08 盘点）

> 246 = 审计脚本 `tools/_audit_claude_md.py` 统计的**端点方法数**（同一 rule 支持 GET/POST 两个方法时计 2）；244 = `create_app().url_map` 里的 **rule 数**（含 Flask 自动注册的 `/static/<path:filename>`）。早期版本的 243 是 `@bp.route(...)` 装饰器文本计数，口径不同，现统一以审计脚本为准。

所有端点已拆分到 `blueprints/` 目录下的 18 个蓝图文件（`point_count.py` 内含 `bp` + `bp_api` 两个 Blueprint → 共 **19 个 Blueprint 实例**）+ 8 个辅助模块 + 1 个包标记 = 27 个 .py，主 `app.py` 只保留工厂、上下文、缓存、登录闸门、蓝图注册。

> 复核命令：`python tools/_audit_claude_md.py`（打印全量 url_map 分组 + 各表行数，用于本文件对账）。
>
> ⚠️ 该脚本会 `create_app()`，**连带触发 `init_db()` 建表/迁移并向 `log/` 落盘**。只想读行数时改用只读连接：`sqlite3.connect("file:D:/worklog-app/worklog.db?mode=ro", uri=True)`。

移动端走 `/m/*` 前缀（**免登录**，见 `app.py._AUTH_PUBLIC_PREFIXES`），便于微信里点链接直接用；登录后路径仍是 `/shipping-records` 等带中文业务的页。

### `blueprints/auth.py` — 身份验证（3 个）

- `GET /login` 登录页（选身份，无密码）· `POST /login` 设 session · `GET|POST /logout` 清 session

### `blueprints/basic_records.py` — 基础记录（13 个）

- `/experience` 工作经验 · `/errorlog` 错误经验 · `/todolist` 待办 · `/vehicle-maintenance` 车辆维护

### `blueprints/info_pages.py` — 信息展示 + stockout（11 个）

- `/` 首页 · `/priceboard` 码数报价 · `/notice-color` 通知彩色版 · `/workflow` 业务流程 · `/warehouse` 仓库布局 · `/count-tips` 点数要点 · `/huandan-guide` 换单要点 · `/billing-tips` 开单要点 · `/stockout` 当前缺货

### `blueprints/notice.py` — 通知（15 个）

- `/notice` CRUD · `/api/v1/notices` REST API · `/api/v1/notice/<id>/images` 图片管理 · `/api/v1/notice/<id>/img-cols` 列数设置

### `blueprints/shipping.py` — 出货（41 个端点 = 2 页面 + 39 API，2026-10-08 盘点）

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

- `POST /api/v1/shipping-orders/ai-recognize` 整单图片 AI 识别（3 引擎可选）
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
- 判定函数 `_is_no_ai_match_item(item)`（2026-10-08 起为唯一谓词，原 `_is_copy_paper_item` 已并入），字段富化 `_enrich_copy_paper_for_item(item)` 原地写 `is_no_ai_match` / `label_images` / `has_label_image`
- 装柜页有同款上传端点 `POST /api/v1/loading-orders/records/<rid>/copy-paper-images`（复用 shipping 的 `COPY_PAPER_LABEL_SOURCE` / `_is_no_ai_match_item`）；**入库页暂无**

**自适应提示词**（共享端点，category_prompts 表）：

- `GET /api/v1/category-prompts` 列出提示词
- `POST /api/v1/category-prompts` 创建提示词
- `DELETE /api/v1/category-prompts/<id>` 软删提示词
- `POST /api/v1/shipping-orders/images/<id>/generate-prompt-suggestion` 基于图片 OCR 生成提示词建议

**加面图标**: 后端 `has_jia_mian` 计算 + 前端 SVG 渲染（"杂胶+加面"→ 重点列网状图标）

**OCR 糊图标记（2026-08-10 移动端）**：行级图 OCR 平均置信度 `avg_conf < 0.5` 时，由 `ocr_pipeline._append_blur_reason_if_low_conf()` 在 `image.reason` 末尾追加 `[图像可能模糊，建议重拍]`，前端 hover 提示用户重拍（幂等，marker 已存在不重复追加）。

**共享弹框/子页面（`templates/`）**：

- `_image_upload_modal.html`（430 行）—— 订单级 + record 级图片上传复用弹框（粘贴/选文件/旋转 90°，旋转后 Pillow 落盘再走 OCR）
- `_smart_add_modal.html`（1,588 行）—— 智能添加明细：📷 AI 图片识别（`#aiEngine` 四选项：Moonshot / **PaddleOCR+MiniMax（默认，推荐）** / PaddleOCR+DeepSeek / 纯 PaddleOCR）+ 📝 文本输入（DeepSeek 结构化）
- `_record_image_script.html`（1,185 行）—— 行级图片所有交互：匹配徽章列动态插入、异步 OCR+AI 比对、人工 ✓ 确认 / 👤 已确认、re-ocr/fuzzy/ai-judge/ocr-detail 按钮、自适应提示词弹框；出货页初始化会调用 `bindReOcrButtons()` 让「重 OCR」按钮在每次刷新后也能响应。内含 31 个具名 `function` + 5 个箭头函数变量 + 3 个 `window.*` 导出（`setImgCols` / `openImageModalForRecord` / `recordImageUploaded`）
- `_placement_count_modal.html`（102 行）—— 摆放图交互式清点弹框，三订单共用；内部同时引入 `placement_count.js` 与 `loading_placement_count.js` ★
- `_voice_input_modal.html`（129 行）—— 语音录入弹框（**仅出货页引入**，入口 `/api/v1/voice/recognize`/`/confirm`）

### `blueprints/inbound.py` — 入库（39 个端点，2026-10-08 盘点）

- `GET /inbound-records` 列表页 · `GET /m/inbound` 移动端当天列表 · `GET /m/inbound/order/<oid>` 移动端详情
- **REST API（`/api/v1/inbound-orders/*`）**: 订单 CRUD、明细 CRUD、move、图片 CRUD
- **行级图片**: 行级上传/合并查询 + manual-verify/fuzzy-match/ai-judge/ocr-detail/gen-prompt（同出货，**无 `re-ocr`**）
- **摆放图（点数）**: 11 个端点（含 **`/unload`**），与出货/装柜同构，模型 `InboundPlacementImage` + 表 `inbound_placement_marks`
- **交互式点数清点弹框已移植**：`inbound-records.html` 引入 `static/js/placement_count.js`（与出货共用同一文件），模板含 `#placementCountModal`
- **日本纸件数换算**: 按件数×每件张数+散装张数对比（支持 loose 与 * 形式）；placement 期望值走 `compute_placement_expected_zhi` 兜底
- **备注**: 2026-07-16 已清理 11 个 HTML form 死端点，统一为 REST API

### `blueprints/loading.py` — 装柜（36 个端点，2026-10-08 盘点）

- `GET /loading-orders` 列表页 · `GET /m/loading` 移动端当天列表 · `GET /m/loading/order/<oid>` 移动端详情
- **REST API（`/api/v1/loading-orders/*`）**: 订单 CRUD（含 `img_cols` 列数持久化）、明细 CRUD、move、图片 CRUD
- **行级图片**: 行级上传/合并查询 + manual-verify/fuzzy-match/ai-judge/ocr-detail/gen-prompt（同出货，**无 `re-ocr`**）
- **摆放图（点数）**: 11 个端点（含 **`/unload`**），模型 `LoadingPlacementImage` + 表 `loading_placement_marks`
- **拷贝纸 / 日本纸**: `POST /api/v1/loading-orders/records/<rid>/copy-paper-images` 上传标签照（复用 shipping 的 `COPY_PAPER_LABEL_SOURCE` / `_is_no_ai_match_item`）—— ⚠️ **后端端点已就绪，但前端无入口**：`loading-orders.html` 对 `copy_paper` 全库 0 命中，`copy_paper.js` 只被 `shipping-records.html` 引入，`loading_order_images` 里 `source='copy_paper_label'` 为 0 行。属于「后端做完了但 UI 从未接」的功能缺口（见「待办 / 待清理」）
- **共享模板**: `_record_image_script.html`（与出货/入库共用）+ `static/js/loading_placement_count.js`（装柜专用点数 JS）

### `blueprints/products.py` — 商品管理（18 条 rule）

- `/product-units` 商品单位（81 条数据）
- `/product-categories` 商品类型（`product_categories` 表，205 条 = 1 根 + 9 大类 + 分类树，以 `sql/new.sql` 为最终标准源）
- `/products` 产品管理（`product` 表，1,287 条）
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

### `blueprints/upload.py` — 静态文件

- `/upload/<path>` 访问 upload 目录下的文件

### 主 `app.py` — 应用入口（204 行，2026-10-08 盘点）

- `create_app()` 工厂函数，**注册 19 个 Blueprint 实例**（18 个蓝图文件；`point_count.py` 一个文件里 `bp` + `bp_api` 两个，含 audit / voice / category_prompts_manage / mobile_shipping）
- `inject_notices` 全局上下文（注入 all_notices 到所有模板）
- `inject_current_operator` 全局上下文（注入当前操作员 Staff dict 到模板；session 失效时主动 `session.pop('operator_id', None)` 避免"什么都没了"假象）
- `_require_login` before_request 闸门（白名单 `_AUTH_PUBLIC_PREFIXES = ("/static", "/api/", `/m/`, "/upload/")` + `_AUTH_PUBLIC_PATHS = ("/login", "/logout", "/favicon.ico")`）
- `add_cache_control_headers` 禁用 HTML 缓存
- `_reset_log_context` after_request 清业务上下文 + `_TRACE_ID`，避免请求间串味
- `init_logging(app)` 在 `init_db()` 之前（建表失败也能落盘到 `log/`）
- `init_db()` 数据库初始化（36 张业务表 DDL + 46 条 try/except ALTER 增量迁移）
- 上传超限 413 友好返回（API 走 JSON，页面走纯文本）

## 数据库（36 张业务表，2026-10-08 实测）

> 库内共 **38 张表** = 36 张业务表 + `sqlite_sequence`（系统自增序列）+ `_cleanup_safety_2026_07_30`（2026-07-30 数据清洗临时表，非业务表，可清理）。
>
> 早期存在的 `product_units_new`（迁移残留）与 `copy_paper_images`（2026-09-09 废弃）**均已删除**。
>
> 另有 43 个索引（34 个手写 + 9 个 `sqlite_autoindex` 隐式）、0 个视图。

### 核心业务表

| 表                           | 用途          | 行数     |
| --------------------------- | ----------- | ------ |
| `work_logs`                 | 工作经验        | 25     |
| `error_logs`                | 错误经验        | 9      |
| `todo_items`                | 待办事项        | 3      |
| `notices` + `notice_images` | 公司通知        | 26 + 5 |
| `vehicle_maintenance`       | 车辆维护        | 2      |
| `stock_out_items`           | 缺货登记        | 1      |
| `voice_phrase_mapping`      | 语音短语映射（自学习） | 0      |

### 订单三表结构

三种订单都遵循 **订单 → 记录/明细 → 图片** 三表模式：

**出货** (`shipping_orders` / `shipping_records` / `shipping_images`)

- 1,022 个订单 / 2,709 条记录 / 5,604 张图片

**入库** (`inbound_orders` / `inbound_records` / `inbound_images`)

- 290 个订单 / 945 条记录 / 841 张图片

**装柜** (`loading_orders` / `loading_order_records` / `loading_order_images`)

- 24 个订单 / 127 条记录 / 270 张图片

通用字段：

- `is_locked` (0/1) — 锁定订单防止修改
- 订单级字段：`date` / `customer` / `order_num`（出货和装柜）
- 入库订单只有 `date`、`supplier` 和 `is_locked`
- 明细字段：`product_name` / `specification` / `quantity` / `unit` / `remark`
- 图片存储在 `upload/` 文件夹，通过相对路径引用
- **图片表统一带 `source` 字段**（三页口径一致）：`upload`（手动）/ `ai`（AI 识别）/ `placement`（摆放图）/ `copy_paper_label`（拷贝纸标签，**仅出货**，不做 OCR）
  - 出货实测分布：upload 3,691 / placement 1,354 / ai 532 / copy_paper_label 28
  - 入库实测分布：upload 656 / ai 174 / placement 11（**placement 已投入使用**）
  - 装柜实测分布：upload 235 / ai 9 / placement 26（**无 copy_paper_label**：后端端点在，前端从未接 UI）
- **摆放图相关列直接挂在图片表上**（不是 marks 表）：`circles`(JSON) / `mark_scale` / `loose_count` / `manual_count` / `is_unload` / `source_tag`

### 摆放图标记表（2026-09 三页齐备）

| 表                         | 归属        | 行数               | 同库摆放图总数                                         |
| ------------------------- | --------- | ---------------- | ----------------------------------------------- |
| `placement_marks`         | 出货摆放图点击标记 | 5,440（覆盖 462 张图） | `shipping_images(source='placement')` 1,354 张   |
| `inbound_placement_marks` | 入库摆放图点击标记 | 15（覆盖 2 张图）      | `inbound_images(source='placement')` 11 张       |
| `loading_placement_marks` | 装柜摆放图点击标记 | 0                | `loading_order_images(source='placement')` 26 张 |

> 装柜 0 行标记**不是 bug**：26 张摆放图全部走 `manual_count` 直接录支数（1-80 支不等，其中 2 张 `is_unload=1`），没有一条点击标记。

三表结构一致：`id / image_id / seq / x_ratio / y_ratio / mark_r / created_at`（坐标为 0-1 比例值）。

### 商品资料

| 表                    | 用途                 | 行数    | 模型类               |
| -------------------- | ------------------ | ----- | ----------------- |
| `product_units`      | 商品单位/规格（YPP 支码换算）  | 81    | `ProductUnit`     |
| `product_categories` | 商品分类（3 级树，带编码）     | 205   | `ProductCategory` |
| `product`            | 产品（品名/规格/条码/价格/库存） | 1,287 | `Product`         |
| `piece_conversions`  | 件数换算规则（件→张/只/令）    | 16    | `PieceConversion` |

### 任务流表

| 表             | 用途                                     | 行数                                                                                     |
| ------------- | -------------------------------------- | -------------------------------------------------------------------------------------- |
| `staff`       | 人员档案（姓名/角色/电话/绑定车辆）                    | 49（**含大量测试残留**：test/verify/smoke-tester/portal-tester/veh-tester，id 6220+；真实员工为低 id 段） |
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

**`ocr_match_event` 关键字段**：`event_type`（record_ocr / ai_match / human_verify）、`ocr_text`（PaddleOCR 原文）、`prompt_payload`（完整比对 prompt）、`ai_match_status/score/reason`、`ai_raw_response`（DeepSeek 原始返回）、`ai_engine`、`prompt_version`（按提示词版本聚合用）、`human_status/reason/verified_by`（人工裁决）。

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

- 常量（`blueprints/shipping.py:56`）：`NO_AI_MATCH_CATEGORY_CODES = ('0105','0106','0107','0108')` + `NO_AI_MATCH_KEYWORDS = ('拷贝','日本纸','快巴','腊光','蜡光')`
  > ⚠️ **真实数据用的是「蜡光纸」**（`shipping_records` 命中 4 行，全部是「蜡光纸」；「腊光」0 行）。`蜡光` 同义收录不是防御性冗余，是当前唯一生效的那个字，**别当成错别字容错删掉**。
- 判定函数 `_is_no_ai_match_item(item)`：关键词先短路，未命中再走 `classify_record()` 拿 `category_code`
- **2026-10-08 合并**：此前还有一个 `_is_copy_paper_item`（只覆盖 0105/0106/0107）。两者关键词集/品类码集互相包含，对同一 `(name, spec)` 会跑两次 `classify_record` 查询；腊光纸纳入后完全等价，已合并为单一谓词，`is_copy_paper` 字段一并删除
- 五层消费：
  1. 后端硬拦截 `POST /api/v1/{shipping,loading}-orders/records/<id>/images` → 400（防 DevTools / 粘贴 / 拖拽绕过 UI）
  2. 整单 `ai-match` 在 OCR 前过滤 `records`（覆盖 OCR 循环 / rows / own_record_ids / verdicts 回写四路径）
  3. PC `shipping-records.html`：**标签 🖼️ 按钮**守卫 `if item.is_no_ai_match`（渲染）+ **普通 🖼️ 按钮**守卫 `if not ... is_no_ai_match`（隐藏）→ 一行恰好一个 🖼️
  4. 移动端 `mobile/shipping-order.html`：隐藏「📷 拍照识别 / 相册」（否则点进去吃 400），改渲染「📷 标签」；徽章从永远满足不了的「待拍」改为「标签图」；卡片打 `data-role="label-only"` 结构标记
  5. `_enrich_copy_paper_for_item` 写入 `item['is_no_ai_match']` + `label_images` / `has_label_image`（供 3/4 消费）

> **标签图上传端点本身没有 `is_copy_paper` 守卫**（`shipping.py` / `loading.py` 的 `/records/<rid>/copy-paper-images` 只校验锁单 + 图片有效性），所以腊光纸能直接复用，无需改后端。
>
> 装柜页（`loading-orders.html`）**至今没有任何标签图按钮** —— 后端 0108 硬拦截 + 后端标签图端点都就绪，UI 从未接（见「待办」）。装柜的腊光纸行因此仍无图片入口。

### 5. 加面图标（商品明细重点列）

`has_jia_mian` 后端计算：`product_name` 含「杂胶」且 `specification` 含「加面」→ True。

前端渲染：重点列显示 SVG 网状图标 + CSS class `.jia-mian-icon`。

原「环保」列已改名为「重点」列。

### 6. 图片上传流程（含安全校验）

**上传校验**（`blueprints/_helpers.py`，2026-07-15 加固）：

- **文件大小**：单张最大 10 MB（`MAX_IMAGE_SIZE`）
- **扩展名白名单**：`.png`, `.jpg`, `.jpeg`, `.gif`, `.webp`
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

| 字段                | 值                          | 说明                                                        |
| ----------------- | -------------------------- | --------------------------------------------------------- |
| `mismatch`        | `''` / `'info'` / `'warn'` | YPP 轨道                                                    |
| `piece_mismatch`  | `''` / `'info'` / `'warn'` | 件数换算轨道                                                    |
| `qty_invalid`     | bool                       | 数量本身非法                                                    |
| `unit_hint`       | str                        | 辅助单位提示（如 `3支+4.5码`）                                       |
| `piece_hint`      | str                        | 件数换算提示（如 `1500.0张/件`）                                     |
| `mismatch_detail` | dict | None                | `{rule,label,expected,actual,diff,severity}`，供 hover 显示差额 |

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

> ⚠️ **代码标识符是历史遗留，别拿它反推中文叫法**：`copy_paper_label` / `copy-paper-images` / `.copy-paper-label-btn` / `copy_paper.js` 这些名字沿用了最早的单一品类，但实际覆盖 4 个品类（含蜡光纸），拍的是实物照而非标签特写。中文一律以本节术语为准。

#### 实现现状

`copy_paper_images` 专表与 `CopyPaperImage` 模型**已废弃删除**（`models/_init.py:976` 有留档注释）。改为复用订单图片表：

- 存储：`shipping_images(source='copy_paper_label', record_pk=<明细行 id>)`，与订单图同表、**不做 OCR / AI 比对**（`shipping.py` 的 `record_imgs` 过滤显式排除 `placement` 与 `copy_paper_label`）
- 上传：`POST /api/v1/shipping-orders/records/<rid>/copy-paper-images`（装柜页有同款，**但装柜 UI 未接入口**，见「待办」）。该端点**没有品类守卫**，只校验锁单 + 图片有效性，任何明细都能传
- 删除：走通用 `DELETE /api/v1/shipping-orders/images/<id>`（自带锁单防御 + 审计）
- 张数：**不在这类图上录入**，统一走 placement 体系（`source='placement'` + `compute_copy_paper_expected_quantity` 算期望张数）
- 常量与判定：`blueprints/shipping.py` 的 `COPY_PAPER_LABEL_SOURCE`、`NO_AI_MATCH_CATEGORY_CODES`、`_is_no_ai_match_item()`、`_enrich_copy_paper_for_item()`（原地写 `is_no_ai_match` / `label_images` / `has_label_image`）；装柜 `loading.py` 直接 import 复用
- 前端：`static/js/copy_paper.js`（62 行，只保留上传/删除，张数相关 DOM 操作已随重构删除）+ 移动端 `mobile_shipping.py` 同步富化

### 23. 独立点数工具（`point_count`，2026-08-17）

`blueprints/point_count.py` 内两个 Blueprint：`bp`（页面，前缀 `/tools/point-count`，3 个端点）+ `bp_api`（REST，前缀 `/api/v1/point-count`，16 个端点）。

用途：**临时开会话 → 连续拍照 → 标记支/散码 → 导出 CSV**，不挂订单/明细，纯独立工具。模型在 `models/point_count.py`（415 行，含独立文件白名单删除 `_safe_remove_file`）。

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

### JS 模块（`static/js/` 全部 13 个文件 / 4,720 行）

- `static/js/common.js`（151 行）—— 公共工具（`escHtml` / `confirmDialog` / `showBtnLoading` 等）
- `static/js/product_row_utils.js`（640 行）—— 商品行工具：件数换算、单位/数量校验、辅助单位提示、备注→数量自动填充、mismatch icon、行内编辑、上移/下移、汇总行（支持 `row-after-table` 与 `tbody-tr` 两种布局）。⚠️ **实测只有 `loading-orders.html` 引入**；出货/入库页各自内联复制了 5-8 个同名函数，改这个文件不会同步到那两页
- `static/js/placement_count.js`（734 行）—— 出货 + 入库共用摆放图：上传/展示/点击计数/撤销/卸货/手动支数/散码
- `static/js/loading_placement_count.js`（753 行）—— 装柜专用摆放图。⚠️ 早期文档称「与上者约 90% 重复、仅 API 前缀不同」，**现已反超** `placement_count.js`（753 > 734），两边各自演进出独立功能，不再是简单前缀差异
- `static/js/copy_paper.js`（62 行）—— 拷贝纸/日本纸标签图上传（**不做 OCR/AI**）
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
- **拷贝纸/日本纸双按钮**（`static/js/copy_paper.js`）：仅出货页有，**装柜后端已就绪但前端未接**

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
- 导航分组（8 个下拉 + 4 个单链 + 1 个拍照）：
  - **运营信息** — 公司通知、通知彩色版、业务流程、仓库布局、**OCR 事件审计**（📊）
  - **当前缺货** — 单页
  - **商品管理** — 商品单位、商品类型、产品管理、件数换算、**分类提示词**（📚）
  - **码数报价** — 单页
  - **操作要点** — 开单要点、换单要点、点数要点、**独立点数**（📷 工具型会话）
  - **快速录入** — 工作经验、错误经验、待办事项、车辆维护记录
  - **任务流** — 任务列表、新建任务、打码抢单池、人员档案、车辆档案
  - **订单操作** — 装柜订单、出货记录（含 ⚖️ YPP 核查）、入库记录、综合查找
- 样式以 Tailwind CSS 为主，共享自定义 CSS 在 `static/css/app.css`（807 行）
- 移动端样式独立在 `static/css/mobile.css`（259 行）
- 共享 JS 工具函数在 `static/js/common.js`
- 每个模板底部用 `<script>` 内联实现交互 + `<script src="...">` 引入专项 JS
- 共享 include 关系（2026-10-08 实测 grep）：
  | 组件                                                                          | 出货 | 入库 | 装柜       |
  | --------------------------------------------------------------------------- | -- | -- | -------- |
  | `_record_image_script.html`                                                 | ✅  | ✅  | ✅        |
  | `_placement_count_modal.html`                                               | ✅  | ✅  | ✅        |
  | `_voice_input_modal.html`                                                   | ✅  | —  | —        |
  | `static/js/placement_count.js`                                              | ✅  | ✅  | ✅（经清点弹框） |
  | `static/js/loading_placement_count.js`                                      | —  | —  | ✅（经清点弹框） |
  | `static/js/copy_paper.js`                                                   | ✅  | —  | —        |
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


### 蓝图（18 个蓝图文件 → 19 个 Blueprint 实例 + 8 个辅助模块 + 1 包标记 = 27 个 .py，共 14,650 行，按行数排序，2026-10-08 实测）

| 文件                                      | 行数    | 关键内容                                                                                                                                                                     |
| --------------------------------------- | ----- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `blueprints/ocr_engine.py`              | 3,991 | 五引擎（Moonshot/PaddleOCR/DeepSeek/MiniMax/PaddleOCR+MiniMax）+ 后处理安全网 + COMPARE_PROMPT + \_wrinkle_ocr + KIND_FORM_NOLINES/KIND_GLARE/KIND_REDSTAMP 三档预处理 + `_to_grayscale` |
| `blueprints/shipping.py`                | 2,210 | 出货 REST + AI 识别 + OCR 匹配 + 行级图片 + 加面 + 自适应提示词 + placement + copy-paper + `_is_no_ai_match_item` + `/re-ocr` + `/shipping-ypp-review` + YPP scan                          |
| `blueprints/inbound.py`                 | 1,772 | 入库 REST + 行级图片 + placement（12 端点）+ 移动端 /m/* + 日本纸件数换算 + align 复制至出货                                                                                                      |
| `blueprints/loading.py`                 | 1,614 | 装柜 REST + 行级图片 + placement + copy-paper（复用 shipping `_is_no_ai_match_item` 硬拦截）+ img_cols + 移动端 /m/*                                                                     |
| `blueprints/_helpers.py`                | 898   | 共享：图片上传校验、YPP 匹配、支数换算、备注校验、compute_placement_expected_zhi、compute_copy_paper_expected_quantity、apply_user_rotation                                                       |
| `blueprints/task_flow.py`               | 617   | 任务流 REST + 状态机 + 证据闸门 + 建单/推进/退单/退货                                                                                                                                      |
| `blueprints/ocr_pipeline.py`            | 437   | 行级图 OCR pipeline 共享层：RecordImageProcessor（三套订单共用）+ 糊图标记 + 模块级 \_OCR_LOCK/\_ASYNC_JOBS ★                                                                                  |
| `blueprints/point_count.py`             | 408   | 独立点数（bp + bp_api 两个 Blueprint）★                                                                                                                                          |
| `blueprints/products.py`                | 302   | 商品管理                                                                                                                                                                     |
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
| `blueprints/basic_records.py`           | 126   | 基础记录                                                                                                                                                                     |
| `blueprints/category_prompts_manage.py` | 117   | 分类提示词管理页 + REST ★                                                                                                                                                        |
| `blueprints/voice_baidu.py`             | 90    | 语音辅助：百度语音 API ★                                                                                                                                                          |
| `blueprints/info_pages.py`              | 87    | 信息展示页                                                                                                                                                                    |
| `blueprints/search.py`                  | 80    | 综合查找                                                                                                                                                                     |
| `blueprints/auth.py`                    | 67    | 登录/登出                                                                                                                                                                    |
| `blueprints/upload.py`                  | 17    | 静态文件服务                                                                                                                                                                   |
| `blueprints/__init__.py`                | 1     | 包标记                                                                                                                                                                      |

### 数据库层（16 个模块，2026-10-08 实测）

| 文件                           | 行数    | 模型数 | 关键类                                                                                                                                                                                                                                                   |
| ---------------------------- | ----- | --- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `models/orders.py`           | 2,967 | 14  | ShippingOrder / ShippingRecord / InboundOrder / InboundRecord / InboundImage / ShippingImage / PlacementImage / LoadingPlacementImage / InboundPlacementImage / OcrMatchEvent / LoadingOrder / LoadingOrderRecord / LoadingOrderImage / UnifiedSearch |
| `models/_init.py`            | 999   | —   | 36 张业务表 DDL（39 段 `CREATE TABLE` 含已废弃留档）+ 46 段 try/except ALTER 增量迁移                                                                                                                                                                                   |
| `models/category_prompt.py`  | 647   | 1   | CategoryPrompt + compose_for_record + classify_record：自适应提示词★                                                                                                                                                                                         |
| `models/tasks_flow.py`       | 491   | 11  | Staff/StaffDB/Task/TaskItem/TaskImage/TaskEvent 等                                                                                                                                                                                                     |
| `models/point_count.py`      | 415   | 2   | PointCountSession / PointCountImage / PointCountMark + `_safe_remove_file` 白名单删除★                                                                                                                                                                     |
| `models/products.py`         | 302   | 3   | ProductUnit / ProductCategory / Product                                                                                                                                                                                                               |
| `models/audit_query.py`      | 273   | 1   | OcrEventAudit：OCR 事件只读查询（prompt_stats/record_pairs/export_rows）★                                                                                                                                                                                      |
| `models/notice.py`           | 144   | 2   | Notice / NoticeImage                                                                                                                                                                                                                                  |
| `models/voice_mapping.py`    | 139   | 1   | VoicePhraseMapping 语音短语自学习★                                                                                                                                                                                                                           |
| `models/basic.py`            | 134   | 4   | WorkLog / ErrorLog / TodoItem / VehicleMaintenance                                                                                                                                                                                                    |
| `models/piece_conversion.py` | 101   | 1   | PieceConversion                                                                                                                                                                                                                                       |
| `models/_permissions.py`     | 100   | —   | Action 枚举 + can() 函数                                                                                                                                                                                                                                  |
| `models/audit.py`            | 58    | 1   | AuditLog                                                                                                                                                                                                                                              |
| `models/__init__.py`         | 42    | —   | re-export                                                                                                                                                                                                                                             |
| `models/stock.py`            | 40    | 1   | StockOutItem                                                                                                                                                                                                                                          |
| `models/_db.py`              | 22    | —   | get_db() + DB_PATH                                                                                                                                                                                                                                    |

### 模板（51 个文件 = 43 主目录 + 8 mobile/，共 19,639 行，按行数排序，2026-10-08 实测）

| 文件                             | 行数    | 说明                                                                         |
| ------------------------------ | ----- | -------------------------------------------------------------------------- |
| `shipping-records.html`        | 2,257 | 出货记录（行级图片 + 摆放图清点弹框 + YPP focus 跳转 + copy-paper 双按钮 + `is_no_ai_match` 守卫） |
| `inbound-records.html`         | 2,185 | 入库记录（行级图片 + **摆放图清点弹框** + 移动端入口）                                           |
| `loading-orders.html`          | 1,685 | 装柜订单（行级图片 + 摆放图清点弹框 + 移动端入口；**后端有 copy-paper 端点但前端未接 UI**）                 |
| `_smart_add_modal.html`        | 1,588 | 共享智能添加弹框（AI 图片识别，默认引擎 `paddleocr_minimax` + 文本批量）                          |
| `_record_image_script.html`    | 1,185 | 共享行级图片 JS（三订单 include 复用，31 具名 function + 5 箭头函数）★                         |
| `manage-category-prompts.html` | 817   | 分类提示词管理页                                                                   |
| `count-tips.html`              | 741   | 点数要点                                                                       |
| `huandan-guide.html`           | 642   | 换单要点                                                                       |
| `billing-tips.html`            | 569   | 开单要点                                                                       |
| `priceboard.html`              | 523   | 码数报价                                                                       |
| `products.html`                | 478   | 产品管理                                                                       |
| `notice-color.html`            | 477   | 通知彩色版                                                                      |
| `product-categories.html`      | 449   | 商品类型                                                                       |
| `base.html`                    | 444   | 公共布局 + 导航（8 分组 + 4 单链 + 顶栏头像）                                              |
| `_image_upload_modal.html`     | 430   | 共享图片上传弹框（旋转 90° 落盘）                                                        |
| `notice.html`                  | 428   | 通知管理                                                                       |
| `tasks-new.html`               | 346   | 新建任务（OCR）                                                                  |
| `staff.html`                   | 309   | 人员档案                                                                       |
| `task-detail.html`             | 301   | 任务详情                                                                       |
| `vehicles.html`                | 255   | 车辆档案                                                                       |
| `unified-search.html`          | 235   | 综合查找                                                                       |
| `audit-ocr-events.html`        | 233   | OCR 事件审计页                                                                  |
| `product-units.html`           | 221   | 商品单位                                                                       |
| `stockout.html`                | 220   | 当前缺货                                                                       |
| `tasks.html`                   | 220   | 任务列表                                                                       |
| `vehicle-maintenance.html`     | 174   | 车辆维护                                                                       |
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

- `templates/mobile/`（8 个文件，815 行）：`shipping-order.html`(228) / `inbound-order.html`(129) / `index.html`(125) / `loading-order.html`(117) / `placement-count.html`(88) / `shipping-today.html`(43) / `inbound-today.html`(43) / `loading-today.html`(43)

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
- [x] ~~**腊光纸（0108）行没有行级图片入口**~~ — 已完成（2026-10-08）：`_is_copy_paper_item` 并入 `_is_no_ai_match_item`（0105-0108 单一谓词），PC 标签 🖼️ 按钮按 `is_no_ai_match` 渲染、移动端隐藏拍照识别改出「📷 标签」+ 徽章「标签图」
- [x] ~~**`_is_no_ai_match_item` 与 `_is_copy_paper_item` 重复查库**~~ — 已完成（2026-10-08）：两个谓词合并为 `_is_no_ai_match_item` 单一入口（0105-0108），`_is_copy_paper_item` 已彻底移除（代码/文档 0 引用），`classify_record` 调用次数从每行 2 次降到 1 次
- [x] ~~**明细导入接口不返回 mismatch 标志（概述 §f 的"导入即校验"闭环）**~~ — 已完成（2026-10-09）：`_helpers.annotate_import_validation()` + `summarize_import_validation()` 新增，三页 `records/batch` 端点在插入后调用并回传 `mismatch`/`piece_mismatch`/`qty_invalid`/`unit_hint`/`piece_hint`/`mismatch_detail` + 顶层 `validation`；前端三页 build 函数改走 `readImportFlags()`（**服务端优先**）+ `notifyImportValidation()` 弹汇总。**拍板不落库**（mismatch 由备注+数量完全决定，刷新现算）。验证：`tools/verify_import_validation.py`（14 用例 + 零漂移）+ `_http.py`（三页端到端全绿，临时数据零残留）+ `_js.js`（前端 29/29）
- [ ] **引擎白名单 4 处重复**：`ocr_engine._VALID_ENGINES` 已是唯一真源，但 shipping / inbound / loading 三个 `ai-recognize` 端点各自硬编码了一份 5 元组 → 下次加引擎要改 4 处。建议蓝图改 `from blueprints.ocr_engine import _VALID_ENGINES`（或把 `_` 前缀去掉暴露成 `VALID_ENGINES`）
- [ ] **`PaddleOCRMiniMaxEngine` 靠私有方法 + 第二份 PaddleOCR 模型**：`DeepSeekEngine.__init__` 里 `self._ocr_engine = PaddleOCREngine()` 是新建实例而非 `get_ocr_engine('paddleocr')` 单例，复合引擎复用它的 `_ocr_image` 后，`paddleocr` + `paddleocr_minimax` 同进程会加载两份 PaddleOCR 模型。建议把 `DeepSeekEngine.__init__` 改成取单例
- [ ] **整单 `ai-match` 全单被过滤时返回误导性 400**：`records` 过滤后为空 → `ocr_text` 空 → 返回「没有可识别的行级图片」，实际语义是「本单全部明细都跳过 AI 比对」。建议在过滤后加早退分支给明确文案
- [ ] **`staff` 表 49 行里绝大部分是测试残留**（`test` / `verify` / `verify2` / `smoke-tester` ×5 / `portal-tester` ×2 / `veh-tester` / `portal-*`，id 段 6220+，全部 `is_active=1`）→ 建议精确 id 删除，**先备份 `D:\BAK\`**
- [ ] **入库 placement 已投入使用但覆盖面很小**（`inbound_images(source='placement')` 11 张图 / `inbound_placement_marks` 仅 15 行、覆盖 2 张图）→ 建议真机再走一遍上传+点数验收
- [ ] **装柜拷贝纸/腊光纸前端无入口**：`loading.py` 的 `POST /api/v1/loading-orders/records/<rid>/copy-paper-images` 后端已就绪（且**无** `is_copy_paper` 守卫，任何明细都能传），但 `loading-orders.html` 对 `copy_paper` 0 命中、`copy_paper.js` 只被出货页引入、装柜侧 `copy_paper_label` 0 行 → 装柜的拷贝纸/日本纸/快巴纸/**腊光纸**行至今没有任何行级图片入口（0108 后端又硬拦截 `/images`，等于零入口）。要么补 UI（照出货页 `copy_paper.js` 挂上去 + 标签 🖼️ 按钮），要么把端点标注为预留。**改动前先 `git log -p -- templates/loading-orders.html` 确认是"漏做"还是"曾做过又删"**
- [ ] **`product_row_utils.js` 只被装柜页引用**（出货/入库内联复制了同名函数）→ 要么给三页都挂上，要么明确放弃共享
- [ ] 推送本地 commits 到 GitHub（需先开梯子，见 [[GitHub 需要梯子]]）
- [x] ~~D 盘数据库备份~~ — 常态执行，脚本 `D:\BAK\_backup_now.py` + `D:\BAK\_verify_backup.py`（最近一份 `worklog_20260927_1927.db`，校验 MATCH）
- [ ] task_flow.py 中 `coding-claim`/`coding-done`/`coding-release` 三个端点 501 占位 → M2 实现
- [ ] OCR Label Profile Registry（`config/ocr_profiles.json`，4 个 profile）—— 当前在 feature 分支，待合并
- [ ] 入库 align 复制至出货：核对入库对齐逻辑能否让出货也用上（2026-08-13 在 inbound.py 加，未在 shipping.py 同步）
- [ ] 入库/装柜**缺 `/re-ocr` 端点**（出货页独占）→ 若需三页一致可补
- [ ] 入库页**缺 copy-paper 上传端点**（出货/装柜已有）
- [ ] 重复代码：`placement_count.js`(734) 与 `loading_placement_count.js`(753) 早期称约 90% 重复，但后者已反超 → 需重新评估哪些差异是有意为之，再决定是否参数化成单文件
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

**解决**：把 `import openai` 提到模块顶层（`ocr_engine.py` 第 15 行），跟其他顶层 import 一起。

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

**教训**：废弃一张表时，`models/_init.py` 要留注释说明「已废弃 + 迁往何处」（本项目 `_init.py:976` 就是这么做的），否则下一个读代码的人会重新加回来。历史上 `product_units_new` 也是同类残留（现已清理）。

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
> 备份脚本已就位：`D:\BAK\_backup_now.py`（一键备份，固定 `src=D:\worklog-app\worklog.db`）+ `D:\BAK\_verify_backup.py`（一致性校验）+ `D:\BAK\_list_tables.py`（表清单）。

**触发时机**：

- 补录完一批订单/通知/分类后（用户明确要求时）
- 任何 db 破坏性操作前（见"踩坑点 6"）
- 每次大改 schema / 跑迁移脚本前

**保留策略**：D 盘容量充足，**不主动清理**——保留历史快照作为时间机器；用户主动说"清理旧的"才动手（必须用 `mavis-trash`，不许 `Remove-Item -Force`）。项目目录下的临时备份保留至少 1 份作近期参考即可，不需要全量同步到 D 盘。
