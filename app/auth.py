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

#: 角色 → 展示信息。**保持 `hr` 为最高权限且是默认**（老库里的账号都是它）。
ROLES = {
    "hr": {"label": "招聘 HR", "desc": "日常招聘操作：看人、改档、推进阶段、发信"},
    "admin": {"label": "系统管理员", "desc": "在 HR 之上再加账号管理与系统配置"},
    "viewer": {"label": "只读观察", "desc": "只能查看，不能改动任何数据"},
}

#: 权限点 → 中文说明（给界面画权限矩阵用；也防止将来加权限时忘了写文案）。
PERM_LABELS = {
    "read": "查看候选人/岗位",
    "view_full_contact": "查看完整联系方式",
    "chat": "使用智能助手",
    "propose": "让助手生成待确认提案",
    "confirm": "确认提案、发送邮件",
    "set_stage": "改档位 / 推进阶段 / 归档",
    "add_note": "写备注",
    "merge": "合并 / 拆分档案",
    "ingest": "导入简历、跑分析任务",
    "export": "导出名单 / 打包下载原件",
    "archive": "归档（提案里执行）",
    "settings": "系统配置（模型、邮箱、字典）",
    "manage_users": "账号管理",
}

#: 只读角色能用的权限。
_READONLY = {"read", "view_full_contact", "chat"}

_PERMS = {
    # 管理员 = HR 的全部权限 + 账号管理
    "admin": set(PERM_LABELS),
    "hr": {"read", "view_full_contact", "chat", "propose", "confirm",
           "set_stage", "add_note", "merge", "ingest", "export", "archive",
           "settings"},
    # 只读：不含 propose/confirm/set_stage/... —— 连"生成提案"都不给，
    # 因为提案被确认后就是真实改动，只读用户不该在系统里留下待办。
    "viewer": set(_READONLY),
}

ROLE_DEFAULT_USERS = [
    # 首次运行建一个管理员；`hr` 账号保留给老部署（老库里已有它）。
    ("admin", "admin", "系统管理员", "admin"),
]


def can(role: str, perm: str) -> bool:
    return perm in _PERMS.get(role or "hr", set())


def permissions(role: str) -> list[str]:
    return sorted(_PERMS.get(role or "hr", set()))


def role_label(role: str) -> str:
    return (ROLES.get(role) or {}).get("label") or role


def ensure_seed_users(conn, default_password: str = "change-me") -> list[str]:
    """准备可登录的账号。分三种情况，**都不能把人锁在外面**：

    ① 空库（全新部署）：建一个 `admin`，口令 `change-me`，界面强制改；
    ② 老库只有 `hr`（v3.0.2 及以前，单角色时代）：把 `hr` **提升为 admin**。
       这是本次升级最关键的一步 —— v3.0.3 起账号登录默认开启，
       而老库里没有任何 admin，若不提升，升级后连"账号管理"页都进不去；
    ③ 已经有 admin：什么都不做。

    为什么不给老库"新建一个 admin/admin"：那会凭空多出一个**弱口令入口**，
    比少一个功能危险得多。提升已有账号只需改写 role 列，不新增凭据。
    """
    users = db.list_users(conn)
    if not users:
        created = []
        for username, _, display, role in ROLE_DEFAULT_USERS:
            if not db.get_user(conn, username):
                db.create_user(conn, username, default_password, role, display)
                created.append(username)
        return created

    if db.count_active_admins(conn) == 0:
        # 没有启用中的管理员 → 把最早的那个启用账号提升上来，保证系统可管理。
        for u in users:
            if u.get("active"):
                db.set_user_role(conn, u["username"], "admin")
                print(f"[auth] 已把账号「{u['username']}」提升为管理员"
                      f"（升级到 v3.0.3 后需要有人能管理账号）")
                break
    return []


#: 种子口令。命中它就在登录响应里带 `must_change_password=True`，
#: 前端弹改密对话框。**不用"是否改过"的标记位**：多一列状态就要多维护一处，
#: 而"口令还是默认值"本身就是最准确的判断。
DEFAULT_PASSWORD = "change-me"


def authenticate(conn, username: str, password: str) -> dict | None:
    user = db.get_user(conn, username)
    if not user or not user.get("active"):
        return None
    if not crypto.verify_password(password, user["password_hash"], user["salt"]):
        return None
    token = db.create_session(conn, user["username"], user["role"])
    # 老库的 display_name 存的是**角色名**（"招聘 HR"，单角色时代建的），
    # 拿它当人名显示会出现"招聘 HR / 系统管理员"这种重复。与角色名相同就退回账号名。
    disp = (user.get("display_name") or "").strip()
    if not disp or disp in {v["label"] for v in ROLES.values()}:
        disp = username
    return {"token": token, "username": user["username"], "role": user["role"],
            "role_label": role_label(user["role"]),
            "display_name": disp,
            "permissions": permissions(user["role"]),
            "must_change_password": (password == DEFAULT_PASSWORD)}


def resolve(conn, token: str | None) -> dict:
    """解析会话。无 token / token 失效时返回匿名（**零权限**）。

    匿名不是"只读 HR"，而是**没有任何权限**——否则 `require()` 只按 role
    判断时，不带令牌就能拿到 HR 的全部权限，登录模式形同虚设。
    匿名 session 由 `server.require()` 在入口统一拒绝（401）。
    """
    if not token:
        return {"username": "anonymous", "role": "guest", "display_name": "未登录",
                "permissions": [], "anonymous": True}
    s = db.get_session(conn, token)
    if not s:
        return {"username": "anonymous", "role": "guest", "display_name": "未登录",
                "permissions": [], "anonymous": True, "expired": True}
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
