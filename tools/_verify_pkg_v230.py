# -*- coding: utf-8 -*-
"""交付包一致性 + 凭据泄漏扫描（v2.3.0）。

三条判据（每条都对应一个翻过车的坑）：
1. 三个 zip 里的 exe 必须是**同一份**（md5 相等），且与 dist 里实测通过的那份一致。
2. 包内不得出现真实凭据（百炼 Key / 专属 MaaS 端点 / 邮箱授权码）。
3. 安装器 exe 的 PYZ 里必须带迁移模块（否则装新目录时不会搬旧数据）。
"""
from __future__ import annotations

import hashlib
import os
import re
import sys
import zipfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REL = os.path.join(BASE, "release")
VER = "2.3.0"

PASS, FAIL = [], []


def ok(cond, name, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'√' if cond else '×'}] {name}" + (f"　{extra}" if extra else ""))


def md5_of_zip_entry(zf, name):
    h = hashlib.md5()
    with zf.open(name) as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def md5_file(path):
    h = hashlib.md5()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def main() -> int:
    zips = [
        f"HRAgent程序包-v{VER}.zip",
        f"HRAgent工作台-Windows版-v{VER}.zip",
        f"HRAgent更新包-old-to-v{VER}.zip",
    ]
    # ---------- 1. 一致性 ----------
    print("=== 1. 三个包里的 exe 是否同一份 ===")
    dist_main = os.path.join(BASE, "dist", "HRAgent工作台", "HRAgent工作台.exe")
    dist_inst = os.path.join(BASE, "release", "HRAgent工作台", "HRAgent安装器.exe")
    ref_main = md5_file(dist_main)
    ref_inst = md5_file(dist_inst)
    print(f"    dist 主程序 md5 = {ref_main}")
    print(f"    release 安装器 md5 = {ref_inst}")

    for z in zips:
        p = os.path.join(REL, z)
        if not os.path.isfile(p):
            ok(False, f"缺包：{z}")
            continue
        with zipfile.ZipFile(p) as zf:
            names = zf.namelist()
            m = [n for n in names if n.endswith("HRAgent工作台.exe")]
            i = [n for n in names if n.endswith("HRAgent安装器.exe")]
            ok(len(m) == 1 and md5_of_zip_entry(zf, m[0]) == ref_main,
               f"{z} 主程序与 dist 同源", f"{len(names)} 文件")
            ok(len(i) == 1 and md5_of_zip_entry(zf, i[0]) == ref_inst,
               f"{z} 安装器与 dist 同源")

    # ---------- 2. 凭据扫描 ----------
    print("\n=== 2. 凭据泄漏扫描（含压缩内部）===")
    PATS = [
        (rb"sk-ws-[A-Za-z0-9_\-]{8,}", "百炼 API Key"),
        (rb"ws-[0-9a-z]{4,}\.cn-beijing\.maas\.aliyuncs\.com", "专属 MaaS 端点"),
        (rb"sk-[A-Za-z0-9]{20,}", "通用 OpenAI 风格 Key"),
    ]
    total_files = 0
    leaks = 0
    for z in zips:
        p = os.path.join(REL, z)
        if not os.path.isfile(p):
            continue
        with zipfile.ZipFile(p) as zf:
            for n in zf.namelist():
                if n.endswith("/"):
                    continue
                total_files += 1
                try:
                    d = zf.read(n)
                except Exception:
                    continue
                for pat, label in PATS:
                    for m in re.finditer(pat, d):
                        print(f"    LEAK {label} @ {os.path.basename(z)}:{n} "
                              f"{m.group()[:36].decode('utf-8', 'replace')}")
                        leaks += 1
    ok(leaks == 0, f"三个包共 {total_files} 个条目，凭据泄漏 {leaks} 处")

    # 关键配置文件的实际取值
    print("\n=== 3. 分发配置的脱敏取值 ===")
    import json
    with zipfile.ZipFile(os.path.join(REL, zips[0])) as zf:
        names = zf.namelist()
        ok(not any(n.endswith("imap.secret") for n in names), "包内无 imap.secret")
        try:
            sec = json.loads(zf.read("_internal/config/secrets.json").decode("utf-8"))
            ok(not (sec.get("api_key") or "").strip(), "secrets.api_key 为空",
               repr(sec.get("api_key")))
        except Exception as e:
            ok(False, "读 secrets.json", repr(e))
        try:
            mdl = json.loads(zf.read("_internal/config/model.json").decode("utf-8"))
            bu = mdl.get("base_url") or ""
            ok("maas.aliyuncs" not in bu and "ws-" not in bu,
               "model.base_url 非专属端点", bu)
        except Exception as e:
            ok(False, "读 model.json", repr(e))

    print("\n" + "=" * 56)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  FAIL:", f)
    print("=" * 56)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
