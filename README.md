# 丰源工作台 (Worklog App)

Flask 多页面工作记录管理系统，用于日常工作经验、错误记录、出货/入库/装柜订单管理。

---

## 技术栈

| 组件 | 技术 |
|------|------|
| 后端 | Python 3.12 + Flask 3.1.3 |
| 数据库 | SQLite 3.45.3 |
| 前端 | 原生 JavaScript + Tailwind CSS |
| 运行 | http://127.0.0.1:5050 / http://192.168.1.149:5050 |
| 调试 | Debug 模式 ON，Debugger PIN: 231-151-848 |

---

## 页面清单（17个）

| 页面 | 路由 | 功能 |
|------|------|------|
| 首页 / 工作经验 | `/` `/experience` | 工作经验记录 CRUD |
| 错误经验 | `/errorlog` | 错误经验记录 CRUD |
| 待办事项 | `/todolist` | 待办清单（勾选完成/删除） |
| 公司通知 | `/notice` | 通知管理（排序、增删改） |
| 通知彩色版 | `/notice-color` | 彩色通知展示 |
| 工作流 | `/workflow` | 业务流程说明 |
| 仓库布局 | `/warehouse` | 仓库布局展示 |
| 计数看板 | `/count-tips` | 点数要点 |
| 换单指南 | `/huandan-guide` | 换单操作指南 |
| 计费提示 | `/billing-tips` | 开单要点 |
| 报价看板 | `/priceboard` | 码数报价 |
| 车辆维护记录 | `/vehicle-maintenance` | 车辆维保 CRUD |
| **出货记录** | `/shipping-records` | 出货订单 + 明细 + 图片 |
| **入库记录** | `/inbound-records` | 入库订单 + 明细 + 图片 |
| **装柜订单** | `/loading-orders` | 装柜订单 + 明细 + 图片 |


---

## 数据库表结构

### 核心业务表

**shipping_orders / shipping_records / shipping_images** — 出货
- `shipping_orders`: id, date, customer, order_num, is_locked
- `shipping_records`: id, order_id, product_name, specification, quantity, unit, remark
- `shipping_images`: id, order_pk, file_path, original_name, created_at

**inbound_orders / inbound_records / inbound_images** — 入库
- `inbound_orders`: id, date, is_locked
- `inbound_records`: id, order_id, product_name, specification, quantity, unit, remark
- `inbound_images`: id, order_pk, file_path, original_name, created_at

**loading_orders / loading_order_records / loading_order_images** — 装柜
- `loading_orders`: id, date, customer, order_num, is_locked
- `loading_order_records`: id, order_pk, product_name, specification, quantity, unit, remark
- `loading_order_images`: id, order_pk, file_path, original_name, created_at

### 其他表

| 表名 | 说明 |
|------|------|
| work_logs | 工作经验 |
| error_logs | 错误经验 |
| todo_items | 待办事项 |
| notices | 公司通知 |
| vehicle_maintenance | 车辆维护 |
| product_categories / products | 商品分类（未启用） |

---

## 路由汇总（63个）

```
/
/experience                                  # 工作经验
/experience/add
/experience/delete/<int:log_id>
/errorlog                                   # 错误经验
/errorlog/add
/errorlog/delete/<int:log_id>
/todolist                                   # 待办事项
/todolist/add
/todolist/toggle/<int:item_id>
/todolist/delete/<int:item_id>

/vehicle-maintenance                        # 车辆维护
/vehicle-maintenance/add
/vehicle-maintenance/delete/<int:record_id>
/shipping-records                           # 出货记录
/shipping-records/add
/shipping-records/add-item
/shipping-records/delete/<int:record_id>
/shipping-records/batch-add
/shipping-records/delete-items/<int:order_id>
/shipping-records/delete-order/<int:order_id>
/shipping-records/lock/<int:order_id>
/shipping-records/ai-recognize
/notice                                     # 公司通知
/notice/add
/notice/delete/<int:notice_id>
/notice/edit/<int:notice_id>
/notice/move_up/<int:notice_id>
/notice/move_down/<int:notice_id>
/priceboard / notice-color / workflow / warehouse / count-tips / huandan-guide / billing-tips
/inbound-records                            # 入库记录
/inbound-records/add
/inbound-records/add-item
/inbound-records/delete/<int:record_id>
/inbound-records/delete-order/<int:order_id>
/inbound-records/lock/<int:order_id>
/inbound-records/batch-add
/inbound-records/upload-image/<int:order_pk>
/inbound-records/delete-image/<int:image_id>
/loading-orders                             # 【新】装柜订单
/loading-orders/add
/loading-orders/add-record
/loading-orders/delete/<int:order_id>
/loading-orders/delete-record/<int:record_id>
/loading-orders/lock/<int:order_id>
/loading-orders/unlock/<int:order_id>
/loading-orders/upload-image/<int:order_pk>
/loading-orders/delete-image/<int:image_id>
/upload/<path:filepath>                     # 静态文件服务
```

---

## 核心功能实现

### 1. 订单锁定/解锁
- 每个订单有 `is_locked` 字段（0/1）
- 锁定后隐藏「添加明细」表单，删除按钮禁用
- 路由使用 Flask 表单提交（非 JS fetch），确保有确认弹窗和页面刷新

### 2. 图片上传
- 支持点击 📷 按钮打开弹窗，或 Ctrl+V 粘贴上传
- 图片存储于 `upload/YYYY-MM/` 目录
- 数据库存相对路径，通过 `/upload/<path:filepath>` 路由访问
- 图片画廊支持 1/2/3/4/5 列切换，偏好存入 localStorage

### 3. 图片点击放大预览
- 点击图片弹出模态框（`previewModal`），居中显示原图
- max-width: 90vw; max-height: 90vh

