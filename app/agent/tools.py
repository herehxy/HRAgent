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

from .. import db, stages as stages_mod, tiers as tiers_mod
from .. import search as search_mod
from ..pipeline import normalize as nz
from ..pipeline.analyze import analyze_fit, draft_interview
from ..pipeline.tier import grade, major_match

WRITE_TOOLS = {"set_stage", "set_tier", "add_tag", "merge_candidates", "add_note",
               # v1.16 补齐：这些动作以前只有界面能点，agent 调不到
               "assign_job", "suggest_job", "mark_review",
               "set_archive", "archive_batch", "split_candidate",
               "create_job", "update_job_jd", "regrade_job",
               # v1.25 补齐：配置类写操作（授权码等敏感项）一律走提案+审计
               "save_mail_config", "update_interview_dict", "save_settings",
               "save_model_config", "update_candidate_fields", "reanalyze_candidate",
               "reanalyze_batch", "route_pending", "archive_by_year",
               "restore_source_files", "remove_source_files", "rebuild_brief",
               "clear_chat_history"}
# ingest_resumes / export_resumes **不在** WRITE_TOOLS：它们是直接执行的
# （取数据、给下载指引），标成"写·需确认"会让界面与实际行为不一致。
# run_proactive_scan 也**不在** WRITE_TOOLS：它只建提案、不改业务数据，
# 标 write=True 却直接执行会让界面/审计口径与实际行为分叉。

# 明确拒绝的工具名（不提供实现，但出现在提示里会被拦下并告知用户）
DISABLED_TOOLS = {"send_email", "send_message", "export_external", "delete_candidate",
                  "delete_resume", "reject_candidate", "notify_candidate"}

READ_TOOLS = {"search_candidates", "get_candidate", "pool_stats", "search_by_skills",
              "semantic_search", "similar_candidates", "pipeline_overview",
              "mailbox_status", "list_audit", "list_proposals", "explain_grade",
              "list_jobs",
              # v1.25：邮件模板与配置（读只回掩码，绝不回明文密钥）
              "list_mail_templates", "get_mail_template", "get_mail_config",
              # v1.25：字典 / 来源 / 回收站 / 运行记录
              "get_interview_dict", "list_source_files", "list_removed_files",
              "list_agent_runs",
              # v2.0.7：学校查询（本地）与对外搜索（护栏 + 需配置）
              "school_info", "web_search"}
COMPUTE_TOOLS = {"analyze_fit", "compare_candidates", "draft_interview_questions",
                 "draft_email", "preview_mail"}

