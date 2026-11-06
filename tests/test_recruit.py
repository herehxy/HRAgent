# -*- coding: utf-8 -*-
"""招聘计划 / 招聘进度 / 签约同步 / 批量初筛 的独立回归测试（v4.0.17）。

跑法（仓库根目录）：
    PYTHONPATH=".runtime_deps;.runtime_local" python -X utf8 tests/test_recruit.py

为什么单独一个文件：
  `tests/selftest.py` 是历史全量自检，里面有一批 v4.0.6 前后就存在的旧断言；
  本文件只覆盖这次新增的链路，失败时能直接定位，不被历史噪音淹没。

覆盖：
  · 老库迁移（recruit 表 / hire_unit / sign_status）
  · 届别校验、计划数学、批量保存、删除单位
  · Excel 导入（合并单元格、merge/overwrite）
  · 已签汇总（新 sign_status + 旧 outcome 兼容）、体检、去重/冲突
  · 面试名单（已签/拒签/毁约/未签约）、是否录用、签约情况
  · 官方/平铺/面试名单 Excel 结构
  · 批量初筛通过 + 采纳建议岗位
  · 权限（edit_plan / export / 审计仅管理员）
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

# 必须在 import app.server 之前定库
_TMP = tempfile.mkdtemp(prefix="recruit_test_")
_DB = os.path.join(_TMP, "test.db")
os.environ["TP_DB_PATH"] = _DB
os.environ["TP_AUTH"] = "0"
os.environ["TP_AUTO_INSIGHT"] = "0"
os.environ["TP_DAILY_TASK"] = "0"
os.environ["TP_BRIEF_LLM"] = "0"

from openpyxl import Workbook, load_workbook  # noqa: E402

from app import auth, db, recruit, recruit_export, regrade, stages  # noqa: E402
from app import server  # noqa: E402
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


def jcall(method, url, body=None, headers=None):
    data = b""
    h = {"Content-Type": "application/json"}
    h.update(headers or {})
    if body is not None:
        if isinstance(body, (bytes, bytearray)):
            data = bytes(body)          # Excel 导入走 raw body，不是 JSON
        else:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    st, text = _asgi(server.app, method, url, h, data)
    try:
        return st, json.loads(text)
    except Exception:                                            # noqa: BLE001
        return st, text


def mk_app(conn, name, edu="博士", stage="已发放offer", unit=None, signed=None,
           sign_status=None, outcome=None, interview_date=None, suggested_job_id=None):
    cid = db.insert_candidate(conn, {
        "identity_key": "rec:" + name, "name": name, "edu_level": edu,
        "school": "测试大学"})
    aid = db.insert_application(conn, {
        "candidate_id": cid, "stage": stage,
        "suggested_job_id": suggested_job_id})
    fields = {}
    if unit is not None:
        fields["hire_unit"] = unit
    if signed is not None:
        fields["signed_at"] = signed
    if sign_status is not None:
        fields["sign_status"] = sign_status
    if outcome is not None:
        fields["outcome"] = outcome
    if interview_date is not None:
        fields["interview_date"] = interview_date
    if fields:
        db.update_application_fields(conn, aid, operator="test", role="hr", **fields)
    return cid, aid


def make_xlsx(rows, merges=()):
    wb = Workbook()
    ws = wb.active
    ws.title = "2027届招聘进度"
    ws.append([" 2027年度招聘进度表（2026.9.1-2027.8.31，以签约时间为准，"
               "计划+追加=签协议+取消）"])
    ws.append(["序号", "单位", "学历", "上会审批需求计划数量", "追加计划数",
               "转上年度计划数", "应取消计划数", "应招总数", "已签人数",
               "签约缺口", "完成率"])
    for i, (unit, edu, n) in enumerate(rows, 1):
        ws.append([i, unit, edu, n, 0, 0, 0])
    for m in merges:
        ws.merge_cells(m)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def main() -> int:
    print("=" * 70)
    print("招聘计划 / 进度 / 签约同步 / 批量初筛 回归测试")
    print("=" * 70)

    conn = db.connect(_DB)

    # ------------------------------------------------------------ 0 迁移
    print("\n0 迁移与表结构")
    tabs = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    ok({"recruit_years", "recruit_plans", "agent_conversations"} <= tabs,
       "新表建立（recruit_years/recruit_plans/agent_conversations）")
    app_cols = {r[1] for r in conn.execute(
        "PRAGMA table_info(applications)").fetchall()}
    ok({"hire_unit", "sign_status"} <= app_cols, "applications 有 hire_unit/sign_status")
    ok(stages.assert_consistent() == [], "stages 兜底表与 config/stages.json 一致")

    legacy_src = os.path.join(BASE, "data", "workbench.db")
    if os.path.exists(legacy_src):
        legacy = os.path.join(_TMP, "legacy.db")
        shutil.copy(legacy_src, legacy)
        lc = db.connect(legacy)
        lcols = {r[1] for r in lc.execute("PRAGMA table_info(applications)").fetchall()}
        ltabs = {r[0] for r in lc.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        n0 = lc.execute("SELECT COUNT(*) FROM candidates").fetchone()[0]
        ok({"hire_unit", "sign_status"} <= lcols and
           {"recruit_years", "recruit_plans"} <= ltabs and n0 >= 0,
           "老库副本可打开并自动补列/建表", f"{n0} 人")
        lc.close()

    # ------------------------------------------------------------ 1 届别
    print("\n1 届别与计划数学")
    y = recruit.create_year(conn, "2027届", "2026-09-01", "2027-08-31")
    ok(y.get("ok"), "创建 2027届")
    yid = y["id"]
    bad = recruit.create_year(conn, "2028届", "2027-01-01", "2027-12-31")
    ok(not bad.get("ok"), "届期重叠被拒", "；".join(bad.get("errors") or []))
    ok(not recruit.create_year(conn, "坏届", "2027-09-01", "2027-08-01").get("ok"),
       "开始日期晚于结束日期被拒")
    # 非 ISO 写法必须归一后再落库，否则字符串比较会骗过重叠校验
    y_norm = recruit.create_year(conn, "2026届", "2025/9/1", "2026/8/31")
    ok(y_norm.get("ok") and y_norm.get("year", {}).get("period_start") == "2025-09-01"
       and y_norm.get("year", {}).get("period_end") == "2026-08-31",
       "非 ISO 届期归一为 YYYY-MM-DD", str(y_norm.get("year")))
    ok(not recruit.create_year(conn, "2026秋", "2026-01-01", "2026-12-31").get("ok"),
       "归一后重叠仍被拒")

    for u, e, n in (("钛所", "博士", 3), ("钛所", "硕士", 3),
                    ("超导所", "博士", 2)):
        db.save_recruit_plan(conn, year_id=yid, unit_name=u, edu_level=e,
                             plan_approved=n, mode="overwrite")
    db.save_recruit_plan(conn, year_id=yid, unit_name="生物所", edu_level="博士",
                         plan_approved=3, plan_carryover=1, mode="overwrite")
    conn.commit()
    p = recruit.build_progress(conn, yid)
    ok(p["totals"]["should_hire"] == 12 and p["totals"]["signed_count"] == 0,
       "计划数学：应招=审批+追加+转上年度-取消", str(p["totals"]))
    ok(recruit.validate_plan_rows([{"id": 1, "plan_approved": -1}]),
       "批量保存负数被拒")
    ok(recruit.validate_plan_rows([{"id": 1, "plan_approved": 1,
                                    "plan_cancel": 2}]),
       "应招总数为负被拒")

    # ------------------------------------------------------------ 2 签约同步
    print("\n2 已签汇总与数据体检")
    cid_a, aid_a = mk_app(conn, "甲", "博士", "已发放offer", "钛所", "2026-10-01",
                          "已签约", "拟录用")
    db.update_application_fields(conn, aid_a, operator="test", role="hr",
                                 onboard_deadline="2027-07-01")
    cid_b, aid_b = mk_app(conn, "乙", "硕士", "已发放offer", "钛所", "2026-10-02",
                          None, "已签约")                 # 旧 outcome 兼容
    mk_app(conn, "丙", "硕士", "已发放offer", "钛所", None, "已签约", "拟录用")
    mk_app(conn, "丁", "博士", "已发放offer", None, "2026-10-03", "已签约", "拟录用")
    mk_app(conn, "戊", "博士", "已发放offer", "钛所", "2025-10-01", "已签约", "拟录用")
    mk_app(conn, "己", "博士", "已发放offer", "不存在单位", "2026-10-04",
           "已签约", "拟录用")
    mk_app(conn, "庚", "其他", "已发放offer", "钛所", "2026-10-05", "已签约", "拟录用")
    # 同一人同一单位两条 → 计 1 人
    cid_h, aid_h1 = mk_app(conn, "辛", "博士", "已发放offer", "钛所", "2026-10-06",
                           "已签约", "拟录用")
    aid_h2 = db.insert_application(conn, {"candidate_id": cid_h, "stage": "已发放offer"})
    db.update_application_fields(conn, aid_h2, operator="test", role="hr",
                                 hire_unit="钛所", signed_at="2026-10-07",
                                 sign_status="已签约", outcome="拟录用")
    # 同一人不同单位 → 冲突，全不计
    cid_i, _ = mk_app(conn, "壬", "博士", "已发放offer", "钛所", "2026-10-08",
                      "已签约", "拟录用")
    aid_i2 = db.insert_application(conn, {"candidate_id": cid_i, "stage": "已发放offer"})
    db.update_application_fields(conn, aid_i2, operator="test", role="hr",
                                 hire_unit="超导所", signed_at="2026-10-09",
                                 sign_status="已签约", outcome="拟录用")
    # 拒签 / 毁约 / 未签约：只进名单，不进进度
    cid_j, _ = mk_app(conn, "癸", "博士", "已结束", None, None, "拒签", "拟录用",
                      "2026-10-10")
    cid_k, _ = mk_app(conn, "子", "博士", "已结束", None, "2026-10-11", "毁约", "拟录用")
    cid_l, _ = mk_app(conn, "丑", "博士", "已发放offer", "钛所", None, "未签约",
                      "拟录用", "2026-10-12")
    # 显式已签约、面试结论未判：只进体检，不改变已签人数（缺录用单位也不计）
    mk_app(conn, "寅", "博士", "已发放offer", None, "2026-10-13", "已签约", None)
    # 真假冲突：显式未签约 + 历史 outcome=已签约；verdict 不能被旧值带跑
    _cid_legacy, _aid_legacy = mk_app(
        conn, "卯", "博士", "已发放offer", "钛所", None, "未签约", "已签约")
    ok(stages.verdict("已面试", "已签约", "未签约") == "",
       "显式未签约时，历史 outcome=已签约 不再冒充录用")
    st, _ = jcall("POST", f"/api/applications/{_aid_legacy}/interview-info",
                  {"sign_status": "未签约"})
    _cur_legacy = db.get_application(conn, _aid_legacy)
    ok(st == 200 and not _cur_legacy.get("outcome"),
       "保存签约情况时清掉历史 outcome=已签约", str(_cur_legacy.get("outcome")))
    # 毁约（旧 outcome 形式）也受彻底删除保护
    _cid_purge, _aid_purge = mk_app(conn, "巳", "博士", "已结束", None, "2026-10-14",
                                    None, "毁约")
    conn.execute("UPDATE candidates SET archived_at = ? WHERE id = ?",
                 ("2026-01-01 00:00:00", _cid_purge))
    conn.commit()
    _purged = db.purge_due_candidates(conn, 30, only_ids=[_cid_purge])
    ok(_purged["purged"] == 0 and any(x["id"] == _cid_purge
                                      for x in _purged.get("skipped_signed") or []),
       "有毁约记录的档案不被彻底删除")

    p = recruit.build_progress(conn, yid)
    # 甲/乙/辛 = 3 人；壬冲突 0；其余体检
    ok(p["totals"]["signed_count"] == 3, "已签汇总：新字段+旧 outcome 兼容，重复只计 1 人",
       str(p["totals"]))
    _sig = next((x for r in p["rows"] for x in (r.get("signees") or [])
                 if x.get("name") == "甲"), {})
    ok(_sig.get("onboard_deadline") == "2027-07-01",
       "签约人下发入职最后期限（页面折叠展示用）", str(_sig))
    ok(p["health"]["missing_signed_at"]["count"] == 1, "缺签约时间进体检")
    ok(p["health"]["missing_hire_unit"]["count"] == 2, "缺录用单位进体检")
    ok(p["health"]["out_of_period"]["count"] == 1, "期外签约进体检")
    ok(p["health"]["unit_not_in_plan"]["count"] == 1, "单位不在计划进体检")
    ok(p["health"]["unknown_edu"]["count"] == 1, "学历异常进体检")
    ok(p["health"]["duplicate_signed"]["count"] == 1, "同单位重复签约按 1 人计并提示")
    ok(p["health"]["conflicting_signings"]["count"] == 1, "跨单位签约冲突全不计")
    ok(p["health"]["signed_without_conclusion"]["count"] == 1,
       "已签约但面试结论未判进体检")
    ok(p["by_education"][0]["should_hire"] == 9,
       "学历汇总：博士 钛所3+超导所2+生物所4=9",
       str([(x["edu_level"], x["should_hire"]) for x in p["by_education"]]))
    ok("招聘完成率" in p["summary"] and "应招聘" in p["summary"], "总结句生成")

    # 跨计划行的重复签约：一条在计划内、一条单位不在计划内，仍算“归属冲突”，
    # 不能只把有效那条计入（否则同一人被两家单位同时签，进度里却显示正常）。
    _y29 = recruit.create_year(conn, "2029届", "2028-09-01", "2029-08-31")
    _y29id = _y29["id"]
    db.save_recruit_plan(conn, year_id=_y29id, unit_name="钛所", edu_level="本科",
                         plan_approved=2, mode="overwrite")
    conn.commit()
    _cid_x = db.insert_candidate(conn, {"identity_key": "rec:跨计划冲突",
                                        "name": "跨计划冲突", "edu_level": "本科"})
    _a1 = db.insert_application(conn, {"candidate_id": _cid_x, "stage": "已发放offer"})
    _a2 = db.insert_application(conn, {"candidate_id": _cid_x, "stage": "已发放offer"})
    db.update_application_fields(conn, _a1, operator="test", role="hr",
                                 hire_unit="钛所", signed_at="2028-10-01",
                                 sign_status="已签约", outcome="拟录用")
    db.update_application_fields(conn, _a2, operator="test", role="hr",
                                 hire_unit="不存在单位", signed_at="2028-10-02",
                                 sign_status="已签约", outcome="拟录用")
    _px = recruit.build_progress(conn, _y29id)
    ok(_px["totals"]["signed_count"] == 0
       and _px["health"]["conflicting_signings"]["count"] == 1
       and _px["health"]["unit_not_in_plan"]["count"] == 0,
       "计划内 + 计划外单位双签按冲突处理，不计入任何一行",
       str(_px["totals"]))

    # ------------------------------------------------------------ 3 名单
    print("\n3 面试总名单")
    ro = recruit.build_roster(conn, yid, include_phone=True)
    by_name = {x["name"]: x for x in ro["items"]}
    ok(by_name["癸"]["sign_status"] == "拒签" and by_name["癸"]["verdict"] == "否",
       "拒签：在面试名单、是否录用=否")
    ok(by_name["子"]["sign_status"] == "毁约" and by_name["子"]["verdict"] == "否",
       "毁约：在面试名单、是否录用=否")
    ok(by_name["丑"]["sign_status"] == "未签约" and by_name["丑"]["verdict"] == "是",
       "未签约但拟录用：在名单、是否录用=是")
    ok(all(x["sign_status"] in db.SIGN_STATUSES for x in ro["items"]),
       "名单签约情况只出现四档")
    ro_f = recruit.build_roster(conn, yid, outcome="拒签")
    ok(ro_f["count"] == 1 and ro_f["items"][0]["name"] == "癸", "名单按签约情况筛选")

    # ------------------------------------------------------------ 4 导入
    print("\n4 Excel 导入")
    xls = make_xlsx([("测试所", "博士", 2), ("测试所", "硕士", 1),
                     ("测试所", "本科", 0), ("测试所", "大专", 0),
                     ("第二所", "博士", 1), ("第二所", "硕士", 0),
                     ("第二所", "本科", 0), ("第二所", "大专", 3)],
                    merges=("B3:B6", "B7:B10", "A3:A6", "A7:A10"))
    parsed = recruit.parse_plan_xlsx(xls)
    ok(parsed["ok"] and len(parsed["rows"]) == 8, "导入解析：8 行（合并单位自动填充）")
    y2 = recruit.create_year(conn, "2028届", "2027-09-01", "2028-08-31")
    y2id = y2["id"]
    rep1 = recruit.apply_plan_import(conn, parsed, y2id, mode="merge")
    rep2 = recruit.apply_plan_import(conn, parsed, y2id, mode="merge")
    rep3 = recruit.apply_plan_import(conn, parsed, y2id, mode="overwrite")
    ok(rep1["inserted"] == 8 and rep2["skipped"] == 8 and rep3["updated"] == 8,
       "导入 merge 只补缺行 / overwrite 覆盖", f"{rep1} {rep2} {rep3}")
    p2 = recruit.build_progress(conn, y2id)
    ok(p2["totals"]["should_hire"] == 7, "导入后应招合计=2+1+1+3=7")

    # ------------------------------------------------------------ 5 删除单位
    print("\n5 删除单位")
    db.save_recruit_plan(conn, year_id=yid, unit_name="临时单位", edu_level="博士",
                         plan_approved=1, mode="overwrite")
    conn.commit()
    d = db.delete_recruit_unit(conn, yid, "临时单位", operator="test", role="hr")
    ok(d["deleted"] == 1, "删除单位计划行")
    ok(db.delete_recruit_unit(conn, yid, "临时单位")["deleted"] == 0,
       "重复删除返回 0")

    # ------------------------------------------------------------ 6 批量初筛
    print("\n6 批量初筛 + 采纳建议岗位")
    jid = db.create_job(conn, "测试岗位", jd={"title": "测试岗位", "must": {},
                                              "preferred": {}}, operator="test")
    conn.commit()
    cid_m, aid_m = mk_app(conn, "批量甲", "博士", "新投递",
                          suggested_job_id=jid)
    cid_n, aid_n = mk_app(conn, "批量乙", "硕士", "新投递")
    bulk = regrade.bulk_initial_pass(conn, [cid_m, cid_n], {}, operator="test", role="hr")
    c = db.connect(_DB)
    a_m = db.get_application(c, aid_m)
    a_n = db.get_application(c, aid_n)
    c.close()
    ok(bulk["advanced"] == 2 and bulk["assigned"] == 1,
       "批量：2 人推进、1 人采纳建议岗位", str(bulk))
    ok(a_m["stage"] == "初筛通过" and a_m["job_id"] == jid,
       "批量甲：阶段=初筛通过、已归岗")
    ok(a_n["stage"] == "初筛通过" and not a_n["job_id"],
       "批量乙：阶段=初筛通过、无建议岗位保持待指定")
    # 已结束的投递不回退、不归岗
    cid_o, aid_o = mk_app(conn, "终态甲", "博士", "已结束", suggested_job_id=jid)
    bulk2 = regrade.bulk_initial_pass(conn, [cid_o], {}, operator="test", role="hr")
    c = db.connect(_DB)
    a_o = db.get_application(c, aid_o)
    c.close()
    ok(bulk2["already_passed"] == 1 and bulk2["assigned"] == 0
       and a_o["stage"] == "已结束" and not a_o["job_id"],
       "已结束投递：批量动作不改阶段、不归岗")
    # 建议岗位已停用：不阻断推进，只跳过归岗
    jid_off = db.create_job(conn, "停用岗位", jd={"title": "停用岗位"}, operator="test")
    db.set_job_active(conn, jid_off, False, "test")
    cid_p, aid_p = mk_app(conn, "批量丙", "博士", "新投递",
                          suggested_job_id=jid_off)
    bulk3 = regrade.bulk_initial_pass(conn, [cid_p], {}, operator="test", role="hr")
    c = db.connect(_DB)
    a_p = db.get_application(c, aid_p)
    c.close()
    ok(bulk3["advanced"] == 1 and bulk3["no_suggestion"] == 1
       and bulk3["assigned"] == 0 and a_p["stage"] == "初筛通过"
       and not a_p["job_id"], "建议岗位停用：仍推进、不归岗", str(bulk3))
    # 多投递：最新一条已结束、上一条仍在流程中 → 推上一条
    cid_q = db.insert_candidate(conn, {"identity_key": "rec:批量丁",
                                       "name": "批量丁"})
    aid_old = db.insert_application(conn, {
        "candidate_id": cid_q, "stage": "新投递",
        "applied_at": "2026-01-01 09:00:00"})
    aid_new = db.insert_application(conn, {
        "candidate_id": cid_q, "stage": "已结束",
        "applied_at": "2026-02-01 09:00:00"})
    bulk4 = regrade.bulk_initial_pass(conn, [cid_q], {}, operator="test", role="hr")
    c = db.connect(_DB)
    a_old = db.get_application(c, aid_old)
    a_new = db.get_application(c, aid_new)
    c.close()
    ok(bulk4["advanced"] == 1 and a_old["stage"] == "初筛通过"
       and a_new["stage"] == "已结束", "多投递：推进最新未结束的那条", str(bulk4))
    bulk5 = regrade.bulk_initial_pass(conn, [cid_n, cid_n], {},
                                      operator="test", role="hr")
    ok(bulk5["advanced"] == 0 and bulk5["already_passed"] == 1,
       "重复 id 去重，只统计一次", str(bulk5))

    # ------------------------------------------------------------ 7 API
    print("\n7 API 链路")
    st, j = jcall("GET", "/api/recruit/meta")
    ok(st == 200 and j.get("suggested"), "GET /api/recruit/meta")
    st, j = jcall("GET", f"/api/recruit/progress?year_id={yid}")
    ok(st == 200 and j["totals"]["signed_count"] == 3, "GET /api/recruit/progress")
    st, j = jcall("GET", f"/api/recruit/roster?year_id={yid}")
    ok(st == 200 and j.get("count", 0) >= 3, "GET /api/recruit/roster")
    st, j = jcall("POST", "/api/recruit/plans/batch",
                  {"year_id": yid, "rows": [{"id": p["rows"][0]["plan_id"],
                                             "plan_approved": 4}]})
    ok(st == 200 and j.get("changed") == 1, "POST /api/recruit/plans/batch")
    st, j = jcall("POST", "/api/recruit/plans",
                  {"year_id": yid, "unit_name": "API单位"})
    ok(st == 200 and j.get("added") == 4, "POST /api/recruit/plans（补齐 4 学历）")
    st, j = jcall("POST", "/api/recruit/plans/delete-unit",
                  {"year_id": yid, "unit_name": "API单位"})
    ok(st == 200 and j.get("deleted") == 4, "POST /api/recruit/plans/delete-unit")
    st, j = jcall("POST", f"/api/recruit/signings/{aid_m}/hire-unit",
                  {"hire_unit": "钛所"})
    ok(st == 200, "POST /api/recruit/signings/{aid}/hire-unit")
    st, j = jcall("POST", "/api/candidates/bulk-initial-pass",
                  {"ids": [cid_m, cid_n]})
    ok(st == 200 and j.get("advanced") == 0 and j.get("already_passed") == 2,
       "POST /api/candidates/bulk-initial-pass（已通过不重复推进）")

    st, j = jcall("POST", "/api/recruit/years",
                  {"label": "2030届", "period_start": "2029-09-01",
                   "period_end": "2030-08-31"})
    ok(st == 200 and j.get("id"), "POST /api/recruit/years")
    _y3 = j.get("id")
    st, j = jcall("PATCH", f"/api/recruit/years/{_y3}", {"org_label": "测试院"})
    ok(st == 200 and j.get("year", {}).get("org_label") == "测试院",
       "PATCH /api/recruit/years/{id}")

    # 档案保存 → 签约情况 → 招聘进度自动同步（走真实 interview-info 接口）
    _cid_s, _aid_s = mk_app(conn, "接口签约", "博士", "已发放offer")
    st, j = jcall("POST", f"/api/applications/{_aid_s}/interview-info",
                  {"sign_status": "已签约", "hire_unit": "测试所",
                   "signed_at": "2028-01-15"})
    ok(st == 200 and "sign_status" in (j.get("changed") or []),
       "POST /api/applications/{aid}/interview-info 写签约情况")
    _p3 = recruit.build_progress(conn, y2id)
    ok(_p3["totals"]["signed_count"] == 1
       and _p3["rows"][0]["signees"][0]["name"] == "接口签约",
       "签约情况 + 录用单位 → 自动同步到 2028 届进度")

    st, j = jcall("POST", "/api/recruit/import?dry_run=1&name=x.xlsx", xls,
                  {"Content-Type": "application/octet-stream"})
    ok(st == 200 and len(j.get("rows") or []) == 8, "POST /api/recruit/import 预览")
    st, j = jcall("POST",
                  "/api/recruit/import?dry_run=0&mode=merge&label=2029%E5%B1%8A"
                  "&period_start=2028-09-01&period_end=2029-08-31&name=x.xlsx",
                  xls, {"Content-Type": "application/octet-stream"})
    ok(st == 200 and j.get("inserted") == 8, "POST /api/recruit/import 落库")

    # ------------------------------------------------------------ 8 导出
    print("\n8 Excel 导出")
    from tests.selftest import _asgi as _a
    caps = {}
    st, _ = _a(server.app, "GET", f"/api/recruit/export?year_id={yid}&fmt=official",
               caps=caps)
    ok(st == 200 and len(caps.get("body_bytes") or b"") > 1000, "官方版导出 200")
    wb = load_workbook(io.BytesIO(caps["body_bytes"]))
    ok(len(wb.sheetnames) == 2 and "招聘进度表" in wb.sheetnames[0]
       and "面试总名单" in wb.sheetnames[1], "官方版含两个工作表", str(wb.sheetnames))
    ws = wb[wb.sheetnames[0]]
    ok(ws["A1"].value and "招聘进度表" in str(ws["A1"].value)
       and ws["A2"].value == "序号" and ws.max_column >= 21,
       "官方版标题/表头/签约人列")
    ok(any(str(r).startswith("A3:") or "A3:" in str(r) for r in ws.merged_cells.ranges),
       "官方版单位列合并")
    st, _ = _a(server.app, "GET", f"/api/recruit/export?year_id={yid}&fmt=flat",
               caps=caps)
    ok(st == 200 and len(caps.get("body_bytes") or b"") > 1000, "平铺版导出 200")
    st, _ = _a(server.app, "GET", f"/api/recruit/roster/export?year_id={yid}",
               caps=caps)
    ok(st == 200 and len(caps.get("body_bytes") or b"") > 1000, "面试名单导出 200")
    wbr = load_workbook(io.BytesIO(caps["body_bytes"]))
    hdr = [wbr.active.cell(row=2, column=c).value for c in range(1, 17)]
    ok(hdr == ["序号", "面试日期", "单位", "姓名", "学校", "学历", "面试方式",
               "电话", "是否录用", "录用函", "发放时间", "签约情况", "签约时间",
               "毕业时间", "入职情况", "备注"], "面试名单 16 列", str(hdr)[:80])

    # 签约人列不能停在 10：实际 12 人时要扩到 12 列
    for i in range(12):
        mk_app(conn, f"多签{i:02d}", "博士", "已发放offer", "多签所",
               "2029-10-01", "已签约", "拟录用")
    db.save_recruit_plan(conn, year_id=_y3, unit_name="多签所", edu_level="博士",
                         plan_approved=20, mode="overwrite")
    conn.commit()
    _p4 = recruit.build_progress(conn, _y3)
    _wb4 = load_workbook(io.BytesIO(
        recruit_export.official_workbook(_p4, recruit.build_roster(conn, _y3))))
    _ws4 = _wb4[_wb4.sheetnames[0]]
    _hdr4 = [_ws4.cell(row=2, column=c).value
             for c in range(1, _ws4.max_column + 1)]
    ok(_p4["totals"]["signed_count"] == 12 and _ws4.max_column >= 23
       and _hdr4[-1] == "签约人12",
       "签约人列按实际人数扩展到 12 列", f"{_ws4.max_column} 列")

    # 权限（函数层，不依赖登录环境）
    ok(auth.can("hr", "edit_plan") and auth.can("admin", "edit_plan")
       and not auth.can("viewer", "edit_plan"), "edit_plan 权限：hr/admin 有，viewer 无")
    ok(auth.can("hr", "export") and not auth.can("viewer", "export"),
       "export 权限：hr 有，viewer 无")
    ok(not auth.can("hr", "manage_users") and auth.can("admin", "manage_users"),
       "审计权限：只有 admin 有 manage_users")

    # ------------------------------------------------------------ 9 助手工具
    print("\n9 智能助手：招聘进度工具")
    from app.agent import tools as agent_tools
    ctx = agent_tools.ToolCtx(db_path=_DB, operator="test", role="hr")
    def tool_call(name, args):
        try:
            return json.loads(agent_tools.execute(name, args, ctx))
        except Exception as exc:                                # noqa: BLE001
            return {"__tool_error": str(exc)}
    t = tool_call("recruit_years", {})
    ok(t.get("count", 0) >= 2 and any(x.get("label") == "2027届" for x in t.get("years") or []),
       "助手工具 recruit_years")
    t = tool_call("recruit_signed_list", {"year": "2027届"})
    ok(t.get("count") == 3 and len(t.get("items") or []) == 3,
       "助手工具 recruit_signed_list（3 名已签）", str(t.get("count")))
    t = tool_call("recruit_health", {"year": "2027届", "limit": 5})
    ok(t.get("health", {}).get("missing_hire_unit", {}).get("count") == 2
       and t.get("health", {}).get("conflicting_signings", {}).get("count") == 1,
       "助手工具 recruit_health")
    t = tool_call("recruit_progress", {"year": "2027届"})
    ok(t.get("totals", {}).get("signed_count") == 3, "助手工具 recruit_progress")
    t = tool_call("recruit_gaps", {"year": "2027届", "limit": 3})
    ok(t.get("count", 0) >= 1 and len(t.get("items") or []) <= 3,
       "助手工具 recruit_gaps")
    t = tool_call("recruit_roster", {"year": "2027届", "outcome": "拒签"})
    ok(t.get("count") == 1 and (t.get("items") or [{}])[0].get("name") == "癸",
       "助手工具 recruit_roster（拒签筛选）")
    # 运行记录按账号隔离：助手工具只能看到本人的
    _ac = db.connect(_DB)
    db.log_agent_run(_ac, {"session_id": "tool-a", "question": "alpha-only",
                           "answer": "", "operator": "alpha", "mode": "rule"})
    db.log_agent_run(_ac, {"session_id": "tool-b", "question": "beta-only",
                           "answer": "", "operator": "beta", "mode": "rule"})
    _ac.close()
    ctx_a = agent_tools.ToolCtx(db_path=_DB, operator="alpha", role="hr")
    t = json.loads(agent_tools.execute("list_agent_runs", {"limit": 20}, ctx_a))
    ok([r.get("question") for r in t.get("runs") or []] == ["alpha-only"],
       "助手工具 list_agent_runs 只返回本人记录", str(t.get("runs")))

    # ------------------------------------------------------------ 10 登录态审计隔离
    print("\n10 登录态：审计/会话隔离")
    os.environ["TP_AUTH"] = "1"
    try:
        cc = db.connect(_DB)
        auth.ensure_seed_users(cc, "change-me")
        if not db.get_user(cc, "hr_t"):
            db.create_user(cc, "hr_t", "pw12345678", "hr", "HR T")
        if not db.get_user(cc, "admin_t"):
            db.create_user(cc, "admin_t", "pw12345678", "admin", "Admin T")
        if not db.get_user(cc, "viewer_t"):
            db.create_user(cc, "viewer_t", "pw12345678", "viewer", "Viewer T")
        hr_tok = db.create_session(cc, "hr_t", "hr")
        ad_tok = db.create_session(cc, "admin_t", "admin")
        vw_tok = db.create_session(cc, "viewer_t", "viewer")
        db.add_audit(cc, "candidate", str(cid_a), "set_tier", "B", "A", "hr_t", "hr")
        c2 = db.insert_candidate(cc, {"identity_key": "aud:1", "name": "审计样例",
                                      "edu_level": "博士"})
        db.add_audit(cc, "candidate", str(c2), "set_tier", "B", "A", "admin_t", "admin")
        cc.close()
        hr_h = {"X-TP-Token": hr_tok}
        ad_h = {"X-TP-Token": ad_tok}
        vw_h = {"X-TP-Token": vw_tok, "Content-Type": "application/json"}
        st, _ = _asgi(server.app, "GET", "/api/audit", hr_h)
        ok(st == 403, "HR 调 /api/audit 被拒")
        st, _ = _asgi(server.app, "GET", "/api/audit", ad_h)
        ok(st == 200, "管理员调 /api/audit 通过")
        st, _ = _asgi(server.app, "POST", "/api/recruit/plans/batch", vw_h,
                      json.dumps({"year_id": yid, "rows": []}).encode())
        ok(st == 403, "只读账号不能改招聘计划")
        st, _ = _asgi(server.app, "GET", f"/api/recruit/export?year_id={yid}", vw_h)
        ok(st == 403, "只读账号不能导出招聘进度")
        st, _ = _asgi(server.app, "POST",
                      f"/api/applications/{aid_m}/interview-info", vw_h,
                      json.dumps({"sign_status": "已签约"}).encode())
        ok(st == 403, "只读账号不能改签约情况")
        st, body = _asgi(server.app, "GET", f"/api/candidates/{c2}", hr_h)
        j = json.loads(body)
        ok(len(j.get("audit_snippet") or []) == 0, "HR 档案不下发审计片段")
        st, body = _asgi(server.app, "GET", f"/api/candidates/{c2}", ad_h)
        j = json.loads(body)
        ok(st == 200 and len(j.get("audit_snippet") or []) >= 1,
           "管理员档案下发审计片段")
    finally:
        os.environ["TP_AUTH"] = "0"

    conn.close()
    print("\n" + "=" * 70)
    print(f"结果：{PASS} 通过，{FAIL} 失败")
    if FAIL:
        print("失败项请按上面的 [FAIL] 定位。")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
