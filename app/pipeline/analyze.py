"""大模型增强分析：把"模板句理由"升级为"模型读简历后的判断"。

两个能力（都在模型不可用时返回 None，调用方自动回退规则结果）：
  analyze_fit       岗位匹配分析 -> 亮点 / 风险 / 一句话结论 / 建议档位 / 置信度
  draft_interview   面试提纲    -> 结合 JD 与简历的定制问题（含考察意图）

注意：模型只输出"建议"，不出最终结论；最终档位与是否面试仍由 HR 确认。
"""
from __future__ import annotations

import sys

from ..agent import llm
from . import normalize as nz
from .tier import guess_major_family

_FIT_SYSTEM = (
    "你是稀有金属材料行业的资深招聘官。依据岗位要求(JD)与候选人简历，做匹配分析。"
    "只依据给定信息，不要编造经历。严格输出 JSON："
    '{"highlights":["亮点，最多4条"],"risks":["不足或需确认点，最多4条"],'
    '"summary":"一句话结论(40字内)","suggested_tier":"A/B/C/D（必填，不能省略）",'
    '"confidence":0.0到1.0,"business_direction":"这个人实际做的业务方向(2-8字，如 材料工艺/后端开发)"}。'
    "**suggested_tier 必须输出 A/B/C/D 之一，这是最重要的字段，漏了等于没做分析。**"
    "档位口径：A=必需条件全中且明显匹配；B=基本匹配、缺1项可培养；C=条件偏弱但有潜力；"
    "D=本岗位暂不匹配（仍会保留进人才库）。"
    "判断时**先看专业方向是否对口**：若候选人技能集中在与岗位侧重完全不同的大类"
    "（例如候选人是软件/计算机方向、岗位要求工艺与材料方向），要在 risks 里明确写成"
    "『专业方向不对口』并说明大类差异，而不是笼统地说『技能缺失X项』——"
    "前者是方向问题（换岗位才有意义），后者是深度问题（培养可以补）。"
    "business_direction 写这个人**实际做的业务**（从经历与技能推断），不是他的专业名称。"
    "岗位信息里没给出的内容（部门、公司业务方向等）不要假设，也不要写进结论。"
)

_IV_SYSTEM = (
    "你是稀有金属材料行业的技术面试官。依据岗位要求与候选人简历，设计有针对性的面试问题。"
    "严格输出 JSON："
    '{"questions":[{"q":"问题","why":"考察意图(20字内)"}]}，问题 5-8 个。'
    "**问题必须锚定「岗位要求」里这个岗位的必需技能与职责**："
    "优先围绕候选人简历中的具体项目、与该岗位相关的技能缺口展开；"
    "**不要问与该岗位无关领域的通用问题**"
    "（例如岗位是软件开发，就不要问材料表征、金相、热处理类问题）。"
    "若候选人的专业方向与该岗位明显不对口，至少用一道题确认其转岗动机与可迁移能力。"
    "**岗位信息里没给出的内容（部门、公司业务方向、团队规模等）一律不要假设**："
    "写「部门:未指定」时就不提部门，不要把别的岗位的部门名安到这个岗位上。"
)


def _cat_text(names: list[str]) -> str:
    """把技能名汇总为「大类（N 项）」，供提示词里做专业大类对照。"""
    cnt: dict[str, int] = {}
    for n in names:
        c = nz.category_of(n)
        cnt[c] = cnt.get(c, 0) + 1
    if not cnt:
        return "无"
    return "、".join(f"{k}（{v} 项）" for k, v in sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0])))


def _names(v) -> list[str]:
    """技能/证书统一取名字。列表页给字符串、完整档案给 {name, verified} 字典，两种都要认。"""
    out: list[str] = []
    for s in v or []:
        if isinstance(s, str):
            out.append(s)
        elif isinstance(s, dict):
            name = s.get("name") or s.get("canonical_name")
            if name:
                out.append(str(name) if s.get("verified", True) else f"{name}(未核验)")
    return out


