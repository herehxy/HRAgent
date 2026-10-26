# -*- coding: utf-8 -*-
"""v2.2.4 专项自检：长路径（带旧数据点"在线查看简历"报错）与端口冲突。

背景（都是业务方那台电脑上真实出现的）：
  · 长路径：旧数据里存的是绝对路径（数据根 + 150+ 字符的附件名），
    迁移后路径变长，越过 Windows 的 260 字符上限 →
    os.path.isfile 静默返回 False → 界面报"原件不在磁盘上"，
    实际文件好好地躺在磁盘上。**在线查看简历必报错。**
  · 端口冲突：天天弹端口冲突，后台都退了也用不了。

跑法：
    python tests/test_longpath_port.py
"""
from __future__ import annotations

import io
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

# 数据目录放 D 盘（开发机 C 盘可能已满）；必须在 import app 之前定下来
_TMP_ROOT = os.environ.get("TP_TEST_TMP") or r"D:\hra_test\lp_port"
os.makedirs(_TMP_ROOT, exist_ok=True)
# 数据根故意做深：真实场景是"桌面/我的工作资料/HRAgentData/data"这种层级，
# 加上长附件名就逼近 260。测试要复现的就是这个组合。
_DEEP = os.path.join("深层数据根", "我的工作资料", "HRAgentData")
os.environ.setdefault("TP_DATA_DIR", os.path.join(_TMP_ROOT, _DEEP, "data"))
os.environ.setdefault("TP_CONFIG_DIR", os.path.join(_TMP_ROOT, _DEEP, "config"))
os.makedirs(os.environ["TP_DATA_DIR"], exist_ok=True)
os.makedirs(os.environ["TP_CONFIG_DIR"], exist_ok=True)

from app import db  # noqa: E402
from app.paths import long_path, fit_name, lp_isfile, lp_open  # noqa: E402
from app.server import app as fastapi_app  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def ok(cond: bool, name: str, extra: str = "") -> bool:
    if cond:
        PASS.append(name)
        print(f"  [√] {name}" + (f"　{extra}" if extra else ""))
    else:
        FAIL.append(name + (f"　{extra}" if extra else ""))
        print(f"  [×] {name}" + (f"　{extra}" if extra else ""))
    return bool(cond)


