"""招聘届别 / 年度计划 / 进度统计（v4.0.16）。

这一层是**唯一口径**：页面、导出、智能助手都调这里，避免三处各算一遍。

三条硬规则（与业务方确认）
--------------------------
1. **签约时间决定届别**：`signed_at` 落在届期外的人不计入本届，进“数据体检”。
2. **不猜归属**：只认 `applications.hire_unit`（录用单位）。为空就进“待归属”，
   不用面试单位/岗位部门自动填 —— 猜错一个单位，整张报表就错一行。
3. **一人只计一次**：同一候选人在本届有多条已签约投递时：
     · 全部落在同一“单位×学历” → 计 1 人，标记“重复签约”；
     · 落在不同单位/学历 → **一条都不计**，进“签约冲突”，由 HR 先定哪条为准。
   静默取最新一条会让数字看起来对、归属却错，比少一个人更糟。
"""
from __future__ import annotations

import io
import re
from datetime import date, datetime
from typing import Any

from . import db, stages as stages_mod

EDU_LEVELS = ("博士", "硕士", "本科", "大专")
EDU_ALIASES = {
    "博士": "博士", "博士研究生": "博士",
    "硕士": "硕士", "硕士研究生": "硕士", "研究生": "硕士",
    "本科": "本科", "学士": "本科",
    "大专": "大专", "专科": "大专", "高职": "大专",
}
EDU_ORDER = {name: i for i, name in enumerate(EDU_LEVELS)}
PERIOD_DATE_RE = re.compile(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})")
TITLE_RANGE_RE = re.compile(
    r"(\d{4})[.\-/年](\d{1,2})[.\-/月](\d{1,2})日?\s*[-–—~至到]\s*"
    r"(\d{4})[.\-/年](\d{1,2})[.\-/月](\d{1,2})日?")
HEALTH_LIMIT = 200


# ---------------------------------------------------------------- 基础归一

def normalize_edu(value: Any) -> str | None:
    """学历 → 博士/硕士/本科/大专；认不出返回 None（**不猜**）。"""
    s = re.sub(r"\s+", "", str(value or ""))
    if not s:
        return None
    if s in EDU_ALIASES:
        return EDU_ALIASES[s]
    # “材料学博士”“工学硕士”这类写法：只认包含的唯一一档，含两档则不猜
    hits = [v for k, v in EDU_ALIASES.items() if k and k in s]
    uniq = sorted(set(hits), key=lambda x: EDU_ORDER.get(x, 9))
    return uniq[0] if len(uniq) == 1 else None


def parse_date(value: Any) -> date | None:
    """常见日期写法 → date；只认到“日”，月份/年份不完整返回 None。

    支持 2026-09-01、2026/9/1、2026.9.1、2026年9月1日；
    带时间的只取前 10 位。`2027-06` 这种只有月份的**不算**（报表按天边界，
    猜成 6 月 1 日或 30 日都会改变届别归属）。
    """
    s = str(value or "").strip()
    if not s:
        return None
    m = PERIOD_DATE_RE.search(s)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def date_in_period(value: Any, start: str, end: str) -> bool:
    d = parse_date(value)
    if not d:
        return False
    s, e = parse_date(start), parse_date(end)
    return bool(s and e and s <= d <= e)


def suggest_year(today: date | None = None) -> dict:
    """按“9 月开学”的校招届别推测：9-12 月 → 明年届；1-8 月 → 今年届。"""
    d = today or date.today()
    if d.month >= 9:
        start = f"{d.year}-09-01"
        end = f"{d.year + 1}-08-31"
        label = f"{d.year + 1}届"
    else:
        start = f"{d.year - 1}-09-01"
        end = f"{d.year}-08-31"
        label = f"{d.year}届"
    return {"label": label, "period_start": start, "period_end": end,
            "org_label": "院本部"}


