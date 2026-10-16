"""分级层（尺子）：把投递归档——**不淘汰、不丢弃**。

档位字母与文案来自 `config/tiers.json`（唯一真源，见 `app/tiers.py`）：
v1.29 起是 **A 推荐面试 / B 储备 / C 暂不匹配** 三档（原 A优先面试 +B建议面试 并入 A，
原 D暂不匹配 改由 C 承担）。**代码里不写字面量** —— 写死的话人事处一改档位体系，
提示词、工具校验、界面徽章会各漏一处，症状还不一样。

与"筛选器"的根本差别：硬性条件不足只降档为**最差档**（暂不匹配，转人才库待召回），
简历与投递记录始终留在库中；解析异常另打 "待人工判读" 标记，不参与排序。

反幻觉约束：只有 `verified=1` 的技能才参与命中计算（见 `pipeline.normalize`），
并且命中项一律附上简历原文证据，界面上可点开逐条核对。

输出::

    score, tier_suggested, reasons, risks,
    hit / miss / preferred_hit,            # 技能口径
    hit_detail,                           # [{skill, evidence}] 命中证据
    breakdown,                            # 四项分值拆解
    needs_review
"""
from __future__ import annotations

from . import majors as mj
from . import normalize as nz
from .. import tiers as tiers_mod

EDU_RANK = {"大专": 1, "专科": 1, "本科": 2, "学士": 2, "研究生": 3, "硕士": 3, "博士": 4}


def _edu_of(cand: dict) -> str:
    """取候选人学历，**两个字段名都要认**（v2.0.1 修）。

    ⚠️ 这是一个"字段名不一致导致学历门槛静默失效"的真 bug：
    `extract()` 产出的键是 `education`，而 `db.candidate_detail()` 给的是 `edu_level`
    —— `edu_gate` 只读 `education`，于是**凡是从库里取档案走进来的路径**
    （`analyze_one` → `auto_insight`，即"入库即分析"）拿到的都是空值，
    `c_rank=0` → `unknown=True` → **学历门槛从未生效**，学历不达标的人也能被模型给 A/B。
    而入库/重新分析那条路（cand 来自 extract）读得到 `education`，门槛照常生效
    —— 同一个候选人两条路结论不同，根子就在这。
    `_cand_brief()` 早就写成 `cand.get("education") or cand.get("edu_level")`，
    说明两个键并存是已知事实，只有这里漏了。
    """
    return str(cand.get("education") or cand.get("edu_level") or "").strip()


def edu_gate(cand: dict, jd: dict) -> dict:
    """**学历门槛的唯一权威判定**（v1.24.15）。

    为什么要有这个函数，而不是让各处自己算一遍 `EDU_RANK`：
    「学历不够就是最差档」这条硬规则过去散落在判档、模型写档、写库守卫三处，
    而其中两处算错了——`grade()` 在未归岗时返回中间档（不是最差档），
    于是「模型可以合法地把学历不够的人改成 A 档」。
    现在只有一个出口算这条规则，其余地方一律调它，避免再各算各的。

    返回::

        {"failed": bool,     # 学历明确不达标 → 必须判 hard_shortfall_tier（配置项）
         "unknown": bool,    # 学历没抽到 → 不能判最差档，交给人工
         "c_rank": int, "m_rank": int,
         "edu": str, "edu_min": str}

    `failed` 只在**学历已识别且低于门槛**时为 True：
    学历读不出来（`unknown`）不算"不够"，那是信息缺失、该人工补填，
    不能当成否定结论把人判死。
    **`jd` 为空（该候选人没有对应岗位）时一律 `failed=False`**：
    没有尺子就没门槛，此时拿默认的"本科"去判死一个人是凭空造规则。
    """
    must = (jd or {}).get("must") or {}
    if not (jd or {}).get("must") and not (jd or {}).get("preferred"):
        # 没有对应岗位：门槛无从谈起，不判最差档（也不需要 unknown 标记）
        return {"failed": False, "unknown": True, "c_rank": 0, "m_rank": 0,
                "edu": (_edu_of(cand) or "").strip() or "学历未识别",
                "edu_min": ""}
    edu = (_edu_of(cand) or "").strip()
    c_rank = EDU_RANK.get(edu, 0)
    m_rank = EDU_RANK.get(must.get("education_min") or "本科", 2)
    return {
        "failed": bool(c_rank > 0 and c_rank < m_rank),
        "unknown": c_rank == 0,
        "c_rank": c_rank,
        "m_rank": m_rank,
        "edu": edu or "学历未识别",
        "edu_min": must.get("education_min") or "本科",
    }


