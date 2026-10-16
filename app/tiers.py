"""档位（分级）定义：**档位字母与文案的唯一真源**。

为什么单独抽一个模块
--------------------
档位字母原来硬编码在 **9 个文件**里：`pipeline/tier.py`（判定）、`pipeline/analyze.py`（**给模型的提示词**里
写着 "A/B/C/D（必填）"）、`agent/tools.py`（工具参数说明与校验）、`db.py`（标签表、排序、统计）、
`feedback.py`（口径偏差）、`ingest.py`（自动分析回写）、`ui.py`（标签与配色、下拉）、
以及测试里的期望值。人事处把 4 档并成 3 档时，**改漏任何一处都会分叉**，而症状各不相同：
提示词没改 → 模型还在吐 `D`；工具校验没改 → 助手改不了档；UI 没改 → 徽章显示空白。

这与 v1.29 阶段模型遇到的是同一个问题，做法也一样：**配置驱动 + 一个读取模块**。
配置在 `config/tiers.json`，改档位只改它。

⚠️ `_FALLBACK` 与 `config/tiers.json` 是两份（一份兜底、一份可编辑），
`assert_consistent()` 比对二者，自检里有判据钉住 —— 新增档位必须同时改这两处。
"""
from __future__ import annotations

import json
import os
import re
from .paths import BASE

CONFIG_PATH = os.path.join(BASE, "config", "tiers.json")

#: 配置缺失/损坏时的兜底。**必须与 config/tiers.json 一致**（有判据校验）。
_FALLBACK: dict = {
    "order": ["A", "B", "C"],
    "tiers": {
        "A": {"label": "推荐面试", "desc": "必需条件基本满足，明显匹配"},
        "B": {"label": "储备", "desc": "条件偏弱但有潜力或相关背景"},
        "C": {"label": "暂不匹配", "desc": "学历低于岗位门槛（唯一判据）；简历仍保留在人才库"},
    },
    "hard_shortfall_tier": "C",
    "review_flag": "待人工判读",
}

_cached: dict | None = None


def load_cfg(force: bool = False) -> dict:
    """读档位配置（进程内缓存一次；改完配置需重启工作台，与其它字典一致）。"""
    global _cached
    if _cached is not None and not force:
        return _cached
    cfg = dict(_FALLBACK)
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, encoding="utf-8") as fh:
                user = json.load(fh)
            if isinstance(user, dict):
                cfg.update({k: v for k, v in user.items() if not k.startswith("_")})
    except Exception as exc:                                    # noqa: BLE001
        import sys
        print(f"[tiers] config/tiers.json 读取失败，已回落内置默认档位："
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        cfg = dict(_FALLBACK)
    if not cfg.get("order") or not isinstance(cfg.get("tiers"), dict):
        cfg = dict(_FALLBACK)
    _cached = cfg
    return _cached


# ------------------------------------------------------------------ 基本查询

def all_tiers() -> list[str]:
    """全部档位字母（按配置顺序，从高到低）。"""
    return list(load_cfg()["order"])


def labels() -> dict[str, str]:
    """{字母: 中文名}。给界面与接口用 —— 原来 db.py 与 ui.py 各抄一份。"""
    t = load_cfg().get("tiers") or {}
    return {k: (v or {}).get("label", k) for k, v in t.items()}


def label(tier: str) -> str:
    return labels().get(str(tier or "").strip(), "")


def desc(tier: str) -> str:
    t = load_cfg().get("tiers") or {}
    return ((t.get(str(tier or "").strip()) or {}).get("desc") or "")


def is_valid_tier(value) -> bool:
    return str(value or "").strip() in all_tiers()


def rank(tier: str) -> int:
    """档位序号（A=0 最好）。用于排序。认不出的返回 len(all_tiers())（排到最后）。"""
    v = str(tier or "").strip()
    return all_tiers().index(v) if v in all_tiers() else len(all_tiers())


def hard_shortfall_tier() -> str:
    """「学历低于岗位要求」时强制判的档位。

    ⚠️ 原来 `tier.py` 里写死 `"D"`，而配置里那个 `hard_shortfall_tier` 字段**从没被读过**
    （v1.12 删打分制后留下的死配置）。现在真的读它 —— 否则合并档位时又要改代码。
    """
    v = str(load_cfg().get("hard_shortfall_tier") or "").strip()
    # 配置写了个不存在的档位就退回最后一档（最差档），不要让判定落空
    return v if v in all_tiers() else all_tiers()[-1]


def mid_tier() -> str:
    """「信息缺失、待人工核对」时给的中性档（取配置顺序的中间那档）。

    用途：学历没识别出来时不能判最差档（那是**否定结论**，而信息缺失只是"没读到"），
    也不该给最好档。原来写死 `"C"`（当时四档的中间档）—— 档位体系一改就错了位。
    现在按配置算：A/B/C 三档时是 B（储备）。
    """
    t = all_tiers()
    return t[len(t) // 2] if t else ""


def review_flag() -> str:
    """解析失败 / 无法判断时的标记（不进档位体系，单独一列）。"""
    return str(load_cfg().get("review_flag") or "待人工判读")


def enum_str(sep: str = "/") -> str:
    """给提示词与工具说明用的枚举串，如 `A/B/C`。

    原来提示词里把 "A/B/C/D" 写死 —— 档位一改，模型就被要求输出一个不存在的档位，
    而模型多半照做（它不知道那是废弃值），库里就混进脏档位。
    """
    return sep.join(all_tiers())


def tier_regex() -> str:
    """档位字母的正则（`re.escape` 过，避免将来用非字母档位时变成任意匹配）。"""
    letters = sorted(all_tiers(), key=len, reverse=True)
    return "|".join(re.escape(x) for x in letters)


# ------------------------------------------------------------------ 一致性

def assert_consistent() -> list[str]:
    """比对 `_FALLBACK` 与 `config/tiers.json`，返回不一致项（空 = 一致）。"""
    try:
        with open(CONFIG_PATH, encoding="utf-8") as fh:
            user = json.load(fh)
    except Exception as exc:                                    # noqa: BLE001
        return [f"读不到 config/tiers.json：{type(exc).__name__}: {exc}"]
    diffs: list[str] = []
    for k in ("order", "tiers", "hard_shortfall_tier", "review_flag"):
        if user.get(k) != _FALLBACK.get(k):
            diffs.append(k)
    return diffs
