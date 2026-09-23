"""技能归一 + 证据强制。

两件事：

1. **归一**：把"XRD / X射线衍射 / X-ray diffraction"收敛到同一个本体条目（canonical），
   这样"做过 X 的人"这类查询才可回答，标签统计才不碎。

2. **证据强制（反幻觉）**：每条技能必须附上**简历原文片段**作为证据；
   找不到片段的技能 `verified=0`，**不计入岗位命中**。
   这条约束直接实现了设计方案里"不得无证据生成技能"的红线。

短英文缩写（≤3 字符，如 UT/MT/RT/PM）采用词边界匹配，避免在普通单词里误命中。
"""
from __future__ import annotations

import json
import os
import re

DEFAULT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "config", "ontology.json",
)

_ASCII_SHORT = re.compile(r"^[A-Za-z0-9+#\-\.]{1,3}$")
_LEVEL_WORDS = [
    ("精通", ("精通", "专精", "深厚的", "资深")),
    ("熟练", ("熟练", "熟悉", "掌握", "擅长", "丰富的", "多年")),
    ("了解", ("了解", "参与", "接触", "初步", "入门", "学习")),
]

_ONTOLOGY: dict | None = None
_INDEX: dict[str, str] | None = None
_META: dict[str, dict] | None = None
_TERMS: list[tuple[str, str]] | None = None


def load_ontology(path: str | None = None, reload: bool = False) -> dict:
    global _ONTOLOGY, _INDEX, _META
    if _ONTOLOGY is not None and not reload and path is None:
        return _ONTOLOGY
    target = path or DEFAULT_PATH
    with open(target, encoding="utf-8") as fh:
        data = json.load(fh)
    return _build(data)


def _build(data: dict) -> dict:
    global _ONTOLOGY, _INDEX, _META, _TERMS
    index: dict[str, str] = {}
    meta: dict[str, dict] = {}
    for item in data.get("skills", []):
        canon = item["canonical"]
        meta[canon] = {"canonical": canon, "category": item.get("category", "其他"),
                       "aliases": list(item.get("aliases") or [])}
        index[canon.lower()] = canon
    # 别名**两遍建索引**：先登记所有 canonical 名字，再登记别名且不覆盖已有键。
    # 为什么：别名与 canonical 同名时（领域包把 `成本核算` 从 `成本控制` 的别名
    # 提升为独立条目），单遍 + 直接赋值的结果取决于**条目在文件里的先后顺序**，
    # 会出现"独立条目被别人的别名顶掉"这种依赖排序的偶发行为。
    # 名字优先，别名只填空白位——这才是稳定口径。
    for item in data.get("skills", []):
        canon = item["canonical"]
        for a in item.get("aliases") or []:
            index.setdefault(a.lower(), canon)
    _ONTOLOGY, _INDEX, _META = data, index, meta
    _TERMS = None  # 词表变了，缓存失效
    return data


def ontology() -> dict:
    return _ONTOLOGY if _ONTOLOGY is not None else load_ontology()


def describe() -> dict:
    onto = ontology()
    cats: dict[str, int] = {}
    for item in onto.get("skills", []):
        cats[item.get("category", "其他")] = cats.get(item.get("category", "其他"), 0) + 1
    return {
        "version": onto.get("version"),
        "canonical_count": len(onto.get("skills", [])),
        "alias_count": len(_INDEX or {}),
        "categories": cats,
    }


def canonical_of(term: str) -> str | None:
    load_ontology()
    return (_INDEX or {}).get((term or "").strip().lower())


def ontology_categories() -> dict[str, str]:
    """本体里的「canonical → 专业大类」全量映射。

    用于把库内 `skills.category` 刷新到与本体一致（本体升级后存量技能要跟上），
    以及给不查库的调用方（如提示词组装）提供大类口径。
    """
    load_ontology()
    out: dict[str, str] = {}
    for item in ((_ONTOLOGY or {}).get("skills") or []):
        canon = item.get("canonical")
        if canon:
            out[str(canon)] = str(item.get("category") or "其他")
    return out


def category_of(term: str | None) -> str:
    """技能名 → 专业大类（材料/工艺/表征/软件/管理/其他）。

    用于「专业大类匹配」：把关键词级的命中上升到大类级的对照。
    别名会先归一到 canonical 再查大类；查不到一律归「其他」——**不猜**，
    宁可归到「其他」也不给它编一个领域，否则大类对照会变成假精确。
    """
    load_ontology()
    canon = canonical_of(term or "") or (term or "").strip()
    return (_META or {}).get(canon, {}).get("category", "其他")


