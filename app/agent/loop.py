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
import urllib.error
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

from .. import db, stages as stages_mod, tiers as tiers_mod
from ..pipeline import normalize as nz
from . import llm
from . import tools as tools_write
from .router import route_tools, tool_catalog_text
from .skills import match_skill
from .tools import DISABLED_TOOLS, STAGES, TOOL_SPECS, ToolCtx, execute

# --------------------------------------------------------------- 文本形式的工具调用
#
# 有些 OpenAI 兼容端点（实测小米 MiMo，部分 Qwen 部署也是如此）不会把工具调用
# 放进 `tool_calls`，而是把模型的原生标签原样写进 `content`：
#
#   <tool_call><function=search_candidates><parameter=job>数字化工程师</parameter>
#   <parameter=limit>50</parameter></function></tool_call>
#
# 不解析会有两个后果：工具一个都没执行；这段标签被当成答复原样显示给 HR。
# 这里按**模型实际输出的形态**解析，而不是要求端点必须规范。
_TOOL_CALL_BLOCK_RE = re.compile(r"<tool_call>\s*(.*?)\s*</tool_call>", re.S | re.I)
_TEXT_FUNCTION_RE = re.compile(
    r"<function\s*=\s*[\"']?([A-Za-z_][\w.-]*)[\"']?\s*>(.*?)</function>", re.S | re.I)
_TEXT_PARAM_RE = re.compile(
    r"<parameter\s*=\s*[\"']?([A-Za-z_][\w.-]*)[\"']?\s*>(.*?)</parameter>", re.S | re.I)
_TEXT_PARAM_NAMED_RE = re.compile(
    r"<parameter\s+name\s*=\s*[\"']([^\"']+)[\"']\s*>(.*?)</parameter>", re.S | re.I)


def _text_arg_value(raw: str):
    """XML 参数值 → Python 值（50→int，true→bool，[1,2]→list，其余保留原文）。"""
    v = (raw or "").strip()
    if not v:
        return ""
    try:
        return json.loads(v)
    except (TypeError, ValueError):
        return v


def _text_tool_calls(content: str) -> tuple[list[dict], str]:
    """从正文里拆出文本形式的工具调用，并返回去掉标签后的正文。

    支持两种实际见过的形态：
      · `<tool_call><function=x><parameter=k>v</parameter></function></tool_call>`
      · `<tool_call>{"name":"x","arguments":{...}}</tool_call>`
    """
    text = content or ""
    calls: list[dict] = []

    def _mk(name: str, args: dict) -> dict:
        return {"id": "call_" + uuid.uuid4().hex[:12], "type": "function",
                "function": {"name": name,
                             "arguments": json.dumps(args, ensure_ascii=False)}}

    for block in _TOOL_CALL_BLOCK_RE.findall(text):
        raw = block.strip()
        # ---- JSON 形态 ----
        if raw.startswith("{"):
            try:
                obj = json.loads(raw)
            except ValueError:
                obj = None
            if isinstance(obj, dict):
                fn = obj.get("function") if isinstance(obj.get("function"), dict) else obj
                name = fn.get("name") or obj.get("name") or ""
                args = fn.get("arguments", obj.get("arguments", {}))
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        args = {}
                if name and isinstance(args, dict):
                    calls.append(_mk(name, args))
                    continue
        # ---- XML 形态 ----
        matched = False
        for fm in _TEXT_FUNCTION_RE.finditer(block):
            matched = True
            args: dict = {}
            for pm in _TEXT_PARAM_RE.finditer(fm.group(2)):
                args[pm.group(1)] = _text_arg_value(pm.group(2))
            for pm in _TEXT_PARAM_NAMED_RE.finditer(fm.group(2)):
                args[pm.group(1)] = _text_arg_value(pm.group(2))
            calls.append(_mk(fm.group(1), args))
        # 半截输出：只有 <function=x> 没有 </function>，参数仍按 <parameter> 收
        if not matched:
            fm = re.search(r"<function\s*=\s*[\"']?([A-Za-z_][\w.-]*)", block, re.I)
            if fm:
                args = {}
                for pm in _TEXT_PARAM_RE.finditer(block):
                    args[pm.group(1)] = _text_arg_value(pm.group(2))
                calls.append(_mk(fm.group(1), args))

    cleaned = _TOOL_CALL_BLOCK_RE.sub("", text)
    cleaned = re.sub(r"</?tool_call>", "", cleaned, flags=re.I).strip()
    return calls, cleaned


def _strip_tool_call_text(text: str) -> str:
    """答复出口的最后一道清洗：文本形式的工具调用不该显示给 HR。"""
    return re.sub(r"<tool_call>.*?</tool_call>", "", text or "",
                  flags=re.S | re.I).strip()


SYSTEM_PROMPT = (
    "你是人才库智能体的 HR 招聘助手，负责人事招聘工作。\n"
    "铁律：\n"
    "1) 涉及候选人数量、条件筛选、技能、简历细节的问题，必须先调用工具取真实数据，"
    "严禁凭印象编造姓名、数字或经历；工具返回为空就如实说没有。\n"
    # v1.24.17：检索是**字面匹配**，而 HR 说的是简称、库里存的是全称。
    "1b) 检索工具是**字面匹配**。HR 说的往往是简称（『西电』『北邮』『人大』），"
    "库里存的是全称（『西安电子科技大学』）。搜不到时**先怀疑检索词**："
    "把简称展开成全称再搜一次；技能同理（先过一遍同义说法）。"
    "两种写法都搜过仍然为空，才回答『没有』。\n"
    "   同理，HR 问『有没有会X的人』时，技能检索（search_by_skills）结果为空，"
    "先确认 X 在技能本体里叫什么，再重试，不要直接答没有。\n"
    "2) 你只给建议，不做决定。最终档位、是否面试、是否录用都由 HR 确认，"
    "不要声称你已经替 HR 做了决定。"
    "**档位只有 " + "、".join(tiers_mod.all_tiers())
    + "（共 " + str(len(tiers_mod.all_tiers())) + " 档），禁止使用 D 或其他字母；"
    "学历低于岗位门槛时按 " + tiers_mod.hard_shortfall_tier() + " 处理。**\n"
    "3) 你没有永久删除候选人/彻底删除数据的权限，也没有对候选人直接发消息的权限；"
    "也不要建议这么做。归档、停用岗位/账号等可逆操作有对应工具，按工具说明走提案。\n"
    "   但**起草**是可以的：用户要发邮件/通知时，用 draft_email 生成草稿"
    "（面试邀请/跟进/婉拒/offer 沟通），把主题与正文给用户看，"
    "并告诉他『这是草稿，发送需要你点确认』——"
    "**不要说『我无法帮你发邮件』就结束**，那是能干的活儿没干。\n"
    # v1.25：截图反馈「说了用模版还是会自己写」。根因是提示词没约束模板路径——
    # 模型默认走 draft_email 的自由撰写分支，把 HR 的模板抛到一边。
    # 加这条硬规则：只要 HR 提了"用模板"，就必须先查模板再按模板渲染，禁止偷偷自由发挥。
    "3b) 用户写邮件/通知/面试邀约/offer/婉拒/跟进时，只要提到「模板 / 我的模板 / 院里的模板 / 按模板 / 用模板写 / 套模板 / 按格式」，"
    "或者明确指定了模板名，**就必须用邮件模板**——绝不允许自己自由发挥："
    "先调 list_mail_templates（或 get_mail_template）找到模板名，"
    "再调 draft_email(template=<模板名>, candidate_id=<候选人>) 渲染——"
    "**忽略 tone/purpose 的自由撰写路径**。\n"
    "   找不到匹配模板时，如实列出已有模板请用户指定，而不是偷偷换成自由撰写"
    "（那等于无视了用户的明确要求）。\n"
    # 让用户能在回复里一眼看到用的是哪套模板（截图原话「回复这里没有渲染出我的模版」）。
    "   渲染成功后，在你的回复里明确写出『已按系统模板「<模板名>」生成草稿』，"
    "不要只用『我帮你写好了』这种含糊说法。\n"
    # v1.25.1：截图显示模型把 draft_email 返回的 HTML body 原样贴进了聊天回复，
    # 结果满屏 <table><tr><td>，且没有草稿卡片也没有按钮。这里明确禁止。
    "3c) **永远不要把 draft_email / preview_mail 返回的邮件正文（HTML 源码、<table> 标签、"
    "{面试时间} 等待填变量、<!--IF:...--> 条件注释等）直接粘贴或引用到你的聊天回复里。**"
    "界面会自动把它渲染成『草稿卡片』，并附带「在编辑器中修改」「确认并发送」按钮。"
    "你的回复只写一句简短确认（如『已按模板「X」生成草稿，点击下方卡片查看』）+"
    "还缺哪些变量即可。若把 HTML 正文直接写进回复，HR 看到的是乱码标签，"
    "而且点不到按钮，视为严重错误。\n"
    "4) 改档、改阶段、加标签、合并档案这类写操作，工具会返回"
    "『已提交待 HR 确认（pending_confirmation）』——此时要明确告诉用户"
    "『已提交待确认』，绝不能说『已经改好了』。\n"
    # v1.24.18：截图里出现过「已为您生成待确认提案」但**底下没有任何按钮**。
    # 根因：模型 rounds=1，一个工具都没调，纯用文字"描述"了一个提案。
    # 铁律 4 只约束了「拿到工具返回后怎么措辞」，**没约束「必须先调工具」**。
    "4b) **凡是『要做的事』，必须先调工具拿到结果，再说话。**"
    "改阶段/改档位/加标签/加备注/合并/起草邮件——一律如此。"
    "不准凭空描述『已为您生成待确认提案』：只要这轮没调过写类工具，"
    "就一定没有提案，界面也不会给确认按钮。宁可多调一次工具，"
    "也不要输出一句没有按钮兜底的『已提交待确认』。\n"
    # v1.25：用户原话「不需要跳转到对应页面，除非自行选择的」。
    # 过去模型爱用「请到 XX 页面自己操作」收尾 —— 那是把活儿推回给 HR。
    "4c) **不要把活儿推回给 HR。** 你能做的事必须自己做完："
    "查数据就调检索工具、要改档案就提交提案（确认按钮就在这条回复下面）。"
    "**禁止**用『请到「XX」页面自己操作』『你可以去系统里点一下』这类话收尾。"
    "确实做不了的（对外发信、彻底删除），要说明是**权限限制**，"
    "并给出你能做的替代（例如草稿已经写好，点「确认并发送」即可）。\n"
    "4d) 每个工具的参数里都写了它**什么时候不要用**，动手前先看清；"
    "工具返回 error 时如实转述原因，不要换个说法掩盖。\n"
    "5) 引用技能或结论时，尽量带上简历原文证据（工具会返回 evidence 字段）。\n"
    "7) 人是用**姓名**指人的：candidate_id 可以直接写候选人姓名（系统会自动解析成正确的 id）；"
    "**绝对不要凭空编数字 id**——编了会直接报'未找到候选人'。\n"
    # v1.25.5（用户截图）：模型在零工具调用的一轮里输出「全量对比表」，
    # 表格里 12 个"候选人"全是虚构的（含填进示例位的名字）。根因之一是
    # 提示词只"劝说"（铁律 1 早有"严禁编造姓名"但它不听）。真正的拦截在
    # 出口 `_data_claim_safety_net`；这里把红线说白，让模型少犯。
    "7c) **回复里出现的每一个候选人姓名，都必须来自本轮工具返回或用户自己提供。**"
    "严禁编造姓名；也不要把示例、常识或之前对话里提到的名字当库内数据。"
    "**零工具调用时不要声称『已调用工具/已获取数据』**；要名单就先调 search_candidates，"
    "要统计就先调 pool_stats；工具查不到的人，就如实回答『查不到』。\n"
    # v1.29.8（业务方截图：技能像的人被列进了别的岗位名单，还编"缓存问题"解释）：
    # 「某岗位的候选人」= 归岗关系，必须走 search_candidates 的 job 参数。
    "7d) 问『**某岗位有哪些候选人 / 谁归在这个岗位**』时，search_candidates **必须带 job 参数**"
    "（按归岗关系筛）。**技能匹配 ≠ 归岗**：技能像不表示归在这个岗位；"
    "工具结果里每条的 job_title 就是他的真实归岗，照实引用，不要按技能重新归堆。\n"
    # v1.25：实测「把 #1 推进至初面」时模型**不查就反问**，
    # 而 HR 已经把编号给出来了 —— 再问一遍等于把活儿推回去。
    # 编号可能是投递 id 也可能是候选人 id，**两个都查一下**再决定要不要问。
    "7b) HR 给出 `#数字` 时，**先查再问**：把它当投递 id 查一次"
    "（get_candidate / explain_grade 都能带出投递），"
    "查到了就直接用；查不到或有多条歧义，才回头问 HR。"
    "不要一上来就要求 HR 再确认一遍编号——**编号就是他要你用的那个**。\n"
    "6) 回答用简体中文，结论先行、结构化（可用编号或短列表），不要输出 JSON。\n"
    "7f) **需要一次看 2 人以上时，用 get_candidates 批量读取**，不要连续多次调用 "
    "get_candidate——每次单读都要多一轮，5 个人就会把回答拖到一分钟以上。\n"
    "7g) 工具调用要克制：同一个目标最多检索 2 轮；信息已经够回答就停下来组织答案，"
    "不要为了“再确认一次”反复查。用户要的是结论，不是工具调用次数。\n"
    "7e) 做分布 / 占比 / 图表时，先调 pool_stats 并传 group_by"
    "（education/school/major/job/stage/tier/gender）拿真实分布，再按第 8 条输出图表；"
    "**不要自己拉全量列表再心算**，也不要把两个同义学历（专科/大专、研究生/硕士）分开列。\n"
    "7h) HR 问「邮件标题/邮件名称/邮件正文包含某字样的人」「发件人是谁的投递」时，"
    "用 search_candidates 的 mail_subject / mail_from / mail_body 参数"
    "（keyword 也已包含邮件标题、发件人与正文）。"
    "**不要说数据库里没有这个信息**——来源邮件已经随简历入库，先用工具查；"
    "升级前收的老邮件可能只有标题/发件人，没有正文，这时如实说明。\n"
    "7i) 需要看某人的完整简历原文/证书/作品集内容时，调 get_document_text"
    "（full=true 取完整）；需要看来源邮件正文时调 get_source_mail。\n"
    "7j) 问「有某证书的人」时，用 search_candidates 的 certificate 参数"
    "（keyword 也已包含证书）；证书名以档案里的证书字段为准，不要自己猜。\n"
    # v4.3.3（业务方截图：要折线图和饼图，模型吐了两段 Mermaid 伪代码，界面
    # 按代码块原样显示）。界面现在能直接把 Mermaid 渲染成图，这里把格式说死：
    # 必须用 ```mermaid 代码块，数据必须来自本轮工具结果。
    "8) 用户要图表时，**必须用 ```mermaid 代码块**输出 Mermaid，界面会自动渲染成图："
    "饼图用 `pie showData`（每行 `\"标签\" : 数值`）；"
    "折线/柱状用 `xychart-beta`（title / x-axis [..] / y-axis \"单位\" 0 --> 上限 / line [..] 或 bar [..]）。"
    "**不要**输出 matplotlib、echarts、Python 绘图代码，也不要输出 `pie showData` 这类"
    "没有代码块包裹的裸伪代码——那样只会显示成文字。图表里的每个数字都必须来自本轮工具结果。\n"
    "9) 回答要有结构：较长的回答用 **加粗小标题** 分段（如 **结论**、**缺口**、**下一步**），"
    "多用短列表和表格；不要把多个要点挤成一大段。\n"
    "10) 涉及日期时以 system 里的【当前时间】为准：用户说今天/明天/后天/本周/下周，"
    "先换算成 YYYY-MM-DD 再传给工具或写进答复；不要凭记忆猜年份。\n"
    "11) 用户一句话里包含多个动作时（如“筛人→归岗→安排面试→导出”），"
    "先拆成编号步骤，**按顺序把每个动作都执行**；写操作分别生成待确认提案，"
    "最后集中汇总，让 HR 一次「全部同意」。不要只做第一个动作就收尾。"
)