def validate_year(conn, label: str, period_start: str, period_end: str,
                  exclude_id: int | None = None) -> list[str]:
    """届别新建/编辑的校验；返回中文错误列表（空 = 通过）。"""
    errors: list[str] = []
    label = str(label or "").strip()
    if not label:
        errors.append("届别名称不能为空")
    s, e = parse_date(period_start), parse_date(period_end)
    if not s or not e:
        errors.append("起止日期格式应为 YYYY-MM-DD")
    elif s > e:
        errors.append("开始日期不能晚于结束日期")
    if errors:
        return errors
    dup = conn.execute("SELECT id FROM recruit_years WHERE label = ? AND id != ?",
                       (label, int(exclude_id or 0))).fetchone()
    if dup:
        errors.append(f"届别「{label}」已存在")
    # 不允许届期重叠：重叠会让同一个签约时间落进两届，统计双算
    overlap = conn.execute(
        """SELECT label, period_start, period_end FROM recruit_years
           WHERE id != ? AND NOT (period_end < ? OR period_start > ?)
           ORDER BY period_start LIMIT 1""",
        (int(exclude_id or 0), str(s), str(e))).fetchone()
    if overlap:
        errors.append(f"与届别「{overlap['label']}」（{overlap['period_start']} 至 "
                      f"{overlap['period_end']}）届期重叠")
    return errors


def create_year(conn, label: str, period_start: str, period_end: str,
                org_label: str = "院本部", note: str = "",
                operator: str = "HR", role: str = "hr") -> dict:
    errors = validate_year(conn, label, period_start, period_end)
    if errors:
        return {"ok": False, "errors": errors}
    # 统一存 ISO（YYYY-MM-DD）：年/月/日各种写法都先归一再落库，
    # 否则重叠判断与默认届别（字符串比较）会被 "2026/9/1" 之类的写法骗过。
    _s, _e = parse_date(period_start), parse_date(period_end)
    yid = db.create_recruit_year(conn, label, _s.isoformat(), _e.isoformat(),
                                 org_label=org_label, note=note,
                                 operator=operator, role=role)
    return {"ok": True, "id": yid, "year": db.get_recruit_year(conn, year_id=yid)}


def update_year(conn, year_id: int, fields: dict,
                operator: str = "HR", role: str = "hr") -> dict:
    cur = db.get_recruit_year(conn, year_id=year_id)
    if not cur:
        return {"ok": False, "errors": ["未找到该届别"]}
    merged = {**cur, **(fields or {})}
    errors = validate_year(conn, merged.get("label"), merged.get("period_start"),
                           merged.get("period_end"), exclude_id=year_id)
    if errors:
        return {"ok": False, "errors": errors}
    patch = dict(fields or {})
    for k in ("period_start", "period_end"):
        if k in patch:
            _d = parse_date(patch[k])
            if _d:
                patch[k] = _d.isoformat()
    out = db.update_recruit_year(conn, year_id, patch, operator=operator, role=role)
    return {"ok": True, "year": out}


# ---------------------------------------------------------------- 元信息

def build_meta(conn, can_edit: bool = False, can_export: bool = False) -> dict:
    years = db.list_recruit_years(conn)
    default = db.get_default_recruit_year(conn)
    return {
        "years": years,
        "default_year_id": default["id"] if default else None,
        "suggested": suggest_year(),
        "edu_levels": list(EDU_LEVELS),
        "units": db.list_recruit_plan_units(conn),
        "can_edit": bool(can_edit),
        "can_export": bool(can_export),
    }


# ---------------------------------------------------------------- 进度统计

def _plan_math(row: dict) -> dict:
    approved = int(row.get("plan_approved") or 0)
    extra = int(row.get("plan_extra") or 0)
    carry = int(row.get("plan_carryover") or 0)
    cancel = int(row.get("plan_cancel") or 0)
    should = approved + extra + carry - cancel
    return {"plan_approved": approved, "plan_extra": extra,
            "plan_carryover": carry, "plan_cancel": cancel,
            "should_hire": should}


def _rate(signed: int, should: int) -> float | None:
    return (signed / should) if should else None


def validate_plan_rows(rows: list[dict]) -> list[str]:
    """批量保存前的校验：非负整数、应招总数不得为负。"""
    errors: list[str] = []
    for item in rows or []:
        try:
            pid = int(item.get("id"))
        except (TypeError, ValueError):
            errors.append("计划行缺少有效 id")
            continue
        vals = {}
        for k in ("plan_approved", "plan_extra", "plan_carryover", "plan_cancel"):
            try:
                v = int(item.get(k) or 0)
            except (TypeError, ValueError):
                errors.append(f"计划行 #{pid} 的 {k} 不是整数")
                v = 0
            if v < 0:
                errors.append(f"计划行 #{pid} 的 {k} 不能为负数")
            vals[k] = v
        if vals["plan_approved"] + vals["plan_extra"] + vals["plan_carryover"] - vals["plan_cancel"] < 0:
            errors.append(f"计划行 #{pid} 的应招总数为负（审批+追加+转上年度 < 应取消）")
    return errors


