# -*- coding: utf-8 -*-
"""2.0.0 老用户升级到 v2.2.5 的端到端回归测试。

**两边都验**：每个断言都必须在"修复前应当失败"的场景里成立。
（记忆里的纪律：恒真判据不报错，只给假安心。）

覆盖三个真实断点：
  1. `layout()` 认不认 2.0.0 的 `_internal/data` 布局（用户手选外层目录）
  2. 迁移后 `mailbox.json` 里的绝对路径有没有跟着搬家
     （不搬 → 来源目录仍指旧位置 → 「在线预览」全 404）
  3. 旧库（缺列缺表）升级后能不能正常用
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, REPO)

from app import profile_migrate as PM            # noqa: E402

FAILS: list[str] = []
PASSES: list[str] = []


def ok(tag: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASSES.append(tag)
        print(f"  PASS  {tag}")
    else:
        FAILS.append(f"{tag} — {detail}")
        print(f"  FAIL  {tag}  {detail}")


def make_200_root(tmp: str) -> tuple[str, str, str]:
    """造一个 2.0.0 形态的安装目录（外层 + _internal/data + _internal/config）。"""
    outer = os.path.join(tmp, "HRAgent工作台")
    internal = os.path.join(outer, "_internal")
    data = os.path.join(internal, "data")
    cfg = os.path.join(internal, "config")
    for p in (data, cfg):
        os.makedirs(p, exist_ok=True)

    db = os.path.join(data, "workbench.db")
    conn = sqlite3.connect(db)
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS documents (
      id INTEGER PRIMARY KEY, file_hash TEXT, candidate_id INTEGER,
      application_id INTEGER, file_name TEXT, file_path TEXT, archived_path TEXT,
      mime TEXT, size INTEGER, received_at TEXT, source_message_id TEXT,
      raw_text TEXT, text_len INTEGER, parse_engine TEXT, parse_ok INTEGER,
      created_at TEXT);
    """)
    conn.execute("INSERT INTO documents (file_name, file_path, archived_path, mime)"
                 " VALUES (?,?,?,?)",
                 ("张三-简历.pdf", "data/archive/2026-10/张三-简历.pdf",
                  "data/archive/2026-10/张三-简历.pdf", "application/pdf"))
    conn.commit()
    conn.close()

    arch = os.path.join(data, "archive", "2026-10")
    os.makedirs(arch, exist_ok=True)
    with open(os.path.join(arch, "张三-简历.pdf"), "wb") as fh:
        fh.write(b"%PDF-1.4\n")
    res = os.path.join(data, "resumes")
    os.makedirs(res, exist_ok=True)
    with open(os.path.join(res, "李四-待导入.pdf"), "wb") as fh:
        fh.write(b"%PDF-1.4\n")

    with open(os.path.join(cfg, "mailbox.json"), "w", encoding="utf-8") as fh:
        json.dump({
            "mode": "imap",
            "eml_dir": data.replace("\\", "/") + "/mail_in",
            "folder_dir": data.replace("\\", "/") + "/resumes",   # 绝对路径
            "archive_dir": data.replace("\\", "/") + "/archive",
            "attachment_ext": [".pdf", ".docx", ".doc", ".txt"],
            "max_attachment_mb": 20, "imap": {}, "dedup": {},
        }, fh, ensure_ascii=False)
    with open(os.path.join(cfg, "master.key"), "w", encoding="utf-8") as fh:
        fh.write("old-master-key")
    return outer, internal, data


print("=== 1. layout() 认 2.0.0 的 _internal 布局 ===")
tmp = tempfile.mkdtemp(prefix="hra_lay_")
outer, internal, data = make_200_root(tmp)

lay_outer = PM.layout(outer)
ok("手选外层目录能认出布局", lay_outer is not None,
   f"layout({outer}) = {lay_outer}")
ok("认出的是 _internal/data", bool(lay_outer) and
   os.path.normcase(lay_outer["data"]) == os.path.normcase(data),
   str(lay_outer))
ok("手选 _internal 也能认出", PM.layout(internal) is not None)
ok("无库的目录不误判", PM.layout(tempfile.mkdtemp()) is None)

# 同目录覆盖升级：2.0.0 的 exe 就在 HRAgent工作台/ 里，库在 _internal/data
cands = PM.candidate_legacy_roots(internal, outer)
ok("覆盖升级时能自动发现旧数据",
   any(os.path.normcase(c) == os.path.normcase(outer) for c in cands),
   str(cands))
shutil.rmtree(tmp, ignore_errors=True)

print()
print("=== 2. 迁移后配置里的绝对路径跟着搬家 ===")
tmp = tempfile.mkdtemp(prefix="hra_cfg_")
outer, internal, data = make_200_root(tmp)
home = os.path.join(tmp, "HRAgentData")
PM.migrate_profile(internal, home)

with open(os.path.join(home, "config", "mailbox.json"), encoding="utf-8") as fh:
    cfg = json.load(fh)