#: v1.24.18：模型声称"已提交/已生成待确认提案"的措辞。
#: 用于出口核对——若本轮没调过写类工具却命中这些措辞，说明是**口头提案**，
#: 必须当场纠正，否则 HR 会拿着一句承诺去找一个不存在的按钮。
#:
#: **判据设计（试了三版才对，教训记下来）**：
#: 1. 「已 + 动词」→ 误伤一切（"已为你找到 3 位候选人"不是写操作）；
#: 2. 枚举「已推进|已修改…」→ 漏判（"已把标签添加好了"里动词被"把"字挤开，
#:    中文被动/把字句变体太多，枚举必然不全）；
#: 3. 状态陈述式「阶段为…」→ 同样漏"已把标签添加好了"，而且
#:    "该候选人阶段为新投递"这种**如实描述**也会命中，反而误伤。
#:
#: 最终结论：**别猜措辞，只问"这段话有没有把档案说成已完成态"**。
#: 判据 =出现档案类对象（阶段/档位/标签/备注/合并/提案），
#: **且** 同句里没有"建议/可以/是否/推荐"这类**商量语气**。
#: 档案类对象：这些东西的值只存在于库里，**必须查过才知道**。
#: 零工具调用却在谈论它们 → 就是编造。
_ARCHIVE_OBJ_RE = re.compile(
    r"(提案|待确认|待\s*(?:HR|您|你)\s*确认|pending_confirmation"
    r"|阶段|档位|标签|备注|合并|档案|"
    # v1.29：阶段名从配置生成，**不再手抄**——写死的话人事处一改流程，
    # 新阶段名就不在这个白名单里，模型"零工具却谈论新阶段"会漏过而不被拦。
    + stages_mod.stage_regex() + r")")

#: 「我已生成/已提交待确认提案」这类**明确承诺**。
#: 它比档案对象本身更严重：模型在句子里写"已为您生成待确认提案"，
#: HR 的合理推断就是"下面会有按钮"，点不到就是"系统坏了"。
#:
#: ⚠️ **只收"承诺"，不收"档案对象"**（第六→第七版的收敛方向）：
#: 早期版本零工具时见"阶段/初面/标签/档案"就拦，误伤两类正常回复——
#:   "3 位候选人卡在初面超过 7 天，建议尽快安排复面"（只是说了阶段名词）
#:   "我查到他的档案记录了"（提到了"档案"）
#: 这两句都没在声称做过写操作。**提及 ≠ 声称**，混在一起必然一边漏一边伤。
#:
#: 收敛后的判据：**零工具 + 出现"已生成/已提交/待确认"这类完成态承诺**才拦。
#: 宁可漏放（出口还有 `_recover_missed_action` 补救兜着），
#: 也不能把如实汇报指成编造——那会直接毁掉信任。
_FAKE_PROPOSAL_CLAIM_RE = re.compile(
    r"待\s*(?:确认|HR\s*确认|您\s*确认|你\s*确认)"   # 「待确认提案」「待 HR 确认」
    r"|pending_confirmation"
    # "已为您生成/创建/提交…的待确认提案" —— 中间允许插入任意修饰
    # （实测模型会写「已为您生成将候选人 张三（某大学）
    #   从「新投递」推进到「已联系」的待确认提案」，中间有 30+ 字）
    r"|已\s*(?:为您|为你|经|统一|均)?\s*"
    r"(?:生成|创建|提交|建立|发起|做完|登记|加入|写入|调整|变更|修改|推进)"
    r"[^\n]{0,60}?(?:提案|标签|备注|档位)"
    # 「已将张伟加入标签「核心」」：动词与宾语被主语隔开，所以这条
    # 不能依赖上面的顺序，必须单独覆盖「加入+标签」的完成态
    r"|(?:已|均已)\s*[^\n。]{0,20}?(?:加入|写入|更新|打上|贴上)\s*[^\n。]{0,6}?标签",
    re.IGNORECASE)

#: **完成态 + 动作动词** —— 这是唯一判据（第八版收敛结果）。
#:
#: 为什么这个结构能同时管住两类句子：
#:   "已为您生成待确认提案"        → 命中（已 + 生成）→ 拦 ✅
#:   "我会帮您生成待确认提案"       → 不命中（生成前是"我会"，不是"已"）→ 不拦 ✅
#:   "确认后我会生成待确认提案"     → 不命中 ✅
#:   "请在界面点击确认即可生效"     → 不命中（没有"已+动作"）→ 不拦 ✅
#:
#: 之前试过"用未来时词表排除"，被中文一字多义坑了两回
#: （"已为您**将**候选人…"的"将"是介词、"点击确认**即可**"的"即可"不是将来式）。
#: **找结构，不找词义。** 这是本项目判据设计的通用教训。
_COMPLETED_ACTION_RE = re.compile(
    r"(?:已|均已|皆已|已为您|已为你|已帮你)\s*"
    r"(?:为\s*您|为\s*你|帮你)?\s*"
    r"(?:生成|创建|提交|建立|发起|做完|登记|加入|写入|调整|变更|修改|推进|打上|贴上)"
    r"[^\n]{0,60}?"
    r"(?:提案|待确认|待\s*(?:HR|您|你)\s*确认|标签|备注|档位|阶段)"
    r"|(?:已经|已)\s*[^\n。]{0,20}?(?:加入|写入|更新|打上|贴上)\s*[^\n。]{0,8}?标签"
    r"|(?:已|均已)\s*[^\n。]{0,16}?提交\s*[^\n。]{0,8}?提案",
    re.IGNORECASE)


#: 从模型答复里抠出投递编号。**两种写法都要认**（实测截图里是 `（ID: 1024）`，
#: 不是 `#1024` —— 只认 `#` 会整条补救静默失效）：
#:   「候选人：张三（ID: 1024）」 / 「投递 #1024」 / 「application_id=1024」
#:
#: 只在答复里出现数字 ID 时才启用补救 —— 模型报得出 ID 说明档案它见过。
#: 只给名字的**不猜**：重名候选人在真实库里是常事，猜错就是替 HR 做了错决定。
_RECOVER_ID_RE = re.compile(
    r"(?:#|＃|ID\s*[:：=]|application_id\s*[:=])\s*(\d{1,9})", re.IGNORECASE)

def _looks_like_fake_proposal(answer: str, trace: list[dict]) -> bool:
    """这段答复是不是「口头提案」——本轮没真正操作，却在声称动过档案。

    **判据只认事实 + 只认承诺**（改到第七版才收敛）：
      - 本轮调过写工具 → 正常，不拦。
      - **零写工具 + 出现完成态承诺**（"已生成待确认提案""待 HR 确认"…）→ 拦。
      - 其余一律不拦。

    **七版迭代记录（同一个病：拿措辞当证据）**：

    | 版本 | 做法 | 结果 |
    |---|---|---|
    | 1-5 | 用中文措辞反推"模型是否做了" | 一边漏一边伤，见下面 |
    | 6 | 改看 `trace` 有没有写工具（客观事实） | ✅ 21/21，但仍漏放真实案例 |
    | 7 | 零工具时只认"完成态承诺" | ✅ 20/20 |

    - **1-5 版失败**：中文被动句/把字句变体太多，枚举必然漏。
      "已把标签添加好了"动词被"把"挤开（漏）；
      改成状态陈述式又误伤如实描述（伤）。
    - **6 版仍漏**：真实案例里"纯征询豁免"是**全文任意位置**匹配
      `建议|是否|可否|要不要`，模型只要在**变更理由**里塞一个"建议"就绕过了：

          已为您生成将候选人 张三 从「新投递」推进到「已联系」的待确认提案。
          变更理由：HR 初步沟通，意向明确，**建议**进入已联系阶段。

      → **豁免分支必须按位置收紧**：征询语气只在句首/问号结尾才算。
    - **7 版又伤**（单测抓到的）：收紧后仍误伤两句正常回复——
      "3位候选人卡在初面超过 7 天，建议尽快安排复面"（只是提阶段名词）、
      "我查到他的档案记录了"（只是提"档案"）。
      根因是**还在用 `_ARCHIVE_OBJ_RE` 泛匹配档案对象**。
      **提及 ≠ 声称**。所以第七版干脆不用档案对象，只认承诺。

    **教训（这才是真正要记住的）**：
    一个判据从"错"改成"对"，通常还会**在另一侧产生新的错**。
    每次收紧判据都必须重跑全量样本，不能只看新加的那个案例。

    ⚠️ **第八版：claim 必须是完成态，不能是将来/条件式**
    真实调用里模型说：

        要推进候选人的投递阶段，我需要知道：1. 候选人姓名 2. …
        您想处理哪位候选人？… 我会帮您生成待确认的提案。

    这是**纯粹在问信息**，却在末尾提了一句"待确认的提案" → 被追加
    "上面这段描述不算数"。**把正常的澄清反问说成编造，比漏放更伤。**

    走过的三条错路（都记下来，别再走）：
      1. 要求出现具体对象 → 空头承诺"已为您生成待确认提案"被放行，
         而那**恰是截图里的形态**。具体对象不是判据。
      2. 用 `_FUTURE_TENSE_RE` 排除将来式 → **中文一字多义，反被它坑**：
         "已为您**将**候选人推进到…"里的"将"是介词；"点击确认**即可**生效"
         里的"即可"也不是将来式。两次都把**真实漏放案例**放过了。
      3. ✅ **锚定完成态动词**：判据 =出现「已/均已/已为您」等**完成态标记**
         **且**后面跟着动作动词（生成/提交/创建/更新/加入…）。
         将来式的动词前面是"我会/将/准备"，**不会**是"已"。
         所以只要 claim 落在「已+动词」这个模式上，天然排除将来式，
         **不需要另写排除规则**。

    这才是稳定的判据：找的是结构（已 + 动作动词），不是词义。
    """
    a = answer or ""
    if not a or any(t.get("write") for t in trace):
        return False
    # 完成态 + 动作动词。将来式/条件式不匹配这个结构，天然排除。
    return bool(_COMPLETED_ACTION_RE.search(a))


