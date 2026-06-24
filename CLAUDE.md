丰源工作台项目指南
================

此文件为 Claude Code (claude.ai/code) 在本仓库中工作时提供指导。

## 项目概述

丰源工作台是一个基于 Flask 的工作日志与订单管理系统，用于记录工作经验、错误日志、待办事项、公司通知、商品资料，以及出入库/装柜订单的全流程管理（订单 + 明细 + 图片 + AI 识别）。

## 技术栈

- **后端**: Python 3.12 + Flask 3.1.3
- **数据库**: SQLite 3.45.3 (文件: `worklog.db`)
- **前端**: 原生 JavaScript + Tailwind CSS (样式内联于 `base.html`)
- **AI 集成**: Moonshot Kimi k2.6 Vision API 用于图片识别

## 开发命令

```bash
# 安装依赖
pip install -r requirements.txt

# 启动开发服务器
python app.py

# 服务器运行在 http://127.0.0.1:5050，已启用 debug 模式
```

## 目录结构

```
worklog-app/
├── app.py                  # 应用入口（85 行，工厂模式注册蓝图）
├── models/                 # 数据模型包(19 个类)
│   ├── __init__.py         # 统一 re-export,from models import X 保持不变
│   ├── _db.py              # get_db() + DB_PATH
│   ├── _init.py            # init_db() 建表 + 迁移
│   ├── basic.py            # WorkLog / ErrorLog / TodoItem / VehicleMaintenance
│   ├── notice.py           # Notice / NoticeImage
│   ├── orders.py           # 三套订单(出货/入库/装柜)共 9 个模型
│   ├── stock.py            # StockOutItem (原名 aux.py,已改名避 Windows 保留名)
│   ├── products.py         # ProductUnit / ProductCategory / Product
│   └── audit.py            # AuditLog
├── requirements.txt        # Python 包依赖（Flask、openai）
├── worklog.db              # SQLite 数据库
├── blueprints/             # 业务蓝图（8 个模块）
│   ├── __init__.py
│   ├── _helpers.py         # 共享：图片上传、单位匹配、支数换算、备注校验、汇总计算
│   ├── upload.py           # /upload/<path> 静态文件服务
│   ├── basic_records.py    # experience/errorlog/todolist/vehicle-maintenance
│   ├── info_pages.py       # 首页/价格板/通知彩色版/工作流/仓库/计数要点/换单要点/开单要点/当前缺货
│   ├── notice.py           # /notice + /api/v1/notices/*
│   ├── products.py         # /product-units /product-categories /products + /api/v1/products
│   ├── shipping.py         # /shipping-records + /api/v1/shipping-orders/* + AI 识别
│   ├── inbound.py          # /inbound-records + /api/v1/inbound-orders/*
│   └── loading.py          # /loading-orders + /api/v1/loading-orders/*
├── templates/              # 21 个 Jinja2 模板（均继承 base.html）
│   ├── base.html           # 公共布局 + 导航 + Tailwind 内联样式
│   ├── experience.html / errorlog.html / todolist.html
│   ├── notice.html / notice-color.html
│   ├── shipping-records.html / inbound-records.html / loading-orders.html
│   ├── vehicle-maintenance.html / stockout.html
│   ├── product-units.html / product-categories.html / products.html
│   └── workflow.html / warehouse.html / count-tips.html /
│     huandan-guide.html / billing-tips.html / priceboard.html /
│     worklog.html
├── static/                 # 静态资源
├── upload/YYYY-MM/         # 用户上传的图片，按月分组
├── sql/                    # SQL 脚本目录
│   ├── new.sql             # 商品分类数据（MySQL 语法，设计稿/标准源）
│   └── new_sqlite.sql      # 商品分类导入脚本（SQLite 语法，从 new.sql 自动生成）
├── start_server.bat        # Windows 启动脚本
└── setup_startup.ps1       # Windows 自启动 PowerShell 脚本
```

## 路由结构（99 个端点，按蓝图分布）

所有端点已拆分到 `blueprints/` 目录下的 8 个业务模块，主 `app.py` 只保留工厂、上下文、缓存、初始化代码。

