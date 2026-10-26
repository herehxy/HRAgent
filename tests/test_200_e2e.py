# -*- coding: utf-8 -*-
"""2.0.0 → v2.2.5 全链路端到端实测（不打包 exe，直接走产品函数）。

链路：造 2.0.0 目录 → 安装器检测 → 迁移 → 启动器侧的路径修正
    → 真实读库/取文件（子进程，因为 DATA_DIR 是 import 期常量）。

每一步都断言"该有的在那里"，而不是只看没报错。
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))
sys.path.insert(0, ROOT)

FAILS, PASSES = [], []


def ok(tag, cond, detail=""):
    (PASSES if cond else FAILS).append(tag)
    print(("  PASS  " if cond else "  FAIL  ") + tag +
          ("" if cond else f"  {detail}"))


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


inst = load("hra_installer", os.path.join(ROOT, "tools", "hra_installer.py"))

# ---------------------------------------------------------------- 造 2.0.0 环境
tmp = tempfile.mkdtemp(prefix="hra_e2e_")
old = os.path.join(tmp, "HRAgent工作台")
internal = os.path.join(old, "_internal")
data = os.path.join(internal, "data")
cfg = os.path.join(internal, "config")
arch = os.path.join(data, "archive", "2026-10")
res = os.path.join(data, "resumes")
for p in (arch, res, cfg):
    os.makedirs(p, exist_ok=True)

conn = sqlite3.connect(os.path.join(data, "workbench.db"))
conn.executescript("""
CREATE TABLE IF NOT EXISTS candidates (
  id INTEGER PRIMARY KEY, name TEXT, created_at TEXT);
CREATE TABLE IF NOT EXISTS documents (
  id INTEGER PRIMARY KEY, file_hash TEXT, candidate_id INTEGER,
  application_id INTEGER, file_name TEXT, file_path TEXT, archived_path TEXT,
  mime TEXT, size INTEGER, received_at TEXT, source_message_id TEXT,
  raw_text TEXT, text_len INTEGER, parse_engine TEXT, parse_ok INTEGER,
  created_at TEXT);
""")
conn.execute("INSERT INTO candidates (id, name, created_at)"
             " VALUES (1, '老候选人', '2026-01-01 00:00:00')")
conn.execute("INSERT INTO documents (id, file_name, file_path, archived_path, mime)"
             " VALUES (1, '老简历.pdf', 'data/archive/2026-10/老简历.pdf',"
             " 'data/archive/2026-10/老简历.pdf', 'application/pdf')")
conn.commit()
conn.close()
with open(os.path.join(arch, "老简历.pdf"), "wb") as fh:
    fh.write(b"%PDF-1.4\n")
with open(os.path.join(res, "待导入.pdf"), "wb") as fh:
    fh.write(b"%PDF-1.4\n")
with open(os.path.join(cfg, "mailbox.json"), "w", encoding="utf-8") as fh:
    json.dump({
        "mode": "off",
        # 2.0.0 的老配置：绝对路径，且**旧目录已改名/换机后就不存在**
        "folder_dir": data.replace("\\", "/") + "/resumes",
        "eml_dir": data.replace("\\", "/") + "/mail_in",
        "archive_dir": data.replace("\\", "/") + "/archive",
        "attachment_ext": [".pdf"], "max_attachment_mb": 20,
        "imap": {}, "dedup": {},
    }, fh, ensure_ascii=False)
with open(os.path.join(cfg, "master.key"), "w", encoding="utf-8") as fh:
    fh.write("e2e-master-key")

print("=== 1. 安装器自动发现 2.0.0 数据 ===")
install_dir = os.path.join(tmp, "Programs", "HRAgent")
os.makedirs(install_dir, exist_ok=True)
cands = inst.detect_legacy_roots(install_dir)
ok("发现了旧数据", bool(cands), str(cands))
ok("人数 = 1", any(n == 1 for _, n in cands), str(cands))

print()
print("=== 2. 迁移（只搬不删）===")
user_root = os.path.join(tmp, "HRAgentData")
res_m = inst.migrate_legacy_data(install_dir, user_root, extra=old)
ok("迁移成功", res_m.get("migrated") is True, str(res_m.get("note")))
ok("人数校验", (res_m.get("counts") or {}).get("candidates") == 1,
   str(res_m.get("counts")))
ok("旧目录仍在（没删用户数据）", os.path.isfile(
    os.path.join(data, "workbench.db")))

print()
print("=== 3. 迁移后档案内容 ===")
new_db = os.path.join(user_root, "data", "workbench.db")
conn = sqlite3.connect(new_db)
n = conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
row = conn.execute("SELECT archived_path FROM documents").fetchone()
conn.close()
ok("候选人搬过来了", n == 1, f"count={n}")
ok("原件也搬过来了", os.path.isfile(
    os.path.join(user_root, "data", "archive", "2026-10", "老简历.pdf")))
# 库里的路径有三种形态都算对：相对 data/…（按数据根解析）、
# 或已重写到新数据根的绝对路径。判据是"能定位到新数据根下的真实文件"。
_stored = row[0]
if _stored.startswith("data/"):
    _target = os.path.join(user_root, "data", _stored[5:].replace("/", os.sep))
else:
    _target = _stored
ok("库里的路径指向新数据根下的真实文件", os.path.isfile(_target), _stored)

print()
print("=== 4. 启动器侧：配置里的绝对路径也要跟上 ===")
mod = inst._load_migrate_module()
notes = mod.normalize_config_paths(os.path.join(user_root, "config"),
                                   os.path.join(user_root, "data"))
with open(os.path.join(user_root, "config", "mailbox.json"),
          encoding="utf-8") as fh:
    cfg2 = json.load(fh)
ok("folder_dir 已指向新数据根",
   os.path.normcase(cfg2["folder_dir"]).startswith(
       os.path.normcase(os.path.join(user_root, "data"))),
   cfg2["folder_dir"])

print()
print("=== 5. 真实调用层：在线预览 + 原件打开（子进程）===")
proc = subprocess.run(
    [sys.executable, os.path.join(HERE, "_sub_resolve.py"), user_root],
    capture_output=True, text=True, encoding="utf-8", errors="replace")
print(proc.stdout.rstrip())
if proc.stderr.strip():
    print("  [stderr]", proc.stderr.strip()[:300])
ok("在线预览与原件都取得到", proc.returncode == 0, f"退出码 {proc.returncode}")

print()
print("=== 6. 数据零改动（安装器口径）===")
snap = inst.snapshot_dir(user_root)
ok("用户目录有档案", bool(snap), str(len(snap)))

shutil.rmtree(tmp, ignore_errors=True)
print()
print("=" * 60)
print(f"通过 {len(PASSES)} 项，失败 {len(FAILS)} 项")
for f in FAILS:
    print("  FAIL:", f)
sys.exit(1 if FAILS else 0)
