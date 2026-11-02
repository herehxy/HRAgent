# -*- mode: python ; coding: utf-8 -*-
import os

ROOT = os.path.abspath(SPECPATH)
SRC = os.path.join(ROOT, "repo_src") if os.path.isdir(
    os.path.join(ROOT, "repo_src")) else ROOT
WORKSPACE = os.path.dirname(ROOT)

a = Analysis(
    [os.path.join(SRC, "update_agent.py")],
    pathex=[SRC],
    binaries=[],
    datas=[],
    hiddenimports=["app.profile_migrate"],
    hookspath=[],
    runtime_hooks=[],
    excludes=["tests", "tkinter"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="HRAgent更新器",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon=os.path.join(WORKSPACE, "app.ico"),
)
