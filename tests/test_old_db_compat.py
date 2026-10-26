# -*- coding: utf-8 -*-
"""旧库（2.0.0 形态）→ 新版能否正常用：schema 兼容性实测。

做法（比"看代码"硬）：造一个**把当前 SCHEMA 里某些列都删掉**的库 ——
删得越狠越接近 2.0.0 —— 然后让产品自己的 db.connect() 走一遍初始化，
最后逐表逐列核对：缺的列补回来了吗？老数据还在吗？关键查询能跑吗？
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, REPO)

TMP = tempfile.mkdtemp(prefix="hra_schema_")
DB = os.path.join(TMP, "workbench.db")

from app import db as D                             # noqa: E402

# ---- 1) 先建一个完整库（新版建表）----
conn = D.connect(DB)
conn.close()
print("1) 新版建库完成")
full = {}
conn = sqlite3.connect(DB)
for t, in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"
                        " AND name NOT LIKE 'sqlite_%'").fetchall():
    full[t] = [r[1] for r in conn.execute(f'PRAGMA table_info("{t}")').fetchall()]
conn.close()
print("   表数 =", len(full), " 总列数 =", sum(len(v) for v in full.values()))
print()

# ---- 2) 造"2.0.0 形态"：删掉后来新增的列，删掉后来新增的表 ----
# 2.0.0 之前肯定没有的表（智能体 v1.25 之后才有的）
LATE_TABLES = ["agent_runs", "proposals", "embeddings", "candidate_insights",
                "daily_briefs", "mail_templates", "candidate_skills",
                "candidate_tags", "tags", "sessions"]
# 2.0.0 之后才加的列（挑明确的近期新增项）
LATE_COLUMNS = {
    "candidates": ["note", "avatar_path", "cet", "first_degree_school",
                   "edu_history", "major_canonical", "major_via", "grad_date",
                   "in_school", "honors_json", "archived_at"],
    "documents": ["kind", "sensitive_found", "merged_from_candidate"],
    "jobs": ["headcount", "dept_id", "active"],
    "applications": ["archived_at", "source"],
}

conn = sqlite3.connect(DB)
# 先塞一点老数据，确保迁移不丢数据
conn.execute("INSERT OR IGNORE INTO candidates (id, name, created_at)"
             " VALUES (1, '老候选人', '2026-01-01 00:00:00')")
conn.execute("INSERT OR IGNORE INTO documents (id, file_name, file_path,"
             " archived_path, mime) VALUES (1, '老简历.pdf', 'data/archive/a.pdf',"
             " 'data/archive/a.pdf', 'application/pdf')")
conn.commit()

for t in LATE_TABLES:
    conn.execute(f'DROP TABLE IF EXISTS "{t}"')
for t, cols in LATE_COLUMNS.items():
    for c in cols:
        try:
            conn.execute(f'ALTER TABLE "{t}" DROP COLUMN "{c}"')
        except sqlite3.Error:
            pass          # 老 SQLite 不支持 DROP COLUMN，跳过
conn.commit()
conn.close()
print("2) 已削成 2.0.0 形态")

conn = sqlite3.connect(DB)
after_drop = {}
for t, in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"
                        " AND name NOT LIKE 'sqlite_%'").fetchall():
    after_drop[t] = [r[1] for r in conn.execute(f'PRAGMA table_info("{t}")').fetchall()]
conn.close()
print("   剩余表数 =", len(after_drop), " 总列数 =",
      sum(len(v) for v in after_drop.values()))
print()

# ---- 3) 让产品自己初始化（这就是"打开旧库"时发生的事）----
D._INITIALIZED.discard(os.path.abspath(DB))
conn = D.connect(DB)
conn.close()
print("3) 新版 db.connect() 已完成")
print()

# ---- 4) 核对：列补齐了吗？表补齐了吗？数据还在吗？----
conn = sqlite3.connect(DB)
now = {}
for t, in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"
                       " AND name NOT LIKE 'sqlite_%'").fetchall():
    now[t] = [r[1] for r in conn.execute(f'PRAGMA table_info("{t}")').fetchall()]
conn.close()

missing_cols = []
for t, cols in full.items():
    for c in cols:
        if c not in now.get(t, []):
            missing_cols.append(f"{t}.{c}")
missing_tables = [t for t in full if t not in now]

print("=== 核对 ===")
print("缺的表 =", missing_tables or "无")
print("缺的列 =", missing_cols or "无")
print()

conn = sqlite3.connect(DB)
n_cand = conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
n_doc = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
name = conn.execute("SELECT name FROM candidates WHERE id=1").fetchone()
conn.close()
print("老候选人还在 =", name[0] if name else "（丢了！）", " 候选人数 =", n_cand)
print("老简历还在   =", n_doc)
print()

# ---- 5) 关键查询跑一遍（真调用，不看代码）----
print("=== 关键查询 ===")
ok = True
try:
    conn = D.connect(DB)
    rows = conn.execute("SELECT id, name FROM candidates ORDER BY id").fetchall()
    print("  查候选人 OK：", len(rows), "行")
    rows = conn.execute("SELECT id, file_name FROM documents").fetchall()
    print("  查原件   OK：", len(rows), "行")
    # 新版功能依赖的新列
    for sql in ("SELECT id, note, avatar_path, cet FROM candidates LIMIT 1",
                "SELECT id, kind, sensitive_found FROM documents LIMIT 1",
                "SELECT id, headcount, active FROM jobs LIMIT 1"):
        conn.execute(sql).fetchall()
    print("  新增列查询 OK")
    conn.close()
except Exception as exc:
    ok = False
    print("  查询失败：", type(exc).__name__, exc)

print()
print("结论 =", "PASS" if (ok and not missing_cols and not missing_tables) else "FAIL")
print("TMP =", TMP)
