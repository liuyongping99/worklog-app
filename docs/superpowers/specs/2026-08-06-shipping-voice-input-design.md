# 出货订单语音录入功能 — 设计文档

- 日期:2026-08-06
- 范围:V1 仅出货订单(`shipping-orders`)
- 目标用户:单人外贸装柜 / 仓库管理(项目现状)

---

## 1. 概述

为出货订单新增「语音录入」功能,用户用自然口语说出「客户 + 商品明细」,系统自动识别并填充到新建订单的明细表格中。

### 1.1 设计目标

1. **口语极简输入**:`白中磅1.2 二十支`、`环保杂胶 50 个` 这种业务口语短词必须能命中系统商品库
2. **可维护口语短语映射表**:用户使用过程中**自动积累**口语短语到商品的映射,长期看越来越准
3. **不依赖 LLM 凭空识别商品规格**:DeepSeek 不知道系统的 1100+ SKU 详细规格,口语短语到商品的映射必须有 product 表/fuzzy 兜底
4. **冷启动可用**:首次使用不需要任何预配置,系统从零数据开始也能跑
5. **离线降级**:外部 API 失败时不卡死,降级到规则路径

### 1.2 非目标(V1 不做)

- 入库订单 / 装柜订单的语音录入(V1.1 再扩展,共享 `_smart_add_modal`)
- 分行录入(逐行报品名)(V1.2)
- 多语种识别(仅中文)
- 长语音(> 60s)流式识别

---

## 2. 架构与数据流

```
┌────────┐                 ┌──────────────┐                ┌─────────────┐
│ 浏览器 │                 │ Flask 后端   │                │ 外部服务    │
│        │                 │              │                │             │
│ ① 点击🎙│                │              │                │             │
│ ② 录音 │                 │              │                │             │
│ ③ POST │                 │              │                │             │
│  /voice/recognize       │              │                │             │
│ (audio blob)            │              │                │             │
│ ────────────────────►  │              │                │             │
│        │                │ ④ ffmpeg 转 PCM(16k/16bit/单声道)        │
│        │                │              │                │             │
│        │                │ ⑤ 调百度短语音 API            │             │
│        │                │ ──────────────────────────►  │             │
│        │                │ ◄──── 文字结果 ────────────── │             │
│        │                │              │                │             │
│        │                │ ⑥ 调 DeepSeek 切句(类目树 + 口语)         │
│        │                │ ──────────────────────────►  │             │
│        │                │ ◄──── 结构化 JSON ─────────── │             │
│        │                │              │                │             │
│        │                │ ⑦ 口语映射表查 → RapidFuzz → LLM 兜底    │
│        │                │              │                │             │
│        │                │ ⑧ 返回候选 JSON               │             │
│        │ ◄────────────  │              │                │             │
│ ⑨ 弹框显示候选         │              │                │             │
│ ⑩ 用户点击确认/修改    │              │                │             │
│ ⑪ POST /voice/confirm │              │                │             │
│ ────────────────────►  │              │                │             │
│        │                │ ⑫ 写口语映射表(use_count + 1)│             │
│        │                │ ⑬ 批量插入明细行              │             │
│        │ ◄──── 成功 ──  │              │                │             │
└────────┘                 └──────────────┘                └─────────────┘
```

**关键路径**:
1. 音频 → 百度 API → 文字(必须,无可替代)
2. 文字 + 类目树 → DeepSeek 切句(必须,LLM 理解口语结构)
3. phrase_part → 口语映射表查(快路径)
4. 未命中 → RapidFuzz 在 product 表搜(本地,免费)
5. RapidFuzz 无候选 → DeepSeek 兜底(带 Top 30 候选上下文)

---

## 3. 数据模型

### 3.1 新表 `voice_phrase_mapping`(口语短语 → 商品映射)

