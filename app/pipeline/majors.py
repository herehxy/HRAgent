"""通用学科目录：把「专业需求」变成一等维度，且不依赖技能本体收录。

设计意图（为什么单独一个模块）：

1. **技能是开放集合，学科是封闭目录。** 技能本体（`config/ontology.json`）无论怎么扩，
   遇到新领域都会漏；而学科目录（学科门类 + 一级学科）是官方收口、有限且稳定的。
   把"方向判定"的底座放在学科目录上，**一次建成即长期够用**——新增行业只加数据。

2. **"未收录"不等于"判错"。** 现行 `guess_major_family` 一旦推不出大类就返回空串，
   下游拿空串做交集，结论退化成"因为词表没有所以无法判定"。
   这里改成两级：先查目录（精确名 → 别名 → 包含关系），查不到再退回**字符相似度**，
   仍然判不出就**如实返回空**并让调用方标注"未识别"——不猜、但也不装死。

3. **字符相似度兜底**（`similarity`）不依赖 embedding 服务，纯本地可复现。
   向量服务在线时可以叠加语义通道，但**兜底不能依赖它**——本次实测本机向量服务
   是降级状态（`local-hash-v1`），若把兜底压在向量上，等于没有兜底。
"""
from __future__ import annotations

import json
import os
import re

DEFAULT_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "config", "majors.json",
)

# 相似度兜底：低于此值不算命中。0.55 是"共享至少一半字符片段"的量级，
# 再低会把"用友"和"金蝶"这种同域不同物误判成同一项。
SIM_THRESHOLD = 0.55

_CATALOG: dict | None = None
_INDEX: dict[str, dict] | None = None     # 归一化后的名字/别名 → 条目
_NAMES: list[tuple[str, dict]] | None = None  # (名字, 条目) 长词优先
_TERMS: list[tuple[str, dict]] | None = None  # (名字 + 别名, 条目) 长词优先，用于包含关系

_PUNCT = re.compile(r"[\s\u3000()（）\[\]【】<>《》,，、。;；:：/\\|\-—_·]+")


def _norm(text: str | None) -> str:
    return _PUNCT.sub("", (text or "").strip().lower())


def _half(text: str) -> str:
    """全角 → 半角（字母数字），避免"Ｍａｔｌａｂ"这类写法漏匹配。"""
    out = []
    for ch in text:
        code = ord(ch)
        if code == 0x3000:
            out.append(" ")
        elif 0xFF01 <= code <= 0xFF5E:
            out.append(chr(code - 0xFEE0))
        else:
            out.append(ch)
    return "".join(out)


def load(path: str | None = None, reload: bool = False) -> dict:
    global _CATALOG, _INDEX, _NAMES, _TERMS
    if _CATALOG is not None and not reload and path is None:
        return _CATALOG
    target = path or DEFAULT_PATH
    with open(target, encoding="utf-8") as fh:
        data = json.load(fh)
    index: dict[str, dict] = {}
    names: list[tuple[str, dict]] = []
    for item in data.get("majors", []):
        name = (item.get("name") or "").strip()
        if not name:
            continue
        entry = {"canonical": name,
                 "category": item.get("category") or "",
                 "aliases": list(item.get("aliases") or [])}
        names.append((_norm(_half(name)), entry))
        index.setdefault(_norm(_half(name)), entry)
    # 别名只在"名字没占用"时登记：`软件工程` 既是独立一级学科、又是 `计算机科学与技术`
    # 的别名，名字优先，否则独立学科会被别名吞掉。
    for item in data.get("majors", []):
        name = (item.get("name") or "").strip()
        if not name:
            continue
        entry = {"canonical": name,
                 "category": item.get("category") or "",
                 "aliases": list(item.get("aliases") or [])}
        for a in (item.get("aliases") or []):
            key = _norm(_half(a))
            if key:
                index.setdefault(key, entry)
    names.sort(key=lambda t: -len(t[0]))
    # 包含关系用「名字 + 别名」一起看：岗位写"材料加工"而目录里是"材料加工工程"，
    # 只看名字会漏（实测踩到）。
    terms: list[tuple[str, dict]] = list(names)
    for item in data.get("majors", []):
        name = (item.get("name") or "").strip()
        if not name:
            continue
        entry = index.get(_norm(_half(name))) or {"canonical": name,
                                                  "category": item.get("category") or "",
                                                  "aliases": list(item.get("aliases") or [])}
        for a in (item.get("aliases") or []):
            k = _norm(_half(a))
            if k:
                terms.append((k, entry))
    terms.sort(key=lambda t: -len(t[0]))
    _CATALOG, _INDEX, _NAMES, _TERMS = data, index, names, terms
    return data


