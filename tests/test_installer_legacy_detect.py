# -*- coding: utf-8 -*-
"""安装器能不能发现 2.0.0 的旧数据目录。

对应产品侧 `detect_legacy_roots()`。改这里之前先看这个测试：
- 手选外层 `HRAgent工作台`（2.0.0 的解压目录）必须能发现；
- 选了没有库的目录不能误报。
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, ROOT)

spec = importlib.util.spec_from_file_location(
    "hra_installer", os.path.join(ROOT, "tools", "hra_installer.py"))
inst = importlib.util.module_from_spec(spec)
spec.loader.exec_module(inst)

FAILS = []
PASSES = []


def ok(tag, cond, detail=""):
    (PASSES if cond else FAILS).append(tag)
    print(("  PASS  " if cond else "  FAIL  ") + tag +
          ("" if cond else f"  {detail}"))


tmp = tempfile.mkdtemp(prefix="hra_inst_")
old = os.path.join(tmp, "HRAgent工作台")
internal = os.path.join(old, "_internal")
data = os.path.join(internal, "data")
cfg = os.path.join(internal, "config")
os.makedirs(data, exist_ok=True)
os.makedirs(cfg, exist_ok=True)

conn = sqlite3.connect(os.path.join(data, "workbench.db"))
conn.executescript("""
CREATE TABLE IF NOT EXISTS candidates (
  id INTEGER PRIMARY KEY, name TEXT, created_at TEXT);
""")
conn.execute("INSERT INTO candidates (id, name, created_at)"
             " VALUES (1, '老候选人', '2026-01-01 00:00:00')")
conn.commit()
conn.close()

install_dir = os.path.join(tmp, "Programs", "HRAgent")
os.makedirs(install_dir, exist_ok=True)

print("=== 1. 自动发现 2.0.0 的旧数据 ===")
cands = inst.detect_legacy_roots(install_dir)
found = [g for g, _ in cands]
ok("发现了 2.0.0 的旧数据目录",
   any(os.path.normcase(g) == os.path.normcase(old) for g in found),
   str(found))
ok("候选里带了人数",
   any(n == 1 for _, n in cands), str(cands))

print()
print("=== 2. 用户在安装器里手选外层目录 ===")
cands = inst.detect_legacy_roots(install_dir, extra=old)
ok("手选外层目录能发现",
   any(os.path.normcase(g) == os.path.normcase(old) for g, _ in cands),
   str(cands))

print()
print("=== 3. 干净机器上不误报（没有任何旧数据）===")
clean = tempfile.mkdtemp(prefix="hra_clean_")
install_dir2 = os.path.join(clean, "Programs", "HRAgent")
os.makedirs(install_dir2, exist_ok=True)
cands = inst.detect_legacy_roots(install_dir2)
ok("干净机器上不误报", not cands, str(cands))
shutil.rmtree(clean, ignore_errors=True)

print()
print("=== 4. 端到端：检测到 → 迁移 → 数据与路径都对 ===")
user_root = os.path.join(tmp, "HRAgentData")
res = inst.migrate_legacy_data(install_dir, user_root, extra=old)
ok("迁移成功", res.get("migrated") is True, str(res.get("note")))
ok("人数校验通过", (res.get("counts") or {}).get("candidates") == 1,
   str(res.get("counts")))

db = os.path.join(user_root, "data", "workbench.db")
ok("新库存在", os.path.isfile(db), db)
if os.path.isfile(db):
    conn = sqlite3.connect(db)
    n = conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
    conn.close()
    ok("老候选人搬过来了", n == 1, f"count={n}")

shutil.rmtree(tmp, ignore_errors=True)

print()
print("=" * 60)
print(f"通过 {len(PASSES)} 项，失败 {len(FAILS)} 项")
for f in FAILS:
    print("  FAIL:", f)
sys.exit(1 if FAILS else 0)
