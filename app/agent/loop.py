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
from . import tools as tools_write
from .tools import DISABLED_TOOLS, STAGES, TOOL_SPECS, ToolCtx, execute

SYSTEM_PROMPT = (
    "你是西北有色金属研究院人才库工作台的 HR 助手，熟悉稀有金属材料（钛合金、难熔合金、"
    "高温合金）研发与工艺岗位的招聘。\n"
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
    "不要声称你已经替 HR 做了决定。\n"
    "3) 你没有删除数据、淘汰候选人、对候选人发消息的权限；也不要建议这么做。\n"
    "   但**起草**是可以的：用户要发邮件/通知时，用 draft_email 生成草稿"
    "（面试邀请/跟进/婉拒/offer 沟通），把主题与正文给用户看，"
    "并告诉他『这是草稿，发送需要你点确认』——"
    "**不要说『我无法帮你发邮件』就结束**，那是能干的活儿没干。\n"
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
    "7) 人是用**姓名**指人的：candidate_id 可以直接写姓名（如 '何晓宇'），系统会自动解析；"
    "**绝对不要凭空编数字 id**——编了会直接报'未找到候选人'。\n"
    # v1.25：实测「把 #1 推进至初面」时模型**不查就反问**，
    # 而 HR 已经把编号给出来了 —— 再问一遍等于把活儿推回去。
    # 编号可能是投递 id 也可能是候选人 id，**两个都查一下**再决定要不要问。
    "7b) HR 给出 `#数字` 时，**先查再问**：把它当投递 id 查一次"
    "（get_candidate / explain_grade 都能带出投递），"
    "查到了就直接用；查不到或有多条歧义，才回头问 HR。"
    "不要一上来就要求 HR 再确认一遍编号——**编号就是他要你用的那个**。\n"
    "6) 回答用简体中文，结论先行、结构化（可用编号或短列表），不要输出 JSON。"
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
    r"|阶段|档位|标签|备注|合并|档案|已入职|初面|复面|新投递|已联系)")

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
    # （实测模型会写「已为您生成将候选人 苗雨桐（西安电子科技大学）
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
#:   「候选人：苗雨桐（ID: 1024）」 / 「投递 #1024」 / 「application_id=1024」
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

          已为您生成将候选人 苗雨桐 从「新投递」推进到「已联系」的待确认提案。
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
                     usage_total: dict) -> list[dict] | None:
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
        msg = llm.chat(probe, tools=TOOL_SPECS, cfg=cfg, tool_choice="required")
    except Exception:
        return None
    _accumulate(usage_total, msg.get("_usage"))
    calls = msg.get("tool_calls")
    if not calls:
        return None
    # 把这一轮也接进对话，保持顺序合法（assistant 的 tool_calls 必须带 id）
    messages.append({"role": "assistant", "content": answer})
    messages.append({"role": "assistant", "content": "",
                     "tool_calls": calls})
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
    # （截图里写「候选人：苗雨桐（ID: 1024）」，语义上更像人ID）。
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

    def _run_calls(calls: list[dict]) -> int:
        """执行一批 tool_calls，写进 trace 与 messages。返回执行了几个。

        抽成函数是为了让"正常路径"和下面的"强制补调路径"走**完全相同**的执行逻辑
        —— 否则两条路容易长歪（一条改了权限校验、另一条没改）。
        """
        n = 0
        for tc in calls:
            fn = (tc.get("function") or {}).get("name", "")
            try:
                args = json.loads((tc.get("function") or {}).get("arguments") or "{}")
            except ValueError:
                args = {}
            result = execute(fn, args, ctx)
            # v1.13.9：邮件草稿要**完整**带回（正文会被 900 字截断，
            # 截断了前端就渲染不出卡片）；其余工具仍然只留摘要。
            trace.append({"tool": fn, "args": args,
                          "result": (result if fn in _FULL_RESULT_TOOLS else result[:900]),
                          # v1.25：中文名给前端，HR 不需要看函数标识符
                          "tool_label": _TOOL_LABEL.get(fn, fn),
                          "write": fn in WRITE_TOOLS,
                          "disabled": fn in DISABLED_TOOLS})
            # 结果必须回灌给模型，否则它下一轮看不见工具返回、只能继续编
            messages.append({"role": "tool", "tool_call_id": tc.get("id", ""),
                             "name": fn, "content": result})
            n += 1
        return n

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

                # ---- v1.25：口头提案 → **强制它真调一次工具** ----
                #
                # 历史（两版，都留着记录为的是不再走回去）：
                #   v1.24.18 —— 只在出口加一句"上面这段描述不算数"。结果 HR 看到的是
                #               "系统说要改却没改"，比明说"做不了"更糟。
                #   v1.24.19 —— 改用 `_recover_missed_action()` 按答案**猜**意图补跑。
                #               但它是枚举中文动词的，实测「推进**至**」就抽不出来
                #              （正则先吃掉`推进`，尾巴剩 `至「初面」` 匹配不上阶段名），
                #               于是警告加了、补救没做——最坏的组合。
                #               **枚举中文动词必然不全**，这是 MEMORY.md 3.7 第 7 条
                #               已经用八版教训换来的结论，不该在补救里重犯。
                #
                # v1.25 的做法：**不猜词，让模型自己重发一次工具调用。**
                # 它刚说完"已推进到初面"，就不可能不知道要调哪个工具。
                # 用 tool_choice="required" 强制本轮必须出 tool_calls，
                # 于是提案真的落库、确认按钮真的出现——
                # 「真正实现了才是最重要的」，而不是加一句免责声明。
                if not any(t.get("write") for t in trace) \
                        and _looks_like_fake_proposal(answer, trace):
                    forced = _force_tool_call(messages, answer, cfg, usage_total)
                    if forced:
                        n_ran = _run_calls(forced)
                        rounds += 1
                        if n_ran and any(t.get("write") for t in trace):
                            # 补调成功：让模型基于**真实工具结果**重说一遍，
                            # 而不是把那段"我已做完"的幻觉话术留给 HR。
                            follow = llm.chat(
                                messages + [{"role": "user", "content":
                                             "工具已按你的意图执行完毕。请基于上面的工具返回，"
                                             "用简体中文向 HR 说明**实际发生了什么**"
                                             "（如已生成待确认提案，就说『已提交待确认』），"
                                             "不要重复你上一条里未经执行的说法。"}],
                                tools=TOOL_SPECS, cfg=cfg)
                            _accumulate(usage_total, follow.get("_usage"))
                            model_used = follow.get("_model") or model_used
                            rounds += 1
                            txt = (follow.get("content") or "").strip()
                            # 模型可能又调工具（如再查一次）；一并执行掉，保证一致性
                            if follow.get("tool_calls"):
                                _run_calls(follow["tool_calls"])
                            if txt:
                                answer = txt
                            else:
                                answer = _summarize_actions(trace)
                        else:
                            # 强制了还没调出写类工具 → 退到确定性文案
                            answer = _summarize_actions(trace)
                return _finalize(ctx, user_message, answer, trace, "llm", rounds,
                                 usage_total, started, model_used)
            messages.append({"role": "assistant", "content": msg.get("content") or "",
                             "tool_calls": calls})
            _run_calls(calls)
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
    return {"candidate_id": r.get("candidate_id"), "name": r.get("name"),
            "to": r.get("to"), "subject": r.get("subject"),
            "body": r.get("body"), "job": r.get("job"),
            "template": r.get("template"),
            "missing_runtime": r.get("missing_runtime") or []}


