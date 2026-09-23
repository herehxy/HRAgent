"""智能体循环：工具调用（function calling）驱动的 ReAct 实现。

流程：用户提问 → 模型决定调哪个工具 → 执行 → 结果回灌 → 再决策 → 答复。

四层治理：

1. **轮数上限**（`max_tool_rounds`，默认 5）：防止小模型陷入无限调用、费用失控；
2. **写入只产出提案**：改档/改阶段/加标签/合并一律走 `proposals`，等 HR 确认；
   模型拿到的工具返回里明确写着"尚未生效"，因此不可能声称已完成；
3. **全量运行留痕**：`agent_runs` 记录问题、工具链、轮数、token、耗时、模式，可复盘可核算；
4. **无模型时的规则路由**（`_rule_answer`）：模型不可用时不是简单地"罢工"，
   而是用关键词 + 技能本体识别意图，直接调同一批工具给出可读答复——
   这正是设计方案里"降级不失效"的要求。
"""
from __future__ import annotations

import json
import re
import time
import uuid

from .. import db
from ..pipeline import normalize as nz
from . import llm
from .tools import DISABLED_TOOLS, STAGES, TOOL_SPECS, ToolCtx, execute

SYSTEM_PROMPT = (
    "你是西北有色金属研究院人才库工作台的 HR 助手，熟悉稀有金属材料（钛合金、难熔合金、"
    "高温合金）研发与工艺岗位的招聘。\n"
    "铁律：\n"
    "1) 涉及候选人数量、条件筛选、技能、简历细节的问题，必须先调用工具取真实数据，"
    "严禁凭印象编造姓名、数字或经历；工具返回为空就如实说没有。\n"
    "2) 你只给建议，不做决定。最终档位、是否面试、是否录用都由 HR 确认，"
    "不要声称你已经替 HR 做了决定。\n"
    "3) 你没有删除数据、淘汰候选人、对候选人发消息的权限；也不要建议这么做。\n"
    "4) 改档、改阶段、加标签、合并档案这类写操作，工具会返回"
    "『已提交待 HR 确认（pending_confirmation）』——此时要明确告诉用户"
    "『已提交待确认』，绝不能说『已经改好了』。\n"
    "5) 引用技能或结论时，尽量带上简历原文证据（工具会返回 evidence 字段）。\n"
    "6) 回答用简体中文，结论先行、结构化（可用编号或短列表），不要输出 JSON。"
)


def run_agent(user_message: str, ctx: ToolCtx, history: list[dict] | None = None) -> dict:
    started = time.time()
    ctx.session_id = ctx.session_id or uuid.uuid4().hex[:12]
    cfg = llm.load_cfg()

    if not cfg.get("enabled", True):
        trace: list[dict] = []
        answer = _rule_answer(user_message, ctx, trace=trace)
        return _finalize(ctx, user_message, answer, trace, "offline", 0, {}, started)

    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    for h in (history or [])[-8:]:
        if h.get("role") in ("user", "assistant") and h.get("content"):
            messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": user_message})

    trace: list[dict] = []
    usage_total: dict = {}
    model_used = cfg.get("model")
    max_rounds = int(cfg.get("max_tool_rounds", 5))
    rounds = 0

    try:
        for _ in range(max_rounds):
            rounds += 1
            msg = llm.chat(messages, tools=TOOL_SPECS, cfg=cfg)
            model_used = msg.get("_model") or model_used
            _accumulate(usage_total, msg.get("_usage"))
            calls = msg.get("tool_calls")
            if not calls:
                answer = (msg.get("content") or "").strip()
                if not answer:
                    raise ValueError("模型返回了空答复")
                return _finalize(ctx, user_message, answer, trace, "llm", rounds,
                                 usage_total, started, model_used)
            messages.append({"role": "assistant", "content": msg.get("content") or "",
                             "tool_calls": calls})
            for tc in calls:
                fn = (tc.get("function") or {}).get("name", "")
                try:
                    args = json.loads((tc.get("function") or {}).get("arguments") or "{}")
                except ValueError:
                    args = {}
                result = execute(fn, args, ctx)
                trace.append({"tool": fn, "args": args, "result": result[:900],
                              "write": fn in WRITE_TOOLS,
                              "disabled": fn in DISABLED_TOOLS})
                messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                                 "name": fn, "content": result})
        # 轮次用尽，强制收敛
        messages.append({"role": "user", "content": "请基于已获取的信息直接给出最终答复。"})
        final = llm.chat(messages, cfg=cfg)
        _accumulate(usage_total, final.get("_usage"))
        rounds += 1
        return _finalize(ctx, user_message, (final.get("content") or "").strip(),
                         trace, "llm", rounds, usage_total, started, model_used)
    except Exception as exc:
        # 模型不可用 → 规则路由兜底（不是罢工）
        st = llm.status()
        reason = st.get("error") or f"{type(exc).__name__}: {exc}"
        answer = _rule_answer(user_message, ctx, reason=reason, trace=trace)
        return _finalize(ctx, user_message, answer, trace, "rule", rounds,
                         usage_total, started, model_used)


