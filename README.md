# 丰源工作台 (Worklog App)

Flask 单用户本地工作台,围绕"事后记账 → 任务流实时管控 + 证据链"演进,服务仓库管理 + 外贸装柜。代码于 2026-08-23 盘点。

---

## 1. 项目概述

丰源工作台是一个**给单人/小团队自己用的工作台**,不是 SaaS。从「记一笔出库单」演进到「美团骑手式实时管控 + 证据链」,现已形成四条业务线 + 一条 AI/OCR 流水线。

| 业务线 | 核心实体 | 一句话描述 |
|--------|----------|------------|
| 基础记录 | 工作日志 / 错误经验 / 待办 / 车辆维护 / 公司通知 | 经验沉淀 + 团队通知 |
| 订单三套 | 出货单 / 入库单 / 装柜单 | 订单 + 明细 + 行级图片 + AI 比对 |
| 任务流 | 任务 / 任务明细 / 证据照 / 事件历史 | 7 状态机:准备中→已装货→已点数→已到达→已卸货→已完成 |
| 商品资料 | 商品单位 / 分类(3 级树) / 产品 / 件数换算 | 1000+ SKU,码/支换算 + 自适应提示词 |
| 独立点数 | 会话 / 图片 / 标记点 | 临时开会话拍照点数,不挂订单 |
| AI/OCR 流水线 | 标签匹配 + 证据链 | Moonshot/PaddleOCR/DeepSeek 三引擎 + 退化类型分桶 |

**用户视角关键事实**:

- 单人经营,既是业务运营者又是工具维护者
- Windows 11 + PowerShell 5.1 工作环境
- 自维护系统:代码、数据库、文档同仓演进,不做多租户/权限严格化
- 关键约束:先备份 + 精确 id 匹配 + 重要变更走 `_safe-snapshot/`,杜绝 2026-06-09 那种 db 误覆盖

---

## 2. 技术栈

| 组件 | 技术 | 备注 |
|------|------|------|
| 后端 | Python 3.12 + Flask 3.1.3 | 工厂模式 + 17 蓝图 |
| 数据库 | SQLite 3.45.3 (WAL) | 文件 `worklog.db`,35 张业务表 |
| 前端 | 原生 JS + Tailwind(内联)+ 共享 `app.css` | 共享 JS 仅 `static/js/common.js`(149 行) |
| AI 视觉(云) | Moonshot Kimi k2.6 | 端点 `https://api.moonshot.cn/v1`,图片直识 |
| AI 结构化(云) | DeepSeek v4 Flash | OpenAI 兼容协议,基址可配 |
| OCR(本地 CPU) | PaddleOCR 3.x | 双实例:`_ocr`(默认阈值)+ `_wrinkle_ocr`(低阈值,专攻褶皱) |
| 模糊匹配 | RapidFuzz 3.x | 标签行匹配 + 语音短语匹配 |
| 图片预处理 | Pillow 10.x | 旋转、背景色检测、文件魔数校验 |
| 语音转码 | ffmpeg(外部依赖) | webm → pcm_s16le 16k 单声道 |
| 依赖(直) | 5 个 | Flask / openai / python-dotenv / rapidfuzz / Pillow |

**Windows DLL 修复**:`ocr_engine.py` 在 `import torch`(PaddleOCR 间接拉入)前把 `torch/lib` 加到 `os.add_dll_directory()`,避免 `shm.dll` 缺失。

---

## 3. 目录结构(简化)

```
worklog-app/
├── app.py                         入口(204 行,工厂 + 19 蓝图注册)
├── models/                        16 个文件,re-export 35 业务表
│   ├── _init.py (972)             init_db() 建表 + 迁移
│   ├── _db.py (22)                get_db() + DB_PATH
│   ├── _permissions.py (100)      Action 枚举(13 种)+ can() 集中校验
│   ├── orders.py (2639)           出货/入库/装柜 9 模型 + UnifiedSearch + OcrMatchEvent
│   ├── tasks_flow.py (492)        Staff/Task/TaskItem/TaskImage/TaskEvent + 对应 DB 类
│   ├── category_prompt.py (616)   CategoryPrompt + classify_record(自适应提示词)
│   ├── audit_query.py (273)       OcrEventAudit(只读查询)
│   ├── products.py / basic.py / notice.py / piece_conversion.py / stock.py
│   ├── point_count.py (405)       PointCountSession / PointCountImage
│   ├── voice_mapping.py (135)     VoiceMapping(口语映射)
│   └── audit.py (57)              AuditLog
├── blueprints/                    26 个 .py(19 蓝图 + 7 辅助模块)
│   ├── 三大订单: shipping.py (1961) / inbound.py (1182) / loading.py (1447)
│   ├── 任务流:   task_flow.py (617)
│   ├── 业务:     notice.py (220) / products.py (302) / basic_records.py (124)
│   │             info_pages.py (85) / search.py (80) / staff.py (177) / vehicles.py (140)
│   │             audit.py (128) / category_prompts_manage.py (117)
│   ├── 新:       mobile_shipping.py (212) / point_count.py (408) / voice.py (210)
│   ├── 认证/上传: auth.py (67) / upload.py (15)
│   └── 辅助:     ocr_engine.py (2486) / ocr_pipeline.py (437) / ocr_log.py (242)
│                 _helpers.py (756) / voice_pipeline.py (142)
│                 voice_baidu.py (86) / voice_fuzzy.py (127) / voice_llm.py (172)
├── templates/                     47 个 Jinja2 模板
│   ├── 三大订单: shipping-records.html (1986) / inbound-records.html (1763) / loading-orders.html (1524)
│   ├── 共享子页面: _image_upload_modal.html (418) / _smart_add_modal.html (995)
│   │              _record_image_script.html (1182) / _voice_input_modal.html (129)
│   ├── 任务流: tasks.html / tasks-new.html (346) / task-detail.html (301) / coding-pool.html
│   ├── 管理: staff.html / vehicles.html / unified-search.html / audit-ocr-events.html
│   │        point-count.html / point-count-session.html / manage-category-prompts.html
│   ├── 信息页: index / notice / notice-color / priceboard / workflow / warehouse
│   │          count-tips / huandan-guide / billing-tips / stockout
│   ├── 基础记录: experience / errorlog / todolist / vehicle-maintenance
│   ├── 出口: shipping_ypp_review.html (YPP 核查,2026-08-22 新增)
│   └── mobile/  8 个移动端子页(出货/入库/装柜 + 点数,2026-08-09 上线)
├── static/                        css/app.css (785) / css/mobile.css / css/point-count.css
│                                  js/common.js (149) / js/voice_input.js (282)
│                                  js/placement_count.js (529) / js/point-count.js
│                                  js/point-count-list.js / js/mobile_*.js (5 个)
├── upload/YYYY-MM/                用户上传图片(2026-08 时 ~6,000+ 张)
├── log/YYYYMM/                    业务日志(按天分文件,OCR/AI 落盘)
├── _safe-snapshot/                变更前备份(db / diff / README 历史快照)
├── tools/                         工具脚本(extract_ocr_fixture / inspect_overlay / split_commits)
├── tests/                         81 个测试文件
├── sql/                           new.sql(MySQL) / new_sqlite.sql(SQLite 分类导入)
├── logging_setup.py               166 行,trace_id ContextVar + 线程上下文
└── requirements.txt               5 个直接依赖
```

