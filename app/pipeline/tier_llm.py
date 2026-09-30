"""档位判定的主通道：**模型主导 + 规则交叉校验**（v1.12，方案 A）。

## 为什么改（2026-09-30 HR 口径）

旧口径是**纯规则加权**定档（技能命中率 0.30 + 学历 0.25 + 年限 0.20 + 方向 0.10 + 加分 ≤0.15，
再按阈值切 A/B/C/D）。它的根本问题是**把"关键词没对上"当成了"人不合适"**：

- 简历里写"真空自耗电弧炉操作、铸锭缺陷分析"，JD 写"真空熔炼"——规则判"缺必需技能"，实际高度对口；
- 换过行业的人（机械背景做工艺）技能词表天然对不上，被压到 D，而阅读判断会觉得"可培养"；
- 反过来，关键词堆砌但方向完全不同的简历，规则也可能给到 C。

所以从 v1.12 起：**档位由模型读完 JD 与简历后给出**，并强制它给出**原文证据**；
规则通道（`tier.grade`）继续并行计算，但**不参与定档**，只做三件事：
① 交叉校验（不一致 → 标"存疑/待复核"）；② 提供稳定的参考分（排序、报表口径不变）；
③ 模型不可用时的**兜底档位**。

## 四条红线（一条都不能松）

1. **反幻觉**：模型声称的每条命中技能都必须给出**简历原文片段**，系统逐条核对
   （归一化后子串匹配）；核对不过的**不计入命中**，进 `unverified_claims` 并在界面注明。
2. **可复现**：`temperature` 沿用 `config/model.json`（默认 0）；`PROMPT_VERSION` 与所用模型名
   随结论落库（`tier_meta`），同一条投递重算可比对"是谁、按哪版口径判的"。
3. **审计与责任**：档位来源（`tier_source: llm | rule`）与两份结论都落库；
   `tier_final` 仍必须 HR 确认——系统只出建议，这条老红线不变。
4. **降级**：模型不可达 / 返回不可解析 / `TP_LLM_GRADING=0` / `config/tiers.json` 里关掉
   → 完全退回旧行为（`tier_suggested = tier_rule`、`tier_source='rule'`），
   自检的确定性靠这个开关保证。

## 调用方式

```python
g = tier_llm.judge(cand, jd, tiers_cfg, raw_text=text, job_confirmed=bool(job_id))
```

返回结构 = 旧 `tier.grade()` 的全部键（`score/tier_suggested/reasons/risks/hit/miss/
preferred_hit/breakdown/needs_review`）+ 新键：

| 键 | 含义 |
|---|---|
| `tier_rule` / `score_rule` | 规则通道结论（参考值与交叉校验用） |
| `tier_source` | `'llm'` 或 `'rule'`（**档位是谁定的**） |
| `agreement` | 两通道是否一致（模型不可用时为 `None`） |
| `divergence` | 不一致时的差异说明（含两边理由） |
| `llm` | 模型结论原文（tier/confidence/summary/model/prompt_version/error） |
| `unverified_claims` | 声称有证据但核对不上的条目（反幻觉留痕） |
| `rule_hit` / `rule_miss` | 规则通道的命中/缺失（对照展示，不被模型结论覆盖） |
"""
from __future__ import annotations

import json
import os
import re

from ..agent import llm
from . import tier

# 提示词版本：改了 prompt 或输出结构必须进位——它随结论落库，用于解释"同一份简历
# 为什么昨天 A 今天 C"（口径变了，而不是模型飘了）。
PROMPT_VERSION = "tier-llm-v1"

_TIER_ORDER = ("A", "B", "C", "D")