# --------------------------------------------------------------- 漏调补救



def _force_tool_call(messages: list[dict], answer: str, cfg: dict,
                     usage_total: dict,
                     thinking: list[str] | None = None,
                     tools: list[dict] | None = None) -> list[dict] | None:
    """模型口头声称做了写操作却没调工具 —— 强制它重发一次工具调用。

    为什么用 `tool_choice="required"` 而不是继续猜中文动词：
    模型**刚刚自己说了**"已把 X 推进到 Y"，它完全知道要调哪个工具、
    要传什么参数。我们唯一需要的是逼它把这一步真的发出来。
    这样「推进到/推进至/挪到/改到/升级到」全部自然覆盖，
    **不需要维护任何动词表** —— 动词表必然漏（已在 v1.24.19 验证过）。

    返回 tool_calls 列表；模型仍然只回文字则返回 None。

    ⚠️ 为什么要把原话回灌：必须让它知道自己上一条说了什么，
    否则它可能答非所问（比如重新去查一遍人）。这里把 answer 作为
    assistant 轮放进对话，保持上下文连贯。
    """
    probe = messages + [
        {"role": "assistant", "content": answer},
        {"role": "user", "content":
         "你上一条声称已经完成了一个写操作，但本轮**没有调用任何工具**，"
         "所以系统里并不存在这个操作，界面上也不会有确认按钮。\n"
         "现在只做一件事：**调用正确的工具**把这件事真正提交掉。"
         "参数取你上一条里已经写明的人与目标值。不要解释，不要重复结论。"},
    ]
    try:
        if tools:
            msg = llm.chat(probe, tools=tools, cfg=cfg, tool_choice="required")
        else:
            # 文本工具协议：不传 tools，模型按 system 里的 XML 清单输出
            msg = llm.chat(probe, cfg=cfg)
    except Exception:
        return None
    _accumulate(usage_total, msg.get("_usage"))
    _reason = msg.get("_reasoning") or ""
    if thinking is not None and _reason:
        thinking.append(_reason.strip())
    calls = msg.get("tool_calls") or []
    if not calls:
        # 有些端点会把工具调用写成正文（见文件头 `_text_tool_calls`）
        calls, _ = _text_tool_calls(msg.get("content") or "")
    if not calls:
        return None
    # 把这一轮也接进对话，保持顺序合法（assistant 的 tool_calls 必须带 id）。
    # MiMo 等推理模型要求把 `reasoning_content` 原样回传，否则多轮工具调用 400。
    messages.append({"role": "assistant", "content": answer})
    _asst = {"role": "assistant", "content": "",
             "tool_calls": calls}
    if _reason:
        _asst["reasoning_content"] = _reason
    messages.append(_asst)
    return calls


def _summarize_actions(trace: list[dict]) -> str:
    """强制补调之后，用**确定性**文案把实际发生了什么说清楚。

    只在模型第二轮仍没给出可读答复时使用 —— 宁可说一句由事实拼成的话，
    也不要留一段未经执行的描述给 HR。
    """
    writes = [t for t in trace if t.get("write")]
    if not writes:
        return "这一轮没有产生任何需要确认的操作。请再说一次你要做什么，我来提交。"
    names = {"set_stage": "推进阶段", "set_tier": "调整档位", "add_tag": "添加标签",
             "add_note": "添加备注", "merge_candidates": "合并档案"}
    done = []
    for t in writes:
        r = t.get("result")
        if isinstance(r, str):
            try:
                r = json.loads(r)
            except Exception:
                r = {}
        if isinstance(r, dict) and r.get("proposal_id"):
            done.append(f"「{names.get(t['tool'], t['tool'])}」提案 #{r['proposal_id']}"
                        f"（{r.get('summary') or ''}）")
    if not done:
        return "操作没有提交成功，请把目标再说明一次（例如「把 #65 推进到初面」）。"
    return ("已提交待 HR 确认：" + "、".join(done)
            + "。点下面的「确认执行」才会真正生效。")


def _recover_missed_action(answer: str, user_message: str, ctx, trace: list[dict]) -> str:
    """模型零工具调用、却在声称做了某个写操作 —— **替它把这一步真的做完**。

    为什么必须动手而不能只提醒（v1.24.19）：
    上一版只在出口加一句"我没真做"，HR 看到的仍是"系统说要改却没改、
    底下没有按钮"。**提醒解决不了"按钮不出现"**，只有把提案真的落库才行。

    两条红线：
      1. **不猜人**。只在答复里出现 `#数字` 时才补跑 —— 模型能报出 ID，
         说明档案确实存在且它见过。名字可能是重名，猜错就是替HR 做了错决定。
      2. **不绕过确认**。补跑的是同一个 `execute()`，`set_stage` 之类仍然
         只生成待确认提案，确认按钮还得 HR 自己点。自动化的只是"提案"这一步。

    抽不出人或动作就返回空串，交给出口的提醒逻辑处理 —— 宁可少做，不可做错。
    """
    a = answer or ""
    if not a:
        return ""

    # ---- 1) 目标动作与目标值 ----
    # ⚠️ **不要用正则去分组抽阶段名**——这是本项目反复栽的那类错。
    # 实测手写正则 `从「X」推进到「Y」` 把「到」吞进了第一组：
    #   ('新投递', '到「已联系')   ← 第二组不是合法阶段名，直接失效
    # 而"推进/推进到/推/调整/变更/改/推到/进入"这种枚举**永远列不全**
    # （模型还会写"改到""挪到""推到""升级到"）。
    #
    # 正确做法：**STAGES 是白名单，直接扫**。先按位置定出
    # "X 之后紧跟的那个动词"，再取动词后面的阶段名；
    # 阶段名只认STAGES 里的字面值 —— 认不出就不补，绝不猜。
    verb_m = re.search(
        r"(?:推进(?:到)?|调整(?:到)?|变更(?:到)?|改(?:到)?|推到|进入|挪到|升级到|变更为|改为)"
        r"\s*[「『\"']?", a)
    if not verb_m:
        return ""
    # 动词之后的片段，截到"提案/阶段/。"为止
    tail = a[verb_m.end():verb_m.end() + 24]
    new_stage = ""
    for st in sorted(STAGES, key=len, reverse=True):
        if tail.startswith(st):
            new_stage = st
            break
    if not new_stage:
        return ""

    # 当前阶段：从动词**之前**最近一个 STAGES 字面值取。
    # 取不到就留空 —— 空值只跳过"阶段是否相符"的校验，不会误放。
    cur_stage = ""
    head = a[:verb_m.start()]
    for st in sorted(STAGES, key=len, reverse=True):
        pos = head.rfind(st)
        if pos >= 0:
            cur_stage = st
            break

    # ---- 2) 目标投递（application_id，不是 candidate_id）----
    idm = _RECOVER_ID_RE.search(a)
    if not idm:
        return ""
    aid = int(idm.group(1))

    reason = ""
    rm = re.search(r"(?:变更理由|理由|原因)\s*[:：]\s*([^\n]{1,120})", a)
    if rm:
        reason = rm.group(1).strip()

    # ---- 3) 确认这个投递确实存在，且当前阶段与模型说的相符 ----
    # ⚠️ **模型报的 ID 可能是 candidate_id 而不是 application_id**
    # （截图里写「候选人：张三（ID: 1024）」，语义上更像人ID）。
    # 两者数值常常不同（真实库里更是完全不同），
    # 所以先按 application_id 试，查不到再回退按 candidate_id 找该人的投递。
    # **回退时若该人多条投递，一律放弃** —— 替 HR 挑一条就是替他做决定。
    conn = db.connect(ctx.db_path)
    try:
        row = conn.execute(
            "SELECT id, stage, candidate_id FROM applications WHERE id=?",
            (aid,)).fetchone()
        if not row:
            cand_rows = conn.execute(
                "SELECT id, stage, candidate_id FROM applications "
                "WHERE candidate_id=? ORDER BY id", (aid,)).fetchall()
            if len(cand_rows) == 1:
                row = cand_rows[0]
            else:
                # 0 条（人没了）或 ≥2 条（投了多个岗位）→ 不猜
                return ""
        if cur_stage and row["stage"] and cur_stage != row["stage"]:
            return ""            # 模型记错了当前阶段，别替它写提案
        cand = conn.execute(
            "SELECT name FROM candidates WHERE id=?", (row["candidate_id"],)
        ).fetchone()
        who = cand["name"] if cand else f"#{row['candidate_id']}"
    except Exception:
        return ""
    finally:
        conn.close()

    if row["stage"] == new_stage:
        return ""                # 已经在目标阶段了，没必要再提一遍

    # ---- 4) 真的补跑一次工具（与模型走的是同一条路径）----
    raw = ""
    try:
        raw = execute("set_stage", {"application_id": aid, "stage": new_stage,
                                    "reason": reason or "HR 指示推进阶段"}, ctx)
        out = json.loads(raw)
    except Exception as exc:
        raw = raw or json.dumps({"error": str(exc)}, ensure_ascii=False)
        out = {"error": str(exc)}
    trace.append({"tool": "set_stage",
                  "args": {"application_id": aid, "stage": new_stage},
                  "result": raw[:900], "write": True,
                  "disabled": False, "recovered": True})

    if out.get("error"):
        return ""
    return (f"已按你的指示，把 **{who}** 的投递 #{aid} 从「{row['stage']}」"
            f"推进到「{new_stage}」，**待确认提案已生成** —— 下面点「确认执行」生效。")


#: v4.0.17：实时调用轨迹（右侧面板轮询）。
#: 只存在进程内，不落库；最终完整 trace 仍随 `/api/agent/chat` 返回并写 agent_runs。
#: key=session_id，value={operator, status, trace:[{tool,tool_label,args,write,disabled}], ts}
_LIVE_TRACES: dict[str, dict] = {}


def _live_update(ctx: ToolCtx, trace: list[dict], status: str = "running",
                 mode: str | None = None, rounds: int = 0,
                 thinking: str = "") -> None:
    sid = str(ctx.session_id or "").strip()
    if not sid:
        return
    now_ts = time.time()
    # 顺手清掉 10 分钟前的残留，避免进程内字典无限增长
    for k in [k for k, v in _LIVE_TRACES.items() if now_ts - float(v.get("ts") or 0) > 600]:
        _LIVE_TRACES.pop(k, None)
    _LIVE_TRACES[sid] = {
        "operator": ctx.operator,
        "status": status,
        "mode": mode,
        "rounds": rounds,
        # 思考过程只用于界面实时展示；最终仍随 /api/agent/chat 返回并落库
        "thinking": thinking or "",
        "route": list(getattr(ctx, "route", []) or []),
        "ts": now_ts,
        "trace": [{
            "tool": t.get("tool"),
            "tool_label": t.get("tool_label") or t.get("tool"),
            "args": t.get("args") or {},
            "write": bool(t.get("write")),
            "disabled": bool(t.get("disabled")),
        } for t in trace],
    }


def live_trace(session_id: str, operator: str) -> dict | None:
    """给右侧轨迹面板读的**当前会话**轨迹；只允许会话所属账号。"""
    v = _LIVE_TRACES.get(str(session_id or ""))
    if not v or str(v.get("operator") or "") != str(operator or ""):
        return None
    return {"status": v.get("status"), "mode": v.get("mode"),
            "rounds": v.get("rounds"), "thinking": v.get("thinking") or "",
            "route": list(v.get("route") or []),
            "trace": list(v.get("trace") or [])}


def _merge_tool_delta(acc: dict, tc: dict) -> None:
    """把流式返回的 tool_call 增量按 index（没有就用 id）拼成完整调用。"""
    idx = tc.get("index")
    if idx is None:
        idx = tc.get("id") or 0
    slot = acc.get(idx)
    if slot is None:
        slot = {"id": "", "type": "function",
                "function": {"name": "", "arguments": ""}}
        acc[idx] = slot
    if tc.get("id"):
        slot["id"] = tc["id"]
    fn = tc.get("function") or {}
    if fn.get("name"):
        slot["function"]["name"] += fn["name"]
    if fn.get("arguments"):
        slot["function"]["arguments"] += fn["arguments"]


def _finalize_tool_calls(acc: dict) -> list[dict]:
    out = []
    for slot in acc.values():
        if not (slot.get("function") or {}).get("name"):
            continue
        if not slot.get("id"):
            slot["id"] = "call_" + uuid.uuid4().hex[:12]
        out.append(slot)
    return out


def _est_tokens(text: str) -> int:
    """粗略 token 估算：中文≈1 字 1 token，英文/符号≈4 字符 1 token。

    只用于右栏"上下文用量"的可视化，精度足够；真实计费仍以模型返回的
    `usage` 为准（`agent_runs.tokens_in/out`）。
    """
    s = str(text or "")
    if not s:
        return 0
    cjk = len(re.findall(r"[一-鿿]", s))
    return max(1, int(cjk + (len(s) - cjk) * 0.28))


