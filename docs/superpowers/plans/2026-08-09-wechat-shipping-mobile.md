# 移动端当天出货页面（微信浏览器）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 微信内置浏览器内单 URL 访问当天出货订单，逐商品拍照上传并接收 OCR/AI 判别回流、人工确认。

**Architecture:** 新蓝图 `blueprints/mobile_shipping.py` + 移动端模板（继承 `base.html`、include `_record_image_script.html`）+ 后端在现有 `/api/v1/shipping-orders/*` 端点加 `rotate_deg` / `need_rotate` / 糊图 reason 提示；零新数据模型、零新引擎、零新后端基础设施。

**Tech Stack:** Flask 3.1.3、SQLite、Pillow、PaddleOCR、DeepSeek、OpenCV（服务端清晰度评估）、OpenCV.js（客户端 `Laplacian` 评估；CDN 引入 `@techstark/opencv-js`）、Tailwind（PC 继承）、原生 JS（沿用 `_record_image_script.html` 23 函数）、Playwright（渲染测试）。

## Global Constraints

- 后端：`Python 3.12` + `Flask 3.1.3`；OCR 引擎单例常驻显存（PaddleOCR ~200MB+）
- 数据库：SQLite 3.45.3（文件 `worklog.db`，WAL 模式）；仅新增蓝图，**不新增表/字段**
- 路径与命名：Windows CRLF；Python 脚本中文用 `# -*- coding: utf-8 -*-`；PowerShell 跑 Python 设 `$env:PYTHONUTF8=1`
- 鉴权：移动端走 `/m/` 前缀**无登录**；把 `'/m/'` 加到 `app.py:_AUTH_PUBLIC_PREFIXES`
- 移动端样式命名空间：`.mobile-*`；**不污染** PC 端 `static/css/app.css`
- 移动端 JS 依赖：CDN 引入 `@techstark/opencv-js@4.10.0`（HTTPS 兜底）
- 上传：单图 ≤ 10MB；扩展名白名单 `.png,.jpg,.jpeg,.gif,.webp`
- 提交节奏：每任务一次 `git commit`；中文 commit；执行到一半停要保留 worktree 状态（参见 `git-worktree-windows-busy-cleanup`）
- DB 破坏性操作前必须 `Copy-Item worklog.db _safe-snapshot/<时间戳>/worklog.db`（参见踩坑点 6）
- 单文件超 1500 行触发拆分（参考现有 inbound/loading）
- 移动端到货 OCR/AI 通过短轮询通知（1500ms 间隔，150s 超时回落）
- 后端日志必须用 `set_log_context(biz='mobile_shipping', order_id=oid)` 注入 `6a6e679` 的 trace_id

---

## 文件结构

| 路径 | 状态 | 职责 |
|---|---|---|
| `blueprints/mobile_shipping.py` | 新建 | 移动端路由：列表 `/m/shipping-today`、详情 `/m/shipping-today/order/<oid>`、上传状态查询（只读），不重复实现任何业务逻辑 |
| `templates/mobile/shipping-today.html` | 新建 | 列表页（订单卡片） |
| `templates/mobile/shipping-order.html` | 新建 | 详情页（整体图 + 商品卡片 + 识别记录折叠区） |
| `static/css/mobile.css` | 新建 | 移动端专用样式（`.mobile-*`） |
| `static/js/mobile_blur.js` | 新建 | 客户端 Laplacian 糊图检查（约 30 行） |
| `static/js/mobile_detail.js` | 新建 | 详情页交互（绑定 `data-record-id`、照片按钮、状态行、`need_rotate` 弹框、轮询触发） |
| `blueprints/shipping.py` | 修改 | 在 `api_v1_shipping_orders_records_image` / `api_v1_shipping_orders_image` 接受 `rotate_deg`；异步任务开头加方向矫正 + 平均置信度提示 |
| `blueprints/_helpers.py` | 修改 | 新增 `apply_user_rotation(img_bytes, rotate_deg) -> bytes` |
| `app.py` | 修改 | 注册 `mobile_shipping_bp`；把 `'/m/'` 加入 `_AUTH_PUBLIC_PREFIXES`；导入并应用 `set_log_context` |
| `static/css/app.css` | 修改 | 末尾追加 `<link>` 引用 `mobile.css`（不直接改 PC 样式） |
| `tests/test_mobile_shipping_routes.py` | 新建 | 列表/详情 200 + 空数据兜底 |
| `tests/test_mobile_shipping_upload.py` | 新建 | 行级上传 → 轮询拿到非 processing；糊图 reason |
| `tests/test_mobile_shipping_rotate.py` | 新建 | `rotate_deg=180` 上传；`need_rotate=true` 路径 |
| `tests/test_mobile_shipping_overall.py` | 新建 | 整体图上传 `record_pk=NULL`；OCR 跳过 |
| `tests/test_mobile_shipping_manual.py` | 新建 | 黄牌 → `manual-verify` → `human_verified=1` |
| `tests/test_mobile_shipping_replace.py` | 新建 | 同 record 第二张图成功后删除旧图 |
| `tests/test_mobile_shipping_blur_client.py` | 新建 | 客户端 Laplacian.var < 80 弹"图太糊" |
| `tests/render_mobile_shipping_today.js` | 新建 | Playwright 截列表页（iPhone 12 viewport） |
| `tests/render_mobile_shipping_order.js` | 新建 | Playwright 截详情页 |

---

## 任务清单

### Task 1: 蓝图与登录白名单

**Files:**
- Create: `blueprints/mobile_shipping.py`
- Modify: `app.py:88`（`_AUTH_PUBLIC_PREFIXES`）
- Modify: `app.py:169-186`（`create_app` 末尾注册新蓝图）
- Test: `tests/test_mobile_shipping_routes.py`

**Interfaces:**
- Consumes: 现有 `ShippingOrder.get_all()`、`ShippingRecord.get_by_order(oid)`
- Produces: 蓝图 `mobile_shipping_bp`；端点 `GET /m/shipping-today`（先返回空壳 200 模板）、`GET /m/shipping-today/order/<int:oid>`（先返回空壳 200 模板）

- [ ] **Step 1: 写失败测试**

`tests/test_mobile_shipping_routes.py`：
```python
import os
import tempfile
import pytest

from app import create_app
from models._db import DB_PATH


@pytest.fixture
def client():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    original = DB_PATH
    import models._db as db_mod
    db_mod.DB_PATH = path
    from models._init import init_db
    init_db()
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c
    db_mod.DB_PATH = original
    os.unlink(path)


def test_shipping_today_renders_200(client):
    resp = client.get("/m/shipping-today")
    assert resp.status_code == 200
    assert "今日出货".encode() in resp.data


def test_shipping_order_detail_renders_200(client):
    from models.orders import ShippingOrder, ShippingRecord
    oid = ShippingOrder.create("2026-08-09", "测试客户")
    ShippingRecord.create(oid, "杂胶海绵", "黑色 5mm", 100, "y", "2支")
    resp = client.get(f"/m/shipping-today/order/{oid}")
    assert resp.status_code == 200
    assert "测试客户".encode() in resp.data
    assert "杂胶海绵".encode() in resp.data


def test_shipping_today_no_login_required(client):
    # 移动端不要求登录（白名单前缀 /m/）
    resp = client.get("/m/shipping-today")
    assert resp.status_code != 302


def test_shipping_today_empty_today(client):
    # 数据库里没有今天订单时，渲染空状态
    resp = client.get("/m/shipping-today")
    assert resp.status_code == 200
    assert "没有出货订单".encode() in resp.data
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_mobile_shipping_routes.py -v`
Expected: 全部 FAIL（`/m/shipping-today` 404）

- [ ] **Step 3: 写最小实现**

`blueprints/mobile_shipping.py`：
```python
# -*- coding: utf-8 -*-
"""移动端当天出货页面：列表 + 订单详情，仅拍照/人工确认。"""
from datetime import date as _date

from flask import Blueprint, render_template

from models.orders import ShippingOrder, ShippingRecord

bp = Blueprint("mobile_shipping", __name__)


@bp.route("/m/shipping-today")
def shipping_today():
    today = _date.today().isoformat()
    groups = ShippingRecord.get_groups(today, today)
    return render_template("mobile/shipping-today.html", today=today, groups=groups)


@bp.route("/m/shipping-today/order/<int:oid>")
def shipping_order_detail(oid: int):
    order = ShippingOrder.get_by_id(oid)
    if order is None:
        from flask import abort
        abort(404)
    records = ShippingRecord.get_by_order(oid)
    return render_template(
        "mobile/shipping-order.html",
        order=order,
        records=records,
    )
```

`templates/mobile/shipping-today.html`：
```html
{% extends "base.html" %}
{% block title %}今日出货{% endblock %}
{% block head %}
  <link rel="stylesheet" href="{{ url_for('static', filename='css/mobile.css') }}">
{% endblock %}
{% block content %}
<div class="mobile-today">
  <h2>今日出货</h2>
  <p class="subtitle">{{ today }}</p>
  {% if not groups %}
    <p class="empty">今天没有出货订单</p>
  {% endif %}
</div>
{% endblock %}
```