def _cand_brief(cand: dict) -> str:
    skills = _names(cand.get("skills"))
    certs = _names(cand.get("certificates"))
    edu = cand.get("education") or cand.get("edu_level")
    years = cand.get("years") if cand.get("years") is not None else cand.get("years_exp")
    major = cand.get("major")
    family = guess_major_family(major)
    canon = cand.get("major_canonical")
    return (
        f"姓名:{cand.get('name')}；学历:{edu}；年限:{years}；"
        f"院校:{cand.get('school')}；专业:{major}"
        + (f"（推断专业大类：{family}）" if family else "")
        + (f"（归一专业：{canon}）" if canon and canon != major else "") + "；"
        f"\n技能:{'、'.join(skills) or '未识别'}"
        f"\n技能专业大类分布:{_cat_text(skills)}"
        f"\n证书:{'、'.join(certs) or '无'}\n"
        f"系统规则评分:{cand.get('score')}（建议档 {cand.get('tier_suggested')}，"
        f"命中{cand.get('hits') or []}，缺失{cand.get('miss') or []}）\n"
        f"简历原文:\n{(cand.get('raw_text') or '')[:3000]}"
    )


def _jd_brief(jd: dict) -> str:
    must_skills = list(jd["must"]["skills_required"])
    pref_skills = list(jd.get("preferred", {}).get("skills", []))
    # 部门为空时必须显式写"未指定"（v1.6 修复）：此前直接 `jd.get('department')`
    # 会渲染成 "部门:"，模型看到空值就自己挑一个填上——实测把软件岗的面试题
    # 写成了"如果加入我们材料工艺所"，把另一个岗位的部门安到了这个岗位上。
    dept = jd.get("department") or "未指定"
    return (
        f"岗位:{jd.get('role')}；部门:{dept}\n"
        f"最低学历:{jd['must']['education_min']}；最低年限:{jd['must']['years_min']}；"
        f"必需技能:{'、'.join(must_skills)}\n"
        f"加分技能:{'、'.join(pref_skills)}"
        f"\n岗位技能专业大类侧重:{_cat_text(must_skills + pref_skills)}"
        + (f"\n岗位职责/说明:{jd['note']}" if jd.get("note") else "")
    )