def _totals_from_rows(rows: list[dict]) -> dict:
    out = {"plan_approved": 0, "plan_extra": 0, "plan_carryover": 0,
           "plan_cancel": 0, "should_hire": 0, "signed_count": 0}
    for r in rows:
        for k in ("plan_approved", "plan_extra", "plan_carryover", "plan_cancel",
                  "should_hire", "signed_count"):
            out[k] += int(r.get(k) or 0)
    out["gap"] = out["should_hire"] - out["signed_count"]
    out["rate"] = _rate(out["signed_count"], out["should_hire"])
    return out


def _by_education(rows: list[dict]) -> list[dict]:
    out = []
    for edu in EDU_LEVELS:
        sub = [r for r in rows if r.get("edu_level") == edu]
        t = _totals_from_rows(sub)
        out.append({"edu_level": edu, **t})
    return out


def _health_bucket() -> dict:
    return {"count": 0, "items": []}


def _health_add(bucket: dict, item: dict) -> None:
    bucket["count"] += 1
    if len(bucket["items"]) < HEALTH_LIMIT:
        bucket["items"].append(item)


def _app_brief(app: dict, reason: str, **extra) -> dict:
    return {
        "candidate_id": app.get("candidate_id"),
        "application_id": app.get("application_id"),
        "name": app.get("name") or "",
        "unit": app.get("hire_unit") or app.get("interview_unit") or "",
        "edu_level": normalize_edu(app.get("edu_level")) or app.get("edu_level") or "",
        "signed_at": app.get("signed_at") or "",
        "stage": app.get("stage") or "",
        "outcome": app.get("outcome") or "",
        "sign_status": db.effective_sign_status(app),
        "reason": reason,
        **extra,
    }


def _suggest_unit(app: dict, unit_keys: dict[str, str]) -> str:
    """从面试单位里找唯一匹配的计划单位，仅作为“建议值”，不自动写库。"""
    raw = str(app.get("interview_unit") or "")
    if not raw:
        return ""
    hits = []
    for part in re.split(r"[、,，;；/]+", raw):
        key = db.recruit_unit_key(part)
        if key in unit_keys and unit_keys[key] not in hits:
            hits.append(unit_keys[key])
    return hits[0] if len(hits) == 1 else ""