---

## 4. 蓝图与路由(19 个 Blueprint,共 224 个端点)

> 数字由 `grep -E '^\s*@bp\.(route|api)\(' blueprints/*.py` 实际数得(2026-08-23)。`point_count` 拆 `bp` + `bp_api` 两个 Blueprint 共用同一文件。

| Blueprint | URL 前缀 | 端点数 | 用途 |
|-----------|----------|--------|------|
| `auth` | `/login` `/logout` | 3 | 选身份登录(无密码) |
| `upload` | `/upload/<path>` | 1 | upload 目录静态文件 |
| `basic_records` | `/experience` `/errorlog` `/todolist` `/vehicle-maintenance` | 13 | 4 类基础记录 CRUD |
| `info_pages` | `/` `/priceboard` `/notice-color` `/workflow` `/warehouse` `/count-tips` `/huandan-guide` `/billing-tips` `/stockout` | 11 | 信息展示 + 当前缺货 |
| `notice` | `/notice` + `/api/v1/notices` | 15 | 公司通知 CRUD + 图片管理 |
| `products` | `/product-units` `/product-categories` `/products` `/piece-conversions` `/api/v1/products` | 18 | 商品资料 4 套 + REST |
| `shipping` | `/shipping-records` + `/shipping-ypp-review` + `/api/v1/shipping-orders` | 40 | 出货订单全套(含 placement-images 11 端点) |
| `inbound` | `/inbound-records` + `/api/v1/inbound-orders` + `/m/inbound-*` | 23 | 入库订单 + 行级图 + 移动端 |
| `loading` | `/loading-orders` + `/api/v1/loading-orders` + `/m/loading-*` | 34 | 装柜订单 + 行级图 + 移动端 |
| `search` | `/unified-search` + `/api/v1/unified-search` | 2 | 综合查找(跨三套订单) |
| `task_flow` | `/tasks` `/tasks/new` `/tasks/<id>` `/coding-pool` + `/api/v1/tasks` | 17 | 任务流 M1(7 状态 + 退单/退货/作废) |
| `staff` | `/staff` | 5 | 人员档案(6 角色 + 软删 + 启用) |
| `vehicles` | `/vehicles` | 5 | 车辆档案 + 软删 + 启用 |
| `audit` | `/audit/ocr-events` + `/api/v1/audit/ocr-events` | 4 | OCR 事件审计(聚合/钻取/CSV) |
| `voice` | `/api/v1/voice` | 6 | 语音识别/确认/口语映射 CRUD |
| `category_prompts_manage` | `/manage/category-prompts` | 4 | 自适应提示词管理页 |
| `mobile_shipping` | `/m/` `/m/shipping-today` + `/order/<id>` + `/placement` | 4 | 移动端当天出货 |
| `point_count`(bp) | `/tools/point-count` | 3 | 独立点数会话列表 + 详情 |
| `point_count`(bp_api) | `/api/v1/point-count` | 16 | 独立点数 REST |
| **合计** | | **224** | |

**辅助模块(无 Blueprint,只被蓝图 import)**:`ocr_engine.py`、`ocr_pipeline.py`、`ocr_log.py`、`_helpers.py`、`voice_pipeline.py`、`voice_baidu.py`、`voice_fuzzy.py`、`voice_llm.py`。

**App 工厂职责**:`app.py` 保留 Flask 工厂、登录闸门(`_require_login`)、缓存控制、上传 413 处理、ContextVar 重置(`_reset_log_context`)、19 次 `register_blueprint`(`auth` 必须最先注册)。

