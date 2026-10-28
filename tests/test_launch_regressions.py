# -*- coding: utf-8 -*-
"""v3.1.12 启动/安装链路的回归测试。

这三处的共同点是**平时看不见、坏了才致命**：
  · `bootstrap.json` 里的数据目录失效 → 安装版双击只留一个后台进程；
  · 端口被旧实例占满 → 启不动、没托盘、没浏览器；
  · `refresh_skills` 每次启动全量重跑 → 上万次 commit 抢写锁。

运行：python tests/test_launch_regressions.py
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, ".runtime_local"))

# 用工作区内的临时目录（不动真实 data/ 与 %APPDATA%）
_TMP = os.path.join(HERE, "_launch_tmp")
os.makedirs(_TMP, exist_ok=True)
os.environ["APPDATA"] = os.path.join(_TMP, "appdata")
os.makedirs(os.path.join(os.environ["APPDATA"], "HRAgent"), exist_ok=True)
os.environ.setdefault("TP_DATA_DIR", os.path.join(_TMP, "data"))

FAILS: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'√' if cond else '×'}] {label}" + (f"　{detail}" if detail else ""))
    if not cond:
        FAILS.append(label)


# ============================================================ T1 数据目录
def t1_user_home():
    print("\nT1 bootstrap.json 记的数据目录失效时不能把启动带死")
    import webapp_launcher as W

    check("可用目录被判为可用", W._home_usable(_TMP))
    check("不存在的盘符被判为不可用", not W._home_usable(r"Z:\dead\path\data"))
    check("系统保护目录被判为不可用",
          not W._home_usable(r"C:\Windows\System32\drivers\etc\hra_probe"))

    # 记录一个死路径 → 必须退回默认目录，而不是抛异常
    json.dump({"user_home": r"Z:\dead\path"},
              open(W._bootstrap_path(), "w", encoding="utf-8"), ensure_ascii=False)
    resolved = W._resolve_user_home(None, None)
    check("失效记录 → 退回默认目录", resolved == W._default_user_home(), resolved)
    saved = json.load(open(W._bootstrap_path(), encoding="utf-8"))
    check("旧值被保留在 stale_user_home 里（不静默丢弃）",
          saved.get("stale_user_home") == r"Z:\dead\path")

    # 显式传入的值不受这条校验影响（用户自己指定就照用）
    explicit = W._resolve_user_home(str(os.path.join(_TMP, "explicit")), None)
    check("显式 --data-dir 优先于记录值",
          explicit == os.path.abspath(os.path.join(_TMP, "explicit")))


# ============================================================ T2 实例发现
def t2_instance_scan():
    print("\nT2 已有实例必须能**在所有候选端口**上被找到")
    import webapp_launcher as W

    ports = W._candidate_ports()
    check("候选端口 ≥ 100 个", len(ports) >= 100, f"{len(ports)} 个")

    # 造一个"漂到扩展段"的实例：只有第 60 个候选端口上跑着我方实例。
    # 注意实现是**两阶段**：先 TCP 扫"谁在听"，再 HTTP 认身份。
    # 所以两个都要打桩，只打 `_is_our_instance` 会走不到（扫描阶段就返回空）。
    target_port = ports[60]
    check("用于测试的端口确实在首选段之外",
          target_port >= W.PORT + W.PORT_TRIES, f"port={target_port}")

    real_probe = W._is_our_instance
    real_scan = W._listening_ports
    W._listening_ports = lambda ps, **kw: [target_port]
    W._is_our_instance = lambda p: "build-xyz" if p == target_port else None
    try:
        hit = W._find_existing_instance()
    finally:
        W._is_our_instance = real_probe
        W._listening_ports = real_scan

    check("漂到扩展段的实例仍能被找到", hit is not None,
          f"{hit[0] if hit else None}")
    check("返回的地址端口正确", bool(hit) and hit[2] == target_port)

    # 没有实例时必须返回 None（不能把"端口空闲"误判成"有实例"）
    # 注意：**不能直接依赖运行环境的真实端口**——开发机上常残留"进程已退、
    # 套接字还在 LISTENING"的幽灵监听（实测 8757 就有一个），
    # 那会让这条断言随机失败。这里把"没人听"造出来。
    W._listening_ports = lambda ps, **kw: []
    try:
        none_hit = W._find_existing_instance()
    finally:
        W._listening_ports = real_scan
    check("没有任何端口在听时返回 None", none_hit is None)


# ============================================================ T3 库指纹
def t3_repair_signature():
    print("\nT3 库指纹（决定 refresh_skills 跑不跑）")
    from app import db, server

    p = os.path.join(_TMP, "sig.db")
    for s in ("", "-wal", "-shm"):
        if os.path.exists(p + s):
            try:
                os.remove(p + s)
            except OSError:
                pass
    conn = db.connect(p)                     # 空库即可，只验指纹的敏感性

    s1 = server._repair_signature(conn)
    check("空库也能算指纹", s1 is not None)
    check("同状态幂等", s1 == server._repair_signature(conn))

    conn.execute("INSERT INTO candidates (name, identity_key) VALUES ('甲', 'k1')")
    conn.execute(
        "INSERT INTO applications (candidate_id, applied_at, stage, created_at, updated_at) "
        "VALUES (1, '2026-10-28 09:00:00', '新投递', '2026-10-28', '2026-10-28')")
    conn.commit()
    s2 = server._repair_signature(conn)
    check("新增投递 → 指纹变化", s2 != s1)

    conn.execute("INSERT INTO documents (candidate_id, file_name, file_path, raw_text) "
                 "VALUES (1, 'a.pdf', 'data/archive/a.pdf', '原文')")
    conn.commit()
    s3 = server._repair_signature(conn)
    check("新增附件 → 指纹变化", s3 != s2)

    conn.execute("UPDATE candidates SET name = '乙' WHERE id = 1")
    conn.commit()
    check("只改姓名 → 指纹不变", server._repair_signature(conn) == s3)

    db.set_setting(conn, server.SKILLS_REFRESH_KEY, s3)
    check("设置项存读闭环",
          db.get_setting(conn, server.SKILLS_REFRESH_KEY, None) == s3)
    conn.close()


# ============================================================ T4 安装器兜底
def t4_installer_migrate():
    print("\nT4 安装器迁移：进程内写被拒时必须降级 robocopy")
    inst = os.path.join(os.path.dirname(REPO), "installer")
    if not os.path.isfile(os.path.join(inst, "setup_wizard.py")):
        print("  [–] 找不到 installer/setup_wizard.py，跳过")
        return
    sys.path.insert(0, inst)
    import setup_wizard as W
    import profile_migrate

    base = os.path.join(_TMP, "wiz")
    if os.path.isdir(base):
        shutil.rmtree(base, ignore_errors=True)
    legacy = os.path.join(base, "legacy")
    os.makedirs(os.path.join(legacy, "data", "archive"), exist_ok=True)
    os.makedirs(os.path.join(legacy, "config"), exist_ok=True)
    c = sqlite3.connect(os.path.join(legacy, "data", "workbench.db"))
    c.execute("CREATE TABLE candidates (id INTEGER PRIMARY KEY, name TEXT)")
    c.execute("INSERT INTO candidates (name) VALUES ('张三')")
    c.commit()
    c.close()
    with open(os.path.join(legacy, "data", "archive", "r1.txt"), "w",
              encoding="utf-8") as fh:
        fh.write("x")
    with open(os.path.join(legacy, "config", "tiers.json"), "w",
              encoding="utf-8") as fh:
        fh.write("{}")

    # 正常路径
    t1 = os.path.join(base, "t1")
    r1 = W._migrate_robust(legacy, t1)
    check("正常迁移：目标目录自动创建", os.path.isdir(t1))
    check("正常迁移：workbench.db 到位",
          os.path.isfile(os.path.join(t1, "data", "workbench.db")))
    check("正常迁移：走进程内通道", r1.get("via", "in-process") == "in-process")

    # 模拟 WinError 5 → 降级 robocopy
    t2 = os.path.join(base, "nested", "deep", "t2")
    real = profile_migrate.migrate_profile

    def deny(*a, **kw):
        raise PermissionError(5, "拒绝访问。", t2)

    profile_migrate.migrate_profile = deny
    try:
        r2 = W._migrate_robust(legacy, t2)
        err = None
    except Exception as exc:                              # noqa: BLE001
        r2, err = None, exc
    finally:
        profile_migrate.migrate_profile = real

    check("进程内被拒后不抛错", err is None, str(err)[:60] if err else "")
    check("降级到 robocopy 通道", bool(r2) and r2.get("via") == "robocopy")
    check("robocopy 也能把库搬过去",
          os.path.isfile(os.path.join(t2, "data", "workbench.db")))


def t5_open_url_once():
    print("\nT5 `_open_url` 一次启动只能交付一次（否则弹多个浏览器窗口）")
    import subprocess as sp
    import webapp_launcher as W

    calls = {"startfile": 0, "popen": 0, "wb": 0}
    real_startfile = getattr(os, "startfile", None)
    real_popen = sp.Popen
    real_wb = W.webbrowser.open

    def fake_startfile(_u):
        calls["startfile"] += 1

    def fake_popen(*_a, **_kw):
        calls["popen"] += 1

        class P:
            returncode = 0

            def poll(self):
                return 0
        return P()

    def fake_wb(*_a, **_kw):
        calls["wb"] += 1
        return True

    W.webbrowser.open = fake_wb
    try:
        os.startfile = fake_startfile          # type: ignore[attr-defined]
        sp.Popen = fake_popen                  # type: ignore[assignment]
        W._BROWSER_OPENED = False
        ok1 = W._open_url("http://127.0.0.1:8999/")
        ok2 = W._open_url("http://127.0.0.1:8999/")
    finally:
        if real_startfile is not None:
            os.startfile = real_startfile      # type: ignore[attr-defined]
        sp.Popen = real_popen                  # type: ignore[assignment]
        W.webbrowser.open = real_wb

    check("首次交付成功", ok1 is True)
    check("第二次被闸门挡下", ok2 is True)
    check("只交付一次", calls["startfile"] == 1, f"startfile={calls['startfile']}")
    check("成功后不再启动浏览器子进程", calls["popen"] == 0)
    check("成功后不再走 webbrowser", calls["wb"] == 0)

    # startfile 失败 → 才降级，且只试一个浏览器
    calls.update({"startfile": 0, "popen": 0})
    W._BROWSER_OPENED = False
    try:
        def boom(_u):
            calls["startfile"] += 1
            raise OSError("no association")

        os.startfile = boom                    # type: ignore[attr-defined]
        sp.Popen = fake_popen                  # type: ignore[assignment]
        W._open_url("http://127.0.0.1:8999/")
    finally:
        if real_startfile is not None:
            os.startfile = real_startfile      # type: ignore[attr-defined]
        sp.Popen = real_popen                  # type: ignore[assignment]
    check("失败才降级，且只启动一个浏览器", calls["popen"] <= 1, f"popen={calls['popen']}")


def t6_bootstrap_nonfatal():
    print("\nT6 `_save_bootstrap` 写失败不能中断启动")
    import app.profile_migrate as pm
    import webapp_launcher as W

    real = pm.atomic_write_json

    def boom(*_a, **_kw):
        raise PermissionError(13, "Permission denied", "bootstrap.json.tmp")

    pm.atomic_write_json = boom
    try:
        d = W._save_bootstrap({"user_home": "C:/x", "last_port": 8756})
        err = None
    except Exception as exc:                              # noqa: BLE001
        d, err = None, exc
    finally:
        pm.atomic_write_json = real
    check("不抛异常", err is None, str(err)[:60] if err else "")
    check("仍返回合并后的字典",
          bool(d) and d.get("user_home") == "C:/x" and d.get("last_port") == 8756)


def main() -> int:
    print("=" * 64)
    print("v3.1.15 启动/安装链路回归测试")
    print("=" * 64)
    t1_user_home()
    t2_instance_scan()
    t3_repair_signature()
    t4_installer_migrate()
    t5_open_url_once()
    t6_bootstrap_nonfatal()
    print("\n" + "=" * 64)
    if FAILS:
        print(f"失败 {len(FAILS)} 项：")
        for f in FAILS:
            print(f"  · {f}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
