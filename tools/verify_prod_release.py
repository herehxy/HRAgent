from __future__ import annotations

import hashlib
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
if str(ROOT) not in os.sys.path:
    os.sys.path.insert(0, str(ROOT))

from app import profile_migrate
from update_agent import apply_update


RELEASE = ROOT / "release_prod"
APP_DIR = ROOT / "dist_prod" / "HRAgent工作台"
APP_EXE = APP_DIR / "HRAgent工作台.exe"
VERIFY = ROOT / "_prod_verify"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def counts(db_path: str) -> dict[str, int]:
    conn = sqlite3.connect(db_path)
    try:
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        return {
            table: int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in ("candidates", "applications", "documents")
            if table in tables
        }
    finally:
        conn.close()


def free_port() -> int:
    for port in range(8901, 8991):
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError("没有可用测试端口")


def http_json(url: str, timeout: float = 2.0) -> dict:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def start_and_check(profile: Path) -> dict:
    port = free_port()
    env = dict(os.environ)
    env["TP_USER_HOME"] = str(profile)
    env["TP_PORT"] = str(port)
    env["TP_NO_BROWSER"] = "1"
    process = subprocess.Popen(
        [str(APP_EXE), "--data-dir", str(profile), "--no-browser"],
        cwd=str(APP_DIR),
        env=env,
    )
    try:
        deadline = time.time() + 90
        health = None
        while time.time() < deadline:
            if process.poll() is not None:
                log = profile / "data" / "webapp.log"
                raise RuntimeError(
                    f"生产程序提前退出，code={process.returncode}\n"
                    + (log.read_text(encoding="utf-8", errors="replace")
                       if log.exists() else "")
                )
            try:
                health = http_json(f"http://127.0.0.1:{port}/api/health")
                break
            except Exception:
                time.sleep(0.5)
        if not health:
            raise RuntimeError("生产程序 90 秒内未就绪")
        meta = http_json(f"http://127.0.0.1:{port}/api/meta")
        if meta.get("product_version") != "2.1.0":
            raise RuntimeError(f"版本戳不对：{meta.get('product_version')}")
        time.sleep(1.5)
        log_path = profile / "data" / "webapp.log"
        log_text = log_path.read_text(encoding="utf-8", errors="replace") \
            if log_path.exists() else ""
        if "托盘启动失败" in log_text:
            raise RuntimeError("生产程序托盘初始化失败\n" + log_text[-3000:])
        if "托盘图标已就绪" not in log_text:
            raise RuntimeError("生产程序日志未确认托盘就绪\n" + log_text[-3000:])
        return {
            "port": port,
            "health": health,
            "meta_version": meta["product_version"],
            "tray_ready": True,
        }
    finally:
        if process.poll() is None:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
            )
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()


def main() -> int:
    required = [
        APP_EXE,
        APP_DIR / "app.ico",
        APP_DIR / "HRAgent更新器.exe",
        RELEASE / "HRAgent-update-v2.1.0.zip",
        RELEASE / "HRAgent安装程序-v2.1.0.exe",
    ]
    for path in required:
        if not path.exists() or path.stat().st_size == 0:
            raise RuntimeError(f"缺少发行文件：{path}")
    if (APP_DIR / "_internal" / "data").exists():
        raise RuntimeError("生产包混入 data 目录")
    if (APP_DIR / "_internal" / "config").exists():
        raise RuntimeError("生产包混入 config 目录")

    source_db = ROOT / "data" / "workbench.db"
    source_key = ROOT / "data" / "master.key"
    source_before = {
        "db_sha": sha256(source_db),
        "key_sha": sha256(source_key) if source_key.exists() else None,
        "counts": counts(str(source_db)),
    }

    if VERIFY.exists():
        shutil.rmtree(VERIFY)
    profile = VERIFY / "profile"
    marker = profile_migrate.migrate_profile(str(ROOT), str(profile))
    if counts(str(profile_migrate.profile_db(str(profile)))) != source_before["counts"]:
        raise RuntimeError("迁移后的档案计数与源库不一致")
    source_after = {
        "db_sha": sha256(source_db),
        "key_sha": sha256(source_key) if source_key.exists() else None,
        "counts": counts(str(source_db)),
    }
    if source_after != source_before:
        raise RuntimeError("迁移修改了源库/源密钥")

    smoke = start_and_check(profile)

    fake_install = VERIFY / "fake-install"
    shutil.copytree(APP_DIR, fake_install, copy_function=os.link)
    os.environ["TP_USER_HOME"] = str(profile)
    before_update = {
        key: (path.stat().st_size, sha256(path))
        for key, path in {
            "db": Path(profile_migrate.profile_db(str(profile))),
            "master": Path(profile) / "config" / "master.key",
            "model": Path(profile) / "config" / "model.json",
        }.items() if path.exists()
    }
    update_zip = RELEASE / "HRAgent-update-v2.1.0.zip"
    result = apply_update(
        str(update_zip), str(fake_install), relaunch=False,
        log=lambda *_args: None,
    )
    after_update = {
        key: (path.stat().st_size, sha256(path))
        for key, path in {
            "db": Path(profile_migrate.profile_db(str(profile))),
            "master": Path(profile) / "config" / "master.key",
            "model": Path(profile) / "config" / "model.json",
        }.items() if path.exists()
    }
    if before_update != after_update:
        raise RuntimeError("热更新改变了用户档案/配置")
    if not (fake_install / "_internal" / "config_template" / "stages.json").is_file():
        raise RuntimeError("热更新后的程序目录不完整")
    shutil.rmtree(Path(result["backup"]).parent, ignore_errors=True)

    with zipfile.ZipFile(update_zip) as zf:
        bad = [
            name for name in zf.namelist()
            if name.startswith("_internal/data/")
            or name.startswith("_internal/config/")
        ]
        if bad:
            raise RuntimeError("热更新包含用户数据：" + ", ".join(bad))

    print(json.dumps({
        "ok": True,
        "migration_counts": marker["counts"],
        "source_unchanged": True,
        "smoke": smoke,
        "update_profile_unchanged": True,
        "update_version": result["version"],
        "update_files": result["files"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
