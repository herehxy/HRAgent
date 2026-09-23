"""FastAPI 服务：本地内网工作台 + 智能体接口。

访问模式：

- **本机模式**（默认）：无需登录，唯一角色 `hr`（单角色，不分子集权限）。
- **登录模式**（设 `TP_AUTH=1`）：走真实账号 + 会话令牌（`X-TP-Token`），身份不可伪造。

端点分组（共 62 条业务路由，含根路径 `/`；FastAPI 自带的 /docs、/redoc、/openapi.json 另计）：

======================  ==========================================================================
分组                     端点
======================  ==========================================================================
会话                     `/` `/api/login` `/api/logout` `/api/me` `/api/meta`
人才库                   `/api/candidates` `/api/candidates/{cid}` `/api/stats` `/api/pipeline`
归档                     `/api/candidates/{cid}/archive|unarchive`（软归档，可恢复）
                         `/api/candidates/{cid}/assign-job`（采纳建议岗位，C 方案）
投递操作                 `/api/applications/{aid}/tier|stage|note`
档案合并                 `/api/candidates/merge` `/api/candidates/split` `/api/candidates/{cid}/duplicates`
收简历与来源             `/api/ingest` `/api/sources` `/api/emails`
来源文件管理             `/api/sources/file` `/api/sources/bundle` `/api/sources/remove`
                        `/api/sources/removed` `/api/sources/restore`
简历原件                 `/api/documents/{did}/file`（下载 / inline=1 预览）
部门与岗位               `/api/departments`（含 `{did}/activate|deactivate`）**——接口层保留、
                        界面已不再暴露**：2026-09 改版后岗位只写名称 + JD，部门概念整体退出
                        产品界面（`jobs.dept_id` 兼容旧库保留为空，不参与任何评分/路由）
                        `/api/jobs` `/api/jobs/{jid}` `/api/jobs/{jid}/jd`
                        `/api/jobs/{jid}/regrade`（JD 改后按新尺子重算已有投递）
                        `/api/jobs/{jid}/activate|deactivate`（停用，不物理删除）邮箱配置                 `/api/mailbox/config`（GET/POST）`/api/mailbox/presets`
                        `/api/mailbox/test` `/api/mailbox/preview`
设置                     `/api/settings`（GET/POST；含性别筛选开关，默认关闭）
检索                     `/api/search/skills` `/api/search/semantic` `/api/search/similar` `/api/search/index` `/api/search/status`
智能体                   `/api/agent/chat` `/api/agent/tools` `/api/agent/runs` `/api/agent/status`
                        `/api/candidates/{cid}/analyze|interview|explain`
提案与审计               `/api/proposals` `/api/proposals/{pid}/decide` `/api/audit`
系统                     `/api/ontology` `/api/policy` `/api/health`
======================  ==========================================================================

注意：检索三条路径的返回值会经 `_with_contact()` 处理后下发——
联系方式解密只在这一层做（与人才库列表同一口径），密文/盲索引/身份键不下发。

**性别**：`/api/candidates` 支持 `gender=男|女|未标注`，但**只有设置项
`gender_filter_enabled` 打开时才真正生效**；开关关闭时请求被忽略并如实回话
（`gender_filter.applied=false`）。性别永不进入 `tier.grade()`，不参与任何评分/分级。
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import threading
import time
import urllib.parse
import uuid
import zipfile
from datetime import datetime

from fastapi import Body, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel

from . import actions, auth, db, ingest as ingest_mod, mailbox as mb, regrade, search
from .agent import llm
from .agent.loop import run_agent
from .agent.tools import ToolCtx, catalog as tool_catalog
from .pipeline import domains as domains_mod
from .pipeline import majors as mj
from .pipeline import normalize as nz
from .pipeline import parse as parse_mod
from .pipeline import sanitize
from .pipeline.analyze import analyze_fit, draft_interview
from .ui import render_page, ui_build

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 允许用 TP_DB_PATH 指向另一个库：演练、验收、接口实测时不必动真实人才库
DB_PATH = os.environ.get("TP_DB_PATH") or os.path.join(BASE, "data", "workbench.db")
JD_PATH = os.path.join(BASE, "config", "jd.json")
TIERS_PATH = os.path.join(BASE, "config", "tiers.json")
RESUME_DIR = os.path.join(BASE, "data", "resumes")
# 回收目录：界面上"删除来源文件"只是**移入这里**，随时可恢复。
# 刻意与 data/archive（原件区，只增不改）分开——两个目录的不变式完全不同，
# 混在一起会让"原件区只增不改"这条红线变得无法核验。
REMOVED_DIR = os.path.join(BASE, "data", "removed")

app = FastAPI(title="企业人才库智能体", version="1.7.0")


def _load(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _jd_tiers() -> tuple[dict, dict]:
    return _load(JD_PATH), _load(TIERS_PATH)


# ---------------------------------------------------------------- 设置项

# 性别筛选开关。**产品默认关闭**：招聘不得限定性别（《就业促进法》第 27 条、
# 《妇女权益保障法》第 43 条），所以它必须是一个"HR 主动打开、且打开留痕"的开关，
# 而不是默认就能用的筛选条件。性别本身**永远不进入 `tier.grade()`**。
GENDER_FILTER_KEY = "gender_filter_enabled"


def _settings(conn) -> dict:
    return {"gender_filter_enabled": bool(db.get_setting(conn, GENDER_FILTER_KEY, False))}


def _auth_enabled() -> bool:
    return os.environ.get("TP_AUTH", "").strip().lower() in ("1", "true", "on", "yes")


def _session(x_tp_token: str | None, x_tp_role: str | None) -> dict:
    conn = db.connect(DB_PATH)
    try:
        if _auth_enabled():
            s = auth.resolve(conn, x_tp_token)
            s["mode"] = "login"
        else:
            # 单 HR 角色：本机模式不再需要角色切换，统一 hr
            s = auth.resolve(conn, None)
            s.update({"role": "hr", "username": "hr",
                      "display_name": auth.ROLES["hr"]["label"],
                      "permissions": auth.permissions("hr"), "mode": "local"})
        return s
    finally:
        conn.close()


def require(session: dict, perm: str) -> None:
    if not auth.can(session["role"], perm):
        raise HTTPException(status_code=403, detail=f"当前角色（{session['role']}）无 {perm} 权限")


def _ctx(session: dict, conn=None) -> ToolCtx:
    jd, tiers = _jd_tiers()
    return ToolCtx(db_path=DB_PATH, jd=jd, tiers=tiers, job_id=None,
                   session_id=uuid.uuid4().hex[:12],
                   operator=session.get("username", "HR"), role=session["role"])


# ============================================================
# 页面与会话
# ============================================================

@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return render_page(auth_enabled=_auth_enabled())


@app.get("/api/health")
def health() -> dict:
    conn = db.connect(DB_PATH)
    try:
        return {"ok": True, "version": app.version, "auth": "login" if _auth_enabled() else "local",
                "people": db.pool_stats(conn)["people"]}
    finally:
        conn.close()


class LoginReq(BaseModel):
    username: str
    password: str


@app.post("/api/login")
def api_login(req: LoginReq) -> dict:
    conn = db.connect(DB_PATH)
    try:
        auth.ensure_seed_users(conn)
        r = auth.authenticate(conn, req.username, req.password)
        if not r:
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        return r
    finally:
        conn.close()


@app.post("/api/logout")
def api_logout(x_tp_token: str | None = Header(default=None, alias="X-TP-Token")) -> dict:
    conn = db.connect(DB_PATH)
    try:
        if x_tp_token:
            db.delete_session(conn, x_tp_token)
        return {"ok": True}
    finally:
        conn.close()


@app.get("/api/me")
def api_me(x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
           x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    return _session(x_tp_token, x_tp_role)


@app.get("/api/meta")
def api_meta(x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
             x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    s = _session(x_tp_token, x_tp_role)
    conn = db.connect(DB_PATH)
    try:
        jd = _load(JD_PATH)
        cfg = mb.load_config()
        return {
            "session": s,
            "jd": jd,
            "tiers": _load(TIERS_PATH),
            # 前端版本戳：验收脚本据此证明"页面跑的是当前这版前端"
            # （服务未重启时它反映旧代码，从而暴露"验的是上一版"）。
            "ui_build": ui_build(),
            "jobs": db.list_jobs(conn, include_inactive=True),
            "departments": db.list_departments(conn, include_inactive=True),
            "tools": tool_catalog(),
            "ontology": nz.describe(),
            "parse": parse_mod.engine_capabilities(),
            "mailbox": {"mode": cfg.get("mode"), "eml_dir": cfg.get("eml_dir"),
                        "imap_host": (cfg.get("imap") or {}).get("host") or "",
                        "imap_port": (cfg.get("imap") or {}).get("port", 993),
                        "imap_ssl": (cfg.get("imap") or {}).get("ssl", True),
                        "imap_user": (cfg.get("imap") or {}).get("user") or "",
                        "imap_folder": (cfg.get("imap") or {}).get("folder", "INBOX"),
                        "password_set": bool(mb.read_secret()),
                        "readonly": (cfg.get("imap") or {}).get("readonly", True),
                        "attachment_ext": cfg.get("attachment_ext"),
                        "max_attachment_mb": cfg.get("max_attachment_mb", 20),
                        "same_job_reapply_days": (cfg.get("dedup") or {}).get("same_job_reapply_days")},
            "settings": _settings(conn),
            # 数据存放（「系统说明」页展示）：相对应用目录，换机器部署口径不变
            "storage": {
                "base": os.path.basename(BASE),
                "db": os.path.relpath(DB_PATH, BASE),
                "archive_dir": os.path.relpath(
                    os.path.join(BASE, "data", "archive"), BASE),
                "resume_dir": os.path.relpath(RESUME_DIR, BASE),
                "removed_dir": os.path.relpath(REMOVED_DIR, BASE),
                "mail_dir": os.path.relpath(os.path.join(BASE, "data", "mail_in"), BASE),
                "backup_dir": os.path.relpath(os.path.join(BASE, "data", "backup"), BASE),
                "config_dir": "config",
            },
            "model": llm.status(),
            "search": search.status(DB_PATH),
            "stages": db.STAGES,
            "tier_labels": db.TIER_LABELS,
        }
    finally:
        conn.close()


# ============================================================
# 人才库
# ============================================================

@app.get("/api/candidates")
def api_candidates(tier: str | None = None, kw: str | None = None,
                   stage: str | None = None, education: str | None = None,
                   min_years: int | None = None, gender: str | None = None,
                   archived: str | None = None,
                   page: int = 0, page_size: int = 0,
                   x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                   x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """人才库列表。

    性别筛选是**受开关约束**的：设置项 `gender_filter_enabled` 关闭时，
    即使传了 `gender` 也按"未筛选"处理，并在响应里说明为什么——
    静默忽略与静默生效一样危险，后者会让 HR 误以为筛出来的就是全部。

    归档（v1.4）：默认只回未归档；`?archived=1` 只回已归档（「归档」页用）。
    归档是软隐藏不是删除，档案/投递/附件原样保留。

    分页（v1.7.1）：`page` 从 1 计、`page_size` 每页条数（界面用 10）。
    **两者都不传（或传 0）时返回全量**——这是刻意保留的兼容口径：
    探针、智能体工具、导出等程序化消费方依赖"一次拿全"，不应被迫翻页；
    分页只是人才库界面的阅读方式，不是接口的默认行为。
    翻页在**全部筛选与建议岗位补齐之后**做，所以 `total` 是筛后总数，
    `gender_facets` 也是全量口径——页大小不会影响任何统计数字。
    """
    s = _session(x_tp_token, x_tp_role)
    require(s, "read")
    conn = db.connect(DB_PATH)
    try:
        cfg = _settings(conn)
        allowed = bool(cfg.get("gender_filter_enabled"))
        want = (gender or "").strip() if allowed else ""
        want_archived = (archived or "").strip() in ("1", "true", "yes")
        items = db.list_candidates(conn, tier=tier, keyword=kw, stage=stage,
                                   education=education, min_years=min_years,
                                   archived=True if want_archived else False)
        # 分布统计与结果过滤共用 `db.gender_matches` 一个口径：
        # 否则会出现"点了男，列表 3 个人，下拉里写着 4"这种对不上的情况。
        facets = {g: sum(1 for i in items if db.gender_matches(i.get("gender"), g))
                  for g in db.GENDER_GROUPS}
        facets["全部"] = len(items)
        if want:
            items = [i for i in items if db.gender_matches(i.get("gender"), want)]
        # 建议岗位（v1.5）：**入库时就已对全部在招岗位逐个试算**，结论落在
        # `applications.suggested_job_id` 上，`score / tier_suggested` 也是按那个岗位的尺子算的
        # （见 ingest._route_by_open_jobs）——所以这里直接读库，不再二次计算：
        # 详情页、列表、导出看到的是同一个数，历史结论也不会因"后来新建了岗位"而变。
        # 只有 v1.5 之前入库的老数据（列为空）才走实时试算兜底。
        jobs_map = {j["id"]: j for j in db.list_jobs(conn, include_inactive=True)}
        pending = [i for i in items if i.get("job_id") is None]
        legacy: list[dict] = []
        for i in pending:
            jid = i.get("suggested_job_id")
            j = jobs_map.get(jid) if jid else None
            if j:
                i["job_suggestion"] = {
                    "job_id": j["id"], "title": j.get("title") or "",
                    "dept": j.get("department_name") or j.get("dept") or "",
                    "score": i.get("score"), "tier_suggested": i.get("tier_suggested"),
                    "source": "stored",
                }
            else:
                legacy.append(i)
        if len(jobs_map):
            for i in pending:
                i.setdefault("job_suggestions_considered", len(jobs_map))
        if legacy:
            tiers_cfg = _load(TIERS_PATH)
            open_jobs = [{"id": j["id"], "title": j.get("title") or "",
                          "dept": j.get("department_name") or j.get("dept") or "",
                          "jd": j.get("jd_json") or {}}
                         for j in db.list_jobs(conn, include_inactive=False)]
            for i in legacy:
                sugs = regrade.suggest_jobs(conn, i.get("resume_doc_id"), open_jobs, tiers_cfg)
                top = sugs[0] if sugs else None
                # 兜底路径不做档位过滤：v1.5 起"不适合"由 HR 看（卡片上标明档位），
                # 隐掉建议反而让人以为系统没算——但会标 `source` 说明是现算的，未经入库固化。
                i["job_suggestion"] = (
                    {"job_id": top["job_id"], "title": top["title"], "dept": top["dept"],
                     "score": top["score"], "tier_suggested": top["tier_suggested"],
                     "source": "live"}
                    # `suggest_jobs` 返回的字段名是 `hits`（与 grade 的 `hit` 不同名），
                    # 判定必须用它——写成 `hit` 会永远取到 None，建议全被吞掉
                    if top and (top.get("hits") or []) else None)
                i["job_suggestions_considered"] = len(open_jobs)
        # 「归档」页要能写清"还有几天被彻底删除"：天数由后端算，前端不自己推日期
        if want_archived:
            meta = db.archive_meta(conn)
            for i in items:
                i["archive"] = meta.get(i["id"]) or {"days_left": None, "purge_at": ""}
        # 分页切片放在最后：total / facets / 建议岗位都按**筛后全量**算好，
        # 再切当前页——翻页只改变"这一屏显示谁"，不改变任何统计与结论。
        total_after_filter = len(items)
        paging = None
        if page_size and page_size > 0:
            pages = max(1, (total_after_filter + page_size - 1) // page_size)
            cur = min(max(1, page or 1), pages)
            items = items[(cur - 1) * page_size: cur * page_size]
            paging = {"page": cur, "page_size": page_size, "total": total_after_filter,
                      "total_pages": pages}
        presented = auth.present_list(items)
        out = {"count": len(presented), "items": presented,
               "my_permissions": s["permissions"],
               "archived_view": want_archived,
               "gender_facets": facets if allowed else None,
               "paging": paging,
               "gender_filter": {
                   "enabled": allowed,
                   "requested": (gender or "").strip() or None,
                   "applied": bool(want),
                   "why": None if allowed else
                          "性别筛选开关处于关闭状态（合规默认），本次按未筛选处理；"
                          "如需使用请到「系统说明」打开并留痕。",
               }}
        if allowed:
            out["gender_filter"]["note"] = "性别仅来自简历明写标签，不参与评分与分级。"
        return out
    finally:
        conn.close()


@app.get("/api/candidates/{cid}")
def api_candidate(cid: int, x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                  x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    s = _session(x_tp_token, x_tp_role)
    require(s, "read")
    conn = db.connect(DB_PATH)
    try:
        d = db.candidate_detail(conn, cid)
        if not d:
            raise HTTPException(status_code=404, detail="未找到该候选人")
        # 查看完整档案 -> 留痕（保密检查会要这份凭证）
        db.add_audit(conn, "candidate", str(cid), "view", "", f"{s['username']} 查看完整档案",
                     s["username"], s["role"])
        latest_doc = d["documents"][0] if d.get("documents") else {}
        raw = d.get("raw_text") or ""
        return {
            **auth.present_candidate(d),
            "skills": d.get("skills"),
            "tags": d.get("tags"),
            "applications": d.get("applications"),
            "documents": [{"id": x["id"], "file_name": x.get("file_name"),
                           "parse_engine": x.get("parse_engine"), "parse_ok": x.get("parse_ok"),
                           "size": x.get("size"), "received_at": x.get("received_at"),
                           "archived_path": os.path.basename(x.get("archived_path") or "")}
                          for x in d.get("documents", [])],
            "raw_text": raw,
            "parse_engine": latest_doc.get("parse_engine"),
            "audit_snippet": db.candidate_audit(conn, cid, limit=20),
            "duplicates": db.suspicious_duplicates(conn, cid),
            "agent_runs": [],
            "tier_effective": d.get("tier_effective"),
        }
    finally:
        conn.close()


@app.post("/api/candidates/{cid}/archive")
def api_candidate_archive(cid: int,
                          x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                          x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """归档：从人才库与检索中隐藏，档案/投递/附件/审计全部保留，可随时取消。"""
    s = _session(x_tp_token, x_tp_role)
    require(s, "set_stage")
    conn = db.connect(DB_PATH)
    try:
        out = db.set_candidate_archived(conn, cid, True, s["username"], s["role"])
        if not out:
            raise HTTPException(status_code=404, detail="未找到该候选人")
        return {"ok": True, "id": cid, "archived": True,
                "note": "已归档：移入「归档」页，不再在人才库与检索中展示。"}
    finally:
        conn.close()


@app.post("/api/candidates/{cid}/unarchive")
def api_candidate_unarchive(cid: int,
                            x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                            x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    s = _session(x_tp_token, x_tp_role)
    require(s, "set_stage")
    conn = db.connect(DB_PATH)
    try:
        out = db.set_candidate_archived(conn, cid, False, s["username"], s["role"])
        if not out:
            raise HTTPException(status_code=404, detail="未找到该候选人")
        return {"ok": True, "id": cid, "archived": False,
                "note": "已取消归档：恢复在人才库与检索中展示。"}
    finally:
        conn.close()


@app.post("/api/candidates/route-pending")
def api_route_pending(apply: int = 1,
                      x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                      x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """重新为「待指定」投递给建议岗位并按最适岗位重算系统建议（v1.5）。

    什么时候用：新建/修改了岗位 JD、或批量导入了没写岗位名的简历之后。
    服务启动时也会自动跑一次（存量数据升级用）。`?apply=0` 只预演不落库。
    """
    s = _session(x_tp_token, x_tp_role)
    require(s, "set_stage")
    conn = db.connect(DB_PATH)
    try:
        return regrade.route_pending(conn, _load(TIERS_PATH), apply=bool(apply),
                                     operator=s["username"], role=s["role"])
    finally:
        conn.close()


class ArchiveBatchReq(BaseModel):
    """批量归档：按 id 列表，或按"归档某年及以前的投递"。

    两个入口都保留，因为 HR 的两种真实动作不一样：
    - 勾选一批人 → `ids`（看名单逐个点）；
    - 换年度 → `before_year`（"把 2026 年以前的都收起来"，人太多不可能一个个勾）。
    两者可以同时给，取并集。
    """
    ids: list[int] | None = None
    before_year: int | None = None
    archived: bool = True


@app.post("/api/candidates/archive-batch")
def api_candidates_archive_batch(req: ArchiveBatchReq,
                                 x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                                 x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """批量归档 / 批量取消归档（v1.5）。

    为什么要批量：这套系统按年使用，第二年的简历进来会和上一年混在一起；
    逐条点不现实。**归档仍是软隐藏**——30 天内随时可取消归档把人捞回来，
    只有归档满 30 天才由清理任务彻底删除（`db.PURGE_AFTER_DAYS`）。
    """
    s = _session(x_tp_token, x_tp_role)
    require(s, "set_stage")
    ids = set(int(i) for i in (req.ids or []))
    conn = db.connect(DB_PATH)
    try:
        picked_by_year = 0
        if req.before_year:
            rows = db.list_candidates(conn, archived=(False if req.archived else True))
            for c in rows:
                # 归档按"最后一条投递的年份"判断：换年度归档时，上一年投递过的人收起来
                year = str(c.get("applied_at") or "")[:4]
                if year.isdigit() and int(year) < req.before_year:
                    ids.add(int(c["id"]))
                    picked_by_year += 1
        # 两种"空"要分开：**没给任何条件**是请求问题（400）；
        # **给了年份但这一年以前没人**是正常结果（200 + changed=0）——
        # 前者是误用，后者是"今年就是最早的一年"，报错会让人以为功能坏了。
        if req.before_year is None and not ids:
            raise HTTPException(status_code=400,
                                detail="没有指定要归档的人（ids 与 before_year 至少给一个）")
        out = db.set_candidates_archived(conn, sorted(ids), req.archived, s["username"], s["role"])
        out["picked_by_year"] = picked_by_year
        out["purge_after_days"] = db.PURGE_AFTER_DAYS
        out["note"] = ("已归档，可在「归档」页查看；满 "
                       f"{db.PURGE_AFTER_DAYS} 天后会被彻底删除" if req.archived
                       else "已取消归档，恢复在人才库与检索中展示")
        return out
    finally:
        conn.close()


def _purge_expired(conn=None, actor: str = "system", role: str = "system") -> dict:
    """把归档满 30 天的档案彻底删除，并把原件移进回收目录。

    自有连接便于被定时任务与接口两处复用。**先移文件再删库**：万一移动失败，
    库里记录还在（下次还能重试），不会出现"记录没了、文件也不知在哪"。
    """
    own = conn is None
    conn = conn or db.connect(DB_PATH)
    try:
        out = db.purge_due_candidates(conn, db.PURGE_AFTER_DAYS, actor, role)
        moved, missing = [], []
        for rel in out["files"]:
            src = rel if os.path.isabs(rel) else os.path.join(BASE, rel)
            if not os.path.exists(src):
                missing.append(rel)
                continue
            dest_dir = os.path.join(REMOVED_DIR, "purged")
            os.makedirs(dest_dir, exist_ok=True)
            dest = os.path.join(dest_dir, os.path.basename(src))
            try:
                if os.path.exists(dest):
                    stamp = datetime.now().strftime("%Y%m%d%H%M%S")
                    dest = os.path.join(dest_dir,
                                        f"{os.path.splitext(os.path.basename(src))[0]}"
                                        f"_{stamp}{os.path.splitext(src)[1]}")
                shutil.move(src, dest)
                moved.append(rel)
            except OSError:
                missing.append(rel)
        out["files_moved"] = len(moved)
        out["files_missing"] = missing
        return out
    finally:
        if own:
            conn.close()


def _purge_chats(conn=None, actor: str = "system", role: str = "system") -> dict:
    """把"清空满 30 天"的对话运行记录真正删掉（v1.6）。

    与 `_purge_expired`（归档满 30 天）是**同一套保留期、两个对象**：档案是"人没了"，
    对话是"聊天记录没了"。清空当天只推进清空点、不动数据，所以这 30 天里人还能
    一键恢复；到期才由这里落最后一刀，删之前先把条数/token 合计写进 `chat_purge` 审计。
    """
    own = conn is None
    conn = conn or db.connect(DB_PATH)
    try:
        return db.purge_due_chats(conn, db.PURGE_AFTER_DAYS, actor, role)
    finally:
        if own:
            conn.close()


@app.post("/api/archive/purge-due")
def api_archive_purge_due(x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                          x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """手动触发一次"到期彻底删除"（正常由服务启动与每日定时任务自动执行）。"""
    s = _session(x_tp_token, x_tp_role)
    require(s, "set_stage")
    out = _purge_expired(actor=s["username"], role=s["role"])
    out["note"] = (f"已彻底删除 {out['purged']} 人（归档满 {out['days']} 天），"
                   f"原件移入回收目录 {out['files_moved']} 份")
    return out


@app.post("/api/candidates/{cid}/purge")
def api_candidate_purge(cid: int,
                        x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                        x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """彻底删除**单个**已归档且已满 30 天的人。

    未满 30 天一律拒绝（409）并说明还剩几天——"删除"必须有一个不可逆的冷静期，
    否则一次误点就没了；这也是界面上"彻底删除"按钮只在这一天才出现的原因。
    """
    s = _session(x_tp_token, x_tp_role)
    require(s, "set_stage")
    conn = db.connect(DB_PATH)
    try:
        meta = db.archive_meta(conn)
        info = meta.get(cid)
        if info is None:
            raise HTTPException(status_code=404, detail="未找到该候选人（或该档案未归档）")
        left = info.get("days_left")
        if left is None or left > 0:
            raise HTTPException(status_code=409,
                                detail=f"归档未满 {info['purge_after_days']} 天（还剩 {left} 天），"
                                       f"期间可随时取消归档；到期后系统会自动彻底删除")
        out = _purge_expired(conn=conn, actor=s["username"], role=s["role"])
        got = [p for p in out["people"] if p["id"] == cid]
        if not got:
            raise HTTPException(status_code=500, detail="删除未生效，请查看审计日志")
        return {"ok": True, "id": cid, "purged": got[0],
                "files_moved": out["files_moved"],
                "note": f"已彻底删除（原件移入回收目录 {REMOVED_DIR}）"}
    finally:
        conn.close()


@app.on_event("startup")
def _startup_purge_task() -> None:
    """启动时清理一次到期数据，之后每 24 小时一次。

    两类对象共用一套 30 天保留期：
    - 归档满 30 天的**档案**（v1.5）：`_purge_expired()`；
    - 清空满 30 天的**对话运行记录**（v1.6）：`_purge_chats()`。

    为什么放在服务进程里而不是依赖外部 cron：这套系统的部署形态是"内网一台机器、
    双击就起"，没人会去配 crontab。启动跑一次保证"关机很久再开机"也能收敛；
    daemon 线程随进程退出，不留残留。**清理失败只打印、不阻断启动**——
    数据清理是后台维护动作，不该让 HR 打不开工作台。
    """
    def _loop() -> None:
        while True:
            time.sleep(24 * 3600)
            try:
                _purge_expired()
            except Exception as exc:                       # noqa: BLE001
                print(f"[purge] 定时清理失败：{exc}", file=sys.stderr)
            # 对话保留期同样每 24 小时检查一次；与归档清理相互独立，
            # 一个失败不影响另一个（所以分成两个 try）。
            try:
                out = _purge_chats()
                if out.get("purged"):
                    print(f"[purge] 定时清理：删除 {out['purged']} 条"
                          f"清空满 {out['days']} 天的对话运行记录")
            except Exception as exc:                       # noqa: BLE001
                print(f"[purge] 对话定时清理失败：{exc}", file=sys.stderr)

    try:
        out = _purge_expired()
        if out.get("purged"):
            print(f"[purge] 启动清理：彻底删除 {out['purged']} 位归档满 "
                  f"{out['days']} 天的人（原件移入 {REMOVED_DIR}）")
    except Exception as exc:                               # noqa: BLE001
        print(f"[purge] 启动清理失败（不影响使用）：{exc}", file=sys.stderr)
    try:
        out = _purge_chats()
        if out.get("purged"):
            print(f"[purge] 启动清理：删除 {out['purged']} 条清空满 "
                  f"{out['days']} 天的对话运行记录（保留期内已无人恢复）")
    except Exception as exc:                               # noqa: BLE001
        print(f"[purge] 对话启动清理失败（不影响使用）：{exc}", file=sys.stderr)
    # 顺手把库内技能分类对齐到当前本体（v1.6）：专业大类匹配依赖 skills.category，
    # 而 upsert_skill 不会改写已存在技能的分类，所以本体新增技能（如 Java/Spring Boot）后
    # 必须同步一次，否则老条目仍归在「其他」里，大类匹配对软件岗就是失真的。
    # 幂等且只改分类列，放在 route_pending 之前——重算抽取出来的技能也按新分类落库。
    try:
        conn = db.connect(DB_PATH)
        try:
            from .pipeline import normalize as _nz
            sync = db.sync_skill_categories(conn, _nz.ontology_categories())
        finally:
            conn.close()
        if sync.get("changed"):
            print(f"[ontology] 技能分类同步：更新 {sync['changed']} 条"
                  f"（库内共 {sync['total']} 条）")
    except Exception as exc:                               # noqa: BLE001
        print(f"[ontology] 技能分类同步失败（不影响使用）：{exc}", file=sys.stderr)
    # 顺手把存量「待指定」投递按最适岗位重算一次：老库里的分数是按默认尺子（材料类）
    # 算的，升级后不清算的话，界面上的"错标"依旧在。只处理未归岗的投递，
    # HR 已确认的档位不动（见 regrade.route_pending）。
    try:
        conn = db.connect(DB_PATH)
        try:
            rep = regrade.route_pending(conn, _load(TIERS_PATH))
        finally:
            conn.close()
        if rep.get("changed"):
            print(f"[route] 已为 {rep['changed']} 条「待指定」投递按最适岗位重算建议"
                  f"（在招岗位 {rep['open_jobs']} 个）")
    except Exception as exc:                               # noqa: BLE001
        print(f"[route] 存量岗位建议重算失败（不影响使用）：{exc}", file=sys.stderr)
    # 再兜底刷一次已归岗投递的技能清单（v1.6）：技能按「本体 + 岗位 JD 词表」抽取，
    # 早期已归岗的投递没跟上刷新，会出现"命中里有 Java、技能栏里没有"的自相矛盾（缺陷 #47）。
    # 幂等且不动档位与分数，所以放启动里跑是安全的；失败只打印、不阻断启动。
    try:
        conn = db.connect(DB_PATH)
        try:
            rsk = regrade.refresh_skills(conn, operator="startup", role="system")
        finally:
            conn.close()
        if rsk.get("refreshed"):
            print(f"[skills] 按对应岗位刷新了 {rsk['refreshed']} 条投递的技能清单"
                  f"（写入 {rsk['skills_written']} 项；档位与分数未改动）")
    except Exception as exc:                               # noqa: BLE001
        print(f"[skills] 技能清单刷新失败（不影响使用）：{exc}", file=sys.stderr)
    threading.Thread(target=_loop, daemon=True, name="tp-purge").start()


class AssignJobReq(BaseModel):
    job_id: int

@app.post("/api/candidates/{cid}/assign-job")
def api_assign_job(cid: int, req: AssignJobReq,
                   x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                   x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """采纳岗位建议：把该候选人最新的「待指定」投递归到指定岗位（C 方案落库动作）。"""
    s = _session(x_tp_token, x_tp_role)
    require(s, "set_stage")
    conn = db.connect(DB_PATH)
    try:
        row = conn.execute(
            """SELECT id FROM applications WHERE candidate_id = ? AND job_id IS NULL
               ORDER BY COALESCE(applied_at,'') DESC, id DESC LIMIT 1""", (cid,)).fetchone()
        if not row:
            raise HTTPException(status_code=400, detail="该候选人没有「待指定」的投递")
        job = db.get_job(conn, req.job_id)
        if not job:
            raise HTTPException(status_code=404, detail="未找到该岗位")
        r = regrade.assign_job(conn, row["id"], req.job_id, job.get("jd_json") or {},
                               _load(TIERS_PATH), s["username"], s["role"])
        if r.get("error"):
            raise HTTPException(status_code=400, detail=r["error"])
        return r
    finally:
        conn.close()


@app.get("/api/stats")
def api_stats(x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
              x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    s = _session(x_tp_token, x_tp_role)
    require(s, "read")
    conn = db.connect(DB_PATH)
    try:
        return db.pool_stats(conn)
    finally:
        conn.close()


@app.get("/api/pipeline")
def api_pipeline(x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                 x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    s = _session(x_tp_token, x_tp_role)
    require(s, "read")
    conn = db.connect(DB_PATH)
    try:
        p = db.pipeline_stats(conn)
        p["stage_order"] = db.STAGES
        return p
    finally:
        conn.close()


@app.get("/api/candidates/{cid}/duplicates")
def api_duplicates(cid: int, x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                   x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    s = _session(x_tp_token, x_tp_role)
    require(s, "read")
    conn = db.connect(DB_PATH)
    try:
        return {"candidate_id": cid, "suspects": db.suspicious_duplicates(conn, cid),
                "note": "系统只提示疑似重复，不会自动合并；合并需 HR 确认且可撤销"}
    finally:
        conn.close()


class MergeReq(BaseModel):
    source_id: int
    target_id: int


@app.post("/api/candidates/merge")
def api_merge(req: MergeReq, x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
              x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    s = _session(x_tp_token, x_tp_role)
    require(s, "merge")
    conn = db.connect(DB_PATH)
    try:
        r = db.merge_candidates(conn, req.source_id, req.target_id, s["username"], s["role"])
        if not r.get("ok"):
            raise HTTPException(status_code=400, detail=r.get("error"))
        return r
    finally:
        conn.close()


class SplitReq(BaseModel):
    candidate_id: int


@app.post("/api/candidates/split")
def api_split(req: SplitReq, x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
              x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    s = _session(x_tp_token, x_tp_role)
    require(s, "merge")
    conn = db.connect(DB_PATH)
    try:
        return db.split_candidate(conn, req.candidate_id, s["username"], s["role"])
    finally:
        conn.close()


# ============================================================
# 投递操作
# ============================================================

class TierReq(BaseModel):
    tier: str
    note: str | None = None


class StageReq(BaseModel):
    stage: str


class NoteReq(BaseModel):
    note: str


@app.post("/api/applications/{aid}/tier")
def api_set_tier(aid: int, req: TierReq, x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                 x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    s = _session(x_tp_token, x_tp_role)
    require(s, "confirm")
    if req.tier not in ("A", "B", "C", "D"):
        raise HTTPException(status_code=400, detail="档位只能是 A/B/C/D")
    conn = db.connect(DB_PATH)
    try:
        r = db.set_application_tier(conn, aid, req.tier, req.note, s["username"], s["role"])
        if not r:
            raise HTTPException(status_code=404, detail="未找到该投递")
        return r
    finally:
        conn.close()


@app.post("/api/applications/{aid}/stage")
def api_set_stage(aid: int, req: StageReq, x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                  x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    s = _session(x_tp_token, x_tp_role)
    require(s, "set_stage")
    if req.stage not in db.STAGES:
        raise HTTPException(status_code=400, detail=f"阶段只能是 {'/'.join(db.STAGES)}")
    conn = db.connect(DB_PATH)
    try:
        r = db.set_application_stage(conn, aid, req.stage, s["username"], s["role"])
        if not r:
            raise HTTPException(status_code=404, detail="未找到该投递")
        return r
    finally:
        conn.close()


@app.post("/api/applications/{aid}/note")
def api_add_note(aid: int, req: NoteReq, x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                 x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    s = _session(x_tp_token, x_tp_role)
    require(s, "add_note")
    conn = db.connect(DB_PATH)
    try:
        r = db.add_note(conn, aid, req.note, s["username"], s["role"])
        if not r:
            raise HTTPException(status_code=404, detail="未找到该投递")
        return r
    finally:
        conn.close()


# ============================================================
# 收简历 / 台账
# ============================================================

class IngestReq(BaseModel):
    source: str = "mailbox"      # mailbox | folder
    use_llm: bool = False
    folder: str | None = None


@app.post("/api/ingest")
def api_ingest(req: IngestReq | None = Body(default=None),
               x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
               x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    s = _session(x_tp_token, x_tp_role)
    require(s, "ingest")
    req = req or IngestReq()
    jd, tiers = _jd_tiers()

    llm_conf = None
    if req.use_llm:
        c = llm.load_cfg()
        llm_conf = {"api_key": c.get("api_key"), "base_url": c.get("base_url"),
                    "model": c.get("model")}

    cfg = mb.load_config()
    if req.source == "folder":
        # 文件夹上传：只分析、不归岗（job_id=None = 待 HR 指定）
        folder = req.folder or cfg.get("folder_dir") or RESUME_DIR
        report = ingest_mod.ingest_dir(folder, jd, tiers, DB_PATH, cfg=cfg, job_id=None,
                                       use_llm=req.use_llm, llm_conf=llm_conf)
        report["source"] = "folder"
        report["source_label"] = f"本地文件夹 {os.path.abspath(folder)}"
    else:
        # 邮箱：按邮件标题归岗（在 ingest_mails 内部完成），job_id 参数不参与
        report = ingest_mod.sync_mailbox(jd, tiers, DB_PATH, job_id=None,
                                         use_llm=req.use_llm, llm_conf=llm_conf, cfg=cfg)
        ic = cfg.get("imap") or {}
        report["source"] = "mailbox"
        report["source_label"] = (f"邮箱 {ic.get('user') or '（未配置账号）'} @ "
                                  f"{ic.get('host') or '（未配置服务器）'} / {ic.get('folder','INBOX')}"
                                  if cfg.get("mode") == "imap"
                                  else f"演练邮件目录 {mb.resolve_dir(cfg.get('eml_dir',''))}")

    # 导入后自动做**增量**建索引：只补"画像变了/还没索引"的人，不做全量重建
    report["index"] = _auto_index()
    _remember_ingest(report, s.get("username") or "hr")
    return report


def _auto_index() -> dict:
    """导入完成后自动增量建索引。失败不影响导入结果，但要如实带回去。"""
    try:
        r = search.build_index(DB_PATH, force=False)
        return {"ok": True, "indexed": r.get("indexed", 0), "skipped": r.get("skipped", 0),
                "total": r.get("total"), "model": r.get("model"),
                "error": r.get("error"), "note": r.get("note")}
    except Exception as exc:  # 索引失败不能影响"简历已收进来"这个事实
        return {"ok": False, "indexed": 0, "error": f"{type(exc).__name__}: {exc}"}


def _remember_ingest(report: dict, operator: str) -> None:
    """把最近一次导入的报表存进 settings，刷新页面后仍能回看"导入了什么"。"""
    brief = {k: v for k, v in report.items() if k != "details"}
    brief["details"] = (report.get("details") or [])[:60]
    brief["at"] = db.now()
    brief["operator"] = operator
    conn = db.connect(DB_PATH)
    try:
        db.set_setting(conn, "last_ingest", brief)
    finally:
        conn.close()


@app.get("/api/sources")
def api_sources(x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """"简历是从哪来的"：本地文件夹绝对路径 + 文件清单；邮箱是哪个账号 + 口令状态。

    清单一并标出「超过体积上限」的文件：导入时会跳过它们，
    提前标红比导完之后再解释"为什么这份没进来"要省事得多。
    """
    s = _session(x_tp_token, x_tp_role)
    require(s, "read")
    cfg = mb.load_config()
    ic = cfg.get("imap") or {}
    limit_bytes = int(cfg.get("max_attachment_mb", 20) or 20) * 1024 * 1024

    folder = cfg.get("folder_dir") or RESUME_DIR
    abs_folder = os.path.abspath(mb.resolve_dir(folder))
    files: list[dict] = []
    if os.path.isdir(abs_folder):
        conn = db.connect(DB_PATH)
        try:
            for name in sorted(os.listdir(abs_folder)):
                full = os.path.join(abs_folder, name)
                if not os.path.isfile(full):
                    continue
                if not name.lower().endswith(tuple(parse_mod.SUPPORTED)):
                    continue
                st = os.stat(full)
                digest = ingest_mod._hash_path(full)
                prev = conn.execute(
                    "SELECT d.id, c.name FROM documents d LEFT JOIN candidates c ON c.id = d.candidate_id"
                    " WHERE d.file_hash = ?", (digest,)).fetchone()
                files.append({
                    "name": name, "size": st.st_size,
                    "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                    "indexed": bool(prev),
                    "candidate": prev["name"] if prev else None,
                    "document_id": prev["id"] if prev else None,
                    "over_limit": st.st_size > limit_bytes,
                })
        finally:
            conn.close()

    eml_dir = mb.resolve_dir(cfg.get("eml_dir", "data/mail_in"))
    removed: list[dict] = []
    if os.path.isdir(REMOVED_DIR):
        for month in sorted(os.listdir(REMOVED_DIR), reverse=True):
            mdir = os.path.join(REMOVED_DIR, month)
            if not os.path.isdir(mdir):
                continue
            for name in sorted(os.listdir(mdir)):
                if os.path.isfile(os.path.join(mdir, name)) and not name.startswith("."):
                    removed.append({"name": name, "month": month})
    conn = db.connect(DB_PATH)
    try:
        recent = db.get_setting(conn, "last_ingest")
        cursor = db.email_cursor(conn, ic.get("folder", "INBOX"))
        last_remove = db.get_setting(conn, "last_remove")
    finally:
        conn.close()

    return {
        "folder": {"path": abs_folder, "exists": os.path.isdir(abs_folder),
                   # config_dir 是配置里写的原值（界面输入框要回填它，path 是解析后的绝对路径）
                   "config_dir": cfg.get("folder_dir") or RESUME_DIR,
                   "default_dir": RESUME_DIR,
                   "max_attachment_mb": int(cfg.get("max_attachment_mb", 20) or 20),
                   "count": len(files), "files": files},
        "eml_dir": {"path": eml_dir, "exists": os.path.isdir(eml_dir)},
        "mailbox": {"mode": cfg.get("mode"), "host": ic.get("host") or "",
                    "port": ic.get("port", 993), "ssl": ic.get("ssl", True),
                    "user": ic.get("user") or "", "folder": ic.get("folder", "INBOX"),
                    "readonly": ic.get("readonly", True),
                    "password_set": bool(mb.read_secret()),
                    "account": (f"{ic.get('user')} @ {ic.get('host')}"
                                if ic.get("user") and ic.get("host") else ""),
                    "cursor": cursor},
        "recycle": {"dir": os.path.relpath(REMOVED_DIR, BASE), "count": len(removed),
                    "items": removed[:60], "last_remove": last_remove},
        "last_ingest": recent,
    }


def _file_session(header_token: str | None, query_token: str | None) -> dict:
    """文件类接口的会话解析：允许用查询参数补令牌。

    预览/下载走的是浏览器原生导航（`<a href>`、`window.open`、新标签页），
    没法带自定义请求头——登录模式下如果只认 `X-TP-Token`，这几个入口会直接 401。
    所以**仅文件类只读接口**接受 `?token=` 兜底；其余接口仍只认请求头。
    """
    return _session(header_token or query_token, None)


@app.get("/api/documents/{did}/file")
def api_document_file(did: int, inline: int = 0, token: str | None = None,
                      x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                      x_tp_role: str | None = Header(default=None, alias="X-TP-Role")):
    """下载/预览简历原件。inline=1 时在浏览器里直接打开（PDF、文本可预览）。"""
    s = _file_session(x_tp_token, token)
    require(s, "read")
    conn = db.connect(DB_PATH)
    try:
        row = conn.execute("SELECT * FROM documents WHERE id = ?", (did,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="未找到该附件")
        d = dict(row)
        db.add_audit(conn, "document", str(did), "download" if not inline else "preview",
                     "", d.get("file_name") or "", s["username"], s["role"])
    finally:
        conn.close()

    path = d.get("archived_path") or d.get("file_path")
    # 库里 archived_path 两种写法并存（老数据是 data/archive/xxx 这样的相对路径），
    # 相对路径必须按仓库根解析——否则服务换个工作目录启动就取不到原件。
    if path and not os.path.isabs(path):
        path = os.path.join(BASE, path)
    if not path or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail=f"原件不在磁盘上：{path or '（未记录路径）'}")
    # 只允许发送本仓库目录下的文件，避免被构造路径读到系统文件
    real = os.path.realpath(path)
    if not real.startswith(os.path.realpath(BASE) + os.sep):
        raise HTTPException(status_code=403, detail="原件路径超出允许范围，已拒绝")

    name = d.get("file_name") or os.path.basename(real)
    disposition = "inline" if inline else "attachment"
    return FileResponse(real, filename=name,
                        content_disposition_type=disposition,
                        media_type=d.get("mime") or _mime_of(name))


# ---------------------------------------------------------------- 来源文件与打包下载

# 浏览器内预览要拿到正确的 Content-Type，否则 PDF 会被当二进制下载。
# 映射表在解析层（`parse.MIME_BY_EXT`），与入库落库的 mime 是同一份，避免两套口径。
_MIME_BY_EXT = parse_mod.MIME_BY_EXT
_mime_of = parse_mod.mime_of
MAX_BUNDLE_FILES = 200
MAX_BUNDLE_BYTES = 500 * 1024 * 1024     # 打包上限 500MB，避免一次拉爆内存

def _source_file_path(name: str) -> str:
    """把界面上的"来源文件名"解析成磁盘绝对路径，并做三重防护。

    1. 只认**纯文件名**（任何路径分隔符、`..`、隐藏文件一律拒绝）——不做路径穿越；
    2. 只认白名单后缀（与导入解析支持的类型一致）；
    3. 解析后的真实路径必须就在配置的来源目录里（防符号链接跳出去）。
    """
    raw = (name or "").strip()
    base = os.path.basename(raw)
    if not raw or base != raw or base.startswith(".") or "/" in raw or "\\" in raw:
        raise HTTPException(status_code=400, detail="文件名不合法")
    if not base.lower().endswith(tuple(parse_mod.SUPPORTED)):
        raise HTTPException(status_code=400, detail=f"不支持预览/下载该类型：{base}")
    cfg = mb.load_config()
    folder = os.path.realpath(mb.resolve_dir(cfg.get("folder_dir") or RESUME_DIR))
    real = os.path.realpath(os.path.join(folder, base))
    if os.path.dirname(real) != folder:
        raise HTTPException(status_code=403, detail="文件不在允许的来源目录内，已拒绝")
    if not os.path.isfile(real):
        raise HTTPException(status_code=404, detail=f"文件不存在：{base}")
    return real


def _unique_name(name: str, used: set[str]) -> str:
    """打包时重名文件自动加序号，避免 zip 里互相覆盖（静默丢件）。"""
    if name not in used:
        used.add(name)
        return name
    stem, ext = os.path.splitext(name)
    i = 2
    while f"{stem}({i}){ext}" in used:
        i += 1
    out = f"{stem}({i}){ext}"
    used.add(out)
    return out


def _zip_response(pairs: list[tuple[str, str]], zip_name: str) -> Response:
    """把若干磁盘文件打成 zip 一次性下发。`pairs` 是 (磁盘路径, 包内文件名)。"""
    if not pairs:
        raise HTTPException(status_code=400, detail="没有选择任何文件")
    if len(pairs) > MAX_BUNDLE_FILES:
        raise HTTPException(status_code=400,
                            detail=f"一次最多打包 {MAX_BUNDLE_FILES} 份，当前 {len(pairs)} 份")
    buf = io.BytesIO()
    used: set[str] = set()
    total = 0
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path, name in pairs:
            if not os.path.isfile(path):
                continue
            total += os.path.getsize(path)
            if total > MAX_BUNDLE_BYTES:
                raise HTTPException(status_code=400,
                                    detail="所选文件合计超过 500MB，请分批下载")
            z.write(path, _unique_name(os.path.basename(name), used))
    data = buf.getvalue()
    if not data:
        raise HTTPException(status_code=404, detail="所选文件都已不在磁盘上")
    # 中文文件名走 RFC 5987：给一个 ASCII 兜底名 + filename* 的真名
    ascii_name = "".join(ch if ch.isascii() and (ch.isalnum() or ch in "._-")
                         else "_" for ch in zip_name) or "bundle.zip"
    if not ascii_name.lower().endswith(".zip"):
        ascii_name += ".zip"
    return Response(content=data, media_type="application/zip", headers={
        "Content-Disposition":
            f'attachment; filename="{ascii_name}"; '
            f"filename*=UTF-8''{urllib.parse.quote(zip_name)}",
    })


@app.get("/api/sources/file")
def api_source_file(name: str, inline: int = 0, token: str | None = None,
                    x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                    x_tp_role: str | None = Header(default=None, alias="X-TP-Role")):
    """预览/下载**来源文件夹里**的简历（含尚未入库的那些）。

    入库后的原件走 `/api/documents/{did}/file`；这里是导入**之前**也要能看内容，
    否则 HR 只能凭文件名猜这份简历该不该导。
    """
    s = _file_session(x_tp_token, token)
    require(s, "read")
    real = _source_file_path(name)
    base = os.path.basename(real)
    conn = db.connect(DB_PATH)
    try:
        db.add_audit(conn, "source_file", base, "preview" if inline else "download",
                     "", base, s["username"], s["role"])
    finally:
        conn.close()
    return FileResponse(real, filename=base,
                        content_disposition_type="inline" if inline else "attachment",
                        media_type=_mime_of(base))


class BundleReq(BaseModel):
    """批量打包下载的选择。

    两类来源都要支持，因为「导入与来源」同一张表里既有"已入库"也有"尚未入库"的行：
    - `names`：来源文件夹里的文件名（未入库的也能看能下）
    - `document_ids`：库内已归档的附件 id（归档目录可能已不在来源文件夹里）
    用请求体而不是逗号拼接的查询串：简历文件名里出现逗号是常事，拼接会切错。
    """
    names: list[str] | None = None
    document_ids: list[int] | None = None


@app.post("/api/sources/bundle")
def api_sources_bundle(req: BundleReq,
                       x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                       x_tp_role: str | None = Header(default=None, alias="X-TP-Role")):
    """把选中的简历打包成 zip 下载（可混选来源文件与已入库原件）。

    单个文件取不到不整体失败：跳过并在响应头 `X-Skipped-Detail` 里如实列出，
    否则一次勾了十份、其中一份原件丢了，整批都下不来，HR 还得逐个试。
    """
    s = _session(x_tp_token, x_tp_role)
    require(s, "read")

    pairs: list[tuple[str, str]] = []
    skipped: list[str] = []

    for nm in (req.names or [])[:MAX_BUNDLE_FILES]:
        try:
            real = _source_file_path(nm)
        except HTTPException as exc:
            skipped.append(f"{nm}（{exc.detail}）")
            continue
        pairs.append((real, os.path.basename(real)))

    ids = [int(i) for i in (req.document_ids or [])][:MAX_BUNDLE_FILES]
    if ids:
        conn = db.connect(DB_PATH)
        try:
            for did in ids:
                row = conn.execute("SELECT * FROM documents WHERE id = ?", (did,)).fetchone()
                if not row:
                    skipped.append(f"附件#{did}（不存在）")
                    continue
                d = dict(row)
                path = d.get("archived_path") or d.get("file_path") or ""
                if path and not os.path.isabs(path):
                    path = os.path.join(BASE, path)
                if not path or not os.path.isfile(path):
                    skipped.append(f"{d.get('file_name') or did}（原件不在磁盘上）")
                    continue
                real = os.path.realpath(path)
                if not real.startswith(os.path.realpath(BASE) + os.sep):
                    skipped.append(f"{d.get('file_name') or did}（路径越界，已拒绝）")
                    continue
                pairs.append((real, d.get("file_name") or os.path.basename(real)))
        finally:
            conn.close()

    # 区分两种"没有可打包的文件"：
    # ① 一份都没勾 → 客户端请求有问题（400），提示要能直接看懂；
    # ② 勾了但都取不到 → 目标不存在（404），并把每一份的原因说清楚。
    # 之前一律返回 404，空选择时的提示是"选中的文件都取不到："后面空着，等于没说。
    if not pairs:
        if not (req.names or req.document_ids):
            raise HTTPException(status_code=400, detail="没有选择任何文件")
        raise HTTPException(status_code=404,
                            detail="选中的文件都取不到：" + "；".join(skipped[:5]))

    conn = db.connect(DB_PATH)
    try:
        db.add_audit(conn, "bundle", f"{len(pairs)} 份", "bundle_download", "",
                     "、".join(n for _p, n in pairs)[:400], s["username"], s["role"])
    finally:
        conn.close()

    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    resp = _zip_response(pairs, f"简历原件打包_{stamp}.zip")
    resp.headers["X-Batch-Count"] = str(len(pairs))
    if skipped:
        resp.headers["X-Skipped"] = str(len(skipped))
        # HTTP 头只允许 latin-1 字符，中文直接塞进去会抛 UnicodeEncodeError——
        # 而这条分支恰恰是"有一份取不到时"要走的路，等于上报跳过的动作本身
        # 把整个下载炸成 500。所以先百分号编码成纯 ASCII，前端解码还原。
        resp.headers["X-Skipped-Detail"] = urllib.parse.quote(
            json.dumps(skipped[:10], ensure_ascii=False))
    return resp


# ---------------------------------------------------------------- 来源文件：移入回收目录 / 恢复
#
# 界面上的"删除"是**移入回收目录**（`data/removed/YYYY-MM/`），不是 `unlink`：
# 简历不出内网、不物理删除是设计红线，误删一次就得重新跟候选人要材料。
#
# 这里还有一道**必须先做**的动作：老库（v0.1 迁移）里有台账的
# `archived_path` 直接指向来源文件夹（`data/resumes/xxx`），而不是归档区副本。
# 一旦把来源文件移走，这些人的"原件"当场 404。
# 所以删除前先给被引用的文件**补一份归档**（原件区只增不改）并把台账改指过去，
# 再移入回收目录——顺序反了就会短暂丢件。

def _removed_month_dir() -> str:
    return os.path.join(REMOVED_DIR, datetime.now().strftime("%Y-%m"))


def _free_path(folder: str, name: str) -> str:
    """回收目录里避免同名互相覆盖（后者会静默盖掉前者，等于丢件）。"""
    stem, ext = os.path.splitext(name)
    path = os.path.join(folder, name)
    i = 2
    while os.path.exists(path):
        path = os.path.join(folder, f"{stem}({i}){ext}")
        i += 1
    return path


def _docs_referencing(conn, real: str) -> list[dict]:
    """找出"原件就指向这个文件"的台账记录（兼容相对/绝对两种历史写法）。"""
    out: list[dict] = []
    for r in conn.execute(
            "SELECT id, file_name, file_path, archived_path FROM documents").fetchall():
        for key in ("file_path", "archived_path"):
            raw = (dict(r).get(key) or "").strip()
            if not raw:
                continue
            full = raw if os.path.isabs(raw) else os.path.join(BASE, raw)
            if os.path.realpath(full) == real:
                out.append(dict(r))
                break
    return out


def _pin_archive(conn, real: str, operator: str, role: str) -> list[int]:
    """把指向来源目录的原件**固化**到归档区，并把台账改指过去。返回改动的附件 id。

    幂等：归档区已有同内容副本时不会重复写（`archive_file` 按内容哈希命名）。
    """
    docs = _docs_referencing(conn, real)
    if not docs:
        return []
    cfg = mb.load_config()
    with open(real, "rb") as fh:
        payload = fh.read()
    archived, _size = ingest_mod.archive_file(cfg, docs[0].get("file_name") or
                                              os.path.basename(real), payload)
    for d in docs:
        db.update_document(conn, d["id"], archived_path=archived)
        db.add_audit(conn, "document", str(d["id"]), "archive_pinned",
                     str(d.get("archived_path") or ""), archived, operator, role)
    return [d["id"] for d in docs]


class SourceNamesReq(BaseModel):
    names: list[str] | None = None


@app.post("/api/sources/remove")
def api_sources_remove(req: SourceNamesReq,
                       x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                       x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """把来源文件夹里的简历移入回收目录（可恢复）。**不做物理删除。**"""
    s = _session(x_tp_token, x_tp_role)
    require(s, "ingest")
    names = [n for n in (req.names or []) if (n or "").strip()]
    if not names:
        raise HTTPException(status_code=400, detail="没有选择任何文件")
    if len(names) > MAX_BUNDLE_FILES:
        raise HTTPException(status_code=400, detail=f"一次最多处理 {MAX_BUNDLE_FILES} 份")

    folder = _removed_month_dir()
    os.makedirs(folder, exist_ok=True)
    moved: list[dict] = []
    skipped: list[dict] = []

    for nm in names:
        try:
            real = _source_file_path(nm)
        except HTTPException as exc:
            skipped.append({"name": nm, "why": str(exc.detail)})
            continue
        base = os.path.basename(real)
        conn = db.connect(DB_PATH)
        try:
            pinned = _pin_archive(conn, real, s["username"], s["role"])
        except OSError as exc:
            skipped.append({"name": base, "why": f"补归档失败，未移动：{exc}"})
            continue
        finally:
            conn.close()
        dest = _free_path(folder, base)
        try:
            shutil.move(real, dest)
        except OSError as exc:
            # 移动失败必须回话说清楚：补归档那一步已经改了台账，但文件还在原处，
            # 两边都还在、不影响使用，只是这次删除没成功。
            skipped.append({"name": base, "why": f"移入回收目录失败：{exc}"})
            continue
        rel = os.path.relpath(dest, BASE)
        conn = db.connect(DB_PATH)
        try:
            db.add_audit(conn, "source_file", base, "remove", base,
                         f"移入回收目录 {rel}"
                         + (f"（原指向来源目录的 {len(pinned)} 条台账已先补归档）" if pinned else ""),
                         s["username"], s["role"])
        finally:
            conn.close()
        moved.append({"name": base, "removed_to": rel, "pinned_documents": pinned})

    if moved:
        # 台账口径要跟着变：这些文件已不在来源目录，清单不该还列着它们
        _remember_remove(moved, s.get("username") or "hr")
    return {"moved": moved, "skipped": skipped,
            "recycle_dir": os.path.relpath(folder, BASE),
            "note": ("已移入回收目录，可随时恢复。原件区（data/archive）只增不改，"
                     "已入库的附件下载不受影响。"
                     if moved else "没有任何文件被移动。")}


def _remember_remove(moved: list[dict], operator: str) -> None:
    conn = db.connect(DB_PATH)
    try:
        db.set_setting(conn, "last_remove", {"at": db.now(), "operator": operator,
                                             "moved": moved[:60]})
    finally:
        conn.close()


@app.get("/api/sources/removed")
def api_sources_removed(x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                        x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """回收目录清单：被"删除"的简历还在，只是挪了个地方。"""
    s = _session(x_tp_token, x_tp_role)
    require(s, "read")
    cfg = mb.load_config()
    src = os.path.realpath(mb.resolve_dir(cfg.get("folder_dir") or RESUME_DIR))
    items: list[dict] = []
    if os.path.isdir(REMOVED_DIR):
        for month in sorted(os.listdir(REMOVED_DIR), reverse=True):
            mdir = os.path.join(REMOVED_DIR, month)
            if not os.path.isdir(mdir) or month.startswith("."):
                continue
            for name in sorted(os.listdir(mdir)):
                full = os.path.join(mdir, name)
                if not os.path.isfile(full) or name.startswith("."):
                    continue
                st = os.stat(full)
                items.append({
                    "name": name, "size": st.st_size,
                    "removed_at": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                    "month": month,
                    "conflict": os.path.exists(os.path.join(src, name)),
                })
    conn = db.connect(DB_PATH)
    try:
        last = db.get_setting(conn, "last_remove")
    finally:
        conn.close()
    return {"count": len(items), "items": items, "recycle_dir": os.path.relpath(REMOVED_DIR, BASE),
            "last_remove": last,
            "note": "这里是「删除」的来源简历。恢复到来源文件夹后即可重新导入；"
                    "若来源文件夹已有同名文件，需先处理同名冲突（系统不覆盖）。"}


@app.post("/api/sources/restore")
def api_sources_restore(req: SourceNamesReq,
                        x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                        x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """把回收目录里的简历**恢复到来源文件夹**。同名冲突时拒绝，绝不覆盖。"""
    s = _session(x_tp_token, x_tp_role)
    require(s, "ingest")
    names = [n for n in (req.names or []) if (n or "").strip()]
    if not names:
        raise HTTPException(status_code=400, detail="没有选择任何文件")
    cfg = mb.load_config()
    dest_dir = os.path.realpath(mb.resolve_dir(cfg.get("folder_dir") or RESUME_DIR))

    restored: list[dict] = []
    skipped: list[dict] = []
    for nm in names:
        base = os.path.basename((nm or "").strip())
        if base != (nm or "").strip() or base.startswith(".") or not base:
            skipped.append({"name": nm, "why": "文件名不合法"})
            continue
        found = None
        if os.path.isdir(REMOVED_DIR):
            for month in sorted(os.listdir(REMOVED_DIR), reverse=True):
                cand = os.path.join(REMOVED_DIR, month, base)
                if os.path.isfile(cand):
                    found = cand
                    break
        if not found:
            skipped.append({"name": base, "why": "回收目录里没有这份文件"})
            continue
        target = os.path.join(dest_dir, base)
        if os.path.exists(target):
            skipped.append({"name": base, "why": "来源文件夹已有同名文件，未覆盖"})
            continue
        try:
            os.makedirs(dest_dir, exist_ok=True)
            shutil.move(found, target)
        except OSError as exc:
            skipped.append({"name": base, "why": f"恢复失败：{exc}"})
            continue
        conn = db.connect(DB_PATH)
        try:
            db.add_audit(conn, "source_file", base, "restore", "", f"恢复到 {dest_dir}",
                         s["username"], s["role"])
        finally:
            conn.close()
        restored.append({"name": base, "restored_to": os.path.join(dest_dir, base)})
    return {"restored": restored, "skipped": skipped,
            "note": "已恢复到来源文件夹；如需重新入库，到「导入与来源」点一次导入即可。"}


@app.get("/api/emails")
def api_emails(limit: int = 100, x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
               x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    s = _session(x_tp_token, x_tp_role)
    require(s, "read")
    conn = db.connect(DB_PATH)
    try:
        items = db.list_emails(conn, limit=limit)
        return {"count": len(items), "items": items,
                "dedup_layers": [
                    {"layer": "邮件层", "key": "message_id", "solves": "同一封邮件被重复拉取"},
                    {"layer": "文件层", "key": "SHA256(附件)", "solves": "同一份简历不同文件名"},
                    {"layer": "内容层", "key": "identity_key", "solves": "同一个人不同版本简历"},
                ]}
    finally:
        conn.close()


# ============================================================
# 检索
# ============================================================

ENC_KEYS = ("phone_enc", "email_enc", "phone_bidx", "email_bidx", "identity_key")


def _with_contact(conn, items: list[dict] | None) -> list[dict]:
    """给检索结果补上明文联系方式，同时确保密文/盲索引/身份键一个都不下发。

    检索层只回业务字段（分数/技能/证据），联系方式是加密列，所以这里按
    candidate_id 取一次档案再解密——和人才库列表走同一个展示口径，
    避免"人才库能看到电话、检索页看不到"这种前后不一致。
    结果条数受 top_k 限制（默认 ≤10），多出的这一次查询可忽略。
    """
    out: list[dict] = []
    for h in items or []:
        item = {k: v for k, v in h.items() if k not in ENC_KEYS}
        c = db.get_candidate(conn, h.get("candidate_id")) or {}
        p = auth.present_candidate(c)
        item["phone"] = p.get("phone")
        item["email"] = p.get("email")
        item["contact_full_visible"] = True
        out.append(item)
    return out


@app.get("/api/search/skills")
def api_search_skills(skills: str, mode: str = "all", tier: str | None = None,
                      x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                      x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    s = _session(x_tp_token, x_tp_role)
    require(s, "read")
    words = [w.strip() for w in skills.replace("、", ",").split(",") if w.strip()]
    conn = db.connect(DB_PATH)
    try:
        r = search.by_skills(conn, words, mode=mode, include_tier=tier)
        r["results"] = _with_contact(conn, r.get("results"))
        return r
    finally:
        conn.close()


@app.get("/api/search/semantic")
def api_search_semantic(q: str, top_k: int = 8,
                        x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                        x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    s = _session(x_tp_token, x_tp_role)
    require(s, "read")
    conn = db.connect(DB_PATH)
    try:
        r = search.semantic(conn, q, top_k=top_k)
        r["results"] = _with_contact(conn, r.get("results"))
        return r
    finally:
        conn.close()


@app.get("/api/search/similar")
def api_search_similar(candidate_id: int, top_k: int = 5,
                       x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                       x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    s = _session(x_tp_token, x_tp_role)
    require(s, "read")
    conn = db.connect(DB_PATH)
    try:
        r = search.similar_to(conn, candidate_id, top_k=top_k)
        r["results"] = _with_contact(conn, r.get("results"))
        return r
    finally:
        conn.close()


class IndexReq(BaseModel):
    force: bool = False


@app.post("/api/search/index")
def api_index(req: IndexReq | None = Body(default=None),
              x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
              x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    s = _session(x_tp_token, x_tp_role)
    require(s, "ingest")
    return search.build_index(DB_PATH, force=bool((req or IndexReq()).force))


@app.get("/api/search/status")
def api_search_status(x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                      x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    _session(x_tp_token, x_tp_role)
    return search.status(DB_PATH)


# ============================================================
# 智能体
# ============================================================

class ChatReq(BaseModel):
    message: str
    history: list[dict] | None = None


class FocusReq(BaseModel):
    focus: str | None = ""


@app.post("/api/agent/chat")
def api_agent_chat(req: ChatReq, x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                   x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    s = _session(x_tp_token, x_tp_role)
    require(s, "chat")
    conn = db.connect(DB_PATH)
    try:
        ctx = _ctx(s, conn)
    finally:
        conn.close()
    out = run_agent(req.message, ctx, req.history)
    out["session"] = {"role": s["role"], "mode": s.get("mode")}
    return out


@app.get("/api/agent/tools")
def api_agent_tools(x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                    x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    _session(x_tp_token, x_tp_role)
    return tool_catalog()


@app.get("/api/agent/history")
def api_agent_history(limit: int = 300,
                      x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                      x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """对话历史（v1.6）：刷新页面、换设备、重启服务后仍能看到。

    内容来自 `agent_runs`（本来就已经落库），不是新存的一份——过去看不到只是因为
    前端把对话放在内存数组里，一刷新就空。**没点清空就不该丢**是这里的口径。
    """
    s = _session(x_tp_token, x_tp_role)
    require(s, "read")
    conn = db.connect(DB_PATH)
    try:
        return db.chat_history(conn, limit=limit)
    finally:
        conn.close()


@app.post("/api/agent/clear")
def api_agent_clear(x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                    x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """清空对话展示 = **软清空**（v1.6）：推进清空点 + 开出保留期，到期自动清理。

    用户口径：「删除三十天之后自动清理，30 天之内可以恢复」。
    所以清空**不是**立刻物理删除：那批运行记录还留着，`PURGE_AFTER_DAYS` 天内
    可以随时 `POST /api/agent/restore` 恢复；期满由后台任务
    （启动一次 + 每 24 小时）执行 `db.purge_due_chats()` 真正清掉。

    这样安排的理由：`agent_runs` 同时承担 token 成本核算与治理审计（这一问调了哪些工具、
    是否走到降级、有没有越权尝试），立刻删掉等于把"花了多少、系统做了什么"一起抹了。
    给一个缓冲期，既满足"清空"的诉求，又不至于一点都救不回来。
    与档案"归档满 30 天才彻底删除"、来源文件"移入回收目录"完全同一套规则。
    """
    s = _session(x_tp_token, x_tp_role)
    require(s, "chat")
    conn = db.connect(DB_PATH)
    try:
        out = db.clear_chat(conn, operator=s.get("username", "HR"), role=s["role"])
        out["note"] = (f"对话已清空。{out['retention_days']} 天内可点「恢复对话」找回；"
                       f"将于 {out['purge_after']} 自动清理底层运行记录"
                       f"（清空/恢复/清理三条痕迹永久保留在「提案与审计」中）。")
        return out
    finally:
        conn.close()


@app.post("/api/agent/restore")
def api_agent_restore(x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                      x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """撤销清空：被清空的对话重新回到界面上（保留期内有效）。

    `restorable` 只看"清空点还在不在"，不卡到期时间：后台任务每 24 小时才跑一次，
    到期后到真正被执行之间还有一段窗口，这段时间里记录仍在库里，人点恢复就该能捞回来
    ——"系统还没删的，人能捞回"。真的删掉之后清空点被复位，恢复会返回 `ok=False`。
    """
    s = _session(x_tp_token, x_tp_role)
    require(s, "chat")
    conn = db.connect(DB_PATH)
    try:
        out = db.restore_chat(conn, operator=s.get("username", "HR"), role=s["role"])
        if out.get("ok"):
            out["note"] = (f"已恢复 {out['restored']} 条对话展示。"
                           f"注意：恢复后不再有到期时间，这批记录会一直保留到下次清空。")
        else:
            out["note"] = "没有可恢复的对话：要么从未清空过，要么已到期被系统清理。"
        return out
    finally:
        conn.close()


@app.post("/api/agent/purge-due")
def api_agent_purge_due(x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                        x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """手动触发一次"对话到期彻底清理"（正常由服务启动与每日定时任务自动执行）。

    与 `/api/archive/purge-due` 对称：运维想立刻收敛而不想重启服务时用这个。
    """
    s = _session(x_tp_token, x_tp_role)
    require(s, "set_stage")
    out = _purge_chats(actor=s["username"], role=s["role"])
    if out.get("purged"):
        out["note"] = (f"已删除 {out['purged']} 条清空满 {out['days']} 天的对话运行记录"
                       f"（合计 tokens {out.get('tokens', 0)}，汇总已写入审计）")
    else:
        out["note"] = f"没有到期可清理的对话：{out.get('reason') or '无清空记录'}"
    return out


@app.get("/api/agent/runs")
def api_agent_runs(limit: int = 30, x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                   x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    s = _session(x_tp_token, x_tp_role)
    require(s, "read")
    conn = db.connect(DB_PATH)
    try:
        return {"count": db.agent_cost_summary(conn)["runs"],
                "cost": db.agent_cost_summary(conn),
                "runs": db.list_agent_runs(conn, limit=limit)}
    finally:
        conn.close()


@app.get("/api/agent/status")
def api_agent_status(x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                     x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    _session(x_tp_token, x_tp_role)
    return {"chat": llm.status(), "embedding": search.status(DB_PATH)}


@app.post("/api/candidates/{cid}/analyze")
def api_analyze(cid: int, x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    s = _session(x_tp_token, x_tp_role)
    require(s, "chat")
    conn = db.connect(DB_PATH)
    try:
        d = db.candidate_detail(conn, cid)
        # v1.6：按候选人「对应岗位」分析（已归岗 > 建议岗位），**不再用材料类默认尺子**
        jd, job_meta = db.resolve_candidate_job(conn, d or {})
    finally:
        conn.close()
    if not d:
        raise HTTPException(status_code=404, detail="未找到该候选人")
    if not jd:
        return {"error": "该候选人尚无对应岗位", "job": job_meta,
                "hint": "先在人才库卡片上归岗、或采纳系统建议岗位，再生成针对该岗位的分析"}
    r = analyze_fit(d, jd)
    if r is None:
        return {"error": llm.status().get("error") or "模型不可用", "job": job_meta,
                "hint": "可改用『档位解释』（纯规则，无需模型）"}
    r["job"] = job_meta
    return r


@app.post("/api/candidates/{cid}/interview")
def api_interview(cid: int, req: FocusReq | None = Body(default=None),
                  x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                  x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    s = _session(x_tp_token, x_tp_role)
    require(s, "chat")
    conn = db.connect(DB_PATH)
    try:
        d = db.candidate_detail(conn, cid)
        # v1.6：面试题纲也必须锚定"这个人对应的岗位"，否则会给软件工程师出材料类面试题
        jd, job_meta = db.resolve_candidate_job(conn, d or {})
    finally:
        conn.close()
    if not d:
        raise HTTPException(status_code=404, detail="未找到该候选人")
    if not jd:
        return {"error": "该候选人尚无对应岗位", "job": job_meta,
                "hint": "面试提纲按岗位定制，请先归岗（或采纳建议岗位）后再生成"}
    r = draft_interview(d, jd, (req.focus if req else "") or "")
    if r is None:
        return {"error": llm.status().get("error") or "模型不可用", "job": job_meta}
    r["job"] = job_meta
    return r


@app.post("/api/candidates/{cid}/explain")
def api_explain(cid: int, x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """纯规则档位解释：不依赖模型，随时可用。"""
    s = _session(x_tp_token, x_tp_role)
    require(s, "read")
    conn = db.connect(DB_PATH)
    try:
        ctx = _ctx(s, conn)
    finally:
        conn.close()
    from .agent.tools import execute as tool_execute

    return json.loads(tool_execute("explain_grade", {"candidate_id": cid}, ctx))


# ============================================================
# 提案与审计
# ============================================================

class DecideReq(BaseModel):
    decision: str = "approve"    # approve | reject


@app.get("/api/proposals")
def api_proposals(status: str | None = None, x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                  x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    s = _session(x_tp_token, x_tp_role)
    require(s, "read")
    conn = db.connect(DB_PATH)
    try:
        items = db.list_proposals(conn, status=status, limit=100)
        return {"count": len(items), "items": items,
                "can_confirm": auth.can(s["role"], "confirm"),
                "note": "提案由智能体生成，必须由 HR 确认才会落到人才库"}
    finally:
        conn.close()


@app.post("/api/proposals/{pid}/decide")
def api_decide(pid: int, req: DecideReq, x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
               x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    s = _session(x_tp_token, x_tp_role)
    require(s, "confirm")
    r = actions.apply_proposal(DB_PATH, pid, req.decision, s["username"], s["role"])
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r.get("error"))
    return r


@app.get("/api/audit")
def api_audit(entity: str | None = None, entity_id: str | None = None, limit: int = 200,
              x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
              x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    s = _session(x_tp_token, x_tp_role)
    require(s, "read")
    conn = db.connect(DB_PATH)
    try:
        items = db.list_audit(conn, entity=entity, entity_id=entity_id, limit=limit)
        return {"count": len(items), "items": items}
    finally:
        conn.close()


# ============================================================
# 系统信息
# ============================================================

@app.get("/api/ontology")
def api_ontology(q: str | None = None, x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                 x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    _session(x_tp_token, x_tp_role)
    onto = nz.ontology()
    skills = onto.get("skills", [])
    if q:
        kw = q.strip().lower()
        skills = [s for s in skills
                  if kw in s["canonical"].lower()
                  or any(kw in a.lower() for a in (s.get("aliases") or []))]
    return {"describe": nz.describe(), "skills": skills}


@app.get("/api/policy")
def api_policy(x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
               x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    _session(x_tp_token, x_tp_role)
    conn = db.connect(DB_PATH)
    try:
        return {"access": auth.policy_report(conn), "sensitive_scrub": sanitize.policy(),
                "red_lines": [
                    "不自动淘汰、不自动发拒信、不对外发送任何内容",
                    "不做录用决定，只给分级建议与证据",
                    "简历与附件不出内网",
                    "不物理删除简历（保留期到期或候选人要求除外）",
                    "对话记录「清空」是软清空：30 天内可恢复，到期才自动清理底层运行记录"
                    "（与档案保留期同一套规则，清空/恢复/清理三条痕迹永久保留）",
                    "不抽取民族/婚育/宗教/健康等敏感属性",
                    "模型不得无证据生成技能（无原文片段不计命中）",
                    "「专业需求」是独立维度：**专业不对口只提示、不参与档位淘汰**"
                    "（材料物理去做工艺是常态）；专业判不出时如实标「未识别」，不等于不满足",
                ]}
    finally:
        conn.close()


# ============================================================
# 学科目录与领域包（v1.7：让"新增一个行业的岗位"不用改代码）
# ============================================================

@app.get("/api/majors")
def api_majors(q: str | None = None, limit: int = 40,
               x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
               x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """通用学科目录（学科门类 → 一级学科）。岗位的「专业需求」从这里选，不要自己造词。"""
    s = _session(x_tp_token, x_tp_role)
    require(s, "read")
    return {"info": mj.describe(), "items": mj.search(q, limit=limit),
            "note": "专业需求写学科名即可（如「材料科学与工程」「会计学」），"
                    "别名也认（「材料学」「会计」）。判不出的写法会在报告里标「未识别」，"
                    "**未识别不等于不满足**。"}


@app.get("/api/domains")
def api_domains(x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """可用领域包列表。领域包是"新增行业的技能词表"，导入即生效，不用改代码。"""
    s = _session(x_tp_token, x_tp_role)
    require(s, "read")
    return {"count": len(domains_mod.list_packs()), "items": domains_mod.list_packs(),
            "dir": domains_mod.pack_dir(),
            "note": "领域包一个 JSON 声明某行业常用技能（含别名与归类），导入后本体内即多出这批词；"
                    "导入会先备份本体到 data/backup/，并逐条报告别名迁移。"}


class DomainImportReq(BaseModel):
    pack: str
    dry_run: bool = True          # 默认预演：导入会改本体文件，先让人看清楚改什么
    apply: bool | None = None     # 兼容用：apply=True 等价 dry_run=False


@app.post("/api/domains/import")
def api_domains_import(req: DomainImportReq,
                       x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                       x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """导入领域包。**默认只预演**（`dry_run=True`），要落地必须显式 `dry_run=false`。

    理由与「重新分析」一致：一次误点就会改写全院共用的技能本体，
    而本体是方向判定的底座——这类"全库口径级"的动作一律先给差异、再落盘。
    """
    s = _session(x_tp_token, x_tp_role)
    require(s, "settings")
    run = req.dry_run if req.apply is None else (not req.apply)
    conn = db.connect(DB_PATH)
    try:
        if run:
            r = domains_mod.preview_import(req.pack)
            return {"ok": True, "dry_run": True, "result": r,
                    "note": "这是预演，没有改动任何文件。确认无误后用 dry_run=false 落地。"}
        r = domains_mod.import_pack(conn, req.pack, operator=s["username"])
        return {"ok": True, "dry_run": False, "result": r,
                "note": "已导入并刷新库内技能分类。已有投递要按新口径重算，"
                        "请在对应岗位上点「重新分析」（会先给差异预览）。"}
    finally:
        conn.close()


# ============================================================
# 设置（性别筛选开关等；开关类设置一律"默认关 + 变更留痕"）
# ============================================================

class SettingsReq(BaseModel):
    gender_filter_enabled: bool | None = None


@app.get("/api/settings")
def api_settings_get(x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                     x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    s = _session(x_tp_token, x_tp_role)
    require(s, "read")
    conn = db.connect(DB_PATH)
    try:
        cur = _settings(conn)
        return {**cur,
                "gender_groups": list(db.GENDER_GROUPS),
                "note": "性别筛选默认关闭。它只影响「列出谁」，不影响任何人被评成什么档——"
                        "性别永不进入评分。",
                "policy": "招聘不得限定性别（《就业促进法》第 27 条、《妇女权益保障法》第 43 条）。"
                          "开启动作会写入审计。"}
    finally:
        conn.close()


@app.post("/api/settings")
def api_settings_set(req: SettingsReq,
                     x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                     x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """改设置。开关从「关」变「开」时写一条审计——这是合规要看的凭证。"""
    s = _session(x_tp_token, x_tp_role)
    require(s, "settings")
    conn = db.connect(DB_PATH)
    try:
        before = _settings(conn)
        if req.gender_filter_enabled is not None:
            db.set_setting(conn, GENDER_FILTER_KEY, bool(req.gender_filter_enabled))
        after = _settings(conn)
        on_before = bool(before.get("gender_filter_enabled"))
        on_after = bool(after.get("gender_filter_enabled"))
        if before != after:
            db.add_audit(conn, "settings", GENDER_FILTER_KEY, "update",
                         f"gender_filter_enabled={on_before}",
                         f"gender_filter_enabled={on_after}",
                         s["username"], s["role"])
        if on_after and not on_before:
            note = "性别筛选已开启（已写入审计）。它只改变列表的筛选条件，不参与任何评分或分级。"
        elif on_before and not on_after:
            note = "性别筛选已关闭，列表恢复为全部候选人。"
        else:
            note = "设置未变化。"
        return {"ok": True, **after, "changed": before != after, "note": note}
    finally:
        conn.close()


# ============================================================
# 部门 / 岗位管理（单 HR，停用而非删除）
# ============================================================

class DeptReq(BaseModel):
    name: str
    description: str = ""


class JobReq(BaseModel):
    title: str
    department_id: int | None = None      # 界面用的字段名
    dept_id: int | None = None            # 兼容直接用库列名的调用方（同一个含义）
    # 岗位 JD（可选）：填了就按填的算，没填才继承 config/jd.json 的默认尺子
    must_skills: list[str] | None = None        # 必需技能
    preferred_skills: list[str] | None = None   # 加分技能
    education_min: str | None = None            # 最低学历：大专/本科/硕士/博士
    years_min: int | None = None                # 最低年限
    # 专业需求（v1.7 新增，一等维度）：写学科名，如「材料科学与工程」「会计学」
    major_required: list[str] | None = None
    note: str | None = None                     # 岗位职责 / 说明（自由文本）


class JobJdReq(BaseModel):
    must_skills: list[str] | None = None
    preferred_skills: list[str] | None = None
    education_min: str | None = None
    years_min: int | None = None
    major_required: list[str] | None = None
    note: str | None = None


def _jd_from_req(base: dict, req, title: str, dept_name: str) -> dict:
    """把表单字段拼成 JD 尺子；未填的字段沿用 base（默认尺子），不做静默清空。"""
    jd = dict(base)
    jd["role"] = title
    # 部门已不再是岗位的必填归属：不传就**不继承**默认尺子里的那个部门名。
    # 为什么特意写一句：base 是 config/jd.json，里面带着一个写死的"研发中心 · 材料工艺所"，
    # 若照旧写成 `dept_name or base.get(...)`，新建的无部门岗位会**静默挂上一个它并不属于的部门**。
    # 宁可留空，也不要显示一个错的。
    if dept_name:
        jd["department"] = dept_name
    else:
        jd.pop("department", None)
    jd["origin"] = "界面录入"
    must = dict(jd.get("must") or {})
    pref = dict(jd.get("preferred") or {})
    if req.must_skills is not None:
        must["skills_required"] = [x.strip() for x in req.must_skills if x and x.strip()]
    if req.education_min is not None:
        must["education_min"] = req.education_min.strip()
    if req.years_min is not None:
        must["years_min"] = int(req.years_min)
    if getattr(req, "major_required", None) is not None:
        # 只保留一个"专业需求"字段：专业本来就不参与硬性淘汰（材料物理做工艺是常态），
        # 再拆"必需/加分"是虚假精度，还会让 HR 多填一遍同样的清单。
        must["major_required"] = [x.strip() for x in req.major_required if x and x.strip()]
    if req.preferred_skills is not None:
        pref["skills"] = [x.strip() for x in req.preferred_skills if x and x.strip()]
    jd["must"] = must
    jd["preferred"] = pref
    if req.note is not None:
        jd["note"] = req.note.strip()
    return jd


def _jd_warnings(jd: dict) -> list[str]:
    """JD 录入的"填错格子"提醒。

    实测踩到：HR 会把招聘公告里的**专业需求原文**（材料科学与工程、计算机…）
    直接粘进「必需技能」。这些是学科名不是技能，塞进技能字段有两个后果：
    ① 技能命中永远为 0（候选人技能栏里不会写"材料科学与工程"）；
    ② 它们会让方向的大类统计失真。
    所以这里主动认出来并告诉他"这句话该填到专业需求里"——比事后解释"为什么全员 D 档"便宜得多。
    """
    out: list[str] = []
    must = (jd.get("must") or {})
    for term in (must.get("skills_required") or []):
        if not nz.canonical_of(term) and mj.resolve(term):
            out.append(f"「{term}」看起来是**学科名**（{mj.resolve(term)['canonical']}）而不是技能，"
                       f"建议填到「专业需求」里；填在「必需技能」会让全部候选人都命中不到它。")
    for term in (must.get("major_required") or []):
        if not mj.resolve(term):
            out.append(f"「{term}」不在通用学科目录中，专业维度将无法判定它"
                       f"（**判不出 ≠ 不满足**，会在报告里如实标注）。"
                       f"如果它其实是技能，请改填到「必需技能」。")
    return out


def _split_skills(raw: str | None) -> list[str]:
    """界面用逗号/顿号/分号分隔输入技能，这里统一切开。"""
    if not raw:
        return []
    out: list[str] = []
    for chunk in str(raw).replace("，", ",").replace("、", ",").replace("；", ",").replace(";", ",").split(","):
        s = chunk.strip()
        if s:
            out.append(s)
    return out


@app.get("/api/departments")
def api_departments(x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                    x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    s = _session(x_tp_token, x_tp_role)
    require(s, "read")
    conn = db.connect(DB_PATH)
    try:
        return {"count": len(db.list_departments(conn)),
                "items": db.list_departments(conn, include_inactive=True)}
    finally:
        conn.close()


@app.post("/api/departments")
def api_dept_create(req: DeptReq, x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                    x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    s = _session(x_tp_token, x_tp_role)
    require(s, "settings")
    conn = db.connect(DB_PATH)
    try:
        if not (req.name or "").strip():
            raise HTTPException(status_code=400, detail="部门名称不能为空")
        did = db.upsert_department(conn, req.name, req.description or "", s["username"])
        return {"ok": True, "id": did, "department": db.get_department(conn, did)}
    finally:
        conn.close()


@app.post("/api/departments/{did}/deactivate")
def api_dept_deactivate(did: int, x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                        x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """停用部门（不物理删除）。"""
    s = _session(x_tp_token, x_tp_role)
    require(s, "settings")
    conn = db.connect(DB_PATH)
    try:
        r = db.set_department_active(conn, did, False, s["username"])
        if not r:
            raise HTTPException(status_code=404, detail="未找到该部门")
        return {"ok": True, "department": r, "note": "部门已停用（未物理删除，可随时启用）"}
    finally:
        conn.close()


@app.post("/api/departments/{did}/activate")
def api_dept_activate(did: int, x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                      x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    s = _session(x_tp_token, x_tp_role)
    require(s, "settings")
    conn = db.connect(DB_PATH)
    try:
        r = db.set_department_active(conn, did, True, s["username"])
        if not r:
            raise HTTPException(status_code=404, detail="未找到该部门")
        return {"ok": True, "department": r}
    finally:
        conn.close()


@app.get("/api/jobs")
def api_jobs(x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
             x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    s = _session(x_tp_token, x_tp_role)
    require(s, "read")
    conn = db.connect(DB_PATH)
    try:
        return {"count": len(db.list_jobs(conn)), "items": db.list_jobs(conn, include_inactive=True)}
    finally:
        conn.close()


@app.post("/api/jobs")
def api_job_create(req: JobReq, x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                   x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    s = _session(x_tp_token, x_tp_role)
    require(s, "settings")
    conn = db.connect(DB_PATH)
    try:
        if not (req.title or "").strip():
            raise HTTPException(status_code=400, detail="岗位名称不能为空")
        did = req.department_id or req.dept_id
        dept_name = ""
        if did:
            d = db.get_department(conn, did)
            if not d:
                raise HTTPException(status_code=400, detail="所选部门不存在")
            dept_name = d.get("name") or ""
        # 填了 JD 字段就按填的算；一个都不填则继承 config/jd.json 的默认尺子
        jd = _jd_from_req(_load(JD_PATH), req, req.title.strip(), dept_name)
        jid = db.create_job(conn, req.title, dept_id=did, jd=jd,
                            operator=s["username"])
        return {"ok": True, "id": jid, "job": db.get_job(conn, jid),
                "warnings": _jd_warnings(jd)}
    finally:
        conn.close()


@app.get("/api/jobs/{jid}")
def api_job_detail(jid: int, x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                   x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    s = _session(x_tp_token, x_tp_role)
    require(s, "read")
    conn = db.connect(DB_PATH)
    try:
        j = db.get_job(conn, jid)
        if not j:
            raise HTTPException(status_code=404, detail="未找到该岗位")
        return {"job": j, "jd": j.get("jd_json") or {}}
    finally:
        conn.close()


@app.post("/api/jobs/{jid}/jd")
def api_job_update_jd(jid: int, req: JobJdReq,
                      x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                      x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """修改岗位 JD。改完只影响**之后**的投递评分，已入库的投递档位不会被追溯改动。

    想让已有投递也按新尺子重算，走 `POST /api/jobs/{jid}/regrade`
    （默认先给差异预览，HR 看过再决定落不落地）。响应里把这条路指出来，
    否则 HR 改完 JD 会发现"匹配结果没变"，以为改动没生效。
    """
    s = _session(x_tp_token, x_tp_role)
    require(s, "settings")
    conn = db.connect(DB_PATH)
    try:
        j = db.get_job(conn, jid)
        if not j:
            raise HTTPException(status_code=404, detail="未找到该岗位")
        base = j.get("jd_json") or {}
        merged = _jd_from_req(base, req, j.get("title") or "", j.get("department_name") or "")
        # 只覆盖本次传了的字段：None 表示"界面没提供"，保持原值
        cur = dict(base)
        cur.update({k: v for k, v in merged.items() if k in ("must", "preferred", "note", "origin")})
        saved = db.update_job_jd(conn, jid, cur, s["username"])
        n_apps = (saved or {}).get("applications_count", 0)
        return {"ok": True, "job": saved, "jd": (saved or {}).get("jd_json") or {},
                "applications_count": n_apps,
                "can_regrade": bool(n_apps),
                "regrade_endpoint": f"/api/jobs/{jid}/regrade",
                "warnings": _jd_warnings(cur),
                "note": (f"JD 已更新。仅影响之后新入库的投递评分，历史档位不变；"
                         f"该岗位已有 {n_apps} 条投递，可点「重新分析」按新尺子重算建议档位"
                         if n_apps else
                         "JD 已更新。仅影响之后新入库的投递评分，历史档位不变。")}
    finally:
        conn.close()


def _effective_jd(job: dict) -> dict:
    """岗位实际生效的评分尺子。

    与收件时的归岗口径一致（`ingest` 里是 `eff_jd = route_jd or jd`）：
    岗位自己填过 JD 就用它，否则沿用默认尺子（`config/jd.json`）。
    老库（v0.1 迁移）里 `jd_json` 可能是空对象，不能拿空的去算——
    那会把"没填门槛"变成"零分"，结果全员掉档。
    """
    jd = job.get("jd_json") or {}
    if jd.get("must") or jd.get("preferred"):
        return jd
    return _load(JD_PATH)


class RegradeReq(BaseModel):
    # 默认预演：先给差异，HR 看过再点"应用"。一次误点就把全岗位建议档位改掉，代价太大。
    apply: bool = False


@app.post("/api/jobs/{jid}/regrade")
def api_job_regrade(jid: int, req: RegradeReq | None = Body(default=None),
                    x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                    x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """按当前 JD 重新分析该岗位下已有投递，给出建议档位差异。

    - `apply=False`（默认）：只算不改，返回"谁从 B 变 C"这种差异清单；
    - `apply=True`：把差异落库（分数/建议档位/命中缺失/理由），逐条写审计；
      **HR 已确认过的档位一概不动**。

    画像一律从简历原文重跑规则通道，不读库里的残缺画像（证书等字段没落库），
    避免把"画像读残"误判成"JD 改了导致降分"。
    """
    s = _session(x_tp_token, x_tp_role)
    require(s, "ingest")
    apply_now = bool((req or RegradeReq()).apply)
    conn = db.connect(DB_PATH)
    try:
        j = db.get_job(conn, jid)
        if not j:
            raise HTTPException(status_code=404, detail="未找到该岗位")
        _, tiers = _jd_tiers()
        rep = regrade.regrade_job(conn, jid, _effective_jd(j), tiers,
                                  operator=s["username"], role=s["role"], apply=apply_now)
        rep["job"] = {"id": jid, "title": j.get("title"),
                      "department_name": j.get("department_name")}
        rep["jd_source"] = ("岗位自填 JD" if (j.get("jd_json") or {}).get("must")
                            or (j.get("jd_json") or {}).get("preferred")
                            else "默认尺子 config/jd.json")
        rep["note"] = ("预演完成：下列差异**尚未写入**人才库。确认无误后点「应用重算结果」。"
                       if not apply_now else
                       "已按新尺子重算并落库（HR 已确认的档位未被覆盖），逐条已写入审计。")
        return rep
    finally:
        conn.close()


@app.post("/api/jobs/{jid}/deactivate")
def api_job_deactivate(jid: int, x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                       x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """停用岗位（不物理删除）。有投递的岗位也只能停用，不能删。"""
    s = _session(x_tp_token, x_tp_role)
    require(s, "settings")
    conn = db.connect(DB_PATH)
    try:
        r = db.set_job_active(conn, jid, False, s["username"])
        if not r:
            raise HTTPException(status_code=404, detail="未找到该岗位")
        return {"ok": True, "job": r,
                "note": "岗位已停用（未物理删除，历史投递仍可查看，可随时启用）"}
    finally:
        conn.close()


@app.post("/api/jobs/{jid}/activate")
def api_job_activate(jid: int, x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                     x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    s = _session(x_tp_token, x_tp_role)
    require(s, "settings")
    conn = db.connect(DB_PATH)
    try:
        r = db.set_job_active(conn, jid, True, s["username"])
        if not r:
            raise HTTPException(status_code=404, detail="未找到该岗位")
        return {"ok": True, "job": r}
    finally:
        conn.close()


# ============================================================
# 邮箱配置
# ============================================================

class MailboxConfigReq(BaseModel):
    mode: str | None = None                 # eml | imap | off
    eml_dir: str | None = None
    folder_dir: str | None = None           # 手工导入用本地文件夹
    imap_host: str | None = None
    imap_port: int | None = None
    imap_ssl: bool | None = None
    imap_user: str | None = None
    imap_folder: str | None = None
    password: str | None = None             # 授权码/口令（写入 imap.secret，不回显）
    attachment_ext: list[str] | None = None
    max_attachment_mb: int | None = None
    same_job_reapply_days: int | None = None


@app.get("/api/mailbox/config")
def api_mailbox_config(x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                       x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    s = _session(x_tp_token, x_tp_role)
    require(s, "read")
    cfg = mb.load_config()
    ic = cfg.get("imap") or {}
    return {"mode": cfg.get("mode"), "eml_dir": cfg.get("eml_dir"),
            "folder_dir": cfg.get("folder_dir") or RESUME_DIR,
            "imap_host": ic.get("host") or "", "imap_port": ic.get("port", 993),
            "imap_ssl": ic.get("ssl", True), "imap_user": ic.get("user") or "",
            "imap_folder": ic.get("folder", "INBOX"), "password_set": bool(mb.read_secret()),
            "attachment_ext": cfg.get("attachment_ext"),
            "max_attachment_mb": int(cfg.get("max_attachment_mb", 20) or 20),
            "same_job_reapply_days": (cfg.get("dedup") or {}).get("same_job_reapply_days"),
            "readonly": ic.get("readonly", True),
            # 提醒由后端一处产出，界面只负责显示——两处各写一套规则必然对不上
            "warnings": _mailbox_warnings(cfg)}


# 常见服务商预设：填错端口/SSL 是"配了没反应"最常见的原因，直接给现成的。
MAILBOX_PRESETS = [
    {"label": "腾讯企业邮", "host": "imap.exmail.qq.com", "port": 993, "ssl": True},
    {"label": "QQ 邮箱", "host": "imap.qq.com", "port": 993, "ssl": True},
    {"label": "网易 163", "host": "imap.163.com", "port": 993, "ssl": True},
    {"label": "阿里云企业邮", "host": "imap.qiye.aliyun.com", "port": 993, "ssl": True},
    {"label": "Outlook / Microsoft 365", "host": "outlook.office365.com", "port": 993, "ssl": True},
    {"label": "Gmail", "host": "imap.gmail.com", "port": 993, "ssl": True},
]


@app.get("/api/mailbox/presets")
def api_mailbox_presets(x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                        x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """常用邮箱的服务器/端口/SSL 预设。国内邮箱必须用**授权码**，不是登录密码。"""
    _session(x_tp_token, x_tp_role)
    return {"presets": MAILBOX_PRESETS,
            "note": "国内邮箱（QQ/163/企业邮）需在邮箱设置里开启 IMAP 并生成"
                    "「授权码」，口令栏填授权码，不是登录密码。"}


def _mailbox_warnings(cfg: dict) -> list[str]:
    """把"看着配好了、其实没生效"的情况直接说出来。

    最常见的一种：模式还是 `eml` 演练（读本地 .eml 目录），但 HR 已经把
    IMAP 账号填好了 —— 于是点"收取邮箱简历"永远读的是本地目录，看起来像没反应。
    """
    warns: list[str] = []
    ic = cfg.get("imap") or {}
    mode = cfg.get("mode")
    if mode == "eml" and (ic.get("user") or ic.get("host")):
        warns.append("当前是 eml 演练模式：收取时读的是本地 .eml 目录，"
                     "不会连真实邮箱。要真正收信，请把「模式」改为 imap 并保存。")
    if mode == "imap" and not ic.get("host"):
        warns.append("模式是 imap 但没填 IMAP 服务器地址，收取会直接报错。")
    if mode == "imap" and not ic.get("user"):
        warns.append("模式是 imap 但没填邮箱账号。")
    if mode == "imap" and not mb.read_secret():
        warns.append("模式是 imap 但还没保存授权码/口令。")
    if mode == "off":
        warns.append("邮箱抓取已关闭：只能靠「本地文件夹导入」。")
    ext = cfg.get("attachment_ext") or []
    if not ext:
        warns.append("附件白名单为空，任何附件都不会被收进来。")
    return warns


@app.post("/api/mailbox/config")
def api_mailbox_save(req: MailboxConfigReq, x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                     x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """保存邮箱配置。口令单独写入 config/imap.secret（0600），不回显。"""
    s = _session(x_tp_token, x_tp_role)
    require(s, "settings")
    updates: dict = {}
    if req.mode is not None:
        updates["mode"] = req.mode
    if req.eml_dir is not None:
        updates["eml_dir"] = req.eml_dir
    if req.folder_dir is not None:
        updates["folder_dir"] = req.folder_dir
    if req.attachment_ext is not None:
        updates["attachment_ext"] = req.attachment_ext
    if req.max_attachment_mb is not None:
        updates["max_attachment_mb"] = max(1, min(500, int(req.max_attachment_mb)))
    imap_updates: dict = {}
    if req.imap_host is not None:
        imap_updates["host"] = req.imap_host
    if req.imap_port is not None:
        imap_updates["port"] = req.imap_port
    if req.imap_ssl is not None:
        imap_updates["ssl"] = req.imap_ssl
    if req.imap_user is not None:
        imap_updates["user"] = req.imap_user
    if req.imap_folder is not None:
        imap_updates["folder"] = req.imap_folder
    if imap_updates:
        updates["imap"] = imap_updates
    if req.same_job_reapply_days is not None:
        updates["dedup"] = {"same_job_reapply_days": int(req.same_job_reapply_days)}
    cfg = mb.save_config(updates)
    if req.password:
        mb.write_secret(req.password)
    conn = db.connect(DB_PATH)
    try:
        db.add_audit(conn, "settings", "mailbox", "update",
                     "", f"邮箱配置已更新（模式 {cfg.get('mode')}）", s["username"], s["role"])
    finally:
        conn.close()
    warns = _mailbox_warnings(cfg)
    return {"ok": True, "mode": cfg.get("mode"),
            "max_attachment_mb": int(cfg.get("max_attachment_mb", 20) or 20),
            "folder_dir": cfg.get("folder_dir") or RESUME_DIR,
            "password_set": bool(mb.read_secret()),
            "warnings": warns,
            "note": ("配置已保存，但有几处需要确认：" + "；".join(warns)) if warns
                    else "配置已保存。口令不会显示在界面或日志中。"}


class MailboxTestReq(BaseModel):
    imap_host: str | None = None
    imap_port: int | None = None
    imap_ssl: bool | None = None
    imap_user: str | None = None
    imap_folder: str | None = None
    password: str | None = None


def _resolve_imap(req) -> tuple[str, int, bool, str, str, str]:
    """把"本次请求填的值"与"已保存的配置"合并：请求里没填的用已保存的。"""
    cfg = mb.load_config()
    ic = cfg.get("imap") or {}
    host = req.imap_host if getattr(req, "imap_host", None) is not None else ic.get("host") or ""
    port = req.imap_port if getattr(req, "imap_port", None) is not None else int(ic.get("port", 993))
    ssl_on = req.imap_ssl if getattr(req, "imap_ssl", None) is not None else bool(ic.get("ssl", True))
    user = req.imap_user if getattr(req, "imap_user", None) is not None else ic.get("user") or ""
    folder = (req.imap_folder if getattr(req, "imap_folder", None) is not None
              else ic.get("folder", "INBOX"))
    password = req.password if getattr(req, "password", None) is not None else mb.read_secret() or ""
    return host, int(port or 993), bool(ssl_on), user, folder, password


@app.post("/api/mailbox/test")
def api_mailbox_test(req: MailboxTestReq, x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                     x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """测试 IMAP 连接与登录。口令不回显。

    连上了不等于配好了——还要把模式切到 imap 才会真正去收信，
    所以成功时把"下一步点哪里"直接写进响应，省掉一轮猜测。
    """
    s = _session(x_tp_token, x_tp_role)
    require(s, "settings")
    host, port, ssl_on, user, folder, password = _resolve_imap(req)
    r = mb.test_imap(host, port, ssl_on, user, password, folder)
    cfg = mb.load_config()
    r["warnings"] = _mailbox_warnings(cfg)
    if r.get("ok"):
        r["next_step"] = ("连接正常。接下来：把「模式」切到 imap → 点「保存」→ "
                          "点「先看邮箱里有什么」确认能读到邮件 → 再点「收取简历」。")
    return r


@app.post("/api/mailbox/preview")
def api_mailbox_preview(req: MailboxTestReq, limit: int = 10,
                        x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                        x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """收信**之前**先看一眼：连的是哪个邮箱、最近几封是什么、有没有简历附件。

    只读打开、只取邮件头与附件名，不下载正文、不改标记；口令不回显、不落日志。
    """
    s = _session(x_tp_token, x_tp_role)
    require(s, "settings")
    cfg = mb.load_config()
    if cfg.get("mode") == "eml":
        # 演练模式：直接列本地 .eml 目录，让"从哪导入"看得见
        folder = mb.resolve_dir(cfg.get("eml_dir", "data/mail_in"))
        mails = []
        if os.path.isdir(folder):
            for name in sorted(os.listdir(folder))[-int(limit):]:
                if name.lower().endswith((".eml", ".txt")):
                    st = os.stat(os.path.join(folder, name))
                    mails.append({"subject": name, "from": "（本地演练文件）",
                                  "date": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                                  "attachments": [], "has_resume": True})
        return {"ok": True, "account": f"本地演练目录 {folder}", "folder": folder,
                "total": len(mails), "mails": list(reversed(mails)),
                "message": f"当前是 eml 演练模式：目录里 {len(mails)} 封"}
    host, port, ssl_on, user, folder, password = _resolve_imap(req)
    exts = tuple(cfg.get("attachment_ext") or (".pdf", ".docx", ".doc", ".txt", ".md"))
    return mb.preview_imap(host, port, ssl_on, user, password, folder,
                           limit=int(limit or 10), exts=exts)