def _accumulate(total: dict, usage: dict | None) -> None:
    for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
        if usage and usage.get(k):
            total[k] = total.get(k, 0) + int(usage[k])


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
            "risk": p["risk"], "candidate_id": cid,
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
              model: str | None = None) -> dict:
    latency = int((time.time() - started) * 1000)
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
    conn = db.connect(ctx.db_path)
    try:
        _all = db.list_proposals(conn, status="待确认", limit=20)
        proposals = [p for p in _all if int(p["id"]) in _mine] if _mine else []
        # v1.24.18：拦截「口头提案」。模型 sometimes 不调工具，直接用文字描述一个
        # 待确认提案（截图实测：rounds=1、proposals 为空、文案却说"已为您生成待确认提案"）。
        # HR 看到的是一段承诺，底下**没有任何确认按钮**——比明说"做不了"更糟：
        # 它让人以为只要去系统里找一找就能点。
        # 这里不指望提示词（那是劝说，小模型会不听），而是在出口**核对事实**：
        # 声称做了写操作却一个写类工具都没调 → 明确告诉 HR 提案不存在。
        _wrote = any(_t.get("write") for t in trace)
        if not _wrote and _looks_like_fake_proposal(answer, trace):
            # v1.25：**不再加"上面这段描述不算数"那种警告。**
            # 用户原话：「不要加那句什么没有实现警告，真正实现了才是最重要的」。
            # 警告是补救，而且是最差的一种补救——HR 既没办成事，
            # 又看了一句自我否定。真做不成的极端情况（比如模型两轮都不肯调工具、
            # 或参数缺得没法执行），就**把话说清楚并给出下一步**，
            # 由 `_summarize_actions` 用事实拼出确定性文案。
            # 走到这里说明强制补调也没成功，属于罕见路径。
            answer = _summarize_actions(trace)
        # 必须在 conn.close() 之前构造：_proposal_brief 里的
        # application_id → candidate_id 要查 applications 表，
        # 放到 return 里用已关闭的连接会抛 "Cannot operate on a closed database"。
        briefs = [_proposal_brief(p, conn) for p in proposals]
        db.log_agent_run(conn, {
            "session_id": ctx.session_id, "question": question, "answer": answer[:4000],
            "model": model,
            # v1.25：除了工具名与参数，还要存下**卡片所需的信息**——
            # 提案 id（供历史回放重建提案卡）与邮件草稿全文（重建草稿卡）。
            # 不存这两样，切页面/刷新后卡片就找不回来了（用户反馈"卡片消失"）。
            "tool_calls": [{"tool": t["tool"], "args": t["args"],
                            "tool_label": t.get("tool_label"),
                            "recovered": bool(t.get("recovered")),
                            **({"proposal_id": _pid} if (_pid := _proposal_id_of(t)) else {}),
                            **({"draft": _draft_of(t)} if _draft_of(t) else {})}
                           for t in trace],
            "rounds": rounds, "tokens_in": usage.get("prompt_tokens", 0),
            "tokens_out": usage.get("completion_tokens", 0),
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
    return {
        "answer": answer,
        "drafts": drafts,
        "trace": trace,
        "mode": mode,
        "rounds": rounds,
        "latency_ms": latency,
        "tokens": usage,
        "session_id": ctx.session_id,
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
        lines.append(f"- 待 HR 确认 {s.get('pending')} 条，信息缺失 {s.get('needs_review')} 条，"
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
