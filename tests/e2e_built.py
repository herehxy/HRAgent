# -*- coding: utf-8 -*-
"""成品端到端验证：用**真实打包出来的 exe** 跑一遍
   ① 带旧数据（超长绝对路径）时"在线查看简历"能不能打开；
   ② 端口被占时能不能自己换端口起来；
   ③ --port-check 体检能不能跑。

跑法：python tests/e2e_built.py <发布目录>
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELEASE = sys.argv[1] if len(sys.argv) > 1 else r"D:\hra_build\release\HRAgent工作台"
EXE = os.path.join(RELEASE, "HRAgent工作台.exe")
WORK = r"D:\hra_test\e2e_built"
PASS, FAIL = [], []


def ok(cond, name, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'√' if cond else '×'}] {name}" + (f"　{extra}" if extra else ""))
    return bool(cond)


def _get(url, timeout=20):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as r:
        return r.status, r.read(), dict(r.headers)


def make_long_path_case():
    """造一份"带旧数据"：库里的绝对路径超过 260 字符。"""
    # 先清干净：上一轮残留的半截表结构会让应用的建表逻辑炸
    # （"no such column: candidate_id"）
    import shutil
    _deep = os.path.join(WORK, "深层数据根")
    if os.path.isdir(_deep):
        shutil.rmtree(_deep, ignore_errors=True)
    data = os.path.join(WORK, "深层数据根", "我的工作资料", "HRAgentData", "data")
    inbox = os.path.join(data, "inbox")
    os.makedirs(inbox, exist_ok=True)
    name = ("cgi-bin_mmwebwx-bin_webwxgetmsgimg__&MsgID=4475119088943494684"
            "&skey=@crypt_1abcc194_e8a768d710ef4237edca0d4cbbd6efec"
            "&mmweb_appid=wx_webfilehelper" + "x" * 40 + ".pdf")
    target = os.path.join(inbox, "20261021_082757__" + name)
    payload = b"%PDF-1.4\n% e2e long path\n" + b"y" * 4096
    # 用 \\?\ 前缀写（普通方式超 260 会 WinError 206）
    lp = ("\\\\?\\" + os.path.abspath(target)) if os.name == "nt" else target
    with open(lp, "wb") as fh:
        fh.write(payload)
    db = os.path.join(data, "workbench.db")
    conn = sqlite3.connect(db)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS documents (id INTEGER PRIMARY KEY,"
                     " file_name TEXT, file_path TEXT, archived_path TEXT,"
                     " mime TEXT, size INTEGER, text_len INTEGER, parse_ok INTEGER,"
                     " created_at TEXT)")
        conn.execute("INSERT INTO documents (file_name, file_path, archived_path,"
                     " mime, size, text_len, parse_ok, created_at)"
                     " VALUES (?,?,?,?,?,?,?,?)",
                     (name, target, target, "application/pdf", len(payload), 0, 1,
                      "2026-10-21 00:00:00"))
        conn.commit()
        did = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    finally:
        conn.close()
    return data, did, target, payload


def main() -> int:
    print("=" * 64)
    print("成品端到端验证：", EXE)
    print("=" * 64)
    if not os.path.isfile(EXE):
        print("找不到 exe：", EXE)
        return 1

    # ---- ① --port-check ----
    # 成品是 windowed 程序：stdout 没有 → 体检结果落在 <数据目录>/port_check.txt
    print("\n── ① --port-check 体检 ──")
    pc_dir = os.path.join(WORK, "pc")
    os.makedirs(pc_dir, exist_ok=True)
    env_pc = dict(os.environ)
    env_pc["TP_DATA_DIR"] = pc_dir
    env_pc["TP_NO_MSGBOX"] = "1"       # 自动化跑，别让弹窗把进程挂住
    try:
        subprocess.run([EXE, "--port-check"], capture_output=True, timeout=120,
                       env=env_pc)
    except Exception as exc:
        print("   （--port-check 异常：", exc, "）")
    pc_file = os.path.join(pc_dir, "port_check.txt")
    out = ""
    if os.path.isfile(pc_file):
        with open(pc_file, "r", encoding="utf-8", errors="replace") as fh:
            out = fh.read()
    ok("端口体检" in out, "1.1 --port-check 能跑并输出体检表",
       pc_file if out else "没生成报告文件")
    ok("能绑定" in out, "1.2 体检表列出每个端口的可用性")
    ok("系统保留端口段" in out, "1.3 体检表列出系统保留端口段")

    # ---- ② 带旧数据启动 + 在线查看简历 ----
    print("\n── ② 带旧数据（>260 字符路径）在线查看简历 ──")
    data, did, target, payload = make_long_path_case()
    print(f"     目标路径长度 = {len(target)}")
    env = dict(os.environ)
    env["TP_DATA_DIR"] = data
    env["TP_CONFIG_DIR"] = os.path.join(WORK, "config")
    env["TP_NO_BROWSER"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    os.makedirs(env["TP_CONFIG_DIR"], exist_ok=True)
    # 成品是 **windowed 程序**（打包时 console=False）→ stdout 无处可去，
    # 启动器把日志重定向到 <TP_DATA_DIR>/webapp.log。读那个文件。
    logf = os.path.join(data, "webapp.log")
    with open(logf, "w", encoding="utf-8") as lf:
        proc = subprocess.Popen([EXE, "--no-browser"], env=env, stdout=lf,
                                stderr=subprocess.STDOUT,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    port = None
    deadline = time.time() + 120
    while time.time() < deadline:
        time.sleep(1.0)
        try:
            with open(logf, "r", encoding="utf-8", errors="replace") as rf:
                text = rf.read()
        except OSError:
            text = ""
        import re
        m = re.search(r"端口 (\d+) 已监听", text)
        if m:
            port = int(m.group(1))
            break
        if proc.poll() is not None:
            break
    ok(port is not None, "2.1 成品 exe 能启动并监听",
       f"端口={port}")
    if port:
        try:
            st, body, hdr = _get(f"http://127.0.0.1:{port}/api/documents/{did}/file?inline=1")
            ok(st == 200 and body == payload,
               "2.2 在线查看简历（超长路径）返回原件",
               f"状态={st} 字节={len(body)}/{len(payload)}")
            ok("inline" in (hdr.get("content-disposition") or ""),
               "2.3 以 inline 方式下发（浏览器直接预览）")
            st2, body2, _ = _get(f"http://127.0.0.1:{port}/api/documents/{did}/file")
            ok(st2 == 200 and body2 == payload, "2.4 下载原件同样正常",
               f"状态={st2}")
        except Exception as exc:
            ok(False, "2.2 在线查看简历（超长路径）返回原件", f"{type(exc).__name__}: {exc}")
        # 端口体检（成品，运行中的实例也要能查）
        pc2 = os.path.join(WORK, "pc2")
        os.makedirs(pc2, exist_ok=True)
        env_pc2 = dict(env)
        env_pc2["TP_DATA_DIR"] = pc2
        env_pc2["TP_NO_MSGBOX"] = "1"
        try:
            subprocess.run([EXE, "--port-check"], capture_output=True, timeout=120,
                           env=env_pc2)
        except Exception:
            pass
        ok(os.path.isfile(os.path.join(pc2, "port_check.txt")),
           "2.5 成品 --port-check 正常（报告落文件）")
    try:
        proc.terminate()
        proc.wait(timeout=15)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass

    # ---- ③ 端口被占时自动换端口 ----
    print("\n── ③ 端口被占时自动换端口（成品）──")
    holders = []
    try:
        for p in range(8756, 8781):
            try:
                hs = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                hs.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                hs.bind(("127.0.0.1", p))
                hs.listen(1)
                holders.append(hs)
            except OSError:
                pass
        env2 = dict(env)
        env2["TP_DATA_DIR"] = os.path.join(WORK, "data2")
        env2["TP_CONFIG_DIR"] = os.path.join(WORK, "config2")
        os.makedirs(env2["TP_DATA_DIR"], exist_ok=True)
        os.makedirs(env2["TP_CONFIG_DIR"], exist_ok=True)
        log2 = os.path.join(env2["TP_DATA_DIR"], "webapp.log")
        with open(log2, "w", encoding="utf-8") as lf:
            proc2 = subprocess.Popen([EXE, "--no-browser"], env=env2, stdout=lf,
                                     stderr=subprocess.STDOUT,
                                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        text = ""
        deadline = time.time() + 150
        while time.time() < deadline:
            time.sleep(1.5)
            try:
                with open(log2, "r", encoding="utf-8", errors="replace") as rf:
                    text = rf.read()
            except OSError:
                text = ""
            if "已监听" in text or "启动中止" in text:
                break
            if proc2.poll() is not None:
                break
        ok("启动中止" not in text, "3.1 端口全被占时**没有**启动中止（不再报端口冲突）")
        ok("自动改用" in text or "系统分配" in text,
           "3.2 自动改用别的端口", [x for x in text.splitlines() if "端口" in x][:1])
        try:
            proc2.terminate()
            proc2.wait(timeout=15)
        except Exception:
            try:
                proc2.kill()
            except Exception:
                pass
    finally:
        for hs in holders:
            try:
                hs.close()
            except OSError:
                pass

    print("\n" + "=" * 64)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  [×]", f)
    print("=" * 64)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