_SYSTEM = """你是资深招聘评估员，为岗位筛选简历。你的任务是给出档位判断，并让每条判断都可核对。

档位口径（务必按此口径，不要自己发明）：
- A 优先面试：核心条件全中，方向明显对口，可直接安排面试。
- B 建议面试：基本匹配，缺 1 项但可培养（如技能可迁移、经历相关）。
- C 储备：条件偏弱，但有潜力或相关背景，暂不出局。
- D 暂不匹配当前岗位：方向或硬门槛明确不符（专业大类错配、学历明确低于岗位要求）。

关键要求：
1. **不要只看关键词命中率**。JD 的技能清单是提示，不是判卷标准：
   简历用了同义/上下位写法（如"真空自耗电弧炉操作"对应"真空熔炼"）要认为命中；
   经历与岗位实质要求相符时，个别技能词没对上**不足以**降档。
2. hits 里每一条都必须给出**简历原文中的证据片段**（逐字摘录，便于人工核对）。
   给不出原文证据的，**不要写进 hits**——宁可少写。
3. 硬门槛（学历低于岗位最低要求、工作年限明显不足）必须在 risks 里明说；
   但**只有明确不符时**才判 D。信息缺失（如年限未识别、专业未写明）不得作为降档理由，
   应如实写进 risks 提示人工核对。
4. 不确定就降低 confidence，不要硬给确定结论。

只输出 JSON（不要解释文字、不要 Markdown 代码块）：
{"tier":"A|B|C|D","confidence":0.0-1.0,
 "summary":"两三句话说清主要依据",
 "reasons":["支持结论的理由，每条一句话"],
 "risks":["风险/待人工核对项，每条一句话"],
 "hits":[{"skill":"技能名","evidence":"简历原文片段"}],
 "misses":["岗位要求但简历里确实找不到的项"]}"""


def _enabled(tiers_cfg: dict, use_llm: bool | None) -> bool:
    """模型通道是否启用。

    三级开关（任一关闭即退回纯规则）：
    - 调用方显式传 `use_llm=False`（例如"建议岗位试算"这种要跑 N 个岗位的路径）；
    - 环境变量 `TP_LLM_GRADING=0`——**自检与批量回放靠它拿到确定性**；
    - `config/tiers.json` 的 `llm_grading.enabled = false`（HR 想回到纯规则时改它）。
    """
    if use_llm is False:
        return False
    if str(os.environ.get("TP_LLM_GRADING", "")).strip().lower() in ("0", "false", "off", "no"):
        return False
    cfg = (tiers_cfg or {}).get("llm_grading") or {}
    if cfg.get("enabled") is False:
        return False
    # 没配模型地址就没有模型通道（不报错、静默走规则）
    return bool((llm.load_cfg() or {}).get("base_url"))


def _norm(s: str) -> str:
    """证据核对的归一：去掉所有空白与常见标点，仅留中日英数字。

    为什么这么狠：简历排版里"真空 熔铸"、"钛合金（TC4）"这类写法很常见，
    按字面比对会把真证据判成"查无此据"，反过来又放过不了假证据。
    """
    return re.sub(r"[\s\W_]+", "", str(s or ""), flags=re.UNICODE)


def evidence_verified(evidence: str, raw_text: str | None) -> bool:
    """模型给的证据片段是否真在原文里（归一化子串匹配，最短 3 字防噪声）。"""
    ev, raw = _norm(evidence), _norm(raw_text)
    if not ev or not raw:
        return False
    if len(ev) < 3:
        return False
    return ev in raw


