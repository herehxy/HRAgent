"""投递阶段与结论标签：**阶段名的唯一真源**。

为什么单独抽一个模块
--------------------
阶段名原来散在 **6 个地方各写一份**：`ui.py`（看板与下拉）、`db.py`（建表注释 + `STAGES`）、
`agent/proactive.py`、`agent/tools.py`（含工具描述里的中文枚举）、`agent/loop.py`（白名单正则）、
`tests/ui_probe.py`（写死"在招流程 5 列"）。人事处每调一次流程就要改 6 处，
漏一处的症状是"某处还认老阶段名"——看板少一列、助手推不动阶段、白名单正则不认新值。
本项目已经为"同一份清单散在多处"反复栽过（密钥清单、写工具清单、草稿字段清单）。
这次一并收成一处：**代码只读配置，不写字面量**。

配置在 `config/stages.json`（改流程不用改代码、不用重新打包）。
读不到或格式坏了**回落到 `_FALLBACK`**，绝不因为一个配置文件让整个工作台起不来。

⚠️ `_FALLBACK` 与 `config/stages.json` 是**两份内容**（一份兜底、一份可编辑）。
为防止它们悄悄分叉，`assert_consistent()` 会比对二者；自检套件里有判据钉住它。
**新增阶段必须同时改这两处**，否则自检会报出来。
"""
from __future__ import annotations

import json
import os
import re
from .paths import cfg as _cfg_path

#: 配置路径（与其它字典同目录，便于 HR/运维直接改）
CONFIG_PATH = _cfg_path("stages.json")

#: 配置缺失/损坏时的兜底。**必须与 config/stages.json 内容一致**（有判据校验）。
_FALLBACK: dict = {
    # ⚠️ v1.29.2（业务方 2026-10-16 定稿）：阶段收成 5 档。
    #   「待发offer」**并掉** —— 「拟录用」不再是一个独立阶段，
    #   而是**已面试阶段直接打的标签**，通过后进入「已发放offer」；
    #   「新投递」改名为「全部简历」—— 业务方要的是"还没进流程的所有简历"这个视角，
    #   而不是一个叫"新投递"的阶段（收到就是全部简历，进流程后才分档）。
    "order": ["全部简历", "初筛通过", "待面试", "已面试", "已发放offer", "已结束"],
    "pipeline": ["全部简历", "初筛通过", "待面试", "已面试", "已发放offer"],
    "terminal": ["已结束"],
    "auto_archive_on": ["已结束"],
    # 结论标签（业务方 2026-10-16 定稿）：
    #   待面试   → 拟录用 / 不录用   ← 结论**产生**在这个环节，所以也挂在这里。
    #              业务方要求：在「待面试」打结论标签会**自动推进到「已面试」**，
    #              而 `outcome_allowed_from` 是"标签必须属于**离开的那个环节**"的语义，
    #              待面试不给这两个标签的话 `待面试→已面试+拟录用` 会被自己拒掉。
    #   已面试   → 拟录用 / 不录用
    #   已发放offer → 已签约 / 拒签
    "outcomes": {"待面试": ["拟录用", "不录用"],
                 "已面试": ["拟录用", "不录用"],
                 "已发放offer": ["已签约", "拒签"]},
    "actions": {
        "全部简历": [{"label": "加入初筛通过", "to": "初筛通过"}],
        # 面试单位**在进入待面试之前**必填（业务方裁定）。
        # 它是面试邀请邮件里的单位，也是「待面试按部门分流」那个筛选的数据来源。
        "初筛通过": [{"label": "加入待面试", "to": "待面试",
                    "require": ["interview_unit"],
                    "require_hint": "请先在完整档案的「投递记录 → 面试与录用信息」里"
                                    "登记面试单位——它由人事手动填写，不随岗位自动变；"
                                    "填完之后发面试邀请邮件与导出 Excel 都会用这个值。"}],
        "待面试": [{"label": "面试完成", "to": "已面试"}],
        # ⚠️ v1.29.2（业务方 2026-10-16 定稿）：「拟录用」**只是人事处打的标签**，
        #   **不自动进入「已发放offer」** —— 拟录用之后还要走"发录用函"这一步，
        #   由 HR 真的把函发出去（或至少确认要发）才进下一档。
        #   所以这里是**自环**（已面试 → 已面试）：只写 outcome，不动阶段。
        "已面试": [{"label": "拟录用", "to": "已面试", "outcome": "拟录用"},
                   {"label": "不录用", "to": "已结束", "outcome": "不录用"},
                   {"label": "发出录用函", "to": "已发放offer"}],
        "已发放offer": [{"label": "已签约", "to": "已发放offer", "outcome": "已签约"},
                        {"label": "拒签", "to": "已结束", "outcome": "拒签"}],
    },
    "colors": {
        "全部简历": "#8a94a6", "初筛通过": "#3370ff", "待面试": "#7b61ff",
        "已面试": "#0f8a6a", "已发放offer": "#d97706", "已结束": "#86909c",
    },
    "default_stage": "全部简历",
    "thresholds": {"pass": 60, "hold": 45},
}

