"""入库编排：邮件/文件夹 → 归档 → 解析 → 抽取 → 归一 → 建档 → 分级 → 待 HR 确认。

**不丢件**的三道保险（与设计方案一一对应）：

1. 附件**落盘归档**（原件区只增不改）**在解析之前**——解析崩了原件也在；
   但**判重必须在落盘之前**，否则重复投递会在原件区留下台账无记录的同哈希副本；
2. 解析失败照样建 Candidate + Application，标 `needs_review`，仍可被检索到；
3. 每一封邮件都写收信台账（`email_messages`），失败原因留痕、可重放；
   重复投递另写 `duplicate_skipped` 审计，保证"为什么没入库"可回答。

**三层去重**（保证零重复）：

=================  ==================================  ==============================
层                  去重键                               解决的问题
=================  ==================================  ==============================
邮件层              `message_id`（email_messages）       同一封邮件被重复拉取
文件层              `SHA256(附件)`（documents）          同一份简历不同文件名
内容层              `identity_key`（candidates）         同一个人不同版本的简历
=================  ==================================  ==============================

内容层命中后**不新建投递**，而是把新简历作为该投递的**新版本附件**归档；
若 HR 已确认过档位则**绝不覆盖**，只追加文档并留审计。
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime

from . import db
from . import mailbox as mb
from . import tiers as tiers_mod
from .identity import build_identity_key
from .pipeline import freshness
from .pipeline import parse as parse_mod
from .pipeline import sanitize
from .pipeline.extract import extract, extract_honors
from .pipeline import subject_meta as subject_meta_mod
from .pipeline import major_llm as major_llm_mod
from .pipeline import majors as majors_mod
from .pipeline import tier as tier_mod


# ---- 后台分析进度表（v1.24.9）：进程级共享，server 的 /api/tasks/{id} 从这里读 ----
# {task_id: {"title","total","done","failed","status","message","started_at"}}
# 导入后的自动分析在后台线程跑，入库 HTTP 已返回，靠这个表让前端轮询到实时进度。
_PROGRESS: dict[str, dict] = {}
_PROGRESS_LOCK = threading.Lock()

#: 可停止任务的中断旗（task_id → Event）。与 _PROGRESS 分开存 ——
#: get_progress 的返回值要能 JSON 序列化（事件对象不能混进去）。
_STOP_FLAGS: dict[str, threading.Event] = {}
_STOP_LOCK = threading.Lock()

#: 任务 → 数据库路径 / 上次落库时间（节流用）。任务进度本身仍在内存里，
#: 数据库只做**重启后可解释**的快照，不能因为写库拖慢每份简历的处理。
_PROGRESS_DB: dict[str, str] = {}
_PROGRESS_LAST: dict[str, float] = {}


def request_stop(task_id: str) -> bool:
    """请求停止一个可停止的后台任务（收取/导入）。返回是否受理。

    语义是**协作式**：执行方在"每处理完一份文件/一封邮件"的边界检查一次，
    读到就停在下一个边界 —— 不会把正在处理的半份砍断，游标也停在
    最后成功处理的那条（下次从那里继续，不重复、不丢件）。
    ⚠️ 已结束的任务不再受理 —— 否则"点停止 → 被回答已请求"会让人以为它还在跑。
    """
    with _STOP_LOCK:
        ev = _STOP_FLAGS.get(task_id)
    if ev is None:
        return False
    with _PROGRESS_LOCK:
        p = _PROGRESS.get(task_id)
    if p is not None and p.get("status") != "running":
        return False
    ev.set()
    return True


def stop_requested(task_id: str | None) -> bool:
    if not task_id:
        return False
    with _STOP_LOCK:
        ev = _STOP_FLAGS.get(task_id)
    return bool(ev and ev.is_set())


def _persist_progress(tid: str, *, force: bool = False) -> None:
    """把任务快照节流写入 background_tasks；失败不影响任务本身。"""
    with _PROGRESS_LOCK:
        p = dict(_PROGRESS.get(tid) or {})
        path = _PROGRESS_DB.get(tid)
        last = _PROGRESS_LAST.get(tid, 0.0)
    if not p or not path:
        return
    now_t = time.time()
    if not force and now_t - last < 1.0:
        return
    _PROGRESS_LAST[tid] = now_t
    data = {k: v for k, v in p.items()
            if k not in ("title", "total", "done", "failed", "status", "message",
                         "started_at", "kind", "meta")}
    db.task_upsert(path, {
        "task_id": tid, "kind": p.get("kind") or "ingest",
        "title": p.get("title") or "", "total": p.get("total") or 0,
        "done": p.get("done") or 0, "failed": p.get("failed") or 0,
        "status": p.get("status") or "running", "message": p.get("message") or "",
        "meta": p.get("meta") or {}, "data": data,
        "started_at": p.get("started_at"),
    })


def _prune_progress() -> None:
    """清掉过期已完成任务的内存记录（数据库快照仍在，按保留期清理）。"""
    cutoff = time.time() - 1800
    with _PROGRESS_LOCK:
        gone = [tid for tid, p in _PROGRESS.items()
                if p.get("status") in ("done", "error")
                and (p.get("finished_at") or 0) < cutoff]
        for tid in gone:
            _PROGRESS.pop(tid, None)
            _PROGRESS_DB.pop(tid, None)
            _PROGRESS_LAST.pop(tid, None)
    if gone:
        with _STOP_LOCK:
            for tid in gone:
                _STOP_FLAGS.pop(tid, None)


def register_progress(title: str, total: int, stoppable: bool = False,
                      db_path: str | None = None,
                      meta: dict | None = None) -> str:
    """登记一个后台任务，返回 task_id（自动分析的进度靠它查询）。

    `stoppable=True` 时同时登记中断旗（收取/导入用，见 `request_stop`）。
    `db_path` 给出时把快照写进 `background_tasks`，页面刷新/服务重启后排障用。
    """
    import uuid
    _prune_progress()
    tid = uuid.uuid4().hex[:12]
    with _PROGRESS_LOCK:
        _PROGRESS[tid] = {"title": title, "total": total, "done": 0,
                          "failed": 0, "status": "running", "message": "",
                          "started_at": time.time(), "kind": "ingest",
                          "meta": dict(meta or {})}
        if db_path:
            _PROGRESS_DB[tid] = db_path
    if stoppable:
        with _STOP_LOCK:
            _STOP_FLAGS[tid] = threading.Event()
    _persist_progress(tid, force=True)
    return tid


def update_progress(tid: str, *, done: int | None = None,
                    failed: int | None = None, status: str | None = None,
                    message: str | None = None, total: int | None = None,
                    extra: dict | None = None) -> None:
    with _PROGRESS_LOCK:
        p = _PROGRESS.get(tid)
        if not p:
            return
        if done is not None:
            p["done"] = done
        if failed is not None:
            p["failed"] = failed
        if status is not None:
            p["status"] = status
            if status in ("done", "error"):
                p["finished_at"] = time.time()
        if message is not None:
            p["message"] = message
        if total is not None:          # v1.29.4：收取任务"统计完数量再报总数"
            p["total"] = total
        if extra:
            p.update(extra)
    _persist_progress(tid, force=status in ("done", "error"))


def get_progress(tid: str) -> dict | None:
    with _PROGRESS_LOCK:
        p = _PROGRESS.get(tid)
        return dict(p) if p else None


def list_running() -> list[dict]:
    """列出**还在跑**的后台任务（收取 / 导入 / 分析）。

    给"页面刷新、或进度弹窗被误关后重新挂回去"用（v1.29.10，业务方：
    "弹窗关了就无法停止了"）：前端先问一句有没有在跑的，有就挂回进度窗口
    （可停止的带停止按钮），避免重复开任务、也避免傻等。
    排序：可停止的在前、新开的在前。
    """
    with _PROGRESS_LOCK:
        rows = [(tid, dict(p)) for tid, p in _PROGRESS.items()
                if p.get("status") == "running"]
    out = []
    for tid, p in rows:
        with _STOP_LOCK:                      # 不嵌套拿锁，避免与 request_stop 反向死锁
            can_stop = tid in _STOP_FLAGS
        out.append({"task_id": tid, "title": p.get("title") or "",
                    "total": p.get("total") or 0, "done": p.get("done") or 0,
                    "message": p.get("message") or "", "stoppable": can_stop,
                    "kind": p.get("kind") or "ingest",
                    "meta": p.get("meta") or {},
                    "started_at": p.get("started_at") or 0})
    out.sort(key=lambda x: (not x["stoppable"], -(x["started_at"] or 0)))
    return out


def _enrich_major(cand: dict, text: str) -> tuple[dict, str]:
    """专业归一（v1.11）：规则归不出来时，让模型在**学科目录**里选一个真实条目。

    为什么放在这里而不是 extract 里：extract 会按岗位跑多次（每个在招岗位一次），
    模型调用放那里就是 N 倍成本；这里只对**最终画像**做一次，且结果写进
    `major_canonical`（归一后的目录条目）与 `major_via`（来源），后续展示可复核。
    模型不可用/目录里真没有 → 返回原样，不打扰流程。
    """
    major = (cand.get("major") or "").strip()
    if not major:
        return cand, ""
    try:
        if majors_mod.resolve(major):
            cand["major_canonical"] = majors_mod.resolve(major)["canonical"]
            cand["major_via"] = "catalog"
            return cand, ""
    except Exception:                                       # noqa: BLE001
        pass
    r = major_llm_mod.classify_major(major, context=text[:600])
    if not r:
        return cand, ""
    cand["major_canonical"] = r["canonical"]
    cand["major_via"] = "模型归一"
    return cand, (f"专业「{major}」由模型归一到学科目录「{r['canonical']}」"
                  f"（{r.get('category') or '门类未识别'}），专业方向已按归一结果重算")



from .pipeline.tier import grade

CONFIDENTIAL_HINTS = ("涉密", "机密", "秘密", "军工", "军品", "武器装备", "保密资格", "国防")


def _lp_size(path: str) -> int:
    """长路径安全的 os.path.getsize（>260 字符在 Windows 上会直接抛 206）。"""
    from .paths import lp_size
    return lp_size(path)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def archive_file(cfg: dict, filename: str, data: bytes) -> tuple[str, int]:
    """把附件写入原件区（按 年-月 分目录，文件名带内容哈希前缀，只增不改）。

    v2.2.4：文件名按**整条路径预算**压短（`fit_name`）。
    为什么：附件原名动辄 150+ 字符（微信转来的附件尤其长），
    加上数据根与年-月目录就逼近 Windows 的 260 字符上限——
    一旦越线，写文件直接报 WinError 206，简历就"收进来了但没有原件"。
    压短后原名仍完整存在数据库 `file_name` 字段里，界面显示不受影响。
    """
    from .paths import fit_name, lp_exists, lp_makedirs, lp_open
    digest = sha256_bytes(data)
    folder = os.path.join(mb.resolve_dir(cfg.get("archive_dir", "data/archive")),
                          datetime.now().strftime("%Y-%m"))
    lp_makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{digest[:16]}_{mb.safe_filename(filename)}")
    path = os.path.join(folder, fit_name(folder, os.path.basename(path)))
    if not lp_exists(path):
        with lp_open(path, "wb") as fh:
            fh.write(data)
    return path, len(data)


def detect_confidential(text: str) -> str | None:
    """识别涉密相关表述，用于 `pii_level` 与访问隔离。"""
    for kw in CONFIDENTIAL_HINTS:
        if kw in (text or ""):
            m = re.search(rf"[^\n。；]{{0,40}}{re.escape(kw)}[^\n。；]{{0,40}}", text)
            return m.group(0).strip() if m else kw
    return None


def classify_pii(text: str) -> str:
    """普通 / 敏感（出现涉密相关表述）。"""
    return "敏感" if detect_confidential(text) else "普通"


def apply_tags(conn, cid: int, cand: dict, channel: str, text: str) -> list[str]:
    src_map = {"邮箱": "邮箱投递", "文件夹": "本地导入", "内推": "内推",
               "招聘会": "招聘会", "官网": "官网"}
    specs: list[tuple[str, str, str | None]] = [(src_map.get(channel, channel), "来源", None)]

    edu = cand.get("education")
    if edu:
        specs.append((edu, "类型", None))
    years = cand.get("years")
    if years in (0, None):
        specs.append(("应届生/经验少", "类型", None))
    if cand.get("years") and cand["years"] >= 8:
        specs.append(("资深", "类型", None))
    conf = detect_confidential(text)
    if conf:
        specs.append(("涉密相关", "类型", conf))

    names = []
    for name, category, evidence in specs:
        tid = db.upsert_tag(conn, name, category)
        db.link_tag(conn, cid, tid, evidence, "rule")
        names.append(name)
    return names


def persist_skills(conn, cid: int, cand: dict, doc_id: int | None) -> dict:
    """把归一后的技能写入本体关联表。含未核验项，但 `verified` 标记区分。

    v3.0.3 性能：**整个循环只提交一次**。
    原来 `upsert_skill` / `link_skill` 各自 `commit()`，一个人十几项技能
    = 十几次独立写事务。启动时 `refresh_skills` 要跑几千条投递，
    于是写锁被反复获取/释放几千次 —— 用户在这期间的任何写操作
    （打开档案写审计、改档）都会撞上 busy_timeout。
    这既是启动慢的主因，也是"刚打开就点不动"的主因。

    另外删掉了循环里的 `db.find_skill(conn, canon)`：
    `upsert_skill` 刚刚用**同一个 canon** 建/取过这条记录，
    再查一次必然是命中（多一次 SELECT，结果恒等于"在"）。
    """
    stat = {"verified": 0, "unverified": 0, "skills_in_ontology": 0}
    items = [it for it in (cand.get("skill_detail") or []) if it.get("canonical")]
    if not items:
        return stat
    for item in items:
        canon = item["canonical"]
        sid = db.upsert_skill(conn, canon, item.get("category", "其他"), commit=False)
        stat["skills_in_ontology"] += 1
        db.link_skill(conn, cid, sid, item.get("level"), item.get("evidence"),
                      doc_id, item.get("source", "rule"), commit=False)
        if item.get("verified"):
            stat["verified"] += 1
        else:
            stat["unverified"] += 1
    conn.commit()
    return stat


def _find_doc_by_hash(conn, digest: str) -> dict | None:
    """按文件哈希取既有原件台账（含原文与归档路径）。

    用于两处：① 重复投递时把已有附件编号带出去，界面能直接预览；
    ② **该人已归档后重新投递**时复用这份原件（`documents.file_hash` 是 UNIQUE，
    一份原件只允许一条台账；复用即"磁盘不重复落盘"，账仍然清楚）。
    """
    row = conn.execute("SELECT * FROM documents WHERE file_hash = ? "
                       "ORDER BY id LIMIT 1", (digest,)).fetchone()
    return dict(row) if row else None


def _find_recent_application(conn, cid: int, job_id: int | None, days: int) -> dict | None:
    """找该人近期的既有投递，用于「新版本归档」而非新建投递。

    匹配规则上容易踩的坑：

    - v0.1 迁移过来的投递 `job_id` 是 NULL（当时没有岗位表），严格按 `job_id = ?`
      匹配会漏掉，于是同一个人同一岗位第二次投递被再建一条——正是"零重复"要避免的；
      故明确岗位时用 `job_id = ? OR job_id IS NULL`，命中后由调用方回填真实 job_id。
    - **本次投递未指定岗位**（邮件主题没带岗位名、或文件夹导入）时，无从判断它
      更新的是哪个岗位；此时取该人**最近一条投递**（无论岗位）作为"新版本"宿主，
      避免同一个人因为"更新简历"换了个不带岗位名的主题就被拆成两条投递。
    """
    if not days:
        return None
    cutoff = datetime.now().timestamp() - days * 86400
    if job_id is not None:
        rows = conn.execute(
            "SELECT * FROM applications WHERE candidate_id = ? "
            "AND (job_id = ? OR job_id IS NULL) "
            "ORDER BY COALESCE(applied_at,'') DESC, id DESC", (cid, job_id)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM applications WHERE candidate_id = ? "
            "ORDER BY COALESCE(applied_at,'') DESC, id DESC LIMIT 1", (cid,)
        ).fetchall()
    for a in rows:
        applied = a["applied_at"] or ""
        try:
            ts = datetime.strptime(applied[:19], "%Y-%m-%d %H:%M:%S").timestamp()
        except ValueError:
            ts = 0
        if ts >= cutoff:
            return db.get_application(conn, a["id"])
    return None


def ensure_person(conn, cand: dict, channel: str, text: str,
                  doc_digest: str = "") -> tuple[int, bool, dict]:
    """身份归一并建/取人档。返回（candidate_id，是否新建，疑似重复提示）。

    注意：若简历里**没有任何可识别信息**（解析失败、姓名/电话/邮箱全无），
    身份键会退化到 `uk`，此时用文档哈希兜底，
    否则所有解析失败的简历都会挤进同一个人档——那是数据污染，不是"不丢件"。
    """
    phone = (cand.get("contact") or {}).get("phone")
    email = (cand.get("contact") or {}).get("email")
    key = build_identity_key(cand.get("name"), phone, email, cand.get("school"), None)
    if key.startswith("uk:") and doc_digest:
        key = f"uk:{doc_digest[:24]}"

    from .crypto import blind_index, encrypt
    from .identity import normalize_phone

    existing = db.find_candidate_by_identity(conn, key)
    weak_hit: dict | None = None
    if existing:
        # 只有手机号/邮箱这类**强身份键**才能自动并入已有档案。
        # `nm:`（姓名）/`ns:`（姓名+院校）是弱键：同名不同人极其常见
        # （扫描件 OCR 失败、简历没写联系方式时最容易命中），自动并档会把
        # 两个人的简历混进同一份档案，且后续版本还会覆盖原字段。
        # 弱键命中时**新建档案**并返回 suspect 提示，交 HR 人工判断。
        kind = key.split(":", 1)[0]
        if kind in ("ph", "em"):
            cid = existing["id"]
            db.update_candidate(conn, cid, last_active_at=db.now())
            return cid, False, {}
        weak_hit = existing

    # 键不同但手机/邮箱命中 -> 疑似同一人，沿用已有档（不新建），并提示
    # 注意：**脱敏号码（138****1234）不能作为"这是同一个人"的证据**——
    # 它只有前 3 后 4 是真实信息，两个不同的人完全可能撞上。
    # 因此只在号码完整（可归一为 11 位）时才用手机盲索引参与比对，
    # 否则宁可为该简历单独建档，交由 HR 用"疑似重复"提示人工判断。
    pidx = blind_index(phone) if normalize_phone(phone) else None
    eidx = blind_index(email)
    suspects = db.find_by_bidx(conn, pidx, eidx)
    if suspects:
        cid = suspects[0]["id"]
        db.update_candidate(conn, cid, last_active_at=db.now())
        db.add_audit(conn, "candidate", str(cid), "identity_weak_match",
                     existing.get("identity_key") if existing else "",
                     f"新简历身份键 {key} 未命中，但{'手机号' if pidx else '邮箱'}命中本档",
                     "system", "system")
        return cid, False, {"suspect": True, "reason": "手机号/邮箱命中已有档案，已归入已有档案"}

    # `candidates.identity_key` 是 UNIQUE：弱键命中时要另建档，
    # 但不能再写同一个键。加一个随机后缀把两个人分开；
    # 后续同名无联系方式的简历依旧会先查到最早那份（weak_hit）并继续提示。
    insert_key = key
    if weak_hit:
        insert_key = f"{key}#{uuid.uuid4().hex[:8]}"
    cid = db.insert_candidate(conn, {
        "identity_key": insert_key,
        "name": cand.get("name"),
        "phone_enc": encrypt(phone),
        "email_enc": encrypt(email),
        "phone_bidx": pidx,
        "email_bidx": eidx,
        "edu_level": cand.get("education"),
        # 毕业时间：从原文里带"毕业"上下文的年月取，供"应届/往届未就业"判定
        "grad_date": freshness.find_grad_date(text),
        # v1.29.4（业务方 2026-10-16）：在读判定 —— 硕士「2024.9-至今」这类还没毕业的
        # 学生，不能被本科毕业年份判成"往届未就业"；在读按应届处理（见 freshness.find_in_school）。
        "in_school": 1 if freshness.find_in_school(text) else 0,
        "school": cand.get("school"),
        "major": cand.get("major"),
        "years_exp": cand.get("years"),
        "current_org": cand.get("current_org"),
        # 性别：仅当简历明写标签行时才有值；**不参与任何评分/分级**，
        # 只用于界面标签与可开关的筛选（见 server 的 gender_filter_enabled）。
        "gender": cand.get("gender"),
        # v1.27：英语四六级原文表述（HR 拿它做硬筛选）。归一在读取侧做
        # （`pipeline.extract.cet_level`），这里只存"简历上写了什么"。
        "cet": cand.get("cet"),
        # v1.29：第一学历（本科）院校。与 school（最高学历院校）**分开存** ——
        # 拿 school 当第一学历会把"本科双非、硕士985"的人筛进「本科985」。
        # 抽不到就是空，界面显示「未识别（可补录）」、筛选落独立的「未识别」档。
        "first_degree_school": cand.get("first_degree_school"),
        # v1.29.2：完整学历院校经历（本科/硕士/博士各一段）。存 JSON 字符串 ——
        # 只随简历抽取一次、只读不编辑，建子表不划算（读时 loads 一次即可）。
        "edu_history": (json.dumps(_edu_hist or [], ensure_ascii=False)
                        if (_edu_hist := cand.get("education_history")) else None),
        "source_first": channel,
        "pii_level": classify_pii(text),
    })
    db.add_audit(conn, "candidate", str(cid), "create", "", f"新建档案：{cand.get('name')}",
                 "system", "system")
    if weak_hit:
        # 弱键同名：已单独建档，但必须让 HR 知道"库里有同名的另一份档案"。
        return cid, True, {
            "suspect": True,
            "reason": (f"按姓名命中了已有档案 #{weak_hit['id']}（{weak_hit.get('name') or ''}），"
                       "但简历没有手机/邮箱，无法确认是同一人；已单独建档，请人工核实"),
        }
    return cid, True, {}


def _regrade_if_unconfirmed(conn, app: dict, g: dict) -> bool:
    """同一投递收到新版本简历时刷新系统建议——但**不触碰 HR 已确认的结果**。"""
    if app.get("tier_final") or (app.get("status") or "") == "已确认":
        return False
    conn.execute(
        """UPDATE applications SET score = ?, tier_suggested = ?, needs_review = ?,
           reasons = ?, risks = ?, hits = ?, miss = ?, preferred_hit = ?, breakdown = ?,
           extract_mode = ?, confidence = ?, updated_at = ?
           WHERE id = ?""",
        (g["score"], g["tier_suggested"], 1 if g["needs_review"] else 0,
         json.dumps(g["reasons"], ensure_ascii=False),
         json.dumps(g["risks"], ensure_ascii=False),
         json.dumps(g["hit"], ensure_ascii=False),
         json.dumps(g["miss"], ensure_ascii=False),
         json.dumps(g["preferred_hit"], ensure_ascii=False),
         json.dumps(g["breakdown"], ensure_ascii=False),
         None, None, db.now(), app["id"]),
    )
    conn.commit()
    return True


def _fill_missing_gender(conn, cid: int, gender: str | None) -> bool:
    """已有档案缺性别时，用新版本简历里的标签补上（**只在原值为空时写**）。

    刻意不做反向覆盖：HR 看到的是简历上明写的标签，新版本没写性别（或写了别的）
    不该把已有值改掉——真需要改由 HR 在档案里手动改并留痕。
    """
    if not gender:
        return False
    row = conn.execute("SELECT gender FROM candidates WHERE id = ?", (cid,)).fetchone()
    if not row or (row["gender"] or "").strip():
        return False
    conn.execute("UPDATE candidates SET gender = ?, updated_at = ? WHERE id = ?",
                 (gender, db.now(), cid))
    conn.commit()
    return True


def _open_jobs_with_jd(conn) -> list[dict]:
    """当前可投递的岗位 + 已解析的 JD（实现在数据层，与存量重算共用一份口径）。"""
    return db.open_jobs_with_jd(conn)


def _route_by_open_jobs(conn, text: str, jd_default: dict,
                        filename: str | None = None,
                        prefer_text: str | None = None, use_llm: bool = False,
                        llm_conf: dict | None = None) -> dict | None:
    """给未归岗的简历找「建议岗位」：**关键词已在上游试过**（没命中才走到这里），这一步问模型。

    v1.12 改口径（原来轮询每个在招岗位打分、取最高分）：
    1. 加权打分已删（HR 反馈打分是噪音），**没有分数就选不出"最高分岗位"**——
       所有岗位并列后按 id 排序，等于随机归岗，比不推荐更糟；
    2. 规则能回答"技能对上几项"，回答不了"这个人该去哪个团队"——后者是模型擅长的；
    3. 成本可控：只在关键词匹不到时调**一次**（不是每个岗位各一次）。

    模型说不出来 → `usable=False`，保持"待指定"由 HR 手动归岗。
    """
    jobs = _open_jobs_with_jd(conn)
    if not jobs:
        return None
    empty = {"id": None, "title": "", "jd": None, "cand": None,
             "usable": False, "considered": len(jobs), "why": ""}
    # v1.29.5（业务方）：**投递标题/文件名里的应聘方向优先做"建议岗位"** ——
    # 本人写下的意向，只要能和在招岗位"对应上"（宽口径 match_job_soft），
    # 就优先于按正文内容让模型去猜。⚠️ 产物仍是**建议**（只进 suggested_job_id），
    # 不直接归岗；"完全对应"的情形在上游就已经直接归岗了，走不到这里。
    wish = (prefer_text or "").strip()
    if not wish and filename:
        try:
            wish = str((subject_meta_mod.parse(filename).get("fields") or {})
                       .get("direction") or "")
        except Exception:                                    # noqa: BLE001
            wish = ""
    if wish:
        _sjid, _sjd = db.match_job_soft(conn, wish)
        _sjob = next((j for j in jobs if j["id"] == _sjid), None) if _sjid else None
        if _sjob is not None:
            _scand = extract(text, _sjob["jd"], use_llm=use_llm, llm_conf=llm_conf,
                             filename=filename)
            return {**_sjob, "cand": _scand, "usable": True, "considered": len(jobs),
                    "why": f"按投递标题/文件名的应聘方向「{wish}」对应到本岗位"}
    if not use_llm:
        # 模型未启用时不猜岗位：宁可待指定，也不要随机塞一个
        return {**empty, "why": "模型未启用，未做归岗判断"}
    from .pipeline import analyze as analyze_mod
    # 先用默认尺子抽一份画像（**故意不调模型**，便宜）——只为让模型读懂这份简历。
    # 注意：它的结果**不落库**，只喂给 suggest_job()。
    cand0 = extract(text, jd_default, use_llm=False, filename=filename)
    pick = analyze_mod.suggest_job(cand0, jobs)
    if not pick:
        # 判不出岗位 → 返回 cand=None，让调用方**保留自己那次带模型的抽取结果**。
        # （v1.23.3 修：原来这里返回 cand0，把上游用模型抽好的字段覆盖成了规则版本，
        #   结果"模型明明跑了，落库的抽取方式却是 heuristic"。）
        return {**empty, "cand": None, "why": "模型没能判断出对应岗位"}
    job = next((j for j in jobs if str(j.get("title") or "").strip() == pick["title"]), None)
    if not job:
        return {**empty, "cand": None, "why": "模型给出的岗位不在在招清单里（已丢弃）"}
    # 用该岗位的 JD 尺子重抽一次：技能词表来自岗位 JD，命中口径才与岗位一致。
    # **必须把 llm_conf 传下去**，否则 use_llm=True 也走规则通道（静默降级）。
    cand = extract(text, job["jd"], use_llm=use_llm, llm_conf=llm_conf, filename=filename)
    return {**job, "cand": cand, "usable": True, "considered": len(jobs),
            "why": pick.get("reason") or ""}


def ingest_one(conn, cfg: dict, jd: dict, tiers: dict, *, filename: str, data: bytes | None,
               local_path: str | None, channel: str, applied_at: str,
               source_message_id: str | None, job_id: int | None,
               use_llm: bool = False, llm_conf: dict | None = None,
               subject_meta: dict | None = None,
               preparsed: tuple[str, str, bool] | None = None) -> dict:
    """单份简历的完整处理链路，返回一条结果记录。

    `subject_meta`：邮件主题/文件名按 `方向+学历+学校+专业+姓名+性别` 解析出的字段
    （见 `pipeline/subject_meta.py`）。它主要用于**岗位方向**；个人字段只在简历
    完全缺失时补空，**绝不覆盖**简历正文/模型抽取出的姓名、学历、学校、专业、性别。
    """
    result: dict = {"file": filename, "status": "unknown", "name": None, "tier": None,
                    "score": None, "notes": []}

    digest = sha256_bytes(data) if data is not None else _hash_path(local_path)
    result["hash"] = digest

    # —— 第二层去重：文件层 ——
    # 关键顺序：**先判重、后落盘**。若先落盘再判重，每一份重复投递都会在原件区
    # 留下一个台账（documents）里没有记录的同哈希副本——磁盘上出现"无账野文件"，
    # 审计时无法解释来源。内容逐字节相同的原件已有一份，重复投递的事实由
    # email_messages 台账与审计记录承担（谁、何时、又投了一次），信息并未丢失。
    #
    # 例外（v1.8.8，HR 口径）：**该人已归档后再投同一份简历**时，不能一律幂等跳过——
    # "重新投递"本身就是要记录的事实。此时复用既有原件（磁盘仍只有一份、不重复落盘），
    # 按最新简历新建一条投递，并把档案移回人才库。
    prior_doc: dict | None = None
    if db.exists_hash(conn, digest):
        prior_doc = _find_doc_by_hash(conn, digest)
        prev_cand = (db.get_candidate(conn, prior_doc["candidate_id"])
                     if prior_doc and prior_doc.get("candidate_id") else None)
        if not (prev_cand and prev_cand.get("archived_at")):
            result["status"] = "skipped_dup"
            result["notes"].append("同一份简历已入库（SHA256 相同），本次不重复导入")
            if prior_doc:
                # 重复的这份原件虽然不落盘，但库里已有一份：把它的附件编号带出去，
                # 界面才能在"逐份明细"里直接预览/下载（否则 HR 只能看到文件名）
                result["document_id"] = prior_doc["id"]
                # 同一封邮件里的其他附件要挂到这个已有档案下；否则重复收取时
                # 补充材料会因为拿不到 candidate_id 而变成"未挂人"。
                result["candidate_id"] = prior_doc.get("candidate_id")
                # 即使原件完全相同、没有新建文档，这次投递本身也是"最近一次收到简历"。
                # 列表按 `last_active_at` 排序，这里必须刷新；否则 HR 刚收到的重复简历
                # 仍沉在旧位置，看起来像没收进来。
                if prior_doc.get("candidate_id"):
                    db.update_candidate(conn, prior_doc["candidate_id"],
                                        last_active_at=db.now())
            db.add_audit(conn, "document", digest[:16], "duplicate_skipped", "",
                         f"重复投递的附件 {filename} 未重复落盘（原件已在库）",
                         "system", "system")
            return result
        result["notes"].append("该人此前已归档，本次为重新投递：复用既有原件（不重复落盘），新建投递")

    if prior_doc is not None:
        # 归档后重新投递：原件与原文都已在本库，直接复用——
        # **不重复落盘、不重复解析**（同一份文件再解析一次结果必然相同），
        # 同时保证新投递的档位/岗位建议仍按当前口径重算。
        archived = prior_doc.get("archived_path") or ""
        size = prior_doc.get("size") or 0
        text = prior_doc.get("raw_text") or ""
        engine = prior_doc.get("parse_engine") or "复用既有原件"
        ok = bool(prior_doc.get("parse_ok", 1))
        reuse_doc = True
    else:
        reuse_doc = False
        # —— 归档（**任何来源都要落一份到原件区**）——
        #
        # 早先的写法是「本地文件本身就是原件区」，直接拿扫描目录里的路径当 archived_path。
        # 那样一来，HR 只要清理或挪动一次来源目录，库里的「原件」就集体 404——
        # 而模块开头写的正是「原件区只增不改」。所以文件夹导入同样复制一份归档，
        # `file_path` 仍记原始出处（可追溯从哪来），`archived_path` 指向归档副本（可长期取用）。
        if data is not None:
            archived, size = archive_file(cfg, filename, data)
        elif local_path:
            with open(local_path, "rb") as fh:
                payload = fh.read()
            archived, size = archive_file(cfg, filename, payload)
        else:
            archived, size = "", 0

        # —— 解析（失败不阻断）——
        # 邮件路径会先在 `_prepare_attachments()` 里解析一次并传进来，
        # 避免同一份图片被 OCR 两遍；文件夹导入等旧路径仍在这里解析。
        if preparsed is not None:
            text, engine, ok = preparsed
            text = str(text or "")
            ok = bool(ok and text.strip())
        else:
            text, engine, ok = parse_mod.parse_file_ex(archived)
    if not ok:
        result["notes"].append(f"解析未成功（{engine}），已标『简历读不出』，原件保留")

    # 一次带模型的抽取作为**默认结果**（v1.23.3：原来这里抽一次、else 分支又抽一次，
    # 白花一次模型调用；且路由失败时会被规则结果覆盖）。
    cand = extract(text, jd, use_llm=use_llm, llm_conf=llm_conf, filename=filename)

    # —— 归岗口径（v1.12）：**没有岗位名就问模型，问不出来就待指定** ——
    # 旧做法（v1.5–v1.11）：把每个在招岗位的 JD 各试算一遍、取分数最高者作「建议岗位」。
    # 为什么改掉：加权打分在 v1.12 被删（HR 反馈那是噪音），**没有分数就选不出"最高分"**，
    # 所有岗位并列后按 id 排序 → 等于随机归岗。现在改成：
    #   ① 文件名/邮件标题里有岗位名 → 上游已直接归岗（走不到这里）；
    #   ② 没有 → 请模型判断最像哪个在招岗位（一次调用）；
    #   ③ 模型也说不出 → 保持待指定、不出建议，由 HR 手动归岗。
    # 岗位本身仍然不落 `job_id`——系统只建议，HR 点「采纳」才真正归岗。
    route = (_route_by_open_jobs(conn, text, jd, filename=filename,
                                 prefer_text=((subject_meta or {}).get("fields") or {})
                                 .get("direction"),
                                 use_llm=use_llm, llm_conf=llm_conf)
             if job_id is None else None)
    if route and route.get("cand") is not None:
        # 画像用"胜出岗位"那一轮抽取的结果：技能清单里会包含该岗位 JD 写的词
        # （例如软件岗的 Java/Spring Boot），HR 点开档案看到的技能才与岗位对得上。
        cand = route["cand"]
        g = grade(cand, route["jd"] or jd)
    else:
        # 判不出岗位 / 无在招岗位 → **保留上面那次带模型的抽取结果**，不重抽、不覆盖。
    # 学历门槛判"最差档"的口径见 pipeline/tier.py（v1.24.15 起不再要求已归岗；
    # v1.29 起档位字母来自 config/tiers.json，当前硬门槛档是 C）。
        g = grade(cand, jd, job_confirmed=(job_id is not None))

    # —— 邮件主题 / 文件名里的结构化字段（v1.9）——
    # 业务方 2026-11-13：**个人信息以简历为准**。标题/文件名只补简历里缺失的字段，
    # 已有值一律不覆盖；岗位方向仍按标题归岗（见 `_route_by_open_jobs`）。
    # 技能与年限一律以正文+证据为准，标题不碰。
    if subject_meta:
        cand, _used = subject_meta_mod.apply_to_candidate(cand, subject_meta, raw_text=text)
        if _used:
            result["notes"].append("邮件标题/文件名补充了简历中缺失的 "
                                   + "、".join(_used))
        for _c in (cand.get("title_override_notes") or []):
            result["notes"].append(_c)

    # —— 专业归一（v1.11）：规则归不出来时，让模型在**学科目录**里选 ——
    # 模型只做"归一"（把"材化/材料成型"这类写法归到目录条目），打分仍走规则；
    # 归一后专业方向判定可能变化，所以**用归一结果重算一次**（纯规则，零成本）。
    # 模型不可用/归不出来时返回空，一切照旧。
    if use_llm:
        cand, major_note = _enrich_major(cand, text)
        if major_note:
            result["notes"].append(major_note)
            if route:
                g = grade(cand, route["jd"])
            else:
                g = grade(cand, jd, job_confirmed=(job_id is not None))
            result["tier"] = g["tier_suggested"]
            result["score"] = g["score"]

    if not ok:
        # **解析失败**与**信息缺失**是两条线（v1.24.9 拆分）：
        # 这里是"原文根本读不出来"，走 parse_failed；needs_review 留给
        # "读出来了但姓名/年限/技能没抽到"。过去两者共用一个字段，
        # 界面上只能统称"待人工判读"，HR 不知道该补字段还是修文件。
        g["parse_failed"] = True
        g["risks"] = list(g.get("risks") or []) + ["原文未能解析，需人工查看附件原件"]
    result["name"] = cand.get("name")
    result["tier"] = g["tier_suggested"]
    result["score"] = g["score"]
    # v1.24.9：详情行也带上parse_failed，上层统计用**字段**判断，
    # 不再靠匹配 notes 里的文案（文案一改统计就静默归零，这个坑踩过）。
    result["parse_failed"] = bool(g.get("parse_failed"))
    if route:
        result["suggested_job"] = {"job_id": route["id"] if route["usable"] else None,
                                   "title": route["title"],
                                   "reason": route.get("why") or "",
                                   "tier_suggested": route.get("tier_suggested"),
                                   "considered": route["considered"]}
        result["notes"].append(
            f"未归岗：文件名/邮件标题里没有岗位名（关键词匹不到），"
            + (f"已请模型判断，最像「{route['title']}」"
               + (f"（依据：{route['why']}）" if route.get("why") else "")
               + "，待 HR 采纳"
               if route["usable"] else
               f"模型也未判断出对应岗位（在招 {route['considered']} 个），"
               "保持待指定、不出建议——硬凑一个岗位比不推荐更糟"))

    # —— 第三层去重：内容层（人）——
    cid, is_new, hint = ensure_person(conn, cand, channel, text, doc_digest=digest)
    if hint.get("suspect"):
        result["notes"].append(hint["reason"])

    # 该档案此前是否已归档（v1.8.8）：归档后再次投递要**新建一条投递**并移回人才库——
    # 归档只表示"上一轮流程结束"，不代表这个人不能再投；重新投递本身是事实。
    # 未归档的人则始终沿用原投递（2026-11-13 口径）。
    _row = db.get_candidate(conn, cid) or {}
    was_archived = bool(_row.get("archived_at"))

    # 专业归一结果落到候选人行（v1.11）：重算、重开应用、卡片展示都要用，
    # 不能只留在本次内存里的画像上。
    if cand.get("major_canonical"):
        db.update_candidate(conn, cid,
                            major_canonical=cand["major_canonical"],
                            major_via=cand.get("major_via") or "规则")
    # v1.17：奖学金/荣誉/论文/专利（规则抽取 + 原文片段）。只在有内容时写库，
    # 免得每次导入都把空数组刷进 honors_json。
    # v1.18：优先用**模型那次分析**里已经抽好的荣誉（，
    # 每条都过了"证据必须逐字在原文里"的校验）；拿不到才退回规则抽取。
    try:
        _hon = cand.get("honors") or extract_honors(text)
        if not isinstance(_hon, list):
            _hon = extract_honors(text)
    except Exception as exc:                                # noqa: BLE001
        _hon = []
        print(f"[ingest] 荣誉抽取失败（不影响入库）：{type(exc).__name__}: {exc}",
              file=sys.stderr)
    if _hon:
        import json as _json
        db.update_candidate(conn, cid,
                            honors_json=_json.dumps(_hon, ensure_ascii=False))
        result["notes"].append(f"识别到荣誉/论文/专利 {len(_hon)} 项")
    # v5.1.1：资格类证书（中级职称/PMP/注册安全工程师…）。抽取器早已有这份数据，
    # 但过去没有落库，导致档案和助手都看不到。有内容才写，避免把空数组刷进库。
    _certs = [str(x).strip() for x in (cand.get("certificates") or []) if str(x).strip()]
    if _certs:
        import json as _json
        db.update_candidate(conn, cid,
                            certificates_json=_json.dumps(_certs, ensure_ascii=False))
        result["notes"].append("识别到证书：" + "、".join(_certs[:8]))

    # v1.27.1：四六级由模型判定，但它给的档位/证据没通过原文自证时已退回规则值 ——
    # 这种情况必须留痕，否则 HR 看到的其实是规则值却以为是模型判的（不留静默降级）。
    if "cet" in (cand.get("unverified_fields") or []):
        result["notes"].append("英语四六级的模型判定未能在简历原文中核实，已改按原文抽取")

    if is_new:
        apply_tags(conn, cid, cand, channel, text)
    skill_stat = persist_skills(conn, cid, cand, None)
    if skill_stat["unverified"]:
        result["notes"].append(f"{skill_stat['unverified']} 项技能未能在原文定位证据，未计入命中")

    # —— 投递：同一个人（未归档）始终沿用最近一条投递，不再新建投递 ——
    # 业务方 2026-11-13：只要身份命中同一个人，不论换了岗位、隔了多久，
    # 后续简历都作为该投递的新版本附件；个人信息始终按最新简历刷新。
    # 例外：**已归档的人再次投递**要新建一条投递档案（业务方 2026-11-13 明确），
    # 旧投递与附件保留，不能直接把旧档案当成这次的新投递。
    existing = None if was_archived else db.latest_application(conn, cid)

    if existing:
        # 既有投递没有岗位（v0.1 迁移数据）时回填，避免同一个人长期挂着"未指定岗位"的投递
        if existing.get("job_id") is None and job_id is not None:
            conn.execute("UPDATE applications SET job_id = ?, updated_at = ? WHERE id = ?",
                         (job_id, db.now(), existing["id"]))
            conn.commit()
            db.add_audit(conn, "application", str(existing["id"]), "job_bound",
                         "岗位未指定", f"按本次投递回填岗位 #{job_id}", "system", "system")
            existing = db.get_application(conn, existing["id"])
        elif job_id is not None and existing.get("job_id") not in (None, job_id):
            # 同一人只有一条投递：新简历若带的是另一个岗位，不自动改岗（避免
            # 悄悄把已有面试/阶段记录挪到别的岗位），只提示 HR 按需在档案里调整。
            result["notes"].append("本次简历带岗位信息，但同一人沿用原投递岗位；如需改岗请在档案里调整")

        # 仍「待指定」的投递：刷新建议岗位（新人新简历可能更匹配别的岗位，
        # 岗位表也可能新增/停用了岗位——建议是"当前在招岗位里的最适者"，本就不是定值）
        if route and existing.get("job_id") is None:
            # 建议岗位与**判断理由**一起落库：列表/卡片直接读库展示，不必每次现算
            # （现算一次 6-8 秒且烧 token，见 server 列表接口的说明）
            conn.execute("UPDATE applications SET suggested_job_id = ?, "
                         "suggested_job_reason = ?, updated_at = ? WHERE id = ?",
                         (route["id"] if route["usable"] else None,
                          (route.get("why") or None) if route["usable"] else None,
                          db.now(), existing["id"]))
            conn.commit()
            existing = db.get_application(conn, existing["id"])

        doc_id = db.insert_document(conn, {
            "file_hash": digest, "candidate_id": cid, "application_id": existing["id"],
            "file_name": filename, "file_path": local_path or archived, "archived_path": archived,
            "mime": parse_mod.mime_of(filename), "size": size, "received_at": applied_at,
            "source_message_id": source_message_id, "raw_text": text,
            "parse_engine": engine, "parse_ok": ok,
            # 送进模型前剔除了什么（隐私红线要可核查），落库供档案页展示
            "sensitive_found": json.dumps(cand.get("sensitive_found") or {},
                                          ensure_ascii=False),
        })
        # 让"当前简历"指针始终指向这一份最新版本；否则按岗位重算/导出仍会读旧版。
        conn.execute("UPDATE applications SET resume_doc_id = ?, updated_at = ? WHERE id = ?",
                     (doc_id, db.now(), existing["id"]))
        conn.commit()
        updated_fields = _apply_resume_fields(conn, cid, cand, text)
        result["candidate_id"] = cid
        result["application_id"] = existing["id"]
        result["document_id"] = doc_id
        if updated_fields:
            result["notes"].append("已按最新简历刷新档案：" + "、".join(
                db.AUDIT_FIELD_LABEL.get(k, k) for k in updated_fields))
        refreshed = _regrade_if_unconfirmed(conn, existing, g)
        _fill_missing_gender(conn, cid, cand.get("gender"))
        if refreshed:
            result["status"] = "merged_version"
            result["notes"].append("同一人已有投递，本次作为新版本附件并刷新系统建议")
            db.add_audit(conn, "application", str(existing["id"]), "new_version",
                         f"档 {existing.get('tier_suggested')}",
                         f"新简历版本，系统建议更新为 {g['tier_suggested']}", "system", "system")
        else:
            result["status"] = "merged_version"
            result["notes"].append("同一人已有投递，新版本已归档；HR 已确认的档位未被覆盖")
            db.add_audit(conn, "application", str(existing["id"]), "new_version_archived",
                         existing.get("tier_final") or "",
                         "新简历版本已归档，保留 HR 已确认档位", "system", "system")
        return result

    app_id = db.insert_application(conn, {
        "candidate_id": cid, "job_id": job_id,
        "suggested_job_id": route["id"] if (route and route["usable"]) else None,
        "channel": channel,
        "applied_at": applied_at, "score": g["score"], "tier_suggested": g["tier_suggested"],
        "needs_review": g["needs_review"], "reasons": g["reasons"], "risks": g["risks"],
        "hit": g["hit"], "miss": g["miss"], "preferred_hit": g["preferred_hit"],
        "breakdown": g["breakdown"], "extract_mode": cand.get("extract_mode"),
        "confidence": cand.get("confidence"),
    })
    if reuse_doc and prior_doc is not None:
        # 复用既有原件：`documents.file_hash` 是 UNIQUE，同一份文件只允许一条台账，
        # 因此不新建 document（磁盘上也确实只有一份文件），把已有附件挂到新投递上。
        doc_id = prior_doc["id"]
        conn.execute("UPDATE documents SET application_id = ? WHERE id = ?", (doc_id, app_id))
    else:
        doc_id = db.insert_document(conn, {
            "file_hash": digest, "candidate_id": cid, "application_id": app_id,
            "file_name": filename, "file_path": local_path or archived, "archived_path": archived,
            "mime": parse_mod.mime_of(filename), "size": size, "received_at": applied_at,
            "source_message_id": source_message_id, "raw_text": text,
            "parse_engine": engine, "parse_ok": ok,
            # 送进模型前剔除了什么（隐私红线要可核查），落库供档案页展示
            "sensitive_found": json.dumps(cand.get("sensitive_found") or {},
                                          ensure_ascii=False),
        })
    conn.execute("UPDATE applications SET resume_doc_id = ? WHERE id = ?", (doc_id, app_id))
    if not is_new:
        # 历史档案没有投递、或已归档后重新投递时，按最新简历刷新一次主档。
        _updated = _apply_resume_fields(conn, cid, cand, text)
        if _updated:
            result["notes"].append("已按最新简历刷新档案：" + "、".join(
                db.AUDIT_FIELD_LABEL.get(k, k) for k in _updated))
    if route and route["usable"]:
        conn.execute("UPDATE applications SET suggested_job_id = ?, "
                     "suggested_job_reason = ? WHERE id = ?",
                     (route["id"], route.get("why") or None, app_id))
    conn.commit()
    db.add_audit(conn, "application", str(app_id), "ingest", "",
                 f"入库，系统建议档位 {g['tier_suggested'] or '待分析'}"
                 + (f"，建议岗位「{route['title']}」#{route['id']}" if route
                    and route.get("usable") else ""),
                 "system", "system")

    # 归档后重新投递：档案回到人才库（原投递、附件、审计全部保留，不删除任何历史）
    if was_archived:
        db.set_candidate_archived(conn, cid, False, "system", "system")
        db.add_audit(conn, "candidate", str(cid), "unarchive_on_reapply", "已归档",
                     f"归档后再次投递（投递 #{app_id}），档案移回人才库", "system", "system")
        result["notes"].append("该人此前已归档：本次新建一条投递档案，并把档案移回人才库"
                               "（原投递与历史附件全部保留）")
        result["unarchived"] = True

    result["status"] = "added"
    result["candidate_id"] = cid
    result["application_id"] = app_id
    # 明细里带上附件编号，界面这一行才能直接「预览 / 下载」原件
    result["document_id"] = doc_id
    return result


def _hash_path(path: str | None) -> str:
    if not path or not os.path.exists(path):
        return sha256_bytes(b"")
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


# ============================================================
# 对外入口
# ============================================================


def _hr_edited_fields(conn, cid: int) -> set[str]:
    """HR 手动改过哪些字段（`audit_log.action='edit_fields'`）。

    重分析要保住这些值：HR 之所以手动改，就是因为抽取抽错了
    （姓名在图片里、学校写简称……）。让重分析把改对的冲掉，
    等于每重跑一次就毁一次人工成果。
    """
    try:
        rows = conn.execute(
            "SELECT before, after FROM audit_log WHERE entity='candidate' AND entity_id=? "
            "AND action='edit_fields'", (str(cid),)).fetchall()
    except Exception:                                         # noqa: BLE001
        return set()
    keys: set[str] = set()
    for r in rows:
        for part in f"{r[0] or ''}；{r[1] or ''}".split("；"):
            if "=" in part:
                keys.add(part.split("=", 1)[0].strip())
    # 审计里用的是中文标签，映射回字段名
    label2field = {"姓名": "name", "学历": "edu_level", "学校": "school",
                   "专业": "major", "性别": "gender",
                   "电话": "phone_enc", "邮箱": "email_enc"}
    return {label2field.get(k, k) for k in keys if k}


def _apply_resume_fields(conn, cid: int, cand: dict, text: str) -> list[str]:
    """把**最新一份简历**的抽取结果写回候选人主档。

    业务口径（2026-11-13）：同一个人后续收到任何简历，都沿用同一条档案/投递，
    并且始终以最新简历为准刷新个人信息。这里只写有值的字段，避免一份缺字段的
    新简历把已有信息清空；HR 手工改过的字段仍然保留（`_hr_edited_fields`）。

    返回实际更新的字段名，供明细里如实展示。
    """
    hr_touched = _hr_edited_fields(conn, cid)
    edu_hist = cand.get("education_history") or []
    _rank = {"大专": 1, "本科": 2, "硕士": 3, "博士": 4}
    _valid = [x for x in edu_hist if isinstance(x, dict)
              and x.get("degree") in _rank and x.get("school")]
    _top = max(_valid, key=lambda x: _rank[x["degree"]]) if _valid else None
    _bachelor = next((x for x in _valid if x["degree"] == "本科"), None)
    want = {
        "name": cand.get("name"),
        # 模型没给最高学历时，用已通过证据校验的教育经历兜底；不凭空猜。
        "edu_level": cand.get("education") or (_top.get("degree") if _top else None),
        "school": cand.get("school") or (_top.get("school") if _top else None),
        "major": cand.get("major") or (_top.get("major") if _top else None),
        "years_exp": cand.get("years"),
        "gender": cand.get("gender"),
        "grad_date": freshness.find_grad_date(text),
        "in_school": 1 if freshness.find_in_school(text) else 0,
        "current_org": cand.get("current_org"),
        "cet": cand.get("cet"),
        "first_degree_school": (cand.get("first_degree_school")
                                or (_bachelor.get("school") if _bachelor else None)),
        "edu_history": (json.dumps(edu_hist, ensure_ascii=False) if edu_hist else None),
    }
    # 联系方式也要随最新简历刷新（v6.0.0 审查 B10）。原白名单漏了
    # phone_enc/email_enc/phone_bidx/email_bidx：同一人换了手机或邮箱再投，
    # 档案里仍是旧号码，新号码也不进盲索引，检索/去重都用不上。
    # 只在**值确实变化**时写（盲索引比对；AES-GCM 每次密文都不同，不能比密文），
    # HR 手工改过的联系方式继续保留。
    from .crypto import blind_index, encrypt
    from .identity import normalize_email, normalize_phone
    _contact = cand.get("contact") or {}
    _cur = db.get_candidate(conn, cid) or {}
    _ph = normalize_phone(_contact.get("phone"))
    _em = normalize_email(_contact.get("email"))
    if _ph and not ({"phone", "phone_enc"} & hr_touched):
        _pidx = blind_index(_ph)
        if _pidx and _pidx != (_cur.get("phone_bidx") or ""):
            want["phone_enc"] = encrypt(_ph)
            want["phone_bidx"] = _pidx
    if _em and not ({"email", "email_enc"} & hr_touched):
        _eidx = blind_index(_em)
        if _eidx and _eidx != (_cur.get("email_bidx") or ""):
            want["email_enc"] = encrypt(_em)
            want["email_bidx"] = _eidx
    upd: dict = {}
    for k, v in want.items():
        if v in (None, "", []):
            continue
        if k in hr_touched:
            continue
        upd[k] = v
    if upd:
        db.update_candidate(conn, cid, **upd)
        labels = "、".join(db.AUDIT_FIELD_LABEL.get(k, k) for k in sorted(upd))
        db.add_audit(conn, "candidate", str(cid), "resume_refresh", "",
                     f"最新简历自动刷新档案：{labels}", "system", "system")
    return sorted(upd)


def reanalyze_full(conn, cfg: dict, jd_default: dict, tiers: dict, candidate_id: int, *,
                   use_llm: bool = True, llm_conf: dict | None = None,
                   wlock: threading.RLock | None = None) -> dict:
    """把一份已有档案按当前配置**完整重跑**一遍（不产生新投递）。

    返回 {ok, steps[], changes{}, kept_hr[], error?}，供界面如实展示每一步做了什么。

    `wlock`（v1.24.8）：批量重跑并发时传入共享连接的写锁。这里各处写入分散，
    逐段加锁改动大且易漏；改为用一把 RLock 把每个**写段**包住，读与模型调用
    仍不持锁。不传就是单线程用法，行为不变。
    """
    lk = wlock or threading.RLock()
    from .pipeline import parse as parse_mod
    from .pipeline.tier import grade

    steps: list[str] = []
    # ----读也必须持锁（v1.24.8 补）----
    # 踩过的坑：最初只给"写段"加锁，读段留在锁外，结果并发照样炸
    # `sqlite3.InterfaceError: bad parameter or other API misuse`，
    # 堆栈落在 db.candidate_detail 里的 conn.execute(...).fetchall()。
    #
    # 为什么读也要锁：**共享的是同一条连接**，而 Connection 上的游标是
    # 连接级共享状态——多个线程各自 execute 后交替 fetch，sqlite3 判定为
    # API 误用。**这跟事务隔离无关，是连接对象的线程安全问题。**
    # 注意：这不损失并发——读段是毫秒级查询，耗时的模型调用在锁外。
    with lk:
        d = db.candidate_detail(conn, candidate_id)
        if not d:
            return {"ok": False, "error": "未找到该候选人"}
        # 只从简历附件里选，不能把成绩单/证书/作品集当成简历重解析。
        # `resume_documents` 已按 id 倒序（最新在前）；老调用方没有这个键时再退回全量。
        docs = d.get("resume_documents") or d.get("documents") or []
        apps = d.get("applications") or []
        app_row = apps[0] if apps else {}
        app_id = app_row.get("id") or 0

    # ---- ① 找到磁盘上的原件并重新解析 ----
    doc = None
    src_path = ""
    for x in docs:
        for key in ("archived_path", "file_path"):
            p = x.get(key) or ""
            if p and os.path.exists(p):
                doc, src_path = x, p
                break
        if doc:
            break
    if not doc:
        return {"ok": False,
                "error": "原件不在磁盘上，无法完整重新分析；请重新导入这份简历"}

    text, engine, ok = parse_mod.parse_file_ex(src_path)
    if not ok or not (text or "").strip():
        return {"ok": False,
                "error": f"原件重新解析失败（{engine}），未改动任何数据"}
    steps.append(f"重新解析原件（{engine}，{len(text)} 字）")

    # ---- ② 归岗口径：已归岗沿用该岗位（不擅自换岗），未归岗才问模型 ----
    with lk:                                    # 读段持锁（同上：共享连接必须）
        jd_used, job_meta = db.resolve_candidate_job(conn, d)
    route = None
    if not jd_used:
        route = _route_by_open_jobs(conn, text, jd_default, filename=doc.get("file_name"),
                                    use_llm=use_llm, llm_conf=llm_conf)
        if route and route.get("cand") is not None:
            jd_used = route.get("jd") or jd_default
            steps.append(f"模型建议岗位：{route.get('title') or '—'}"
                         + ("（已采纳为建议）" if route.get("usable") else ""))
        else:
            jd_used = jd_default
            if route:
                steps.append("未归岗：模型也没判断出对应岗位，保持待指定")
    else:
        steps.append(f"沿用已归岗岗位：{job_meta.get('job_title') or job_meta.get('source')}")

    # ---- ③ 重新抽取字段（模型）----
    cand = extract(text, jd_used, use_llm=use_llm, llm_conf=llm_conf,
                   filename=doc.get("file_name"))
    steps.append(f"重新抽取字段（{cand.get('extract_mode')}）")
    # 毕业时间与"在读"由**原文**重算（extract 不产出这两个；不补的话下面 want
    # 取到 None 会把已算好的值冲掉）。与入库时（ensure_person）同一口径。
    cand["grad_date"] = freshness.find_grad_date(text)
    cand["in_school"] = 1 if freshness.find_in_school(text) else 0

    # ---- ④ 写回字段：保住 HR 人工更正过的 ----
    kept_hr: list[str] = []
    hr_touched = _hr_edited_fields(conn, candidate_id)
    want = {"name": cand.get("name"), "edu_level": cand.get("education"),
            "school": cand.get("school"), "major": cand.get("major"),
            "years_exp": cand.get("years"), "gender": cand.get("gender"),
            "grad_date": cand.get("grad_date"), "current_org": cand.get("current_org"),
            # v1.29.4：在读状态一起重算（换了份没写"至今"的简历不能还挂着在读）
            "in_school": cand.get("in_school"),
            # v1.27：四六级一起重算。漏掉的后果很隐蔽 —— 首次入库抽到了、
            # 换了一份没写四六级的简历后重新分析就会静默清空，HR 筛六级时人不见了。
            "cet": cand.get("cet"),
            # v1.29：第一学历同理一起重算（同一个坑）
            "first_degree_school": cand.get("first_degree_school"),
            # v1.29.2：完整学历院校经历同样重算（换了一份没写教育经历的简历就会空）
            "edu_history": (json.dumps(_eh or [], ensure_ascii=False)
                            if (_eh := cand.get("education_history")) else None)}
    changes: dict[str, list] = {}
    upd: dict = {}
    for k, v in want.items():
        cur = d.get(k)
        if v in (None, "", []) or v == cur:
            continue
        if k in hr_touched:
            kept_hr.append(k)
            continue
        upd[k] = v
        changes[k] = [cur, v]
    if upd:
        with lk:                                # 写段持锁（v1.24.8）
            db.update_candidate(conn, candidate_id, **upd)
        steps.append("更新字段：" + "、".join(db.AUDIT_FIELD_LABEL.get(k, k) for k in upd))
    if kept_hr:
        steps.append("保留人工更正：" + "、".join(db.AUDIT_FIELD_LABEL.get(k, k) for k in kept_hr))
    _certs = [str(x).strip() for x in (cand.get("certificates") or []) if str(x).strip()]
    if _certs:
        with lk:
            db.update_candidate(conn, candidate_id,
                                certificates_json=json.dumps(_certs, ensure_ascii=False))
        steps.append("更新证书：" + "、".join(_certs[:6]))

    # ---- ⑤ 重挂技能（新抽取到技能才重建，避免模型挂了把旧技能清空）----
    sk = cand.get("skill_detail") or []
    if sk:
        with lk:                                # 写段持锁（v1.24.8）
            conn.execute("DELETE FROM candidate_skills WHERE candidate_id=?", (candidate_id,))
            persist_skills(conn, candidate_id, cand, doc.get("id"))
            conn.commit()
        steps.append(f"重建技能：{len(sk)} 项（含未核验）")

    # ---- ⑥ 重判档（只写建议档，绝不动 HR 定档与阶段）----
    g = grade(cand, jd_used, job_confirmed=bool(app_row.get("job_id")))
    # ⚠️ v2.0.1：`grade()` 在"学历门槛之外"已经**不再产出档位**（返回 None）——
    # 三档制下 A/B/C 由模型定，规则只负责学历门槛那条硬规则。
    # 而这里原来无条件把 `g.get("tier_suggested")` 写回去，等于**先抹成空**，
    # 全靠下面第 ⑧ 步模型成功返回才补回来；模型超时/不可用就永久留空。
    # 实测：副本上重跑张思瑞，档位由 C 变成 None。所以规则没给档时**保留原值**。
    _keep_tier = g.get("tier_suggested") or app_row.get("tier_suggested")
    if app_id:
        with lk:                                # 写段持锁（v1.24.8）
            conn.execute(
                """UPDATE applications SET score=?, tier_suggested=?, extract_mode=?,
                       reasons=?, risks=?, hits=?, miss=?, preferred_hit=?, breakdown=?,
                       suggested_job_id=COALESCE(?, suggested_job_id),
                       suggested_job_reason=COALESCE(?, suggested_job_reason),
                       updated_at=? WHERE id=?""",
                (g.get("score"), _keep_tier, cand.get("extract_mode"),
                 json.dumps(g.get("reasons") or [], ensure_ascii=False),
                 json.dumps(g.get("risks") or [], ensure_ascii=False),
                 json.dumps(g.get("hit") or [], ensure_ascii=False),
                 json.dumps(g.get("miss") or [], ensure_ascii=False),
                 json.dumps(g.get("preferred_hit") or [], ensure_ascii=False),
                 json.dumps(g.get("breakdown") or [], ensure_ascii=False),
                 (route.get("id") if (route and route.get("usable")) else None),
                 (route.get("why") if route else None),
                 db.now(), app_id))
            conn.commit()
        steps.append(f"重算建议档：{g.get('tier_suggested') or '—（规则不产档，保留原值）'}")

    # ---- ⑦ 附件台账补清洗记录与解析引擎 ----
    if doc:
        with lk:                                # 写段持锁（v1.24.8）
            conn.execute("UPDATE documents SET sensitive_found=?, parse_engine=?, "
                         "parse_ok=1, raw_text=?, text_len=? WHERE id=?",
                         (json.dumps(cand.get("sensitive_found") or {}, ensure_ascii=False),
                          engine, text, len(text), doc.get("id")))
            conn.commit()

    # ---- ⑧ 重算分析结论（画像 + 建议档同步）----
    # 原件已重新解析：旧的项目经历摘要可能来自上一版简历，先清缓存，
    # 让 analyze_one 在下面按新原文重炼（它只在 projects_json 为 NULL 时调模型）。
    with lk:
        db.update_candidate(conn, candidate_id, projects_json=None)
    analyze_one(conn, candidate_id, app_id, source="manual_reanalyze", wlock=lk)
    steps.append("重算系统自动分析（含项目经历）")

    with lk:                                    # 审计也是写，v1.24.8
        def _fmt_value(v):
            return '（空）' if v in (None, '') else v
        db.add_audit(conn, "candidate", str(candidate_id), "reanalyze_full",
                     "；".join(f"{db.AUDIT_FIELD_LABEL.get(k, k)}={_fmt_value(v[0])}"
                               for k, v in changes.items()) or "（字段无变化）",
                     "；".join(f"{db.AUDIT_FIELD_LABEL.get(k, k)}={_fmt_value(v[1])}"
                               for k, v in changes.items()) or "（字段无变化）",
                     "hr", "hr")
    return {"ok": True, "steps": steps, "changes": changes, "kept_hr": kept_hr,
            "extract_mode": cand.get("extract_mode"),
            "tier_suggested": g.get("tier_suggested"), "job": job_meta}



def _prepare_attachments(atts: list[dict]) -> list[dict]:
    """先把每份附件解析一次，供“选哪份是简历”和后续入库复用。

    为什么不能只看文件名：`技术研发岗-硕士-西安科技大学-电子信息-淡永康-男.pdf`
    不含“简历”二字，而 `CD389237_CODE5705....png` 可能是一张身份证照片。
    只有读内容，才能把真简历和补充材料分开。

    解析结果挂在 `_text / _engine / _ok`：
    - `ingest_one()` 用 `preparsed=` 复用，避免选中简历后再次 OCR；
    - `_save_other_attachments()` 直接落库，其他图片也因此“可读取”。

    解析失败不抛出：某一份附件坏了，不能拖垮整封邮件。
    """
    import tempfile

    mime_ext = {
        "application/pdf": ".pdf",
        "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
        "image/webp": ".webp", "image/bmp": ".bmp", "image/tiff": ".tif",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    }
    out: list[dict] = []
    with tempfile.TemporaryDirectory(prefix="hragent-att-") as td:
        for i, att in enumerate(atts or []):
            item = dict(att)
            name = str(item.get("filename") or f"attachment-{i}")
            ext = os.path.splitext(name)[1].lower()
            if ext not in parse_mod.SUPPORTED:
                ext = mime_ext.get(str(item.get("mime") or "").lower(), "")
            path = os.path.join(td, f"{i}{ext}")
            data = item.get("data") or b""
            try:
                with open(path, "wb") as fh:
                    fh.write(data)
                text, engine, ok = parse_mod.parse_file_ex(path)
            except Exception as exc:                          # noqa: BLE001
                text, engine, ok = "", f"preparse_failed:{type(exc).__name__}", False
            item["_text"] = str(text or "")
            item["_engine"] = str(engine or "")
            item["_ok"] = bool(ok and item["_text"].strip())
            out.append(item)
    return out


def _save_other_attachments(conn, cfg: dict, atts: list[dict],
                            candidate_id: int | None, message_id: str | None,
                            received_at: str | None = None) -> int:
    """归档**非简历附件**（v1.27）。返回成功归档的数量。

    做的事：按内容哈希落一份到原件区 + 写一条 `documents(kind='other')` 台账。
    刻意**不做**的事：不解析、不建档、不参与任何评分与去重判断。

    幂等：`documents.file_hash` 是 UNIQUE，重复收取同一份作品集只留一条台账、
    磁盘上也只有一份（`archive_file` 按内容哈希命名）。

    `candidate_id` 为 None 时照样归档 —— 本封邮件没有可归属的简历（比如简历本身
    被白名单过滤掉了），此时台账的 candidate_id 为空，"完整档案"里看不到，
    但原件仍在库里且可按 `source_message_id` 追溯。**宁可暂存也不丢件**。
    """
    saved = 0
    for att in atts or []:
        name = att.get("filename") or "attachment"
        data = att.get("data")
        if not data:
            continue
        try:
            archived, size = archive_file(cfg, name, data)
            digest = sha256_bytes(data)
            # 邮件路径已经预解析过：图片在这里已经 OCR，文本一并落库。
            # 没有预解析的旧调用方保持原行为（raw_text 空、parse_ok=None），
            # 界面仍显示“未解析”，不会把“没尝试”误报成“解析失败”。
            text = str(att.get("_text") or "")
            engine = str(att.get("_engine") or "")
            prepared = "_ok" in att
            if prepared:
                ok = bool(att.get("_ok") and text.strip())
                if not engine:
                    engine = "ocr_empty" if not ok else "parsed"
            else:
                ok = None
                engine = "非简历附件，未解析"
            db.insert_document(conn, {
                "file_hash": digest, "candidate_id": candidate_id,
                "application_id": None,
                "file_name": name, "file_path": archived, "archived_path": archived,
                "mime": att.get("mime") or parse_mod.mime_of(name), "size": size,
                "received_at": received_at or db.now(),
                "source_message_id": message_id,
                "raw_text": text, "parse_engine": engine,
                "parse_ok": ok, "kind": "other",
            })
            saved += 1
        except Exception:                                    # noqa: BLE001
            # 单个附件归档失败不能拖垮整封邮件（简历已经入库了）。
            # 失败只丢这一个附件，不回滚、不影响简历 —— 但也不能当没事发生，
            # 邮件台账里 attachment_count 仍记着它（record_email 已写）。
            continue
    return saved


def ingest_mails(mails: list[dict], cfg: dict, jd: dict, tiers: dict, db_path: str,
                 job_id: int | None = None, use_llm: bool = False,
                 llm_conf: dict | None = None, report: dict | None = None,
                 spawn: bool = True) -> dict:
    """邮件批次入库。

    归岗口径：**只看邮件标题**。标题里出现某个在招岗位名 → 该封简历归到那个岗位，
    并用该岗位自己的 JD 评分；标题里没有岗位名（或岗位已停用）→ 投递落「所属岗位待指定」，
    由 HR 事后在界面上指定。

    因此 `job_id` 参数**不参与归岗**（保留仅为兼容调用签名）：显式传一个岗位 id
    也不会让标题没写岗位名的简历自动挂上去 —— 否则一次误传就会把整批简历写错岗位。

    `report` / `spawn`（v1.29.4）：逐封调用时把一个**累积报表**传进来（计数与
    details 往它上面累加），并把 `spawn=False`（整批结束后由调用方挂一次分析）——
    避免"收一封分析一批"把进度和提示窗口搅乱。
    """
    conn = db.connect(db_path)
    if report is None:
        report = {"mails": 0, "attachments": 0, "added": 0, "merged_versions": 0,
                  "skipped_dup": 0, "failed": 0, "no_attachment": 0, "parse_failed": 0,
                  "routed": 0, "unassigned": 0,
                  # v1.27：非简历附件计数（收到几个 / 归档成功几个）
                  "other_attachments": 0, "other_saved": 0,
                  "details": []}
    report["mails"] = int(report.get("mails") or 0) + len(mails)
    try:
        for mail in mails:
            mid = mail.get("message_id")
            prev = conn.execute("SELECT processed FROM email_messages WHERE message_id = ?",
                                (mid,)).fetchone()
            if prev and prev["processed"]:
                report["skipped_dup"] += 1
                continue

            # —— 邮件标题：① 归岗 ② 结构化字段 ——
            # 主题格式 `应聘方向+学历+学校+专业+姓名+性别`（单位招聘邮箱的固定投递格式）。
            # 归岗先用"岗位名出现在主题里"的强匹配；没命中再拿「应聘方向」段做弱匹配——
            # 方向常写成"材料工艺"这类与岗位名不完全一致的词。
            meta = subject_meta_mod.parse(mail.get("subject"))
            route_job_id, route_jd = db.match_job_by_subject(conn, mail.get("subject"))
            if not route_job_id and (meta["fields"].get("direction")):
                route_job_id, route_jd = db.match_job_by_direction(
                    conn, meta["fields"]["direction"])
            eff_job_id = route_job_id if route_job_id else None
            eff_jd = route_jd or jd
            if eff_job_id:
                report["routed"] += 1
            else:
                report["unassigned"] += 1

            db.record_email(conn, {
                "message_id": mid, "uid": mail.get("uid"), "mailbox": mail.get("mailbox"),
                "from_addr": mail.get("from_addr"), "subject": mail.get("subject"),
                "body_text": mail.get("body_text"), "body_html": mail.get("body_html"),
                "received_at": mail.get("received_at"),
                "has_attachment": bool(mail.get("attachments")),
                "attachment_count": len(mail.get("attachments") or []),
                "error": mail.get("error"),
            })

            atts = mail.get("attachments") or []
            if not atts:
                report["no_attachment"] += 1
                # 让完成弹窗能列出"哪封邮件没有附件"，而不是只给一个数字。
                report["details"].append({
                    "file": mail.get("subject") or "（无附件邮件）",
                    "status": "no_attachment",
                    "name": None,
                    "notes": [f"发件人：{mail.get('from_addr') or '未知'}；"
                              f"{mail.get('error') or '没有简历附件'}"],
                })
                db.update_email(conn, mid, processed=1, processed_at=db.now(),
                                error=mail.get("error") or "无简历附件")
                continue

            added = mm = skipped = failed = pf = other_saved = 0
            # v1.27：先按 kind 分流。非简历附件（作品集/成绩单/证书扫描件…）
            # **不建档、不解析** —— 解析它们只会得到一堆空字段，
            # 然后各自变成一个"解析失败的新人"混进人才库。
            # 它们在下面统一挂到本封邮件**那位投递人**的档案上。
            # 先解析一遍全部附件，再按**内容**选简历。
            # 文件名只作线索：`技术研发岗-...-淡永康-男.pdf` 不含“简历”，
            # 而 `CD389237_....png` 也可能是一张身份证照片。
            # 一封邮件仍然只选一份简历、只建一个档案，其余作为补充材料归档；
            # 但补充材料也会 OCR/解析并落 raw_text，不再是“无法读取”。
            atts = _prepare_attachments(atts)
            _resume_idx = mb.choose_resume_attachment(atts)
            for _i, _a in enumerate(atts):
                _a["kind"] = "resume" if _i == _resume_idx else "other"
            _resume_atts = [atts[_resume_idx]] if _resume_idx >= 0 else []
            _other_atts = [a for _i, a in enumerate(atts) if _i != _resume_idx]
            # 本封邮件建出来的人（**局部变量**）。不能回头扫 `report["details"]` ——
            # 那个列表是**整批累积**的，会把上一封邮件的人挂到这封的附件上。
            _mail_cid: int | None = None

            for att in _resume_atts:
                report["attachments"] += 1
                try:
                    r = ingest_one(conn, cfg, eff_jd, tiers,
                                   filename=att["filename"], data=att["data"],
                                   local_path=None, channel="邮箱",
                                   applied_at=mail.get("received_at") or db.now(),
                                   source_message_id=mid, job_id=eff_job_id,
                                   use_llm=use_llm, llm_conf=llm_conf,
                                   subject_meta=meta,
                                   preparsed=((att.get("_text") or "",
                                               att.get("_engine") or "",
                                               bool(att.get("_ok")))
                                              if "_ok" in att else None))
                    if r["status"] == "added":
                        added += 1
                    elif r["status"] == "merged_version":
                        mm += 1
                    else:
                        skipped += 1
                    # 判"解析失败"用 parse_failed 字段，不靠匹配 notes 文案——
                    # v1.24.9 文案改成「简历读不出」后，按字符串匹配会静默失效。
                    if r.get("parse_failed"):
                        pf += 1
                    if _mail_cid is None and r.get("candidate_id"):
                        _mail_cid = r["candidate_id"]
                    report["details"].append(r)
                except Exception as exc:  # 单份附件失败不影响整批
                    failed += 1
                    report["details"].append({"file": att.get("filename"), "status": "failed",
                                              "notes": [f"处理异常：{exc}"]})

            # 非简历附件：挂到**这封邮件建出来的人**下面。
            # 挂不上的（本封没有任何简历成功建档）也要归档，只是暂时找不到归属
            # —— 不能静默丢弃，那等于"收到了但没存"。
            if _other_atts:
                other_saved += _save_other_attachments(
                    conn, cfg, _other_atts, candidate_id=_mail_cid, message_id=mid,
                    received_at=mail.get("received_at"))
                report["other_attachments"] += len(_other_atts)
                _notes = [f"{n}已归档"
                          + ("" if _mail_cid else "（本封没有可归属的简历，暂存未挂人）")
                          for n in [a.get("filename", "") for a in _other_atts]]
                # 口径（业务方 2026-10-27 明确）：**一封邮件只建一个档案**。
                # 同封邮件里的其余文件一律视为这个人的补充材料，挂在他的
                # 「其他附件」下。真有两个人的简历被塞进同一封邮件，
                # 那是投递方的问题 —— 不做自动拆分，也不额外解析去猜。
                report["details"].append({
                    "file": f"（本封共 {len(_other_atts)} 个非简历附件）",
                    "status": "other_saved",
                    "candidate_id": _mail_cid,
                    "notes": _notes,
                })
            report["added"] += added
            report["merged_versions"] += mm
            report["skipped_dup"] += skipped
            report["failed"] += failed
            report["parse_failed"] += pf
            report["other_saved"] += other_saved
            db.update_email(conn, mid, processed=1, processed_at=db.now(),
                            added=added, skipped=skipped, failed=failed)
        # 入库完成 → 后台异步做分析（不阻断本次返回，失败也不影响上面这些计数）。
        # spawn=False 时（逐封调用的批次）由调用方在整批结束后挂一次分析（v1.29.4）。
        if spawn:
            spawn_auto_analysis(db_path, report)
        return report
    finally:
        conn.close()


def source_roots(cfg: dict | None = None) -> list[str]:
    """允许作为「导入目录」的根：配置的三个来源目录 + 数据目录本身。"""
    from .paths import DATA_DIR
    roots = [os.path.join(DATA_DIR, "resumes"), DATA_DIR]
    for k in ("folder_dir", "eml_dir", "archive_dir"):
        v = (cfg or {}).get(k)
        if v:
            roots.append(mb.resolve_dir(v))
    out: list[str] = []
    for r in roots:
        try:
            rp = os.path.realpath(r)
        except OSError:
            continue
        if rp not in out:
            out.append(rp)
    return out


def resolve_source_dir(value: str | None, cfg: dict | None = None) -> str:
    """把导入目录解析成绝对路径，并**收口在允许的来源目录内**（代码审查 P1-12）。

    `folder` 这类参数直接来自请求体/模型参数，而 `mb.resolve_dir` 对**绝对路径
    原样返回**——不校验就等于开一个本地任意文件读取口子：谁都能让服务去扫
    （并入库）本机任意目录里的 PDF / Word。
    不传值时走配置的 `folder_dir`（界面「导入本地文件夹」就走这条），行为不变。

    越界抛 `ValueError`，由调用方决定怎么告诉用户（接口转 4xx、助手回一句话）。
    """
    from .paths import DATA_DIR
    raw = (value or "").strip()
    if not raw:
        return mb.resolve_dir((cfg or {}).get("folder_dir") or os.path.join(DATA_DIR, "resumes"))
    if ".." in raw.replace("\\", "/").split("/"):
        raise ValueError("导入目录不合法（不接受 ..）")
    real = os.path.realpath(mb.resolve_dir(raw))
    for root in source_roots(cfg):
        if real == root or real.startswith(root + os.sep):
            return real
    raise ValueError(f"导入目录不在允许的来源目录内：{real}"
                     "（可在系统配置里改「导入文件夹」后再导入）")


def ingest_dir(folder: str, jd: dict, tiers: dict, db_path: str,
               cfg: dict | None = None, channel: str = "文件夹",
               job_id: int | None = None, use_llm: bool = False,
               llm_conf: dict | None = None, progress_tid: str | None = None) -> dict:
    """手工导入文件夹中的简历（离线可用，不依赖邮箱）。

    体积上限与邮件附件**同一口径**（`max_attachment_mb`）。此前这条校验只在
    邮件路径上有，文件夹导入是"照单全收"——一份 17MB 的技术书 PDF 就是这么进来的：
    解析必然失败、落成一个"待人工判读"的空档案，还凭空多出一个候选人。
    超限的文件**不导入、也不删**，只在明细里说明原因（HR 自己决定怎么处理）。
    """
    cfg = cfg or mb.load_config()
    limit = int(cfg.get("max_attachment_mb", 20) or 20) * 1024 * 1024
    conn = db.connect(db_path)
    files = sorted(f for f in glob.glob(os.path.join(folder, "*"))
                   if f.lower().endswith(parse_mod.SUPPORTED))
    report = {"scanned": len(files), "added": 0, "merged_versions": 0,
              "skipped_dup": 0, "skipped_oversize": 0, "failed": 0,
              "parse_failed": 0, "max_attachment_mb": limit // (1024 * 1024), "details": []}
    # v1.29.4（业务方）：带 progress_tid 时 = "先报总数、再逐份推进 + 可停止"。
    # 停止是**读完当前文件**到下一个循环边界才停（不会把半份文件砍断）；
    # 不带进度的旧调用方（助手工具 / 自检）行为与原来完全一致。
    if progress_tid:
        update_progress(progress_tid, total=len(files),
                        message=f"已发现 {len(files)} 份文件，开始读取…")
    try:
        for _i, path in enumerate(files):
            if stop_requested(progress_tid):
                report["stopped"] = True
                if progress_tid:
                    update_progress(progress_tid,
                                    message=f"已停止：完成 {_i} / {len(files)} 份后按你的要求停下")
                break
            base = os.path.basename(path)
            try:
                size = _lp_size(path)
            except OSError as exc:
                report["failed"] += 1
                report["details"].append({"file": base, "status": "failed",
                                          "notes": [f"读取文件失败：{exc}"]})
                if progress_tid:
                    update_progress(progress_tid, done=_i + 1)
                continue
            if size > limit:
                report["skipped_oversize"] += 1
                report["details"].append({
                    "file": base, "status": "skipped_oversize", "size": size,
                    "notes": [f"超过体积上限：{size / 1024 / 1024:.1f} MB > "
                              f"{limit // (1024 * 1024)} MB，未导入（文件仍在原处，未删）"],
                })
                db.add_audit(conn, "source_file", base, "oversize_skipped", "",
                             f"{size} 字节 > {limit} 字节，未导入", "system", "system")
                if progress_tid:
                    update_progress(progress_tid, done=_i + 1)
                continue
            try:
                # 文件名归岗（与邮件「标题归岗」同一口径）：
                # 文件名里出现某个在招岗位名 → 归到该岗位，并按该岗位的 JD 评分。
                # 为什么要这样：HR 的实际做法就是把投递岗位写进简历文件名
                # （「张三-科学研究-校招-简历.pdf」），文件名本身就是投递意向的表达，
                # 不认它，全员都会落到"待指定"、再按默认尺子算出一个错的分。
                # 文件名里没有岗位名时仍为 None（待 HR 指定），不硬凑。
                fj_id, fj_jd = db.match_job_by_subject(conn, base)
                # v1.9：文件名若按「方向+学历+学校+专业+姓名+性别」的格式命名，
                # 同一套解析器直接复用——这些字段是投递方填的，比正文抽取准。
                fmeta = subject_meta_mod.parse(base)
                if not fj_id and fmeta["fields"].get("direction"):
                    fj_id, fj_jd = db.match_job_by_direction(
                        conn, fmeta["fields"]["direction"])
                eff_job_id = fj_id if fj_id else job_id
                eff_jd = fj_jd if fj_jd else jd
                if fj_id:
                    report["routed"] = report.get("routed", 0) + 1
                r = ingest_one(conn, cfg, eff_jd, tiers,
                               filename=base, data=None, local_path=path,
                               channel=channel, applied_at=db.now(), source_message_id=None,
                               job_id=eff_job_id, use_llm=use_llm, llm_conf=llm_conf,
                               subject_meta=fmeta)
                report[{"added": "added", "merged_version": "merged_versions",
                        "skipped_dup": "skipped_dup"}.get(r["status"], "failed")] += 1
                # 同上：用字段判断，不匹配文案
                if r.get("parse_failed"):
                    report["parse_failed"] += 1
                report["details"].append(r)
            except Exception as exc:
                report["failed"] += 1
                report["details"].append({"file": base, "status": "failed",
                                          "notes": [f"处理异常：{exc}"]})
            if progress_tid:
                update_progress(progress_tid, done=_i + 1)
        # 与邮件路径同一处钩子：文件夹导入同样"进门就有判断"
        spawn_auto_analysis(db_path, report)
        return report
    finally:
        conn.close()


def _cursor_load(raw, account: str) -> str:
    """读收信游标（v1.29.4：游标**按账号隔离**）。

    新格式 {"account": "user@host", "uid": "403"}：账号不一致 → 当没有游标，
    重扫一遍（去重兜底，不丢不重）—— 换账号后拿旧账号的 UID 去跳邮件 = 静默漏收，
    这是比"多扫一遍"严重得多的事故。
    历史格式（纯 UID 字符串、没有账号信息）：沿用一次，下次保存自动升级为新格式
    —— 升级当天的行为与以前完全一致，不做任何隐式清零。
    """
    if isinstance(raw, dict):
        return str(raw.get("uid") or "") if str(raw.get("account") or "") == account else ""
    s = str(raw or "").strip()
    return s if s.isdigit() else ""


def _cursor_save(db_path: str, folder: str, account: str, uid: str) -> tuple[bool, str]:
    """写收信游标（best-effort）。返回 (是否成功, 错误文本)。

    游标只是"上次收到哪"的备忘，**写不进去不该让整条收取报错**：
    邮件此刻已经收到并入库了，让 HR 看到 500 反而以为白跑了。
    v1.24.11：实测"目录刚复制完、进程刚启动"瞬间 SQLite 还在建 WAL，
    这里会抛 attempt to write a readonly database —— 降级为警告即可，
    最坏后果只是下次重扫一遍（入库侧按内容去重）。中途停止时也照常落盘。
    """
    try:
        conn = db.connect(db_path)
        try:
            db.set_email_cursor(conn, folder, {"account": account, "uid": str(uid)})
        finally:
            conn.close()
        return True, ""
    except Exception as exc:                             # noqa: BLE001
        print(f"[ingest] 邮件游标写入失败（不影响本次收取结果）：{exc}")
        return False, str(exc)


def _is_transient_imap_error(exc: Exception) -> bool:
    """连接断开、超时、DNS/代理暂时不可用 → 可以无限重试。

    认证失败、密码没配置这类是永久错误，重试只会刷日志，必须直接报错。
    """
    import imaplib

    if isinstance(exc, (OSError, EOFError, TimeoutError)):
        return True
    if isinstance(exc, imaplib.IMAP4.abort):
        return True
    if isinstance(exc, imaplib.IMAP4.error):
        text = str(exc).lower()
        return any(k in text for k in
                   ("unavailable", "timeout", "try again", "temporarily",
                    "connection", "eof", "bye"))
    return False


def sync_mailbox(jd: dict, tiers: dict, db_path: str, job_id: int | None = None,
                 use_llm: bool = False, llm_conf: dict | None = None,
                 cfg: dict | None = None, progress_tid: str | None = None,
                 limit: int = 200) -> dict:
    """拉取邮箱 → 入库。统一的"收简历"动作。

    v1.29.4（业务方）：带 `progress_tid`（且 imap 模式）时按**逐封**处理 ——
      · 先生出总数（"已发现 N 封"），再每收完一封报一次进度；
      · 每封之间检查中断旗（`request_stop`），"读完当前这封"就停下，
        游标停在最后成功处理的那封（下次从这里继续，不重复、不丢件）；
      · 每封入库**不单独挂分析**，整批结束后挂一次 —— "抓一个分析一个"
        会把进度与提示窗口搅乱，业务方点名不要。
    不带 progress_tid 时行为与旧版一致（一次性拉全再入库，分析钩子在 ingest_mails 里）。
    """
    cfg = cfg or mb.load_config()
    if cfg.get("mode") == "off":
        return {"mode": "off", "mails": 0, "added": 0,
                "message": "邮箱抓取已在 config/mailbox.json 中关闭"}
    ic = cfg.get("imap") or {}
    folder_name = ic.get("folder", "INBOX")
    # v1.29.4：游标按账号隔离（换收信账号不再共享同一个 UID 游标）
    account = f"{ic.get('user') or ''}@{ic.get('host') or ''}"
    conn = db.connect(db_path)
    cursor = _cursor_load(db.email_cursor(conn, folder_name), account)
    conn.close()

    newest = cursor or ""
    stopped = False

    if cfg.get("mode") == "imap" and progress_tid:
        # ---- 逐封模式：先总数、再逐封（可停止、可接力分析）----
        report: dict = {"mails": 0, "attachments": 0, "added": 0, "merged_versions": 0,
                        "skipped_dup": 0, "failed": 0, "no_attachment": 0,
                        "parse_failed": 0, "routed": 0, "unassigned": 0,
                        "other_attachments": 0, "other_saved": 0, "details": []}
        total = 0
        done = 0
        mailbox_total = 0
        # 关键：**先把新邮件全部下载完，再离线入库**。
        #
        # 以前是"下载一封 → OCR/模型处理几十秒 → 再下载下一封"：IMAP
        # 连接在 OCR 期间空闲，企业邮/代理会按空闲超时把它断掉，表现就是
        # 收了几封后 ConnectionResetError，然后靠重连硬撑。
        #
        # 现在先一次性下载（连接一直有数据流动，不会被空闲超时），
        # 下载完关闭连接，再慢慢入库。下载失败仍按退避无限重试；
        # 用户停止、服务关闭、全部收完是仅有的三种结束条件。
        retries = 0
        mails: list[dict] = []
        new_cursor = cursor
        while True:
            try:
                update_progress(progress_tid, message="正在连接邮箱并下载新邮件…")
                mails, new_cursor = mb.fetch_imap(cfg, cursor, limit)
                break
            except Exception as exc:
                if stop_requested(progress_tid):
                    stopped = True
                    report.pop("error", None)
                    break
                if not _is_transient_imap_error(exc):
                    report["error"] = str(exc)
                    break
                retries += 1
                report["reconnects"] = retries
                report.pop("error", None)
                delay = min(60, 2 ** min(retries, 6))
                update_progress(
                    progress_tid,
                    message=(f"连接中断（{type(exc).__name__}），"
                             f"{delay} 秒后第 {retries} 次重试；"
                             f"正在重新下载…"))
                # 退避期间也响应"停止"，不能睡死。
                for _ in range(delay):
                    if stop_requested(progress_tid):
                        stopped = True
                        break
                    time.sleep(1)
                if stopped:
                    break
                continue

        if not report.get("error") and not stopped:
            total = len(mails)
            newest = new_cursor or newest
            report["new_mails"] = total
            update_progress(
                progress_tid, total=total,
                message=(f"已下载 {total} 封新邮件，开始入库…" if total
                         else "没有新邮件"))
            for item in mails:
                if stop_requested(progress_tid):
                    stopped = True
                    update_progress(progress_tid,
                                    message=f"已停止：完成 {done} / {total} 封后按你的要求停下")
                    break
                ingest_mails([item], cfg, jd, tiers, db_path, job_id, use_llm, llm_conf,
                             report=report, spawn=False)
                newest = item.get("uid") or newest
                done += 1
                update_progress(progress_tid, done=done)
                # 长批次每 25 封落一次游标；被强杀后下次从断点继续。
                if done % 25 == 0 and newest:
                    _cursor_save(db_path, folder_name, account, newest)
        # 整批结束后挂一次分析（中途停止时分析"已收到的这批"）
        spawn_auto_analysis(db_path, report)
    else:
        # ---- 旧路径（eml 演练模式 / 不带进度的调用）----
        try:
            mails, new_cursor = mb.incoming(cfg, cursor)
        except Exception as exc:
            return {"mode": cfg.get("mode"), "mails": 0, "added": 0, "error": str(exc)}
        if progress_tid:
            update_progress(progress_tid, total=len(mails),
                            message=f"已发现 {len(mails)} 封邮件，开始收取…")
        report = ingest_mails(mails, cfg, jd, tiers, db_path, job_id, use_llm, llm_conf)
        newest = new_cursor or newest
        if progress_tid:
            update_progress(progress_tid, done=len(mails))

    report["mode"] = cfg.get("mode")
    if stopped:
        report["stopped"] = True
    # ⚠️ 中途停止/报错也走这里：游标停在"最后成功处理的那封"，下次继续。
    # 写失败只降级为警告（v1.24.11 口径不变），报告里带 cursor_saved=False。
    if cfg.get("mode") == "imap" and newest and newest != (cursor or ""):
        _ok, _err = _cursor_save(db_path, folder_name, account, newest)
        if not _ok:
            report["cursor_saved"] = False
            report["cursor_error"] = _err
    return report


# ============================================================
# 入库即分析（v1.8）：把"人点一下才出分析"变成"进门就有判断"
# ============================================================

def _insight_targets(report: dict) -> list[dict]:
    """挑出需要自动分析的结果：新增（added）与新版本归档（merged_version）。

    跳过重复件与失败件——它们没有新的候选人信息，分析也没有意义。
    """
    out: list[dict] = []
    for d in report.get("details") or []:
        if (d.get("status") in ("added", "merged_version")
                and d.get("candidate_id") and d.get("application_id")):
            out.append({"candidate_id": int(d["candidate_id"]),
                        "application_id": int(d["application_id"])})
    return out


def analyze_one(conn, candidate_id: int, application_id: int,
                source: str = "auto_ingest",
                wlock: threading.RLock | None = None) -> dict | None:
    """给一位候选人跑一次分析并落库，返回落库的 insight（失败返回 None）。

    **自动与手动共用这一份逻辑**（v1.13.4）："入库即分析"开关关掉后，HR 手动批量
    触发走的是同一段代码——不然两条路会各自漂移，最后"手动分析出来的"和
    "自动分析的"口径不一致，那比没有开关更糟。
    分析只写 `candidate_insights` 与「系统建议档位」，**不动 HR 已确认的档位、
    不动阶段、不动岗位**。

    `wlock`（v1.24.8）：并发分析时传入共享连接的写锁。**只在落库那一小段加锁**，
    读与模型调用都不持锁——并发收益 99.98% 来自"并行等模型"，不要自己掐掉。
    不传 wlock 就是单线程用法（手动触发、单元测试），行为不变。
    """
    from .pipeline.analyze import auto_insight     # 延迟导入，避免模块环
    lk = wlock or threading.RLock()
    with lk:
        cand = db.candidate_detail(conn, candidate_id)
        if not cand:
            return None
        apps = cand.get("applications") or []
        app_row = next((a for a in apps if a.get("id") == application_id),
                       apps[0] if apps else {})
        # 未归岗时 jd 为 None —— 不硬套默认尺子，但仍产出简历画像
        jd, _meta = db.resolve_candidate_job(conn, cand)
        # 读完收一次事务，别把只读事务带进后面的网络等待
        try:
            conn.commit()
        except Exception:                 # noqa: BLE001 — 兜底即可
            pass

    # ↓ 这一段**不持锁**：耗时 6~15 秒的网络调用，并发全靠它
    ins = auto_insight(cand, jd, app_row)
    if not ins:
        return None

    # 项目经历：**入库分析阶段一并提炼并缓存**（v5.0.1）。
    # 过去只在前端打开档案时按需调模型，HR 每点一个人都要等十几秒；
    # 现在随自动分析预先算好，点开档案直接命中缓存。已有缓存不重复调用；
    # 完整重分析会先清空缓存（server.reanalyze），因此换简历后仍会重炼。
    proj_payload = None
    if not cand.get("projects_checked"):
        raw_text = str(cand.get("raw_text") or "").strip()
        if raw_text:
            try:
                from .agent import llm as _llm
                _cfg = _llm.load_cfg() or {}
                if _cfg.get("enabled", True) and _cfg.get("api_key"):
                    from .pipeline import projects as projects_mod
                    proj_payload = projects_mod.summarize(raw_text, _cfg)
            except Exception as exc:                       # noqa: BLE001
                print(f"[projects] 候选人#{candidate_id} 项目经历预提炼失败："
                      f"{type(exc).__name__}: {exc}", file=sys.stderr)

    # 落库：只包住写操作，串行化的成本约 1.7ms，可忽略
    with lk:
        db.upsert_insight(conn, candidate_id, application_id,
                          summary=ins.get("summary") or "",
                          reasons=ins.get("reasons"),
                          risks=ins.get("risks"),
                          evidence=ins.get("evidence"),
                          source=ins.get("source") or source,
                          model=ins.get("model") or "",
                          business_direction=ins.get("business_direction"))
        if proj_payload is not None:
            db.update_candidate(conn, candidate_id,
                                projects_json=json.dumps(proj_payload, ensure_ascii=False))
        # 模型建议档位 → 更新 tier_suggested（HR 未确认时才更新）
        mt = ins.get("suggested_tier")
        if mt and mt in tiers_mod.all_tiers() and application_id:
            ar = conn.execute(
                "SELECT tier_suggested, tier_final FROM applications WHERE id=?",
                (application_id,)).fetchone()
            # v1.24.15：这里曾经只有"HR 未确认"一个条件，而注释写着「D 不覆盖」——
            # 代码根本没实现。学历不够的人若因未归岗被存成 C，模型就能改成 A。
            # 现在由 auto_insight 产出的 `edu_gate` 现算裁决，
            # 并且**只有学历门槛判出的 D 才冻结**——模型自己判的 D
            # （学历达标但业务方向不对口）在后续分析里仍可改写，
            # 否则会把正常的重判一并锁死。
            gate = ins.get("edu_gate") or tier_mod.edu_gate(cand, jd or {})
            _edu_locked = bool(gate.get("failed"))
            _t, _src = tier_mod.lock_tier(mt if _edu_locked else None,
                                          None if _edu_locked else mt, gate)
            if _t != mt:
                print(f"[analyze] 候选人#{candidate_id} 模型建议 {mt} 档被学历门槛否决"
                      f"（{gate.get('edu')}<{gate.get('edu_min')}），保留 {_t}", file=sys.stderr)
                mt = _t
            if ar and not ar["tier_final"] and ar["tier_suggested"] != mt \
                    and not (_edu_locked and ar["tier_suggested"] == tiers_mod.hard_shortfall_tier()
                             and mt != tiers_mod.hard_shortfall_tier()):
                conn.execute(
                    "UPDATE applications SET tier_suggested=?, updated_at=? WHERE id=?",
                    (mt, db.now(), application_id))
                conn.commit()
                print(f"[analyze] 候选人#{candidate_id} 档位 "
                      f"{ar['tier_suggested']} → {mt}（{_src}）", file=sys.stderr)
    return ins


def spawn_auto_analysis(db_path: str, report: dict, limit: int = 0) -> int:
    """把入库结果排入后台分析队列，返回排队条数。

    `limit=0`（默认）= **本次导入的全部**都分析（v1.23.4）。
    原来默认 20：一次导入 200 份只分析前 20 份，剩下的要 HR 手动点
    「分析待分析的人」——HR 的原话是「我导入的全部分析就好了，反正我是有开关的」。
    关掉自动分析有专门的开关（`auto_insight_on_ingest`），不需要再用条数隐式截断。

    现在是**并发**跑（`TP_ANALYZE_CONCURRENCY`，默认 6 路）：模型调用是网络等待型，
    串行时每人 11 秒、1000 人要 3 小时；并发后同样的活能压到 40 分钟量级。

    设计约束：
    1) **异步且不阻断**：分析再慢、模型再挂，入库结果都已返回，
       HR 看到的"新增 4"就是真的新增了 4；
    2) **不碰档位**：只写候选人的分析文本（`candidate_insights`），
       档位/阶段/岗位一律不动——自动分析不等于自动决定；
    3) **失败只记录**：单条失败不影响其余，也不会让入库报错；
    4) **可用 `TP_AUTO_INSIGHT=0` 关闭**（自检用），也可用**产品开关**
       `auto_insight_on_ingest` 关闭（v1.13.4，HR 自己在界面上关）。
       关掉时把**跳过条数**写进 report，让入库结果如实说明"这几个人没分析"，
       并提示可手动批量触发——不能让人误以为"已经分析过了"。
    """
    if os.environ.get("TP_AUTO_INSIGHT", "1") == "0":
        return 0
    targets_all = _insight_targets(report)
    # 产品开关：默认开（保持既有行为）；关掉时入库不自动调模型
    try:
        _c = db.connect(db_path)
        _on = bool(db.get_setting(_c, "auto_insight_on_ingest", True))
        _c.close()
    except Exception as exc:                               # noqa: BLE001
        print(f"[auto_insight] 读开关失败，按默认开启处理："
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        _on = True
    if not _on:
        report["insight_skipped"] = len(targets_all)
        return 0
    # limit<=0 = 不截断（全部分析）
    targets = targets_all if int(limit) <= 0 else targets_all[:int(limit)]
    if not targets:
        return 0

    def _work(tid: str) -> None:
        # **串行，每人一条自己的连接**（v1.24.8 定案，与批量重跑一致）。
        # 进度（v1.24.9）：每完成一人就 update_progress，前端轮询画进度条。
        #
        # 为什么放弃并发——两种方案都试过，都不稳：
        #   ① 每个线程各自 db.connect() → 间歇 `attempt to write a readonly database`
        #      （Windows 上多连接并发写同一 WAL 库，文件锁判定不稳）
        #   ② 共享一条连接 + RLock → 写段稳了，但读段/游标仍触发
        #      `InterfaceError: bad parameter or other API misuse`
        #      （Connection 的游标是连接级共享状态，多线程交替 fetch 即误用）
        #
        # 串行的代价：N 份简历的模型调用排队，N×6~15 秒。
        # 入库分析本来就是后台跑（HTTP 立即返回"已入队"），HR 不等它，
        # 所以这个代价**落在后台、不挡界面**，比偶发失败划算得多。
        #
        # 以后要提速，正确方向是**多进程**（进程间不共享连接状态），
        # 而不是多线程共享一条连接。
        done = {"ok": 0, "fail": 0}
        try:
            for t in targets:
                conn = None
                try:
                    conn = db.connect(db_path)
                    analyze_one(conn, t["candidate_id"], t["application_id"],
                                source="auto_ingest")
                    done["ok"] += 1
                except Exception as exc:               # noqa: BLE001 — 单条失败不扩散
                    done["fail"] += 1
                    print(f"[auto_insight] 候选人#{t['candidate_id']} 分析失败："
                          f"{type(exc).__name__}: {exc}", file=sys.stderr)
                    traceback.print_exc()             # 堆栈落盘，便于定位
                finally:
                    if conn is not None:
                        try:
                            conn.close()
                        except Exception:             # noqa: BLE001
                            pass
                update_progress(tid, done=done["ok"] + done["fail"],
                                failed=done["fail"])
        finally:
            update_progress(tid, status="done",
                            message=(f"成功 {done['ok']} 人"
                                     + (f"，失败 {done['fail']} 人"
                                        if done["fail"] else "")))
        print(f"[auto_insight] 完成 {done['ok']} 条，失败 {done['fail']} 条"
              f"（串行）", file=sys.stderr)

    # 登记进度任务，task_id 写进 report，前端轮询 /api/tasks/{task_id}（v1.24.9）
    # meta 带上 phase 与上一阶段 report：页面刷新/服务重启后能直接认出
    # "这是分析阶段"，而不是误当成收取阶段、把统计全显示成 0。
    tid = register_progress("入库后自动分析", len(targets), db_path=db_path,
                            meta={"phase": "insight", "report": dict(report)})
    report["insight_task_id"] = tid

    # 后台异步跑，不阻断入库结果返回（HR 看到的"新增 N"是真的新增）
    threading.Thread(target=_work, name="auto-insight", daemon=True,
                     args=(tid,)).start()

    report["insight_queued"] = len(targets)
    # 串行（v1.24.8）：保留该字段只为兼容前端进度显示，值固定为 1。
    report["insight_concurrency"] = 1
    return len(targets)
