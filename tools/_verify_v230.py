# -*- coding: utf-8 -*-
"""打包后验证：包里是否真的带上了本轮前端改动 + 迁移能力。

不靠"打包成功"推断 —— 拆 exe 查 PYZ 里的模块与字符串常量。
用法：python tools/_verify_v230.py
"""
from __future__ import annotations

import io
import os
import sys

from PyInstaller.archive.readers import CArchiveReader
from PyInstaller.loader.pyimod01_archive import ZlibArchiveReader

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(BASE, "dist", "HRAgent工作台", "HRAgent工作台.exe")

PASS, FAIL = [], []


def ok(cond, name, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'√' if cond else '×'}] {name}" + (f"　{extra}" if extra else ""))
    return bool(cond)


def all_strings(code, acc=None):
    """递归取字节码里所有字符串常量（CSS/JS 都在这）。"""
    if acc is None:
        acc = []
    for k in code.co_consts:
        if isinstance(k, str):
            acc.append(k)
        elif hasattr(k, "co_consts"):
            all_strings(k, acc)
    return acc


def all_names(code, acc=None):
    if acc is None:
        acc = set()
    acc.update(code.co_names)
    for k in code.co_consts:
        if hasattr(k, "co_names"):
            all_names(k, acc)
    return acc


def main() -> int:
    if not os.path.isfile(MAIN):
        print("[×] 找不到主程序 exe")
        return 2

    data = open(MAIN, "rb").read()
    off = data.find(b"PYZ\x00")
    z = ZlibArchiveReader(MAIN, off)
    print(f"[i] 主程序 PYZ 模块数：{len(z.toc)}")

    # ---------- 1. 前端（v2 主界面）----------
    print("\n=== 1. 前端改动是否进包 ===")
    code = z.extract("app.ui_v2")
    s = "\n".join(all_strings(code))
    ok("--field" in s, "新增输入框令牌 --field")
    ok("--scrim" in s, "新增遮罩令牌 --scrim")
    ok("border:1px solid transparent" in s, "输入框改用透明边框")
    ok("0 0 0 3px rgba(51,112,255,.12)" in s, "聚焦用柔光而非实心 outline")
    ok("openDlg" in s, "统一开合入口 openDlg")
    ok("dlgScrimClose" in s, "遮罩点击受门控锁约束")
    ok('aria-labelledby="dlgTitle"' in s, "弹窗 aria-labelledby 就位")
    ok("prefers-reduced-motion" in s, "尊重系统减少动效")
    ok("min(400px" in s and "min(940px" in s, "尺寸分档 400/940 就位")
    ok("flex:1;min-height:0;overflow-y:auto" in s, "正文独立滚动区")
    ok("dlg .dbar{display:flex;gap:8px;justify-content:flex-end;margin-top:16px;flex:none}" in s,
       "底栏 flex:none（固定不滚）")

    # ---------- 2. 前端（v1 回退页）----------
    print("\n=== 2. 回退页也带改动 ===")
    code1 = z.extract("app.ui")
    s1 = "\n".join(all_strings(code1))
    ok("--field" in s1, "v1 也加了 --field")
    ok("openModal" in s1, "v1 走统一入口 openModal")
    ok("modalScrimClose" in s1, "v1 遮罩门控")
    ok('role="dialog"' in s1, "v1 有 role=dialog")

    # ---------- 3. 迁移能力 ----------
    print("\n=== 3. 迁移能力（旧数据）===")
    pm = z.extract("app.profile_migrate")
    pn = all_names(pm)
    ok("normalize_config_paths" in pn, "配置路径改写 normalize_config_paths")
    ok("_is_under" in pn, "_is_under 判定")
    ok("layout" in pn, "layout() 识别旧布局")
    ok("migrate_profile" in pn, "migrate_profile 入口")
    ps = "\n".join(all_strings(pm))
    ok("_internal" in ps, "layout 认识 2.0.0 的 _internal 布局")

    # 启动流程也调了配置修正（覆盖"先升级后换机"）
    launcher = None
    for cand in z.toc:
        if cand.endswith("webapp_launcher") or cand == "webapp_launcher":
            launcher = cand
            break
    if launcher:
        lc = z.extract(launcher)
        ln = all_names(lc)
        ok("normalize_config_paths" in ln, "启动流程也调 normalize_config_paths")
    else:
        # 入口脚本在 CArchive 里
        r = CArchiveReader(MAIN)
        if "webapp_launcher" in r.toc:
            import marshal
            lc = marshal.loads(r.extract("webapp_launcher"))
            ln = all_names(lc)
            ok("normalize_config_paths" in ln, "启动流程也调 normalize_config_paths（CArchive）")
        else:
            ok(False, "找不到 webapp_launcher 入口")

    # ---------- 4. 版本号 ----------
    print("\n=== 4. 版本号 ===")
    vc = z.extract("app.version")
    vs = [x for x in all_strings(vc) if x.count(".") == 2 and x[0].isdigit()]
    ok("2.3.0" in vs, "包内 VERSION = 2.3.0", str(vs))

    print("\n" + "=" * 56)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  FAIL:", f)
    print("=" * 56)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