def lock_tier(rule_tier: str | None, model_tier: str | None,
              gate: dict | None) -> tuple[str | None, str]:
    """**档位最终裁决**：学历门槛优先，模型只能在门槛之内说话。

    返回 `(档位, 来源说明)`。

    这是「学历不够的档位模型不允许改」的**唯一执行点**。
    过去这条约束散在调用方的 `if rule_tier != "D"` 里，而调用方手里的
    `rule_tier` 取自 applications 行的历史值（不是本次判档结果），
    于是库内存 C 时模型就能改成 A。这里改成从 `gate` 现算，不再依赖历史值。

    三条规则（第 2、3 条 v2.0.1 收紧）：
      1. 学历明确不达标 → 一律最差档，**不给模型任何选择权**（含模型给该档时的对齐）；
      2. 学历未识别 → 信息缺失 ≠ 条件不达标 → 不得给最差档，最低取中性档；
      3. 学历达标 → 采信模型，但**不接受最差档**；模型给了就下调一档
         （配置顺序上比最差档好一档，A/B/C 三档时即 B「储备」）。

    **底线（v2.0.1，业务方定，不可逾越）**：最差档是**学历不达标专用**的否定结论。
    业务方原话："硕士投递工艺技术是达标的，除非学历不够是不能判定为 C" ——
    学历达标的人，技能再弱，最低也只能是「储备」，**模型也不允许把他判成最差档**。
    （第 2 条原来写的是"采信模型"，与本文档自述的"不判最差档"不符，顺手补齐。）
    模型没给档位时用规则值，同样受 2、3 两条约束。
    """
    _tiers = tiers_mod.all_tiers()
    _hard = tiers_mod.hard_shortfall_tier()          # 配置项，原来这里写死 "D"
    # 「最差档的上一档」：学历达标/未识别时，模型若给最差档就下调到这里
    _i = _tiers.index(_hard) if _hard in _tiers else max(len(_tiers) - 1, 0)
    _above_hard = _tiers[_i - 1] if _i > 0 else _hard

    # ① 学历明确不达标 —— 唯一的硬门槛，模型没有任何选择权
    if gate is not None and gate.get("failed"):
        return _hard, (f"学历门槛（{gate.get('edu')}低于岗位要求 {gate.get('edu_min')}，"
                       f"由系统直接判 {_hard}）")

    # ②③ 学历达标 / 未识别 —— 都不允许给出「最差档」这个否定结论
    _unknown = bool(gate is not None and gate.get("unknown"))
    _floor = tiers_mod.mid_tier() if _unknown else _above_hard
    _who = "学历未识别" if _unknown else "学历已达标"

    if model_tier in _tiers:
        if tiers_mod.rank(model_tier) > tiers_mod.rank(_floor):
            return _floor, (f"{_who}，模型建议的 {model_tier} 触到档位底线 → "
                            f"下调为 {_floor}（{tiers_mod.label(_floor)}）")
        return model_tier, "模型"
    if rule_tier in _tiers:
        if tiers_mod.rank(rule_tier) > tiers_mod.rank(_floor):
            return _floor, (f"{_who}，规则档 {rule_tier} 触到档位底线 → "
                            f"下调为 {_floor}（{tiers_mod.label(_floor)}）")
        return rule_tier, "学历门槛" if rule_tier == _hard else "规则"
    return rule_tier, "规则"


def _evidence_map(cand: dict) -> dict[str, str]:
    out: dict[str, str] = {}
    for d in (cand.get("skill_detail") or []):
        if d.get("evidence"):
            out[d.get("canonical")] = d["evidence"]
    return out


