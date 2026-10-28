"""Isolated executable test: migrate, remove the old app, and verify the copy."""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parent.parent
SETUP = ROOT / "release" / "installer" / "_dist" / "HRAgent工作台-安装程序-v2.2.5.exe"


def main() -> int:
    if not SETUP.is_file():
        raise SystemExit(f"先构建安装包：{SETUP}")
    root = Path(tempfile.mkdtemp(prefix="hra-e2e-", dir=ROOT / "_tmp"))
    old = root / "old" / "HRAgent工作台"
    internal = old / "_internal"
    (internal / "data" / "archive").mkdir(parents=True)
    (internal / "config").mkdir()
    (old / "HRAgent工作台.exe").write_bytes(b"old-program")
    (internal / "data" / "archive" / "resume.pdf").write_bytes(b"%PDF-1.4 old-resume")
    (internal / "config" / "master.key").write_text("old-secret-key", encoding="utf-8")
    db = sqlite3.connect(internal / "data" / "workbench.db")
    db.executescript("""
        CREATE TABLE candidates(id INTEGER PRIMARY KEY, name TEXT, avatar_path TEXT);
        CREATE TABLE applications(id INTEGER PRIMARY KEY, candidate_id INTEGER);
        CREATE TABLE documents(id INTEGER PRIMARY KEY, file_path TEXT);
        INSERT INTO candidates VALUES(1, '旧版候选人', '');
        INSERT INTO applications VALUES(1, 1);
    """)
    db.execute("INSERT INTO documents VALUES(1, ?)", (str(internal / "data" / "archive" / "resume.pdf"),))
    db.commit()
    db.close()
    env = dict(os.environ, LOCALAPPDATA=str(root / "local"), APPDATA=str(root / "roaming"),
               USERPROFILE=str(root / "profile"),
               HRAGENT_SETUP_LOG=str(root / "setup.log"),
               HRAGENT_SKIP_UNINSTALL_REGISTRY="1")
    target = root / "new" / "HRAgent工作台"
    if os.environ.get("HRAGENT_TEST_SOURCE"):
        payload = ROOT / "release" / "installer" / "payload.zip"
        command = [sys.executable, str(ROOT / "installer" / "setup_wizard.py")]
    else:
        payload = None
        command = [str(SETUP)]
    result = subprocess.run(command + ["--install", "--target", str(target), "--legacy", str(internal),
                    "--remove-old", "--no-shortcuts"] + ([str(payload)] if payload else []),
                    env=env, capture_output=True, text=True, timeout=120)
    print(result.stderr[-6000:])
    log = root / "setup.log"
    if log.is_file():
        lines = log.read_text(encoding="utf-8").splitlines()
        print("\n".join(line for line in lines
                        if not line.startswith("正在解压文件")))
    result.check_returncode()
    for _ in range(60):
        if not old.exists():
            break
        time.sleep(0.5)
    if old.exists():
        helper = target / "HRAgent卸载.exe"
        subprocess.run([str(helper), "--cleanup-legacy", str(old), str(target),
                        str(root / "local" / "HRAgentData")], check=True)
    assert not old.exists(), "旧版目录未删除"
    assert (target / "HRAgent工作台.exe").is_file()
    assert (target / "HRAgent卸载.exe").is_file()
    home = root / "local" / "HRAgentData"
    assert (home / "data" / "archive" / "resume.pdf").read_bytes() == b"%PDF-1.4 old-resume"
    assert (home / "config" / "master.key").read_text(encoding="utf-8") == "old-secret-key"
    migrated = sqlite3.connect(home / "data" / "workbench.db")
    assert migrated.execute("SELECT name FROM candidates").fetchone()[0] == "旧版候选人"
    path = migrated.execute("SELECT file_path FROM documents").fetchone()[0]
    assert Path(path) == home / "data" / "archive" / "resume.pdf", path
    assert str(old).lower() not in path.lower()
    migrated.close()
    import winreg
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, r"Software\HRAgentE2E")
    except OSError:
        pass
    print(f"PASS: 完整迁移、旧目录删除、最终文件路径校验通过。测试目录：{root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