def _iter_terms() -> list[tuple[str, str]]:
    """(匹配词, canonical)，长词优先，避免"钛合金"被"钛"抢先。结果缓存。"""
    global _TERMS
    load_ontology()
    if _TERMS is not None:
        return _TERMS
    terms: list[tuple[str, str]] = []
    for canon, info in (_META or {}).items():
        terms.append((canon, canon))
        for a in info.get("aliases", []):
            terms.append((a, canon))
    terms.sort(key=lambda t: -len(t[0]))
    _TERMS = terms
    return terms


def _find_all(text: str, term: str) -> list[re.Match]:
    if _ASCII_SHORT.match(term):
        pattern = r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])"
        return list(re.finditer(pattern, text, re.IGNORECASE))
    return list(re.finditer(re.escape(term), text, re.IGNORECASE))


def sentence_around(text: str, idx: int, width: int = 70) -> str:
    """截取包含 `idx` 处命中的整句，作为证据片段。

    关键点：边界必须**围绕命中点**向两侧找——向左侧找最近的句/行边界，
    向右侧找最近的句/行边界。只按绝对位置截取会取到相邻的无关句子。
    """
    if not text:
        return ""
    start = max(0, idx - width)
    end = min(len(text), idx + width)
    seg = text[start:end]
    rel = idx - start  # 命中点在 seg 内的位置

    left = 0
    for mark in ("\n", "。", "！", "？", "!", "?"):
        p = seg.rfind(mark, 0, rel)
        if p >= 0:
            left = max(left, p + 1)
    if rel - left > 60:
        for mark in ("；", ";", "，", ","):
            p = seg.rfind(mark, 0, rel)
            if p >= 0:
                left = max(left, p + 1)

    right = len(seg)
    for mark in ("\n", "。", "！", "？", "!", "?"):
        p = seg.find(mark, rel)
        if p >= 0:
            right = min(right, p)
    if right - rel > 60:
        for mark in ("；", ";", "，", ","):
            p = seg.find(mark, rel)
            if p >= 0:
                right = min(right, p)

    out = seg[left:right].strip()
    if len(out) > 140:
        out = out[:140] + "…"
    return out


def infer_level(text: str, idx: int, lookback: int = 24) -> str | None:
    window = text[max(0, idx - lookback): idx]
    for level, words in _LEVEL_WORDS:
        if any(w in window for w in words):
            return level
    return None


_BULLET_HINTS = ("负责", "参与", "主导", "熟悉", "掌握", "使用", "从事", "完成",
                 "开发", "优化", "研究", "管理", "编制")
_SECTION_KEYS = ("技能", "经历", "背景", "证书", "项目", "成果")
_EDU_MARKS = ("大学", "学院", "职业技术学院", "专业：")


def section_of(text: str, idx: int) -> str:
    """向上回看，找出命中点所属的小标题（如"专业技能"）。"""
    recent = [ln.strip() for ln in text[:idx].split("\n")][-10:]
    for line in reversed(recent):
        if 2 <= len(line) <= 14 and any(k in line for k in _SECTION_KEYS):
            return line
    return ""


def best_evidence(text: str, terms: list[str]) -> tuple[str, int]:
    """同一技能多次出现时，挑最能支撑"这是他的技能"的那一句。

    打分优先级：工作经历/技能段落 > 教育背景段落（"材料成型及控制工程"是专业名，
    出现在专业名里不足以证明这是其职业能力，故降权）。
    """
    best_score: int | None = None
    best_idx = -1
    best_seg = ""
    for term in terms:
        for m in _find_all(text, term)[:5]:
            seg = sentence_around(text, m.start())
            if not seg:
                continue
            score = 0
            if any(h in seg for h in _BULLET_HINTS):
                score += 2
            sec = section_of(text, m.start())
            if "技能" in sec:
                score += 2
            elif sec:
                score += 1
            if any(k in seg for k in _EDU_MARKS):
                score -= 1
            if (best_score is None or score > best_score
                    or (score == best_score and m.start() < best_idx)):
                best_score, best_idx, best_seg = score, m.start(), seg
    if best_score is None:
        return "", -1
    return best_seg, best_idx