def resolve_requirements(jd: dict) -> tuple[list[str], list[str]]:
    """把 JD 里的必需/加分技能归一到本体名，避免"XRD"与"X射线衍射"被当成两回事。"""
    must = jd.get("must", {})
    pref = jd.get("preferred", {})
    req = nz.resolve_query_terms(list(must.get("skills_required") or []))
    bonus = nz.resolve_query_terms(list(pref.get("skills") or []))
    return req, bonus


def _job_hint(jd: dict) -> str:
    """给"按建议岗位判的 D"一句可读的来路说明（界面上要让人知道这把尺子是谁的）。"""
    return str((jd or {}).get("title") or (jd or {}).get("_job_title") or "建议岗位")


def grade(cand: dict, jd: dict, job_confirmed: bool = False) -> dict:
    """极简分级：学历不达标 + 已归岗 → D；其余一律 C（等模型给更准确的建议）。

    v1.12 重构：删掉了整套加权打分（学历/年限/技能/加分/专业方向五维度）和
    A/B/C 阈值分档——那些是"系统假装很精确"的产物。实际上 HR 看的是
    模型的分析文字和业务方向，不是小数点后三位的分数。
    本函数只做两件事：
      1. 学历门槛检查（唯一能判最差档的条件）；
      2. 技能命中/缺失（给模型和展示用）。
    其余全部交给模型。模型不可用时调用方给 C（中性默认）。

    `job_confirmed`（v1.24.15 起只影响**提示文案**，不再影响是否判最差档）：
    这把尺子是否来自已明确归岗的投递。未归岗而判最差档时，risks 里会写明
    "按系统建议岗位的学历门槛判定"，让 HR 知道结论的依据不是已确认的岗位。
    """
    must = jd.get("must", {})
    pref = jd.get("preferred", {})

    reasons: list[str] = []
    risks: list[str] = []

    # —— 学历：唯一的 D 判据（v1.24.15 走 edu_gate，与 lock_tier 同一把尺子）——
    gate = edu_gate(cand, jd)
    c_rank = gate["c_rank"]

    # —— 技能命中/缺失（给模型和展示用，不计分）——
    # v2.0.1：命中只认**已核验**技能（简历原文有出处）。未核验项是"岗位 JD 的词汇"
    # 被补进技能表的结果，算成命中会造出假命中（实测"分析化学的人命中真空熔铸"），
    # 而这一行命中/缺失会进给模型的提示词，直接左右档位结论。
    skills = set(_skill_names(cand.get("skills"), verified_only=True))
    # ⚠️ 但「技能有没有抽到」是另一回事，必须用**未过滤**的名字：
    # 抽到了却没证据 ≠ 信息缺失，后者是要 HR 去补字段的（missing_fields 会写进审计
    # 并在界面提示"缺：技能"）。两个口径混用会让"技能全未核验"的人被误标成缺技能。
    _all_skill_names = set(_skill_names(cand.get("skills")))
    ev = _evidence_map(cand)
    req, bonus_pool = resolve_requirements(jd)
    hit = [s for s in req if s in skills]
    miss = [s for s in req if s not in skills]
    hit_detail = [{"skill": s, "evidence": ev.get(s, "")} for s in hit]

    # —— 年限（给模型和展示用）——
    years = cand.get("years")
    # v1.13.6：年限维度已删（只剩学历门槛这一条硬规则），原先算出来的
    # y_min / years_ok 一次都没被用到，属于"看起来在判年限、其实没判"的误导代码。

    # —— 专业方向（给模型和展示用）——
    major_required = (must.get("major_required")
                      or pref.get("major_required") or [])
    major_check = mj.in_list(cand.get("major_canonical") or cand.get("major")
                             or cand.get("education_major"), major_required)
    major_check["via"] = cand.get("major_via") or "规则"

    # —— 分档：最差档只看学历，其余留给模型给建议 ——
    # v1.24.15：判最差档 **不再要求 job_confirmed**。
    # 过去"未归岗就不判"是为了避免用猜出来的建议岗位判死人，
    # 但代价是——学历不够的人在未归岗时落到了**中间档**，而中间档是模型可以合法改写的档，
    # 于是"学历不够直接判最差档"这条规则事实上只在已归岗时才成立。
    # 现在由 `edu_gate` 兜底：没有对应岗位（jd 为空）时不判最差档，
    # 有岗位（无论归岗还是系统建议）且学历明确不达标 → 一律判 `hard_shortfall_tier`（配置项）。
    # v1.29：档位字母/最差档/中性档全部来自 config/tiers.json，代码不写字面量。
    tier = None
    if gate["failed"]:
        tier = tiers_mod.hard_shortfall_tier()
        reasons.append(f"学历{gate['edu']}低于岗位要求（{gate['edu_min']}）")
        risks.append("学历不达本岗位门槛，本岗位暂不匹配；建议保留入池，供其他岗位召回")
        if not job_confirmed:
            risks.append(f"该投递尚未明确归岗，此处按系统建议岗位「{_job_hint(jd)}」的学历门槛判定")
    elif gate["unknown"]:
        # 学历未识别：只给一个**中性档**并提示人工核对。不能判最差档 ——
        # 那是"否定结论"，而信息缺失只是"没读到"，把人判死是凭空造结论。
        tier = tiers_mod.mid_tier()
        risks.append("学历未识别，请人工核对")

    if hit:
        reasons.append(f"命中技能 {len(hit)}/{len(req)}：{'、'.join(hit[:3])}")
    if miss:
        risks.append(f"缺技能 {'、'.join(miss[:3])}")

    # **信息缺失**（v1.24.9 拆分）：姓名/年限/技能任一抽不到 → HR 看得见缺什么。
    # 这跟"简历原文解析失败"（ingest.py 的 parse_ok=False）是**两件不同的事**：
    #   · 信息缺失 = 简历读出来了，但姓名/年限/技能没抽到 → 人工补填即可
    #   · 解析失败 = 原文根本读不出来（PDF 损坏、图片无 OCR） → 要去看原件
    # 过去两者共用 needs_review 一个字段，界面上只能统称"待人工判读"，
    # HR 看到不知道该去补字段还是去修文件——现在分成 needs_review / parse_failed。
    missing_fields: list[str] = []
    if not cand.get("name"):
        missing_fields.append("姓名")
    if years is None:
        missing_fields.append("年限")
    if not _all_skill_names:
        missing_fields.append("技能")
    needs_review = bool(missing_fields)

    return {
        "tier_suggested": tier,           # 最差档（学历门槛）/ 中性档（学历未识别）/ None（等模型给建议）
        "score": None,                    # v1.12 移除加权打分：没有分数就是没有，不填 0 冒充
        "reasons": reasons,
        "risks": risks,
        "hit": hit,
        "miss": miss,
        "miss_ontology": [s for s in miss if nz.canonical_of(s)],
        "miss_custom": [s for s in miss if not nz.canonical_of(s)],
        "preferred_hit": [],
        "hit_detail": hit_detail,
        "major_check": major_check,
        # breakdown 是 JSON 列，v1.12 加权打分删掉后基本空着；
        # 这里借它存"缺哪些字段"（v1.24.9），界面卡片直接显示"缺：年限、技能"，
        # 不用改表结构、不用数据迁移。
        "breakdown": {"missing_fields": missing_fields} if missing_fields else {},
        # 信息缺失（缺哪些字段一目了然，界面上直接显示"缺：年限、技能"）
        "needs_review": bool(needs_review),
        "missing_fields": missing_fields,
        # 解析失败由 ingest.py 按 parse_ok 写入，这里默认 False（两条线不混）
        "parse_failed": False,
    }