**登录闸门**:`_AUTH_PUBLIC_PREFIXES = ("/static", "/api/", "/m/", "/upload/")`,`_AUTH_PUBLIC_PATHS = ("/login", "/logout", "/favicon.ico")`。`/m/*` 免登录,便于微信直接打开链接;`/api/*` 自行返回 401,由前端引导跳转。

---

## 5. 数据库(35 张业务表)

> 数字由 `grep "CREATE TABLE" models/_init.py` 实际数得。`models/__init__.py` 统一 re-export 所有模型类。

### 5.1 基础记录(6 张)

| 表 | 用途 | 字段要点 | 实际行数(2026-08-23) |
|----|------|----------|----------------------|
| `work_logs` | 工作经验 | id / title / content / created_at | 25 |
| `error_logs` | 错误经验 | id / title / error_type / solution | 9 |
| `todo_items` | 待办事项 | id / content / done | 3 |
| `notices` | 公司通知 | id / category / content / sort_order / img_cols | 26 |
| `notice_images` | 通知图片 | id / notice_pk / file_name(走 `static/notice/`) | 5 |
| `vehicle_maintenance` | 车辆维护 | id / date / vehicle_plate / type / cost | 1 |

### 5.2 三大订单(9 张)

三套订单都遵循 **订单 → 记录/明细 → 图片** 三表模式:

**出货**(`shipping_orders` / `shipping_records` / `shipping_images`)

- 758 订单 / 1,864 记录 / 3,117 图片
- 订单字段:date / customer / order_num / is_locked
- 记录字段:product_name / specification / quantity / unit / remark
- 图片特殊:`match_status` (green/yellow/red) / `match_score` / `record_pk` / `source` (AI/手动) / `bg_color` / `human_verified` / `human_status` / `human_reason` / `human_verified_by`

**入库**(`inbound_orders` / `inbound_records` / `inbound_images`)

- 199 订单 / 653 记录 / 569 图片
- 订单只有 `date` / `supplier` / `is_locked`(无客户、无单号)
- 支持日本纸件数换算(loose + *)

**装柜**(`loading_orders` / `loading_order_records` / `loading_order_images`)

- 22 订单 / 119 记录 / 219 图片
- 与出货同构,装柜行为:**不注入自适应提示词**(2026-07-30 决定,`with_supplement=False`)

### 5.3 订单配套(2 张,2026-08 上线)

| 表 | 用途 | 行数 |
|----|------|------|
| `placement_marks` | 出货**摆放图**(散点 + 散码 + 支/件),关联 shipping_records.id | 2,104 |
| `loading_placement_marks` | 装柜**摆放图**(装柜当前未启用) | 0 |

### 5.4 商品资料(4 张)

| 表 | 用途 | 字段要点 | 行数 |
|----|------|----------|------|
| `product_units` | 商品单位/规格 + YPP 码/支换算 | product_name / spec_keyword / yards_per_piece / is_usingyardforcounting | 77 |
| `product_categories` | 3 级树(1 根 + 9 大类 + 叶子) | category_code / category_name / level(1-4)/ parent_id / sort_order / status | 205 |
| `product` | 产品库 | product_code / product_name / category_id / specification / barcode / cost_price / stock_quantity / preset_price | 1,145 |
| `piece_conversions` | 件数换算规则(件→张/只/令) | product_name / units_per_piece / target_unit | 16 |

> 迁移残留:`product_units_new` 在 `_init.py` 出现,实际为旧迁移 ALTER 残留,业务上不直接用。

### 5.5 任务流(5 张,2026-08 M1 落地)

| 表 | 用途 | 字段要点 | 行数 |
|----|------|----------|------|
| `staff` | 人员档案(6 角色) | name / role(CHECK 约束)/ phone / vehicle_id(FK)/ is_active(软删) | 7 |
| `vehicles` | 车辆档案 | plate_no(UNIQUE)/ tonnage / length / width / height / status | 1 |
| `tasks` | 任务(主线) | task_no / status / coding_status / customer / dest_address / driver_id / vehicle_id / is_cancelled / depart_at / arrive_at | 0 |
| `task_items` | 任务明细 | product_name / specification / quantity / unit / remark / sort_order | 0 |
| `task_images` | 任务证据照(9 种 stage) | stage(白名单)/ image_path | 0 |
| `task_events` | 任务事件历史 | event_type(advance/assign/driver_release)/ from_status / to_status / operator_id | 0 |

> 任务表 2026-08-23 实际尚未投产,但 schema/状态机/UI 全部就位。

### 5.6 审计 + OCR 事件 + 提示词 + 口语映射(4 张)

| 表 | 用途 | 字段要点 | 行数 |
|----|------|----------|------|
| `audit_log` | 操作审计 | entity_type / entity_id / action / user_id / created_at | 7,023 |
| `ocr_match_event` | OCR 标签匹配事件 | event_type(record_ocr/ai_match/human_verify)/ ocr_text / ai_match_status / score / reason / prompt_version / prompt_payload / ai_raw_response / human_status | 1,083 |
| `category_prompts` | 自适应提示词 | scope(category/spec)/ category_code / prompt_text / status | 211 |
| `voice_phrase_mapping` | 口语短语映射 | phrase / product_id(FK)/ spec_hint / source(user_confirmed/llm_fallback) | 0 |

### 5.7 独立点数(3 张,2026-08-17 上线)