def build_progress(conn, year_id: int | None = None, unit: str | None = None,
                   edu: str | None = None) -> dict:
    """组装进度表。`unit`/`edu` 只筛展示行；数据体检始终看全届。"""
    years = db.list_recruit_years(conn)
    year = (db.get_recruit_year(conn, year_id=year_id) if year_id
            else db.get_default_recruit_year(conn))
    if not year:
        return {"needs_setup": True, "needs_year": True, "needs_plan": True,
                "years": years, "year": None, "rows": [], "units": [],
                "totals": _totals_from_rows([]), "by_education": _by_education([]),
                "health": {"count": 0}, "summary": "", "filters": {}}
    plans = db.list_recruit_plans(conn, year["id"])
    apps = db.list_recruit_applications(conn)
    plan_by_key = {(p["unit_key"], p["edu_level"]): p for p in plans}
    unit_names = {}
    for p in plans:
        unit_names.setdefault(p["unit_key"], p["unit_name"])

    health: dict[str, dict] = {
        "missing_signed_at": _health_bucket(),
        "invalid_signed_at": _health_bucket(),
        "signed_without_conclusion": _health_bucket(),
        "out_of_period": _health_bucket(),
        "stage_mismatch": _health_bucket(),
        "missing_hire_unit": _health_bucket(),
        "unknown_edu": _health_bucket(),
        "unit_not_in_plan": _health_bucket(),
        "conflicting_signings": _health_bucket(),
        "duplicate_signed": _health_bucket(),
        "signed_without_plan": _health_bucket(),
        "many_signees": _health_bucket(),
        "duplicate_names": _health_bucket(),
    }

    # 候选级分组：同一人的所有有效期内「已签约」投递先收在一起。
    # ⚠️ 必须在学历/单位/计划归属过滤**之前**分组：这些过滤只决定
    # 「算到哪一行」，不决定「是不是冲突」。先过滤会把“A单位有效 + B单位无效”
    # 的跨单位冲突当成一条正常签约（漏报冲突、还多计 1 人）。
    groups: dict[int, list[dict]] = {}
    for app in apps:
        sign = db.effective_sign_status(app)
        if sign != "已签约":
            continue
        signed_raw = str(app.get("signed_at") or "").strip()
        signed_date = parse_date(signed_raw)
        if not signed_raw:
            _health_add(health["missing_signed_at"],
                        _app_brief(app, "已签约但没有签约时间，未计入本届"))
            continue
        if not signed_date:
            _health_add(health["invalid_signed_at"],
                        _app_brief(app, "签约时间格式无法识别，未计入本届"))
            continue
        if not date_in_period(signed_raw, year["period_start"], year["period_end"]):
            _health_add(health["out_of_period"],
                        _app_brief(app, f"签约时间不在本届 "
                                        f"{year['period_start']} 至 {year['period_end']}"))
            continue
        # 新模型下 explicitly 设了已签约，但面试结论不是拟录用 → 提醒（仍计入）
        if str(app.get("sign_status") or "").strip()                 and str(app.get("outcome") or "").strip() != "拟录用":
            _health_add(health["signed_without_conclusion"],
                        _app_brief(app, "已签约但面试结论不是「拟录用」，请核对（仍计入）"))
        if str(app.get("stage") or "").strip() != "已发放offer":
            # 签约是事实，阶段不一致只提示、不拦计入（历史数据可能停在已结束）
            _health_add(health["stage_mismatch"],
                        _app_brief(app, "已签约但阶段不是「已发放offer」，"
                                        "请核对（仍计入）"))
        edu_norm = normalize_edu(app.get("edu_level"))
        hire_unit = str(app.get("hire_unit") or "").strip()
        unit_key = db.recruit_unit_key(hire_unit) if hire_unit else ""
        # key 始终存在（缺失单位/学历用空串占位），这样“一条有效 + 一条缺单位”
        # 也会被识别为归属不一致，而不是静默只算有效那条。
        groups.setdefault(int(app["candidate_id"]), []).append(
            {**app, "_edu_norm": edu_norm, "_unit_key": unit_key,
             "_key": (unit_key, edu_norm or "")})

    counted: dict[tuple, list[dict]] = {}
    for cid, group in groups.items():
        keys = {a["_key"] for a in group}
        if len(group) > 1:
            if len(keys) == 1:
                group = sorted(group, key=lambda a: (str(a.get("signed_at") or ""),
                                                     int(a.get("application_id") or 0)))
                _health_add(health["duplicate_signed"],
                            _app_brief(group[0], f"同单位同学历有 {len(group)} 条已签约投递，"
                                                 f"按 1 人计"))
            else:
                units = "、".join(sorted({f"{a.get('hire_unit') or '（空）'}/"
                                          f"{a.get('_edu_norm') or '（空）'}"
                                          for a in group}))
                _health_add(health["conflicting_signings"],
                            _app_brief(group[0], f"同一人在本届有 {len(group)} 条已签约投递，"
                                                 f"归属不一致（{units}）；全部暂不计入，"
                                                 f"请先确认哪条为准"))
                continue
        picked = sorted(group, key=lambda a: (str(a.get("signed_at") or ""),
                                              int(a.get("application_id") or 0)))[0]
        edu_norm = picked.get("_edu_norm")
        hire_unit = str(picked.get("hire_unit") or "").strip()
        if not edu_norm:
            _health_add(health["unknown_edu"],
                        _app_brief(picked, f"学历「{picked.get('edu_level') or '空'}」"
                                           f"不在四档口径内，未计入"))
            continue
        if not hire_unit:
            _health_add(health["missing_hire_unit"],
                        _app_brief(picked, "缺少录用单位，未计入",
                                   suggested_unit=_suggest_unit(picked, unit_names)))
            continue
        key = (picked["_unit_key"], edu_norm)
        if key not in plan_by_key:
            _health_add(health["unit_not_in_plan"],
                        _app_brief(picked, f"「{hire_unit} / {edu_norm}」不在本届计划中，未计入"))
            continue
        picked["_plan_id"] = plan_by_key[key]["id"]
        counted.setdefault(key, []).append(picked)

    rows: list[dict] = []
    for p in plans:
        key = (p["unit_key"], p["edu_level"])
        signees = sorted(counted.get(key, []),
                         key=lambda a: (str(a.get("signed_at") or ""),
                                        str(a.get("name") or ""),
                                        int(a.get("application_id") or 0)))
        math = _plan_math(p)
        if math["should_hire"] == 0 and signees:
            _health_add(health["signed_without_plan"],
                        _app_brief(signees[0], f"「{p['unit_name']} / {p['edu_level']}」"
                                               f"应招总数为 0，但有 {len(signees)} 人签约"))
        names = [str(a.get("name") or "") for a in signees if a.get("name")]
        dup_names = sorted({n for n in names if names.count(n) > 1})
        if dup_names:
            _health_add(health["duplicate_names"],
                        {"candidate_id": None, "application_id": None,
                         "name": "、".join(dup_names), "unit": p["unit_name"],
                         "edu_level": p["edu_level"], "signed_at": "",
                         "reason": "同一计划行有重名签约人，请核对是否为同一人"})
        if len(signees) > 10:
            _health_add(health["many_signees"],
                        {"candidate_id": None, "application_id": None,
                         "name": f"{len(signees)} 人", "unit": p["unit_name"],
                         "edu_level": p["edu_level"], "signed_at": "",
                         "reason": "签约人超过原模板 10 列，导出会自动扩展列数"})
        row = {
            "plan_id": p["id"],
            "unit_name": p["unit_name"],
            "unit_key": p["unit_key"],
            "sort_order": int(p.get("sort_order") or 0),
            "edu_level": p["edu_level"],
            "note": p.get("note") or "",
            **math,
            "signed_count": len(signees),
            "gap": math["should_hire"] - len(signees),
            "rate": _rate(len(signees), math["should_hire"]),
            "signees": [{
                "candidate_id": a.get("candidate_id"),
                "application_id": a.get("application_id"),
                "name": a.get("name") or "",
                "school": a.get("school") or "",
                "edu_level": a.get("_edu_norm"),
                "signed_at": a.get("signed_at") or "",
                "onboard_deadline": a.get("onboard_deadline") or "",
                "onboard_status": a.get("onboard_status") or "",
                "stage": a.get("stage") or "",
            } for a in signees],
        }
        if unit and db.recruit_unit_key(unit) != p["unit_key"]:
            continue
        if edu and edu != p["edu_level"]:
            continue
        rows.append(row)

    totals = _totals_from_rows(rows)
    by_edu = _by_education(rows)
    # 岗位招聘数量是另一个口径：只做差异提示，不参与计划数学
    try:
        _hc = conn.execute("SELECT COALESCE(SUM(headcount),0) AS n FROM jobs").fetchone()["n"]
    except Exception:                                            # noqa: BLE001
        _hc = 0
    health["job_headcount"] = {"count": int(_hc or 0),
                               "plan_total": totals["should_hire"],
                               "diff": int(_hc or 0) - totals["should_hire"]}
    health["count"] = sum(v.get("count", 0) for k, v in health.items()
                          if isinstance(v, dict) and k not in ("job_headcount",))
    filters = {}
    if unit:
        filters["unit"] = unit
    if edu:
        filters["edu"] = edu
    summary = summary_sentence(year, totals, by_edu, filtered=bool(filters))
    return {
        "needs_setup": not plans,
        "needs_year": False,
        "needs_plan": not plans,
        "years": years,
        "year": year,
        "rows": rows,
        "units": sorted({p["unit_name"] for p in plans},
                        key=lambda n: (min((int(p.get("sort_order") or 0) for p in plans
                                            if p["unit_name"] == n), default=0), n)),
        "totals": totals,
        "by_education": by_edu,
        "health": health,
        "summary": summary,
        "filters": filters,
    }