def _accumulate(total: dict, usage: dict | None) -> None:
    for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
        if usage and usage.get(k):
            total[k] = total.get(k, 0) + int(usage[k])


def _finalize(ctx: ToolCtx, question: str, answer: str, trace: list[dict], mode: str,
              rounds: int, usage: dict, started: float,
              model: str | None = None) -> dict:
    latency = int((time.time() - started) * 1000)
    proposals = []
    conn = db.connect(ctx.db_path)
    try:
        proposals = db.list_proposals(conn, status="待确认", limit=20)
        db.log_agent_run(conn, {
            "session_id": ctx.session_id, "question": question, "answer": answer[:4000],
            "model": model, "tool_calls": [{"tool": t["tool"], "args": t["args"]} for t in trace],
            "rounds": rounds, "tokens_in": usage.get("prompt_tokens", 0),
            "tokens_out": usage.get("completion_tokens", 0),
            "latency_ms": latency, "status": "ok", "mode": mode, "operator": ctx.operator,
        })
    finally:
        conn.close()
    return {
        "answer": answer,
        "trace": trace,
        "mode": mode,
        "rounds": rounds,
        "latency_ms": latency,
        "tokens": usage,
        "session_id": ctx.session_id,
        "pending_proposals": [{"proposal_id": p["id"], "tool": p["tool"],
                               "summary": p["summary"], "risk": p["risk"]} for p in proposals],
        "disclaimer": "系统只给建议，最终档位与招聘决定由 HR 确认。"
                      "写操作一律以『待确认提案』形式提交，不会自动生效。",
    }


# ============================================================
# 规则路由：模型不可用时的确定性兜底
# ============================================================

_NUM_RE = re.compile(r"(?:第\s*)?(#|id\s*=?\s*|编号\s*)?(\d{1,6})\s*(?:号|号候选人)?")
_STAGE_HINTS = {s: s for s in STAGES}

#: 写类工具。它们在 `tools.execute` 里只产出待确认提案，这里标记出来供留痕区分。
WRITE_TOOLS = frozenset({"set_stage", "set_tier", "add_tag", "merge_candidates", "add_note"})


def _pick_candidate_id(q: str) -> int | None:
    m = _NUM_RE.search(q)
    if not m:
        return None
    # 避免把"3年"、"2026"这类数字当 id
    tail = q[m.end():m.end() + 3]
    if tail.startswith("年") or tail.startswith("届"):
        return None
    return int(m.group(2))


