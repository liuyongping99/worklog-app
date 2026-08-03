# 丰源工作台项目指南

此文件为 Claude Code (claude.ai/code) 在本仓库中工作时提供指导。

## 项目概述

丰源工作台是一个基于 Flask 的工作日志与订单管理系统，已从「事后记账」演进为「任务流实时管控 + 证据链」系统。核心能力：

- **工作日志**：工作经验、错误经验、待办事项、公司通知、车辆维护
- **订单管理**：出货/入库/装柜三套订单（订单 + 明细 + 图片 + AI 识别 + OCR 匹配）
- **商品资料**：商品单位、商品分类（3 级树）、产品库（1000+ SKU）、件数换算规则
- **任务流**：状态机驱动的送货任务管理（准备中 → 已装货 → 已点数 → 已到达 → 已卸货 → 已完成）+ 退单/退货/作废
- **人员/车辆**：人员档案（6 种角色）、车辆档案、身份选择登录
- **AI/OCR**：三引擎（Moonshot 云端视觉 / PaddleOCR 本地 / DeepSeek 结构化）+ 明细行标签 OCR 匹配校验
- **综合查找**：跨出货/入库/装柜的模糊搜索

## 技术栈

- **后端**: Python 3.12 + Flask 3.1.3
- **数据库**: SQLite 3.45.3 (文件: `worklog.db`, WAL 模式)
- **前端**: 原生 JavaScript + Tailwind CSS（内联在模板 + `static/css/app.css`）
- **JS 模块**: 仅 `static/js/common.js`（149 行）提供公共工具；各模板末尾用 `<script>` 内联实现交互
- **AI 集成**:
  - Moonshot Kimi k2.6 Vision API — 云端图片直接识别
  - PaddleOCR 3.x — 本地 CPU OCR 文字提取
  - DeepSeek v4 Flash API — OCR 文字结构化 + 明细行标签比对
- **OCR 架构**: ABC 基类 + 工厂模式（`blueprints/ocr_engine.py`），三引擎可切换
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

## 目录结构