def _asgi(method: str, url: str, headers=None, body: bytes = b"") -> tuple[int, bytes, dict]:
    """直接驱动 ASGI（不依赖 httpx），返回 (状态码, 响应体, 响应头)。"""
    import asyncio
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    if body:
        raw.append((b"content-length", str(len(body)).encode()))
    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "method": method.upper(), "scheme": "http",
        "path": parts.path, "raw_path": parts.path.encode(),
        "query_string": parts.query.encode(), "root_path": "",
        "headers": raw, "client": ("127.0.0.1", 51234),
        "server": ("127.0.0.1", 80), "state": {},
    }
    state = {"sent": False}
    cap = {"status": 0, "body": bytearray(), "headers": {}}

    async def receive():
        if state["sent"]:
            return {"type": "http.disconnect"}
        state["sent"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        if message["type"] == "http.response.start":
            cap["status"] = message["status"]
            cap["headers"] = {k.decode("latin-1").lower(): v.decode("latin-1")
                              for k, v in message.get("headers", [])}
        elif message["type"] == "http.response.body":
            cap["body"] += message.get("body", b"")

    asyncio.run(fastapi_app(scope, receive, send))
    return cap["status"], bytes(cap["body"]), cap["headers"]


def _long_name(n: int = 40) -> str:
    """造一个"微信转发的附件名"（真实场景最长见过 167 字符）。

    注意上限：**单个文件名不得超过 255 字符**（NTFS 单组件限制），
    比整条路径的 260 更早触发。所以这里控制名字长度，
    让"整条路径"超限靠的是**目录深度**——真实场景正是
    "深层数据根 + 长附件名" 撞上 260。
    """
    base = ("cgi-bin_mmwebwx-bin_webwxgetmsgimg__&MsgID=4475119088943494684"
            "&skey=@crypt_1abcc194_e8a768d710ef4237edca0d4cbbd6efec"
            "&mmweb_appid=wx_webfilehelper")
    name = base[:120] + "x" * n + ".pdf"
    assert len(name) <= 255, len(name)
    return name


def group_longpath() -> None:
    print("\n═══ 长路径（带旧数据 → 在线查看简历报错）═══")
    data_dir = os.environ["TP_DATA_DIR"]
    archive = os.path.join(data_dir, "archive", "2026-10")
    os.makedirs(archive, exist_ok=True)

    # --- L1: 造一个 >260 字符的绝对路径文件 ---
    fname = _long_name(40)
    long_dir = os.path.join(archive, "长路径用例", "2026年10月", "来自微信的附件",
                            "候选人资料", "2026届")
    target = os.path.join(long_dir, fname)
    payload = b"%PDF-1.4\n% long path case\n" + b"x" * 2048
    ok(len(target) > 260, "L1 造出超过 260 字符的绝对路径",
       f"整条={len(target)} 文件名={len(fname)}")
    ok(len(fname) <= 255, "L1b 文件名本身不超 255（NTFS 单组件上限）",
       f"文件名={len(fname)}")
    # 用 \\?\ 前缀创建（普通方式在 Windows 上会直接 WinError 206）
    os.makedirs(long_path(long_dir), exist_ok=True)
    with lp_open(target, "wb") as fh:
        fh.write(payload)
    ok(lp_isfile(target), "L2 长路径文件落盘成功（\\?\\ 前缀）")

    # --- L3: 落进台账 ---
    db_path = os.path.join(data_dir, "workbench.db")
    # db.connect 会建表并跑迁移（server.py 里到处这么用）
    _c = db.connect(db_path)
    _c.close()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO documents (file_name, file_path, archived_path, mime,"
            " size, text_len, parse_ok, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (fname, target, target, "application/pdf", len(payload), 0, 1,
             "2026-10-21 00:00:00"))
        conn.commit()
        did = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    finally:
        conn.close()
    ok(bool(did), "L3 台账里写入超长绝对路径", f"documents.id={did}")

    # --- L4: 在线预览（inline=1）必须 200 且字节一致 ---
    st, body, hdr = _asgi("GET", f"/api/documents/{did}/file?inline=1")
    ok(st == 200 and body == payload,
       "L4 在线查看简历（inline）返回原文",
       f"状态={st} 字节={len(body)}/{len(payload)}")
    ok("inline" in (hdr.get("content-disposition") or ""),
       "L5 预览走 inline 处置方式",
       (hdr.get("content-disposition") or "")[:60])

    # --- L6: 下载 ---
    st2, body2, hdr2 = _asgi("GET", f"/api/documents/{did}/file")
    ok(st2 == 200 and body2 == payload, "L6 下载原件返回原文",
       f"状态={st2} 字节={len(body2)}")
    ok("attachment" in (hdr2.get("content-disposition") or ""),
       "L7 下载走 attachment 处置方式")

    # --- L8: 安全门没被打开（越界仍 403）---
    outside = os.path.join(tempfile.gettempdir(), "hra_outside_test.pdf")
    with open(outside, "wb") as fh:
        fh.write(b"secret")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO documents (file_name, file_path, archived_path, mime,"
            " size, text_len, parse_ok, created_at) VALUES (?,?,?,?,?,?,?,?)",
            ("outside.pdf", outside, outside, "application/pdf", 6, 0, 1,
             "2026-10-21 00:00:00"))
        conn.commit()
        oid = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    finally:
        conn.close()
    st3, _b3, _h3 = _asgi("GET", f"/api/documents/{oid}/file")
    ok(st3 == 403, "L8 仓库外的路径仍被拒绝（403，长路径改造没拆掉安全门）",
       f"状态={st3}")

    # --- L9: 落盘文件名预算 ---
    fitted = fit_name(os.path.join(data_dir, "inbox"), fname)
    full = os.path.join(data_dir, "inbox", fitted)
    ok(len(full) <= 240, "L9 超长附件名被压到路径预算内",
       f"落盘后长度={len(full)}")
    ok(fitted.endswith(".pdf") and len(fitted) <= len(fname),
       "L10 压短后仍保留扩展名", fitted[-40:])

    # --- L11: 迁移（带旧数据）——长路径拷得过去、路径被重写、预览仍能开 ---
    old_home = os.path.join(_TMP_ROOT, "oldhome")
    new_home = os.path.join(_TMP_ROOT, "newhome")
    for h in (old_home, new_home):
        os.makedirs(os.path.join(h, "data"), exist_ok=True)
    src_dir = os.path.join(old_home, "data", "archive", "2026-10", "长路径用例",
                           "2026年10月", "来自微信的附件", "候选人资料", "2026届")
    os.makedirs(src_dir, exist_ok=True)
    with lp_open(os.path.join(src_dir, fname), "wb") as fh:
        fh.write(payload)
    # 旧库：绝对路径指向旧数据根
    old_db = os.path.join(old_home, "data", "workbench.db")
    conn = sqlite3.connect(old_db)
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS documents (id INTEGER PRIMARY KEY,"
                     " file_name TEXT, file_path TEXT, archived_path TEXT)")
        conn.execute("INSERT INTO documents (file_name, file_path, archived_path)"
                     " VALUES (?,?,?)",
                     (fname, os.path.join(src_dir, fname),
                      os.path.join(src_dir, fname)))
        conn.commit()
    finally:
        conn.close()
    from app import profile_migrate as pm
    copied = pm._copy_tree_if_missing(
        os.path.join(old_home, "data"),
        os.path.join(new_home, "data"), [])
    ok(copied >= 1, "L11 迁移拷贝长路径成功（没踩 WinError 206）",
       f"拷贝文件数={copied}")
    new_path = os.path.join(new_home, "data", "archive", "2026-10", "长路径用例",
                            "2026年10月", "来自微信的附件", "候选人资料", "2026届", fname)
    ok(lp_isfile(new_path), "L12 迁移后长路径文件确实在目标位置",
       f"长度={len(new_path)}")
    changed = pm.normalize_existing_data_paths(
        old_db, os.path.join(new_home, "data"))
    ok(changed >= 1, "L13 旧库里的绝对路径被重写到新数据根", f"改写={changed}")
    conn = sqlite3.connect(old_db)
    try:
        row = conn.execute("SELECT archived_path FROM documents LIMIT 1").fetchone()
    finally:
        conn.close()
    ok(row and row[0].startswith(os.path.join(new_home, "data")),
       "L14 重写后的路径指向新数据根", (row[0][:110] if row else "无"))
    ok(lp_isfile(row[0]) if row else False,
       "L15 重写后的长路径仍可访问", f"长度={len(row[0]) if row else 0}")