### `blueprints/basic_records.py` — 基础记录（11 个）
- `/` 首页（其实在 `info_pages.py`）· `/experience` 工作经验 · `/errorlog` 错误经验 · `/todolist` 待办 · `/vehicle-maintenance` 车辆维护

### `blueprints/info_pages.py` — 信息展示 + stockout（11 个）
- `/` 首页 · `/priceboard` 码数报价 · `/notice-color` 通知彩色版 · `/workflow` 业务流程 · `/warehouse` 仓库布局 · `/count-tips` 点数要点 · `/huandan-guide` 换单要点 · `/billing-tips` 开单要点 · `/stockout` 当前缺货

### `blueprints/notice.py` — 通知（15 个）
- `/notice` CRUD · `/api/v1/notices` REST API · `/api/v1/notice/<id>/images` 图片管理 · `/api/v1/notice/<id>/img-cols` 列数设置

### `blueprints/shipping.py` — 出货（19 个端点）
- `GET /shipping-records` 列表页
- **HTML 接口**: 锁定/解锁、批量添加、删除订单
- **REST API（`/api/v1/shipping-orders/*`）**: 订单 CRUD、明细 CRUD、move、图片 CRUD、AI 识别

### `blueprints/inbound.py` — 入库（16 个端点）
- `GET /inbound-records` 列表页
- **HTML 接口**: add / add-item / edit / delete / delete-order / lock / batch-add / move-up / move-down / upload-image / delete-image
- **REST API（`/api/v1/inbound-orders/*`）**: 订单 CRUD、明细 CRUD、move、图片 CRUD

### `blueprints/loading.py` — 装柜（10 个端点）
- `GET /loading-orders` 列表页
- **REST API（`/api/v1/loading-orders/*`）**: 订单 CRUD、明细 CRUD、move、图片 CRUD

### `blueprints/products.py` — 商品管理（12 个）
- `/product-units` 商品单位（67 条数据）
- `/product-categories` 商品类型（使用 `product_categories` 表，186 条数据 = 1 根 + 9 大类 + 93 中类 + 83 细类，以 `sql/new.sql` 为最终标准源）
- `/products` 产品管理（`product` 表，0 条）
- `/api/v1/products` REST API（GET/POST/PUT/DELETE）

### `blueprints/upload.py` — 静态文件
- `/upload/<path>` 访问 upload 目录下的文件

### 主 `app.py` — 应用入口
- `create_app()` 工厂函数，注册所有蓝图
- `inject_notices` 全局上下文（注入 all_notices 到所有模板）
- `add_cache_control_headers` 禁用 HTML 缓存
- `init_db()` 数据库初始化

## 数据库（19 张表 + sqlite_sequence）

### 核心业务表

| 表 | 用途 | 行数 |
|---|---|---|
| `work_logs` | 工作经验 | 24 |
| `error_logs` | 错误经验 | 9 |
| `todo_items` | 待办事项 | 3 |
| `notices` + `notice_images` | 公司通知 | 23 + 2 |
| `vehicle_maintenance` | 车辆维护 | 1 |
| `stock_out_items` | 缺货登记 | 1 |

### 订单三表结构

每种订单都遵循 **订单 → 记录/明细 → 图片** 三表模式：

**出货** (`shipping_orders` / `shipping_records` / `shipping_images`)
- 131 个订单 / 409 条记录 / 474 张图片

**入库** (`inbound_orders` / `inbound_records` / `inbound_images`)
- 55 个订单 / 218 条记录 / 171 张图片

**装柜** (`loading_orders` / `loading_order_records` / `loading_order_images`)
- 11 个订单 / 44 条记录 / 104 张图片

通用字段：
- `is_locked` (0/1) — 锁定订单防止修改
- 订单级字段：`date` / `customer` / `order_num`（出货和装柜）
- 入库订单只有 `date` 和 `is_locked`
- 明细字段：`product_name` / `specification` / `quantity` / `unit` / `remark`
- 图片存储在 `upload/` 文件夹，通过相对路径引用

### 商品资料

| 表 | 用途 | 行数 | 模型类 |
|---|---|---|---|
| `product_units` | 商品单位/规格 | 67 | `ProductUnit` |
| `product_categories` | 商品分类（带层级/编码） | 186 | `ProductCategory` |
| `product` | 产品（带价格/库存/条形码） | 0 | `Product` |

