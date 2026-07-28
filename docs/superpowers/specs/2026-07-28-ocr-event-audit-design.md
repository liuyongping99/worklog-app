# OCR 比对事件审计页 — 设计文档

- 日期:2026-07-28
- 页面:`/audit/ocr-events`
- 路线图定位:阶段 2「AI 验数 + 校对」的**评测数据消费层** —— 把 plan `2026-07-27-出货OCR比对历史记录` 沉淀的事件,以 prompt_version 维度聚合呈现 AI vs 人一致率,为后续优化提示词提供"看效果"界面
- 状态:待用户 review

## 1. 背景与目标

`ocr_match_event` 表已落地 7 task(2026-07-27 plan,本仓库 commit `212e2ef..0e5a456`),三类事件(record_ocr / ai_match / human_verify)以 append-only 形式留痕。但**还没有任何 UI** 让用户看到这些数据 —— 设计文档 §8 明确「不出货页前端 UI」。本设计填补这一缺口。

### 1.1 用途(已与用户确认)

主场景:**分析 prompt 效果(按 prompt_version 统计 AI 准确率)**

- 给定某 prompt_version,看 AI 与人工裁决的一致率
- 一致率高的版本:沿用
- 一致率低的版本:找具体 record 看 AI 错在哪类行 → 调 prompt → bump 版本号 → 重测
- 长期累积「AI vs 人裁决 delta 数据集」,未来可能用于离线训练/微调

次要场景(隐含支持):
- 「同一 record 全链路事件」查询(record_ocr → ai_match → human_verify 三连击)
- 离线导出(CSV)做 Excel 透视

### 1.2 目标 / 非目标

**目标**:
- 主表:按 prompt_version 聚合一致率 + 配对数 + 最后使用时间
- 钻入:点击聚合一行的「明细」,在同一页下半部分展开该 prompt_version 下每个 record 的 (ai_status, human_status, 一致?, 详情)
- 过滤:时间范围(start_date / end_date)+ event_type 多选 + prompt_version 多选
- 导出:CSV(全字段 + 当前过滤范围)
- 与现有 `shipping-records.html` 风格保持一致(Tailwind + vanilla JS)

**非目标(明确排除)**:
- 不做实时打分、不做 A/B 流量分发(纯只读)
- 不做 prompt 编辑器(那是另一个独立功能)
- 不做用户登录/权限细分(继承现有 `_require_login` 闸门即可)
- 不做图表(数字直接展示,够用;有需要再单独加)
- 不做「重新评估」(同一 record 不触发新 AI 调用 —— 数据是只读历史)
- 不动 `OcrMatchEvent` 已有方法(只新增读查询,不修改现有接口)

## 2. 总体架构

```
[页面加载 /audit/ocr-events]
   └→ 渲染 templates/audit-ocr-events.html(Jinja2 + base.html 继承)
   └→ 模板内置 JS 立刻 fetch /api/v1/audit/ocr-events/aggregate
      → 填充主表

[用户改过滤 / 点导出]
   └→ fetch 聚合端点 → 局部刷新主表
   └→ 或 window.location = '.../export.csv?...'

[点聚合一行的「明细」]
   └→ fetch /api/v1/audit/ocr-events/records?prompt_version=v1&...
      → 填充同页下半部分的钻入面板
```

事件读取是只读审计性质,不写任何表;沿用 `OcrMatchEvent` 已有模型方法 + 新增 3 个查询助手(聚合 / 配对 / 导出)。

## 3. 数据模型

不修改 `ocr_match_event` 表;新增 3 个查询助手放在新模块 `models/audit_query.py`(独立模块,跟 `models/orders.py` 平级,避免 `orders.py` 越长越大)。

### 3.1 新模块 `models/audit_query.py`

