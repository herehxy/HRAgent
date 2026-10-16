"""应届 / 往届 / 工作时长：**招聘对象身份**的统一口径。

为什么需要它：校招场景下"这个人能不能投"取决于身份，而不是一个干巴巴的年限数字。

- 有正式工作经历 → 看**工作年限**
- 没有正式工作经历 → 看**毕业时间**：
  - 当年或往后毕业 → **应届**
  - 前 1–2 年毕业 → **往届未就业**（用户提的场景：去年毕业但没参加工作）
  - 更早 → 无工作经历（需人工确认）

校招节奏上也对得上：秋招（前一年 10–12 月）与春招（当年 3–5 月）招的主力，
正是**次年 6 月或年初毕业**的人；所以"当年及以后毕业"判应届是合理的。
"""
from __future__ import annotations

import re
from datetime import datetime

#: 抓 "2026年6月" / "2026.06" / "2026-6" 这类年月
_YM_RE = re.compile(r"(20\d{2})\s*[年.\-/]\s*(\d{1,2})?")

#: 应届窗口：毕业年份落在当前年或之后 → 应届
#: 往届未就业窗口：毕业后 1–2 年内且无工作经历
FRESH_WINDOW = 0
IDLE_WINDOW = 2

#: 「在读」判定的词表（v1.29.4，业务方 2026-10-16）：
#:   学位词 = "这一段是不是教育经历"的正面证据；工作词 = 反证据。
#: ⚠️ 只列**歧义小**的词：「研究所」不列（"中科院某研究所 博士研究生"会被误伤）、
#: 「工作」不列（"无工作经历"这类句子会被误伤）。
_DEG_WORDS = ("本科", "硕士", "博士", "研究生", "学士", "学位", "在读")
_WORK_WORDS = ("公司", "任职", "在职", "就职", "讲师", "工程师", "岗位")


def find_in_school(text: str | None) -> bool:
    """从简历原文判断是否**仍在读**（未毕业的学生身份）。

    业务方场景（2026-10-16）：硕士「2024.9-至今」、本科 2024.6 毕业 ——
    按"取最晚毕业时间"会被判成「往届未就业」，实际是在读应届生。

    规则（**不走模型** —— 身份判定要确定性），按"时间段 + 标签行"配对：
      · 时间段行：含「至今 / 在读」，或结束年份 > 当前年（"2024.9-2027.6"）；
      · 标签行：时间段行自身带学位/工作词就用自身，否则看**后面 1-2 行**
        （简历常见排版：时间一行、院校/单位一行），再兜底看前一行；
      · 标签行是工作段（公司/工程师/任职…）→ 不算在读；是学位段 → 在读。
    ⚠️ 独立成行的「2024.9-至今」靠这个配对才能命中（实测的真实简历排版）。
    """
    if not text:
        return False
    text = str(text)
    cur_y = datetime.now().year
    lines = [ln.strip() for ln in text.splitlines()]
    idx = [i for i, ln in enumerate(lines) if ln]
    if len(idx) < 2:
        # 单行长文本兜底：以"至今/在读"或未来年份为中心 ±40 字符找证据
        for m in re.finditer(r"至今|在读", text):
            seg = text[max(0, m.start() - 40): m.end() + 40]
            if any(w in seg for w in _DEG_WORDS) and not any(w in seg for w in _WORK_WORDS):
                return True
        for m in _YM_RE.finditer(text):
            if int(m.group(1)) > cur_y:
                seg = text[max(0, m.start() - 40): m.end() + 40]
                if any(w in seg for w in _DEG_WORDS) and not any(w in seg for w in _WORK_WORDS):
                    return True
        return False

    def _has_any(s: str, words) -> bool:
        return any(w in s for w in words)

    for k, i in enumerate(idx):
        ln = lines[i]
        ongoing = ("至今" in ln or "在读" in ln)
        future = any(int(m.group(1)) > cur_y for m in _YM_RE.finditer(ln))
        if not (ongoing or future):
            continue
        # 找这一段属于"教育"还是"工作"的标签行
        label = None
        if _has_any(ln, _DEG_WORDS) or _has_any(ln, _WORK_WORDS):
            label = ln
        else:
            # 简历排版常把一段拆成几行：时间 → 学校 → 专业 → 学位（实测 4 行），
            # 所以取**第一条带学位/工作词的行**，窗口给到后 5 行；
            # "第一条"是关键 —— 工作段（公司/工程师）会在自己那行先被认出来。
            for j in idx[k + 1: k + 6]:
                cand = lines[j]
                if _has_any(cand, _DEG_WORDS) or _has_any(cand, _WORK_WORDS):
                    label = cand
                    break
            if label is None and k > 0:          # 兜底：前一行
                prev = lines[idx[k - 1]]
                if _has_any(prev, _DEG_WORDS) or _has_any(prev, _WORK_WORDS):
                    label = prev
        if label is None:
            continue
        if _has_any(label, _WORK_WORDS):
            continue                              # 这是工作段
        if _has_any(label, _DEG_WORDS):
            return True                           # 这是教育段且未结束
    return False