> 表命名已统一：分类表使用复数 `product_categories`（语义"分类的集合"）。
> 
> 旧版 `product_category`（单数）已通过 `init_db()` 自动迁移到新表后 DROP，迁移是幂等的（启动时检测到旧表存在就迁，检测不到就跳过）。

**ProductCategory 表 schema：**
```sql
CREATE TABLE product_categories (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT,                            -- 旧 schema 残留，允许 NULL
    category_code   TEXT NOT NULL,
    category_name   TEXT NOT NULL,
    parent_id       INTEGER DEFAULT NULL,
    level           INTEGER NOT NULL DEFAULT 1,
    sort_order      INTEGER DEFAULT 0,
    status          INTEGER DEFAULT 1,
    created_at      TEXT DEFAULT (datetime('now','localtime')),
    updated_at      TEXT DEFAULT (datetime('now','localtime')),
    FOREIGN KEY (parent_id) REFERENCES product_categories(id) ON DELETE CASCADE
);
```

**模型方法形参**：使用 `cat_id`（不是 `category_id`），避免与 `Product.get_by_category(category_id)` 中的 `product.category_id` 字段混淆。

## 数据库层（models/ 包）

- `get_db()` — 返回启用了 WAL 模式的 SQLite 连接（`timeout=30, check_same_thread=False`）
- `init_db()` — 创建所有表（如不存在）
- 19 个模型类（纯静态方法，无 ORM）：`WorkLog` / `ErrorLog` / `TodoItem` / `Notice` / `NoticeImage` / `VehicleMaintenance` / `ShippingOrder` / `ShippingRecord` / `ShippingImage` / `InboundOrder` / `InboundRecord` / `InboundImage` / `LoadingOrder` / `LoadingOrderRecord` / `LoadingOrderImage` / `StockOutItem` / `ProductUnit` / `ProductCategory` / `Product`

## 关键实现细节

### 1. 图片上传流程
- 支持文件上传和 Ctrl+V 粘贴（base64）
- 存储路径为 `upload/YYYY-MM/<uuid>.<ext>`
- 数据库存绝对路径，模板通过 `/upload/<path>` 路由转换
- 上传端点：
  - HTML 风格：`/inbound-records/upload-image/<id>`
  - REST 风格：`/api/v1/{shipping|inbound|loading}-orders/<id>/images`

### 2. 订单锁定
- 每个订单有 `is_locked` 字段
- 锁定后隐藏添加明细表单，禁用删除按钮
- 锁定切换使用 POST + 表单提交（非 fetch），确保有确认弹窗和页面刷新

### 3. 路由顺序（关键！）
Flask 按定义顺序匹配路由。具体路由如 `/loading-orders/delete/<int:order_id>` 必须定义在通配路由如 `/loading-orders/<int:record_id>` 之前。

### 4. 表单处理
- 旧功能（经验/错误/待办/通知/维护）使用传统 POST + redirect + `flash()`
- 三大订单的 HTML 接口保留这种风格
- 三大订单的 REST API 使用 fetch + JSON（无刷新）
- 商品管理用 REST API + 前端 JS

### 5. 缓存控制
`app.py` 中已禁用开发缓存：
```python
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0
# 所有 HTML 响应添加 Cache-Control 头
```

### 6. AI 识别（出货）
`/api/v1/shipping-orders/ai-recognize` 接收图片，调用 Moonshot Kimi k2.6 Vision，提取商品信息后返回 JSON。

**踩坑**：Moonshot kimi-k2.6 模型**锁死 temperature 必须为 1**。调到 0.2 等其他值会被服务端 400 拒绝（`invalid temperature: only 1 is allowed for this model`）。其他可调参数：`max_tokens` 已设到 4000 防大单据截断；prompt 抽到模块顶部 `AI_RECOGNIZE_PROMPT` 常量。

### 7. 辅助单位提示与备注校验（支数换算）

每个明细行显示"辅助单位提示"列，通过 YPP（Yards Per Piece，码/支）将码数换算为支数。三层逻辑（Python 后端 + JS 前端一致）：