| 字段 | 类型 | 约束 | 说明 |
|---|---|---|---|
| `id` | INTEGER | PK AUTOINCREMENT | 自增 |
| `phrase` | TEXT | NOT NULL | 归一化后的口语短语(不含规格数字),如 `白中磅`、`环保杂胶` |
| `product_id` | INTEGER | NOT NULL FK → `product(id)` ON DELETE CASCADE | 关联商品 |
| `spec_hint` | TEXT NULL | | 默认规格提示(如 `中性`),可空 |
| `source` | TEXT | NOT NULL | `'user_confirmed'` / `'llm_fallback'` |
| `use_count` | INTEGER | DEFAULT 1 | 累计确认次数 |
| `status` | TEXT | DEFAULT `'pending'` | `'pending'` / `'active'` |
| `created_at` | TEXT | NOT NULL | 创建时间(ISO 8601) |
| `last_used_at` | TEXT | NOT NULL | 最近使用时间(ISO 8601) |

**唯一索引**:`UNIQUE(phrase, product_id)` — 同一短语→同一商品只一条

**归一化规则**:`phrase = strip() + lower()`,避免 `环保杂胶` / `环保杂胶 ` / ` 环保杂胶` 被当作三条

### 3.2 启用规则(在 Python 端实现)

| 触发 | 行为 |
|---|---|
| 用户在弹框选了候选 | `upsert(phrase, product_id, spec_hint)`:存在 → `use_count + 1`,`last_used_at = now`;不存在 → 新建,`use_count=1, status='pending'` |
| `use_count` 达到 2 | 该条 `status = 'active'`,下次查询直接命中 |
| 管理员手工批准 | 该条 `status = 'active'` |
| 管理员手工停用 | 该条 `status = 'pending'` |
| 管理员删除 | 行删除 |

### 3.3 查询逻辑

```python
# 优先查 active,fallback pending,合并返回
def lookup_phrase(phrase):
    rows_active = SELECT * FROM voice_phrase_mapping WHERE phrase=? AND status='active'
    rows_pending = SELECT * FROM voice_phrase_mapping WHERE phrase=? AND status='pending'
    return rows_active + rows_pending  # active 排前
```

### 3.4 现有表不变

`product` / `product_categories` / `shipping_orders` / `shipping_records` 全部不动。类目树和商品库通过现有 ID 关联读取。

---

## 4. 核心 Pipeline

### 4.1 端点

| 端点 | 方法 | 说明 |
|---|---|---|
| `/api/v1/voice/recognize` | POST | 接收音频 → 返回候选 JSON |
| `/api/v1/voice/confirm` | POST | 用户在弹框确认 → 写映射表 + 返回最终结构供批量插入 |
| `/api/v1/voice/mappings` | GET | 口语映射管理列表(产品页 tab 用) |
| `/api/v1/voice/mappings` | POST | 新增口语映射 |
| `/api/v1/voice/mappings/<id>` | PATCH | 编辑/启用/停用 |
| `/api/v1/voice/mappings/<id>` | DELETE | 删除 |

### 4.2 `/api/v1/voice/recognize` 流程

```
1. 接收 multipart/form-data:
     - audio: 音频 blob(MediaRecorder 默认 webm/opus)
     - mode: 'full' | 'line'(V1 仅 'full')

2. 保存到临时文件 audio_xxx.webm

3. ffmpeg 转 16kHz 16bit 单声道 PCM:
     ffmpeg -i audio_xxx.webm -ar 16000 -ac 1 -f wav audio_xxx.wav

4. 调百度短语音 API → recognized_text
     - 失败重试 1 次,失败返回 500 + 错误提示

5. 删除临时文件

6. 调 DeepSeek 切句:
     - 输入:商品分类树(level=4 108 条叶子类目) + recognized_text
     - 输出:[
         {phrase_part: "白中磅", spec_part: "1.2", quantity: 20, unit: "支"},
         {phrase_part: "环保杂胶", spec_part: null, quantity: 50, unit: "个"}
       ]
     - 失败降级:用规则切句(顿号/逗号/空白/`和`/`与` 分隔)

7. 后端验证 phrase_part:
     - 在 level=4 类目名 fuzzy 验证(score > 60)
     - 失败 → phrase_part = 整段口语,spec_part = null,走全 fuzzy

8. 对每个 item:
     a. 查 voice_phrase_mapping WHERE phrase=phrase_part → candidates += [{source:'mapping', use_count}]
     b. RapidFuzz 在 product 表搜 Top 5 → candidates += [{source:'fuzzy', score}]
     c. 若 candidates 为空:调 DeepSeek 兜底
        - 输入:phrase_part + spec_part + 该 phrase 历史映射 + product Top 30 fuzzy 候选(同类目优先)
        - 输出:1 个 product_id(或 null)
        - 候选加入 candidates(source='llm_fallback')

9. 返回完整 JSON
```