def build_context_estimate(*, window: int = 1_000_000, messages_text: str = "",
                           allowed_specs: list[dict] | None = None,
                           text_tools: bool = False,
                           skill: dict | None = None,
                           memory: str = "",
                           model: str = "",
                           params: dict | None = None,
                           window_source: str = "config",
                           model_params: str = "") -> dict:
    """构造右栏「上下文」面板的数据（估算，不调模型）。"""
    try:
        win = int(window or 1_000_000)
    except (TypeError, ValueError):
        win = 1_000_000
    specs = allowed_specs or []
    sys_tools_text = (tool_catalog_text(specs) if text_tools
                      else json.dumps(specs, ensure_ascii=False))
    parts = [
        {"key": "messages", "label": "对话消息", "tokens": _est_tokens(messages_text)},
        {"key": "system_prompt", "label": "系统提示词", "tokens": _est_tokens(SYSTEM_PROMPT)},
        {"key": "system_tools", "label": "工具说明", "tokens": _est_tokens(sys_tools_text)},
        {"key": "skills", "label": "SOP / 技能",
         "tokens": _est_tokens((skill["sop"] + skill["output"]) if skill else "")},
        {"key": "memory", "label": "会话记忆", "tokens": _est_tokens(memory)},
    ]
    used = sum(p["tokens"] for p in parts)
    parts.append({"key": "free", "label": "剩余空间", "tokens": max(0, win - used)})
    return {"window": win, "used": used,
            "percent": round(used * 100.0 / win, 1) if win else 0,
            "parts": parts, "source": "estimate",
            "model": str(model or ""), "params": dict(params or {}),
            "model_params": str(model_params or ""),
            "window_source": window_source}


_SIMPLE_GREET_RE = re.compile(
    r"^(你好|您好|hi|hello|嗨|哈喽|在吗|在不在|早上好|上午好|下午好|晚上好|早安|晚安)"
    r"[\s!！。.~～,，]*$", re.I)
_SIMPLE_THANKS_RE = re.compile(
    r"^(谢谢|多谢|感谢|thanks|thank you|thx)[\s!！。.~～,，]*$", re.I)
_SIMPLE_BYE_RE = re.compile(
    r"^(再见|拜拜|bye|goodbye|回头见)[\s!！。.~～,，]*$", re.I)
_SIMPLE_ACK_RE = re.compile(
    r"^(好|好的|好吧|行|可以|收到|明白|了解|嗯|哦|ok|okay)[\s!！。.~～,，]*$", re.I)


def _simple_reply(message: str) -> str | None:
    """问候/致谢/告别/乱码 → 固定短回复，不调模型。

    目的不是"更聪明"，而是**别把一句"你好"也拖成 10 秒的模型思考**。
    只匹配整句，不做子串匹配："你好，帮我看下岗位"仍走正常流程。
    """
    s = re.sub(r"\s+", "", str(message or ""))
    if not s:
        return "可以直接说：查谁、哪个岗位，或者要做什么。"
    if _SIMPLE_GREET_RE.match(s):
        return "你好，我是人才库智能助手。查人、看招聘进度、导出名单都可以直接说。"
    if _SIMPLE_THANKS_RE.match(s):
        return "不客气，还有需要直接说。"
    if _SIMPLE_BYE_RE.match(s):
        return "好的，随时再叫我。"
    if _SIMPLE_ACK_RE.match(s):
        return "好的。"
    # 纯标点、单字符重复、纯数字、无中文的键盘乱码（短、单个长 Latin 词）
    if len(s) <= 24 and re.fullmatch(r"[?？!！。.、，,~～]+", s):
        return "没太看懂。可以直接说：查谁、哪个岗位、要做什么。"
    if re.fullmatch(r"(.)\1{2,}", s):
        return "没太看懂。可以直接说：查谁、哪个岗位、要做什么。"
    if re.fullmatch(r"\d{1,6}", s):
        return "没太看懂。可以直接说：查谁、哪个岗位、要做什么。"
    if len(s) <= 24 and not re.search(r"[一-鿿0-9]", s):
        words = re.findall(r"[a-z]+", s.lower())
        _known = {"hi", "hello", "ok", "okay", "hr", "jd", "offer", "excel",
                  "word", "pdf", "python", "java", "sql", "ppt", "cet", "cet4", "cet6"}
        if words and len(words) == 1 and len(words[0]) >= 5 and words[0] not in _known:
            return "没太看懂。可以直接说：查谁、哪个岗位、要做什么。"
    return None


def _tools_unsupported(exc: Exception) -> bool:
    """端点是否明确表示"不支持 tools / function calling"。

    只认 400/422 + tool/function 关键字，避免把网络错误、限流、鉴权失败
    误判成"换文本协议就好了"——那会白白多发一次请求。
    """
    text = str(exc).lower()
    code = ""
    if isinstance(exc, urllib.error.HTTPError):
        code = str(getattr(exc, "code", "") or "")
        try:
            text += " " + exc.read().decode("utf-8", "ignore").lower()
        except Exception:                                    # noqa: BLE001
            pass
    if code and code not in ("400", "422"):
        return False
    return ("tool" in text or "function" in text) and any(
        k in text for k in ("unsupport", "not support", "invalid", "unknown",
                            "unrecognized", "does not support", "不支持"))


def _tool_ok(result: str) -> bool:
    try:
        d = json.loads(result)
    except (TypeError, ValueError):
        return True
    return not (isinstance(d, dict) and d.get("error"))


def _result_brief(result: str) -> str:
    """工具结果给前端的一行摘要（不泄露长 JSON）。"""
    try:
        d = json.loads(result)
    except (TypeError, ValueError):
        return str(result or "")[:160]
    if isinstance(d, dict):
        for k in ("summary", "note", "message", "error", "hint"):
            if d.get(k):
                return str(d[k])[:180]
        if d.get("count") is not None:
            return f"{d['count']} 条"
        if isinstance(d.get("items"), list):
            return f"{len(d['items'])} 条"
    return str(result or "")[:160]