def summary_sentence(year: dict, totals: dict, by_edu: list[dict],
                     filtered: bool = False) -> str:
    """与源表 A73 同口径的总结句。"""
    edu = {x["edu_level"]: x for x in by_edu or []}
    plan_total = (int(totals.get("plan_approved") or 0)
                  + int(totals.get("plan_extra") or 0)
                  + int(totals.get("plan_carryover") or 0))
    rate = totals.get("rate")
    rate_s = f"{rate * 100:.1f}%" if rate is not None else "—"
    def _edu_line(field: str) -> str:
        return "、".join(f"{name}{int(edu.get(name, {}).get(field) or 0)}人"
                        for name in EDU_LEVELS)
    s = (f"总结：{year.get('label') or ''}（{year.get('period_start')} 至 "
         f"{year.get('period_end')}）{year.get('org_label') or '院本部'}计划招聘"
         f"{plan_total}人，取消招聘计划{int(totals.get('plan_cancel') or 0)}人，"
         f"应招聘{int(totals.get('should_hire') or 0)}人，其中"
         f"{_edu_line('should_hire')}，实际招聘签约{int(totals.get('signed_count') or 0)}人，"
         f"其中{_edu_line('signed_count')}，招聘完成率{rate_s}。")
    if filtered:
        s += "（当前为筛选视图，合计不等于全届）"
    return s


