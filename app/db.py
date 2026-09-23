"""数据层：企业人才库（Talent Pool）模型。

实体关系（核心是"一档多投"）::

    Candidate (人, 一档)  1 ── N  Application (投递)  N ── 1  Job (岗位)
    Candidate             1 ── N  Document    (附件原件, 只增不改)
    Candidate             N ── N  Skill       (经 candidate_skills, 带原文证据)
    Candidate             N ── N  Tag
    Application                ──  AuditLog / Proposal
    AgentRun                   ── 智能体可观测

三条硬约束（贯穿全库）：
1. **不丢**：`documents` 原件区只增不改；解析失败也留档，投递记录照建。
2. **建议与决定分离**：`applications.tier_suggested`（系统）与 `tier_final`（HR 确认）分列。
3. **反幻觉**：`candidate_skills.verified` 标识该技能是否有原文片段支撑；无片段者不计入命中。

v0.1（扁平的 candidates + audit）会在 `connect()` 时自动迁移为新模型，历史数据不丢。
"""
from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ---------- 人（一档） ----------
CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    identity_key TEXT UNIQUE,
    name TEXT,
    gender TEXT,
    birth_year INTEGER,
    phone_enc TEXT,             -- AES-GCM 密文
    email_enc TEXT,
    phone_bidx TEXT,            -- 盲索引，用于身份比对
    email_bidx TEXT,
    edu_level TEXT,
    school TEXT,
    major TEXT,
    years_exp INTEGER,
    current_org TEXT,
    source_first TEXT,
    first_seen_at TEXT,
    last_active_at TEXT,
    pool_status TEXT DEFAULT '在池',   -- 在池/已联系/面试中/已入职/已归档
    pii_level TEXT DEFAULT '普通',     -- 普通/敏感/涉密
    blind INTEGER DEFAULT 0,
    merged_into INTEGER,               -- 软合并指向主档
    archived_at TEXT,                  -- 软归档时间戳（v1.4）：NULL=在库，非空=已归档
    created_at TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_cand_bidx   ON candidates(phone_bidx, email_bidx);
CREATE INDEX IF NOT EXISTS idx_cand_merged ON candidates(merged_into);

-- ---------- 投递（N 次） ----------
CREATE TABLE IF NOT EXISTS applications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL,
    job_id INTEGER,
    suggested_job_id INTEGER,             -- v1.5「待指定」时的建议岗位（只建议不归岗）
    channel TEXT DEFAULT '文件夹',        -- 邮箱/内推/招聘会/官网/文件夹
    applied_at TEXT,
    resume_doc_id INTEGER,
    score REAL,
    tier_suggested TEXT,
    tier_final TEXT,
    stage TEXT DEFAULT '新投递',          -- 新投递/已联系/初面/复面/待offer/已入职/已结束
    status TEXT DEFAULT '待确认',         -- 待确认/已确认
    note TEXT DEFAULT '',
    needs_review INTEGER DEFAULT 0,
    reasons TEXT, risks TEXT, hits TEXT, miss TEXT, preferred_hit TEXT, breakdown TEXT,
    extract_mode TEXT,
    confidence REAL,
    created_at TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_app_cand ON applications(candidate_id);
CREATE INDEX IF NOT EXISTS idx_app_job  ON applications(job_id, id);
CREATE INDEX IF NOT EXISTS idx_app_stage ON applications(stage);

-- ---------- 附件（原件与解析分离） ----------
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_hash TEXT UNIQUE,                -- SHA256：幂等去重的唯一依据
    candidate_id INTEGER,
    application_id INTEGER,
    file_name TEXT, file_path TEXT, archived_path TEXT,
    mime TEXT, size INTEGER,
    received_at TEXT,
    source_message_id TEXT,
    raw_text TEXT, text_len INTEGER,
    parse_engine TEXT, parse_ok INTEGER DEFAULT 1,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_doc_cand ON documents(candidate_id);
CREATE INDEX IF NOT EXISTS idx_doc_msg  ON documents(source_message_id);

-- ---------- 技能本体 ----------
CREATE TABLE IF NOT EXISTS skills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_name TEXT UNIQUE,
    aliases TEXT,                          -- JSON 数组
    category TEXT,                         -- 材料/工艺/表征/软件/管理/其他
    parent_id INTEGER
);
CREATE TABLE IF NOT EXISTS candidate_skills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL,
    skill_id INTEGER NOT NULL,
    level TEXT,                            -- 了解/熟练/精通
    evidence TEXT,                         -- 原文片段（反幻觉：无片段不计命中）
    verified INTEGER DEFAULT 1,
    from_doc_id INTEGER,
    source TEXT DEFAULT 'rule',            -- rule/llm/hr
    created_at TEXT,
    UNIQUE(candidate_id, skill_id)
);
CREATE INDEX IF NOT EXISTS idx_cs_skill ON candidate_skills(skill_id);
CREATE INDEX IF NOT EXISTS idx_cs_cand  ON candidate_skills(candidate_id);

-- ---------- 标签 ----------
CREATE TABLE IF NOT EXISTS tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE,
    category TEXT DEFAULT '能力'            -- 来源/状态/类型/能力
);
CREATE TABLE IF NOT EXISTS candidate_tags (
    candidate_id INTEGER NOT NULL,
    tag_id INTEGER NOT NULL,
    evidence TEXT, source TEXT DEFAULT 'rule', created_at TEXT,
    PRIMARY KEY (candidate_id, tag_id)
);

-- ---------- 部门 ----------
CREATE TABLE IF NOT EXISTS departments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE,
    description TEXT DEFAULT '',
    active INTEGER DEFAULT 1,             -- 停用而非删除
    created_at TEXT, updated_at TEXT
);

-- ---------- 岗位 ----------
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT, dept TEXT,
    dept_id INTEGER,                      -- FK -> departments.id（旧数据可空）
    jd_json TEXT,
    owner TEXT, status TEXT DEFAULT '开放',
    active INTEGER DEFAULT 1,             -- 停用而非删除
    opened_at TEXT, closed_at TEXT,
    created_at TEXT, updated_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_active ON jobs(active, id);

-- ---------- 收信台账 ----------
CREATE TABLE IF NOT EXISTS email_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id TEXT UNIQUE,
    uid TEXT, mailbox TEXT,
    from_addr TEXT, subject TEXT,
    received_at TEXT,
    has_attachment INTEGER DEFAULT 0,
    attachment_count INTEGER DEFAULT 0,
    processed INTEGER DEFAULT 0,
    added INTEGER DEFAULT 0, skipped INTEGER DEFAULT 0, failed INTEGER DEFAULT 0,
    error TEXT,
    fetched_at TEXT, processed_at TEXT
);

-- ---------- 审计 ----------
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity TEXT, entity_id TEXT,
    action TEXT, before TEXT, after TEXT,
    operator TEXT, role TEXT, ip TEXT, ts TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ent ON audit_log(entity, entity_id, id);

-- ---------- 智能体运行 ----------
CREATE TABLE IF NOT EXISTS agent_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT, question TEXT, answer TEXT,
    model TEXT, tool_calls TEXT, rounds INTEGER,
    tokens_in INTEGER DEFAULT 0, tokens_out INTEGER DEFAULT 0,
    latency_ms INTEGER, status TEXT, mode TEXT,
    operator TEXT, ts TEXT
);

-- ---------- 写入提案（模型不得直接改库） ----------
CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    tool TEXT, args TEXT,
    summary TEXT, risk TEXT DEFAULT '中',
    status TEXT DEFAULT '待确认',           -- 待确认/已执行/已拒绝
    decided_by TEXT, decided_at TEXT, result TEXT,
    created_at TEXT
);

-- ---------- 账号与会话（RBAC） ----------
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE, display_name TEXT,
    role TEXT DEFAULT 'viewer',             -- viewer/recruiter/admin
    password_hash TEXT, salt TEXT,
    active INTEGER DEFAULT 1, created_at TEXT
);
CREATE TABLE IF NOT EXISTS sessions (
    token TEXT PRIMARY KEY, username TEXT, role TEXT,
    created_at TEXT, expires_at TEXT
);

-- ---------- 向量索引 ----------
CREATE TABLE IF NOT EXISTS embeddings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_type TEXT, owner_id INTEGER,
    model TEXT, dim INTEGER, vector BLOB, text_hash TEXT,
    created_at TEXT,
    UNIQUE(owner_type, owner_id, model)
);

-- ---------- 设置 ----------
CREATE TABLE IF NOT EXISTS settings (k TEXT PRIMARY KEY, v TEXT);
"""

# JSON 字段：读出时自动反序列化
_JSON_FIELDS = (
    "reasons", "risks", "hits", "miss", "preferred_hit",
    "breakdown", "aliases", "tool_calls", "args",
)


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# ============================================================
# 连接与迁移
# ============================================================

def _cols(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.DatabaseError:
        return set()


def _migrate_v01(conn: sqlite3.Connection) -> dict | None:
    """把 v0.1 的扁平 candidates 迁移为 Candidate + Application + Document。

    判定条件：存在 candidates 表但缺少 identity_key 列。
    返回迁移统计；无需迁移时返回 None。
    """
    cols = _cols(conn, "candidates")
    if not cols or "identity_key" in cols:
        return None

    stat = {"candidates": 0, "applications": 0, "documents": 0, "audit": 0}
    conn.execute("ALTER TABLE candidates RENAME TO candidates_v01")
    if _cols(conn, "audit"):
        conn.execute("ALTER TABLE audit RENAME TO audit_v01")
    conn.executescript(SCHEMA)

    from .identity import build_identity_key  # 延迟导入，避免循环依赖

    rows = conn.execute("SELECT * FROM candidates_v01").fetchall()
    id_map: dict[int, int] = {}
    for r in rows:
        d = dict(r)
        contact = d.get("contact")
        try:
            contact = json.loads(contact) if contact else {}
        except (ValueError, TypeError):
            contact = {}
        key = build_identity_key(d.get("name"), contact.get("phone"), contact.get("email"),
                                 d.get("school"), None)
        cur = conn.execute(
            """INSERT OR IGNORE INTO candidates
               (identity_key, name, edu_level, school, major, years_exp, source_first,
                first_seen_at, last_active_at, pool_status, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,'在池',?,?)""",
            (key, d.get("name"), d.get("education"), d.get("school"), d.get("major"),
             d.get("years"), d.get("source", "folder"), d.get("created_at"), d.get("updated_at"),
             d.get("created_at"), d.get("updated_at")),
        )
        cid = cur.lastrowid
        if not cid:  # 身份已存在，取既有档
            found = conn.execute("SELECT id FROM candidates WHERE identity_key = ?", (key,)).fetchone()
            cid = found["id"] if found else None
        if cid:
            id_map[d["id"]] = cid
            stat["candidates"] += 1

        doc_id = None
        if d.get("file_hash"):
            dc = conn.execute(
                """INSERT OR IGNORE INTO documents
                   (file_hash, candidate_id, file_name, file_path, raw_text, text_len,
                    parse_engine, parse_ok, created_at)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (d["file_hash"], cid, d.get("file_name"), d.get("file_path"),
                 d.get("raw_text", ""), len(d.get("raw_text") or ""),
                 d.get("extract_mode"), 0 if d.get("extract_mode") == "parse_failed" else 1,
                 d.get("created_at")),
            )
            doc_id = dc.lastrowid or None
            stat["documents"] += 1

        conn.execute(
            """INSERT INTO applications
               (candidate_id, channel, applied_at, resume_doc_id, score, tier_suggested,
                tier_final, stage, status, note, needs_review, reasons, risks, hits, miss,
                extract_mode, confidence, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?, '新投递', ?,?,?,?,?,?,?,?,?,?,?)""",
            (cid, d.get("source", "文件夹"), d.get("created_at"), doc_id, d.get("score"),
             d.get("tier_suggested"), d.get("tier_final"), d.get("status") or "待确认",
             d.get("note", ""), d.get("needs_review", 0), d.get("reasons"), d.get("risks"),
             d.get("hits"), d.get("miss"), d.get("extract_mode"), d.get("confidence"),
             d.get("created_at"), d.get("updated_at")),
        )
        stat["applications"] += 1

    if _cols(conn, "audit_v01"):
        for r in conn.execute("SELECT * FROM audit_v01").fetchall():
            d = dict(r)
            new_cid = id_map.get(d.get("candidate_id"))
            conn.execute(
                """INSERT INTO audit_log (entity, entity_id, action, before, after, operator, role, ts)
                   VALUES ('application', ?, ?, ?, ?, ?, 'HR', ?)""",
                (str(new_cid) if new_cid else str(d.get("candidate_id")), d.get("action"),
                 d.get("before", ""), d.get("after", ""), d.get("operator"), d.get("ts")),
            )
            stat["audit"] += 1
    conn.commit()
    return stat


