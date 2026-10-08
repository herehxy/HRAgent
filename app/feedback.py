"""决策反馈闭环：把 HR 的每一次定档，变成对系统的反馈。

系统现在的分级权重是**设计时的假设**（`config/tiers.json`：学历 .25 / 年限 .25 /
必需技能 .35 / 加分项 .15），不是本院实测出来的。而 HR 每天在用行动投票：
系统建议 A、HR 定 B —— 这说明系统高估了；建议 B、HR 定 A —— 说明低估了。

**这些配对数据本来就在库里**（`applications` 同一行的 `tier_suggested` 与 `tier_final`），
本模块只是把它读出来、算清楚、说人话。

三条自我约束：

1. **只读**。报告绝不修改 `config/tiers.json`——调权是人的决定。
2. **每句话都要能追溯到数字**。建议文字用规则生成，不让模型自由发挥：
   统计结论写成漂亮但不对的话，比不给结论更糟。
3. **样本不足就说不足**。少于 `MIN_SAMPLE` 条有效反馈时直接说"看不出趋势"，
   不硬编一个趋势出来。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

#: 档位高低（越大越好）。偏差 = 建议档位序 - 实际档位序：
#: 正数=系统给高了（偏高），负数=系统给低了（偏低）
TIER_ORDER = {"A": 4, "B": 3, "C": 2, "D": 1}
TIERS = ["A", "B", "C", "D"]

#: 少于这么多条有效反馈就只报"样本不足"
MIN_SAMPLE = 10

#: 归因用的特征（每条给一个可判定的函数）
FEATURES = [
    ("年限 < 3 年", lambda r: isinstance(r.get("years_exp"), int) and r["years_exp"] < 3),
    ("加分项命中 ≥ 4 项", lambda r: len(_load(r.get("preferred_hit"))) >= 4),
    ("必需技能全中", lambda r: bool(_load(r.get("hits"))) and not _load(r.get("miss"))),
    ("学历高于岗位线", lambda r: _edu_above(r)),
    ("简历里未识别出学历", lambda r: not r.get("edu_level")),
]


def _load(v) -> list:
    """hit/miss/preferred_hit 三种写法都要认（JSON 串 / 列表 / None）。"""
    if isinstance(v, list):
        return v
    if not v:
        return []
    try:
        out = json.loads(v)
        return out if isinstance(out, list) else []
    except (ValueError, TypeError):
        return []


def _edu_above(row) -> bool:
    from . import db
    got = db.EDU_RANK.get(row.get("edu_level") or "", 0)
    need = db.EDU_RANK.get(row.get("required_edu") or "", 0)
    return bool(got and need and got > need)


def _deviation(suggested: str | None, final: str | None) -> int:
    s = TIER_ORDER.get((suggested or "").upper(), 0)
    f = TIER_ORDER.get((final or "").upper(), 0)
    if not s or not f:
        return 0
    return s - f


def decision_report(conn, days: int = 90) -> dict:
    """口径偏差报告。只读，不改任何权重文件。"""
    threshold = (datetime.now() - timedelta(days=int(days))).strftime("%Y-%m-%d %H:%M:%S")
    rows = [dict(r) for r in conn.execute(
        """SELECT a.id, a.candidate_id, a.score, a.tier_suggested, a.tier_final,
                  a.hits, a.miss, a.preferred_hit,
                  j.title AS job_title,
                  ((SELECT jd_json FROM jobs WHERE id = COALESCE(a.job_id, a.suggested_job_id))) AS jd_json,
                  c.name, c.edu_level, c.years_exp, c.major, c.school
           FROM applications a
           JOIN candidates c ON c.id = a.candidate_id
           LEFT JOIN jobs j ON j.id = a.job_id
           WHERE COALESCE(a.tier_final, '') != ''
             AND COALESCE(c.archived_at, '') = ''
             AND COALESCE(a.applied_at, a.created_at) >= ?""",
        (threshold,)).fetchall()]

    # 补上"岗位要求的学历"，学历切片要用
    for r in rows:
        try:
            jd = json.loads(r.get("jd_json") or "{}")
        except (ValueError, TypeError):
            jd = {}
        r["required_edu"] = ((jd.get("must") or {}).get("education_min")) or ""

    total = len(rows)
    base = {"days": int(days), "threshold": threshold, "total": total,
            "min_sample": MIN_SAMPLE}
    if total < MIN_SAMPLE:
        return {**base, "insufficient": True,
                "message": f"样本不足（当前 {total} 份已确认，至少需要 {MIN_SAMPLE} 份），"
                           f"暂不做趋势判断。等你多确认几批档位后，这里会出现偏差分析。",
                "matrix": None, "attribution": None, "suggestions": []}

    matrix = {s: {f: 0 for f in TIERS} for s in TIERS}
    same = 0
    high, low = [], []          # 系统偏高 / 偏低 的样本
    for r in rows:
        s = (r.get("tier_suggested") or "").upper()
        f = (r.get("tier_final") or "").upper()
        if s in matrix and f in matrix[s]:
            matrix[s][f] += 1
        d = _deviation(s, f)
        if d == 0:
            same += 1
        elif d > 0:
            high.append(r)
        else:
            low.append(r)

    def rate(group, feat):
        if not group:
            return 0.0
        return round(100.0 * sum(1 for r in group if feat(r)) / len(group), 1)

    attribution = []
    for label, fn in FEATURES:
        all_rate = rate(rows, fn)
        item = {"feature": label, "all": all_rate,
                "high": rate(high, fn), "low": rate(low, fn)}
        # 只报差异明显的（≥20 个百分点），否则列出十几条无关特征没有意义
        item["gap_high"] = round(item["high"] - all_rate, 1)
        item["gap_low"] = round(item["low"] - all_rate, 1)
        if abs(item["gap_high"]) >= 20 or abs(item["gap_low"]) >= 20:
            attribution.append(item)

    # —— 建议（规则生成，每句都挂着数字）——
    suggestions: list[str] = []
    for it in attribution:
        if it["gap_low"] >= 20:
            suggestions.append(
                f"「{it['feature']}」在【被系统低估】的样本里占 {it['low']}%"
                f"（全体 {it['all']}%），提示这一维度可能给分不足。")
        if it["gap_high"] >= 20:
            suggestions.append(
                f"「{it['feature']}」在【被系统高估】的样本里占 {it['high']}%"
                f"（全体 {it['all']}%），提示这一维度可能给分偏高。")
    if len(high) > len(low) * 2 and len(high) >= 3:
        suggestions.append(f"整体上系统偏严：{len(high)} 份被 HR 上调，仅 {len(low)} 份被下调。")
    elif len(low) > len(high) * 2 and len(low) >= 3:
        suggestions.append(f"整体上系统偏松：{len(low)} 份被 HR 下调，仅 {len(high)} 份被上调。")
    if not suggestions:
        suggestions.append("暂未发现明显成规律的偏差方向，继续积累样本。")

    return {
        **base, "insufficient": False,
        "same": same, "high": len(high), "low": len(low),
        "consistency": round(100.0 * same / total, 1),
        "matrix": matrix,
        "attribution": attribution,
        "suggestions": suggestions,
        "note": "本报告只做统计观察，不修改任何权重。是否调整由 HR 决定"
                "（调整方式：编辑 config/tiers.json 的权重）。",
    }


def export_csv(conn, days: int = 90) -> str:
    """导出明细 CSV，供 HR 自己在 Excel 里做进一步分析。"""
    import csv
    import io

    rep = decision_report(conn, days=days)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["候选人", "岗位", "学历", "年限", "系统建议", "HR 定档", "评分",
                "偏差方向", "投递ID"])
    threshold = rep["threshold"]
    for r in conn.execute(
            """SELECT a.id, a.score, a.tier_suggested, a.tier_final,
                      c.name, c.edu_level, c.years_exp, j.title AS job_title
               FROM applications a
               JOIN candidates c ON c.id = a.candidate_id
               LEFT JOIN jobs j ON j.id = a.job_id
               WHERE COALESCE(a.tier_final, '') != ''
                 AND COALESCE(c.archived_at, '') = ''
                 AND COALESCE(a.applied_at, a.created_at) >= ?
               ORDER BY a.id""", (threshold,)).fetchall():
        d = _deviation(r["tier_suggested"], r["tier_final"])
        w.writerow([r["name"] or "未识别", r["job_title"] or "待指定",
                    r["edu_level"] or "", r["years_exp"] if r["years_exp"] is not None else "",
                    r["tier_suggested"] or "", r["tier_final"] or "",
                    r["score"] if r["score"] is not None else "",
                    "一致" if d == 0 else ("系统偏高" if d > 0 else "系统偏低"),
                    r["id"]])
    return buf.getvalue()
