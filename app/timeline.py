# -*- coding: utf-8 -*-
"""教育/工作时间线检查：断档、在读期间工作。

只做**确定性规则**，不调模型；所有结论都能回到 edu_history / work_history
的起止时间。时间缺失的段落不猜，直接跳过。
"""
from __future__ import annotations

import re
from datetime import date

#: “大于一年”的断档：严格超过 12 个月才报，正常暑期空档不会误报。
GAP_MONTHS = 12

#: 实习/兼职不算“在读期间正式工作”：学生暑期实习、兼职本来就不该提示。
#: 只有岗位/单位能明确看出是实习时才排除；看不出性质的仍按正式工作提示。
_INTERN_RE = re.compile(r"实习|见习|intern(?:ship)?|兼职|part[\s-]?time", re.I)


def _work_nature(work: dict) -> str:
    """工作性质：intern（实习/兼职）/ formal（正式）/ unknown（无法判断）。

    优先用模型抽取的 `kind`（结合简历上下文判断）；老数据没有 kind 时才回退
    到岗位/单位关键词。unknown 仍会提示，但文案会要求 HR 复核。
    """
    kind = str(work.get("kind") or "").strip()
    if kind in ("实习", "兼职"):
        return "intern"
    if kind == "正式":
        return "formal"
    text = f"{work.get('title') or ''} {work.get('org') or ''}"
    if _INTERN_RE.search(text):
        return "intern"
    return "unknown"


def _is_internship(work: dict) -> bool:
    return _work_nature(work) == "intern"



def _parse_ym(value) -> tuple[int, int] | None:
    """'2019.07' / '2019-7' / '2019年7月' / '2019' → (2019, 7)；认不出返回 None。"""
    s = str(value or "").strip()
    if not s:
        return None
    m = re.match(r"((?:19|20)\d{2})(?:[.\-/年]\s*(\d{1,2}))?", s)
    if not m:
        return None
    year = int(m.group(1))
    month = int(m.group(2)) if m.group(2) else 1
    if not (1 <= month <= 12):
        return None
    return year, month


def _to_month(ym: tuple[int, int]) -> int:
    return ym[0] * 12 + (ym[1] - 1)


def _now_month() -> int:
    t = date.today()
    return t.year * 12 + (t.month - 1)


def _fmt(months: int) -> str:
    return f"{months // 12:04d}.{months % 12 + 1:02d}"


def _end_month(value) -> int | None:
    s = str(value or "").strip()
    if s in ("至今", "现在", "目前", "迄今"):
        return _now_month()
    ym = _parse_ym(s)
    return _to_month(ym) if ym else None


def _span(item: dict) -> tuple[int, int] | None:
    start = _parse_ym(item.get("start"))
    end = _end_month(item.get("end"))
    if start is None or end is None or end < _to_month(start):
        return None
    return _to_month(start), end


def _span_text(start: int, end: int) -> str:
    return f"{_fmt(start)}–{_fmt(end)}"


def _overlap(a: tuple[int, int], b: tuple[int, int]) -> bool:
    return a[0] <= b[1] and b[0] <= a[1]


def analyze_timeline(edu_history, work_history=None, *, gap_months: int = GAP_MONTHS) -> list[dict]:
    """返回需要提示 HR 的时间线问题（教育断档 / 在读期间工作）。

    每项：{"kind": "edu_gap"|"work_during_study", "text": 文案, "months": 断档月数}
    """
    issues: list[dict] = []
    edus = []
    for e in (edu_history or []):
        if not isinstance(e, dict):
            continue
        span = _span(e)
        if span:
            edus.append((span, e))
    works = []
    for w in (work_history or []):
        if not isinstance(w, dict):
            continue
        span = _span(w)
        if span:
            works.append((span, w))
    # 实习/兼职不参与“在读期间工作”判断，也不计入断档期间的正式工作。
    formal_works = [(s, w) for s, w in works if not _is_internship(w)]

    # ---- 教育断档 ----
    edus.sort(key=lambda x: x[0][0])
    cursor_end = None
    for span, item in edus:
        if cursor_end is not None and span[0] - cursor_end > gap_months:
            gap = span[0] - cursor_end
            during = [item for wspan, item in formal_works
                      if _overlap((cursor_end, span[0]), wspan)]
            during_txt = ""
            if during:
                names = []
                for w in during[:2]:
                    org = str(w.get("org") or "").strip()
                    if org:
                        names.append(org)
                if names:
                    during_txt = "，期间有工作经历：" + "、".join(names)
            issues.append({
                "kind": "edu_gap",
                "months": gap,
                "text": (f"教育经历断档 {gap // 12} 年 {gap % 12} 个月"
                         f"（{_fmt(cursor_end)}–{_fmt(span[0])}）{during_txt}；"
                         "建议打开档案核对简历原文后复核"),
            })
        cursor_end = span[1] if cursor_end is None else max(cursor_end, span[1])

    # 只有一段教育经历不需要“衔接”判断
    if len(edus) < 2:
        issues = [i for i in issues if i.get("kind") != "edu_gap"]

    # ---- 在读期间工作（只算正式工作；实习/兼职跳过）----
    for wspan, w in formal_works:
        for espan, e in edus:
            if not _overlap(espan, wspan):
                continue
            # 工作完全覆盖在学期内或部分重叠都提示；同一段工作只报一次。
            over_start = max(espan[0], wspan[0])
            over_end = min(espan[1], wspan[1])
            org = str(w.get("org") or "").strip() or "工作"
            title = str(w.get("title") or "").strip()
            label = org + (f"（{title}）" if title else "")
            degree = str(e.get("degree") or "").strip()
            school = str(e.get("school") or "").strip()
            edu_label = " ".join(x for x in (degree, school) if x) or "在读阶段"
            issues.append({
                "kind": "work_during_study",
                "months": over_end - over_start + 1,
                "text": (f"{edu_label}在读期间有工作经历：{label}"
                         f"（{_span_text(wspan[0], wspan[1])}）；"
                         + ("工作性质无法确定，" if _work_nature(w) == "unknown" else "")
                         + "如为非全日制/在职学习可忽略；"
                           "建议打开档案核对简历原文后复核"),
            })
            break
    return issues