### 4.3 DeepSeek 切句 Prompt 模板

```
以下是本系统的商品分类树(level=4 叶子类目,共 108 条,作为切句基准):

白磅布三文治
黑磅布三文治
B级 磅布三文治
7P环保杂胶
7P环保三文治
7P环保磅布三文治
7P环保磅布杂胶
7P环保纯胶
7P环保路华里
7P环保LB鱼鳞布
7P环保HA猪皮纹
7P环保毛底底胶
7P环保里布革
7P环保高弹
RA弹力胶
订做弹力胶
弹力胶1.4
弹力胶1.5
硬弹力胶
加硬黑55度1.4
加硬黑55度1.5
卷材黑色按支
卷材白色(斤)
卷材白色(码)
...
[共 108 条]

用户说:「丰泽出货,白中磅1.2二十支,环保杂胶50个」

请按上述类目的口语化简写切分为 JSON 数组:
[
  {
    "phrase_part": "白中磅",
    "spec_part": "1.2",
    "quantity": 20,
    "unit": "支"
  },
  {
    "phrase_part": "环保杂胶",
    "spec_part": null,
    "quantity": 50,
    "unit": "个"
  }
]

规则:
1. phrase_part 必须是上述类目的口语化简写(如「白中磅」对应类目「白磅布三文治」)
2. spec_part 仅含规格数字/单位(如 "1.2"、"中性"、"黑色"),无则 null
3. quantity 可为 null(用户只报品名未报数量)
4. unit 可为 null,标准单位:支/y/码/kg/桶/件/箱/张/块/卷/令/个
5. 中文数字转阿拉伯("二十" → 20,"五十" → 50)
6. 严格输出 JSON,不要其他文字
```

### 4.4 响应 JSON Schema

```json
{
  "success": true,
  "recognized_text": "丰泽出货,白中磅1.2二十支,环保杂胶50个",
  "customer": "丰泽",
  "items": [
    {
      "phrase_part": "白中磅",
      "spec_part": "1.2",
      "quantity": 20,
      "unit": "支",
      "candidates": [
        {"product_id": 169, "product_name": "白磅布三文治", "specification": "1.2中性", "score": 92.5, "source": "mapping"},
        {"product_id": 170, "product_name": "黑磅布三文治", "specification": "1.2黑色", "score": 78.3, "source": "fuzzy"}
      ]
    },
    {
      "phrase_part": "环保杂胶",
      "spec_part": null,
      "quantity": null,
      "unit": null,
      "candidates": [
        {"product_id": 143, "product_name": "7P环保杂胶", "specification": "黑色", "score": 88.1, "source": "mapping"}
      ]
    }
  ],
  "needs_disambiguation": true
}
```

`needs_disambiguation`:至少一个 item 有 ≥2 候选 → 前端必须弹框。false 时前端可一键直接插入。

### 4.5 `/api/v1/voice/confirm` 流程

