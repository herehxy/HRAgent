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
import re
import sqlite3
import threading
from datetime import datetime, timedelta

from . import univ as univ_mod  # noqa: PLC0415 — 院校层次识别（985/211/强势 标签）
# v1.29：阶段与结论标签的唯一真源。顶层导入（`stages` 只依赖 paths，不反向依赖 db，无环）——
# 阶段取值要在建表默认值、迁移、归档判定、统计口径里用到，延迟导入会变成"有时可用有时不可用"。
from . import stages as stages_mod
from . import tiers as tiers_mod
# v1.27：英语四六级归一。**顶层导入**（不延迟导入）——
# `pipeline.extract` 只依赖 `net`/`normalize`，不反向依赖 db，不构成环；
# 而延迟导入会让 `cet_level` 的口径在"有时可用有时不可用"之间摇摆。
from .pipeline import extract as extract_mod

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
    note TEXT,                         -- 人工备注（v1.25.7）：HR 工作记录，不参与档位判定
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
    suggested_job_reason TEXT,            -- v1.13.2 该建议的模型判断理由（落库后列表直接读）
    merged_from_candidate INTEGER,        -- v1.13.6 合并来源（撤销合并时据此搬回）
    channel TEXT DEFAULT '文件夹',        -- 邮箱/内推/招聘会/官网/文件夹
    applied_at TEXT,
    resume_doc_id INTEGER,
    score REAL,
    tier_suggested TEXT,
    tier_final TEXT,
    -- ⚠️ 取值域以 config/stages.json 为准（唯一真源，见 app/stages.py）；这里只是注释。
    -- v1.29.2：全部简历/初筛通过/待面试/已面试/已发放offer/已结束
    -- （旧的 已联系/初面/复面/待offer/已入职 已废弃）
    stage TEXT DEFAULT '全部简历',
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

-- ---------- 系统自动分析结果（v1.8：入库即分析） ----------
-- 为什么单独存表而不是塞进 applications：
--   1) 分析是**派生数据**，可随时重算；混进业务表会让"重算"变成"改库"；
--   2) 一条投递可能被重分析多次，只保留最新一条（UNIQUE + 覆盖），历史留在 agent_runs；
--   3) 分析失败或未出结果时，业务表完全不受影响——这是"不阻断主流程"的落点。
CREATE TABLE IF NOT EXISTS candidate_insights (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    candidate_id INTEGER NOT NULL,
    application_id INTEGER NOT NULL,
    summary TEXT,                       -- 一句话画像
    reasons TEXT,                       -- 匹配理由（JSON 数组）
    risks TEXT,                         -- 风险点（JSON 数组）
    evidence TEXT,                      -- 原文证据（JSON 数组，反幻觉要求）
    source TEXT DEFAULT 'auto_ingest',  -- auto_ingest / manual / rule_fallback
    model TEXT,                         -- 实际使用的模型名；规则降级时为空
    created_at TEXT,
    UNIQUE (candidate_id, application_id)
);

-- ---------- 每日待办摘要（v1.8） ----------
-- 一天一条（brief_date 唯一），幂等重算覆盖；payload 里存统计 + agent 的优先级判断。
CREATE TABLE IF NOT EXISTS daily_briefs (
    brief_date TEXT PRIMARY KEY,        -- YYYY-MM-DD
    payload TEXT,                       -- JSON：计数、优先事项、生成方式
    created_at TEXT
);

-- ---------- 邮件模板（v1.8.3） ----------
-- 模板是**人写的**，系统只做变量替换；发送一律要 HR 确认（不做自动发送）。
CREATE TABLE IF NOT EXISTS mail_templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE,                  -- 模板名，如「初面邀约」
    scene TEXT DEFAULT '其他通知',      -- 场景标签
    subject TEXT,                      -- 主题模板（支持 {变量}）
    body TEXT,                         -- 正文模板
    enabled INTEGER DEFAULT 1,
    created_at TEXT, updated_at TEXT
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
               VALUES (?,?,?,?,?,?,?, ?,?,?,?,?,?,?,?,?,?,?,?)""",
            (cid, d.get("source", "文件夹"), d.get("created_at"), doc_id, d.get("score"),
             d.get("tier_suggested"), d.get("tier_final"),
             # 第 8 位是 stage：读配置里的初始档（**不写字面量**——
             # 阶段名改过一次（v1.29.2 新投递→全部简历），字面量就会跟着过期）
             d.get("stage") or stages_mod.DEFAULT_STAGE,
             d.get("status") or "待确认",
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


#: 已完成建表/迁移/补列的库（进程内缓存，key 为绝对路径）。
#: 自检会在同一进程里开另一个库，所以不能只看"有没有初始化过"。
_INITIALIZED: set[str] = set()
_INIT_LOCK = threading.Lock()
#: 每个库最近一次迁移的返回值（按路径），让新连接也能读到
_MIGRATION_BY_PATH: dict[str, dict] = {}


def connect(db_path: str, *, check_same_thread: bool = True) -> sqlite3.Connection:
    """打开连接：自动建表、自动迁移 v0.1 数据、自动补列。

    `check_same_thread` 默认 True（sqlite3 原生行为：连接只在创建它的线程里用）。
    **本项目现状（v1.24.8）：分析路径已全部改为串行，每条处理用自己的连接，
    所以调用方不需要传 False。** 参数留着是为了将来若改多进程/线程池时不必改签名。

    **建表/迁移/补列只在进程内第一次打开某个库时做**（v1.13.6）。
    原来每次连接都重跑全套（实测每次 45ms），而列表页对每个候选人开一次连接
    → 10 人 0.5 秒、100 人 4.8 秒。这是"打开人才库要等一下"的根因。

    为什么进程内缓存是安全的：这些操作**幂等**（IF NOT EXISTS / 缺列才 ALTER）；
    进程重启会再跑一遍，新版本加的列与表照常生效；首次并发用锁串行化，
    避免两个线程同时 ALTER 撞车。确需运行期强制重跑时调 `_init_schema(conn)`。
    """
    directory = os.path.dirname(os.path.abspath(db_path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=check_same_thread)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # 并发写保护：默认 busy_timeout=0 时，后台巡检正在写与 HR 正在点会直接
    # 抛 database is locked；5 秒等待对本地单文件库足够。
    conn.execute("PRAGMA busy_timeout = 5000")
    key = os.path.abspath(db_path)
    if key not in _INITIALIZED:
        with _INIT_LOCK:
            if key not in _INITIALIZED:
                migration = _init_schema(conn)
                if migration:
                    _MIGRATION_BY_PATH[key] = migration
                _INITIALIZED.add(key)
    if key in _MIGRATION_BY_PATH:
        _MIGRATIONS[id(conn)] = _MIGRATION_BY_PATH[key]
    return conn


def _init_schema(conn: sqlite3.Connection) -> dict | None:
    """建表 + 迁移 + 补列（每个库在每个进程里执行一次）。

    补列必须早于 executescript：SCHEMA 里的 `CREATE INDEX ... ON jobs(active, id)`
    依赖新增列，而 CREATE TABLE IF NOT EXISTS 不会给老表加列。
    executescript 之后要**再补一次**：全新库第一遍跑补列时表还不存在
    （`ALTER TABLE` 会因"表不存在"跳过），建表之后这一遍才真正补得上。
    **新列仍必须同时写进 SCHEMA** —— 这里只是兜底，不是"可以不写建表语句"的许可
    （教训见缺陷 #39）。
    """
    migration = _migrate_v01(conn)
    _ensure_columns(conn)
    conn.executescript(SCHEMA)
    _ensure_columns(conn)
    conn.commit()
    return migration


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
            # v1.29.6（业务方 2026-10-16）：招聘数量（招聘需求，计划招几人）。
            # 纯管理信息，不参与归岗/判档；NULL=未填。
            "headcount": "INTEGER",
        },
        # 候选人软归档（v1.4 试用反馈）：归档后不再出现在人才库与检索，
        # 集中在「归档」页展示，可随时取消——不是删除，历史投递全保留。
        "candidates": {
            "archived_at": "TEXT",
            # v1.11：专业归一（学科目录条目）与其来源（catalog/模型归一）。
            # 规则归不出来时由模型在目录里选一个**真实存在**的条目（major_llm），
            # 打分仍由 tier.grade 的规则完成——模型只做归一，不做决定。
            "major_canonical": "TEXT",
            "major_via": "TEXT",
            # v1.8.5：毕业时间（YYYY-MM 或 YYYY）。校招场景要靠它判"应届/往届未就业"，
            # 光看工作年限不够——去年毕业还没参加工作的人，年限是 0，身份却不是应届。
            "grad_date": "TEXT",
            # v1.29.4（业务方 2026-10-16）：是否仍在读（1/0）。硕士「2024.9-至今」
            # 这类没毕业的学生不能被本科毕业年份判成"往届未就业"；在读按应届处理。
            "in_school": "INTEGER",
            # v1.17：奖学金/荣誉/论文/专利
            "honors_json": "TEXT",
            # v1.25.7：人工备注（HR 工作记录：沟通印象/内推关系/谈薪情况……），不参与档位判定
            "note": "TEXT",
            # v1.27：英语等级。存**原文表述**（如「大学英语六级」「CET-4」）
            # 而不是归一后的档位——归一交给 `pipeline/extract.cet_level()`，
            # 这样将来加「英语专业八级」「雅思」时不用改库、也不用迁移老数据。
            # 空串/None = 简历里没写（界面显示为"—"，筛选时算"未提供"而非"没有证书"）。
            "cet": "TEXT",
            # v1.29：第一学历（本科）院校。**与 `school` 是两件事**：
            # `school` 只存一所学校，也就是最高学历那所；而"本科985/本科211"
            # 要的是**本科就读院校**。拿 school 当第一学历用，会把"本科双非、
            # 硕士985"的人筛进「本科985」名单 —— 而 HR 是拿这个名单做取舍的，
            # 筛错人比筛不到人严重得多。
            # 抽不到就留空 → 筛选里单列「未识别」一档（与"不满足"严格分开，
            # 否则 HR 会把"没识别出来"当成"不是985"）。
            "first_degree_school": "TEXT",
            # v1.29.2：**完整学历院校经历**（业务方要求「本科什么学校、硕士什么学校、
            # 博士什么学校」在分析结果里完整体现）。
            # 存 JSON 数组字符串：[{"degree":"本科","school":"…","major":"…"}, …]。
            # 为什么不用现成的 `school` + `first_degree_school` 两个字段拼：
            # 那是"最高 + 本科"两段，**中间的研究生段（硕士/博士）就丢了**，
            # 而 HR 看一个人的学校水平时恰恰会顺着学历一路看下去。
            # 存 JSON 而不是建子表：一段教育经历只随简历抽取一次、只读不编辑，
            # 建表会带来主键/级联/合并不必要的复杂度；读时 `json.loads` 一次即可。
            "edu_history": "TEXT",
        },
        # v1.5：待指定投递的「建议岗位」（轮询在招岗位取最适者，只建议不归岗）
        "applications": {
            "suggested_job_id": "INTEGER",
            # v1.13.2：建议岗位的**模型判断理由**。存下来是为了让列表/卡片直接读库展示，
            # 不必每次刷新都重新问模型（实测：一次 6-8 秒且烧 token）。
            "suggested_job_reason": "TEXT",
            # v1.13.6：合并时记下"这条原本属于谁"，否则撤销合并无法把投递搬回去
            "merged_from_candidate": "INTEGER",
            # v1.8：阶段变更时间——停滞提醒（超期未推进）需要一个可比较的时间戳，
            # 靠 audit_log 推导太脆（人工改阶段、批量导入都可能缺审计）
            "stage_changed_at": "TEXT",
            # v1.17：奖学金/荣誉/论文/专利（规则抽取 + 原文片段）
            "honors_json": "TEXT",
            # ---- v1.29：面试与录用流程 ----
            # 结论标签（取值受阶段约束，见 app/stages.py 的 outcomes_of）：
            # `已面试` → 不录用；`已发放offer` → 已签约 / 拒签；其余阶段为空。
            # 注意**没有「拟录用」**：业务方裁定「拟录用 ≡ 待发offer」，
            # 它就是那个阶段本身，不再另打一个标签（否则"阶段+标签"两处都能表达同一件事）。
            "outcome": "TEXT",
            # 面试单位（＝面试部门）。初筛通过阶段由 HR 登记；发面试邀请时自动带出，
            # 发信成功后再以**邮件实际值**为准回写（候选人被告知的是邮件里那个单位）。
            "interview_unit": "TEXT",
            # 面试日期/时段/方式：**发面试邀请邮件成功那一刻**从邮件的运行时变量里捕获，
            # 不是"HR 点了推进按钮那天"。两者能差好几天（提前一周发邀请是常态）。
            # 存两个字段（日期 / 时段原文）而不是一个拼好的串：改时段不必重填日期，
            # 且导出时要显示到分钟，拼串后还得再拆一次。
            "interview_date": "TEXT",
            "interview_slot": "TEXT",
            "interview_mode": "TEXT",
            # 进入「已面试」阶段的时刻。**只用于圈定"曾到过已面试"的导出行集**，
            # 不参与展示。为什么不能用 interview_date 代替：面试日期在**邀约时**就有了
            # （待面试阶段），而"到底面了没有"要等面试完成 —— 用日期当标记会把
            # "被邀约但没来"的人算进已面试台账；反过来用进阶段时间当面试日期，
            # HR 看到的就是"我点按钮那天"，不是面试安排。
            "interviewed_at": "TEXT",
            # 录用接收函发出时刻：**发信成功后系统自动写**，绝不手工填。
            "offer_sent_at": "TEXT",
            # 以下三项由人事处人工维护（业务方明确要求「入职最后期限」也手工填）
            "signed_at": "TEXT",
            "onboard_deadline": "TEXT",
            "onboard_status": "TEXT",
        },
        # v1.29：模板用途。**必须显式存**而不是按模板名判断 ——
        # 模板名 HR 随时会改（「初面邀约」改成「面试通知」），按名字匹配的话
        # 改个名"发信自动回写面试信息"就静默失效了。用途是结构化的，改名字不影响。
        # interview_invite=面试邀约（回写面试日期/时段/单位/方式）；offer=录用相关（回写 offer_sent_at）
        "mail_templates": {
            "kind": "TEXT DEFAULT 'other'",
        },
        # v1.13.6：合并来源。documents 也要有——老库不会因为 CREATE TABLE 补列，
        # 没有这一列时拆分只能"如实说明搬不回"，等于白拆。
        "documents": {
            "merged_from_candidate": "INTEGER",
            # 敏感字段清洗结果（v1.23.4）：{"民族": 1, "婚育": 2} —— 送进模型**之前**
            # 被剔除的内容与条数。原来只在内存里算出来就丢了，HR 看不到
            # "到底哪些信息没给模型看"，而这恰恰是隐私红线要能被核查的地方。
            # 注意：这个字典**只能有一份 documents 键**，重复会互相覆盖（踩过）。
            "sensitive_found": "TEXT",
            # v1.27：附件性质 'resume'（简历，参与解析与建档） / 'other'（其他附件，
            # 随简历一起收下但**不进人才库、不参与解析**）。
            # 为什么需要：求职邮件常在简历之外还带作品集、成绩单、证书扫描件、
            # 身份证/学位证照片。过去这些一律走完整入库链路 —— 它们解析不出
            # 姓名电话，会被当成一个"解析失败的新人"塞进人才库（实测：多附件邮件
            # 一次能凭空多出好几个人）。现在只归档、展示在档案里，不建档。
            # 默认 'resume'：老库全是简历，不回填成 other 就等于把老附件全变附件。
            "kind": "TEXT DEFAULT 'resume'",
        },
        # v1.8 主动提案：区分「HR 问出来的」与「系统自己发现的」。
        # 两者的确认流、留痕、执行路径完全一致，只有来源不同——
        # 界面上要能分开看，否则 HR 无法判断该对哪些多一分警惕。
        "proposals": {
            "source": "TEXT DEFAULT 'hr_asked'",
            "dedup_key": "TEXT",
        },
        # v1.11：模型提炼的「业务方向」（如 材料工艺 / 后端开发），与专业方向互补：
        # 专业=学历背景，业务=实际干过的事。入库自动分析产出，卡片与档案展示。
        "candidate_insights": {
            "business_direction": "TEXT",
        },
    }
    # v1.24.4：`_ADD` 里同一个表出现**两次**会被 Python 静默合并成一个——
    # 后写的键覆盖先写的，于是"我明明加了列、迁移却没生效"。
    # 实测踩过（`documents.sensitive_found` 就是这么丢的，症状是运行时才报
    # `no such column`，而代码看着完全正常）。这里直接在迁移前检查源码文本，
    # 把重复变成**启动期**的明确报错，而不是等某条 SQL 炸掉才发现。
    # 注意必须读**源码**：dict 里已经没有重复键了（重复只存在于源码中）。
    _ADD_SRC = ""
    try:
        import pathlib
        _txt = pathlib.Path(__file__).read_text(encoding="utf-8")
        _i = _txt.index("_ADD = {")
        _ADD_SRC = _txt[_i:_txt.index("\n    }", _i)]
    except Exception:                                        # noqa: BLE001
        _ADD_SRC = ""            # 取不到就跳过检查，不因此拦住启动

    for table, cols in _ADD.items():
        have = _cols(conn, table)
        if not have:
            continue
        for col, ddl in cols.items():
            if col not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")

    # v1.24.4：`_ADD` 里同一个表出现**两次**会被 Python 静默合并成一个——
    # 后写的键覆盖先写的，于是"我明明加了列、迁移却没生效"。
    # 实测踩过（documents.sensitive_found 就是这么丢的，症状是运行时报
    # `no such column`，而代码看着完全正常）。这里直接把重复变成启动期断言，
    # 而不是等到某条 SQL 炸掉才发现。
    _seen_tables: set[str] = set()
    for table in re.findall(r'^\s{8}"(\w+)":\s*\{', _ADD_SRC, re.M):
        if table in _seen_tables:
            raise RuntimeError(
                # 措辞里**别写成"模块名 . _ADD"那种点号形式**：自检的静态调用面检查
                # 会把它当成一次属性调用，而 `_ADD` 是本函数内的局部变量、不在
                # `dir(db)` 里，于是年年报一条假警。只写「_ADD」即可。
                f"_ADD 里表 {table!r} 出现了两次：同名键会互相覆盖，"
                f"导致新增的列不迁移。请把同一个表的列合并到同一个键下。")
        _seen_tables.add(table)


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
               operator: str = "HR", headcount: int | None = None) -> int:
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
    hc = None if not headcount or headcount <= 0 else int(headcount)   # 0/负 = 未填
    if row:
        conn.execute(
            "UPDATE jobs SET dept_id = ?, jd_json = ?, active = 1, status = '开放',"
            " closed_at = NULL, headcount = COALESCE(?, headcount), updated_at = ? WHERE id = ?",
            (dept_id, json.dumps(jd or {}, ensure_ascii=False), hc, stamp, row["id"]))
        conn.commit()
        return row["id"]
    cur = conn.execute(
        """INSERT INTO jobs (title, dept, dept_id, jd_json, owner, status, active,
                             headcount, opened_at, created_at, updated_at)
           VALUES (?,?,?,?,?,'开放',1,?,?,?,?)""",
        (title, dept_name, dept_id, json.dumps(jd or {}, ensure_ascii=False), owner,
         hc, stamp, stamp, stamp),
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
        # v1.8.9：只数**未归档**的投递。界面上的数字要和"点进去看到的人"对得上，
        # 也和「重新分析」实际处理的范围一致（regrade 同样排除归档）。
        j["applications_count"] = conn.execute(
            """SELECT COUNT(*) AS n FROM applications a
               JOIN candidates c ON c.id = a.candidate_id
               WHERE a.job_id = ? AND COALESCE(c.archived_at, '') = ''""",
            (j["id"],)).fetchone()["n"]
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
                  operator: str = "HR", headcount: int | None = None) -> dict | None:
    """更新岗位 JD 尺子。只影响之后新入库的投递评分，不改动历史档位。

    headcount（招聘数量，v1.29.6）：None = 本次没传，保持原值；0/负 = 清空（NULL）。
    它是岗位的管理信息，和 JD 尺子一起在同一个弹窗里编辑。"""
    j = get_job(conn, jid)
    if not j:
        return None
    sets = ["jd_json = ?", "updated_at = ?"]
    vals: list = [json.dumps(jd or {}, ensure_ascii=False), now()]
    if headcount is not None:
        sets.append("headcount = ?")
        vals.append(headcount if headcount > 0 else None)
    vals.append(jid)
    conn.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id = ?", vals)
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


def match_job_by_direction(conn: sqlite3.Connection, direction: str) -> tuple[int | None, dict]:
    """按「应聘方向」段匹配岗位（`match_job_by_subject` 的弱兜底）。

    为什么需要：投递方在标题里写的方向常与岗位名不完全一致——岗位叫「工艺技术」，
    方向写「材料工艺」；岗位叫「检验检测」，方向写「理化检测」。强匹配（岗位名
    出现在标题里）在这种情况下落空，而**方向段是投递方明确表达的意向**，
    不该直接丢掉。

    匹配规则（仍然保守，宁可不归岗也不归错）：
    1. 岗位标题或核心词**包含**方向段（方向是岗位名的子串）——如方向「科研」→「科学研究」；
    2. 方向段**包含**岗位标题核心词（方向更细）——如方向「材料工艺技术」→「工艺技术」；
    3. 两个方向都要求**共有至少 2 个连续汉字**且长度不短于 2，避免单字误撞。
    多条命中取共有片段最长的那个；仍然命中不了就返回 None（保持待指定，只出建议）。
    """
    import re as _re                       # 与 _job_core_title 同一风格：就近导入

    d = _re.sub(r"\s+", "", str(direction or "")).strip()
    if len(d) < 2:
        return None, {}
    jobs = [j for j in list_jobs(conn, include_inactive=False) if (j.get("title") or "").strip()]
    best, best_len = None, 0
    for j in jobs:
        title = _re.sub(r"\s+", "", str(j.get("title") or ""))
        core = _re.sub(r"\s+", "", _job_core_title(title))
        for cand in (title, core):
            if not cand or len(cand) < 2:
                continue
            if cand in d or d in cand:
                overlap = min(len(cand), len(d))
                if overlap > best_len:
                    best, best_len = j, overlap
    if best:
        return best["id"], best.get("jd_json") or {}
    return None, {}


def match_job_soft(conn: sqlite3.Connection, text: str) -> tuple[int | None, dict]:
    """宽口径"能对应上"的岗位匹配（**只用于建议岗位的优先序**，不自动归岗）。

    v1.29.5（业务方）：投递方在标题/文件名里写明的应聘方向，只要能和某个在招岗位
    "对应上"，就该优先于按正文内容让模型去猜——它是**本人写下的意向**。

    与 `match_job_by_direction` 的保守关爱不同：这里允许**共有连续片段**命中
    （方向「数字化中心」↔ 岗位「数字化工程师」共有「数字化」）。宽一点是安全的：
    输出只是"建议"，错了 HR 一眼能看见并纠正；漏掉意向反而误导。
    规则：与岗位标题/核心词的**最长公共连续子串 ≥ 2 字、且不少于较短一侧的一半**；
    多条命中取更长者。仍匹配不到 → (None, {})。
    """
    import re as _re

    d = _re.sub(r"\s+", "", str(text or "")).strip()
    if len(d) < 2:
        return None, {}

    def _lcs(a: str, b: str) -> int:
        # 最长公共**连续**子串长度（两串都很短，直接 O(n*m)）
        best = 0
        prev = [0] * (len(b) + 1)
        for ca in a:
            cur = [0] * (len(b) + 1)
            for j, cb in enumerate(b, 1):
                if ca == cb:
                    cur[j] = prev[j - 1] + 1
                    if cur[j] > best:
                        best = cur[j]
            prev = cur
        return best

    jobs = [j for j in list_jobs(conn, include_inactive=False)
            if (j.get("title") or "").strip()]
    best, best_score = None, 0
    for j in jobs:
        title = _re.sub(r"\s+", "", str(j.get("title") or ""))
        core = _re.sub(r"\s+", "", _job_core_title(title))
        for cand in (title, core):
            if not cand or len(cand) < 2:
                continue
            n = _lcs(d, cand)
            # ≥2 字、且不少于较短一侧的一半 —— 避免只共用一个"工/学"字就命中
            if n >= 2 and n * 2 >= min(len(d), len(cand)) and n > best_score:
                best, best_score = j, n
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
            first_seen_at, last_active_at, pool_status, pii_level, blind, grad_date, in_school, cet,
            first_degree_school, edu_history,
            created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (rec.get("identity_key"), rec.get("name"), rec.get("gender"), rec.get("birth_year"),
         rec.get("phone_enc"), rec.get("email_enc"), rec.get("phone_bidx"), rec.get("email_bidx"),
         rec.get("edu_level"), rec.get("school"), rec.get("major"), rec.get("years_exp"),
         rec.get("current_org"), rec.get("source_first"),
         rec.get("first_seen_at", stamp), rec.get("last_active_at", stamp),
         rec.get("pool_status", "在池"), rec.get("pii_level", "普通"),
         1 if rec.get("blind") else 0,
         # 毕业时间（v1.13.6 补写）：ingest 明明传了却曾被静默丢弃，
         # 导致新人"应届/往届未就业"永远判不出来——只能靠启动时的存量补写救。
         rec.get("grad_date"),
         # v1.29.4：在读（同一个显式列清单坑的第四次）
         rec.get("in_school"),
         # v1.27：英语四六级原文表述。本函数是**显式列清单**（不是字典展开），
         # 漏加这一列的症状与其他字段一样是"传了却静默丢弃"——已修过 grad_date 一次。
         rec.get("cet"),
         # v1.29：第一学历（本科）院校。同一个坑第三次了（grad_date、cet 各一次）——
         # 本函数是**显式列清单**，新增列别忘了同步加进来，否则"传了却静默丢弃"。
         rec.get("first_degree_school"),
         # v1.29.2：完整学历院校经历（本科/硕士/博士各一段）。JSON 字符串。
         rec.get("edu_history"),
         stamp, stamp),
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


def _assert_valid_columns(conn: sqlite3.Connection, table: str, fields: dict) -> None:
    """校验 update_* 的键名都在真实列里，杜绝把键名拼进 SQL 列位置造成注入。

    值走参数化（?），但**列名无法参数化**，是用 f-string 拼的。若一个提案
    写入路径把非白名单键塞进 fields（如 `{'name, pii_level=(SELECT 1)': 2}`），
    就会被原样拼进 SQL。这里对照 PRAGMA table_info 做白名单校验。
    """
    cols = {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    bad = [k for k in fields if k not in cols]
    if bad:
        raise ValueError(f"非法字段名（不在 {table} 列中）：{bad}")


def update_candidate(conn: sqlite3.Connection, cid: int, **fields) -> None:
    if not fields:
        return
    _assert_valid_columns(conn, "candidates", fields)
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
       a.note          AS app_note,     -- v1.25.7：投递备注改用 app_note（note 让位给候选人备注）
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
       sj2.title          AS suggested_job_title,
       a.suggested_job_reason AS suggested_job_reason,
       -- v1.29：面试与录用流程字段。**必须在这里显式列出**（本视图是列清单不是 `a.*`），
       -- 漏列的症状与 insert 的显式列清单一样：库里明明有值，界面永远读不到。
       a.outcome       AS outcome,
       a.interview_unit  AS interview_unit,
       a.interview_date  AS interview_date,
       a.interview_slot  AS interview_slot,
       a.interview_mode  AS interview_mode,
       a.interviewed_at  AS interviewed_at,
       a.offer_sent_at   AS offer_sent_at,
       a.signed_at       AS signed_at,
       a.onboard_deadline AS onboard_deadline,
       a.onboard_status  AS onboard_status,
       j.title         AS job_title,
       j.active        AS job_active,
       -- v1.24.9：区分「简历读不出」与「信息缺失」，两者过去共用 needs_review
       d0.parse_ok     AS doc_parse_ok
FROM candidates c
LEFT JOIN applications a ON a.id = (
    SELECT id FROM applications WHERE candidate_id = c.id
    ORDER BY COALESCE(applied_at,'') DESC, id DESC LIMIT 1
)
LEFT JOIN jobs j ON j.id = a.job_id
LEFT JOIN jobs sj2 ON sj2.id = a.suggested_job_id
LEFT JOIN documents d0 ON d0.id = (
    -- v1.27：**只取简历附件**（kind='resume'）。原来这里取"最新一条附件"，
    -- 多附件邮件下最新那条很可能是作品集/证书扫描件（kind='other'），
    -- 它的 parse_ok 常年为真 → 列表里"简历读不出"会假性消失。
    -- `COALESCE(kind,'resume')` 兜住老库没补列的极端情况（此时恒不命中 → NULL，安全）。
    SELECT id FROM documents WHERE candidate_id = c.id
      AND COALESCE(kind, 'resume') != 'other'
    ORDER BY id DESC LIMIT 1
)
"""