| 表 | 用途 | 行数 |
|----|------|------|
| `point_count_sessions` | 点数会话(title / expected_count / unit / status(open/closed)) | 1 |
| `point_count_images` | 会话图 | 1 |
| `point_count_marks` | 点击计数(x_ratio / y_ratio) | 14 |

### 5.8 缺货(1 张)

| 表 | 用途 | 行数 |
|----|------|------|
| `stock_out_items` | 当前缺货(name / color) | 1 |

---

## 6. 核心功能

### 6.1 三大订单(出货/入库/装柜)

**核心共性**:订单 → 明细 → 行级图片(标签)→ AI 比对 → 人工覆盖 → (可选)摆放图。每张行级图都跑一遍 OCR + DeepSeek 比对,落库 `match_status` + `match_score`,前端按 green/yellow/red 渲染徽章。

**三套对比**:

| 维度 | 出货 | 入库 | 装柜 |
|------|------|------|------|
| 页面路径 | `/shipping-records` | `/inbound-records` | `/loading-orders` |
| 移动端 | `/m/shipping-today` | `/m/inbound-today` | `/m/loading-today` |
| 模板行数 | 1,986 | 1,763 | 1,524 |
| Blueprint 行数 | 1,961 | 1,182 | 1,447 |
| 订单字段 | date/customer/order_num | date/supplier | date/customer/order_num |
| 自适应提示词 | ✅ 注入(出货 + 入库) | ✅ 注入 | ❌ 不注入 |
| YPP 核查页 | ✅ `/shipping-ypp-review` | — | — |
| 摆放图 | ✅ `placement_marks` | — | ⚠️ schema 有,数据为 0 |
| align 复制至出货 | — | ✅(`align` API) | — |

**统一 OCR pipeline(`blueprints/ocr_pipeline.py`,2026-08-19 抽取)**:

```
[行级图上传]
    ↓ RecordImageProcessor.extract_ocr()
    ├── (use_cached=True) 从 ocr_match_event 表查 record_ocr 缓存
    └── (未命中) PaddleOCR 抽字 + detect_bg_color (黑/白磅布三文治)
    ↓ RecordImageProcessor.classify()
    ├── DeepSeek.compare_single_record(优先,带自适应提示词)
    └── 失败/未配置 → RapidFuzz 本地降级
    ↓ RecordImageProcessor.persist_match()
    ├── 写 image.match_status / score / reason
    ├── 写 OcrMatchEvent('record_ocr' + 'ai_match')
    └── avg_conf < 0.5 时 append "[图像可能模糊，建议重拍]" 到 reason
```

三套订单通过注入不同的 `image_model`(`ShippingImage` / `InboundImage` / `LoadingOrderImage`)共用同一处理器;`ocr_engine_getter` 用 lambda 包裹,让测试 `mock.patch('blueprints.shipping.get_ocr_engine')` 能穿透到 `ocr_pipeline`。

**PaddleOCR 退化类型分桶**(由 `ocr_engine.ocr_preprocess_kind(product_name)` 判定):

| kind | 触发品名示例 | 处理 |
|------|------------|------|
| `KIND_FORM_NOLINES` | 磅布三文治 | 用 `_wrinkle_ocr`(低阈值)分行 OCR |
| `KIND_GLARE` | 无纺布 | 反光抑制预处理 |
| `KIND_REDSTAMP` | 杂胶/纯胶 | 红章掩膜 + 擦除 |

**订单级图特殊字段**:`source_tag`(备货照/装车照/归仓照,移动端用,2026-08-10 增)+ `rotate_deg`(90/180/270,移动端拍照方向修正)。

**输出汇总行检测**:`_filter_summary_items()` 安全网过滤 LLM 未能跳过的汇总行(5 种检测:关键词/无品名/异常大数量/remark 汇总/规格汇总),所有引擎统一后处理。

### 6.2 任务流(M1,7 状态机)

**主线**(对应 `task_flow.ALLOWED_TRANSITIONS`):

```
准备中 ─→ 已装货 ─→ 已点数 ─→ 已到达 ─→ 已卸货 ─→ 已完成
                                  └─→(异常)已拒收
                                  └─→(异常)已作废
```

**推进闸门**(`EVIDENCE_GATES`):

| 跳转 | 必填证据照(stage) | 额外 |
|------|-------------------|------|
| 准备中 → 已装货 | — | 必须已指派司机 + coding_status ∈ {无需打码, 打码完成} |
| 已装货 → 已点数 | 装车照 | `can(op, Action.LOAD, t)` |
| 已点数 → 已到达 | 点数标签照 / 点数整体照 | `can(op, Action.COUNT, t)` |
| 已到达 → 已卸货 | 卸货照 | `can(op, Action.ARRIVE, t)` |
| 已卸货 → 已完成 | 签收单 | `can(op, Action.UNLOAD, t)` |

**异常处理**:
- 司机退单 `POST /api/v1/tasks/<id>/driver-release`:已装货 → 准备中,必须先有 `退单照` 证据,解绑司机/车辆
- 任务作废 `POST /api/v1/tasks/<id>/cancel`:仅 准备中 状态,需调度/文员
- 整单退回 `POST /api/v1/tasks/<id>/return-all`:上传退货照 → 自动生成退货任务(数量负)

**占位端点**(M1 留空,后续 M2 补,当前 501):
- `coding-claim` / `coding-done` / `coding-release`(打码三操作,仅打码角色)

**权限矩阵**(`models/_permissions.py` 中 `can()` 集中校验,13 种 Action 枚举):

