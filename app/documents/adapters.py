"""业务 Adapter：抽取结果 → 某个业务域允许写入的字段。"""
from __future__ import annotations


def employee_payload(fields: dict | None) -> dict:
    """过滤成员工档案白名单；数组字段保持列表，标量保持原值。

    这里只做“字段白名单”，不写库；实际写入仍由 `staff.update_employee`
    执行，复用它的加密、日期规范化、审计。
    """
    from .. import staff

    allowed = set(staff.PLAIN_FIELDS) | set(staff.ENCRYPTED_FIELDS)
    out: dict = {}
    for k, v in (fields or {}).items():
        if k in staff.JSON_FIELDS and isinstance(v, list):
            if v:
                out[k] = v
        elif k in allowed and str(v or "").strip():
            out[k] = v
    return out