def run_agent(user_message: str, ctx: ToolCtx, history: list[dict] | None = None,
              emit=None, should_stop=None) -> dict:
    started = time.time()
    ctx.session_id = ctx.session_id or uuid.uuid4().hex[:12]
    # 问候/致谢/告别/乱码：固定短回复，绝不为了"你好"跑一轮模型和工具。
    _simple = _simple_reply(user_message)
    if _simple:
        return _finalize(ctx, user_message, _simple, [], "simple", 0, {},
                         started, thinking="")
    cfg = llm.load_cfg()

    if not cfg.get("enabled", True):
        trace: list[dict] = []
        answer = _rule_answer(user_message, ctx, trace=trace)
        _live_update(ctx, trace, status="done", mode="offline", rounds=0)
        return _finalize(ctx, user_message, answer, trace, "offline", 0, {}, started,
                         thinking="")

    # 模型无关的工具路由：68 个工具按意图收成 10~22 个。
    # 没有命中任何关键词时给一组覆盖面最广的读工具，避免"问什么都缺工具"。
    _allowed_specs, _route_groups = route_tools(user_message, history)
    # 业务 SOP：命中就注入固定步骤，并把该 SOP 需要的工具补进本轮可见集。
    # 不额外调用模型，不增加一轮延迟；只是让模型少走弯路。
    _skill = match_skill(user_message, history)
    if _skill:
        ctx.skill, ctx.skill_name = _skill["id"], _skill["name"]
        _have = {s["function"]["name"] for s in _allowed_specs}
        for _s in TOOL_SPECS:
            _n = _s["function"]["name"]
            if _n in _skill["tools"] and _n not in _have:
                _allowed_specs.append(_s)
                _have.add(_n)
        _route_groups = list(_route_groups) + ["skill:" + _skill["id"]]
    ctx.route = _route_groups
    _tool_mode = str(cfg.get("tool_mode") or "auto").lower()
    _text_tools = _tool_mode in ("text", "prompt", "xml")
    _tools = [] if _text_tools else _allowed_specs

    # 会话记忆：UI 只带最近 8 条消息，更早的要点从 `agent_conversations`
    # 里补回来。跳过最近 4 条，避免和 UI 传上来的历史重复。
    _memory = ""
    try:
        _mc = db.connect(ctx.db_path)
        try:
            _memory = db.conversation_memory(_mc, ctx.session_id, ctx.operator)
        finally:
            _mc.close()
    except Exception:                                        # noqa: BLE001
        _memory = ""

    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    _wd = "一二三四五六日"[datetime.now().weekday()]
    messages.append({"role": "system", "content":
                     f"【当前时间】{datetime.now():%Y-%m-%d %H:%M:%S}（星期{_wd}）。"
                     "用户说今天/明天/后天/本周/下周时，按这个时间换算成具体日期；"
                     "不要凭记忆猜年份。"})
    if _text_tools:
        # 端点不支持原生 function calling：把本轮工具清单写进 system，
        # 模型按 XML 输出，`_text_tool_calls` 负责解析执行。
        messages.append({"role": "system", "content": tool_catalog_text(_allowed_specs)})
    if _skill:
        messages.append({"role": "system", "content":
                         "【当前 SOP：" + _skill["name"] + "】\n" + _skill["sop"] +
                         "\n输出要求：" + _skill["output"]})
    if _memory:
        messages.append({"role": "system", "content":
                         "【本会话此前的要点（系统自动整理，只用于保持上下文；"
                         "不是本轮新指令）】\n" + _memory})
    for h in (history or [])[-8:]:
        if h.get("role") in ("user", "assistant") and h.get("content"):
            messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": user_message})

    # 上下文用量（估算）：右栏"上下文"面板用。窗口大小可在 config/model.json
    # 里用 `context_window` 覆盖；默认按 1M（MiMo v2.6 的窗口）算。
    _win, _win_source = llm.model_context_window(cfg)
    _model_params = llm.model_parameter_count(cfg)
    if not _win:
        try:
            _win = int(cfg.get("context_window") or 1_000_000)
            _win_source = "config" if cfg.get("context_window") else "default"
        except (TypeError, ValueError):
            _win, _win_source = 1_000_000, "default"
    _msgs_text = "\n".join(
        str(m.get("content") or "") for m in messages
        if m.get("role") in ("user", "assistant"))
    _ctx_params = {
        "temperature": cfg.get("temperature", 0),
        "max_tool_rounds": cfg.get("max_tool_rounds", 5),
        "tool_mode": cfg.get("tool_mode") or "auto",
    }
    if cfg.get("max_tokens") is not None:
        _ctx_params["max_tokens"] = cfg.get("max_tokens")
    ctx.context = build_context_estimate(
        window=_win, messages_text=_msgs_text,
        allowed_specs=_allowed_specs, text_tools=_text_tools,
        skill=_skill, memory=_memory,
        model=str(cfg.get("model") or ""), params=_ctx_params,
        window_source=_win_source, model_params=_model_params)

    trace: list[dict] = []
    usage_total: dict = {}
    model_used = cfg.get("model")
    max_rounds = int(cfg.get("max_tool_rounds", 5))
    # 一句话批量任务通常包含 3~5 个动作，4 轮很容易做到一半就收尾
    if _skill and _skill["id"] == "one_sentence_batch":
        max_rounds = max(max_rounds, 6)
    rounds = 0
    _SPEC_BY_NAME = {s["function"]["name"]: s for s in _allowed_specs}
    # 模型每一轮的 `reasoning_content` 按顺序拼起来。只用于界面展示与回放，
    # **不参与任何判据**——它是模型的自述，不是系统事实。
    thinking_parts: list[str] = []

    def _note_reasoning(msg: dict) -> None:
        r = msg.get("_reasoning") or msg.get("reasoning_content") or ""
        if isinstance(r, str) and r.strip():
            thinking_parts.append(r.strip())

    def _thinking_text() -> str:
        return "\n\n".join(thinking_parts)

    def _activate_text_mode() -> None:
        """端点拒绝原生 tools 时，切到 XML 文本协议（只切一次）。"""
        nonlocal _text_tools, _tools
        if _text_tools:
            return
        _text_tools = True
        _tools = []
        messages.append({"role": "system",
                         "content": tool_catalog_text(_allowed_specs)})

    _stream_answer = False

    def _emit(ev: dict) -> None:
        if emit:
            try:
                emit(ev)
            except Exception:                                # noqa: BLE001
                pass          # 前端断开不该拖垮后端

    def _cancelled() -> bool:
        try:
            return bool(should_stop and should_stop())
        except Exception:                                    # noqa: BLE001
            return False

    def _replace_answer(text: str) -> None:
        """清掉前端已显示的流式答案，再推最终版（安全网改写时用）。"""
        nonlocal _stream_answer
        if not emit:
            return
        _emit({"type": "answer_reset"})
        if text:
            _emit({"type": "answer_delta", "text": text})
        _stream_answer = bool(text)

    def _turn(msgs: list[dict], tools: list[dict] | None):
        """一次模型调用。有 emit 时走流式并逐块推送；失败自动退回非流式。"""
        nonlocal _stream_answer, model_used
        if not emit:
            try:
                return llm.chat(msgs, tools=tools, cfg=cfg)
            except Exception as exc:                         # noqa: BLE001
                if tools and _tools_unsupported(exc):
                    _activate_text_mode()
                    _m = (msgs if msgs is messages else
                          msgs + [{"role": "system",
                                   "content": tool_catalog_text(_allowed_specs)}])
                    return llm.chat(_m, tools=None, cfg=cfg)
                raise
        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        calls_acc: dict = {}
        try:
            for ev in llm.chat_stream(msgs, tools=tools, cfg=cfg):
                if ev.get("model"):
                    model_used = ev["model"] or model_used
                if ev.get("usage"):
                    _accumulate(usage_total, ev["usage"])
                delta = ev.get("delta") or {}
                r = delta.get("reasoning_content") or delta.get("reasoning") or ""
                if isinstance(r, str) and r:
                    reasoning_parts.append(r)
                    _emit({"type": "thinking_delta", "text": r})
                c = delta.get("content") or ""
                if isinstance(c, str) and c:
                    content_parts.append(c)
                    _emit({"type": "answer_delta", "text": c})
                    _stream_answer = True
                for tc in (delta.get("tool_calls") or []):
                    _merge_tool_delta(calls_acc, tc)
        except Exception as exc:                            # noqa: BLE001
            # 只要还没给用户看到任何文字，就退回非流式重来；
            # 已经显示了一半再换协议会造成重复/截断，宁可把错误抛给上层。
            if reasoning_parts or content_parts:
                raise
            if tools and _tools_unsupported(exc):
                _activate_text_mode()
                _m = (msgs if msgs is messages else
                      msgs + [{"role": "system",
                               "content": tool_catalog_text(_allowed_specs)}])
                return llm.chat(_m, tools=None, cfg=cfg)
            # 端点不支持流式（或直接报错）→ 用同一条消息退回非流式，
            # 保证换任何通用 LLM 都不会因为 stream 参数把对话打断。
            return llm.chat(msgs, tools=tools, cfg=cfg)
        msg = {"content": "".join(content_parts),
               "tool_calls": _finalize_tool_calls(calls_acc),
               "_reasoning": "".join(reasoning_parts)}
        # 什么都没收到（有的端点返回 200 但空流）→ 退回非流式
        if not msg["content"] and not msg["tool_calls"] and not msg["_reasoning"]:
            msg = llm.chat(msgs, tools=tools, cfg=cfg)
        elif msg["content"] and not msg["tool_calls"]:
            # 正文已经出完，后面只剩服务端的收尾（安全网/落库）。
            # 前端据此把状态改成"正在收尾"，不再显示成"还在生成"。
            _emit({"type": "answer_complete"})
        return msg

    def _run_calls(calls: list[dict]) -> int:
        """执行一批 tool_calls，写进 trace 与 messages。返回执行了几个。

        抽成函数是为了让"正常路径"和下面的"强制补调路径"走**完全相同**的执行逻辑
        —— 否则两条路容易长歪（一条改了权限校验、另一条没改）。
        """
        prepared = []
        for tc in calls:
            fn = (tc.get("function") or {}).get("name", "")
            try:
                args = json.loads((tc.get("function") or {}).get("arguments") or "{}")
            except ValueError:
                args = {}
            if not isinstance(args, dict):
                args = {}
            spec = _SPEC_BY_NAME.get(fn)
            result = None
            if not spec:
                result = json.dumps({
                    "error": f"未知工具「{fn}」",
                    "hint": "只能用本轮提供的工具；需要别的能力时先向用户说明。",
                    "available": sorted(_SPEC_BY_NAME)[:30],
                }, ensure_ascii=False)
            else:
                required = ((spec.get("function") or {}).get("parameters") or {}) \
                    .get("required") or []
                missing = [k for k in required
                           if k not in args or args[k] is None
                           or args[k] == "" or args[k] == []]
                if missing:
                    result = json.dumps({
                        "error": "缺少必填参数：" + "、".join(missing),
                        "tool": fn, "missing": missing,
                        "hint": "补齐参数后重新调用这个工具，不要改用其它工具。",
                    }, ensure_ascii=False)
            prepared.append({"tc": tc, "fn": fn, "args": args, "result": result})

        def _start(p: dict) -> None:
            _emit({"type": "tool_start", "id": p["tc"].get("id", ""),
                   "tool": p["fn"], "tool_label": _TOOL_LABEL.get(p["fn"], p["fn"]),
                   "args": p["args"]})

        def _record(p: dict) -> None:
            fn, result = p["fn"], p["result"]
            # v1.13.9：邮件草稿要**完整**带回（正文会被 900 字截断，
            # 截断了前端就渲染不出卡片）；其余工具仍然只留摘要。
            trace.append({"tool": fn, "args": p["args"],
                          "result": (result if fn in _FULL_RESULT_TOOLS else result[:900]),
                          "tool_label": _TOOL_LABEL.get(fn, fn),
                          "write": fn in WRITE_TOOLS,
                          # 结果可能被截到 900 字，不能再靠 json.loads 判断成败；
                          # 在截断前把 ok / download 记下来。
                          "ok": _tool_ok(result),
                          "download": _download_of({"result": result}),
                          "disabled": fn in DISABLED_TOOLS})
            _emit({"type": "tool_result", "id": p["tc"].get("id", ""),
                   "tool": fn, "tool_label": _TOOL_LABEL.get(fn, fn),
                   "ok": _tool_ok(result), "summary": _result_brief(result)})
            _live_update(ctx, trace, thinking=_thinking_text())
            messages.append({"role": "tool", "tool_call_id": p["tc"].get("id", ""),
                             "name": fn, "content": result})

        runnable = [p for p in prepared if p["result"] is None]
        # 同一批里全是只读工具 → 并行执行。5 次读取档案串行虽然每次不慢，
        # 但叠起来 + 模型每轮思考会让用户觉得"卡死"；并行是纯收益。
        parallel = (len(runnable) > 1 and all(
            p["fn"] not in WRITE_TOOLS and p["fn"] not in DISABLED_TOOLS
            for p in runnable))
        if parallel:
            for p in prepared:
                _start(p)
            with ThreadPoolExecutor(max_workers=min(6, len(runnable))) as ex:
                futs = {ex.submit(execute, p["fn"], p["args"], ctx): p for p in runnable}
                for f in as_completed(futs):
                    p = futs[f]
                    try:
                        p["result"] = f.result()
                    except Exception as exc:                 # noqa: BLE001
                        p["result"] = json.dumps(
                            {"error": f"{type(exc).__name__}: {exc}"},
                            ensure_ascii=False)
            for p in prepared:
                _record(p)
        else:
            for p in prepared:
                _start(p)
                if p["result"] is None:
                    try:
                        p["result"] = execute(p["fn"], p["args"], ctx)
                    except Exception as exc:                 # noqa: BLE001
                        p["result"] = json.dumps(
                            {"error": f"{type(exc).__name__}: {exc}"},
                            ensure_ascii=False)
                _record(p)
        return len(prepared)

    try:
        for _ in range(max_rounds):
            if _cancelled():
                return _finalize(ctx, user_message, "已停止。", trace, "stopped",
                                 rounds, usage_total, started, model_used,
                                 thinking=_thinking_text())
            rounds += 1
            msg = _turn(messages, _tools)
            model_used = msg.get("_model") or model_used
            _accumulate(usage_total, msg.get("_usage"))
            _note_reasoning(msg)
            calls = msg.get("tool_calls") or []
            if not calls:
                # 端点把工具调用写进正文时，**先拆出来真的执行**，
                # 而不是把这段 XML 当成答复显示给 HR。
                calls, _clean = _text_tool_calls(msg.get("content") or "")
                if calls:
                    msg["content"] = _clean
            elif msg.get("content"):
                # 原生 tool_calls 与正文里的 XML 同时出现时，正文也要清掉，
                # 否则下一轮回灌给模型的上下文里还带着一段伪工具调用。
                msg["content"] = _strip_tool_call_text(msg.get("content") or "")
            if not calls:
                answer = _strip_tool_call_text(msg.get("content") or "").strip()
                if not answer:
                    raise ValueError("模型返回了空答复")
                _streamed_answer = answer      # 前端已经流式看到的版本

                # 所有“出口检测/补做”兜底已停用（业务方 2026-11-05）：
                # 它们会在正常导出、进度、名单回答上误判，并把 LLM 原文替换成
                # 系统拼接的文案（例如“这一轮没有产生任何需要确认的操作”）。
                # 现在回答完全以模型本轮输出为准；写操作仍由模型显式调工具生成提案。
                if emit and answer != _streamed_answer:
                    _replace_answer(answer)
                return _finalize(ctx, user_message, answer, trace, "llm", rounds,
                                 usage_total, started, model_used,
                                 thinking=_thinking_text())
            if emit and _stream_answer:
                _replace_answer("")     # 这一轮只是"先说一句再调工具"，清掉再执行
            _asst = {"role": "assistant", "content": msg.get("content") or "",
                     "tool_calls": calls}
            # MiMo 等推理模型：带 tool_calls 的 assistant 轮必须把
            # reasoning_content 原样回传，否则下一轮直接 400。
            if msg.get("_reasoning"):
                _asst["reasoning_content"] = msg["_reasoning"]
            messages.append(_asst)
            if _cancelled():
                return _finalize(ctx, user_message, "已停止。", trace, "stopped",
                                 rounds, usage_total, started, model_used,
                                 thinking=_thinking_text())
            _run_calls(calls)
        # 轮次用尽，强制收敛；不再做邮件/口头提案等出口兜底，
        # 最终答复以模型输出为准。
        messages.append({"role": "user", "content": "请基于已获取的信息直接给出最终答复。"})
        final = _turn(messages, None)
        _accumulate(usage_total, final.get("_usage"))
        _note_reasoning(final)
        rounds += 1
        _fa = _strip_tool_call_text(final.get("content") or "").strip()
        _streamed_answer = _fa
        # 数据来源兜底已停用，见上方说明；不再替换最终答复。
        if emit and _fa != _streamed_answer:
            _replace_answer(_fa)
        return _finalize(ctx, user_message, _fa,
                         trace, "llm", rounds, usage_total, started, model_used,
                         thinking=_thinking_text())
    except Exception as exc:
        # 模型不可用 → 规则路由兜底（不是罢工）
        st = llm.status()
        reason = st.get("error") or f"{type(exc).__name__}: {exc}"
        answer = _rule_answer(user_message, ctx, reason=reason, trace=trace)
        if emit and _stream_answer:
            _replace_answer(answer)
        return _finalize(ctx, user_message, answer, trace, "rule", rounds,
                         usage_total, started, model_used,
                         thinking=_thinking_text())


def _proposal_id_of(t: dict) -> int | None:
    """从一条 trace 的工具返回里取 proposal_id（没有就 None）。"""
    r = t.get("result")
    if isinstance(r, str):
        try:
            r = json.loads(r)
        except Exception:
            return None
    if isinstance(r, dict) and r.get("proposal_id"):
        try:
            return int(r["proposal_id"])
        except (TypeError, ValueError):
            return None
    return None


def _download_of(t: dict) -> dict | None:
    """从工具返回里提取可下载产物，供历史回放/右栏「本次产出」使用。"""
    if isinstance(t.get("download"), dict):
        return t["download"]
    r = t.get("result")
    if isinstance(r, str):
        try:
            r = json.loads(r)
        except Exception:                                    # noqa: BLE001
            return None
    if isinstance(r, dict) and r.get("download_url"):
        return {"download_url": r["download_url"],
                "scope": r.get("scope") or r.get("label") or ""}
    return None


def _draft_of(t: dict) -> dict | None:
    """从 trace 里取出可渲染成草稿卡的内容（draft_email / preview_mail）。

    两个工具都要：它们的区别只是"内容来自模型还是模板"，
    对 HR 来说都是"一封待确认的邮件"。只带 draft_email 会让
    "按模板写"那条路界面上没有草稿卡（用户反馈过：说了给草稿却没卡片）。
    """
    if t.get("tool") not in ("draft_email", "preview_mail"):
        return None
    r = t.get("result")
    if isinstance(r, str):
        try:
            r = json.loads(r)
        except Exception:
            return None
    if not isinstance(r, dict) or not r.get("body"):
        return None
    # v1.25：必须带上 body_html。前端 openDraftInMailer 用 `d.body_html || esc(d.body)`
    # 填富文本编辑器——free-write 分支如果不带 body_html，正文会被当成转义后的纯文本塞进
    # contenteditable（换行变空格、排版全乱），HR 点「在编辑器中修改」看到的就是一团乱码/像没填。
    # 模板分支本来就有 body_html，free-write 分支 draft_email 也产了 body_html，这里只是"漏带"。
    return {"candidate_id": r.get("candidate_id"), "name": r.get("name"),
            "to": r.get("to"), "subject": r.get("subject"),
            "body": r.get("body"), "body_html": r.get("body_html"), "job": r.get("job"),
            "template": r.get("template"),
            "missing_runtime": r.get("missing_runtime") or []}


