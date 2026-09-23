"""身份归一：把"同一个人"的不同简历版本收敛到同一个 Candidate 档。

`identity_key` 生成优先级（稳定、不可逆）：

1. 手机号（11 位）—— 最强标识
2. 邮箱（小写）
3. 姓名 + 出生年份 + 毕业院校 —— 无联系方式时的兜底
4. 姓名 —— 最弱兜底（会与同名者区分不开，此时系统只给"疑似重复"提示，不自动合并）

设计取舍：`identity_key` 用**无密钥** SHA256 截断（跨密钥轮换保持稳定），
另用 `crypto.blind_index` 生成 `phone_bidx / email_bidx` 用于检索比对。
系统**不自动合并**，只在命中不同键但相似度高时提示 HR 确认——误合并两个人档案的破坏性，
远大于同一人重复入库。
"""
from __future__ import annotations

import hashlib
import re

_PHONE_RE = re.compile(r"1[3-9]\d{9}")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")


def normalize_phone(value: str | None) -> str | None:
    if not value:
        return None
    digits = re.sub(r"\D", "", str(value))
    if len(digits) == 13 and digits.startswith("86"):
        digits = digits[2:]
    if len(digits) == 11 and digits.startswith("1"):
        return digits
    if len(digits) >= 11:
        m = _PHONE_RE.search(digits)
        return m.group(0) if m else None
    return None


def normalize_email(value: str | None) -> str | None:
    if not value:
        return None
    m = _EMAIL_RE.search(str(value))
    return m.group(0).lower() if m else None


def normalize_name(value: str | None) -> str | None:
    if not value:
        return None
    name = re.sub(r"\s+", "", str(value))
    name = re.sub(r"^(个人简历|简历|姓名[:：])", "", name)
    return name or None


def normalize_school(value: str | None) -> str | None:
    if not value:
        return None
    return re.sub(r"\s+", "", str(value)) or None


def _digest(parts: list[str], prefix: str) -> str:
    raw = "|".join(parts)
    return f"{prefix}:{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:32]}"


def build_identity_key(name: str | None, phone: str | None = None,
                       email: str | None = None, school: str | None = None,
                       birth_year: int | None = None) -> str:
    """生成身份主键。始终返回非空字符串。"""
    ph = normalize_phone(phone)
    if ph:
        return _digest([ph], "ph")
    em = normalize_email(email)
    if em:
        return _digest([em], "em")
    nm = normalize_name(name)
    sc = normalize_school(school)
    if nm and sc:
        return _digest([nm, sc, str(birth_year or "")], "ns")
    if nm:
        return _digest([nm, str(birth_year or "")], "nm")
    return _digest(["unknown"], "uk")


def explain_key(key: str) -> str:
    """把键类型翻译成人话，用于界面展示。"""
    return {
        "ph": "按手机号识别",
        "em": "按邮箱识别",
        "ns": "按姓名+院校识别",
        "nm": "按姓名识别（弱）",
        "uk": "无有效标识（需人工核实）",
    }.get(key.split(":", 1)[0], "未知")
