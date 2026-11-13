"""按意图选择本轮要暴露给模型的工具（模型无关的确定性路由）。

为什么需要：`TOOL_SPECS` 现在有近百个工具，一次性全塞给模型会带来三个问题：

1. **选择困难**：弱模型在 `search_candidates` / `semantic_search` /
   `search_by_skills` / `similar_candidates` 之间反复试探；
2. **上下文浪费**：所有工具说明占掉大量 token，真正的问题反而被挤掉；
3. **误调用**：模型看到 `save_model_config` 就更容易去改配置。

这里用**关键词 + 分组**做确定性路由，不依赖任何模型能力，也不改变
`execute()` 的权限边界。路由只决定"本轮给模型看哪些工具"，写工具仍然
只产出待确认提案。`max_tools` 是保险丝：即使路由命中多个组也不会把
全部工具重新塞回去。

以后换任何通用 LLM，这个路由都同样生效；如果模型走文本工具协议
（不支持原生 function calling），`tool_catalog_text()` 也只列出这一小批。
"""
from __future__ import annotations

import re

from .tools import (COMPUTE_TOOLS, MANAGE_TOOL_NAMES, READ_TOOLS,
                    TOOL_SPECS, WRITE_TOOLS)

#: 任何问题都保留的最小工具集：找人、看档、看岗位、看总量
CORE_MIN = [
    "search_candidates", "get_candidate", "get_candidates", "list_jobs", "pool_stats",
    # 邮件/原件是"候选人的全部信息"的一部分，任何问题都保留
    "get_source_mail", "get_document_text",
]

#: 分组。工具名必须与 `TOOL_SPECS` 一致；`missing_tools()` 用于自检。
GROUPS: dict[str, list[str]] = {
    "profile": [
        # 读（检索统一走 search_candidates 的 mode，不再暴露 4 个重复工具）
        "analyze_fit", "compare_candidates", "explain_grade",
        "school_info", "get_interview_dict", "draft_interview_questions",
        # 写（只出提案）
        "set_tier", "add_tag", "add_note", "update_candidate_fields",
        "reanalyze_candidate", "merge_candidates", "split_candidate",
        "mark_review", "set_archive", "archive_batch",
        "set_interview_info", "compare_channel", "manage_avatar",
        "manage_candidate",
    ],
    "recruit": [
        # 读（招聘进度统一走 recruit_overview 的 view）
        "pipeline_overview", "recruit_overview", "list_recruit_plans",
        # 办公：名单下载
        "export_roster",
        # 写（只出提案）
        "set_stage", "set_sign_status", "bulk_initial_pass",
        "bulk_assign_job", "assign_job", "suggest_job", "route_pending",
        "archive_by_year", "set_interview_info", "get_interview_dict", "manage_recruit_plan",
        "import_recruit_plan_excel", "manage_interview", "manage_plan",
        "manage_candidate",
    ],
    "mail": [
        "mailbox_status", "list_mail_templates", "get_mail_template",
        "preview_mail", "draft_email", "get_mail_config", "save_mail_config",
        "manage_mail_template", "manage_mail",
    ],
    "govern": [
        "list_proposals", "list_audit", "list_import_history", "list_agent_runs",
        "run_proactive_scan", "rebuild_brief", "clear_chat_history",
        "list_removed_files", "restore_source_files", "remove_source_files",
        "get_feedback_report", "export_feedback", "list_conversations",
        "restore_chat_history", "truncate_chat", "manage_system",
    ],
    "admin": [
        "create_job", "update_job_jd", "regrade_job", "ingest_resumes",
        "list_import_history", "export_resumes", "list_source_files",
        "get_settings", "save_settings",
        "get_model_config", "save_model_config", "update_interview_dict",
        "route_pending", "compare_channel", "manage_job",
    ],
    "system": [
        # v5.1：账号 / 部门 / 计划 / 岗位启停 / 领域包（此前只有界面能操作）
        "list_users", "manage_user",
        "list_departments", "manage_department",
        "list_recruit_plans", "manage_recruit_plan",
        "set_job_active", "update_job_info", "import_recruit_plan_excel",
        "list_domain_packs", "import_domain_pack",
        "manage_job", "manage_plan", "manage_system",
    ],
    "office": [
        "export_roster", "export_resume_bundle", "export_interview_schedule",
        "recruit_overview",
        "search_candidates", "get_candidates", "list_jobs", "pool_stats",
    ],
}