```
worklog-app/
├── app.py                  # 应用入口（170 行，工厂模式 + 登录闸门 + 15 蓝图注册）
├── models/                 # 数据模型包（14 个文件，29 张业务表）
│   ├── __init__.py         # 统一 re-export（38 行）
│   ├── _db.py              # get_db() + DB_PATH（22 行）
│   ├── _init.py            # init_db() 建表 + 迁移（756 行）
│   ├── _permissions.py     # 权限系统：Action 枚举 + can() 集中校验（99 行）
│   ├── basic.py            # WorkLog / ErrorLog / TodoItem / VehicleMaintenance（134 行）
│   ├── notice.py           # Notice / NoticeImage（144 行）
│   ├── orders.py           # 三套订单 9 模型 + UnifiedSearch + OcrMatchEvent（2,068 行，最大文件）
│   ├── stock.py            # StockOutItem（40 行）
│   ├── products.py         # ProductUnit / ProductCategory / Product（302 行）
│   ├── piece_conversion.py # PieceConversion 件数换算规则（101 行）
│   ├── tasks_flow.py       # Staff/Task/TaskItem/TaskImage/TaskEvent（491 行）
│   ├── audit.py            # AuditLog（58 行）
│   ├── audit_query.py      # OcrEventAudit：OCR 事件审计只读查询（273 行）★
│   └── category_prompt.py  # CategoryPrompt + classify_record：自适应提示词（238 行，新增）★
├── blueprints/             # 17 个文件（14 个蓝图 + 2 个辅助模块 + 包标记）
│   ├── __init__.py         # 包标记（1 行）
│   ├── _helpers.py         # 辅助：图片上传校验、YPP 匹配、支数换算、备注校验、汇总（552 行）
│   ├── ocr_engine.py       # 辅助：OCR 引擎抽象层三引擎（1,064 行）
│   ├── auth.py             # 蓝图：登录/登出（67 行）
│   ├── upload.py           # 蓝图：/upload/<path> 静态文件（17 行）
│   ├── basic_records.py    # 蓝图：经验/错误/待办/车辆维护（126 行）
│   ├── info_pages.py       # 蓝图：首页/价格板/通知/流程/仓库/要点/缺货（87 行）
│   ├── notice.py           # 蓝图：/notice + REST API（220 行）
│   ├── products.py         # 蓝图：商品管理（302 行）
│   ├── shipping.py         # 蓝图：/shipping-records + REST + AI/OCR + 自适应提示词（1,577 行）
│   ├── inbound.py          # 蓝图：/inbound-records + 行级图片（1,159 行）
│   ├── loading.py          # 蓝图：/loading-orders + 行级图片（1,049 行）
│   ├── search.py           # 蓝图：/unified-search 综合查找（80 行）
│   ├── staff.py            # 蓝图：/staff 人员档案（176 行）
│   ├── vehicles.py         # 蓝图：/vehicles 车辆档案（139 行）
│   ├── task_flow.py        # 蓝图：任务流 REST + 状态机（616 行）
│   └── audit.py            # 蓝图：OCR 事件审计页 + 聚合/钻取/CSV（132 行，新增）★
├── templates/              # 34 个 Jinja2 模板（31 内容页 + 3 include 组件）
│   ├── base.html           # 公共布局 + 导航 + Tailwind + 顶栏头像（431 行）
│   ├── _smart_add_modal.html    # 共享智能添加弹框（989 行）
│   ├── _image_upload_modal.html # 共享图片上传弹框（397 行）
│   ├── _record_image_script.html # 共享行级图片 JS（883 行，三订单共用）
│   ├── login.html / index.html（70 / 108 行）
│   ├── 三大订单: shipping-records.html（1,540） / inbound-records.html（1,704） / loading-orders.html（1,520）
│   ├── 商品管理: product-units.html / product-categories.html / products.html / piece-conversions.html
│   ├── 信息页: notice.html / notice-color.html / priceboard.html / workflow.html / warehouse.html
│   ├── 操作要点: count-tips.html（741） / huandan-guide.html（642） / billing-tips.html（569）
│   ├── 基础记录: experience.html / errorlog.html / todolist.html / vehicle-maintenance.html / stockout.html
│   ├── 任务流: tasks.html / tasks-new.html / task-detail.html / coding-pool.html
│   ├── 管理: staff.html / vehicles.html / unified-search.html / audit-ocr-events.html（232，新增）
├── static/                 # 静态资源
│   ├── css/app.css         # 共享样式（762 行）
│   ├── js/common.js        # 共享 JS（149 行）—— 唯一的外部 JS 文件 ★
│   ├── mainflow.png / furongflow.png / warehouse.png
│   └── notice/             # 通知图片
├── upload/YYYY-MM/         # 用户上传图片，按月分组（~2,900 张）
├── tests/                  # 测试套件（32 个测试文件 + __init__.py）
├── tools/                  # 工具脚本（3 个）
│   ├── extract_ocr_fixture.py / inspect_overlay.py / split_commits.py
├── sql/                    # SQL 脚本（2 个）
│   ├── new.sql             # 商品分类标准源（MySQL 语法）
│   └── new_sqlite.sql      # 商品分类导入脚本（SQLite 语法）
├── start_server.bat        # Windows 启动脚本
└── setup_startup.ps1       # Windows 自启动 PowerShell 脚本
```

## 路由结构（~159 个端点，按蓝图分布）

所有端点已拆分到 `blueprints/` 目录下的 14 个蓝图 + 2 个辅助模块，主 `app.py` 只保留工厂、上下文、缓存、登录闸门、蓝图注册。

### `blueprints/auth.py` — 身份验证（3 个）
- `GET /login` 登录页（选身份，无密码）· `POST /login` 设 session · `GET|POST /logout` 清 session

### `blueprints/basic_records.py` — 基础记录（13 个）
- `/experience` 工作经验 · `/errorlog` 错误经验 · `/todolist` 待办 · `/vehicle-maintenance` 车辆维护

### `blueprints/info_pages.py` — 信息展示 + stockout（11 个）
- `/` 首页 · `/priceboard` 码数报价 · `/notice-color` 通知彩色版 · `/workflow` 业务流程 · `/warehouse` 仓库布局 · `/count-tips` 点数要点 · `/huandan-guide` 换单要点 · `/billing-tips` 开单要点 · `/stockout` 当前缺货

### `blueprints/notice.py` — 通知（15 个）
- `/notice` CRUD · `/api/v1/notices` REST API · `/api/v1/notice/<id>/images` 图片管理 · `/api/v1/notice/<id>/img-cols` 列数设置

### `blueprints/shipping.py` — 出货（25 个端点）

**页面与订单 CRUD**：
- `GET /shipping-records` 列表页（日期范围/客户筛选/搜索）
- `POST /api/v1/shipping-orders` 创建订单
- `GET|PATCH|DELETE /api/v1/shipping-orders/<id>` 订单 CRUD

