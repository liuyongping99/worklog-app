# Pyright 静态类型检查接入 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在丰源工作台接入 pyright 静态类型检查,守住入口和迁移层(约 1000 行),捕获基线报告,为长期渐进式收紧类型检查打基础。

**Architecture:** 用项目根的 `pyrightconfig.json` 限定最小作用域(app.py + models/_db.py + models/__init__.py + models/_init.py),不开 strict 模式,先观察基线报告。基线报告存到 `log/pyright-baseline-<date>.txt`,作为后续优化对照。命令记录到 README,因项目无 CI 由用户手动跑。

**Tech Stack:** pyright 1.1.411(已在 `D:\Program Files\Nodecache\pyright` 通过 npm 全局安装)、Python 3.12、Flask 3.1.3

## Global Constraints

- **平台**: Windows 10,PowerShell/Git Bash 均可;pyright 命令走 npm 全局版,不引入 pip 包。
- **不引入 CI**: 项目无 `.github/` 等 CI 配置,所有验证通过本地命令。
- **不引入 strict 模式**: 基线阶段不开 strict,先看默认严格度下的报错量。
- **配置文件最小化**: pyrightconfig.json 只包含必要字段,每个字段必须有注释说明用意。
- **不动其他文件**: 本计划只新增 2 个文件 + 修改 1 个文件;不修改任何业务代码、不动 models/ 业务模块、不动 requirements.txt。
- **长期步骤不实施**: 用户已确认第三步(渐进式类型注解 + 渐进开 strict)是长期工作,本计划**不**涉及。

---

## File Structure

| 路径 | 类型 | 用途 |
|---|---|---|
| `pyrightconfig.json` | 新建 | pyright 项目配置,限定最小作用域 |
| `log/pyright-baseline-2026-08-05.txt` | 新建 | 基线检查报告(后续对照基线) |
| `README.md` | 修改 | 在「开发命令」章节加 pyright 一行命令 |

不动任何业务代码(`app.py` / `models/*.py` / `blueprints/*.py` 全部保持原样)。

---

## Task 1: 写 pyrightconfig.json(最小作用域,不开 strict)

**Files:**
- Create: `C:\Users\Administrator\worklog-app\pyrightconfig.json`

**接口约定:**
- 该文件被 `pyright` 命令(不带任何参数)自动加载,放在项目根即可。
- `include` 路径相对项目根解析。
- `exclude` 用 glob 模式。
- 不设置 `strict` 字段 → 走 pyright 默认级别(basic)。

**- [ ] Step 1: 写 pyrightconfig.json**

在项目根创建文件,内容如下(每个 key 都有注释解释用意):

```json
{
    // 限定检查范围:仅入口 + 数据库层 + re-export 层
    // 暂不覆盖 blueprints/(业务端点) 和 models/{basic,orders,...}.py(业务模型)
    // 因为这俩层大量 cursor.execute() 直返 dict,缺类型注解,纳入会刷大量噪音
    "include": [
        "app.py",
        "models/_db.py",
        "models/__init__.py",
        "models/_init.py"
    ],

    // 标准排除项
    "exclude": [
        "**/__pycache__",
        "**/.*",
        "_safe-snapshot",
        "tests",
        "node_modules",
        "upload",
        "log"
    ],

    // 项目用 Python 3.12 + Flask 3.1.3
    "pythonVersion": "3.12",

    // venv/系统库路径(可选,有 venv 时设;本项目无独立 venv,跳过)

    // 不开 strict —— 默认 basic 模式足以暴露明显错
    // 长期计划:逐步在 models/ 加类型注解后再开 strict
    "typeCheckingMode": "basic"
}
```

**- [ ] Step 2: 验证 JSON 合法**

跑(用 Git Bash 或 PowerShell):
```bash
cd "C:/Users/Administrator/worklog-app"
pyright --version
```

预期输出:`pyright 1.1.411`(或更高版本号)。

如果命令找不到,把 `D:\Program Files\Nodecache` 加到 PATH 或直接用绝对路径。