1. **unit='支'**：数量本身就是支数，直接显示 `X支`（无需 YPP 配置）
2. **unit='y'/'码' 且有 YPP 配置**：`qty / ypp` 换算出支数，显示 `X支+Y码`
3. **无 YPP 配置**：从备注中提取 `X支` 作为兜底

**YPP 匹配**（`_match_unit_in_cache` / `findUnit` / `findInboundUnit` / `findLoadingUnit`）：
- 第一轮：找 `product_name` 匹配且 `spec_keyword` 在规格中出现的行（精确匹配）
- 第二轮：兜底取没有 `spec_keyword` 的默认行
- 避免默认行抢在精确匹配之前返回

**备注校验**（`check_remark` / `checkMismatch`）：
- 解析备注中的 `X支*Yy`（乘法语义）和 `X支+Yy`（加法语义）
- 期望值 = pieces × yards_per_piece + loose_yards，与 quantity 比较
- 不一致时：单支标粉色(info)，多支标红色(warn)

### 8. 备注汇总行（两列统计）

每个日期组底部有汇总行，两列：
- **📊 备注支数**：汇总备注中 `X支` 的支数 + 散码出现次数
- **📊 明细支数**：汇总辅助单位提示列中提取的支数（含 unit='支' 直接显示 + unit='y' 换算后的结果）

Python 端 `summarize_remarks()` 和 JS 端 `recalcXxxSummary()` 逻辑一致。

### 9. 单位归一化（码 → y）

**问题**：历史数据中 `inbound_records` 有 117 条 `unit='码'`，而 shipping/loading 已统一用 `unit='y'`。

**三层防御**（防止 '码' 再次写入）：
1. **模型层**：`InboundRecord.create` / `ShippingRecord.create` / `LoadingOrderRecord.create` 均自动归一化
2. **API 层**：三个蓝图的 update + batch-add 端点均检查 `unit == '码' → 'y'`
3. **前端 JS**：三个订单模板的编辑保存流程均归一化

**数据库修复**：`UPDATE inbound_records SET unit='y' WHERE unit='码'`（已先备份到 `_safe-snapshot/`）

## 模板设计约定

- 所有模板继承 `base.html`
- 导航分为 6 个分组（下拉菜单）：
  - **运营信息** — 公司通知、通知彩色版、业务流程、仓库布局
  - **当前缺货** — 单页
  - **商品管理** — 商品单位、商品类型、产品管理
  - **码数报价** — 单页
  - **操作要点** — 开单要点、换单要点、点数要点
  - **快速录入** — 工作经验、错误经验、待办事项、车辆维护记录
  - **订单操作**（单独） — 装柜订单、出货记录、入库记录
- 样式以 Tailwind CSS 为主，少量自定义 CSS 在 `base.html` 顶部 `<style>` 块
- JS 在每个模板底部用 `<script>` 内联实现交互

## Windows 环境注意事项

1. **CRLF 换行符**: 代码库使用 Windows CRLF 换行符。注意 SQL 查询中的多行字符串替换。

2. **PowerShell 解析**: 避免在 PowerShell 中使用带有特殊字符（`&`、`)`）的内联 Python。始终先将 Python 脚本写入文件，再执行。

3. **文件路径**: 数据库存储 Windows 风格路径（`upload\2026-05\file.jpg`）。模板通过 `get_relative_path()` 方法转换为 URL 风格。

4. **PowerShell 字符串**: 在 PowerShell 中执行内联 Python 时避免使用双引号字符串包裹含双引号的 SQL/JSON，会被解释器吃掉。优先用脚本文件。

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

### 添加商品类功能

参考 `/product-units` / `/product-categories` / `/products` 的实现（在 `blueprints/products.py`）：
- 模板用 Tailwind 表单
- 提交用 fetch + REST API
- 无锁定功能，编辑/删除走 POST 表单

## 重要文件参考

### 应用入口
- `app.py` — 94 行(`create_app()` 工厂 + 缓存控制 + `inject_notices` 上下文 + 蓝图注册)
- `app.py:30-85` — `create_app()` 工厂
- `app.py:67-83` — 蓝图导入与注册

