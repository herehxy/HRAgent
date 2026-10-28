# -*- coding: utf-8 -*-
"""v3.0.2 性能相关改动的回归测试。

放这里（而不是临时脚本目录）是因为这三处改动都**没有可见的失败方式**：

- 启动钩子线程化：改坏了表现为"偶发启动慢/多实例"，平时看不出来；
- 别名索引缓存：改坏了表现为"某个别名词搜不到"，只有带别名调用才暴露；
- 写锁 → 503：改坏了回退成 500，只是错误信息变难懂。

运行：python tests/test_perf_regressions.py
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, ".runtime_local"))

# 用临时数据根，别碰真实 data/
_TMP_DATA = os.path.join(HERE, "_perf_tmp_data")
os.makedirs(_TMP_DATA, exist_ok=True)
os.environ.setdefault("TP_DATA_DIR", _TMP_DATA)

FAILS: list[str] = []


def check(label: str, cond: bool, detail: str = "") -> None:
    print(f"  [{'√' if cond else '×'}] {label}" + (f"　{detail}" if detail else ""))
    if not cond:
        FAILS.append(label)


# ============================================================ T1 正则预编译
def t1_term_pattern():
    print("\nT1 normalize._term_pattern 缓存与语义")
    from app.pipeline import normalize as nz

    nz._term_pattern.cache_clear()
    p1 = nz._term_pattern("钛合金焊接")
    p2 = nz._term_pattern("钛合金焊接")
    check("同一词条返回同一编译对象（缓存生效）", p1 is p2)

    def old_find_all(text, term):
        if nz._ASCII_SHORT.match(term):
            pattern = r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])"
            return list(re.finditer(pattern, text, re.IGNORECASE))
        return list(re.finditer(re.escape(term), text, re.IGNORECASE))

    cases = [
        ("熟悉钛合金焊接工艺", "钛合金焊接"),
        ("掌握 XRD 物相分析", "XRD"),
        ("用 UT 做超声检测", "UT"),
        ("python 与 Python3 与 PYTHON", "python"),
        ("没有命中的文本", "Java"),
        ("C++ 与 C#", "C++"),
        ("a.b.c 点号转义", "a.b"),
    ]
    same = True
    for text, term in cases:
        a = [(m.start(), m.group()) for m in old_find_all(text, term)]
        b = [(m.start(), m.group()) for m in nz._find_all(text, term)]
        if a != b:
            same = False
            print(f"      差异 term={term!r}\n        旧={a}\n        新={b}")
    check("7 组样例匹配结果与原实现完全一致", same)
    check("缓存上限 ≥ 本体词条数 858", nz._term_pattern.cache_info().maxsize >= 858)


# ============================================================ T2 别名索引
def t2_alias_index():
    print("\nT2 db.find_skill 别名索引（含 UPDATE 失效）")
    from app import db

    p = os.path.join(_TMP_DATA, "_t2.db")
    for s in ("", "-wal", "-shm"):
        if os.path.exists(p + s):
            try:
                os.remove(p + s)
            except OSError:
                pass
    conn = db.connect(p)                 # 全新库：触发建表 + 触发器 + 计数器
    db._SKILL_ALIAS_CACHE.clear()

    db.upsert_skill(conn, "测试技能甲", "其他", aliases=["别名一"])
    check("① 建技能 + 别名一 → 查到", db.find_skill(conn, "别名一") is not None)

    db.upsert_skill(conn, "测试技能甲", "其他", aliases=["别名二"])
    check("② **追加**别名二 → 也能查到（回归点）",
          db.find_skill(conn, "别名二") is not None,
          "未命中 = 版本戳漏了 UPDATE")

    v1 = conn.execute("SELECT v FROM meta_counters WHERE k='skills_alias_rev'").fetchone()[0]
    db.upsert_skill(conn, "测试技能甲", "其他", aliases=["别名三"])
    v2 = conn.execute("SELECT v FROM meta_counters WHERE k='skills_alias_rev'").fetchone()[0]
    check("③ 追加别名让计数器 +1", v2 == v1 + 1, f"{v1} → {v2}")

    db._SKILL_ALIAS_CACHE.clear()
    check("④ 跨连接可见", db.find_skill(db.connect(p), "别名三") is not None)

    db.find_skill(conn, "别名一")        # 建缓存
    db.upsert_skill(conn, "测试技能乙", "其他", aliases=["别名四"])
    check("⑤ 新建技能（COUNT/MAX(id) 变）正常失效",
          db.find_skill(conn, "别名四") is not None)

    check("⑥ 未命中返回 None", db.find_skill(conn, "绝对不存在的技能ZZZ") is None)

    v3 = conn.execute("SELECT v FROM meta_counters WHERE k='skills_alias_rev'").fetchone()[0]
    db.upsert_skill(conn, "测试技能甲", "其他", aliases=["别名一"])   # 已存在，不该 UPDATE
    v4 = conn.execute("SELECT v FROM meta_counters WHERE k='skills_alias_rev'").fetchone()[0]
    check("⑦ 重复追加同一别名不增计数（幂等）", v3 == v4, f"{v3} → {v4}")
    conn.close()

    # 老库升级路径：skills 表没有 rev 列、没有计数器、没有触发器
    p2 = os.path.join(_TMP_DATA, "_t2old.db")
    for s in ("", "-wal", "-shm"):
        if os.path.exists(p2 + s):
            try:
                os.remove(p2 + s)
            except OSError:
                pass
    raw = sqlite3.connect(p2)
    raw.execute("CREATE TABLE skills (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "canonical_name TEXT UNIQUE, aliases TEXT, category TEXT, parent_id INTEGER)")
    raw.execute("INSERT INTO skills (canonical_name, aliases, category) "
                "VALUES ('老技能', '[\"老别名\"]', '其他')")
    raw.commit()
    raw.close()
    db._INITIALIZED.discard(os.path.abspath(p2))
    db._MIGRATION_BY_PATH.pop(os.path.abspath(p2), None)
    try:
        c2 = db.connect(p2)
        db._SKILL_ALIAS_CACHE.clear()
        has_cnt = c2.execute("SELECT v FROM meta_counters WHERE k='skills_alias_rev'").fetchone()
        has_trg = c2.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' "
                             "AND name='trg_skills_alias_rev'").fetchone()
        db.upsert_skill(c2, "老技能", "其他", aliases=["新别名"])
        got = db.find_skill(c2, "新别名")
        c2.close()
        ok = has_cnt is not None and has_trg is not None and got is not None
    except Exception as exc:                                  # noqa: BLE001
        ok = False
        print(f"      异常：{type(exc).__name__}: {exc}")
    check("⑧ 老库自动补齐计数器/触发器/rev 列后可用", ok)

    for q in (p, p2):
        for s in ("", "-wal", "-shm"):
            try:
                os.remove(q + s)
            except OSError:
                pass


# ============================================================ T3 503 处理器
def t3_busy_handler():
    print("\nT3 sqlite3.OperationalError → 503")
    from app import server as srv

    h = srv.app.exception_handlers.get(sqlite3.OperationalError)
    check("已注册处理器", h is not None, f"共 {len(srv.app.exception_handlers)} 个")
    if h is None:
        return

    r_busy = asyncio.run(h(None, sqlite3.OperationalError("database is locked")))
    check("database is locked → 503", r_busy.status_code == 503,
          f"status={r_busy.status_code}")
    body = json.loads(r_busy.body)
    check("含 retryable=True", body.get("retryable") is True)
    check("带 Retry-After 头", r_busy.headers.get("Retry-After") == "3")

    r_other = asyncio.run(h(None, sqlite3.OperationalError("no such column: foo")))
    check("其它数据库错误 → 500（不误报成 503）", r_other.status_code == 500,
          f"status={r_other.status_code}")


# ============================================================ T4 启动器
def t4_launcher():
    print("\nT4 启动器等待阈值")
    import webapp_launcher as W

    check("STARTUP_WAIT 已定义", hasattr(W, "STARTUP_WAIT"))
    check("STARTUP_WAIT ≥ 60 秒（原为 12）", W.STARTUP_WAIT >= 60,
          f"={W.STARTUP_WAIT}s")
    src = open(os.path.join(REPO, "webapp_launcher.py"), encoding="utf-8").read()
    check("不再有写死的 `deadline = time.time() + 12`",
          "deadline = time.time() + 12" not in src)
    check("两处循环都用 STARTUP_WAIT",
          src.count("deadline = time.time() + STARTUP_WAIT") >= 2)


# ============================================================ T5 启动钩子
def t5_startup_hook():
    print("\nT5 启动钩子不再同步跑 refresh_skills")
    src = open(os.path.join(REPO, "app", "server.py"), encoding="utf-8").read()
    i = src.index("def _startup_purge_task")
    j = src.index("\n@", i) if "\n@" in src[i:] else len(src)
    body = src[i:j]
    check("钩子内启动 startup-repair 后台线程", 'name="startup-repair"' in body)
    bad = re.search(r"\n    try:\s*\n        conn = db\.connect\(DB_PATH\)\s*\n"
                    r"        try:\s*\n            rsk = regrade\.refresh_skills", body)
    check("钩子主体里没有同步 refresh_skills", bad is None)


def main() -> int:
    print("=" * 64)
    print("v3.0.2 性能改动回归测试")
    print("=" * 64)
    t1_term_pattern()
    t2_alias_index()
    t3_busy_handler()
    t4_launcher()
    t5_startup_hook()
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