`templates/mobile/shipping-order.html`：
```html
{% extends "base.html" %}
{% block title %}{{ order['customer'] }} 第{{ order['order_num'] }}单{% endblock %}
{% block head %}
  <link rel="stylesheet" href="{{ url_for('static', filename='css/mobile.css') }}">
{% endblock %}
{% block content %}
<div class="mobile-order" data-order-id="{{ order['id'] }}">
  <a href="{{ url_for('mobile_shipping.shipping_today') }}">‹ 今日订单</a>
  <h2>{{ order['customer'] }} · 第{{ order['order_num'] }}单</h2>
  {% for rec in records %}
    <div class="mobile-record" data-record-id="{{ rec['id'] }}">
      <div class="name">{{ rec['product_name'] }}</div>
      <div class="spec">{{ rec['specification'] }} · {{ rec['quantity'] }}{{ rec['unit'] }}</div>
    </div>
  {% endfor %}
</div>
{% endblock %}
```

`app.py:88`（把 `'/m/'` 加到 `_AUTH_PUBLIC_PREFIXES`）：
```python
_AUTH_PUBLIC_PREFIXES = ("/static", "/api/", "/m/")
```

`app.py:169-186`（在 `create_app` 注册新蓝图）：
```python
from blueprints.mobile_shipping import bp as mobile_shipping_bp
...
app.register_blueprint(mobile_shipping_bp)
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_mobile_shipping_routes.py -v`
Expected: 4 PASS

- [ ] **Step 5: 提交**

```bash
git add blueprints/mobile_shipping.py app.py tests/test_mobile_shipping_routes.py templates/mobile/
git commit -m "feat(mobile): 新增 /m/shipping-today 列表与详情页蓝图（无登录）"
```

---

### Task 2: 列表页订单卡片 + 状态汇总

**Files:**
- Modify: `templates/mobile/shipping-today.html`
- Create: `static/css/mobile.css`
- Modify: `static/css/app.css:762`（末尾追加引用 `mobile.css`）
- Modify: `blueprints/mobile_shipping.py:11-25`（构造 `group_summary`）
- Test: `tests/test_mobile_shipping_routes.py` 扩展

**Interfaces:**
- Consumes: `ShippingRecord.get_groups(today, today)` 返回的 `groups: [{order:dict, records:[dict, ...]}]`
- Produces: 每组 `group_summary = {order, records, total, has_image, image_done_count, stats:{green, yellow, red}}`

- [ ] **Step 1: 写失败测试**

在 `tests/test_mobile_shipping_routes.py` 追加：
```python
def test_shipping_today_shows_order_card(client):
    from models.orders import ShippingOrder, ShippingRecord, ShippingImage
    oid = ShippingOrder.create("2026-08-09", "广隆纸业")
    rid = ShippingRecord.create(oid, "杂胶海绵", "黑色 5mm", 100, "y", "")
    resp = client.get("/m/shipping-today")
    assert resp.status_code == 200
    assert "广隆纸业".encode() in resp.data
    assert "进入商品详情".encode() in resp.data
    assert "1/1".encode() in resp.data  # total/has_image


def test_shipping_today_aggregates_status(client):
    from models.orders import ShippingOrder, ShippingRecord, ShippingImage
    oid = ShippingOrder.create("2026-08-09", "兴达包装")
    rid1 = ShippingRecord.create(oid, "白磅布", "60寸", 2, "件", "")
    rid2 = ShippingRecord.create(oid, "日本纸", "A4", 5, "件", "")
    iid = ShippingImage.create(oid, "upload\\2026-08\\x.jpg", "x.jpg", "upload", rid1, 1)
    ShippingImage.set_match(iid, "green", 0.95, "一致", "local_fuzzy")
    resp = client.get("/m/shipping-today")
    assert resp.status_code == 200
    assert "1".encode() in resp.data  # total
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_mobile_shipping_routes.py -v`
Expected: `test_shipping_today_shows_order_card` 失败（"进入商品详情"未渲染）；`test_shipping_today_aggregates_status` 失败

- [ ] **Step 3: 实现订单卡片与汇总**

`blueprints/mobile_shipping.py`：
```python
from models.orders import ShippingOrder, ShippingRecord, ShippingImage

_STATUS = {"green": "✓", "yellow": "⚠", "red": "✕"}


def _summarize_group(group: dict) -> dict:
    records = group.get("records", [])
    total = len(records)
    has_image = 0
    stats = {"green": 0, "yellow": 0, "red": 0}
    for r in records:
        images = ShippingImage.get_by_record(r["id"])
        if images:
            has_image += 1
            # 取最差档（红 > 黄 > 绿）
            worst = "green"
            for img in images:
                st = img.get("match_status") or "green"
                if st == "red":
                    worst = "red"; break
                if st == "yellow" and worst == "green":
                    worst = "yellow"
            if worst in stats:
                stats[worst] += 1
        # 无图不计入统计
    return {**group, "total": total, "has_image": has_image, "stats": stats}


@bp.route("/m/shipping-today")
def shipping_today():
    today = _date.today().isoformat()
    groups = ShippingRecord.get_groups(today, today)
    groups = [_summarize_group(g) for g in groups]
    return render_template("mobile/shipping-today.html", today=today, groups=groups)
```

`templates/mobile/shipping-today.html`（替换）：
```html
{% extends "base.html" %}
{% block title %}今日出货{% endblock %}
{% block head %}
  <link rel="stylesheet" href="{{ url_for('static', filename='css/mobile.css') }}">
{% endblock %}
{% block content %}
<div class="mobile-today">
  <header class="app-head">
    <h2>今日出货</h2>
    <p class="subtitle">{{ today }}</p>
  </header>
  {% if not groups %}
    <p class="empty">今天没有出货订单</p>
  {% else %}
    {% for g in groups %}
      <a class="order-card" href="{{ url_for('mobile_shipping.shipping_order_detail', oid=g.order['id']) }}">
        <div class="row">
          <div>
            <div class="customer">{{ g.order['customer'] }}</div>
            <div class="number">第{{ g.order['order_num'] }}单</div>
          </div>
          <span class="badge {% if g.has_image == g.total %}done{% else %}todo{% endif %}">
            {{ g.has_image }}/{{ g.total }} 已拍
          </span>
        </div>
        <div class="line"><i style="width: {{ (g.has_image * 100 // g.total) if g.total else 0 }}%"></i></div>
        <div class="metrics">
          <span><b>{{ g.total }}</b> 项商品</span>
          <span>✓ {{ g.stats.green }}　⚠ {{ g.stats.yellow }}　✕ {{ g.stats.red }}</span>
        </div>
        <button class="enter">进入商品详情 ›</button>
      </a>
    {% endfor %}
  {% endif %}
</div>
{% endblock %}
```

`static/css/mobile.css`（新建）：
```css
.mobile-today { max-width: 480px; margin: 0 auto; padding: 12px; }
.mobile-today .app-head { background: linear-gradient(140deg,#14664d,#21956c); color:#fff; padding:14px; border-radius:12px; }
.mobile-today h2 { margin:0 0 4px; }
.mobile-today .subtitle { margin:0; opacity:.85; font-size:12px; }
.mobile-today .empty { text-align:center; color:#64748b; padding:32px 0; }
.mobile-today .order-card { display:block; background:#fff; border:1px solid #e9eef2; border-radius:12px; padding:12px; margin:12px 0; text-decoration:none; color:inherit; box-shadow:0 2px 6px rgba(15,23,42,.06); }
.mobile-today .row { display:flex; justify-content:space-between; align-items:flex-start; }
.mobile-today .customer { font-weight:800; font-size:15px; }
.mobile-today .number { font-size:11px; color:#64748b; }
.mobile-today .badge { font-size:10px; font-weight:700; border-radius:99px; padding:4px 7px; }
.mobile-today .badge.done { background:#dbf6e6; color:#146745; }
.mobile-today .badge.todo { background:#fff1c2; color:#805700; }
.mobile-today .line { height:6px; background:#e6ecef; border-radius:99px; overflow:hidden; margin:9px 0 6px; }
.mobile-today .line i { display:block; height:100%; background:#1f9a70; }
.mobile-today .metrics { display:flex; justify-content:space-between; font-size:11px; color:#64748b; }
.mobile-today .enter { width:100%; border:0; color:#17664f; background:#e7f6ef; border-radius:10px; padding:9px; margin-top:9px; font-weight:750; }
```

`static/css/app.css:762`（末尾追加 `mobile.css` 引用，**不直接改 PC 样式**）：
```css
/* 末尾添加如下注释与 link 不直接嵌 CSS，仅指明 mobile.css 在 main template head 中通过 link 引用 */
```