| 动作 | 司机 | 调度 | 搬运 | 打码 | 仓管 | 文员 |
|------|------|------|------|------|------|------|
| 建单/编辑 | ✓ | ✓ | ✓ | ✓ | ✓ | ✓ |
| 装货/到达/卸货 | 本单 | | | | | |
| 司机退单 | 本单 | | | | | |
| 指派司机 | | ✓ | | | | ✓ |
| 任务作废 | | ✓ | | | | ✓ |
| 打码三操作 | | | | ✓ | | |
| 点数 | | | | | ✓ | ✓ |
| 退货 | | | | | | ✓ |

### 6.3 OCR 引擎抽象层(三引擎)

`blueprints/ocr_engine.py` 提供 `recognize(image_bytes, filename) → {success, items: [...]}`:

| 引擎 | 类型 | 适用场景 | 关键常量 |
|------|------|----------|----------|
| **Moonshot** (kimi-k2.6) | 云端视觉 | 手写单据、复杂排版 | 端点 `https://api.moonshot.cn/v1` |
| **PaddleOCR** | 本地 CPU | 免费离线 OCR | `_ocr` 默认阈值 + `_wrinkle_ocr` 低阈值 |
| **DeepSeek** (v4-flash) | 云端结构化 | PaddleOCR 文字 → LLM → JSON | OpenAI 兼容,`compare_single_record()` |

**工厂模式**:`get_ocr_engine(name)` 单例缓存,前端 `#aiEngine` 下拉或 `OCR_BACKEND` 环境变量选择。

**DeepSeek 比对 4 大规则**(`COMPARE_PROMPT`):环保/7P/15P/18P/21P 等价词;手感/柔软/硬挺 分级;规格数字 ±10% 容差;识别置信度低的标签默认 yellow 而非 red。

**prompt 版本**:`OCR_MATCH_PROMPT_VERSION = 'compare_rows_v2'`,写 `OcrMatchEvent.prompt_version`,审计页按版本聚合。

### 6.4 自适应提示词(`category_prompts`)

每条提示词 = `scope(category|spec) × category_code(精确) + prompt_text(规则描述)`。AI 比对前由 `CategoryPrompt.compose_for_record(category_code, product_name, spec)` 拼进 prompt。

**两种 scope**:
- `category`:作用于该 category_code 下所有明细
- `spec`:更细,需 product_name/spec 匹配(预留,装柜暂未启用)

**管理端**:`/manage/category-prompts` 列表 + 单条编辑 + 批量导入;出货页 `image.reason` 落 yellow/red 时一键生成"建议补充提示词"草稿(模板化,不调 LLM,免 API key 依赖)。

### 6.5 语音录入(3 路径匹配,2026-08-06 上线)

```
浏览器录音 webm
    ↓ ffmpeg 转 pcm_s16le 16k 单声道
    ↓ BaiduASR.transcribe(短语音识别,30 天 token 缓存)
    ↓ DeepSeek 切句(失败 → 规则切句降级)
    ↓ phrase_part 候选匹配
    ├── 路径 1: voice_phrase_mapping 表(用户确认过的,score=100,优先)
    ├── 路径 2: RapidFuzz 在 product 表 fuzzy(top_n=5)
    └── 路径 3: DeepSeek 兜底(给 Top 30 候选,选 1 个 product_id)
    ↓ 用户在前端选择/修正候选
    ↓ POST /api/v1/voice/confirm
        ├── 写 voice_phrase_mapping(source=user_confirmed)
        └── 批量插入 ShippingRecord(出货页码/支单位自动归一化)
```

**语音子模块拆分**:`voice_baidu.py`(BaiduASR)+ `voice_fuzzy.py`(中文数字/RapidFuzz/规则切句)+ `voice_llm.py`(DeepSeek 切句 + LLM 兜底 + 类目树)+ `voice_pipeline.py`(orchestrator)。

**环境变量**:`BAIDU_API_KEY` / `BAIDU_SECRET_KEY` / `FFMPEG_PATH` / `DEEPSEEK_API_KEY` / `DEEPSEEK_BASE_URL` / `DEEPSEEK_MODEL` / `VOICE_DEBUG_SAVE=1`(保存原始录音到 `upload/voice-debug/`)。

### 6.6 摆放图 / 独立点数

**两种场景**:
- **订单摆放图**:`placement_marks` / `loading_placement_marks`,挂在 shipping_records.id 下,移动端 + PC 端共用后端
- **独立点数**:`point_count_*` 3 张表,完全独立于订单,临时开会话拍照点数 → 导出 CSV

**11 个 placement 端点**(`shipping.py` 下,2026-08 上线):

| 端点 | 方法 | 用途 |
|------|------|------|
| `/api/v1/shipping-orders/records/<id>/placement-images` | POST / GET | 上传 / 列出 |
| `/api/v1/shipping-orders/placement-images/<id>` | GET / DELETE | 单图详情 / 删除 |
| `/api/v1/shipping-orders/placement-images/<id>/detect` | POST | 自动检测散落点 |
| `/api/v1/shipping-orders/placement-images/<id>/mark-scale` | POST | 调整标记点大小 |
| `/api/v1/shipping-orders/placement-images/<id>/loose-count` | POST | 录入散码数 |
| `/api/v1/shipping-orders/placement-images/<id>/manual-count` | POST | 覆盖手动支数 |
| `/api/v1/shipping-orders/placement-images/<id>/unload` | POST | 标记卸货(取负) |
| `/api/v1/shipping-orders/placement-images/<id>/marks` | POST | 追加点击标记 |
| `/api/v1/shipping-orders/placement-images/<id>/marks/last` | DELETE | 撤销最后标记 |

