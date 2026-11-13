"""工具参数校验/纠偏（纯函数，便于单测；不依赖模型或数据库）。"""
from __future__ import annotations

import re


def validate_tool_args(spec: dict, args: dict) -> tuple[dict, list[str]]:
    """按工具 schema 做轻量校验/纠偏。

    模型最常见的两类参数错误是"数字/布尔写成字符串"和"数组写成逗号串"。
    这里能纠的直接纠；枚举值不对、类型实在转不过来才返回错误，让模型
    下一轮按提示重试，而不是把脏参数交给业务 handler 半路炸掉。
    """
    params = ((spec.get("function") or {}).get("parameters") or {})
    props = params.get("properties") or {}
    out = dict(args or {})
    errors: list[str] = []
    for k, v in list(out.items()):
        sch = props.get(k) or {}
        typ = sch.get("type")
        types = typ if isinstance(typ, list) else ([typ] if typ else [])
        try:
            if "integer" in types and not isinstance(v, bool):
                if isinstance(v, str) and v.strip().lstrip("-").isdigit():
                    out[k] = int(v.strip())
                    v = out[k]
                elif isinstance(v, float) and v.is_integer():
                    out[k] = int(v)
                    v = out[k]
            if "number" in types and isinstance(v, str):
                out[k] = float(v)
                v = out[k]
            if "boolean" in types and isinstance(v, str):
                low = v.strip().lower()
                if low in ("true", "1", "yes", "是"):
                    out[k] = True; v = True
                elif low in ("false", "0", "no", "否"):
                    out[k] = False; v = False
            if "array" in types and isinstance(v, str):
                out[k] = [x.strip() for x in re.split(r"[,，、;；]+", v)
                          if x.strip()]
                v = out[k]
        except (TypeError, ValueError):
            errors.append(f"{k} 应为 {'/'.join(types)}")
            continue
        if "integer" in types and (not isinstance(v, int) or isinstance(v, bool)):
            errors.append(f"{k} 应为整数")
            continue
        if "number" in types and (not isinstance(v, (int, float)) or isinstance(v, bool)):
            errors.append(f"{k} 应为数字")
            continue
        if "boolean" in types and not isinstance(v, bool):
            errors.append(f"{k} 应为布尔值")
            continue
        if "array" in types and not isinstance(v, list):
            errors.append(f"{k} 应为数组")
            continue
        enum = sch.get("enum")
        if enum and v not in enum:
            errors.append(f"{k} 只能是 {'、'.join(map(str, enum))}")
    return out, errors