```python
class OcrEventAudit:
    """OCR 事件审计只读查询(不写库)。

    三类方法:
      - prompt_stats: 按 prompt_version 聚合一致率
      - record_pairs: 按 prompt_version 列每个 record 的配对详情
      - export_rows:  扁平全字段导出(供 CSV)
    """

    @staticmethod
    def prompt_stats(*, start_date=None, end_date=None,
                     prompt_versions=None) -> list[dict]:
        """返回 [{prompt_version, total_pairs, consistent_pairs,
                  inconsistent_pairs, null_pairs, consistency_rate,
                  last_used_at}, ...]
        按 last_used_at DESC 排序。
        """

    @staticmethod
    def record_pairs(*, prompt_version, start_date=None, end_date=None,
                     limit=500, offset=0) -> tuple[list[dict], int]:
        """返回 (rows, total)。
        rows: [{record_id, order_id, order_date, customer, product_name,
                specification, quantity, unit,
                ai_status, ai_reason, ai_time,
                human_status, human_reason, operator_name, human_time,
                is_consistent}, ...]
        total: 满足条件的总 record 数(用于分页提示)
        """

    @staticmethod
    def export_rows(*, start_date=None, end_date=None,
                    event_types=None, prompt_versions=None) -> list[dict]:
        """扁平返回每个事件全字段,供 CSV 导出。
        列顺序固定,与 CSV header 一一对应。
        """
```

### 3.2 模型层错误处理

- 三个方法内部都用 try/except + `current_app.logger.exception` + 抛回 `RuntimeError`(蓝图层捕获返 500)
- 不写库,无需 `_db_mod` 间接调用;直接 `from ._db import get_db` 即可(读路径无需 mock 拦截)

### 3.3 新模型是否需要 re-export 到 `models/__init__.py`?

是。在 `models/__init__.py` 加:
```python
from .audit_query import OcrEventAudit
# + __all__ 列表追加
```

## 4. 写库触发点

**无**。本页是只读审计,不写任何表。

## 5. API + 蓝图

### 5.1 新蓝图 `blueprints/audit.py`

```python
bp = Blueprint('audit', __name__)


@bp.route('/audit/ocr-events')
def audit_ocr_events():
    """渲染审计主页(Jinja2 模板 + base.html)"""
    return render_template('audit-ocr-events.html')


@bp.route('/api/v1/audit/ocr-events/aggregate')
def api_audit_aggregate():
    """聚合数据 JSON,供前端 fetch。"""
    params = _parse_common_filters()  # start/end/prompt_versions
    rows = OcrEventAudit.prompt_stats(**params)
    return jsonify({'success': True, 'rows': rows})


@bp.route('/api/v1/audit/ocr-events/records')
def api_audit_records():
    """钻入明细 JSON。"""
    prompt_version = request.args.get('prompt_version', '').strip()
    if not prompt_version:
        return jsonify({'success': False, 'error': 'prompt_version 必填'}), 400
    limit = min(int(request.args.get('limit', 500)), 2000)
    offset = max(int(request.args.get('offset', 0)), 0)
    start = request.args.get('start_date', '').strip() or None
    end = request.args.get('end_date', '').strip() or None
    rows, total = OcrEventAudit.record_pairs(
        prompt_version=prompt_version,
        start_date=start, end_date=end,
        limit=limit, offset=offset,
    )
    return jsonify({'success': True, 'rows': rows, 'total': total,
                    'limit': limit, 'offset': offset})


@bp.route('/api/v1/audit/ocr-events/export.csv')
def api_audit_export_csv():
    """导出 CSV,直接返 text/csv 文件流。"""
    start = request.args.get('start_date', '').strip() or None
    end = request.args.get('end_date', '').strip() or None
    event_types_csv = request.args.get('event_types', '').strip()
    prompt_versions_csv = request.args.get('prompt_versions', '').strip()
    event_types = [t for t in event_types_csv.split(',') if t] or None
    prompt_versions = [v for v in prompt_versions_csv.split(',') if v] or None
    rows = OcrEventAudit.export_rows(
        start_date=start, end_date=end,
        event_types=event_types, prompt_versions=prompt_versions,
    )
    # 生成文件名
    fname = f'ocr_events_{datetime.now().strftime("%Y%m%d_%H%M")}.csv'
    # CSV body
    import csv, io
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_EXPORT_FIELDS)
    writer.writeheader()
    for r in rows:
        writer.writerow({k: r.get(k, '') for k in _EXPORT_FIELDS})
    body = buf.getvalue().encode('utf-8-sig')  # BOM 让 Excel 识别 UTF-8
    return Response(
        body, mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename="{fname}"'},
    )


def _parse_common_filters():
    """解析 start/end/prompt_versions 公共过滤参数,统一入口。"""
    start = request.args.get('start_date', '').strip() or None
    end = request.args.get('end_date', '').strip() or None
    pv_csv = request.args.get('prompt_versions', '').strip()
    prompt_versions = [v for v in pv_csv.split(',') if v] or None
    # 日期格式校验
    for label, val in (('start_date', start), ('end_date', end)):
        if val:
            try:
                datetime.strptime(val, '%Y-%m-%d')
            except ValueError:
                raise BadRequest(f'{label} 格式错误,应为 YYYY-MM-DD')
    if start and end and start > end:
        raise BadRequest('开始日期不能晚于结束日期')
    return {'start_date': start, 'end_date': end, 'prompt_versions': prompt_versions}
```