**点数三段对比**(详情页):

```
支合计 = Σ(manual_count if not None else n_marks)
散码合计 = Σ(loose_count)
备注期望 = regex "(\d+)\s*支" / "(\d+)\s*[yY]"
```

2026-08-19 修复:`manual_count` 优先于 `n_marks`,避免"点数弹框里输入 X 支,但订单页按 n_marks 对不上备注"的割裂。

### 6.7 OCR 事件审计

`/audit/ocr-events` 主页 + 3 个 API:

| API | 用途 |
|-----|------|
| `GET /api/v1/audit/ocr-events/aggregate` | 按 `prompt_version` 聚合(每版命中 green/yellow/red 数) |
| `GET /api/v1/audit/ocr-events/records` | 钻入某版本的 record↔image 配对明细 |
| `GET /api/v1/audit/ocr-events/export.csv` | 导出 CSV(带 `=+-@` 公式注入防护) |

后端走 `models/audit_query.py` 的 `OcrEventAudit`(`prompt_stats` / `record_pairs` / `export_rows`)。

### 6.8 人员/车辆 + 登录

**登录闸门**:`_require_login` 保护除了 `/static` `/api/` `/m/` `/upload/` 之外的所有页面。`/api/*` 自行返回 401,由前端引导跳转。

**session 模型**:`session['operator_id']`(int, Staff.id),无密码。多人化时只加密码层,gate 不变。`inject_current_operator` 主动 `session.pop` 失效的 operator(防"什么都没了"假象)。

**人员档案**:6 种角色(司机/调度/搬运/打码/仓管/文员)+ 软删 + 启用。`staff.html` 支持 `?role=` 过滤 + `?show_disabled=1` 切换。

**车辆档案**:车牌唯一 + 软删 + 启用。`staff.vehicle_id` 是 FK,ON DELETE SET NULL(但软删不触发)。

---

## 7. 共享子页面(4 个 include 弹框)

| 文件 | 行数 | 引入位置 | 用途 |
|------|------|----------|------|
| `_image_upload_modal.html` | 418 | 三大订单页 | 订单级 + record 级图片上传(粘贴/选文件/旋转 90°,旋转后 Pillow 落盘再走 OCR) |
| `_smart_add_modal.html` | 995 | 三大订单页 | 智能添加明细:📷 AI 图片识别(Moonshot/PaddleOCR/DeepSeek 三引擎)+ 📝 文本输入(DeepSeek 结构化) |
| `_record_image_script.html` | 1,182 | 三大订单页 | 行级图片所有交互:匹配徽章列动态插入、异步 OCR+AI 比对、人工 ✓ 确认 / 👤 已确认、re-ocr/fuzzy/ai-judge/ocr-detail 按钮 |
| `_voice_input_modal.html` | 129 | 出货页 | 语音录入弹框(`/api/v1/voice/recognize` / `confirm`) |

**前端模块**:
- 共享 JS:`static/js/common.js`(149 行,工具函数)
- 出货页摆放图:`static/js/placement_count.js`(529 行)
- 独立点数:`static/js/point-count.js`(313 行) + `static/js/point-count-list.js`
- 语音:`static/js/voice_input.js`(282 行)
- 移动端:`static/js/mobile_blur.js` / `mobile_detail.js` / `mobile_inbound_detail.js` / `mobile_loading_detail.js` / `mobile_placement.js`(479 行)

---

## 8. 启动 & 配置

```bash
# 安装依赖
pip install -r requirements.txt

# 启动开发服务器(debug 模式,host 0.0.0.0,本机 + 局域网)
python app.py
# 访问 http://127.0.0.1:5050
# 局域网访问 http://192.168.1.149:5050(需先放行防火墙)

# 运行测试
python -m pytest tests/ -v
```

### 8.1 环境变量(`.env`)

| 变量 | 必填 | 默认 | 用途 |
|------|------|------|------|
| `SECRET_KEY` | 否 | `worklog-dev-secret-change-me` | Flask session 签名 |
| `OCR_BACKEND` | 否 | `moonshot` | 整单 AI 识别默认引擎 |
| `MOONSHOT_API_KEY` | 是(出货) | — | Moonshot Kimi k2.6 |
| `DEEPSEEK_API_KEY` | 是(行级图/语音) | — | DeepSeek v4 Flash |
| `DEEPSEEK_BASE_URL` | 否 | `https://api.deepseek.com` | OpenAI 兼容基址 |
| `DEEPSEEK_MODEL` | 否 | `deepseek-chat` | 模型名 |
| `BAIDU_API_KEY` | 是(语音) | — | 百度短语音识别 |
| `BAIDU_SECRET_KEY` | 是(语音) | — | 同上 |
| `FFMPEG_PATH` | 否 | `ffmpeg` | 语音 webm → pcm 转码 |
| `VOICE_DEBUG_SAVE` | 否 | `0` | `1` 时保留原始录音到 `upload/voice-debug/` |

### 8.2 数据库

- 路径:`worklog.db`(项目根)
- 模式:WAL(允许并发读)
- 初始化:`app.py.create_app()` 调用 `init_db()`,在 `init_logging()` 之后(建表失败也能落盘)
- 上传:`upload/YYYY-MM/` 按月分目录(由 `blueprints/_helpers.get_upload_dir()` 集中管理)
- 通知图片走 `static/notice/`,不走 upload
- 大小限制:`app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024`(单次请求含图片),413 友好返回