**- [ ] Step 3: 干跑一次(只验证配置加载,不保存报告)**

跑:
```bash
cd "C:/Users/Administrator/worklog-app"
pyright
```

预期:
- pyright 加载 `pyrightconfig.json`
- 输出形如 `Loading pyrightconfig.json from C:\Users\Administrator\worklog-app\pyrightconfig.json`
- 然后展示 N 个文件的诊断信息(错误数不重要,基线在 Task 2 记录)

**- [ ] Step 4: 提交**

```bash
cd "C:/Users/Administrator/worklog-app"
git add pyrightconfig.json
git commit -m "feat(dev): add pyright config (basic mode, entry+migration scope)"
```

---

## Task 2: 跑基线 + 保存报告

**Files:**
- Create: `C:\Users\Administrator\worklog-app\log\pyright-baseline-2026-08-05.txt`

**接口约定:**
- 报告文件名固定日期:`pyright-baseline-2026-08-05.txt`(后续基线变更时按新日期建新文件,旧的保留作历史)。
- 报告应包含完整诊断输出 + 一段总结头。
- 该文件不进 git(看 `.gitignore` 是否已忽略 `log/` —— 若已忽略,本任务不需要再处理)。

**- [ ] Step 1: 验证 log/ 在 .gitignore 中**

跑:
```bash
cd "C:/Users/Administrator/worklog-app"
git check-ignore log/pyright-baseline-2026-08-05.txt
```

预期输出:`log/pyright-baseline-2026-08-05.txt`(被忽略)。

如果没被忽略,**停**——说明 `log/` 没在 `.gitignore` 里,本任务先**不创建报告文件**,回到本计划外,先单独处理 `.gitignore`。这是为了避免意外提交诊断输出到 git。

**- [ ] Step 2: 跑 pyright 并保存报告**

跑:
```bash
cd "C:/Users/Administrator/worklog-app"
mkdir -p log
pyright > log/pyright-baseline-2026-08-05.txt 2>&1
```

预期:命令退出(无论 0/非 0),`log/pyright-baseline-2026-08-05.txt` 文件已生成。

**- [ ] Step 3: 在报告顶部插入总结头**

跑(用 Python 一行命令):
```bash
cd "C:/Users/Administrator/worklog-app"
python -c "
import datetime
header = f'''# Pyright Baseline Report
# Date: {datetime.date.today().isoformat()}
# Scope: app.py + models/_db.py + models/__init__.py + models/_init.py
# Mode: basic (no strict)
# Note: This is the baseline. Future changes should compare against this file.

'''
with open('log/pyright-baseline-2026-08-05.txt', 'r', encoding='utf-8') as f:
    body = f.read()
with open('log/pyright-baseline-2026-08-05.txt', 'w', encoding='utf-8') as f:
    f.write(header + body)
print('header inserted, total lines:', len((header+body).splitlines()))
"
```

预期:打印 `header inserted, total lines: <N>`,N 比原行数大 7。

**- [ ] Step 4: 读取报告,把摘要回贴给用户**

跑:
```bash
cd "C:/Users/Administrator/worklog-app"
head -20 log/pyright-baseline-2026-08-05.txt
echo "---"
tail -10 log/pyright-baseline-2026-08-05.txt
```

把头尾各 10-20 行贴回对话,告诉用户:
- 检查了多少个文件
- 报错总数(error/warning/info 三档分别多少)
- 几个最常见的错误类型(Top 3)

**(此步骤不写到 commit,只是对话沟通用)**

**- [ ] Step 5: 跳过 commit(报告文件被 .gitignore 忽略)**

不需要 `git add`,因为 `log/` 已被忽略。直接进入 Task 3。

---

## Task 3: 在 README.md 加 pyright 一行命令

**Files:**
- Modify: `C:\Users\Administrator\worklog-app\README.md`(找到「开发命令」章节,加一行)