def connect(db_path: str) -> sqlite3.Connection:
    """打开连接：自动建表、自动迁移 v0.1 数据、自动补列。"""
    directory = os.path.dirname(os.path.abspath(db_path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    migration = _migrate_v01(conn)
    # 补列必须早于 executescript：SCHEMA 里的 `CREATE INDEX ... ON jobs(active, id)`
    # 依赖新增列，而 CREATE TABLE IF NOT EXISTS 不会给老表加列。
    _ensure_columns(conn)
    conn.executescript(SCHEMA)
    # 再补一次：全新库第一遍跑补列时表还不存在（`ALTER TABLE` 会因"表不存在"跳过），
    # executescript 建表之后这一遍才真正补得上。**新列仍必须同时写进 SCHEMA**——
    # 这里只是兜底，不是"可以不写建表语句"的许可（教训见缺陷 #39）。
    _ensure_columns(conn)
    conn.commit()
    if migration:
        _MIGRATIONS[id(conn)] = migration
    return conn


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """为已存在的旧表补上后加列（幂等）。

    SQLite 的 `CREATE TABLE IF NOT EXISTS` 不会给已存在的表加列，
    因此新增列必须靠 `ALTER TABLE ... ADD COLUMN` 显式补齐，
    否则升级后的老库会缺列、在运行到查询时才报 `no such column`。
    """
    _ADD = {
        "jobs": {
            "dept_id": "INTEGER",
            "active": "INTEGER DEFAULT 1",
        },
        # 候选人软归档（v1.4 试用反馈）：归档后不再出现在人才库与检索，
        # 集中在「归档」页展示，可随时取消——不是删除，历史投递全保留。
        "candidates": {
            "archived_at": "TEXT",
        },
        # v1.5：待指定投递的「建议岗位」（轮询在招岗位取最适者，只建议不归岗）
        "applications": {
            "suggested_job_id": "INTEGER",
        },
    }
    for table, cols in _ADD.items():
        have = _cols(conn, table)
        if not have:
            continue
        for col, ddl in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")


# sqlite3.Connection 不允许挂自定义属性，故用模块级字典暂存迁移报告
_MIGRATIONS: dict[int, dict] = {}


def migration_report(conn: sqlite3.Connection) -> dict | None:
    """取出并清除本次连接的迁移报告（用完即抛，避免 id 复用误报）。"""
    return _MIGRATIONS.pop(id(conn), None)


def _json_obj(raw):
    """把 TEXT 列里的 JSON 对象还原成 dict。

    `jobs.jd_json` 落库时是 `json.dumps` 出来的文本，直接用会让调用方
    在 `jd.get(...)` 上炸出 `'str' object has no attribute 'get'`。
    坏数据/非对象一律给空 dict，保证调用方不用判空。
    """
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            v = json.loads(raw)
            return v if isinstance(v, dict) else {}
        except (ValueError, TypeError):
            return {}
    return {}


def _decode(d: dict) -> dict:
    if "jd_json" in d:
        d["jd_json"] = _json_obj(d["jd_json"])
    for k in _JSON_FIELDS:
        if k in d and isinstance(d[k], str):
            try:
                d[k] = json.loads(d[k])
            except (ValueError, TypeError):
                d[k] = [] if k not in ("breakdown", "args") else {}
    for k in ("needs_review", "has_attachment", "processed", "active", "blind", "verified", "parse_ok"):
        if k in d and d[k] is not None:
            d[k] = bool(d[k])
    return d


def _row(r: sqlite3.Row | None) -> dict | None:
    return _decode(dict(r)) if r else None


# ============================================================
# 部门
# ============================================================

def upsert_department(conn: sqlite3.Connection, name: str, description: str = "",
                      operator: str = "HR") -> int:
    """新建部门；同名则复用（幂等）。返回部门 id。"""
    name = (name or "").strip()
    if not name:
        raise ValueError("部门名称不能为空")
    stamp = now()
    row = conn.execute("SELECT id FROM departments WHERE name = ?", (name,)).fetchone()
    if row:
        conn.execute("UPDATE departments SET description = ?, active = 1, updated_at = ? WHERE id = ?",
                     (description or "", stamp, row["id"]))
        conn.commit()
        return row["id"]
    cur = conn.execute(
        "INSERT INTO departments (name, description, active, created_at, updated_at) VALUES (?,?,1,?,?)",
        (name, description or "", stamp, stamp),
    )
    conn.commit()
    add_audit(conn, "department", str(cur.lastrowid), "create", "", name, operator, "hr")
    return cur.lastrowid


def get_department(conn: sqlite3.Connection, did: int) -> dict | None:
    return _row(conn.execute("SELECT * FROM departments WHERE id = ?", (did,)).fetchone())


def list_departments(conn: sqlite3.Connection, include_inactive: bool = True) -> list[dict]:
    rows = [_row(r) for r in conn.execute("SELECT * FROM departments ORDER BY id").fetchall()]
    for d in rows:
        d["jobs_count"] = conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE dept_id = ?", (d["id"],)).fetchone()["n"]
    return rows if include_inactive else [d for d in rows if d.get("active", 1)]


def set_department_active(conn: sqlite3.Connection, did: int, active: bool,
                          operator: str = "HR") -> dict | None:
    """停用/启用部门。停用**不物理删除**（只改 active 标记）。"""
    d = get_department(conn, did)
    if not d:
        return None
    conn.execute("UPDATE departments SET active = ?, updated_at = ? WHERE id = ?",
                 (1 if active else 0, now(), did))
    conn.commit()
    add_audit(conn, "department", str(did), "deactivate" if not active else "activate",
              d.get("name"), d.get("name"), operator, "hr")
    return get_department(conn, did)


# ============================================================
# 岗位
# ============================================================

def upsert_job(conn: sqlite3.Connection, jd: dict, title: str | None = None,
               dept: str | None = None, owner: str = "HR") -> int:
    title = title or jd.get("role") or "未命名岗位"
    dept = dept or jd.get("department") or ""
    row = conn.execute("SELECT id FROM jobs WHERE title = ? AND dept = ?", (title, dept)).fetchone()
    stamp = now()
    if row:
        conn.execute("UPDATE jobs SET jd_json = ?, active = 1, updated_at = ? WHERE id = ?",
                     (json.dumps(jd, ensure_ascii=False), stamp, row["id"]))
        conn.commit()
        return row["id"]
    cur = conn.execute(
        """INSERT INTO jobs (title, dept, jd_json, owner, status, active, opened_at, created_at, updated_at)
           VALUES (?,?,?,?,'开放',1,?,?,?)""",
        (title, dept, json.dumps(jd, ensure_ascii=False), owner, stamp, stamp, stamp),
    )
    conn.commit()
    return cur.lastrowid


def create_job(conn: sqlite3.Connection, title: str, dept_id: int | None = None,
               jd: dict | None = None, owner: str = "HR",
               operator: str = "HR") -> int:
    """创建岗位。**部门是可选的**：`dept_id=None` 时 `jobs.dept` 留空字符串，
    岗位照常打分、归岗、重算——部门只影响展示，不参与任何判定。
    jd_json 由调用方提供（默认用通用 JD 模板）。"""
    title = (title or "").strip()
    if not title:
        raise ValueError("岗位名称不能为空")
    dept_name = ""
    if dept_id:
        d = get_department(conn, dept_id)
        dept_name = d.get("name") if d else ""
    row = conn.execute("SELECT id FROM jobs WHERE title = ? AND dept = ?",
                       (title, dept_name)).fetchone()
    stamp = now()
    if row:
        conn.execute(
            "UPDATE jobs SET dept_id = ?, jd_json = ?, active = 1, status = '开放',"
            " closed_at = NULL, updated_at = ? WHERE id = ?",
            (dept_id, json.dumps(jd or {}, ensure_ascii=False), stamp, row["id"]))
        conn.commit()
        return row["id"]
    cur = conn.execute(
        """INSERT INTO jobs (title, dept, dept_id, jd_json, owner, status, active,
                             opened_at, created_at, updated_at)
           VALUES (?,?,?,?,?,'开放',1,?,?,?)""",
        (title, dept_name, dept_id, json.dumps(jd or {}, ensure_ascii=False), owner,
         stamp, stamp, stamp),
    )
    conn.commit()
    add_audit(conn, "job", str(cur.lastrowid), "create", "", title, operator, "hr")
    return cur.lastrowid


def _enrich_job(conn: sqlite3.Connection, j: dict, with_counts: bool = True) -> dict:
    """补上部门名/部门是否停用/投递数。

    `get_job` 与 `list_jobs` 必须补同一批字段：曾经只有 `list_jobs` 补，
    导致「新建岗位」接口的响应里没有 department_name，前端拿到就显示空白。
    """
    j["department_name"] = ""
    j["department_active"] = True
    if j.get("dept_id"):
        d = get_department(conn, j["dept_id"])
        if d:
            j["department_name"] = d.get("name") or ""
            j["department_active"] = bool(d.get("active", 1))
        else:
            j["department_name"] = j.get("dept") or ""
    else:
        j["department_name"] = j.get("dept") or ""
    if with_counts:
        j["applications_count"] = conn.execute(
            "SELECT COUNT(*) AS n FROM applications WHERE job_id = ?", (j["id"],)).fetchone()["n"]
    return j


def get_job(conn: sqlite3.Connection, jid: int) -> dict | None:
    row = _row(conn.execute("SELECT * FROM jobs WHERE id = ?", (jid,)).fetchone())
    return _enrich_job(conn, row) if row else None


def list_jobs(conn: sqlite3.Connection, include_inactive: bool = True) -> list[dict]:
    rows = [_enrich_job(conn, _row(r))
            for r in conn.execute("SELECT * FROM jobs ORDER BY id").fetchall()]
    if include_inactive:
        return rows
    # 部门停用 → 其下岗位一并视为不可投递：否则会出现「部门已关，简历还往这个部门的岗位里归」
    return [j for j in rows if j.get("active", 1) and j.get("department_active", True)]


def routable_job_ids(conn: sqlite3.Connection) -> set[int]:
    """当前可接收标题归岗的岗位 id 集合（岗位在招 且 所属部门未停用）。"""
    return {j["id"] for j in list_jobs(conn, include_inactive=False)}


def open_jobs_with_jd(conn: sqlite3.Connection) -> list[dict]:
    """当前在招岗位 + 已解析的 JD（`jd_json` 在数据层已解码为 dict）。

    入库试算（`ingest`）与存量重算（`regrade`）共用同一份口径：
    两处若各写一遍"哪些岗位算在招"，迟早会出现"入库时按 3 个岗位试算、
    重算时按 2 个"这类对不上的情况。
    """
    out = []
    for j in list_jobs(conn, include_inactive=False):
        jd = j.get("jd_json") or {}
        out.append({"id": j["id"], "title": j.get("title") or "",
                    "dept": j.get("department_name") or j.get("dept") or "",
                    "jd": jd if isinstance(jd, dict) else {}})
    return out


def update_job_jd(conn: sqlite3.Connection, jid: int, jd: dict,
                  operator: str = "HR") -> dict | None:
    """更新岗位 JD 尺子。只影响之后新入库的投递评分，不改动历史档位。"""
    j = get_job(conn, jid)
    if not j:
        return None
    conn.execute("UPDATE jobs SET jd_json = ?, updated_at = ? WHERE id = ?",
                 (json.dumps(jd or {}, ensure_ascii=False), now(), jid))
    conn.commit()
    must = (jd or {}).get("must") or {}
    add_audit(conn, "job", str(jid), "update_jd",
              json.dumps((j.get("jd_json") or {}).get("must") or {}, ensure_ascii=False),
              json.dumps(must, ensure_ascii=False), operator, "hr")
    return get_job(conn, jid)


def set_job_active(conn: sqlite3.Connection, jid: int, active: bool,
                   operator: str = "HR") -> dict | None:
    """停用/启用岗位。**停用不物理删除**（有投递的岗位只能停用，不能删）。"""
    j = get_job(conn, jid)
    if not j:
        return None
    conn.execute(
        "UPDATE jobs SET active = ?, status = ?, closed_at = ?, updated_at = ? WHERE id = ?",
        (1 if active else 0, "开放" if active else "停用",
         None if active else now(), now(), jid),
    )
    conn.commit()
    add_audit(conn, "job", str(jid), "deactivate" if not active else "activate",
              j.get("title"), j.get("title"), operator, "hr")
    return get_job(conn, jid)


def job_of(conn: sqlite3.Connection, jid: int | None) -> dict:
    """取岗位的 JD 字典；缺失时返回空壳，保证调用方不用判空。"""
    if not jid:
        return {}
    j = get_job(conn, jid)
    if not j:
        return {}
    return j.get("jd_json") or {}


def _fill_jd_meta(jd: dict, job: dict) -> dict:
    """把岗位行上的名称/部门回填进 JD 字典（v1.6）。

    `jd_json` 是**创建岗位时的快照**，早年创建（或由 CLI/夹具写入）的岗位可能没有
    `department` 字段，于是提示词里渲染成"部门:"——模型看到空值会自己编一个部门名，
    实测把软件岗的面试题写成了"如果加入我们材料工艺所"。这里以岗位行为准补齐。
    """
    out = dict(jd)
    if not out.get("role"):
        out["role"] = job.get("title")
    if not out.get("department"):
        out["department"] = job.get("dept") or ""
    return out


def resolve_candidate_job(conn: sqlite3.Connection, cand: dict) -> tuple[dict, dict]:
    """定位候选人「对应岗位」的 JD 与来源（v1.6）。

    口径与 v1.5 归岗一致，**不再使用 `config/jd.json` 那把默认尺子**：
      1. 名下投递里已归岗（`job_id` 非空）→ 用该岗位，`source=assigned`；
      2. 否则用建议岗位（`suggested_job_id`）→ `source=suggested`；
      3. 都没有 → 返回空 JD 与 `source=none`，**由调用方如实说明"尚未归岗、无对应岗位"**。

    为什么要有这条统一口径：面试题纲、岗位匹配分析、档位解释三条路径过去各自
    直接读 `config/jd.json`（材料类），于是软件岗候选人拿到的是材料类面试题——
    **归岗口径改了、下游三条路径没跟着改**，这是本轮修掉的缺陷 #46。

    多个投递时取最近的（`candidate_detail` 的 `applications` 已按投递时间倒序）。
    """
    apps = cand.get("applications") or []
    for a in apps:
        jid = a.get("job_id")
        if not jid:
            continue
        jd = job_of(conn, jid)
        if jd:
            j = get_job(conn, jid) or {}
            return _fill_jd_meta(jd, j), {"job_id": jid, "title": j.get("title"),
                                          "dept": j.get("dept"),
                                          "source": "assigned",
                                          "why": "该投递已归到该岗位（标题归岗或 HR 采纳后确认）"}
    for a in apps:
        jid = a.get("suggested_job_id")
        if not jid:
            continue
        jd = job_of(conn, jid)
        if jd:
            j = get_job(conn, jid) or {}
            return _fill_jd_meta(jd, j), {"job_id": jid, "title": j.get("title"),
                                          "dept": j.get("dept"),
                                          "source": "suggested",
                                          "why": "该投递尚未归岗，按系统建议岗位（对全部在招岗位逐个试算取最适者）"}
    return {}, {"job_id": None, "title": None, "dept": None, "source": "none",
                "why": "该候选人名下既没有已归岗的投递，也没有可用的建议岗位"}


def skill_categories(conn: sqlite3.Connection) -> dict[str, str]:
    """技能名 → 专业大类（材料/工艺/表征/软件/管理/其他），供「专业大类匹配」使用。

    大类口径来自技能本体 `config/ontology.json`（落库在 `skills.category`）。
    返回整表映射而不是逐次查库：一次调用要对照几十个技能名，逐条 SELECT 不划算。

    **以本体为准、库表为补充**（v1.7 修）：`skills` 表里只有"曾经出现过"的技能，
    导入领域包新增的技能在**第一次被人用到之前**库里并没有行；如果只用库表，
    新领域的技能会全部落"其他"，方向判定又退化了。本体是权威口径，库表只是
    历史遗留（含当年 JD 词表带进来、本体里没有的自定义技能），两者合并、
    本体覆盖库表，才是完整口径。
    """
    from .pipeline import normalize as _nz

    out: dict[str, str] = {}
    for r in conn.execute("SELECT canonical_name, category FROM skills").fetchall():
        d = _row(r)
        if d and d.get("canonical_name"):
            out[str(d["canonical_name"])] = str(d.get("category") or "其他")
    out.update(_nz.ontology_categories())          # 本体覆盖库表
    return out


def sync_skill_categories(conn: sqlite3.Connection, cat_of: dict[str, str]) -> dict:
    """按技能本体刷新 `skills.category`（本体升级后让存量技能跟上）。

    为什么需要：`upsert_skill` 对**已存在**的技能只合并别名、**不更新分类**
    （避免每次入库都改写历史行），所以本体里新增/调整分类后，
    库里老条目的 category 会停在旧值——专业大类匹配就会继续按旧分类算。
    本函数是幂等的：跑几次结果一样。

    `cat_of` 传本体映射（`normalize.ontology_categories()`），**不是**库里的旧分类。
    """
    rows = [_row(r) for r in
            conn.execute("SELECT id, canonical_name, category FROM skills").fetchall()]
    changed = 0
    not_in_ontology: list[str] = []
    for d in rows:
        if not d:
            continue
        name = str(d.get("canonical_name"))
        want = cat_of.get(name)
        if want is None:
            # 本体里没有这条技能（多由岗位 JD 词表带进来），如实列出而不是硬塞一个大类
            not_in_ontology.append(name)
            continue
        if want != (d.get("category") or "其他"):
            conn.execute("UPDATE skills SET category = ? WHERE id = ?", (want, d["id"]))
            changed += 1
    conn.commit()
    return {"total": len(rows), "changed": changed,
            "not_in_ontology": not_in_ontology[:20],
            "not_in_ontology_count": len(not_in_ontology)}


def _job_core_title(title: str) -> str:
    """取岗位标题的「核心词」：去掉括号内容并压缩空白。

    例：『工艺工程师（钛合金 / 难熔合金方向）』→『工艺工程师』；
    『材料研发工程师』→『材料研发工程师』。用于邮件标题的宽松匹配。
    """
    import re as _re
    t = (title or "").strip()
    t = _re.sub(r"[（(【\[][^）)】\]]*[）)】\]]", "", t)
    return _re.sub(r"\s+", "", t)


def match_job_by_subject(conn: sqlite3.Connection, subject: str) -> tuple[int | None, dict]:
    """从邮件标题解析投递岗位，返回 (job_id, jd)。

    规则（由强到弱）：
    1. 活动岗位的**完整标题**出现在主题里 —— 最强命中（如主题写了全称）；
    2. 否则用**核心词**匹配（去掉括号后的标题，如「工艺工程师」）——
       覆盖主题里只写简称/带不同括号说明的常见情况；
    3. 多个命中取更「长」的标题（更具体），避免「工艺」误中「工艺工程师」与「工艺员」。

    主题不带任何岗位名 → 返回 (None, {})，即「所属岗位待指定」。

    只有**在招**岗位参与匹配：岗位被停用、或它所属的部门被停用，都不再归岗
    （否则会出现「部门已经关了，简历还在往这个部门的岗位上挂」）。
    """
    subj = (subject or "").strip()
    jobs = [j for j in list_jobs(conn, include_inactive=False) if (j.get("title") or "").strip()]
    if not subj or not jobs:
        return None, {}
    best, best_len = None, 0
    for j in jobs:
        title = (j.get("title") or "").strip()
        core = _job_core_title(title)
        for cand, weight in ((title, 2), (core, 1)):
            if not cand:
                continue
            if cand in subj and len(cand) * weight > best_len:
                best, best_len = j, len(cand) * weight
    if best:
        return best["id"], best.get("jd_json") or {}
    return None, {}


# ============================================================
# 候选人（人）
# ============================================================

def insert_candidate(conn: sqlite3.Connection, rec: dict) -> int:
    stamp = now()
    cur = conn.execute(
        """INSERT INTO candidates
           (identity_key, name, gender, birth_year, phone_enc, email_enc, phone_bidx, email_bidx,
            edu_level, school, major, years_exp, current_org, source_first,
            first_seen_at, last_active_at, pool_status, pii_level, blind, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (rec.get("identity_key"), rec.get("name"), rec.get("gender"), rec.get("birth_year"),
         rec.get("phone_enc"), rec.get("email_enc"), rec.get("phone_bidx"), rec.get("email_bidx"),
         rec.get("edu_level"), rec.get("school"), rec.get("major"), rec.get("years_exp"),
         rec.get("current_org"), rec.get("source_first"),
         rec.get("first_seen_at", stamp), rec.get("last_active_at", stamp),
         rec.get("pool_status", "在池"), rec.get("pii_level", "普通"),
         1 if rec.get("blind") else 0, stamp, stamp),
    )
    conn.commit()
    return cur.lastrowid


def find_candidate_by_identity(conn: sqlite3.Connection, identity_key: str) -> dict | None:
    return _row(conn.execute(
        "SELECT * FROM candidates WHERE identity_key = ? AND merged_into IS NULL",
        (identity_key,)).fetchone())


def find_by_bidx(conn: sqlite3.Connection, phone_bidx: str | None,
                 email_bidx: str | None) -> list[dict]:
    """用于"疑似重复"提示：手机号或邮箱命中。"""
    hits: dict[int, dict] = {}
    for col, val in (("phone_bidx", phone_bidx), ("email_bidx", email_bidx)):
        if not val:
            continue
        for r in conn.execute(
            f"SELECT * FROM candidates WHERE {col} = ? AND merged_into IS NULL", (val,)
        ).fetchall():
            hits[r["id"]] = _row(r)
    return list(hits.values())


def update_candidate(conn: sqlite3.Connection, cid: int, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = now()
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE candidates SET {sets} WHERE id = ?", (*fields.values(), cid))
    conn.commit()


def get_candidate(conn: sqlite3.Connection, cid: int) -> dict | None:
    return _row(conn.execute("SELECT * FROM candidates WHERE id = ?", (cid,)).fetchone())


_LATEST_APP_JOIN = """
SELECT c.*,
       a.id            AS application_id,
       a.job_id        AS job_id,
       a.channel       AS channel,
       a.applied_at    AS applied_at,
       a.score         AS score,
       a.tier_suggested AS tier_suggested,
       a.tier_final    AS tier_final,
       a.stage         AS stage,
       a.status        AS app_status,
       a.note          AS note,
       a.needs_review  AS needs_review,
       a.reasons       AS reasons,
       a.risks         AS risks,
       a.hits          AS hits,
       a.miss          AS miss,
       a.preferred_hit AS preferred_hit,
       a.breakdown     AS breakdown,
       a.extract_mode  AS extract_mode,
       a.confidence    AS confidence,
       a.resume_doc_id AS resume_doc_id,
       a.suggested_job_id AS suggested_job_id,
       j.title         AS job_title,
       j.active        AS job_active
FROM candidates c
LEFT JOIN applications a ON a.id = (
    SELECT id FROM applications WHERE candidate_id = c.id
    ORDER BY COALESCE(applied_at,'') DESC, id DESC LIMIT 1
)
LEFT JOIN jobs j ON j.id = a.job_id
"""


def _attach_skills(conn: sqlite3.Connection, items: list[dict]) -> list[dict]:
    if not items:
        return items
    ids = [i["id"] for i in items]
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"""SELECT cs.candidate_id, s.canonical_name, cs.level, cs.verified, cs.evidence
            FROM candidate_skills cs JOIN skills s ON s.id = cs.skill_id
            WHERE cs.candidate_id IN ({marks})
            ORDER BY cs.verified DESC, s.canonical_name""",
        ids,
    ).fetchall()
    bucket: dict[int, list[str]] = {}
    detail: dict[int, list[dict]] = {}
    for r in rows:
        bucket.setdefault(r["candidate_id"], []).append(r["canonical_name"])
        detail.setdefault(r["candidate_id"], []).append(
            {"name": r["canonical_name"], "level": r["level"],
             "verified": bool(r["verified"]), "evidence": r["evidence"]}
        )
    for i in items:
        i["skills"] = bucket.get(i["id"], [])
        i["skill_detail"] = detail.get(i["id"], [])
        i["tier_effective"] = i.get("tier_final") or i.get("tier_suggested")
    return items


def list_candidates(conn: sqlite3.Connection, tier: str | None = None, keyword: str | None = None,
                    skill: str | list[str] | None = None, min_years: int | None = None,
                    education: str | None = None, job_id: int | None = None,
                    stage: str | None = None, pool_status: str | None = None,
                    needs_review: bool | None = None, include_merged: bool = False,
                    archived: bool | None = None, limit: int | None = None) -> list[dict]:
    """`archived` 参数三态（v1.4 软归档）：
    `None`（默认）不过滤——统计、合并等内部口径保持全量，行为与旧版完全一致；
    `False` 只看未归档——人才库列表与检索走这个口径，归档的不再展示；
    `True` 只看已归档（「归档」页用）。
    """
    sql = _LATEST_APP_JOIN
    where: list[str] = []
    args: list = []
    if not include_merged:
        where.append("c.merged_into IS NULL")
    if archived is not None:
        where.append("c.archived_at IS " + ("NOT NULL" if archived else "NULL"))
    if job_id:
        where.append("a.job_id = ?")
        args.append(job_id)
    if stage:
        where.append("a.stage = ?")
        args.append(stage)
    if pool_status:
        where.append("c.pool_status = ?")
        args.append(pool_status)
    if needs_review is not None:
        where.append("a.needs_review = ?")
        args.append(1 if needs_review else 0)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY COALESCE(a.score, -1) DESC, c.id DESC"
    if limit:
        sql += f" LIMIT {int(limit)}"

    items = [_decode(dict(r)) for r in conn.execute(sql, args).fetchall()]

    if tier and tier != "ALL":
        if tier == "REVIEW":
            items = [i for i in items if i.get("needs_review")]
        elif tier == "UNCONFIRMED":
            items = [i for i in items if (i.get("app_status") or "待确认") != "已确认"]
        else:
            items = [i for i in items if (i.get("tier_final") or i.get("tier_suggested")) == tier]
    if education:
        rank = _EDU_RANK.get(education, 0)
        items = [i for i in items if _EDU_RANK.get(i.get("edu_level") or "", 0) >= rank]
    if min_years is not None:
        items = [i for i in items if (i.get("years_exp") or 0) >= int(min_years)]

    # 技能必须**先挂载再过滤**（v1.6 修复）：此前 `if skill:` 写在
    # `_attach_skills()` 之前，而 skills 字段是 `_attach_skills` 才填上的，
    # 于是 `i.get("skills", [])` 恒为空列表、`any(...)` 恒为 False——
    # 任何带技能条件的筛选都会返回 0 条（表现为"库里明明有 Java 候选人，
    # 对话却答『没有会 Java 的候选人』"）。keywords 过滤本来就在挂载之后，
    # 两处口径此前不一致，这也是它没被发现的原因。
    items = _attach_skills(conn, items)

    if skill:
        raw = [skill] if isinstance(skill, str) else list(skill)
        wants = [str(s).strip().lower() for s in raw if str(s).strip()]
        if wants:
            items = [i for i in items
                     if any(w in str(s).lower()
                            for w in wants for s in (i.get("skills") or []))]

    if keyword:
        kw = keyword.strip().lower()
        items = [
            i for i in items
            if kw in (i.get("name") or "").lower()
            or kw in (i.get("edu_level") or "").lower()
            or kw in (i.get("school") or "").lower()
            or kw in (i.get("major") or "").lower()
            or any(kw in s.lower() for s in i.get("skills", []))
        ]
    return items


_EDU_RANK = {"大专": 1, "专科": 1, "本科": 2, "学士": 2, "研究生": 3, "硕士": 3, "博士": 4}

# 性别分组（含"未标注"）。**只用于展示与筛选，绝不进入 `tier.grade()`**。
# “未标注”是一个真实分组：简历没写性别的档案不能因为筛选而凭空消失。
GENDER_GROUPS = ("男", "女", "未标注")


def gender_matches(value: str | None, want: str | None) -> bool:
    """性别筛选的**唯一口径**：分布统计与结果过滤都走它，避免两处规则不一致。"""
    if not want:
        return True
    cur = (value or "").strip()
    if want == "未标注":
        return not cur
    return cur == want


def candidate_detail(conn: sqlite3.Connection, cid: int) -> dict | None:
    """完整档案：人 + 全部投递 + 全部附件 + 技能（含证据）+ 标签。"""
    c = get_candidate(conn, cid)
    if not c:
        return None
    apps = [_decode(dict(r)) for r in conn.execute(
        """SELECT a.*, j.title AS job_title, j.dept AS job_dept
           FROM applications a LEFT JOIN jobs j ON j.id = a.job_id
           WHERE a.candidate_id = ? ORDER BY COALESCE(a.applied_at,'') DESC, a.id DESC""",
        (cid,)).fetchall()]
    docs = [_decode(dict(r)) for r in conn.execute(
        "SELECT * FROM documents WHERE candidate_id = ? ORDER BY id DESC", (cid,)).fetchall()]
    skills = [_decode(dict(r)) for r in conn.execute(
        """SELECT s.canonical_name AS name, s.category, cs.level, cs.verified,
                  cs.evidence, cs.source, cs.from_doc_id
           FROM candidate_skills cs JOIN skills s ON s.id = cs.skill_id
           WHERE cs.candidate_id = ? ORDER BY cs.verified DESC, s.category, s.canonical_name""",
        (cid,)).fetchall()]
    tags = [_decode(dict(r)) for r in conn.execute(
        """SELECT t.name, t.category, ct.evidence, ct.source
           FROM candidate_tags ct JOIN tags t ON t.id = ct.tag_id
           WHERE ct.candidate_id = ? ORDER BY t.category, t.name""",
        (cid,)).fetchall()]
    # 命中/缺失来自最近一次投递
    latest = apps[0] if apps else {}
    return {
        **c,
        "applications": apps,
        "documents": docs,
        "skills": skills,
        "tags": tags,
        "latest": latest,
        "tier_suggested": latest.get("tier_suggested"),
        "tier_final": latest.get("tier_final"),
        "score": latest.get("score"),
        "stage": latest.get("stage"),
        "status": latest.get("status"),
        "note": latest.get("note"),
        "needs_review": latest.get("needs_review"),
        "reasons": latest.get("reasons") or [],
        "risks": latest.get("risks") or [],
        "hits": latest.get("hits") or [],
        "miss": latest.get("miss") or [],
        "raw_text": (docs[0].get("raw_text") if docs else "") or "",
        "file_name": docs[0].get("file_name") if docs else None,
        "file_path": docs[0].get("archived_path") or (docs[0].get("file_path") if docs else None),
        "tier_effective": latest.get("tier_final") or latest.get("tier_suggested"),
    }


def merge_candidates(conn: sqlite3.Connection, src_id: int, dst_id: int,
                     operator: str = "HR", role: str = "recruiter") -> dict:
    """软合并：把 src 的投递/文档/技能/标签挂到 dst，src 标记 merged_into（可拆）。"""
    if src_id == dst_id:
        return {"ok": False, "error": "不能与自身合并"}
    src, dst = get_candidate(conn, src_id), get_candidate(conn, dst_id)
    if not src or not dst:
        return {"ok": False, "error": "候选人不存在"}
    conn.execute("UPDATE applications SET candidate_id = ?, updated_at = ? WHERE candidate_id = ?",
                 (dst_id, now(), src_id))
    conn.execute("UPDATE documents SET candidate_id = ? WHERE candidate_id = ?", (dst_id, src_id))
    moved = {"applications": conn.total_changes}
    # 技能：冲突时保留已有，其余迁移
    conn.execute(
        """INSERT OR IGNORE INTO candidate_skills
           (candidate_id, skill_id, level, evidence, verified, from_doc_id, source, created_at)
           SELECT ?, skill_id, level, evidence, verified, from_doc_id, source, created_at
           FROM candidate_skills WHERE candidate_id = ?""",
        (dst_id, src_id),
    )
    conn.execute(
        """INSERT OR IGNORE INTO candidate_tags (candidate_id, tag_id, evidence, source, created_at)
           SELECT ?, tag_id, evidence, source, created_at FROM candidate_tags WHERE candidate_id = ?""",
        (dst_id, src_id),
    )
    conn.execute("UPDATE candidates SET merged_into = ?, pool_status = '已归档', updated_at = ? WHERE id = ?",
                 (dst_id, now(), src_id))
    add_audit(conn, "candidate", str(dst_id), "merge",
              f"源档 #{src_id} {src.get('name')}", f"并入本档 #{dst_id} {dst.get('name')}",
              operator, role)
    conn.commit()
    return {"ok": True, "merged_into": dst_id, "src": src_id}


def split_candidate(conn: sqlite3.Connection, src_id: int, operator: str = "HR",
                    role: str = "recruiter") -> dict:
    """撤销软合并。"""
    conn.execute("UPDATE candidates SET merged_into = NULL, pool_status = '在池', updated_at = ? WHERE id = ?",
                 (now(), src_id))
    add_audit(conn, "candidate", str(src_id), "split", "已并入其他档", "恢复为独立档", operator, role)
    conn.commit()
    return {"ok": True, "id": src_id}


def suspicious_duplicates(conn: sqlite3.Connection, cid: int) -> list[dict]:
    """给出"疑似重复"提示（不自动合并）。同姓名或同院校+专业。"""
    me = get_candidate(conn, cid)
    if not me:
        return []
    out: list[dict] = []
    if me.get("name"):
        for r in conn.execute(
            "SELECT id, name, school, major, edu_level FROM candidates "
            "WHERE name = ? AND id != ? AND merged_into IS NULL", (me["name"], cid)
        ).fetchall():
            out.append({**_decode(dict(r)), "reason": "姓名相同"})
    if me.get("school") and me.get("major"):
        for r in conn.execute(
            "SELECT id, name, school, major, edu_level FROM candidates "
            "WHERE school = ? AND major = ? AND id != ? AND merged_into IS NULL",
            (me["school"], me["major"], cid),
        ).fetchall():
            if not any(o["id"] == r["id"] for o in out):
                out.append({**_decode(dict(r)), "reason": "同院校同专业"})
    return out


# ============================================================
# 投递
# ============================================================

def insert_application(conn: sqlite3.Connection, rec: dict) -> int:
    stamp = now()
    cur = conn.execute(
        """INSERT INTO applications
           (candidate_id, job_id, suggested_job_id, channel, applied_at, resume_doc_id,
            score, tier_suggested,
            tier_final, stage, status, note, needs_review, reasons, risks, hits, miss,
            preferred_hit, breakdown, extract_mode, confidence, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (rec.get("candidate_id"), rec.get("job_id"), rec.get("suggested_job_id"),
         rec.get("channel", "文件夹"),
         rec.get("applied_at", stamp), rec.get("resume_doc_id"), rec.get("score"),
         rec.get("tier_suggested"), None, rec.get("stage", "新投递"),
         rec.get("status", "待确认"), rec.get("note", ""),
         1 if rec.get("needs_review") else 0,
         json.dumps(rec.get("reasons", []), ensure_ascii=False),
         json.dumps(rec.get("risks", []), ensure_ascii=False),
         json.dumps(rec.get("hit", rec.get("hits", [])), ensure_ascii=False),
         json.dumps(rec.get("miss", []), ensure_ascii=False),
         json.dumps(rec.get("preferred_hit", []), ensure_ascii=False),
         json.dumps(rec.get("breakdown", {}), ensure_ascii=False),
         rec.get("extract_mode"), rec.get("confidence"), stamp, stamp),
    )
    conn.commit()
    return cur.lastrowid


def get_application(conn: sqlite3.Connection, aid: int) -> dict | None:
    return _row(conn.execute("SELECT * FROM applications WHERE id = ?", (aid,)).fetchone())


def latest_application(conn: sqlite3.Connection, cid: int) -> dict | None:
    return _row(conn.execute(
        "SELECT * FROM applications WHERE candidate_id = ? "
        "ORDER BY COALESCE(applied_at,'') DESC, id DESC LIMIT 1", (cid,)).fetchone())


def list_applications(conn: sqlite3.Connection, cid: int | None = None,
                      job_id: int | None = None, stage: str | None = None) -> list[dict]:
    sql = ("SELECT a.*, c.name AS candidate_name, j.title AS job_title "
           "FROM applications a LEFT JOIN candidates c ON c.id = a.candidate_id "
           "LEFT JOIN jobs j ON j.id = a.job_id")
    where, args = [], []
    if cid:
        where.append("a.candidate_id = ?")
        args.append(cid)
    if job_id:
        where.append("a.job_id = ?")
        args.append(job_id)
    if stage:
        where.append("a.stage = ?")
        args.append(stage)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY COALESCE(a.applied_at,'') DESC, a.id DESC"
    return [_decode(dict(r)) for r in conn.execute(sql, args).fetchall()]


def set_application_tier(conn: sqlite3.Connection, aid: int, tier: str,
                         note: str | None = None, operator: str = "HR",
                         role: str = "recruiter") -> dict | None:
    cur = get_application(conn, aid)
    if not cur:
        return None
    before = cur.get("tier_final") or cur.get("tier_suggested")
    note_val = cur.get("note", "") if note is None else note
    conn.execute(
        "UPDATE applications SET tier_final = ?, status = '已确认', note = ?, updated_at = ? WHERE id = ?",
        (tier, note_val, now(), aid),
    )
    add_audit(conn, "application", str(aid), "set_tier", str(before), str(tier), operator, role)
    conn.commit()
    return get_application(conn, aid)


def set_application_stage(conn: sqlite3.Connection, aid: int, stage: str,
                          operator: str = "HR", role: str = "recruiter") -> dict | None:
    cur = get_application(conn, aid)
    if not cur:
        return None
    conn.execute("UPDATE applications SET stage = ?, updated_at = ? WHERE id = ?", (stage, now(), aid))
    add_audit(conn, "application", str(aid), "set_stage", cur.get("stage"), stage, operator, role)
    conn.commit()
    return get_application(conn, aid)


def set_candidate_archived(conn: sqlite3.Connection, cid: int, archived: bool,
                           operator: str = "HR", role: str = "hr") -> dict | None:
    """候选人软归档（v1.4）：归档后不再出现在人才库与检索，集中在「归档」页展示。

    是"归档"不是"删除"：档案、投递、附件、审计全部原样保留，可随时取消归档。
    逐次写审计（archive / unarchive），谁在什么时候归档的可追溯。
    """
    cur = get_candidate(conn, cid)
    if not cur:
        return None
    ts = now() if archived else None
    conn.execute("UPDATE candidates SET archived_at = ? WHERE id = ?", (ts, cid))
    add_audit(conn, "candidate", str(cid), "archive" if archived else "unarchive",
              "未归档" if archived else "已归档",
              "已归档（移入「归档」页，不再在人才库与检索中展示）" if archived
              else "取消归档（恢复在人才库与检索中展示）", operator, role)
    conn.commit()
    return get_candidate(conn, cid)


def set_candidates_archived(conn: sqlite3.Connection, cids: list[int], archived: bool,
                            operator: str = "HR", role: str = "hr") -> dict:
    """批量归档 / 批量取消归档（v1.5）。

    为什么要批量：这套系统**按年使用**——第二年的简历进来时，上一年的档案仍留在
    人才库里会和新人混在一起；逐条点归档不现实，所以给"一次归档一批"的入口
    （界面上是勾选多人，或按投递年份整批归档）。

    逐人写审计（与单条归档同一套动作名 archive / unarchive），
    返回 `{changed, skipped, ids}`；不存在的 id 计入 skipped，不整体失败——
    避免"列表是几分钟前刷的、其中一条已被别的操作删掉"把整批动作打断。
    """
    changed: list[int] = []
    skipped: list[int] = []
    for cid in cids:
        cur = get_candidate(conn, cid)
        if not cur:
            skipped.append(cid)
            continue
        ts = now() if archived else None
        if (cur.get("archived_at") or "") and archived:
            skipped.append(cid)          # 已归档的不重复写审计
            continue
        conn.execute("UPDATE candidates SET archived_at = ? WHERE id = ?", (ts, cid))
        add_audit(conn, "candidate", str(cid), "archive" if archived else "unarchive",
                  "未归档" if archived else "已归档",
                  "批量归档（移入「归档」页，不再在人才库与检索中展示）" if archived
                  else "批量取消归档（恢复在人才库与检索中展示）", operator, role)
        changed.append(cid)
    conn.commit()
    return {"changed": len(changed), "skipped": len(skipped),
            "ids": changed, "skipped_ids": skipped, "archived": bool(archived)}


#: 归档满多少天后**彻底删除**（试用反馈："删除那个应该做成 30 天后彻底删除"）。
#: 设成 0 表示关闭自动清理（只保留手动删除入口）。
PURGE_AFTER_DAYS = 30


def purge_due_candidates(conn: sqlite3.Connection, days: int | None = None,
                         operator: str = "system", role: str = "system") -> dict:
    """把**归档已满 `days` 天**的档案彻底删除（连带投递、附件记录、标签、技能、向量）。

    设计取舍：
    - **删除的前提是"先归档满 30 天"**，不是随手一点就删。归档是缓冲期，
      这 30 天里 HR 随时能取消归档把人捞回来；期满才真正不可恢复。
    - 只删数据库记录，**原件区文件由调用方回收**（`file_path` / `archived_path`
      随返回值一起给出，服务层把它们移进回收目录 `data/removed/`），
      这样"库删了、文件还在回收目录里"——比直接 `unlink` 安全。
    - 每个被删除的人写一条 `purge` 审计（含归档时间与投递/附件数量），
      审计本身**不删除**——"谁什么时候被清掉的"必须留痕。
    """
    days = PURGE_AFTER_DAYS if days is None else days
    if days <= 0:
        return {"purged": 0, "days": days, "people": [], "files": [], "disabled": True}
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    rows = conn.execute(
        "SELECT id, name, archived_at FROM candidates "
        "WHERE COALESCE(archived_at,'') != '' AND archived_at <= ? ORDER BY id",
        (cutoff,)).fetchall()
    people: list[dict] = []
    files: list[str] = []
    for r in rows:
        cid = r["id"]
        docs = conn.execute(
            "SELECT file_name, file_path, archived_path FROM documents WHERE candidate_id = ?",
            (cid,)).fetchall()
        n_app = conn.execute("SELECT COUNT(*) AS n FROM applications WHERE candidate_id = ?",
                             (cid,)).fetchone()["n"]
        n_doc = len(docs)
        for d in docs:
            for p in (d["archived_path"], d["file_path"]):
                if p and p not in files:
                    files.append(p)
        conn.execute("DELETE FROM candidate_skills WHERE candidate_id = ?", (cid,))
        conn.execute("DELETE FROM candidate_tags WHERE candidate_id = ?", (cid,))
        conn.execute("DELETE FROM embeddings WHERE owner_type = 'candidate' AND owner_id = ?",
                     (cid,))
        conn.execute("DELETE FROM documents WHERE candidate_id = ?", (cid,))
        conn.execute("DELETE FROM applications WHERE candidate_id = ?", (cid,))
        conn.execute("DELETE FROM candidates WHERE id = ?", (cid,))
        add_audit(conn, "candidate", str(cid), "purge",
                  f"{r['name']}（{r['archived_at']} 归档）",
                  f"归档满 {days} 天彻底删除：{n_app} 条投递、{n_doc} 份附件（原件移入回收目录）",
                  operator, role)
        people.append({"id": cid, "name": r["name"], "archived_at": r["archived_at"],
                       "applications": n_app, "documents": n_doc})
    conn.commit()
    return {"purged": len(people), "days": days, "cutoff": cutoff,
            "people": people, "files": files}


def archive_meta(conn: sqlite3.Connection, days: int | None = None) -> dict:
    """归档页用的口径：每人的归档时间、已归档天数、还有几天被彻底删除。

    天数同时给 `days_left`（前端直接展示），`purge_at` 是到期时间点——
    让界面能写"将于 X 月 X 日彻底删除"，而不是让 HR 自己算。
    """
    days = PURGE_AFTER_DAYS if days is None else days
    rows = conn.execute(
        "SELECT id, archived_at FROM candidates WHERE COALESCE(archived_at,'') != ''").fetchall()
    out: dict[int, dict] = {}
    for r in rows:
        left = None
        if days > 0 and r["archived_at"]:
            try:
                ts = datetime.strptime(r["archived_at"][:19], "%Y-%m-%d %H:%M:%S")
                left = max(0, days - (datetime.now() - ts).days)
                due = (ts + timedelta(days=days)).strftime("%Y-%m-%d")
            except ValueError:
                due = ""
        else:
            due = ""
        out[r["id"]] = {"archived_at": r["archived_at"], "days_left": left,
                        "purge_at": due, "purge_after_days": days}
    return out


def add_note(conn: sqlite3.Connection, aid: int, note: str, operator: str = "HR",
             role: str = "recruiter") -> dict | None:
    cur = get_application(conn, aid)
    if not cur:
        return None
    conn.execute("UPDATE applications SET note = ?, updated_at = ? WHERE id = ?", (note, now(), aid))
    add_audit(conn, "application", str(aid), "add_note", cur.get("note", ""), note, operator, role)
    conn.commit()
    return get_application(conn, aid)


# ============================================================
# 附件（原件）
# ============================================================

def exists_hash(conn: sqlite3.Connection, file_hash: str) -> bool:
    return conn.execute("SELECT 1 FROM documents WHERE file_hash = ?", (file_hash,)).fetchone() is not None


def insert_document(conn: sqlite3.Connection, rec: dict) -> int:
    cur = conn.execute(
        """INSERT OR IGNORE INTO documents
           (file_hash, candidate_id, application_id, file_name, file_path, archived_path,
            mime, size, received_at, source_message_id, raw_text, text_len,
            parse_engine, parse_ok, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (rec.get("file_hash"), rec.get("candidate_id"), rec.get("application_id"),
         rec.get("file_name"), rec.get("file_path"), rec.get("archived_path"),
         rec.get("mime"), rec.get("size"), rec.get("received_at", now()),
         rec.get("source_message_id"), rec.get("raw_text", ""), len(rec.get("raw_text") or ""),
         rec.get("parse_engine"), 1 if rec.get("parse_ok", True) else 0, now()),
    )
    conn.commit()
    return cur.lastrowid


def update_document(conn: sqlite3.Connection, did: int, **fields) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE documents SET {sets} WHERE id = ?", (*fields.values(), did))
    conn.commit()


def get_document(conn: sqlite3.Connection, did: int) -> dict | None:
    return _row(conn.execute("SELECT * FROM documents WHERE id = ?", (did,)).fetchone())


def list_documents(conn: sqlite3.Connection, candidate_id: int | None = None,
                   parse_ok: bool | None = None) -> list[dict]:
    sql, args = "SELECT * FROM documents", []
    where = []
    if candidate_id:
        where.append("candidate_id = ?")
        args.append(candidate_id)
    if parse_ok is not None:
        where.append("parse_ok = ?")
        args.append(1 if parse_ok else 0)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC"
    return [_decode(dict(r)) for r in conn.execute(sql, args).fetchall()]


# ============================================================
# 技能本体
# ============================================================

def upsert_skill(conn: sqlite3.Connection, canonical: str, category: str = "其他",
                 aliases: list[str] | None = None) -> int:
    row = conn.execute("SELECT id, aliases FROM skills WHERE canonical_name = ?", (canonical,)).fetchone()
    if row:
        if aliases:
            try:
                existing = set(json.loads(row["aliases"] or "[]"))
            except (ValueError, TypeError):
                existing = set()
            merged = sorted(existing | set(aliases))
            if len(merged) != len(existing):
                conn.execute("UPDATE skills SET aliases = ? WHERE id = ?",
                             (json.dumps(merged, ensure_ascii=False), row["id"]))
                conn.commit()
        return row["id"]
    cur = conn.execute(
        "INSERT INTO skills (canonical_name, aliases, category) VALUES (?,?,?)",
        (canonical, json.dumps(aliases or [], ensure_ascii=False), category),
    )
    conn.commit()
    return cur.lastrowid


def find_skill(conn: sqlite3.Connection, name: str) -> dict | None:
    """按本体名或别名精确查找。"""
    row = conn.execute("SELECT * FROM skills WHERE canonical_name = ?", (name,)).fetchone()
    if row:
        return _decode(dict(row))
    for r in conn.execute("SELECT * FROM skills").fetchall():
        d = _decode(dict(r))
        if name in (d.get("aliases") or []):
            return d
    return None


def link_skill(conn: sqlite3.Connection, candidate_id: int, skill_id: int,
               level: str | None = None, evidence: str | None = None,
               from_doc_id: int | None = None, source: str = "rule") -> None:
    """挂技能到候选人。**没有原文片段时 verified=0**（不计入岗位命中）。"""
    verified = 1 if (evidence or "").strip() else 0
    conn.execute(
        """INSERT INTO candidate_skills
           (candidate_id, skill_id, level, evidence, verified, from_doc_id, source, created_at)
           VALUES (?,?,?,?,?,?,?,?)
           ON CONFLICT(candidate_id, skill_id) DO UPDATE SET
             level = COALESCE(excluded.level, level),
             evidence = CASE WHEN LENGTH(excluded.evidence) > LENGTH(COALESCE(evidence,''))
                             THEN excluded.evidence ELSE evidence END,
             verified = MAX(verified, excluded.verified),
             source = excluded.source""",
        (candidate_id, skill_id, level, evidence, verified, from_doc_id, source, now()),
    )
    conn.commit()


def candidate_skill_rows(conn: sqlite3.Connection, candidate_id: int,
                         verified_only: bool = False) -> list[dict]:
    sql = ("SELECT s.id, s.canonical_name AS name, s.category, cs.level, cs.verified, "
           "cs.evidence, cs.source FROM candidate_skills cs JOIN skills s ON s.id = cs.skill_id "
           "WHERE cs.candidate_id = ?")
    if verified_only:
        sql += " AND cs.verified = 1"
    sql += " ORDER BY s.category, s.canonical_name"
    return [_decode(dict(r)) for r in conn.execute(sql, (candidate_id,)).fetchall()]


def candidate_skill_names(conn: sqlite3.Connection, candidate_id: int,
                          verified_only: bool = True) -> list[str]:
    return [r["name"] for r in candidate_skill_rows(conn, candidate_id, verified_only)]


def search_candidates_by_skills(conn: sqlite3.Connection, names: list[str],
                                mode: str = "all", verified_only: bool = True) -> list[dict]:
    """按技能本体名检索候选人。mode: all=全中 / any=任一命中。"""
    want = {n.strip() for n in names if n and n.strip()}
    if not want:
        return []
    ids = [
        r["id"] for r in conn.execute(
            "SELECT id FROM candidates WHERE merged_into IS NULL").fetchall()
    ]
    out = []
    for cid in ids:
        have = set(candidate_skill_names(conn, cid, verified_only))
        ok = want <= have if mode == "all" else bool(want & have)
        if ok:
            out.append(cid)
    if not out:
        return []
    wanted = set(out)
    return [c for c in list_candidates(conn) if c["id"] in wanted]


# ============================================================
# 标签
# ============================================================

def upsert_tag(conn: sqlite3.Connection, name: str, category: str = "能力") -> int:
    row = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute("INSERT INTO tags (name, category) VALUES (?,?)", (name, category))
    conn.commit()
    return cur.lastrowid


def link_tag(conn: sqlite3.Connection, candidate_id: int, tag_id: int,
             evidence: str | None = None, source: str = "rule") -> None:
    conn.execute(
        """INSERT OR IGNORE INTO candidate_tags (candidate_id, tag_id, evidence, source, created_at)
           VALUES (?,?,?,?,?)""",
        (candidate_id, tag_id, evidence, source, now()),
    )
    conn.commit()


def candidate_tag_rows(conn: sqlite3.Connection, candidate_id: int) -> list[dict]:
    return [_decode(dict(r)) for r in conn.execute(
        """SELECT t.name, t.category, ct.evidence, ct.source
           FROM candidate_tags ct JOIN tags t ON t.id = ct.tag_id
           WHERE ct.candidate_id = ? ORDER BY t.category, t.name""", (candidate_id,)).fetchall()]


def list_tags(conn: sqlite3.Connection) -> list[dict]:
    return [_decode(dict(r)) for r in conn.execute(
        """SELECT t.*, (SELECT COUNT(*) FROM candidate_tags ct WHERE ct.tag_id = t.id) AS uses
           FROM tags t ORDER BY uses DESC, t.name""").fetchall()]


# ============================================================
# 审计
# ============================================================

def add_audit(conn: sqlite3.Connection, entity: str, entity_id: str, action: str,
              before: str = "", after: str = "", operator: str = "system",
              role: str = "system", ip: str | None = None) -> None:
    conn.execute(
        """INSERT INTO audit_log (entity, entity_id, action, before, after, operator, role, ip, ts)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (entity, str(entity_id), action, str(before), str(after), operator, role, ip, now()),
    )
    conn.commit()


def list_audit(conn: sqlite3.Connection, entity: str | None = None,
               entity_id: str | None = None, limit: int = 100) -> list[dict]:
    sql, args = "SELECT * FROM audit_log", []
    where = []
    if entity:
        where.append("entity = ?")
        args.append(entity)
    if entity_id:
        where.append("entity_id = ?")
        args.append(str(entity_id))
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    return [_decode(dict(r)) for r in conn.execute(sql, args).fetchall()]


def candidate_audit(conn: sqlite3.Connection, cid: int, limit: int = 50) -> list[dict]:
    """某个候选人的完整操作轨迹（档案 + 名下投递 + 涉及附件）。

    只查 `entity='candidate'` 是不够的：改档、改阶段、合并、保留 HR 结论这些动作
    都记在 `application` 上，"重复附件未重复落盘"记在 `document` 上。
    保密检查要的是"这个人的档案被谁在什么时候动过"，
    因此这里把三类实体合并成一条时间线。
    """
    keys: list[tuple[str, str]] = [("candidate", str(cid))]
    for r in conn.execute("SELECT id FROM applications WHERE candidate_id = ?",
                          (cid,)).fetchall():
        keys.append(("application", str(r["id"])))
    for r in conn.execute("SELECT file_hash FROM documents WHERE candidate_id = ?",
                          (cid,)).fetchall():
        if r["file_hash"]:
            keys.append(("document", r["file_hash"][:16]))
    clauses = " OR ".join("(entity = ? AND entity_id = ?)" for _ in keys)
    args: list = []
    for ent, eid in keys:
        args.extend([ent, eid])
    args.append(int(limit))
    sql = f"SELECT * FROM audit_log WHERE {clauses} ORDER BY id DESC LIMIT ?"
    return [_decode(dict(r)) for r in conn.execute(sql, args).fetchall()]


# ============================================================
# 提案（写入类工具的前置确认）
# ============================================================

def create_proposal(conn: sqlite3.Connection, session_id: str, tool: str, args: dict,
                    summary: str, risk: str = "中") -> int:
    cur = conn.execute(
        """INSERT INTO proposals (session_id, tool, args, summary, risk, status, created_at)
           VALUES (?,?,?,?,?,'待确认',?)""",
        (session_id, tool, json.dumps(args, ensure_ascii=False), summary, risk, now()),
    )
    conn.commit()
    return cur.lastrowid


def get_proposal(conn: sqlite3.Connection, pid: int) -> dict | None:
    return _row(conn.execute("SELECT * FROM proposals WHERE id = ?", (pid,)).fetchone())


def list_proposals(conn: sqlite3.Connection, status: str | None = None, limit: int = 50) -> list[dict]:
    sql, args = "SELECT * FROM proposals", []
    if status:
        sql += " WHERE status = ?"
        args.append(status)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    return [_decode(dict(r)) for r in conn.execute(sql, args).fetchall()]


def decide_proposal(conn: sqlite3.Connection, pid: int, decision: str,
                    operator: str = "HR", role: str = "recruiter",
                    result: str = "") -> dict | None:
    cur = get_proposal(conn, pid)
    if not cur:
        return None
    status = "已执行" if decision == "approve" else "已拒绝"
    conn.execute("UPDATE proposals SET status = ?, decided_by = ?, decided_at = ?, result = ? WHERE id = ?",
                 (status, operator, now(), result, pid))
    add_audit(conn, "proposal", str(pid), decision, cur.get("status"), status, operator, role)
    conn.commit()
    return get_proposal(conn, pid)


# ============================================================
# 收信台账
# ============================================================

def record_email(conn: sqlite3.Connection, rec: dict) -> int:
    row = conn.execute("SELECT id FROM email_messages WHERE message_id = ?",
                       (rec.get("message_id"),)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute(
        """INSERT INTO email_messages
           (message_id, uid, mailbox, from_addr, subject, received_at, has_attachment,
            attachment_count, processed, added, skipped, failed, error, fetched_at, processed_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (rec.get("message_id"), rec.get("uid"), rec.get("mailbox"), rec.get("from_addr"),
         rec.get("subject"), rec.get("received_at"),
         1 if rec.get("has_attachment") else 0, rec.get("attachment_count", 0),
         1 if rec.get("processed") else 0, rec.get("added", 0), rec.get("skipped", 0),
         rec.get("failed", 0), rec.get("error"), rec.get("fetched_at", now()),
         rec.get("processed_at")),
    )
    conn.commit()
    return cur.lastrowid


def update_email(conn: sqlite3.Connection, message_id: str, **fields) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    conn.execute(f"UPDATE email_messages SET {sets} WHERE message_id = ?", (*fields.values(), message_id))
    conn.commit()


def list_emails(conn: sqlite3.Connection, limit: int = 100) -> list[dict]:
    return [_decode(dict(r)) for r in conn.execute(
        "SELECT * FROM email_messages ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()]


def email_cursor(conn: sqlite3.Connection, mailbox: str) -> str | None:
    return get_setting(conn, f"imap_cursor:{mailbox}")


def set_email_cursor(conn: sqlite3.Connection, mailbox: str, cursor: str) -> None:
    set_setting(conn, f"imap_cursor:{mailbox}", cursor)


# ============================================================
# 智能体运行记录
# ============================================================

def log_agent_run(conn: sqlite3.Connection, rec: dict) -> int:
    cur = conn.execute(
        """INSERT INTO agent_runs
           (session_id, question, answer, model, tool_calls, rounds, tokens_in, tokens_out,
            latency_ms, status, mode, operator, ts)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (rec.get("session_id"), rec.get("question"), rec.get("answer"), rec.get("model"),
         json.dumps(rec.get("tool_calls", []), ensure_ascii=False), rec.get("rounds", 0),
         rec.get("tokens_in", 0), rec.get("tokens_out", 0), rec.get("latency_ms", 0),
         rec.get("status", "ok"), rec.get("mode"), rec.get("operator", "HR"), now()),
    )
    conn.commit()
    return cur.lastrowid


def list_agent_runs(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    return [_decode(dict(r)) for r in conn.execute(
        "SELECT * FROM agent_runs ORDER BY id DESC LIMIT ?", (int(limit),)).fetchall()]


# ============================================================
# 对话历史（v1.6）
# ============================================================
# 背景：前端 CHAT 只是一个内存数组，刷新页面就没了，看起来像"历史被删了"。
# 实际上每次问答**早就落库在 agent_runs 里**（question/answer/工具链/成本）——
# 丢的是"没去读"，不是"没存"。所以这里只补读取口径，**不另建一份会话存储**：
# 同一内容存两处，迟早会对不上。

CHAT_CLEARED_KEY = "chat_cleared_at"
# 清空点以**运行序号**为准，时间戳只作展示与审计。原因见 `_chat_since`。
CHAT_CLEARED_ID_KEY = "chat_cleared_run_id"
# 清空后的保留期截止时间（v1.6）。**与归档彻底删除共用 `PURGE_AFTER_DAYS`**：
# 本产品里"缓冲期"只应该有一个数字，两处各写一个迟早会不一致。
CHAT_PURGE_AFTER_KEY = "chat_purge_after"


def _parse_ts(value) -> datetime | None:
    try:
        return datetime.strptime(str(value)[:19], "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def _chat_since(conn: sqlite3.Connection) -> tuple[str, list]:
    """构造"清空点之后"的 WHERE 片段，供历史读取与清空计数共用。

    **为什么以序号为准而不是时间戳**：`now()` 只精确到秒，而"点清空 → 同一秒内
    再问一句"是很自然的操作（规则路由下几乎必然同秒）。此时新记录的 `ts` 等于
    清空点，`ts > since` 判定为"在清空点之前"，于是刚问的那句话在历史里看不见——
    自检「S续」正是这样抓到了它。`agent_runs.id` 是自增的、严格单调，
    不存在同秒歧义。老库只有时间戳时退回按时间戳过滤，不至于读不出历史。
    """
    st = chat_clear_state(conn)
    if st["cleared_through_run"]:
        return " WHERE id > ?", [st["cleared_through_run"]]
    if st["cleared_at"]:
        return " WHERE ts > ?", [st["cleared_at"]]
    return "", []


def chat_clear_state(conn: sqlite3.Connection) -> dict:
    """当前清空状态：清空点、清空时间、到期时间、还剩几天、能否恢复。

    **清空 = 软清空**（v1.6，用户口径：「删除三十天之后自动清理，30 天之内可以恢复」）：
    点「清空」只是把清空点推进到当前最大运行号，那批对话在界面上消失、但记录还在，
    保留 `PURGE_AFTER_DAYS` 天内可随时**恢复**；到期后由系统自动清理底层记录。

    `restorable` 只取决于"清空点还在不在"，不再卡到期时间：到期清理由后台任务执行
    （启动一次 + 每 24 小时一次），因此到期与真正清理之间最多有 24 小时窗口。
    **这段窗口里允许恢复是有意为之**——系统还没删掉的，人能捞回来；
    一旦清理执行就不可恢复。宁可让人多救一次，也不抢在人前面做不可逆决定。
    界面上如实显示"将于 X 自动清理"，不假装没有这条线。

    老库只有 `chat_cleared_at` 没有 `chat_purge_after` 时由清空时间现算；
    连清空时间都读不出来（脏数据）就**不设到期时间**——宁可永不自动删，
    也不能因为解析失败就把一批记录清掉。
    """
    rid_raw = get_setting(conn, CHAT_CLEARED_ID_KEY)
    try:
        rid = int(rid_raw or 0)
    except (TypeError, ValueError):
        rid = 0
    ts = str(get_setting(conn, CHAT_CLEARED_KEY) or "")
    due = str(get_setting(conn, CHAT_PURGE_AFTER_KEY) or "")
    if rid and not due:
        base = _parse_ts(ts)
        if base:
            due = (base + timedelta(days=PURGE_AFTER_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    left = None
    if due:
        d = _parse_ts(due)
        if d:
            # 向上取整，与归档的 `days - (now - ts).days` 同一口径：**刚清空显示"还剩 30 天"**。
            # 向下取整会在清空后立刻显示 29 天（30 天整减去毫秒级执行耗时），
            # 读起来像"系统偷偷少给了一天"；向上取整后两个功能的倒计时数字对得上。
            secs = (d - datetime.now()).total_seconds()
            left = max(0, -(-int(secs) // 86400))
    return {"cleared_through_run": rid, "cleared_at": ts, "purge_after": due,
            "retention_days": PURGE_AFTER_DAYS, "days_left": left,
            "restorable": bool(rid)}


def chat_history(conn: sqlite3.Connection, limit: int = 300) -> dict:
    """把 `agent_runs` 组装成前端可直接渲染的对话流（按时间正序）。

    只展示「清空点」之后的运行；清空点之前的记录仍在库中，直到保留期到期被自动清理
    （见 `clear_chat` / `purge_due_chats`）。
    运行失败的记录（没有 answer）仍保留用户那一条——"我问过什么"是事实，
    不该因为回答失败就从历史里消失。
    """
    st = chat_clear_state(conn)
    where, args = _chat_since(conn)
    sql = ("SELECT id, question, answer, mode, rounds, tool_calls, status, ts, "
           "tokens_in, tokens_out, latency_ms FROM agent_runs" + where)
    sql += " ORDER BY id ASC LIMIT ?"
    args = args + [int(limit)]

    messages: list[dict] = []
    for r in conn.execute(sql, args).fetchall():
        d = _decode(dict(r))
        q = (d.get("question") or "").strip()
        a = (d.get("answer") or "").strip()
        if not q:
            continue
        messages.append({"role": "user", "content": q, "at": d.get("ts"),
                         "run_id": d.get("id")})
        if a:
            messages.append({"role": "assistant", "content": a, "at": d.get("ts"),
                             "run_id": d.get("id"), "mode": d.get("mode"),
                             "rounds": d.get("rounds") or 0,
                             "tools": d.get("tool_calls") or [],
                             "latency_ms": d.get("latency_ms"),
                             "tokens": (d.get("tokens_in") or 0) + (d.get("tokens_out") or 0)})

    shown = conn.execute("SELECT COUNT(*) AS n FROM agent_runs" + where, args[:-1]).fetchone()["n"]
    total = conn.execute("SELECT COUNT(*) AS n FROM agent_runs").fetchone()["n"]
    hidden = conn.execute("SELECT COUNT(*) AS n FROM agent_runs WHERE id <= ?",
                          (st["cleared_through_run"],)).fetchone()["n"] if st["cleared_through_run"] else 0
    return {"messages": messages, "runs_shown": shown, "runs_total": total,
            "runs_cleared": hidden, **st}


def clear_chat(conn: sqlite3.Connection, operator: str = "HR", role: str = "hr") -> dict:
    """清空对话展示 = **软清空**：推进清空点，并给出保留期，到期后自动清理。

    为什么不是立刻物理删除：`agent_runs` 同时承担两件事——① token 成本核算，
    ② 治理审计（这一问调了哪些工具、是否走到降级、有没有越权尝试）。
    立刻删掉等于把"这段对话花了多少、系统做了什么"一起抹了。

    所以给一个 `PURGE_AFTER_DAYS` 天的缓冲期，与归档档案完全同一套口径：
    **30 天内随时能恢复，期满由系统自动清理**（`purge_due_chats`）。
    到期清理前会先把这批记录的**汇总**（条数、token 合计、运行号与时间区间）
    写进审计，**明细才删**——成本与治理的"总量"证据不随明细一起消失。

    **多次清空以最后一次为准**：清空点取当前最大运行号，保留期从这次清空重新起算。
    即"只要还在清空，就不会有东西被清掉"，整批一起在最后一次清空后 30 天清理。
    这样比逐批记各自到期日简单，而且方向是**保留更久**，不会提前删。
    """
    before = chat_clear_state(conn)
    where, args = _chat_since(conn)
    n = conn.execute("SELECT COUNT(*) AS n FROM agent_runs" + where, args).fetchone()["n"]
    through = conn.execute("SELECT COALESCE(MAX(id), 0) AS n FROM agent_runs").fetchone()["n"]
    stamp = now()
    due = (datetime.now() + timedelta(days=PURGE_AFTER_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    set_setting(conn, CHAT_CLEARED_ID_KEY, through)
    set_setting(conn, CHAT_CLEARED_KEY, stamp)
    set_setting(conn, CHAT_PURGE_AFTER_KEY, due)
    add_audit(conn, "chat", "default", "chat_clear",
              before["cleared_at"] or "（未清空过）",
              f"清空 {n} 条对话展示（清空至运行 #{through}）；保留 {PURGE_AFTER_DAYS} 天可恢复，"
              f"将于 {due} 自动清理底层运行记录",
              operator=operator, role=role)
    return {"cleared": n, "cleared_at": stamp, "cleared_through_run": through,
            "purge_after": due, "retention_days": PURGE_AFTER_DAYS, "restorable": True}


def restore_chat(conn: sqlite3.Connection, operator: str = "HR", role: str = "hr") -> dict:
    """撤销清空：清空点复位，被清空的对话重新回到界面上。

    只在清空点还在时有效（即系统尚未清理那批记录）。**恢复本身也写审计**——
    "什么时候清空过、又什么时候恢复过"是一条完整的行为链，缺一半就说不清。
    """
    st = chat_clear_state(conn)
    if not st["cleared_through_run"]:
        return {"ok": False, "restored": 0,
                "note": "当前没有可恢复的清空记录（未清空过，或那批记录已被清理）。"}
    n = conn.execute("SELECT COUNT(*) AS n FROM agent_runs WHERE id <= ?",
                     (st["cleared_through_run"],)).fetchone()["n"]
    set_setting(conn, CHAT_CLEARED_ID_KEY, 0)
    set_setting(conn, CHAT_CLEARED_KEY, "")
    set_setting(conn, CHAT_PURGE_AFTER_KEY, "")
    add_audit(conn, "chat", "default", "chat_restore",
              f"{st['cleared_at'] or '（未知时间）'} 清空至运行 #{st['cleared_through_run']}",
              f"撤销清空，恢复 {n} 条对话展示（在保留期内，未走自动清理）",
              operator=operator, role=role)
    conn.commit()
    return {"ok": True, "restored": n, "cleared_at": st["cleared_at"]}


def purge_due_chats(conn: sqlite3.Connection, days: int | None = None,
                    operator: str = "system", role: str = "system") -> dict:
    """把**已清空满 `days` 天**的对话运行记录真正删除（与归档彻底删除同一套规则）。

    三条设计取舍：

    1. **先汇总、再删明细**。删除前把条数、token 合计、运行号与时间区间写进
       `chat_purge` 审计。这样"这段时期一共跑了多少次、花了多少 token"这个总量证据
       留得住，被抹掉的只是逐条明细——成本核算与治理复盘都还有据可依。
    2. **审计不删**。`chat_clear` / `chat_restore` / `chat_purge` 三条记录永久保留，
       "谁什么时候清空过、什么时候又恢复过、系统什么时候清掉的"可以完整回答。
    3. **没有可解析的到期时间就不动手**。宁可永不自动删，也不因为脏数据误删一批记录。

    `days<=0` 视为关闭该功能（返回 `disabled`），与 `purge_due_candidates` 同口径。
    """
    days = PURGE_AFTER_DAYS if days is None else days
    if days <= 0:
        return {"purged": 0, "days": days, "disabled": True}
    st = chat_clear_state(conn)
    due = _parse_ts(st["purge_after"])
    if not st["cleared_through_run"]:
        return {"purged": 0, "days": days, "reason": "没有清空记录"}
    if not due:
        return {"purged": 0, "days": days, "reason": "清空记录没有可解析的到期时间，不自动清理"}
    if datetime.now() < due:
        return {"purged": 0, "days": days, "reason": "未到期",
                "purge_after": st["purge_after"], "days_left": st["days_left"]}

    agg = conn.execute(
        """SELECT COUNT(*) AS n, COALESCE(SUM(tokens_in),0) AS tin,
                  COALESCE(SUM(tokens_out),0) AS tout, MIN(id) AS lo, MAX(id) AS hi,
                  MIN(ts) AS t0, MAX(ts) AS t1
           FROM agent_runs WHERE id <= ?""", (st["cleared_through_run"],)).fetchone()
    n = agg["n"]
    detail = ""
    if n:
        detail = (f"（运行 #{agg['lo']}–#{agg['hi']}，{agg['t0'] or '—'} ~ {agg['t1'] or '—'}，"
                  f"合计 tokens {agg['tin'] + agg['tout']}）")
    conn.execute("DELETE FROM agent_runs WHERE id <= ?", (st["cleared_through_run"],))
    set_setting(conn, CHAT_CLEARED_ID_KEY, 0)
    set_setting(conn, CHAT_CLEARED_KEY, "")
    set_setting(conn, CHAT_PURGE_AFTER_KEY, "")
    add_audit(conn, "chat", "default", "chat_purge",
              f"{st['cleared_at']} 清空、{st['purge_after']} 到期的对话",
              f"清空满 {days} 天自动清理：删除 {n} 条运行记录{detail}；"
              f"逐条明细已清除，汇总与清空/恢复痕迹永久保留",
              operator, role)
    conn.commit()
    return {"purged": n, "days": days, "cleared_at": st["cleared_at"],
            "purge_after": st["purge_after"], "tokens": agg["tin"] + agg["tout"]}


def agent_cost_summary(conn: sqlite3.Connection) -> dict:
    r = conn.execute(
        """SELECT COUNT(*) AS runs, COALESCE(SUM(tokens_in),0) AS tin,
                  COALESCE(SUM(tokens_out),0) AS tout, COALESCE(AVG(latency_ms),0) AS lat
           FROM agent_runs""").fetchone()
    return {"runs": r["runs"], "tokens_in": r["tin"], "tokens_out": r["tout"],
            "avg_latency_ms": round(r["lat"] or 0, 1)}


# ============================================================
# 向量
# ============================================================

def save_embedding(conn: sqlite3.Connection, owner_type: str, owner_id: int, model: str,
                   vector: list[float], text_hash: str | None = None) -> None:
    import array

    blob = array.array("f", vector).tobytes()
    conn.execute(
        """INSERT INTO embeddings (owner_type, owner_id, model, dim, vector, text_hash, created_at)
           VALUES (?,?,?,?,?,?,?)
           ON CONFLICT(owner_type, owner_id, model) DO UPDATE SET
             vector = excluded.vector, dim = excluded.dim,
             text_hash = excluded.text_hash, created_at = excluded.created_at""",
        (owner_type, owner_id, model, len(vector), blob, text_hash, now()),
    )
    conn.commit()


def load_embeddings(conn: sqlite3.Connection, model: str,
                    owner_type: str = "candidate") -> list[tuple[int, list[float]]]:
    import array

    out = []
    for r in conn.execute(
        "SELECT owner_id, dim, vector FROM embeddings WHERE model = ? AND owner_type = ?",
        (model, owner_type),
    ).fetchall():
        arr = array.array("f")
        arr.frombytes(r["vector"])
        out.append((r["owner_id"], list(arr)))
    return out


def embedding_count(conn: sqlite3.Connection, model: str | None = None) -> int:
    if model:
        row = conn.execute("SELECT COUNT(*) AS n FROM embeddings WHERE model = ?", (model,)).fetchone()
    else:
        row = conn.execute("SELECT COUNT(*) AS n FROM embeddings").fetchone()
    return row["n"]


# ============================================================
# 账号与会话
# ============================================================

def create_user(conn: sqlite3.Connection, username: str, password: str, role: str = "viewer",
                display_name: str = "") -> int:
    from .crypto import hash_password

    pw, salt = hash_password(password)
    cur = conn.execute(
        """INSERT INTO users (username, display_name, role, password_hash, salt, active, created_at)
           VALUES (?,?,?,?,?,1,?)
           ON CONFLICT(username) DO UPDATE SET role = excluded.role,
             password_hash = excluded.password_hash, salt = excluded.salt,
             display_name = excluded.display_name, active = 1""",
        (username, display_name or username, role, pw, salt, now()),
    )
    conn.commit()
    return cur.lastrowid


def get_user(conn: sqlite3.Connection, username: str) -> dict | None:
    return _row(conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone())


def list_users(conn: sqlite3.Connection) -> list[dict]:
    return [_decode(dict(r)) for r in conn.execute(
        "SELECT id, username, display_name, role, active, created_at FROM users ORDER BY id").fetchall()]


def create_session(conn: sqlite3.Connection, username: str, role: str, ttl_hours: int = 12) -> str:
    import secrets
    from datetime import timedelta

    token = secrets.token_urlsafe(32)
    created = datetime.now()
    conn.execute(
        "INSERT INTO sessions (token, username, role, created_at, expires_at) VALUES (?,?,?,?,?)",
        (token, username, role, created.strftime("%Y-%m-%d %H:%M:%S"),
         (created + timedelta(hours=ttl_hours)).strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()
    return token


def get_session(conn: sqlite3.Connection, token: str) -> dict | None:
    row = conn.execute("SELECT * FROM sessions WHERE token = ?", (token,)).fetchone()
    if not row:
        return None
    d = dict(row)
    if d.get("expires_at") and d["expires_at"] < now():
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
        conn.commit()
        return None
    return d


def delete_session(conn: sqlite3.Connection, token: str) -> None:
    conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
    conn.commit()


# ============================================================
# 设置
# ============================================================

def get_setting(conn: sqlite3.Connection, key: str, default=None):
    row = conn.execute("SELECT v FROM settings WHERE k = ?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["v"])
    except (ValueError, TypeError):
        return row["v"]


def set_setting(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO settings (k, v) VALUES (?,?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
        (key, json.dumps(value, ensure_ascii=False)),
    )
    conn.commit()


# ============================================================
# 统计
# ============================================================

TIER_LABELS = {"A": "优先面试", "B": "建议面试", "C": "储备", "D": "暂不匹配当前岗位"}
STAGES = ["新投递", "已联系", "初面", "复面", "待offer", "已入职", "已结束"]


def pool_stats(conn: sqlite3.Connection) -> dict:
    items = list_candidates(conn)
    tiers = {t: 0 for t in ("A", "B", "C", "D")}
    stages = {s: 0 for s in STAGES}
    for i in items:
        t = i.get("tier_effective")
        tiers[t] = tiers.get(t, 0) + 1
        stages[i.get("stage") or "新投递"] = stages.get(i.get("stage") or "新投递", 0) + 1
    apps = conn.execute("SELECT COUNT(*) AS n FROM applications").fetchone()["n"]
    docs = conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"]
    mails = conn.execute("SELECT COUNT(*) AS n FROM email_messages").fetchone()["n"]
    pending_mails = conn.execute(
        "SELECT COUNT(*) AS n FROM email_messages WHERE processed = 0").fetchone()["n"]
    return {
        "people": len(items),
        "applications": apps,
        "documents": docs,
        # v1.6：总览里补岗位口径——问"现在招几个岗位"过去拿不到数（没有对应字段），
        # 只能答人才库人数，属于答非所问。岗位数是招聘侧最基本的现状数字。
        "jobs_open": conn.execute(
            "SELECT COUNT(*) AS n FROM jobs WHERE active = 1").fetchone()["n"],
        "jobs_total": conn.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"],
        "merged": conn.execute(
            "SELECT COUNT(*) AS n FROM candidates WHERE merged_into IS NOT NULL").fetchone()["n"],
        "tiers": tiers,
        "stages": stages,
        "pending": sum(1 for i in items if (i.get("app_status") or "待确认") != "已确认"),
        "confirmed": sum(1 for i in items if (i.get("app_status") or "") == "已确认"),
        "needs_review": sum(1 for i in items if i.get("needs_review")),
        "in_pool": sum(1 for i in items if (i.get("pool_status") or "在池") == "在池"),
        "emails": mails,
        "emails_pending": pending_mails,
        "skills": conn.execute("SELECT COUNT(*) AS n FROM skills").fetchone()["n"],
        "tags": conn.execute("SELECT COUNT(*) AS n FROM tags").fetchone()["n"],
        "proposals_pending": conn.execute(
            "SELECT COUNT(*) AS n FROM proposals WHERE status = '待确认'").fetchone()["n"],
        "embeddings": embedding_count(conn),
    }


def pipeline_stats(conn: sqlite3.Connection) -> dict:
    """招聘管道视图：各阶段数量 + 停留天数 + 来源分布。"""
    rows = conn.execute(
        """SELECT a.stage, a.channel, a.applied_at, a.id, c.name AS candidate_name,
                  j.title AS job_title
           FROM applications a LEFT JOIN candidates c ON c.id = a.candidate_id
           LEFT JOIN jobs j ON j.id = a.job_id
           WHERE a.candidate_id IS NOT NULL
           ORDER BY COALESCE(a.applied_at,'') ASC""").fetchall()
    stages: dict[str, list] = {s: [] for s in STAGES}
    channels: dict[str, int] = {}
    today_str = today()
    for r in rows:
        st = r["stage"] or "新投递"
        days = 0
        if r["applied_at"]:
            try:
                d0 = datetime.strptime(r["applied_at"][:10], "%Y-%m-%d")
                d1 = datetime.strptime(today_str, "%Y-%m-%d")
                days = max(0, (d1 - d0).days)
            except ValueError:
                days = 0
        item = {"application_id": r["id"], "candidate_name": r["candidate_name"],
                "job_title": r["job_title"], "days": days, "applied_at": r["applied_at"],
                "channel": r["channel"]}
        stages.setdefault(st, []).append(item)
        channels[r["channel"] or "未知"] = channels.get(r["channel"] or "未知", 0) + 1
    return {
        "stages": {s: {"count": len(v), "items": v[:20],
                       "overdue": sum(1 for i in v if i["days"] >= 15)}
                   for s, v in stages.items()},
        "channels": channels,
        "open_total": sum(len(v) for s, v in stages.items() if s not in ("已入职", "已结束")),
    }


def tier_history(conn: sqlite3.Connection, cid: int) -> list[dict]:
    return list_audit(conn, entity=None, entity_id=str(cid), limit=200)
