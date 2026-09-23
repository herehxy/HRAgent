"""分级层（尺子）：把投递归入 A/B/C/D 四档——**不淘汰、不丢弃**。

与"筛选器"的根本差别：硬性条件不足只降档为 D（暂不匹配当前岗位，转人才库待召回），
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

EDU_RANK = {"大专": 1, "专科": 1, "本科": 2, "学士": 2, "研究生": 3, "硕士": 3, "博士": 4}


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


def grade(cand: dict, jd: dict, tiers_cfg: dict) -> dict:
    must = jd.get("must", {})
    pref = jd.get("preferred", {})
    thr = tiers_cfg.get("thresholds", {"A": 0.85, "B": 0.65, "C": 0.45})
    d_tier = tiers_cfg.get("hard_shortfall_tier", "D")

    reasons: list[str] = []
    risks: list[str] = []

    # —— 学历 ——
    c_rank = EDU_RANK.get(cand.get("education") or "", 0)
    m_rank = EDU_RANK.get(must.get("education_min", "本科"), 2)
    edu_ok = c_rank >= m_rank
    if c_rank == 0:
        risks.append("学历未识别")
        edu_score = 0.0
    elif not edu_ok:
        risks.append(f"学历低于最低线（要求 {must.get('education_min')}）")
        edu_score = 0.0
    else:
        reasons.append(f"学历{cand.get('education')}达标")
        if c_rank > m_rank:
            reasons.append("学历高于最低线")
        edu_score = 0.25

    # —— 年限 ——
    years = cand.get("years")
    y_min = int(must.get("years_min", 0) or 0)
    if years is None:
        risks.append("工作年限未识别")
        years_score, years_ok = 0.0, False
    elif years < y_min:
        risks.append(f"工作年限不足（要求 {y_min} 年）")
        years_score = round(0.15 * (years / y_min), 3) if y_min else 0.0
        years_ok = False
    else:
        reasons.append(f"{years} 年经验达标")
        years_score = 0.15 + min(0.10, 0.02 * (years - y_min))
        years_ok = True

    # —— 必需技能（缺失只提示，不淘汰；仅计已核验技能）——
    # 不变式①：把"必须项"分成**本体已收录**与**岗位自定义（本体未收录）**两类再报缺。
    # 为什么分：两者都确实是岗位要求，但含义不同——前者可以在界面上点开证据核对，
    # 后者只能按文字比对、且说明"这个人不行"之前要先知道"我们本来就没收录这个词"。
    # 合在一行报"缺必需技能"，读起来像"这人能力不足"，实际可能是本体缺口。
    # 入参两种形态都要认（技能名字符串 / 带 evidence 的技能字典）：
    # `major_match` 早就用 `_skill_names` 兼容了，`grade` 只认字符串——
    # 于是"用完整档案（candidate_detail）调 grade"会直接
    # `TypeError: unhashable type: 'dict'`。同一份数据在两个函数里两种待遇，
    # 迟早有人在另一条路径上踩到。
    skills = set(_skill_names(cand.get("skills")))
    ev = _evidence_map(cand)
    req, bonus_pool = resolve_requirements(jd)
    hit = [s for s in req if s in skills]
    miss = [s for s in req if s not in skills]
    miss_onto = [s for s in miss if nz.canonical_of(s)]
    miss_custom = [s for s in miss if not nz.canonical_of(s)]
    must_score = 0.35 * (len(hit) / len(req)) if req else 0.35
    if hit:
        reasons.append(f"必需技能命中 {len(hit)}/{len(req)}")
    if miss_onto:
        risks.append("缺必需技能：" + "、".join(miss_onto))
    if miss_custom:
        risks.append("岗位自定义要求未命中（不在技能本体中，按文字比对）："
                     + "、".join(miss_custom))

    # —— 加分项 ——
    certs = set(cand.get("certificates") or [])
    pref_hits = [s for s in bonus_pool if s in skills]
    pref_certs = [c for c in (pref.get("certificates") or []) if c in certs]
    pref_score = min(0.15, 0.03 * (len(pref_hits) + len(pref_certs)))
    if pref_hits or pref_certs:
        reasons.append(f"加分项命中 {len(pref_hits) + len(pref_certs)} 项")

    score = round(edu_score + years_score + must_score + pref_score, 3)

    # —— 分档（无淘汰）——
    # 规则一：学历/年限未达硬性门槛 -> D（本岗位暂不匹配，转人才库保留，日后可被其他岗位召回）
    # 规则二：技能缺口 >= 2 项 -> 分数达标给 C（有相关背景的储备），不达标才给 D
    # 规则三：其余按分数与缺口数落 A/B/C
    thr_c = thr.get("C", 0.45)
    if (not edu_ok) or (not years_ok):
        tier = d_tier
        if not edu_ok:
            risks.append("学历未过门槛，本岗位暂不匹配；建议保留入池，供其他岗位召回")
        if not years_ok:
            risks.append("年限未过门槛，本岗位暂不匹配；建议保留入池")
    elif len(miss) >= 2:
        tier = "C" if score >= thr_c else d_tier
        if tier == "C":
            reasons.append("技能缺口较大但具备相关背景，建议入储备池")
    elif not miss and score >= thr.get("A", 0.85):
        tier = "A"
    elif score >= thr.get("B", 0.65) and len(miss) <= 1:
        tier = "B"
    elif score >= thr_c:
        tier = "C"
    else:
        tier = d_tier

    # —— 加分项缺口 ——
    # v1.7.1 修语义：`unverified_skills` 里的东西**不是**"候选人自称但没证据的技能"，
    # 而是"**岗位要求的**这项技能在简历原文里找不到证据"，也就是候选人**不具备**。
    # 抽取器的词表本身就是该岗位 JD 的技能清单（见 extract._jd_terms），
    # 命中才写 verified=1，找不到原文片段就写 verified=0——所以它和 `miss` 同源。
    # 改造前写成"以下技能未能在原文中定位到证据，未计入命中"，读起来像"简历里写了、
    # 只是没证据"，方向正好反了；更糟的是换过尺子的候选人名下会残留旧尺子的需求词
    # （实测：软件岗候选人被列出"增材制造、热加工、真空熔铸"等材料类词），
    # HR 会以为简历里真写过这些。
    # 现在只报 `miss` 之外的部分（即未命中的**加分项**），并如实说明这是岗位要求。
    _miss_set = set(miss)
    unverified = [s for s in (cand.get("unverified_skills") or []) if s not in _miss_set]
    if unverified:
        risks.append("岗位加分技能未在原文中找到证据，未计入命中："
                     + "、".join(unverified[:5]))

    needs_review = (not cand.get("name")) or (years is None) or (not skills)

    # —— 不变式③ 专业需求（一等维度，**不参与打分与淘汰**）——
    # 材料物理去做工艺是常态，专业不对口不该判死；但必须让 HR 看得见。
    major_required = (must.get("major_required")
                      or pref.get("major_required") or [])
    major_check = mj.in_list(cand.get("major") or cand.get("education_major"),
                             major_required)

    return {
        "score": score,
        "tier_suggested": tier,
        "reasons": reasons,
        "risks": risks,
        "hit": hit,
        "miss": miss,
        "miss_ontology": miss_onto,
        "miss_custom": miss_custom,
        "preferred_hit": pref_hits + pref_certs,
        "hit_detail": [{"skill": s, "evidence": ev.get(s, "")} for s in hit],
        "major_check": major_check,
        "breakdown": {
            "学历": round(edu_score, 3),
            "年限": round(years_score, 3),
            "必需技能": round(must_score, 3),
            "加分项": round(pref_score, 3),
        },
        "needs_review": bool(needs_review),
    }


# ============================================================
# 专业大类匹配（v1.6 → v1.7 开放词表改造）
# ============================================================
# 为什么要有这一层：关键词级的命中/缺失只能回答"哪几项技能对上了"，
# **回答不了"这个人的专业方向对不对口"**。实战里 D 档最常见的真实原因是
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


def _skill_names(items) -> list[str]:
    """技能项取名字：调用方可能给技能名字符串，也可能给完整技能字典
    （`candidate_detail` 的 skills 带 evidence/verified/category）。

    两种都要认——只处理字符串时，传字典进来会在 `cat_of.get(...)` 处直接抛
    `TypeError: unhashable type: 'dict'`（自检「S续」用完整档案调 major_match 时抓到）。
    生产路径恰好都传了字符串，所以这个坑一直没暴露，但接口本身不该只认一种入参。
    """
    out: list[str] = []
    for s in items or []:
        if isinstance(s, str):
            out.append(s)
        elif isinstance(s, dict):
            n = s.get("name") or s.get("canonical_name")
            if n:
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
    # 专业维度先算出来：它既是独立呈现的一等维度，也是技能通道失效时的第二条通道
    major_check = mj.in_list(cand.get("major") or cand.get("education_major"),
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
