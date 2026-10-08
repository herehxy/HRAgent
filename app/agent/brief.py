"""每日待办摘要 + 优先级判断。

与"报表"的区别就在这里：不是把计数罗列给 HR 看，而是**让模型判断
「今天最该先处理哪件事」并说明理由**。计数谁都会数，判断才是价值。

模型不可用时**不硬编优先级**，而是退回一条明确的规则排序
（影响面 × 紧急度），并在结果里如实标注 `source="rule"`——
HR 需要知道这条建议是判断出来的还是排出来的。
"""

from __future__ import annotations

import json
from datetime import datetime

from .. import db

#: 摘要结构版本。凡是"字段含义/措辞"变动（例如标题里要带姓名）都要 +1，
#: 服务端据此判断"库里那份是不是旧结构"，旧的就重算，避免页面一直显示旧措辞。
BRIEF_VERSION = 3

_BRIEF_SYSTEM = (
    "你是企业人才库工作台的 HR 助手。下面是今天的待办统计与明细，"
    "请判断**今天最该先关注什么**，帮 HR 排优先级。"
    "要求：\n"
    "1) 只依据给出的数据，不要编造候选人或数字；\n"
    "2) **标题里必须写出具体是谁**（用数据里的真实姓名，1-3 个，多个用顿号），"
    "不要写『1 位候选人』『某候选人』这类没有信息量的说法；\n"
    "3) 优先考虑：影响面大（涉及多人/整批）、有明确截止压力、"
    "拖久了会造成损失（如高分候选人流失、投递久了失联）的事项；\n"
    "4) priorities 最多 3 条，按该做的先后排序；\n"
    "5) 每条说清『做什么』和『为什么现在做』，不要复述统计数字；\n"
    "6) 你只给建议，不代替 HR 做任何决定。\n"
    "严格输出 JSON："
    '{"headline":"一句话：今天最该关注什么，**要带上姓名**（30字内）",'
    '"priorities":[{"title":"要做的事，**带上姓名**（20字内）",'
    '"why":"为什么现在做（30字内）",'
    '"action":"建议动作（20字内，如『建议核对并确认档位』）"}]}'
)


def _who(items: list[dict], limit: int = 3) -> str:
    """把"1 位候选人"换成**具体是谁**——HR 要的是能直接照着办事的名字。

    数量少时列全名；多时列前 3 个并带上总数（"张三、李四、王五 等 7 位"），
    既不啰嗦也不含糊。取不到姓名时返回空串，调用方退回原来的计数表述。
    """
    names = [str(i.get("name") or "").strip() for i in (items or [])]
    names = [n for n in names if n]
    if not names:
        return ""
    if len(names) <= limit:
        return "、".join(names)
    return "、".join(names[:limit]) + f" 等 {len(names)} 位"


def _rule_priorities(workload: dict) -> list[dict]:
    """规则降级：按「影响面 × 紧急度」排，不假装是判断。

    每条**带上当事人姓名**：只说"确认 1 位高分候选人的档位"，HR 还得自己去列表里翻；
    直接写"确认「张一鸣」的档位（0.92 · B）"，点一下就能办。
    """
    c = workload["counts"]
    items = workload.get("items") or {}
    out: list[dict] = []
    if c.get("high_score"):
        _hs = items.get("high_score") or []
        who = _who(_hs)
        detail = ""
        if len(_hs) == 1:
            _i = _hs[0]
            detail = f"（{_i.get('score')} · {_i.get('tier_suggested')}）" if _i.get("score") is not None else ""
        out.append({"title": (f"确认「{who}」的档位{detail}" if who
                              else f"确认 {c['high_score']} 位高分候选人的档位"),
                    "why": "匹配度已达标但未确认，拖久了人才容易流失",
                    "action": "打开人才库按匹配度排序，逐条确认档位",
                    "names": [i.get("name") for i in _hs if i.get("name")],
                    "count": c["high_score"]})
    if c.get("stuck"):
        _st = items.get("stuck") or []
        who = _who(_st)
        out.append({"title": (f"推进「{who}」的停滞投递" if who
                              else f"处理 {c['stuck']} 条停滞投递"),
                    "why": f"已在同一阶段停留超过 {workload['stuck_days']} 天",
                    "action": "看「投递管道」视图，逐条决定推进或结束",
                    "names": [i.get("name") for i in _st if i.get("name")],
                    "count": c["stuck"]})
    if c.get("needs_review"):
        _nr = items.get("needs_review") or []
        who = _who(_nr)
        out.append({"title": (f"人工判读「{who}」的简历" if who
                              else f"人工判读 {c['needs_review']} 份解析失败的简历"),
                    "why": "原件已保留但内容未读出，不处理会一直占着『待判读』",
                    "action": "打开档案看原件，手工补录关键字段",
                    "names": [i.get("name") for i in _nr if i.get("name")],
                    "count": c["needs_review"]})
    if c.get("pending_job"):
        _pj = items.get("pending_job") or []
        who = _who(_pj)
        out.append({"title": (f"给「{who}」指定岗位" if who
                              else f"为 {c['pending_job']} 条投递指定岗位"),
                    "why": "未归岗的投递不会进入任何岗位的选拔流程",
                    "action": "在人才库卡片上采纳系统建议岗位或自行指定",
                    "names": [i.get("name") for i in _pj if i.get("name")],
                    "count": c["pending_job"]})
    if c.get("pending_confirm") and not c.get("high_score"):
        _pc = items.get("pending_confirm") or []
        who = _who(_pc)
        out.append({"title": (f"确认「{who}」的档位" if who
                              else f"确认 {c['pending_confirm']} 条待确认档位"),
                    "why": "系统已给出建议档位，等 HR 背书",
                    "action": "打开人才库逐条确认",
                    "names": [i.get("name") for i in _pc if i.get("name")],
                    "count": c["pending_confirm"]})
    return out[:3]