# ---------------------------------------------------------------- 面试名单

def _roster_effective_date(app: dict) -> date | None:
    return (parse_date(app.get("interview_date"))
            or parse_date(app.get("interviewed_at"))
            or parse_date(app.get("signed_at")))


def _mask_phone(phone: str) -> str:
    p = str(phone or "").strip()
    if len(p) >= 7:
        return f"{p[:3]}****{p[-4:]}"
    return "—" if not p else p


def build_roster(conn, year_id: int | None = None, *, date_from: str | None = None,
                 date_to: str | None = None, unit: str | None = None,
                 edu: str | None = None, outcome: str | None = None,
                 keyword: str | None = None, include_phone: bool = False) -> dict:
    """面试总名单。行集 = 本届内**面试日期或签约日期**落在届期的投递。"""
    year = (db.get_recruit_year(conn, year_id=year_id) if year_id
            else db.get_default_recruit_year(conn))
    if not year:
        return {"needs_setup": True, "year": None, "items": [], "count": 0,
                "years": db.list_recruit_years(conn), "health": {"count": 0}}
    apps = db.list_recruit_applications(conn)
    from .crypto import decrypt
    df = parse_date(date_from) if date_from else None
    dt = parse_date(date_to) if date_to else None
    items: list[dict] = []
    missing_date = 0
    for app in apps:
        stage = str(app.get("stage") or "").strip()
        sign = db.effective_sign_status(app)
        interview_d = parse_date(app.get("interview_date"))
        interviewed_d = parse_date(app.get("interviewed_at"))
        signed_d = parse_date(app.get("signed_at"))
        # 行集：面试/签约任一日期在本届期内。只邀约未面试的也算（有面试日期）。
        in_period = any(d and date_in_period(d.isoformat(), year["period_start"],
                                             year["period_end"])
                        for d in (interview_d, interviewed_d, signed_d))
        eff = interview_d or interviewed_d or signed_d
        # 已签约/拒签/毁约的人即使缺面试/签约日期，也要进名单（用户口径：
        # 拒签和毁约只在面试总名单里看）。用投递日期兜底，避免他们凭空消失。
        if not in_period and sign in ("已签约", "拒签", "毁约"):
            applied_d = parse_date(app.get("applied_at"))
            if applied_d and date_in_period(applied_d.isoformat(), year["period_start"],
                                            year["period_end"]):
                in_period = True
                eff = eff or applied_d
        if not in_period:
            continue
        if df and (not eff or eff < df):
            continue
        if dt and (not eff or eff > dt):
            continue
        if not eff:
            missing_date += 1
        edu_norm = normalize_edu(app.get("edu_level"))
        if edu and edu_norm != edu:
            continue
        unit_show = str(app.get("interview_unit") or app.get("hire_unit")
                        or app.get("job_dept") or "").strip()
        if unit:
            key = db.recruit_unit_key(unit)
            _unit_keys = {db.recruit_unit_key(x) for x in
                          re.split(r"[、,，;；/]+", unit_show)
                          if db.recruit_unit_key(x)}
            _unit_keys.add(db.recruit_unit_key(app.get("hire_unit") or ""))
            if key not in _unit_keys:
                continue
        sign = db.effective_sign_status(app)
        verdict_raw = stages_mod.verdict(stage, app.get("outcome"), sign)
        verdict = {"拟录用": "是", "不录用": "否"}.get(verdict_raw, "—")
        if outcome:
            if outcome in db.SIGN_STATUSES:
                if sign != outcome:
                    continue
            elif outcome == "未判":
                if verdict_raw:
                    continue
            elif verdict_raw != outcome:
                continue
        kw = str(keyword or "").strip()
        if kw and kw not in (str(app.get("name") or "") + str(app.get("school") or "")
                             + unit_show + str(app.get("job_title") or "")):
            continue
        phone = ""
        if include_phone:
            try:
                phone = decrypt(app.get("phone_enc")) or ""
            except Exception:                                    # noqa: BLE001
                phone = ""
        else:
            try:
                phone = _mask_phone(decrypt(app.get("phone_enc")) or "")
            except Exception:                                    # noqa: BLE001
                phone = "—"
        status = sign or "未签约"
        offer_sent = "已发" if (app.get("offer_sent_at") or stage == "已发放offer") else "未发"
        items.append({
            "application_id": app.get("application_id"),
            "candidate_id": app.get("candidate_id"),
            "interview_date": str(app.get("interview_date") or "")[:16],
            "unit": unit_show,
            "name": app.get("name") or "",
            "school": app.get("school") or "",
            "edu_level": edu_norm or app.get("edu_level") or "",
            "interview_mode": app.get("interview_mode") or "",
            "phone": phone,
            "verdict": verdict,
            "verdict_raw": verdict_raw,
            "offer_sent": offer_sent,
            "offer_sent_at": str(app.get("offer_sent_at") or "")[:16],
            "sign_status": status,
            "signed_at": str(app.get("signed_at") or "")[:10],
            "grad_date": app.get("grad_date") or "",
            "onboard_status": app.get("onboard_status") or "",
            "note": app.get("app_note") or app.get("candidate_note") or "",
            "_eff": eff.isoformat() if eff else "",
        })
    items.sort(key=lambda x: (x.get("_eff") or "", x.get("name") or ""), reverse=True)
    for i, it in enumerate(items, 1):
        it["seq"] = i
        it.pop("_eff", None)
    health = {"count": missing_date,
              "items": ([{"reason": f"有 {missing_date} 条面试/签约记录日期缺失，"
                                    f"未进入名单"}] if missing_date else [])}
    return {"needs_setup": False, "year": year, "items": items, "count": len(items),
            "years": db.list_recruit_years(conn), "health": health}


