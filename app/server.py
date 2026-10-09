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
import time as _time
import re
import shutil
import sys
import threading
import time
import urllib.parse
import uuid
import zipfile
from datetime import datetime

from fastapi import Body, FastAPI, Header, HTTPException, Request, Query
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel

from . import actions, auth, db, feedback as feedback_mod
from . import ingest as ingest_mod, mailbox as mb, regrade, search
from . import mail_send as mail_send_mod
from . import mail_template as mail_template_mod
from .agent import brief as brief_mod
from .agent import llm
from .agent import proactive as proactive_mod
from .agent.loop import run_agent
from .agent.tools import ToolCtx, catalog as tool_catalog
from .pipeline import domains as domains_mod
from .pipeline import majors as mj
from .pipeline import normalize as nz
from .pipeline import parse as parse_mod
from .pipeline import freshness
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

# 「入库即分析」开关（v1.13.4）。**产品默认开**：进门就有判断是这个工具的卖点；
# 但它每次都会调模型（一次 2-8 秒 + 额度），所以必须给 HR 一个显式开关，
# 让他按自己的额度与节奏决定"入库就分析"还是"攒一批手动补"。
# 关掉不影响任何其它功能：分析结论照样写 candidate_insights，只是改由 HR 点按钮触发。
AUTO_INSIGHT_KEY = "auto_insight_on_ingest"


def _settings(conn) -> dict:
    return {"gender_filter_enabled": bool(db.get_setting(conn, GENDER_FILTER_KEY, False)),
            "auto_insight_on_ingest": bool(db.get_setting(conn, AUTO_INSIGHT_KEY, True))}


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
def index() -> Response:
    """首页（**整个前端内联在这一个 HTML 里**）。

    必须带 `Cache-Control: no-store`（v1.14.3）：JS 是内联的，所以浏览器一旦缓存了
    这个 HTML，就等于缓存了旧版前端——HR 会看到"我明明改了却没反应"（当天反复遇到）。
    这个工具是单机自用、页面只有几十 KB，不值得为它做缓存。
    """
    resp = HTMLResponse(render_page(auth_enabled=_auth_enabled()))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


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

def _edu_check(item: dict, jobs_map: dict) -> dict | None:
    """判断学历是否达到该岗位的学历线。

    学历是**硬门槛**，界面不能只显示一个"本科"让人自己去跟岗位比——
    要直接给出"够不够"的判断，不达标标红。

    岗位取"已归岗 > 建议岗位"，与「对应岗位」的口径一致
    （见 `db.resolve_candidate_job`）。**没有岗位就没有尺子**：
    未归岗且无建议时返回 None，不给"不达标"的结论——那会是无中生有。
    """
    jid = item.get("job_id")
    if not jid:
        jid = (item.get("job_suggestion") or {}).get("job_id")
    job = jobs_map.get(jid) if jid else None
    if not job:
        return None
    need = ((job.get("jd_json") or {}).get("must") or {}).get("education_min")
    if not need:
        return None
    actual = item.get("edu_level") or ""
    a_rank = db.EDU_RANK.get(actual, 0)
    n_rank = db.EDU_RANK.get(need, 0)
    return {"required": need, "actual": actual or None,
            "ok": a_rank >= n_rank,
            # 学历未识别时既不能算达标也不能算不达标，界面要单独提示"待判定"
            "unknown": a_rank == 0,
            "job_title": job.get("title")}


@app.post("/api/upload")
async def api_upload(request: Request, name: str = Query(...),
                     x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                     x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """上传一个简历文件（裸字节，文件名放?name=）。

    为什么不用 multipart：打包产物里没有 python-multipart，引入依赖会让
    "能不能跑"取决于打包有没有收进去——而这条路径恰恰是**离线兜底**，
    不能再引入任何"看心情装上"的依赖。一个文件一个请求，前端循环即可。
    """
    s = _session(x_tp_token, x_tp_role)
    require(s, "ingest")
    import os as _os
    from . import ingest as _ing
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="文件是空的")
    inbox = _os.path.join(BASE, "data", "inbox")
    _os.makedirs(inbox, exist_ok=True)
    # 文件名只留 basename，去掉路径分隔符（防目录穿越）
    safe = _os.path.basename(name or "resume").strip() or "resume"
    for ch in ("\\", "/", ":", "*", "?", "\"", "<", ">", "|"):
        safe = safe.replace(ch, "_")
    stamp = _time.strftime("%Y%m%d_%H%M%S")
    dest = _os.path.join(inbox, f"{stamp}_{safe}")
    i = 1
    while _os.path.exists(dest):
        dest = _os.path.join(inbox, f"{stamp}_{i}_{safe}")
        i += 1
    with open(dest, "wb") as fh:
        fh.write(body)
    jd, tiers = _jd_tiers()
    rep = _ing.ingest_dir(inbox, jd, tiers, DB_PATH, job_id=None, use_llm=False)
    return {"ok": True, "stored": _os.path.basename(dest),
            "bytes": len(body), "index": rep.get("index"),
            "notes": rep.get("notes") or [],
            "hint": "到人才库里点开这个人的完整档案，看「简历原文」那一段——"
                    "那就是 OCR 识别出来的文字，可逐字核对。"}


