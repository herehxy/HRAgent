"""上下文预算、工具结果压缩与运行指标（纯函数，便于单测）。

为什么单独成模块：
`loop.py` 已经同时承担协议、路由、工具执行与落库，再把“预算怎么分、结果怎么裁”
塞进去会很难验证。这里只做确定性的文本/JSON 处理，不调用模型、不查库；
`loop.py` 在每个模型轮次前调用 `govern_messages()`，并把统计写进 `agent_runs`。

治理原则（与项目既有约束一致）：

1. **不删业务事实，只压缩传输形态**：工具完整结果仍留在 trace / 数据库，
   给模型的只是带 `_context_trimmed` 标记的紧凑版；需要细节时模型可按 id
   再调 `get_candidate` / `get_document_text` 等工具。
2. **不破坏 tool 协议**：`assistant(tool_calls)` 与对应 `tool` 消息必须成对保留，
   所以只替换 `tool` 消息的 content，不单独删除其中一条。
3. **优先级固定**：系统提示词、当前用户问题、最近工具结果优先；更早的历史
   消息先截断，再按整轮丢弃。
4. **预算按窗口自适应**：32k 窗口自动少给工具、少留历史；1M 窗口也不会
   无限塞，默认单轮最多 120k tokens，避免“窗口够大就烧钱”。
"""
from __future__ import annotations

import json
import re
from typing import Any

#: 1M 窗口也不把整轮 prompt 堆到 1M；这是成本保险丝。
DEFAULT_BUDGET_CAP = 120_000
DEFAULT_MAX_OUTPUT = 4_096
#: 单条工具结果默认上限；真正的总预算由 `govern_messages()` 再兜一次。
DEFAULT_TOOL_RESULT_TOKENS = 6_000
#: 历史消息单条上限；更早的整轮会被丢弃。
DEFAULT_HISTORY_MESSAGE_TOKENS = 1_800
#: 会话记忆/SOP 注入上限。
DEFAULT_MEMORY_TOKENS = 3_000
DEFAULT_SKILL_TOKENS = 2_500

#: JSON 结果里必须尽量保留的字段：模型和前端卡片都靠它们继续工作。
_PRIORITY_KEYS = frozenset({
    "error", "hint", "summary", "note", "message", "ok", "status",
    "count", "total", "page", "limit", "has_more", "next_page",
    "candidate_id", "job_id", "application_id", "proposal_id", "run_id",
    "download_url", "scope", "label", "name", "title", "subject", "from",
    "stage", "tier", "score", "verified",
})

#: 少数工具的结果本身就是给模型读的长文（邮件草稿/原件正文），
#: 允许比普通工具多留一些；但仍然受总预算约束。
_TOOL_RESULT_FLOOR = {
    "draft_email": 10_000,
    "preview_mail": 10_000,
    "get_mail_template": 8_000,
    "get_document_text": 8_000,
    "get_source_mail": 8_000,
    "get_candidate": 8_000,
}


def estimate_tokens(text: Any) -> int:
    """粗略 token 估算：中文≈1 字 1 token，英文/符号≈4 字符 1 token。

    与 `loop._est_tokens` 保持同一口径；真实计费仍以模型返回的 usage 为准。
    """
    s = str(text or "")
    if not s:
        return 0
    cjk = len(re.findall(r"[一-鿿]", s))
    return max(1, int(cjk + (len(s) - cjk) * 0.28))


def _positive_int(v: Any, default: int = 0) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        return default
    return n if n > 0 else default