### 4. 批量添加明细
- `/shipping-records/batch-add` 和 `/inbound-records/batch-add`
- 支持多行文本输入，每行一条明细

### 5. AI 识别（出货记录）
- `/shipping-records/ai-recognize` 路由
- 上传图片 → AI 识别商品信息 → 自动填充表单

---

## ⚠️ 踩坑点 & 经验教训

### 1. Windows CRLF 导致 Python str.replace() 匹配失败
**问题：** `app.py` 里用 `query.replace('SELECT ...\n...', 'SELECT COUNT(*)')` 构建计数查询，但文件实际是 CRLF 换行，`replace` 永远匹配不上，返回 NoneType 错误。  
**解决：** 不依赖 `replace`，独立构建 `count_query` 的 WHERE 子句。  
**教训：** Windows 上 Python 源码中的多行字符串字面量包含 `\r\n`，不要用 `replace` 做大段 SQL 替换。

---

### 2. 文件写入必须用 `scripts/write_file.py`
**问题：** 内置 `write` 工具硬编码 utf-8 无 BOM，不支持跨平台编码适配。Windows Excel 打开 CSV 中文乱码，Windows `.bat` 含中文乱码。  
**解决：** 所有文本文件写入统一使用 `qclaw-text-file` skill 的 `scripts/write_file.py` 脚本。  
**教训：** 禁止内置 `write` 直接写最终目标文件（代码、配置、CSV、脚本等）。

---

### 3. PowerShell `&` 和 `)` 解析破坏 Python 代码
**问题：** PowerShell 中 `&` 是后台作业运算符，`)` 用于语法分组。执行 `python -c "..."` 时，Python 代码中的 `&` 或 `)` 会被 PowerShell 拦截，导致 ParserError。  
**解决：** 将 Python 代码写入 `.py` 文件，再用 `python <file>.py` 执行。  
**示例报错：**
```
The ampersand (&) character is not allowed. Wrap the value in double quotes.
```

---

### 4. GBK 终端中文乱码（Windows 默认编码）
**问题：** Windows 终端（PowerShell/cmd）默认编码为 GBK（cp936），Python `print()` 含中文或 emoji 时会触发 `UnicodeEncodeError`。  
**现象：**
```
UnicodeEncodeError: 'gbk' codec can't encode character '\U0001f4ce'
```
**解决：** 数据写入不受影响（SQLite 存 UTF-8），仅终端显示乱码，可忽略。如需正常显示，设置 `$OutputEncoding = [Text.Encoding]::UTF8`。

---

### 5. CSS `display:none` 内联样式与 JS `classList.add('show')` 冲突
**问题：** HTML 元素同时写了 `style="display:none"` 和内联样式，JS 通过 `classList.add('show')` 切换 CSS `.image-modal.show { display: flex }`，但内联样式优先级更高，弹框无法显示。  
**解决：** 移除 HTML 中的 `style="display:none"`，由 JS 通过 class 切换控制显隐。  
**教训：** class 切换控制显隐时，HTML 不应写死 `style="display:none"`。

---

### 6. Jinja2 模板中 `dict.get()` 在 `{% if %}` 条件中直接使用
**问题：** `{% if date_images.get(ns.last_date) %}` 在 Jinja2 中有时返回 unexpected results（尤其是 key 不存在时），且 `ns.last_date` 在循环结束后可能为空。  
**解决：** 先 `{% set prev_imgs = date_images.get(ns.last_date, []) %}`，再判断 `{% if prev_imgs %}`。  
**教训：** 避免在 `{% if %}` 条件中直接调用 `dict.get()`，应先 set 变量再判断。

---

### 7. Flask 路由顺序错误导致 404
**问题：** `@app.route('/loading-orders/delete/<int:order_id>')` 写在 `@app.route('/loading-orders/<int:record_id>')` 之后，Flask 会把 `delete` 误匹配为 `record_id`，导致 404。  
**解决：** 具体子路由（如 `/delete/<int:order_id>`）必须写在通配路由（如 `/<int:record_id>`）之前。  
**教训：** Flask 路由匹配按代码顺序，先定义的优先。`<int:xxx>` 会匹配所有数字路径，包括 `delete`。

---

### 8. 前端事件绑定：inline onclick → data 属性 + addEventListener
**问题：** `onclick="someFunc('{{ var }}')"` 在 Jinja2 渲染时，如果 `var` 含特殊字符会破坏 JS 语法。  
**解决：** 改用 `data-*` 属性 + `addEventListener`。  
**示例：**
```html
<!-- 错误 -->
<button onclick="deleteItem({{ id }})">删除</button>

<!-- 正确 -->
<button class="btn-delete" data-id="{{ id }}">删除</button>
<script>
document.querySelectorAll('.btn-delete').forEach(btn => {
    btn.addEventListener('click', () => deleteItem(btn.dataset.id));
});
</script>
```

---

### 9. 图片上传后 `location.reload()` 整页刷新
**问题：** 上传图片后调用 `location.reload()` 导致整页刷新，用户体验差。  
**优化方向（待实现）：** 上传成功后用 JS 局部刷新图片区域（如 `fetch` 重新渲染图片列表），不刷新整个页面。

---

## 启动方式

```bash
cd C:\Users\Administrator\worklog-app
python app.py
```

访问 http://127.0.0.1:5050

---

## 待办 / 待清理

- [x] 删除旧表 `loading_records` / `loading_images`（确认用户不需要后）
- [x] 删除旧模板 `templates/loading-records.html`
- [x] 删除迁移脚本（`migrate_old_loading_data.py` / `migrate_old_loading_images.py`）
- [ ] 图片上传后局部刷新（替代 `location.reload()`）
- [ ] `product_categories` / `products` 表启用（当前为空）

---

*最后更新：2026-06-06*
