"""权限与个人信息保护：单 HR 角色 + 展示脱敏替代方案。

产品定位：本系统**只给 HR 使用**，不分角色。因此：

- 账号层只保留单一角色 `hr`（具备全部权限）；
- 前端不再有角色切换器，也不再需要"按角色脱敏"——HR 可直接看到完整联系方式；
- **库内仍为 AES-GCM 加密存储**（`candidates.phone_enc / email_enc`），
  "直接展示"指的是下发到前端时解密成明文，而非把密文也下发；
- 密文字段、盲索引、身份键**绝不下发**前端（避免被浏览器脚本拿去比对）。

保留的能力：
- 查看留痕：每次打开某人完整档案写一条审计（谁、何时、看了谁）；
- 盲筛已移除（单 HR 用户无盲筛诉求）。**性别标签**按校招场景需要保留：
  只取简历上**明写的标签行**（不做任何推断/反推），只用于界面展示与
  **默认关闭**的筛选开关，且永不进入 `tier.grade()`——评分只看学历/年限/技能/证书。
  出生年份仍不采集。
"""
from __future__ import annotations

from . import crypto, db

ROLES = {
    "hr": {"label": "招聘 HR", "desc": "唯一角色，具备全部权限"},
}

_PERMS = {
    "hr": {"read", "chat", "propose", "confirm", "set_stage", "add_note", "export",
           "ingest", "manage_users", "settings", "view_full_contact", "merge"},
}

ROLE_DEFAULT_USERS = [
    ("hr", "hr", "招聘 HR", "hr"),
]


def can(role: str, perm: str) -> bool:
    return perm in _PERMS.get(role or "hr", set())


def permissions(role: str) -> list[str]:
    return sorted(_PERMS.get(role or "hr", set()))


def ensure_seed_users(conn, default_password: str = "change-me") -> list[str]:
    """首次运行时创建示例账号（单一 HR 角色）。**首次登录后请立即改口令。**"""
    created = []
    for username, _, display, role in ROLE_DEFAULT_USERS:
        if not db.get_user(conn, username):
            db.create_user(conn, username, default_password, role, display)
            created.append(username)
    return created


def authenticate(conn, username: str, password: str) -> dict | None:
    user = db.get_user(conn, username)
    if not user or not user.get("active"):
        return None
    if not crypto.verify_password(password, user["password_hash"], user["salt"]):
        return None
    token = db.create_session(conn, user["username"], user["role"])
    return {"token": token, "username": user["username"], "role": user["role"],
            "display_name": user.get("display_name"), "permissions": permissions(user["role"])}


def resolve(conn, token: str | None) -> dict:
    """解析会话。无 token 时返回匿名（最小权限，等价于只读）。"""
    if not token:
        return {"username": "anonymous", "role": "hr", "display_name": "未登录",
                "permissions": permissions("hr"), "anonymous": True}
    s = db.get_session(conn, token)
    if not s:
        return {"username": "anonymous", "role": "hr", "display_name": "未登录",
                "permissions": permissions("hr"), "anonymous": True, "expired": True}
    return {"username": s["username"], "role": s["role"], "display_name": s["username"],
            "permissions": permissions(s["role"]), "anonymous": False}


# ---------------------------------------------------------------- 展示（完整联系方式）

def present_candidate(cand: dict) -> dict:
    """下发前处理：解密联系方式为明文，剥离密文/盲索引/身份键。返回副本，不改库。"""
    if not cand:
        return cand
    out = dict(cand)
    phone = crypto.decrypt(cand.get("phone_enc"))
    email = crypto.decrypt(cand.get("email_enc"))
    out["phone"] = phone or "—"
    out["email"] = email or "—"
    for k in ("phone_enc", "email_enc", "phone_bidx", "email_bidx", "identity_key"):
        out.pop(k, None)
    out["contact_full_visible"] = True
    return out


def present_list(items: list[dict]) -> list[dict]:
    return [present_candidate(i) for i in items]


def policy_report(conn) -> dict:
    return {
        "roles": ROLES,
        "permission_matrix": {r: permissions(r) for r in ROLES},
        "pii_protection": crypto.protection(),
        "contact_note": "联系方式对 HR 完整展示；库内为 AES-GCM 加密存储，密文与盲索引不下发前端。",
        "不采集": "民族/婚姻/生育/宗教/健康/身份证号/住址/政治面貌/身高体重/照片"
                 "在进入模型前即被屏蔽；出生年份不写入结构化档案、不参与匹配",
        "性别处理": "仅当简历**明写**「性别：男/女」标签行时记录，不做推断；"
                  "只用于界面展示与一个**默认关闭**的筛选开关（开启动作写入审计）；"
                  "永不参与评分与分级。依据：招聘不得限定性别"
                  "（《就业促进法》第 27 条、《妇女权益保障法》第 43 条）。",
        "audit_rule": "打开候选人完整档案、改档、改阶段、导出、确认提案均写入审计",
        "retention_note": "未录用简历建议保留 12–24 个月，到期提醒后清理（见设置项）",
    }
