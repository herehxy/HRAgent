"""院校层次识别：985 / 211 标签与筛选（v1.7.3）。

名单在 `config/universities.json`：985 一张表、211 一张表（只收非 985 的高校，
因为 985 学校全部同时是 211），外加常见简称别名。

匹配规则刻意保守（宁缺勿错，标错比不标严重）：
1. 归一：去首尾与内部空白，全角括号转半角，全角括号内若有"校区/分校/（华东）"
   这类后缀，剥掉后再试一次——「哈尔滨工业大学（深圳）」算哈工大；
2. 精确匹配全名或别名；**不做包含匹配**——「南京大学金陵学院」这类独立学院
   全名对不上、剥后缀也对不上，不会被误标成 985/211；
3. 研究院、企业大学等非高校名称天然全部对不上，返回 None。

查不到名单文件时功能整体退化为"无人有标签"（返回 None），不报错——
这个字段是加分展示项，不能因为它拖垮人才库列表。
"""
from __future__ import annotations

import json
import os
from functools import lru_cache

_CFG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "config", "universities.json")


def _norm(name: str) -> str:
    s = str(name or "").replace("（", "(").replace("）", ")")
    return "".join(s.split())


def _base(name: str) -> str:
    """剥掉尾部的 (校区/所在地) 后缀：「哈尔滨工业大学(深圳)」→「哈尔滨工业大学」。"""
    s = _norm(name)
    if "(" in s and s.endswith(")"):
        s = s[: s.index("(")]
    return s


@lru_cache(maxsize=1)
def _tiers() -> dict[str, str]:
    """归一名 → 层次（'985' / '211'）。加载失败返回空表 = 功能整体退化。"""
    table: dict[str, str] = {}
    try:
        with open(_CFG, encoding="utf-8") as f:
            cfg = json.load(f)
        for name in cfg.get("985") or []:
            table[_norm(name)] = "985"
            table[_base(name)] = "985"
        for name in cfg.get("211") or []:
            table[_norm(name)] = "211"
            table[_base(name)] = "211"
        for alias, canonical in (cfg.get("aliases") or {}).items():
            tier = table.get(_norm(canonical))
            if tier:
                table[_norm(alias)] = tier
    except Exception:  # noqa: BLE001 — 名单缺失/损坏只降级不报错
        return {}
    return table


def uni_tier(school: str | None) -> str | None:
    """院校名 → '985' / '211' / None。None 含"非 985/211 高校"与"非高校单位"。"""
    if not school or not str(school).strip():
        return None
    table = _tiers()
    if not table:
        return None
    s = _norm(school)
    t = table.get(s)
    if t:
        return t
    return table.get(_base(s))