# ============================================================
# 专业大类匹配（v1.6 → v1.7 开放词表改造）
# ============================================================
# 为什么要有这一层：关键词级的命中/缺失只能回答"哪几项技能对上了"，
# **回答不了"这个人的专业方向对不对口"**。实战里最差档最常见的真实原因是
# "大类错配"——候选人是干软件的，岗位要的是工艺，于是必需技能一项都对不上，
# 看上去像"这人条件差"，其实是"方向不同"。把大类层单独算出来，
# 解释就从"缺 3 项技能"升级为"专业方向不对口"，HR 一眼能看懂。
#
# v1.7 改造（三条不变式，见 docs §13）：
#
#   ① **未收录 ≠ 判错**。旧实现把「其他」（= 本体没收录）整体剔除出方向判定，
#      于是新领域一进来就退化：数字化岗 5 项全落"其他"→ 判"无法判定"；
#      财务岗 9 项只收录 1 项、两侧各剩同一个残留项、交集恰好非空 → **判"对口"**。
#      **结论由"哪一项碰巧被收录"决定**，这是本层最危险的失效模式。
#      现在：大类通道之外补一条**词面兜底通道**，未收录项照样参与判断，
#      并输出**置信度**与未收录明细，让"低置信"可见，而不是假装确定。
#
#   ② 大类底座换成**通用学科目录**（`config/majors.json`），不再用自造的业务大类。
#      学科目录有限且官方收口，一次建成长期够用；新增行业只加数据。
#
#   ③ 新增**专业需求**一等维度（`jd.must.major_required`）。
#      **专业不对口只提示、不淘汰**（材料物理去做工艺是常态），
#      只给 HR 一个显式的"在/不在需求清单内"标识与理由。