def app_status_display(item: dict) -> tuple[str, str]:
    """投递状态的**展示文案 + 下一步动作**（v1.14：取消"待确认"）。

    为什么取消「待确认」：查过代码，这个状态**不驱动任何自动化**——确认与否，
    系统后续行为完全一样（不自动推进、不自动发信、不自动归档，统计也不依赖它）。
    它只是"你看过没有"的标记，却天天挂在绝大多数卡片上。HR 说得对：
    **认同默认档位的人不需要任何动作**，凭什么要他点一下？

    所以现在只提示**真正卡住的事**：
      待归岗 → 没岗位就判不了档（学历门槛无从判定），先归岗
      待分析 → 档位还没出来（模型没给结论），可点「重算自动分析」
      待复核 → 模型给了判断、HR 还没表达过态度（**唯一带"待办"味道的**，
                而且它提示的是"看一眼"，不是"必须做动作"）
    其余情况返回空字符串——**不打扰**。HR 真的改了档位时，档位徽章本身就是信号。

    返回 `(标签, 说明)`；说明直接当界面 title 用。
    两种调用方的键名不同（列表接口 `app_status`、完整档案投递行 `status`），都认。
    """
    confirmed = (item.get("app_status") or item.get("status") or "") == "已确认"
    tier = item.get("tier_final") or item.get("tier_suggested")
    if not item.get("job_id"):
        return "待归岗", ("这条投递还没归到岗位。未归岗不判档（学历门槛无从判定），"
                          "请先归岗或采纳系统给出的建议岗位")
    if not tier:
        return "待分析", "已归岗，但档位还没出来（模型尚未给出结论）；可点「重算自动分析」"
    if not confirmed:
        # 只在"模型给了判断、人还没看过"时提一句；不写成"必须确认"的口气
        return "待复核", ("还没复核过这个人的信息。认同系统给的建议档就点「复核」"
                          "（不改档位）；不同意就改档位或拒绝。")
    return "已复核", "这个人的信息已核对完毕（档位是 HR 自己定的，与复核分开记）"



def _attach_skills(conn: sqlite3.Connection, items: list[dict],
                   with_skills: bool = True) -> list[dict]:
    """给列表项补技能明细与派生字段。

    `with_skills=False`（v1.23.5）：**只补不查库的派生字段**，跳过技能查询。
    用途是"先过滤、再只为当前页装配"：1000 人时把全库都装配出来要 59ms，
    而界面一屏只有 20 条——先轻量过一遍筛出这 20 条 id，再装配它们即可。
    派生的三个字段（tier_effective / status_display / status_hint）两种模式都要，
    因为筛选与 facets 依赖它们。
    """
    if not items:
        return items
    if with_skills:
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
        if with_skills:
            i["skills"] = bucket.get(i["id"], [])
            i["skill_detail"] = detail.get(i["id"], [])
        else:
            i["skills"] = []
            i["skill_detail"] = []
        i["tier_effective"] = i.get("tier_final") or i.get("tier_suggested")
        # 状态标签按实际情况给（待归岗 / 待分析 / 待确认 / 已确认），
        # 不再直接把库里的默认值「待确认」原样贴上去
        i["status_display"], i["status_hint"] = app_status_display(i)
    return items


