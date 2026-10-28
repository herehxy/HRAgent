"""Build the lightweight native uninstall helper used by the setup wizard."""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "release" / "installer"
NAME = "HRAgent卸载"
OUT.mkdir(parents=True, exist_ok=True)
spec = OUT / "_uninstall.spec"
spec.write_text(f'''# -*- mode: python -*-
a = Analysis([{str(ROOT / "installer" / "app_uninstall.py")!r}], pathex=[],
    binaries=[], datas=[], hiddenimports=[], hookspath=[], hooksconfig={{}},
    runtime_hooks=[], excludes=[], noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name={NAME!r}, debug=False,
    bootloader_ignore_signals=False, strip=False, upx=False, console=False,
    disable_windowed_traceback=False, argv_emulation=False, target_arch=None,
    codesign_identity=None, entitlements_file=None,
    icon={str(ROOT / "app.ico")!r})
''', encoding="utf-8")
runtime = ROOT / "repo_src" / ".runtime_pyinstaller"
if runtime.is_dir():
    sys.path.insert(0, str(runtime))
from PyInstaller.__main__ import run as run_pyinstaller
run_pyinstaller([str(spec), "--distpath", str(OUT),
                 "--workpath", str(OUT / "_uninstall_build"), "--noconfirm"])