**明细行操作**：
- `POST /api/v1/shipping-orders/<id>/records` 添加明细行
- `GET|PATCH|DELETE /api/v1/shipping-records/<id>` 明细行 CRUD
- `POST /api/v1/shipping-records/<id>/move` 移动明细行
- `POST /api/v1/shipping-orders/<id>/records/batch-add` 批量添加

**订单锁定与图片列**：
- `POST /api/v1/shipping-orders/<id>/lock|unlock` 锁定/解锁
- `POST /api/v1/shipping-orders/<id>/img-cols` 图片列数设置

**图片上传与删除**：
- `POST /api/v1/shipping-orders/<id>/images` 订单级图片上传
- `POST /api/v1/shipping-orders/records/<id>/images` 行级图片上传（DeepSeek 优先 + 本地 fallback）
- `GET /api/v1/shipping-orders/records/<id>/images-area` 行级图片合并查询
- `DELETE /api/v1/shipping-orders/images/<id>` 删除图片

**匹配与人工核查**：
- `POST /api/v1/shipping-orders/images/<id>/manual-verify` 人工覆盖/撤销
- `POST /api/v1/shipping-orders/images/<id>/fuzzy-match` 本地模糊匹配重跑
- `POST /api/v1/shipping-orders/images/<id>/ai-judge` AI 判别重跑
- `GET /api/v1/shipping-orders/images/<id>/ocr-detail` OCR/提示词/推理详情
- `POST /api/v1/shipping-orders/<id>/ai-match` 整单 AI 比对（含自适应提示词注入）
- `POST /api/v1/shipping-orders/ai-recognize` AI 图片识别（整单）
- `POST /api/v1/shipping-orders/<id>/check-receipt` 智能验单（订单图片识别）

**自适应提示词**：
- `POST /api/v1/shipping-orders/images/<id>/generate-prompt-suggestion` 生成自适应提示词
- `GET|POST /api/v1/category-prompts` 提示词管理（共享）
- `DELETE /api/v1/category-prompts/<id>` 提示词删除（共享）

**加面图标**: 后端 `has_jia_mian` 计算 + 前端 SVG 渲染（"杂胶+加面"→ 重点列网状图标）

### `blueprints/inbound.py` — 入库（20 个端点，已同步出货行级图片功能）
- `GET /inbound-records` 列表页
- **REST API（`/api/v1/inbound-orders/*`）**: 订单 CRUD、明细 CRUD、move、图片 CRUD
- **行级图片**: 行级上传/合并查询 + manual-verify/fuzzy-match/ai-judge/ocr-detail/gen-prompt（同出货）
- **共享模板**: `_record_image_script.html`（与出货/装柜共用 23 个 JS 函数）
- **日本纸件数换算**: 按件数×每件张数+散装张数对比（支持 loose 与 * 形式）
- **备注**: 2026-07-16 已清理 11 个 HTML form 死端点，统一为 REST API

### `blueprints/loading.py` — 装柜（20 个端点，已同步出货行级图片功能）
- `GET /loading-orders` 列表页
- **REST API（`/api/v1/loading-orders/*`）**: 订单 CRUD、明细 CRUD、move、图片 CRUD
- **行级图片**: 行级上传/合并查询 + manual-verify/fuzzy-match/ai-judge/ocr-detail/gen-prompt（同出货）
- **共享模板**: `_record_image_script.html`（与出货/入库共用 23 个 JS 函数）

### `blueprints/products.py` — 商品管理（18 个）
- `/product-units` 商品单位（72 条数据）
- `/product-categories` 商品类型（`product_categories` 表，205 条 = 1 根 + 9 大类 + 分类树，以 `sql/new.sql` 为最终标准源）
- `/products` 产品管理（`product` 表，1145 条）
- `/piece-conversions` 件数换算规则（16 条）
- `/api/v1/products` REST API（GET/POST/PUT/DELETE）

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
  - `POST /api/v1/tasks/ai-recognize` OCR 识别（复用三引擎）
  - 占位（501）：coding-claim / coding-done / coding-release

### `blueprints/audit.py` — OCR 事件审计（4 个端点，新增）★

- `GET /audit/ocr-events` 审计主页（渲染 `audit-ocr-events.html`，挂导航「📊 OCR 事件审计」）
- `GET /api/v1/audit/ocr-events/aggregate` 按 `prompt_version` 聚合统计（每提示词版本命中 green/yellow/red 数）
- `GET /api/v1/audit/ocr-events/records` 钻取某 `prompt_version` 下的 record↔image 配对明细（支持日期过滤 + 分页）
- `GET /api/v1/audit/ocr-events/export.csv` 导出 CSV（带公式注入防护：`=+-@` 开头强制加 `'` 转文本）