def list_candidates(conn: sqlite3.Connection, tier: str | None = None, keyword: str | None = None,
                    skill: str | list[str] | None = None, min_years: int | None = None,
                    education: str | None = None, job_id: int | None = None,
                    univ: str | None = None,
                    # v1.27：`strong` = 特定院校（逗号分隔的多个校名，**并集**语义），
                    # `cet` = 英语四六级（逗号分隔，归一档位；"未提供"是特殊值）。
                    # 两者在**这一层**实现而不是只在 server 层 —— 否则智能助手的
                    # `search_candidates` 与导出走的是同一个 `list_candidates`，
                    # 却拿不到这两个参数，"界面上能筛、助手查不到"必然发生
                    #（与 v1.23.4 把 univ 筛选下沉到 db 的道理完全一样）。
                    strong: str | None = None, cet: str | None = None,
                    # v1.29：新增筛选维度。全部在**这一层**实现，理由同 strong/cet ——
                    # 智能助手的 `search_candidates` 与导出共用本函数，
                    # 只在 server 层筛会出现"界面上能筛、助手查不到"。
                    #   overseas = 海外院校：1=名单内 / 0=名单外 / 未识别=学校为空
                    #   bachelor = 第一学历层次：985 / 211 / 特殊 / 未识别
                    #   date_from / date_to = 投递时间段（按 applied_at，含当天）
                    #   interview_unit = 面试部门（逗号分隔 = 并集）
                    overseas: str | None = None, bachelor: str | None = None,
                    date_from: str | None = None, date_to: str | None = None,
                    interview_unit: str | None = None,
                    stage: str | None = None, pool_status: str | None = None,
                    needs_review: bool | None = None, include_merged: bool = False,
                    archived: bool | None = None, limit: int | None = None,
                    light: bool = False) -> list[dict]:
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
    # 投递时间段：按 applied_at（**投递时间**，不是入库时间）——
    # 补录历史简历时两者能差几个月，按入库时间筛会把"上个月投的"当成"今天投的"。
    # 比较用字符串（库里存的是 `YYYY-MM-DD HH:MM:SS`，字典序即时间序）。
    if date_from:
        where.append("COALESCE(a.applied_at,'') >= ?")
        args.append(str(date_from).strip()[:10])
    if date_to:
        # 含当天：界面只给到「日」，所以上界取**次日**。
        # 写成 `<= date_to` 会把当天 00:00 之后投的全漏掉
        # （字符串比较下 `2026-10-15 09:00` > `2026-10-15`）——HR 会以为"今天没人投"。
        where.append("COALESCE(a.applied_at,'') < ?")
        args.append(_plus_one_day(str(date_to).strip()[:10]))
    if interview_unit:
        # v1.29.11：面试单位支持**多选**（「分析测试中心、稀土研究院」）。
        # 旧实现是 IN（整串相等），多值记录谁也筛不到；现在把 , ， 、 一律当
        # 分隔符，用「以逗号为边界」的 LIKE 命中任一所选单位 —— 避免
        # 「第一实验室」误中「第十一实验室」这类子串假命中。
        _units = [u.strip() for u in re.split(r"[、,，]+", str(interview_unit)) if u.strip()]
        if _units:
            _norm = ("REPLACE(REPLACE(REPLACE(COALESCE(a.interview_unit,''),"
                     " '、', ','), '，', ','), ' ', '')")
            conds = []
            for u in _units:
                conds.append(f"(',' || {_norm} || ',') LIKE ?")
                args.append("%," + u.replace(" ", "") + ",%")
            where.append("(" + " OR ".join(conds) + ")")
    if keyword:
        # v1.23.5：关键词改为**在 SQL 里筛**（原来是把全库装配成对象后在 Python 里逐条比）。
        # 两个原因：
        #   ① 轻量模式（light=True）不装配技能，Python 版本会因为 skills 为空而
        #      **静默搜不到技能** —— 迁到 SQL 才能与分页优化共存；
        #   ② 少装配 95% 的行。
        # 口径与原来完全一致：姓名/学历/院校/专业/岗位名/建议岗位名/技能，任一**包含**即命中。
        # **对应岗位必须能被搜到**（v1.17）：HR 嘴里的"数字化工程师的人"指的是岗位名，
        # 不是技能词——搜不到会让模型回答"没有符合的候选人"（误导）。
        # 注意：这段必须在 `sql` 执行**之前**，否则条件加进的是一个已经用过的列表。
        like = "%" + keyword.strip().lower() + "%"
        where.append(
            "(LOWER(COALESCE(c.name,'')) LIKE ? OR LOWER(COALESCE(c.edu_level,'')) LIKE ?"
            " OR LOWER(COALESCE(c.school,'')) LIKE ? OR LOWER(COALESCE(c.major,'')) LIKE ?"
            " OR LOWER(COALESCE(j.title,'')) LIKE ?"
            " OR LOWER(COALESCE(sj2.title,'')) LIKE ?"
            " OR LOWER(COALESCE(c.note,'')) LIKE ?"   # v1.25.7：备注可搜（"内推""谈薪"能找到人）
            " OR EXISTS (SELECT 1 FROM candidate_skills cs3 JOIN skills s3 ON s3.id = cs3.skill_id"
            "            WHERE cs3.candidate_id = c.id"
            "              AND LOWER(COALESCE(s3.canonical_name,'')) LIKE ?))")
        args.extend([like] * 8)
    if where:
        sql += " WHERE " + " AND ".join(where)
    # 排序（v1.8.8，HR 口径）：**最差档一律沉底**，其余不看分数、按导入时间倒序。
    # 为什么不再按分数排：分数受"是否识别出年限/技能词表命中"影响，准确性还不到
    # 能当排序依据的程度——按分数排会让 HR 误以为"前几名就是最优"。档位里
    # D 是唯一明确的否（学历不过线），所以只有它特殊处理；A/B/C 按投递时间排，
    # 最新投递在最上面，符合"刚收到的先看"的实际工作方式。
    sql += (" ORDER BY CASE WHEN COALESCE(a.tier_final, a.tier_suggested, '') = ?"
            " THEN 1 ELSE 0 END, COALESCE(a.applied_at,'') DESC, c.id DESC")
    if limit:
        sql += f" LIMIT {int(limit)}"

    # ⚠️ ORDER BY 里那个 `?` 是**最后**追加的参数，顺序不能错 —— 它必须跟在
    # 所有 WHERE 参数之后。用 `?` 而不是把档位字母拼进 SQL：档位来自配置文件，
    # 拼串一旦配置里带了引号就会把整条 SQL 弄坏。
    args = [*args, tiers_mod.hard_shortfall_tier()]
    items = [_decode(dict(r)) for r in conn.execute(sql, args).fetchall()]
    # v1.24.9：把"缺哪些字段"与"简历有没有读出来"解出来给界面用。
    # 过去只有 needs_review 一个布尔，两种情况都显示"待人工判读"，
    # HR 不知道该去补字段还是去修文件——现在分开。
    for it in items:
        bd = it.get("breakdown")
        it["missing_fields"] = (bd.get("missing_fields") or []) if isinstance(bd, dict) else []
        # v1.25.6：旧数据（v1.24.9 之前的投递）breakdown 里没有清单，界面上只能显示
        # 一个笼统的"待补录"。这里按**当前数据**实时兜底算一遍，让"缺什么"永远
        # 说得出来（用户要求：要说明缺失的信息是什么，而不是只提示缺失）。
        # 姓名/年限两项在列表端可靠可判；技能因轻量模式不装配，不在此处妄断。
        if it.get("needs_review") and not it["missing_fields"]:
            _mf = []
            if not (it.get("name") or "").strip():
                _mf.append("姓名")
            if it.get("years_exp") is None:
                _mf.append("年限")
            it["missing_fields"] = _mf
        it["parse_failed"] = (it.get("doc_parse_ok") is not None
                              and not it.get("doc_parse_ok"))
    # 院校层次标签（v1.7.3）：每条都挂上（不限筛选时卡片也要显示 985/211 标签）。
    # **筛选也放这里**（v1.23.4）：原来只在 server 层筛，导致智能助手的
    # `search_candidates` 根本没这个参数——HR 问"985 的人有哪些"，
    # 助手只能回"没有标注 985/211 的候选人"，而列表页明明挂着这些标签。
    # 同一份口径只能有一处实现，否则两处必然漂移。
    for _i in items:
        _i["uni_tier"] = univ_mod.uni_tier(_i.get("school") or "")
        # v1.27：英语四六级归一后的档位。**在这一处算**（与 uni_tier 同一原则）：
        # 列表接口、智能体 `search_candidates`、导出都读这一个字段，
        # 不然"界面上筛六级"与"助手查六级"会给出两拨人。
        # 归一规则在 `pipeline.extract.cet_level`（唯一口径）。
        _i["cet_level"] = extract_mod.cet_level(_i.get("cet"))
        # v1.29.2：学历院校经历（JSON → 列表）。解析失败给空列表，
        # 绝不让一个脏值把整个列表接口带崩（这个字段只用于展示）。
        _i["edu_history"] = _loads_edu_history(_i.get("edu_history"))
        # v1.29：第一学历（本科）院校层次 + 海外院校标记。
        # **两个独立维度**（海外校既不是 985 也不是 211），也在这里算一次给三处用。
        _i["bachelor_tier"] = univ_mod.uni_tier(_i.get("first_degree_school") or "")
        _i["overseas"] = univ_mod.is_overseas(_i.get("school") or "")
        _i["verdict"] = stages_mod.verdict(_i.get("stage"), _i.get("outcome"))
    # v1.29：国内院校层次。取值 985/211/**特殊**/**未识别**。
    # 「特殊」= v1.27 的 strong 名单（非 985/211 的强校）；v1.29 把它并进这一个维度，
    # 界面上不再有第二个入口指向同一批人（原来「院校层次」与「特定院校」是两个下拉，
    # 其中的"强势"与"特定院校"说的是同一集合）。
    # 「未识别」= **学校字段为空**，与"不是 985/211"严格分开：前者要人工补录，
    # 后者可以直接排除，混在一起 HR 会读成"这人不是 985"。
    if univ:
        _u = str(univ).strip()
        if _u == "未识别":
            items = [i for i in items if not (i.get("school") or "").strip()]
        elif _u == "特殊":
            items = [i for i in items if i.get("uni_tier") == "强势"]
        elif _u == "985":
            items = [i for i in items if i.get("uni_tier") == "985"]
        elif _u == "211":
            # 985 院校全部同时是 211，所以选 211 时 985 也算（与界面提示一致）
            items = [i for i in items
                     if i.get("uni_tier") in ("985", "211")]
    # v1.29：海外院校（**独立维度**，不与上面的国内层次混）。1 / 0 / 未识别。
    if overseas:
        _o = str(overseas).strip()
        if _o == "未识别":
            items = [i for i in items if i.get("overseas") is None]
        elif _o in ("1", "海外"):
            items = [i for i in items if i.get("overseas") is True]
        elif _o in ("0", "非海外"):
            items = [i for i in items if i.get("overseas") is False]
    # v1.29：第一学历层次。985 / 211 / 特殊 / 未识别（未识别 = 本科院校没抽到，需补录）
    if bachelor:
        _b = str(bachelor).strip()
        if _b == "未识别":
            items = [i for i in items
                     if not (i.get("first_degree_school") or "").strip()]
        elif _b == "特殊":
            items = [i for i in items if i.get("bachelor_tier") == "强势"]
        elif _b == "985":
            items = [i for i in items if i.get("bachelor_tier") == "985"]
        elif _b == "211":
            items = [i for i in items
                     if i.get("bachelor_tier") in ("985", "211")]
    # v1.27：特定院校（strong）。逗号分隔 = 并集（"看下这几所学校的人"）。
    # 比较前走 `univ._norm/_base` 归一，否则「中国科学院大学（北京）」这类
    # 带括号/校区的写法会漏。
    if strong:
        _names = [s for s in str(strong).split(",") if s.strip()]
        _want = {univ_mod._norm(s) for s in _names} | {univ_mod._norm(univ_mod._base(s)) for s in _names}
        items = [i for i in items
                 if univ_mod._norm(i.get("school") or "") in _want
                 or univ_mod._norm(univ_mod._base(i.get("school") or "")) in _want]
    # v1.27：英语四六级。逗号分隔 = 并集；"未提供"是**特殊值**（简历里没写）。
    # ⚠️ 三种语义必须严格分开：`cet_level` 有三个取值域 ——
    #   归一档位（'六级'/'专业四级'…）、'无'（简历明写未通过）、''（简历里没写）。
    # 不能写成 `i.get("cet_level") or "" in _want_lv`：
    # Python 里 `'' in ['六级']` 恒为 True（空串是任何串的子串），
    # 于是筛"六级"会把**全部没写四六级的人**都放进来 —— 看起来"筛了没用"。
    # 也不能让 '' 命中'未提供'之外的任何值，两者对 HR 的含义完全相反。
    if cet:
        _parts = [c.strip() for c in str(cet).split(",") if c.strip()]
        _want_lv = [c for c in _parts if c != "未提供"]
        _want_missing = "未提供" in _parts
        items = [i for i in items
                 if ((i.get("cet_level") or "") in _want_lv
                     if (i.get("cet_level") or "") else _want_missing)]

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
    items = _attach_skills(conn, items, with_skills=not light)

    if skill:
        raw = [skill] if isinstance(skill, str) else list(skill)
        wants = [str(s).strip().lower() for s in raw if str(s).strip()]
        if wants:
            items = [i for i in items
                     if any(w in str(s).lower()
                            for w in wants for s in (i.get("skills") or []))]

    if keyword:
        # 关键词已在 SQL 阶段筛过（见上），这里无需重复——重复会导致
        # "轻量模式下二次过滤把技能命中的人筛掉"的老问题回归。
        pass
    return items


_EDU_RANK = {"大专": 1, "专科": 1, "本科": 2, "学士": 2, "研究生": 3, "硕士": 3, "博士": 4}
# 公开别名：server 层做「最低学历」筛选与"无法判定人数"统计时复用同一把尺子，
# 避免两处各写一份阶梯、将来口径漂移。
EDU_RANK = _EDU_RANK

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



