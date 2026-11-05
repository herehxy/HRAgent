# -*- coding: utf-8 -*-
"""项目经历提炼（AI 总结，v4.0.18）。

档案页要的不是简历原文里的整段项目描述，而是 HR 能两行看完的要点：
**做了什么、用了什么方法、拿到什么结果**，再加一句方向总结。所以这里：
  · 只让模型输出短摘要，不把原文贴进档案；
  · 每条都带原文证据，证据/项目名在原文里找不到的直接丢掉（反幻觉）；
  · 结果缓存进 candidates.projects_json，同一份简历只调一次模型；
  · 没有项目就返回空列表，界面不显示这个区块。
"""
from __future__ import annotations

import re
from typing import Any

from . import sanitize

MAX_PROJECTS = 8
MAX_NAME = 40
MAX_SUMMARY = 110
MAX_DIRECTION = 80
MAX_TEXT = 24000

_SYSTEM = (
    "你是招聘简历信息提炼助手。只依据给定简历原文提炼项目经历，严禁编造。\n"
    "要求：\n"
    "1) 只提炼真正的项目 / 科研课题 / 工程项目 / 竞赛项目 / 实习中的项目；"
    "课程名称、课程作业、日常事务不算；\n"
    "2) 每个项目的 summary 控制在两行以内（中文不超过 80 字），"
    "说清『做了什么 + 用了什么方法/技术 + 得到什么结果』，"
    "**不要照抄原文大段文字**；\n"
    "3) direction_summary 用一句不超过 40 字的话，概括这些项目共同的方向 / 技术主线；\n"
    "4) evidence 必须是简历原文中**逐字存在**的片段（不超过 120 字），"
    "不得改写、不得拼接；找不到原文依据的项目不要输出；\n"
    "5) 没有项目经历就返回空数组，不要硬凑；\n"
    "6) 不要输出民族、婚姻、生育、宗教、健康、身份证号、住址等敏感信息。\n"
    "严格输出 JSON，不要解释、不要代码块：\n"
    '{"direction_summary":"项目方向总结（40字内）",'
    '"projects":[{"name":"项目名（30字内）",'
    '"summary":"两行以内的项目总结（80字内）",'
    '"evidence":"原文逐字片段"}]}'
)

_PUNCT_RE = re.compile(r"[\s\-－—–_()（）\[\]【】·,，.。:：;；/\\|、]+")


def _norm(value: Any) -> str:
    """比对用的规范化：去掉空白与常见分隔符，排版差异不算编造。"""
    return _PUNCT_RE.sub("", str(value or "")).lower()


def _one_line(value: Any, limit: int) -> str:
    s = re.sub(r"\s+", " ", str(value or "")).strip()
    return s[:limit].strip()


def clean_projects(data: Any, source_text: str) -> dict | None:
    """校验模型输出：证据/项目名必须能在原文里找到，摘要截断到可控长度。

    返回 None 表示模型输出不可用（调用方不缓存，下次可重试）；
    返回空列表表示**模型确认没有项目**（可以缓存，避免反复调用）。
    """
    if not isinstance(data, dict):
        return None
    raw_items = data.get("projects")
    if raw_items is None:
        raw_items = []
    if not isinstance(raw_items, list):
        return None
    text_norm = _norm(source_text)
    out: list[dict] = []
    seen: set[str] = set()
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        name = _one_line(item.get("name"), MAX_NAME)
        summary = _one_line(item.get("summary"), MAX_SUMMARY)
        evidence = _one_line(item.get("evidence"), 160)
        if not name or not summary:
            continue
        key = _norm(name)
        if not key or key in seen:
            continue
        ev_ok = bool(evidence) and _norm(evidence) in text_norm
        name_ok = key in text_norm
        # 摘要本身是模型改写的，不能拿它验原文；用项目名或证据锚定事实。
        if not (ev_ok or name_ok):
            continue
        seen.add(key)
        out.append({"name": name, "summary": summary,
                    "evidence": evidence if ev_ok else ""})
        if len(out) >= MAX_PROJECTS:
            break
    direction = _one_line(data.get("direction_summary"), MAX_DIRECTION)
    if not out:
        direction = ""
    return {"projects": out, "direction_summary": direction}


def summarize(text: str, cfg: dict | None = None) -> dict | None:
    """调用模型提炼；失败返回 None（不写缓存，下次打开档案可重试）。"""
    raw = str(text or "").strip()
    if not raw:
        return {"projects": [], "direction_summary": ""}
    try:
        safe_text, _found = sanitize.scrub(raw)
    except Exception:                                          # noqa: BLE001
        safe_text = raw
    if not str(safe_text or "").strip():
        return {"projects": [], "direction_summary": ""}
    try:
        from ..agent import llm
        data = llm.chat_json(_SYSTEM, str(safe_text)[:MAX_TEXT], cfg=cfg)
    except Exception as exc:                                   # noqa: BLE001
        print(f"[projects] 项目经历提炼失败：{type(exc).__name__}: {exc}")
        return None
    return clean_projects(data, safe_text)