### 蓝图
- `blueprints/_helpers.py` — 共享函数：`get_upload_dir` / `get_ypp` / `calc_hint` / `check_remark` / `summarize_remarks`（~270 行）
- `blueprints/upload.py` — 静态文件服务（15 行）
- `blueprints/basic_records.py` — 基础记录（116 行）
- `blueprints/info_pages.py` — 信息展示页 + stockout（84 行）
- `blueprints/notice.py` — 公司通知 + REST（212 行）
- `blueprints/products.py` — 商品管理（197 行）
- `blueprints/shipping.py` — 出货 + REST + AI 识别（~440 行）
- `blueprints/inbound.py` — 入库 + REST（~505 行，最大）
- `blueprints/loading.py` — 装柜 + REST（~270 行）

### 数据库层
- `models/orders.py` — ShippingOrder / ShippingRecord / ShippingImage / InboundOrder / InboundRecord / InboundImage / LoadingOrder / LoadingOrderRecord / LoadingOrderImage(9 个模型,~1120 行)
- `models/products.py` — ProductUnit / ProductCategory / Product
- `models/basic.py` — WorkLog / ErrorLog / TodoItem / VehicleMaintenance
- `models/notice.py` / `models/stock.py` / `models/audit.py` — Notice + 缺货 + 审计

### 模板
- `templates/base.html:515-562` — 导航菜单

## 待办 / 待清理

- [x] ~~统一 `product` / `product_category` 命名~~ — **已完成**：旧 `product_category` 表 154 行已迁移到 `product_categories`（新 schema），所有模型/蓝图/模板引用已更新
- [x] ~~图片上传后局部刷新（替代 `location.reload()`）~~ — **已完成**：三套订单均已实现 fetch + DOM 局部更新，E2E 验证通过
- [x] ~~`app.py` 按业务拆分蓝图（Blueprint）~~ — **已完成**：从 1861 行单文件 → `app.py` 85 行 + `blueprints/` 下 8 个模块（最大 494 行）
- [x] ~~`shipping-records.html` 删除 `deleteShippingImage(imageId, index)` 死代码~~ — **已删除**
- [x] ~~单位归一化 `码 → y`~~ — **已完成**：DB 117 条已修复，模型+API+前端三层防御已就位（2026-06-24）
- [x] ~~辅助单位提示升级：unit='支' 直接显示 + 无 YPP 时从备注提取支数~~ — **已完成**：Python `calc_hint` + JS `getXxxHint` 均已实现三层逻辑（2026-06-24）
- [x] ~~备注汇总行拆分为「备注支数」+「明细支数」两列~~ — **已完成**：Python `summarize_remarks` + JS `recalcXxxSummary` 均已更新（2026-06-24）
- [x] ~~商品分类树按 category_code 排序~~ — **已完成**：`ProductCategory.get_tree()` 递归排序（2026-06-24）

## 长期规划（2026-06-08 拍板）

**核心方向**：把出货/入库/装柜从「事后记账系统」升级为「送货流程的实时管控 + 证据链」。

### 业务模式参考：美团骑手模式
- 每单都经历"接单 → 提货 → 装车 → 出发 → 到达 → 签收 → 异常上报"多个状态点
- 每个状态点对应**一次拍照 + 一次状态推进**，形成可追溯证据链
- 系统侧实时知道货在哪里、谁在送、是否异常

### 目标用户
- **当前**：用户本人（业务 + 工具维护都做）
- **未来**：扩展到司机（现场拍照+点数+异常上报）+ 仓管（扫码复核）+ 客户（签收确认）

### 演进路线（**用户已定优先级：先商品库，后司机端**）

#### 阶段 1：商品库完善（**当前在做**）
- `product` 表目前 0 行，需要录入：品名、规格、单位、箱规、条码、缩略图、典型包装图（AI 识别锚点）
- 预期 **1000+ SKU**，需要分批录入工具（批量导入、批量匹配、相似 SKU 合并）
- 配套：`product_units`（每支多少码）从 67 条扩到接近 1:1
- 配套：`product_categories` 186 条已就位（1+9+93+83 = 186，以 `sql/new.sql` 为最终标准源），但可能需要重新组织分类树