```
1. 接收:
   {
     order_id: <新建的出货订单 ID>,
     customer: "丰泽",
     items: [
       {
         phrase_part: "白中磅",
         product_id: 169,
         specification: "1.2中性",
         quantity: 20,
         unit: "支",
         source: "mapping" | "fuzzy" | "llm_fallback" | "manual"
       },
       ...
     ]
   }

2. 对每个 item:
     a. upsert voice_phrase_mapping(phrase_part, product_id):
        - 存在 → use_count + 1, last_used_at = now
        - 不存在 → 插入, use_count=1, status='pending', source='user_confirmed'
        - 若 source='llm_fallback' → 改为 'llm_fallback'
        - use_count 达到 2 → status='active'

3. 对每个 item:
     - POST 到 /api/v1/shipping-orders/<order_id>/records
     - (复用现有明细行创建逻辑)

4. 返回:
   {
     success: true,
     order_id: ...,
     records: [<插入的明细行对象>],
     mappings_updated: [<更新的映射条目>]
   }
```

---

## 5. 前端 UI

### 5.1 入口

`templates/shipping-records.html` 的「新建出货单」表单右侧加个 🎙️ 按钮。

```html
<button type="button" id="voiceInputBtn" class="voice-btn">🎙️ 语音录入</button>
```

### 5.2 弹框结构

**录音视图**:
- 顶部:标题「🎙️ 语音录入」
- 中间:大圆形「按住说话」按钮(或点一下开始/再点停止)
- 实时显示录音时长 + 波形
- 底部:「❌ 取消」按钮

**候选确认视图**(识别完成后切换):
- 顶部:「已识别文字」折叠区(供用户对照)
- 中间:每个 item 一行,字段:
  - 口语短语(只读,灰色)
  - 候选下拉(显示 `商品 - 规格`,默认选 Top 1,可切换)
  - 规格下拉(候选商品选定后,该商品下的所有 specification 选项)
  - 数量输入框(默认填识别结果)
  - 单位输入框(默认填识别结果)
- 行底部:「❌ 删除该行」/「➕ 手动添加行」
- 弹框底部:「❌ 取消」/「✅ 确认全部」

**无数量兼容**:
- 数量输入框为空 → 行标红 + 显示「⚠️ 请补充数量」
- 「确认全部」按钮在该行未填数量前禁用(防止漏填)

### 5.3 弹框状态机

```
[关闭]
  │ 点击 🎙️ 按钮
  ▼
[录音中] ─── 用户点击停止 ──► [识别中] ─── 成功 ──► [候选确认]
  │                              │                     │
  │                              │ 失败 ─► 提示重录    │ 用户确认 ─► 批量插入 ─► [关闭]
  │                              │                     │
  └──── 用户取消 ────────────────┴─── 用户取消 ────────┘
```

### 5.4 口语短语管理 UI

`templates/products.html` 加 tab「🎙️ 口语短语映射」。

- 列表:`phrase` / `product_name` / `spec_hint` / `use_count` / `status` / `source` / `last_used_at`
- 搜索框(按 phrase / product_name)
- 行操作:✏️ 编辑 / ✅ 启用 / ⏸ 停用 / 🗑️ 删除
- 「➕ 新增」按钮(选 product + 填 phrase + 选 spec_hint)

---

## 6. 自动维护规则详解

### 6.1 写入时机

| 时机 | 写入内容 |
|---|---|
| 用户在弹框选了非手工候选 | `upsert(phrase, product_id, spec_hint)`,`source='user_confirmed'` |
| 用户选的是 LLM 兜底候选 | `upsert(phrase, product_id, spec_hint)`,`source='llm_fallback'` |
| 用户选的是手工挑选的商品 | 同上,`source='user_confirmed'`(用户选了就算确认) |
| 用户跳过某个 item(删除该行) | 不写 |

### 6.2 启用路径

**自动启用**:`use_count ≥ 2` → `status='active'`。下次该 phrase 进来直接命中,不再 fuzzy。

**人工启用**:产品管理页 → 口语短语映射 tab → 点 ✅ → `status='active'`。

**优先级**:`active` 优先匹配,`pending` 兜底返回。

### 6.3 编辑/删除路径

