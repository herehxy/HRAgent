from __future__ import annotations

import json
import os
import shutil
import socket
import sqlite3
import subprocess
import time
import urllib.request
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "release_prod" / "HRAgent工作台-v2.2.2-便携版.zip"
TEST = ROOT / "_prod_portable_test"


def main() -> int:
    if TEST.exists():
        shutil.rmtree(TEST)
    TEST.mkdir()
    with zipfile.ZipFile(ARCHIVE) as zf:
        zf.extractall(TEST)
    app = TEST / "HRAgent工作台"
    exe = app / "HRAgent工作台.exe"
    assert exe.is_file()
    assert (app / "portable.flag").is_file()
    assert (app / "data").is_dir()
    assert (app / "config").is_dir()
    for name in ("data", "config"):
        shutil.copytree(ROOT / name, app / name, dirs_exist_ok=True)
    db_path = app / "data" / "workbench.db"
    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT id, COALESCE(archived_path, file_path) FROM documents "
            "WHERE COALESCE(archived_path, file_path) IS NOT NULL LIMIT 1"
        ).fetchone()
        original = row[1] if row else "data/archive/test.pdf"
        suffix = str(original).replace("\\", "/").split("/data/", 1)[-1]
        fake = "D:/OldComputer/data/" + suffix
        conn.execute(
            "UPDATE documents SET file_path=?, archived_path=? WHERE id=?",
            (fake, fake, row[0]),
        )
        conn.commit()
    finally:
        conn.close()
    env = dict(os.environ)
    for name in ("TP_USER_HOME", "TP_CONFIG_DIR", "TP_DATA_DIR"):
        env.pop(name, None)
    env["TP_PORT"] = "8977"
    env["TP_NO_BROWSER"] = "1"
    process = subprocess.Popen([str(exe), "--no-browser"], cwd=str(app), env=env)
    try:
        deadline = time.time() + 60
        payload = None
        while time.time() < deadline:
            try:
                opener = urllib.request.build_opener(
                    urllib.request.ProxyHandler({})
                )
                with opener.open(
                    "http://127.0.0.1:8977/api/health", timeout=2
                ) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                break
            except Exception:
                time.sleep(0.5)
        if not payload or payload.get("people", 0) <= 0:
            raise RuntimeError(f"便携模式未读到候选人：{payload}")
        conn = sqlite3.connect(db_path)
        try:
            repaired = conn.execute(
                "SELECT COALESCE(archived_path, file_path) FROM documents "
                "WHERE id=?", (row[0],)
            ).fetchone()[0]
        finally:
            conn.close()
        if str(repaired).startswith("D:"):
            raise RuntimeError(f"旧绝对路径未修复：{repaired}")
        print(payload)
        print({"path_repaired": repaired})
        return 0
    finally:
        if process.poll() is None:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
            )


if __name__ == "__main__":
    raise SystemExit(main())