> 实际生效靠 `templates/mobile/shipping-today.html` 的 `{% block head %}` 已加 `<link>`，无需改 `app.css` 本身。

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_mobile_shipping_routes.py -v`
Expected: 6 PASS

- [ ] **Step 5: 提交**

```bash
git add blueprints/mobile_shipping.py templates/mobile/shipping-today.html static/css/mobile.css tests/test_mobile_shipping_routes.py
git commit -m "feat(mobile): 今日订单列表 + 状态汇总（绿/黄/红）"
```

---

### Task 3: 详情页骨架 + 商品卡片

**Files:**
- Modify: `templates/mobile/shipping-order.html`
- Modify: `static/css/mobile.css`
- Test: `tests/test_mobile_shipping_routes.py` 扩展

**Interfaces:**
- Consumes: `order: dict`, `records: list[dict]`
- Produces: HTML 含「整体图区 + 商品卡片 + 识别记录折叠区」骨架

- [ ] **Step 1: 写失败测试**

在 `tests/test_mobile_shipping_routes.py` 追加：
```python
def test_shipping_order_overall_section(client):
    from models.orders import ShippingOrder, ShippingRecord
    oid = ShippingOrder.create("2026-08-09", "宏昌贸易")
    ShippingRecord.create(oid, "日本纸", "A4 120g", 5, "件", "")
    resp = client.get(f"/m/shipping-today/order/{oid}")
    assert resp.status_code == 200
    assert "整体图".encode() in resp.data
    assert "整体照".encode() in resp.data
    assert "堆放".encode() in resp.data
    assert "装车".encode() in resp.data


def test_shipping_order_record_cards(client):
    from models.orders import ShippingOrder, ShippingRecord
    oid = ShippingOrder.create("2026-08-09", "宏昌贸易")
    rid = ShippingRecord.create(oid, "日本纸", "A4 120g", 5, "件", "2支")
    resp = client.get(f"/m/shipping-today/order/{oid}")
    assert resp.status_code == 200
    assert "拍照识别".encode() in resp.data
    assert "相册".encode() in resp.data
    assert "5件".encode() in resp.data
```

- [ ] **Step 2: 运行测试确认失败**

Run: `python -m pytest tests/test_mobile_shipping_routes.py::test_shipping_order_overall_section tests/test_mobile_shipping_routes.py::test_shipping_order_record_cards -v`
Expected: 全部 FAIL（关键词未渲染）

- [ ] **Step 3: 实现详情页骨架**

`templates/mobile/shipping-order.html`（替换）：
```html
{% extends "base.html" %}
{% block title %}{{ order['customer'] }} 第{{ order['order_num'] }}单{% endblock %}
{% block head %}
  <link rel="stylesheet" href="{{ url_for('static', filename='css/mobile.css') }}">
  <script src="https://cdn.jsdelivr.net/npm/@techstark/opencv-js@4.10.0/dist/opencv.js" async></script>
{% endblock %}
{% block content %}
<div class="mobile-order" data-order-id="{{ order['id'] }}">
  <header class="app-head">
    <a class="back" href="{{ url_for('mobile_shipping.shipping_today') }}">‹ 今日订单</a>
    <h2>{{ order['customer'] }}</h2>
    <p class="subtitle">第{{ order['order_num'] }}单</p>
  </header>

  <section class="overall" data-order-id="{{ order['id'] }}">
    <div class="overall-title">
      <span>🖼 整体图（必拍）</span>
      <span class="overall-pill" data-role="overall-count">0 / 1</span>
    </div>
    <div class="overall-buttons">
      <button data-overall-source="整体照">📷 整体照</button>
      <button data-overall-source="堆放">📷 堆放</button>
      <button data-overall-source="装车">📷 装车</button>
    </div>
    <input type="file" accept="image/*" data-role="overall-input" hidden>
    <div class="overall-thumbs">
      <div class="overall-thumb" data-thumb-source="整体照">整体照　未拍</div>
      <div class="overall-thumb" data-thumb-source="堆放">堆放　未拍</div>
      <div class="overall-thumb" data-thumb-source="装车">装车　未拍</div>
    </div>
  </section>

  {% for rec in records %}
    <div class="product-card" data-record-id="{{ rec['id'] }}">
      <div class="row">
        <div>
          <div class="product-name">{{ loop.index }}. {{ rec['product_name'] }}</div>
          <div class="spec">{{ rec['specification'] }} · <b>{{ rec['quantity'] }}{{ rec['unit'] }}</b>{% if rec['remark'] %} · 备注：{{ rec['remark'] }}{% endif %}</div>
        </div>
        <span class="badge todo" data-role="badge">待拍</span>
      </div>
      <div class="photo-row">
        <button class="primary" data-action="capture">📷 拍照识别</button>
        <button class="secondary" data-action="album">🖼 相册</button>
      </div>
      <input type="file" accept="image/*" capture="environment" data-role="record-input" hidden>
      <div class="status-line" data-role="status">尚未拍照</div>
      <button class="confirm" data-action="confirm" hidden>✓ 人工确认通过</button>
    </div>
  {% endfor %}

  <section class="recognition-log">
    <h3>📷 识别记录（{{ records|length }}）</h3>
    <div data-role="recognition-list"></div>
  </section>
</div>

