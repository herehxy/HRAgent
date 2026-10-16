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

from .paths import CONFIG_DIR as _C          # 根目录唯一来源（认 TP_HOME）
_CFG = os.path.join(_C, "universities.json")


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
    """归一名 → 层次（'985' / '211' / '强势'）。加载失败返回空表 = 功能整体退化。"""
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
        # v1.27：`strong`（既非 985 也非 211、但学科实力公认很强的院校）。
        # ⚠️ 顺序上**必须在 985/211 之后**：一所学校只能有一个标签，
        # 若它同时在 985 名单里（如中科大本身是 985），就该显示 985 而不是
        # "强势院校" —— HR 按 985 筛时必须还能筛到他，反之亦然。
        # `strong` 段的语义是"**名单里没有的**补充"，不是优先级更高的标签。
        for name in cfg.get("strong") or []:
            k = _norm(name)
            if k not in table:                    # 不覆盖已有的 985/211
                table[k] = "强势"
                table[_base(name)] = "强势"
        for alias, canonical in (cfg.get("strong_aliases") or {}).items():
            tier = table.get(_norm(canonical))
            if tier == "强势":
                table[_norm(alias)] = "强势"
        # 原始名单另存一份：界面生成「特定院校」下拉要用**配置里的原名**
        # （归一后的 key 已被剥掉校区后缀，显示出来 HR 对不上号）。
        _STRONG_LIST[:] = [str(n) for n in (cfg.get("strong") or [])]
    except Exception:  # noqa: BLE001 — 名单缺失/损坏只降级不报错
        return {}
    return table


#: strong 段的原始名单（`strong_schools()` 返回它）。模块级可变列表是
#: lru_cache 下唯一能跨调用共享的形态 —— 每次重读 json 没必要，
#: 而 `_tiers` 已经缓存了整套表，用普通 dict 存不到 cache 内部去。
#: 声明在 `_tiers` **之前**：函数体只在被调用时执行，所以位置其实无所谓，
#: 但读代码的人会先看到列表再看到给它赋值的函数，顺序上更直观。
_STRONG_LIST: list[str] = []


def uni_tier(school: str | None) -> str | None:
    """院校名 → '985' / '211' / '强势' / None。

    None 含"非 985/211/强势 高校"与"非高校单位"。

    ⚠️ '强势'（v1.27）是**第三种并列标签**，不是 985/211 的子类：
    它表示"名单之外的强校"。界面配色与文案都另给，否则 HR 会误以为它是 211。
    """
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


@lru_cache(maxsize=1)
def _overseas() -> frozenset[str]:
    """境外院校名单的归一名集合（加载失败返回空集 = 功能降级为"都不算海外"）。

    ⚠️ **与 `_tiers()` 完全独立**：境外高校既不是 985 也不是 211，
    不能塞进同一张表（塞进去的话 HR 勾「985」时海外校不会命中、
    勾「海外」时国内校不会命中，而 facets 人数还会把两类混在一个总数里）。
    这正是"海外必须独立成维度"的原因。
    """
    try:
        with open(_CFG, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:  # noqa: BLE001 — 名单缺失只降级，不影响人才库
        return frozenset()
    out: set[str] = set()
    for name in cfg.get("overseas") or []:
        # ⚠️ **不做 `_base()` 括号剥离** —— 那是给 985/211 用的（"哈尔滨工业大学(深圳)"
        # 算哈工大，同一个学校的不同校区）。对境外名单它会**制造撞名**：
        # 「东北大学（日本）」剥成「东北大学」后，我国 985 的东北大学被误判为境外校。
        # 实测踩到。境外校名一律按**完整写法精确匹配**，宁可漏认也不误判。
        out.add(_norm(name))
    for alias, canonical in (cfg.get("overseas_aliases") or {}).items():
        out.add(_norm(alias))
    return frozenset(out)


def is_overseas(school: str | None) -> bool | None:
    """院校名是否是**境外院校**（见 config/universities.json 的 `overseas` 段）。

    返回三态，**不是** bool：
      - `True`  → 在名单里（国外高校，或中国香港、中国澳门、中国台湾地区的高校）
      - `False` → 不在名单里（按名单口径算"非海外"）
      - `None`  → **未识别**（学校为空 / 名单文件读不到）

    ⚠️ 为什么必须把 `None` 与 `False` 分开：Hr 筛选里「未识别」需要**单列一档**。
    混进 `False` 的话，HR 会把"简历里没识别出院校"读成"这人的学校不是境外校" ——
    这两件事的处理方式完全相反（前者要人工补录，后者可以直接排除）。

    ⚠️ 名单是**枚举式**的：一所名单外的境外高校会被判成 `False`（说不准）。
    系统不猜境外校名（"XX University" 这种模式匹配会误伤国内合作办学与培训机构）。
    所以界面上要写清"名单外算非海外"，并由人事处维护名单。
    """
    if not school or not str(school).strip():
        return None
    allowed = _overseas()
    if not allowed:
        return None                     # 名单读不到 = 未识别，不假装"非海外"
    # 与 `_overseas()` 同一口径：**不做括号剥离**（见那里的说明，会与国内校撞名）
    return _norm(school) in allowed


def overseas_schools() -> list[str]:
    """配置里的境外院校原名（供界面提示"名单里有哪些"）。"""
    try:
        with open(_CFG, encoding="utf-8") as f:
            cfg = json.load(f)
        return [str(n) for n in (cfg.get("overseas") or [])]
    except Exception:  # noqa: BLE001
        return []


def strong_schools() -> list[str]:
    """配置里的强势院校名单（供界面生成下拉）。取不到返回空表。

    先触发 `_tiers()` 加载，再返回原始名单 —— 不能直接读模块级列表，
    那是"还没加载过时的空表"，未调用 `uni_tier` 前读它必然返回空。
    """
    _tiers()
    return list(_STRONG_LIST)


def is_strong(school: str | None) -> bool:
    """院校名是否落在 `strong` 名单里（用于"特定院校"筛选的快速判断）。"""
    return uni_tier(school) == "强势"