@app.get("/api/candidates")
def api_candidates(tier: str | None = None, kw: str | None = None,
                   stage: str | None = None, education: str | None = None,
                   univ: str | None = None,
                   min_years: int | None = None, gender: str | None = None,
                   archived: str | None = None,
                   page: int = 0, page_size: int = 0,
                   x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                   x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """人才库列表。

    性别筛选是**受开关约束**的：设置项 `gender_filter_enabled` 关闭时，
    即使传了 `gender` 也按"未筛选"处理，并在响应里说明为什么——
    静默忽略与静默生效一样危险，后者会让 HR 误以为筛出来的就是全部。

    最低学历（v1.7.3）：`education=硕士` 即"硕士及以上"，尺子是 `db.EDU_RANK`
    （博士>硕士>本科>大专）。学历"无法判定"的人不命中任何具体层次——
    但**不能静默消失**：被隐藏的人数放进 `edu_filter.hidden_unknown`，界面出提示。

    院校层次（v1.7.3）：`univ=985 / 211`。985 学校全部同时是 211，
    所以选"211"时 985 也算；识别用 `config/universities.json` 名单
    （精确匹配全名/别名 + 校区后缀归一，独立学院不会误标），
    匹配结果同时以 `uni_tier` 字段随每条下发（卡片标签用）。
    学历与院校都是**岗位相关硬条件**，与性别筛选不同，不需要开关约束。

    归档（v1.4）：默认只回未归档；`?archived=1` 只回已归档（「归档」页用）。
    归档是软隐藏不是删除，档案/投递/附件原样保留。

    分页（v1.7.1）：`page` 从 1 计、`page_size` 每页条数（界面用 10）。
    **两者都不传（或传 0）时返回全量**——这是刻意保留的兼容口径：
    探针、智能体工具、导出等程序化消费方依赖"一次拿全"，不应被迫翻页；
    分页只是人才库界面的阅读方式，不是接口的默认行为。
    翻页在**全部筛选与建议岗位补齐之后**做，所以 `total` 是筛后总数，
    `gender_facets` / `uni_facets` 也是全量口径——页大小不会影响任何统计数字。
    """
    s = _session(x_tp_token, x_tp_role)
    require(s, "read")
    conn = db.connect(DB_PATH)
    try:
        cfg = _settings(conn)
        allowed = bool(cfg.get("gender_filter_enabled"))
        want = (gender or "").strip() if allowed else ""
        want_archived = (archived or "").strip() in ("1", "true", "yes")
        want_edu = (education or "").strip() or None
        want_univ = (univ or "").strip()
        items = db.list_candidates(conn, tier=tier, keyword=kw, stage=stage,
                                   min_years=min_years,
                                   archived=True if want_archived else False)
        # 最低学历：在 server 层做而不是塞进 list_candidates——
        # "无法判定被隐藏的人数"要数的是**除学历外其他条件都命中**的人，
        # 这个集合只有在这里拿得到。
        hidden_unknown = 0
        if want_edu:
            rank = db.EDU_RANK.get(want_edu, 0)
            hidden_unknown = sum(1 for i in items
                                 if db.EDU_RANK.get(i.get("edu_level") or "", 0) == 0)
            items = [i for i in items
                     if db.EDU_RANK.get(i.get("edu_level") or "", 0) >= rank]
        # 院校层次 facets：在自身筛选生效前统计（与性别 facets 同一套口径），
        # 这样下拉里写的人数不会因当前选项而缩水，对得上"共 N 人"。
        _c985 = sum(1 for i in items if i.get("uni_tier") == "985")
        _c211 = sum(1 for i in items if i.get("uni_tier") == "211")
        uni_facets = {"985": _c985, "211": _c985 + _c211}
        if want_univ in ("985", "211"):
            items = [i for i in items
                     if i.get("uni_tier") == "985"
                     or (want_univ == "211" and i.get("uni_tier") == "211")]
        # 分布统计与结果过滤共用 `db.gender_matches` 一个口径：
        # 否则会出现"点了男，列表 3 个人，下拉里写着 4"这种对不上的情况。
        facets = {g: sum(1 for i in items if db.gender_matches(i.get("gender"), g))
                  for g in db.GENDER_GROUPS}
        facets["全部"] = len(items)
        if want:
            items = [i for i in items if db.gender_matches(i.get("gender"), want)]
        # 建议岗位：**只读库，绝不在这里调模型**（v1.13.2）。
        # 结论落在 `applications.suggested_job_id`（+ `suggested_job_reason`），
        # 由入库时的 `ingest._route_by_open_jobs`、或 HR 主动点的「判断建议岗位」写入。
        #
        # 为什么把原来的"现算兜底"删掉：老数据（没有存储结论）每打开一次人才库
        # 就会现调一次模型判断，一次 6-8 秒**且烧 token**——HR 实测反馈
        # 「每次点击人才库页面都会重新调用模型，这完全没必要，应该存储」。
        # 现在库里没有结论就如实留空，并在界面上给一个明确动作让 HR 决定何时判断。
        jobs_map = {j["id"]: j for j in db.list_jobs(conn, include_inactive=True)}
        pending = [i for i in items if i.get("job_id") is None]
        for i in pending:
            jid = i.get("suggested_job_id")
            j = jobs_map.get(jid) if jid else None
            if j:
                i["job_suggestion"] = {
                    "job_id": j["id"], "title": j.get("title") or "",
                    "dept": j.get("department_name") or j.get("dept") or "",
                    "reason": i.get("suggested_job_reason") or "",
                    "tier_suggested": i.get("tier_suggested"),
                    "source": "stored",
                }
            else:
                # 没存过结论：不猜、不现算，交给 HR 点一次（结果会落库）
                i["job_suggestion"] = None
                i["job_suggestion_missing"] = True
            i["job_suggestions_considered"] = len(jobs_map)
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
        # 自动分析结果（v1.8「入库即分析」）：在分页之后按当前页批量取，
        # 只对真正会下发的那批人查库——一次 IN 查询，不做 N+1。
        # 未分析完（后台线程还在跑）时为 None，界面显示"分析中…"，不是错误状态。
        imap = db.insights_for(conn, [i["id"] for i in items])
        # 档位来源明细（v1.12）：学历门槛现算 + 库内生效档位与来源，口径与完整档案一致
        # （见 `_tier_detail_of`）。计算很轻（约 5ms/人）且只对当前页做，直接跟着列表下发。
        # 档位明细要用"岗位 JD"与"技能分类"：各查一次供整页复用（不再逐人查库）
        _jdm, _cats = _jd_map(conn), _skill_cats(conn)
        for i in items:
            i["insight"] = imap.get(i["id"])
            # 学历达标判断：必须放在 job_suggestion 算完之后
            # （未归岗的人靠"建议岗位"才有尺子可比）
            i["edu_check"] = _edu_check(i, jobs_map)
            # 招聘对象身份：有工作经历看年限，没有就看毕业时间（应届/往届未就业）
            i["exp_display"] = freshness.exp_label(
                i.get("years_exp"), bool(i.get("years_exp")), i.get("grad_date"))
            i["tier_detail"] = _tier_detail_of(i, _jdm, _cats)
        presented = auth.present_list(items)
        out = {"count": len(presented), "items": presented,
               # v1.13.4：入库即分析的开关状态与待分析人数。列表页靠它渲染开关与
               # "分析待分析的人（N）"按钮——顺带带上是��为了不多发一个请求。
               "insight_switch": {"enabled": bool(db.get_setting(conn, AUTO_INSIGHT_KEY, True)),
                                  "pending": db.count_needing_insight(conn)},
               "my_permissions": s["permissions"],
               "archived_view": want_archived,
               "gender_facets": facets if allowed else None,
               "uni_facets": uni_facets,
               "edu_filter": {"min": want_edu, "applied": bool(want_edu),
                              "hidden_unknown": hidden_unknown},
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


def _jd_map(conn) -> dict:
    """岗位 JD 映射（整页查一次，不逐人查）。"""
    return {j["id"]: {"id": j["id"], "title": j.get("title") or "",
                      "jd_json": j.get("jd_json") or {}}
            for j in db.list_jobs(conn, include_inactive=True)}


def _skill_cats(conn) -> list:
    """技能分类（整页查一次）。失败返回空列表——专业方向显示不出来不影响其它内容。"""
    try:
        return db.skill_categories(conn)
    except Exception:                                        # noqa: BLE001
        return []


def _detail_flat_item(conn, cid: int) -> dict:
    """把完整档案的"人 + 最新投递"压成与列表同形状的一条（给 _tier_detail_of 用）。"""
    d = db.candidate_detail(conn, cid) or {}
    apps = d.get("applications") or []
    a = next((x for x in apps if x.get("job_id")), apps[0] if apps else {})
    return {
        "id": cid, "job_id": a.get("job_id"),
        "tier_final": a.get("tier_final"), "tier_suggested": a.get("tier_suggested"),
        "hits": a.get("hits") or [], "miss": a.get("miss") or [],
        "risks": a.get("risks") or [],
        "major": d.get("major"), "major_canonical": d.get("major_canonical"),
        "edu_level": d.get("edu_level"),
        "skills": [x.get("name") for x in (d.get("skills") or [])],
    }


def _tier_detail_of(item: dict, jd_map: dict, cats: list) -> dict | None:
    """档位来源明细（v1.12 口径）：**直接读库，不再逐人调工具重算**（v1.13.6）。

    为什么不再借 `explain_grade`：那个工具会为一个人重跑 grade() 与 major_match()，
    而列表要展示的结论本来就在库里（见上方说明）。实测每人约 48ms，
    10 人 0.5 秒、100 人 4.8 秒。

    口径与之前完全一致（HR 已看过一版，不因性能改动而变化）：
    tier = 库内生效档位（HR 确认优先）；tier_source 说清它从哪来。
    """
    from .pipeline.tier import major_match          # v1.13.6：major_match 在 tier.py，不在 major_llm
    job = jd_map.get(item.get("job_id")) if item.get("job_id") else None
    if not job:
        return None            # 未归岗且无建议岗位：没有尺子，谈不上档位来源
    tier = item.get("tier_final") or item.get("tier_suggested")
    confirmed = bool(item.get("tier_final"))
    if confirmed:
        _src = "HR 已确认"
    elif tier == "D":
        _src = "学历门槛（规则判定，可复现）"
    elif tier:
        _src = "模型分析（自动分析给出的建议档）"
    else:
        _src = "待分析（模型尚未给出结论）"
    try:
        _mm = major_match({"skills": item.get("skills") or [],
                           "major": item.get("major"),
                           "major_canonical": item.get("major_canonical"),
                           "education": item.get("edu_level")},
                          job.get("jd_json") or {}, cats)
    except Exception:                                        # noqa: BLE001
        _mm = {}
    return {
        "tier": tier,
        "tier_rule": "D" if (tier == "D" and not confirmed) else None,
        "tier_source": _src,
        "hit": item.get("hits") or [],
        "miss": item.get("miss") or [],
        "miss_custom": [],
        "major": _mm,
        "risks": item.get("risks") or [],
        "breakdown": {},
        "score": None,
        "consistency": None,
    }


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
            # 档位来源明细（与列表同一份口径）：完整档案是 HR 逐条核对的地方，
            # 这里没有它就只能看到档位=B却不知道凭什么（v1.12 修）
            # 与列表同一份口径：直接读库算明细（不再借工具重算）
            "tier_detail": _tier_detail_of(_detail_flat_item(conn, cid),
                                           _jd_map(conn), _skill_cats(conn)),
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
            # 自动分析（入库即分析）：这是**系统自己看的结论**，
            # 与 HR 点「模型分析」得到的即时应答分开呈现，避免两者被当同一回事。
            "insight": db.get_insight(conn, cid),
            "tier_effective": d.get("tier_effective"),
        }
    finally:
        conn.close()


@app.post("/api/candidates/{cid}/reanalyze")
def api_candidate_reanalyze(cid: int,
                            x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                            x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """重跑自动分析并立刻返回结果（同步，供界面「重算」按钮用）。

    与入库时那条异步钩子的区别：这里是 HR 在等结果，所以同步执行、直接回结果。
    **仍然只写分析文本**，不碰档位——重算分析不等于重算档位。
    """
    from .pipeline.analyze import auto_insight

    s = _session(x_tp_token, x_tp_role)
    require(s, "chat")
    conn = db.connect(DB_PATH)
    try:
        d = db.candidate_detail(conn, cid)
        if not d:
            raise HTTPException(status_code=404, detail="未找到该候选人")
        apps = d.get("applications") or []
        app_row = apps[0] if apps else {}
        app_id = app_row.get("id") or 0
        jd, job_meta = db.resolve_candidate_job(conn, d)
        ins = auto_insight(d, jd, app_row)
        db.upsert_insight(conn, cid, app_id,
                          summary=ins.get("summary") or "",
                          reasons=ins.get("reasons"), risks=ins.get("risks"),
                          evidence=ins.get("evidence"),
                          source=ins.get("source") or "manual",
                          model=ins.get("model") or "",
                          business_direction=ins.get("business_direction"))
        # 模型建议档位 → 更新 tier_suggested（HR 未确认时才更新；D 不覆盖）
        _mt = ins.get("suggested_tier")
        if _mt and _mt in ("A", "B", "C", "D") and app_id:
            _ar = conn.execute("SELECT tier_suggested, tier_final FROM applications WHERE id=?",
                               (app_id,)).fetchone()
            if _ar and not _ar["tier_final"] and _ar["tier_suggested"] != _mt:
                conn.execute("UPDATE applications SET tier_suggested=?, updated_at=? WHERE id=?",
                             (_mt, db.now(), app_id))
                conn.commit()
        db.add_audit(conn, "candidate", str(cid), "reanalyze", "",
                     f"{s['username']} 重跑自动分析（{ins.get('source')}）",
                     s["username"], s["role"])
        return {"ok": True, "job": job_meta, "insight": db.get_insight(conn, cid, app_id),
                "note": "分析已更新。模型建议档位已同步到建议档（HR 未确认的）；阶段与岗位不受影响。"}
    finally:
        conn.close()


@app.get("/api/candidates/{cid}/channel-compare")
def api_channel_compare(cid: int,
                        x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                        x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """**模型通道 vs 规则通道**对照（只读、不落库、不改档位）。

    为什么要这个：口径上"哪些地方用模型、哪些地方必须用规则"是有讲究的，
    但光靠说明文字没法验证模型到底有没有增益。这里把同一份简历、同一把 JD 尺子
    的两条通道结果并排摆出来：

    - 规则通道 = 库内结论（可复核、可复现、可审计，也是自动归档/排序的依据）；
    - 模型通道 = 现跑一次（`analyze_fit`），给出它的档位建议、亮点、风险与置信度。

    两者不一致时**不做任何自动动作**，只把差异说清楚——模型是参考，不是决定。
    模型不可用时如实返回 `reason_unavailable`，界面照实显示（不假装跑过）。
    """
    s = _session(x_tp_token, x_tp_role)
    require(s, "read")
    from .agent import llm as llm_mod
    from .pipeline.analyze import analyze_fit

    conn = db.connect(DB_PATH)
    try:
        d = db.candidate_detail(conn, cid)
        if not d:
            raise HTTPException(status_code=404, detail="未找到该候选人")
        jd, job_meta = db.resolve_candidate_job(conn, d)
        apps = d.get("applications") or []
        cur = apps[0] if apps else {}
        rule = {
            "tier": cur.get("tier_final") or cur.get("tier_suggested"),
            "tier_final": cur.get("tier_final"),
            "score": cur.get("score"),
            "reasons": cur.get("reasons") or [],
            "risks": cur.get("risks") or [],
            "hits": cur.get("hit") or [],
            "miss": cur.get("miss") or [],
            "job": (job_meta or {}).get("title") or "",
            "tier_source": ("学历门槛（规则）" if (cur.get("tier_final")
                                                or cur.get("tier_suggested")) == "D"
                            else "模型分析（库内上次结论）"),
            "note": "库内生效结论（人才库/管道里显示的就是它）：学历门槛由规则判，"
                    "档位 A/B/C 由上次的模型分析给出",
        }
        cand = dict(d)
        for k in ("score", "tier_suggested", "hits", "miss", "applied_at"):
            if cur.get(k) is not None:
                cand[k] = cur.get(k)
    finally:
        conn.close()

    llm_out, why = None, None
    st = llm_mod.status()
    if not jd:
        why = "该候选人还没有可用岗位（既未归岗、也没有建议岗位），没有尺子可比"
    elif not st.get("reachable") or not st.get("model_installed"):
        why = st.get("error") or "模型不可用"
    else:
        fit = analyze_fit(cand, jd)
        if not fit:
            why = "模型没有返回结果（见服务端日志）"
        else:
            llm_out = {
                "tier": fit.get("suggested_tier"),
                "summary": fit.get("summary") or "",
                "highlights": fit.get("highlights") or [],
                "risks": fit.get("risks") or [],
                "confidence": fit.get("confidence"),
                "model": st.get("model"),
            }
    diff = None
    if llm_out and rule.get("tier"):
        rt, lt = rule["tier"], llm_out.get("tier")
        if rt and lt:
            rank = {"A": 3, "B": 2, "C": 1, "D": 0}
            diff = {"same": rt == lt,
                    "delta": (rank.get(lt, -1) - rank.get(rt, -1)),
                    "text": ("两条通道结论一致（%s）" % rt if rt == lt else
                             "模型给 %s，规则给 %s —— %s" % (
                                 lt, rt,
                                 "模型更宽松" if rank.get(lt, -1) > rank.get(rt, -1)
                                 else "模型更保守"))}
    return {"ok": True, "candidate_id": cid, "job": job_meta, "rule": rule,
            "llm": llm_out, "diff": diff, "reason_unavailable": why,
            "model_status": {"reachable": st.get("reachable"), "model": st.get("model"),
                             "route": (st.get("route") or {}).get("route"),
                             "proxy_env": list((st.get("proxy_env") or {}).keys())},
            "disclaimer": "这是**现跑一次**的模型判断，只作对照：不写入档案、不改档位。"
                          "库内结论（学历门槛 + 上次模型分析）才是生效的；"
                          "两次模型结论不同属正常波动，需要的话点「重算自动分析」刷新库内结论。"}


@app.post("/api/candidates/{cid}/rename")
def api_candidate_rename(cid: int, req: dict,
                         x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                         x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """人工修正档案字段：姓名 / 学历 / 学校 / 专业 / 电话 / 邮箱（v1.11 起支持联系方式）。

    为什么必须有这个口子：这些字段一旦识别错（姓名在图片里、学校写简称、
    专业是自造写法、电话正则没匹配到），会连带影响去重、检索、专业方向判定与分级展示。
    与其让算法硬猜，不如让最了解情况的 HR 一步改对——每次改动都写审计，
    改前改后都留痕，可追溯可回退。
    电话/邮箱是加密存储的，修改时同步更新盲索引（用于跨渠道身份比对）。
    """
    s = _session(x_tp_token, x_tp_role)
    require(s, "chat")
    req = req or {}
    fields: dict = {}
    if "name" in req:
        name = str(req.get("name") or "").strip()
        if not re.fullmatch(r"[\u4e00-\u9fa5·A-Za-z\s]{1,30}", name):
            raise HTTPException(status_code=422, detail="姓名只应为 1-30 个汉字/字母")
        fields["name"] = name
    if "education" in req:
        edu = str(req.get("education") or "").strip()
        if edu and edu not in db.EDU_RANK:
            raise HTTPException(status_code=422,
                                detail="学历只能填：大专 / 本科 / 硕士 / 博士（或留空表示未识别）")
        fields["edu_level"] = edu or None
    if "school" in req:
        fields["school"] = str(req.get("school") or "").strip() or None
    if "major" in req:
        fields["major"] = str(req.get("major") or "").strip() or None
    if "phone" in req:
        phone = str(req.get("phone") or "").strip()
        if phone and not re.fullmatch(r"[\d\s\-+()]{7,20}", phone):
            raise HTTPException(status_code=422, detail="电话格式不对（7-20 位数字/符号）")
        if phone:
            from .crypto import encrypt, blind_index
            from .identity import normalize_phone
            norm = normalize_phone(phone)
            fields["phone_enc"] = encrypt(phone)
            fields["phone_bidx"] = blind_index(phone) if norm else None
        else:
            fields["phone_enc"] = None
            fields["phone_bidx"] = None
    if "email" in req:
        email = str(req.get("email") or "").strip()
        if email and "@" not in email:
            raise HTTPException(status_code=422, detail="邮箱格式不对")
        if email:
            from .crypto import encrypt, blind_index
            fields["email_enc"] = encrypt(email)
            fields["email_bidx"] = blind_index(email)
        else:
            fields["email_enc"] = None
            fields["email_bidx"] = None
    if not fields:
        raise HTTPException(status_code=422, detail="没有要修改的字段")
    from .crypto import decrypt
    conn = db.connect(DB_PATH)
    try:
        d = db.candidate_detail(conn, cid)
        if not d:
            raise HTTPException(status_code=404, detail="未找到该候选人")
        label = {"name": "姓名", "edu_level": "学历", "school": "学校", "major": "专业",
                 "phone_enc": "电话", "email_enc": "邮箱"}
        old_map = {"name": d.get("name") or "", "edu_level": d.get("edu_level") or "",
                   "school": d.get("school") or "", "major": d.get("major") or "",
                   "phone_enc": decrypt(d.get("phone_enc")) or "",
                   "email_enc": decrypt(d.get("email_enc")) or ""}
        changed = {k: (old_map.get(k), fields[k]) for k in fields
                   if str(old_map.get(k) or "") != str(fields[k] or "")}
        if not changed:
            return {"ok": True, "id": cid, "changed": [], "note": "字段没有变化。"}
        db.update_candidate(conn, cid, **fields)
        before = "；".join(f"{label[k]}={v[0] or '（空）'}" for k, v in changed.items())
        after = "；".join(f"{label[k]}={v[1] or '（空）'}" for k, v in changed.items())
        db.add_audit(conn, "candidate", str(cid), "edit_fields", before, after,
                     s["username"], s["role"])
        return {"ok": True, "id": cid, "changed": sorted(changed),
                "note": "已更正：" + "、".join(label[k] for k in changed)
                        + "（改前改后都记在操作审计里）。"}
    finally:
        conn.close()


def _end_open_applications(conn, cid: int, s: dict) -> int:
    """把某候选人**进行中**的投递置为「已结束」（归档联动用），返回改动条数。

    终态不动：`已入职` 是有价值的历史事实，不能因为归档就被改写成"已结束"；
    `已结束` 本来就是终点，无需重复写。
    """
    n = 0
    rows = conn.execute("SELECT id, stage FROM applications WHERE candidate_id = ?",
                        (int(cid),)).fetchall()
    for a in rows:
        if (a["stage"] or "") in ("已入职", "已结束"):
            continue
        db.set_application_stage(conn, a["id"], "已结束", s["username"], s["role"])
        n += 1
    return n


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
        # 归档即结束流程：把还在推进中的投递一并置为「已结束」。
        # 否则人从人才库消失了，投递却还挂在「初面」上，管道视图会永远在催它。
        ended = _end_open_applications(conn, cid, s)
        return {"ok": True, "id": cid, "archived": True, "ended_applications": ended,
                "note": "已归档（移入「归档」页，不再在人才库与检索中展示）"
                        + (f"，并自动把 {ended} 条进行中的投递置为「已结束」。"
                           if ended else "。")}
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


@app.post("/api/candidates/{cid}/suggest-job")
def api_suggest_job(cid: int,
                    x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                    x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """**主动**判断一次「建议岗位」并落库（v1.13.2）。

    为什么要有这个显式动作：列表接口原来对没有存储结论的投递会现算模型判断，
    每打开一次人才库就等 6-8 秒、还烧 token（HR 实测反馈「完全没必要，应该存储」）。
    现在列表只读库；想更新建议时点一次这里，结论落 `suggested_job_id` 与
    `suggested_job_reason`，之后所有展示都是免费读取。动作写审计。
    """
    s = _session(x_tp_token, x_tp_role)
    require(s, "set_stage")
    if not _llm_enabled():
        return {"ok": False, "error": "模型未启用，无法判断建议岗位；可直接手动「指定岗位」"}
    conn = db.connect(DB_PATH)
    try:
        out = regrade.suggest_job_for_application(conn, cid, s["username"], s["role"])
        if out.get("ok"):
            out["job"] = db.resolve_candidate_job(conn, db.candidate_detail(conn, cid) or {})[1]
        return out
    finally:
        conn.close()


class InsightBatchReq(BaseModel):
    # 一次最多分析多少人（前端循环调用）。上限 20：再多单请求会超时，
    # 而且模型连着调 20 次也让 HR 等太久没有反馈。
    limit: int = 5


@app.post("/api/insights/analyze-pending")
def api_analyze_pending(req: InsightBatchReq,
                        x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                        x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """**手动批量补分析**（"入库即分析"关掉时用，v1.13.4）。

    口径与自动入库分析**完全一致**（都走 `ingest_mod.analyze_one`）——这是刻意的：
    两条路一旦各写一套，日后必然漂移，"手动分析出来的"和"自动分析的"口径不同，
    比没有开关更难解释。

    为什么同步而不是后台：模型一次 2-8 秒，前端要能显示"已分析 5/12"；
    后台跑的话 HR 只能等，不知道进度。前端循环调用直到 remaining=0。
    分析只写分析结论与「系统建议档位」；**不动 HR 已确认的档位、阶段与岗位**。
    """
    s = _session(x_tp_token, x_tp_role)
    require(s, "set_stage")
    if not _llm_enabled():
        return {"ok": False, "analyzed": 0, "remaining": 0,
                "error": "模型未启用，无法分析；可在「模型设置」里配置后再试"}
    limit = max(1, min(int(req.limit or 5), 20))
    conn = db.connect(DB_PATH)
    try:
        targets = db.candidates_needing_insight(conn, limit=limit)
        done: list[dict] = []
        failed: list[dict] = []
        for t in targets:
            try:
                ins = ingest_mod.analyze_one(conn, t["candidate_id"], t["application_id"],
                                             source="manual_batch")
                if ins:
                    done.append({"candidate_id": t["candidate_id"], "name": t.get("name"),
                                 "tier": ins.get("suggested_tier")})
                else:
                    failed.append({"candidate_id": t["candidate_id"], "name": t.get("name"),
                                   "why": "无分析结果"})
            except Exception as exc:                        # noqa: BLE001 — 单条失败不中断整批
                print(f"[analyze-pending] 候选人#{t['candidate_id']} 失败："
                      f"{type(exc).__name__}: {exc}", file=sys.stderr)
                failed.append({"candidate_id": t["candidate_id"], "name": t.get("name"),
                               "why": type(exc).__name__})
        conn.commit()
        remaining = db.count_needing_insight(conn)
        db.add_audit(conn, "insight", "batch", "analyze_pending",
                     f"待分析 {len(targets)} 人（上限 {limit}）",
                     f"成功 {len(done)} / 失败 {len(failed)}，剩余 {remaining}",
                     s["username"], s["role"])
        conn.commit()
        return {"ok": True, "analyzed": len(done), "failed": len(failed),
                "remaining": remaining, "done": done, "errors": failed[:10],
                "note": (f"已分析 {len(done)} 人"
                         + (f"，失败 {len(failed)} 人" if failed else "")
                         + (f"，还剩 {remaining} 人待分析" if remaining else "，全部完成"))}
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
                                     operator=s["username"], role=s["role"],
                                     use_llm=_llm_enabled())
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


def _purge_expired(conn=None, actor: str = "system", role: str = "system",
                   only_ids: list[int] | None = None) -> dict:
    """把归档满 30 天的档案彻底删除，并把原件移进回收目录。

    自有连接便于被定时任务与接口两处复用。**先移文件再删库**：万一移动失败，
    库里记录还在（下次还能重试），不会出现"记录没了、文件也不知在哪"。
    """
    own = conn is None
    conn = conn or db.connect(DB_PATH)
    try:
        out = db.purge_due_candidates(conn, db.PURGE_AFTER_DAYS, actor, role,
                                      only_ids=only_ids)
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
        # **只删这个人**（v1.13.6 修：原来这里删的是"所有到期归档的人"）
        out = _purge_expired(conn=conn, actor=s["username"], role=s["role"], only_ids=[cid])
        got = [p for p in out["people"] if p["id"] == cid]
        if not got:
            raise HTTPException(status_code=409,
                                detail="未删除：归档时间可能被并发改过，或已不在到期名单里。"
                                       "请刷新后重试")
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
    # 顺手把存量「待指定」投递的建议岗位清理一次：老库里 `suggested_job_id` 是按
    # "默认尺子打分最高"给的，而加权打分已在 v1.12 删除——这些旧建议不再有依据。
    # **启动路径不调模型**（use_llm=False）：启动要快，不能为 N 条存量投递各调一次模型；
    # 这里只把失效的旧建议清成"待指定"，HR 需要重新判断时点界面上的按钮（那次才调模型）。
    try:
        conn = db.connect(DB_PATH)
        try:
            rep = regrade.route_pending(conn, _load(TIERS_PATH), use_llm=False)
        finally:
            conn.close()
        if rep.get("changed"):
            print(f"[route] 已清理 {rep['changed']} 条「待指定」投递的失效岗位建议"
                  f"（在招岗位 {rep['open_jobs']} 个）；需要重新判断请点界面上「重新判断建议岗位」")
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
    # v1.13.3：可选指定是哪一条投递（一个人可能有多条投递）；
    # 不传就取该候选人最新一条（归岗/改岗位都用同一个入口）
    application_id: int | None = None

@app.post("/api/candidates/{cid}/assign-job")
def api_assign_job(cid: int, req: AssignJobReq,
                   x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                   x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """采纳岗位建议：把该候选人最新的「待指定」投递归到指定岗位（C 方案落库动作）。"""
    s = _session(x_tp_token, x_tp_role)
    require(s, "set_stage")
    conn = db.connect(DB_PATH)
    try:
        # v1.13.3：**归岗与改岗位走同一个入口**。
        # 原来只找「待指定」的投递——已归岗的人想改岗位会被 400 挡回去，
        # HR 就没办法纠正识别错的归属（实测反馈："要能给每个人一个修改岗位的按钮"）。
        # 现在：优先用显式指定的 application_id；否则取该候选人最新一条投递
        # （已归岗的会被 assign_job 当成"改岗位"处理，并写 reassign_job 审计）。
        if req.application_id:
            row = conn.execute("SELECT id, job_id FROM applications WHERE id = ? AND candidate_id = ?",
                               (req.application_id, cid)).fetchone()
        else:
            row = conn.execute(
                """SELECT id, job_id FROM applications WHERE candidate_id = ?
                   ORDER BY COALESCE(applied_at,'') DESC, id DESC LIMIT 1""", (cid,)).fetchone()
        if not row:
            raise HTTPException(status_code=400, detail="该候选人没有可归岗的投递记录")
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


class ReviewReq(BaseModel):
    # v1.14.1：复核可以撤销（HR 觉得"我还没想清楚"时用）
    undo: bool = False


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


@app.post("/api/applications/{aid}/review")
def api_review(aid: int, req: ReviewReq, x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
               x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """**复核**这条建议（认可系统的判断，不改档位）。

    与 `set_tier` 分开的理由（v1.14.1）：见 `db.review_mark` 的注释——
    认可与改判是两件事，绑在一起会诱导 HR 为了消标签而改档位。
    权限用 `set_stage` 级：它不改动任何结论，只是标记"我看过"。
    """
    s = _session(x_tp_token, x_tp_role)
    require(s, "set_stage")
    conn = db.connect(DB_PATH)
    try:
        r = (db.unreview_mark(conn, aid, s["username"], s["role"]) if req.undo
             else db.review_mark(conn, aid, s["username"], s["role"]))
        if not r:
            raise HTTPException(status_code=404, detail="未找到该投递")
        return {"ok": True, "application": r,
                "status": r.get("status"),
                "note": ("已撤销复核" if req.undo else "已复核：认可系统给的建议档（档位未改动）")}
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
        # 流程结束即归档：走到终点的人不该继续占着人才库列表。
        # 「已入职」也归——他是历史战绩，同样不需要天天出现在待处理列表里。
        archived = False
        if req.stage in ("已结束", "已入职") and r.get("candidate_id"):
            db.set_candidate_archived(conn, r["candidate_id"], True, s["username"], s["role"])
            archived = True
        return {**r, "auto_archived": archived,
                "note": ("阶段已更新，并已自动归档该候选人"
                         "（可在「归档」页随时取消归档）。" if archived else "")}
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
    # v1.13.7：None = 自动（模型已配置就用模型抽取基本信息，带原文证据校验）；
    # 显式 True/False 可强制。默认不再是 False——纯规则对两栏/表格/非常规日期
    # 的简历漏抽太厉害（HR 实测反馈），而模型抽的值有服务端证据校验兜着。
    use_llm: bool | None = None
    folder: str | None = None


@app.post("/api/ingest")
def api_ingest(req: IngestReq | None = Body(default=None),
               x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
               x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    s = _session(x_tp_token, x_tp_role)
    require(s, "ingest")
    req = req or IngestReq()
    jd, tiers = _jd_tiers()

    # 自动模式：模型已配置就用（抽取走模型 + 证据校验）。显式 False 时完全不调模型。
    _use_llm = req.use_llm
    if _use_llm is None:
        _c0 = llm.load_cfg()
        _use_llm = bool(_c0.get("api_key"))
    llm_conf = None
    if _use_llm:
        c = llm.load_cfg()
        llm_conf = {"api_key": c.get("api_key"), "base_url": c.get("base_url"),
                    "model": c.get("model")}

    cfg = mb.load_config()
    if req.source == "folder":
        # 文件夹上传：只分析、不归岗（job_id=None = 待 HR 指定）
        folder = req.folder or cfg.get("folder_dir") or RESUME_DIR
        report = ingest_mod.ingest_dir(folder, jd, tiers, DB_PATH, cfg=cfg, job_id=None,
                                       use_llm=_use_llm, llm_conf=llm_conf)
        report["source"] = "folder"
        report["source_label"] = f"本地文件夹 {os.path.abspath(folder)}"
    else:
        # 邮箱：按邮件标题归岗（在 ingest_mails 内部完成），job_id 参数不参与
        report = ingest_mod.sync_mailbox(jd, tiers, DB_PATH, job_id=None,
                                         use_llm=_use_llm, llm_conf=llm_conf, cfg=cfg)
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
    if not path:
        raise HTTPException(status_code=404, detail="原件未记录路径")
    # 只允许发送本仓库目录下的文件，避免被构造路径读到系统文件。
    # 顺序要点：**先判越界、再判存在**。若反过来，构造一个仓库外但不存在的路径
    # 会先得到 404——等于把"该文件在不在"变成了可探测的信息（存在性 oracle）；
    # 而且自检里"构造越界路径必须被拒"这条断言，在 Windows（该路径不存在）
    # 上会拿到 404 而不是 403，掩盖真实的越界分支是否生效。
    real = os.path.realpath(path)
    if not real.startswith(os.path.realpath(BASE) + os.sep):
        raise HTTPException(status_code=403, detail="原件路径超出允许范围，已拒绝")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail=f"原件不在磁盘上：{path}")

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
                if not path:
                    skipped.append(f"{d.get('file_name') or did}（未记录路径）")
                    continue
                # 同 /api/documents/{id}/file：先判越界再判存在，
                # 否则越界文件会被记成"原件不在磁盘上"，把实情报错。
                real = os.path.realpath(path)
                if not real.startswith(os.path.realpath(BASE) + os.sep):
                    skipped.append(f"{d.get('file_name') or did}（路径越界，已拒绝）")
                    continue
                if not os.path.isfile(path):
                    skipped.append(f"{d.get('file_name') or did}（原件不在磁盘上）")
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
        # need_job 标记：让前端在报错旁边给出「指定岗位」入口——只报错不给入口，
        # HR 会卡在"无法分析、也不知道去哪儿归岗"（实测反馈）。
        return {"error": "该候选人尚无对应岗位", "job": job_meta, "need_job": True,
                "hint": "分析要按岗位的 JD 来。请先归岗（或采纳建议岗位），再生成分析"}
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
        return {"error": "该候选人尚无对应岗位", "job": job_meta, "need_job": True,
                "hint": "面试提纲按岗位定制：请先归岗（或采纳建议岗位）后再生成"}
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
    # v1.13.4：入库即分析开关（默认开）。关掉后入库不自动调模型，改由 HR 手动批量补。
    auto_insight_on_ingest: bool | None = None


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


class ModelCfgReq(BaseModel):
    """模型配置保存。api_key 传空/缺省 = 不修改（前端"留空不改"）。"""
    base_url: str | None = None
    model: str | None = None
    api_key: str | None = None


@app.get("/api/model-config")
def api_model_config_get(x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                         x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """模型与密钥配置（只读）。

    **API Key 只以掩码形态离开服务端**（`sk-****1234`），完整值永不下发——
    这条与"密文不下发"同一条红线：界面要能看状态，但不能成为密钥的出口。
    """
    s = _session(x_tp_token, x_tp_role)
    require(s, "read")
    c = llm.load_cfg()
    key = c.get("api_key") or ""
    if os.environ.get("LLM_API_KEY"):
        src = "环境变量 LLM_API_KEY"
    elif c.get("api_key_env"):
        src = f"环境变量 {c['api_key_env']}" if os.environ.get(c["api_key_env"]) else "未解析到（该环境变量未设置）"
    elif os.path.exists(llm.SECRETS_PATH):
        src = "密钥文件 config/secrets.json"
    else:
        src = "config/model.json" if key else "未配置"
    env_set = [k for k in ("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY") if os.environ.get(k)]
    return {"base_url": c.get("base_url") or "", "model": c.get("model") or "",
            "key_masked": llm.mask_key(key), "key_set": bool(key),
            "key_source": src, "env_override": env_set,
            "note": "API Key 只显示掩码；保存写 config/secrets.json（0600，不入库不提交），"
                    "保存即生效（每次模型调用重新读取配置）。环境变量优先于文件值。"}


@app.post("/api/model-config")
def api_model_config_set(req: ModelCfgReq,
                         x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                         x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """保存模型配置（写文件，写审计）。

    审计记录**只记改了哪些字段**，api_key 的值与掩码都不写进审计——
    审计日志随库备份流动，秘密值不进去是底线。
    """
    s = _session(x_tp_token, x_tp_role)
    require(s, "settings")
    if req.base_url is not None and req.base_url.strip() and not req.base_url.strip().startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="模型地址要以 http:// 或 https:// 开头")
    r = llm.save_cfg(base_url=req.base_url, model=req.model, api_key=req.api_key)
    if not r["changed"]:
        return {"ok": True, "changed": [], "note": "没有需要保存的修改（API Key 留空 = 不修改）。"}
    conn = db.connect(DB_PATH)
    try:
        # 审计只记字段名：base_url/model 属配置可记，api_key 只记"已更新"
        fields = "、".join(("api_key（值不记录）" if f == "api_key" else f) for f in r["changed"])
        db.add_audit(conn, "settings", "model-config", "update",
                     "", f"更新模型配置：{fields}", s["username"], s["role"])
    finally:
        conn.close()
    c2 = llm.load_cfg()
    note = "已保存并即生效（每次模型调用重新读取配置）。"
    if r["env_override"]:
        note += (" 注意：环境变量 " + "、".join(r["env_override"]) +
                 " 优先于文件值，当前进程里它会盖过刚保存的配置——"
                 "要么删掉环境变量，要么继续用环境变量管理。")
    return {"ok": True, "changed": r["changed"],
            "key_masked": llm.mask_key(c2.get("api_key")), "key_set": bool(c2.get("api_key")),
            "env_override": r["env_override"], "note": note}


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
        if getattr(req, "auto_insight_on_ingest", None) is not None:
            db.set_setting(conn, AUTO_INSIGHT_KEY, bool(req.auto_insight_on_ingest))
        after = _settings(conn)
        on_before = bool(before.get("gender_filter_enabled"))
        on_after = bool(after.get("gender_filter_enabled"))
        _ai_before = bool(before.get("auto_insight_on_ingest"))
        _ai_after = bool(after.get("auto_insight_on_ingest"))
        if before != after:
            for _k, _b, _a in (("gender_filter_enabled", on_before, on_after),
                               ("auto_insight_on_ingest", _ai_before, _ai_after)):
                if _b != _a:
                    db.add_audit(conn, "settings", _k, "update",
                                 f"{_k}={_b}", f"{_k}={_a}",
                                 s["username"], s["role"])
        _changed = []
        if on_after != on_before:
            _changed.append("性别筛选已开启（已写入审计）。它只改变列表的筛选条件，"
                            "不参与任何评分或分级。" if on_after
                            else "性别筛选已关闭，列表恢复为全部候选人。")
        if _ai_after != _ai_before:
            _changed.append("入库即分析已开启：新简历入库后自动分析一次（每人约 2-8 秒、"
                            "消耗模型额度）。" if _ai_after
                            else "入库即分析已关闭：新简历入库不再自动调模型，"
                                 "可在人才库点「分析待分析的人」批量补上（已写入审计）。")
        note = " ".join(_changed) if _changed else "设置未变化。"
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
                                  operator=s["username"], role=s["role"], apply=apply_now,
                                  use_llm=_llm_enabled())
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


# ============================================================
# v1.8 智能化三件套：入库即分析（在 ingest 里）/ 主动提案 / 每日摘要
# ============================================================

def _brief_use_llm() -> bool:
    """摘要是否允许调模型判断优先级。环境变量可关（自检用）。"""
    return os.environ.get("TP_BRIEF_LLM", "1") != "0"


def _llm_enabled() -> bool:
    """模型通道是否可用（配置里 enabled 且自检可关）。

    归岗判断与按尺子重算都会调模型；**模型不可用时不能硬猜岗位或档位**，
    必须走"待指定 / 待分析"的如实分支（v1.12 删掉规则打分后尤其重要：
    没有规则兜底，猜错就是纯错）。
    """
    if os.environ.get("TP_LLM", "1") == "0":
        return False
    try:
        from .agent import llm as _llm
        return bool((_llm.load_cfg() or {}).get("enabled", True))
    except Exception:                                          # noqa: BLE001
        return True


def _rebuild_brief_async() -> None:
    """后台补一次「带模型判断」的摘要（首屏先拿到规则版，不被模型超时拖住）。"""
    try:
        conn = db.connect(DB_PATH)
        try:
            payload = brief_mod.build_and_save(conn, use_llm=True)
            print(f"[daily] 摘要已刷新（{payload['source']}）：{payload['headline']}")
        finally:
            conn.close()
    except Exception as exc:  # noqa: BLE001 — 后台任务失败只记录
        print(f"[daily] 后台摘要刷新失败：{type(exc).__name__}: {exc}", file=sys.stderr)


@app.get("/api/brief/today")
def api_brief_today(x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                    x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """今日待办摘要。

    **首屏绝不等待模型**：库里没有今天的摘要时，先用规则版秒回，
    同时在后台跑一次带模型判断的版本，刷新后即见。
    否则模型不可达时（连接超时 180 秒）首屏会长时间空白——那比没有摘要更糟。
    """
    s = _session(x_tp_token, x_tp_role)
    require(s, "read")
    today = f"{datetime.now():%Y-%m-%d}"
    conn = db.connect(DB_PATH)
    try:
        saved = db.get_brief(conn, today)
        # 摘要结构升级（如 v1.10 起 each 条要带具体姓名）：老缓存没有新字段，
        # 直接返回会出现"页面还是旧措辞"，所以版本不一致就重算一次。
        if saved and (saved.get("payload") or {}).get("brief_version") == brief_mod.BRIEF_VERSION:
            return {"ok": True, "cached": True, **(saved.get("payload") or {})}
        payload = brief_mod.build(conn, use_llm=False)
        db.save_brief(conn, today, payload)
    finally:
        conn.close()
    if _brief_use_llm():
        threading.Thread(target=_rebuild_brief_async, name="brief-llm", daemon=True).start()
    return {"ok": True, "cached": False, **payload,
            "note": "首次生成：先给出规则版，模型判断版稍后刷新可见。"}


@app.post("/api/brief/rebuild")
def api_brief_rebuild(use_llm: bool = False,
                      x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                      x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """重算今日摘要。`use_llm=1` 时同步调模型（HR 主动点，愿意等）。"""
    s = _session(x_tp_token, x_tp_role)
    require(s, "read")
    conn = db.connect(DB_PATH)
    try:
        payload = brief_mod.build_and_save(conn, use_llm=bool(use_llm) and _brief_use_llm())
        return {"ok": True, **payload}
    finally:
        conn.close()


@app.post("/api/proactive/scan")
def api_proactive_scan(stuck_days: int = 7,
                       x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                       x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """手动触发一次系统巡检，产出待确认提案。

    巡检**只建提案不执行**——产出的每一条都要 HR 点确认才生效。
    同一件事 7 天内不会重复提（去重窗口），避免把提案列表变成噪音。
    """
    s = _session(x_tp_token, x_tp_role)
    require(s, "read")
    conn = db.connect(DB_PATH)
    try:
        r = proactive_mod.scan_and_propose(conn, stuck_days=int(stuck_days))
        return {"ok": True, "created_count": r["created_count"],
                "skipped": r["skipped"], "created": r["created"],
                "scanned": r["scanned"], "session_id": r["session_id"],
                "note": ("已产出 %d 条待确认提案，到「提案」列表逐条确认或拒绝。"
                         % r["created_count"]) if r["created_count"]
                        else "本轮没有需要新建的提案（可能已提过且未超过去重窗口）。"}
    finally:
        conn.close()


def _backfill_grad_date(limit: int = 500) -> int:
    """给还没算出毕业时间的候选人补一次（以解析原文为准）。

    为什么单独补：入库路径拿到的文本可能已被处理过，识别不到"毕业"字样；
    而 `documents.raw_text` 是解析原文，一定带着教育经历。启动时补一次，
    新导入的人下次启动也会被补齐——与"存量补分析"同一套兜底思路。
    """
    from .pipeline import freshness
    conn = db.connect(DB_PATH)
    n = 0
    try:
        rows = conn.execute(
            """SELECT c.id, d.raw_text FROM candidates c
               JOIN documents d ON d.candidate_id = c.id
               WHERE COALESCE(c.grad_date, '') = '' AND COALESCE(d.raw_text, '') != ''
               ORDER BY c.id LIMIT ?""", (int(limit),)).fetchall()
        for r in rows:
            gd = freshness.find_grad_date(r["raw_text"])
            if gd:
                conn.execute("UPDATE candidates SET grad_date = ? WHERE id = ?",
                             (gd, r["id"]))
                n += 1
        conn.commit()
        if n:
            db.add_audit(conn, "candidate", "*", "backfill_grad_date", "",
                         f"为 {n} 位候选人补齐毕业时间（用于判定应届/往届）",
                         "system", "system")
    finally:
        conn.close()
    return n


def _backfill_insights(limit: int = 50) -> int:
    """给"还没有分析结果"的存量投递补一次分析，返回补做条数。

    为什么必须有这一步：入库钩子只对**新入库**生效，而升级到 v1.8 的已有库里
    所有历史投递都没有分析结果——HR 打开界面会看到一片"生成中"，
    看起来像功能没生效。这里按"最新优先"补一批，已有的自然跳过
    （`LEFT JOIN ... IS NULL`），所以重复启动不会重复算。
    """
    if os.environ.get("TP_AUTO_INSIGHT", "1") == "0":
        return 0
    conn = db.connect(DB_PATH)
    try:
        rows = conn.execute(
            """SELECT a.id, a.candidate_id FROM applications a
               JOIN candidates c ON c.id = a.candidate_id
               LEFT JOIN candidate_insights i
                      ON i.candidate_id = a.candidate_id AND i.application_id = a.id
               WHERE i.id IS NULL AND COALESCE(c.archived_at, '') = ''
               ORDER BY a.id DESC LIMIT ?""", (int(limit),)).fetchall()
    finally:
        conn.close()
    if not rows:
        return 0
    report = {"details": [{"status": "added", "candidate_id": r[1],
                           "application_id": r[0]} for r in rows]}
    return ingest_mod.spawn_auto_analysis(DB_PATH, report, limit=int(limit))


@app.on_event("startup")
def _startup_daily_task() -> None:
    """每日业务巡检：补存量分析 + 生成今日摘要 + 产出主动提案。

    与 `_startup_purge_task` 同一套思路（不依赖外部 cron、daemon 线程随进程退出、
    失败只打印不阻断启动），区别是**只做"看"与"提建议"，不删除任何东西**。

    启动时**不阻塞**：任务丢进后台线程跑——模型不可达时单次调用要等连接超时，
    若同步执行会把启动拖住几分钟。
    """
    if os.environ.get("TP_DAILY_TASK", "1") == "0":
        return

    def _run_once() -> None:
        try:
            g = _backfill_grad_date()
            if g:
                print(f"[daily] 为 {g} 位候选人补齐毕业时间")
        except Exception as exc:  # noqa: BLE001
            print(f"[daily] 补齐毕业时间失败（不影响使用）：{exc}", file=sys.stderr)
        try:
            n = _backfill_insights()
            if n:
                print(f"[daily] 为 {n} 条存量投递补做分析")
        except Exception as exc:  # noqa: BLE001
            print(f"[daily] 补做分析失败（不影响使用）：{exc}", file=sys.stderr)
        try:
            conn = db.connect(DB_PATH)
            try:
                if not db.get_brief(conn, f"{datetime.now():%Y-%m-%d}"):
                    payload = brief_mod.build_and_save(conn, use_llm=_brief_use_llm())
                    print(f"[daily] 今日摘要：{payload['headline']}（{payload['source']}）")
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            print(f"[daily] 摘要生成失败（不影响使用）：{exc}", file=sys.stderr)
        try:
            conn = db.connect(DB_PATH)
            try:
                r = proactive_mod.scan_and_propose(conn)
                if r["created_count"]:
                    print(f"[daily] 系统巡检产出 {r['created_count']} 条待确认提案"
                          f"（跳过 {r['skipped']} 条重复）")
            finally:
                conn.close()
        except Exception as exc:  # noqa: BLE001
            print(f"[daily] 巡检失败（不影响使用）：{exc}", file=sys.stderr)

    def _loop() -> None:
        while True:
            time.sleep(24 * 3600)
            _run_once()

    threading.Thread(target=_loop, name="daily-task", daemon=True).start()
    threading.Thread(target=_run_once, name="daily-first", daemon=True).start()


# ============================================================
# 决策反馈闭环（v1.8.2）：把 HR 的定档变成对系统的反馈
# ============================================================

@app.get("/api/feedback/report")
def api_feedback_report(days: int = 90,
                        x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                        x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """口径偏差报告（系统建议 vs HR 决定）。

    **只读**：不修改任何权重文件。样本不足时如实说明，不硬下结论。
    """
    s = _session(x_tp_token, x_tp_role)
    require(s, "read")
    conn = db.connect(DB_PATH)
    try:
        return {"ok": True, **feedback_mod.decision_report(conn, days=int(days))}
    finally:
        conn.close()


@app.get("/api/feedback/export")
def api_feedback_export(days: int = 90,
                        x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                        x_tp_role: str | None = Header(default=None, alias="X-TP-Role")):
    """导出偏差明细 CSV。

    导出要留痕：这是"数据离开系统"的动作，与查看不同——
    谁在什么时候把候选人数据导出去了，审计里必须答得上来。
    """
    s = _session(x_tp_token, x_tp_role)
    require(s, "read")
    conn = db.connect(DB_PATH)
    try:
        text = feedback_mod.export_csv(conn, days=int(days))
        db.add_audit(conn, "feedback", "report", "export", "",
                     f"{s['username']} 导出偏差明细 CSV（最近 {days} 天）",
                     s["username"], s["role"])
    finally:
        conn.close()
    name = urllib.parse.quote(f"口径偏差明细-{datetime.now():%Y%m%d}.csv")
    return Response(content=text, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename*=UTF-8''{name}"})


# ============================================================
# 邮件（v1.8.3）：模板 + 草稿 + 发送
# ============================================================
# 口径：**生成全自动，发送一律人工确认**。界面上不提供"自动发送"开关。
# 因此 /draft（纯计算、无副作用）与 /send（不可逆）是两个接口 ——
# 合成一个就等于给了"不小心点一下就发出去"的机会。

class MailTemplateReq(BaseModel):
    id: int | None = None
    name: str
    scene: str | None = None
    subject: str | None = None
    body: str | None = None


class SmtpReq(BaseModel):
    host: str | None = None
    port: int | None = None
    ssl: bool | None = None
    user: str | None = None
    from_name: str | None = None
    reply_to: str | None = None
    password: str | None = None          # 写 imap.secret，不回显


class MailDraftReq(BaseModel):
    candidate_id: int | None = None
    application_id: int | None = None
    template_id: int | None = None
    subject: str | None = None           # 没有模板时直接用这两项
    body: str | None = None
    runtime: dict | None = None          # 面试时间/地点等运行时变量


class MailSendReq(BaseModel):
    to: str
    subject: str
    body: str = ""                       # 纯文本正文（给了 html 时可留空，服务端会拆出来）
    html: str | None = None              # 富文本正文（所见即所得编辑器产出的 HTML）
    candidate_id: int | None = None
    application_id: int | None = None
    template_name: str | None = None


class MailPreviewReq(BaseModel):
    """预览请求：**只有正文**。

    之前直接复用了 MailSendReq，`to`/`subject` 是必填 → 预览请求被判 422，
    按钮点了什么都不发生（实测反馈）。预览跟收件人无关，就不该要求这些字段。
    """
    body: str = ""
    html: str | None = None


@app.get("/api/mail/templates")
def api_mail_templates(x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                       x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    _session(x_tp_token, x_tp_role)
    conn = db.connect(DB_PATH)
    try:
        return {"items": db.list_mail_templates(conn),
                "builtin_vars": mail_template_mod.BUILTIN_VARS,
                "runtime_vars": mail_template_mod.RUNTIME_VARS,
                "scenes": mail_template_mod.SCENES,
                "note": "变量写在花括号里，如 {姓名}、{应聘岗位}。取不到值的变量会显示成"
                        "【待填：xxx】，提醒你补上——不会静默留空。"}
    finally:
        conn.close()


@app.post("/api/mail/templates")
def api_mail_template_save(req: MailTemplateReq,
                           x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                           x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """新增/更新模板。模板是人写的，系统只做变量替换。"""
    s = _session(x_tp_token, x_tp_role)
    require(s, "settings")
    name = (req.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="模板名不能为空")
    conn = db.connect(DB_PATH)
    try:
        existing = [t for t in db.list_mail_templates(conn)
                    if t["name"] == name and t["id"] != (req.id or 0)]
        if existing:
            raise HTTPException(status_code=400,
                                detail=f"已有同名模板「{name}」，换个名字或直接编辑那一条")
        tid = db.upsert_mail_template(conn, name, (req.scene or "其他通知").strip(),
                                      req.subject or "", req.body or "", req.id)
        db.add_audit(conn, "settings", f"mail_template#{tid}", "update", "",
                     f"{s['username']} 保存邮件模板「{name}」", s["username"], s["role"])
        return {"ok": True, "id": tid, "template": db.get_mail_template(conn, tid)}
    finally:
        conn.close()


@app.post("/api/mail/templates/{tid}/delete")
def api_mail_template_delete(tid: int,
                             x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                             x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    s = _session(x_tp_token, x_tp_role)
    require(s, "settings")
    conn = db.connect(DB_PATH)
    try:
        t = db.get_mail_template(conn, tid)
        if not t:
            raise HTTPException(status_code=404, detail="模板不存在")
        db.delete_mail_template(conn, tid)
        db.add_audit(conn, "settings", f"mail_template#{tid}", "delete",
                     t.get("name") or "", "", s["username"], s["role"])
        return {"ok": True, "note": "模板已删除。已发出的邮件不受影响（发送记录里存的是当时的正文）。"}
    finally:
        conn.close()


@app.get("/api/mail/smtp")
def api_smtp_get(x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                 x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """发信配置（口令只回是否已设置，不回显）。"""
    _session(x_tp_token, x_tp_role)
    conf = mail_send_mod.load_smtp()
    return {**conf, "presets": mail_send_mod.SMTP_PRESETS,
            "note": "163 邮箱需先开启 SMTP 服务并生成授权码（不是登录密码）；"
                    "收信与发信用同一个授权码，填一次即可。"}


@app.post("/api/mail/smtp")
def api_smtp_save(req: SmtpReq,
                  x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                  x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    s = _session(x_tp_token, x_tp_role)
    require(s, "settings")
    updates: dict = {}
    if req.host is not None:
        updates["host"] = req.host.strip()
    if req.port is not None:
        updates["port"] = int(req.port)
    if req.ssl is not None:
        updates["ssl"] = bool(req.ssl)
    if req.user is not None:
        updates["user"] = req.user.strip()
    if req.from_name is not None:
        updates["from_name"] = req.from_name.strip()
    if req.reply_to is not None:
        updates["reply_to"] = req.reply_to.strip()
    if updates:
        mb.save_config({"smtp": updates})
    if req.password:
        mb.write_secret(req.password)
    conf = mail_send_mod.load_smtp()
    db_path_conn = db.connect(DB_PATH)
    try:
        db.add_audit(db_path_conn, "settings", "smtp", "update", "",
                     f"{s['username']} 更新发信配置（{conf['host']}:{conf['port']}）",
                     s["username"], s["role"])
    finally:
        db_path_conn.close()
    return {"ok": True, **conf,
            "note": "已保存。授权码不会回显，也不写进日志。"}


@app.post("/api/mail/test-smtp")
def api_smtp_test(x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                  x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """检查发信配置是否就绪：真连一次 SMTP 并登录验证凭据，**不会真的发信**。"""
    s = _session(x_tp_token, x_tp_role)
    require(s, "settings")
    return mail_send_mod.check_smtp()


@app.post("/api/mail/preview")
def api_mail_preview(req: MailPreviewReq,
                     x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                     x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """只做一件事：把当前正文转成 HTML，给界面**实时显示收件人看到的样子**。

    两种入口：① 富文本编辑器把 HTML 直接传进来（照原样回，所见即所得）；
    ② 只给纯文本（含 `| 列 |` 表格写法）→ 转成 HTML 再回。
    刻意**不做变量替换**（那是 `/api/mail/draft` 的活）：这里预览的就是
    "我写了什么、发出去长什么样"，顺手替换一遍变量反而与真实发送不一致。
    """
    _session(x_tp_token, x_tp_role)
    if req.html:
        html = mail_template_mod.sanitize_email_html(req.html)
        return {"ok": True, "html": html, "rich": True,
                "text": mail_template_mod.html_to_text(html),
                "note": "按 HTML 邮件发送（同时附带纯文本版本，兼容不显示 HTML 的客户端）。"}
    text = req.body or ""
    rich = mail_template_mod.needs_html(text)
    return {"ok": True, "html": mail_template_mod.to_html(text), "rich": rich, "text": text,
            "note": ("正文里检测到表格/粗体，将按 HTML 邮件发送"
                     "（同时附带纯文本版本，兼容不显示 HTML 的客户端）。"
                     if rich else
                     "正文是纯文字，仍按纯文本发送。要出表格，点「插入表格」直接画。")}


@app.post("/api/mail/draft")
def api_mail_draft(req: MailDraftReq,
                   x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                   x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """生成草稿（**纯计算，不发送、不留痕**）：模板 + 候选人数据 → 主题与正文。

    `missing` 里是没取到值的变量——界面要把它高亮出来，提醒 HR 补填。
    """
    s = _session(x_tp_token, x_tp_role)
    require(s, "chat")
    conn = db.connect(DB_PATH)
    try:
        cand, job_title = {}, ""
        if req.candidate_id:
            d = db.candidate_detail(conn, req.candidate_id)
            if not d:
                raise HTTPException(status_code=404, detail="未找到该候选人")
            cand = auth.present_candidate(d)       # 解密联系方式（发信要用真实邮箱）
            jd, meta = db.resolve_candidate_job(conn, d)
            job_title = (meta or {}).get("title") or ""
        subject_tpl, body_tpl = req.subject or "", req.body or ""
        tpl_name = ""
        if req.template_id:
            t = db.get_mail_template(conn, req.template_id)
            if not t:
                raise HTTPException(status_code=404, detail="模板不存在")
            subject_tpl, body_tpl, tpl_name = t["subject"] or "", t["body"] or "", t["name"]
        ctx = mail_template_mod.build_ctx(cand, job_title, s.get("display_name") or s["username"],
                                          req.runtime)
        sj = mail_template_mod.render(subject_tpl, ctx)
        bd = mail_template_mod.render(body_tpl, ctx)
        missing = sorted(set(sj["missing"]) | set(bd["missing"]))
        # 富文本正文：**总是给一份 HTML**——写邮件页是所见即所得编辑器，
        # 它需要 HTML 来填内容。两种来源：
        #   ① 模板本身就是 HTML（复杂版式：合并单元格、居中标题）→ 原样用；
        #   ② 极简写法（`| 列 | 列 |` 表格 / `**加粗**`）→ 转成 HTML。
        body_text = bd["text"]
        body_html = (body_text if mail_template_mod.looks_like_html(body_text)
                     else mail_template_mod.to_html(body_text))
        return {"ok": True, "template_name": tpl_name,
                "subject": sj["text"], "body": body_text,
                "body_html": body_html,
                "rich": mail_template_mod.looks_like_html(body_text)
                        or mail_template_mod.needs_html(body_text),
                "missing": missing,
                "to": cand.get("email") if cand.get("email") not in (None, "—", "") else "",
                "candidate": {"id": cand.get("id"), "name": cand.get("name"),
                              "email": cand.get("email") if cand.get("email") != "—" else "",
                              "job": job_title},
                "note": ("有变量没取到值，已在正文里标成【待填：xxx】，发送前请补上。"
                         if missing else "草稿已生成，确认无误后点「确认发送」。")}
    finally:
        conn.close()


@app.post("/api/mail/send")
def api_mail_send(req: MailSendReq,
                  x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                  x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """**真正发信**。需要 `confirm` 权限；成功与否都留痕。

    内容里若还残留 `【待填：...】` 会被拦下——宁可让 HR 补一句，也不发半成品出去。
    正文用了表格/粗体标记时按 **multipart/alternative** 发（HTML + 纯文本双版本），
    转换只在这里做一次：模板渲染 → 转 HTML → 发送，**单一真源**，
    避免前后端各转一遍导致"预览和收到的不是一回事"。
    """
    s = _session(x_tp_token, x_tp_role)
    require(s, "confirm")
    html_body = (mail_template_mod.sanitize_email_html(req.html) if req.html
                 else (req.body if mail_template_mod.looks_like_html(req.body)
                       else (mail_template_mod.to_html(req.body)
                             if mail_template_mod.needs_html(req.body) else None)))
    # 纯文本部分：编辑器会给 innerText，但表格会被压成制表符；统一由服务端从 HTML
    # 拆一份"表格按 `列1 | 列2` 排"的版本，纯文本客户端读起来才是表而不是一坨。
    plain = mail_template_mod.html_to_text(html_body) if html_body else (req.body or "")
    if "【待填：" in (req.subject or "") or "【待填：" in plain or "【待填：" in (html_body or ""):
        raise HTTPException(status_code=400,
                            detail="正文里还有【待填：xxx】没补上，请先填好再发送")
    r = mail_send_mod.send_mail(req.to, req.subject, plain, html=html_body)
    conn = db.connect(DB_PATH)
    try:
        db.add_audit(conn, "mail", str(req.candidate_id or ""), "send",
                     "", f"发送邮件给 {req.to}｜主题：{req.subject}"
                         f"｜模板：{req.template_name or '（无）'}｜{'成功' if r.get('ok') else '失败'}",
                     s["username"], s["role"])
        if r.get("ok") and req.application_id:
            db.add_note(conn, int(req.application_id),
                        f"已发送邮件：{req.subject}（模板：{req.template_name or '无'}）",
                        s["username"], s["role"])
    finally:
        conn.close()
    if not r.get("ok"):
        raise HTTPException(status_code=400, detail=r.get("error") or "发送失败")
    return {"ok": True, **r, "note": "邮件已发出，动作已写入审计。"}