| 操作 | 影响 |
|---|---|
| 删除映射 | 该 phrase 下次回到未命中状态,重新 fuzzy/兜底 |
| 启用/停用 | 立即生效,影响下次查询 |
| 编辑 phrase / product_id | 实际是 upsert,旧记录保留 |

---

## 7. 错误处理

| 阶段 | 失败场景 | 处理 |
|---|---|---|
| 录音 | 浏览器无麦克风权限 | 弹框顶部红字提示「请允许麦克风权限」+ 不发请求 |
| 录音 | MediaRecorder 不支持(老浏览器) | 弹框顶部红字提示「请用最新版 Chrome/Edge」 |
| 上传 | 音频 > 10MB | 400 返回,提示压缩 |
| 转码 | ffmpeg 不在 PATH | 500 返回,提示「服务端需安装 ffmpeg」 |
| 百度 | 401 invalid credentials | 500 返回,提示「百度 API key 无效」 |
| 百度 | 识别失败(返回空文字) | 弹框提示「未能识别,请重新录入」+ 保留录音按钮 |
| 百度 | 限流(429) | 重试 1 次,失败则提示「请稍后再试」 |
| DeepSeek 切句 | timeout / JSON 格式错 | 降级:用规则切句(顿号/逗号/空白/`和`/`与` 分隔) |
| RapidFuzz | 无候选(score 全 < 60) | 进入 LLM 兜底 |
| LLM 兜底 | 仍无候选 | 该 item 显示「❌ 无匹配商品」,让用户「手动选择商品」按钮 |
| 映射表 | 数据库写入失败 | 不阻断,弹 warning,继续走批量插入 |
| 弹框 | 用户中途关闭 | 不写映射表,不插入,静默退出 |

---

## 8. 测试策略

### 8.1 单元测试(主要)

```python
# tests/test_voice_phrase_normalize.py
def test_phrase_normalize_strip():
    assert normalize_phrase('  环保杂胶 ') == '环保杂胶'
    assert normalize_phrase('白中磅') == '白中磅'

# tests/test_voice_mapping_crud.py
def test_use_count_threshold_activates():
    VoiceMapping.upsert('白中磅', product_id=169)  # pending, count=1
    m = VoiceMapping.upsert('白中磅', product_id=169)  # count=2
    assert m.use_count == 2
    assert m.status == 'active'

def test_unique_constraint():
    VoiceMapping.upsert('环保杂胶', product_id=143)
    # 同 phrase + 同 product_id 第二次 upsert 不应重复创建
    m2 = VoiceMapping.upsert('环保杂胶', product_id=143)
    assert VoiceMapping.count_by_phrase('环保杂胶') == 1

# tests/test_voice_fuzzy.py
def test_phrase_to_category_fuzzy():
    candidates = fuzzy_match_category('白中磅', top_n=3)
    assert candidates[0]['name'] == '白磅布三文治'

# tests/test_voice_pipeline.py (mock 外部 API)
def test_llm_fallback_only_when_fuzzy_fails(monkeypatch):
    # fuzzy score < 60 → 调 LLM
    # fuzzy score ≥ 60 → 不调 LLM
    call_count = mock_deepseek_call_count(monkeypatch)
    result = recognize_pipeline(audio='fixture1.webm', mock_engine=MockEngine(top_score=80))
    assert call_count == 0  # fuzzy 解决,没调 LLM

def test_no_quantity_keeps_compatibility():
    result = recognize_pipeline(audio='fixture_no_qty.webm', mock_engine=...)
    assert result['items'][0]['quantity'] is None
```

### 8.2 集成测试

- 用录好的固定 fixture 音频(`tests/fixtures/voice/*.webm`)
- mock 百度 API + DeepSeek API
- 验证完整 pipeline:音频 → JSON → confirm → 写库

### 8.3 端到端测试(Playwright)

- 启动 dev server
- 浏览器访问 `/shipping-records`
- 点击 🎙️ → 播放 fixture 音频 → 等待候选 → 选第一个 → 点确认
- 验证明细行已插入数据库 + 口语映射表新增条目

### 8.4 Fixture 准备

