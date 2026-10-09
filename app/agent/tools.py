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
import re
from dataclasses import dataclass, field

from .. import db
from .. import search as search_mod
from ..pipeline import normalize as nz
from ..pipeline.analyze import analyze_fit, draft_interview
from ..pipeline.tier import grade, major_match

WRITE_TOOLS = {"set_stage", "set_tier", "add_tag", "merge_candidates", "add_note",
               # v1.16 补齐：这些动作以前只有界面能点，agent 调不到
               "assign_job", "suggest_job", "mark_review",
               "set_archive", "archive_batch", "split_candidate",
               "create_job", "update_job_jd", "regrade_job"}
# ingest_resumes / export_resumes **不在** WRITE_TOOLS：它们是直接执行的
# （取数据、给下载指引），标成"写·需确认"会让界面与实际行为不一致。

# 明确拒绝的工具名（不提供实现，但出现在提示里会被拦下并告知用户）
DISABLED_TOOLS = {"send_email", "send_message", "export_external", "delete_candidate",
                  "delete_resume", "reject_candidate", "notify_candidate"}

READ_TOOLS = {"search_candidates", "get_candidate", "pool_stats", "search_by_skills",
              "semantic_search", "similar_candidates", "pipeline_overview",
              "mailbox_status", "list_audit", "list_proposals", "explain_grade",
              "list_jobs"}
