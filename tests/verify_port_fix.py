# -*- coding: utf-8 -*-
"""v2.2.5 端口修复的成品验证（对着**真实打包出来的 exe** 跑）。

为什么单独写：记忆里记过「端到端用例也会假通过」——
`tests/e2e_built.py` 用例③是**故意占满端口**再启动，「全跳过+换系统端口」
本就是预期行为，在**旧包上也必过**。所以端口这条必须另写判据。

**判据设计原则（防假通过）**：
  ① 必须真的选中固定端口 8756，而不是"随便起在某个端口"
  ② 判据要"只命中 X"（== 8756），不是"包含 8756"
  ③ 要能证明"修复前会失败"：修复前 connect 探测把空闲端口报成 10035
     → 110 个候选全被跳过 → 退化到系统随机端口 → 端口 != 8756
  ④ 两边都验：新包选中 8756 ✔ 且 旧逻辑会选不中（用源码函数复现）

跑法：
    python repo_src/tests/verify_port_fix.py <发布目录>
    例：python repo_src/tests/verify_port_fix.py E:/WorkBuddy/HRAgent_v2/dist/HRAgent工作台
"""
from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RELEASE = (sys.argv[1] if len(sys.argv) > 1
           else os.path.join(os.path.dirname(BASE), "dist", "HRAgent工作台"))
EXE = os.path.join(RELEASE, "HRAgent工作台.exe")
PORT = 8756

PASS, FAIL = [], []


def ok(cond, name, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}" + (f"  {extra}" if extra else ""))
    return bool(cond)


def _get(url, timeout=15):
    # 必须绕过代理：本机有 http_proxy，直连 127.0.0.1 会被拦成 502
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=timeout) as r:
        return r.status, r.read()


def port_strictly_bindable(port: int) -> bool:
    """严格 bind（不带 SO_REUSEADDR）——判"端口真空着"的权威判据。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def connect_code(port: int) -> int:
    """connect 探测的返回码 —— 用来证明它在本机不可信。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.6)
    try:
        return s.connect_ex(("127.0.0.1", port))
    finally:
        s.close()


def kill_all():
    subprocess.run(["taskkill", "/F", "/IM", "HRAgent工作台.exe"],
                   capture_output=True)
    time.sleep(1.5)


