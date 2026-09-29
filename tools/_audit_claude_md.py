# -*- coding: utf-8 -*-
"""审计脚本：产出 CLAUDE.md 更新所需的事实数据（路由表、表行数、蓝图端点分布）。"""
import os
import sys
import sqlite3
from collections import Counter, defaultdict

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

from app import create_app  # noqa: E402

app = create_app()
rules = list(app.url_map.iter_rules())

print("=" * 70)
print("总端点方法数 :", sum(len(r.methods - {"HEAD", "OPTIONS"}) for r in rules))
print("总 rule 数   :", len(rules))
print("=" * 70)

# 按 blueprint 分组
groups = defaultdict(list)
for r in rules:
    bp = r.endpoint.split(".")[0]
    methods = ",".join(sorted(r.methods - {"HEAD", "OPTIONS"}))
    groups[bp].append((sorted(r.methods - {"HEAD", "OPTIONS"}), str(r.rule)))

for bp in sorted(groups, key=lambda k: -len(groups[k])):
    print(f"\n### {bp}  ({len(groups[bp])} 条 rule)")
    for methods, rule in sorted(groups[bp], key=lambda x: x[1]):
        print(f"   {'/'.join(methods):<12} {rule}")

print("\n" + "=" * 70)
print("数据库表行数")
print("=" * 70)

DB = os.path.join(BASE, "worklog.db")
con = sqlite3.connect(DB)
con.row_factory = sqlite3.Row
tables = [r[0] for r in con.execute(
    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
print("表总数:", len(tables))
for t in tables:
    try:
        n = con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
    except Exception as e:
        n = f"ERR {e}"
    print(f"   {t:<36} {n}")

# 索引/视图
print("\n索引数:", con.execute(
    "SELECT COUNT(*) FROM sqlite_master WHERE type='index'").fetchone()[0])
print("视图数:", con.execute(
    "SELECT COUNT(*) FROM sqlite_master WHERE type='view'").fetchone()[0])
con.close()