#: 邮件意图关键词。命中且能从消息里认出库里的候选人 → 才会触发兜底补调；
#: 光有候选人不算（"查一下赵敏"不是发邮件），光有邮件词也不算（"邮件配置"不是起草）。
_EMAIL_INTENT_RE = re.compile(
    r"邮件|发信|发个|发一封|发一下|发邮件|邀请|邀约|通知|模板|草稿|拟一封|拟个|回执")


def _has_email_draft(trace: list[dict]) -> bool:
    """trace 里是否已经存在**成功**的邮件草稿（有正文才算，报错的不算）。"""
    for t in trace:
        if t.get("tool") in ("draft_email", "preview_mail"):
            r = t.get("result")
            if isinstance(r, str):
                try:
                    r = json.loads(r)
                except Exception:
                    r = {}
            if isinstance(r, dict) and r.get("body"):
                return True
    return False


def _force_email_draft(user_message: str, ctx: ToolCtx, trace: list[dict]) -> str | None:
    """弱模型没调邮件工具、却明显要"给某人发（模板）邮件"时的**确定性兜底**。

    为什么需要（v1.25.x）：本地 qwen 这类弱模型，HR 说"用模板给赵敏发面试邀请"，
    它常常**一轮就停**，直接把模板 HTML 当聊天文字吐出来（截图实测：轮数 1、
    无 tool_calls、聊天框里是一坨 `<table>`、`<{面试时间}>` 占位符）。
    提示词规则（3b/3c）是"劝说"，小模型会不听；这里改在**出口用事实兜底**：
    认得出库里的候选人 + 命中邮件意图 → 系统自己调一次 `draft_email`，
    把草稿塞进 trace，前端就能渲染出带「在编辑器中修改 / 确认并发送」的卡片，
    同时用一句简短确认替换掉那段 raw HTML。

    返回：替换用的确认文案；不插手时返回 None（调用方继续走原逻辑）。
    """
    um = (user_message or "").strip()
    conn = db.connect(ctx.db_path)
    try:
        # 1) 候选人在库里吗？—— 子串匹配姓名（HR 用全名指人，库里也是全名）。
        #    认不出具体人就不瞎调，避免把"张三"的草稿发到"张"身上。
        cands = conn.execute(
            "SELECT id, name FROM candidates WHERE merged_into IS NULL").fetchall()
        hit_cid = hit_name = None
        for c in cands:
            nm = (c["name"] or "").strip()
            if nm and nm in um:
                hit_cid, hit_name = int(c["id"]), nm
                break
        if not hit_cid:
            return None

        # 2) 模板：子串匹配模板名 / 场景；都命不中就取第一个启用模板（宁可给一个，
        #    也比让弱模型自由发挥吐 raw HTML 强）。
        tpls = db.list_mail_templates(conn, enabled_only=True)
        tkey = None
        for t in tpls:
            nm = (t.get("name") or "")
            sc = (t.get("scene") or "")
            if nm and nm in um:
                tkey = nm
                break
            if sc and sc in um:
                tkey = nm
                break
        if not tkey and tpls:
            tkey = tpls[0]["name"]

        args = {"candidate_id": hit_cid}
        if tkey:
            args["template"] = tkey

        # 3) 补调。模板没匹配上（名字像但不唯一）→ 退自由撰写再试一次，
        #    保证"至少给封能发的草稿"而不是空手而归。
        raw = execute("draft_email", args, ctx)
        result = raw if not isinstance(raw, str) else _try_json(raw)
        if result.get("error") and tkey:
            raw2 = execute("draft_email", {"candidate_id": hit_cid}, ctx)
            r2 = raw2 if not isinstance(raw2, str) else _try_json(raw2)
            if not r2.get("error"):
                raw, result, tkey = raw2, r2, None
        if result.get("error"):
            return None

        trace.append({"tool": "draft_email", "args": args,
                      "result": (raw if "draft_email" in _FULL_RESULT_TOOLS else raw[:900]),
                      "tool_label": _TOOL_LABEL.get("draft_email", "draft_email"),
                      "write": False, "disabled": False})
        miss = result.get("missing_runtime") or []
        tpl_used = result.get("template") or "自由撰写"
        return (f"已按系统模板「{tpl_used}」为 **{hit_name}** 生成邮件草稿，"
                f"请在下方卡片里确认，或点「在编辑器中修改」直接改。"
                + (f" 还缺：{'、'.join(miss)}（告诉我具体时间 / 地点后我重渲染一版）。"
                   if miss else ""))
    except Exception:
        return None
    finally:
        conn.close()


def _try_json(s: str) -> dict:
    try:
        v = json.loads(s)
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _email_safety_net(user_message: str, ctx: ToolCtx, trace: list[dict]):
    """判定是否触发邮件兜底。返回 (answer, fired)。fired=True 时调用方应直接 finalize。"""
    if _has_email_draft(trace):
        return None, False
    if not _EMAIL_INTENT_RE.search(user_message or ""):
        return None, False
    ans = _force_email_draft(user_message, ctx, trace)
    if ans:
        return ans, True
    return None, False


# ============================================================
# 数据来源校验（出口兜底）—— v1.25.5
# ============================================================
#
# 用户截图（2026-10-13）：模型在一轮**零工具调用**里输出了「全量对比表」，
# 表格里 12 个"候选人"**全部是虚构的**——数据库、对话历史里都不存在这些名字。
# 三类根因叠加：
#   ① 铁律 1 早有"严禁编造姓名"，但提示词只是**劝说**，模型不听；
#   ② 出口只拦「写操作的虚假完成态」（_looks_like_fake_proposal），
#      **读类**的"已调用工具获取全量数据"从没被拦过；
#   ③ 没有任何"回复内容 ↔ 库内真实数据"的核对。
#
# 判据（**结构位提取 + 白名单比对**，不做 NLP、不猜词义）：
#   · 只从两个"展示姓名的固定位置"提取疑似人名：markdown 表格首列、粗体词；
#   · 词必须"像名字"（常见姓氏开头、非字段词结尾）才进入判定；
#   · 白名单 = 库内全部姓名 ∪ 用户消息里出现 ∪ 本轮工具返回文本里出现；
#   · 白名单外 → 判定虚构。另有独立判据：答复带表格但本轮**没有任何成功的
#     读类工具** → 数据无来源，同样触发。
#
# 触发后的处理（与 _email_safety_net 同款"出口兜底"模式）：
#   1. 系统自己补调 search_candidates（真·全量）拿回真实名单；
#   2. 连同一段"只许用真实数据"的系统指令注入上下文，让模型重写；
#   3. 重写稿再复检；仍出现库外姓名 → 用**确定性文案**接管：
#      明确指出哪些名字查无档案 + 给出数据库里的真实名单。
#      —— 宁可说一句由事实拼成的话，也不让虚构数据留给 HR。
#
# 本版边界：只校验"姓名与来源"（人名幻觉是本次用户点名的形态）。
# 数值级校验（学历/年限等逐字段核对）暂不做——重写时注入了真实数据，
# 且名字全真时编数字的动机大减；先观察拦截日志再决定要不要加。
#
# 误报的代价 = 多一次真实检索 + 一次重写。**故意宁严勿松**：
# 用户原话「出现了编造的数据，这是不可容忍的，智能助手的回答必须有真实数据来源」。

#: 常见姓氏（百家姓高频段）。只用来**降低**误报：提取词首字不在集合里就不判人名。
_SURNAMES = (
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜"
    "戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳史唐费廉"
    "岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟平黄和穆"
    "萧尹姚邵汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞熊纪舒屈项祝董梁杜阮蓝"
    "闵席季麻强贾路娄危江童颜郭梅盛林刁钟徐邱骆高夏蔡田樊胡凌霍虞万支"
    "柯管卢莫房裘解应宗丁宣贲邓郁单杭洪包诸左石崔吉龚程邢裴陆荣翁荀羊"
    "甄曲家封芮储靳松井段富巫乌焦巴牧山谷车侯全班秋仲伊宫宁仇栾甘厉武"
    "符刘景詹束龙叶幸司黎薄印宿白怀蒲鄂索咸赖卓蔺屠蒙池乔阴胥能苍双闻"
    "党翟谭贡劳姬申扶堵冉宰郦雍桑桂牛寿通边扈燕冀浦尚农温别庄晏柴瞿阎"
    "慕连茹习艾鱼容向古易慎戈廖终居衡步都耿满弘匡国文寇广东欧沃利越隆"
    "师巩聂晁勾敖冷辛简饶曾沙鞠丰巢关蒯查红游竺权盖益桓"
)
_SURNAME2 = ("欧阳", "司马", "上官", "诸葛", "东方", "皇甫", "尉迟", "公孙",
             "慕容", "长孙", "宇文", "司徒", "司空", "令狐", "夏侯", "西门")

#: 不会出现在**人名末尾**的字（"高匹配组 / 优先级 / 覆盖率 / 下一步"这类）。
_NAME_TAIL_BLACK = frozenset(
    "组度率性化法则图册页项类档级期区内外上下前后中边单双多少新旧的了的吗呢吧啊么批条次个位种类")

#: 常见非人名（字段词/统计词）——即便出现在表格首列或粗体里也不作人名判定。
#: 注意收进"查/核"开头的功能短语：'查' 是罕见姓氏（zhā），
#: 但"查无档案/查询结果"这类词的出现频率高得多（兜底文案自己就含它们）。
_NAME_STOPWORDS = frozenset({
    "查无档案", "查无此人", "查询结果", "查询记录", "核查结果", "核查记录", "调查结果",
    "姓名", "学历", "年限", "院校", "技能", "档位", "备注", "建议", "档案", "候选人",
    "人才库", "总览", "统计", "指标", "数值", "说明", "合计", "总计", "小计", "平均",
    "分析", "报告", "结论", "原因", "依据", "操作", "步骤", "流程", "规则", "策略",
    "问题", "诊断", "风险", "状态", "来源", "证据", "命中", "缺失", "提示", "名单",
    "高匹配", "低匹配", "高潜力", "低潜力", "匹配度", "高匹配组", "低匹配组",
    "优先", "优先级", "重点", "第一步", "第二步", "下一步", "综上", "综上所述",
    "总结", "小结", "概览", "概况", "背景", "现状", "前言", "目标", "计划", "方法",
    "方案", "行动", "执行", "结果", "数据", "信息", "内容", "细节", "字段", "参数",
    "配置", "设置", "入口", "按钮", "页面", "功能", "权限", "表格", "列表", "清单",
    "分类", "分组", "对比", "差异", "变化", "趋势", "分布", "结构", "构成", "比例",
    "占比", "总数", "总人数", "数量", "人数", "投递", "阶段", "学历结构", "院校层次",
    "结论", "核心结论", "总览", "整体情况", "详细", "缺口", "完成率", "已签", "待签",
    "应招", "应签", "签约", "录用", "入职", "单位", "下一步", "数据来源",
    "材料研发", "数字化", "应届生", "应届", "在职", "离职", "实习", "正式", "试用",
})

#: 表格行首单元格：`| 某某 |` / `| **某某** |`
_TABLE_ROW_RE = re.compile(r"^\s*\|\s*\**([\u4e00-\u9fa5]{2,4})\**\s*\|", re.M)
#: 粗体词：`**某某某**`
_BOLD_TEXT_RE = re.compile(r"\*\*([\u4e00-\u9fa5]{2,4})\*\*")
#: markdown 表格分隔行 `|------|------|`（有它才认为"这是一个数据表"）
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s:]*-{3,}[\s:\-|]*\|?\s*$", re.M)

#: "数据类"读工具——只有这些成功执行过，答复里的候选人数据才算"有来源"。
#: 故意收窄：读了模板/审计/岗位等不算（它们不是候选人数据）。
_READ_DATA_TOOLS = frozenset({
    "search_candidates", "get_candidate", "get_candidates", "pool_stats",
    "search_by_skills", "semantic_search", "similar_candidates",
    "pipeline_overview", "list_jobs", "recruit_overview",
    "recruit_progress", "recruit_gaps", "recruit_roster",
    "recruit_signed_list", "recruit_health", "recruit_years",
})


def _looks_like_name(w: str) -> bool:
    """启发式：这个词看起来像不像人名（用于收敛误报）。"""
    if not (2 <= len(w) <= 4):
        return False
    if w in _NAME_STOPWORDS:
        return False
    if w[0] not in _SURNAMES and w[:2] not in _SURNAME2:
        return False
    if w[-1] in _NAME_TAIL_BLACK:
        return False
    return True


