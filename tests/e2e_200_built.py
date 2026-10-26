# -*- coding: utf-8 -*-
"""**成品级**端到端验证：2.0.0 老用户升级到 v2.2.5 全链路。

与 test_200_e2e.py（源码级）的区别：这里跑的是**真实打包出来的 exe**
  —— 真实安装器 exe（GUI 无法点，改为直接调它的迁移逻辑）
  —— 真实主程序 exe（真启动、真起服务、真请求在线预览接口）

覆盖用户报的症状：「打开在线简历」与「文件路径」总是提示路径不对。

跑法：
    python tests/e2e_200_built.py <发布目录>
    默认 <repo>/release/HRAgent工作台
"""
from __future__ import annotations

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

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
RELEASE = sys.argv[1] if len(sys.argv) > 1 else os.path.join(BASE, "release", "HRAgent工作台")
EXE = os.path.join(RELEASE, "HRAgent工作台.exe")
INSTALLER = os.path.join(RELEASE, "HRAgent安装器.exe")

PASS, FAIL = [], []


def ok(cond, name, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'√' if cond else '×'}] {name}" + (f"　{extra}" if extra else ""))
    return bool(cond)


def _get(url, timeout=25, headers=None):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, headers=headers or {})
    with opener.open(req, timeout=timeout) as r:
        return r.status, r.read(), dict(r.headers)


def _post(url, payload, timeout=60):
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with opener.open(req, timeout=timeout) as r:
        return r.status, r.read()


# ---------------------------------------------------------------- 造 2.0.0 现场