def resolve_budget(window: int | None = None, *, max_output_tokens: Any = None,
                   configured_budget: Any = None, cap: Any = None) -> dict:
    """把模型窗口换算成“本轮 prompt 预算”和配套上限。

    返回的 `budget` 是**输入**预算，不包含输出；`window` 仍然用于界面展示
    真实窗口。32k 模型会自动降低工具数量，避免系统提示词+工具说明就吃掉一半。
    """
    win = max(4_096, _positive_int(window, 1_000_000))
    out = _positive_int(max_output_tokens, DEFAULT_MAX_OUTPUT)
    # 输出上限不能超过窗口的一半，否则输入预算会被挤成负数
    out = max(512, min(out, max(1_024, win // 2)))
    available = max(1_024, win - out - 512)
    cap_n = _positive_int(cap, DEFAULT_BUDGET_CAP)
    configured = _positive_int(configured_budget, 0)
    budget = min(available, configured or cap_n)
    budget = max(2_048, budget)

    if win <= 16_384:
        max_tools = 10
    elif win <= 32_768:
        max_tools = 12
    elif win <= 65_536:
        max_tools = 18
    elif win <= 131_072:
        max_tools = 24
    else:
        max_tools = 30

    tool_limit = min(8_000, max(2_000, int(budget * 0.18)))
    history_limit = min(2_400, max(700, int(budget * 0.06)))
    memory_limit = min(DEFAULT_MEMORY_TOKENS, max(500, int(budget * 0.05)))
    skill_limit = min(DEFAULT_SKILL_TOKENS, max(500, int(budget * 0.05)))
    return {
        "window": win,
        "max_output_tokens": out,
        "budget": budget,
        "cap": cap_n,
        "configured": bool(configured),
        "max_tools": max_tools,
        "tool_result_tokens": tool_limit,
        "history_message_tokens": history_limit,
        "memory_tokens": memory_limit,
        "skill_tokens": skill_limit,
    }


def _prefix_chars(text: str, tokens: int) -> str:
    if tokens <= 0:
        return ""
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if estimate_tokens(text[:mid]) <= tokens:
            lo = mid
        else:
            hi = mid - 1
    return text[:lo]


def _suffix_chars(text: str, tokens: int) -> str:
    if tokens <= 0:
        return ""
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if estimate_tokens(text[-mid:]) <= tokens:
            lo = mid
        else:
            hi = mid - 1
    return text[-lo:] if lo else ""


def clip_text(text: Any, max_tokens: int, *, head_ratio: float = 0.6,
              marker: str = "\n…（已按上下文预算截断）…\n") -> str:
    """按 token 预算保留头尾；不会返回超过预算的字符串（marker 也计入）。"""
    s = str(text or "")
    max_tokens = _positive_int(max_tokens, 1)
    if estimate_tokens(s) <= max_tokens:
        return s
    marker = str(marker or "")
    marker_tokens = estimate_tokens(marker)
    if marker_tokens >= max_tokens:
        return _prefix_chars(s, max_tokens)
    remain = max_tokens - marker_tokens
    head = int(remain * max(0.0, min(1.0, head_ratio)))
    tail = max(0, remain - head)
    return _prefix_chars(s, head) + marker + _suffix_chars(s, tail)


def _compact_value(value: Any, budget: int, depth: int = 0) -> Any:
    """递归压缩 JSON 值。只用于“给模型看”的副本，不修改原对象。"""
    budget = max(24, int(budget))
    if isinstance(value, str):
        return clip_text(value, budget, marker="…")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, list):
        if not value:
            return []
        keep = min(len(value), 12 if depth == 0 else 4)
        per = max(24, budget // max(1, keep))
        out = [_compact_value(v, per, depth + 1) for v in value[:keep]]
        if len(value) > keep:
            out.append({"_truncated": True, "_total": len(value)})
        return out
    if isinstance(value, dict):
        if depth >= 5:
            return clip_text(json.dumps(value, ensure_ascii=False), budget, marker="…")
        items = list(value.items())
        # 字段太多时每个字段分不到预算；先保证关键字段整值保留。
        priority = [kv for kv in items if kv[0] in _PRIORITY_KEYS]
        rest = [kv for kv in items if kv[0] not in _PRIORITY_KEYS]
        out: dict[str, Any] = {}
        for k, v in priority:
            out[k] = _compact_value(v, max(32, budget // max(1, len(items))), depth + 1)
        left = max(0, budget - estimate_tokens(json.dumps(out, ensure_ascii=False)))
        for k, v in rest:
            out[k] = _compact_value(v, max(24, left // max(1, len(rest))), depth + 1)
        return out
    return clip_text(str(value), budget, marker="…")


def _brief_of(obj: Any) -> str:
    if isinstance(obj, dict):
        for k in ("summary", "error", "note", "message", "hint"):
            if obj.get(k):
                return str(obj[k])[:240]
        if obj.get("count") is not None:
            return f"{obj['count']} 条"
        if isinstance(obj.get("items"), list):
            return f"{len(obj['items'])} 条"
    if isinstance(obj, list):
        return f"{len(obj)} 条"
    return str(obj or "")[:240]


def compact_tool_result(tool: str, result: Any, max_tokens: int = DEFAULT_TOOL_RESULT_TOKENS) -> str:
    """把一条工具结果压到预算内，保留 id / 错误 / 下载链接等可继续操作的字段。

    返回的字符串仍然是合法 JSON（原结果是 JSON 时）；被裁剪时带
    `_context_trimmed=true` 和 `_context_hint`，让模型知道“还有完整内容，
    需要时按 id 再查”，而不是以为数据只有这些。
    """
    raw = str(result or "")
    max_tokens = _positive_int(max_tokens, DEFAULT_TOOL_RESULT_TOKENS)
    floor = _TOOL_RESULT_FLOOR.get(str(tool or ""), 0)
    # 只有在“正常单条上限”附近才给长文工具放宽；治理层把它压到 300 时
    # 必须尊重这个硬上限，否则压缩完仍然超预算。放宽也不超过原上限的 1.5 倍。
    if floor and max_tokens >= 3_000:
        max_tokens = max(max_tokens, min(floor, int(max_tokens * 1.5), 12_000))
    if estimate_tokens(raw) <= max_tokens:
        return raw

    try:
        obj = json.loads(raw)
    except (TypeError, ValueError):
        return clip_text(raw, max_tokens, marker="\n…（工具结果过长，已按上下文预算截断）…\n")

    if not isinstance(obj, (dict, list)):
        return clip_text(raw, max_tokens, marker="\n…（工具结果过长，已按上下文预算截断）…\n")

    budget = max_tokens
    for _ in range(4):
        compact = _compact_value(obj, budget)
        if isinstance(compact, list):
            # 根是数组时也包一层：否则模型只看到被截短的数组，
            # 不知道“还有更多、可以按 id 再查”。
            compact = {"_context_trimmed": True,
                       "_context_hint": "工具结果过长，已按上下文预算压缩；"
                                        "需要完整内容请用对应工具按 id 重新读取。",
                       "count": len(obj), "items": compact}
        elif isinstance(compact, dict):
            compact["_context_trimmed"] = True
            compact.setdefault(
                "_context_hint",
                "工具结果过长，已按上下文预算压缩；需要完整内容请用对应工具按 id 重新读取。")
        text = json.dumps(compact, ensure_ascii=False)
        if estimate_tokens(text) <= max_tokens:
            return text
        budget = max(160, int(budget * 0.65))

    # 最后兜底：只保留最关键字段 + 一小段预览，绝不让单条结果撑爆整轮。
    if isinstance(obj, dict):
        head = {k: obj[k] for k in _PRIORITY_KEYS if k in obj}
        if not head:
            head = {"summary": _brief_of(obj)}
    else:
        head = {"count": len(obj), "items": obj[:3]}
    head["_context_trimmed"] = True
    head["_context_hint"] = "工具结果过长，已仅保留关键字段；需要完整内容请按 id 重新查询。"
    head["_preview"] = clip_text(raw, max(200, max_tokens // 2), marker="…")
    text = json.dumps(head, ensure_ascii=False)
    if estimate_tokens(text) <= max_tokens:
        return text
    return clip_text(text, max_tokens, marker="…")


def message_tokens(message: dict) -> int:
    """一条消息在 prompt 里的大致 token（含 tool_calls 参数）。"""
    parts = [str(message.get("content") or "")]
    # 推理模型会把 reasoning_content 原样回传，它同样占 prompt 预算。
    if message.get("reasoning_content"):
        parts.append(str(message["reasoning_content"]))
    if message.get("tool_calls"):
        try:
            parts.append(json.dumps(message["tool_calls"], ensure_ascii=False))
        except (TypeError, ValueError):
            parts.append(str(message["tool_calls"]))
    if message.get("name"):
        parts.append(str(message["name"]))
    return estimate_tokens("\n".join(parts))


def govern_messages(messages: list[dict], *, budget: int,
                    tool_limit: int = DEFAULT_TOOL_RESULT_TOKENS,
                    history_message_tokens: int = DEFAULT_HISTORY_MESSAGE_TOKENS,
                    keep_recent_tools: int = 2) -> dict:
    """就地裁剪 `messages`，返回本轮治理统计。

    安全边界：
      · 不删除 system 消息；
      · 不删除 assistant(tool_calls) 或对应的 tool 消息（只压缩 content）；
      · 不删除当前用户消息（预算极小时才截断它，且尽量保留头尾）。
    """
    budget = max(2_048, _positive_int(budget, 120_000))
    tool_limit = max(240, _positive_int(tool_limit, DEFAULT_TOOL_RESULT_TOKENS))
    history_message_tokens = max(300, _positive_int(
        history_message_tokens, DEFAULT_HISTORY_MESSAGE_TOKENS))

    before = sum(message_tokens(m) for m in messages)
    stats = {
        "budget": budget,
        "estimated_before": before,
        "estimated_after": before,
        "trimmed_tokens": 0,
        "trimmed_messages": 0,
        "compacted_tools": 0,
        "dropped_history": 0,
        "over_budget": False,
        "tool_tokens": 0,
        "tool_ratio": 0.0,
        "parts": {},
    }

    last_user = -1
    for i, m in enumerate(messages):
        if m.get("role") == "user":
            last_user = i

    # 1) 先截断更早的历史消息（保留最近的完整问答）。
    for i, m in enumerate(messages):
        if i >= last_user or m.get("role") not in ("user", "assistant"):
            continue
        old = str(m.get("content") or "")
        if message_tokens(m) <= history_message_tokens:
            continue
        new = clip_text(old, history_message_tokens,
                        marker="\n…（较早对话已按上下文预算截断）…\n")
        if new != old:
            m["content"] = new
            stats["trimmed_messages"] += 1

    # 2) 工具结果：最近几条保留较高上限，更早的压成很短的摘要。
    tool_indices = [i for i, m in enumerate(messages) if m.get("role") == "tool"]
    for pos, i in enumerate(tool_indices):
        m = messages[i]
        old = str(m.get("content") or "")
        is_recent = pos >= len(tool_indices) - max(1, int(keep_recent_tools))
        cap = tool_limit if is_recent else max(400, tool_limit // 4)
        new = compact_tool_result(str(m.get("name") or ""), old, cap)
        if new != old:
            m["content"] = new
            stats["compacted_tools"] += 1

    def _total(exclude: set[int] | None = None) -> int:
        exclude = exclude or set()
        return sum(message_tokens(m) for i, m in enumerate(messages) if i not in exclude)

    # 3) 仍然超预算：按整轮丢弃最旧的历史（user+assistant 成对）。
    history_idx = [i for i, m in enumerate(messages)
                   if i < last_user and m.get("role") in ("user", "assistant")]
    drop: set[int] = set()
    while history_idx and _total(drop) > budget:
        drop.update(history_idx[:2])
        history_idx = history_idx[2:]
    if drop:
        messages[:] = [m for i, m in enumerate(messages) if i not in drop]
        stats["dropped_history"] = len(drop)
        # 删除后索引会变，重新定位当前用户，避免最后一步截错消息。
        last_user = -1
        for i, m in enumerate(messages):
            if m.get("role") == "user":
                last_user = i

    # 4) 还超：把工具结果再压到最低限度（工具消息不能删，只能缩短）。
    if _total() > budget:
        for m in messages:
            if m.get("role") != "tool":
                continue
            old = str(m.get("content") or "")
            new = compact_tool_result(str(m.get("name") or ""), old,
                                      max(240, tool_limit // 10))
            if new != old:
                m["content"] = new
                stats["compacted_tools"] += 1

    # 5) 最后一层：截断非 system 的长文本。当前用户问题只截到预算的 1/4，
    #    避免把“问题本身”裁得看不懂。
    if _total() > budget:
        for i, m in enumerate(messages):
            role = m.get("role")
            if role == "system" or role == "tool":
                continue
            cap = max(300, budget // 4) if i == last_user else max(240, budget // 8)
            old = str(m.get("content") or "")
            new = clip_text(old, cap, marker="\n…（已按上下文预算截断）…\n")
            if new != old:
                m["content"] = new
                stats["trimmed_messages"] += 1

    after = _total()
    tool_tokens = sum(message_tokens(m) for m in messages if m.get("role") == "tool")
    parts = {
        "system": sum(message_tokens(m) for m in messages if m.get("role") == "system"),
        "tool": tool_tokens,
        "user": sum(message_tokens(m) for m in messages if m.get("role") == "user"),
        "assistant": sum(message_tokens(m) for m in messages if m.get("role") == "assistant"),
    }
    stats.update({
        "estimated_after": after,
        "trimmed_tokens": max(0, before - after),
        "tool_tokens": tool_tokens,
        "tool_ratio": (tool_tokens / after) if after else 0.0,
        "parts": parts,
        "over_budget": after > budget,
    })
    return stats


def _threshold(thresholds: dict | None, key: str, default: float) -> float:
    try:
        return float((thresholds or {}).get(key, default))
    except (TypeError, ValueError):
        return default


def evaluate_alerts(*, tokens_in: int = 0, prompt_estimate: int = 0,
                    budget: int = 0, tool_tokens: int = 0,
                    tool_calls: int = 0, tool_errors: int = 0,
                    rounds: int = 0, max_rounds: int = 5,
                    cache_hit_tokens: int = 0, cache_reported: int = 0,
                    trimmed_tokens: int = 0,
                    thresholds: dict | None = None) -> list[dict]:
    """按阈值生成本轮告警；只返回结论，不负责落库/展示。"""
    prompt = _positive_int(tokens_in, _positive_int(prompt_estimate, 0))
    if not prompt and not tool_calls:
        return []
    budget = max(1, _positive_int(budget, 0))
    tool_tokens = max(0, int(tool_tokens or 0))
    tool_calls = max(0, int(tool_calls or 0))
    tool_errors = min(tool_calls, max(0, int(tool_errors or 0)))
    rounds = max(0, int(rounds or 0))
    max_rounds = max(1, int(max_rounds or 5))
    trimmed_tokens = max(0, int(trimmed_tokens or 0))
    alerts: list[dict] = []

    def add(code: str, level: str, message: str, value: Any = None,
            threshold: Any = None) -> None:
        alerts.append({"code": code, "level": level, "message": message,
                       "value": value, "threshold": threshold})

    ratio = (prompt / budget) if budget else 0.0
    near = _threshold(thresholds, "budget_ratio", 0.85)
    if budget and ratio >= 1.0:
        add("context_over_budget", "high",
            f"本轮输入 {prompt} tokens 已超过预算 {budget}（{ratio:.0%}）",
            round(ratio, 3), 1.0)
    elif budget and ratio >= near:
        add("context_near_limit", "warn",
            f"本轮输入 {prompt} tokens 接近预算 {budget}（{ratio:.0%}）",
            round(ratio, 3), near)

    tool_ratio = (tool_tokens / prompt) if prompt else 0.0
    tool_warn = _threshold(thresholds, "tool_ratio", 0.55)
    if tool_calls >= 2 and tool_ratio >= tool_warn:
        add("tool_ratio_high", "warn",
            f"工具结果占输入 {tool_ratio:.0%}，可能挤压业务上下文",
            round(tool_ratio, 3), tool_warn)

    error_rate = (tool_errors / tool_calls) if tool_calls else 0.0
    err_warn = _threshold(thresholds, "tool_error_rate", 0.20)
    if tool_calls >= 3 and error_rate >= err_warn:
        add("tool_error_high", "high" if error_rate >= 0.40 else "warn",
            f"工具错误 {tool_errors}/{tool_calls}（{error_rate:.0%}）",
            round(error_rate, 3), err_warn)

    if rounds >= max_rounds:
        add("round_limit", "warn",
            f"本轮用满 {rounds}/{max_rounds} 个工具轮次，可能需要拆分问题",
            rounds, max_rounds)

    trim_warn = _threshold(thresholds, "trim_ratio", 0.25)
    if budget and trimmed_tokens / budget >= trim_warn:
        add("context_trimmed_heavy", "warn",
            f"上下文压缩 {trimmed_tokens} tokens（占预算 {trimmed_tokens / budget:.0%}），"
            "较早信息可能已被截断",
            round(trimmed_tokens / budget, 3), trim_warn)

    if cache_reported and prompt >= 8_000:
        cache_rate = min(1.0, max(0.0, cache_hit_tokens / prompt))
        cache_min = _threshold(thresholds, "cache_rate_min", 0.10)
        if cache_rate < cache_min:
            add("cache_low", "info",
                f"缓存命中 {cache_rate:.0%}，长系统提示词可能未命中服务商缓存",
                round(cache_rate, 3), cache_min)

    order = {"high": 0, "warn": 1, "info": 2}
    alerts.sort(key=lambda a: (order.get(a["level"], 9), a["code"]))
    return alerts


def alert_level(alerts: list[dict] | None) -> str:
    """取最高级别；无告警返回空串。"""
    for level in ("high", "warn", "info"):
        if any(a.get("level") == level for a in (alerts or [])):
            return level
    return ""