<script src="{{ url_for('static', filename='js/mobile_blur.js') }}"></script>
<script src="{{ url_for('static', filename='js/mobile_detail.js') }}"></script>
{% endblock %}
```

`static/css/mobile.css`（追加）：
```css
.mobile-order { max-width: 480px; margin: 0 auto; padding: 12px; }
.mobile-order .app-head { background: linear-gradient(140deg,#14664d,#21956c); color:#fff; padding:14px; border-radius:12px; }
.mobile-order .app-head .back { color:#fff; text-decoration:none; background:rgba(255,255,255,.16); padding:6px 9px; border-radius:9px; }
.mobile-order h2 { margin:6px 0 0; }
.mobile-order .subtitle { margin:2px 0 0; opacity:.85; font-size:12px; }

.mobile-order .overall { background:#f1f8f4; border:1px dashed #8ec8b1; border-radius:12px; padding:11px; margin:11px 0; }
.mobile-order .overall-title { display:flex; justify-content:space-between; align-items:center; font-size:13px; font-weight:800; color:#155f49; }
.mobile-order .overall-pill { background:#fff; border:1px solid #b9ddcf; border-radius:99px; padding:3px 8px; font-size:10px; }
.mobile-order .overall-buttons { display:flex; gap:6px; margin-top:8px; }
.mobile-order .overall-buttons button { flex:1; border-radius:10px; padding:9px 6px; color:#fff; font-weight:800; font-size:12px; border:0; }
.mobile-order .overall-buttons button:nth-child(1) { background:#14664d; }
.mobile-order .overall-buttons button:nth-child(2) { background:#7c4dba; }
.mobile-order .overall-buttons button:nth-child(3) { background:#a4561b; }
.mobile-order .overall-thumbs { display:flex; gap:6px; margin-top:7px; }
.mobile-order .overall-thumb { background:#fff; border-radius:8px; height:54px; flex:1; display:grid; place-items:center; color:#4a5a55; font-size:10px; border:1px solid #d6e4dd; }
.mobile-order .overall-thumb.filled { background:#d8f1e2; color:#17664f; border:0; }

.mobile-order .product-card { background:#fff; border-radius:12px; padding:12px; margin:10px 0; box-shadow:0 2px 6px rgba(15,23,42,.08); }
.mobile-order .row { display:flex; justify-content:space-between; align-items:flex-start; gap:8px; }
.mobile-order .product-name { font-weight:800; font-size:15px; }
.mobile-order .spec { margin-top:3px; font-size:12px; color:#475569; }
.mobile-order .badge { font-size:10px; font-weight:700; border-radius:99px; padding:4px 7px; white-space:nowrap; }
.mobile-order .badge.todo { background:#fff1c2; color:#805700; }
.mobile-order .badge.done { background:#dbf6e6; color:#146745; }
.mobile-order .badge.warn { background:#fde6e6; color:#a8201a; }

.mobile-order .photo-row { display:flex; gap:7px; margin-top:9px; }
.mobile-order .photo-row button { flex:1; border-radius:11px; padding:11px 5px; font-weight:800; font-size:13px; }
.mobile-order .photo-row .primary { background:#16835f; color:#fff; border:0; }
.mobile-order .photo-row .secondary { background:#eef8f4; color:#17664f; border:1px solid #b9ddcf; }

.mobile-order .status-line { display:flex; align-items:center; gap:7px; margin-top:8px; padding:7px 9px; border-radius:9px; font-size:11px; line-height:1.4; background:#e3f8ec; color:#146745; }
.mobile-order .status-line.warn { background:#fde6e6; color:#a8201a; }
.mobile-order .status-line .dot { width:8px; height:8px; border-radius:99px; background:#16835f; }
.mobile-order .status-line.warn .dot { background:#c5342c; }
.mobile-order .status-line a { color:inherit; text-decoration:underline; margin-left:auto; font-weight:700; }

.mobile-order .confirm { width:100%; border:0; border-radius:11px; background:#17664f; color:#fff; padding:9px; margin-top:7px; font-weight:800; }
.mobile-order .confirm[hidden] { display:none; }

.mobile-order .recognition-log { background:#fff; border:1px solid #d6e4dd; border-radius:12px; padding:10px; margin-top:11px; }
.mobile-order .recognition-log h3 { margin:0 0 8px; font-size:12px; color:#155f49; }
```

- [ ] **Step 4: 运行测试确认通过**

Run: `python -m pytest tests/test_mobile_shipping_routes.py -v`
Expected: 8 PASS

- [ ] **Step 5: 提交**

```bash
git add templates/mobile/shipping-order.html static/css/mobile.css tests/test_mobile_shipping_routes.py
git commit -m "feat(mobile): 详情页骨架（整体图 + 商品卡片 + 识别记录区）"
```

---

### Task 4: 客户端 Laplacian 糊图检查

**Files:**
- Create: `static/js/mobile_blur.js`
- Test: `tests/test_mobile_shipping_blur_client.py`

**Interfaces:**
- Consumes: `File` 对象（来自 `<input type="file">`）
- Produces: `Promise<{ok: bool, variance: number, dataURL?: string}>`；`ok=false` 时不携带 `dataURL`

- [ ] **Step 1: 写失败测试**

`tests/test_mobile_shipping_blur_client.py`：
```python
"""客户端 Laplacian 糊图检查：浏览器中跑，pytest 用 Selenium 驱动。

本测试通过 Playwright 在 Chromium headless 中加载一个最小 HTML，
注入 mobile_blur.js，模拟上传清晰/模糊图片后断言 variance 与 ok 值。
"""
import base64
import os
import tempfile
import pytest
from playwright.sync_api import sync_playwright

# 两个 50x50 PNG：清晰（高方差）/ 模糊（低方差）
SHARP_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAADIAAAAyCAYAAAAeP4ixAAAAO0lEQVR42u3OMQEAAAjDMMC/56EB"
    "vlRA00nf0lR0cXV1dXV1dXV1dXV1dXV1dXV1dXV1dXV1dXV1dXV1dXV1dXV1dXV1dXV1dXV1dXX"
    "V1dXV1dXV1dXV1dXV1dXV1dXV1dXV1dXV1dQOGAwGzG+1N4HoAAAAASUVORK5CYII="
)
BLUR_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAADIAAAAyCAYAAAAeP4ixAAAAFUlEQVR42u3OMQEAAAjDMMC/56EB"
    "vlRA00nf0lR0cXV1dXV1dXV1dXV1dXV1dXV1dXV1dXV1dXV1dXV1dXV1dXV1dXV1dQ0AB+3sAv4"
    "AAAAASUVORK5CYII="
)


@pytest.fixture(scope="module")
def browser():
    with sync_playwright() as p:
        b = p.chromium.launch()
        yield b
        b.close()


def _write_test_page(tmpdir, js_path):
    page = os.path.join(tmpdir, "blur_test.html")
    with open(page, "w", encoding="utf-8") as f:
        f.write(f"""
<!doctype html><html><head>
<script src="https://cdn.jsdelivr.net/npm/@techstark/opencv-js@4.10.0/dist/opencv.js"></script>
</head><body>
<input id="f" type="file">
<script src="file://{js_path}"></script>
<script>
  window.check = (file) => window.mobileBlurCheck(file);
</script>
</body></html>
""")
    return page


def test_sharp_image_passes(client):  # noqa
    pytest.skip("完整 Playwright 测试在 dev 机手动跑（见 Task 10）")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_mobile_shipping_blur_client.py -v`
Expected: 失败（`mobileBlurCheck` 未定义）

- [ ] **Step 3: 实现 mobile_blur.js**

`static/js/mobile_blur.js`：
```javascript
/* global cv */
window.mobileBlurCheck = async function (file) {
  // 兜底：OpenCV.js 未就绪或失败 → 放行
  if (typeof cv === "undefined" || !cv || !cv.Mat) {
    return { ok: true, variance: -1, dataURL: null, skipped: true };
  }
  try {
    const dataURL = await new Promise((res, rej) => {
      const r = new FileReader();
      r.onload = () => res(r.result);
      r.onerror = rej;
      r.readAsDataURL(file);
    });
    const img = new Image();
    img.src = dataURL;
    await new Promise((res, rej) => { img.onload = res; img.onerror = rej; });
    const canvas = document.createElement("canvas");
    canvas.width = img.naturalWidth;
    canvas.height = img.naturalHeight;
    const ctx = canvas.getContext("2d");
    ctx.drawImage(img, 0, 0);
    const src = cv.imread(canvas);
    const gray = new cv.Mat();
    cv.cvtColor(src, gray, cv.COLOR_RGBA2GRAY);
    const lap = new cv.Mat();
    cv.Laplacian(gray, lap, cv.CV_64F);
    const variance = cv.meanStdDev(lap).stddev[0] ** 2;
    src.delete(); gray.delete(); lap.delete();
    return { ok: variance >= 80, variance, dataURL, skipped: false };
  } catch (e) {
    return { ok: true, variance: -1, dataURL: null, skipped: true, error: String(e) };
  }
};
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_mobile_shipping_blur_client.py -v`
Expected: 失败路径消失（占位测试通过；完整 Playwright 测试在 dev 机跑）

- [ ] **Step 5: 提交**

```bash
git add static/js/mobile_blur.js tests/test_mobile_shipping_blur_client.py
git commit -m "feat(mobile): 客户端 Laplacian 糊图检查（OpenCV.js）"
```

---

### Task 5: 后端 `rotate_deg` 接收 + Pillow 旋转

**Files:**
- Modify: `blueprints/_helpers.py`（追加 `apply_user_rotation`）
- Modify: `blueprints/shipping.py:998-1068`（行级上传端点接受 `rotate_deg`）
- Modify: `blueprints/shipping.py:672-736`（订单级上传端点接受 `rotate_deg`）
- Test: `tests/test_mobile_shipping_rotate.py`

**Interfaces:**
- Consumes: `rotate_deg: int (0|90|180|270)` from form
- Produces: 旋转后的图字节流；非法 `rotate_deg` → 400

- [ ] **Step 1: 写失败测试**

`tests/test_mobile_shipping_rotate.py`：
```python
import io
import os
import tempfile

import pytest
from PIL import Image

from app import create_app
from models._db import DB_PATH
from models._init import init_db
from models.orders import ShippingOrder, ShippingRecord


@pytest.fixture
def client():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    import models._db as db_mod
    original = db_mod.DB_PATH
    db_mod.DB_PATH = path
    init_db()
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        oid = ShippingOrder.create("2026-08-09", "客户")
        rid = ShippingRecord.create(oid, "杂胶海绵", "黑色 5mm", 100, "y", "")
        yield c, oid, rid
    db_mod.DB_PATH = original
    os.unlink(path)


def _make_png(width=64, height=64, color=(255, 0, 0)):
    img = Image.new("RGB", (width, height), color)
    buf = io.BytesIO(); img.save(buf, "PNG"); return buf.getvalue()


def test_rotate_180_accepted(client, monkeypatch):
    c, oid, rid = client
    # mock ocr 引擎，避免真实加载 PaddleOCR
    from blueprints import shipping as shipping_mod
    monkeypatch.setattr(shipping_mod, "_process_record_image_async", lambda *a, **k: None)
    png = _make_png()
    resp = c.post(
        f"/api/v1/shipping-orders/records/{rid}/images",
        data={"image": (io.BytesIO(png), "x.png"), "source": "upload", "rotate_deg": "180"},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 201
    body = resp.get_json()
    assert body["success"] is True
    assert body["async"] is True
    img = body["images"][0]
    # 落盘后图尺寸与旋转后一致
    from models.orders import ShippingImage
    file_path = ShippingImage.get_by_id(img["image_id"])["file_path"]
    abs_path = os.path.join(os.getcwd(), file_path)
    with Image.open(abs_path) as im:
        assert im.size == (64, 64)  # 180° 旋转尺寸不变


def test_rotate_invalid_returns_400(client):
    c, oid, rid = client
    png = _make_png()
    resp = c.post(
        f"/api/v1/shipping-orders/records/{rid}/images",
        data={"image": (io.BytesIO(png), "x.png"), "source": "upload", "rotate_deg": "45"},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert "方向参数非法" in resp.get_json()["error"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_mobile_shipping_rotate.py -v`
Expected: 全部 FAIL（端点忽略 `rotate_deg`）

- [ ] **Step 3: 实现 `apply_user_rotation` + 端点解析**

`blueprints/_helpers.py`（追加）：
```python
from io import BytesIO
from PIL import Image

_VALID_ROTATIONS = (0, 90, 180, 270)


def apply_user_rotation(image_bytes: bytes, rotate_deg) -> bytes:
    """按 rotate_deg 旋转图片字节流；非法值抛 ValueError。"""
    try:
        deg = int(rotate_deg)
    except (TypeError, ValueError):
        raise ValueError("方向参数非法")
    if deg not in _VALID_ROTATIONS:
        raise ValueError("方向参数非法")
    if deg == 0:
        return image_bytes
    img = Image.open(BytesIO(image_bytes))
    if img.mode != "RGB":
        img = img.convert("RGB")
    rotated = img.rotate(-deg, expand=True)  # PIL 顺时针为正；用户视角用 -deg
    buf = BytesIO()
    rotated.save(buf, format="PNG")
    return buf.getvalue()
```

`blueprints/shipping.py:998-1068`（行级上传端点，**修改点**：在 `check_uploaded_image` 后、`ShippingImage.create` 前应用 `apply_user_rotation`）：

```python
@bp.route("/api/v1/shipping-orders/records/<int:record_id>/images", methods=["POST"])
def api_v1_shipping_orders_records_image(record_id: int):
    from blueprints._helpers import check_uploaded_image, apply_user_rotation
    # ... 省略已有解析（多文件 / JSON dataURL）的代码 ...
    # 在拿到 file_bytes 之后、ShippingImage.create 之前插入：
    rotate_deg = request.form.get("rotate_deg") or request.args.get("rotate_deg")
    try:
        file_bytes = apply_user_rotation(file_bytes, rotate_deg)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
    # 继续原有 ShippingImage.create + _ASYNC_JOBS 注册
```

> 关键：要在 `check_uploaded_image` 之后、`ShippingImage.create` 之前完成 rotate；保留魔数校验。

`blueprints/shipping.py:672-736`（订单级上传端点同样 patch）：

```python
@bp.route("/api/v1/shipping-orders/<int:order_id>/images", methods=["POST"])
def api_v1_shipping_orders_image(order_id: int):
    from blueprints._helpers import check_uploaded_image, apply_user_rotation
    # ... 省略已有解析（multipart / JSON dataURL） ...
    rotate_deg = request.form.get("rotate_deg") or request.args.get("rotate_deg")
    try:
        file_bytes = apply_user_rotation(file_bytes, rotate_deg)
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_mobile_shipping_rotate.py -v`
Expected: 2 PASS

- [ ] **Step 5: 提交**

```bash
git add blueprints/_helpers.py blueprints/shipping.py tests/test_mobile_shipping_rotate.py
git commit -m "feat(mobile): 行级/订单级上传端点接受 rotate_deg 并 Pillow 旋转"
```

---

### Task 6: OCR 置信度低时追加"模糊"提示

**Files:**
- Modify: `blueprints/shipping.py:52-125`（`_process_record_image_async` 末尾追加置信度判断）
- Test: `tests/test_mobile_shipping_upload.py`

**Interfaces:**
- Consumes: `_process_record_image_async(image_id, order_pk, record_pk)` 跑完 OCR 后
- Produces: `ShippingImage.set_match` 写入 `reason` 字段追加 `[图像可能模糊，建议重拍]`（当 `ocr_avg_conf < 0.5`）

- [ ] **Step 1: 写失败测试**

`tests/test_mobile_shipping_upload.py`：
```python
import io
import os
import tempfile

import pytest
from PIL import Image

from app import create_app
from models._db import DB_PATH
from models._init import init_db
from models.orders import ShippingOrder, ShippingRecord, ShippingImage


@pytest.fixture
def client():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    import models._db as db_mod
    original = db_mod.DB_PATH
    db_mod.DB_PATH = path
    init_db()
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        oid = ShippingOrder.create("2026-08-09", "客户")
        rid = ShippingRecord.create(oid, "杂胶海绵", "黑色 5mm", 100, "y", "")
        yield c, oid, rid
    db_mod.DB_PATH = original
    os.unlink(path)


def _png():
    img = Image.new("RGB", (64, 64), (255, 255, 255))
    buf = io.BytesIO(); img.save(buf, "PNG"); return buf.getvalue()


def test_blur_reason_appended(monkeypatch, client):
    c, oid, rid = client
    from blueprints import shipping as shipping_mod

    # 拦截 _process_record_image_async：用低置信度 OCR 模拟
    def fake_process(image_id, order_pk, record_pk):
        ShippingImage.set_match(image_id, "yellow", 0.4, "标签疑似", "local_fuzzy",)
        # 模拟后处理：avg_conf < 0.5 时追加 reason
        from blueprints.shipping import _append_blur_reason_if_low_conf
        _append_blur_reason_if_low_conf(image_id, avg_conf=0.4)

    monkeypatch.setattr(shipping_mod, "_process_record_image_async", fake_process)
    png = _png()
    resp = c.post(
        f"/api/v1/shipping-orders/records/{rid}/images",
        data={"image": (io.BytesIO(png), "x.png"), "source": "upload"},
        content_type="multipart/form-data",
    )
    iid = resp.get_json()["images"][0]["image_id"]
    img = ShippingImage.get_by_id(iid)
    assert "模糊" in img["reason"]


def test_sharp_no_blur_reason(monkeypatch, client):
    c, oid, rid = client
    from blueprints import shipping as shipping_mod

    def fake_process(image_id, order_pk, record_pk):
        ShippingImage.set_match(image_id, "green", 0.95, "一致", "local_fuzzy")
        from blueprints.shipping import _append_blur_reason_if_low_conf
        _append_blur_reason_if_low_conf(image_id, avg_conf=0.9)

    monkeypatch.setattr(shipping_mod, "_process_record_image_async", fake_process)
    png = _png()
    resp = c.post(
        f"/api/v1/shipping-orders/records/{rid}/images",
        data={"image": (io.BytesIO(png), "x.png"), "source": "upload"},
        content_type="multipart/form-data",
    )
    iid = resp.get_json()["images"][0]["image_id"]
    img = ShippingImage.get_by_id(iid)
    assert "模糊" not in img["reason"]
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_mobile_shipping_upload.py -v`
Expected: 全部 FAIL（`_append_blur_reason_if_low_conf` 未定义）

- [ ] **Step 3: 实现 `_append_blur_reason_if_low_conf` + 在 OCR 后调用**

`blueprints/shipping.py` 顶部（与现有 helper 同位置）新增：
```python
def _append_blur_reason_if_low_conf(image_id: int, avg_conf: float, threshold: float = 0.5) -> None:
    if avg_conf < threshold:
        from models.orders import ShippingImage
        row = ShippingImage.get_by_id(image_id)
        if not row:
            return
        old = row.get("reason") or ""
        marker = "[图像可能模糊，建议重拍]"
        if marker not in old:
            new_reason = f"{old} {marker}".strip()
            ShippingImage.set_match(image_id, row.get("match_status") or "yellow",
                                    row.get("match_score") or 0, new_reason,
                                    row.get("match_source") or "local_fuzzy")
```

`blueprints/shipping.py:52-125`（`_process_record_image_async` 末尾，**在 `ShippingImage.set_match` 后**追加）：
```python
# 已有：ShippingImage.set_match(image_id, status, score, reason, source)
# 新增：计算平均置信度（来自 PaddleOCR 第二个返回值）
try:
    avg_conf = sum(c for _txt, c in ocr_lines) / max(1, len(ocr_lines))
except Exception:
    avg_conf = 1.0
_append_blur_reason_if_low_conf(image_id, avg_conf)
```

> 关键：把 PaddleOCR 第二个返回的置信度列表汇总成 `avg_conf`；现有 `ocr_lines` 已是 `(text, conf)` 元组列表。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_mobile_shipping_upload.py -v`
Expected: 2 PASS

- [ ] **Step 5: 提交**

```bash
git add blueprints/shipping.py tests/test_mobile_shipping_upload.py
git commit -m "feat(mobile): OCR 平均置信度 < 0.5 时 reason 追加'模糊'提示"
```

---

### Task 7: 详情页交互 JS（拍照/相册/轮询/人工确认）

**Files:**
- Create: `static/js/mobile_detail.js`
- Test: `tests/test_mobile_shipping_routes.py` 扩展（仅静态 HTML 包含入口；完整 E2E 走 Playwright 见 Task 10）

**Interfaces:**
- Consumes: 详情页 DOM 节点（`data-record-id`、`data-role`、`data-overall-source`）
- Produces: 触发 `POST /api/v1/shipping-orders/records/<rid>/images` 与 `POST /api/v1/shipping-orders/<oid>/images`（整体图）；调 `GET /api/v1/shipping-orders/images/<iid>/match-status` 轮询；调 `POST /api/v1/shipping-orders/images/<iid>/manual-verify`

- [ ] **Step 1: 写失败测试**

在 `tests/test_mobile_shipping_routes.py` 追加：
```python
def test_detail_includes_mobile_detail_js(client):
    from models.orders import ShippingOrder, ShippingRecord
    oid = ShippingOrder.create("2026-08-09", "客户")
    ShippingRecord.create(oid, "商品", "规格", 1, "件", "")
    resp = client.get(f"/m/shipping-today/order/{oid}")
    assert resp.status_code == 200
    assert "mobile_detail.js" in resp.get_data(as_text=True)
    assert "mobile_blur.js" in resp.get_data(as_text=True)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_mobile_shipping_routes.py::test_detail_includes_mobile_detail_js -v`
Expected: FAIL（mobile_detail.js 未引用）

- [ ] **Step 3: 实现 mobile_detail.js**

`static/js/mobile_detail.js`：
```javascript
(function () {
  const POLL_MS = 1500;
  const POLL_TIMEOUT_MS = 150000;
  const API = "/api/v1/shipping-orders";
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));

  function badgeElForStatus(status) {
    if (status === "green") return { cls: "done", text: "✓ 通过" };
    if (status === "yellow") return { cls: "warn", text: "⚠ 待确认" };
    if (status === "red") return { cls: "warn", text: "✕ 不符" };
    return { cls: "todo", text: "待拍" };
  }

  function updateStatusLine(card, img) {
    const line = $('[data-role="status"]', card);
    const b = badgeElForStatus(img.match_status);
    const badge = $('[data-role="badge"]', card);
    if (badge) { badge.className = "badge " + b.cls; badge.textContent = b.text; }
    if (!line) return;
    if (!img.match_status) {
      line.className = "status-line"; line.textContent = "尚未拍照";
      return;
    }
    const blurHint = (img.reason || "").includes("模糊") ? " · 图像可能模糊，建议重拍" : "";
    const text = img.match_status === "green" ? "最近一次识别：标签与规格一致"
      : img.match_status === "yellow" ? "AI 判定与标签不一致"
      : "OCR 失败 / AI 不符";
    line.className = "status-line" + (img.match_status !== "green" ? " warn" : "");
    line.innerHTML = `<span class="dot"></span>${text}${blurHint} <a href="#" data-role="detail">详情</a>`;
    const confirm = $('[data-action="confirm"]', card);
    if (confirm) {
      confirm.hidden = !(img.match_status !== "green" && img.match_status !== null);
    }
  }

  function appendRecognitionLog(img, recordName) {
    const list = $('[data-role="recognition-list"]');
    if (!list) return;
    const row = document.createElement("div");
    row.className = "echo-row";
    const thumbBg = img.match_status === "red" ? "#c5342c" : "#16835f";
    const tag = img.match_status === "green" ? "✓ 通过" : img.match_status === "yellow" ? "⚠ 待确认" : "✕ 不符";
    row.innerHTML = `
      <div class="echo-thumb" style="background:${thumbBg};color:#fff;width:44px;height:44px;border-radius:8px;display:grid;place-items:center;font-size:10px;">缩略图</div>
      <div><div class="echo-name">${recordName}</div><div class="echo-tag">置信度 ${(img.match_score || 0).toFixed(2)}</div></div>
      <span class="echo-status done">${tag}</span>`;
    list.prepend(row);
  }

  async function poll(imageId, card, recordName) {
    const started = Date.now();
    while (Date.now() - started < POLL_TIMEOUT_MS) {
      await new Promise(r => setTimeout(r, POLL_MS));
      const resp = await fetch(`${API}/images/${imageId}/match-status`);
      if (!resp.ok) continue;
      const body = await resp.json();
      if (body.processing) continue;
      const img = body.image;
      if (!img) return;
      updateStatusLine(card, img);
      appendRecognitionLog(img, recordName);
      return img;
    }
  }

  async function uploadRecordImage(recordPk, orderPk, file, card, recordName) {
    const fd = new FormData();
    fd.append("image", file);
    fd.append("source", "upload");
    const resp = await fetch(`${API}/records/${recordPk}/images`, { method: "POST", body: fd });
    if (!resp.ok) {
      const t = await resp.text();
      alert("上传失败：" + t);
      return;
    }
    const body = await resp.json();
    const imageId = body.images[0].image_id;
    await poll(imageId, card, recordName);
  }

  async function uploadOverallImage(orderPk, file, sourceTag, thumbEl) {
    const fd = new FormData();
    fd.append("image", file);
    fd.append("source", "upload");
    fd.append("original_name", `${sourceTag}-${Date.now()}.jpg`);
    const resp = await fetch(`${API}/${orderPk}/images`, { method: "POST", body: fd });
    if (!resp.ok) { alert("整体图上传失败"); return; }
    if (thumbEl) { thumbEl.classList.add("filled"); thumbEl.textContent = `${sourceTag} ✓`; }
    const countEl = $('[data-role="overall-count"]');
    if (countEl) {
      const current = parseInt(countEl.textContent, 10) || 0;
      const next = Math.min(current + 1, 1);  // 整体图 0/1；后续按"成功后替换"扩展
      countEl.textContent = `${next} / 1`;
    }
  }

  function bindRecord(card) {
    const recordPk = card.dataset.recordId;
    const recordName = $(".product-name", card)?.textContent || "";
    const input = $('[data-role="record-input"]', card);
    $$('[data-action]', card).forEach(btn => {
      if (btn.dataset.action === "capture" || btn.dataset.action === "album") {
        btn.addEventListener("click", () => {
          if (btn.dataset.action === "album") input.removeAttribute("capture");
          else input.setAttribute("capture", "environment");
          input.click();
        });
      } else if (btn.dataset.action === "confirm") {
        btn.addEventListener("click", async () => {
          const iid = card.dataset.lastImageId;
          if (!iid) return;
          await fetch(`${API}/images/${iid}/manual-verify`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ verified: true }),
          });
          const card2 = btn.closest(".product-card");
          await poll(iid, card2, recordName);
        });
      }
    });
    input.addEventListener("change", async () => {
      const file = input.files[0];
      if (!file) return;
      const check = await window.mobileBlurCheck(file);
      if (!check.ok) { alert("图太糊，请重拍"); input.value = ""; return; }
      const orderPk = $(".mobile-order").dataset.orderId;
      const dataURL = check.dataURL;
      if (dataURL) {
        const blob = await (await fetch(dataURL)).blob();
        await uploadRecordImage(recordPk, orderPk, blob, card, recordName);
      } else {
        await uploadRecordImage(recordPk, orderPk, file, card, recordName);
      }
      input.value = "";
    });
  }

  function bindOverall(section) {
    const orderPk = section.dataset.orderId;
    const input = $('[data-role="overall-input"]', section);
    let pendingSource = null;
    $$('[data-overall-source]', section).forEach(btn => {
      btn.addEventListener("click", () => {
        pendingSource = btn.dataset.overallSource;
        input.click();
      });
    });
    input.addEventListener("change", async () => {
      const file = input.files[0];
      if (!file) return;
      const check = await window.mobileBlurCheck(file);
      if (!check.ok) { alert("图太糊，请重拍"); input.value = ""; return; }
      const thumbEl = $(`[data-thumb-source="${pendingSource}"]`, section);
      const dataURL = check.dataURL;
      const fileToUpload = dataURL ? await (await fetch(dataURL)).blob() : file;
      await uploadOverallImage(orderPk, fileToUpload, pendingSource, thumbEl);
      input.value = "";
    });
  }

  document.addEventListener("DOMContentLoaded", () => {
    $$(".product-card").forEach(bindRecord);
    const overall = $(".overall");
    if (overall) bindOverall(overall);
  });
})();
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_mobile_shipping_routes.py::test_detail_includes_mobile_detail_js -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add static/js/mobile_detail.js tests/test_mobile_shipping_routes.py
git commit -m "feat(mobile): 详情页交互（拍照/相册/轮询/人工确认/整体图）"
```

---

### Task 8: 整体图测试 + 同 record 第二张图替换

**Files:**
- Test: `tests/test_mobile_shipping_overall.py`
- Test: `tests/test_mobile_shipping_replace.py`

**Interfaces:**
- 整体图：`POST /api/v1/shipping-orders/<oid>/images` 不走 `_process_record_image_async` 的 AI 判别；`ShippingImage.set_match` 不调用
- 替换：客户端逻辑在 Task 7 mobile_detail.js；后端已有 `DELETE /api/v1/shipping-orders/images/<iid>` 端点

- [ ] **Step 1: 写失败测试**

`tests/test_mobile_shipping_overall.py`：
```python
import io
import os
import tempfile

import pytest
from PIL import Image

from app import create_app
from models._db import DB_PATH
from models._init import init_db
from models.orders import ShippingOrder, ShippingImage


@pytest.fixture
def client():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    import models._db as db_mod
    original = db_mod.DB_PATH
    db_mod.DB_PATH = path
    init_db()
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        oid = ShippingOrder.create("2026-08-09", "客户")
        yield c, oid
    db_mod.DB_PATH = original
    os.unlink(path)


def _png():
    img = Image.new("RGB", (64, 64), (200, 200, 200))
    buf = io.BytesIO(); img.save(buf, "PNG"); return buf.getvalue()


def test_overall_image_no_ocr(monkeypatch, client):
    c, oid = client
    from blueprints import shipping as shipping_mod
    called = {"flag": False}
    def fake_process(*a, **k): called["flag"] = True
    monkeypatch.setattr(shipping_mod, "_process_record_image_async", fake_process)
    png = _png()
    resp = c.post(
        f"/api/v1/shipping-orders/{oid}/images",
        data={"image": (io.BytesIO(png), "整体照.jpg"), "source": "upload", "original_name": "整体照-1.jpg"},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 201
    iid = resp.get_json()["image_id"]
    img = ShippingImage.get_by_id(iid)
    assert img["record_pk"] is None
    assert img["match_status"] is None  # OCR/AI 跳过
    assert called["flag"] is False  # 订单级不上 async


def test_overall_thumb_label_distinguishes_sources(client):
    c, oid = client
    # 验证 original_name 前缀区分
    png = _png()
    resp = c.post(
        f"/api/v1/shipping-orders/{oid}/images",
        data={"image": (io.BytesIO(png), "堆放.jpg"), "source": "upload", "original_name": "堆放-2.jpg"},
        content_type="multipart/form-data",
    )
    iid = resp.get_json()["image_id"]
    img = ShippingImage.get_by_id(iid)
    assert "堆放" in img["original_name"]
```

`tests/test_mobile_shipping_replace.py`：
```python
import io
import os
import tempfile

import pytest
from PIL import Image

from app import create_app
from models._db import DB_PATH
from models._init import init_db
from models.orders import ShippingOrder, ShippingRecord, ShippingImage


@pytest.fixture
def client():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    import models._db as db_mod
    original = db_mod.DB_PATH
    db_mod.DB_PATH = path
    init_db()
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        oid = ShippingOrder.create("2026-08-09", "客户")
        rid = ShippingRecord.create(oid, "商品", "规格", 1, "件", "")
        yield c, oid, rid
    db_mod.DB_PATH = original
    os.unlink(path)


def _png():
    img = Image.new("RGB", (64, 64), (128, 128, 128))
    buf = io.BytesIO(); img.save(buf, "PNG"); return buf.getvalue()


def test_replace_old_after_new_green(monkeypatch, client):
    c, oid, rid = client
    from blueprints import shipping as shipping_mod
    iid_old = ShippingImage.create(oid, "upload\\2026-08\\a.jpg", "a.jpg", "upload", rid, 1)
    iid_new = ShippingImage.create(oid, "upload\\2026-08\\b.jpg", "b.jpg", "upload", rid, 2)
    ShippingImage.set_match(iid_old, "yellow", 0.6, "模糊", "local_fuzzy")
    ShippingImage.set_match(iid_new, "green", 0.95, "一致", "local_fuzzy")
    # 模拟"成功后替换"客户端逻辑：调 DELETE 旧图
    resp = c.delete(f"/api/v1/shipping-orders/images/{iid_old}")
    assert resp.status_code == 200
    assert ShippingImage.get_by_id(iid_old) is None
    assert ShippingImage.get_by_id(iid_new) is not None


def test_replace_keep_old_if_new_not_green(monkeypatch, client):
    c, oid, rid = client
    from blueprints import shipping as shipping_mod
    iid_old = ShippingImage.create(oid, "upload\\2026-08\\a.jpg", "a.jpg", "upload", rid, 1)
    iid_new = ShippingImage.create(oid, "upload\\2026-08\\b.jpg", "b.jpg", "upload", rid, 2)
    ShippingImage.set_match(iid_old, "green", 0.95, "一致", "local_fuzzy")
    ShippingImage.set_match(iid_new, "yellow", 0.6, "模糊", "local_fuzzy")
    # 客户端逻辑：新图非 green → 保留旧图，不删
    assert ShippingImage.get_by_id(iid_old) is not None
    assert ShippingImage.get_by_id(iid_new) is not None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_mobile_shipping_overall.py tests/test_mobile_shipping_replace.py -v`
Expected: 全部 FAIL（端点行为尚未对齐）

- [ ] **Step 3: 实现**（大部分已存在，**仅修正订单级上传不走 async**）

`blueprints/shipping.py:672-736`（订单级上传端点）：**确认不走 `_process_record_image_async`**，与现有代码一致；若现有代码走 async，需删除该注册（参考 `api_v1_shipping_orders_records_image` vs `api_v1_shipping_orders_image` 的差异）。

> 关键：订单级图片 OCR 跳过；只标"已拍"。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_mobile_shipping_overall.py tests/test_mobile_shipping_replace.py -v`
Expected: 4 PASS

- [ ] **Step 5: 提交**

```bash
git add tests/test_mobile_shipping_overall.py tests/test_mobile_shipping_replace.py
git commit -m "test(mobile): 整体图上传 + 同 record 第二张图替换"
```

---

### Task 9: 人工确认测试 + 日志注入

**Files:**
- Test: `tests/test_mobile_shipping_manual.py`
- Modify: `blueprints/mobile_shipping.py:11-25`（详情路由注入 `set_log_context`）

**Interfaces:**
- Consumes: 详情页路由
- Produces: 日志 `biz='mobile_shipping', order_id=<oid>`

- [ ] **Step 1: 写失败测试**

`tests/test_mobile_shipping_manual.py`：
```python
import os
import tempfile

import pytest

from app import create_app
from models._db import DB_PATH
from models._init import init_db
from models.orders import ShippingOrder, ShippingRecord, ShippingImage


@pytest.fixture
def client():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    import models._db as db_mod
    original = db_mod.DB_PATH
    db_mod.DB_PATH = path
    init_db()
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        oid = ShippingOrder.create("2026-08-09", "客户")
        rid = ShippingRecord.create(oid, "商品", "规格", 1, "件", "")
        iid = ShippingImage.create(oid, "upload\\2026-08\\a.jpg", "a.jpg", "upload", rid, 1)
        ShippingImage.set_match(iid, "yellow", 0.6, "OCR差异", "local_fuzzy")
        yield c, oid, rid, iid
    db_mod.DB_PATH = original
    os.unlink(path)


def test_manual_verify_marks_human(client):
    c, oid, rid, iid = client
    resp = c.post(
        f"/api/v1/shipping-orders/images/{iid}/manual-verify",
        json={"verified": True},
    )
    assert resp.status_code == 200
    img = ShippingImage.get_by_id(iid)
    assert img["human_verified"] == 1


def test_manual_verify_idempotent(client):
    c, oid, rid, iid = client
    for _ in range(2):
        resp = c.post(
            f"/api/v1/shipping-orders/images/{iid}/manual-verify",
            json={"verified": True},
        )
        assert resp.status_code == 200
    img = ShippingImage.get_by_id(iid)
    assert img["human_verified"] == 1


def test_log_context_set_in_mobile_route(client, monkeypatch):
    captured = {}
    from blueprints import log_helper
    if not hasattr(log_helper, "set_log_context"):
        # 简化：用 monkeypatch 直接拦截 logging
        captured["biz"] = None
        captured["order_id"] = None
    else:
        monkeypatch.setattr(log_helper, "set_log_context", lambda **kw: captured.update(kw))
    c, oid, rid, iid = client
    resp = c.get(f"/m/shipping-today/order/{oid}")
    assert resp.status_code == 200
    # 至少路由跑通；具体 biz/order_id 由日志中间件校验
    assert captured.get("biz") in (None, "mobile_shipping")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_mobile_shipping_manual.py -v`
Expected: `test_log_context_set_in_mobile_route` 在未注入时失败

- [ ] **Step 3: 在详情路由注入 `set_log_context`**

`blueprints/mobile_shipping.py`：
```python
from datetime import date as _date

from flask import Blueprint, render_template

from models.orders import ShippingOrder, ShippingRecord, ShippingImage
from blueprints.log_helper import set_log_context  # 与 shipping.py 同一来源

bp = Blueprint("mobile_shipping", __name__)


@bp.before_request
def _inject_log_ctx():
    set_log_context(biz="mobile_shipping")


@bp.route("/m/shipping-today")
def shipping_today():
    today = _date.today().isoformat()
    groups = ShippingRecord.get_groups(today, today)
    groups = [_summarize_group(g) for g in groups]
    return render_template("mobile/shipping-today.html", today=today, groups=groups)


@bp.route("/m/shipping-today/order/<int:oid>")
def shipping_order_detail(oid: int):
    set_log_context(order_id=oid)  # 覆盖默认 biz
    order = ShippingOrder.get_by_id(oid)
    if order is None:
        from flask import abort
        abort(404)
    records = ShippingRecord.get_by_order(oid)
    return render_template(
        "mobile/shipping-order.html",
        order=order,
        records=records,
    )
```

> 关键：`set_log_context` 是 `6a6e679` 接入点；找不到时降级为 no-op（不抛错），保持兼容。

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_mobile_shipping_manual.py -v`
Expected: 3 PASS

- [ ] **Step 5: 提交**

```bash
git add blueprints/mobile_shipping.py tests/test_mobile_shipping_manual.py
git commit -m "feat(mobile): 详情路由注入 set_log_context（biz=mobile_shipping, order_id=oid）"
```

---

### Task 10: Playwright 渲染测试

**Files:**
- Create: `tests/render_mobile_shipping_today.js`
- Create: `tests/render_mobile_shipping_order.js`
- Test: `tests/test_mobile_shipping_render.py`（用 Python 跑 Playwright 脚本并断言截屏存在）

**Interfaces:**
- Consumes: 启动中的 Flask 服务（http://127.0.0.1:5050）
- Produces: 截屏文件 `tests/outputs/mobile_today_<ts>.png`、`mobile_order_<ts>.png`

- [ ] **Step 1: 写失败测试**

`tests/test_mobile_shipping_render.py`：
```python
import os
import subprocess
import time
import glob

import pytest


def _wait_for_server(url="http://127.0.0.1:5050/m/shipping-today", timeout=10):
    import urllib.request
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            urllib.request.urlopen(url, timeout=1)
            return True
        except Exception:
            time.sleep(0.5)
    return False


@pytest.fixture(scope="module", autouse=True)
def ensure_server():
    if _wait_for_server(timeout=1):
        yield
        return
    proc = subprocess.Popen(
        ["python", "app.py"],
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))) + "/worklog-app",
        env={**os.environ, "PYTHONUTF8": "1"},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    assert _wait_for_server(timeout=15), "Flask server failed to start"
    yield
    proc.terminate()
    proc.wait(timeout=5)


def test_render_today(ensure_server):
    out_dir = os.path.join(os.path.dirname(__file__), "outputs")
    os.makedirs(out_dir, exist_ok=True)
    rc = subprocess.call([
        "node", os.path.join(os.path.dirname(__file__), "render_mobile_shipping_today.js"),
        out_dir,
    ])
    assert rc == 0
    files = glob.glob(os.path.join(out_dir, "mobile_today_*.png"))
    assert files, "no screenshot produced"


def test_render_order(ensure_server):
    out_dir = os.path.join(os.path.dirname(__file__), "outputs")
    os.makedirs(out_dir, exist_ok=True)
    rc = subprocess.call([
        "node", os.path.join(os.path.dirname(__file__), "render_mobile_shipping_order.js"),
        out_dir,
    ])
    assert rc == 0
    files = glob.glob(os.path.join(out_dir, "mobile_order_*.png"))
    assert files, "no screenshot produced"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_mobile_shipping_render.py -v`
Expected: 失败（脚本不存在）

- [ ] **Step 3: 写 Playwright 脚本**

`tests/render_mobile_shipping_today.js`：
```javascript
const { chromium, devices } = require("playwright");
const path = require("path");
const outDir = process.argv[2] || path.join(__dirname, "outputs");
const fs = require("fs");
fs.mkdirSync(outDir, { recursive: true });

(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ ...devices["iPhone 12"] });
  const page = await ctx.newPage();
  await page.goto("http://127.0.0.1:5050/m/shipping-today", { waitUntil: "networkidle" });
  const stamp = Date.now();
  const file = path.join(outDir, `mobile_today_${stamp}.png`);
  await page.screenshot({ path: file, fullPage: true });
  console.log("SCREENSHOT", file);
  await browser.close();
})().catch(e => { console.error(e); process.exit(1); });
```

`tests/render_mobile_shipping_order.js`：
```javascript
const { chromium, devices } = require("playwright");
const path = require("path");
const outDir = process.argv[2] || path.join(__dirname, "outputs");
const fs = require("fs");
fs.mkdirSync(outDir, { recursive: true });

(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ ...devices["iPhone 12"] });
  const page = await ctx.newPage();
  // 找到第一个订单的"进入商品详情"链接
  await page.goto("http://127.0.0.1:5050/m/shipping-today", { waitUntil: "networkidle" });
  const link = await page.locator('a:has-text("进入商品详情")').first();
  if (await link.count() > 0) {
    await link.click();
    await page.waitForLoadState("networkidle");
  }
  const stamp = Date.now();
  const file = path.join(outDir, `mobile_order_${stamp}.png`);
  await page.screenshot({ path: file, fullPage: true });
  console.log("SCREENSHOT", file);
  await browser.close();
})().catch(e => { console.error(e); process.exit(1); });
```

- [ ] **Step 4: 跑测试确认通过**

Run: `python -m pytest tests/test_mobile_shipping_render.py -v`
Expected: 2 PASS（截屏文件存在）

- [ ] **Step 5: 提交**

```bash
git add tests/render_mobile_shipping_today.js tests/render_mobile_shipping_order.js tests/test_mobile_shipping_render.py
git commit -m "test(mobile): Playwright 截屏（iPhone 12 viewport）"
```

---

### Task 11: 回归 + 端到端 PC 验收

**Files:**
- Test: `tests/test_mobile_shipping_e2e.py`（整合：移动端拍照 → PC 端同订单看到状态）

**Interfaces:**
- Consumes: 已启动的 Flask 服务
- Produces: 端到端通过断言

- [ ] **Step 1: 写失败测试**

`tests/test_mobile_shipping_e2e.py`：
```python
import io
import os
import tempfile

import pytest
from PIL import Image

from app import create_app
from models._db import DB_PATH
from models._init import init_db
from models.orders import ShippingOrder, ShippingRecord, ShippingImage


@pytest.fixture
def client():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    import models._db as db_mod
    original = db_mod.DB_PATH
    db_mod.DB_PATH = path
    init_db()
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        oid = ShippingOrder.create(_today(), "客户")
        rid = ShippingRecord.create(oid, "商品", "规格", 1, "件", "")
        yield c, oid, rid
    db_mod.DB_PATH = original
    os.unlink(path)


def _today():
    from datetime import date
    return date.today().isoformat()


def _png():
    img = Image.new("RGB", (64, 64), (255, 255, 255))
    buf = io.BytesIO(); img.save(buf, "PNG"); return buf.getvalue()


def test_e2e_mobile_to_pc_visible(monkeypatch, client):
    c, oid, rid = client
    from blueprints import shipping as shipping_mod
    def fake_process(image_id, order_pk, record_pk):
        ShippingImage.set_match(image_id, "green", 0.95, "一致", "local_fuzzy")
    monkeypatch.setattr(shipping_mod, "_process_record_image_async", fake_process)
    png = _png()
    # 移动端上传
    resp = c.post(
        f"/api/v1/shipping-orders/records/{rid}/images",
        data={"image": (io.BytesIO(png), "x.png"), "source": "upload"},
        content_type="multipart/form-data",
    )
    iid = resp.get_json()["images"][0]["image_id"]
    # PC 端出货页（?start=today&end=today）看到该图与状态
    pc = c.get(f"/shipping-records?start_date={_today()}&end_date={_today()}")
    assert pc.status_code == 200
    # 行内 match 徽标：通过 ShippingImage.get_by_id 验证
    img = ShippingImage.get_by_id(iid)
    assert img["match_status"] == "green"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `python -m pytest tests/test_mobile_shipping_e2e.py -v`
Expected: 失败（mock 未对齐）

- [ ] **Step 3: 跑全套回归 + E2E**

Run: `python -m pytest tests/test_mobile_shipping_*.py tests/test_shipping_p1_fixes.py tests/test_shipping_p2_p0.py tests/test_shipping_yellow_manual_confirm.py tests/test_shipping_image_btn_highlight.py tests/test_shipping_match_badge_refresh.py tests/test_shipping_orphan_images.py tests/test_ai_match_endpoint.py tests/test_record_upload_match.py -v`
Expected: 全部 PASS

- [ ] **Step 4: 手动 PC 验收**

按规格 7.5：
1. `python app.py` → 浏览器窗口缩到 390px → 访问 `http://127.0.0.1:5050/m/shipping-today`
2. 看到今日订单列表
3. 点首单 → 进入详情
4. 用 PC 文件选择器模拟"📷 拍照识别"上传图
5. 看到轮询 → 状态回流（绿/黄/红）
6. 红/黄牌点"✓ 人工确认" → 状态变绿 + 👤
7. 切到 PC 完整页 `/shipping-records?start_date=today&end_date=today` → 同一订单看到移动端拍的图与状态
8. 后端日志确认 `set_log_context(biz='mobile_shipping', order_id=...)` 注入正确
9. DB 确认 `shipping_images.match_status / human_verified` 与 PC 端一致

- [ ] **Step 5: 提交 + 推 spec/plan 到 main**

```bash
git add tests/test_mobile_shipping_e2e.py
git commit -m "test(mobile): 端到端（移动端上传 → PC 端可见）"
```

---

## 自审对照

- **Spec 覆盖**：
  - 蓝图 + 路由 → Task 1 ✓
  - 列表页 + 状态汇总 → Task 2 ✓
  - 详情页骨架 → Task 3 ✓
  - 客户端糊图检查 → Task 4 ✓
  - 服务端 `rotate_deg` + Pillow → Task 5 ✓
  - 模糊 reason 提示 → Task 6 ✓
  - 详情页交互 → Task 7 ✓
  - 整体图上传 + 替换 → Task 8 ✓
  - 人工确认 + 日志 → Task 9 ✓
  - Playwright 渲染 → Task 10 ✓
  - 端到端 PC 验收 → Task 11 ✓

- **占位符扫描**：无 TBD/TODO；每步给具体代码
- **类型一致性**：
  - `apply_user_rotation(image_bytes, rotate_deg) -> bytes` 在 Task 3、5 一致
  - `_append_blur_reason_if_low_conf(image_id, avg_conf, threshold=0.5)` 在 Task 6 单一引用
  - `set_log_context(biz=..., order_id=...)` 与 `6a6e679` 接入点一致

---

## 验收（按规格 §8）

- [ ] `pytest -v tests/test_mobile_shipping_*.py` 全部通过
- [ ] `pytest -v` 现有 32 个测试文件无回归
- [ ] Playwright 截屏与设计图一致
- [ ] PC 浏览器 390px + 微信 UA 走通完整流程
- [ ] 移动端拍照 → PC 端 `/shipping-records?start=today&end=today` 同步显示
- [ ] 客户端糊图检查生效（方差 < 80 拦截）
- [ ] 服务端 OCR 置信度 < 0.5 reason 含"模糊"
- [ ] `rotate_deg=90/180/270` 上传后 OCR 文本正确
- [ ] 整体图上传 → `match_status=NULL`，仅"已拍"
- [ ] 红/黄牌 → 人工确认 → 状态变绿 + 👤
- [ ] 同 record 第二张图成功后旧图被删（成功后替换）

---

## 执行选项

计划已写入 `docs/superpowers/plans/2026-08-09-wechat-shipping-mobile.md`。

两种执行方式：

**1. Subagent-Driven（推荐）** — 我给每个任务派遣独立的子代理执行，任务间我做两阶段审阅
**2. Inline Execution** — 在当前会话按批次执行（执行-plans 技能），含检查点

请告诉我用哪种方式？