### 8.3 Windows 启动脚本

- `start_server.bat` — 双击启动
- `setup_startup.ps1` — 开机自启

---

## 9. 踩坑点 & 经验教训

按"事故类型"分组,新增 2026-08 模块的踩坑点,删去 HTML form 路由相关的旧坑。

### 9.1 异步与并发

- **后端 OCR 跑在后台线程**:`threading.Thread` 不会自动传 `contextvars`,必须显式 `contextvars.copy_context().run(...)` 把当前请求的 `trace_id` + 业务上下文带进后台线程,否则 OCR 日志全显示 `-`(`shipping.py._spawn_record_image_processing`)。
- **绑定方法不能直接传给 `ctx.run`**:`ctx.run(callable, *args)` 会把 args 全传给 callable,绑定方法再传 self 会得到 6 个参数。必须包一层 lambda/wrapper。
- **PaddleOCR + DeepSeek 不严格线程安全**:`ocr_pipeline._OCR_LOCK` 串行化"抽字→比对",但写库(image + event)在锁外,缩短临界区。
- **`_ASYNC_JOBS` 进程内存**:`{image_id: {state, started, finished, deepseek_failed}}`,单用户本地部署足够,无需 Redis/Celery。前端 `/match-status` 端点轮询。

### 9.2 状态机与权限

- **`can(op, action, t)` 集中校验**:`Action` 枚举 13 种,`models/_permissions.py` 一处改全局生效。任务流所有推进都走这条路径,不要在视图函数里再写 `if role == '司机'`。
- **GATE_TO_ACTION 按 `to_status` 取**:按 `from_status` 取会让"已装货→已点数"只允许司机(LOAD),文员无法推进。语义应该是"进入该状态需要的动作" → 按 to_status 取。
- **事务保护建单**:`create_task` 的 `TaskDB.create + TaskItemDB.create + TaskEventDB.create` 全部 `commit=False`,最后由端点统一 `commit`,失败 `rollback`,不留孤儿 task(`task_flow.create_task`)。

### 9.3 数据与序列化

- **空字符串撞 UNIQUE 约束**:`source_order_no` 等字段空串入库会撞 UNIQUE。`task_flow._clean()` 统一把 `''` 转 `None`。
- **bool('false') 是 True**:JSON 布尔序列化成字符串时,`bool('0')` / `bool('false')` 都是 True。`_as_bool()` 显式识别字符串假值(`false` / `0` / `no` / `off` / `''`)。
- **CSV 公式注入**:`_sanitize_csv_cell()` 对 `=+-@\t\r\n` 开头强制加 `'` 转文本,防 Excel 把外部文本当公式执行(审计导出用)。
- **中文数字正则**:`(?<![\d.])(\d+(?:\.\d+)?)\s*[yY]` 负向后顾,排除小数点后的 y,避免把 "34.5y" 误识别为"散码=5"。

### 9.4 PaddleOCR / 图像预处理

- **Windows DLL 修复**:`torch` 的 `shm.dll` 需 `os.add_dll_directory(torch/lib)`,必须在 `import torch` 之前(由 PaddleOCR 间接 import),否则 DLL 缺失。
- **旋转先于 validate**:`apply_user_rotation()` 必须在 `validate_image_content()` 之前先旋转,否则宽高识别错(移动端上传)。
- **OCR 缓存读**:`fuzzy-match` / `ai-judge` 端点优先从 `ocr_match_event` 表取最近一次 `record_ocr` 的 `ocr_text`,命中则不重跑 PaddleOCR。
- **糊图自动标记**:`avg_conf < 0.5` 时在 `image.reason` 末尾追加 `[图像可能模糊，建议重拍]`,前端 hover 提示用户重拍(幂等,marker 已存在不重复追加)。
- **`KIND_FORM_NOLINES` 自动走 `_wrinkle_ocr`**:磅布三文治等无表格线表单,`_wrinkle_ocr` 加载失败时 `_ensure_model` try/except 回退到 `_ocr`。

### 9.5 登录 / Session / ContextVar

- **登录闸门要先于 context_processor 清理 session**:必须在 `before_request` 里 redirect,不能只在 `inject_current_operator` 清掉,否则请求已通过闸门,页面渲染但 `current_operator=None`,看起来"什么都没了"。
- **每个请求结束清业务上下文 + trace_id**:`_reset_log_context` after_request 兜底,Flask 默认不做 contextvars 隔离,得显式清,否则下一次请求(或下一次测试)看到的业务上下文 / trace_id 就是上一次的值。
- **`/m/*` 走白名单免登录**:微信点链接直接用,但意味着 `/m/*` 端点本身不能假设有 session(`current_operator` 可能是 None)。

### 9.6 模板与前端

- **Jinja2 `dict.get`**:复杂结构用 `record.get('product_name', '')` 防御 KeyError,不要假设字段一定有值。
- **出货页初始化调用 `bindReOcrButtons()`**:`_record_image_script.html` 里的「重 OCR」按钮必须在每次刷新后重新绑定事件。
- **三套订单共用 `_record_image_script.html`**:出货/入库/装柜同一个 include 弹框,改动一处全跟随;但要保证 `image_model` 注入的接口一致(`get_by_id` / `set_match` / `set_bg_color`)。

