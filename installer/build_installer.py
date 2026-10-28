# -*- coding: utf-8 -*-
"""
一键出安装包：PyInstaller 产物 -> Inno Setup 安装程序。

    python installer/build_installer.py

前提：
  · 先跑 HRAgentWebapp.spec 生成 dist/HRAgent工作台/
  · 已安装 Inno Setup 6（本脚本会自动找，找不到会给出下载指引）

为什么要单独一个安装包，而不是继续发 zip：
  · zip 得让 HR 自己找 exe、自己解压到有权限的目录（解压到桌面/下载常常跑不起来）
  · 数据目录在 %LOCALAPPDATA%\\HRAgentData，与安装目录解耦，所以代码可以装到
    Program Files 这类只读位置 —— 这是安装包能成立的前提
  · 免掉 UAC：默认装到当前用户目录，HR 大概率不是管理员
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ISS = ROOT / "installer" / "HRAgent.iss"
DIST = ROOT / "dist" / "HRAgent工作台"
OUT = ROOT / "release" / "installer"

# 常见的 Inno Setup 安装位置（按优先级）
ISCC_CANDIDATES = [
    r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    r"C:\Program Files\Inno Setup 6\ISCC.exe",
    r"C:\Program Files (x86)\Inno Setup 7\ISCC.exe",
    r"C:\Program Files\Inno Setup 7\ISCC.exe",
]


def find_iscc() -> str | None:
    """定位 ISCC.exe。先看常见路径，再看 PATH。"""
    for p in ISCC_CANDIDATES:
        if Path(p).is_file():
            return p
    # PATH 里可能有个别情况（自建便携版）
    from shutil import which
    return which("ISCC.exe") or which("iscc")


def check_dist() -> None:
    """打包前先确认产物齐不齐，别等编译两分钟才发现目录是空的。"""
    if not DIST.is_dir():
        sys.exit(
            f"[x] 找不到打包产物：{DIST}\n"
            f"    请先跑 PyInstaller 生成 dist/HRAgent工作台/ 再执行本脚本。"
        )
    exe = DIST / "HRAgent工作台.exe"
    if not exe.is_file():
        sys.exit(f"[x] 缺少主程序：{exe}")
    internal = DIST / "_internal"
    if not internal.is_dir():
        sys.exit(
            f"[x] 缺少 _internal 目录：{internal}\n"
            f"    onedir 模式的运行时都在这里，缺了装了也起不来。"
        )

    total = sum(f.stat().st_size for f in DIST.rglob("*") if f.is_file())
    n = len([f for f in DIST.rglob("*") if f.is_file()])
    print(f"[i] 产物校验通过：{n} 个文件，{total / 1024/ 1024:.1f} MB")


def compile_setup(iscc: str) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    print(f"[i] 编译安装包：{ISS}")
    # Inno Setup 输出很长，只在失败时全量打印
    r = subprocess.run(
        [iscc, str(ISS)],
        cwd=str(ROOT / "installer"),
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if r.returncode != 0:
        print(r.stdout[-4000:])
        print(r.stderr[-2000:])
        sys.exit(f"[x] 编译失败，退出码 {r.returncode}")
    print("[+] 编译成功")

    exes = sorted(OUT.glob("*.exe"), key=lambda p: p.stat().st_mtime)
    if not exes:
        sys.exit(f"[x] 编译成功但没找到产物：{OUT}")
    return exes[-1]


def main() -> None:
    print("=" * 60)
    print("HRAgent 安装包构建")
    print("=" * 60)

    check_dist()

    iscc = find_iscc()
    if not iscc:
        sys.exit(
            "[x] 没找到 Inno Setup 6（ISCC.exe）。\n"
            "    下载：https://jrsoftware.org/isdl.php\n"
            "    注意：官网已把下载迁到 GitHub releases，files.jrsoftware.org 的\n"
            "          老路径全部 404，别在那儿浪费时间。"
        )
    print(f"[i] Inno Setup：{iscc}")

    exe = compile_setup(iscc)
    size = exe.stat().st_size / 1024 / 1024
    print()
    print(f"[✓] 安装包：{exe}")
    print(f"    大小：{size:.1f} MB")
    print("    把它发给 HR，双击即可安装，可自选安装路径。")


if __name__ == "__main__":
    main()