def catalog() -> dict:
    return _CATALOG if _CATALOG is not None else load()


def describe() -> dict:
    data = catalog()
    counts: dict[str, int] = {}
    for item in data.get("majors", []):
        c = item.get("category") or "未归类"
        counts[c] = counts.get(c, 0) + 1
    return {"version": data.get("version"),
            "categories": list(data.get("categories") or []),
            "major_count": len(data.get("majors") or []),
            "matchable_count": len(_INDEX or {}),
            "by_category": counts}


def all_majors() -> list[dict]:
    return [{"name": m.get("name"), "category": m.get("category"),
             "aliases": list(m.get("aliases") or [])}
            for m in catalog().get("majors", [])]


def search(q: str | None, limit: int = 40) -> list[dict]:
    """按关键词列学科（给界面做下拉/提示用）。"""
    load()
    key = _norm(_half(q or ""))
    out = []
    for m in catalog().get("majors", []):
        if not key:
            out.append({"name": m.get("name"), "category": m.get("category")})
            continue
        hay = _norm(_half(m.get("name") or "")) + "|" + \
              "|".join(_norm(_half(a)) for a in (m.get("aliases") or []))
        if key in hay:
            out.append({"name": m.get("name"), "category": m.get("category")})
        if len(out) >= limit:
            break
    return out[:limit]


def resolve(text: str | None) -> dict | None:
    """学科文本 → 目录条目。判不出返回 None（**不硬猜**）。

    三级：精确名/别名 → 文本里包含某个学科名（长词优先）→ 反向包含（关键词是
    学科名的片段，且长度 >= 4，避免"材料"这种短词把十几个学科全捞进来）。
    """
    load()
    raw = _half((text or "").strip())
    if not raw:
        return None
    key = _norm(raw)
    hit = (_INDEX or {}).get(key)
    if hit:
        return {**hit, "matched": text.strip(), "how": "exact"}
    # 文本里含学科名/别名（"材料科学与工程（金属材料方向）"→ 材料科学与工程）
    for name, entry in (_TERMS or []):
        if len(name) >= 2 and name in key:
            return {**entry, "matched": name, "how": "contains"}
    # 关键词是学科名/别名的片段：只在足够长时才认，否则会把"材料"扩成整个材料类
    if len(key) >= 4:
        best = None
        for name, entry in (_TERMS or []):
            if key in name and (best is None or len(name) < len(best[0])):
                best = (name, entry)
        if best:
            return {**best[1], "matched": best[0], "how": "partial"}
    return None


def category_of(text: str | None) -> str:
    """学科文本 → 学科门类（工学/理学/管理学…）。判不出返回空串。"""
    hit = resolve(text)
    return hit.get("category", "") if hit else ""


def similarity(a: str | None, b: str | None) -> float:
    """字符级相似度（0~1）：等值 > 包含 > 二元组 Dice。不依赖外部服务。"""
    x, y = _norm(_half(a or "")), _norm(_half(b or ""))
    if not x or not y:
        return 0.0
    if x == y:
        return 1.0
    if x in y or y in x:
        short, long_ = (x, y) if len(x) <= len(y) else (y, x)
        return round(0.6 + 0.4 * (len(short) / len(long_)), 3)
    if len(x) < 2 or len(y) < 2:
        return 0.0
    gx = {x[i:i + 2] for i in range(len(x) - 1)}
    gy = {y[i:i + 2] for i in range(len(y) - 1)}
    if not gx or not gy:
        return 0.0
    inter = len(gx & gy)
    return round(2 * inter / (len(gx) + len(gy)), 3)


