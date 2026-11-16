"""合规屏蔽层：在简历进入任何模型之前，先剔除受法律保护的敏感属性。

依据《个人信息保护法》最小必要原则与反就业歧视要求，以下信息**不得**参与岗位匹配：

- 民族 / 族别
- 婚姻状况（已婚、未婚、离异）
- 生育状况（已育、子女情况、孕期）
- 宗教信仰
- 健康状况 / 病史 / 残疾
- 身份证号（保留必要性最低，且属高敏感）
- 家庭住址 / 户籍
- 政治面貌
- 身高、体重（除岗位有法定体能要求）
- 照片

处理方式是**在文本层面替换掉具体取值**（保留"此项已被合规屏蔽"的可读提示），
这样既不破坏简历可读性，也让模型完全看不到这些字段；
`detect()` 会回报每份简历命中了哪些类别，落入审计，便于向信息安全评审举证。

注意：这是"屏蔽"而不是"删除原文"——原件附件仍完整留档（有访问控制与审计），
符合"不丢件"与"可溯源"两条约束。
"""
from __future__ import annotations

import re

PLACEHOLDER = "【已屏蔽】"

# 每类：标签词 + 取值模式。先在标签后取值，再单独命中裸关键词。
RULES: dict[str, list[str]] = {
    "民族": [r"民族[:：]?\s*[\u4e00-\u9fa5]{1,8}", r"族别[:：]?\s*[\u4e00-\u9fa5]{1,8}",
             r"[\u4e00-\u9fa5]{1,4}族(?!属)"],
    "婚姻状况": [r"婚姻(?:状况|状态)?[:：]?\s*[\u4e00-\u9fa5]{1,6}",
                 r"已婚|未婚|离异|丧偶"],
    "生育状况": [r"(?:生育|子女)(?:状况|情况)?[:：]?\s*[\u4e00-\u9fa5]{1,10}",
                 r"已育|未育|已孕|怀孕|孕期|育有[一二三四五六七八九十\d]+[子女]"],
    "宗教信仰": [r"(?:宗教|信仰)[:：]?\s*[\u4e00-\u9fa5]{1,8}"],
    "健康状况": [r"(?:健康(?:状况)?|病史|体检)[:：]?\s*[\u4e00-\u9fa5，,。;；\s]{1,20}",
                 r"乙肝|残疾|色盲|色弱|精神病|慢性病"],
    "身份证号": [r"\b\d{17}[\dXx]\b", r"身份证(?:号|号码)?[:：]?\s*[\dXx*]{6,20}"],
    "家庭住址": [r"(?:家庭住址|家庭地址|户籍(?:所在地|地址)?|户口)[:：]?\s*[^\n，,；;]{2,40}"],
    "政治面貌": [r"政治面貌[:：]?\s*[\u4e00-\u9fa5]{1,8}",
                 r"中共党员|预备党员|共青团员|民主党派|群众(?=\s|$)"],
    "体貌信息": [r"身高[:：]?\s*\d{2,3}\s*(?:cm|CM|厘米)?", r"体重[:：]?\s*\d{2,3}\s*(?:kg|KG|公斤)?",
                 r"照片[:：]?"],
}

_COMPILED = {cat: [re.compile(p) for p in pats] for cat, pats in RULES.items()}

# 抽取结果里必须丢弃的键（即使模型返回了也不采纳）
FORBIDDEN_KEYS = {
    "gender", "sex", "性别", "ethnicity", "ethnic", "民族", "nation",
    "marital", "marital_status", "婚姻", "婚姻状况",
    "children", "fertility", "生育", "生育状况",
    "religion", "宗教信仰", "faith",
    "health", "health_status", "健康状况", "medical", "病史", "disability",
    "id_card", "idcard", "身份证", "身份证号",
    "address", "home_address", "家庭住址", "户籍",
    "political", "政治面貌", "party",
    "height", "weight", "身高", "体重", "photo", "照片", "age", "年龄",
}


def detect(text: str) -> dict[str, int]:
    """返回 {类别: 命中次数}，用于审计举证。"""
    if not text:
        return {}
    out: dict[str, int] = {}
    for cat, pats in _COMPILED.items():
        n = sum(len(p.findall(text)) for p in pats)
        if n:
            out[cat] = n
    return out


def scrub(text: str) -> tuple[str, dict[str, int]]:
    """屏蔽敏感取值，返回（脱敏文本，命中统计）。"""
    if not text:
        return text or "", {}
    found: dict[str, int] = {}
    cleaned = text
    for cat, pats in _COMPILED.items():
        hits = 0
        for pat in pats:
            def _sub(m: re.Match, _cat: str = cat) -> str:
                nonlocal hits
                hits += 1
                label = m.group(0).split(":")[0].split("：")[0]
                # 保留字段名，屏蔽取值：如 "民族：汉" -> "民族：【已屏蔽】"
                if ("：" in m.group(0)) or (":" in m.group(0)):
                    sep = "：" if "：" in m.group(0) else ":"
                    return f"{label}{sep}{PLACEHOLDER}"
                return PLACEHOLDER
            cleaned = pat.sub(_sub, cleaned)
        if hits:
            found[cat] = hits
    return cleaned, found


def strip_forbidden(data: dict) -> tuple[dict, list[str]]:
    """从抽取结果中移除敏感键。返回（清洗后字典，被移除的键）。"""
    removed: list[str] = []
    out: dict = {}
    for k, v in (data or {}).items():
        if str(k).strip().lower() in FORBIDDEN_KEYS or str(k).strip() in FORBIDDEN_KEYS:
            removed.append(str(k))
            continue
        if isinstance(v, dict):
            sub, sub_removed = strip_forbidden(v)
            removed.extend(f"{k}.{r}" for r in sub_removed)
            out[k] = sub
        else:
            out[k] = v
    return out, removed


def policy() -> dict:
    return {
        "屏蔽类别": list(RULES.keys()),
        "依据": "《个人信息保护法》最小必要原则 + 反就业歧视",
        "作用范围": "进入任何模型之前的文本，以及模型返回的抽取结果",
        "原件留存": "原件附件完整留档，受访问控制与审计约束",
    }