#: 已被统一入口取代、不再暴露给模型的旧工具。
#: 它们仍保留在 `TOOL_SPECS` / `execute()` 里，旧脚本与老提示词不会断；
#: 但模型本轮看不到，避免 4 个搜索工具、6 个招聘读工具互相抢选择。
HIDDEN_TOOLS = {
    "search_by_skills", "semantic_search", "similar_candidates",
    "recruit_progress", "recruit_gaps", "recruit_roster",
    "recruit_signed_list", "recruit_health", "recruit_years",
    # 用户明确要 Excel；.ics 保留执行层兼容，但不再让模型优先选
    "export_interview_ics",
    # 旧版只给“去页面下载”的指引；新版 export_resume_bundle 才真的给下载链接
    "export_resumes",
}
# 旧写工具全部保留在 execute() 中做兼容，但不再直接暴露给模型；
# 模型统一走 6 个 manage_* action 入口，减少工具选择噪声。
HIDDEN_TOOLS |= (WRITE_TOOLS - MANAGE_TOOL_NAMES)

#: 关键词 → 组。中文按**实际说法**列，不要求穷尽；命中一个词即可。
#: 读/写不在这里区分——写工具即便被路由进来，也只能生成待确认提案。
KEYWORDS: dict[str, tuple[str, ...]] = {
    "profile": (
        "简历", "档案", "候选人", "查人", "找人", "这个人", "那个人", "谁",
        "技能", "会什么", "会哪些", "会做", "会用",
        "掌握", "具备", "做过", "经历", "项目",
        "对比", "比较", "适合", "匹配", "分析", "面试提纲", "学校", "院校",
        "专业", "学历", "电话", "邮箱", "标签", "备注", "复核", "标记",
        "合并", "拆分", "改档", "档位", "字段", "头像", "通道对比",
        "模型规则对比", "规则通道", "通道", "原件", "正文", "邮件正文", "证书",
    ),
    "recruit": (
        "招聘", "进度", "缺口", "计划", "名额", "签约", "已签", "拒签",
        "毁约", "完成率", "届别", "本届", "上届", "哪届", "哪些单位", "各单位",
        "单位缺口", "单位分布", "单位名额", "录用单位", "面试单位", "分布", "人数", "统计", "面试名单",
        "待面试", "offer", "录用", "入职", "归岗", "岗位", "推进", "阶段",
        "初筛", "面试", "上会", "审批", "应招", "转上年度",
    ),
    "mail": (
        "邮件", "发信", "写信", "通知", "邀请", "邀约", "婉拒", "模板",
        "草稿", "收件", "收信", "收简历", "新邮件", "附件", "smtp", "imap", "邮箱",
    ),
    "govern": (
        "提案", "待确认", "审计", "操作记录", "巡检", "扫描", "待办",
        "跟进", "今天要做", "今天先做什么", "摘要", "周报", "恢复", "清理", "删除会话", "运行记录",
        "导入记录", "导入历史", "重复检测", "重复简历", "被判重复", "被标记为重复",
        "哪些重复", "为什么没入库", "没入库", "偏差", "反馈报告", "对话历史",
        "恢复对话", "截断", "会话",
    ),
    "admin": (
        "新建岗位", "jd", "导入", "导出简历", "设置", "配置", "模型",
        "密钥", "字典", "重算", "重新分析", "来源文件", "回收站", "邮箱配置",
    ),
    "system": (
        "账号", "用户", "权限", "部门", "启用岗位", "停用岗位", "岗位启停",
        "启用", "停用", "领域包", "导入领域包", "技能本体",
        "计划", "名额", "招聘计划", "届别", "岗位名称", "招聘数量", "所属部门",
        "修改岗位", "改岗位", "岗位信息", "Excel导入", "计划导入", "导入计划",
    ),
    "office": (
        "导出", "excel", "表格", "名单", "csv", "下载", "报表", "打印", "日程",
    ),
}