### 5.2 蓝图注册

在 `app.py:create_app()` 加:
```python
from blueprints.audit import bp as audit_bp
app.register_blueprint(audit_bp)
```

### 5.3 导出 CSV 列顺序(`_EXPORT_FIELDS`)

固定 19 列,与 `ocr_match_event` 表 + `shipping_records` 关联字段一致:
```python
_EXPORT_FIELDS = [
    'event_id', 'created_at', 'event_type',
    'record_id', 'order_id', 'image_id',
    'product_name', 'specification',
    'ocr_text', 'ocr_engine',
    'prompt_version', 'ai_engine', 'prompt_payload',
    'ai_match_status', 'ai_match_score', 'ai_match_reason', 'ai_raw_response',
    'human_status', 'human_reason', 'human_verified_by',
]
```

## 6. 前端(`templates/audit-ocr-events.html`)

继承 `base.html`,放「运营信息」导航组(新增入口)。

### 6.1 页面结构

```html
{% extends 'base.html' %}
{% block content %}
<h1>OCR 比对事件审计</h1>

<!-- 过滤栏 -->
<section id="filters">
  <label>开始 <input type="date" id="start_date"></label>
  <label>结束 <input type="date" id="end_date"></label>
  <label>事件类型
    <select multiple id="event_types">
      <option value="record_ocr" selected>record_ocr</option>
      <option value="ai_match" selected>ai_match</option>
      <option value="human_verify" selected>human_verify</option>
    </select>
  </label>
  <label>prompt 版本 <select multiple id="prompt_versions"></select></label>
  <button id="btn_apply">应用</button>
  <button id="btn_export">导出 CSV</button>
</section>

<!-- 主表:聚合 -->
<section id="aggregate_panel">
  <table id="aggregate_table">
    <thead><tr>
      <th>prompt 版本</th><th>配对数</th>
      <th>一致</th><th>不一致</th><th>人工未核</th>
      <th>一致率</th><th>最后使用</th><th>操作</th>
    </tr></thead>
    <tbody></tbody>
  </table>
  <p id="aggregate_empty" hidden>暂无数据</p>
</section>

<!-- 钻入面板 -->
<section id="drill_panel" hidden>
  <h2>明细:<span id="drill_pv"></span></h2>
  <table id="records_table">
    <thead><tr>
      <th>record_id</th><th>订单</th><th>日期</th><th>客户</th>
      <th>品名</th><th>规格</th><th>数量</th>
      <th>AI 判定</th><th>AI 理由</th><th>AI 时间</th>
      <th>人工判定</th><th>人工理由</th><th>操作员</th><th>人工时间</th>
      <th>一致?</th>
    </tr></thead>
    <tbody></tbody>
  </table>
  <p id="records_pagination"></p>
</section>

<script>
// vanilla JS:fetch + DOM 局部刷新,跟 shipping-records.html 风格一致
async function loadAggregate() {
  const params = buildParams();  // 从 #filters 收集
  const resp = await fetch('/api/v1/audit/ocr-events/aggregate?' + params);
  const data = await resp.json();
  if (!data.success) { alert(data.error); return; }
  renderAggregateTable(data.rows);
}

function renderAggregateTable(rows) {
  const tbody = document.querySelector('#aggregate_table tbody');
  tbody.innerHTML = '';
  if (!rows.length) { document.querySelector('#aggregate_empty').hidden = false; return; }
  document.querySelector('#aggregate_empty').hidden = true;
  rows.forEach(r => {
    const tr = document.createElement('tr');
    const rate = r.consistency_rate;
    const cls = rate == null ? '' : (rate >= 80 ? 'rate-good' : rate >= 60 ? 'rate-mid' : 'rate-bad');
    tr.innerHTML = `
      <td>${escapeHtml(r.prompt_version)}</td>
      <td>${r.total_pairs}</td>
      <td>${r.consistent_pairs}</td>
      <td>${r.inconsistent_pairs}</td>
      <td>${r.null_pairs}</td>
      <td class="${cls}">${rate == null ? '-' : rate.toFixed(1) + '%'}</td>
      <td>${escapeHtml(r.last_used_at)}</td>
      <td><button class="btn-drill" data-pv="${escapeHtml(r.prompt_version)}">明细</button></td>
    `;
    tbody.appendChild(tr);
  });
}

async function drillDown(pv) {
  // 折叠展开 + fetch records
  const resp = await fetch(`/api/v1/audit/ocr-events/records?prompt_version=${encodeURIComponent(pv)}&${buildParams()}`);
  const data = await resp.json();
  if (!data.success) { alert(data.error); return; }
  document.querySelector('#drill_pv').textContent = pv;
  document.querySelector('#drill_panel').hidden = false;
  renderRecordsTable(data.rows);
  document.querySelector('#records_pagination').textContent =
    `显示 ${data.rows.length} / 共 ${data.total} 条`;
}

document.querySelector('#btn_export').onclick = () => {
  const params = buildParams();
  window.location = '/api/v1/audit/ocr-events/export.csv?' + params;
};

// 初始化
window.addEventListener('DOMContentLoaded', () => {
  loadAggregate();
  document.querySelector('#btn_apply').onclick = loadAggregate;
  document.addEventListener('click', e => {
    if (e.target.classList.contains('btn-drill')) {
      drillDown(e.target.dataset.pv);
    }
  });
});
</script>
{% endblock %}
```