def _extract_claimed_names(answer: str) -> list[str]:
    """从答复中提取"疑似候选人姓名"。

    **只认两个结构位**（表格首列、粗体词）——中文没有词边界，全文扫 2-4 字词
    会把"档案/硕士/建议"全卷进来。**找结构，不找词义**（本项目判据的老规矩）。
    """
    if not answer:
        return []
    raw: list[str] = []
    raw.extend(m.group(1) for m in _TABLE_ROW_RE.finditer(answer))
    raw.extend(m.group(1) for m in _BOLD_TEXT_RE.finditer(answer))
    out: list[str] = []
    for w in raw:
        w = w.strip()
        if _looks_like_name(w) and w not in out:
            out.append(w)
    return out


def _has_markdown_table(answer: str) -> bool:
    return bool(_TABLE_SEP_RE.search(answer or ""))


def _has_ok_read(trace: list[dict]) -> bool:
    """本轮是否有**成功的**数据类读工具（返回里没有 error 才算）。"""
    for t in trace:
        if t.get("tool") not in _READ_DATA_TOOLS:
            continue
        # 新 trace 直接带 ok（结果可能已截断，不能靠解析判断）
        if "ok" in t:
            if t.get("ok"):
                return True
            continue
        r = t.get("result")
        if isinstance(r, str):
            try:
                r = json.loads(r)
            except Exception:
                continue
        if isinstance(r, dict) and not r.get("error"):
            return True
    return False