def _group_honors(raw) -> dict:
    """把 honors_json 按 奖学金/荣誉/论文/专利 分组（缺类补空）。"""
    out = {"奖学金": [], "荣誉": [], "论文": [], "专利": []}
    try:
        items = json.loads(raw) if raw else []
    except (ValueError, TypeError):
        items = []
    for it in items or []:
        k = (it or {}).get("kind")
        if k in out:
            out[k].append({"name": (it or {}).get("name") or "",
                           "evidence": (it or {}).get("evidence") or ""})
    return out


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
    for _a in apps:
        # 每条投递的状态标签同样按实际情况给（完整档案的投递记录表要用）
        _a["status_display"], _a["status_hint"] = app_status_display(_a)
    docs = [_decode(dict(r)) for r in conn.execute(
        "SELECT * FROM documents WHERE candidate_id = ? ORDER BY id DESC", (cid,)).fetchall()]
    # v1.27：档案里简历附件与其他附件**分开两组**下发。
    # 为什么在服务端拆而不是让前端 filter：`documents[0]`（原文取用）必须是简历
    # ——若让前端自己过滤，它按下标取"最新一条"时可能取到作品集，简历原文就空了。
    # 口径：老库无 kind 列时 `_ensure_columns` 已补 'resume' 默认值，
    # 兜底再判一次 `kind != 'other'`（而不是 `== 'resume'`），
    # 免得手工改过库的记录两边都不显示。
    resume_docs = [d for d in docs if (d.get("kind") or "resume") != "other"]
    other_docs = [d for d in docs if (d.get("kind") or "resume") == "other"]
    for _d in docs:
        _d["kind"] = _d.get("kind") or "resume"
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
    # v1.25.6：完整档案里也给出"缺什么"——按**当前数据**实时算（姓名 / 年限 / 技能，
    # 与 tier.py 的口径一致），不看旧 breakdown，老数据也能说清；
    # 档案端有装配好的 skills，三项都可靠可判。
    _missing: list[str] = []
    if not (c.get("name") or "").strip():
        _missing.append("姓名")
    if c.get("years_exp") is None:
        _missing.append("年限")
    if not skills:
        _missing.append("技能")
    return {
        **c,
        "applications": apps,
        "documents": docs,
        # v1.27：简历附件 / 其他附件分开。`documents` 保留全量（老消费方不变），
        # 界面用这两个分组渲染两张表。
        "resume_documents": resume_docs,
        "other_documents": other_docs,
        "skills": skills,
        "tags": tags,
        "latest": latest,
        "tier_suggested": latest.get("tier_suggested"),
        "tier_final": latest.get("tier_final"),
        "score": latest.get("score"),
        "stage": latest.get("stage"),
        "status": latest.get("status"),
        # v1.29.2：面试信息**提到顶层**。档案弹层的「面试信息」区读的是 `d.*`，
        # 而这些值只存在于最新那条投递上 —— 之前没往顶层拷，界面输入框**永远是空的**，
        # HR 看到"这一步必填"的提示却又看不到已登记的单位，
        # 很可能就再填一遍（值倒是一样的，但会多一条审计，看着像改过）。
        # 与 `stage`/`status` 同一处理：那也是"当前这条投递的状态"。
        "application_id": latest.get("id"),
        "interview_unit": latest.get("interview_unit"),
        "interview_date": latest.get("interview_date"),
        "interview_slot": latest.get("interview_slot"),
        "interview_mode": latest.get("interview_mode"),
        "outcome": latest.get("outcome"),
        "offer_sent_at": latest.get("offer_sent_at"),
        "signed_at": latest.get("signed_at"),
        "onboard_deadline": latest.get("onboard_deadline"),
        "onboard_status": latest.get("onboard_status"),
        # v1.25.7：投递备注不再占用顶层 note（改名 app_note 保留数据）；
        # 顶层 note 现在由 **c 提供 = 候选人的人工备注（HR 工作记录）。
        "app_note": latest.get("note"),
        "needs_review": latest.get("needs_review"),
        # v1.25.6：给完整档案用的"缺什么"清单与"原文是否读得出"（界面直接展示）
        "missing_fields": _missing,
        # v1.27：`parse_failed` / `raw_text` / `file_name` / `file_path` 全部改用
        # **简历附件组**（resume_docs）而不是 docs。
        # 原因：docs 是按 id 倒序的全量附件，最新一条很可能是作品集（kind='other'），
        # 而作品集 `parse_ok` 常为真、`raw_text` 是空的 —— 那样"简历读不出"
        # 会假性消失、简历原文变空。全程只读 docs[0] 的写法在这条数据下是错的。
        "parse_failed": bool(resume_docs) and resume_docs[0].get("parse_ok") is not None
                        and not resume_docs[0].get("parse_ok"),
        "reasons": latest.get("reasons") or [],
        "risks": latest.get("risks") or [],
        "hits": latest.get("hits") or [],
        "miss": latest.get("miss") or [],
        "raw_text": (resume_docs[0].get("raw_text") if resume_docs else "") or "",
        "file_name": resume_docs[0].get("file_name") if resume_docs else None,
        # 注意：守卫必须在**取值之前**。此前写成
        # `docs[0].get("archived_path") or (docs[0].get("file_path") if docs else None)`
        # —— `or` 左侧先求值，`docs` 为空时 `docs[0]` 直接 IndexError，
        # 于是"没有任何附件的候选人"（手工录入、或附件被清理过的）打开完整档案会 500。
        "file_path": ((resume_docs[0].get("archived_path") or resume_docs[0].get("file_path"))
                      if resume_docs else None),
        "tier_effective": latest.get("tier_final") or latest.get("tier_suggested"),
        # v1.27：英语四六级。`cet` 是简历原文表述（库里存的那份），
        # `cet_level` 是归一后的档位（界面下拉与人才库筛选都用它）。
        # 两个都下发：档案里 HR 要看到原文（与原件一致），也要能按档位改。
        "cet_level": extract_mod.cet_level(c.get("cet")),
        # v1.29.2：学历院校经历（库里是 JSON 字符串，这里解成列表给界面）
        "edu_history": _loads_edu_history(c.get("edu_history")),
        # v1.29.4（业务方）：档案「教育经历」要标 985/211 —— 与列表层同一口径
        # （`list_candidates` 里早就有这两个字段，详情层此前漏了），
        # 最高学历院校与第一学历院校的层次在这里一并算好下发。
        "uni_tier": univ_mod.uni_tier(c.get("school") or ""),
        "bachelor_tier": univ_mod.uni_tier(c.get("first_degree_school") or ""),
        # v1.29.4（业务方）：来源邮件（标题/发件人/收信时间）随档案下发，
        # 展示在「简历附件」上方；非邮件来源（文件夹/上传）为 None。
        "source_mail": _source_mail_of(conn, resume_docs),
        # v1.17：荣誉/论文/专利（按类分组，界面直接渲染）
        "honors": _group_honors(c.get("honors_json")),
    }


def merge_candidates(conn: sqlite3.Connection, src_id: int, dst_id: int,
                     operator: str = "HR", role: str = "recruiter") -> dict:
    """软合并：把 src 的投递/文档/技能/标签挂到 dst，src 标记 merged_into（可拆）。"""
    if src_id == dst_id:
        return {"ok": False, "error": "不能与自身合并"}
    src, dst = get_candidate(conn, src_id), get_candidate(conn, dst_id)
    if not src or not dst:
        return {"ok": False, "error": "候选人不存在"}
    # v1.13.6：搬过去的同时**记下来源**，否则"撤销合并"无法把它们搬回来
    conn.execute("UPDATE applications SET candidate_id = ?, merged_from_candidate = ?, "
                 "updated_at = ? WHERE candidate_id = ?",
                 (dst_id, src_id, now(), src_id))
    conn.execute("UPDATE documents SET candidate_id = ?, merged_from_candidate = ? "
                 "WHERE candidate_id = ?", (dst_id, src_id, src_id))
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
    """撤销软合并：**把当初搬过去的投递与附件搬回来**（v1.13.6）。

    为什么必须搬：合并只是"挂过去"，撤销就必须"挂回来"——否则拆分后源档案
    变成一个没有任何投递的空壳，而它的投递永久留在主档下（实测缺陷）。

    只搬**带 `merged_from_candidate = src_id` 标记**的行：那些才是当初从本档
    搬过去的。合并发生在加标记之前的老数据没有这个痕迹 → **如实说明无法自动搬回**，
    不假装拆干净了（HR 可以到主档手工处理）。

    技能/标签不搬：它们是派生属性，本档下次分析/刷新时会重新生成。
    """
    # 先看合并记录（拆分要写在同一批里，避免中途失败留下半拆状态）
    src = get_candidate(conn, src_id)
    dst_id = (src or {}).get("merged_into")
    moved = {"applications": 0, "documents": 0, "legacy_untracked": False}
    if dst_id:
        cur = conn.execute(
            "UPDATE applications SET candidate_id = ?, merged_from_candidate = NULL, "
            "updated_at = ? WHERE candidate_id = ? AND merged_from_candidate = ?",
            (src_id, now(), dst_id, src_id))
        moved["applications"] = cur.rowcount or 0
        cur = conn.execute(
            "UPDATE documents SET candidate_id = ?, merged_from_candidate = NULL "
            "WHERE candidate_id = ? AND merged_from_candidate = ?", (src_id, dst_id, src_id))
        moved["documents"] = cur.rowcount or 0
        # 合并过、但一条都没标记 → 老数据（加标记之前合的），如实说
        if not moved["applications"] and not moved["documents"]:
            moved["legacy_untracked"] = bool(conn.execute(
                "SELECT 1 FROM candidates WHERE id = ? AND merged_into = ?",
                (src_id, dst_id)).fetchone())
    conn.execute("UPDATE candidates SET merged_into = NULL, pool_status = '在池', updated_at = ? WHERE id = ?",
                 (now(), src_id))
    _detail = (f"恢复为独立档，投递搬回 {moved['applications']} 条、附件 {moved['documents']} 份"
               if not moved["legacy_untracked"] else
               "恢复为独立档；注意：这次合并发生在系统记录来源之前，"
               "投递无法自动搬回（如需归位请到主档手工处理）")
    add_audit(conn, "candidate", str(src_id), "split", "已并入其他档", _detail, operator, role)
    conn.commit()
    return {"ok": True, "id": src_id, "moved": moved, "note": _detail}


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
         # `rec.get("applied_at", stamp)` 在 key 存在但值为 None 时会返回 None，
         # 写成 NULL 后排序会把它甩到最后（COALESCE 到空串），档案页"最新投递"就取错行。
         (rec.get("applied_at") or stamp), rec.get("resume_doc_id"), rec.get("score"),
         rec.get("tier_suggested"), None, rec.get("stage") or stages_mod.DEFAULT_STAGE,
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
    # v1.14.1：**不再顺手把复核状态置成"已确认"**。
    # 档位（tier_final）与复核（status）是两件独立的事——HR 可以认同系统给的 A 档
    # 而不改任何值；以前逼着"改档位"才能消掉标签，等于诱导 HR 污染自己的判断。
    # 复核请走独立的 review_mark()。
    conn.execute(
        "UPDATE applications SET tier_final = ?, note = ?, updated_at = ? WHERE id = ?",
        (tier, note_val, now(), aid),
    )
    add_audit(conn, "application", str(aid), "set_tier", str(before), str(tier), operator, role)
    conn.commit()
    return get_application(conn, aid)



def review_mark(conn: sqlite3.Connection, aid: int, operator: str = "HR",
                role: str = "recruiter") -> dict | None:
    """标记「HR 已复核这条建议」（**不改档位**）。

    为什么要有这个独立动作（v1.14.1）：系统给的建议档要么认可、要么改。
    如果"认可"只能通过改档位来表达，HR 为了消掉标签就会去动档位——
    而 HR 真正的判断（tier_final）被"顺手确认"污染了，是更坏的事。
    所以：认可 = 点一下复核（写 review 审计）；改判 = 改档位（写 set_tier 审计）。两件事分开记。
    """
    cur = get_application(conn, aid)
    if not cur:
        return None
    if (cur.get("status") or "") == "已确认":
        return cur  # 已经复核过，不重复写审计
    conn.execute("UPDATE applications SET status = '已确认', updated_at = ? WHERE id = ?",
                 (now(), aid))
    add_audit(conn, "application", str(aid), "review",
              f"建议档 {cur.get('tier_suggested') or '（无）'}",
              "HR 复核认可（未改档位）", operator, role)
    conn.commit()
    return get_application(conn, aid)


def unreview_mark(conn: sqlite3.Connection, aid: int, operator: str = "HR",
                  role: str = "recruiter") -> dict | None:
    """撤销复核（回到"未复核"）。用于 HR 觉得"我还没想清楚"时。"""
    cur = get_application(conn, aid)
    if not cur:
        return None
    conn.execute("UPDATE applications SET status = '待确认', updated_at = ? WHERE id = ?",
                 (now(), aid))
    add_audit(conn, "application", str(aid), "unreview", "已复核", "撤销复核（回到未复核）",
              operator, role)
    conn.commit()
    return get_application(conn, aid)


#: 建单**之后**可更新的 applications 字段（**白名单**）。
#: 为什么要有白名单：`insert_application` 是**显式列清单**，新增列漏加进去会
#: "传了却静默丢弃"（`cet`、`grad_date` 各栽过一次）。这个白名单让"建单后写入"
#: 有一条明确的、可枚举的通道，配合自检里的覆盖判据：
#: `_ADD['applications']` 的每一列，必须落在 insert 列清单或本白名单里 —— 二者皆无即报错。
APPLICATION_WRITABLE = (
    "outcome", "interview_unit", "interview_date", "interview_slot", "interview_mode",
    "interviewed_at", "offer_sent_at", "signed_at", "onboard_deadline", "onboard_status",
)

#: 写审计时字段的中文名（缺一个就会在审计文案里露出英文键名）
APPLICATION_FIELD_LABEL = {
    "outcome": "结论", "interview_unit": "面试单位", "interview_date": "面试日期",
    "interview_slot": "面试时段", "interview_mode": "面试方式",
    "interviewed_at": "进入面试时间", "offer_sent_at": "录用函发送时间",
    "signed_at": "签约时间", "onboard_deadline": "入职最后期限",
    "onboard_status": "入职情况",
}


def update_application_fields(conn: sqlite3.Connection, aid: int, *,
                              operator: str = "HR", role: str = "recruiter",
                              outcome: str | None = None, **fields) -> dict | None:
    """更新一条投递的业务字段（**白名单**，逐字段写审计）。

    只认 `APPLICATION_WRITABLE` 里的列，白名单外的键**直接忽略**（不报错）——
    这样调用方多传一个内部字段不会炸，但也绝不会悄悄写进库。
    返回更新后的投递；没变化则原样返回且不写审计（避免"保存了但什么都没改"制造噪音）。

    ⚠️ `outcome` 单独提出来是为了能区分"**不改结论**"与"**把结论清空**"：
    `outcome=None` 表示不改；要清空传空串 `""`。两者在流程上含义不同
    （拒签→改判拟录用时必须能把结论清掉），用一个 None 表达不了。
    """
    cur = get_application(conn, aid)
    if not cur:
        return None
    patch: dict = {}
    if outcome is not None:
        patch["outcome"] = str(outcome).strip() or None
    for k, v in fields.items():
        if k in APPLICATION_WRITABLE and k != "outcome":
            patch[k] = v if v is None or isinstance(v, str) else str(v)

    changed = {k: v for k, v in patch.items()
               if (cur.get(k) or None) != (v or None)}
    if not changed:
        return cur
    _stamp = now()
    sets = ", ".join(f"{k} = ?" for k in changed)
    conn.execute(f"UPDATE applications SET {sets}, updated_at = ? WHERE id = ?",
                 (*changed.values(), _stamp, aid))
    # 审计逐字段写：改前改后都留痕，"谁把结论从拒签改回拟录用"能查
    for k, v in changed.items():
        add_audit(conn, "application", str(aid), "set_field",
                  str(cur.get(k) or "（空）"),
                  f"{APPLICATION_FIELD_LABEL.get(k, k)} → {v or '（空）'}", operator, role)
    conn.commit()
    return get_application(conn, aid)


def set_application_stage(conn: sqlite3.Connection, aid: int, stage: str,
                          outcome: str | None = None,
                          operator: str = "HR", role: str = "recruiter",
                          skip_auto_archive: bool = False) -> dict | None:
    """推进投递阶段，可选同时写结论标签，并在**走到「已结束」时**归档。

    ⚠️ **归档只有两个入口**（业务方 2026-10-16 明确）：
      ① 阶段推进到「已结束」；② HR 点「归档」（单个 / 批量 / 按年）。
      **其它任何操作都不归档** —— 改档位、改面试信息、发邮件、导出、打结论标签
      （拟录用 / 不录用）都只是改字段。
      理由：归档会把人从人才库**移走**（不可逆的隐藏动作，档案与原件都还在），
      而"信息填全了""标签打好了"这类中间状态远没到该收起来的时候 ——
      之前把"打标签"顺手带上归档，就会出现"刚标了不录用、人就没了、
      HR 改判回来还得去归档页捞"的荒谬情况。

    v1.29 新增：
      - `outcome`：结论标签。**必须属于该阶段**（`已面试`→拟录用/不录用、
        `已发放offer`→已签约/拒签），否则拒绝 —— 挡住「已面试·已签约」这种
        不存在的组合。传 None 表示不改结论。**打标签不动阶段**。
      - 进入「已面试」时写 `interviewed_at`（只在首次写；重复推进不覆盖）。
        它的**唯一用途**是圈定"曾到过已面试"的导出行集 —— 详见 `list_interviewed`。
      - 走到「已结束」时：该候选人**所有**投递都到终点才归档
        （`count_pending_applications`："必须全部到终点才算处理完"）。

    ⚠️ **防双向环**：`set_candidate_archived(archived=True)` 里有反向联动 ——
    归档时把该人进行中的投递统一置为「已结束」。两个方向同时存在。
    `skip_auto_archive=True` 就是用来切断这条潜在环的：归档流程内部的写操作
    **不得**再触发归档判定。**不要**把这个参数当成可选项删掉。
    """
    cur = get_application(conn, aid)
    if not cur:
        return None
    st = str(stage or "").strip()
    if not st:
        return get_application(conn, aid)
    if not stages_mod.is_valid_stage(st):
        raise ValueError(f"未知阶段「{st}」；可用阶段：{'、'.join(stages_mod.all_stages())}")
    _from = str(cur.get("stage") or "")
    # v1.29：这一步**必填**的字段没过就先拦住（业务方裁定：面试单位在进入待面试之前就要填）。
    # 放在 db 层而不是界面层：界面有三条推进路径（卡片下拉 / 管道下拉 / 智能助手提案），
    # 只在界面拦 = 另两条绕过；助手那条更糟，会生成一个注定失败的提案。
    _req = stages_mod.required_fields(_from, st)
    if _req:
        _missing = [k for k in _req if not str(cur.get(k) or "").strip()]
        if _missing:
            _labels = "、".join(stages_mod.REQUIRED_FIELD_LABEL.get(k, k) for k in _missing)
            raise ValueError(
                f"从「{_from}」推进到「{st}」之前必须先填：{_labels}。"
                + (stages_mod.require_hint(_from, st) or ""))
    oc = str(outcome).strip() if outcome is not None else None
    # 结论标签必须属于"离开的那个环节"（见 stages.outcome_allowed_from）：
    # 只按目标阶段校验的话，`已面试→已结束+不录用` 会被自己拦住——流程根本走不通。
    if oc and not stages_mod.outcome_allowed_from(_from, st, oc):
        _ok = stages_mod.outcomes_of(_from) + [
            o for o in stages_mod.outcomes_of(st) if o not in stages_mod.outcomes_of(_from)]
        raise ValueError(
            f"「{_from} → {st}」这一步不能打结论「{oc}」；"
            + (f"可选：{'、'.join(_ok)}" if _ok else "这一步没有结论标签"))

    _stamp = now()
    sets = ["stage = ?", "stage_changed_at = ?", "updated_at = ?"]
    vals: list = [st, _stamp, _stamp]
    # 结论：只在调用方明确传了才动（None = 保留原值，"" = 清空）
    if outcome is not None:
        sets.insert(1, "outcome = ?")
        vals.insert(1, oc or None)
    # 「已面试」写首次进入时间：已有值就不覆盖，否则"第一次面试时间"会被反复改掉
    if st == "已面试" and not cur.get("interviewed_at"):
        sets.insert(1, "interviewed_at = ?")
        vals.insert(1, _stamp)
    conn.execute(f"UPDATE applications SET {', '.join(sets)} WHERE id = ?", (*vals, aid))
    _desc = st + (f"（{oc}）" if oc else "")
    add_audit(conn, "application", str(aid), "set_stage", cur.get("stage"), _desc, operator, role)
    conn.commit()

    # ---- 流程走完 → 自动归档（只在该阶段被标记为 auto_archive_on 时）----
    # ⚠️ **自动归档只有这一处**。原先 `server.api_set_stage` 里还写了一份
    # "到「已结束/已入职」就归档"的**无条件**版本 —— 那是错的：一个人可能同时
    # 在两个岗位的流程里，结束其中一个不代表该归档（`count_pending_applications`
    # 的 docstring 写得很清楚："必须全部到终点"）。两处并存必然分叉，
    # 所以服务端那份已删除，改为读本函数返回的 `_auto_archived`。
    auto_archived = False
    if not skip_auto_archive and stages_mod.is_auto_archive(st):
        cid = cur.get("candidate_id")
        if cid:
            left = count_pending_applications(conn, int(cid))
            c = get_candidate(conn, int(cid))
            if left == 0 and c and not (c.get("archived_at") or ""):
                # 显式 skip：归档内部会把"进行中"的投递置为已结束，
                # 若那里回头再调本函数就会形成环（见 docstring）
                set_candidate_archived(conn, int(cid), archived=True,
                                       operator=operator, role=role,
                                       skip_auto_archive=True)
                auto_archived = True
    out = get_application(conn, aid)
    if out is not None:
        # 给调用方（服务端/助手）一个明确的"刚才顺手归档了"信号。
        # 用下划线前缀与普通业务字段区分开，和 candidates 上的 `_ended_applications` 同一约定。
        out["_auto_archived"] = auto_archived
    return out
    return get_application(conn, aid)


