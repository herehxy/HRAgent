"""主动提案：系统自己巡检、自己发现问题，而不是等人来问。

这是"问答式助手"与"智能体"的分界线——**没有任何人提问，系统也会开口**。

三条铁律（与项目既有约束一脉相承）：

1. **只提案，不执行**。产出的仍是"待确认"提案，走与模型提案**完全同一条**
   确认流（`actions.apply_proposal`）。"系统自己发现的"不是自动执行的理由。
2. **不轰炸**。同一件事在去重窗口内（默认 7 天）只提一次——否则 HR 会
   把整个提案列表当成噪音忽略掉，功能反而失效。
3. **可归因**。`source='agent_auto'` 标出来源，`session_id='auto-YYYYMMDD'`
   把同一轮巡检产生的提案归到一组，便于复盘"那天系统为什么提这个"。

当前只做两条规则（宁缺勿滥）：
- **高分待确认**：系统建议已达 A 档门槛但 HR 还没背书 → 建议直接定档（HR 一键确认）
- **阶段停滞**：非终点阶段停留超期 → 建议推进到下一阶段（HR 可拒绝）
"""

from __future__ import annotations

from datetime import datetime

from .. import db

#: 与 agent/tools.py 的 STAGES 保持同一口径（阶段是全系统共用的封闭集合）
STAGES = ["新投递", "已联系", "初面", "复面", "待offer", "已入职", "已结束"]
_FINISHED = ("已入职", "已结束")

#: 去重窗口：同一件事多久内不重复提
DEDUP_DAYS = 7
#: 单轮巡检最多产出多少条提案（防止一次刷屏）
MAX_PER_RUN = 10
#: 高分门槛：达到即值得提请确认
HIGH_SCORE = 0.85


def _next_stage(stage: str | None) -> str | None:
    """下一个可推进的阶段；已是终点或未知阶段则返回 None。"""
    if stage in _FINISHED or stage not in STAGES:
        return None
    i = STAGES.index(stage)
    nxt = STAGES[i + 1] if i + 1 < len(STAGES) else None
    return nxt


def _stage_days(since: str | None) -> int:
    """停留天数（算不出来时返回 0，宁可少说不瞎说）。"""
    if not since:
        return 0
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return max(0, (datetime.now() - datetime.strptime(since[:len(fmt) + 2].strip(), fmt)).days)
        except ValueError:
            continue
    return 0


def _propose_if_new(conn, *, tool: str, args: dict, summary: str, risk: str,
                    dedup_key: str, session_id: str, result: dict) -> None:
    """带去重窗口的建提案。命中窗口就跳过并计数，不报错。"""
    if db.recent_proposal_exists(conn, dedup_key, DEDUP_DAYS):
        result["skipped"] += 1
        return
    pid = db.create_proposal(conn, session_id, tool, args, summary, risk,
                             source="agent_auto", dedup_key=dedup_key)
    result["created"].append({"proposal_id": pid, "tool": tool,
                              "summary": summary, "dedup_key": dedup_key})


def scan_and_propose(conn, stuck_days: int = 7, max_per_run: int = MAX_PER_RUN,
                     session_id: str | None = None) -> dict:
    """巡检一遍待办，产出待确认提案。返回本轮结果（建了哪些、跳过了几条）。"""
    session_id = session_id or f"auto-{datetime.now():%Y%m%d}"
    w = db.pending_workload(conn, stuck_days=stuck_days, limit=max_per_run * 2)
    result: dict = {"session_id": session_id, "created": [], "skipped": 0,
                    "scanned": w["counts"], "stuck_days": stuck_days}

    # —— 规则 1：系统认为很合适但 HR 还没背书 → 建议定档（HR 一键确认即生效）——
    # 口径变迁：v1.12 删掉加权打分后没有"匹配度分数"了，改用**建议档 = A**（新数据）
    # 或历史分数 ≥ 0.85（老库兼容）。文案里也别再写"匹配度 —"——分数已经不存在，
    # 写个破折号只会让人以为"系统算不出来还硬要提个建议"。
    for it in w["items"]["high_score"]:
        if len(result["created"]) >= max_per_run:
            break
        aid = it["id"]
        tier = it.get("tier_suggested")
        name = it.get("name") or f"候选人#{it.get('candidate_id')}"
        if not tier:
            continue
        score = it.get("score")
        if isinstance(score, (int, float)):
            basis = f"匹配度 {score:.2f}"
        elif tier == "A":
            basis = "模型判断为 A 档（明显匹配）"
        else:
            basis = "系统判断为高匹配"
        _propose_if_new(
            conn, tool="set_tier",
            args={"application_id": aid, "tier": tier,
                  "note": f"系统巡检建议（{basis}）"},
            summary=f"{name} {basis}、系统建议 {tier} 档，尚未确认——"
                    f"确认后即定为最终档位",
            risk="中", dedup_key=f"high_unconfirmed:app#{aid}",
            session_id=session_id, result=result)

    # —— 规则 2：阶段停滞 → 建议推进到下一阶段 ——
    for it in w["items"]["stuck"]:
        if len(result["created"]) >= max_per_run:
            break
        aid = it["id"]
        stage = it.get("stage")
        nxt = _next_stage(stage)
        name = it.get("name") or f"候选人#{it.get('candidate_id')}"
        if not nxt:
            continue
        days = _stage_days(it.get("since"))
        job = it.get("job_title")
        where = f"（{job}）" if job else ""
        _propose_if_new(
            conn, tool="set_stage",
            args={"application_id": aid, "stage": nxt},
            summary=f"{name}{where}在「{stage}」停留 {days} 天，"
                    f"建议推进到「{nxt}」——不合适可拒绝",
            risk="中", dedup_key=f"stage_stuck:app#{aid}:{stage}",
            session_id=session_id, result=result)

    result["created_count"] = len(result["created"])
    if result["created"]:
        # 一轮巡检留一条审计：能回答"这些提案是哪来的"
        db.add_audit(conn, "settings", "proactive", "scan", "",
                     f"系统巡检产出 {len(result['created'])} 条待确认提案"
                     f"（跳过 {result['skipped']} 条重复）",
                     "scheduler", "system")
    return result