后端走 `models/audit_query.py` 的 `OcrEventAudit`（只读查询：`prompt_stats` / `record_pairs` / `export_rows`）。用途：追踪「自适应提示词」各版本上线后对 AI 比对准确率的实际影响。

### `blueprints/upload.py` — 静态文件
- `/upload/<path>` 访问 upload 目录下的文件

### 主 `app.py` — 应用入口（170 行）
- `create_app()` 工厂函数，注册 14 个蓝图（含 audit 审计蓝图）
- `inject_notices` 全局上下文（注入 all_notices 到所有模板）
- `inject_current_operator` 全局上下文（注入当前操作员 Staff dict 到模板）
- `_require_login` before_request 闸门（白名单：`/static`, `/api/`, `/login`, `/logout`, `/favicon.ico`）
- `add_cache_control_headers` 禁用 HTML 缓存
- `init_db()` 数据库初始化
- 上传超限 413 友好返回

## 数据库（29 张业务表 + sqlite_sequence + 1 个临时清理表）

> 实际 31 张表 = 29 业务表 + `sqlite_sequence`（系统自增序列）+ `_cleanup_safety_2026_07_30`（2026-07-30 数据清洗临时表，非业务表，可忽略/清理）。

### 核心业务表

| 表 | 用途 | 行数 |
|---|---|---|
| `work_logs` | 工作经验 | 25 |
| `error_logs` | 错误经验 | 9 |
| `todo_items` | 待办事项 | 3 |
| `notices` + `notice_images` | 公司通知 | 26 + 5 |
| `vehicle_maintenance` | 车辆维护 | 1 |
| `stock_out_items` | 缺货登记 | 1 |

### 订单三表结构

三种订单都遵循 **订单 → 记录/明细 → 图片** 三表模式：

**出货** (`shipping_orders` / `shipping_records` / `shipping_images`)
- 610 个订单 / 1,387 条记录 / 1,999 张图片

**入库** (`inbound_orders` / `inbound_records` / `inbound_images`)
- 154 个订单 / 515 条记录 / 412 张图片

**装柜** (`loading_orders` / `loading_order_records` / `loading_order_images`)
- 20 个订单 / 111 条记录 / 192 张图片

通用字段：
- `is_locked` (0/1) — 锁定订单防止修改
- 订单级字段：`date` / `customer` / `order_num`（出货和装柜）
- 入库订单只有 `date`、`supplier` 和 `is_locked`
- 明细字段：`product_name` / `specification` / `quantity` / `unit` / `remark`
- 图片存储在 `upload/` 文件夹，通过相对路径引用
- **出货图片特殊**：`match_status`（green/yellow/red）/ `match_score` / `record_pk`（关联明细行）/ `source`（AI/手动）

### 商品资料

| 表 | 用途 | 行数 | 模型类 |
|---|---|---|---|
| `product_units` | 商品单位/规格（YPP 支码换算） | 72 | `ProductUnit` |
| `product_categories` | 商品分类（3 级树，带编码） | 205 | `ProductCategory` |
| `product` | 产品（品名/规格/条码/价格/库存） | 1,145 | `Product` |
| `piece_conversions` | 件数换算规则（件→张/只/令） | 16 | `PieceConversion` |

### 任务流表

| 表 | 用途 | 行数 |
|---|---|---|
| `staff` | 人员档案（姓名/角色/电话/绑定车辆） | 1 |
| `vehicles` | 车辆档案（车牌/吨位/长宽高/年检日期） | 1 |
| `tasks` | 任务（task_no/状态/客户/地址/司机/车辆/打码状态） | 0 |
| `task_items` | 任务明细行（品名/规格/数量/单位/备注） | 0 |
| `task_images` | 任务证据照（stage 白名单 + image_path） | 0 |
| `task_events` | 任务事件历史（advance/assign/return_create 等） | 0 |

### 审计 & OCR 事件

| 表 | 用途 | 行数 |
|---|---|---|
| `audit_log` | 操作审计日志 | 3,507 |
| `ocr_match_event` | OCR 标签匹配事件记录（record_ocr / ai_match / human_verify 三类事件） | 139 |
| `category_prompts` | 自适应提示词（scope=category/spec，注入 AI 比对，见提示词系统） | 见提示词系统 |