COMPUTE_TOOLS = {"analyze_fit", "compare_candidates", "draft_interview_questions",
                 "draft_email"}

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
          {"candidate_id": {"type": "string",
                            "description": "候选人 id 或**姓名**（两种都行，系统自动解析）"}},
           ["candidate_id"]),
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
          {"candidate_id": {"type": "string",
                            "description": "候选人 id 或**姓名**（两种都行，系统自动解析）"}},
           ["candidate_id"]),

    # ------------------------------ 算 ------------------------------
    _spec("analyze_fit",
          "用模型对某候选人做岗位匹配分析，输出亮点/风险/一句话结论/建议档位。"
          "只在需要深入判断时调用；结果仅为建议，最终档位由 HR 决定。",
          {"candidate_id": {"type": "integer"}, "job_id": {"type": "integer", "description": "岗位 id，可空"}},
          ["candidate_id"]),
    _spec("compare_candidates",
          "在给定候选人之间做横向对比（学历/年限/技能命中/评分），输出差异表。纯规则计算。",
          {"candidate_ids": {"type": "array", "items": {"type": "integer"}}}, ["candidate_ids"]),
    _spec("draft_email",
          "为某候选人**起草**一封邮件（面试邀请 / 跟进 / 婉拒 / offer 沟通 / 其他）。"
          "只起草、不发送——发送必须由 HR 在邮件编辑器里确认后亲自点。"
          "返回主题、正文与收件人，可直接在对话里改或交给编辑器。",
          {"candidate_id": {"type": "integer"},
           "purpose": {"type": "string",
                       "description": "面试邀请 / 跟进 / 婉拒 / offer沟通 / 其他（默认按用途猜）"},
           "tone": {"type": "string", "description": "语气：客气简洁 / 正式 / 轻松，可空"},
           "interview_time": {"type": "string", "description": "面试时间，可空"},
           "interview_place": {"type": "string", "description": "面试地点/会议链接，可空"},
           "extra": {"type": "string", "description": "要特别说明的事项，可空"}},
          ["candidate_id"]),
    _spec("draft_interview_questions",
          "为某候选人生成定制面试提纲（含考察意图），围绕其技能缺口与项目经历。",
          {"candidate_id": {"type": "integer"},
           "focus": {"type": "string", "description": "HR 特别关注的点，可空"}}, ["candidate_id"]),

    # ------------------------------ 写（提案） ------------------------------
    _spec("ingest_resumes",
          "收取/导入简历（邮箱或本地文件夹）。会真的读文件、真的入库。",
          {"source": {"type": "string", "description": "mailbox（收邮箱）/ folder（本地目录）"},
           "folder": {"type": "string", "description": "source=folder 时的目录路径，可空（用配置的）"}},
          ["source"]),
    _spec("assign_job",
          "把某人的投递归到指定岗位（未归岗=归岗，已归岗=改岗位），并按该岗位 JD 重算建议档位。",
          {"candidate_id": {"type": "string", "description": "候选人 id 或姓名"},
           "job": {"type": "string", "description": "岗位 id 或岗位名（模糊匹配也可以）"},
           "application_id": {"type": "integer", "description": "有多条投递时指定哪条，可空"}},
          ["candidate_id", "job"]),
    _spec("suggest_job",
          "让模型判断这条待指定投递最像哪个在招岗位（只判断、只落库建议，不归岗）。",
          {"candidate_id": {"type": "string", "description": "候选人 id 或姓名"}},
          ["candidate_id"]),
    _spec("mark_review",
          "标记「HR 已核对过这个人的信息」（复核）。**不改档位**，与档位判断分开记。",
          {"candidate_id": {"type": "string", "description": "候选人 id 或姓名"},
           "undo": {"type": "boolean", "description": "true=撤销复核"}},
          ["candidate_id"]),
    _spec("set_archive",
          "归档 / 取消归档某个候选人（归档不是删除：档案、附件、审计全保留，随时可恢复）。",
          {"candidate_id": {"type": "string", "description": "候选人 id 或姓名"},
           "archived": {"type": "boolean", "description": "true=归档，false=取消归档"}},
          ["candidate_id", "archived"]),
    _spec("archive_batch",
          "批量归档/取消归档。**先用 search_candidates 看清楚要处理谁**，再调用。",
          {"candidate_ids": {"type": "array", "items": {"type": "integer"}},
           "archived": {"type": "boolean", "description": "true=归档，false=取消归档"}},
          ["candidate_ids", "archived"]),
    _spec("split_candidate",
          "撤销合并：把被并入另一档的人恢复成独立档案，投递与附件搬回本档。",
          {"candidate_id": {"type": "string", "description": "被合并的那个候选人 id 或姓名"}},
          ["candidate_id"]),
    _spec("create_job",
          "新建岗位（名字 + 可选 JD）。",
          {"title": {"type": "string"},
           "must_skills": {"type": "array", "items": {"type": "string"},
                           "description": "必需技能"},
           "education_min": {"type": "string", "description": "学历门槛：中专/大专/本科/硕士/博士"},
           "years_min": {"type": "integer", "description": "年限门槛，可空"},
           "note": {"type": "string", "description": "职责/备注，可空"}},
          ["title"]),
    _spec("update_job_jd",
          "改某个岗位的 JD（必需技能/学历门槛/年限门槛）。**只影响之后新入库或重算的投递**。",
          {"job_id": {"type": "integer"},
           "must_skills": {"type": "array", "items": {"type": "string"}},
           "education_min": {"type": "string"},
           "years_min": {"type": "integer"},
           "note": {"type": "string"}},
          ["job_id"]),
    _spec("regrade_job",
          "按岗位当前 JD 重算该岗位下所有投递的建议档位。**默认只预演**（不写库）；"
          "apply=true 也只是生成待确认提案，HR 点头后才落库。",
          {"job_id": {"type": "integer"},
           "apply": {"type": "boolean", "description": "true=确认写入；默认只预演"}},
          ["job_id"]),
    _spec("export_resumes",
          "导出简历原件（打包 zip）或导出人才清单 CSV，返回下载方式。",
          {"candidate_ids": {"type": "array", "items": {"type": "integer"},
                             "description": "为空则导出全部在库的人"},
           "what": {"type": "string", "description": "resumes=简历原件包（默认）/ csv=人才清单"}},
          []),
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