### 9.7 通用 Windows 5.1 提示

- **CRLF**:仓库内已统一 CRLF,跨工具协作避免 `sed -i` 之类改行尾。
- **GBK 终端**:日志输出避免在 stdout 写非 ASCII 字符(中文 flash 走 `flash()`,不 print)。
- **ffmpeg 路径**:`FFMPEG_PATH` 默认 `ffmpeg`,Windows 装好后用 `where ffmpeg` 查路径,不建议改默认。

---

## 10. 已知待办 / 改进方向

> 仅列当前最重要、有明确技术债方向的;不是完整 backlog。

1. **任务流 M2**:打码抢单池 `coding-claim` / `coding-done` / `coding-release` 当前 501 占位;M1 schema + 状态机 + UI 已就位,等真实使用再补。
2. **入站 / 装柜 YPP 核查页**:当前只有 `shipping_ypp_review.html`(2026-08-22 上线,扫描全表),入库/装柜尚未镜像。
3. **`product_units_new` 迁移残留清理**:`_init.py` 里有这张表的 CREATE 残留(已不可访问),正式清理需写迁移脚本比对 `product_units` 后删除。
4. **装柜自适应提示词未启用**:`loading_processor` 强制 `with_supplement=False`,后续看 YPP 准确率再决定。
5. **`vehicle_id` 软删级联**:staff 软删不会触发 FK SET NULL(行还在),但 staff 已被指派给某 task 时,task.driver_id / operator_id 引用要硬防御。建议增加"指派时校验 staff.is_active"。
6. **`worklog.db` 自动备份**:目前备份靠 `_safe-snapshot/` 手动 + `setup_startup.ps1` 调度,缺一个"启动前自动 dump 昨日 db"机制。
7. **多人化(阶段 4)**:session 模型为单用户假设,加密码层 + bcrypt + session token + 权限升级是必经路径,但 gate 不动。
8. **语音短语映射**:`voice_phrase_mapping` 表已建但 2026-08-23 仍 0 行(表无数据),实际靠 RapidFuzz 兜底;等用户多次"确认候选"后才会累积。

---

## 11. 文档约定

- **CLAUDE.md**(项目根)给 AI 代理读的项目指南,内容已部分滞后(蓝图/路由/表数滞后于本文)
- **README.md**(本文)给人类看的总览
- **`_safe-snapshot/`** 重要变更前的备份(db / diff / README 旧版本),不放 git
- **`tools/`** 一次性工具脚本(extract_ocr_fixture / inspect_overlay / split_commits),不进生产代码
- **`sql/new.sql`** 商品分类的 MySQL 源,`**new_sqlite.sql**` 是 SQLite 导入脚本
- **`tests/`** 81 个测试文件,涵盖 e2e / 单元 / OCR pipeline / 状态机 / 权限 / 移动端

---

## 附录 A:核心常量速查

```python
# OCR
OCR_MATCH_PROMPT_VERSION = 'compare_rows_v2'
KIND_FORM_NOLINES = 'form_nolines'   # 磅布三文治
KIND_GLARE = 'glare'                 # 无纺布
KIND_REDSTAMP = 'redstamp'           # 杂胶/纯胶

# 任务流
ALLOWED_TRANSITIONS = {准备中→[已装货], 已装货→[已点数], ...}
EVIDENCE_GATES = {(已装货,已点数):(装车照,), (已卸货,已完成):(签收单,), ...}
ALLOWED_STAGES = {识别原图, 装车照, 点数标签照, 点数整体照, 卸货照, 签收单, 打码照, 退单照, 退货照}

# 权限
Action = Enum(CREATE_TASK, EDIT_TASK, ASSIGN, CODING_CLAIM, CODING_DONE,
              CODING_RELEASE, LOAD, ARRIVE, UNLOAD, COUNT,
              DRIVER_RELEASE, TASK_CANCEL, RETURN_CREATE)
ROLE_OPTIONS = [司机, 调度, 搬运, 打码, 仓管, 文员]
```

## 附录 B:模型类速查(43 个类)

| 文件 | 类 | 用途 |
|------|------|------|
| `audit.py` | AuditLog | 操作审计 |
| `audit_query.py` | OcrEventAudit | OCR 事件只读查询 |
| `basic.py` | WorkLog / ErrorLog / TodoItem / VehicleMaintenance | 4 类基础记录 |
| `category_prompt.py` | CategoryPrompt + classify_record() | 自适应提示词 |
| `notice.py` | Notice / NoticeImage | 通知 |
| `orders.py` | ShippingOrder/Record/Image, InboundOrder/Record/Image, LoadingOrder/Record/Image, PlacementImage, LoadingPlacementImage, OcrMatchEvent, UnifiedSearch | 三大订单 9 模型 |
| `piece_conversion.py` | PieceConversion | 件数换算 |
| `point_count.py` | PointCountSession / PointCountImage | 独立点数 |
| `products.py` | ProductUnit / ProductCategory / Product | 商品 |
| `stock.py` | StockOutItem | 缺货 |
| `tasks_flow.py` | Staff / Task / TaskItem / TaskImage / TaskEvent + 5 DB 类 | 任务流 |
| `voice_mapping.py` | VoiceMapping | 口语映射 |
| `_permissions.py` | Action | 权限枚举 |

---

> 本文档以 2026-08-23 实际代码为准,数字由 grep + Python sqlite3 实际跑得;涉及"待办/未来"的部分单独标注。