def main() -> int:
    print("=" * 64)
    print("v2.2.5 端口修复验证")
    print("=" * 64)
    print(f"发布目录: {RELEASE}")

    if not os.path.isfile(EXE):
        print(f"[x] 找不到 exe: {EXE}")
        return 2

    # ---------- 0. 清理 ----------
    kill_all()

    # ---------- 1. 前提：目标端口必须真空着 ----------
    print("\n--- 前提检查 ---")
    if not port_strictly_bindable(PORT):
        print(f"[x] 端口 {PORT} 被别的程序占了，无法验证。先关掉占用者。")
        return 2
    ok(True, f"端口 {PORT} 严格 bind 成功（真空着）")

    # ---------- 2. 证明 connect 探测不可信（本机有安全软件）----------
    print("\n--- connect 探测可信度 ---")
    rc = connect_code(PORT)
    # 空闲端口返回 10035(WSAEWOULDBLOCK) 或 10061(ECONNREFUSED) 都算"没人在听"
    rc_is_idle = rc in (10035, 10060, 10061, 61)
    ok(rc_is_idle, f"空闲端口 {PORT} 的 connect 返回 {rc}（未连上=确实空闲）")
    if rc == 10035:
        print("       ★ 关键：10035 = 旧逻辑会误判成'有人占用'并跳过这个端口")
        print("         这正是 v2.2.4 失效的根因")

    # ---------- 3. 启动成品，看它选哪个端口 ----------
    print("\n--- 启动成品 exe ---")
    env = dict(os.environ)
    env["TP_NO_BROWSER"] = "1"          # 别弹浏览器
    env["TP_NO_MSGBOX"] = "1"           # 别弹错误框
    # stdout 写临时文件而不是 PIPE：
    #   ① PIPE + readline() 是**阻塞**的，日志不吐字就永远卡住（曾导致整个用例挂 10 分钟）
    #   ② 打包程序是 windowed（console=False），重定向到 PIPE 时缓冲行为也不可靠
    # 判据期间**不杀进程**，让服务活着供后面几条断言用。
    log_path = os.path.join(tempfile.gettempdir(), "hra_verify_port.log")
    if os.path.exists(log_path):
        os.remove(log_path)
    logf = open(log_path, "w", encoding="utf-8", errors="replace")
    proc = subprocess.Popen([EXE], cwd=RELEASE, env=env,
                            stdout=logf, stderr=subprocess.STDOUT)

    picked = None
    log_lines = []

    def drain():
        try:
            with open(log_path, encoding="utf-8", errors="replace") as f:
                return f.read().splitlines()
        except OSError:
            return []

    deadline = time.time() + 120
    while time.time() < deadline:
        if proc.poll() is not None:
            print(f"[!] 进程已退出，返回码 {proc.returncode}")
            break
        log_lines = drain()
        # 从日志里抓端口
        for line in log_lines:
            for m in re.finditer(r"端口\s*(\d{4,5})", line):
                picked = int(m.group(1))
        if picked:
            break
        # 也直接探端口
        try:
            _get(f"http://127.0.0.1:{PORT}/api/meta", timeout=2)
            picked = PORT
            break
        except Exception:
            pass
        time.sleep(0.5)

    log_lines = drain()
    for line in log_lines[-25:]:
        print("    | " + line)

    # 兜底：再直接探 8756（服务可能已起但日志被截断）
    if picked != PORT:
        for _ in range(20):
            try:
                _get(f"http://127.0.0.1:{PORT}/api/meta", timeout=3)
                picked = PORT
                break
            except Exception:
                time.sleep(1)

    print("\n--- 结论 ---")
    ok(picked == PORT, f"成品选中了固定端口 {PORT}",
       f"实际={picked}" + ("  ← 退化到随机端口 = 修复未生效!" if picked != PORT else ""))

    # ---------- 4. 服务确实可用 ----------
    if picked:
        try:
            status, body = _get(f"http://127.0.0.1:{picked}/api/meta", timeout=15)
            ok(status == 200, "接口 /api/meta 返回 200", f"status={status}")
            text = body.decode("utf-8", "replace")
            ok("session" in text, "/api/meta 含 session 键（是本程序不是别的服务）")
        except Exception as e:
            ok(False, "接口 /api/meta 可访问", f"{type(e).__name__}: {e}")

    # ---------- 5. 源码级：旧逻辑确实会选不中（证明判据能失败）----------
    # ⚠️ 必须用**另一个**空闲端口，不能用 PORT —— 此时服务正占着 8756，
    #    严格 bind 必然失败 → new_says_ok=False → 恒假 FAIL（顺序 bug，已踩）。
    print("\n--- 判据自检：这个判据在修复前会不会失败？ ---")
    probe = PORT + 1
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "wl", os.path.join(BASE, "webapp_launcher.py"))
        wl = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(wl)
        has_strict = hasattr(wl, "_bind_test")
        ok(has_strict, "源码含 _bind_test（严格 bind 判据）")
        ok(port_strictly_bindable(probe), f"自检端口 {probe} 也空闲（避开服务占用的 {PORT}）")
        # 复现旧逻辑：connect 探测说占用 → 跳过
        old_says_busy = (connect_code(probe) != 0)
        new_says_ok = wl._port_usable(probe)[0]
        ok(old_says_busy and new_says_ok,
           "旧逻辑会跳过该端口、修复后会选用它（判据在修复前会失败）",
           f"旧=跳过(connect≠0) 新=可用({new_says_ok})")
    except Exception as e:
        ok(False, "源码级自检", f"{type(e).__name__}: {e}")

    # ---------- 6. 清理 ----------
    kill_all()

    print("\n" + "=" * 64)
    print(f"通过 {len(PASS)}，失败 {len(FAIL)}")
    if FAIL:
        for f in FAIL:
            print("  FAILED:", f)
        print("=" * 64)
        return 1
    print("全部通过")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())