def _resolve_cid(conn, value, what: str = "候选人") -> tuple[int | None, dict | None]:
    """把"何晓宇"或 12 解析成候选人 id。**不支持时返回 (None, 错误信息)**。

    为什么要有（v1.13.9）：HR 说"给何晓宇发邮件"，模型为了调工具只能先猜一个
    数字 id——实测它猜了 12345，整条链路断在"未找到候选人 #12345"。
    人是用名字指人的，工具不该强迫模型先知道数据库主键。
    精确匹配优先；命中多个时把名单返回，让模型追问而不是继续猜。
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, {"error": f"没提供{what}（给 id 或姓名都可以）"}
    if isinstance(value, int):
        return int(value), None
    v = str(value).strip()
    if re.fullmatch(r"\d+", v):
        return int(v), None
    rows = conn.execute(
        "SELECT id, name FROM candidates WHERE merged_into IS NULL AND name = ? "
        "ORDER BY id LIMIT 2", (v,)).fetchall()
    if len(rows) == 1:
        return int(rows[0]["id"]), None
    if len(rows) > 1:
        return None, {"error": f"有 {len(rows)} 位候选人叫「{v}」",
                      "hint": "请先用 search_candidates 确认是哪一位，再带上 id 调用"}
    rows = conn.execute(
        "SELECT id, name FROM candidates WHERE merged_into IS NULL AND name LIKE ? "
        "ORDER BY id LIMIT 6", (f"%{v}%",)).fetchall()
    if len(rows) == 1:
        return int(rows[0]["id"]), None
    if len(rows) > 1:
        return None, {"error": f"有 {len(rows)} 位候选人名字里含「{v}」",
                      "hint": "请先 search_candidates 或让用户确认是哪一位",
                      "candidates": [{"id": r["id"], "name": r["name"]} for r in rows]}
    return None, {"error": f"库里没有叫「{v}」的候选人",
                  "hint": "先用 search_candidates 按姓名/技能搜一下；确实没有就让用户先导入简历"}


def _jd_tiers() -> tuple[dict, dict]:
    """取当前生效的 JD 与档位配置（与接口层同一套口径）。

    **不 import server**（会造成循环依赖：server → tools → server），
    所以自己读 config 下的两个文件；路径算法与 server 一致（仓库根/config）。
    """
    import json as _json
    import os as _os
    _base = _os.path.dirname(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))
    def _rd(_n):
        try:
            with open(_os.path.join(_base, "config", _n), encoding="utf-8") as _f:
                return _json.load(_f)
        except (OSError, ValueError):
            return {}
    return _rd("jd.json"), _rd("tiers.json")


def _jd_from_args(args: dict) -> dict:
    """把工具参数拼成一份 JD（只填传了的字段，不猜）。"""
    must = {}
    sk = args.get("must_skills")
    if isinstance(sk, list) and sk:
        must["skills_required"] = [str(x).strip() for x in sk if str(x).strip()]
    edu = (args.get("education_min") or "").strip()
    if edu:
        must["education_min"] = edu
    try:
        yrs = int(args.get("years_min") or 0)
    except (TypeError, ValueError):
        yrs = 0
    if yrs:
        must["years_min"] = yrs
    jd = {"role": (args.get("title") or "").strip(), "must": must, "preferred": {"skills": []}}
    note = (args.get("note") or "").strip()
    if note:
        jd["note"] = note
    return jd


def _merge_jd_args(jd: dict, args: dict) -> None:
    """把参数里**传了**的字段并进已有 JD（没传的保持原样，不清空）。"""
    sk = args.get("must_skills")
    if isinstance(sk, list):
        jd.setdefault("must", {})["skills_required"] = [str(x).strip() for x in sk if str(x).strip()]
    edu = (args.get("education_min") or "").strip()
    if edu:
        jd.setdefault("must", {})["education_min"] = edu
    if args.get("years_min") is not None:
        try:
            jd.setdefault("must", {})["years_min"] = int(args["years_min"] or 0)
        except (TypeError, ValueError):
            pass
    note = (args.get("note") or "").strip()
    if note:
        jd["note"] = note


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
                job_id=ctx.job_id,
                # 归档的人不在人才库里，助手也就不该把他们列出来（与界面同一口径）
                archived=False)
            limit = int(args.get("limit") or 20)
            return _ok({"count": len(items), "returned": min(limit, len(items)),
                        "candidates": [_brief(c) for c in items[:limit]]})

        if name == "get_candidate":
            cid, _cerr = _resolve_cid(conn, args.get("candidate_id"))
            if _cerr:
                return _err(_cerr["error"], _cerr.get("hint"))
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
            _cid_s, _cerr_s = _resolve_cid(conn, args.get(candidate_id))
            if _cerr_s:
                return _err(_cerr_s[error], _cerr_s.get(hint))
            res = search_mod.similar_to(conn, _cid_s,
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
                         "若系统连建议都没给，通常是模型也判断不出最像哪个在招岗位"
                         "（例如跨行业简历），此时只需人工确认一个岗位即可。")
            # 解释档位时同样区分"已归岗"与"只是建议岗位"：建议岗位的学历门槛
            # 不能用来判 D（v1.8.9），解释里也就不该出现按猜出来的门槛下的结论。
            _has_job = any(x.get("job_id") for x in (d.get("applications") or []))
            g = grade(cand, jd, job_confirmed=_has_job)
            mm = major_match({**cand, "major": d.get("major")}, jd, db.skill_categories(conn))
            # 与库内记录对账（v1.6）：这里是"现在重算"，库里是"上次重算时写下的结论"。
            # 不一致通常是因为技能词表更新过（如本体补充了软件类技能），
            # **如实标出来并给出刷新路径**，比让 HR 在两处看到不同数字却不知为什么好。
            _apps = d.get("applications") or []
            _db_app = next((x for x in _apps if x.get("job_id")), (_apps[0] if _apps else {}))
            consistency = None
            if _db_app.get("tier_suggested") is not None:
                # v1.12：打分已删，只对账档位（学历门槛结论一致即可）
                _same = (_db_app.get("tier_suggested") or "") == (g["tier_suggested"] or "")
                consistency = {
                    "same": _same,
                    "db_tier": _db_app.get("tier_suggested"),
                    "db_hits": _db_app.get("hits") or [],
                    "note": ("库内记录与当前重算一致" if _same else
                             "库内记录与当前重算不一致（技能词表或该岗位 JD 更新过）："
                             "库内是上次重算写下的结论。可在「部门与岗位」页对该岗位点"
                             "「重新分析」刷新库内结论。"),
                }
            # v1.12：**档位答案取库内生效值**——学历门槛由规则定（不达标→D）、
            # A/B/C 由模型的自动分析给出。现算只负责"学历门槛结论 + 技能命中 + 专业方向"：
            # 规则已经产不出 A/B/C，拿现算值当答案会让所有学历达标的人显示成"待分析"。
            _stored = _db_app.get("tier_final") or _db_app.get("tier_suggested")
            _rule = g["tier_suggested"]                     # 现算：None 或 D
            if _stored == "D" or _rule == "D":
                _src = "学历门槛（规则判定，可复现）"
            elif _stored:
                _src = "模型分析（自动分析给出的建议档）"
            else:
                _src = "待分析（模型尚未给出结论）"
            return _ok({
                "candidate_id": cid, "name": c.get("name"),
                "job": job_meta,
                "tier_suggested": _stored, "tier_rule": _rule,
                "tier_source": _src,
                "breakdown": g["breakdown"],
                "reasons": g["reasons"], "risks": g["risks"],
                "hit": [{"skill": h["skill"], "evidence": h["evidence"]} for h in g["hit_detail"]],
                "miss": g["miss"], "preferred_hit": g["preferred_hit"],
                # v1.6：专业大类对照——回答"方向对不对口"，不只回答"缺哪几项技能"
                "major_match": mm,
                "consistency": consistency,
                "needs_review": g["needs_review"],
                "unverified_skills": cand["unverified_skills"],
                "method": ("档位：学历门槛由规则判（不达标→D），A/B/C 由模型分析给出；"
                           "本工具不调模型，只做现算的学历门槛与技能/专业方向核对"),
            })

        # ---------------- 算 ----------------
        if name == "analyze_fit":
            cid, _cerr = _resolve_cid(conn, args.get("candidate_id"))
            if _cerr:
                return _err(_cerr["error"], _cerr.get("hint"))
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

        if name == "draft_email":
            # v1.13.7：起草邮件。**不发送**（send_email 仍在 DISABLED_TOOLS）。
            from .. import auth as _auth            # 延迟导入：取真实邮箱要解密
            from .. import mail_template as _mt
            from ..pipeline.analyze import draft_mail
            cid, _cerr = _resolve_cid(conn, args.get("candidate_id"))
            if _cerr:
                return _err(_cerr["error"], _cerr.get("hint"))
            d = db.candidate_detail(conn, cid)
            if not d:
                return _err(f"未找到候选人 #{cid}")
            cand = _auth.present_candidate(d)
            jd, meta = db.resolve_candidate_job(conn, d)
            job_title = (meta or {}).get("title") or ""
            to_addr = (cand.get("email") or "").strip()
            rt = {"面试时间": args.get("interview_time") or "",
                  "面试地点": args.get("interview_place") or "",
                  "联系人": ctx.operator, "补充说明": args.get("extra") or ""}
            ctxd = _mt.build_ctx(cand, job_title, ctx.operator, rt)
            m = draft_mail(cand, job_title, args.get("purpose") or "",
                           args.get("tone") or "", rt)
            if m is None:
                return _err("模型不可用，没能起草这封邮件。"
                            "可以改用「邮件」页里的模板草稿（不依赖模型）。",
                            hint="模板草稿同样能生成可编辑的正文，路径：邮件 → 新建草稿")
            subj = (m.get("subject") or "").strip()
            body = (m.get("body") or "").strip()
            body_html = (body if _mt.looks_like_html(body) else _mt.to_html(body))
            _miss = [k for k, v in rt.items() if not v]
            return _ok({
                "candidate_id": cid, "name": cand.get("name"),
                "to": to_addr, "subject": subj, "body": body, "body_html": body_html,
                "job": job_title,
                "missing_runtime": _miss,
                "note": ("**这是草稿，没有发送。**请在对话里确认内容，或点「在邮件编辑器中打开」"
                         "由你亲自点发送。"
                         + (f" 还缺：{'、'.join(_miss)}（补上后我重写一版）。" if _miss else "")
                         + ("" if to_addr else " ⚠️ 库里没有这个人��邮箱——发送前请先补联系方式。")
                         if to_addr else
                         " ⚠️ **库里没有这个人的邮箱**，没法直接发。"
                         "可以先把简历里的邮箱补进档案，或改成你手动转发。"),
                "disclaimer": "智能体只起草，不发送；对外动作必须由 HR 亲自执行。",
            })

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
            cid, _cerr = _resolve_cid(conn, args.get("candidate_id"))
            if _cerr:
                return _err(_cerr["error"], _cerr.get("hint"))
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
        if name == "ingest_resumes":
            # 导入是**真的读文件**，不是提案：它本身没有"改档案"的歧义，
            # 且 HR 明确要求 agent 能主动收简历。归档/改档仍然走提案。
            from . import ingest as _ing
            src = (args.get("source") or "mailbox").strip()
            if src not in ("mailbox", "folder"):
                return _err(f"source 只能是 mailbox 或 folder（收到的是 {src}）")
            _conn_jd = _jd_tiers()
            if src == "folder":
                folder = (args.get("folder") or "").strip()
                if not folder:
                    return _err("source=folder 需要给 folder 路径",
                                hint="可以去「系统配置 → 导入与来源」看已配置的目录")
                rep = _ing.ingest_dir(folder, _conn_jd[0], _conn_jd[1], ctx.db_path,
                                      job_id=None, use_llm=False)
            else:
                rep = _ing.sync_mailbox(_conn_jd[0], _conn_jd[1], ctx.db_path, job_id=None,
                                        use_llm=False)
            return _ok({"source": src, "index": rep.get("index"),
                        "note": "已入库。新人默认进「待指定」，可在人才库点「指定岗位」，"
                                "或让我帮你判断（suggest_job）。"
                                "是否自动分析由「入库即分析」开关控制。"})

        if name == "assign_job":
            cid, cerr = _resolve_cid(conn, args.get("candidate_id"))
            if cerr:
                return _err(cerr["error"], cerr.get("hint"))
            jobs = db.list_jobs(conn, include_inactive=False)
            kw = (args.get("job") or "").strip()
            j = next((x for x in jobs if str(x["id"]) == kw), None)
            if not j:
                cands = [x for x in jobs if kw and (kw in (x.get("title") or "")
                                                     or kw in (x.get("department_name") or ""))]
                if len(cands) == 1:
                    j = cands[0]
                elif len(cands) > 1:
                    return _err(f"有 {len(cands)} 个岗位匹配「{kw}」",
                                hint="请给岗位 id 或更完整的岗位名",
                                jobs=[{"id": x["id"], "title": x.get("title")} for x in cands[:6]])
                else:
                    return _err(f"没有匹配「{kw}」的在招岗位",
                                hint="先用 list_jobs 看有哪些岗位，或 create_job 新建")
            d = db.candidate_detail(conn, cid) or {}
            apps = d.get("applications") or []
            if args.get("application_id"):
                aid = int(args["application_id"])
            else:
                _a0 = next((a for a in apps if a.get("job_id") is None),
                           apps[0] if apps else None)
                if not _a0:
                    return _err("这个人还没有投递记录，无法归岗")
                aid = _a0["id"]
            cur_job = next((a.get("job_title") or "待指定"
                            for a in apps if a.get("id") == aid), "待指定")
            return _propose(conn, ctx, "assign_job",
                            {"application_id": aid, "job_id": j["id"]},
                            f"把 {(d.get('name') or '#'+str(cid))} 的投递 #{aid} "
                            f"从「{cur_job}」归到「{j.get('title')}」")

        if name == "suggest_job":
            cid, cerr = _resolve_cid(conn, args.get("candidate_id"))
            if cerr:
                return _err(cerr["error"], cerr.get("hint"))
            from .. import regrade as _rg2
            r = _rg2.suggest_job_for_application(conn, cid, ctx.operator, ctx.role)
            if not r.get("ok"):
                return _err(r.get("error") or "判断失败",
                            hint="确认模型可用、且这个人确实还没归岗")
            return _ok({"candidate_id": cid, "suggested_job": r.get("title"),
                        "reason": r.get("reason"), "note": r.get("note"),
                        "next": "确认后用 assign_job 归岗（会再提案，等 HR 点头）"})

        if name == "mark_review":
            cid, cerr = _resolve_cid(conn, args.get("candidate_id"))
            if cerr:
                return _err(cerr["error"], cerr.get("hint"))
            d = db.candidate_detail(conn, cid) or {}
            apps = d.get("applications") or []
            if not apps:
                return _err("这个人还没有投递记录")
            aid = apps[0]["id"]
            cur = apps[0].get("status")
            undo = bool(args.get("undo"))
            if (cur == "已确认") == (not undo):
                return _ok({"candidate_id": cid, "unchanged": True,
                            "note": "已经是你要的状态了，无需改动"})
            return _propose(conn, ctx, "mark_review",
                            {"candidate_id": cid, "undo": undo},
                            ("撤销" if undo else "标记") + f" {d.get('name') or '#'+str(cid)} "
                            + ("的复核" if undo else "「信息已核对完毕」"))

        if name == "set_archive":
            cid, cerr = _resolve_cid(conn, args.get("candidate_id"))
            if cerr:
                return _err(cerr["error"], cerr.get("hint"))
            d = db.get_candidate(conn, cid) or {}
            archived = bool(args.get("archived"))
            left = ((d.get("archive") or {}).get("days_left"))
            return _propose(conn, ctx, "set_archive",
                            {"candidate_id": cid, "archived": archived},
                            ("归档" if archived else "取消归档") + f" {d.get('name') or '#'+str(cid)}"
                            + (f"（归档已满 {left} 天，**执行后会被彻底删除**，原件移入回收目录）"
                               if archived and left == 0 else ""))

        if name == "archive_batch":
            ids = [int(x) for x in (args.get("candidate_ids") or [])][:200]
            if not ids:
                return _err("candidate_ids 为空",
                            hint="先用 search_candidates 找到要处理的人，再把 id 传进来")
            archived = bool(args.get("archived"))
            names = []
            due = 0
            for _i in ids:
                _c = db.get_candidate(conn, _i) or {}
                names.append(_c.get("name") or f"#{_i}")
                if archived and ((_c.get("archive") or {}).get("days_left")) == 0:
                    due += 1
            return _propose(conn, ctx, "archive_batch",
                            {"ids": ids, "archived": archived},
                            f"{'归档' if archived else '取消归档'} {len(ids)} 人："
                            + "、".join(names[:8]) + ("…" if len(names) > 8 else "")
                            + (f"；其中 **{due} 人归档已满 30 天，执行后会被彻底删除**"
                               if due else ""))

        if name == "split_candidate":
            cid, cerr = _resolve_cid(conn, args.get("candidate_id"))
            if cerr:
                return _err(cerr["error"], cerr.get("hint"))
            c = db.get_candidate(conn, cid) or {}
            _cn = c.get("name") or cid
            _into = c.get("merged_into")
            if not _into:
                return _err(f"{_cn} 不是被合并进来的档案，无需拆分")
            return _propose(conn, ctx, "split_candidate", {"candidate_id": cid},
                            f"撤销合并：把 {_cn} 从 #{_into} 拆回独立档（投递与附件会搬回本档）")

        if name == "create_job":
            title = (args.get("title") or "").strip()
            if not title:
                return _err("岗位名不能为空")
            if any((j.get("title") or "") == title for j in db.list_jobs(conn, include_inactive=True)):
                return _err(f"已经有叫「{title}」的岗位了", hint="要改它的 JD 请用 update_job_jd")
            jd = _jd_from_args(args)
            return _propose(conn, ctx, "create_job", {"title": title, "jd": jd},
                            f"新建岗位「{title}」"
                            + (f"，必需技能 {'、'.join(jd['must']['skills_required'])}"
                               if jd["must"].get("skills_required") else "")
                            + (f"，学历门槛 {jd['must']['education_min']}"
                               if jd["must"].get("education_min") else ""),
                            risk="中")

        if name == "update_job_jd":
            jid = int(args["job_id"])
            job = db.get_job(conn, jid)
            if not job:
                return _err(f"未找到岗位 #{jid}", hint="用 list_jobs 看现有岗位")
            jd = dict(job.get("jd_json") or {})
            _merge_jd_args(jd, args)
            return _propose(conn, ctx, "update_job_jd", {"job_id": jid, "jd": jd},
                            f"改岗位「{job.get('title')}」的 JD：必需技能 "
                            f"{'、'.join(jd.get('must', {}).get('skills_required') or []) or '（未改）'}"
                            f"，学历门槛 {jd.get('must', {}).get('education_min') or '（未改）'}"
                            "。已有投递的档位不会自动变，要不要重算请另行确认。",
                            risk="中")

        if name == "regrade_job":
            jid = int(args["job_id"])
            job = db.get_job(conn, jid)
            if not job:
                return _err(f"未找到岗位 #{jid}", hint="用 list_jobs 看现有岗位")
            apply_ = bool(args.get("apply"))
            from .. import regrade as _rg
            if apply_:
                # 落库也必须等 HR 点头：重算会改一批人的系统建议档位。
                return _propose(conn, ctx, "regrade_job",
                                {"job_id": jid, "apply": True},
                                f"按当前 JD 重算岗位「{job.get('title')}」下**所有**投递的"
                                f"建议档位（HR 已确认的档位不会被覆盖）",
                                risk="中")
            rep = _rg.regrade_job(conn, jid, job.get("jd_json") or {},
                                 _jd_tiers()[1], apply=False,
                                 operator=ctx.operator, role=ctx.role)
            return _ok({"job": job.get("title"), "applied": False,
                        "changed": rep.get("changed"), "kept": rep.get("kept"),
                        "items": (rep.get("items") or [])[:20],
                        "note": ("**这是预演，没有改任何档位**。"
                                 "确认这些差异没问题后，再调一次 apply=true 才会生成待确认提案。")})

        if name == "export_resumes":
            what = (args.get("what") or "resumes").strip()
            ids = [int(x) for x in (args.get("candidate_ids") or [])][:500]
            if what == "csv":
                return _ok({"what": "csv", "count": len(ids) or None,
                            "note": "人才清单 CSV 请在人才库页点「导出 CSV」——"
                                    "导出在浏览器本地生成，不需要我代劳"})
            return _ok({"what": "resumes", "candidate_ids": ids,
                        "note": f"简历原件打包 {len(ids) or '全部'} 份，"
                                f"请在「人才库」勾选后点「批量下载选中」"})

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
