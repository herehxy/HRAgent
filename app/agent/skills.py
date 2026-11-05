"""业务 SOP（skill）注册表。

**tool 与 skill 的区别**：
  · tool  = 一个原子动作（读档案、改阶段、导名单），有权限、审计、参数校验；
  · skill = 一段稳定的办事流程（周报、批量初筛、面试安排），规定先查什么、
            再怎么判断、最后输出成什么形态。

为什么需要：HR 的高频任务大多是**固定套路**。把这些套路写进系统提示词，
模型每轮都要重新"悟"一遍，既慢又容易漏步骤。做成 skill 后：
  1. 命中关键词就注入一段确定的 SOP（不额外调模型，不增加延迟）；
  2. 自动把该 SOP 需要的工具补进本轮工具集；
  3. 输出格式固定（表格/图表/提案），前端也能显示"按 SOP：招聘周报"。

skill **不绕过**任何现有约束：写操作仍然只生成待确认提案，权限仍由
`execute()` 校验。它只是把"老练 HR 的做法"提前告诉模型。

以后要加新 SOP，只在这个文件里加一条；不要再去改 `SYSTEM_PROMPT` 的长文本。
"""
from __future__ import annotations

#: 每个 skill 的字段：
#:   id / name / desc       —— 标识与展示名
#:   strong                 —— 命中任意一个就直接启用（意图很明确）
#:   triggers               —— 命中 2 个及以上才启用（避免"面试"这种泛词误触发）
#:   tools                  —— 这条 SOP 需要补进本轮的模型可见工具
#:   sop                    —— 注入 system 的步骤说明
#:   output                 —— 输出形态要求
SKILLS: list[dict] = [
    {
        "id": "weekly_recruit_report",
        "name": "招聘周报",
        "desc": "汇总招聘进度、缺口、学历分布，生成表格与图表",
        "strong": ("招聘周报", "招聘简报", "周报", "汇报材料", "给领导看"),
        "triggers": ("招聘进度", "缺口", "签约", "学历分布", "图表", "汇总", "完成率"),
        "tools": ("pool_stats", "recruit_overview", "search_candidates",
                  "export_roster", "list_jobs"),
        "sop": (
            "1) 先调 pool_stats 取总人数/阶段/档位；\n"
            "2) 调 pool_stats(group_by=education) 取学历分布；\n"
            "3) 调 recruit_overview(view=progress) 取应招/已签/缺口，"
            "view=gaps 取缺口最大的单位；\n"
            "4) 用表格汇总关键数字，再用 ```mermaid 画一张饼图或折线图；\n"
            "5) 用户明确要文件时再调 export_roster 生成下载链接。"
        ),
        "output": "结论先行；表格 + 图表；每个数字必须来自本轮工具结果。",
    },
    {
        "id": "one_sentence_batch",
        "name": "一句话批量任务",
        "desc": "一句话里包含多个动作时，拆步骤、全部执行、最后一次性确认",
        "strong": ("一句话", "批量任务", "一次搞定", "一起处理", "同时完成",
                   "并生成", "顺便", "帮我一次性"),
        "triggers": ("批量", "归岗", "初筛", "面试安排", "导出", "草稿",
                     "同时", "一起", "并且", "然后"),
        "tools": ("search_candidates", "get_candidates", "analyze_fit",
                  "bulk_initial_pass", "bulk_assign_job", "set_stage",
                  "set_sign_status", "assign_job", "export_roster",
                  "export_interview_schedule", "export_resume_bundle", "draft_email", "list_jobs"),
        "sop": (
            "1) 先把用户这句话里的**所有动作**拆成编号步骤，不要只做第一个；\n"
            "2) 先完成所有读操作/筛选（search_candidates、get_candidates）；\n"
            "3) 每个写动作分别调用对应工具生成待确认提案："
            "批量推进用 bulk_initial_pass，批量归岗用 bulk_assign_job，"
            "单人阶段用 set_stage，签约用 set_sign_status；\n"
            "4) 文件/邮件动作分别调用 export_roster / export_resume_bundle / "
            "export_interview_schedule / draft_email；\n"
            "5) 最后按步骤汇总结果，把所有待确认提案集中列出——"
            "HR 可以点一次「全部同意」全部执行。"
        ),
        "output": "按步骤编号汇报做了什么；写操作全部落在待确认提案里；不要漏动作、不要只做第一步。",
    },
    {
        "id": "bulk_screening",
        "name": "批量初筛/归岗",
        "desc": "按条件筛出一批人，批量推进初筛或归岗",
        "strong": ("批量初筛", "批量归岗", "筛一批", "这批人", "批量处理"),
        "triggers": ("初筛", "归岗", "A档", "简历", "候选人", "推进"),
        "tools": ("search_candidates", "get_candidates", "analyze_fit",
                  "bulk_initial_pass", "bulk_assign_job", "set_tier", "list_jobs"),
        "sop": (
            "1) 先用 search_candidates 一次筛出目标人群（不要逐个查）；\n"
            "2) 需要看多人详情时用 get_candidates 批量读取，不要连续 get_candidate；\n"
            "3) 先汇报人数、档位、岗位分布和风险，再询问是否提交批量提案；\n"
            "4) 用户确认后用 bulk_initial_pass / bulk_assign_job 生成待确认提案。"
        ),
        "output": "先给影响面（多少人、哪些岗位），再给待确认提案；不要自动执行。",
    },
    {
        "id": "interview_scheduling",
        "name": "面试安排",
        "desc": "确定面试人、单位、时间，起草面试邀请并推进阶段",
        "strong": ("安排面试", "发面试邀请", "面试通知", "约面试"),
        "triggers": ("面试", "邀请", "邮件", "时间", "单位", "面试官", "安排"),
        "tools": ("get_candidate", "get_candidates", "list_jobs", "draft_email",
                  "set_stage", "get_interview_dict", "export_interview_schedule"),
        "sop": (
            "1) 先确认候选人、岗位、面试单位、日期、时段、方式；缺什么问什么；\n"
            "2) 用 get_candidate/get_candidates 取档案与联系方式；\n"
            "3) 用 draft_email（面试邀请模板）生成草稿，正文只给摘要，不要把 HTML 贴进回复；\n"
            "4) 用户确认草稿后，再用 set_stage 生成推进到「待面试」的待确认提案。"
        ),
        "output": "逐项列出已确认/仍缺的信息；邮件只出草稿卡，发送仍需人工确认。",
    },
    {
        "id": "offer_followup",
        "name": "签约跟进",
        "desc": "看谁还没签约、登记签约/拒签/毁约",
        "strong": ("签约跟进", "谁还没签约", "签约情况", "毁约"),
        "triggers": ("签约", "offer", "录用", "入职", "拒签", "已签"),
        "tools": ("recruit_overview", "get_candidates", "set_sign_status",
                  "export_roster"),
        "sop": (
            "1) 先用 recruit_overview(view=signed) 看已签名单，view=roster 看待签约/已录用名单；\n"
            "2) 用 get_candidates 批量核对重点人的单位、签约日期、入职期限；\n"
            "3) 需要登记时用 set_sign_status 生成待确认提案，不要直接改库；\n"
            "4) 用户要名单时用 export_roster 生成下载链接。"
        ),
        "output": "按单位/学历汇总已签与缺口；提案逐条列出，等人确认。",
    },
    {
        "id": "data_health_check",
        "name": "数据体检",
        "desc": "找出缺字段、异常、冲突的数据并给出修正建议",
        "strong": ("数据体检", "数据质量", "异常数据", "数据检查"),
        "triggers": ("缺失", "异常", "冲突", "体检", "补全", "修正"),
        "tools": ("recruit_overview", "search_candidates", "get_candidates",
                  "update_candidate_fields", "set_sign_status"),
        "sop": (
            "1) 先调 recruit_overview(view=health) 取招聘数据体检结果；\n"
            "2) 再按需用 search_candidates 找缺字段/待归岗的人；\n"
            "3) 按问题类型分组列出：缺签约时间、缺录用单位、期外签约、签约冲突、单位不在计划；\n"
            "4) 修正类操作一律用 update_candidate_fields / set_sign_status 生成待确认提案。"
        ),
        "output": "按问题类型分组，每条带姓名与可执行的下一步；不要静默修正。",
    },
]


def match_skill(message: str, history: list[dict] | None = None) -> dict | None:
    """按关键词匹配一条 SOP；命中多个时取分数最高的。

    只取最近两条用户消息做上下文，避免历史话题把当前意图带偏。
    没有命中返回 None，走普通 ReAct。
    """
    text = str(message or "")
    users = [h.get("content") or "" for h in (history or [])
             if h.get("role") == "user"]
    if users:
        text += "\n" + "\n".join(users[-2:])
    low = text.lower()

    best, best_score = None, 0
    for skill in SKILLS:
        strong_hit = sum(1 for w in skill["strong"] if w.lower() in low)
        trigger_hit = sum(1 for w in skill["triggers"] if w.lower() in low)
        if not strong_hit and trigger_hit < 2:
            continue
        score = strong_hit * 3 + trigger_hit
        if score > best_score:
            best, best_score = skill, score
    return best