**`ocr_match_event` 关键字段**：`event_type`（record_ocr / ai_match / human_verify）、`ocr_text`（PaddleOCR 原文）、`prompt_payload`（完整比对 prompt）、`ai_match_status/score/reason`、`ai_raw_response`（DeepSeek 原始返回）、`ai_engine`、`prompt_version`（按提示词版本聚合用）、`human_status/reason/verified_by`（人工裁决）。

## 关键实现细节

### 1. 登录与权限（T6 新增）

**登录闸门**：`app.py` 的 `_require_login` before_request 保护除 `/static`、`/api/`、`/login`、`/logout` 外的所有页面。REST API 自己返回 401，由前端引导跳转。

**身份模型**：选人即登录（无密码），适合单人/小团队场景。session 存 `operator_id`（int, Staff.id）。多人化时只需加密码层，gate 不变。

**权限系统**（`models/_permissions.py`）：`Action` 枚举（11 种动作）+ `can(operator, action, task)` 集中校验。规则：
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

### 3. OCR 引擎抽象层（三引擎）

`blueprints/ocr_engine.py` 提供统一接口 `recognize(image_bytes, filename) → {"success": bool, "items": [...]}`：

| 引擎 | 类型 | 适用场景 |
|---|---|---|
| **Moonshot** (kimi-k2.6) | 云端视觉 | 手写单据、复杂排版，直接图片→JSON |
| **PaddleOCR** | 本地 CPU | 免费离线 OCR 文字提取，打印单据效果好 |
| **DeepSeek** (v4-flash) | 云端结构化 | PaddleOCR 文字 → LLM → JSON，兼顾免费+语义理解 |

**引擎工厂**：`get_ocr_engine(name)` 单例缓存，通过 `OCR_BACKEND` 环境变量或前端 `#aiEngine` 下拉选择。

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

### 5. 加面图标（航运明细重点列）

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

### 10. 缓存控制
`app.py` 中已禁用开发缓存：
```python
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0
# 所有 HTML 响应添加 Cache-Control 头
```

### 11. 辅助单位提示与备注校验（支数换算）

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

### 12. 备注汇总行（两列统计）

每个日期组底部有汇总行，两列：
- **📊 备注支数**：汇总备注中 `X支` 的支数 + 散码出现次数
- **📊 明细支数**：汇总辅助单位提示列中提取的支数

### 13. 单位归一化（码 → y）

三层防御（防止 '码' 再次写入）：
1. **模型层**：`InboundRecord.create` / `ShippingRecord.create` / `LoadingOrderRecord.create` 均自动归一化
2. **API 层**：三个蓝图的 update + batch-add 端点均检查 `unit == '码' → 'y'`
3. **前端 JS**：三个订单模板的编辑保存流程均归一化

### 14. 整列隐藏（shipping-records.html）

`data-has-jiamian` 属性标记在行上，通过 CSS 控制：有加面的行显示重点列，无加面的行隐藏该列。CSS class `columns-N`（N 为列数）由服务端 render，保证刷新后列宽正确。

### 15. 登录状态与操作员头像

`base.html` 顶栏右侧：登录后显示当前操作员头像（圆角色色块+姓名首字）+ 下拉菜单（切换身份/退出）；未登录时不显示。`inject_current_operator` 上下文注入 `current_operator` dict 到所有模板。

## 出货页面功能流程

### 页面结构

- 新建订单表单（日期 + 客户）→ 日期范围筛选 → 订单分组列表
- 每订单：订单头部（日期/客户/锁定）→ 明细表格 → 图片区
- 每明细行：品名/规格/数量/单位/备注/辅助提示/AI 比对列/操作列

### 图片匹配流水线

```
上传图片
  → PaddleOCR 提取文字 (~2-4s)
  → 背景色检测 [标签背景: 黑/白]
  → DeepSeek compare_single_record() 优先
  → 成功 → match_status(green/yellow/red) + match_score + reason + match_source(deepseek)
  → 失败 → fallback 本地 RapidFuzz → match_source(local_fuzzy)
  → 写入 shipping_images.match_status + match_score + reason + match_source
  → 写入 ocr_match_event (record_ocr 事件, audit trail)
  → 若 DeepSeek → 额外写入 ai_match 事件(含 prompt_payload + raw_response)
```

### 徽章系统

- **match_status**: green(✓/⊛) / yellow(⚠/◇) / red(✗/◆)
- **match_source**: local_fuzzy(✓/⚠/✗) vs deepseek(⊛/◇/◆)
- **human_verified** + yellow/red → 👤（绿，#10b981）
- **行级徽章**: 取该 record 所有图片最差一档
- **bg_color**: ⬛ 黑底 / ⬜ 白底（自动检测标签背景色）