### 6.2 一致率配色 CSS(加在 `static/css/app.css`)

```css
.rate-good { color: #16a34a; font-weight: 600; }   /* 绿 ≥ 80% */
.rate-mid  { color: #ca8a04; font-weight: 600; }   /* 黄 60-80% */
.rate-bad  { color: #dc2626; font-weight: 600; }   /* 红 < 60% */
```

### 6.3 导航入口(`templates/base.html`)

在「运营信息」分组下加一条:
```html
<a href="{{ url_for('audit.audit_ocr_events') }}" ...>OCR 事件审计</a>
```

## 7. 错误处理

| 场景 | HTTP | 响应 |
|---|---|---|
| `prompt_version` 缺失(records 端点) | 400 | `{success: false, error: 'prompt_version 必填'}` |
| 日期格式错 | 400 | `{success: false, error: 'start_date 格式错误,应为 YYYY-MM-DD'}` |
| 开始 > 结束 | 400 | `{success: false, error: '开始日期不能晚于结束日期'}` |
| 没事件 | 200 | `{success: true, rows: []}` + 前端显示空态 |
| 钻入超过 LIMIT | 200 | 返 LIMIT 条 + `total` 字段 + 前端提示「显示 N/共 M 条,用日期收窄」 |
| SQLite 报错 | 500 | `{success: false, error: '查询失败'}` + log.error 完整 traceback |

