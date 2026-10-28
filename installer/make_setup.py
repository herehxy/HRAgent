# -*- coding: utf-8 -*-
"""Build a self-contained one-file installer with an embedded program payload."""
from __future__ import annotations

import os
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "repo_src" / "dist_prod" / "HRAgent工作台"
if not DIST.is_dir():
    DIST = ROOT / "dist" / "HRAgent工作台"
BOOT = ROOT / "installer" / "setup_boot.py"
OUT = ROOT / "release" / "installer"


def _version() -> str:
    """版本号唯一来源 app/version.py，避免和 build_prod.py 各写一份对不上。"""
    sys.path.insert(0, str(ROOT / "repo_src"))
    from app.version import VERSION as _v
    return _v


VERSION = _version()
APPNAME = "HRAgent工作台"
INSTALLER_NAME = f"{APPNAME}-安装程序-v{VERSION}"


def build_payload() -> Path:
    if not DIST.is_dir() or not (DIST / f"{APPNAME}.exe").is_file():
        sys.exit(f"[x] 缺少完整程序产物：{DIST}")
    OUT.mkdir(parents=True, exist_ok=True)
    payload = OUT / "payload.zip"
    files = [f for f in DIST.rglob("*") if f.is_file()]
    with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for f in files:
            zf.write(f, f.relative_to(DIST))
    print(f"[i] 载荷：{len(files)} 个文件，{payload.stat().st_size / 1024**2:.1f} MB")
    return payload


def main() -> None:
    runtime = ROOT / "repo_src" / ".runtime_pyinstaller"
    if runtime.is_dir():
        sys.path.insert(0, str(runtime))
    payload = build_payload()
    spec = OUT / "_setup.spec"
    spec.write_text(f'''# -*- mode: python -*-
import os
a = Analysis([{str(BOOT)!r}], pathex=[{str(BOOT.parent)!r}], binaries=[],
    datas=[({str(payload)!r}, "."), ({str(ROOT / "app.ico")!r}, "."),
           ({str(ROOT / "repo_src" / "app" / "profile_migrate.py")!r}, "."),
           ({str(OUT / "HRAgent卸载.exe")!r}, ".")],
    hiddenimports=["sqlite3", "uuid", "hashlib", "tempfile", "shutil", "json",
                   "datetime", "pathlib"], hookspath=[], hooksconfig={{}}, runtime_hooks=[],
    excludes=["numpy", "pandas", "scipy", "torch", "PIL", "fitz", "openpyxl",
              "fastapi", "uvicorn", "pymupdf", "markitdown"], noarchive=False)
pyz = PYZ(a.pure)
# v3.1.14：**单文件（onefile）**。
#
# 现场核实（2026-10-28）：v3.1.9 的单文件安装器在**别人电脑上完全正常**，
# 只有业务方这台机器会报 "Could not create temporary directory!" ——
# 那是它 %TEMP% 里留下了两个被 ACL 锁死、连 takeown 都删不掉的 `_MEI*` 目录，
# 属于**单机环境问题**，不是打包格式的问题。
#
# 所以分发用的就是这个格式。万一某台机器也中招，包里同时带了
# 「安装.cmd」（robocopy + PowerShell 版安装脚本），双击它一样能装。
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name={INSTALLER_NAME!r},
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=False, disable_windowed_traceback=False, argv_emulation=False,
    target_arch=None, codesign_identity=None, entitlements_file=None,
    icon={str(ROOT / "app.ico")!r})
''', encoding="utf-8")
    dist_dir = OUT / "_dist"
    work_dir = OUT / "_build"
    print("[i] 构建单文件安装器")
    from PyInstaller.__main__ import run as run_pyinstaller
    run_pyinstaller([str(spec), "--distpath", str(dist_dir),
                     "--workpath", str(work_dir), "--noconfirm"])
    built = dist_dir / f"{INSTALLER_NAME}.exe"
    if not built.is_file():
        sys.exit(f"[x] 安装器构建完成但找不到产物：{built}")
    print(f"[OK] {built} ({os.path.getsize(built) / 1024**2:.1f} MB)")
    _zip_onefile(built, OUT / f"{INSTALLER_NAME}.zip")


def _zip_onefile(exe: Path, dst: Path) -> None:
    """把单文件安装器打成 zip（只含那一个 exe）。"""
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.write(exe, exe.name)
    print(f"[OK] 分发包：{dst} ({dst.stat().st_size / 1024**2:.1f} MB)")


if __name__ == "__main__":
    main()