def set_candidate_archived(conn: sqlite3.Connection, cid: int, archived: bool,
                           operator: str = "HR", role: str = "hr",
                           skip_auto_archive: bool = False) -> dict | None:
    """候选人软归档（v1.4）：归档后不再出现在人才库与检索，集中在「归档」页展示。

    是"归档"不是"删除"：档案、投递、附件、审计全部原样保留，可随时取消归档。
    逐次写审计（archive / unarchive），谁在什么时候归档的可追溯。

    v1.8.4：归档时**自动结束该候选人还在进行中的投递流程**。
    归档的含义就是"这个人处理完了"——若留着流程挂在"新投递/初面"，
    人才库里看不到、归档页里也不显眼，就成了没人管的孤儿数据。
    结束了多少条放在返回值的 `_ended_applications` 里，由调用方提示给 HR。

    v1.29：`skip_auto_archive` 是给**自动归档流程自己**用的标志 ——
    自动归档由 `set_application_stage` 在"所有投递都已到终点"时触发，
    而本函数内部又会把"进行中"的投递置为已结束。两向并存，
    这个参数把潜在的反向环切断（见 `set_application_stage` 的说明）。
    """
    cur = get_candidate(conn, cid)
    if not cur:
        return None
    ts = now() if archived else None
    conn.execute("UPDATE candidates SET archived_at = ? WHERE id = ?", (ts, cid))
    ended = 0
    if archived:
        # 终态表从配置取（v1.29）：不再写死 `(已结束, 已入职)` 两个值——
        # 「已入职」已按业务方裁定去掉，写死的话历史值会被当成"未结束"，
        # 卡住上面那条自动归档，也会混进"进行中投递"统计。
        _ph, _fin = _finished_in_clause()
        cur2 = conn.execute(
            f"""UPDATE applications SET stage = ?, updated_at = ?
                WHERE candidate_id = ? AND stage NOT IN ({_ph})""",
            (STAGE_ENDED, now(), cid, *_fin))
        ended = cur2.rowcount or 0
        if ended:
            add_audit(conn, "candidate", str(cid), "auto_end_stage", "",
                      f"归档时自动结束 {ended} 条进行中的投递流程", operator, role)
    add_audit(conn, "candidate", str(cid), "archive" if archived else "unarchive",
              "未归档" if archived else "已归档",
              "已归档（移入「归档」页，不再在人才库与检索中展示）" if archived
              else "取消归档（恢复在人才库与检索中展示）", operator, role)
    conn.commit()
    out = get_candidate(conn, cid) or {}
    out["_ended_applications"] = ended
    return out


