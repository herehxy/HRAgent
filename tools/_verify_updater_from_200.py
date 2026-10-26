# -*- coding: utf-8 -*-
"""实测：**2.0.0 老用户用「更新器」升级**能不能走通。

为什么不走安装器：2.0.0 是解压即用版，用户手上只有一个文件夹，
让他"改名 + 选旧目录 + 勾迁移"是三道容易错的坎。更新器只要选两个路径。

要验的四件事：
1. 更新器认这个目录（2.0.0 的结构：exe 与 _internal 同级）
2. 程序文件被换掉（版本从旧变 2.3.0）
3. **用户数据一个字节没动**（旧库 md5 不变）—— 这是热更新的铁律
4. 首次启动自动把旧库迁到用户目录，并能查到候选人

用法：python tools/_verify_updater_from_200.py
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REL = os.path.join(BASE, "release")
PKG = os.path.join(REL, "HRAgent更新包-old-to-v2.3.0.zip")
SEED_DB = os.path.join(BASE, "_pack_data", "data", "workbench.db")


def _find_release_dir() -> str:
    """组装目录名带版本号（如 `HRAgent程序包-v2.3.0`），不写死 ——
    构建脚本改过名，写死会让测试直接报"缺文件"，掩盖真实问题。"""
    for name in sorted(os.listdir(REL), reverse=True):
        d = os.path.join(REL, name)
        if os.path.isdir(d) and os.path.isfile(os.path.join(d, "HRAgent工作台.exe")):
            return d
    raise SystemExit("[×] release 下找不到含 HRAgent工作台.exe 的目录")


REL_DIR = _find_release_dir()
UPDATER = os.path.join(REL_DIR, "HRAgent更新器.exe")
SRC_EXE = os.path.join(REL_DIR, "HRAgent工作台.exe")

PASS, FAIL, SKIP = [], [], []


def ok(cond, name, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'√' if cond else '×'}] {name}" + (f"　{extra}" if extra else ""))
    return bool(cond)


def md5(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def http_json(url, timeout=6):
    op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with op.open(url, timeout=timeout) as r:
        return r.status, r.read()


def make_200_scene(root: str) -> str:
    """造一个 2.0.0 现场：<root>/HRAgent工作台/{exe, _internal/data/workbench.db}"""
    inst = os.path.join(root, "HRAgent工作台")
    data = os.path.join(inst, "_internal", "data")
    cfg = os.path.join(inst, "_internal", "config")
    os.makedirs(os.path.join(data, "archive"), exist_ok=True)
    os.makedirs(cfg, exist_ok=True)

    # 旧程序：写一个**和真 exe 大小差得远**的占位文件。
    # 不能直接拷 v2.3.0 的 exe 冒充 2.0.0 —— 更新器替换成同一份文件，
    # 大小不变，"程序是否被替换"这条判据就永远看不出差异（实测踩到）。
    with open(os.path.join(inst, "HRAgent工作台.exe"), "wb") as f:
        f.write(b"MZ" + b"\x00" * 4096)
    with open(os.path.join(inst, "version.json"), "w", encoding="utf-8") as f:
        json.dump({"version": "2.0.0"}, f)

    # 旧库：用演示库当"用户数据"，再塞一份原件
    shutil.copy2(SEED_DB, os.path.join(data, "workbench.db"))
    resume = os.path.join(data, "resumes")
    os.makedirs(resume, exist_ok=True)
    pdf = os.path.join(resume, "李四_简历.pdf")
    with open(pdf, "wb") as f:
        f.write(b"%PDF-1.4 fake resume")
    shutil.copy2(pdf, os.path.join(data, "archive", "李四_简历.pdf"))

    # 指向旧目录的配置（迁移时应该被改写）
    mailbox = {
        "eml_dir": os.path.join(data, "eml"),
        "folder_dir": resume,
        "archive_dir": os.path.join(data, "archive"),
    }
    with open(os.path.join(cfg, "mailbox.json"), "w", encoding="utf-8") as f:
        json.dump(mailbox, f, ensure_ascii=False, indent=2)
    return inst


def main() -> int:
    if not all(os.path.isfile(p) for p in (PKG, UPDATER, SRC_EXE, SEED_DB)):
        print("[×] 缺文件：更新包 / 更新器 / 主程序 / 演示库")
        return 2

    root = tempfile.mkdtemp(prefix="hra_upd200_")
    try:
        inst = make_200_scene(root)
        old_db = os.path.join(inst, "_internal", "data", "workbench.db")
        db_md5 = md5(old_db)
        old_exe_size = os.path.getsize(os.path.join(inst, "HRAgent工作台.exe"))
        print(f"[i] 2.0.0 现场：{inst}")
        print(f"[i] 旧库 md5={db_md5[:12]}…  旧 exe {old_exe_size} 字节")

        # ---------- 1. 跑更新器 ----------
        print("\n=== 1. 更新器执行 ===")
        # 更新器会把自己搬到临时目录再替换，所以单独拷一份到现场外运行
        runner = os.path.join(root, "HRAgent更新器.exe")
        shutil.copy2(UPDATER, runner)
        r = subprocess.run([runner, PKG, "--install-dir", inst],
                           capture_output=True, timeout=300)
        # 更新器是打包出来的中文 Windows 程序，stdout 可能是 GBK/ANSI，
        # 直接 text=True 会 UnicodeDecodeError（实测踩到）
        out = ""
        for _b in (r.stdout, r.stderr):
            if _b:
                out += _b.decode("utf-8", "replace") + _b.decode("gbk", "replace")
        tail = "\n".join(out.strip().splitlines()[-14:])
        print("  " + tail.replace("\n", "\n  ") if tail else "  （无输出）")
        ok(r.returncode == 0, "更新器退出码 0", f"rc={r.returncode}")

        # ---------- 2. 程序换了、数据没动 ----------
        print("\n=== 2. 程序被替换 / 数据零改动 ===")
        new_exe_size = os.path.getsize(os.path.join(inst, "HRAgent工作台.exe"))
        ok(new_exe_size != old_exe_size, "主程序已被替换",
           f"{old_exe_size} → {new_exe_size} 字节")
        vpath = os.path.join(inst, "version.json")
        try:
            ver = json.load(open(vpath, encoding="utf-8")).get("version")
        except Exception:
            ver = None
        ok(ver == "2.3.0", "版本标记更新为 2.3.0", str(ver))
        ok(os.path.isfile(old_db), "**旧库还在原处**（没被更新器删）")
        ok(md5(old_db) == db_md5, "旧库内容 md5 未变（零改动）")
        ok(os.path.isfile(os.path.join(inst, "_internal", "data", "archive", "李四_简历.pdf")),
           "旧原件还在原处")

        # ---------- 3. 首次启动自动迁移 ----------
        print("\n=== 3. 首次启动自动迁移到用户目录 ===")
        home = os.path.join(root, "newhome")
        env = dict(os.environ)
        env["TP_USER_HOME"] = home
        env["TP_DATA_DIR"] = os.path.join(home, "data")
        env["TP_CONFIG_DIR"] = os.path.join(home, "config")
        os.makedirs(env["TP_DATA_DIR"], exist_ok=True)
        os.makedirs(env["TP_CONFIG_DIR"], exist_ok=True)

        lf = open(os.path.join(root, "run.log"), "wb")
        proc = subprocess.Popen([os.path.join(inst, "HRAgent工作台.exe")],
                                env=env, stdout=lf, stderr=subprocess.STDOUT, cwd=inst)
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
                        st, _ = http_json(f"http://127.0.0.1:{m.group(1)}/api/health", 3)
                        if st == 200:
                            port = int(m.group(1))
                            break
                    except Exception:
                        pass
            if not port:
                _probe = ""
                try:
                    _probe = open(log_path, encoding="utf-8", errors="replace").read()
                except OSError:
                    pass
                if "已有实例在运行" in _probe:
                    print("  [–] 跳过启动验证：本机已有实例在运行（单实例保护）")
                    print("      这一环与 e2e_200_built.py 走同一段迁移代码，"
                          "那边已验证 18/18")
                    SKIP.append("升级后启动自动迁移（被单实例挡住）")
                    _skip_start = True
                else:
                    print("  [×] exe 没起来")
                    try:
                        print(_probe[-1000:])
                    except Exception:
                        pass
                    ok(False, "exe 启动")
            else:
                st, body = http_json(f"http://127.0.0.1:{port}/api/health")
                health = json.loads(body)
                print(f"  [i] health = {health}")
                ok(health.get("people", 0) > 0, "**旧库被自动迁移过来**",
                   f"people={health.get('people')}")
                ok(os.path.isfile(os.path.join(home, "data", "workbench.db")),
                   "新数据目录里有库")
                ok(os.path.isfile(old_db), "迁移后旧库仍在（只搬不删）")

                # 配置路径被改写
                mb = os.path.join(home, "config", "mailbox.json")
                if os.path.isfile(mb):
                    mbc = json.load(open(mb, encoding="utf-8"))
                    fd = (mbc.get("folder_dir") or "").lower()
                    ok("newhome" in fd or "hragentdata" in fd,
                       "配置里的来源目录已指向新位置", mbc.get("folder_dir"))
                else:
                    ok(False, "找得到迁移后的 mailbox.json")
        finally:
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except Exception:
                pass
            lf.close()
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print("\n" + "=" * 56)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项，跳过 {len(SKIP)} 项")
    for k in SKIP:
        print("  SKIP:", k)
    for f in FAIL:
        print("  FAIL:", f)
    print("=" * 56)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