def find_grad_date(text: str | None) -> str | None:
    """从简历原文里找毕业时间，返回 `YYYY-MM`（只有年份时返回 `YYYY`）。

    只在**"毕业"附近**的年月上取值：简历里满是教育经历的起止时间
    （2018.09-2021.06），不限定上下文就会把入学时间当成毕业时间。
    """
    if not text:
        return None
    best: tuple[int, str] | None = None
    for m in _YM_RE.finditer(text):
        seg = text[max(0, m.start() - 24): m.end() + 24]
        if "毕业" not in seg and "学位" not in seg:
            continue
        year = int(m.group(1))
        month = int(m.group(2)) if m.group(2) else 0
        # 同一份简历里可能有多个"毕业"（本科/硕士各一次）→ 取最晚的那个
        val = f"{year}-{month:02d}" if month else str(year)
        if best is None or year > best[0]:
            best = (year, val)
    return best[1] if best else None


def fmt_grad(grad_date: str | None) -> str:
    if not grad_date:
        return ""
    g = str(grad_date)
    if "-" in g:
        y, m = g.split("-")[:2]
        try:
            return f"{y}年{int(m)}月"
        except ValueError:
            return f"{y}年"
    if "年" in g:            # 已经是「2026年6月」这类写法，直接用，别再补一个"年"
        return g
    return f"{g}年"


def exp_label(years, has_work: bool, grad_date: str | None,
              in_school: bool = False) -> dict:
    """给出招聘对象身份与展示文案。

    返回 `{kind, label, note, in_school}`：
      kind：work / fresh / past_idle / no_exp / unknown —— 便于前端配色与筛选
      in_school：仍在读（未毕业）—— 前端据此决定"在读"标签是否常显
    """
    cur_y = datetime.now().year
    gy = int(grad_date[:4]) if grad_date else None
    gtxt = fmt_grad(grad_date)
    # **在读优先**（v1.29.4，业务方 2026-10-16）：硕士「2024.9-至今」、本科 2024.6 毕业 ——
    # 本科的毕业年份不能拿来判"往届未就业"，人还没毕业呢。在读一律按应届处理。
    if in_school:
        lab = (f"{gtxt}毕业 · 应届（在读）"
               if gy is not None and gy >= cur_y else "在读 · 应届")
        return {"kind": "fresh", "in_school": True, "label": lab,
                "note": "现行学历仍在读（未毕业），按应届生处理；实习经历不计为正式工作"}
    # **应届优先**：当年或之后毕业 → 应届。
    # 顺序不能反：校招简历里几乎都有实习经历，抽取出来往往被算成"1 年工作"，
    # 先判工作年限就会把应届生显示成"工作 1 年"，身份整个错掉。
    if gy is not None and gy >= cur_y - FRESH_WINDOW:
        return {"kind": "fresh", "in_school": False, "label": f"{gtxt}毕业 · 应届",
                "note": "应届毕业生（当年或之后毕业）；实习经历不计为正式工作"}
    if has_work and isinstance(years, int) and years > 0:
        return {"kind": "work", "in_school": False, "label": f"工作 {years} 年",
                "note": "有正式工作经历"}
    if gy is None:
        return {"kind": "unknown", "in_school": False, "label": "毕业时间未识别",
                "note": "简历里没找到毕业时间，建议人工确认"}
    if gy >= cur_y - FRESH_WINDOW:
        return {"kind": "fresh", "in_school": False, "label": f"{gtxt}毕业 · 应届",
                "note": "应届毕业生（当年或之后毕业）"}
    if gy >= cur_y - IDLE_WINDOW:
        return {"kind": "past_idle", "in_school": False,
                "label": f"{gtxt}毕业 · 往届未就业",
                "note": f"{cur_y - gy} 年前毕业且无工作经历"}
    return {"kind": "no_exp", "in_school": False, "label": f"{gtxt}毕业 · 无工作经历",
            "note": "毕业较早且无工作经历，建议人工确认"}