def scan_text(text: str, max_hits: int = 60) -> list[dict]:
    """规则通道：直接在原文里扫本体词，输出带证据的技能。

    即使模型不可用（或模型漏抽），这一通道仍能给出可靠、可溯源的技能集。
    """
    if not text:
        return []
    seen: dict[str, dict] = {}
    for term, canon in _iter_terms():
        if canon in seen:
            continue
        ev, pos = best_evidence(text, [term])
        if not ev:
            continue
        seen[canon] = {
            "canonical": canon,
            "category": (_META or {}).get(canon, {}).get("category", "其他"),
            "matched": term,
            "evidence": ev,
            "level": infer_level(text, pos) if pos >= 0 else None,
            "source": "rule",
        }
        if len(seen) >= max_hits:
            break
    return list(seen.values())


def _norm_ws(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def verify_claim(text: str, claim: str | None) -> bool:
    """核对模型声称的原文片段是否真的出现在简历里（反幻觉硬校验）。"""
    if not claim or not claim.strip():
        return False
    if claim.strip() in (text or ""):
        return True
    return _norm_ws(claim) in _norm_ws(text)


def normalize_skills(raw_skills: list[str] | None, text: str,
                     llm_evidence: dict[str, str] | None = None,
                     source: str = "llm") -> list[dict]:
    """把任意来源的技能名归一到本体，并补齐证据。

    归一与证据的判定顺序：

    1. 在原文里直接搜到该技能（或其别名）→ 证据取所在整句，`verified=1`；
    2. 搜不到，但模型给了片段且该片段**确实存在于原文** → 采用该片段，`verified=1`；
    3. 都不成立 → `verified=0`（信息仍保留供人工核对，但**不计入岗位技能命中**）。

    `source` 标记词表来源（`llm` / `jd` / `rule`），落库后可区分"这个技能是模型抽的、
    还是岗位 JD 里写了、原文也有的"。**不在本体里的词不会被丢掉**：
    `canonical_of(raw) or raw` 保留原词，因此岗位 JD 写的 `Java`、`Spring Boot`
    这类本体外技能同样能带上原文证据参与命中（见 `extract._jd_terms`）。
    """
    out: dict[str, dict] = {}
    source_text = text or ""
    claims = {k.strip().lower(): v for k, v in (llm_evidence or {}).items()}

    for name in (raw_skills or []):
        raw = (name or "").strip()
        if not raw:
            continue
        canon = canonical_of(raw) or raw
        if canon in out:
            continue
        info = (_META or {}).get(canon, {})
        evidence, matched, verified, pos = "", None, False, -1
        ev, pos = best_evidence(source_text, [canon] + list(info.get("aliases", [])) + [raw])
        if ev:
            evidence, matched, verified = ev, raw, True
        if not verified:
            claim = claims.get(raw.lower()) or claims.get(canon.lower())
            if verify_claim(source_text, claim):
                evidence, matched, verified = claim.strip(), raw, True
        out[canon] = {
            "canonical": canon,
            "category": info.get("category", "其他"),
            "matched": matched,
            "evidence": evidence,
            "level": infer_level(source_text, pos) if pos >= 0 else None,
            "verified": verified,
            "source": source,
        }

    # 规则通道补漏：模型漏抽的技能从原文里找回
    for item in scan_text(source_text):
        if item["canonical"] not in out:
            item["verified"] = True
            out[item["canonical"]] = item
    return list(out.values())


def canon_names(items: list[dict], verified_only: bool = True) -> list[str]:
    return [i["canonical"] for i in items if (i.get("verified") or not verified_only)]


def resolve_query_terms(words: list[str]) -> list[str]:
    """把用户/模型给的自由词（如"真空熔铸"、"XRD"）转成本体 canonical 名。"""
    resolved: list[str] = []
    for w in words or []:
        w = (w or "").strip()
        if not w:
            continue
        canon = canonical_of(w)
        if canon:
            resolved.append(canon)
            continue
        # 部分匹配：在本体名/别名里找包含关系
        for canon_name in (_META or {}):
            if w in canon_name or any(w in a for a in (_META or {})[canon_name].get("aliases", [])):
                resolved.append(canon_name)
                break
        else:
            resolved.append(w)
    return sorted(set(resolved))