def analyze_fit(cand: dict, jd: dict) -> dict | None:
    try:
        return llm.chat_json(_FIT_SYSTEM, f"【岗位要求】\n{_jd_brief(jd)}\n\n【候选人】\n{_cand_brief(cand)}")
    except Exception as exc:
        print(f"[analyze_fit] 模型调用失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return None


def draft_interview(cand: dict, jd: dict, focus: str = "") -> dict | None:
    extra = f"\n【HR 特别关注】{focus}" if focus else ""
    try:
        return llm.chat_json(
            _IV_SYSTEM,
            f"【岗位要求】\n{_jd_brief(jd)}\n\n【候选人】\n{_cand_brief(cand)}{extra}",
        )
    except Exception as exc:
        print(f"[draft_interview] 模型调用失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return None


def _skill_rows(cand: dict) -> list[dict]:
    """技能列表的两种形态都要认：列表接口给 `skill_detail`，详情接口给 `skills`。"""
    rows = cand.get("skill_detail") or cand.get("skills") or []
    return [r for r in rows if isinstance(r, dict)]


def _collect_evidence(cand: dict, limit: int = 5) -> list[dict]:
    """从**已核实**的技能里取原文片段。

    反幻觉的要求：自动生成的分析必须能回答"依据是什么"，所以只收
    `verified=True` 且带 `evidence` 原文的技能——未核实的技能本身就没找到出处，
    拿它当证据等于把幻觉固化进库里。
    """
    out: list[dict] = []
    for s in _skill_rows(cand):
        if s.get("verified") and (s.get("evidence") or "").strip():
            out.append({"skill": s.get("name"), "quote": s["evidence"].strip()})
        if len(out) >= limit:
            break
    return out


def auto_insight(cand: dict, jd: dict | None, grade_result: dict | None = None) -> dict:
    """入库即分析：产出「一句话画像 + 理由 + 风险 + 证据」。

    与 `/api/candidates/{cid}/analyze` 的区别：**这里永不返回 None**。
    那条接口是 HR 主动点的，模型不可用就如实报错；而入库路径是自动的，
    失败必须降级成规则结论——否则"自动分析"会变成"模型一挂就无声无息地没有分析"。

    `jd` 为 None（候选人尚未归岗）时**不做岗位匹配判断**（项目口径：不硬套默认尺子），
    但仍给一份与岗位无关的简历画像——技能概览、数据完整度、待确认项。
    否则新库里一个岗位都没有时，这项功能看起来完全没生效。

    注意：本函数**只产文本**，不写任何档位字段。自动分析不等于自动决定。
    """
    evidence = _collect_evidence(cand)

    # —— 无对应岗位：只做简历画像，不做匹配判断 ——
    if not jd:
        app = grade_result or {}
        edu = cand.get("edu_level") or "学历未识别"
        years = cand.get("years_exp")
        major = cand.get("major") or "专业未识别"
        rows = _skill_rows(cand)
        verified = [s.get("name") for s in rows if s.get("verified") and s.get("name")]
        all_skills = [s.get("name") for s in rows if s.get("name")]
        bits = [edu]
        if isinstance(years, int):
            bits.append(f"{years} 年经验")
        bits.append(major)
        summary = "｜".join(bits)
        if verified:
            summary += f"；已核实技能 {len(verified)} 项"
        # 理由取已核实技能（有原文出处的那些），而不是全部技能名
        reasons = verified[:4]
        risks = ["尚未归岗，本条未做岗位匹配判断——在「岗位管理」建岗位后可重算"]
        if len(all_skills) > len(verified):
            risks.append(f"有 {len(all_skills) - len(verified)} 项技能未在简历原文找到出处，"
                         "未计入命中")
        if app.get("needs_review"):
            risks.append("原文未能完整解析，档案标『待人工判读』")
        # 业务方向（模型提炼，仅展示）：未归岗也给，HR 看卡片时就知道这个人是干什么的
        bd = None
        try:
            from .major_llm import business_direction as _biz
            bd = _biz(cand)
        except Exception as exc:                           # noqa: BLE001
            print(f"[auto_insight] 业务方向提炼失败：{exc}", file=sys.stderr)
        return {"summary": summary, "reasons": reasons, "risks": risks,
                "evidence": evidence, "source": "auto_profile", "model": "",
                "suggested_tier": app.get("tier_suggested"), "confidence": None,
                "business_direction": bd}

    try:
        fit = analyze_fit(cand, jd)
    except Exception as exc:  # analyze_fit 内部已兜底，这里再保一层
        print(f"[auto_insight] 模型分析异常：{exc}", file=sys.stderr)
        fit = None

    if isinstance(fit, dict) and (fit.get("summary") or fit.get("highlights")):
        # suggested_tier 兜底：模型有时把档位写在 summary 里而不是结构化字段里
        model_tier = fit.get("suggested_tier")
        if model_tier not in ("A", "B", "C", "D"):
            import re as _re
            m = _re.search(r"(?:建议|初判|评分)[^\n]*?([A-D])\s*档",
                           fit.get("summary") or "")
            model_tier = m.group(1) if m else None
        # **档位决策**：D（学历不达标）优先于模型建议——
        # 学历是硬门槛，模型不能越过；其余情况模型建议优先于规则
        rule_tier = (grade_result or {}).get("tier_suggested")
        final_tier = rule_tier                                # 默认用规则
        if rule_tier != "D" and model_tier in ("A", "B", "C", "D"):
            final_tier = model_tier                           # 模型可用且规则非 D → 用模型
        return {
            "summary": (fit.get("summary") or "").strip(),
            "reasons": [x for x in (fit.get("highlights") or []) if x][:4],
            "risks": [x for x in (fit.get("risks") or []) if x][:4],
            "evidence": evidence,
            "source": "auto_ingest",
            "model": llm.load_cfg().get("model") or "",
            "suggested_tier": final_tier,
            "confidence": fit.get("confidence"),
            "business_direction": str(fit.get("business_direction") or "").strip() or None,
        }

    # —— 规则降级：用分级通道的结果，如实注明"未经模型判断" ——
    g = grade_result or {}
    score = g.get("score")
    tier = g.get("tier_suggested") or "—"
    summary = f"规则通道初判 {tier} 档"
    if isinstance(score, (int, float)):
        summary += f"（{score:.2f}）"
    summary += "。模型不可用，本条仅有规则结论，建议稍后重跑分析。"
    return {
        "summary": summary,
        "reasons": [x for x in (g.get("reasons") or []) if x][:4],
        "risks": [x for x in (g.get("risks") or []) if x][:4],
        "evidence": evidence,
        "source": "rule_fallback",
        "model": "",
        "suggested_tier": g.get("tier_suggested"),
        "confidence": g.get("confidence"),
        "business_direction": None,
    }