def guess_major_family(major: str | None) -> str:
    """从「专业」文本推断**学科门类**（工学 / 理学 / 管理学…）。

    v1.7 起底座是通用学科目录（`config/majors.json`，93 个一级学科 / 502 个可匹配写法），
    返回的是学科门类而不是 v1.6 的自造大类（软件/计算机、材料/冶金…）。
    为什么换：自造大类是"业务词表"，实测 18 项专业需求漏 4 项
    （凝聚态物理、仪器科学与技术、仪器仪表工程、无损检测），必然追不上新领域。
    推不出来如实返回空串（**不硬猜**）。
    """
    return mj.category_of(major)


def _skill_names(items, verified_only: bool = False) -> list[str]:
    """技能项取名字：调用方可能给技能名字符串，也可能给完整技能字典
    （`candidate_detail` 的 skills 带 evidence/verified/category）。

    两种都要认——只处理字符串时，传字典进来会在 `cat_of.get(...)` 处直接抛
    `TypeError: unhashable type: 'dict'`（自检「S续」用完整档案调 major_match 时抓到）。
    生产路径恰好都传了字符串，所以这个坑一直没暴露，但接口本身不该只认一种入参。

    `verified_only=True`（v2.0.1）= **只取简历原文有出处的技能**，命中判定专用。
    为什么必须分出来：未核验项算"命中"会造出**假命中** —— 实测出现过
    "分析化学背景的人命中 真空熔铸/热处理/材料成型"，这三项在简历里根本找不到
    出处（只是岗位 JD 的词汇被补进了技能表），却被算成命中、还写进给模型的提示词，
    把分析结论往错的方向带（业务方报的"工艺技术被判 C"就是这条链上的）。
    字符串入参没有核验信息（老接口形态），此时保持原行为、不误伤。
    """
    out: list[str] = []
    for s in items or []:
        if isinstance(s, str):
            out.append(s)
        elif isinstance(s, dict):
            n = s.get("name") or s.get("canonical_name")
            if not n:
                continue
            if verified_only and not s.get("verified"):
                continue
            out.append(str(n))
    return out