new_data = os.path.join(home, "data")
for key in ("eml_dir", "folder_dir", "archive_dir"):
    val = cfg.get(key, "")
    ok(f"{key} 已指向新数据根",
       os.path.normcase(val).startswith(os.path.normcase(new_data)),
       f"实际 = {val}")

# 关键：改完后来源目录必须真的能取到文件（在线预览的前提）。
# ⚠️ 必须**开子进程**验：`app.paths` 的 DATA_DIR 是 import 时算出的模块级常量，
# 同进程里改 os.environ 不生效（那样测的是假象）。
import subprocess                            # noqa: E402

SUB = os.path.join(HERE, "_sub_resolve.py")
proc = subprocess.run(
    [sys.executable, SUB, home],
    capture_output=True, text=True, encoding="utf-8", errors="replace")
print(proc.stdout.rstrip())
if proc.stderr.strip():
    print("  [stderr]", proc.stderr.strip()[:400])
ok("子进程实测：来源目录与原件都能取到", proc.returncode == 0,
   f"退出码 {proc.returncode}")
shutil.rmtree(tmp, ignore_errors=True)

print()
print("=== 3. 用户自己指定的外接盘目录不得被改 ===")
tmp = tempfile.mkdtemp(prefix="hra_keep_")
outer, internal, data = make_200_root(tmp)
ext = os.path.join(tmp, "我的简历盘")
os.makedirs(ext, exist_ok=True)
with open(os.path.join(ext, "外部.pdf"), "wb") as fh:
    fh.write(b"%PDF-1.4\n")
cfgp = os.path.join(internal, "config", "mailbox.json")
with open(cfgp, encoding="utf-8") as fh:
    cfg = json.load(fh)
cfg["folder_dir"] = ext
with open(cfgp, "w", encoding="utf-8") as fh:
    json.dump(cfg, fh, ensure_ascii=False)

home = os.path.join(tmp, "HRAgentData")
PM.migrate_profile(internal, home)
with open(os.path.join(home, "config", "mailbox.json"), encoding="utf-8") as fh:
    cfg2 = json.load(fh)
ok("用户自定的外接盘目录原样保留",
   os.path.normcase(cfg2["folder_dir"]) == os.path.normcase(ext),
   f"实际 = {cfg2['folder_dir']}")
shutil.rmtree(tmp, ignore_errors=True)

print()
print("=== 4. 旧库（缺列缺表）升级后可用 ===")
tmp = tempfile.mkdtemp(prefix="hra_old_")
db = os.path.join(tmp, "workbench.db")
from app import db as D                          # noqa: E402
conn = D.connect(db)
conn.close()
conn = sqlite3.connect(db)
full = {}
for t, in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"
                        " AND name NOT LIKE 'sqlite_%'").fetchall():
    full[t] = [r[1] for r in conn.execute(f'PRAGMA table_info("{t}")').fetchall()]
for t in ("agent_runs", "proposals", "embeddings", "candidate_insights"):
    conn.execute(f'DROP TABLE IF EXISTS "{t}"')
for t, cols in {"candidates": ["note", "avatar_path", "cet"],
                "documents": ["kind", "sensitive_found"],
                "jobs": ["headcount", "active"]}.items():
    for c in cols:
        try:
            conn.execute(f'ALTER TABLE "{t}" DROP COLUMN "{c}"')
        except sqlite3.Error:
            pass
conn.execute("INSERT INTO candidates (id, name, created_at)"
             " VALUES (1, '老候选人', '2026-01-01 00:00:00')")
conn.commit()
conn.close()
D._INITIALIZED.discard(os.path.abspath(db))
conn = D.connect(db)
now = {}
for t, in conn.execute("SELECT name FROM sqlite_master WHERE type='table'"
                       " AND name NOT LIKE 'sqlite_%'").fetchall():
    now[t] = [r[1] for r in conn.execute(f'PRAGMA table_info("{t}")').fetchall()]
missing = [f"{t}.{c}" for t, cols in full.items() for c in cols
           if c not in now.get(t, [])]
ok("缺的列都补回来了", not missing, str(missing[:8]))
ok("缺的表都建回来了", not [t for t in full if t not in now])
name = conn.execute("SELECT name FROM candidates WHERE id=1").fetchone()
ok("老数据还在", bool(name and name[0] == "老候选人"), str(name))
conn.execute("SELECT id, note, avatar_path, cet FROM candidates LIMIT 1").fetchall()
conn.execute("SELECT id, kind FROM documents LIMIT 1").fetchall()
ok("新版查询能跑", True)
conn.close()
shutil.rmtree(tmp, ignore_errors=True)

print()
print("=" * 60)
print(f"通过 {len(PASSES)} 项，失败 {len(FAILS)} 项")
for f in FAILS:
    print("  FAIL:", f)
sys.exit(1 if FAILS else 0)