_cached: dict | None = None


def default_stage() -> str:
    """初始阶段（新简历进来时的阶段；也是认不出的历史阶段的归并去处）。

    ⚠️ **从配置读，不写字面量**。这里原来硬编码 `DEFAULT_STAGE = "新投递"`，
    而配置里早已改成「全部简历」—— 于是 `normalize_stage()` 把所有未知值归到
    「新投递」，而 `all_stages()` 里根本没有这个值，两边对不上。
    后果实测很隐蔽：界面上一切正常（`normalize` 的兜底会显示），但**主动提案那条
    规则（"A 档却没推进"）永远匹配不上**，静默产出 0 条，判据全红。

    这正是本模块要消灭的那类"同一份清单散在多处"——**包括我自己留的这一处**。
    认不出的值统一归并到它（且它必然在 `all_stages()` 里，否则配置本身坏了）。
    """
    v = str(load_cfg().get("default_stage") or "").strip()
    order = all_stages()
    if v and v in order:
        return v
    fb = str((_FALLBACK.get("default_stage") or "")).strip()
    return fb if fb in order else (order[0] if order else "")




def load_cfg(force: bool = False) -> dict:
    """读阶段配置。缺文件/坏 JSON/缺关键键 → 回落 `_FALLBACK`（并打印一行到 stderr）。

    缓存策略：进程内缓存一次。人事处改完 `stages.json` 后**需要重启工作台**才生效——
    这与其它字典（tiers/interview）一致，不做热加载：阶段取值同时在数据库里、
    在审计文案里、在前端正则里，热加载只会造成"部分生效"的中间态。
    """
    global _cached
    if _cached is not None and not force:
        return _cached
    cfg = dict(_FALLBACK)
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, encoding="utf-8") as fh:
                user = json.load(fh)
            if isinstance(user, dict):
                # 逐键覆盖：允许只写要改的键（其余用兜底值），但 order 必须非空
                cfg.update({k: v for k, v in user.items() if not k.startswith("_")})
    except Exception as exc:                                    # noqa: BLE001
        import sys
        print(f"[stages] config/stages.json 读取失败，已回落内置默认阶段表："
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        cfg = dict(_FALLBACK)
    if not cfg.get("order"):
        cfg["order"] = list(_FALLBACK["order"])
    if not cfg.get("pipeline"):
        cfg["pipeline"] = [s for s in cfg["order"] if s not in (cfg.get("terminal") or [])]
    if not cfg.get("terminal"):
        cfg["terminal"] = ["已结束"]
    _cached = cfg
    return _cached


# ------------------------------------------------------------------ 基本查询

def all_stages() -> list[str]:
    """全部阶段（按流程先后）。"""
    return list(load_cfg()["order"])


def pipeline_stages() -> list[str]:
    """在招流程阶段（不含终态）——看板只画这些列。"""
    return list(load_cfg().get("pipeline") or
                [s for s in all_stages() if s not in terminal_stages()])


def terminal_stages() -> list[str]:
    return list(load_cfg().get("terminal") or [])


def auto_archive_stages() -> list[str]:
    """到了这些阶段 → 该候选人所有投递都到终点时自动归档。"""
    return list(load_cfg().get("auto_archive_on") or [])


def is_valid_stage(value) -> bool:
    return str(value or "").strip() in all_stages()


def normalize_stage(value) -> str:
    """认不出的阶段值 → 归并成 `DEFAULT_STAGE`。

    为什么必须归并（而不是原样显示）：历史库/手工改过的值不在阶段表里时，
    看板与比例环都渲染不出来 → 这些人**凭空消失**，环总数 ≠ 候选人数。
    v1.26.0 已经用这条口径处理过 `pool_stats`，这里把它提升为全局口径。
    """
    v = str(value or "").strip()
    return v if v in all_stages() else default_stage()


def outcomes_of(stage: str) -> list[str]:
    """该阶段允许的结论标签。不在表里的阶段 → 空列表（不允许打结论）。"""
    return list((load_cfg().get("outcomes") or {}).get(str(stage or "").strip()) or [])


def all_outcomes() -> list[str]:
    """全部结论标签（去重，按配置里出现的先后）。"""
    out: list[str] = []
    for _s, vals in (load_cfg().get("outcomes") or {}).items():
        for v in vals:
            if v not in out:
                out.append(v)
    return out


def is_valid_outcome(stage: str, outcome) -> bool:
    """结论必须**属于该阶段** —— 挡住「已面试·已签约」这种不存在的组合。"""
    o = str(outcome or "").strip()
    return bool(o) and o in outcomes_of(stage)


def outcome_allowed_from(from_stage: str, to_stage: str, outcome) -> bool:
    """结论打在这一步上是否合法：**以「离开的环节」为准**。

    为什么不是看目标阶段（这是被自己的用例抓出来的设计错）：
    动作「不录用」是 `已面试 ──► 已结束` **同时**带结论 `不录用` ——
    结论描述的是"这一轮面试的结果"，属于**离开的那个环节**（已面试），
    而目标阶段「已结束」是终态、本来就不该配结论标签。
    只按目标阶段校验，`已面试→已结束+不录用` 会被自己拦住，流程根本走不通。
    （同一类：`已发放offer ──► 已结束` 带 `拒签`。）

    为什么也不是"两边任一允许就行"：那样从 `已发放offer` 出发时，
    `已签约` 会因为"离开的环节允许"而被放行到任何目标阶段 —— 校验形同虚设。
    结论只属于离开的那个环节，所以**只看它**。
    `from_stage` 为空（新建投递时直接带上结论）才退而看目标阶段。

    ⚠️ 已知宽松处：这里只校验"结论合不合法"，**不校验这一步的走向是否合理**
    （`已发放offer → 已面试 + 已签约` 会被放过）。走向由 `config/stages.json`
    的 `actions` 约束，界面只渲染配置内的动作，走不到这种组合；只有直调接口才可能。
    真要收紧需要再立一张"允许的流转表"，本版没做 —— 记在这里，别当成已经拦住了。
    """
    o = str(outcome or "").strip()
    if not o:
        return True                      # 空 = 清空结论，不算打标签
    base = str(from_stage or "").strip() or str(to_stage or "").strip()
    return o in outcomes_of(base)


def auto_advance_stage(current_stage: str, requested_stage: str, outcome) -> str:
    """只改结论时，决定实际要写入的阶段。

    v2 把「改结论」和「推进阶段」拆成了两个动作，但业务上人既然已经有面试结论，
    就不该还停在「已面试」之前。这里只处理**同一个阶段的标签编辑**（例如
    `待面试 → 待面试 + 拟录用`）；显式选了其它阶段时仍以调用方选择为准。

    自动推进只发生在这三个条件同时满足时：
      1. 当前阶段确实早于「已面试」；
      2. 当前阶段允许这个结论（避免从「初筛通过」直接跳档绕过流程）；
      3. 「已面试」也允许这个结论（只认面试结论，不把签约标签带进来）。
    """
    current = str(current_stage or "").strip()
    requested = str(requested_stage or "").strip()
    oc = str(outcome or "").strip()
    order = all_stages()
    target = "已面试"
    if not oc or current != requested or target not in order:
        return requested
    if current not in order or order.index(current) >= order.index(target):
        return requested
    if oc not in outcomes_of(current) or oc not in outcomes_of(target):
        return requested
    return target


def actions_of(stage: str) -> list[dict]:
    """该阶段可执行的动作（界面据此渲染按钮）。"""
    return [dict(a) for a in ((load_cfg().get("actions") or {}).get(str(stage or "").strip()) or [])]


#: 必填字段的中文名（报错文案里用；别在报错处再抄一份）
REQUIRED_FIELD_LABEL = {
    "interview_unit": "面试单位",
}


def required_fields(from_stage: str, to_stage: str) -> list[str]:
    """从 `from_stage` 推到 `to_stage` 这一步**必填**哪些字段。

    v1.29：面试单位挂在「初筛通过 → 待面试」这一步。业务方裁定「在变成待面试之前
    就该让 HR 填」——之前挂在「新投递 → 初筛通过」，等于让人在还没初筛时就填面试单位，
    那时候多半还不知道由哪个单位面。

    ⚠️ v4.0.6：**按"目标阶段"判定，不再按"动作"判定**。
    原来只认 `actions` 里配了的那一对（初筛通过 → 待面试），于是界面上的阶段下拉
    直接跳「全部简历 → 已面试」就绕过去了 —— 业务方实测"没填面试单位也能进已面试"。
    面试单位是面试这件事的前提，凡是从**待面试之前**跨到**待面试及之后**的推进，
    都必须先有它，无论中间跳过几档、走哪条路径（卡片下拉 / 管道 / 助手提案）。
    """
    to_stage = str(to_stage or "").strip()
    for a in actions_of(from_stage):
        if a.get("to") == to_stage:
            return list(a.get("require") or [])
    order = all_stages()
    if "待面试" in order and to_stage in order:
        i_to, i_iv = order.index(to_stage), order.index("待面试")
        i_from = order.index(str(from_stage or "").strip()) if from_stage in order else -1
        if i_to >= i_iv and i_from < i_iv:
            return ["interview_unit"]
    return []


def require_hint(from_stage: str, to_stage: str) -> str:
    """必填没过时给 HR 看的那句话（说清"去哪填、为什么"）。"""
    to_stage = str(to_stage or "").strip()
    for a in actions_of(from_stage):
        if a.get("to") == to_stage:
            return str(a.get("require_hint") or "")
    if required_fields(from_stage, to_stage):
        return ("请先在完整档案的「面试与录用 → 面试单位」里登记由哪个单位面试；"
                "登记之后发面试邀请邮件与导出 Excel 都会用这个值。")
    return ""


def is_interview_started(stage: str) -> bool:
    """是否已经进入面试流程（`待面试` 及其之后）。

    用于"面试单位要不要在卡片上展示"：初筛通过阶段单位常常还没定，
    硬显示一个空的"面试：—"只是噪音。
    """
    order = all_stages()
    s = str(stage or "").strip()
    if "待面试" not in order or s not in order:
        return False
    return order.index(s) >= order.index("待面试")


def stage_color(stage: str) -> str:
    return (load_cfg().get("colors") or {}).get(str(stage or "").strip(), "#a7b0bf")


def stage_regex() -> str:
    """阶段名的正则（给 `agent/loop.py` 的白名单用）。

    ⚠️ 必须**转义**：阶段名里可能有正则元字符，直接拼会把白名单变成任意匹配。
    长的排前面，避免「已发放offer」被「已面试」这类短名提前吃掉。
    """
    names = sorted(all_stages(), key=len, reverse=True)
    return "|".join(re.escape(n) for n in names)


# ------------------------------------------------------------------ 推导

def verdict(stage: str, outcome: str | None) -> str:
    """(阶段, 结论) → 「是否录用」列的取值。**不另存字段**，避免两处口径分叉。

    规则：
      outcome=不录用 或 **拒签**            → 不录用
      outcome=拟录用 或 已签约               → 拟录用
      stage = 已发放offer                  → 拟录用（走到这一步还没拒 = 拟录用）
      其余（还在已面试、或未判）            → 空（导出时显示 `—`）

    ⚠️ **拒签必须算"不录用"**（业务方 2026-10-15 口径）：
    发了录用函、对方拒签 → 结果就是**没录用**。原来只映射 `不录用`，
    于是拒签的人那列是**空的** —— 而"他拒了"恰恰是最该被 HR 看到的一条，
    留空等于"还不知道"，性质完全不同。

    ⚠️ v1.29.2：「拟录用」从"独立阶段"改成**已面试阶段打的标签**，
    所以它现在走 `outcome` 分支；老库里 stage 还写着「待发offer」的行会被
    `normalize_stage` 归并到「全部简历」，但**那些行 outcome 为空**，
    导出上会显示 `—` —— 属于需要人工确认一次的历史数据，不猜。
    """
    o = str(outcome or "").strip()
    s = str(stage or "").strip()
    if o in ("不录用", "拒签"):
        return "不录用"
    if o in ("拟录用", "已签约"):
        return "拟录用"
    if s == "已发放offer":
        return "拟录用"
    return ""


def is_auto_archive(stage: str) -> bool:
    return str(stage or "").strip() in auto_archive_stages()


# ------------------------------------------------------------------ 一致性

def assert_consistent() -> list[str]:
    """比对 `_FALLBACK` 与 `config/stages.json`，返回不一致项（空 = 一致）。

    用途：防止"兜底表"和"可编辑配置"悄悄分叉——那是同一个清单散在两处的老毛病。
    自检套件调它；返回非空即判失败。
    """
    diffs: list[str] = []
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            user = json.load(fh)
    except Exception as exc:                                    # noqa: BLE001
        return [f"读不到 config/stages.json：{type(exc).__name__}: {exc}"]
    for k in ("order", "pipeline", "terminal", "auto_archive_on", "outcomes",
              "actions", "colors"):
        if user.get(k) != _FALLBACK.get(k):
            diffs.append(k)
    return diffs


#: 模块级快照（给不想每次调函数的调用方用）。**它由 `default_stage()` 算出**，
#: 不再是另一份手写的字面量 —— 改了配置两个入口一起变。
#: ⚠️ 必须放在**模块末尾**：`default_stage()` 依赖 `load_cfg()` / `all_stages()`，
#: 写在中段会在 import 期就 NameError。
DEFAULT_STAGE = default_stage()