def _count_cats(names: list[str], cat_of: dict[str, str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for n in _skill_names(names):
        c = cat_of.get(n) or "其他"
        out[c] = out.get(c, 0) + 1
    return out


def _top_cats(counts: dict[str, int], top: int = 2) -> list[str]:
    # 计数降序；同计数按名称排序，保证同一份数据每次输出顺序一致（可复现）
    return [k for k, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:top]]


def _dedup(names: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for n in _skill_names(names):
        if n not in seen:
            seen.add(n)
            out.append(n)
    return out


def _unclassified(names: list[str], cat_of: dict[str, str]) -> list[str]:
    return [n for n in _dedup(names) if (cat_of.get(n) or "其他") == "其他"]


def major_match(cand: dict, jd: dict, cat_of: dict[str, str]) -> dict:
    """专业大类对照：岗位侧重哪些大类 ↔ 候选人能力集中在哪些大类。

    `cat_of` 是「技能名 → 大类」映射（`db.skill_categories()`），
    **岗位侧与候选人侧用同一个映射**，否则两边的统计口径会不一致。

    返回 `verdict`：对口 / 部分对口 / 错配 / 无法判定；外加 `confidence`
    （高/中/低）与 `channel`（大类 / 词面），见下方"三条不变式①"。

    **「其他」不参与方向判断**：它表示"本体里没有这条技能、归不了类"，
    是"我们不知道它属于哪个方向"，不是"这个人擅长杂项"。
    把它算进侧重会出现「岗位侧重 其他（4 项）」这种没有信息量、还会误导人的结论
    （实测踩到：软件岗 5 项技能里 4 项未归类，"其他"一度排在侧重第一位）。

    **但"排除"的只是它进入"侧重/交集"统计，不是把它从判定里丢掉**——
    v1.7 前"丢掉"的后果是：两侧已归类技能都为空时直接判"无法判定"
    （数字化岗 0/5），或两侧各剩同一个残留项、交集恰好非空而判"对口"（财务岗 1/9）。
    现在这两种退化都在 `channel="词面"` 的兜底通道里被兜住，并降级为低置信。
    """
    OTHER = "其他"
    req, bonus_pool = resolve_requirements(jd)
    job_names = _dedup(list(req) + list(bonus_pool))
    cand_names = _dedup(list(cand.get("skills") or []))

    job_cats = _count_cats(job_names, cat_of)
    cand_cats = _count_cats(cand_names, cat_of)
    job_other = job_cats.pop(OTHER, 0)      # 取出后从"侧重统计"里排除
    cand_other = cand_cats.pop(OTHER, 0)
    job_focus, cand_focus = _top_cats(job_cats), _top_cats(cand_cats)
    overlap = sorted(set(job_cats) & set(cand_cats))
    major_family = guess_major_family(cand.get("major") or cand.get("education_major"))

    # —— 不变式① 兜底通道：未收录项不丢，靠字符相似度参与判定 ——
    job_un = _unclassified(job_names, cat_of)
    cand_un = _unclassified(cand_names, cat_of)
    fallback: list[dict] = []
    for jt in job_un:
        best, score = mj.best_match(jt, cand_names)
        if best is not None and score >= mj.SIM_THRESHOLD:
            fallback.append({"job_skill": jt, "cand_skill": best, "score": score})

    has_cls = bool(job_cats) and bool(cand_cats)
    # 专业维度先算出来：它既是独立呈现的一等维度，也是技能通道失效时的第二条通道。
    # v1.11：优先用**归一后的专业**（与 grade 同一口径，避免两处判定不一致）。
    major_check = mj.in_list(cand.get("major_canonical") or cand.get("major")
                             or cand.get("education_major"),
                             (jd.get("must") or {}).get("major_required")
                             or (jd.get("preferred") or {}).get("major_required"))

    # —— 置信度：判定的可靠程度，与判定结论分开表达 ——
    job_n, cand_n = len(job_names), len(cand_names)
    job_cov = (job_n - job_other) / job_n if job_n else 0.0
    cand_cov = (cand_n - cand_other) / cand_n if cand_n else 0.0
    min_cov = min(job_cov, cand_cov)

    if has_cls:
        channel = "大类"
        if not overlap:
            verdict = "错配"
        elif job_focus and cand_focus and job_focus[0] == cand_focus[0]:
            verdict = "对口"                   # 两侧的**首位**大类一致才算方向对得上
        else:
            verdict = "部分对口"
        # **依据薄弱时不给"对口"**：财务岗 9 项需求只收录 1 项、两侧各剩同一个残留项，
        # 交集恰好非空就判"对口"——结论由"哪一项碰巧被收录"决定。
        # 覆盖率不足 35% 一律降级为"部分对口"，并把依据薄弱写在结论旁边。
        if verdict == "对口" and min_cov < 0.35:
            verdict = "部分对口"
    elif major_check.get("in_list") is True:
        channel = "专业"
        verdict = "部分对口"
    elif fallback:
        channel = "词面"
        verdict = "部分对口"
    else:
        channel = "无"
        verdict = "无法判定"

    if channel == "词面":
        confidence = "低"
    elif channel == "专业":
        confidence = "中"
    elif min_cov >= 0.6:
        confidence = "高"
    elif min_cov >= 0.35:
        confidence = "中"
    else:
        confidence = "低"

    def _fmt(c: dict[str, int]) -> str:
        return "、".join(f"{k}（{v} 项）" for k, v in
                         sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))) or "无"

    other_note = ""
    if job_other or cand_other:
        other_note = (f"（另有未归类技能：岗位侧 {job_other} 项、候选人侧 {cand_other} 项，"
                      f"本体中没有对应条目；它们不计入「侧重」统计，"
                      f"但已纳入词面兜底通道）")

    weak = ""
    if channel == "大类" and min_cov < 0.35:
        weak = (f"（**依据薄弱**：岗位侧 {job_other}/{job_n} 项、候选人侧 {cand_other}/{cand_n} 项"
                f"不在技能本体中，方向结论的覆盖率仅 {round(min_cov * 100)}%，"
                f"已按「部分对口」呈现）")

    if verdict == "错配":
        note = (f"岗位侧重 {_fmt(job_cats)}，候选人技能集中在 {_fmt(cand_cats)}，"
                f"两者大类无交集——**这不是条件差，而是专业方向不对口**")
    elif verdict == "对口":
        note = f"岗位侧重 {_fmt(job_cats)}，候选人所长同为 {_fmt(cand_cats)}，专业方向对口"
    elif verdict == "部分对口" and channel == "大类":
        note = (f"岗位侧重 {_fmt(job_cats)}，候选人所长为 {_fmt(cand_cats)}，"
                f"大类有交集（{'、'.join(overlap)}）但重心不完全重合")
    elif verdict == "部分对口" and channel == "专业":
        note = ("两侧技能项未能在本体中归类，**改按专业维度判断**："
                f"{major_check.get('note') or '候选人专业落在岗位专业需求清单内'}")
    elif verdict == "部分对口":
        samples = ["{}↔{}".format(f["job_skill"], f["cand_skill"]) for f in fallback[:3]]
        note = (f"两侧已归类技能不足，改按「词面命中」判断：岗位需求中有 "
                f"{len(fallback)} 项与候选人技能文字相符"
                + (f"（如 {'、'.join(samples)}）" if samples else "")
                + f"；岗位侧 {job_other} 项、候选人侧 {cand_other} 项不在技能本体中，"
                f"**判定可靠性低**")
    else:
        reason = []
        if not job_names:
            reason.append("岗位未设定技能要求")
        if not cand_names:
            reason.append("候选人技能未识别")
        if not reason and not has_cls:
            reason.append("两侧需求项均未在技能本体中归类，且无法按文字比对")
        if not reason:
            reason.append("两侧已归类技能不足")
        note = "、".join(reason) + "，无法做专业大类对照"

    note = note + weak + other_note
    if major_check.get("note") and channel != "专业":
        # 专业通道下正文已经引用了这句，避免同一句话出现两遍
        note += "；" + major_check["note"]

    # 把"其他"放回统计里（供界面完整展示），但方向判断用的是排除后的口径
    if job_other:
        job_cats[OTHER] = job_other
    if cand_other:
        cand_cats[OTHER] = cand_other

    return {
        "job_categories": job_cats,
        "cand_categories": cand_cats,
        "job_focus": job_focus,
        "cand_focus": cand_focus,
        "overlap": overlap,
        "verdict": verdict,
        "confidence": confidence,
        "channel": channel,
        "major_family": major_family,
        "major_label": mj.family_label(cand.get("major") or cand.get("education_major")),
        "major_check": major_check,
        "fallback": fallback,
        "unclassified": {"job": job_other, "cand": cand_other},
        "unclassified_terms": {"job": job_un, "cand": cand_un},
        "note": note + other_note,
    }
