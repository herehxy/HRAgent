# -*- coding: utf-8 -*-
"""项目经历 AI 提炼回归测试（v4.0.18）。

覆盖：
  · 模型输出校验：证据/项目名必须能在原文找到，编造条目丢弃，摘要截断
  · 缓存写入与复用（同一份简历只调一次模型）
  · 无简历原文时缓存空结果，界面不显示该区块
"""
from __future__ import annotations

import json
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

_TMP = tempfile.mkdtemp(prefix="projects_test_")
_DB = os.path.join(_TMP, "test.db")
os.environ["TP_DB_PATH"] = _DB
os.environ["TP_AUTH"] = "0"
os.environ["TP_AUTO_INSIGHT"] = "0"
os.environ["TP_DAILY_TASK"] = "0"
os.environ["TP_BRIEF_LLM"] = "0"

from app import db, server  # noqa: E402
from app.pipeline import projects as projects_mod  # noqa: E402
from tests.selftest import _asgi  # noqa: E402

PASS = FAIL = 0


def ok(cond, label, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {label}" + (f"  ({detail})" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f"  ({detail})" if detail else ""))
    return bool(cond)


def jcall(method, url, headers=None):
    st, text = _asgi(server.app, method, url,
                     headers or {"Content-Type": "application/json"}, b"")
    try:
        return st, json.loads(text)
    except Exception:                                            # noqa: BLE001
        return st, text


def main() -> int:
    print("=" * 70)
    print("项目经历 AI 提炼回归测试")
    print("=" * 70)

    conn = db.connect(_DB)
    tabs = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    ok("candidates" in tabs, "测试库已建表")
    cols = {r[1] for r in conn.execute("PRAGMA table_info(candidates)")}
    ok("projects_json" in cols, "candidates 有 projects_json")

    text = ("项目经历：\n"
            "1. 基于钛合金的组织调控项目，负责热处理工艺设计，"
            "使抗拉强度提升 12%。\n"
            "2. 高温合金粉末制备项目，参与雾化参数优化。\n"
            "荣誉：国家奖学金。")

    # ---- 1 模型输出校验 ----
    print("\n1 模型输出校验")
    dirty = {
        "direction_summary": "钛合金与高温合金材料研发",
        "projects": [
            {"name": "基于钛合金的组织调控项目",
             "summary": "负责热处理工艺设计，抗拉强度提升 12%。",
             "evidence": "基于钛合金的组织调控项目，负责热处理工艺设计"},
            # 证据和项目名都不在原文，属于编造 → 丢弃
            {"name": "火星基地材料项目", "summary": "负责火星材料。",
             "evidence": "火星基地材料项目"},
            {"name": "高温合金粉末制备项目",
             "summary": "参与雾化参数优化，改善粉末球形度。",
             "evidence": "高温合金粉末制备项目，参与雾化参数优化"},
            # 重复项目 → 去重
            {"name": "高温合金粉末制备项目", "summary": "重复", "evidence": ""},
            # 缺摘要 → 丢弃
            {"name": "基于钛合金的组织调控项目", "summary": ""},
        ],
    }
    cleaned = projects_mod.clean_projects(dirty, text)
    ok(cleaned is not None and len(cleaned["projects"]) == 2,
       "只保留有原文依据且不重复的项目", str(cleaned))
    ok(cleaned["projects"][0]["name"] == "基于钛合金的组织调控项目"
       and cleaned["projects"][1]["name"] == "高温合金粉末制备项目",
       "项目顺序与内容正确")
    ok(cleaned["direction_summary"] == "钛合金与高温合金材料研发",
       "方向总结保留")
    ok(projects_mod.clean_projects({"projects": []}, text) == {
        "projects": [], "direction_summary": ""}, "无项目返回空结果")
    ok(projects_mod.clean_projects("not-a-dict", text) is None,
       "非法模型输出返回 None（不缓存）")
    long_out = projects_mod.clean_projects({
        "projects": [{"name": "基于钛合金的组织调控项目",
                      "summary": "长" * 300,
                      "evidence": "基于钛合金的组织调控项目"}],
        "direction_summary": "方" * 200}, text)
    ok(len(long_out["projects"][0]["summary"]) <= projects_mod.MAX_SUMMARY
       and len(long_out["direction_summary"]) <= projects_mod.MAX_DIRECTION,
       "摘要与方向总结按上限截断")

    # ---- 2 接口与缓存 ----
    print("\n2 接口与缓存")
    cid = db.insert_candidate(conn, {"identity_key": "proj:1", "name": "项目甲",
                                     "edu_level": "硕士"})
    db.insert_document(conn, {"file_hash": "h-proj-1", "candidate_id": cid,
                              "file_name": "项目甲.pdf", "raw_text": text,
                              "parse_ok": True, "kind": "resume"})
    conn.commit()

    calls = {"n": 0}
    original = projects_mod.summarize

    def fake_summarize(raw, cfg=None):
        calls["n"] += 1
        return projects_mod.clean_projects(dirty, raw)

    projects_mod.summarize = fake_summarize
    original_enabled = server._llm_enabled
    original_cfg = server.llm.load_cfg
    server._llm_enabled = lambda: True
    server.llm.load_cfg = lambda: {"api_key": "test", "base_url": "http://test",
                                   "model": "test"}
    try:
        st, j = jcall("GET", f"/api/candidates/{cid}/projects")
        ok(st == 200 and j.get("ok") and not j.get("cached")
           and len(j.get("projects") or []) == 2,
           "首次请求生成项目总结", str(j))
        ok(calls["n"] == 1, "首次只调用一次模型")
        st, j = jcall("GET", f"/api/candidates/{cid}/projects")
        ok(st == 200 and j.get("cached") and len(j.get("projects") or []) == 2,
           "第二次请求命中缓存", str(j))
        ok(calls["n"] == 1, "命中缓存后不再调用模型")
        row = db.get_candidate(conn, cid) or {}
        ok(row.get("projects_json"), "项目总结已写入 candidates.projects_json")
        detail = db.candidate_detail(conn, cid) or {}
        ok(detail.get("projects_checked") is True
           and len((detail.get("projects") or {}).get("projects") or []) == 2,
           "candidate_detail 下发项目总结与 checked 标记")
    finally:
        projects_mod.summarize = original
        server._llm_enabled = original_enabled
        server.llm.load_cfg = original_cfg

    # 无原文：直接缓存空结果，不调模型
    cid2 = db.insert_candidate(conn, {"identity_key": "proj:2", "name": "项目乙"})
    conn.commit()
    st, j = jcall("GET", f"/api/candidates/{cid2}/projects")
    ok(st == 200 and j.get("ok") and not (j.get("projects") or []),
       "无简历原文返回空项目", str(j))
    row2 = db.get_candidate(conn, cid2) or {}
    ok(row2.get("projects_json") is not None,
       "无原文也记“已检查”，避免反复请求")

    conn.close()
    print("\n" + "=" * 70)
    print(f"结果：{PASS} 通过，{FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