#: 默认（没有命中任何组）：覆盖面最广的一组读工具，避免"问什么都缺工具"
DEFAULT_NAMES = CORE_MIN + [
    "analyze_fit", "explain_grade", "pipeline_overview", "recruit_overview",
]


def _all_specs() -> dict[str, dict]:
    return {s["function"]["name"]: s for s in TOOL_SPECS}


def missing_tools() -> list[str]:
    """自检：分组里写了但 `TOOL_SPECS` 不存在的工具名。"""
    have = set(_all_specs())
    out: list[str] = []
    for names in GROUPS.values():
        for n in names:
            if n not in have and n not in out:
                out.append(n)
    return out


def route_tools(message: str, history: list[dict] | None = None,
                max_tools: int = 30) -> tuple[list[dict], list[str]]:
    """返回 `(本轮工具 specs, 命中的组名)`。

    `history` 只取最近两条用户消息，用于"那这个岗位呢"这类省略句；
    不把整段对话拼进来，避免历史关键词把当前意图带偏。
    """
    text = str(message or "")
    users = [h.get("content") or "" for h in (history or [])
             if h.get("role") == "user"]
    if users:
        text += "\n" + "\n".join(users[-2:])
    low = text.lower()

    scores: dict[str, int] = {}
    for group, words in KEYWORDS.items():
        n = sum(1 for w in words if w.lower() in low)
        if n:
            scores[group] = n
    # 「2027届」这类届别说法没有固定关键词，单独加分；否则只问届别时会漏掉
    # 招聘域，退到默认读工具集。
    if re.search(r"\d{4}\s*届", text):
        scores["recruit"] = scores.get("recruit", 0) + 2
    groups = [g for g, _ in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))]

    if not groups:
        names = list(DEFAULT_NAMES)
    else:
        primary = groups[0]
        names = [n for n in (list(CORE_MIN) + list(GROUPS.get(primary, [])))
                 if n not in HIDDEN_TOOLS]
        # 次组只补**读/算**工具。次组里的写工具通常不是当前问题的主意图，
        # 全部塞进来只会推高上下文；主组才保留完整读写能力。
        for g in groups[1:]:
            extra = [n for n in GROUPS.get(g, [])
                     if n not in names and n not in HIDDEN_TOOLS
                     and (n in READ_TOOLS or n in COMPUTE_TOOLS)]
            if len(names) + len(extra) <= max_tools:
                names.extend(extra)

    # 去重 + 保持 TOOL_SPECS 的声明顺序（模型看到的顺序稳定）
    wanted = set(names)
    specs = [s for s in TOOL_SPECS if s["function"]["name"] in wanted]
    if len(specs) > max_tools:
        core = [s for s in specs if s["function"]["name"] in CORE_MIN]
        rest = [s for s in specs if s["function"]["name"] not in CORE_MIN]
        specs = (core + rest)[:max_tools]
    return specs, ["core"] + groups


def tool_catalog_text(specs: list[dict]) -> str:
    """给"不支持原生 function calling"的模型看的紧凑工具清单。

    只列本轮路由到的工具，不列全部工具；参数只写名字和类型，详细约束仍由
    `execute()` 在服务端校验。模型按下面的 XML 输出，`loop._text_tool_calls`
    会把它解析成真正的工具调用。
    """
    lines = [
        "【工具调用格式】当前端点不支持原生 function calling。",
        "需要调用工具时，**只输出**下面这种 XML，不要解释、不要用 Markdown 代码块：",
        "<tool_call><function=工具名><parameter=参数名>值</parameter></function></tool_call>",
        "一次可以输出多个 <tool_call>。参数值按 JSON 写法（数字/布尔/数组直接写）。",
        "可用工具：",
    ]
    for s in specs:
        fn = s["function"]
        props = (fn.get("parameters") or {}).get("properties") or {}
        args = ", ".join(f"{k}:{v.get('type', 'string')}" for k, v in props.items())
        desc = str(fn.get("description") or "").replace("\n", " ")[:180]
        lines.append(f"- {fn['name']}({args}): {desc}")
    return "\n".join(lines)