### 每图按钮（img-meta 2 行布局）

- **Row 1**: match-badge + manual-confirm(✓确认通过)/manual-undo(👤已确认) + bg-color-tag + gen-prompt(💡生成提示词)
- **Row 2**: fuzzy-match(本地 RapidFuzz) + ai-judge(DeepSeek) + ocr-detail(详情弹窗)

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

`_record_image_script.html`（883 行，23 函数）：
- 通过 `{% set api_prefix %}` 参数化 URL
- 三套订单共用：shipping / loading / inbound

### 关键 API 模式

- **整单比对**: `/api/v1/shipping-orders/<id>/ai-match`（注入自适应提示词）
- **行级上传**: `/api/v1/shipping-orders/records/<id>/images`（自动 OCR + 匹配）
- **图片操作**: manual-verify / fuzzy-match / ai-judge / ocr-detail / generate-prompt-suggestion

### 行级功能

- **record-image-btn(🖼️)**: 行级上传图片，`has-image` class
- **match-col**: 行级 AI 比对徽章（取最差）
- 编辑/删除/移动/批量添加明细行
- 辅助单位提示（支数换算）+ 备注校验
- 加面图标（杂胶 + 加面 → 重点列网状图标）

### 数据流 — 创建订单

1. 用户填日期 + 客户 → `POST /api/v1/shipping-orders`
2. 订单 + 明细行插入 `shipping_orders` + `shipping_records`
3. 页面刷新显示订单组
4. 行级上传图片 → OCR 匹配 → badge 更新

### 数据流 — 图片匹配

1. 上传行级图片 → PaddleOCR 提取文字
2. DeepSeek 优先 → fallback 本地 RapidFuzz
3. 写入 match_status/score/reason/source
4. 前端局部刷新徽章 + 行级徽章
5. 黄/红牌 → 人工确认按钮 → 确认后变 👤 绿色
6. 确认后显示 💡 生成提示词 → 可保存自适应提示词

## 模板设计约定

- 所有模板继承 `base.html`
- 导航分为 7 个分组（下拉菜单）：
  - **运营信息** — 公司通知、通知彩色版、业务流程、仓库布局、综合查找
  - **当前缺货** — 单页
  - **商品管理** — 商品单位、商品类型、产品管理、件数换算
  - **码数报价** — 单页
  - **操作要点** — 开单要点、换单要点、点数要点
  - **快速录入** — 工作经验、错误经验、待办事项、车辆维护记录
  - **任务流**（新增） — 任务列表、新建任务、打码抢单池、人员档案、车辆档案
  - **订单操作**（单独） — 装柜订单、出货记录、入库记录
- 样式以 Tailwind CSS 为主，共享自定义 CSS 在 `static/css/app.css`
- 共享 JS 工具函数在 `static/js/common.js`
- 每个模板底部用 `<script>` 内联实现交互
- 共享弹框 `_smart_add_modal.html` / `_image_upload_modal.html` 被多个模板 include 复用

## Windows 环境注意事项

1. **CRLF 换行符**: 代码库使用 Windows CRLF 换行符。注意 SQL 查询中的多行字符串替换。

2. **PowerShell 解析**: 避免在 PowerShell 中使用带有特殊字符（`&`、`)`）的内联 Python。始终先将 Python 脚本写入文件，再执行。

3. **文件路径**: 数据库存储 Windows 风格路径（`upload\2026-05\file.jpg`）。模板通过 `get_relative_path()` 方法转换为 URL 风格。

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
- `app.py` — 170 行（`create_app()` 工厂 + 缓存控制 + 登录闸门 + 14 蓝图注册 + 操作员上下文）

