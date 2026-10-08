"""专业归一 / 业务方向：把模型接进"专业方向"判定，但只做**归一与提炼**，不做决定。

分层口径（与项目六条硬约束一致）：
- **打分与档位仍由 `tier.grade` 的规则算**——可复现、可审计，HR 问"凭什么"时有稳定答案；
- 模型只负责一件规则做不好的事：把简历里千奇百怪的专业写法
  （"材化"、"材料成型"、"高分子材料"）**归一到学科目录的真实条目**；
- **模型返回的条目必须真实存在于目录**（按名字/别名归一后一致，集合成员校验），
  否则丢弃回退规则——模型说"他属于物理学"没用，目录里没有就是没有（不猜）；
- 「业务方向」（这个人实际干的事）由模型从简历提炼，但**只作展示字段**，
  不参与打分与档位（与性别同口径：展示可以，决策不行）。

为什么模型只在"规则归不出来"时才出场：规则（精确/别名/包含/相似度）已经能覆盖
大多数写法，而且是零成本、零延迟；模型那次调用留给真正的长尾。
同一写法的结果会缓存——10 份简历里 5 个人都写"材料学"时只调一次模型。
"""
from __future__ import annotations

import sys

from ..agent import llm
from . import majors

_SYSTEM = (
    "你是高校学科目录专家。给你一份**学科目录清单**（一级学科及其门类），"
    "以及一个待归一的专业写法（可能来自简历的简写/俗称/自造写法）。"
    "请从清单里选出最匹配的**一个**条目。"
    "只准从清单里选，**绝对不准自造条目**；确实没有匹配的就输出空字符串。"
    "严格输出 JSON："
    '{"canonical":"目录条目原名","category":"其所属门类","reason":"一句话依据(30字内)"}'
)

_CACHE: dict[str, dict | None] = {}


def _catalog_lines() -> list[str]:
    data = majors.catalog()
    out: list[str] = []
    for item in data.get("majors", []):
        name = (item.get("name") or "").strip()
        cat = item.get("category") or ""
        if name:
            out.append(f"- {name}（{cat}）")
    return out


def classify_major(major_text: str, context: str = "") -> dict | None:
    """把专业写法归一到学科目录条目。

    返回 `{"canonical","category","reason","via"}`；**失败/目录外 → None**，
    调用方回退规则结果。规则（majors.resolve）能精确归一的直接走规则、不花调用。
    """
    text = (major_text or "").strip()
    if not text or len(text) > 40:
        return None
    if text in _CACHE:
        return _CACHE[text]

    # 规则先试：能精确归一就不需要模型（省一次调用，也避免不确定性）
    try:
        hit = majors.resolve(text)
    except Exception:                                       # noqa: BLE001
        hit = None
    if hit:
        _CACHE[text] = {"canonical": hit["canonical"],
                        "category": hit.get("category") or "",
                        "reason": "学科目录精确/别名命中", "via": "catalog"}
        return _CACHE[text]

    user = ("【学科目录】\n" + "\n".join(_catalog_lines())
            + f"\n\n【专业写法】{text}\n"
            + (f"【简历线索】\n{context[:800]}" if context else "")
            + "\n请从上面的学科目录中选出最匹配的一个条目，严格输出 JSON："
              '{"canonical":"目录条目原名","category":"其所属门类",'
              '"reason":"一句话依据(30字内)"}。'
              "只准从目录里选；实在没有匹配的，输出 {\"canonical\":\"\"}。")
    try:
        r = llm.chat_json(_SYSTEM, user)
    except Exception as exc:                                # noqa: BLE001
        print(f"[major_llm] 模型归一失败，回退规则：{type(exc).__name__}: {exc}",
              file=sys.stderr)
        _CACHE[text] = None
        return None

    canon = str((r or {}).get("canonical") or "").strip()
    if not canon:
        _CACHE[text] = None
        return None
    # **目录成员校验**：模型给的条目必须是目录里**精确存在的名字**。
    # 不能用 majors.resolve()——它带相似度兜底，模型编造的条目会被"容忍"进来，
    # 校验就成了摆设。用精确匹配：canonical 必须与目录里某个 major.name 完全一致。
    names = {m.get("name") for m in majors.catalog().get("majors", [])}
    if canon not in names:
        _CACHE[text] = None
        return None
    cat = next((m.get("category") or "" for m in majors.catalog().get("majors", [])
                if m.get("name") == canon), "")
    _CACHE[text] = {"canonical": canon,
                    "category": cat,
                    "reason": str((r or {}).get("reason") or "")[:60],
                    "via": "llm"}
    return _CACHE[text]


_DIRECTION_SYSTEM = (
    "你是招聘业务分析助手。根据候选人简历（技能、经历、项目），"
    "提炼这个人**实际做的业务方向**——用行业里的大白话，2-8 个字一个，最多 2 个。"
    "例如：材料工艺 / 检测分析 / 后端开发 / 质量体系。"
    "只依据简历里真实写过的内容，不要编造、不要拔高。"
    '严格输出 JSON：{"directions":["方向1","方向2"]}。'
)

_DIR_CACHE: dict[str, str | None] = {}


def business_direction(cand: dict) -> str | None:
    """从简历提炼「业务方向」（这个人实际干的事），**只作展示，不参与打分**。"""
    skills = [s.get("name") for s in (cand.get("skill_detail") or cand.get("skills") or [])
              if isinstance(s, dict) and (s.get("name") or "").strip()]
    raw = (cand.get("raw_text") or "")[:1500]
    key = "|".join(filter(None, [",".join(filter(None, skills)), raw[:200]]))
    if key in _DIR_CACHE:
        return _DIR_CACHE[key]
    user = (f"技能:{'、'.join(skills) or '未识别'}\n"
            f"专业:{cand.get('major') or '未识别'}\n"
            f"简历摘录:\n{raw}")
    try:
        r = llm.chat_json(_DIRECTION_SYSTEM, user)
    except Exception as exc:                                # noqa: BLE001
        print(f"[major_llm] 业务方向提炼失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        _DIR_CACHE[key] = None
        return None
    dirs = [str(x).strip() for x in ((r or {}).get("directions") or []) if str(x).strip()]
    dirs = [d[:12] for d in dirs][:2]
    out = "、".join(dirs) if dirs else None
    _DIR_CACHE[key] = out
    return out
