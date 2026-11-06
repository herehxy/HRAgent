# -*- coding: utf-8 -*-
"""阶段变更 / 提案执行的回归测试（v4.1.5）。

背景：`db.set_application_stage` 新增 `sign_status` 参数后，两个调用点仍用
位置传参，把「用户名」传成了「签约情况」——
  · 档案右上角改阶段 → 422「签约情况只能是：未签约、已签约、拒签、毁约」
  · 提案「同意并执行」set_stage → 同样的错误
本文件锁住这两条链。
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile

from openpyxl import load_workbook

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

_TMP = tempfile.mkdtemp(prefix="stage_test_")
_DB = os.path.join(_TMP, "test.db")
os.environ["TP_DB_PATH"] = _DB
os.environ["TP_AUTH"] = "0"
os.environ["TP_AUTO_INSIGHT"] = "0"
os.environ["TP_DAILY_TASK"] = "0"
os.environ["TP_BRIEF_LLM"] = "0"

from app import actions, db, server  # noqa: E402
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


def jcall(method, url, body=None):
    data = b""
    headers = {"Content-Type": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    st, text = _asgi(server.app, method, url, headers, data)
    try:
        return st, json.loads(text)
    except Exception:                                            # noqa: BLE001
        return st, text


def mk_app(conn, name, stage):
    cid = db.insert_candidate(conn, {
        "identity_key": "stage:" + name, "name": name,
        "edu_level": "硕士", "school": "测试大学"})
    aid = db.insert_application(conn, {"candidate_id": cid, "stage": stage})
    return cid, aid


def main() -> int:
    print("=" * 70)
    print("阶段变更 / 提案执行回归测试")
    print("=" * 70)

    conn = db.connect(_DB)

    # ---- 1 档案右上角：待面试 → 新投递（回退） ----
    print("\n1 阶段回退（待面试 → 新投递）")
    cid, aid = mk_app(conn, "回退甲", "待面试")
    st, j = jcall("POST", f"/api/applications/{aid}/stage",
                  {"stage": "新投递"})
    row = db.get_application(conn, aid) or {}
    ok(st == 200 and row.get("stage") == "新投递",
       "改阶段返回 200 且阶段已回退", f"HTTP {st} stage={row.get('stage')} {j}")
    ok(not row.get("sign_status"),
       "改阶段不会把用户名写进签约情况", str(row.get("sign_status")))
    ok(row.get("sign_status") != "HR",
       "签约情况没有被污染为操作人")

    # ---- 2 提案「同意并执行」：set_stage ----
    print("\n2 提案执行（set_stage）")
    cid2, aid2 = mk_app(conn, "提案乙", "待面试")
    pid = db.create_proposal(
        conn, "stage-test", "set_stage",
        {"application_id": aid2, "stage": "新投递"},
        "把「提案乙」回退到新投递", risk="低")
    r = actions.apply_proposal(_DB, pid, "approve", operator="hr_t", role="hr")
    row2 = db.get_application(conn, aid2) or {}
    ok(r.get("ok") and row2.get("stage") == "新投递",
       "同意提案后阶段已回退", f"{r} stage={row2.get('stage')}")
    ok(not row2.get("sign_status"),
       "提案执行不会污染签约情况", str(row2.get("sign_status")))

    # ---- 3 面试提纲的 JD 摘要必须容忍缺字段 ----
    print("\n3 面试提纲 JD 摘要容错")
    from app.pipeline.analyze import _jd_brief
    brief = _jd_brief({"role": "数字化工程师", "must": {}})
    ok("未指定" in brief and "不限" in brief,
       "JD 缺 skills_required / education_min 时不抛 KeyError", brief.replace("\n", " | ")[:100])
    brief2 = _jd_brief({"role": "工艺技术", "must": {"education_min": "硕士"}})
    ok("硕士" in brief2 and "必需技能:未指定" in brief2,
       "JD 只有部分字段时正常渲染")

    # ---- 4 跨盘路径展示 ----
    print("\n4 跨盘路径")
    from app.server import BASE as _BASE, _rel_or_abs
    _other = "C:\\hra_cross_drive_probe" if not _BASE.upper().startswith("C:") else "D:\\hra_cross_drive_probe"
    ok(_rel_or_abs(_other) == _other,
       "数据目录与程序不在同一盘时不抛 ValueError", _rel_or_abs(_other))

    # ---- 5 提案拒绝 ----
    print("\n5 提案拒绝")
    cid3, aid3 = mk_app(conn, "提案丙", "待面试")
    pid3 = db.create_proposal(
        conn, "stage-test", "set_stage",
        {"application_id": aid3, "stage": "新投递"},
        "把「提案丙」回退到新投递", risk="低")
    r3 = actions.apply_proposal(_DB, pid3, "reject", operator="hr_t", role="hr")
    row3 = db.get_application(conn, aid3) or {}
    ok(r3.get("ok") and r3.get("status") == "已拒绝"
       and row3.get("stage") == "待面试",
       "拒绝提案只改提案状态、不动档案", str(r3))

    # ---- 6 过期提案 / 阶段时间 / 旧签约值 ----
    print("\n6 过期提案与阶段时间")
    cid4, aid4 = mk_app(conn, "过期提案", "新投递")
    pid4 = db.create_proposal(
        conn, "stage-test", "set_stage",
        {"application_id": aid4, "stage": "初筛通过"},
        "把「过期提案」推进到初筛通过", risk="低")
    db.update_application_fields(conn, aid4, operator="test", role="hr",
                                 interview_unit="测试所")
    db.set_application_stage(conn, aid4, "待面试", operator="test", role="hr")
    r4 = actions.apply_proposal(_DB, pid4, "approve", operator="hr_t", role="hr")
    row4 = db.get_application(conn, aid4) or {}
    ok(not r4.get("ok") and "刷新" in str(r4.get("error") or "")
       and row4.get("stage") == "待面试",
       "档案已被推进后，过期提案不会把阶段回退", f"{r4} stage={row4.get('stage')}")

    cid5, aid5 = mk_app(conn, "阶段时间", "已面试")
    conn.execute("UPDATE applications SET stage_changed_at = ? WHERE id = ?",
                 ("2026-01-01 09:00:00", aid5))
    conn.commit()
    db.set_application_stage(conn, aid5, "已面试", "拟录用",
                             operator="test", role="hr")
    row5 = db.get_application(conn, aid5) or {}
    ok(row5.get("stage_changed_at") == "2026-01-01 09:00:00",
       "只改面试结论不刷新 stage_changed_at", str(row5.get("stage_changed_at")))

    try:
        db.set_application_stage(conn, aid5, "已面试", "已签约",
                                 operator="test", role="hr")
        _bad_legacy = False
    except ValueError as exc:
        _bad_legacy = "已发放offer" in str(exc)
    ok(_bad_legacy, "旧「已签约」结论不能写进「已面试」阶段")

    # ---- 7 批量归岗 / 按阶段与面试日期导出 ----
    print("\n7 批量归岗与按范围导出")
    _jid = db.create_job(conn, "批量归岗测试岗",
                         jd={"title": "批量归岗测试岗", "must": {}, "preferred": {}},
                         operator="test")
    _c6, _a6 = mk_app(conn, "批量归岗甲", "新投递")
    _c7, _a7 = mk_app(conn, "批量归岗乙", "新投递")
    st, j = jcall("POST", "/api/candidates/bulk-assign-job",
                  {"ids": [_c6, _c7], "job_id": _jid})
    _r6 = db.get_application(conn, _a6) or {}
    _r7 = db.get_application(conn, _a7) or {}
    ok(st == 200 and j.get("assigned") == 2
       and _r6.get("job_id") == _jid and _r7.get("job_id") == _jid,
       "批量归岗：两人都归到指定岗位", str(j))

    _c8, _a8 = mk_app(conn, "导出甲", "待面试")
    _c9, _a9 = mk_app(conn, "导出乙", "待面试")
    db.update_application_fields(conn, _a8, operator="test", role="hr",
                                 interview_date="2026-12-01")
    db.update_application_fields(conn, _a9, operator="test", role="hr",
                                 interview_date="2026-12-02")
    _caps = {}
    _st, _ = _asgi(server.app, "GET",
                   "/api/export/roster?stage=%E5%BE%85%E9%9D%A2%E8%AF%95"
                   "&interview_date=2026-12-01", {}, b"", caps=_caps)
    _wb = load_workbook(io.BytesIO(_caps.get("body_bytes") or b""))
    _ws = _wb.active
    _names = [str(_ws.cell(row=r, column=4).value or "")
              for r in range(2, _ws.max_row + 1)]
    ok(_st == 200 and "导出甲" in _names and "导出乙" not in _names,
       "导出名单可按阶段 + 面试日期筛选", f"{_names}")

    # ---- 8 助手新工具：名单下载 / 批量操作提案 ----
    print("\n8 助手新工具")
    from app.agent import tools as agent_tools
    _ctx = agent_tools.ToolCtx(db_path=_DB, operator="test", role="hr")
    _t = json.loads(agent_tools.execute(
        "export_roster", {"stage": "待面试", "interview_date": "2026-12-01"}, _ctx))
    ok(_t.get("count") == 1 and "interview_date=2026-12-01" in (_t.get("download_url") or ""),
       "助手 export_roster 返回按日期筛选的下载链接", str(_t)[:120])
    _t = json.loads(agent_tools.execute(
        "bulk_initial_pass", {"candidate_ids": [_c6, _c7]}, _ctx))
    ok(_t.get("proposal_id"), "助手 bulk_initial_pass 生成待确认提案", str(_t)[:120])
    _t = json.loads(agent_tools.execute(
        "bulk_assign_job", {"candidate_ids": [_c6, _c7], "job": str(_jid)}, _ctx))
    ok(_t.get("proposal_id"), "助手 bulk_assign_job 生成待确认提案", str(_t)[:120])
    from app.server import _needs_full_reanalysis
    _tg = _needs_full_reanalysis(conn, 20, False, [_c6])
    ok(len(_tg) == 1 and _tg[0]["cid"] == _c6,
       "批量重新分析按选中 id 过滤", str(_tg))
    _tg2 = _needs_full_reanalysis(conn, 20, True)
    ok(all(x["cid"] for x in _tg2),
       "分析未分析的人按规则通道筛选", f"{len(_tg2)} 人")

    conn.close()
    print("\n" + "=" * 70)
    print(f"结果：{PASS} 通过，{FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