def _db_names_and_roster(ctx: ToolCtx) -> tuple[set[str], list[dict]]:
    """库内真实名单：姓名全集 + 渲染兜底文案用的行（只读）。"""
    conn = db.connect(ctx.db_path)
    try:
        rows = conn.execute(
            "SELECT name, edu_level, years_exp, school FROM candidates "
            "WHERE merged_into IS NULL AND coalesce(name,'') != '' ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    names: set[str] = set()
    roster: list[dict] = []
    for r in rows:
        nm = (r["name"] or "").strip()
        if not nm:
            continue
        names.add(nm)
        roster.append({"name": nm, "edu": r["edu_level"],
                       "years": r["years_exp"], "school": r["school"]})
    return names, roster


def _trace_text(trace: list[dict]) -> str:
    """本轮全部工具返回拼成一段文本（用于"名字在工具结果里出现过吗"）。"""
    parts: list[str] = []
    for t in trace:
        r = t.get("result")
        parts.append(r if isinstance(r, str)
                     else json.dumps(r, ensure_ascii=False, default=str))
    return "\n".join(parts)


def _truth_fallback_answer(ctx: ToolCtx, bad_names: list[str]) -> str:
    """确定性兜底：明确提出查无档案的名字 + 数据库里的真实名单。

    **不经过模型**——这是最后一道防线；宁可少说，不可说错。
    """
    _, roster = _db_names_and_roster(ctx)
    head = "【系统核对】"
    if bad_names:
        head += ("上一条回复中的 " + "、".join(f"「{w}」" for w in bad_names[:12])
                 + " 在人才库中查无档案——相关内容已拦下，智能助手只使用库内真实数据。")
    else:
        head += "上一条回复中的表格没有任何检索结果支撑，已拦下。"
    if not roster:
        return head + "\n\n当前人才库中没有候选人。"
    body = []
    for it in roster:
        y = "年限未知" if it.get("years") is None else f"{it['years']} 年"
        body.append(f"· {it['name']}（{it.get('edu') or '学历未知'} · {y} · "
                    f"{it.get('school') or '院校未知'}）")
    return (head + f"\n\n以上为数据库中的真实候选人（共 {len(roster)} 人）：\n"
            + "\n".join(body)
            + "\n\n如需对比分析，请直接说名字或条件，我来实时检索。")


def _data_claim_safety_net(user_message: str, answer: str, ctx: ToolCtx,
                           trace: list[dict], messages: list[dict],
                           cfg: dict, usage_total: dict,
                           thinking: list[str] | None = None) -> tuple[str | None, bool]:
    """出口数据来源校验。命中疑似编造 → 补真实数据 → 重写 → 复检 → 兜底。

    返回 (new_answer, fired)。fired=False 时调用方保持原答复不动。
    """
    try:
        a = answer or ""
        claimed = _extract_claimed_names(a)
        table_no_source = bool(a) and _has_markdown_table(a) and not _has_ok_read(trace)
        if not claimed and not table_no_source:
            return None, False

        names_in_db, _roster = _db_names_and_roster(ctx)
        um = user_message or ""
        trace_txt = _trace_text(trace)
        bad = [w for w in claimed
               if w not in names_in_db and w not in um and w not in trace_txt]
        if not bad and not table_no_source:
            return None, False

        # 1) 补调真实全量（读操作，安全；结果并入 trace 供界面与留痕）
        truth = execute("search_candidates", {"limit": 100}, ctx)
        truth_txt = truth if isinstance(truth, str) else json.dumps(
            truth, ensure_ascii=False, default=str)
        trace.append({"tool": "search_candidates",
                      "args": {"limit": 100, "reason": "系统数据来源校验自动补调"},
                      "result": truth_txt[:4000],
                      "tool_label": _TOOL_LABEL.get("search_candidates", "search_candidates"),
                      "write": False, "disabled": False, "recovered": True})

        # 2) 让模型基于真实数据重写（注入用 user 角色，兼容各类兼容端点）
        why = ("你上一条答复里出现了人才库中**查无档案**的名字：" + "、".join(bad)) if bad else \
              "你上一条答复包含候选人数据表格，但本轮没有任何检索结果支撑"
        rewrite_messages = messages + [{"role": "user", "content":
            f"【系统核查】{why}。智能助手只允许使用真实数据，这条内容已被系统拦下。\n"
            "系统已重新检索，结果如下（真实数据，必须以此为准）：\n"
            f"{truth_txt}\n\n"
            "请用简体中文重写答复：只允许出现上述检索结果里真实存在的姓名与数据；"
            "检索结果没有的内容（如某人的技能命中、档位）就说明需要另行查询，"
            "不要补全、不要猜测；若用户要求的表格无法用真实数据完成，如实说明缺什么。"}]
        new_a = ""
        try:
            rw = llm.chat(rewrite_messages, cfg=cfg)
            _accumulate(usage_total, rw.get("_usage"))
            _rw_reason = rw.get("_reasoning") or ""
            if thinking is not None and _rw_reason:
                thinking.append(_rw_reason.strip())
            _rw_txt = rw.get("content") or ""
            _rw_calls = rw.get("tool_calls") or []
            if not _rw_calls:
                _rw_calls, _rw_txt = _text_tool_calls(_rw_txt)
            new_a = _strip_tool_call_text(_rw_txt).strip()
            # 模型可能顺手又调工具：执行掉保持一致（正常不应发生）
            for tc in _rw_calls:
                fn = (tc.get("function") or {}).get("name", "")
                try:
                    _args = json.loads((tc.get("function") or {}).get("arguments") or "{}")
                except ValueError:
                    _args = {}
                execute(fn, _args, ctx)
        except Exception:
            new_a = ""

        # 3) 复检；仍不干净 → 确定性兜底（不经过模型）
        if new_a:
            names2, _ = _db_names_and_roster(ctx)
            bad2 = [w for w in _extract_claimed_names(new_a)
                    if w not in names2 and w not in um and w not in trace_txt
                    and w not in truth_txt]
            if not bad2:
                return new_a, True
        return _truth_fallback_answer(ctx, bad), True
    except Exception:
        # 校验器自身出问题时不改变原答复（保守放行），但绝不让异常击穿整轮对话
        return None, False


def _accumulate(total: dict, usage: dict | None) -> None:
    if not usage:
        return
    for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
        if usage.get(k):
            total[k] = total.get(k, 0) + int(usage[k])
    # 各家缓存命中的字段名不同：OpenAI 在 prompt_tokens_details.cached_tokens，
    # DeepSeek 用 prompt_cache_hit_tokens，Anthropic 兼容层用 cache_read_input_tokens。
    hit = usage.get("cache_hit_tokens")
    reported = hit is not None
    if hit is None:
        details = usage.get("prompt_tokens_details")
        if isinstance(details, dict) and "cached_tokens" in details:
            hit = details.get("cached_tokens")
            reported = True
    if hit is None:
        hit = usage.get("prompt_cache_hit_tokens")
        reported = hit is not None
    if hit is None:
        hit = usage.get("cache_read_input_tokens")
        reported = hit is not None
    if reported:
        total["cache_reported"] = 1
        total["cache_hit_tokens"] = total.get("cache_hit_tokens", 0) + int(hit or 0)


def _proposal_brief(p: dict, conn=None) -> dict:
    """提案的对外形状。

    多带一个 `candidate_id` 是为了让对话里的提案卡能直接开档案：
    HR 看到"把陈志远推进到初面"，得能点进去确认**这确实是陈志远的档案**，
    否则点「确认执行」就是在盲签。取不到就返回 None，卡片自动不显示该按钮。

    ⚠️ **不能把 application_id 直接当 candidate_id 用**。
    set_stage 改的是「投递」，字段名是 application_id；`showDetail()` 要的是
    candidate_id。demo 库里两者数值恰好都是 1，**看着能用**，
    但真实库一旦有同一人投了两个岗位（id 不再对齐），点「打开档案核对」
    就会打开**另一个人的档案**——而 HR 正要点「确认执行」。
    所以必须查表转换；查不到宁可不给按钮。
    """
    cid = None
    a = p.get("args")
    if isinstance(a, str):
        try:
            a = json.loads(a)
        except Exception:
            a = None
    if isinstance(a, dict):
        def _as_int(v):
            try:
                return int(v) if v is not None else None
            except (TypeError, ValueError):
                return None
        cid = _as_int(a.get("candidate_id"))
        if cid is None and conn is not None:
            # 投递 id → 候选人 id，必须查表
            aid = _as_int(a.get("application_id"))
            if aid is not None:
                row = conn.execute(
                    "SELECT candidate_id FROM applications WHERE id=?",
                    (aid,)).fetchone()
                if row:
                    cid = _as_int(row["candidate_id"])
    return {"proposal_id": p["id"], "tool": p["tool"],
            # v1.25：给卡片一个**中文名**。原来显示的是 `set_stage` 这种函数名，
            # HR 不需要看懂代码标识符。取不到就回落到原名，不至于空白。
            "tool_label": _TOOL_LABEL.get(p["tool"], p["tool"]),
            "summary": p["summary"],
            "risk": p["risk"],
            # 状态必须带出来：历史回放里已执行/已拒绝的提案不能还显示可点的按钮。
            "status": p.get("status") or "待确认",
            "candidate_id": cid,
            # v1.25：结构化前后值，让 HR 在卡片上就能核对完，不必跳去档案页盲签。
            "changes": _proposal_changes(p, a if isinstance(a, dict) else {}, conn)}


#: 提案工具名 → 中文。**定义在 db 层**，因为实时对话与历史回放都要用；
#: 分成两份迟早分叉（本项目吃过"两处定义不一致"的亏）。
_TOOL_LABEL = db.TOOL_LABEL


def _proposal_changes(p: dict, a: dict, conn) -> list[dict]:
    """实时对话路径的前后值计算 —— 直接复用 db 层的实现，不另写一份。"""
    return db.proposal_changes(conn, {"tool": p.get("tool"), "args": a})

def _finalize(ctx: ToolCtx, question: str, answer: str, trace: list[dict], mode: str,
              rounds: int, usage: dict, started: float,
              model: str | None = None, thinking: str = "") -> dict:
    latency = int((time.time() - started) * 1000)
    # 文本形式的工具调用绝不能出现在答复里（模型偶尔会漏成裸标签）
    answer = _strip_tool_call_text(answer)
    if not answer:
        answer = "模型这一轮没有返回可显示的内容。请换一种说法再试一次。"
    thinking = (thinking or "").strip()
    if len(thinking) > 20000:
        thinking = thinking[:20000] + "\n\n…（思考过程过长，已截断）"
    # 有真实 usage 时，用 prompt_tokens 校准上下文总量；分项按比例缩放。
    # 流式端点没返回 usage 时保留估算值，不强行编一个数。
    _actual_prompt = int(usage.get("prompt_tokens") or 0) if usage else 0
    if _actual_prompt and ctx.context and ctx.context.get("window"):
        _parts = ctx.context.get("parts") or []
        _nonfree = [p for p in _parts if p.get("key") != "free"]
        _old = sum(int(p.get("tokens") or 0) for p in _nonfree)
        if _old > 0:
            _scale = _actual_prompt / _old
            for p in _nonfree:
                p["tokens"] = int(round(int(p.get("tokens") or 0) * _scale))
        _win = int(ctx.context.get("window") or 0)
        for p in _parts:
            if p.get("key") == "free":
                p["tokens"] = max(0, _win - _actual_prompt)
        ctx.context["used"] = _actual_prompt
        ctx.context["percent"] = round(_actual_prompt * 100.0 / _win, 1) if _win else 0
        ctx.context["source"] = "actual"
    _live_update(ctx, trace, status="done", mode=mode, rounds=rounds,
                 thinking=thinking)
    # v1.14：**只回传本轮产生的提案**。
    # 原来取的是全库「待确认」，于是每轮对话下面都挂着一堆不相干的历史提案——
    # HR 点进去发现是上周的事。要按"本轮工具调用创建了哪些 proposal_id"来过滤。
    _mine: set[int] = set()
    for _t in trace:
        _r = _t.get("result")
        if isinstance(_r, str):
            try:
                _r = json.loads(_r)
            except Exception:
                continue
        if isinstance(_r, dict) and _r.get("proposal_id"):
            try:
                _mine.add(int(_r["proposal_id"]))
            except (TypeError, ValueError):
                pass
    proposals = []
    briefs: list[dict] = []
    _run_id = None
    conn = db.connect(ctx.db_path)
    try:
        _all = db.list_proposals(conn, status="待确认", limit=100)
        # ① 本轮工具真正创建/引用过的提案
        # ② 答复正文里用 `#55` 点名的提案
        # ③ 本会话最近仍待确认的提案（兜底：模型在上一轮建了提案，这一轮只做汇总）
        # 三种来源取并集，保证"当前这条回答下面"就能点确认，而不是逼 HR 去提案页翻。
        _mentioned = {int(m.group(1)) for m in re.finditer(r"#(\d{1,9})", answer or "")}
        _picked = set(_mine) | _mentioned
        proposals = [p for p in _all if int(p["id"]) in _picked] if _picked else []
        if not proposals:
            _session_pending = [
                p for p in _all
                if str(p.get("session_id") or "") == str(ctx.session_id or "")
            ]
            if mode != "simple" and _session_pending:
                proposals = _session_pending[:6]
        # v1.24.18：拦截「口头提案」。模型 sometimes 不调工具，直接用文字描述一个
        # 待确认提案（截图实测：rounds=1、proposals 为空、文案却说"已为您生成待确认提案"）。
        # HR 看到的是一段承诺，底下**没有任何确认按钮**——比明说"做不了"更糟：
        # 它让人以为只要去系统里找一找就能点。
        # 这里不指望提示词（那是劝说，小模型会不听），而是在出口**核对事实**：
        # 声称做了写操作却一个写类工具都没调 → 明确告诉 HR 提案不存在。
        # 出口不再改写答案：所有回复以模型本轮输出为准。
        # 之前的“口头提案”检测会在正常导出/名单回答上误判，
        # 把 LLM 原文替换成“这一轮没有产生任何需要确认的操作”。
        # 必须在 conn.close() 之前构造：_proposal_brief 里的
        # application_id → candidate_id 要查 applications 表，
        # 放到 return 里用已关闭的连接会抛 "Cannot operate on a closed database"。
        briefs = [_proposal_brief(p, conn) for p in proposals]
        # 只有“本轮创建/正文点名”的提案参与「全部同意」；本会话旧提案
        # 仍可以单独处理，但不能被一次批量误点执行。
        for _b in briefs:
            try:
                _b["current_turn"] = int(_b.get("proposal_id")) in _mine \
                    or int(_b.get("proposal_id")) in _mentioned
            except (TypeError, ValueError):
                _b["current_turn"] = False
        # 兜底：部分兼容端点即使请求了 usage 也不返回。至少按消息/答复估算，
        # 让「今日 Token」不会是 0；有真实 usage 时一律优先真实值。
        if (model and not usage.get("prompt_tokens")
                and not usage.get("completion_tokens")):
            try:
                usage = {
                    "prompt_tokens": _est_tokens(question or ""),
                    "completion_tokens": _est_tokens(answer or ""),
                }
            except Exception:                              # noqa: BLE001
                pass
        _run_id = db.log_agent_run(conn, {
            "session_id": ctx.session_id, "question": question, "answer": answer[:4000],
            "thinking": thinking,
            "context": getattr(ctx, "context", {}) or {},
            "model": model,
            # v1.25：除了工具名与参数，还要存下**卡片所需的信息**——
            # 提案 id（供历史回放重建提案卡）与邮件草稿全文（重建草稿卡）。
            # 不存这两样，切页面/刷新后卡片就找不回来了（用户反馈"卡片消失"）。
            "tool_calls": [{"tool": t["tool"],
                            "args": db._mask_sensitive_args(t.get("args") or {}),
                            "tool_label": t.get("tool_label"),
                            "recovered": bool(t.get("recovered")),
                            **({"proposal_id": _pid} if (_pid := _proposal_id_of(t)) else {}),
                            **({"draft": _draft_of(t)} if _draft_of(t) else {}),
                            **({"download": _dl} if (_dl := _download_of(t)) else {})}
                           for t in trace],
            "rounds": rounds, "tokens_in": usage.get("prompt_tokens", 0),
            "tokens_out": usage.get("completion_tokens", 0),
            "cache_hit_tokens": usage.get("cache_hit_tokens", 0),
            "cache_reported": usage.get("cache_reported", 0),
            "latency_ms": latency, "status": "ok", "mode": mode, "operator": ctx.operator,
        })
    finally:
        conn.close()
    # v1.13.7：把本轮起草出的邮件草稿带出去（前端渲染成卡片，一键进编辑器）。
    # 只带草稿内容，不带任何"已发送"含义——发送仍然由 HR 在编辑器里点。
    #
    # v1.25：统一走 `_draft_of()`，**不再在这里另写一份组装**。
    # 之前这里和 `_draft_of` 各有一份字段清单，于是新加的 `template`
    # 只进了后者、没进前者 —— 草稿卡上「模板」那栏一直是空的
    # （实测：模型明明用模板渲染了，卡片却不显示来源模板）。
    # 又是"两处定义必须同源"，一份就够。
    drafts = [d for d in (_draft_of(_t) for _t in trace) if d]
    # 数据来源摘要：按工具聚合（不逐条刷屏），给前端一行“数据来源：…”。
    # 这不是装饰——HR 需要知道这句话是查出来的还是模型自己说的。
    citations: list[dict] = []
    _cite_by_tool: dict[str, dict] = {}
    for _t in trace:
        _tool = str(_t.get("tool") or "")
        if not _tool:
            continue
        _brief = _result_brief(_t.get("result") or "")
        if _tool in _cite_by_tool:
            _c = _cite_by_tool[_tool]
            _c["count"] += 1
            if _brief and len(_c["summaries"]) < 3:
                _c["summaries"].append(_brief)
        else:
            _c = {"tool": _tool,
                  "label": _t.get("tool_label") or _tool,
                  "count": 1,
                  "summaries": [_brief] if _brief else [],
                  "write": bool(_t.get("write")),
                  "disabled": bool(_t.get("disabled"))}
            _cite_by_tool[_tool] = _c
            citations.append(_c)
    return {
        "answer": answer,
        "thinking": thinking,
        "route": list(getattr(ctx, "route", []) or []),
        "skill": getattr(ctx, "skill", "") or "",
        "skill_name": getattr(ctx, "skill_name", "") or "",
        "context": getattr(ctx, "context", {}) or {},
        "citations": citations[:10],
        "drafts": drafts,
        "trace": trace,
        "mode": mode,
        "rounds": rounds,
        "latency_ms": latency,
        "tokens": usage,
        "session_id": ctx.session_id,
        "run_id": _run_id,
        "pending_proposals": briefs,
        "disclaimer": "系统只给建议，最终档位与招聘决定由 HR 确认。"
                      "写操作一律以『待确认提案』形式提交，不会自动生效。",
    }


# ============================================================
# 规则路由：模型不可用时的确定性兜底
# ============================================================

_NUM_RE = re.compile(r"(?:第\s*)?(#|id\s*=?\s*|编号\s*)?(\d{1,6})\s*(?:号|号候选人)?")
_STAGE_HINTS = {s: s for s in STAGES}

#: 写类工具。它们在 `tools.execute` 里只产出待确认提案，这里标记出来供留痕区分。
#:
#: ⚠️ v1.25 修：这里原来自成一份**只有 5 个**的写工具清单
#: （set_stage/set_tier/add_tag/merge_candidates/add_note），
#: 而 `tools.WRITE_TOOLS` 有 14 个。两份定义一分叉就出问题：
#: 模型调 `assign_job`（真的产生了提案）时，trace 里 write=False，
#: 于是 `_looks_like_fake_proposal` 会把它当成"口头提案"误报，
#: 甚至触发下面那条强制补调 —— **明明做成了却要重做一遍**。
#: 现在从 tools 引一份，单一事实来源。
#: （同 MEMORY.md 记过的「两处判据定义必须同源」教训。）
WRITE_TOOLS = tools_write.WRITE_TOOLS

#: 结果需要**原样完整**带回前端的工具（不能被 900 字摘要截断）。
#: 截断了前端就渲染不出卡片正文，HR 只能看到一行省略号。
_FULL_RESULT_TOOLS = frozenset({"draft_email", "get_mail_template",
                                "list_mail_templates", "preview_mail"})

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

    # 0) 招聘进度（v4.0.16）
    # 必须排在「管道/统计」之前：两者都认「进度 / 多少」，不先拦住就会答成
    # 候选人阶段分布，而不是“本届应招多少、还差多少”。
    recruit_q = any(k in q for k in
                    ("招聘进度", "签约", "签约缺口", "完成率", "还差多少", "缺口"))
    if recruit_q:
        r = call("recruit_progress", {})
        handled = True
        lines.append("\n招聘进度：")
        if r.get("error"):
            lines.append(f"- 查询失败：{r['error']}"
                         + (f"（{r.get('hint')}）" if r.get("hint") else ""))
        else:
            y = r.get("year") or {}
            t = r.get("totals") or {}
            rate = t.get("rate")
            rate_s = f"{rate * 100:.1f}%" if rate is not None else "—"
            lines.append(f"- {y.get('label') or '本届'}（{y.get('period_start')} 至 "
                         f"{y.get('period_end')}）：应招 {t.get('should_hire')} 人，"
                         f"已签 {t.get('signed_count')} 人，缺口 {t.get('gap')} 人，"
                         f"完成率 {rate_s}")
            for e in r.get("by_education") or []:
                lines.append(f"  · {e.get('edu_level')}：应招 {e.get('should_hire')} / "
                             f"已签 {e.get('signed_count')} / 缺口 {e.get('gap')}")
            gaps = r.get("top_gaps") or []
            if gaps:
                lines.append("- 缺口最大：")
                for g in gaps[:5]:
                    lines.append(f"  · {g.get('unit')} / {g.get('education')}："
                                 f"缺 {g.get('gap')} 人")

    # 0b) 岗位（v1.6）
    # 必须有这一条、且必须排在"统计"之前：过去没有岗位分支，"发布了几个岗位"
    # 会被下面的"几个/多少"命中而返回人才库人数总览——**答非所问**。
    job_q = any(k in q for k in ("岗位", "职位", "在招", "招什么", "招哪些", "招聘计划")) \
        and not recruit_q
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
        lines.append("- 改邮箱账号/文件夹路径也请在「邮箱配置」页改（涉及密码，不进对话留痕）。")

    # 1) 统计 / 概览（问的是岗位时跳过，避免和岗位分支各答一半）
    if not job_q and not recruit_q and any(
            k in q for k in ("统计", "多少", "几个", "几人", "总览", "概览", "情况", "数量")):
        s = call("pool_stats", {})
        handled = True
        lines.append("\n人才库总览：")
        lines.append(f"- 候选人数 {s.get('people')} 人，投递 {s.get('applications')} 条，"
                     f"简历附件 {s.get('documents')} 份，收信 {s.get('emails')} 封"
                     f"（其中 {s.get('emails_pending')} 封待处理）")
        lines.append(f"- 在招岗位 {s.get('jobs_open')} 个（岗位总数 {s.get('jobs_total')}）")
        lines.append(f"- 档位分布：{json.dumps(s.get('tiers', {}), ensure_ascii=False)}")
        lines.append(f"- 待 HR 确认 {s.get('pending')} 条，信息缺失 {s.get('needs_review')} 条，"
                     f"待确认提案 {s.get('proposals_pending')} 条")
        lines.append(f"- 库内已出现技能 {s.get('skills')} 种、标签 {s.get('tags')} 种"
                     f"（技能本体共 {nz.describe().get('canonical_count')} 条）")

    # 2) 管道
    if not recruit_q and any(k in q for k in ("管道", "进度", "流程", "积压", "阶段")):
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
            # v1.13.6：不再输出"评分 None"（打分已删，这个字段恒为空）
            lines.append(f"- 档位：建议 {d.get('tier_suggested') or '待分析'}"
                         f"（HR 确认 {d.get('tier_final') or '未确认'}）")
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
        lines.append("- 现在招聘管道各阶段有多少、有没有积压（管道）")
        lines.append("- 邮箱收了多少封简历、有没有没处理的（收信台账）")
        lines.append("- 有哪些待确认的提案 / 最近谁改了什么（提案与审计）")
        lines.append("- 看看 1 号候选人的档案（用编号查询）")
    return "\n".join(lines)
