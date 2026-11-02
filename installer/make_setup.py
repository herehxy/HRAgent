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
# 单文件安装器的解包目录。**不能再用 %TEMP%**：现场 %TEMP% 里有 ACL
# 锁死的 _MEI* 残留，而企业策略又拦未签名 exe 在 %TEMP% 里解包，
# 表现就是"双击 exe 没反应，但 Install-HRAgent.cmd 能起"。
# %LOCALAPPDATA% 会被 PyInstaller 启动器展开，实测解包到
# C:\Users\<用户>\AppData\Local\HRAgentInstallerTmp\_MEIxxxxxx。
RUNTIME_TMPDIR = r"%LOCALAPPDATA%\HRAgentInstallerTmp"


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
# v4.0.9：**单文件（onefile）** —— 与 4.0.6 完全一致的打包方式。
#
# 历史：中间试过"目录版"来绕开 %TEMP% 里那两个 ACL 锁死的 `_MEI*` 残留
# （报错 "Could not create temporary directory!"）。但业务方要求按 4.0.6 的方式
# 发一个单文件 exe，所以恢复 onefile。
#
# v4.0.15：onefile 保留，但**解包目录从 %TEMP% 挪到 %LOCALAPPDATA%**。
# 这是"双击 exe 没反应、用 Install-HRAgent.cmd 就能起"的根因修复：
#   · %TEMP% 里有历史 _MEI* 残留，普通进程读不了也删不掉；
#   · 企业策略拦未签名 exe 在 %TEMP% 里创建/解包；
#   · 启动器不会因为换目录而失效，验证过 _MEIPASS =
#     C:/Users/<用户>/AppData/Local/HRAgentInstallerTmp/_MEIxxxxxx。
# 代价：每次启动仍要解出约 180 MB，只是不再依赖 %TEMP%。
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name={INSTALLER_NAME!r},
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=False, disable_windowed_traceback=False, argv_emulation=False,
    target_arch=None, codesign_identity=None, entitlements_file=None,
    icon={str(ROOT / "app.ico")!r},
    runtime_tmpdir={RUNTIME_TMPDIR!r})
''', encoding="utf-8")
    dist_dir = OUT / "_dist"
    work_dir = OUT / "_build"
    print("[i] 构建单文件安装器（与 4.0.6 同款）")
    from PyInstaller.__main__ import run as run_pyinstaller
    run_pyinstaller([str(spec), "--distpath", str(dist_dir),
                     "--workpath", str(work_dir), "--noconfirm"])
    built = dist_dir / f"{INSTALLER_NAME}.exe"
    if not built.is_file():
        sys.exit(f"[x] 安装器构建完成但找不到产物：{built}")
    print(f"[OK] {built} ({os.path.getsize(built) / 1024**2:.1f} MB)")
    _zip_onefile(built, OUT / f"{INSTALLER_NAME}.zip")


def _zip_onefile(exe: Path, dst: Path) -> None:
    """单文件安装器打成 zip（只含那一个 exe，和 4.0.6 的分发包同构）。"""
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        zf.write(exe, exe.name)
    print(f"[OK] 分发包：{dst} ({dst.stat().st_size / 1024**2:.1f} MB)")


if __name__ == "__main__":
    main()