### 蓝图（17 个模块，按大小排序）
| 文件 | 行数 | 关键内容 |
|---|---|---|
| `blueprints/shipping.py` | 1,577 | 出货 REST + AI 识别 + OCR 匹配 + 行级图片 + 加面 + 自适应提示词 |
| `blueprints/inbound.py` | 1,159 | 入库 REST + 行级图片（已删 HTML form 端点，统一 REST） |
| `blueprints/ocr_engine.py` | 1,064 | 三引擎（Moonshot/PaddleOCR/DeepSeek）+ 后处理安全网 + COMPARE_PROMPT |
| `blueprints/loading.py` | 1,049 | 装柜 REST + 行级图片 |
| `blueprints/task_flow.py` | 616 | 任务流 REST + 状态机 + 证据闸门 + 建单/推进/退单/退货 |
| `blueprints/_helpers.py` | 552 | 共享：图片上传校验、YPP 匹配、支数换算、备注校验、汇总 |
| `blueprints/products.py` | 302 | 商品管理 |
| `blueprints/notice.py` | 220 | 通知 CRUD |
| `blueprints/staff.py` | 176 | 人员档案 CRUD |
| `blueprints/audit.py` | 132 | OCR 事件审计：聚合/钻取/CSV 导出（新增）★ |
| `blueprints/basic_records.py` | 126 | 基础记录 |
| `blueprints/info_pages.py` | 87 | 信息展示页 |
| `blueprints/search.py` | 80 | 综合查找 |
| `blueprints/auth.py` | 67 | 登录/登出 |
| `blueprints/upload.py` | 17 | 静态文件服务 |

### 数据库层（14 个模块）
| 文件 | 行数 | 模型数 | 关键类 |
|---|---|---|---|
| `models/orders.py` | 2,068 | 10 | 三套订单 9 模型 + UnifiedSearch + OcrMatchEvent |
| `models/_init.py` | 756 | — | 29 张表 DDL + 迁移逻辑 |
| `models/tasks_flow.py` | 491 | 10 | Staff/StaffDB/Task/TaskItem/TaskImage/TaskEvent |
| `models/products.py` | 302 | 3 | ProductUnit / ProductCategory / Product |
| `models/audit_query.py` | 273 | 1 | OcrEventAudit：OCR 事件只读查询（prompt_stats/record_pairs/export_rows）★ |
| `models/category_prompt.py` | 238 | 1 | CategoryPrompt + classify_record：自适应提示词（新增）★ |
| `models/notice.py` | 144 | 2 | Notice / NoticeImage |
| `models/basic.py` | 134 | 4 | WorkLog / ErrorLog / TodoItem / VehicleMaintenance |
| `models/piece_conversion.py` | 101 | 1 | PieceConversion |
| `models/_permissions.py` | 99 | — | Action 枚举 + can() 函数 |
| `models/audit.py` | 58 | 1 | AuditLog |
| `models/stock.py` | 40 | 1 | StockOutItem |
| `models/__init__.py` | 38 | — | re-export |
| `models/_db.py` | 22 | — | get_db() + DB_PATH |

### 模板（34 个文件，按大小排序）
| 文件 | 行数 | 说明 |
|---|---|---|
| `inbound-records.html` | 1,704 | 入库记录（含行级图片 JS） |
| `shipping-records.html` | 1,540 | 出货记录（含行级图片 JS） |
| `loading-orders.html` | 1,520 | 装柜订单（含行级图片 JS） |
| `_smart_add_modal.html` | 989 | 共享智能添加弹框 |
| `_record_image_script.html` | 883 | 共享行级图片 JS（三订单 include 复用，23 函数）★ |
| `count-tips.html` | 741 | 点数要点 |
| `huandan-guide.html` | 642 | 换单要点 |
| `billing-tips.html` | 569 | 开单要点 |
| `priceboard.html` | 523 | 码数报价 |
| `products.html` | 478 | 产品管理 |
| `notice-color.html` | 477 | 通知彩色版 |
| `product-categories.html` | 449 | 商品类型 |
| `base.html` | 431 | 公共布局+导航 |
| `notice.html` | 428 | 通知管理 |
| `_image_upload_modal.html` | 397 | 共享图片上传弹框 |
| `tasks-new.html` | 345 | 新建任务（OCR） |
| `staff.html` | 308 | 人员档案 |
| `task-detail.html` | 300 | 任务详情 |
| `vehicles.html` | 254 | 车辆档案 |
| `unified-search.html` | 235 | 综合查找 |
| `audit-ocr-events.html` | 232 | OCR 事件审计页（新增）★ |
| `product-units.html` | 221 | 商品单位 |
| `stockout.html` | 220 | 当前缺货 |
| `tasks.html` | 219 | 任务列表 |
| `vehicle-maintenance.html` | 174 | 车辆维护 |
| `piece-conversions.html` | 125 | 件数换算 |
| `index.html` | 108 | 首页 |
| `coding-pool.html` | 100 | 打码抢单池 |
| `errorlog.html` | 72 | 错误经验 |
| `workflow.html` | 71 | 业务流程 |
| `login.html` | 70 | 登录选身份 |
| `todolist.html` | 50 | 待办事项 |
| `experience.html` | 48 | 工作经验 |
| `warehouse.html` | 45 | 仓库布局 |