#### 阶段 2：AI 验数 + 校对
- 出货/入库/装柜时拍单据图，AI 自动识别商品 + 数量 + 单位
- 和人工录入的明细做 mismatch 警告（已经做了一半：辅助单位提示 + 备注 mismatch 红/粉行）
- 重点 SKU 提前建好"标准包装图"，AI 拿来做参考识别

#### 阶段 3：司机端 / 移动端
- 形态待定（Web PWA / 小程序 / 原生 App 取决于设备情况——用户尚未明确）
- 核心功能：接单、扫码提货、装车拍照、点数辅助、签收、异常上报
- 离线/弱网支持（送货路上信号差是常态）

#### 阶段 4：多人化 + 权限
- 单人 → 多司机的过渡（**不要立刻上重型多用户架构**，避免过度设计）
- 司机身份：单独账号 vs 司机码/链接（无登录），技术差别大
- 数据隔离 + 操作日志 + 简单权限（仓管/司机/老板）

### 已落地的"司机端预备工作"
- [x] `loading-orders.html` 加「辅助单位提示」列（虚拟字段，从 `product_units` 计算支数+散码）
- [x] `loading-orders.html` 加 `mismatch` 红/粉行提示（备注支数+散码 vs 实际数量不一致时告警）
- [x] `shipping-records.html` / `inbound-records.html` 已有相同机制

### 给建议时的原则
- 单人能放宽的（无认证、外网风险）也要为多人用做铺垫，但**不要立刻上**重型的多用户架构
- 业务便利性（效率/防错）和工程性（架构/复用/可扩展）两手都兼顾
- 端起来（落地可用）优先于完美设计——快速出能跑的小工具，远胜长期空想

## 图片上传流程（局部刷新已实现）

三套订单（出货 / 入库 / 装柜）均已实现**完整局部刷新**，无整页 reload。

**API 端点**：
- `POST /api/v1/{shipping|inbound|loading}-orders/<order_id>/images` — 上传（multipart/form-data, field=`image`）
- `DELETE /api/v1/{...}-orders/images/<image_id>` — 删除

**响应 JSON**（统一格式）：
```json
{
  "success": true,
  "image": "2026-06/loading_31_xxx.png",   // 相对路径
  "image_id": 116
}
```

**前端拼 URL**：`/upload/` + `data.image`

**DOM 局部更新模式**（每个页面相同）：
1. 上传成功 → `dateGroup.querySelector('.img-area')` 找容器，没有则创建
2. `createElement` + `innerHTML` 构造新 `.img-item`，append 到容器
3. 更新 `.img-count` 文本
4. 删除成功 → `closest('.img-item').remove()`，更新计数；图片为 0 时移除整个 `image-section`

**fallback 行为**：4 处 `location.reload()` 是兜底（DOM 节点找不到时），正常路径下不会触发。

---

## 项目背景

- **当前用户**：单人使用；同时承担外贸装柜/仓库管理业务 + 维护本工具（用户自述，2026-06-08）
- **业务目标**：确保各环节装货正确、点数正确
- **未来规划**：从「事后记账」升级为「美团骑手式送货流程实时管控 + 证据链」，会扩展到多人使用
- **演进路线（用户已定优先级）**：先商品库录入（1000+ SKU）→ AI 验数 → 司机端/移动端 → 多人化 + 权限
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



## 数据库备份约定（2026-06-10 起）

**专用备份目录**：`D:\BAK\`

**命名规则**：`worklog_YYYYMMDD_HHmm.db`（24 小时制，本地时间）

**典型命令**：
```powershell
Copy-Item "C:\Users\Administrator\worklog-app\worklog.db" "D:\BAK\worklog_$(Get-Date -Format 'yyyyMMdd_HHmm').db" -Force
```

**触发时机**：
-补录完一批订单/通知/分类后（用户明确要求时）
-任何 db破坏性操作前（见"踩坑点6"）
-每次大改 schema /跑迁移脚本前

**保留策略**：D盘容量充足，**不主动清理**——保留历史快照作为时间机器；用户主动说"清理旧的"才动手（必须用 `mavis-trash`，不许 `Remove-Item -Force`）。项目目录下的临时备份（`worklog0609.db`之类）保留至少1份作近期参考即可，不需要全量同步到 D盘。
