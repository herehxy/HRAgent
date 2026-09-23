"""智能体工具集：模型通过 function calling 调用这些工具去"做事"。

工具是"智能体"与"工作流 + 规则"的分水岭——模型自己决定调哪个、传什么参数、调几次。

按**风险等级**分四类，权限策略各不相同：

===============  ====================================================================  ==========================
类别              工具                                                                   策略
===============  ====================================================================  ==========================
读                检索/取档/统计/技能召回/语义召回/相似人才/管道/收信/审计/提案列表         默认开放
算                匹配分析/多人对比/面试提纲/档位解释                                     默认开放（不改数据）
写（提案）        改阶段/改档/加标签/合并档案/写备注                                       **只产出待确认提案**，HR 点确认才落库
外发（禁用）      发邮件/导出到外部                                                        **不提供**，越界调用直接拒绝
===============  ====================================================================  ==========================

设计要点：写类工具**不返回"已完成"，只返回"已提交待确认"**。
模型因此不可能在 HR 不知情的情况下改动人才库——这是"建议与决定分离"在工具层的落实。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from .. import db
from .. import search as search_mod
from ..pipeline import normalize as nz
from ..pipeline.analyze import analyze_fit, draft_interview
from ..pipeline.tier import grade, major_match

WRITE_TOOLS = {"set_stage", "set_tier", "add_tag", "merge_candidates", "add_note"}

# 明确拒绝的工具名（不提供实现，但出现在提示里会被拦下并告知用户）
DISABLED_TOOLS = {"send_email", "send_message", "export_external", "delete_candidate",
                  "delete_resume", "reject_candidate", "notify_candidate"}

READ_TOOLS = {"search_candidates", "get_candidate", "pool_stats", "search_by_skills",
              "semantic_search", "similar_candidates", "pipeline_overview",
              "mailbox_status", "list_audit", "list_proposals", "explain_grade",
              "list_jobs"}
COMPUTE_TOOLS = {"analyze_fit", "compare_candidates", "draft_interview_questions"}

STAGES = ["新投递", "已联系", "初面", "复面", "待offer", "已入职", "已结束"]

_EDU_ORDER = {"大专": 1, "专科": 1, "本科": 2, "学士": 2, "研究生": 3, "硕士": 3, "博士": 4}


@dataclass
class ToolCtx:
    db_path: str
    jd: dict = field(default_factory=dict)
    tiers: dict = field(default_factory=dict)
    job_id: int | None = None
    session_id: str = ""
    operator: str = "HR"
    role: str = "recruiter"
    cfg: dict = field(default_factory=dict)


def _spec(name: str, desc: str, props: dict | None = None, required: list[str] | None = None) -> dict:
    params: dict = {"type": "object", "properties": props or {}}
    if required:
        params["required"] = required
    return {"type": "function", "function": {"name": name, "description": desc, "parameters": params}}


TOOL_SPECS = [
    # ------------------------------ 读 ------------------------------
    _spec("search_candidates",
          "按条件筛选候选人（档位/学历/年限/关键词/阶段）。返回真实库内数据，找不到就返回空，不要编造。",
          {"tier": {"type": "string", "description": "A/B/C/D/REVIEW/UNCONFIRMED/ALL"},
           "keyword": {"type": "string", "description": "姓名、院校、专业关键词"},
           "min_years": {"type": "integer", "description": "最低工作年限"},
           "education": {"type": "string", "description": "最低学历：大专/本科/硕士/博士"},
           "skill": {"type": "string", "description": "技能关键词"},
           "stage": {"type": "string", "description": "投递阶段，取值：" + "/".join(STAGES)},
           "limit": {"type": "integer", "description": "最多返回条数，默认 20"}}),
    _spec("get_candidate",
          "取某候选人的完整档案：基本信息、全部投递记录、技能（含原文证据）、标签。",
          {"candidate_id": {"type": "integer"}}, ["candidate_id"]),
    _spec("pool_stats",
          "人才库总览：人数、投递数、各档位分布、各阶段分布、待确认/待人工判读数量。"),
    _spec("search_by_skills",
          "按技能精确召回候选人。技能会先归一到本体（如 XRD = X射线衍射）。"
          "这是回答『做过 X 的人有哪些』的首选工具，结果可解释、每条都带原文证据。",
          {"skills": {"type": "array", "items": {"type": "string"}, "description": "技能名列表"},
           "mode": {"type": "string", "description": "all=全部具备（默认）/ any=具备其一"},
           "tier": {"type": "string", "description": "限定档位，可空"}}, ["skills"]),
    _spec("semantic_search",
          "语义检索：用于说不清关键词的需求（如『有难熔合金研发背景的博士』）。"
          "不如技能召回精确，适合探索性查找。",
          {"query": {"type": "string"}, "top_k": {"type": "integer", "description": "默认 5"}},
          ["query"]),
    _spec("similar_candidates",
          "找出与某候选人最相似的其他人才（向量相似），用于岗位适配替换、人才盘点。",
          {"candidate_id": {"type": "integer"}, "top_k": {"type": "integer", "description": "默认 5"}},
          ["candidate_id"]),
    _spec("list_jobs",
          "列出岗位：名称、部门、状态（在招/停用）、必需技能、加分技能、学历与年限门槛、已收投递数。"
          "回答『现在招哪些岗位』『发布了几个岗位』『某岗位要求是什么』一律用这个工具，"
          "不要凭印象说岗位名称或数量。", 
          {"include_inactive": {"type": "boolean",
                                "description": "是否包含已停用岗位，默认 false（只看在招）"}}),
    _spec("pipeline_overview",
          "招聘管道总览：各阶段人数、停留超过 15 天的积压、来源渠道分布。"),
    _spec("mailbox_status",
          "查看招聘邮箱抓取状态：模式、收信总数、待处理数、最近收到的邮件、抓取配置。"),
    _spec("list_audit",
          "查询操作审计日志（谁在何时改了什么）。可指定 entity（candidate/application/proposal）与 id。",
          {"entity": {"type": "string"}, "entity_id": {"type": "string"},
           "limit": {"type": "integer", "description": "默认 20"}}),
    _spec("list_proposals",
          "列出待 HR 确认的写入提案（改档/改阶段/加标签/合并）。"),
    _spec("explain_grade",
          "解释某候选人当前档位是怎么算出来的：四项分值拆解、命中技能及其原文证据、缺失项、风险提示。"
          "纯规则计算，不调用模型，结论稳定可复现。",
          {"candidate_id": {"type": "integer"}}, ["candidate_id"]),

    # ------------------------------ 算 ------------------------------
    _spec("analyze_fit",
          "用模型对某候选人做岗位匹配分析，输出亮点/风险/一句话结论/建议档位。"
          "只在需要深入判断时调用；结果仅为建议，最终档位由 HR 决定。",
          {"candidate_id": {"type": "integer"}, "job_id": {"type": "integer", "description": "岗位 id，可空"}},
          ["candidate_id"]),
    _spec("compare_candidates",
          "在给定候选人之间做横向对比（学历/年限/技能命中/评分），输出差异表。纯规则计算。",
          {"candidate_ids": {"type": "array", "items": {"type": "integer"}}}, ["candidate_ids"]),
    _spec("draft_interview_questions",
          "为某候选人生成定制面试提纲（含考察意图），围绕其技能缺口与项目经历。",
          {"candidate_id": {"type": "integer"},
           "focus": {"type": "string", "description": "HR 特别关注的点，可空"}}, ["candidate_id"]),

    # ------------------------------ 写（提案） ------------------------------
    _spec("set_stage",
          "【需 HR 确认】把某条投递推进到新阶段（新投递/已联系/初面/复面/待offer/已入职/已结束）。"
          "本工具只生成待确认提案，不会直接改库。",
          {"application_id": {"type": "integer"}, "stage": {"type": "string"},
           "reason": {"type": "string", "description": "变更理由"}}, ["application_id", "stage"]),
    _spec("set_tier",
          "【需 HR 确认】调整某条投递的档位。本工具只生成待确认提案，不会直接改库。",
          {"application_id": {"type": "integer"}, "tier": {"type": "string", "description": "A/B/C/D"},
           "note": {"type": "string"}}, ["application_id", "tier"]),
    _spec("add_tag",
          "【需 HR 确认】为候选人添加能力/类型标签。只生成待确认提案。",
          {"candidate_id": {"type": "integer"}, "tag": {"type": "string"},
           "category": {"type": "string", "description": "能力/类型/来源，默认能力"},
           "evidence": {"type": "string", "description": "依据（原文片段）"}}, ["candidate_id", "tag"]),
    _spec("merge_candidates",
          "【需 HR 确认】把疑似重复的两份档案合并（软合并，可撤销）。只生成待确认提案。"
          "系统不会自动合并——误合并的破坏性远大于重复入库。",
          {"source_id": {"type": "integer", "description": "被并入的一方"},
           "target_id": {"type": "integer", "description": "保留的主档"},
           "reason": {"type": "string"}}, ["source_id", "target_id"]),
    _spec("add_note",
          "【需 HR 确认】给某条投递给 HR 备注。只生成待确认提案。",
          {"application_id": {"type": "integer"}, "note": {"type": "string"}},
          ["application_id", "note"]),
]


# ============================================================
# 辅助
# ============================================================

def _brief(c: dict) -> dict:
    return {
        "candidate_id": c["id"], "name": c.get("name"),
        "education": c.get("edu_level"), "years": c.get("years_exp"),
        "school": c.get("school"), "major": c.get("major"),
        "tier_suggested": c.get("tier_suggested"), "tier_final": c.get("tier_final"),
        "tier": c.get("tier_effective"), "score": c.get("score"),
        "stage": c.get("stage"), "confirmed": (c.get("app_status") or "") == "已确认",
        "need_review": bool(c.get("needs_review")),
        "hit": c.get("hits") or [], "miss": c.get("miss") or [],
        "skills": (c.get("skills") or [])[:10],
    }


def _ok(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


def _err(msg: str, hint: str | None = None) -> str:
    """错误响应。`hint` 是"下一步该怎么办"——与 `msg`（哪里不对）分开，
    界面与模型都能直接把它当成可执行动作，而不是让人在一段话里自己找。
    """
    out = {"error": msg}
    if hint:
        out["hint"] = hint
    return json.dumps(out, ensure_ascii=False)


def _propose(conn, ctx: ToolCtx, tool: str, args: dict, summary: str, risk: str = "中") -> str:
    pid = db.create_proposal(conn, ctx.session_id, tool, args, summary, risk)
    return _ok({
        "status": "pending_confirmation",
        "proposal_id": pid,
        "summary": summary,
        "note": "已提交待 HR 确认。在你（模型）这一侧，此操作**尚未生效**；"
                "不要向用户声称已完成修改，应说明『已提交待确认』。",
    })


def _skill_rows(conn, cid: int) -> list[dict]:
    return db.candidate_skill_rows(conn, cid, verified_only=False)


# ============================================================
# 执行
# ============================================================

def execute(name: str, args: dict, ctx: ToolCtx) -> str:
    """执行工具，返回 JSON 字符串。任何异常都转成可读错误，绝不让循环崩掉。"""
    args = args or {}
    if name in DISABLED_TOOLS:
        return _err(
            f"工具 {name} 在本系统中不提供。原因：系统不做对外发送、不做删除、不做淘汰决定。"
            "若确需对外沟通或删除，请由 HR 在系统外人工完成。")

    conn = db.connect(ctx.db_path)
    try:
        # ---------------- 读 ----------------
        if name == "search_candidates":
            # 技能词先过本体（"XRD" → "X射线衍射"），再带上原词一起匹配：
            # 归一是为了让别名也能召回，保留原词是为了本体没收录的新技能
            # （如刚出现的框架名）仍能按字面命中，不至于"查无此人"。
            raw_skill = (args.get("skill") or "").strip()
            skill_terms = None
            if raw_skill:
                skill_terms = sorted(set(nz.resolve_query_terms([raw_skill])) | {raw_skill})
            items = db.list_candidates(
                conn, tier=args.get("tier"), keyword=args.get("keyword"),
                skill=skill_terms, min_years=args.get("min_years"),
                education=args.get("education"), stage=args.get("stage"),
                job_id=ctx.job_id)
            limit = int(args.get("limit") or 20)
            return _ok({"count": len(items), "returned": min(limit, len(items)),
                        "candidates": [_brief(c) for c in items[:limit]]})

        if name == "get_candidate":
            cid = int(args["candidate_id"])
            d = db.candidate_detail(conn, cid)
            if not d:
                return _err(f"未找到候选人 #{cid}")
            return _ok({
                "candidate_id": cid, "name": d.get("name"),
                "education": d.get("edu_level"), "years": d.get("years_exp"),
                "school": d.get("school"), "major": d.get("major"),
                "current_org": d.get("current_org"),
                "pool_status": d.get("pool_status"), "pii_level": d.get("pii_level"),
                "tier_suggested": d.get("tier_suggested"), "tier_final": d.get("tier_final"),
                "score": d.get("score"), "stage": d.get("stage"),
                "reasons": d.get("reasons"), "risks": d.get("risks"),
                "hit": d.get("hits"), "miss": d.get("miss"),
                "needs_review": d.get("needs_review"),
                "applications": [{"application_id": a["id"], "job": a.get("job_title"),
                                  "channel": a.get("channel"), "applied_at": a.get("applied_at"),
                                  "tier": a.get("tier_final") or a.get("tier_suggested"),
                                  "stage": a.get("stage"), "status": a.get("status"),
                                  "score": a.get("score")} for a in d.get("applications", [])],
                "skills": [{"name": s["name"], "category": s.get("category"),
                            "level": s.get("level"), "verified": s.get("verified"),
                            "evidence": s.get("evidence")} for s in d.get("skills", [])],
                "tags": [t["name"] for t in d.get("tags", [])],
                "documents": [{"file": x.get("file_name"), "engine": x.get("parse_engine"),
                               "parse_ok": x.get("parse_ok")} for x in d.get("documents", [])],
                "raw_text_excerpt": (d.get("raw_text") or "")[:2000],
            })

        if name == "pool_stats":
            s = db.pool_stats(conn)
            s["agent_cost"] = db.agent_cost_summary(conn)
            return _ok(s)

        if name == "search_by_skills":
            skills = args.get("skills") or []
            if isinstance(skills, str):
                skills = [s.strip() for s in skills.replace("、", ",").split(",") if s.strip()]
            res = search_mod.by_skills(conn, skills, mode=args.get("mode") or "all",
                                       include_tier=args.get("tier"))
            res["results"] = res["results"][:12]
            return _ok(res)

        if name == "semantic_search":
            res = search_mod.semantic(conn, args.get("query", ""),
                                      top_k=int(args.get("top_k") or 5))
            return _ok(res)

        if name == "similar_candidates":
            res = search_mod.similar_to(conn, int(args["candidate_id"]),
                                        top_k=int(args.get("top_k") or 5))
            return _ok(res)

        if name == "list_jobs":
            inc = bool(args.get("include_inactive"))
            rows = db.list_jobs(conn, include_inactive=True)
            # 在招 = 岗位本身启用 且 所属部门未停用（与归岗用的 routable 口径一致：
            # 部门停用的岗位收不到简历，不该算作"在招"）
            active = [j for j in rows if j.get("active", 1) and j.get("department_active", True)]
            show = rows if inc else active
            items = []
            for j in show:
                jd = j.get("jd_json") or {}
                must = jd.get("must") or {}
                items.append({
                    "job_id": j["id"], "title": j.get("title"),
                    "department": j.get("department_name") or j.get("dept"),
                    "status": j.get("status"),
                    "active": bool(j.get("active", 1)),
                    "must_skills": list(must.get("skills_required") or []),
                    "preferred_skills": list((jd.get("preferred") or {}).get("skills") or []),
                    "education_min": must.get("education_min"),
                    "years_min": must.get("years_min"),
                    "applications_count": j.get("applications_count"),
                })
            return _ok({"open_count": len(active), "total_count": len(rows),
                        "include_inactive": inc, "jobs": items,
                        "note": "open_count 为当前在招岗位数（不含已停用岗位与停用部门下的岗位）"})

        if name == "pipeline_overview":
            p = db.pipeline_stats(conn)
            return _ok({
                "open_total": p["open_total"], "channels": p["channels"],
                "stages": {k: {"count": v["count"], "overdue_15d": v["overdue"]}
                           for k, v in p["stages"].items()},
                "stage_order": STAGES,
            })

        if name == "mailbox_status":
            from .. import mailbox as mb

            cfg = mb.load_config()
            conn2 = conn
            mode = cfg.get("mode")
            last = db.list_emails(conn2, limit=5)
            s = db.pool_stats(conn2)
            return _ok({
                "mode": mode,
                "mode_note": {"eml": "读取本地 .eml 邮件目录（离线/演练模式）",
                              "imap": "IMAP 只读增量抓取专用招聘收件箱",
                              "off": "抓取已关闭"}.get(mode, mode),
                "eml_dir": cfg.get("eml_dir"),
                "imap_host": (cfg.get("imap") or {}).get("host") or "未配置",
                "imap_readonly": (cfg.get("imap") or {}).get("readonly", True),
                "emails_total": s["emails"], "emails_pending": s["emails_pending"],
                "recent": [{"subject": m.get("subject"), "from": m.get("from_addr"),
                            "received_at": m.get("received_at"), "attachments": m.get("attachment_count"),
                            "processed": m.get("processed"), "error": m.get("error")}
                           for m in last],
                "dedup_layers": ["message_id（邮件层）", "SHA256 附件（文件层）", "identity_key（内容层）"],
            })

        if name == "list_audit":
            rows = db.list_audit(conn, entity=args.get("entity"),
                                 entity_id=args.get("entity_id"),
                                 limit=int(args.get("limit") or 20))
            return _ok({"count": len(rows), "records": [
                {"entity": r["entity"], "entity_id": r["entity_id"], "action": r["action"],
                 "before": r["before"], "after": r["after"], "operator": r["operator"],
                 "ts": r["ts"]} for r in rows]})

        if name == "list_proposals":
            rows = db.list_proposals(conn, status=args.get("status") or "待确认")
            return _ok({"count": len(rows), "proposals": [
                {"proposal_id": r["id"], "tool": r["tool"], "summary": r["summary"],
                 "risk": r["risk"], "status": r["status"], "created_at": r["created_at"]}
                for r in rows]})

        if name == "explain_grade":
            cid = int(args["candidate_id"])
            c = db.get_candidate(conn, cid)
            if not c:
                return _err(f"未找到候选人 #{cid}")
            d = db.candidate_detail(conn, cid) or {}
            cand = {
                "skills": db.candidate_skill_names(conn, cid, verified_only=True),
                "unverified_skills": [s["name"] for s in _skill_rows(conn, cid)
                                      if not s.get("verified")],
                "skill_detail": [{"canonical": s["name"], "evidence": s.get("evidence"),
                                  "verified": s.get("verified")} for s in _skill_rows(conn, cid)],
                "education": c.get("edu_level"), "years": c.get("years_exp"),
                "name": c.get("name"),
            }
            # v1.6：按候选人「对应岗位」（已归岗 > 建议岗位）解释，**不再用材料类默认尺子**；
            # 与入库时的评分用的是同一把尺子，所以解释与库里的档位必然对得上。
            jd, job_meta = db.resolve_candidate_job(conn, d)
            if not jd:
                return _err(
                    "该候选人尚无对应岗位（既未归岗、也没有可用的建议岗位），无法解释档位。",
                    hint="请在「人才库」卡片上归岗，或采纳系统给出的建议岗位。"
                         "若系统连建议都没给，通常是该候选人与所有在招岗位的距离都超出建议阈值"
                         "（例如跨行业简历），此时只需人工确认一个岗位即可。")
            g = grade(cand, jd, ctx.tiers)
            mm = major_match({**cand, "major": d.get("major")}, jd, db.skill_categories(conn))
            # 与库内记录对账（v1.6）：这里是"现在重算"，库里是"上次重算时写下的结论"。
            # 不一致通常是因为技能词表更新过（如本体补充了软件类技能），
            # **如实标出来并给出刷新路径**，比让 HR 在两处看到不同数字却不知为什么好。
            _apps = d.get("applications") or []
            _db_app = next((x for x in _apps if x.get("job_id")), (_apps[0] if _apps else {}))
            consistency = None
            if _db_app.get("score") is not None:
                _same = (float(_db_app.get("score") or 0) == float(g["score"])
                         and (_db_app.get("tier_suggested") or "") == g["tier_suggested"])
                consistency = {
                    "same": _same,
                    "db_score": _db_app.get("score"),
                    "db_tier": _db_app.get("tier_suggested"),
                    "db_hits": _db_app.get("hits") or [],
                    "note": ("库内记录与当前重算一致" if _same else
                             "库内记录与当前重算不一致（技能词表或该岗位 JD 更新过）："
                             "库内是上次重算写下的结论。可在「部门与岗位」页对该岗位点"
                             "「重新分析」刷新库内结论。"),
                }
            return _ok({
                "candidate_id": cid, "name": c.get("name"),
                "job": job_meta,
                "tier_suggested": g["tier_suggested"], "score": g["score"],
                "breakdown": g["breakdown"],
                "reasons": g["reasons"], "risks": g["risks"],
                "hit": [{"skill": h["skill"], "evidence": h["evidence"]} for h in g["hit_detail"]],
                "miss": g["miss"], "preferred_hit": g["preferred_hit"],
                # v1.6：专业大类对照——回答"方向对不对口"，不只回答"缺哪几项技能"
                "major_match": mm,
                "consistency": consistency,
                "needs_review": g["needs_review"],
                "unverified_skills": cand["unverified_skills"],
                "method": "纯规则计算，不调用模型；分值构成：学历 0.25 / 年限 0.25 / 必需技能 0.35 / 加分项 0.15",
            })

        # ---------------- 算 ----------------
        if name == "analyze_fit":
            cid = int(args["candidate_id"])
            d = db.candidate_detail(conn, cid)
            if not d:
                return _err(f"未找到候选人 #{cid}")
            # v1.6：没显式指定岗位时，按候选人「对应岗位」分析（已归岗 > 建议岗位），
            # 不再回落到材料类默认尺子——否则软件岗候选人会被按材料岗评估。
            if args.get("job_id"):
                jid = int(args["job_id"])
                j = db.get_job(conn, jid) or {}
                jd, job_meta = db.job_of(conn, jid), {
                    "job_id": jid, "title": j.get("title"), "dept": j.get("dept"),
                    "source": "explicit", "why": "调用方显式指定了岗位"}
            else:
                jd, job_meta = db.resolve_candidate_job(conn, d)
            if not jd:
                return _err("该候选人尚无对应岗位（既未归岗、也没有可用的建议岗位），"
                            "无法做岗位匹配分析。",
                            hint="请在「人才库」卡片上归岗，或采纳系统给出的建议岗位。")
            r = analyze_fit(d, jd)
            if r is None:
                return _err("模型不可用，未能完成深度分析。可改用 explain_grade（纯规则，随时可用）")
            r["job"] = job_meta
            r["disclaimer"] = "模型建议，仅供参考；最终档位由 HR 确认"
            return _ok(r)

        if name == "compare_candidates":
            ids = [int(i) for i in (args.get("candidate_ids") or [])][:8]
            if not ids:
                return _err("请提供至少一个 candidate_id")
            rows = []
            for cid in ids:
                c = db.get_candidate(conn, cid)
                if not c:
                    continue
                app = db.latest_application(conn, cid)
                rows.append({
                    "candidate_id": cid, "name": c.get("name"),
                    "education": c.get("edu_level"), "years": c.get("years_exp"),
                    "school": c.get("school"), "tier": (app or {}).get("tier_final")
                        or (app or {}).get("tier_suggested"),
                    "score": (app or {}).get("score"),
                    "hit": (app or {}).get("hits") or [], "miss": (app or {}).get("miss") or [],
                    "skills_count": len(db.candidate_skill_names(conn, cid)),
                })
            if not rows:
                return _err("给定的候选人都不存在")
            # 差异点
            dims = ["education", "years", "tier", "score"]
            diff = {d: {str(r["candidate_id"]): r.get(d) for r in rows} for d in dims}
            return _ok({"count": len(rows), "rows": rows, "dimension_map": diff,
                        "method": "纯规则对比"})

        if name == "draft_interview_questions":
            cid = int(args["candidate_id"])
            d = db.candidate_detail(conn, cid)
            if not d:
                return _err(f"未找到候选人 #{cid}")
            # v1.6：面试提纲锚定候选人「对应岗位」，否则会给软件工程师出材料类面试题
            jd, job_meta = db.resolve_candidate_job(conn, d)
            if not jd:
                return _err("该候选人尚无对应岗位，无法生成面试提纲（提纲按岗位 JD 定制，"
                            "没有岗位就只能出通用题）。",
                            hint="请先归岗或采纳建议岗位，再生成提纲。")
            r = draft_interview(d, jd, args.get("focus", ""))
            if r is None:
                return _err("模型不可用，未能生成面试提纲")
            r["job"] = job_meta
            return _ok(r)

        # ---------------- 写（提案）----------------
        if name == "set_stage":
            aid = int(args["application_id"])
            app = db.get_application(conn, aid)
            if not app:
                return _err(f"未找到投递 #{aid}")
            stage = (args.get("stage") or "").strip()
            if stage not in STAGES:
                return _err(f"阶段取值非法：{stage}；只能是 {'/'.join(STAGES)}")
            cand = db.get_candidate(conn, app["candidate_id"]) or {}
            return _propose(conn, ctx, "set_stage",
                            {"application_id": aid, "stage": stage},
                            f"把 {cand.get('name') or '#' + str(app['candidate_id'])} 的投递 "
                            f"#{aid} 从「{app.get('stage')}」推进到「{stage}」"
                            + (f"（理由：{args['reason']}）" if args.get("reason") else ""))

        if name == "set_tier":
            aid = int(args["application_id"])
            app = db.get_application(conn, aid)
            if not app:
                return _err(f"未找到投递 #{aid}")
            tier = (args.get("tier") or "").strip().upper()
            if tier not in ("A", "B", "C", "D"):
                return _err("档位只能是 A/B/C/D")
            cand = db.get_candidate(conn, app["candidate_id"]) or {}
            cur = app.get("tier_final") or app.get("tier_suggested")
            return _propose(conn, ctx, "set_tier",
                            {"application_id": aid, "tier": tier, "note": args.get("note") or ""},
                            f"把 {cand.get('name') or '#' + str(app['candidate_id'])} 的投递 "
                            f"#{aid} 档位从「{cur}」改为「{tier}」")

        if name == "add_tag":
            cid = int(args["candidate_id"])
            c = db.get_candidate(conn, cid)
            if not c:
                return _err(f"未找到候选人 #{cid}")
            return _propose(conn, ctx, "add_tag",
                            {"candidate_id": cid, "tag": args["tag"],
                             "category": args.get("category") or "能力",
                             "evidence": args.get("evidence") or ""},
                            f"给 {c.get('name') or '#' + str(cid)} 添加标签「{args['tag']}」",
                            risk="低")

        if name == "merge_candidates":
            src, dst = int(args["source_id"]), int(args["target_id"])
            a, b = db.get_candidate(conn, src), db.get_candidate(conn, dst)
            if not a or not b:
                return _err("候选人不存在，无法生成合并提案")
            return _propose(conn, ctx, "merge_candidates",
                            {"source_id": src, "target_id": dst},
                            f"把「{a.get('name')}」(# {src}) 合并进「{b.get('name')}」(# {dst})"
                            f"（软合并，可撤销）", risk="高")

        if name == "add_note":
            aid = int(args["application_id"])
            app = db.get_application(conn, aid)
            if not app:
                return _err(f"未找到投递 #{aid}")
            return _propose(conn, ctx, "add_note",
                            {"application_id": aid, "note": args["note"]},
                            f"给投递 #{aid} 写入备注：{str(args['note'])[:60]}", risk="低")

        return _err(f"未知工具 {name}。可用工具：{', '.join(sorted(READ_TOOLS | COMPUTE_TOOLS | WRITE_TOOLS))}")
    except KeyError as exc:
        return _err(f"工具 {name} 缺少必需参数：{exc}")
    except Exception as exc:
        return _err(f"工具 {name} 执行失败：{type(exc).__name__}: {exc}")
    finally:
        conn.close()


def catalog() -> dict:
    """给界面用的工具清单。"""
    return {
        "read": sorted(READ_TOOLS),
        "compute": sorted(COMPUTE_TOOLS),
        "write_requires_confirmation": sorted(WRITE_TOOLS),
        "disabled": sorted(DISABLED_TOOLS),
        "total": len(TOOL_SPECS),
    }