def best_match(term: str | None, others: list[str]) -> tuple[str | None, float]:
    """在 `others` 里找与 `term` 最像的一个（兜底通道用）。"""
    best, score = None, 0.0
    for o in others or []:
        s = similarity(term, o)
        if s > score:
            best, score = o, s
    return best, round(score, 3)


# 学科名里的"公共外壳"。一级学科名大量共享这些词，直接算字符相似度会误判：
# 实测 `计算机科学与技术` 与 `仪器科学与技术` 的二元组相似度是 0.615
# ——它们唯一相同的就是尾巴「科学与技术」，于是"计算机专业"被判成"在检验检测岗的专业清单内"。
# 所以学科之间的相似度必须先剥壳、再比"专业内核"。
_SHELL = ("科学与技术", "科学与工程", "科学技术", "工程与技术", "及", "与",
          "方向", "专业", "类", "技术", "科学", "工程", "学")


def _core(text: str | None) -> str:
    x = _norm(_half(text or ""))
    for token in _SHELL:
        x = x.replace(token, "")
    return x


def major_similarity(a: str | None, b: str | None) -> float:
    """学科名之间的相似度：**先剥掉公共外壳再比内核**。

    剥壳后 `计算机`↔`仪器` 相似度为 0（正确：不相干）；
    `材料`↔`材料物理化学` 仍能命中（正确：同族）。
    """
    ca, cb = _core(a), _core(b)
    if not ca or not cb:
        return 0.0
    return similarity(ca, cb)


def in_list(cand_major: str | None,
            required: list[str] | None) -> dict:
    """候选人的专业是否落在岗位的「专业需求」清单内。

    判定顺序：① 双方各自归一到目录条目，条目同名即命中（"材料学" ≡ "材料科学与工程"）；
    ② 退到相似度兜底（>= 阈值算命中）；③ 都判不出 → `in_list=None`，
    含义是"无法判定"，**不等于不满足**（这是本模块的核心口径）。"""
    req = [r for r in (required or []) if r and str(r).strip()]
    cand = (cand_major or "").strip()
    out = {"required": req, "candidate": cand or None,
           "candidate_canonical": None, "candidate_category": None,
           "in_list": None, "hit_term": None, "how": None, "note": ""}
    if not req or not cand:
        out["note"] = ("岗位未设专业需求，不参与专业维度判定" if not req
                       else "候选人专业未识别，无法与专业需求比对")
        return out
    cres = resolve(cand)
    if cres:
        out["candidate_canonical"] = cres["canonical"]
        out["candidate_category"] = cres["category"]
    for r in req:
        rres = resolve(r)
        rname = rres["canonical"] if rres else r
        if cres and rres and cres["canonical"] == rres["canonical"]:
            out.update({"in_list": True, "hit_term": r, "how": "catalog",
                        "note": f"专业「{cres['canonical']}」（{cres['category']}）在岗位专业需求清单内"
                                f"（以「{r}」命中）"})
            return out
    for r in req:
        s = major_similarity(cand, r)
        if s >= SIM_THRESHOLD:
            out.update({"in_list": True, "hit_term": r, "how": "similar",
                        "note": f"专业「{cand}」与需求项「{r}」内核相近（相似度 {s}），按命中计"})
            return out
    if cres:
        out.update({"in_list": False, "how": "catalog",
                    "note": f"专业「{cres['canonical']}」（{cres['category']}）不在岗位专业需求清单内；"
                            f"清单归口门类为 {'、'.join(sorted({(resolve(r) or {}).get('category') or '未识别' for r in req}))}。"
                            f"**专业不对口只作提示，不参与档位淘汰**"})
    else:
        out["note"] = (f"专业「{cand}」未在通用学科目录中识别，无法判定是否在需求清单内"
                       f"——**未识别不等于不满足**")
    return out


def family_label(cand_major: str | None) -> str:
    """给界面用的短标签：「软件工程（工学）」。未识别返回空串。"""
    hit = resolve(cand_major)
    if not hit:
        return ""
    cat = hit.get("category") or ""
    return f"{hit['canonical']}（{cat}）" if cat else hit["canonical"]