def _llm_priorities(workload: dict) -> dict | None:
    """让模型判断优先级。模型不可用返回 None（调用方退回规则排序）。"""
    from . import llm

    def _brief(items: list[dict], keys: list[str], limit: int = 8) -> list[dict]:
        return [{k: it.get(k) for k in keys} for it in (items or [])[:limit]]

    payload = {
        "日期": f"{datetime.now():%Y-%m-%d}",
        "统计": workload["counts"],
        "停滞判定天数": workload["stuck_days"],
        "高分未确认": _brief(workload["items"]["high_score"],
                        ["name", "score", "tier_suggested", "job_title"]),
        "停滞投递": _brief(workload["items"]["stuck"],
                       ["name", "stage", "since", "job_title"]),
        "待人工判读": _brief(workload["items"]["needs_review"], ["name", "since"]),
        "待指定岗位": _brief(workload["items"]["pending_job"],
                        ["name", "score", "suggested_job_title"]),
    }
    try:
        r = llm.chat_json(_BRIEF_SYSTEM, json.dumps(payload, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001 — 简报不能因为模型挂了就出不来
        print(f"[brief] 模型判断失败，退回规则排序：{type(exc).__name__}: {exc}")
        return None
    if not isinstance(r, dict) or not r.get("priorities"):
        return None
    return r


def build(conn, stuck_days: int = 7, use_llm: bool = True) -> dict:
    """生成今日摘要（不落库；调用方决定是否保存）。"""
    workload = db.pending_workload(conn, stuck_days=stuck_days)
    stats = workload["counts"]
    total = sum(stats.values())

    llm_out = _llm_priorities(workload) if use_llm else None
    if llm_out:
        priorities = [p for p in (llm_out.get("priorities") or []) if p.get("title")][:3]
        headline = (llm_out.get("headline") or "").strip()
        source = "llm"
    else:
        priorities = _rule_priorities(workload)
        source = "rule"
        headline = (priorities[0]["title"] if priorities else "今天没有待处理事项")

    if not total:
        headline = "今天没有待处理事项，人才库是干净的"
        priorities = []

    return {
        "date": f"{datetime.now():%Y-%m-%d}",
        "generated_at": f"{datetime.now():%Y-%m-%d %H:%M:%S}",
        "brief_version": BRIEF_VERSION,
        "stuck_days": stuck_days,
        "stats": stats,
        "total": total,
        "headline": headline,
        "priorities": priorities,
        "source": source,
        "note": ("优先级由模型基于当前数据判断。" if source == "llm"
                 else "模型不可用，优先级按「影响面 × 紧急度」规则排序，"
                      "未经模型判断——仅供参考。"),
        "disclaimer": "本摘要只提示该看什么，不代替你做决定。",
    }


def build_and_save(conn, stuck_days: int = 7, use_llm: bool = True) -> dict:
    """生成并落库（按日期幂等覆盖）。"""
    payload = build(conn, stuck_days=stuck_days, use_llm=use_llm)
    db.save_brief(conn, payload["date"], payload)
    return payload