def _build_user_prompt(cand: dict, jd: dict, raw_text: str | None,
                       rule: dict | None = None) -> str:
    """JD 摘要 + 候选人摘要 + **规则通道初判** + 原文节选。

    为什么把规则初判也喂进去：规则通道最擅长抓**硬门槛**（学历不达标、年限明显不足），
    而这正是模型容易"心软"放过去的地方；规则最不擅长的是关键词级的方向判断，
    那正是模型该覆盖的。把它作为"参照"而非"结论"给出，两边各用所长。
    原文节选是**证据核对的底本**：模型只能摘原文里的片段，否则会被核验挡下。
    截断到 6000 字符——本地 14B 模型上下文有限，且简历有效信息集中在前几屏。
    """
    from .analyze import _cand_brief, _jd_brief   # 复用同一套摘要口径，避免两处漂移

    must = jd.get("must") or {}
    head = [
        "【岗位要求】", _jd_brief(jd),
        f"学历最低:{must.get('education_min') or '未设'}；"
        f"年限最低:{must.get('years_min') if must.get('years_min') is not None else '未设'}；"
        f"专业需求:{'、'.join(must.get('major_required') or []) or '未设'}",
        "",
        "【候选人（已抽取字段）】", _cand_brief(cand),
    ]
    if rule:
        head += [
            "",
            "【规则通道初判（仅供参照，你可以不采纳）】",
            f"档位 {rule.get('tier_suggested')}（加权分 {rule.get('score')}）。"
            "规则只看关键词命中率与硬门槛，**它把'技能词没对上'当作不匹配**——"
            "这正是需要你复核的地方：若候选人的实际经历与岗位要求相符，请按你的判断给档位。",
        ]
        if rule.get("risks"):
            head.append("规则列出的风险：" + "；".join(rule["risks"][:6]))
    body = (raw_text or "").strip()
    if body:
        head += ["", "【简历原文节选（证据只能摘这里面的片段）】", body[:6000]]
    else:
        head += ["", "（未取得简历原文：无法核对证据，请只依据上面字段判断并降低 confidence）"]
    return "\n".join(head)


def _llm_grade(cand: dict, jd: dict, raw_text: str | None, rule: dict | None = None) -> dict:
    """走一次模型，返回 {"tier":..., "confidence":..., "hits":[...], ..., "model":..., "error":None}。

    任何失败都不抛给上层：返回带 `error` 的结果，由 judge() 决定退回规则。
    """
    cfg = llm.load_cfg() or {}
    out: dict = {"tier": None, "confidence": None, "summary": "", "reasons": [], "risks": [],
                 "hits": [], "misses": [], "model": cfg.get("model") or "",
                 "prompt_version": PROMPT_VERSION, "error": None}
    try:
        data = llm.chat_json(_SYSTEM, _build_user_prompt(cand, jd, raw_text, rule), cfg=cfg)
    except Exception as exc:                      # noqa: BLE001 — 模型/网络/解析任何失败都降级
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out
    if not isinstance(data, dict):
        out["error"] = "模型未返回 JSON 对象"
        return out
    t = str(data.get("tier") or "").strip().upper()[:1]
    if t not in _TIER_ORDER:
        out["error"] = f"模型档位不可识别：{data.get('tier')!r}"
        return out
    out["tier"] = t
    try:
        out["confidence"] = max(0.0, min(1.0, float(data.get("confidence"))))
    except (TypeError, ValueError):
        out["confidence"] = None
    out["summary"] = str(data.get("summary") or "").strip()
    out["reasons"] = [str(x).strip() for x in (data.get("reasons") or []) if str(x).strip()]
    out["risks"] = [str(x).strip() for x in (data.get("risks") or []) if str(x).strip()]
    out["misses"] = [str(x).strip() for x in (data.get("misses") or []) if str(x).strip()]
    for h in (data.get("hits") or []):
        if isinstance(h, dict):
            skill = str(h.get("skill") or "").strip()
            ev = str(h.get("evidence") or "").strip()
        else:                                     # 容错：模型偶尔只给技能名
            skill, ev = str(h).strip(), ""
        if skill or ev:
            out["hits"].append({"skill": skill, "evidence": ev})
    return out