def count_pending_applications(conn: sqlite3.Connection, cid: int) -> int:
    """该候选人还有几条投递没走到终点。

    用于"流程都结束了 → 自动归档"的判断：一个人可能同时在两个岗位的流程里，
    结束其中一个不代表该归档，**必须全部到终点**才算处理完。

    ⚠️ v1.29 之前这个函数**从未被任何代码调用过**（死代码）——
    "流程全结束 → 自动归档"这个设计早就写好了，只是没接线。
    `set_application_stage` 现在会调它。
    """
    _ph, _fin = _finished_in_clause()
    row = conn.execute(
        f"""SELECT COUNT(*) AS n FROM applications
            WHERE candidate_id = ? AND stage NOT IN ({_ph})""",
        (int(cid), *_fin)).fetchone()
    return int(row["n"] if row else 0)


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
                         operator: str = "system", role: str = "system",
                         only_ids: list[int] | None = None) -> dict:
    """彻底删除**已归档**的档案（连带投递、附件记录、标签、技能、向量）。

    设计取舍：
    - **删除的前提是"先归档"**，不需要归档满 30 天。
      v1.24.8 起HR 要求取消冷静期：归档页点「立即彻底删除」当场生效。
      防误删改由这几道承担：必须先归档 → 界面二次确认 → 审计永久留痕
      → 原件移入 `data/removed/` 而不是直接抹掉（可手工捞回）。
    - 只删数据库记录，**原件区文件由调用方物理删除**（`file_path` / `archived_path`
      随返回值一起给出，服务层逐个 `unlink`）。
      v1.25.9：归档页「彻底删除」就是真删，不再移入回收目录堆文件。
    - 每个被删除的人写一条 `purge` 审计（含归档时间与投递/附件数量），
      审计本身**不删除**——"谁什么时候被清掉的"必须留痕。

    `only_ids`：**只删指定的几个人**（单人"彻底删除"接口用）。
    为什么必须有这个参数：原来单人接口调的是不带过滤的本函数，
    于是 HR 点"彻底删除"一个人，**所有**归档满 30 天的人都被一起删掉，
    而接口只回报他要的那一个——不可逆的数据丢失，且没有任何提示。
    传 None 时退化为"只清已满 `days` 天的"，供定时任务使用——
    **这样取消冷静期不会波及自动清理任务**。
    """
    days = PURGE_AFTER_DAYS if days is None else days
    if days <= 0:
        return {"purged": 0, "days": days, "people": [], "files": [], "disabled": True}
    # 只要求"已归档"（archived_at 非空），**不再要求归档满 30 天**——
    # HR 要求取消冷静期：归档页点"立即彻底删除"就能删。
    # 防误删的两道闸移到别处：① 必须先归档（回收站外的人根本没这个按钮）
    # ② 界面删除前有二次确认；③ 审计永久留痕；④ 原件一并物理删除（不可恢复）。
    rows_all = conn.execute(
        "SELECT id, name, archived_at FROM candidates "
        "WHERE COALESCE(archived_at,'') != '' ORDER BY id").fetchall()
    if only_ids is not None:
        _ids = {int(x) for x in only_ids}
        rows = [r for r in rows_all if r["id"] in _ids]
        cutoff = None# 单人接口不按天数筛（v1.24.8 取消冷静期）
    else:
        # 定时任务仍只清到期者：接口走 only_ids，这里兜底批量时按天数过滤
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
        rows = [r for r in rows_all if (r["archived_at"] or "") <= cutoff]
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
        conn.execute("DELETE FROM candidate_insights WHERE candidate_id = ?", (cid,))
        conn.execute("DELETE FROM embeddings WHERE owner_type = 'candidate' AND owner_id = ?",
                     (cid,))
        conn.execute("DELETE FROM documents WHERE candidate_id = ?", (cid,))
        conn.execute("DELETE FROM applications WHERE candidate_id = ?", (cid,))
        conn.execute("DELETE FROM candidates WHERE id = ?", (cid,))
        how = (f"归档 {r['archived_at']} 后由 HR 立即彻底删除" if cutoff is None
              else f"归档满 {days} 天自动清理")
        add_audit(conn, "candidate", str(cid), "purge",
                  f"{r['name']}（{r['archived_at']} 归档）",
                  f"{how}：{n_app} 条投递、{n_doc} 份附件（原件已物理删除）",
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
    """写入附件台账。返回文档 id。

    v1.23.5：**不再用裸 `INSERT OR IGNORE`**。它的本意只是"同一哈希不重复写"，
    但 `OR IGNORE` 会连**列不存在、类型不符、非空约束**这类真错误一起吞掉——
    实测踩到：新加的 `sensitive_found` 列在旧库上还没迁移时，
    插入被静默忽略，结果是**简历进了库、附件台账却没有这一条**（丢记录还查不出来）。
    现在只吞唯一键冲突：命中则返回既有 id；其它异常照抛。
    """
    try:
        cur = conn.execute(
            """INSERT INTO documents
               (file_hash, candidate_id, application_id, file_name, file_path, archived_path,
                mime, size, received_at, source_message_id, raw_text, text_len,
                parse_engine, parse_ok, sensitive_found, kind, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rec.get("file_hash"), rec.get("candidate_id"), rec.get("application_id"),
             rec.get("file_name"), rec.get("file_path"), rec.get("archived_path"),
             rec.get("mime"), rec.get("size"), rec.get("received_at", now()),
             rec.get("source_message_id"), rec.get("raw_text", ""), len(rec.get("raw_text") or ""),
             rec.get("parse_engine"),
             # v1.27：`parse_ok` 有三态 —— True 解析成功 / False 解析失败 /
             # **None 未解析**（非简历附件刻意不解析）。
             # 原来写的是 `1 if rec.get("parse_ok", True) else 0`：显式传 None
             # 会被当成 False，界面把作品集显示成"简历读不出"——又是一次
             # "None 当 False"的坑（与 pipeline 里 db.get_setting 同类）。
             # 只有 `is False` / `0` 才记 0，None 原样存 NULL。
             (None if rec.get("parse_ok", True) is None
              else (1 if rec.get("parse_ok", True) else 0)),
             rec.get("sensitive_found"),
             # v1.27：附件性质。老库/未显式给的一律按 'resume'，
             # 与列默认值一致 —— 不给会让"完整档案"里的简历附件表少行。
             (rec.get("kind") or "resume"), now()),
        )
        conn.commit()
        return cur.lastrowid
    except sqlite3.IntegrityError:
        # 唯一键冲突：同一份文件（file_hash）已经入过账，返回既有 id
        row = conn.execute("SELECT id FROM documents WHERE file_hash = ?",
                           (rec.get("file_hash"),)).fetchone()
        if row:
            return int(row["id"] if not isinstance(row, tuple) else row[0])
        raise


def update_document(conn: sqlite3.Connection, did: int, **fields) -> None:
    if not fields:
        return
    _assert_valid_columns(conn, "documents", fields)
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


# ============================================================
# 操作审计的中文表述（v1.25）
# ============================================================
# 背景（用户反馈）：
#   ① 界面上直接把 `view` / `set_stage` 这种**英文动作码**摊给 HR 看，读不懂；
#   ② 「查看档案」这类**只看不改**的记录混在里面，纯噪音；
#   ③ 已发过邮件的候选人没有标记。
#
# 口径：**审计照旧全量落库**（本地日志该留的都留），
#       但**给 HR 看的列表只显示"确实改动了数据"的动作**，且一律中文、
#       一句话说明"做了什么"。
#
# ⚠️ 动作名是从代码里 `add_audit(...)` 的真实调用点**扫出来**的
#   （`_scan_audit_actions.py`，55 组），不是凭记忆列的 ——
#   只看数据库会漏掉"将来才会用到"的动作，到时界面又冒英文。

#: 只读 / 无数据变更的动作：不进 HR 视图（数据仍在库里，随时可查）。
AUDIT_READONLY = {
    "view",                 # 查看档案
    "preview",              # 预览来源文件
    "download",             # 下载原件
    "bundle_download",      # 打包下载
    "export",               # 导出
    "scan",                 # 系统巡检（只建提案、不改档案）
    "analyze_pending",      # 统计待分析数量
    "duplicate_skipped",    # 导入时跳过重复
    "oversize_skipped",     # 导入时跳过超大文件
    "identity_weak_match",  # 弱身份匹配提示
    # 系统自动重建技能：确实写库，但它是**系统自动触发**且量极大
    # （实测一个库里 50 条），对 HR 的决策没有任何含义。
    # 归入"不展示"，避免把真正该看的改动淹掉。
    "refresh_skills",
    "backfill_grad_date",   # 自动回填毕业时间，同上
}

#: 动作 → 中文。多个实体同名动作语义不同时，用 `AUDIT_LABEL_BY_ENTITY` 覆盖。
AUDIT_LABEL = {
    "view": "查看了档案", "preview": "预览了来源文件", "download": "下载了简历原件",
    "bundle_download": "打包下载了简历", "export": "导出了数据", "scan": "跑了一次系统巡检",
    "analyze_pending": "统计了待分析数量", "duplicate_skipped": "导入时跳过了重复简历",
    "oversize_skipped": "导入时跳过了超大文件", "identity_weak_match": "提示了身份存疑",
    "refresh_skills": "系统重建了技能", "backfill_grad_date": "系统回填了毕业时间",
    "ingest": "导入了简历", "set_stage": "推进了阶段", "set_tier": "调整了档位",
    "edit_fields": "更正了字段", "add_tag": "加了标签", "add_note": "加了备注",
    "archive": "归档了", "unarchive": "取消归档了", "unarchive_on_reapply": "重新投递，自动取消归档",
    "merge": "合并了重复档案", "split": "撤销了合并", "review": "标记为已复核",
    "unreview": "取消了已复核", "reanalyze": "重跑了分析", "reanalyze_full": "完整重新分析",
    "reanalyze_batch": "批量重跑了分析", "regrade": "按岗位 JD 重算了档位",
    "regrade_batch": "批量重算了岗位档位", "update_jd": "修改了岗位 JD",
    "job_bound": "归到了岗位", "assign_job": "归到了岗位", "reassign_job": "修改了岗位",
    "suggest_job": "给出岗位建议", "route_suggest": "给出了岗位建议",
    "auto_end_stage": "系统自动结束了投递", "purge": "清理了到期数据",
    "delete": "删除了数据", "clear": "清空了数据", "remove": "移入了回收站",
    "restore": "从回收站恢复", "archive_pinned": "归档了原件",
    "import_domain": "导入了领域词表", "send": "发送了邮件",
    "chat_clear": "清空了对话", "chat_restore": "恢复了对话", "chat_purge": "清理了对话记录",
    "update": "修改了", "edit_settings": "修改了系统设置", "edit_config": "修改了配置",
    # 提案的处置：`db.decide_proposal` 传的 action 就是 decision 本身
    "approve": "确认执行了提案", "reject": "拒绝了提案",
}

#: 审计文案里**必须打码/解密**的字段。
AUDIT_MASK_FIELDS = {"phone_enc", "email_enc", "phone", "email"}


#: 字段名 → 中文（审计 before/after 里出现的裸字段名要翻译成 HR 能看懂的字）。
AUDIT_FIELD_LABEL = {
    "name": "姓名", "edu_level": "学历", "school": "学校", "major": "专业",
    "years_exp": "工作年限", "gender": "性别", "grad_date": "毕业时间",
    "current_org": "现单位", "phone": "电话", "email": "邮箱",
    "phone_enc": "电话", "email_enc": "邮箱", "tier_suggested": "建议档",
    "tier_final": "最终档", "stage": "阶段", "job_id": "岗位", "note": "备注",
    "tags": "标签", "score": "匹配分",
    # v1.27：英语四六级。放在这个 dict 的位置无关 —— `humanize_audit` 会按
    # key **长度倒序**替换（短的先替会把长的切碎），加新键不必考虑书写顺序。
    "cet": "英语四六级",
    "first_degree_school": "第一学历（本科）院校",
}


#: 系统级操作人 → 中文展示名（单 HR 系统里用户名就是 hr，直接显示太像英文）。
AUDIT_OPERATOR_LABEL = {"hr": "招聘 HR", "system": "系统", "anonymous": "未登录"}


def audit_field_value(field: str, value) -> str:
    """给审计文案用的字段值：**解密 + 电话打码**。

    为什么要这一步（v1.25 实测踩到）：`update_candidate_fields` 的入参里，
    电话/邮箱可能已经是 `enc:…` 密文（前端或工具层先加密过）。
    原样写进 `after`，界面上就是这种东西：

        更正了字段：… → 电话=enc:B+3i8tlA6rhm8ZztuDBjOfq0l0zswK4GEYQiGEmrK5YkRlBZxvb0

    —— 对 HR 毫无意义，还等于把密文抄进了日志。**审计是为了给人读的**，
    写进去的东西必须是人能看懂的。
    顺带：电话一律打码，避免审计表变成一份新的 PII 台账。
    """
    v = str(value or "")
    if v.startswith("enc:"):
        try:
            from .crypto import decrypt          # 延迟导入：避免 db↔crypto 循环
            v = decrypt(v) or ""
        except Exception:
            return "（已加密，无法还原）"
    v = v.strip()
    if not v:
        return "（空）"
    if field in AUDIT_MASK_FIELDS and "phone" in field and len(v) >= 7:
        return v[:3] + "*" * max(4, len(v) - 7) + v[-4:]
    return v

#: 同名的动作在实体不同时说法不同（如 create）。
AUDIT_LABEL_BY_ENTITY = {
    ("candidate", "create"): "新建了候选人档案",
    ("job", "create"): "新建了岗位",
    ("department", "create"): "新建了部门",
    ("application", "new_version"): "收到该候选人的一份新投递",
    ("application", "new_version_archived"): "收到新投递（档案已归档，未自动恢复）",
    ("job", "deactivate"): "停用了岗位",
    ("department", "deactivate"): "停用了部门",
    ("settings", "update"): "修改了系统设置",
}

#: entity → 中文对象名（没有就用原值，避免显示成英文）
AUDIT_ENTITY_LABEL = {
    "candidate": "候选人", "application": "投递", "job": "岗位",
    "department": "部门", "document": "简历原件", "mail": "邮件",
    "proposal": "提案", "settings": "系统设置", "source_file": "来源文件",
    "ontology": "领域词表", "chat": "对话", "bundle": "打包", "feedback": "反馈",
    "insight": "分析", "interview_dict": "面试字典", "mailbox": "邮箱配置",
}


def audit_label(entity: str, action: str) -> str:
    """把 (实体, 动作) 翻成一句中文。查不到就退化成去掉下划线的原文，
    **绝不把英文动作码直接摊给 HR**。"""
    key = (entity or "", action or "")
    if key in AUDIT_LABEL_BY_ENTITY:
        return AUDIT_LABEL_BY_ENTITY[key]
    if action in AUDIT_LABEL:
        return AUDIT_LABEL[action]
    return (action or "").replace("_", " ")


def audit_visible(entity: str, action: str) -> bool:
    """这条记录该不该出现在 HR 的审计列表里（只看不改的不展示）。"""
    return (action or "") not in AUDIT_READONLY


_ENC_RE = re.compile(r"enc:[A-Za-z0-9+/=_\-]{8,}")


def _translate_field_names(text: str) -> str:
    """把审计文案里裸字段名替换成中文，兼容历史记录。

    典型场景：完整重新分析写入
        years_exp=（空）；current_org=（空） → years_exp=0；current_org=西安电子科技大学
    展示层替换成：
        工作年限=（空）；现单位=（空） → 工作年限=0；现单位=西安电子科技大学
    """
    if not text:
        return text or ""
    out = text
    # 先替换最长的，避免短的把长的前缀吃掉（如 phone 不应影响 phone_enc）
    for k, v in sorted(AUDIT_FIELD_LABEL.items(), key=lambda x: -len(x[0])):
        pattern = rf"(^|[；：,\s]){re.escape(k)}(?==|[；：,\s]|$)"
        out = re.sub(pattern, rf"\1{v}", out)
    return out


def sanitize_audit_text(text: str) -> str:
    """把审计文案里嵌着的 `enc:…` 密文换成可读内容。

    为什么展示层也要做一遍：写入点已经改成走 `audit_field_value` 了，
    但**历史记录**里已经躺着几条密文（实测 3 条）。只修写入点的话，
    老记录仍然是一串看不懂的字符 —— 而审计的意义就是让人看懂。
    这层是**只读展示**的清洗，不回头改库（审计一经写入就不该被静默篡改）。
    解密失败时只说"（加密内容）"，不猜、不留半截密文。
    """
    if not text:
        return ""

    def _rep(m):
        try:
            from .crypto import decrypt               # 延迟导入：避免循环依赖
            plain = decrypt(m.group(0)) or ""
        except Exception:
            return "（加密内容）"
        return _ENC_RE.sub("（加密内容）", plain) if "enc:" in plain else (plain or "（加密内容）")

    out = _ENC_RE.sub(_rep, text)
    # 顺手把电话打码，避免审计表变成新的 PII 台账
    out = re.sub(r"(?<!\d)(1\d{2})\d{4}(\d{4})(?!\d)", r"\1****\2", out)
    # 历史记录里曾有 Python None 直接字符串化：eg. "归到 ... 建议为 None（None）"
    out = re.sub(r"\bNone\b", "—", out)
    return out


def _decorate_audit(row: dict) -> dict:
    row["before"] = _translate_field_names(sanitize_audit_text(row.get("before") or ""))
    row["after"] = _translate_field_names(sanitize_audit_text(row.get("after") or ""))
    row["action_label"] = audit_label(row.get("entity"), row.get("action"))
    row["entity_label"] = AUDIT_ENTITY_LABEL.get(row.get("entity"), row.get("entity"))
    # 「做了什么」：一句中文 + 变化。before/after 都为空时（多数动作）就只留中文。
    b, a = (row.get("before") or "").strip(), (row.get("after") or "").strip()
    row["readable"] = row["action_label"] + (f"：{b} → {a}" if b and a else (f"：{a}" if a else ""))
    # 操作人：系统默认角色用中文展示，真实用户名保持原样。
    op = (row.get("operator") or "").strip().lower()
    row["operator"] = AUDIT_OPERATOR_LABEL.get(op, row.get("operator"))
    return row


def list_audit(conn: sqlite3.Connection, entity: str | None = None,
               entity_id: str | None = None, limit: int = 100,
               include_readonly: bool = False) -> list[dict]:
    """读审计记录。默认**只返回确实改动了数据的**（HR 视图）。

    只读动作（查看档案、下载、预览…）默认不出现在这里 —— 它们量最大、
    对 HR 的判断没有意义。但**数据一条没删**：需要全量时传
    `include_readonly=True`（等价于查 `/api/audit?include_readonly=1`），
    智能助手的 list_audit 工具走的就是这条全量路径。

    每行都补三个给界面用的字段：`action_label`（中文动作）、
    `entity_label`（中文对象）、`readable`（一句话"做了什么"）。
    """
    sql, args = "SELECT * FROM audit_log", []
    where = []
    if entity:
        where.append("entity = ?")
        args.append(entity)
    if entity_id:
        where.append("entity_id = ?")
        args.append(str(entity_id))
    if not include_readonly:
        # 只读动作不进 HR 视图。数据仍在库里（这是**展示过滤**，不是删除），
        # 需要全量时传 include_readonly=True。
        skip = sorted(AUDIT_READONLY)
        where.append("COALESCE(action,'') NOT IN (%s)" % ",".join("?" * len(skip)))
        args.extend(skip)
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    return [_decorate_audit(_decode(dict(r))) for r in conn.execute(sql, args).fetchall()]


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
    # v1.25：把**发信记录**并进这条时间线。
    # 原来只并了 candidate/application/document 三类，于是"给这个人发过面试邀请"
    # 这件事在他的档案里看不到 —— 而 HR 恰恰最需要这一条（重复发信就是这么发生的）。
    keys.append(("mail", str(cid)))
    clauses = " OR ".join("(entity = ? AND entity_id = ?)" for _ in keys)
    args: list = []
    for ent, eid in keys:
        args.extend([ent, eid])
    skip = sorted(AUDIT_READONLY)
    clauses = f"({clauses}) AND COALESCE(action,'') NOT IN (%s)" % ",".join("?" * len(skip))
    args.extend(skip)
    args.append(int(limit))
    sql = f"SELECT * FROM audit_log WHERE {clauses} ORDER BY id DESC LIMIT ?"
    return [_decorate_audit(_decode(dict(r))) for r in conn.execute(sql, args).fetchall()]


def candidate_email_state(conn: sqlite3.Connection, cid: int) -> dict:
    """这位候选人**发过邮件没有**，以及最近一次的时间与主题。

    判定口径（v1.25.8 起）：`entity='mail'` + `action='send'` + `before='ok'`。
    `before` 是结构化结果码（ok/fail），发信路径里显式写入，不再靠人写的
    `after` 摘要里的「成功」子串——本项目吃过"靠匹配文案字符串做判断"的亏
    （改一次文案就静默失效）。老数据没有 before 标记，回退到 `after` 含「成功」。
    """
    rows = conn.execute(
        "SELECT ts, COALESCE(after,'') AS a, COALESCE(before,'') AS b FROM audit_log "
        "WHERE entity='mail' AND action='send' AND entity_id=? "
        "AND (before='ok' OR instr(COALESCE(after,''),'成功') > 0) "
        "ORDER BY id DESC LIMIT 20",
        (str(cid),)).fetchall()
    if not rows:
        return {"sent": False, "count": 0, "last_at": "", "last_subject": ""}
    subject = ""
    m = re.search(r"主题：([^｜]*)", rows[0]["a"])
    if m:
        subject = m.group(1).strip()
    return {"sent": True, "count": len(rows), "last_at": rows[0]["ts"] or "",
            "last_subject": subject}


# ============================================================
# 提案（写入类工具的前置确认）
# ============================================================

def create_proposal(conn: sqlite3.Connection, session_id: str, tool: str, args: dict,
                    summary: str, risk: str = "中",
                    source: str = "hr_asked", dedup_key: str | None = None) -> int:
    """建一条待确认提案。

    `source`：hr_asked = HR 提问后模型产生；agent_auto = 系统自己巡检发现。
    两者**走同一条确认流**，绝不因为"是系统自己发现的"就自动执行。
    `dedup_key`：主动提案的去重键（如 `stage_stuck:app#12`），配合
    `recent_proposal_exists()` 防止同一件事天天提醒、把 HR 烦到忽略整个列表。
    """
    # 敏感值（SMTP 授权码 / 模型 API Key / 候选人电话邮箱）**不能明文落库**：
    # proposals.args 用 json.dumps 原样存、decide_proposal 又不清 args，
    # 明文会永久留存并随库备份流动。这里用 AES-GCM 加密后落库，
    # apply_proposal 执行时再解密（见 actions 里对应的 decrypt 处）。
    # 注意：db 层不 import crypto（避免环），走延迟导入。
    args = _seal_sensitive_args(args)
    cur = conn.execute(
        """INSERT INTO proposals (session_id, tool, args, summary, risk, status,
                                  source, dedup_key, created_at)
           VALUES (?,?,?,?,?,'待确认',?,?,?)""",
        (session_id, tool, json.dumps(args, ensure_ascii=False), summary, risk,
         source, dedup_key, now()),
    )
    conn.commit()
    return cur.lastrowid


#: 提案 args 里需要加密落库的敏感键（见 _seal_sensitive_args）。
#: 命中的键值会被 AES-GCM 加密为 `enc:<ciphertext>` 形式，apply_proposal 再解密。
_SENSITIVE_ARG_KEYS = {"password", "pass", "api_key", "apikey", "secret",
                       "token", "auth_code", "phone_enc", "email_enc"}


def _seal_sensitive_args(args: dict) -> dict:
    """递归加密提案 args 里的敏感值，避免明文落库。返回新 dict，不改入参。

    直接用 crypto.encrypt 的 enc:/plain: 约定——有主密钥时是 AES-GCM 密文，
    降级（无密钥）时是 plain: 前缀的明文标记，与 phone_enc/email_enc 同口径。
    """
    from . import crypto
    out: dict = {}
    for k, v in (args or {}).items():
        key = str(k).lower()
        if key in _SENSITIVE_ARG_KEYS and isinstance(v, str) and v \
                and not v.startswith(("enc:", "plain:")):
            out[k] = crypto.encrypt(v) or ""
        elif isinstance(v, dict):
            out[k] = _seal_sensitive_args(v)
        else:
            out[k] = v
    return out


def open_sensitive_args(args: dict) -> dict:
    """解密 _seal_sensitive_args 落库后的 args（apply_proposal 用）。幂等。"""
    from . import crypto
    out: dict = {}
    for k, v in (args or {}).items():
        if isinstance(v, str) and v.startswith(("enc:", "plain:")) and len(v) > 6:
            out[k] = crypto.decrypt(v) or v[6:]
        elif isinstance(v, dict):
            out[k] = open_sensitive_args(v)
        else:
            out[k] = v
    return out


def _mask_sensitive_args(args: dict) -> dict:
    """把敏感键（含已加密的 enc:...）掩码为「已设置」，供前端展示。"""
    out: dict = {}
    for k, v in (args or {}).items():
        if str(k).lower() in _SENSITIVE_ARG_KEYS and v not in (None, ""):
            out[k] = "（已设置）"
        elif isinstance(v, dict):
            out[k] = _mask_sensitive_args(v)
        else:
            out[k] = v
    return out


def recent_proposal_exists(conn: sqlite3.Connection, dedup_key: str, days: int = 7) -> bool:
    """去重窗口：同一 dedup_key 在 N 天内出现过（不论是否已处理）就不再重复提。"""
    if not dedup_key:
        return False
    row = conn.execute(
        """SELECT 1 FROM proposals
           WHERE dedup_key = ? AND created_at >= datetime('now', ?) LIMIT 1""",
        (dedup_key, f"-{int(days)} days"),
    ).fetchone()
    return bool(row)


def get_proposal(conn: sqlite3.Connection, pid: int) -> dict | None:
    return _row(conn.execute("SELECT * FROM proposals WHERE id = ?", (pid,)).fetchone())


def list_proposals(conn: sqlite3.Connection, status: str | None = None, limit: int = 50,
                   source: str | None = None) -> list[dict]:
    sql, args = "SELECT * FROM proposals", []
    conds, cond_args = [], []
    if status:
        conds.append("status = ?")
        cond_args.append(status)
    if source:
        conds.append("source = ?")
        cond_args.append(source)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    args.extend(cond_args)
    sql += " ORDER BY id DESC LIMIT ?"
    args.append(int(limit))
    out = []
    for r in conn.execute(sql, args).fetchall():
        d = _decode(dict(r))
        # v1.25：带上中文工具名。界面上原来直接显示 `set_stage` 这种英文码，
        # HR 读不懂。复用已有的 TOOL_LABEL（与提案卡、对话历史同源，
        # 不另建一份映射 —— 否则"三处说法不一致"的老问题会再来一遍）。
        d["tool_label"] = TOOL_LABEL.get(d.get("tool") or "",
                                         audit_label("proposal", d.get("tool") or ""))
        # 敏感值已加密落库（enc:...），下发给前端前统一掩码，
        # 既不出密文也不出明文，只提示「已设置」。
        if isinstance(d.get("args"), str):
            try:
                _a = json.loads(d["args"])
                d["args"] = json.dumps(_mask_sensitive_args(_a), ensure_ascii=False)
            except Exception:
                pass
        out.append(d)
    return out


# ============================================================
# 系统自动分析结果（入库即分析）
# ============================================================

def upsert_insight(conn: sqlite3.Connection, candidate_id: int, application_id: int, *,
                   summary: str, reasons: list | None = None, risks: list | None = None,
                   evidence: list | None = None, source: str = "auto_ingest",
                   model: str = "", business_direction: str | None = None) -> None:
    """写入/覆盖某条投递的分析结果（同投递只留最新一条，重分析即覆盖）。

    `business_direction`：模型从简历提炼的「业务方向」（如 材料工艺 / 后端开发），
    与专业方向互补——专业看学历背景，业务看实际干过的事。
    """
    conn.execute(
        """INSERT INTO candidate_insights
               (candidate_id, application_id, summary, reasons, risks, evidence,
                source, model, business_direction, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT (candidate_id, application_id) DO UPDATE SET
               summary = excluded.summary, reasons = excluded.reasons,
               risks = excluded.risks, evidence = excluded.evidence,
               source = excluded.source, model = excluded.model,
               business_direction = excluded.business_direction,
               created_at = excluded.created_at""",
        (candidate_id, application_id, summary,
         json.dumps(reasons or [], ensure_ascii=False),
         json.dumps(risks or [], ensure_ascii=False),
         json.dumps(evidence or [], ensure_ascii=False),
         source, model, business_direction, now()),
    )
    conn.commit()


def candidates_needing_insight(conn: sqlite3.Connection, limit: int = 20) -> list[dict]:
    """列出**还没有分析结论**的投递（"手动批量补分析"用，v1.13.4）。

    口径：每个人的**最新一条投递**，且它在 `candidate_insights` 里没有记录。
    为什么要它：把"入库即分析"做成可关的开关后，如果不给一条明确的补分析路径，
    库里就会积一批"从没分析过"的档案，而且没人知道——直到有人问"为什么这个人没有分析"。
    同一条投递只列一次（避免一人多投递时重复出现）。

    排序按投递时间从早到晚：先补最早进来的（等得最久的）。
    """
    rows = conn.execute(
        """SELECT a.id AS application_id, a.candidate_id, c.name, a.job_id,
                  COALESCE(a.applied_at, a.created_at) AS since
             FROM applications a
             JOIN candidates c ON c.id = a.candidate_id
             WHERE COALESCE(c.archived_at, '') = ''
               AND a.id = (SELECT id FROM applications WHERE candidate_id = c.id
                           ORDER BY COALESCE(applied_at, '') DESC, id DESC LIMIT 1)
               AND NOT EXISTS (SELECT 1 FROM candidate_insights i
                               WHERE i.candidate_id = a.candidate_id
                                 AND i.application_id = a.id)
             ORDER BY since ASC LIMIT ?""", (int(limit),)).fetchall()
    return [dict(r) for r in rows]


def count_needing_insight(conn: sqlite3.Connection) -> int:
    """待分析人数（只算数量，用于列表页显示按钮上的数字）。"""
    return int(conn.execute(
        """SELECT COUNT(*) AS n FROM applications a
             JOIN candidates c ON c.id = a.candidate_id
            WHERE COALESCE(c.archived_at, '') = ''
              AND a.id = (SELECT id FROM applications WHERE candidate_id = c.id
                          ORDER BY COALESCE(applied_at, '') DESC, id DESC LIMIT 1)
              AND NOT EXISTS (SELECT 1 FROM candidate_insights i
                              WHERE i.candidate_id = a.candidate_id
                                AND i.application_id = a.id)""").fetchone()["n"])


def get_insight(conn: sqlite3.Connection, candidate_id: int,
                application_id: int | None = None) -> dict | None:
    """取分析结果；不指定投递时取该候选人最新一条。"""
    if application_id is None:
        row = conn.execute(
            """SELECT * FROM candidate_insights WHERE candidate_id = ?
               ORDER BY id DESC LIMIT 1""", (candidate_id,)).fetchone()
    else:
        row = conn.execute(
            """SELECT * FROM candidate_insights
               WHERE candidate_id = ? AND application_id = ?""",
            (candidate_id, application_id)).fetchone()
    if not row:
        return None
    return _decode_insight(dict(row))


def insights_for(conn: sqlite3.Connection, candidate_ids: list[int]) -> dict[int, dict]:
    """批量取多个候选人的最新分析（列表页用，一次查询避免 N+1）。"""
    ids = [int(c) for c in candidate_ids if c is not None]
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    rows = conn.execute(
        f"""SELECT * FROM candidate_insights WHERE candidate_id IN ({marks})
            ORDER BY id ASC""", ids).fetchall()
    out: dict[int, dict] = {}
    for r in rows:              # ASC 遍历：后面的覆盖前面的，最终留下最新一条
        d = _decode_insight(dict(r))
        out[d["candidate_id"]] = d
    return out


def _decode_insight(d: dict) -> dict:
    for k in ("reasons", "risks", "evidence"):
        try:
            d[k] = json.loads(d.get(k) or "[]")
        except (ValueError, TypeError):
            d[k] = []
    return d


# ============================================================
# 每日待办摘要
# ============================================================

def save_brief(conn: sqlite3.Connection, brief_date: str, payload: dict) -> None:
    """按日期存摘要（同一天重算即覆盖，保证幂等）。"""
    conn.execute(
        """INSERT INTO daily_briefs (brief_date, payload, created_at) VALUES (?,?,?)
           ON CONFLICT (brief_date) DO UPDATE SET
               payload = excluded.payload, created_at = excluded.created_at""",
        (brief_date, json.dumps(payload, ensure_ascii=False), now()),
    )
    conn.commit()


def get_brief(conn: sqlite3.Connection, brief_date: str) -> dict | None:
    row = conn.execute("SELECT * FROM daily_briefs WHERE brief_date = ?",
                       (brief_date,)).fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["payload"] = json.loads(d.get("payload") or "{}")
    except (ValueError, TypeError):
        d["payload"] = {}
    return d


# ============================================================
# 邮件模板（v1.8.3）
# ============================================================

def list_mail_templates(conn: sqlite3.Connection, enabled_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM mail_templates"
    if enabled_only:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY scene, id"
    return [dict(r) for r in conn.execute(sql).fetchall()]


def get_mail_template(conn: sqlite3.Connection, tid: int) -> dict | None:
    row = conn.execute("SELECT * FROM mail_templates WHERE id = ?", (int(tid),)).fetchone()
    return dict(row) if row else None


def upsert_mail_template(conn: sqlite3.Connection, name: str, scene: str,
                         subject: str, body: str, tid: int | None = None) -> int:
    """新增或更新模板。模板名唯一——重名时报错优于静默覆盖别人写的模板。"""
    now_ = now()
    if tid:
        conn.execute(
            """UPDATE mail_templates SET name=?, scene=?, subject=?, body=?, updated_at=?
               WHERE id=?""",
            (name, scene, subject, body, now_, int(tid)))
        conn.commit()
        return int(tid)
    cur = conn.execute(
        """INSERT INTO mail_templates (name, scene, subject, body, enabled,
                                       created_at, updated_at)
           VALUES (?,?,?,?,1,?,?)""",
        (name, scene, subject, body, now_, now_))
    conn.commit()
    return cur.lastrowid


def delete_mail_template(conn: sqlite3.Connection, tid: int) -> None:
    conn.execute("DELETE FROM mail_templates WHERE id = ?", (int(tid),))
    conn.commit()


# 阶段口径：**从 config/stages.json 读**，不再在这里写字面量（v1.29 收口）。
#: 流程终点：到了这个阶段就不再推进。原来是 `(已入职, 已结束)` 两个，
#: v1.29 按业务方裁定去掉「已入职」（已签约的人停在「已发放offer」），只留「已结束」。
STAGE_ENDED = "已结束"
_FINISHED_STAGES = tuple(stages_mod.terminal_stages()) or (STAGE_ENDED,)


def _loads_edu_history(raw) -> list:
    """`candidates.edu_history`（JSON 字符串）→ 列表。坏值给空列表，绝不抛。"""
    if not raw:
        return []
    if isinstance(raw, list):
        return raw
    try:
        v = json.loads(raw)
    except (ValueError, TypeError):
        return []
    if not isinstance(v, list):
        return []
    # v1.29.2：**逐段挂院校层次**（985/211/强势）。HR 判断学校水平是顺着学历
    # 一路看下去的 —— 只在档案开头标一个「最高学历」等于把中间的段抹掉了。
    # 层次判定复用 `univ.uni_tier`（同一份名单，前端不自己判）。
    for h in v:
        if isinstance(h, dict):
            h["tier"] = univ_mod.uni_tier(h.get("school") or "")
    return v


def _source_mail_of(conn: sqlite3.Connection, resume_docs: list) -> dict | None:
    """简历附件对应的**来源邮件**（标题 / 发件人 / 收信时间）。取不到返回 None。

    v1.29.4（业务方）：邮件标题本身是"应聘意向的表达"（也参与按标题归岗），
    展示在档案「简历附件」上方；文件夹导入 / 上传没有来源邮件 → None，界面不渲染。
    纯展示信息，任何异常都不该影响档案打开。
    """
    try:
        sid = next((d.get("source_message_id") for d in resume_docs
                    if d.get("source_message_id")), None)
        if not sid:
            return None
        row = conn.execute(
            "SELECT subject, from_addr, received_at FROM email_messages "
            "WHERE message_id = ?", (sid,)).fetchone()
        if not row:
            return None
        return {"subject": row["subject"] or "", "from": row["from_addr"] or "",
                "received_at": row["received_at"] or ""}
    except Exception:                                    # noqa: BLE001
        return None


def _plus_one_day(day: str) -> str:
    """`YYYY-MM-DD` → 次日（同格式）。解析不出来就原样返回（宁可筛松也不报错）。

    用途：投递时间段的上界。界面只给到「日」，而库里是 `YYYY-MM-DD HH:MM:SS`，
    字符串比较下 `2026-10-15 09:00` > `2026-10-15` —— 用 `<= date_to` 会把
    当天投的全漏掉，HR 会以为"今天没人投"。取次日再比 `<` 才是"含当天"。
    """
    try:
        return (datetime.strptime(str(day)[:10], "%Y-%m-%d")
                + timedelta(days=1)).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return str(day)[:10]


def _finished_in_clause() -> tuple[str, tuple]:
    """终态集合的 `NOT IN (?, ?, ...)` 占位串与参数（跟着配置走，不写死个数）。

    为什么要有这个 helper：原来两处 SQL 都写死 `NOT IN (?, ?)` 并绑
    `(已结束, 已入职)`。终态个数一变（v1.29 就去掉了一个），写死的地方
    要么漏改、要么得手动数问号——**问号个数和参数个数对不上是会静默取错的**。
    """
    return ",".join("?" * len(_FINISHED_STAGES)), _FINISHED_STAGES


def pending_workload(conn: sqlite3.Connection, stuck_days: int = 7,
                     limit: int = 20) -> dict:
    """待办全景：主动提案与每日摘要共用这一个查询源。

    为什么不各写各的：如果提案用一套口径、摘要用另一套，两边数字对不上，
    HR 第一反应会是"系统自己都没搞清"，信任度直接掉。**一个来源，两处消费。**
    """
    threshold = (datetime.now() - timedelta(days=int(stuck_days))).strftime("%Y-%m-%d %H:%M:%S")
    base = """
        FROM applications a
        JOIN candidates c ON c.id = a.candidate_id
        LEFT JOIN jobs j ON j.id = a.job_id
        WHERE COALESCE(c.archived_at, '') = ''
    """
    # 1) 待确认档位（系统给了建议，HR 还没背书）
    #    v1.13 收窄：**必须真的有建议档位**才算——未归岗、或档位还没算出来的投递
    #    没有档可确认，混进来会让「确认 X 位候选人的档位」这条待办名不副实。
    #    排序也改按投递时间（v1.12 已删掉加权打分，score 恒为 NULL，按它排没有意义）。
    confirm = conn.execute(
        f"""SELECT a.id, a.candidate_id, c.name, a.score, a.tier_suggested,
                   a.tier_final, a.stage, a.job_id, j.title AS job_title,
                   COALESCE(a.applied_at, a.created_at) AS since
            {base} AND a.tier_final IS NULL AND a.tier_suggested IS NOT NULL
                  AND a.status != '已确认'
            ORDER BY COALESCE(a.applied_at, a.created_at) DESC LIMIT ?""",
        (int(limit),)).fetchall()
    # 2) 待指定岗位（有建议岗位但没归岗）
    #    这里不复用 base：需要额外 join 一次 jobs（拿"建议岗位"的标题），
    #    FROM 子句与 base 不同，硬拼会漏 join 导致 no such column。
    pending_job = conn.execute(
        """SELECT a.id, a.candidate_id, c.name, a.score, a.tier_suggested,
                  a.suggested_job_id, sj.title AS suggested_job_title,
                  COALESCE(a.applied_at, a.created_at) AS since
           FROM applications a
           JOIN candidates c ON c.id = a.candidate_id
           LEFT JOIN jobs j  ON j.id  = a.job_id
           LEFT JOIN jobs sj ON sj.id = a.suggested_job_id
           WHERE COALESCE(c.archived_at, '') = ''
             AND a.job_id IS NULL AND a.suggested_job_id IS NOT NULL
           ORDER BY a.score DESC LIMIT ?""", (int(limit),)).fetchall()
    # 3) 停滞投递（非终点阶段、超期未动）
    stuck = conn.execute(
        f"""SELECT a.id, a.candidate_id, c.name, a.score, a.tier_final, a.stage,
                   a.job_id, j.title AS job_title,
                   COALESCE(a.stage_changed_at, a.applied_at, a.created_at) AS since
            {base} AND a.stage NOT IN ({','.join('?' * len(_FINISHED_STAGES))})
                  AND COALESCE(a.stage_changed_at, a.applied_at, a.created_at) <= ?
            ORDER BY since ASC LIMIT ?""",
        (*_FINISHED_STAGES, threshold, int(limit))).fetchall()
    # 4) 待人工判读（解析失败，原件还在，等人看一眼）
    review = conn.execute(
        f"""SELECT a.id, a.candidate_id, c.name, a.stage, a.needs_review,
                   COALESCE(a.applied_at, a.created_at) AS since
            {base} AND a.needs_review = 1
            ORDER BY since ASC LIMIT ?""", (int(limit),)).fetchall()
    # 5) 高分未确认（最该先看的那批）
    #    v1.13：v1.12 删掉加权打分后 `score` 恒为 NULL，原来按「分数 ≥ 0.85」永远选不出人，
    #    这条待办实际上死掉了（HR 再也看不到"很合适但没背书"的提醒）。
    #    新口径 = **建议档 A 且未确认**；同时保留 `score ≥ 0.85` 分支兼容**老库**里
    #    还带着分数的历史投递（不因为口径升级就把老数据漏掉）。
    high = conn.execute(
        f"""SELECT a.id, a.candidate_id, c.name, a.score, a.tier_suggested, a.stage
            {base} AND a.tier_final IS NULL
                  AND (a.tier_suggested = 'A' OR a.score >= 0.85)
            ORDER BY COALESCE(a.applied_at, a.created_at) ASC LIMIT ?""",
        (int(limit),)).fetchall()

    def rows(rs) -> list[dict]:
        return [dict(r) for r in rs]

    items = {"pending_confirm": rows(confirm), "pending_job": rows(pending_job),
             "stuck": rows(stuck), "needs_review": rows(review),
             "high_score": rows(high)}
    counts = {k: len(v) for k, v in items.items()}
    return {"stuck_days": int(stuck_days), "threshold": threshold,
            "counts": counts, "items": items}


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
    _assert_valid_columns(conn, "email_messages", fields)
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


#: 提案工具名 → HR 看得懂的中文（v1.25）。
#: 放在 db 层是因为**两个地方都要用**：实时对话（loop）与历史回放（chat_history）。
#: 分成两份迟早会分叉——本项目已经吃过"两处定义不一致"的亏（权限名 archive、
#: 写工具清单 5 vs 14）。一份定义，两处引用。
TOOL_LABEL = {
    "set_stage": "推进阶段", "set_tier": "调整档位", "add_tag": "添加标签",
    "add_note": "添加备注", "merge_candidates": "合并档案",
    "split_candidate": "撤销合并", "assign_job": "归岗 / 改岗位",
    "suggest_job": "建议岗位", "mark_review": "标记已复核",
    "set_archive": "归档 / 取消归档", "archive_batch": "批量归档",
    "archive_by_year": "按年归档", "create_job": "新建岗位",
    "update_job_jd": "修改岗位 JD", "regrade_job": "重算岗位档位",
    "update_candidate_fields": "更正候选人字段",
    "reanalyze_candidate": "完整重新分析", "reanalyze_batch": "批量重新分析",
    "route_pending": "待归岗批处理",
    "update_interview_dict": "维护面试字典",
    "save_mail_config": "修改发信配置", "save_model_config": "修改模型配置",
    "save_settings": "修改系统设置",
    "remove_source_files": "移入回收站", "restore_source_files": "从回收站恢复",
    "rebuild_brief": "重算今日待办", "clear_chat_history": "清空对话历史",
    "draft_email": "起草邮件", "preview_mail": "预览邮件模板",
    "list_mail_templates": "查看邮件模板", "get_mail_template": "查看模板正文",
}


def proposal_changes(conn: sqlite3.Connection, proposal: dict) -> list[dict]:
    """把提案 args 拆成「字段 / 改前 / 改后」，供卡片直接展示。

    ⚠️ **改前值要现查库**，不能用工具当时读到的值缓存进 args ——
    提案可能隔几分钟、甚至跨天才被 HR 点确认，期间别人（或巡检）可能已经改过。
    展示一个过期值会让 HR 以为"现在是这样"，比不显示更危险。
    查不到就留空（前端显示「（空）」），绝不猜。
    """
    a = proposal.get("args")
    if isinstance(a, str):
        try:
            a = json.loads(a)
        except Exception:
            a = None
    if not isinstance(a, dict):
        return []
    # 敏感值落库前被加密（create_proposal → _seal_sensitive_args），展示前先解密，
    # 否则「改后」栏会把 enc:... 密文显示出来。解密失败（密钥已变）时退回掩码。
    try:
        a = open_sensitive_args(a)
    except Exception:
        a = {k: ("（已设置）" if str(k).lower() in _SENSITIVE_ARG_KEYS else v)
             for k, v in a.items()}
    out: list[dict] = []

    def _q(sql, params):
        try:
            return conn.execute(sql, params).fetchone()
        except Exception:
            return None

    tool = proposal.get("tool")
    if tool == "update_candidate_fields":
        fields = a.get("fields") or {}
        label = {"name": "姓名", "edu_level": "学历", "school": "学校",
                 "major": "专业", "phone_enc": "电话", "email_enc": "邮箱"}
        cid = a.get("candidate_id")
        cur = {}
        if cid is not None:
            row = _q("SELECT name, edu_level, school, major, phone_enc, email_enc "
                     "FROM candidates WHERE id=?", (cid,))
            if row:
                try:
                    from .crypto import decrypt
                    cur = {"name": row["name"] or "", "edu_level": row["edu_level"] or "",
                           "school": row["school"] or "", "major": row["major"] or "",
                           "phone_enc": decrypt(row["phone_enc"]) or "",
                           "email_enc": decrypt(row["email_enc"]) or ""}
                except Exception:
                    cur = {}
        for k, v in fields.items():
            if k in label:
                out.append({"field": k, "label": label[k],
                            "before": cur.get(k, ""), "after": v or ""})

    elif tool == "set_stage":
        aid, stage = a.get("application_id"), a.get("stage")
        row = _q("SELECT stage FROM applications WHERE id=?", (aid,)) if aid else None
        out.append({"field": "stage", "label": "阶段",
                    "before": (row["stage"] if row else ""), "after": stage or ""})

    elif tool == "set_tier":
        aid, tier = a.get("application_id"), a.get("tier")
        row = _q("SELECT COALESCE(tier_final, tier_suggested, '') AS t "
                 "FROM applications WHERE id=?", (aid,)) if aid else None
        out.append({"field": "tier", "label": "档位",
                    "before": (row["t"] if row else ""), "after": tier or ""})

    elif tool in ("set_archive", "archive_batch", "archive_by_year"):
        out.append({"field": "archived", "label": "归档",
                    "before": ("在池" if a.get("archived") else "已归档"),
                    "after": ("已归档" if a.get("archived") else "在池")})
    return out


def card_for_proposal(conn: sqlite3.Connection, pid: int) -> dict | None:
    """按提案 id 组装卡片。历史回放用 —— 让"我做过的操作"能重现在对话里。"""
    p = get_proposal(conn, pid)
    if not p:
        return None
    cid = None
    a = p.get("args")
    if isinstance(a, str):
        try:
            a = json.loads(a)
        except Exception:
            a = None
    if isinstance(a, dict):
        def _as_int(v):
            try:
                return int(v) if v is not None else None
            except (TypeError, ValueError):
                return None
        cid = _as_int(a.get("candidate_id"))
        if cid is None:
            aid = _as_int(a.get("application_id"))
            if aid is not None:
                row = conn.execute(
                    "SELECT candidate_id FROM applications WHERE id=?", (aid,)).fetchone()
                if row:
                    cid = _as_int(row["candidate_id"])
    return {"proposal_id": p["id"], "tool": p["tool"],
            "tool_label": TOOL_LABEL.get(p["tool"], p["tool"]),
            "summary": p["summary"], "risk": p["risk"],
            "status": p["status"],            # 待确认 / 已执行 / 已拒绝
            "candidate_id": cid,
            "changes": proposal_changes(conn, p)}


def _draft_with_sent(conn: sqlite3.Connection, draft: dict) -> dict:
    """给历史里的草稿卡补一个 `sent` 标记。

    ⚠️ 为什么必须补：草稿卡上有个「确认并发送」按钮，一点就**真的寄出**。
    重建历史时若无条件还原这个按钮，HR 切一次页面再点，
    就会**给候选人重复发一封一模一样的邮件** —— 这比"卡片消失"严重得多。
    判据取审计表：发信成功会写 `entity='mail'` + `action='send'`，
    `after` 里含收件人与主题。用 instr() 做子串匹配（不用 LIKE：
    主题里可能带 % 或 _，会被当通配符）。
    """
    out = dict(draft or {})
    to_addr = (out.get("to") or "").strip()
    subj = (out.get("subject") or "").strip()
    if not to_addr:
        out["sent"] = False
        return out
    try:
        row = conn.execute(
            "SELECT 1 FROM audit_log WHERE entity='mail' AND action='send' "
            "AND instr(COALESCE(after,''), ?) > 0 "
            "AND instr(COALESCE(after,''), '成功') > 0 LIMIT 1",
            (to_addr,)).fetchone()
        out["sent"] = bool(row)
    except Exception:
        out["sent"] = False
    # 主题相同才更可信；但主题可能被改过。这里只在"收件人命中"时保守判为
    # **可能已发**，前端据此只改文案（不再给发送按钮），不影响其他判断。
    out["sent_subject_hint"] = subj
    return out


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
            # v1.25：把本轮产生的**卡片**一并带出去（提案 + 邮件草稿）。
            # 为什么：卡片原先只活在浏览器 DOM 里，切一次页面就被重建抹掉，
            # HR 想回看"我刚才让它做了什么"就没了。这里从库里重建，
            # 刷新/换页/重启都还在 —— 与「没点清空就不该丢」是同一个口径。
            tcs = d.get("tool_calls") or []
            props, drafts = [], []
            for tc in tcs:
                if not isinstance(tc, dict):
                    continue
                pid = tc.get("proposal_id")
                if pid:
                    card = card_for_proposal(conn, int(pid))
                    if card:
                        props.append(card)
                if tc.get("draft"):
                    drafts.append(_draft_with_sent(conn, tc["draft"]))
            messages.append({"role": "assistant", "content": a, "at": d.get("ts"),
                             "run_id": d.get("id"), "mode": d.get("mode"),
                             "rounds": d.get("rounds") or 0,
                             "tools": tcs,
                             "latency_ms": d.get("latency_ms"),
                             "proposals": props, "drafts": drafts,
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

TIER_LABELS = tiers_mod.labels()
#: v1.29：阶段表**不再在这里写**，统一读 `config/stages.json`（见 app/stages.py）。
#: 保留这个名字是为了不动所有 `db.STAGES` 的调用方（含 test 与 agent），
#: 但它现在是"计算出来的"，不是"抄下来的"。
STAGES = stages_mod.all_stages()


def pool_stats(conn: sqlite3.Connection) -> dict:
    """人才库**现状**统计（v1.8.9：只算未归档的人）。

    为什么改口径：归档 = 从人才库收起来。统计若把归档的人算进去，
    HR 归档完一整批人，页面上的"候选人数 / 投递数 / 各档位"纹丝不动，
    看起来像归档没生效——和"管道里还挂着归档的人"是同一类问题。
    需要"一共收过多少"的台账数字用 `*_total`（附件、邮件本来就是台账，不随归档增减）。

    `stages` 的口径（v1.26.0）：每人取最新一条投递；stage 为空、或出现不在 `STAGES`
    里的历史值（旧数据/手工改过），**一律归「新投递」**。前端只渲染 `STAGES` 那 7 个键，
    不归并的话这些人会在比例环里凭空消失，导致环总数 ≠ 候选人数。
    """
    _ARCH = "COALESCE(c.archived_at, '') = ''"
    # 人数也直接数（不再把全库装配成对象）
    people = conn.execute(
        f"""SELECT COUNT(*) AS n FROM candidates c
            WHERE {_ARCH} AND c.merged_into IS NULL""").fetchone()["n"]
    # 档位/阶段分布改用 SQL 聚合（v1.23.5）：原来是把**全库候选人装配成对象**
    # 再在 Python 里数一遍——1000 人时要 60ms，且随人数线性增长。
    # 口径与 list_candidates 完全一致：取每人**最新一条投递**、排除已归档、
    # 档位取 tier_final 优先于 tier_suggested（`tier_effective`）。
    _LATEST = ("a.id = (SELECT id FROM applications WHERE candidate_id = c.id "
               "ORDER BY COALESCE(applied_at,'') DESC, id DESC LIMIT 1)")
    tiers = {t: 0 for t in tiers_mod.all_tiers()}
    for r in conn.execute(
            f"""SELECT COALESCE(a.tier_final, a.tier_suggested) AS t, COUNT(*) AS n
                FROM candidates c JOIN applications a ON {_LATEST}
                WHERE {_ARCH} AND c.merged_into IS NULL
                GROUP BY t""").fetchall():
        if r["t"]:
            tiers[r["t"]] = tiers.get(r["t"], 0) + r["n"]
    stages = {s: 0 for s in STAGES}
    # v1.25.10：环的统计口径 = 人才库里的**所有人**，不看"怎么来的"。
    # 原来 INNER JOIN applications 只数"有投递记录"的人，没投递记录的人凭空消失，
    # 导致环总数 ≠ 候选人数。改 LEFT JOIN：没投递 / stage 为空的都归「新投递」。
    # 非标准 stage 值同样归并（口径见本函数 docstring）。
    for r in conn.execute(
            f"""SELECT COALESCE(NULLIF(a.stage, ''), '新投递') AS s, COUNT(*) AS n
                FROM candidates c LEFT JOIN applications a
                  ON a.candidate_id = c.id AND a.id = (
                     SELECT id FROM applications WHERE candidate_id = c.id
                     ORDER BY COALESCE(applied_at,'') DESC, id DESC LIMIT 1)
                WHERE {_ARCH} AND c.merged_into IS NULL
                GROUP BY s""").fetchall():
        _k = r["s"] if r["s"] in stages else "新投递"
        stages[_k] = stages.get(_k, 0) + r["n"]
    apps = conn.execute(
        f"""SELECT COUNT(*) AS n FROM applications a
            JOIN candidates c ON c.id = a.candidate_id WHERE {_ARCH}""").fetchone()["n"]
    apps_total = conn.execute("SELECT COUNT(*) AS n FROM applications").fetchone()["n"]
    people_total = conn.execute(
        """SELECT COUNT(*) AS n FROM candidates
           WHERE merged_into IS NULL""").fetchone()["n"]
    docs = conn.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"]
    mails = conn.execute("SELECT COUNT(*) AS n FROM email_messages").fetchone()["n"]
    pending_mails = conn.execute(
        "SELECT COUNT(*) AS n FROM email_messages WHERE processed = 0").fetchone()["n"]
    # v1.25.6：统计区新元素（HR 要求）——"近 7 天新增简历"与"最近导入"。
    # received_at 是本地时间字符串；date('now','localtime') 保证与它同口径。
    docs_7d = conn.execute(
        "SELECT COUNT(*) AS n FROM documents "
        "WHERE date(received_at) >= date('now','localtime','-6 days')").fetchone()["n"]
    _last_doc_row = conn.execute(
        "SELECT d.received_at, c.name FROM documents d "
        "LEFT JOIN candidates c ON c.id = d.candidate_id "
        "ORDER BY d.received_at DESC, d.id DESC LIMIT 1").fetchone()
    return {
        "people": people,
        # 含归档的总数：回答"这些年一共收了多少人"时才用得上
        "people_total": people_total,
        "archived": max(0, people_total - people),
        "applications": apps,
        "applications_total": apps_total,
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
        "pending": conn.execute(
            f"""SELECT COUNT(*) AS n FROM candidates c JOIN applications a ON {_LATEST}
                WHERE {_ARCH} AND c.merged_into IS NULL
                  AND COALESCE(a.status, '待确认') != '已确认'""").fetchone()["n"],
        "confirmed": conn.execute(
            f"""SELECT COUNT(*) AS n FROM candidates c JOIN applications a ON {_LATEST}
                WHERE {_ARCH} AND c.merged_into IS NULL
                  AND COALESCE(a.status, '') = '已确认'""").fetchone()["n"],
        "needs_review": conn.execute(
            f"""SELECT COUNT(*) AS n FROM candidates c JOIN applications a ON {_LATEST}
                WHERE {_ARCH} AND c.merged_into IS NULL AND a.needs_review = 1""").fetchone()["n"],
        "in_pool": conn.execute(
            f"""SELECT COUNT(*) AS n FROM candidates c
                WHERE {_ARCH} AND c.merged_into IS NULL
                  AND COALESCE(c.pool_status, '在池') = '在池'""").fetchone()["n"],
        "emails": mails,
        "emails_pending": pending_mails,
        "skills": conn.execute("SELECT COUNT(*) AS n FROM skills").fetchone()["n"],
        "tags": conn.execute("SELECT COUNT(*) AS n FROM tags").fetchone()["n"],
        "proposals_pending": conn.execute(
            "SELECT COUNT(*) AS n FROM proposals WHERE status = '待确认'").fetchone()["n"],
        "embeddings": embedding_count(conn),
        "docs_7d": docs_7d,
        "last_doc": ({"name": _last_doc_row["name"] or "未识别",
                      "at": _last_doc_row["received_at"]} if _last_doc_row else None),
    }


def pipeline_stats(conn: sqlite3.Connection) -> dict:
    """招聘管道视图：各阶段数量 + 停留天数 + 来源分布。

    v1.8.9：**排除已归档的人**。归档的含义就是"这个人这一轮处理完了、从人才库收起来"，
    管道是"在招流程看板"——归档完一批人，管道里还挂着他们，会让 HR 以为归档没生效
    （实测反馈）。要看历史，去「归档」页。
    """
    rows = conn.execute(
        """SELECT a.stage, a.channel, a.applied_at, a.id, a.candidate_id,
                  c.name AS candidate_name, j.title AS job_title,
                  -- v1.29：待面试那一列要能看出"这人归哪个单位面"，否则 HR 认领不了
                  a.interview_unit AS interview_unit,
                  -- v1.29.2：结论标签要展示在管道里（拟录用/已签约/不录用/拒签）
                  a.outcome AS outcome
           FROM applications a
           JOIN candidates c ON c.id = a.candidate_id
           LEFT JOIN jobs j ON j.id = a.job_id
           WHERE COALESCE(c.archived_at, '') = ''
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
        item = {"application_id": r["id"], "candidate_id": r["candidate_id"],
                "candidate_name": r["candidate_name"],
                "job_title": r["job_title"], "days": days, "applied_at": r["applied_at"],
                "channel": r["channel"],
                # v1.29：面试单位（待面试那一列按它认领人）
                "interview_unit": r["interview_unit"],
                # v1.29.2：结论标签（界面与导出同款口径）
                "outcome": r["outcome"]}
        stages.setdefault(st, []).append(item)
        channels[r["channel"] or "未知"] = channels.get(r["channel"] or "未知", 0) + 1
    # v1.25.7：超期阈值可配置（HR 在管道上的「超期设置」里改；默认 15 天）。
    # 在这里读一次设置，别在列表推导里反复查。
    _overdue_days = int(get_setting(conn, "pipeline_overdue_days", 15) or 15)
    return {
        # v1.7.4：管道看板嵌入人才库后要**全部展示**，不再截断每阶段前 20 条
        # （本地单 HR 场景量级小；此前下拉形态截断是为省那一屏）。
        "stages": {s: {"count": len(v), "items": v,
                       "overdue": sum(1 for i in v if i["days"] >= _overdue_days)}
                   for s, v in stages.items()},
        "channels": channels,
        "open_total": sum(len(v) for s, v in stages.items()
                          if s not in set(stages_mod.terminal_stages())),
        "overdue_days": _overdue_days,
    }


def tier_history(conn: sqlite3.Connection, cid: int) -> list[dict]:
    return list_audit(conn, entity=None, entity_id=str(cid), limit=200)