## 待办 / 待清理

- [x] ~~统一 `product` / `product_category` 命名~~ — 已完成
- [x] ~~图片上传后局部刷新~~ — 已完成
- [x] ~~`app.py` 按业务拆分蓝图~~ — 已完成
- [x] ~~单位归一化 `码 → y`~~ — 已完成
- [x] ~~辅助单位提示升级~~ — 已完成
- [x] ~~备注汇总行拆分~~ — 已完成
- [x] ~~商品分类树按 category_code 排序~~ — 已完成
- [x] ~~任务流 M1~~ — 基本完工（2026-07-22），17 task 完成 16，仅抢单池占位
- [ ] 打码抢单池 M2（抢单/完成/释放真逻辑，当前 501 占位）
- [ ] 推送 66 个本地 commits 到 GitHub（需先开梯子，见 [[GitHub 需要梯子]]）
- [ ] D 盘数据库备份（已停 5 天 +）
- [ ] task_flow.py 中 `coding-claim`/`coding-done`/`coding-release` 三个端点 501 占位 → M2 实现

## 长期规划（2026-06-08 拍板）

**核心方向**：把出货/入库/装柜从「事后记账系统」升级为「送货流程的实时管控 + 证据链」。

### 当前进度

**阶段 1：商品库完善** ✅ 基本完成
- `product` 表从 0 行 → 1145 行
- `product_units` 从 67 → 72 条
- `product_categories` 从 186 → 205 条
- `piece_conversions` 件数换算规则 16 条

**阶段 2：AI 验数 + 校对** ✅ 已完成
- 三引擎 OCR（Moonshot/PaddleOCR/DeepSeek）+ 汇总行安全网
- 出货明细行标签 OCR 匹配（PaddleOCR + DeepSeek COMPARE_PROMPT）
- AI 比对 reason + human_verified 人工确认
- 辅助单位提示 + 备注 mismatch 红/粉行

**阶段 3：任务流 M1** ✅ 基本完成
- 7 态状态机 + 证据闸门 + 权限系统
- 人员档案（6 种角色）+ 车辆档案
- 登录闸门（选身份，无密码）
- 打码抢单池占位（M2 接）

**阶段 4：多人化 + 权限** 🚧 M1 已做准备
- 身份选择登录（无密码）→ 登录闸门 → 权限枚举
- 架构已预留密码升级路径（`login_name` / `password_hash` 字段已存在但未启用）
- 待定：司机端/移动端形态（Web PWA / 小程序 / 原生 App）

### 已落地的"司机端预备工作"
- [x] 任务流状态机（7 态推进 + 证据链拍照）
- [x] 权限模型（6 角色 × 11 动作）
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
- **当前阶段**：任务流 M1 基本完工（2026-07-22），已从「事后记账」升级为「任务流实时管控 + 证据链」
- **演进路线**：商品库 ✅ → AI 验数 ✅ → 任务流 M1 ✅ → 打码抢单池 M2 → 司机端/移动端 → 多人化 + 权限
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
**维护提示（2026-07-25）**：Moonshot/DeepSeek 偶尔下线旧模型，出现 400 + "model not found" 时先到对应平台 docs 查当前可用模型清单再改 MODEL 常量。

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
1. 任何涉及 db 的破坏性操作（删行、覆盖、回滚、批量更新）**之前**，必须 `Copy-Item worklog.db _safe-snapshot\<时间戳>\worklog.db` 完整备份一份
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

## 数据库备份约定（2026-06-10 起）

**专用备份目录**：`D:\BAK\`

**命名规则**：`worklog_YYYYMMDD_HHmm.db`（24 小时制，本地时间）

**典型命令**：
```powershell
Copy-Item "C:\Users\Administrator\worklog-app\worklog.db" "D:\BAK\worklog_$(Get-Date -Format 'yyyyMMdd_HHmm').db" -Force
```

**触发时机**：
- 补录完一批订单/通知/分类后（用户明确要求时）
- 任何 db 破坏性操作前（见"踩坑点 6"）
- 每次大改 schema / 跑迁移脚本前

**保留策略**：D 盘容量充足，**不主动清理**——保留历史快照作为时间机器；用户主动说"清理旧的"才动手（必须用 `mavis-trash`，不许 `Remove-Item -Force`）。项目目录下的临时备份保留至少 1 份作近期参考即可，不需要全量同步到 D 盘。