# ---------------------------------------------------------------- Excel 导入解析

def _norm_header(value: Any) -> str:
    return re.sub(r"\s+", "", str(value or ""))


def _to_int(value: Any) -> int | None:
    if value is None or value == "":
        return 0
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    s = str(value).strip().replace(",", "")
    if not s:
        return 0
    if not re.fullmatch(r"-?\d+", s):
        return None
    return int(s)


def parse_plan_xlsx(data: bytes) -> dict:
    """解析源表 Sheet1 的计划列（D-G），忽略公式列/签约人列/汇总块。"""
    from openpyxl import load_workbook
    try:
        wb = load_workbook(io.BytesIO(data), data_only=True, read_only=False)
    except Exception as exc:                                     # noqa: BLE001
        return {"ok": False, "errors": [f"无法读取 Excel：{exc}"],
                "rows": [], "warnings": []}
    ws = None
    for cand in wb.worksheets:
        if "招聘进度" in str(cand.title):
            ws = cand
            break
    ws = ws or wb.worksheets[0]
    if ws.max_row > 1000:
        return {"ok": False, "errors": ["工作表行数超过 1000，疑似不是招聘进度表"],
                "rows": [], "warnings": []}
    header_row = None
    col: dict[str, int] = {}
    for r in range(1, min(ws.max_row, 15) + 1):
        vals = [_norm_header(ws.cell(row=r, column=c).value)
                for c in range(1, min(ws.max_column, 40) + 1)]
        if not any("单位" in v for v in vals) or not any("学历" in v for v in vals):
            continue
        for i, h in enumerate(vals, 1):
            if not h:
                continue
            if h.startswith("单位") and "unit" not in col:
                col["unit"] = i
            elif h.startswith("学历") and "edu" not in col:
                col["edu"] = i
            elif "上会审批" in h and "approved" not in col:
                col["approved"] = i
            elif "追加" in h and "extra" not in col:
                col["extra"] = i
            elif "转上年度" in h and "carry" not in col:
                col["carry"] = i
            elif "应取消" in h and "cancel" not in col:
                col["cancel"] = i
        if "unit" in col and "edu" in col and any(
                k in col for k in ("approved", "extra", "carry", "cancel")):
            header_row = r
            break
    if not header_row:
        return {"ok": False,
                "errors": ["未找到表头行：需要包含「单位」「学历」以及计划数列"],
                "rows": [], "warnings": []}
    title = ""
    title_period = None
    for r in range(1, header_row):
        t = str(ws.cell(row=r, column=1).value or "")
        if "招聘进度" in t:
            title = t.strip()
            m = TITLE_RANGE_RE.search(t)
            if m:
                title_period = {
                    "period_start": f"{int(m.group(1)):04d}-{int(m.group(2)):02d}-{int(m.group(3)):02d}",
                    "period_end": f"{int(m.group(4)):04d}-{int(m.group(5)):02d}-{int(m.group(6)):02d}",
                }
            break
    rows: list[dict] = []
    warnings: list[str] = []
    errors: list[str] = []
    seen: dict[tuple, int] = {}
    unit_order: dict[str, int] = {}
    last_unit = ""
    for r in range(header_row + 1, ws.max_row + 1):
        a_s = str(ws.cell(row=r, column=1).value or "").strip()
        if a_s in ("合计", "总计") or "学历分布" in a_s or "总结" in a_s:
            break
        raw_unit = ws.cell(row=r, column=col["unit"]).value
        raw_edu = ws.cell(row=r, column=col["edu"]).value
        unit_s = str(raw_unit or "").strip()
        edu_s = str(raw_edu or "").strip()
        if (unit_s in ("合计", "总计") or "学历分布" in unit_s
                or "总结" in unit_s or "学历分布" in edu_s or "总结" in edu_s):
            break
        if unit_s:
            last_unit = unit_s
        use_unit = unit_s or last_unit
        edu = normalize_edu(edu_s)
        if not use_unit or not edu:
            continue
        key = (db.recruit_unit_key(use_unit), edu)
        if key in seen:
            warnings.append(f"第 {r} 行与第 {seen[key]} 行重复（{use_unit} / {edu}），已跳过")
            continue
        nums = {}
        bad = False
        for name in ("approved", "extra", "carry", "cancel"):
            idx = col.get(name)
            val = _to_int(ws.cell(row=r, column=idx).value) if idx else 0
            if val is None or val < 0:
                errors.append(f"第 {r} 行 {use_unit} / {edu} 的 {name} 不是非负整数，已跳过")
                bad = True
                break
            nums[name] = val
        if bad:
            continue
        if nums["approved"] + nums["extra"] + nums["carry"] - nums["cancel"] < 0:
            errors.append(f"第 {r} 行 {use_unit} / {edu} 应招总数为负，已跳过")
            continue
        unit_order.setdefault(key[0], len(unit_order))
        seen[key] = r
        rows.append({
            "unit_name": use_unit,
            "unit_key": key[0],
            "edu_level": edu,
            "plan_approved": nums["approved"],
            "plan_extra": nums["extra"],
            "plan_carryover": nums["carry"],
            "plan_cancel": nums["cancel"],
            "sort_order": unit_order[key[0]] * 10 + EDU_ORDER.get(edu, 9),
            "source_row": r,
        })
    return {"ok": bool(rows), "errors": errors, "warnings": warnings,
            "rows": rows, "title": title, "title_period": title_period,
            "sheet_name": ws.title, "header_row": header_row}