def judge(cand: dict, jd: dict, tiers_cfg: dict, *, raw_text: str | None = None,
          job_confirmed: bool = False, use_llm: bool | None = None) -> dict:
    """档位判定的统一出口：模型主导，规则交叉校验，模型不可用则规则兜底。

    参数与 `tier.grade()` 保持一致（外加 `raw_text`：证据核对的底本），
    返回结构见模块 docstring——**旧键全部保留**，所以调用方可以只换函数名。
    """
    rule = tier.grade(cand, jd, tiers_cfg, job_confirmed=job_confirmed)
    out = dict(rule)
    out.update({
        "tier_rule": rule["tier_suggested"],
        "score_rule": rule["score"],
        "rule_hit": list(rule.get("hit") or []),
        "rule_miss": list(rule.get("miss") or []),
        "tier_source": "rule",
        "agreement": None,
        "divergence": None,
        "llm": None,
        "unverified_claims": [],
    })

    if not _enabled(tiers_cfg, use_llm):
        out["llm"] = {"skipped": True,
                      "why": "模型档位通道未启用（调用方关闭 / TP_LLM_GRADING=0 / "
                             "tiers.json 里 llm_grading.enabled=false / 未配置模型地址）"}
        return out

    g = _llm_grade(cand, jd, raw_text, rule)
    out["llm"] = g
    if g.get("error") or not g.get("tier"):
        # 降级：档位用规则值，但把"模型没判成"这件事留在数据里（不假装是模型判的）
        out["tier_source"] = "rule"
        out["divergence"] = {"kind": "llm_unavailable", "note": "模型档位不可用，已退回规则档位",
                             "error": g.get("error")}
        return out

    # —— 反幻觉：逐条核对证据，核不过的不计入命中 ——
    verified, unverified = [], []
    for h in g["hits"]:
        if h["evidence"] and evidence_verified(h["evidence"], raw_text):
            verified.append(h)
        else:
            unverified.append(h)
    out["unverified_claims"] = unverified

    # 模型结论落到对外字段：档位/理由/风险/命中都以模型为准（规则值另存 rule_* 供对照）
    out["tier_suggested"] = g["tier"]
    out["tier_source"] = "llm"
    if g["reasons"]:
        out["reasons"] = list(g["reasons"])
    if g["risks"]:
        out["risks"] = list(g["risks"])
    if verified:
        out["hit"] = [h["skill"] for h in verified if h["skill"]]
    if g["misses"]:
        out["miss"] = list(g["misses"])
    if unverified:
        out["risks"] = list(out["risks"]) + [
            f"模型声称命中但原文未找到证据（不计入）：{'、'.join(h['skill'] or h['evidence'] for h in unverified)}"
        ]

    cfg_llm = (tiers_cfg or {}).get("llm_grading") or {}
    min_conf = float(cfg_llm.get("min_confidence", 0.5))
    agreement = (g["tier"] == rule["tier_suggested"])
    out["agreement"] = agreement
    low_conf = (g["confidence"] is None) or (g["confidence"] < min_conf)
    if not agreement:
        out["divergence"] = {
            "kind": "tier_mismatch",
            "rule_tier": rule["tier_suggested"], "llm_tier": g["tier"],
            "rule_score": rule["score"],
            "rule_reasons": list(rule.get("reasons") or []),
            "rule_risks": list(rule.get("risks") or []),
            "note": "规则与模型档位不一致：模型判 "
                    f"{g['tier']}，规则按加权分 {rule['score']} 判 {rule['tier_suggested']}",
        }
    # 需要人工复核的三种情形：两通道打架 / 模型自己不确定 / 规则通道本来就要复核
    out["needs_review"] = bool(rule.get("needs_review")) or (not agreement) or low_conf
    return out


def meta_for_db(g: dict) -> str:
    """把模型通道的元信息序列化进 `applications.tier_meta`（可解释、可追责）。

    只存"是谁按哪版口径判的 + 置信度 + 证据核验结果"，不存 prompt 全文。
    """
    llm_info = g.get("llm") or {}
    return json.dumps({
        "tier_source": g.get("tier_source"),
        "agreement": g.get("agreement"),
        "prompt_version": llm_info.get("prompt_version"),
        "model": llm_info.get("model"),
        "confidence": llm_info.get("confidence"),
        "summary": llm_info.get("summary"),
        "error": llm_info.get("error"),
        "skipped": llm_info.get("skipped"),
        "unverified_claims": g.get("unverified_claims") or [],
        "divergence": g.get("divergence"),
    }, ensure_ascii=False)