def _rule_answer(question: str, ctx: ToolCtx, reason: str = "",
                 trace: list[dict] | None = None) -> str:
    """不依赖模型：按意图调用同一批工具并组织成可读答复。

    `trace` 与模型模式共用同一份结构（tool/args/result/write/disabled），
    这样 `agent_runs` 里的工具链在**降级路径下同样完整**——
    否则"降级"就等于"无法复盘"，与全量留痕的要求相冲突。
    """
    q = question or ""
    lower = q.lower()
    lines: list[str] = []
    header = "【规则模式】模型未就绪，已改用确定性规则应答（数据仍来自真实库）。"
    if reason:
        header += f"\n原因：{reason}"
    lines.append(header)
    if reason:
        lines.append("启用模型后可用自然语言追问、生成面试提纲与深度分析。"
                     "配置方式见 config/model.json（Ollama / 内网 vLLM / DeepSeek 均可）。")

    def call(name: str, args: dict) -> dict:
        raw = ""
        try:
            raw = execute(name, args, ctx)
            out = json.loads(raw)
        except Exception as exc:
            raw = raw or json.dumps({"error": str(exc)}, ensure_ascii=False)
            out = {"error": str(exc)}
        if trace is not None:
            trace.append({"tool": name, "args": args, "result": raw[:900],
                          "write": name in WRITE_TOOLS,
                          "disabled": name in DISABLED_TOOLS})
        return out

    def emit_hits(items: list[dict]) -> None:
        for x in items[:10]:
            lines.append(f"- {x.get('name') or '未识别'}（#{x['candidate_id']}，"
                         f"{x.get('education') or '—'}，"
                         f"{x.get('years') if x.get('years') is not None else '—'} 年，"
                         f"档 {x.get('tier') or '—'}）")
            for sk, ev in (x.get("evidence") or {}).items():
                lines.append(f"    证据[{sk}]：{ev}")

    handled = False

    # 0) 岗位（v1.6）
    # 必须有这一条、且必须排在"统计"之前：过去没有岗位分支，"发布了几个岗位"
    # 会被下面的"几个/多少"命中而返回人才库人数总览——**答非所问**。
    job_q = any(k in q for k in ("岗位", "职位", "在招", "招什么", "招哪些", "招聘计划"))
    if job_q:
        r = call("list_jobs", {})
        handled = True
        lines.append("\n在招岗位：")
        if r.get("error"):
            lines.append(f"- 查询失败：{r['error']}")
        else:
            lines.append(f"- 当前在招 {r.get('open_count')} 个岗位"
                         f"（岗位总数 {r.get('total_count')}，含已停用）")
            for j in (r.get("jobs") or []):
                tail = []
                if j.get("education_min"):
                    tail.append(str(j["education_min"]))
                if j.get("years_min"):
                    tail.append(f"{j['years_min']} 年以上")
                if j.get("must_skills"):
                    tail.append("必需：" + "、".join(j["must_skills"][:6]))
                if j.get("applications_count") is not None:
                    tail.append(f"已收 {j['applications_count']} 条投递")
                state = "" if j.get("active") else "（已停用）"
                lines.append(f"  · #{j.get('job_id')} {j.get('title')}{state}"
                             f"｜{j.get('department') or '—'}"
                             + ("｜" + "；".join(tail) if tail else ""))

    # 0b) 执行类意图：如实说明"这里不能直接执行"，并给出界面路径（v1.6）
    # 不假装能做，也不静默忽略——用户问得出"帮我抓取最新简历"，就说明这个入口不好找。
    _exec_words = ("抓取", "拉取", "导入", "收取", "同步", "采集")
    _exec_objs = ("简历", "邮件", "收件箱", "文件夹", "邮箱")
    if any(k in q for k in _exec_words) and any(k in q for k in _exec_objs) \
            and not any(k in q for k in ("能不能", "可以吗", "支持吗", "怎么", "如何")):
        handled = True
        lines.append("\n关于『抓取/导入简历』：")
        lines.append("- 本工作台**不接受由对话直接触发抓取**：抓取会真实落盘并入库，"
                     "属于有副作用的操作，按设计只由 HR 在界面上点击执行（界面就是凭证，操作会写审计）。")
        lines.append("- 具体入口：「导入与来源」页 —— 「收邮件」按当前邮箱配置抓取；"
                     "「导入文件夹」按『本地简历文件夹』路径导入。")
        lines.append("- 我可以帮你**查状态**：说『邮箱抓取状态』看收信情况，说『最近一次导入』看导入明细。")
        lines.append("- 改邮箱账号/文件夹路径也请在「邮箱配置」页改（涉及口令，不进对话留痕）。")

    # 1) 统计 / 概览（问的是岗位时跳过，避免和岗位分支各答一半）
    if not job_q and any(k in q for k in ("统计", "多少", "几个", "几人", "总览", "概览", "情况", "数量")):
        s = call("pool_stats", {})
        handled = True
        lines.append("\n人才库总览：")
        lines.append(f"- 候选人数 {s.get('people')} 人，投递 {s.get('applications')} 条，"
                     f"简历附件 {s.get('documents')} 份，收信 {s.get('emails')} 封"
                     f"（其中 {s.get('emails_pending')} 封待处理）")
        lines.append(f"- 在招岗位 {s.get('jobs_open')} 个（岗位总数 {s.get('jobs_total')}）")
        lines.append(f"- 档位分布：{json.dumps(s.get('tiers', {}), ensure_ascii=False)}")
        lines.append(f"- 待 HR 确认 {s.get('pending')} 条，待人工判读 {s.get('needs_review')} 条，"
                     f"待确认提案 {s.get('proposals_pending')} 条")
        lines.append(f"- 库内已出现技能 {s.get('skills')} 种、标签 {s.get('tags')} 种"
                     f"（技能本体共 {nz.describe().get('canonical_count')} 条）")

    # 2) 管道
    if any(k in q for k in ("管道", "进度", "流程", "积压", "阶段")):
        p = call("pipeline_overview", {})
        handled = True
        lines.append("\n招聘管道：")
        for st in STAGES:
            v = (p.get("stages") or {}).get(st) or {}
            if v.get("count"):
                lines.append(f"- {st}：{v['count']} 条"
                             + (f"（其中 {v['overdue_15d']} 条停留超 15 天）" if v.get("overdue_15d") else ""))
        lines.append(f"- 在流程中合计 {p.get('open_total')} 条；来源分布 "
                     f"{json.dumps(p.get('channels', {}), ensure_ascii=False)}")

    # 3) 邮箱
    if any(k in q for k in ("邮箱", "收信", "邮件", "抓取")):
        m = call("mailbox_status", {})
        handled = True
        lines.append("\n邮箱抓取状态：")
        lines.append(f"- 模式：{m.get('mode')}（{m.get('mode_note')}）")
        lines.append(f"- 累计收信 {m.get('emails_total')} 封，待处理 {m.get('emails_pending')} 封")
        lines.append(f"- 三层去重：{' / '.join(m.get('dedup_layers') or [])}")
        for r in (m.get("recent") or [])[:3]:
            lines.append(f"  · {r.get('received_at')} 「{r.get('subject')}」"
                         f"附件 {r.get('attachments')} 个{'' if r.get('processed') else '（未处理）'}")

    # 4) 待确认提案
    if any(k in q for k in ("提案", "待确认", "待办")):
        p = call("list_proposals", {})
        handled = True
        lines.append(f"\n待确认提案 {p.get('count')} 条：")
        for x in (p.get("proposals") or [])[:10]:
            lines.append(f"- #{x['proposal_id']} [{x['risk']}风险] {x['summary']}")

    # 5) 审计
    if any(k in q for k in ("审计", "谁改", "操作记录", "日志")):
        a = call("list_audit", {"limit": 8})
        handled = True
        lines.append("\n最近操作记录：")
        for r in (a.get("records") or []):
            lines.append(f"- {r['ts']} {r['operator']} 对 {r['entity']}#{r['entity_id']} "
                         f"执行 {r['action']}：{r['before']} → {r['after']}")

    # 6) 技能召回（"做过/会/有…经验的人"）
    skills = [s["canonical"] for s in nz.scan_text(q)]
    if skills and any(k in q for k in ("找", "有没有", "哪些", "谁", "做过", "会", "具备", "经验", "人选", "推荐")):
        r = call("search_by_skills", {"skills": skills, "mode": "all"})
        handled = True
        lines.append(f"\n按技能召回（同时具备 {'、'.join(skills)}）：命中 {r.get('count')} 人")
        emit_hits(r.get("results") or [])
        if not r.get("count"):
            if len(skills) > 1:
                # 条件太严以至于无人满足时，不能只回一句"没有"就结束——
                # 放宽为"任一命中"再查一次，把库里真实存在的人选如实给出，
                # 并说明命中了几项，由 HR 判断是否够用。
                r2 = call("search_by_skills", {"skills": skills, "mode": "any"})
                if r2.get("count"):
                    lines.append(f"\n库内暂无同时具备上述全部技能的人；放宽为『任一命中』"
                                 f"共 {r2['count']} 人（名单与命中项如下）：")
                    emit_hits(r2.get("results") or [])
                    miss_note = "、".join(
                        f"{x.get('name') or '未识别'}（{'、'.join(x.get('matched_skills') or [])}）"
                        for x in (r2.get("results") or [])[:10])
                    lines.append(f"- 各人实际命中：{miss_note}")
                else:
                    lines.append("- 库内暂无具备上述任一技能的人。")
            else:
                lines.append("- 库内暂无具备该技能的人。")
        if r.get("unknown_terms"):
            lines.append(f"- 注意：{'、'.join(r['unknown_terms'])} 不在技能本体中，"
                         f"未参与匹配，请换用标准技能名或先在本体中登记。")

    # 7) 某人的档案
    cid = _pick_candidate_id(q)
    if cid and any(k in q for k in ("详情", "档案", "简历", "看看", "介绍")):
        d = call("get_candidate", {"candidate_id": cid})
        handled = True
        if d.get("error"):
            lines.append(f"\n{d['error']}")
        else:
            lines.append(f"\n{d.get('name') or '未识别'}（#{cid}）")
            lines.append(f"- {d.get('education') or '—'} · "
                         f"{d.get('years') if d.get('years') is not None else '—'} 年 · "
                         f"{d.get('school') or '—'} · {d.get('major') or '—'}")
            lines.append(f"- 档位：建议 {d.get('tier_suggested') or '—'}"
                         f"（HR 确认 {d.get('tier_final') or '未确认'}），评分 {d.get('score')}")
            lines.append(f"- 投递 {len(d.get('applications') or [])} 条，"
                         f"附件 {len(d.get('documents') or [])} 份")
            hits = [h for h in (d.get("hit") or [])]
            if hits:
                lines.append(f"- 命中必需技能：{'、'.join(hits)}")
            if d.get("miss"):
                lines.append(f"- 缺失：{'、'.join(d['miss'])}")

    if not handled:
        lines.append("\n我可以直接回答这些问题（无需模型）：")
        lines.append("- 人才库有多少人？各档位分布如何？（统计）")
        lines.append("- 帮我找做过真空熔铸、会 XRD 的候选人（技能召回）")
        lines.append("- 现在招聘管道各阶段有多少、有没有积压（管道）")
        lines.append("- 邮箱收了多少封简历、有没有没处理的（收信台账）")
        lines.append("- 有哪些待确认的提案 / 最近谁改了什么（提案与审计）")
        lines.append("- 看看 1 号候选人的档案（用编号查询）")
    return "\n".join(lines)