def apply_plan_import(conn, parsed: dict, year_id: int, mode: str = "merge",
                      operator: str = "HR", role: str = "hr") -> dict:
    """把解析结果写入计划表。merge=只补缺行，overwrite=覆盖已有行。"""
    mode = "overwrite" if str(mode).lower() == "overwrite" else "merge"
    inserted = updated = skipped = 0
    for row in parsed.get("rows") or []:
        out = db.save_recruit_plan(
            conn, year_id=year_id, unit_name=row["unit_name"],
            edu_level=row["edu_level"], plan_approved=row["plan_approved"],
            plan_extra=row["plan_extra"], plan_carryover=row["plan_carryover"],
            plan_cancel=row["plan_cancel"], sort_order=row.get("sort_order") or 0,
            mode=mode)
        if out["action"] == "inserted":
            inserted += 1
        elif out["action"] == "updated":
            updated += 1
        else:
            skipped += 1
    if inserted or updated:
        db.add_audit(conn, "recruit_plan", str(year_id), "import", "",
                     f"{mode}：新增 {inserted} 行、覆盖 {updated} 行、跳过 {skipped} 行",
                     operator, role)
    else:
        conn.commit()
    return {"inserted": inserted, "updated": updated, "skipped": skipped,
            "total": inserted + updated + skipped, "mode": mode}