#: v1.29：从配置取，不再手抄（原来这里和 agent/proactive.py 各一份，容易只改一边）
STAGES = stages_mod.all_stages()

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
          "按条件筛选候选人（档位/学历/年限/关键词/院校层次/阶段/**归岗岗位**）。返回真实库内数据，找不到就返回空，不要编造。"
          "**keyword 是字面匹配**：院校请用全称（库里存的是『西安电子科技大学』，"
          "传『西电』会返回空——遇到简称先展开成全称再搜）。"
          "**问『某岗位有哪些候选人』用 job 参数**：它按**归岗关系**筛（只返回已归到该岗位下的人），"
          "不是技能匹配——不要用 skill/keyword 拼一个『岗位人选』出来。",
          {"tier": {"type": "string", "description": tiers_mod.enum_str("/") + "/REVIEW/UNCONFIRMED/ALL"},
           "job": {"type": "string",
                   "description": "岗位名称（如『科学研究』）。按**归岗关系**筛选：只返回归到该岗位下的人。"
                                  "岗位名以 list_jobs 的返回为准。"},
           "keyword": {"type": "string", "description": "姓名、院校、专业关键词（院校用全称）"},
           "min_years": {"type": "integer", "description": "最低工作年限"},
           "education": {"type": "string", "description": "学历（v2.0.2 起为**精确匹配**，不是「及以上」；多个用逗号分隔）：大专/本科/硕士/博士。不传=不限"},
           "univ": {"type": "string",
                    "description": "院校层次：985 或 211。用户说『985/211 的人』时**必须用这个参数**，"
                                   "不要用 keyword 去猜校名（名单外会漏）。选 211 时 985 也算。"},
           "skill": {"type": "string", "description": "技能关键词"},
           "stage": {"type": "string", "description": "投递阶段，取值：" + "/".join(STAGES)},
           "limit": {"type": "integer", "description": "最多返回条数，默认 20"}}),
    _spec("get_candidate",
          "取某候选人的完整档案：基本信息、全部投递记录、技能（含原文证据）、标签。",
          {"candidate_id": {"type": "string",
                            "description": "候选人 id 或**姓名**（两种都行，系统自动解析）"}},
           ["candidate_id"]),
    _spec("pool_stats",
          "人才库总览：人数、投递数、各档位分布、各阶段分布、待确认/信息缺失数量。"),
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

    # ------------------------------ 邮件模板与配置（v1.25） ------------------------------
    # 为什么必须单独有这一组：截图里 HR 问「你能看到我系统配置里面的模版吗」，
    # 助手答「我无法直接查看或读取您系统后台配置的内置模版」——
    # **它说的是实话**，因为 draft_email 的参数里根本没有模板。
    # 这不是模型能力问题，是我们没把能力交给它。
    _spec("list_mail_templates",
          "列出系统里配置的**邮件模板**：模板名、适用场景、主题、正文、启用状态。"
          "HR 问『有哪些模板』『有哪些邮件模板』『面试邀请模板长什么样』"
          "或要求**按模板**写邮件时，先调这个工具拿到真实模板，"
          "不要凭印象编模板名。",
          {"enabled_only": {"type": "boolean",
                            "description": "只看启用的，默认 false（全部）"}}),
    _spec("get_mail_template",
          "按模板名或 id 取一个模板的**完整正文**（含 {变量} 占位符）。"
          "用于「把面试邀请模板给我看看」「照 XX 模板改一份」这类要求；"
          "只要名单不要正文用 list_mail_templates 就够。",
          {"template": {"type": "string",
                        "description": "模板名（可模糊，如『面试邀请』）或模板 id"}},
          ["template"]),
    _spec("preview_mail",
          "把某个模板**套在某位候选人身上**渲染出来（变量替换后的真实效果），"
          "返回主题与正文。用于『用面试邀请模板给陈志远生成一封』这类要求，"
          "得到的就是可以直接发给 HR 看的成品草稿（仍然不会自动发送）。"
          "额外参数（面试时间/地点等）可以一起传。",
          {"template": {"type": "string", "description": "模板名或 id"},
           "candidate_id": {"type": "string", "description": "候选人 id 或姓名"},
           "extra_vars": {"type": "object",
                          "description": "要填的运行时变量，如 {\"面试时间\":\"2026-10-20 14:00\"}"}},
          ["template", "candidate_id"]),
    _spec("get_mail_config",
          "查看发信配置（发信账号、SMTP 服务器、端口、加密方式、抓信模式）。"
          "**授权码/密码一律只返回掩码**，不返回明文。"
          "用于回答『发信账号配好了吗』『用的是哪个邮箱』。"),
    _spec("save_mail_config",
          "修改发信配置（发信账号 / SMTP 服务器 / 端口 / 加密方式 / 授权码）。"
          "【需 HR 确认】只生成待确认提案，不会直接改库。"
          "⚠️ 授权码属于密钥，**只在 HR 明确给出时**才传，不要自行猜测或留空覆盖。",
          {"user": {"type": "string", "description": "发信邮箱账号"},
           "host": {"type": "string", "description": "SMTP 服务器，如 smtp.163.com"},
           "port": {"type": "integer", "description": "端口，如 465 / 587"},
           "ssl": {"type": "boolean", "description": "true=SSL(465)，false=STARTTLS(587)"},
           "password": {"type": "string", "description": "授权码/密码（可空=不改）"}}),

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
          "返回主题、正文与收件人，可直接在对话里改或交给编辑器。"
          "**HR 只要提到「用模板 / 套模板 / 按我的模板」，就必须传 template（模板名或 id），"
          "用 list_mail_templates 查到名字后再传——此时忽略 tone/purpose 的自由撰写路径。**"
          "不传 template 才由模型自由撰写（仅用于 HR 没要求模板、且系统模板里也没有合适模板时）。",
          {"candidate_id": {"type": "integer"},
           "purpose": {"type": "string",
                       "description": "面试邀请 / 跟进 / 婉拒 / offer沟通 / 其他（默认按用途猜）；传了 template 时本字段被忽略"},
           "tone": {"type": "string", "description": "语气：客气简洁 / 正式 / 轻松，可空；传了 template 时本字段被忽略"},
           "template": {"type": "string",
                        "description": "【关键】模板名（可模糊）或 id。HR 要求用模板时必须传；传了就按该模板渲染，tone/purpose 不再生效"},
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
    # ------------------------------ 面试字典 / 来源 / 回收站（v1.25） ------------------------------
    _spec("get_interview_dict",
          "读面试字典：面试单位、会议室、时段、联系人（含电话/部门）。"
          "写邮件时要填「面试地点/单位/时段/联系人」就从这里取，**不要自己编会议室名**。"),
    _spec("update_interview_dict",
          "【需 HR 确认】维护面试字典（加/删单位、会议室、时段、联系人）。"
          "只生成待确认提案。**改动会影响之后所有写邮件时的可选项**，"
          "所以 HR 说改哪个就改哪个，不要顺带\"整理\"别的条目。",
          {"kind": {"type": "string",
                    "description": "units / rooms / slots / contacts 之一"},
           "action": {"type": "string", "description": "add 或 remove"},
           "value": {"type": "string",
                     "description": "name / rooms/slots 的字面值；contacts 建议传 {name,phone,dept}"}},
          ["kind", "action", "value"]),
    _spec("list_source_files",
          "列出「待导入简历」目录里的文件（文件名、大小、是否已入库、是否超大被跳过）。"
          "用于回答『还有哪些简历没导』『收件夹里现在有什么』。"),
    _spec("list_removed_files",
          "列出回收站（被移入回收目录的简历）。系统**不做物理删除**，"
          "「删除」只是挪进回收站，随时可恢复。"),
    _spec("remove_source_files",
          "【需 HR 确认】把来源文件移入**回收站**（不是删除，可恢复）。只生成待确认提案。",
          {"names": {"type": "array", "items": {"type": "string"},
                     "description": "文件名列表，先用 list_source_files 看清楚再传"}},
          ["names"]),
    _spec("restore_source_files",
          "【需 HR 确认】把回收站里的简历恢复到来源目录。只生成待确认提案。"
          "同名冲突时系统会拒绝而**不会覆盖**。",
          {"names": {"type": "array", "items": {"type": "string"},
                     "description": "文件名列表，先用 list_removed_files 看清楚再传"}},
          ["names"]),

    # ------------------------------ 字段更正 / 重算 / 批处理（v1.25） ------------------------------
    _spec("update_candidate_fields",
          "【需 HR 确认】更正候选人字段：姓名 / 学历 / 学校 / 专业 / 电话 / 邮箱 / 备注。"
          "只生成待确认提案；HR 确认后落库并记录改前改后（电话/邮箱加密存储）。"
          "备注是 HR 的自由工作记录（沟通印象、内推关系、谈薪情况……），≤500 字，空串表示清空。"
          "**技能与年限不在此处改**——那必须来自简历原文与证据核对。"
          "HR 说『把陈志远的电话改成 138…』『这个人学校识别错了』『给沈亦舟加备注：周四电话沟通』时用这个。",
          {"candidate_id": {"type": "string", "description": "候选人 id 或姓名"},
           "name": {"type": "string"}, "education": {"type": "string",
                     "description": "大专/本科/硕士/博士，留空表示未识别"},
           "school": {"type": "string"}, "major": {"type": "string"},
           "phone": {"type": "string"}, "email": {"type": "string"},
           "note": {"type": "string",
                    "description": "人工备注（≤500 字）：沟通印象/内推关系/谈薪情况等；空串表示清空"}},
          ["candidate_id"]),
    _spec("reanalyze_candidate",
          "【需 HR 确认】对某位候选人**完整重跑分析**（重新解析原件 → 重抽字段 → 重建技能 "
          "→ 重新归岗 → 重判建议档）。只生成待确认提案。"
          "不动三样：HR 定的档、阶段、已归岗岗位；人工更正过的字段也保留。"
          "每人约 6-15 秒，可以随时关页面。",
          {"candidate_id": {"type": "string", "description": "候选人 id 或姓名"}},
          ["candidate_id"]),
    _spec("reanalyze_batch",
          "【需 HR 确认】批量重跑分析。只生成待确认提案。"
          "不传 candidate_ids 则处理所有「走规则通道」的档案（历史上模型不可用时入库的）。",
          {"candidate_ids": {"type": "array", "items": {"type": "integer"},
                             "description": "为空=所有规则通道档案"},
           "limit": {"type": "integer", "description": "最多处理几人，默认 50"}}),
    _spec("route_pending",
          "【需 HR 确认】为「待指定岗位」的投递重新建议岗位并按最适岗位重算建议档。"
          "只生成待确认提案。新建/改了岗位 JD 或批量导入没写岗位名的简历之后用。",
          {"apply": {"type": "boolean", "description": "true=落库，默认 true"}}),
    _spec("archive_by_year",
          "【需 HR 确认】把「某年及以前」的投递整批归档。只生成待确认提案。"
          "适合『新一年开始，把旧简历收起来』——人数太多不可能一个个勾。",
          {"before_year": {"type": "integer", "description": "归档该年及以前的投递"}},
          ["before_year"]),

    # ------------------------------ 系统配置 / 待办 / 对话（v1.25） ------------------------------
    _spec("get_settings",
          "读系统设置与策略：访问策略、性别筛选开关、导入来源配置、数据存放路径等。"
          "**密钥类一律只回掩码**。用于回答『这个开关打开了吗』『数据存在哪』。"),
    _spec("save_settings",
          "【需 HR 确认】修改系统设置。只生成待确认提案。"
          "只改 HR 明确说的那一项，不要顺带调整别的开关。",
          {"settings": {"type": "object",
                        "description": "要改的键值对，如 {\"gender_filter_enabled\": true}"}},
          ["settings"]),
    _spec("get_model_config",
          "读模型配置：模型地址、模型名、是否启用、最大工具轮数。"
          "**API Key 只回报是否已设置，不回明文**。"),
    _spec("save_model_config",
          "【需 HR 确认】修改模型配置（地址 / 模型名 / API Key）。只生成待确认提案。"
          "⚠️ API Key 只在 HR 明确给出时才传；HR 没提就**不要动它**。",
          {"base_url": {"type": "string"}, "model": {"type": "string"},
           "api_key": {"type": "string", "description": "可空=不改动"}}),
    _spec("rebuild_brief",
          "【需 HR 确认】重算「今日待办」摘要。只生成待确认提案。"
          "use_llm=true 会调用模型（较慢，但结论更贴业务）。"),
    _spec("run_proactive_scan",
          "立即跑一次系统巡检：找出长期停滞的投递等，产出**待确认提案**。"
          "巡检本身只建提案、不改任何数据，所以**直接执行**、不需要再确认。"
          "同一件事 7 天内不重复提。"),
    _spec("list_agent_runs",
          "查看智能助手自己的历史运行记录（问过什么、调了哪些工具、耗时与 token 消耗）。"
          "用于回答『你刚才做了什么』『这个月花了多少 token』。"),
    _spec("clear_chat_history",
          "【需 HR 确认】清空智能助手的对话历史（运行记录）。只生成待确认提案。"
          "⚠️ 这只是清对话，**不会动任何候选人数据**。"),

    _spec("set_stage",
          # 阶段枚举由配置拼——写死的话改流程后模型还在推「初面」这种不存在的阶段
          "【需 HR 确认】把某条投递推进到新阶段（" + "/".join(STAGES) + "）。"
          "**结论标签**（已面试→不录用；已发放offer→已签约/拒签）用 `outcome` 传，"
          "其余阶段不要传。"
          "本工具只生成待确认提案，不会直接改库。"
          "目标可以给 **application_id（投递编号）**，"
          "也可以只给 **candidate_id（姓名或编号）** —— 后者会自动找他的投递，"
          "**只有一条时才用**，多条会要求你指明，不会替你挑。"
          "HR 说『把 #N 推进到 X』时，先试投递编号，不行再试人名，不要一上来就反问。",
          {"application_id": {"type": "integer", "description": "投递编号（二选一）"},
           "candidate_id": {"type": "string",
                            "description": "候选人 id 或姓名（二选一）"},
           "stage": {"type": "string", "description": "目标阶段，取值见本工具说明"},
           # v1.29：结论标签。只在「离开的阶段」允许它时才认（见 stages.outcome_allowed_from）：
           # 已面试→不录用；已发放offer→已签约/拒签。不给就是"不改结论"。
           "outcome": {"type": "string",
                       "description": "结论标签（仅部分阶段可用）：不录用 / 已签约 / 拒签"},
           "reason": {"type": "string", "description": "变更理由"}},
          ["stage"]),
    _spec("set_tier",
          "【需 HR 确认】调整某条投递的档位。本工具只生成待确认提案，不会直接改库。"
          f"注意：学历低于岗位门槛的投递，系统已直接判 {tiers_mod.hard_shortfall_tier()}，"
          "不要为它提出上调档位的提案——学历是硬门槛，模型判断不能越过。",
          {"application_id": {"type": "integer"}, "tier": {"type": "string", "description": tiers_mod.enum_str("/")},
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
    # ---- v2.0.7：学校查询与对外搜索 ----
    _spec("school_info",
          "查一所学校的层次与排名：985/211/强势标签、**软科综合排名名次**、校实力 A/B/C/D 档、是否海外。"
          "用户问某校「什么水平 / 什么档次 / 排第几 / 是不是 985」时**必须用这个工具**，"
          "不要凭记忆回答。名单外的学校返回『无排名数据』——这不是学校差，是没数据，照实说。"
          "⚠️ 本工具只有**综合排名**，没有教育部学科评估等级（A+/A-那种），"
          "被问到学科评估要如实说查不到，不要编。",
          {"school": {"type": "string", "description": "学校全称或常用简称（如 西安电子科技大学 / 西电）"}},
          ["school"]),
    _spec("web_search",
          "查**库外的公开信息**（学校排名、学科实力、行业政策等），结果带来源链接。"
          "⚠️ **只允许查学校/专业/行业**：查询里**不得出现任何候选人姓名、电话、邮箱**，"
          "否则会被系统直接拒绝。要了解某人的院校背景，请先用 school_info 查学校本身。"
          "⚠️ 回答时必须**附来源链接**；没有搜索结果就不要编。"
          "未配置搜索服务时本工具会返回『未配置』，此时请如实告诉 HR，不要自己编答案。",
          {"query": {"type": "string",
                     "description": "公开信息类查询，如「西安电子科技大学 计算机 学科评估」。"
                                    "禁止包含人名/联系方式"}},
          ["query"]),
]


# ============================================================
# 辅助
# ============================================================

def _brief(c: dict) -> dict:
    return {
        "candidate_id": c["id"], "name": c.get("name"),
        "education": c.get("edu_level"), "years": c.get("years_exp"),
        "school": c.get("school"), "major": c.get("major"),
        # 院校层次要如实带给助手，否则它没法回答"这些人是 985 吗"
        "univ_tier": c.get("uni_tier"),
        "tier_suggested": c.get("tier_suggested"), "tier_final": c.get("tier_final"),
        "tier": c.get("tier_effective"), "score": c.get("score"),
        "stage": c.get("stage"), "confirmed": (c.get("app_status") or "") == "已确认",
        # v1.29.8（业务方 2026-10-16 截图）：把**归岗岗位**带进每条结果里。
        # 之前不带 → 模型把"技能匹配的人"当成"某岗位的人"（有人明明归岗到
        # 工艺技术，却被列进科学研究名单，还编了"缓存问题"来解释）。数据打底，
        # 模型就不用猜"这个人属于哪个岗位"。
        "job_title": c.get("job_title") or "",
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
    """把候选人姓名（字符串）或数字 id 解析成候选人 id。**不支持时返回 (None, 错误信息)**。

    为什么要有（v1.13.9）：HR 说「给张三发邮件」，模型为了调工具只能先猜一个
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


def _resolve_job_id(conn, key: str) -> tuple[int | None, str | None]:
    """把岗位名解析成 job_id（v1.29.8，给 search_candidates 的 job 参数用）。

    **只认唯一命中**——猜错岗位会把"岗位归属"答成另一个岗位，比说
    "没找到这个岗位"严重得多（有人被技能匹配塞进别的岗位名单，就是这类错）。
    顺序：全称精确（允许带/不带「岗位/职位」后缀）→ 唯一包含匹配；
    多命中或零命中都返回错误，并把当前岗位名列表一并给出（免得模型自己编名字）。
    """
    key = re.sub(r"(岗位|职位)$", "", (key or "").strip()).strip()
    if not key:
        return None, None
    jobs = db.list_jobs(conn, include_inactive=True)
    hits = [j for j in jobs if (j.get("title") or "").strip() == key]
    if not hits:
        hits = [j for j in jobs
                if key in (j.get("title") or "") or (j.get("title") or "") in key]
    if len(hits) == 1:
        return int(hits[0]["id"]), None
    if len(hits) > 1:
        act = [j for j in hits if j.get("active", 1)]
        if len(act) == 1:
            return int(act[0]["id"]), None
        names = "、".join((j.get("title") or "") for j in hits)
        return None, f"岗位名「{key}」对上了多个岗位（{names}），请用完整岗位名再试"
    titles = "、".join((j.get("title") or "") for j in jobs if j.get("active", 1))
    return None, f"没有名为「{key}」的岗位。当前岗位：" + (titles or "（暂无在招岗位）")


def _find_template(conn, key: str) -> dict | None:
    """按 id 或模板名找模板。名字支持**模糊匹配**（HR 会说「面试邀请」，
    库里可能叫「初面邀约-标准版」），但只在唯一命中时才认——

    为什么要求唯一：选错模板会让 HR 看到一封**内容完全不对的**草稿，
    而他还以为这就是他要的。宁可回一句"有这几个，你要哪个"。
    """
    key = (key or "").strip()
    if not key:
        return None
    if key.isdigit():
        return db.get_mail_template(conn, int(key))
    all_t = db.list_mail_templates(conn)
    exact = [t for t in all_t if (t.get("name") or "").strip() == key]
    if len(exact) == 1:
        return exact[0]
    part = [t for t in all_t
            if key in (t.get("name") or "") or key in (t.get("scene") or "")]
    if len(part) == 1:
        return part[0]
    return None


#: 模板正文里的 `{变量}` 占位符。与 mail_template._VAR_RE 同口径，
#: 单独写一份是为了不把私有正则带出来（那边没导出）。
_TPL_VAR_RE = re.compile(r"\{([^{}\n]{1,15})\}")


def _read_mail_cfg(ctx) -> dict:
    """读发信配置。tools 层不 import server（循环依赖），自己按仓库根读 config。"""
    import json as _json
    import os as _os
    from ..paths import CONFIG_DIR

    for rel in ("config/mail.json", "config/mailbox.json"):
        p = _os.path.join(CONFIG_DIR, _os.path.basename(rel))
        if _os.path.exists(p):
            try:
                with open(p, encoding="utf-8") as fh:
                    return _json.load(fh) or {}
            except Exception:
                continue
    return {}


def _repo_config_path(*rel: str) -> str:
    """定位仓库根的 config/ 下某个文件。

    tools 层**不 import server**（server → tools → server 会循环依赖），
    所以自己按 __file__ 推路径 —— 与 server 的 BASE 算法同源
    （app/agent/tools.py 上溯三层 = 仓库根）。
    """
    import os as _os
    from ..paths import CONFIG_DIR

    return _os.path.join(CONFIG_DIR, *rel)


def _read_json_or(path: str, default):
    import json as _json
    try:
        with open(path, encoding="utf-8") as fh:
            return _json.load(fh)
    except (OSError, ValueError):
        return default


def _read_interview_dict() -> dict:
    """读面试字典（config/interview.json）。读不到就回内置兜底。"""
    d = _read_json_or(_repo_config_path("interview.json"), None)
    if not isinstance(d, dict):
        d = {}
    fallback = {
        "modes": ["现场面试", "线上面试"],
        "slots": ["09:00-10:00", "10:00-11:00", "14:00-15:00", "15:00-16:00"],
        "rooms": ["创新大楼1519会议室"],
        "units": ["人事处", "财务处", "数字化中心"],
        "contacts": [{"dept": "人力资源部", "name": "张老师", "phone": "029-8888XXXX"}],
    }
    for k, v in fallback.items():
        if not d.get(k):
            d[k] = v
    return d


def _data_dir() -> str:
    """数据目录。优先认 TP_DATA_DIR / TP_HOME（与 paths.py 同口径）。"""
    import os as _os
    env = _os.environ.get("TP_DATA_DIR")
    if env:
        return env
    home = _os.environ.get("TP_HOME")
    if home:
        return _os.path.join(home, "data")
    base = _os.path.dirname(_os.path.dirname(_os.path.dirname(
        _os.path.abspath(__file__))))
    return _os.path.join(base, "data")


def _source_files_snapshot(conn, ctx) -> dict:
    """来源目录里的文件清单（待导入的简历）。

    与界面 `/api/sources` 同口径：只列支持的扩展名，标出超大被跳过的。
    """
    import os as _os
    try:
        from ..pipeline import parse as _parse
        supported = tuple(_parse.SUPPORTED)
    except Exception:
        supported = (".pdf", ".docx", ".doc", ".png", ".jpg", ".jpeg", ".txt")
    cfg = _read_json_or(_repo_config_path("mailbox.json"), {}) or {}
    folder = cfg.get("folder_dir") or _os.path.join(_data_dir(), "resumes")
    abs_folder = _os.path.abspath(_os.path.expanduser(folder))
    files: list[dict] = []
    if _os.path.isdir(abs_folder):
        limit_mb = int(cfg.get("max_attachment_mb", 20) or 20)
        for nm in sorted(_os.listdir(abs_folder)):
            full = _os.path.join(abs_folder, nm)
            if not _os.path.isfile(full) or nm.startswith("."):
                continue
            if not nm.lower().endswith(supported):
                continue
            st = _os.stat(full)
            files.append({"name": nm, "size": st.st_size,
                          "oversize": st.st_size > limit_mb * 1024 * 1024})
    return {"folder": abs_folder, "count": len(files), "files": files,
            "max_attachment_mb": int(cfg.get("max_attachment_mb", 20) or 20),
            "note": ("这些是**还没导入**的简历；导入用 ingest_resumes(source='folder')。"
                     if files else "来源目录里没有待导入的简历文件。")}


def _removed_month_dir():
    import os as _os
    from datetime import datetime as _dt
    return _os.path.join(_data_dir(), "removed", _dt.now().strftime("%Y-%m"))


def _removed_files_snapshot() -> dict:
    """回收站清单。系统不做物理删除，「删除」只是挪进这里。"""
    import os as _os
    from datetime import datetime as _dt
    root = _os.path.join(_data_dir(), "removed")
    items: list[dict] = []
    if _os.path.isdir(root):
        for month in sorted(_os.listdir(root), reverse=True):
            mdir = _os.path.join(root, month)
            if not _os.path.isdir(mdir) or month.startswith("."):
                continue
            for nm in sorted(_os.listdir(mdir)):
                full = _os.path.join(mdir, nm)
                if not _os.path.isfile(full) or nm.startswith("."):
                    continue
                st = _os.stat(full)
                items.append({"name": nm, "size": st.st_size, "month": month,
                              "removed_at": _dt.fromtimestamp(
                                  st.st_mtime).strftime("%Y-%m-%d %H:%M")})
    return {"count": len(items), "items": items,
            "note": ("这些文件没有丢，只是被移出了来源目录，可以恢复。"
                     if items else "回收站是空的。"),
            "keep_note": "回收**不会覆盖同名文件**——冲突时会跳过并告诉你。"}


def _read_settings_safe() -> dict:
    """读系统设置与策略。**密钥类一律脱敏。**"""
    import os as _os
    base = _os.path.dirname(_os.path.dirname(_os.path.dirname(
        _os.path.abspath(__file__))))
    out: dict = {}
    pol = _read_json_or(_os.path.join(base, "config", "policy.json"), {})
    if isinstance(pol, dict):
        out["policy"] = pol
    tiers = _read_json_or(_os.path.join(base, "config", "tiers.json"), {})
    if isinstance(tiers, dict):
        out["tiers"] = tiers
    mb = _read_json_or(_os.path.join(base, "config", "mailbox.json"), {})
    if isinstance(mb, dict):
        out["mailbox"] = {k: v for k, v in mb.items()
                          if k.lower() not in ("password", "pass", "secret",
                                               "auth_code", "imap_secret")}
        out["mailbox"]["password_set"] = bool(
            mb.get("password") or mb.get("pass") or mb.get("imap_secret"))
    out["data_dir"] = _data_dir()
    out["paths"] = {"db": _os.path.join(_data_dir(), "workbench.db"),
                    "resumes": _os.path.join(_data_dir(), "resumes"),
                    "archive": _os.path.join(_data_dir(), "archive"),
                    "removed": _os.path.join(_data_dir(), "removed")}
    out["secret_note"] = "密钥只回报是否已设置，不返回明文。"
    return out


def _read_model_cfg_safe() -> dict:
    """读模型配置。**API Key 只回报是否已设置。**"""
    c = _read_json_or(_repo_config_path("model.json"), {}) or {}
    safe = {k: v for k, v in c.items()
            if k.lower() not in ("api_key", "apikey", "key", "token")}
    has_key = bool(c.get("api_key") or c.get("apikey"))
    safe["api_key_set"] = has_key
    return {"config": safe, "base_url": c.get("base_url"), "model": c.get("model"),
            "enabled": c.get("enabled", True),
            "max_tool_rounds": c.get("max_tool_rounds"),
            "api_key_set": has_key,
            "api_key": ("（已设置）" if has_key else "（未设置）"),
            "note": "API Key 不返回明文；要改请用 save_model_config（需 HR 确认）。"}


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
            # v1.29.8：「岗位」筛选按**归岗关系**（applications.job_id），
            # 不是技能/关键词字面匹配——HR 说"某岗位的人"指的是归岗，不是"技能像"。
            # 显式传的 job 优先；没传时沿用会话上下文里的岗位（ctx.job_id）。
            job_key = (args.get("job") or "").strip()
            job_id = ctx.job_id
            if job_key:
                job_id, _jerr = _resolve_job_id(conn, job_key)
                if _jerr:
                    return _err(_jerr)
            items = db.list_candidates(
                conn, tier=args.get("tier"), keyword=args.get("keyword"),
                skill=skill_terms, min_years=args.get("min_years"),
                education=args.get("education"), stage=args.get("stage"),
                univ=args.get("univ"),
                job_id=job_id,
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
            # v1.24.4 修：三处未定义名（candidate_id / error / hint）——
            # 调用「找相似的人」这个工具必然抛 NameError，工具等于不可用。
            # 与同文件 get_candidate 的写法保持一致。
            _cid_s, _cerr_s = _resolve_cid(conn, args.get("candidate_id"))
            if _cerr_s:
                return _err(_cerr_s["error"], _cerr_s.get("hint"))
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
            cid, cerr = _resolve_cid(conn, args.get("candidate_id"))
            if cerr:
                return _err(cerr["error"], cerr.get("hint"))
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
            if _stored == tiers_mod.hard_shortfall_tier() or _rule == tiers_mod.hard_shortfall_tier():
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

        # ---------------- 邮件模板与配置（v1.25） ----------------
        # 设计取舍：模板是**读**操作，直接返回正文，不做提案。
        # 因为读模板不改变任何状态，走确认反而是骚扰。
        if name == "list_mail_templates":
            from .. import mail_template as _mt
            only = bool(args.get("enabled_only"))
            tpls = db.list_mail_templates(conn, enabled_only=only)
            return _ok({
                "count": len(tpls),
                "items": [{"id": t["id"], "name": t["name"], "scene": t.get("scene") or "",
                           "subject": t.get("subject") or "",
                           "enabled": bool(t.get("enabled")),
                           # 正文给个开头就够指认，要全文用 get_mail_template
                           "body_preview": (t.get("body") or "")[:120]} for t in tpls],
                "available_scenes": _mt.SCENES,
                "scenes_note": "scene 是系统内置场景分类，不是模板名",
                "note": ("要正文用 get_mail_template；要**套在某位候选人身上**生成成品"
                         "用 preview_mail 或 draft_email(template=...)。"
                         if tpls else
                         "系统里还没有任何邮件模板。HR 可在「系统配置 → 模板管理」新建；"
                         "这也是为什么我无法按模板写——目前无模板可用。"),
            })

        if name == "get_mail_template":
            key = str(args.get("template") or "").strip()
            if not key:
                return _err("要指定模板名或 id")
            t = _find_template(conn, key)
            if not t:
                names = [x["name"] for x in db.list_mail_templates(conn)]
                return _err(f"没有找到叫「{key}」的模板",
                            hint=("现有模板：" + "、".join(names)) if names
                            else "系统里目前没有任何邮件模板。")
            return _ok({
                "id": t["id"], "name": t["name"], "scene": t.get("scene") or "",
                "subject": t.get("subject") or "", "body": t.get("body") or "",
                "enabled": bool(t.get("enabled")),
                "placeholders": sorted(set(_TPL_VAR_RE.findall(t.get("body") or ""))),
            })

        if name == "preview_mail":
            from .. import auth as _auth
            from .. import mail_template as _mt
            t = _find_template(conn, str(args.get("template") or "").strip())
            if not t:
                return _err("没有找到该模板，先用 list_mail_templates 看有哪些")
            cid, _cerr = _resolve_cid(conn, args.get("candidate_id"))
            if _cerr:
                return _err(_cerr["error"], _cerr.get("hint"))
            d = db.candidate_detail(conn, cid)
            if not d:
                return _err(f"未找到候选人 #{cid}")
            cand = _auth.present_candidate(d)
            _jd, meta = db.resolve_candidate_job(conn, d)
            rt = dict(args.get("extra_vars") or {})
            r = _mt.render(t.get("body") or "",
                           _mt.build_ctx(cand, (meta or {}).get("title") or "",
                                         ctx.operator, rt))
            subj = _mt.render(t.get("subject") or "",
                              _mt.build_ctx(cand, (meta or {}).get("title") or "",
                                            ctx.operator, rt)).get("text", "")
            body = r.get("text") or ""
            return _ok({
                "candidate_id": cid, "name": cand.get("name"),
                "to": (cand.get("email") or "").strip(),
                "template": t["name"],
                "subject": subj, "body": body,
                "body_html": (body if _mt.looks_like_html(body) else _mt.to_html(body)),
                "missing_runtime": r.get("missing") or [],
                "note": "**这是按模板渲染的草稿，没有发送。**要改内容可以直接说，"
                        "或点「在编辑器中修改」。",
            })

        if name == "get_mail_config":
            from .. import mail_send as _ms
            c = _ms.load_cfg() if hasattr(_ms, "load_cfg") else {}
            if not c:
                c = _read_mail_cfg(ctx)
            safe = {k: v for k, v in (c or {}).items()
                    if k.lower() not in ("password", "pass", "secret", "auth_code")}
            has_pw = bool((c or {}).get("password") or (c or {}).get("pass"))
            safe["password_set"] = has_pw
            return _ok({
                "config": safe,
                "password_set": has_pw,
                "note": "授权码/密码**只回报是否已设置，不返回明文**（安全口径）。",
            })

        if name == "save_mail_config":
            from .. import mail_send as _ms
            cur = (_ms.load_cfg() if hasattr(_ms, "load_cfg") else {}) or _read_mail_cfg(ctx)
            new = dict(cur or {})
            for k in ("user", "host", "port", "ssl"):
                if args.get(k) is not None:
                    new[k] = args[k]
            if args.get("password"):
                new["password"] = args["password"]
            changed = [k for k in ("user", "host", "port", "ssl", "password")
                       if str((cur or {}).get(k)) != str(new.get(k))]
            if not changed:
                return _err("没有要修改的配置项")
            shown = {k: ("（已设置，新值不回显）" if k == "password" else new.get(k))
                     for k in changed}
            return _propose(conn, ctx, "save_mail_config",
                            {"config": new},
                            "修改发信配置：" + "、".join(
                                f"{k}={v}" for k, v in shown.items()),
                            risk="高")

        # ---------------- 面试字典 / 来源 / 回收站（v1.25） ----------------
        if name == "get_interview_dict":
            return _ok({"dict": _read_interview_dict(),
                        "note": "写邮件要填的面试地点/单位/时段/联系人都从这里取。"})

        if name == "update_interview_dict":
            kind = str(args.get("kind") or "").strip()
            action = str(args.get("action") or "add").strip()
            value = args.get("value")
            if kind not in ("units", "rooms", "slots", "contacts", "modes"):
                return _err("kind 只能是 units / rooms / slots / contacts / modes")
            if action not in ("add", "remove"):
                return _err("action 只能是 add 或 remove")
            cur = _read_interview_dict()
            before = list(cur.get(kind) or [])
            if action == "add":
                if isinstance(value, str) and value.strip() in \
                        [str(x) if not isinstance(x, dict) else x.get("name") for x in before]:
                    return _err(f"「{value}」已经在{kind}里了，不用重复添加")
            else:
                if isinstance(value, str):
                    hit = [x for x in before
                           if (x if isinstance(x, str) else x.get("name")) == value]
                    if not hit:
                        return _err(f"「{value}」不在{kind}里",
                                    hint="先用 get_interview_dict 看看现有条目")
            after = list(before)
            if action == "add":
                after.append(value)
            else:
                after = [x for x in after
                         if (x if isinstance(x, str) else x.get("name")) != value]
            _CN = {"units": "面试单位", "rooms": "会议室", "slots": "时段",
                   "contacts": "联系人", "modes": "面试方式"}
            verb = "添加" if action == "add" else "删除"
            return _propose(conn, ctx, "update_interview_dict",
                            {"interview_dict": {kind: after}},
                            f"{verb}{_CN[kind]}：{value}"
                            f"（原来 {len(before)} 项 → 改后 {len(after)} 项）",
                            risk="中")

        if name == "list_source_files":
            return _ok(_source_files_snapshot(conn, ctx))

        if name == "list_removed_files":
            return _ok(_removed_files_snapshot())

        if name == "remove_source_files":
            names = [str(x) for x in (args.get("names") or []) if str(x).strip()]
            if not names:
                return _err("没有指定要移入回收站的文件名")
            have = {f["name"] for f in _source_files_snapshot(conn, ctx).get("files", [])}
            unknown = [n for n in names if n not in have]
            if unknown:
                return _err("这些文件不在来源目录里：" + "、".join(unknown[:5]),
                            hint="先用 list_source_files 核对文件名（要写全名含扩展名）")
            return _propose(conn, ctx, "remove_source_files", {"names": names},
                            "把 %d 个来源文件移入回收站（可恢复）：%s"
                            % (len(names), "、".join(names[:5])
                               + ("…" if len(names) > 5 else "")),
                            risk="中")

        if name == "restore_source_files":
            names = [str(x) for x in (args.get("names") or []) if str(x).strip()]
            if not names:
                return _err("没有指定要恢复的文件名")
            have = {f["name"] for f in _removed_files_snapshot().get("items", [])}
            unknown = [n for n in names if n not in have]
            if unknown:
                return _err("这些文件不在回收站里：" + "、".join(unknown[:5]),
                            hint="先用 list_removed_files 核对文件名")
            return _propose(conn, ctx, "restore_source_files", {"names": names},
                            "把 %d 个文件从回收站恢复到来源目录：%s"
                            % (len(names), "、".join(names[:5])
                               + ("…" if len(names) > 5 else "")),
                            risk="中")

        # ---------------- 字段更正 / 重算 / 批处理（v1.25） ----------------
        if name == "update_candidate_fields":
            cid, _cerr = _resolve_cid(conn, args.get("candidate_id"))
            if _cerr:
                return _err(_cerr["error"], _cerr.get("hint"))
            d = db.candidate_detail(conn, cid)
            if not d:
                return _err(f"未找到候选人 #{cid}")
            _LABEL = {"name": "姓名", "edu_level": "学历", "school": "学校",
                      "major": "专业", "phone_enc": "电话", "email_enc": "邮箱",
                      "note": "备注"}   # v1.25.7：人工备注
            fields: dict = {}
            if args.get("name") is not None:
                nm = str(args.get("name") or "").strip()
                if not re.fullmatch(r"[\u4e00-\u9fa5·A-Za-z\s]{1,30}", nm):
                    return _err("姓名只应为 1-30 个汉字/字母")
                fields["name"] = nm
            if args.get("education") is not None:
                edu = str(args.get("education") or "").strip()
                if edu and edu not in db.EDU_RANK:
                    return _err("学历只能填：大专 / 本科 / 硕士 / 博士（或留空表示未识别）")
                fields["edu_level"] = edu or None
            for k, col in (("school", "school"), ("major", "major")):
                if args.get(k) is not None:
                    fields[col] = str(args.get(k) or "").strip() or None
            if args.get("phone") is not None:
                ph = str(args.get("phone") or "").strip()
                if ph and not re.fullmatch(r"[\d\s\-+()]{7,20}", ph):
                    return _err("电话格式不对（7-20 位数字/符号）")
                fields["phone_enc"] = ph
            if args.get("email") is not None:
                em = str(args.get("email") or "").strip()
                if em and "@" not in em:
                    return _err("邮箱格式不对")
                fields["email_enc"] = em
            if args.get("note") is not None:            # v1.25.7：人工备注（≤500 字，空串=清空）
                nt = str(args.get("note") or "").strip()
                if len(nt) > 500:
                    return _err("备注最多 500 字（当前 %d 字）" % len(nt))
                fields["note"] = nt or None
            if not fields:
                return _err("没有要修改的字段")
            from ..crypto import decrypt
            _cur = {"name": d.get("name") or "", "edu_level": d.get("edu_level") or "",
                    "school": d.get("school") or "", "major": d.get("major") or "",
                    "note": d.get("note") or "",
                    "phone_enc": decrypt(d.get("phone_enc")) or "",
                    "email_enc": decrypt(d.get("email_enc")) or ""}
            pairs = []
            for k, v in fields.items():
                old = _cur.get(k) or ""
                if str(old) != str(v or ""):
                    pairs.append(f"{_LABEL[k]}：{old or '（空）'} → {v or '（空）'}")
            if not pairs:
                return _ok({"candidate_id": cid, "changed": [],
                            "note": "字段没有变化。"})
            return _propose(conn, ctx, "update_candidate_fields",
                            {"candidate_id": cid, "fields": fields},
                            f"更正 {d.get('name') or ('#' + str(cid))} 的字段："
                            + "；".join(pairs), risk="中")

        if name == "reanalyze_candidate":
            cid, _cerr = _resolve_cid(conn, args.get("candidate_id"))
            if _cerr:
                return _err(_cerr["error"], _cerr.get("hint"))
            d = db.candidate_detail(conn, cid)
            if not d:
                return _err(f"未找到候选人 #{cid}")
            return _propose(conn, ctx, "reanalyze_candidate", {"candidate_id": cid},
                            f"对 {d.get('name') or ('#' + str(cid))} 完整重跑分析"
                            "（重解析→重抽→重建技能→重归岗→重判档；"
                            "HR 定的档与人工更正的字段保持不动）", risk="中")

        if name == "reanalyze_batch":
            ids = [int(x) for x in (args.get("candidate_ids") or [])]
            limit = int(args.get("limit") or 50)
            if ids:
                desc = f"批量重跑分析 {len(ids)} 位指定候选人"
            else:
                # ⚠️ extract_mode 在 **applications** 表，不在 candidates 表。
                # 第一版写成 `FROM candidates ... extract_mode`，直接
                # OperationalError: no such column —— 而且是**只在真调时才暴露**
                # （静态看代码完全合理）。所以每个新工具都必须真跑一次。
                n_heur = conn.execute(
                    "SELECT count(DISTINCT a.candidate_id) FROM applications a "
                    "JOIN candidates c ON c.id = a.candidate_id "
                    "WHERE c.merged_into IS NULL AND COALESCE(c.archived_at,'') = '' "
                    "AND COALESCE(a.extract_mode,'') != 'llm+rule'").fetchone()[0]
                desc = (f"批量重跑分析所有「规则通道」档案（当前 {n_heur} 位，"
                        f"本次最多 {limit} 位）")
            return _propose(conn, ctx, "reanalyze_batch",
                            {"candidate_ids": ids, "limit": limit}, desc, risk="中")

        if name == "route_pending":
            return _propose(conn, ctx, "route_pending",
                            {"apply": bool(args.get("apply", True))},
                            "为「待指定岗位」的投递重新建议岗位并重算建议档", risk="中")

        if name == "archive_by_year":
            try:
                y = int(args.get("before_year"))
            except (TypeError, ValueError):
                return _err("before_year 要是年份数字，如 2026")
            n = conn.execute(
                "SELECT count(*) FROM candidates WHERE archived_at IS NULL "
                "AND merged_into IS NULL").fetchone()[0]
            return _propose(conn, ctx, "archive_by_year",
                            {"before_year": y},
                            f"归档 {y} 年及以前的投递（当前在池 {n} 位，"
                            "仅最后一条投递早于该年的会被归档）", risk="高")

        # ---------------- 系统配置 / 待办 / 对话（v1.25） ----------------
        if name == "get_settings":
            return _ok(_read_settings_safe())

        if name == "save_settings":
            st = args.get("settings")
            if not isinstance(st, dict) or not st:
                return _err("settings 要是一个非空对象，如 {\"gender_filter_enabled\": true}")
            bad = [k for k in st if any(s in k.lower()
                                        for s in ("key", "secret", "password", "token"))]
            if bad:
                return _err("这些键属于密钥，不能通过本工具修改：" + "、".join(bad),
                            hint="密钥请在「系统配置」页手工填写")
            cur = _read_settings_safe()
            pairs = [f"{k}：{cur.get(k, '（未设置）')} → {v}" for k, v in st.items()]
            return _propose(conn, ctx, "save_settings", {"settings": st},
                            "修改系统设置：" + "；".join(pairs), risk="中")

        if name == "get_model_config":
            return _ok(_read_model_cfg_safe())

        if name == "save_model_config":
            cur = _read_model_cfg_safe()
            new = dict(cur.get("config") or {})
            pairs = []
            for k in ("base_url", "model"):
                if args.get(k):
                    pairs.append(f"{k}：{cur.get(k, '（空）')} → {args[k]}")
                    new[k] = args[k]
            if args.get("api_key"):
                pairs.append("api_key：（已设置，新值不回显）")
                new["api_key"] = args["api_key"]
            if not pairs:
                return _err("没有要修改的模型配置项")
            return _propose(conn, ctx, "save_model_config", {"config": new},
                            "修改模型配置：" + "；".join(pairs), risk="高")

        if name == "rebuild_brief":
            use_llm = bool(args.get("use_llm"))
            return _propose(conn, ctx, "rebuild_brief", {"use_llm": use_llm},
                            "重算今日待办摘要" + ("（含模型判断，较慢）" if use_llm else "（规则版）"),
                            risk="低")

        if name == "run_proactive_scan":
            # 巡检只建提案、不改任何业务数据 → **直接执行**，不再套一层确认。
            # 再多一次确认会让 HR 觉得"点两遍才动"，而它本身是只读+提案的。
            from . import proactive as _pa
            r = _pa.scan_and_propose(conn, stuck_days=int(args.get("stuck_days") or 7))
            return _ok({"created_count": r.get("created_count"),
                        "scanned": r.get("scanned"),
                        "skipped": r.get("skipped"),
                        "note": (f"巡检完成，产出 {r.get('created_count')} 条待确认提案，"
                                 "在下面的卡片里逐条确认或拒绝。"
                                 if r.get("created_count") else
                                 "巡检完成，本轮没有需要新建的提案"
                                 "（已提过的在 7 天内不重复提）。")})

        if name == "list_agent_runs":
            runs = db.list_agent_runs(conn, limit=int(args.get("limit") or 10))
            return _ok({"count": len(runs),
                        "runs": [{"question": r.get("question"),
                                  "rounds": r.get("rounds"),
                                  "tokens": (r.get("tokens_in") or 0) + (r.get("tokens_out") or 0),
                                  "latency_ms": r.get("latency_ms"),
                                  "ts": r.get("ts")} for r in runs]})

        if name == "clear_chat_history":
            return _propose(conn, ctx, "clear_chat_history", {},
                            "清空智能助手的对话历史（不影响任何候选人数据）", risk="低")

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
            # ---- v1.25：指定了模板就**按模板渲染**，不再自由撰写 ----
            # 这是截图 2 的正面修法。过去只能自由撰写，于是 HR 问「能用我的模板吗」
            # 时助手只能答"我读不到模板"——它说的是实话，因为压根没这条路。
            # 现在：传了 template 就走真实模板（变量替换、条件块都走同一套 render），
            # 渲染结果里没填的变量会变成【待填：xxx】，前端能直接指出缺什么。
            tpl = None
            _tkey = str(args.get("template") or "").strip()
            if _tkey:
                tpl = _find_template(conn, _tkey)
                if not tpl:
                    names = [x["name"] for x in db.list_mail_templates(conn)]
                    return _err(
                        f"没有找到叫「{_tkey}」的邮件模板",
                        hint=("现有模板：" + "、".join(names)) if names
                        else "系统里目前一个模板都没有，只能由我自由撰写（不传 template 即可）")

            if tpl is not None:
                # 模板渲染上下文：候选人字段 + 岗位 + 运行时变量（面试时间/地点等）。
                # ⚠️ 必须在渲染**之前**构造——原来这行在自由撰写分支里，
                # 我加模板分支时挪丢了它，于是模板路径直接 NameError: ctxd。
                # 教训：新增分支改动了公共前置变量时，要检查另一条分支是否还在用。
                ctxd = _mt.build_ctx(cand, job_title, ctx.operator, rt)
                _r_sub = _mt.render(tpl.get("subject") or "", ctxd)
                _r_body = _mt.render(tpl.get("body") or "", ctxd)
                subj = (_r_sub.get("text") or "").strip()
                body = (_r_body.get("text") or "").strip()
                _miss = _r_body.get("missing") or []
                body_html = body if _mt.looks_like_html(body) else _mt.to_html(body)
                return _ok({
                    "candidate_id": cid, "name": cand.get("name"),
                    "to": to_addr, "subject": subj, "body": body, "body_html": body_html,
                    "job": job_title,
                    "template": tpl["name"],
                    "missing_runtime": _miss,
                    "note": ("**这是草稿，没有发送。**正文按系统里的模板「%s」渲染，"
                             "变量已替换。" % tpl["name"])
                            + (f" 还缺：{'、'.join(_miss)}（补上后我重渲染一版）。" if _miss else "")
                            + ("" if to_addr else " ⚠️ 库里没有这个人的邮箱，发送前请先补联系方式。"),
                })

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
            #
            # ⚠️ v1.25 修：原来是 `from . import ingest` —— 在 app/agent/tools.py
            # 里 `.` 指的是 **app.agent**，而 ingest 在 **app** 下，于是
            # ImportError: cannot import name 'ingest' from 'app.agent'。
            # 因为这个导入写在**函数体内**（延迟导入），模块加载不报错，
            # 于是这个工具在清单里"一直都在"、却**从来没被真正调用过一次**。
            # 教训：延迟导入的路径错误不会在启动时暴露 —— 新工具必须真跑一遍。
            from .. import ingest as _ing
            src = (args.get("source") or "mailbox").strip()
            if src not in ("mailbox", "folder"):
                return _err(f"source 只能是 mailbox 或 folder（收到的是 {src}）")
            _conn_jd = _jd_tiers()
            if src == "folder":
                folder = (args.get("folder") or "").strip()
                # 没给路径就用**系统配置里的「导入文件夹」**——
                # 界面上「导入简历」就是这么做的（server 端同口径）。
                # 原来这里直接报错要求 HR 手打路径，而路径本来就配好了、
                # 界面上也有，让 HR 再抄一遍纯属多余。
                import os as _os
                _mbcfg = _read_json_or(_repo_config_path("mailbox.json"), {}) or {}
                if not folder:
                    folder = _mbcfg.get("folder_dir") or _os.path.join(_data_dir(), "resumes")
                else:
                    # 给了路径也要**收口在允许的来源目录内**（与 /api/ingest 同一条规则）：
                    # 这里的 folder 来自模型参数，不校验就是一条任意目录读取路径。
                    try:
                        folder = _ing.resolve_source_dir(folder, _mbcfg)
                    except ValueError as exc:
                        return _err(str(exc))
                if not _os.path.isdir(folder):
                    try:
                        _os.makedirs(folder, exist_ok=True)
                    except OSError as exc:
                        return _err(f"导入目录不存在也建不出来：{folder}（{exc}）")
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
            # 建议岗位会写 suggested_job_id（影响档案页展示），是写操作 → 走提案。
            # 之前这里直接调 suggest_job_for_application 落库、却留在 WRITE_TOOLS 里，
            # 于是 apply_proposal 那条分支永远不触发、写操作绕过了 HR 确认。
            # 改成提案：HR 点头后由 apply_proposal 真正计算并落库。
            return _propose(conn, ctx, "suggest_job", {"candidate_id": cid},
                            f"为 #{cid} 的待指定投递判断一条建议岗位（模型判断，约 6-8 秒）",
                            risk="低")

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
            # v1.25：支持用**候选人**指定目标，而不只是投递 id。
            #
            # 为什么必须加：HR 真实说法是「把 #1 推进至初面」——这个 `#1`
            # 可能是投递 id 也可能是候选人 id，模型无法分辨，于是只能反问
            # 「请告诉我是哪条投递」。**HR 已经把编号给出来了，再问一遍
            # 就是把活儿推回去**。现在两种都认：
            #   · 给了 application_id → 直接用（原有行为）
            #   · 只给了 candidate → 找他的投递；**恰好一条**才用，多条则要求
            #     HR 指明是哪条（替他挑一条＝替他做决定，绝对不做）
            aid = args.get("application_id")
            app = None
            if aid is not None:
                try:
                    app = db.get_application(conn, int(aid))
                except (TypeError, ValueError):
                    app = None
                if not app:
                    return _err(f"未找到投递 #{aid}")
            else:
                cid, cerr = _resolve_cid(conn, args.get("candidate_id"))
                if cerr:
                    return _err(cerr["error"], cerr.get("hint"))
                rows = conn.execute(
                    "SELECT id FROM applications WHERE candidate_id=? ORDER BY id",
                    (cid,)).fetchall()
                if not rows:
                    return _err("这位候选人还没有投递记录，无法推进阶段")
                if len(rows) > 1:
                    names = "、".join("#%s" % r["id"] for r in rows)
                    return _err(
                        f"这位候选人有 {len(rows)} 条投递（{names}），需要指明哪一条",
                        hint="请告诉我要推进的是哪条投递（例如「推进投递 #%s」）"
                             % rows[0]["id"])
                app = db.get_application(conn, rows[0]["id"])
                aid = app["id"]
            stage = (args.get("stage") or "").strip()
            if stage not in STAGES:
                return _err(f"阶段取值非法：{stage}；只能是 {'/'.join(STAGES)}")
            # v1.29：结论标签。**在这一层就校验**（与 db.set_application_stage 同一口径）——
            # 让模型在"提案阶段"就知道打错了，而不是等 HR 点了确认才 500。
            outcome = (args.get("outcome") or "").strip() or None
            # 同一阶段只改面试结论时，提案目标也要显示为「已面试」，确认后同口径落库。
            stage = stages_mod.auto_advance_stage(app.get("stage"), stage, outcome)
            if outcome and not stages_mod.outcome_allowed_from(app.get("stage"), stage, outcome):
                _ok_oc = stages_mod.outcomes_of(app.get("stage"))
                return _err(
                    f"「{app.get('stage')} → {stage}」这一步不能打结论「{outcome}」"
                    + (f"；可选 {'/'.join(_ok_oc)}" if _ok_oc else "；这一步没有结论标签"))
            cand = db.get_candidate(conn, app["candidate_id"]) or {}
            # 已在目标阶段**且结论也没变**才不必再提一遍
            # （否则卡片上会出现"初面 → 初面"，但"已发放offer + 已签约"是有效变更）
            if (app.get("stage") or "") == stage and (app.get("outcome") or None) == outcome:
                return _ok({"note": f"{cand.get('name') or '这位候选人'} 已经在「{stage}」阶段"
                                    + (f"（{outcome}）" if outcome else "")
                                    + "，不需要再推进。", "no_change": True})
            _args = {"application_id": int(aid), "stage": stage}
            if outcome:
                _args["outcome"] = outcome
            return _propose(conn, ctx, "set_stage", _args,
                            f"把 {cand.get('name') or '#' + str(app['candidate_id'])} 的投递 "
                            f"#{aid} 从「{app.get('stage')}」推进到「{stage}」"
                            + (f"，结论「{outcome}」" if outcome else "")
                            + (f"（理由：{args['reason']}）" if args.get("reason") else ""))

        if name == "set_tier":
            aid = int(args["application_id"])
            app = db.get_application(conn, aid)
            if not app:
                return _err(f"未找到投递 #{aid}")
            tier = (args.get("tier") or "").strip().upper()
            if tier not in tiers_mod.all_tiers():
                return _err(f"档位只能是 {tiers_mod.enum_str()}")
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

        # ---- v2.0.7：查学校（本地数据，零外发） ----
        if name == "school_info":
            from .. import univ as _univ

            school = str(args.get("school") or "").strip()
            if not school:
                return _err("school 不能为空")
            band = _univ.school_band(school)
            return _ok({
                "school": school,
                # 985 / 211 / 强势 / None —— 身份标签
                "tier": _univ.uni_tier(school),
                # 软科**综合**排名名次与实力档（A/B/C/D）。None = 名单里没有这所学校。
                "rank": band[1] if band else None,
                "band": band[0] if band else None,
                "rank_source": "2025 软科中国大学排名（主榜）",
                "overseas": _univ.is_overseas(school),
                # 明确告诉模型"没有学科评估数据"，否则它会拿综合排名去冒充 A+/A-
                "has_discipline_eval": False,
                "discipline_note": "本系统只有综合排名，没有教育部学科评估等级（A+/A-/B+），"
                                   "被问到学科评估请如实说查不到。",
                "note": ("" if band else "无排名数据只表示名单里没有这所学校，**不代表学校差**。"),
            })

        # ---- v2.0.7：对外搜索（**护栏在代码里**，见 app/websearch.py） ----
        if name == "web_search":
            from .. import websearch as _ws

            query = str(args.get("query") or "").strip()
            # 两关：① 必须查学校/专业/行业 ② 不能带候选人姓名/电话/邮箱/身份证
            # 不满足就直接拒，并把原因如实返回 —— 静默失败会让模型自己编答案。
            deny = _ws.guard(conn, query)
            if deny:
                return _err(deny)
            res = _ws.search(query, limit=5)
            if not res.get("ok"):
                return _err(res.get("reason") or "搜索失败")
            return _ok(res)

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
