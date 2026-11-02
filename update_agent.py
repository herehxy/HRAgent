"""Standalone hot-update helper.

The updater replaces only the PyInstaller ``_internal`` program directory. It waits
for the running app to exit, refuses payloads that contain user data/config, backs
up the current program files, and verifies the user profile before/after.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
import uuid
import zipfile
from pathlib import Path

APP_ID = "HRAgent"
MAIN_EXE = "HRAgent工作台.exe"
FORBIDDEN_PREFIXES = ("_internal/data/", "_internal/config/")


def _message(text: str, *, error: bool = False) -> None:
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(
            None, text, "HRAgent 热更新", 0x10 if error else 0x40
        )
    except Exception:
        print(text)


def _choose_update() -> str | None:
    try:
        import ctypes
        from ctypes import wintypes

        class OPENFILENAMEW(ctypes.Structure):
            _fields_ = [
                ("lStructSize", wintypes.DWORD),
                ("hwndOwner", wintypes.HWND),
                ("hInstance", wintypes.HINSTANCE),
                ("lpstrFilter", wintypes.LPCWSTR),
                ("lpstrCustomFilter", wintypes.LPWSTR),
                ("nMaxCustFilter", wintypes.DWORD),
                ("nFilterIndex", wintypes.DWORD),
                ("lpstrFile", wintypes.LPWSTR),
                ("nMaxFile", wintypes.DWORD),
                ("lpstrFileTitle", wintypes.LPWSTR),
                ("nMaxFileTitle", wintypes.DWORD),
                ("lpstrInitialDir", wintypes.LPCWSTR),
                ("lpstrTitle", wintypes.LPCWSTR),
                ("Flags", wintypes.DWORD),
                ("nFileOffset", wintypes.WORD),
                ("nFileExtension", wintypes.WORD),
                ("lpstrDefExt", wintypes.LPCWSTR),
                ("lCustData", wintypes.LPARAM),
                ("lpfnHook", ctypes.c_void_p),
                ("lpTemplateName", wintypes.LPCWSTR),
            ]

        buffer = ctypes.create_unicode_buffer(32768)
        info = OPENFILENAMEW()
        info.lStructSize = ctypes.sizeof(info)
        info.lpstrFilter = "HRAgent 更新包 (*.zip)\0*.zip\0\0"
        info.lpstrFile = ctypes.cast(buffer, wintypes.LPWSTR)
        info.nMaxFile = len(buffer)
        info.lpstrTitle = "选择 HRAgent 热更新包"
        info.Flags = 0x00080000 | 0x00001000 | 0x00000008
        info.lpstrDefExt = "zip"
        if ctypes.windll.comdlg32.GetOpenFileNameW(ctypes.byref(info)):
            return os.path.abspath(buffer.value)
    except Exception as exc:
        print(f"[update] 文件选择器不可用：{exc}")
    return None


def _bootstrap() -> dict:
    root = os.environ.get("APPDATA") or ""
    try:
        with open(os.path.join(root, APP_ID, "bootstrap.json"),
                  encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _user_home(install_dir: str | None = None) -> str:
    if not os.environ.get("TP_USER_HOME"):
        bootstrap_home = _bootstrap().get("user_home")
        if bootstrap_home:
            return os.path.abspath(bootstrap_home)
    if install_dir and not os.environ.get("TP_USER_HOME"):
        marker = os.path.join(install_dir, "portable.flag")
        if (os.path.isfile(marker)
                or os.path.isfile(os.path.join(install_dir, "data", "workbench.db"))):
            return os.path.abspath(install_dir)
    return os.path.abspath(
        os.environ.get("TP_USER_HOME")
        or os.path.join(os.environ.get("LOCALAPPDATA") or "", "HRAgentData")
    )


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _long_path(path: str) -> str:
    """Use the Windows extended-length prefix so deep attachment paths work."""
    if os.name != "nt":
        return path
    absolute = os.path.abspath(path)
    if absolute.startswith("\\\\?\\"):
        return absolute
    return "\\\\?\\" + absolute


def _candidate_count(db_path: str) -> int:
    if not os.path.isfile(db_path):
        return -1
    import sqlite3
    try:
        conn = sqlite3.connect(_long_path(db_path))
        try:
            return int(conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0])
        finally:
            conn.close()
    except Exception:
        return -1


def _profile_fingerprint(user_home: str) -> dict:
    """Fingerprint only user data/config that an update is forbidden to alter."""
    data = os.path.join(user_home, "data")
    config = os.path.join(user_home, "config")
    files = [
        os.path.join(data, "workbench.db"),
        os.path.join(data, "master.key"),
        os.path.join(config, "master.key"),
        os.path.join(config, "model.json"),
        os.path.join(config, "secrets.json"),
        os.path.join(config, "mailbox.json"),
        os.path.join(config, "imap.secret"),
        os.path.join(config, "smtp.secret"),
    ]
    out = {}
    for path in files:
        if os.path.isfile(path):
            out[os.path.relpath(path, user_home)] = {
                "size": os.path.getsize(path),
                "sha256": _sha256(path),
            }
    return out


def _wait_for_exit(pid: int | None, timeout: int = 90) -> None:
    if not pid or pid <= 0:
        return
    if os.name != "nt":
        return
    import ctypes
    kernel32 = ctypes.windll.kernel32
    synchronize = 0x00100000
    handle = kernel32.OpenProcess(synchronize, False, int(pid))
    if not handle:
        return
    try:
        kernel32.WaitForSingleObject(handle, max(1, timeout) * 1000)
    finally:
        kernel32.CloseHandle(handle)


def _safe_members(zf: zipfile.ZipFile) -> list[zipfile.ZipInfo]:
    members = []
    for info in zf.infolist():
        name = info.filename.replace("\\", "/")
        if name.startswith("/") or ".." in name.split("/"):
            raise ValueError(f"更新包包含非法路径：{info.filename}")
        if name == "update-manifest.json":
            continue
        if not name.startswith("_internal/"):
            raise ValueError(f"更新包只能替换 _internal：{info.filename}")
        if any(name.startswith(prefix) for prefix in FORBIDDEN_PREFIXES):
            raise ValueError(f"更新包包含用户数据/配置，已拒绝：{info.filename}")
        if not info.is_dir():
            members.append(info)
    return members


def _verify_manifest(zf: zipfile.ZipFile, members: list[zipfile.ZipInfo]) -> dict:
    try:
        manifest = json.loads(zf.read("update-manifest.json").decode("utf-8"))
    except Exception as exc:
        raise ValueError(f"缺少或无法读取 update-manifest.json：{exc}")
    expected = manifest.get("files") or {}
    actual_names = {i.filename.replace("\\", "/") for i in members}
    if actual_names != set(expected):
        raise ValueError("更新清单与实际文件列表不一致")
    for info in members:
        name = info.filename.replace("\\", "/")
        digest = hashlib.sha256(zf.read(info)).hexdigest()
        if digest != expected[name]:
            raise ValueError(f"更新文件校验失败：{name}")
    return manifest


def _copy_backup(source: str, destination: str, *,
                 exclude_state: bool = False) -> None:
    if os.path.isdir(source):
        if os.path.exists(destination):
            shutil.rmtree(_long_path(destination))

        def ignore_state(directory, names):
            if not exclude_state:
                return set()
            source_norm = os.path.normcase(os.path.abspath(source))
            directory_norm = os.path.normcase(
                os.path.abspath(directory).replace("\\\\?\\", "")
            )
            if directory_norm == source_norm:
                return {name for name in names if name in {"data", "config"}}
            return set()

        shutil.copytree(
            _long_path(source), _long_path(destination),
            ignore=ignore_state,
        )
    elif os.path.isfile(source):
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        shutil.copy2(_long_path(source), _long_path(destination))


def apply_update(package: str, install_dir: str, *, pid: int | None = None,
                 relaunch: bool = True, log=print) -> dict:
    package = os.path.abspath(package)
    install_dir = os.path.abspath(install_dir)
    internal = os.path.join(install_dir, "_internal")
    main_exe = os.path.join(install_dir, MAIN_EXE)
    if not os.path.isfile(package):
        raise FileNotFoundError(package)
    if not os.path.isfile(main_exe):
        raise FileNotFoundError(f"安装目录缺少主程序：{main_exe}")

    _wait_for_exit(pid)
    user_home = _user_home(install_dir)
    before_profile = _profile_fingerprint(user_home)

    # 老版本把数据放在 _internal。先复制到独立用户目录，再允许替换程序文件。
    from app import profile_migrate
    install_parent = os.path.dirname(install_dir)
    earlier_profile = bool(profile_migrate.has_profile(user_home))
    target_count = _candidate_count(profile_migrate.profile_db(user_home))
    legacy_roots = profile_migrate.candidate_legacy_roots(install_dir, install_parent)
    legacy_candidates = []
    for root in legacy_roots:
        old_layout = profile_migrate.layout(root)
        if not old_layout:
            continue
        count = _candidate_count(os.path.join(old_layout["data"], "workbench.db"))
        if count > 0:
            legacy_candidates.append((count, root))
    legacy_candidates.sort(reverse=True)

    if not earlier_profile and legacy_candidates:
        source_root = legacy_candidates[0][1]
        log(f"[update] 检测到旧版档案（{legacy_candidates[0][0]} 人），先复制到用户目录")
        profile_migrate.migrate_profile(source_root, user_home, log=log)
        before_profile = _profile_fingerprint(user_home)
    elif earlier_profile:
        log(f"[update] 用户档案已独立存放（{target_count} 人），更新不会触碰")
        if legacy_candidates and legacy_candidates[0][0] > max(0, target_count):
            raise RuntimeError(
                f"旧程序目录里有 {legacy_candidates[0][0]} 人，当前用户目录只有 "
                f"{target_count} 人。为防止覆盖较新的档案，更新已中止；"
                "请先把旧目录中的 data/config 恢复或核对后再更新。"
            )

    token = uuid.uuid4().hex[:10]
    staging_parent = os.path.join(install_dir, f".update-{token}")
    staging_internal = os.path.join(staging_parent, "_internal")
    old_internal = os.path.join(install_dir, f".internal-old-{token}")
    os.makedirs(staging_parent, exist_ok=True)

    with zipfile.ZipFile(package) as zf:
        members = _safe_members(zf)
        manifest = _verify_manifest(zf, members)
        for info in members:
            name = info.filename.replace("\\", "/")
            target = os.path.join(staging_parent, name.replace("/", os.sep))
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with zf.open(info) as source, open(target, "wb") as destination:
                shutil.copyfileobj(source, destination)

    backup_root = os.path.join(
        os.environ.get("LOCALAPPDATA") or os.path.dirname(install_dir),
        APP_ID + "Updates", "backups",
        f"{manifest.get('version') or 'unknown'}-{time.strftime('%Y%m%d-%H%M%S')}-{token}",
    )
    os.makedirs(backup_root, exist_ok=True)
    log(f"[update] 备份当前程序文件：{backup_root}")
    _copy_backup(
        internal, os.path.join(backup_root, "_internal"),
        exclude_state=True,
    )
    _copy_backup(main_exe, os.path.join(backup_root, MAIN_EXE))

    old_renamed = False
    new_installed = False
    try:
        if os.path.exists(internal):
            os.replace(internal, old_internal)
            old_renamed = True
        os.replace(staging_internal, internal)
        new_installed = True
    except Exception:
        if new_installed and os.path.exists(internal):
            shutil.rmtree(internal, ignore_errors=True)
        if old_renamed and os.path.exists(old_internal):
            os.replace(old_internal, internal)
        raise

    after_profile = _profile_fingerprint(user_home)
    if before_profile != after_profile:
        if new_installed:
            shutil.rmtree(internal, ignore_errors=True)
        if old_renamed and os.path.exists(old_internal):
            os.replace(old_internal, internal)
        raise RuntimeError("更新前后用户数据/配置指纹不一致，已回滚程序文件")

    # 旧版若还把 data/config 放在程序目录里，整个保留在备份中，绝不删除。
    legacy_state = (
        os.path.isdir(os.path.join(old_internal, "data"))
        or os.path.isdir(os.path.join(old_internal, "config"))
    )
    if legacy_state and os.path.exists(old_internal):
        preserved = os.path.join(backup_root, "legacy-internal")
        os.replace(_long_path(old_internal), _long_path(preserved))
    else:
        shutil.rmtree(_long_path(old_internal), ignore_errors=True)
    shutil.rmtree(_long_path(staging_parent), ignore_errors=True)
    result = {
        "ok": True,
        "version": manifest.get("version"),
        "install_dir": install_dir,
        "backup": backup_root,
        "profile_unchanged": True,
        "files": len(members),
    }
    log("[update] 完成，用户数据与配置指纹未变化")
    if relaunch:
        subprocess.Popen([main_exe], cwd=install_dir)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="HRAgent 热更新器")
    parser.add_argument("--package", help="更新包 zip（省略时弹出选择器）")
    parser.add_argument("--install-dir", help="主程序所在目录")
    parser.add_argument("--pid", type=int, default=0, help="等待退出的主进程 PID")
    parser.add_argument("--no-relaunch", action="store_true")
    args = parser.parse_args(argv)
    if not args.package:
        args.package = _choose_update()
        if not args.package:
            return 1
    install_dir = args.install_dir or os.path.dirname(
        os.path.abspath(sys.executable if getattr(sys, "frozen", False)
                        else sys.argv[0])
    )
    install_dir = os.path.abspath(install_dir)
    log_path = os.path.join(
        os.environ.get("LOCALAPPDATA") or install_dir,
        APP_ID + "Updates", "update.log",
    )
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    try:
        with open(log_path, "a", encoding="utf-8", buffering=1) as log_file:
            def log(message: str) -> None:
                log_file.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}\n")
                print(message)

            result = apply_update(
                args.package, install_dir, pid=args.pid,
                relaunch=not args.no_relaunch, log=log,
            )
        _message("热更新完成。\n版本：" + str(result.get("version") or "未知") +
                 "\n用户数据与配置未改动。")
        return 0
    except Exception as exc:
        message = f"热更新失败：{type(exc).__name__}: {exc}"
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(message + "\n")
            fh.write(traceback.format_exc() + "\n")
        _message(message + "\n\n当前程序已保留或自动回滚。", error=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
