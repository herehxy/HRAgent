# -*- coding: utf-8 -*-
"""验证"包里不带数据"的完整链路（2026-10-26 业务方要求）。

三条判据：
1. **包里**没有 workbench.db / seed/ / archive 里的简历 —— zip 层面直接查。
2. **全新目录首次运行** → 库是空的（people=0）、能登录、页面正常 —— 不靠推断。
3. 首次运行**不会**把任何演示数据搬进用户目录（种子已不存在，扫不到）。

用法：python tools/_verify_empty_first_run.py
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REL = os.path.join(BASE, "release")
PKG_ZIP = os.path.join(REL, "HRAgent程序包-v2.3.0.zip")
EXE_DIR = os.path.join(REL, "HRAgent工作台")
EXE = os.path.join(EXE_DIR, "HRAgent工作台.exe")

PASS, FAIL = [], []


def ok(cond, name, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'√' if cond else '×'}] {name}" + (f"　{extra}" if extra else ""))
    return bool(cond)


def http_json(url, data=None, timeout=8):
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, data=data,
                                 headers={"Content-Type": "application/json"} if data else {})
    with op.open(req, timeout=timeout) as r:
        return r.status, r.read()


def main() -> int:
    # ---------- 1. zip 层面：包里不得有任何数据 ----------
    print("=== 1. 包内是否夹带数据 ===")
    with zipfile.ZipFile(PKG_ZIP) as zf:
        names = zf.namelist()
        db = [n for n in names if n.endswith("workbench.db")]
        seed = [n for n in names if "/seed/" in n or n.startswith("seed/")]
        archive = [n for n in names if "/archive/" in n]
        ok(not db, "包内无 workbench.db", f"{len(db)} 个" if db else "")
        ok(not seed, "包内无 seed/ 目录", f"{len(seed)} 个" if seed else "")
        ok(not archive, "包内无 archive 简历原件", f"{len(archive)} 个" if archive else "")
        # 配置模板必须还在，否则界面下拉框全空
        cfg = [n for n in names if n.startswith("_internal/config/")]
        ok(len(cfg) >= 5, "配置模板仍在（界面默认值靠它）", f"{len(cfg)} 个文件")
        ok(any(n.endswith("interview.json") for n in cfg), "面试单位字典在")
        ok(any(n.endswith("stages.json") for n in cfg), "阶段配置在")

    # ---------- 2. 全新目录首次运行：必须是空库 ----------
    print("\n=== 2. 全新安装首次运行（空目录）===")
    home = tempfile.mkdtemp(prefix="hra_empty_")
    env = dict(os.environ)
    env["TP_USER_HOME"] = home
    env["TP_DATA_DIR"] = os.path.join(home, "data")
    env["TP_CONFIG_DIR"] = os.path.join(home, "config")
    os.makedirs(env["TP_DATA_DIR"], exist_ok=True)
    os.makedirs(env["TP_CONFIG_DIR"], exist_ok=True)

    lf = open(os.path.join(home, "stdout.log"), "wb")
    proc = subprocess.Popen([EXE], env=env, stdout=lf, stderr=subprocess.STDOUT, cwd=EXE_DIR)
    port = None
    log_path = os.path.join(env["TP_DATA_DIR"], "webapp.log")
    try:
        for _ in range(90):
            time.sleep(1)
            if proc.poll() is not None:
                break
            try:
                txt = open(log_path, encoding="utf-8", errors="replace").read()
            except OSError:
                continue
            m = re.search(r"http://127\.0\.0\.1:(\d{4,5})", txt)
            if m:
                try:
                    st, body = http_json(f"http://127.0.0.1:{m.group(1)}/api/health", timeout=3)
                    if st == 200:
                        port = int(m.group(1))
                        break
                except Exception:
                    pass
        if not port:
            print("[×] exe 没起来")
            try:
                print(open(log_path, encoding="utf-8", errors="replace").read()[-1500:])
            except OSError:
                pass
            return 1

        print(f"[i] exe 起来了，端口 {port}")
        st, body = http_json(f"http://127.0.0.1:{port}/api/health")
        health = __import__("json").loads(body)
        print(f"[i] health = {health}")
        ok(health.get("people") == 0, "首次运行是**空库**（people=0）",
           f"people={health.get('people')}")

        # 能登录（HR 账号是建库时创建的，不依赖演示数据）
        st, body = http_json(f"http://127.0.0.1:{port}/api/login",
                             data=__import__("json").dumps(
                                 {"username": "hr", "password": "change-me"}).encode())
        ok(st == 200, "空库也能登录（HR 账号正常创建）", f"status={st}")

        # 页面正常
        st, body = http_json(f"http://127.0.0.1:{port}/")
        html = body.decode("utf-8", "replace")
        ok(st == 200 and len(html) > 100000, "首页正常渲染", f"{len(html)} 字节")
        ok("--field" in html, "前端新样式的在")

        # 用户目录里没有任何简历原件
        data_dir = env["TP_DATA_DIR"]
        junk = []
        for root, dirs, files in os.walk(data_dir):
            if os.path.basename(root) in ("archive", "resumes", "eml"):
                junk.extend(files)
        ok(not junk, "用户数据目录里没有被塞进来的简历", f"{len(junk)} 个文件")

        # 日志里不该出现"迁移"
        logtxt = open(log_path, encoding="utf-8", errors="replace").read()
        ok("已迁移" not in logtxt, "日志里没有'已迁移'（没搬任何东西进来）")
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=10)
        except Exception:
            pass
        lf.close()
        shutil.rmtree(home, ignore_errors=True)

    print("\n" + "=" * 56)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  FAIL:", f)
    print("=" * 56)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
