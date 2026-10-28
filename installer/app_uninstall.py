"""Small, independent uninstaller copied into the installed app directory."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

APP_SHORT = "HRAgent工作台"
KEY_PATH = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\HRAgentWorkbench"


def main() -> int:
    import winreg

    target = Path(sys.executable).resolve().parent
    if not (target / f"{APP_SHORT}.exe").is_file():
        return 2
    subprocess.run(["taskkill", "/F", "/IM", f"{APP_SHORT}.exe"],
                   capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, KEY_PATH)
    except OSError:
        pass
    for folder in (
        Path(os.environ.get("USERPROFILE", "")) / "Desktop",
        Path(os.environ.get("APPDATA", "")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
    ):
        try:
            (folder / f"{APP_SHORT}.lnk").unlink(missing_ok=True)
        except OSError:
            pass
    # The running uninstaller must survive deletion of its original directory.
    temp = Path(tempfile.mkdtemp(prefix="hragent_uninstall_"))
    running = temp / Path(sys.executable).name
    shutil.copy2(sys.executable, running)
    subprocess.Popen(
        [str(running), "--cleanup", str(target)],
        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "DETACHED_PROCESS", 0),
        close_fds=True,
    )
    return 0


def cleanup_legacy() -> int:
    if len(sys.argv) < 5:
        return 2
    root, target, data = (Path(p).resolve() for p in sys.argv[2:5])
    if not (root / f"{APP_SHORT}.exe").is_file():
        return 2
    for protected in (target, data):
        if protected == root or root in protected.parents:
            return 3
    for _ in range(20):
        try:
            shutil.rmtree(root)
            return 0
        except OSError:
            time.sleep(0.5)
    return 4


def cleanup() -> int:
    if len(sys.argv) < 3:
        return 2
    target = Path(sys.argv[2]).resolve()
    if not (target / f"{APP_SHORT}.exe").is_file():
        return 2
    # Keep data/config in place: they are user data even when portable.
    keep = {"data", "config", ".hrprofile.json", "portable.flag"}
    for child in list(target.iterdir()):
        if child.name in keep:
            continue
        if child.is_dir():
            shutil.rmtree(child, ignore_errors=False)
        else:
            child.unlink(missing_ok=False)
    temp = Path(sys.executable).resolve().parent
    subprocess.Popen(
        ["cmd", "/c", "rmdir", "/s", "/q", str(temp)],
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(cleanup_legacy() if "--cleanup-legacy" in sys.argv
                     else cleanup() if "--cleanup" in sys.argv else main())