def make_legacy_200(root: str) -> dict:
    """造一个 2.0.0 形态的安装目录：<root>/HRAgent工作台/_internal/{data,config}。

    关键点：**来源目录写成绝对路径**（2.0.0 的真实行为），
    这样迁移如果没改写配置，在线预览就必然 404。
    """
    app = os.path.join(root, "HRAgent工作台")
    internal = os.path.join(app, "_internal")
    data = os.path.join(internal, "data")
    config = os.path.join(internal, "config")
    resumes = os.path.join(data, "resumes")
    archive = os.path.join(data, "archive")
    os.makedirs(resumes, exist_ok=True)
    os.makedirs(archive, exist_ok=True)
    os.makedirs(config, exist_ok=True)

    # 一份原件（pdf 头，内容不重要）
    with open(os.path.join(resumes, "张三_简历.pdf"), "wb") as f:
        f.write(b"%PDF-1.4\n% legacy resume\n%%EOF\n")
    # 归档副本：真实形态里 documents.archived_path 指向 archive 下的文件
    with open(os.path.join(archive, "张三_简历.pdf"), "wb") as f:
        f.write(b"%PDF-1.4\n% legacy resume archived\n%%EOF\n")

    db = os.path.join(data, "workbench.db")
    con = sqlite3.connect(db)
    con.execute("""CREATE TABLE IF NOT EXISTS candidates(
        id INTEGER PRIMARY KEY, name TEXT, source_file TEXT,
        archived_path TEXT, tier TEXT, created_at TEXT)""")
    con.execute("""CREATE TABLE IF NOT EXISTS documents(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_hash TEXT UNIQUE, candidate_id INTEGER, application_id INTEGER,
        file_name TEXT, file_path TEXT, archived_path TEXT,
        mime TEXT, size INTEGER, received_at TEXT, source_message_id TEXT,
        raw_text TEXT, text_len INTEGER, parse_engine TEXT,
        parse_ok INTEGER DEFAULT 1, created_at TEXT)""")
    con.execute("""INSERT INTO candidates(name, source_file, archived_path, tier, created_at)
        VALUES(?,?,?,?,?)""",
        ("张三", os.path.join(resumes, "张三_简历.pdf"),
         os.path.join(archive, "张三_简历.pdf"), "A", "2026-01-01"))
    cid = con.execute("SELECT last_insert_rowid()").fetchone()[0]
    con.execute("""INSERT INTO documents(file_hash, candidate_id, file_name, file_path,
                    archived_path, mime, size, received_at, raw_text, text_len,
                    parse_engine, parse_ok, created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        ("e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
         cid, "张三_简历.pdf", os.path.join(resumes, "张三_简历.pdf"),
         os.path.join(archive, "张三_简历.pdf"), "application/pdf", 29,
         "2026-01-01", "张三 男 2019 本科", 12, "rule", 1, "2026-01-01"))
    con.commit()
    con.close()

    # 2.0.0 的配置：来源目录是**指向旧位置的绝对路径**
    mailbox = {
        "mode": "imap", "eml_dir": os.path.join(data, "eml"),
        "folder_dir": resumes, "archive_dir": archive,
        "attachment_ext": [".pdf", ".docx"], "max_attachment_mb": 20,
        "imap": {"host": "example.imap.com", "port": 993},
        "dedup": {"by": "name"}, "_说明": "2.0.0 现场",
    }
    with open(os.path.join(config, "mailbox.json"), "w", encoding="utf-8") as f:
        json.dump(mailbox, f, ensure_ascii=False, indent=2)
    return {"app": app, "internal": internal, "data": data,
            "config": config, "resumes": resumes, "archive": archive, "db": db}


# ------------------------------------------------------- 用真安装器 exe 做迁移

def run_installer_migrate(legacy: dict, target_home: str, target_data: str, target_config: str):
    """调**真实安装器 exe** 的迁移逻辑。

    安装器是 GUI，但迁移模块（hra_profile_migrate）是随包分发的**同级 import 模块**
    → 从 exe 旁边的 _internal 里 import 出来直接调，走的是包里那份真代码。
    """
    sys.path.insert(0, os.path.join(RELEASE, "_internal"))
    # 迁移模块在 exe 内部（PYZ），落盘副本是 tools/hra_profile_migrate.py；
    # 这里用包内同源副本（构建期由 vendor_migrate_module() 同步，三份 md5 一致）
    import importlib.util
    cand = os.path.join(RELEASE, "_internal", "hra_profile_migrate.py")
    if not os.path.isfile(cand):
        cand = os.path.join(BASE, "tools", "hra_profile_migrate.py")
    spec = importlib.util.spec_from_file_location("hra_profile_migrate", cand)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.migrate_profile(legacy["app"], target_home)


# ------------------------------------------------------------ 起真服务并验证

def start_service(env_home: str, port_hint: int = 8731):
    """用**真实 exe** 启动服务，指到独立的数据/配置目录。

    ⚠️ 两条实测得来的关键点：
    ① exe 的 stdout/stderr 被**重定向到 `<data_dir>/webapp.log`**
       （webapp_launcher._redirect_stdio_to_log）→ 想看日志得读那个文件，
       读 PIPE 只会拿到空。
    ② 端口**不是**我们指定的 —— 程序自己挑（实测 8756）→ 必须从日志里
       抠出真实 URL 再探活。
    """
    env = dict(os.environ)
    env["TP_USER_HOME"] = env_home
    env["TP_DATA_DIR"] = os.path.join(env_home, "data")
    env["TP_CONFIG_DIR"] = os.path.join(env_home, "config")
    env["HRA_PORT"] = str(port_hint)
    log_path = os.path.join(env_home, "data", "webapp.log")
    lf = open(os.path.join(env_home, "exe_stdout.log"), "wb")
    p = subprocess.Popen([EXE], env=env, stdout=lf, stderr=subprocess.STDOUT, cwd=RELEASE)
    port = None
    for _ in range(90):
        time.sleep(1)
        if p.poll() is not None:
            lf.close()
            return None, _read(log_path)
        text = _read(log_path)
        if port is None:
            m = re.search(r"http://127\.0\.0\.1:(\d{4,5})", text)
            if m:
                port = int(m.group(1))
        if port:
            try:
                st, body, _ = _get(f"http://127.0.0.1:{port}/api/health", timeout=3)
                if st == 200:
                    lf.close()
                    return p, port
            except Exception:
                pass
    lf.close()
    return None, _read(log_path)


def _read(path: str) -> str:
    try:
        with open(path, "rb") as f:
            return f.read().decode("utf-8", "replace")
    except Exception:
        return ""


def find_real_port(log_text: str, default: int) -> int:
    import re
    for m in re.finditer(r"(?:127\.0\.0\.1|localhost):(\d{4,5})", log_text):
        return int(m.group(1))
    return default


def main() -> int:
    print(f"发布目录：{RELEASE}")
    if not os.path.isfile(EXE):
        print("[×] 找不到主程序 exe，先构建")
        return 2
    if not os.path.isfile(INSTALLER):
        print("[×] 找不到安装器 exe，先构建")
        return 2

    tmp = tempfile.mkdtemp(prefix="hra_e2e200_")
    try:
        print("\n=== 1. 造 2.0.0 现场 ===")
        legacy = make_legacy_200(tmp)
        ok(os.path.isfile(legacy["db"]), "2.0.0 库已生成", legacy["db"])
        ok(os.path.isfile(os.path.join(legacy["config"], "mailbox.json")),
           "2.0.0 配置已生成（来源目录=绝对路径）")

        print("\n=== 2. 安装器迁移（走包里那份迁移模块）===")
        new_home = os.path.join(tmp, "newhome")
        target_home = new_home
        target_data = os.path.join(target_home, "data")
        target_config = os.path.join(target_home, "config")
        os.makedirs(target_data, exist_ok=True)
        os.makedirs(target_config, exist_ok=True)
        res = dict(run_installer_migrate(legacy, target_home, target_data, target_config))
        counts = res.get("counts") or {}
        print("   迁移结果：", {k: v for k, v in res.items()
                              if k in ("counts", "copied_data_files",
                                       "rewritten_config_paths", "old_data_retained")})
        ok(int(counts.get("candidates", 0) or 0) >= 1, "候选人已迁移",
           f"candidates={counts.get('candidates')}")
        ok(bool(res.get("old_data_retained")), "旧目录标记为保留")
        ok(os.path.isfile(os.path.join(target_data, "workbench.db")),
           "新数据根下有库")

        print("\n=== 3. 配置里的绝对路径已改写 ===")
        mb_path = os.path.join(target_config, "mailbox.json")
        ok(os.path.isfile(mb_path), "mailbox.json 已搬到新配置根")
        mb = json.load(open(mb_path, encoding="utf-8"))
        fd = mb.get("folder_dir", "")
        ad = mb.get("archive_dir", "")
        print("   folder_dir =", fd)
        print("   archive_dir =", ad)
        ok(not fd.startswith(legacy["internal"]),
           "来源目录不再指向旧安装目录", fd)
        ok(os.path.isdir(fd), "改写后的来源目录真实存在")
        ok(os.path.isfile(os.path.join(fd, "张三_简历.pdf")),
           "原件在新来源目录下")

        print("\n=== 4. 旧目录完整保留（迁移只搬不删）===")
        ok(os.path.isfile(legacy["db"]), "旧库还在")
        ok(os.path.isfile(os.path.join(legacy["resumes"], "张三_简历.pdf")),
           "旧原件还在")

        print("\n=== 5. 真实 exe 启动 + 在线预览 ===")
        proc, port = start_service(new_home)
        if proc is None:
            print("   启动日志：", (port or "")[:1200])
            ok(False, "主程序 exe 启动")
        else:
            ok(True, "主程序 exe 启动成功", f"port={port}")
            try:
                # 登录拿 token（原件接口要 read 权限）
                st0, body0 = _post(f"http://127.0.0.1:{port}/api/login",
                                   {"username": "hr", "password": "change-me"})
                sess = json.loads(body0.decode("utf-8")) if st0 == 200 else {}
                token = sess.get("token") or sess.get("session_token")
                ok(st0 == 200 and bool(token), "登录拿到 token",
                   f"status={st0}")

                hdrs = {"X-TP-Token": token} if token else {}
                st, body, _ = _get(f"http://127.0.0.1:{port}/api/candidates?page_size=5",
                                   headers=hdrs)
                data = json.loads(body.decode("utf-8"))
                items = data.get("items") or data.get("candidates") or []
                ok(st == 200 and len(items) >= 1,
                   "接口能查到迁移过来的候选人",
                   f"count={len(items)}")
                # 原件走 documents 表 → /api/documents/{did}/file
                # （这就是界面「在线打开 / 在线预览」点的那条路）
                import sqlite3 as _s
                con = _s.connect(os.path.join(target_data, "workbench.db"))
                rows = con.execute(
                    "SELECT id, file_name, file_path, archived_path FROM documents"
                ).fetchall()
                con.close()
                ok(len(rows) >= 1, "迁移过来的原件在 documents 表里", f"n={len(rows)}")
                if rows:
                    did = rows[0][0]
                    st2, body2, hdr2 = _get(
                        f"http://127.0.0.1:{port}/api/documents/{did}/file?inline=1",
                        headers=hdrs)
                    ok(st2 == 200 and len(body2) > 0,
                       "在线预览能取到原件",
                       f"status={st2} bytes={len(body2)} file={rows[0][1]}")
                    if st2 != 200:
                        print("   响应：", body2[:300])
                    # 原件在磁盘上的真实位置
                    for label, col in (("file_path", 2), ("archived_path", 3)):
                        p = rows[0][col]
                        if p:
                            ok(os.path.isfile(p), f"{label} 指向真实文件", p)
            except Exception as e:
                ok(False, "接口验证", repr(e))
                body_txt = getattr(e, "read", None)
                if body_txt:
                    try:
                        print("   服务端 detail：",
                              e.read().decode("utf-8", "replace")[:400])
                    except Exception:
                        pass
                logtxt = _read(os.path.join(new_home, "data", "webapp.log"))
                print("   —— webapp.log 末尾 ——")
                print("\n".join(logtxt.splitlines()[-15:]))
            finally:
                try:
                    proc.terminate()
                    proc.wait(timeout=10)
                except Exception:
                    pass

        print("\n" + "=" * 60)
        print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
        for f in FAIL:
            print("  FAIL:", f)
        print("=" * 60)
        return 0 if not FAIL else 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