**接口约定:**
- 在 README 现存的「开发命令」block 里(从 CLAUDE.md 看是放在文档靠前位置,实际行号以读到的内容为准)加一条 pyright 命令。
- 不动 README 其他内容,不破坏现有格式。

**- [ ] Step 1: 找到 README 中的开发命令章节**

跑:
```bash
cd "C:/Users/Administrator/worklog-app"
grep -n "pytest\|开发命令\|启动开发服务器" README.md | head -10
```

预期输出:看到 `pytest` 或 `启动开发服务器` 的行号,知道章节在 README 第几行。

**- [ ] Step 2: 读取上下文(确认为 markdown 列表结构)**

读 `README.md` 中包含 `启动开发服务器` 那一段(用 Step 1 拿到的行号,向上读 3 行,向下读 15 行):

例如用 Read 工具读 `offset=<行号>-3`, `limit=18`。

**- [ ] Step 3: 在 pytest 那行后面加一条 pyright 命令**

找到现有 `python -m pytest tests/ -v` 这一行,在它下面加一行:

```markdown
# 运行 pyright 静态类型检查(项目根的 pyrightconfig.json 自动加载)
pyright
```

如果 README 用的是 `bash` 代码块包裹命令,把这一行加到同一个代码块里。
如果 README 没有 `bash` 代码块而是裸文本,把这一行也保持文本格式。

**- [ ] Step 4: 验证 README 改动合理**

读改后的 README.md 那一段,确认:
- 排版没破(列表/代码块缩进正确)
- 新加的命令用与现有命令一致的格式(都是 `bash` 块或都是裸文本)
- 没改到其他无关行

**- [ ] Step 5: 提交**

```bash
cd "C:/Users/Administrator/worklog-app"
git add README.md
git commit -m "docs: add pyright command to README dev workflow"
```

---

## Task 4: 汇总 + 让用户验证

**Files:** 无(纯对话步骤)

**- [ ] Step 1: 列出本次 commit**

跑:
```bash
cd "C:/Users/Administrator/worklog-app"
git log --oneline -3
```

预期:看到本次新增的 2 个 commit:
- `feat(dev): add pyright config (basic mode, entry+migration scope)`
- `docs: add pyright command to README dev workflow`

**- [ ] Step 2: 让用户手动验证**

告诉用户:
- 命令:`pyright`(在项目根跑,任意时刻可手动触发)
- 报告路径:`log/pyright-baseline-2026-08-05.txt`(后续每次跑会写到 stdout,想保留重定向即可)
- 预期表现:每次跑应当**与基线报告一致或更少**(因为本次没改任何业务代码)
- 如果发现 pyright 命令找不到,确认 `D:\Program Files\Nodecache` 在 PATH 里

**- [ ] Step 3: 提醒长期路径(但不动手)**

告诉用户第三步(渐进式类型注解 + 渐进开 strict)是长期工作,**不在本次计划内**。等用户想推进时再单独启动计划。

**- [ ] Step 4: 完成**

无 commit,纯对话收尾。

---

## Self-Review Checklist(写完后自查)

- [x] **Spec 覆盖**: 用户提的 3 步 → Task 1(Task 1 = 步骤 1 的配置)+ Task 2(Task 2 = 步骤 1 的跑通 + 基线报告)+ Task 3(Task 3 = 步骤 2 的命令文档)。第三步明确在 Task 4 标为「不动手」。
- [x] **无 placeholder**: 没有「TODO」「类似 Task N」「实现细节待定」之类。
- [x] **类型一致**: 全程只用到 `pyrightconfig.json` / `log/pyright-baseline-2026-08-05.txt` / `README.md` 三个文件路径,Task 间一致。
- [x] **测试覆盖**: pyright 本身就是类型检查工具,无需另写单元测试。Task 2 Step 4 的「贴摘要」就是验收手段。
- [x] **commit 粒度**: Task 1 一个 commit,Task 3 一个 commit,Task 2 / 4 无 commit。粒度合理。
- [x] **不在本次范围**: 长期第三步(给 models/ 加类型注解)被显式排除,Task 4 Step 3 提醒但不实施。