# -*- coding: utf-8 -*-
"""初筛简历按时间段下载回归测试（v4.1.1）。

覆盖：
  · 按**投递时间**筛选：所选时间段投递 + 当前初筛通过
  · 不是按“进入初筛通过”的时间筛选
  · 压缩包与名单表使用同一时间段
  · 上次下载时间段写入 settings，并在 GET 接口返回
  · 非法日期 / 空结果不覆盖上次成功记录
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import zipfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

_TMP = tempfile.mkdtemp(prefix="bundle_test_")
_DB = os.path.join(_TMP, "test.db")
os.environ["TP_DB_PATH"] = _DB
os.environ["TP_AUTH"] = "0"
os.environ["TP_AUTO_INSIGHT"] = "0"
os.environ["TP_DAILY_TASK"] = "0"
os.environ["TP_BRIEF_LLM"] = "0"

from app import db, export, server  # noqa: E402
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


def jcall(method, url):
    st, text = _asgi(server.app, method, url,
                     {"Content-Type": "application/json"}, b"")
    try:
        return st, json.loads(text)
    except Exception:                                            # noqa: BLE001
        return st, text


def add_candidate(conn, name, applied_at, *, with_file=True,
                  stage_changed_at="2026-11-01 10:00:00"):
    cid = db.insert_candidate(conn, {
        "identity_key": "bundle:" + name, "name": name,
        "edu_level": "硕士", "school": "测试大学", "major": "材料科学与工程",
        "gender": "男"})
    aid = db.insert_application(conn, {
        "candidate_id": cid, "stage": "初筛通过",
        "applied_at": applied_at})
    conn.execute("UPDATE applications SET stage_changed_at = ? WHERE id = ?",
                 (stage_changed_at, aid))
    conn.commit()
    if with_file:
        p = os.path.join(_TMP, f"{name}.pdf")
        with open(p, "wb") as fh:
            fh.write(b"%PDF-1.4\n% test resume\n")
        db.insert_document(conn, {
            "file_hash": "hash-" + name, "candidate_id": cid,
            "application_id": aid, "file_name": f"{name}.pdf",
            "archived_path": p, "parse_ok": True, "kind": "resume"})
    return cid, aid


def main() -> int:
    print("=" * 70)
    print("初筛简历按时间段下载回归测试")
    print("=" * 70)

    conn = db.connect(_DB)
    # 九月投递：甲有原件，丙原件丢了；乙十月投递。丙/乙的“变成初筛通过”
    # 时间故意放在别处，证明筛选看的是投递时间。
    add_candidate(conn, "九月甲", "2026-09-10 10:00:00",
                  stage_changed_at="2026-11-01 10:00:00")
    add_candidate(conn, "十月乙", "2026-10-10 10:00:00",
                  stage_changed_at="2026-09-15 10:00:00")
    # 九月通过但原件丢了：名单里应有他，zip 里不可能有
    add_candidate(conn, "九月丙", "2026-09-20 10:00:00", with_file=False)
    # 一月投递、九月才变成初筛通过：按投递时间不应落进九月区间
    add_candidate(conn, "老数据", "2026-01-10 09:00:00",
                  stage_changed_at="2026-09-25 10:00:00")

    # ---- 1 组装层 ----
    print("\n1 组装层")
    zname, pairs = export.bundle_name(
        conn, {"date_from": "2026-09-01", "date_to": "2026-09-30"},
        operator="test", role="hr", scope="初筛通过", n=0)
    ok(zname == "初筛通过-2026-09-01-2026-09-30.zip", "zip 文件名带时间段", zname)
    ok(len(pairs) == 1 and "九月甲" in pairs[0][1],
       "只打包九月投递且当前初筛通过的人", str([p[1] for p in pairs]))
    roster = export.bundle_roster(conn, operator="test", role="hr",
                                  date_from="2026-09-01",
                                  date_to="2026-09-30")
    ok(roster["n"] == 2 and "2026-09-01" in roster["filename"]
       and "2026-09-30" in roster["filename"],
       "名单表同一投递时间段（含原件丢失的丙）", f"{roster['n']} 人 {roster['filename']}")

    # ---- 2 接口与上次下载 ----
    print("\n2 接口与上次下载")
    st, j = jcall("GET", "/api/bundle/initial-pass/last")
    ok(st == 200 and j.get("last") is None, "首次没有上次下载记录", str(j))

    caps = {}
    st, _ = _asgi(server.app, "POST",
                  "/api/bundle/initial-pass?date_from=2026-09-01&date_to=2026-09-30",
                  {"Content-Type": "application/json"}, b"", caps=caps)
    ok(st == 200 and len(caps.get("body_bytes") or b"") > 100,
       "按时间段下载返回 zip", str(st))
    zf = zipfile.ZipFile(io.BytesIO(caps.get("body_bytes") or b""))
    names = zf.namelist()
    ok(any("九月甲" in n for n in names) and not any("十月乙" in n for n in names),
       "zip 只含九月的人", str(names)[:160])
    ok(any(n.startswith("初筛通过名单-") for n in names),
       "zip 内附名单表")

    st, j = jcall("GET", "/api/bundle/initial-pass/last")
    last = (j or {}).get("last") or {}
    ok(st == 200 and last.get("date_from") == "2026-09-01"
       and last.get("date_to") == "2026-09-30"
       and last.get("files") == 1 and last.get("roster") == 2,
       "上次下载时间段已记录", str(last))

    st, j = jcall("POST", "/api/bundle/initial-pass?date_from=2026-13-01")
    ok(st == 422, "非法日期被拒", str(j))
    st, j = jcall("POST",
                  "/api/bundle/initial-pass?date_from=2026-10-01&date_to=2026-09-01")
    ok(st == 422, "开始日期晚于结束日期被拒", str(j))
    st, j = jcall("POST",
                  "/api/bundle/initial-pass?date_from=2026-08-01&date_to=2026-08-31")
    ok(st == 404, "空结果返回 404", str(j))
    st, j = jcall("GET", "/api/bundle/initial-pass/last")
    last2 = (j or {}).get("last") or {}
    ok(last2.get("date_from") == "2026-09-01" and last2.get("date_to") == "2026-09-30",
       "空结果不覆盖上次成功记录", str(last2))

    # 不带日期仍然可下载全部（兼容旧调用）
    st, j = jcall("POST", "/api/bundle/initial-pass")
    ok(st == 200, "不带日期仍下载全部（兼容）", str(st)[:80])
    st, j = jcall("GET", "/api/bundle/initial-pass/last")
    last3 = (j or {}).get("last") or {}
    ok(last3.get("date_from") == "" and last3.get("date_to") == "",
       "全部下载记录为空时间段", str(last3))

    conn.close()
    print("\n" + "=" * 70)
    print(f"结果：{PASS} 通过，{FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