## 8. 迁移

无新表/无新列。零迁移。

## 9. 测试

`tests/test_audit_ocr_events.py`,7 用例:

| 用例 | 验证 |
|---|---|
| `test_prompt_stats_consistency_rate` | 写 4 个 record × 不同 (ai,human) 组合,验证一致率 66.7% (2/3 一致 + 1 null 不计入分母) |
| `test_prompt_stats_filters_by_date` | 时间范围外事件被排除 |
| `test_prompt_stats_filters_by_versions` | `prompt_versions=v1` 只返 v1 |
| `test_record_pairs_join_logic` | ai_match + human_verify 配对正确;只有 ai_match 没有 human_verify 的 record 不出现 |
| `test_record_pairs_latest_wins` | 同一 record 多次核查,latest human_verify 生效 |
| `test_export_csv_includes_all_fields` | 导出 CSV 含 19 列;UTF-8 BOM |
| `test_aggregate_no_events_returns_empty` | 空表返 `rows: []`,不报错 |

**不测**:
- 模板渲染(项目惯例;Jinja2 + Tailwind 视觉靠人工)
- CSV 大文件性能(N<10k 不优化)
- 过滤栏 UI 交互(纯前端,后端 API 已覆盖)

## 10. 风险 / 坑

1. **SQLite 窗口函数依赖**:SQLite 3.45.3 才完整支持 `ROW_NUMBER() OVER (...)`。项目已用 3.45.3 ✓。
2. **大表性能**:当前表约 ~1k 行(7 task 刚 commit,生产数据积累到 ~10k/年级别)。一次聚合查所有 prompt_version 数据量可控;钻入明细默认 LIMIT 500 兜底。
3. **CSV 大文件**:全字段导出若超过 10k 行,响应会 1-2 MB。浏览器直接下载没问题;不需要分页/流式。
4. **FK ON DELETE CASCADE 已生效**:删 record 时事件被清,聚合查自动反映(空数据时前端显示「暂无」)。
5. **`human_verified_by` 当前存的是 staff_id 不是名字**:前端展示需要 JOIN staff 表拿 `name`。在 `record_pairs` SQL 里 LEFT JOIN staff 一次拿 `operator_name`。

## 11. 待实现清单(供后续 writing-plans 展开)

1. `models/audit_query.py` + `OcrEventAudit` 类(3 个查询方法)
2. `models/__init__.py` re-export `OcrEventAudit`
3. `blueprints/audit.py` + 4 个路由
4. `app.py` 注册蓝图
5. `templates/audit-ocr-events.html`(继承 base.html)
6. `static/css/app.css` 加 `.rate-good/.rate-mid/.rate-bad`
7. `templates/base.html` 导航入口
8. `tests/test_audit_ocr_events.py` 7 用例

## 12. 已拍板决策记录

- 页面位置:`/audit/ocr-events`(新顶级路由,不挂 `/shipping-records` 下,因为它是审计而非业务)
- 主表维度:prompt_version(已与用户确认)
- 一致率算法:每 record 取 latest(ai_match) + latest(human_verify),一致 = ai_status == human_status,人工未核记录不计入分母(已与用户确认)
- 钻入:同页下半部分 fetch(已与用户确认方案 A)
- 导出 + 过滤:都加(已与用户确认)
- 不做:实时打分、图表、用户/权限细分、prompt 编辑器

## 13. 不在范围 / 待办

- 图表(KPI 卡片 / 趋势线)— 后续有需要再单独加
- 跨订单/record 维度的二级聚合 — 当前主表已够用
- 数据存档/归档策略 — 当前表数据量小,无需