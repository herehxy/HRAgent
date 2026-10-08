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


def exp_label(years, has_work: bool, grad_date: str | None) -> dict:
    """给出招聘对象身份与展示文案。

    返回 `{kind, label, note}`：
      kind：work / fresh / past_idle / no_exp / unknown —— 便于前端配色与筛选
    """
    cur_y = datetime.now().year
    gy = int(grad_date[:4]) if grad_date else None
    gtxt = fmt_grad(grad_date)
    # **应届优先**：当年或之后毕业 → 应届。
    # 顺序不能反：校招简历里几乎都有实习经历，抽取出来往往被算成"1 年工作"，
    # 先判工作年限就会把应届生显示成"工作 1 年"，身份整个错掉。
    if gy is not None and gy >= cur_y - FRESH_WINDOW:
        return {"kind": "fresh", "label": f"{gtxt}毕业 · 应届",
                "note": "应届毕业生（当年或之后毕业）；实习经历不计为正式工作"}
    if has_work and isinstance(years, int) and years > 0:
        return {"kind": "work", "label": f"工作 {years} 年",
                "note": "有正式工作经历"}
    if gy is None:
        return {"kind": "unknown", "label": "毕业时间未识别",
                "note": "简历里没找到毕业时间，建议人工确认"}
    if gy >= cur_y - FRESH_WINDOW:
        return {"kind": "fresh", "label": f"{gtxt}毕业 · 应届",
                "note": "应届毕业生（当年或之后毕业）"}
    if gy >= cur_y - IDLE_WINDOW:
        return {"kind": "past_idle", "label": f"{gtxt}毕业 · 往届未就业",
                "note": f"{cur_y - gy} 年前毕业且无工作经历"}
    return {"kind": "no_exp", "label": f"{gtxt}毕业 · 无工作经历",
            "note": "毕业较早且无工作经历，建议人工确认"}