- `tests/fixtures/voice/` 存 5-10 个 .webm 短音频(覆盖不同口音、不同复杂度)
- `tests/fixtures/voice/expected.json` 存每个音频对应的期望解析结果

---

## 9. 部署与配置

### 9.1 .env 新增字段

```bash
# 百度短语音识别
BAIDU_API_KEY=your_api_key
BAIDU_SECRET_KEY=your_secret_key
BAIDU_VOICE_TOKEN_URL=https://aip.baidubce.com/oauth/2.0/token
BAIDU_VOICE_API_URL=https://vop.baidu.com/server_api

# ffmpeg 路径(Windows)
FFMPEG_PATH=C:\Program Files\ffmpeg\bin\ffmpeg.exe
```

### 9.2 新依赖

- **ffmpeg**:系统级二进制,项目不打包
  - Windows:从 https://ffmpeg.org/download.html 下载,加入 PATH 或配置 FFMPEG_PATH
  - Linux:`apt install ffmpeg`
- **Python 端**:不新增 pip 依赖(`requests` + `subprocess` 足够)

### 9.3 安装文档(README 增补)

```markdown
## 语音录入功能(出货订单)

### 前置依赖
1. 安装 ffmpeg:https://ffmpeg.org/download.html
   - Windows:下载 exe,加入 PATH
   - Linux:apt install ffmpeg
2. 申请百度短语音识别 API:
   - 访问 https://console.bce.baidu.com/ 创建应用
   - 开通「短语音识别」服务
   - 拿到 API Key 和 Secret Key

### 配置 .env
添加以下字段:
BAIDU_API_KEY=xxx
BAIDU_SECRET_KEY=yyy
FFMPEG_PATH=/path/to/ffmpeg

### 使用
Chrome / Edge 桌面版访问出货页,允许麦克风权限,点击「🎙️ 语音录入」开始。
```

---

## 10. 风险与开放问题

### 10.1 风险

| 风险 | 缓解 |
|---|---|
| 百度 API 限流(每日 5 万次) | 单人用,实际每天 < 100 次,远低于限额 |
| ffmpeg 安装遗漏 | README 醒目位置提示;启动时检查 ffmpeg 可用性,缺失则禁用语音按钮 |
| 口语短语表累积过多(~500+ 条) | V1 暂不优化;V2 可考虑按 LRU / 使用频率清理 |
| 浏览器兼容性(仅 Chrome/Edge 支持 webm 录音) | UI 提示「请用 Chrome/Edge」 |
| LLM 兜底偶尔选错规格 | 弹框设计就是让用户改,兜底只是建议 |

### 10.2 开放问题(V2/V3 解决)

- **分行录入**(V1.2):逐行报品名
- **三订单通用化**(V1.1):入库/装柜也用,整合到 `_smart_add_modal`
- **Top N 高频口语短语 动态打包进切句 prompt**(V2):`use_count ≥ 5` 且最近 30 天的 Top 30 短语作为 few-shot
- **LLM 兜底带类目片段**(V2):给 LLM 同 phrase 的同类目 product 候选(不是 Top 30 全表)

---

## 11. 工期估算

| 阶段 | 内容 | 估算 |
|---|---|---|
| 1. 数据模型 + 迁移 | 新建 `voice_phrase_mapping` 表 + 建索引 + 配套 model | 半天 |
| 2. 后端 pipeline | 百度客户端 + DeepSeek 切句 + RapidFuzz + LLM 兜底 | 3 天 |
| 3. API 端点 | `/voice/recognize` + `/voice/confirm` + 管理 CRUD | 1 天 |
| 4. 前端弹框 | 录音 + 候选确认 + 集成到出货页 | 2 天 |
| 5. 产品页 tab | 口语短语映射管理 UI | 1 天 |
| 6. 测试 | 单元 + 集成 + 端到端 | 2 天 |
| 7. 文档 | README + 部署说明 | 半天 |
| **合计** | | **~10 工作日 / 2 周** |