def group_port() -> None:
    print("\n═══ 端口冲突（后台都退了也有）═══")
    import webapp_launcher as wl

    # --- P1: 正常端口可用 ---
    ok(wl._port_usable(8762)[0], "P1 空闲端口判定为可用")

    # --- P2: 被别人监听 ---
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", 8763))
    s.listen(1)
    try:
        ok(wl._port_busy(8763) is not None, "P2 有程序在听 → 探测到占用")
        ok(not wl._port_usable(8763)[0], "P3 有程序在听 → 判定不可绑定")
    finally:
        s.close()

    # --- P4: "已 bind 未 listen" —— 旧探测看不出来的那个坑 ---
    s2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s2.bind(("127.0.0.1", 8764))          # 不 listen
    try:
        busy = wl._port_busy(8764)
        okable, why = wl._port_usable(8764)
        ok(busy is not None,
           "P4 已 bind 未 listen → Windows 上 connect 仍能连上（也算占用，保守但安全）")
        ok(not okable, "P5 已 bind 未 listen → 判定不可用，自动换端口", why)
    finally:
        s2.close()

    # --- P6: 候选端口表覆盖首选段+扩展段 ---
    cands = wl._candidate_ports()
    ok(len(cands) >= 110 and cands[0] == wl.PORT and cands[-1] > wl.PORT + 100,
       "P6 候选端口表含首选段与扩展段", f"共 {len(cands)} 个")
    ok(wl._os_pick_port() is not None, "P7 系统能分配兜底端口（起不来的最后防线）")

    # --- P8: 失败原因能说人话 ---
    msg = wl._bind_reason(OSError(10048, "address already in use"))
    ok("占用" in msg, "P8 10048 → 识别为'被占用'", msg)
    msg2 = wl._bind_reason(OSError(10013, "permission denied"))
    ok("保留" in msg2, "P9 10013 → 识别为'被系统保留'", msg2)

    # --- P10: 端到端——首选段被占满时应自动改用扩展段，而不是弹"端口冲突" ---
    holders = []
    try:
        for p in range(8756, 8781):        # 占满首选段 + 前 15 个扩展段
            try:
                hs = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                hs.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                hs.bind(("127.0.0.1", p))
                hs.listen(1)
                holders.append(hs)
            except OSError:
                pass
        log = os.path.join(_TMP_ROOT, "port_e2e.log")
        env = dict(os.environ)
        env["TP_PORT"] = "8756"
        env["TP_NO_BROWSER"] = "1"
        env["TP_DATA_DIR"] = os.path.join(_TMP_ROOT, "data2")
        env["TP_CONFIG_DIR"] = os.path.join(_TMP_ROOT, "config2")
        # 不缓冲：stdout 重定向到文件时 Python 默认块缓冲，日志要等进程退出才出现
        env["PYTHONUNBUFFERED"] = "1"
        os.makedirs(env["TP_DATA_DIR"], exist_ok=True)
        os.makedirs(env["TP_CONFIG_DIR"], exist_ok=True)
        # 输出写文件再轮询——**不能用 readline()**：它是阻塞的，
        # 子进程暂时不吐字就会一直卡住（上次卡了 10 分钟）。
        with io.open(log, "w", encoding="utf-8") as lf:
            proc = subprocess.Popen(
                [sys.executable, os.path.join(BASE, "webapp_launcher.py"),
                 "--no-browser"],
                cwd=BASE, env=env, stdout=lf, stderr=subprocess.STDOUT,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            text = ""
            deadline = time.time() + 150
            while time.time() < deadline:
                time.sleep(1.0)
                try:
                    with io.open(log, "r", encoding="utf-8", errors="replace") as rf:
                        text = rf.read()
                except OSError:
                    text = ""
                if ("已监听" in text or "自动改用" in text
                        or "系统分配" in text or "启动中止" in text):
                    break
                if proc.poll() is not None:
                    break
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        try:
            with io.open(log, "r", encoding="utf-8", errors="replace") as rf:
                text = rf.read()
        except OSError:
            text = ""
        ok("自动改用" in text or "系统分配" in text,
           "P10 端口全被占时自动改用别的端口（不弹'端口冲突'）",
           [x for x in text.splitlines() if "端口" in x][:2])
        ok("端口 8756-8765 都被其他程序占用" not in text,
           "P11 不再出现原来那句误导性的'端口被占用'报错")
    finally:
        for hs in holders:
            try:
                hs.close()
            except OSError:
                pass


def main() -> int:
    print("=" * 64)
    print("v2.2.4 专项自检：长路径 + 端口")
    print("=" * 64)
    group_longpath()
    group_port()
    print("\n" + "=" * 64)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print(f"  [×] {f}")
    print("=" * 64)
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
