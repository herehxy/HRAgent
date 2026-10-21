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
设置                     `/api/settings`（GET/POST；入库即分析开关、超期天数等）
检索                     `/api/search/skills` `/api/search/semantic` `/api/search/similar` `/api/search/index` `/api/search/status`
智能体                   `/api/agent/chat` `/api/agent/tools` `/api/agent/runs` `/api/agent/status`
                        `/api/candidates/{cid}/analyze|interview|explain`
提案与审计               `/api/proposals` `/api/proposals/{pid}/decide` `/api/audit`
系统                     `/api/ontology` `/api/policy` `/api/health`
======================  ==========================================================================

注意：检索三条路径的返回值会经 `_with_contact()` 处理后下发——
联系方式解密只在这一层做（与人才库列表同一口径），密文/盲索引/身份键不下发。

**性别**：`/api/candidates` 支持 `gender=男|女|未标注`，按**普通筛选条件**处理
（v1.29.4，业务方 2026-10-16 定调：性别不做脱敏、不设"红线"开关）。
性别永远只来自简历**明写**的标签行（绝不推断），也不进入 `tier.grade()`，不参与评分/分级。
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
import traceback
import urllib.parse
import uuid
import zipfile
# 注：v1.24.8 起批量分析全部改串行，已不再使用 ThreadPoolExecutor，
# 故此处不再 import —— 留着会让人以为"还能开并发"。
from datetime import datetime

from fastapi import Body, FastAPI, Header, HTTPException, Request, Query
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from . import actions, auth, db, feedback as feedback_mod
from . import ingest as ingest_mod, mailbox as mb, regrade, search
from . import mail_send as mail_send_mod
from . import mail_template as mail_template_mod
from . import univ as univ_mod
# v1.29：阶段/结论的唯一真源。服务端要下发阶段配置给前端（看板与下拉都从这里来），
# 并对阶段流转做校验，所以是顶层导入。
from . import stages as stages_mod
# v1.29：档位（字母/标签/硬门槛档）的唯一真源 —— 文案与校验都从这里取，
# 不再写死 "A/B/C/D"（三档后写死的 D 会把提示语说错）
from . import tiers as tiers_mod
# v1.29：导出（xlsx/CSV）与简历命名
from . import export as export_mod
from . import xlsx as xlsx_mod
from . import avatar as avatar_mod
from .agent import brief as brief_mod
from .agent import llm
from .agent import proactive as proactive_mod
from .agent.loop import run_agent
from .agent.tools import ToolCtx, catalog as tool_catalog
from .pipeline import domains as domains_mod
from .pipeline import majors as mj
from .pipeline import normalize as nz
from .pipeline import parse as parse_mod
from .pipeline import extract as extract_mod
from .pipeline import freshness
from .pipeline import sanitize
from .pipeline import tier as tier_mod
from .pipeline.analyze import analyze_fit, draft_interview
from .ui import render_page, ui_build
from .ui_v2 import render_page_v2

#: 程序根目录。默认按文件位置推导；**TP_HOME 可覆盖**——
#: 开发/测试时用它把源码版指向已安装目录（_internal/），
#: 这样源码跑起来用的是同一套 data/ 与 config/，测的就是真实数据。
from .paths import BASE      # 根目录唯一来源（认 TP_HOME）
from .paths import DATA_DIR  # 数据目录（认 TP_DATA_DIR，可能不在 BASE 下）
from .paths import cfg as _cfg_path
from .paths import resolve_data_path
from .version import VERSION as PRODUCT_VERSION
# 允许用 TP_DB_PATH 指向另一个库：演练、验收、接口实测时不必动真实人才库
DB_PATH = os.environ.get("TP_DB_PATH") or os.path.join(DATA_DIR, "workbench.db")
JD_PATH = _cfg_path("jd.json")
TIERS_PATH = _cfg_path("tiers.json")
#: v2.0.4：从简历里抽出来的证件照落这里（相对 BASE 的 data/avatars/）
AVATAR_DIR = os.path.join(DATA_DIR, "avatars")
RESUME_DIR = os.path.join(DATA_DIR, "resumes")
# 回收目录：界面上"删除来源文件"只是**移入这里**，随时可恢复。
# 刻意与 data/archive（原件区，只增不改）分开——两个目录的不变式完全不同，
# 混在一起会让"原件区只增不改"这条红线变得无法核验。
REMOVED_DIR = os.path.join(DATA_DIR, "removed")


def _rel_or_abs(path: str) -> str:
    """相对 BASE 表示路径；跨盘或带一堆 .. 时改给绝对路径。"""
    try:
        rel = os.path.relpath(path, BASE)
    except ValueError:            # 跨盘符（relpath 直接抛 ValueError）
        return path
    return path if rel.startswith("..") else rel


def _abs_stored(rel: str) -> str:
    """把库里存的路径还原成绝对路径。

    库里的路径有两种形态并存（老数据是 `data/archive/xxx` 这样的相对路径，
    新写入的可能是绝对路径）。相对路径里 `data/` 开头的要走 DATA_DIR ——
    数据目录被挪走（TP_DATA_DIR）后，按 BASE 拼就会指回只读的包内目录，
    于是"文件明明在那儿却读不到"。其余相对路径仍按 BASE 拼，行为不变。
    """
    return resolve_data_path(rel)


def _inside_allowed_root(path: str) -> bool:
    """原件只允许来自程序资源根或用户数据根。"""
    real = os.path.realpath(path)
    for root in (BASE, DATA_DIR):
        normalized = os.path.realpath(root)
        if real == normalized or real.startswith(normalized + os.sep):
            return True
    return False


app = FastAPI(title="NIN人才库智能助手", version=PRODUCT_VERSION)


# ---- 本机来源校验（防 DNS rebinding / 本地 CSRF）----
# 服务只监听 127.0.0.1，本机内网工具。恶意网页可把自己的域名解析到 127.0.0.1
# （DNS rebinding）或从任意页面发跨站表单（本地 CSRF）打到本机接口。
# 对「会改变状态」的请求校验 Host / Origin / Referer，非本机来源一律 403。
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "[::1]"}


def _is_local_host(host: str | None) -> bool:
    if not host:
        return False
    # 去掉端口
    h = host.split(":", 1)[0].lower() if not host.startswith("[") else host.lower()
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    else:
        h = host.rsplit(":", 1)[0].lower()
    return h in _LOCAL_HOSTS


@app.middleware("http")
async def _guard_local_origin(request: Request, call_next):
    """状态变更请求（非 GET/HEAD/OPTIONS）必须来自本机。

    只拦破坏性方法：GET 是只读接口（且走 require() 鉴权），跨站 <img>/<script>
    只能发 GET，拦 POST/PUT/DELETE/PATCH 即挡住 CSRF 的写路径。

    - DNS rebinding：浏览器把 evil.com 解析到 127.0.0.1 后，请求带 `Host: evil.com`，
      据此拒绝（Host 缺失时不拦——真实浏览器必带 Host，缺失只发生在测试/畸形请求）。
    - 本地 CSRF：跨站表单 POST 会带 `Origin: https://evil.com`，据此拒绝。
    """
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        host = request.headers.get("host") or ""
        # Host 存在且非本机 → 拒绝（DNS rebinding）
        if host and not _is_local_host(host):
            return Response(status_code=403, content="Forbidden: 仅接受本机请求")
        # 若带了 Origin/Referer，其主机也必须是本机（跨站表单会带外部 Origin）
        origin = request.headers.get("origin")
        if origin:
            try:
                from urllib.parse import urlparse
                ohost = urlparse(origin).hostname or ""
                if not _is_local_host(ohost):
                    return Response(status_code=403, content="Forbidden: 跨站来源被拒绝")
            except Exception:
                return Response(status_code=403, content="Forbidden: 非法来源")
    return await call_next(request)


# ---- 后台任务进度（v1.24.9）：导入/分析这类耗时操作，前端轮询这里拿实时进度 ----
# 进程内内存表：{task_id: {"title","total","done","failed","status","message"}}
# status: running | done | error。分析线程每完成一人就更新 done/failed，
# 前端 `/api/tasks/{task_id}` 轮询，弹窗里画进度条。
_PROGRESS: dict[str, dict] = {}
_PROGRESS_LOCK = threading.Lock()
#: 邮件请求幂等表：client_request_id -> task_id。网络中断后前端重试同一封邮件时，
#: 后端返回原任务，而不是再发一封。
_MAIL_REQ: dict[str, str] = {}
_MAIL_REQ_LOCK = threading.Lock()


def _progress_new(title: str, total: int, *, kind: str = "general",
                  meta: dict | None = None) -> str:
    """开一个新任务，返回 task_id。"""
    tid = uuid.uuid4().hex[:12]
    with _PROGRESS_LOCK:
        _PROGRESS[tid] = {"title": title, "total": total, "done": 0,
                          "failed": 0, "status": "running", "message": "",
                          "started_at": _time.time(), "kind": kind,
                          "meta": meta or {}}
    return tid


def _progress_set(tid: str, *, done: int | None = None,
                  failed: int | None = None, status: str | None = None,
                  message: str | None = None,
                  extra: dict | None = None) -> None:
    """更新任务进度（缺省字段不动）。"""
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
        if message is not None:
            p["message"] = message
        if extra:
            p.update(extra)


@app.get("/api/tasks/active")
def api_tasks_active(kind: str | None = None, include_done: bool = False,
                     x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                     x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """当前后台任务；静态路径必须放在 `/api/tasks/{task_id}` 前面。

    除了可停止的收取/导入任务，也返回邮件发送任务。页面刷新或误关进度窗后，
    前端可以靠这里恢复轮询，而不是重新提交一封邮件。
    """
    _session(x_tp_token, x_tp_role)
    wanted = str(kind or "").strip()
    items: list[dict] = []
    with _PROGRESS_LOCK:
        for tid, p in _PROGRESS.items():
            if wanted and p.get("kind") != wanted:
                continue
            if not wanted and p.get("kind") not in ("ingest",):
                continue
            if not include_done and p.get("status") != "running":
                continue
            item = dict(p)
            item["task_id"] = tid
            item.setdefault("kind", "general")
            items.append(item)
    # 收取/导入任务由 ingest 模块维护独立进度表；原先这里只有它。
    if not wanted or wanted == "ingest":
        for t in (ingest_mod.list_running() if hasattr(ingest_mod, "list_running") else []):
            item = dict(t)
            item.setdefault("kind", "ingest")
            items.append(item)
    items.sort(key=lambda x: x.get("started_at") or 0, reverse=True)
    return {"ok": bool(items), "task": (items[0] if items else None),
            "tasks": items, "count": len(items)}


@app.get("/api/tasks/{task_id}")
def api_task_progress(task_id: str,
                      x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                      x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """查后台任务进度。前端轮询（每 1~2 秒一次）画进度条。

    返回 {ok, task_id, title, total, done, failed, status, message, percent}。
    status：running=进行中；done=全部完成；error=中断出错。

    有两张进度表：server 模块级（批量重跑/手动分析）与 ingest 模块级
    （入库后自动分析，后台线程跑的）。这里两张都查。
    """
    _session(x_tp_token, x_tp_role)
    with _PROGRESS_LOCK:
        p = _PROGRESS.get(task_id)
    if not p:
        # 自动分析的任务登记在 ingest 模块的进度表里
        p = ingest_mod.get_progress(task_id) if hasattr(ingest_mod, "get_progress") else None
    if not p:
        return {"ok": False, "task_id": task_id, "error": "任务不存在或已过期"}
    total = p.get("total") or 0
    done = p.get("done") or 0
    # v1.29.4：0 总数不再一律 100%（收取任务"统计数量"阶段会短暂 total=0，
    # 显示满格会让人以为已经收完了）；只有已完成的任务才给 100。
    percent = int(done / total * 100) if total else (100 if p.get("status") == "done" else 0)
    out = {"ok": True, "task_id": task_id, "title": p.get("title"),
           "total": total, "done": done, "failed": p.get("failed") or 0,
           "status": p.get("status"), "message": p.get("message"),
           "percent": max(0, min(100, percent))}
    # v1.29.4：收取任务把**完整报表**与停止标记挂在进度里带回
    # （最后一次轮询就能拿全，前端不需要再来一次结果请求）。
    for _k in ("report", "stopped"):
        if _k in p:
            out[_k] = p[_k]
    if "result" in p:
        out["result"] = p["result"]
    return out


@app.post("/api/tasks/{task_id}/stop")
def api_task_stop(task_id: str,
                  x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                  x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """请求停止一个**可停止**的后台任务（收取/导入）。

    协作式停止：执行方在"每处理完一份文件/一封邮件"的边界检查，
    读完当前这份才停 —— 不会把半份砍断，游标停在最后成功处理的那条
    （下次从那里继续，不重复、不丢件）。
    """
    s = _session(x_tp_token, x_tp_role)
    require(s, "ingest")
    ok = bool(ingest_mod.request_stop(task_id))
    return {"ok": ok,
            "message": ("已请求停止：读完当前文件后停下" if ok
                        else "该任务不可停止（可能已结束，或不是收取任务）")}


def _load(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _jd_tiers() -> tuple[dict, dict]:
    return _load(JD_PATH), _load(TIERS_PATH)


# ---------------------------------------------------------------- 设置项

# v1.29.4（业务方 2026-10-16）：性别**按正常字段处理** —— 原「合规开关」
# `gender_filter_enabled` 已随本批移除（连同设置项与前端抑制逻辑）。
# 不可动摇的两条仍在：性别只来自简历**明写**的标签行（绝不推断）、
# 永远不进入 `tier.grade()` 评分。

# 「入库即分析」开关（v1.13.4）。**产品默认开**：进门就有判断是这个工具的卖点；
# 但它每次都会调模型（一次 2-8 秒 + 额度），所以必须给 HR 一个显式开关，
# 让他按自己的额度与节奏决定"入库就分析"还是"攒一批手动补"。
# 关掉不影响任何其它功能：分析结论照样写 candidate_insights，只是改由 HR 点按钮触发。
AUTO_INSIGHT_KEY = "auto_insight_on_ingest"


def _settings(conn) -> dict:
    return {"auto_insight_on_ingest": bool(db.get_setting(conn, AUTO_INSIGHT_KEY, True)),
            "pipeline_overdue_days": int(db.get_setting(conn, "pipeline_overdue_days", 15) or 15)}


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
                      "permissions": auth.permissions("hr"), "mode": "local",
                      "anonymous": False})
        return s
    finally:
        conn.close()


def require(session: dict, perm: str) -> None:
    # 匿名（无 token / token 失效）没有权限，登录模式下必须挡在入口——
    # 否则 resolve() 若回退成 hr 全权限，不带令牌就能删库/导出。
    if session.get("anonymous"):
        raise HTTPException(status_code=401, detail="未登录或会话已失效")
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
    """首页 = **v2 主从两栏版**（v2.2.0 起）。

    旧版单页仍在 `/v1`，随时可切回（改这一行即可：把 render_page_v2 换回
    render_page，并把 /v1 的内容对调）。

    必须带 `Cache-Control: no-store`（v1.14.3）：JS 是内联的，所以浏览器一旦缓存了
    这个 HTML，就等于缓存了旧版前端——HR 会看到"我明明改了却没反应"（当天反复遇到）。
    这个工具是单机自用、页面只有几十 KB，不值得为它做缓存。
    """
    resp = HTMLResponse(render_page_v2(auth_enabled=_auth_enabled()))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.get("/v1", response_class=HTMLResponse)
def index_v1() -> Response:
    """**旧版**单页（保留为回退入口，不是默认）。"""
    resp = HTMLResponse(render_page(auth_enabled=_auth_enabled()))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


@app.get("/v2", response_class=HTMLResponse)
def index_v2() -> Response:
    """v2 页面（`/` 的别名，两个地址都能开）。

    设计取向写在 `app/ui_v2.py` 的模块 docstring 里：从"卡片流"改成"主从两栏"，
    一屏内完成 找人 → 判断 → 推进；证据默认可见；单一强调色。
    """
    resp = HTMLResponse(render_page_v2(auth_enabled=_auth_enabled()))
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
            "product_version": PRODUCT_VERSION,
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
                        # 发件人显示名与发信账号：放进 meta 是为了让界面在
                        # 「已发送」凭据里能写清"是以什么名义发的"。
                        # 这两项都是公开信息（不像密码要脱敏）。
                        "from_name": (cfg.get("smtp") or {}).get("from_name") or "",
                        "smtp_user": (cfg.get("smtp") or {}).get("user") or "",
                        "readonly": (cfg.get("imap") or {}).get("readonly", True),
                        "attachment_ext": cfg.get("attachment_ext"),
                        "max_attachment_mb": cfg.get("max_attachment_mb", 20),
                        "same_job_reapply_days": (cfg.get("dedup") or {}).get("same_job_reapply_days")},
            "settings": _settings(conn),
            # 数据存放（「系统说明」页展示）：尽量给相对路径，换机器部署口径不变；
            # 但数据目录被挪走时（TP_DATA_DIR，见 app/paths.py）相对 BASE 会得到
            # 一串 ../..，那对用户毫无意义，所以这种情况直接展示绝对路径。
            "storage": {
                "base": os.path.basename(BASE),
                "db": _rel_or_abs(DB_PATH),
                "archive_dir": _rel_or_abs(os.path.join(DATA_DIR, "archive")),
                "resume_dir": _rel_or_abs(RESUME_DIR),
                "removed_dir": _rel_or_abs(REMOVED_DIR),
                "mail_dir": _rel_or_abs(os.path.join(DATA_DIR, "mail_in")),
                "backup_dir": _rel_or_abs(os.path.join(DATA_DIR, "backup")),
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


#: 单个上传的体积上限。简历是 PDF/Word/图片，正常几 MB 以内；
#: 留 50MB 是给扫描版高清图，超过就没意义了（也挡住"打满内存/磁盘"这条路）。
MAX_UPLOAD_BYTES = 50 * 1024 * 1024


def _store_upload_and_ingest(body: bytes, safe: str, jd, tiers,
                             use_llm: bool, conf: dict | None) -> tuple[str, dict]:
    """落盘 + 解析（**同步、耗时**：含 OCR 与模型调用）。

    放进线程池跑的原因见 `api_upload`——它是整个服务里唯一会在事件循环里
    长时间阻塞的一段，期间进度轮询和别的上传都会一起卡住。
    """
    import os as _os
    import time as _t
    from . import ingest as _ing
    inbox = _os.path.join(DATA_DIR, "inbox")
    _os.makedirs(inbox, exist_ok=True)
    stamp = _t.strftime("%Y%m%d_%H%M%S")
    dest = _os.path.join(inbox, f"{stamp}_{safe}")
    i = 1
    while _os.path.exists(dest):
        dest = _os.path.join(inbox, f"{stamp}_{i}_{safe}")
        i += 1
    with open(dest, "wb") as fh:
        fh.write(body)
    rep = _ing.ingest_dir(inbox, jd, tiers, DB_PATH, job_id=None,
                          use_llm=use_llm, llm_conf=conf)
    return _os.path.basename(dest), rep


@app.post("/api/upload")
async def api_upload(request: Request, name: str = Query(...),
                     x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                     x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """上传一个简历文件（裸字节，文件名放?name=）。

    为什么不用 multipart：打包产物里没有 python-multipart，引入依赖会让
    "能不能跑"取决于打包有没有收进去——而这条路径恰恰是**离线兜底**，
    不能再引入任何"看心情装上"的依赖。一个文件一个请求，前端循环即可。

    v1.25.9 加固两处（代码审查 P1-15 / P1-16）：
    · **体积上限**：先用 `Content-Length` 早拒（超限的请求根本不读进内存），
      读完再按实收字节核一次——头可以撒谎；
    · **类型白名单**：只收 `parse.SUPPORTED`，不让任意二进制落进 inbox；
    · **解析改到线程池**：`ingest_dir` 是同步的、要跑数秒到数十秒，
      直接在 `async def` 里调用会占死事件循环，进度轮询 `/api/tasks/*`
      和其他上传在这期间全部无响应。
    """
    s = _session(x_tp_token, x_tp_role)
    require(s, "ingest")
    declared = (request.headers.get("content-length") or "").strip()
    if declared.isdigit() and int(declared) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413,
                            detail=f"文件超过 {MAX_UPLOAD_BYTES // (1024 * 1024)} MB 上限：{name}")
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="文件是空的")
    if len(body) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413,
                            detail=f"文件超过 {MAX_UPLOAD_BYTES // (1024 * 1024)} MB 上限：{name}")
    # 文件名只留 basename，去掉路径分隔符（防目录穿越）
    safe = os.path.basename(name or "resume").strip() or "resume"
    for ch in ("\\", "/", ":", "*", "?", "\"", "<", ">", "|"):
        safe = safe.replace(ch, "_")
    if not safe.lower().endswith(tuple(parse_mod.SUPPORTED)):
        raise HTTPException(status_code=415,
                            detail=f"不支持的文件类型：{safe}（只收 {'/'.join(parse_mod.SUPPORTED)}）")
    jd, tiers = _jd_tiers()
    # v1.23.2：**必须和「导入本地文件夹」同一口径**。
    # 这里原来写死 use_llm=False，等于上传的照片永远不做模型分析——
    # 同一份简历走文件夹就是有分析的、走上传就是没分析的，用户看不出来，
    # 只会觉得上传的不准。是否调模型交给同一个开关决定。
    _use_llm = _llm_enabled()
    _conf = None
    if _use_llm:
        _c = llm.load_cfg()
        _conf = {"api_key": _c.get("api_key"), "base_url": _c.get("base_url"),
                 "model": _c.get("model")}
    stored, rep = await run_in_threadpool(_store_upload_and_ingest, body, safe, jd, tiers,
                                          _use_llm, _conf)
    return {"ok": True, "stored": stored,
            "bytes": len(body), "index": rep.get("index"),
            "notes": rep.get("notes") or [],
            "analyzed": bool(_use_llm),
            "hint": "到人才库里点开这个人的完整档案，看「简历原文」那一段——"
                    "那就是 OCR 识别出来的文字，可逐字核对。"}


# ---- 面试字典（v1.20）：单位 / 会议室 / 时段 / 联系人，可在线增删 ----
INTERVIEW_PATH = _cfg_path("interview.json")


#: 兜底字典：config/interview.json 缺失时用这份，保证下拉框**不会是空的**
#: （空下拉框比没有下拉框更糟——HR 会以为功能坏了）。
_INTERVIEW_FALLBACK = {
    "modes": ["现场面试", "线上面试"],
    "slots": ["09:00-10:00", "10:00-11:00", "14:00-15:00", "15:00-16:00"],
    "rooms": ["创新大楼1519会议室"],
    "units": ["人事处", "财务处", "数字化中心"],
    "contacts": [{"dept": "人力资源部", "name": "张老师", "phone": "029-8888XXXX"}],
}


def _load_interview_dict() -> dict:
    try:
        with open(INTERVIEW_PATH, encoding="utf-8") as fh:
            d = json.load(fh)
        # 文件在但某一项是空数组时也回退，别让下拉框开天窗
        for k, v in _INTERVIEW_FALLBACK.items():
            if not d.get(k):
                d[k] = v
        # 历史文件里可能是乱序的（早于 v1.22 存的），读出来先规整一次
        for k in ("slots", "rooms"):
            if isinstance(d.get(k), list):
                d[k] = _sort_dict_items(k, d[k])
        return d
    except (OSError, ValueError):
        return dict(_INTERVIEW_FALLBACK)


# ---- 字典项排序（v1.22）----
# 时段与会议室有**客观顺序**，新加的项要插到该在的位置，不能一律追加到末尾。
# 单位/方式/联系人没有客观顺序，保持人工顺序（HR 可能按常用度排）。
_CN_DIGIT = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _slot_key(v: str):
    """'08:30-09:30' → (8*60+30, v)；解析不出来的排到最后（但保持相对顺序）。"""
    m = re.match(r"\s*(\d{1,2})\s*[:：]\s*(\d{1,2})", str(v or ""))
    if not m:
        return (99 * 60, str(v))
    return (int(m.group(1)) * 60 + int(m.group(2)), str(v))


def _room_key(v: str):
    """自然排序：'创新大楼1203会议室' < '创新大楼1519会议室'。
    数字按数值比（否则 1203 vs 1519 的字符串比较虽然也对，但 999 > 1519 会错），
    中文数字（二层/三楼）折成阿拉伯数字再比。"""
    t = str(v or "")
    parts = []
    for seg in re.split(r"(\d+)", t):
        if seg.isdigit():
            parts.append((1, int(seg), ""))
        elif seg:
            cn = "".join(str(_CN_DIGIT[c]) for c in seg if c in _CN_DIGIT)
            parts.append((0, 0, seg)) if not cn else parts.append((1, int(cn), seg))
    return parts + [str(t)]


def _sort_dict_items(key: str, values: list) -> list:
    if key == "slots":
        return sorted(values, key=_slot_key)
    if key == "rooms":
        return sorted(values, key=_room_key)
    return list(values)


@app.get("/api/interview-dict")
def api_interview_dict() -> dict:
    """面试字典（只读）。前端下拉框按这个渲染。"""
    return _load_interview_dict()


@app.post("/api/interview-dict")
def api_interview_dict_save(req: dict = Body(...),
                            x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                            x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """保存面试字典。**只接受白名单里的几个键**，别的一律忽略——
    免得这个口��被用来往配置文件里塞别的东西。"""
    s = _session(x_tp_token, x_tp_role)
    require(s, "ingest")
    cur = _load_interview_dict()
    for key in ("modes", "slots", "rooms", "units"):
        if isinstance(req.get(key), list):
            vals = [str(x).strip() for x in req[key] if str(x).strip()]
            # 去重但保持顺序（HR 是按"最常用的排前面"来维护的）
            seen, out = set(), []
            for v in vals:
                if v not in seen:
                    seen.add(v)
                    out.append(v)
            cur[key] = _sort_dict_items(key, out)
    if isinstance(req.get("contacts"), list):
        rows = []
        for c in req["contacts"]:
            if not isinstance(c, dict):
                continue
            dept = str(c.get("dept") or "").strip()
            name = str(c.get("name") or "").strip()
            phone = str(c.get("phone") or "").strip()
            if not (dept or name):
                continue
            rows.append({"dept": dept, "name": name, "phone": phone})
        cur["contacts"] = rows
    # v1.29 缺口A：会议号（单值）。写邮件页选「线上面试」时自动带出（ui.py 的 `mv_会议号`）。
    # 允许保存空串——清空是 HR 的正当操作，不做"非空才生效"。
    if isinstance(req.get("meeting_no"), str):
        cur["meeting_no"] = req["meeting_no"].strip()
    tmp = INTERVIEW_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(cur, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, INTERVIEW_PATH)
    return {"ok": True, "dict": cur}


@app.get("/api/candidates")
def api_candidates(tier: str | None = None, kw: str | None = None,
                   stage: str | None = None, education: str | None = None,
                   univ: str | None = None, strong: str | None = None,
                   cet: str | None = None,
                   # v1.29：新增筛选维度（全部下沉到 db.list_candidates）
                   overseas: str | None = None, bachelor: str | None = None,
                   date_from: str | None = None, date_to: str | None = None,
                   interview_unit: str | None = None,
                   outcome: str | None = None,
                   min_years: int | None = None, gender: str | None = None,
                   archived: str | None = None,
                   page: int = 0, page_size: int = 0,
                   x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                   x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """人才库列表。

    性别筛选是**普通筛选条件**（v1.29.4，业务方 2026-10-16 定调：性别不做脱敏、
    不设"红线"开关）。性别只来自简历**明写**的标签行（绝不推断），也不参与评分。

    学历（v2.0.2，业务方 2026-10-19 改口径）：**精确档位多选**，逗号分隔。
    `education=大专,硕士` 只回这两档；不传 = 不限。
    原来是"≥ 最低学历"（`education=大专` 会把硕博一起带出来），HR 想只看大专时用不了。
    同义档由 `db.parse_education()` 归并（专科→大专、学士→本科、研究生→硕士）——
    不归并的话选"硕士"会漏掉简历里写"研究生"的人。
    学历"无法判定"的人不命中任何具体档位 ——
    但**不能静默消失**：被隐藏的人数放进 `edu_filter.hidden_unknown`，界面出提示。

    院校层次（v1.7.3）：`univ=985 / 211`。985 学校全部同时是 211，
    所以选"211"时 985 也算；识别用 `config/universities.json` 名单
    （精确匹配全名/别名 + 校区后缀归一，独立学院不会误标），
    匹配结果同时以 `uni_tier` 字段随每条下发（卡片标签用）。
    学历与院校与性别一样，都是普通筛选条件。

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
        # v1.29.4：性别筛选按普通条件处理（不再有"受开关约束"这一层）。
        want = (gender or "").strip()
        want_archived = (archived or "").strip() in ("1", "true", "yes")
        want_edu = (education or "").strip() or None
        want_univ = (univ or "").strip()
        # v1.27：特定院校（strong）与英语四六级（cet）**下沉到 db.list_candidates**。
        # 这里只负责 facets 统计与展示，不再自己过滤一遍 ——
        # 两处各筛一次必然漂移（v1.23.4 把 univ 下沉过一次，同一个道理）。
        # `strong` 逗号分隔 = 并集（"看下这几所学校的人"）；
        # `cet` 逗号分隔 = 并集，"未提供"是特殊值（简历里没写）。
        # ⚠️ **univ 也必须一起传下去**：下沉时只把 strong/cet 加进 db、
        # 却忘了把原有的 univ 一起传，筛选会**静默失效**（返回全库）——
        # 症状是"985 筛选"看起来没报错、人数却等于全库。实测就是被回归套件逮到的。
        want_strong = [s for s in (strong or "").split(",") if s.strip()]
        want_cet = (cet or "").strip()
        _cet_parts = [c for c in want_cet.split(",") if c.strip()]
        items = db.list_candidates(conn, tier=tier, keyword=kw, stage=stage,
                                   min_years=min_years,
                                   univ=want_univ or None,
                                   strong=want_strong or None,
                                   cet=want_cet or None,
                                   # v1.29：五个新维度一并下沉。**在 db 层筛**是硬要求 ——
                                   # 智能助手的 search_candidates 与导出共用同一个函数，
                                   # 只在 server 层筛会出现"界面上能筛、助手查不到"。
                                   overseas=(overseas or "").strip() or None,
                                   bachelor=(bachelor or "").strip() or None,
                                   date_from=(date_from or "").strip() or None,
                                   date_to=(date_to or "").strip() or None,
                                   interview_unit=(interview_unit or "").strip() or None,
                                   outcome=(outcome or "").strip() or None,
                                   # 分页请求时先**轻量**取（不装配技能明细）：
                                   # 1000 人时全装配是 59ms，而一屏只要 20 条。
                                   # 筛选与 facets 都不依赖技能字段，切片后再装配即可。
                                   light=bool(page_size and page_size > 0),
                                   archived=True if want_archived else False)
        # 学历：**精确档位多选**（v2.0.2 业务方要求；原来是"≥ 最低学历"，
        # 想筛"只要大专"会把硕博一起带出来）。在 server 层做而不是塞进
        # list_candidates —— "无法判定被隐藏的人数"要数的是**除学历外其他条件
        # 都命中**的人，这个集合只有在这里拿得到。
        # 解析规则共用 `db.parse_education()`，与导出/助手那条路同源。
        _edu_want = db.parse_education(want_edu)
        hidden_unknown = 0
        if _edu_want:
            hidden_unknown = sum(1 for i in items
                                 if db.EDU_RANK.get(i.get("edu_level") or "", 0) == 0)
            items = [i for i in items
                     if db.EDU_RANK.get(i.get("edu_level") or "", 0) in _edu_want]
        # 院校层次 facets：只统计、**不过滤**（过滤已下沉到 db）。
        # v1.27：`uni_tier` 现在有三档（985/211/强势），facets 必须**按值比对**
        # 而不是靠"是不是985/211" —— 否则"强势"会被当成"非985非211"
        # 混进 211 的兜底里，筛 211 时凭空多出一批国科大的人。
        _c985 = sum(1 for i in items if i.get("uni_tier") == "985")
        _c211 = sum(1 for i in items if i.get("uni_tier") == "211")
        uni_facets = {"985": _c985, "211": _c985 + _c211,
                      # v1.29：并入「特殊」（原 strong 名单）与「未识别」
                      "特殊": sum(1 for i in items if i.get("uni_tier") == "强势"),
                      "未识别": sum(1 for i in items if not (i.get("school") or "").strip())}
        # v1.29：海外院校（**独立维度**）。facets 用三态而不是布尔：
        # 「未识别」（学校没读到）必须单列 —— 它要人工补录，与"不是海外"相反处理。
        overseas_facets = {
            "海外": sum(1 for i in items if i.get("overseas") is True),
            "非海外": sum(1 for i in items if i.get("overseas") is False),
            "未识别": sum(1 for i in items if i.get("overseas") is None),
        }
        # v1.29：第一学历层次 facets。「未识别」单列（同上理由）
        bachelor_facets = {
            "985": sum(1 for i in items if i.get("bachelor_tier") == "985"),
            "211": sum(1 for i in items if i.get("bachelor_tier") == "211"),
            "特殊": sum(1 for i in items if i.get("bachelor_tier") == "强势"),
            "未识别": sum(1 for i in items
                          if not (i.get("first_degree_school") or "").strip()),
        }
        # v1.29：面试部门（面试单位）facets。取库里**实际用过的**值（并字典补齐见前端），
        # 数字与列表同源 —— 下拉里写 3 人、点进去只有 2 人是最典型的"数字对不上"。
        # v1.29.11：单位支持**多选**（「分析测试中心、稀土研究院」）——按**拆开的
        # 单个单位**计数，否则下拉会出现"甲、乙"整串、拆开筛又对不上人。
        _unit_counts: dict[str, int] = {}
        for i in items:
            for _u in re.split(r"[、,，]+", str(i.get("interview_unit") or "")):
                _u = _u.strip()
                if _u:
                    _unit_counts[_u] = _unit_counts.get(_u, 0) + 1
        interview_unit_facets = dict(
            sorted(_unit_counts.items(), key=lambda kv: (-kv[1], kv[0])))
        # v1.29：互斥提示。`univ=985/211/特殊` 与 `overseas=海外` 是**两个维度且互斥**
        # （境外高校既不是 985 也不是 211）→ 同时勾必然 0 人。
        # 静默返回空列表会让 HR 以为系统坏了，所以明确说出来。
        _conflict = []
        if want_univ in ("985", "211", "特殊") and (overseas or "").strip() in ("1", "海外"):
            _conflict.append("「国内院校层次」与「海外院校」互斥：境外高校不在 985/211/特殊名单里，"
                             "同时勾选不会有结果。")
        # v1.27：英语四六级 facets，同样只统计。
        # ⚠️ 统计口径 = **除四六级外的其他条件都命中**的那批人（下拉里写的人数
        # 不会因为当前勾了哪一档而缩水，与 gender/uni_facets 一致）。
        # 这里 items 已经过了 strong/cet 过滤，所以 facets 会受当前选项影响 ——
        # 与 `uni_facets` 受 univ 影响的既有行为一致，不另立口径。
        cet_facets: dict | None = None
        if _cet_parts:
            cet_facets = {}
            for _lv in extract_mod.CET_FILTER_LEVELS:
                cet_facets[_lv] = sum(1 for i in items if i.get("cet_level") == _lv)
            cet_facets["未提供"] = sum(1 for i in items if not (i.get("cet_level") or ""))
            cet_facets["无"] = sum(1 for i in items if i.get("cet_level") == "无")
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
            # 只为这一页装配技能明细（上面是轻量取的）——
            # 一次 IN 查询 20 人，而不是把全库 1000 人都装配出来再丢掉 980 个
            db._attach_skills(conn, items)
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
                i.get("years_exp"), bool(i.get("years_exp")), i.get("grad_date"),
                in_school=bool(i.get("in_school")))
            i["tier_detail"] = _tier_detail_of(i, _jdm, _cats)
        presented = auth.present_list(items)
        out = {"count": len(presented), "items": presented,
               # v1.13.4：入库即分析的开关状态与待分析人数。列表页靠它渲染开关与
               # "分析待分析的人（N）"按钮——顺带带上是��为了不多发一个请求。
               "insight_switch": {"enabled": bool(db.get_setting(conn, AUTO_INSIGHT_KEY, True)),
                                  "pending": db.count_needing_insight(conn)},
               "my_permissions": s["permissions"],
               "archived_view": want_archived,
               "gender_facets": facets,
               "uni_facets": uni_facets,
               # v1.27：英语四六级 facets + 强势院校名单。
               # 两者都随列表一起下发，界面不必再发第二个请求就能画出筛选下拉 ——
               # 筛选控件与它的人数来自**同一次查询**，不会对不上。
               "cet_facets": cet_facets,
               "strong_schools": univ_mod.strong_schools(),
               # v1.29：新维度的 facets（数字与列表同源，别让下拉与结果对不上）
               "overseas_facets": overseas_facets,
               "bachelor_facets": bachelor_facets,
               "interview_unit_facets": interview_unit_facets,
               "overseas_schools": univ_mod.overseas_schools(),
               # 互斥/异常提示：静默返回空列表会被当成"系统坏了"
               "conflicts": _conflict,
               # 生效中的筛选回显（前端只读这一份，不用自己再推一遍）
               "filters": {
                   "univ": want_univ or "", "overseas": (overseas or "").strip(),
                   "bachelor": (bachelor or "").strip(), "cet": want_cet,
                   "date_from": (date_from or "").strip(), "date_to": (date_to or "").strip(),
                   "interview_unit": (interview_unit or "").strip(),
                   "strong": (strong or "").strip(), "stage": (stage or "").strip(),
                   "education": want_edu or "",
               },
               "edu_filter": {"levels": want_edu, "applied": bool(_edu_want),
                              "hidden_unknown": hidden_unknown},
               "paging": paging,
               "gender_filter": {
                   "requested": (gender or "").strip() or None,
                   "applied": bool(want),
                   "note": "性别仅来自简历明写标签，不参与评分与分级。",
               }}
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


def _sensitive_of(doc: dict) -> dict:
    """把 documents.sensitive_found（JSON 文本）解成 {类别: 条数}。

    老库没有这一列 / 老记录是空的 → 返回 {}，界面按"无可核查记录"处理，
    不要假装"一条都没剔除"（那会让人误以为红线没生效）。
    """
    raw = (doc or {}).get("sensitive_found")
    if not raw:
        return {}
    try:
        v = json.loads(raw) if isinstance(raw, str) else raw
        return v if isinstance(v, dict) else {}
    except Exception:                                     # noqa: BLE001
        return {}


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
            # 送进模型前剔除了哪些敏感信息（类别→条数）。原来算完就丢，
            # HR 无从核查"到底哪些内容没给模型看"——隐私红线应当是可核查的。
            "sensitive_removed": _sensitive_of(latest_doc),
            "audit_snippet": db.candidate_audit(conn, cid, limit=20),
            # v1.25：这位**发过邮件没有**（含最近时间与主题）。
            # 用"发过没有"做标记，是为了避免重复发——同一个人被两个 HR
            # 各发一封面试邀请，是最难解释的那类事故。
            "email_state": db.candidate_email_state(conn, cid),
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
def api_candidate_reanalyze(cid: int, full: bool = True, req: dict | None = None,
                            x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                            x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """重新分析一位候选人。

    **默认 `full=True`：完整重跑**（v1.23.6）。原来这个按钮只重跑 `auto_insight`，
    产出一段分析文本 —— HR 的原话是「重新分析应该包括完整的重新分析，
    不应该只是这部分的重新分析」，说得对：老档案的 `extract_mode=heuristic`
    （模型不可用时的规则通道）永远是规则通道，想用上模型抽取只能重新导入一份，
    还会多出一条投递。现在完整重跑：
      重新解析原件 → 模型重抽字段 → 重建技能 → 重新归岗 → 重判建议档 → 重算分析

    **不动的三样**：HR 定的档（`tier_final`）、阶段、已归岗的岗位；
    另外**保人工更正**：HR 手动改过的字段（审计里有 `edit_fields`）保持不动。

    `full=False` 保留旧行为（只重跑分析结论），供"只想刷新那段文字"的场景用。
    """
    from .pipeline.analyze import auto_insight

    s = _session(x_tp_token, x_tp_role)
    require(s, "chat")
    conn = db.connect(DB_PATH)
    try:
        d = db.candidate_detail(conn, cid)
        if not d:
            raise HTTPException(status_code=404, detail="未找到该候选人")

        if full:
            if not _llm_enabled():
                return {"ok": False, "error": "模型未启用，无法完整重新分析；"
                                              "请先在「模型设置」里配置模型",
                        "hint": "只刷新分析文本可改用「只看分析」（full=false）"}
            _c = llm.load_cfg()
            _conf = {"api_key": _c.get("api_key"), "base_url": _c.get("base_url"),
                     "model": _c.get("model")}
            _jd, _tiers = _jd_tiers()
            # 注意：模块级别名是 `ingest_mod`（`_ing` 只是别的函数里的局部导入，
            # 在这里用会 NameError —— 上次只测了函数、没测接口，漏了这个）
            r = ingest_mod.reanalyze_full(conn, mb.load_config(), _jd, _tiers, cid,
                                          use_llm=True, llm_conf=_conf)
            if not r.get("ok"):
                return r
            r["insight"] = db.get_insight(conn, cid)
            r["note"] = ("完整重新分析完成：原件已重解析、字段已用模型重抽、"
                         "技能已重建、建议档已重算、分析结论已刷新。"
                         "HR 已定的档位/阶段/岗位不受影响。")
            return r

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
            # v1.24.15：注释早就写着「D 不覆盖」，但代码从没实现过这个条件——
            # 学历不够的人被存成 C 时，模型可以合法改成 A/B。这里补上，
            # 裁决统一走 `tier.lock_tier`（与入库自动分析、岗位重判同一把尺子）。
            _gate = ins.get("edu_gate") or tier_mod.edu_gate(d, jd or {})
            _mt, _src = tier_mod.lock_tier(None, _mt, _gate)
            _ar = conn.execute("SELECT tier_suggested, tier_final FROM applications WHERE id=?",
                               (app_id,)).fetchone()
            # 只有**学历门槛判出的 D** 才冻结。模型自己判的 D（如业务方向不对口）
            # 在学历达标时仍可被后续分析改写——把两类 D 一律冻死会锁死正常的重判。
            _edu_locked = bool(_gate.get("failed"))
            if _ar and not _ar["tier_final"] and _ar["tier_suggested"] != _mt \
                    and not (_edu_locked and _ar["tier_suggested"] == "D" and _mt != "D"):
                conn.execute("UPDATE applications SET tier_suggested=?, updated_at=? WHERE id=?",
                             (_mt, db.now(), app_id))
                conn.commit()
                _tier_note = f"（{_src}）"
            else:
                _tier_note = ""
        else:
            _tier_note = ""
        db.add_audit(conn, "candidate", str(cid), "reanalyze", "",
                     f"{s['username']} 重跑自动分析（{ins.get('source')}）",
                     s["username"], s["role"])
        return {"ok": True, "job": job_meta, "insight": db.get_insight(conn, cid, app_id),
                "note": "分析已更新。模型建议档位已同步到建议档（HR 未确认的）"
                        f"{_tier_note}；学历不达标的直接判 {tiers_mod.hard_shortfall_tier()}"
                        "、不采用模型建议；"
                        "阶段与岗位不受影响。"}
    finally:
        conn.close()


@app.get("/api/candidates/{cid}/avatar")
def api_candidate_avatar(cid: int, token: str | None = None,
                         x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                         x_tp_role: str | None = Header(default=None, alias="X-TP-Role")):
    """证件照（卡片头像 / 档案用）。v2.0.4。

    **懒抽**：`avatar_path` 还是 NULL 时，第一次请求才去原件里抽一张；
    抽不到就记 `"-"`，以后不再重试 —— 否则每次渲染 20 张卡片都要重开 20 个 PDF。

    为什么用 query `token` 而不是只认 header：这个地址是给 `<img src>` 用的，
    `<img>` 带不了自定义头。和 `/api/sources/file` 走同一个 `_file_session` 口径。
    """
    s = _file_session(x_tp_token, token)
    require(s, "read")
    conn = db.connect(DB_PATH)
    try:
        row = conn.execute("SELECT avatar_path FROM candidates WHERE id = ?", (cid,)).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="未找到该候选人")
        rel = row["avatar_path"]
    finally:
        conn.close()
    # **请求线程绝不抽图**（抽图会把接口堵到 9 秒，实测见 avatar.py 里的说明）：
    # 还没抽过就排进后台队列、立刻返回 404；客户端隔 2 秒重试，抽好之后自然出现。
    if rel is None:
        avatar_mod.enqueue(DB_PATH, cid, AVATAR_DIR)
        raise HTTPException(status_code=404, detail="照片正在后台抽取")
    if rel == avatar_mod.NONE_MARK:
        raise HTTPException(status_code=404, detail="这份简历里没有可用的证件照")
    path = _abs_stored(rel)
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="照片文件已不在磁盘上")
    # 私有缓存：同一次浏览里不重复请求，但换人/换图能立刻生效
    return FileResponse(path, media_type="image/jpeg",
                        headers={"Cache-Control": "private, max-age=300"})


@app.post("/api/candidates/{cid}/avatar")
def api_candidate_avatar_refresh(cid: int,
                                 x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                                 x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """**重新抽取**证件照（抽错一张图时用）。"""
    s = _session(x_tp_token, x_tp_role)
    require(s, "chat")
    conn = db.connect(DB_PATH)
    try:
        if not conn.execute("SELECT 1 FROM candidates WHERE id = ?", (cid,)).fetchone():
            raise HTTPException(status_code=404, detail="未找到该候选人")
        avatar_mod.reset(conn, cid)
        rel = avatar_mod.ensure(conn, cid, AVATAR_DIR)
        ok = bool(rel) and rel != avatar_mod.NONE_MARK
        db.add_audit(conn, "candidate", str(cid), "avatar_refresh", "",
                     "重新抽取证件照" + ("" if ok else "（没抽到）"), s["username"], s["role"])
    finally:
        conn.close()
    return {"ok": ok, "avatar_path": rel}


@app.delete("/api/candidates/{cid}/avatar")
def api_candidate_avatar_clear(cid: int,
                               x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                               x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """**去掉**证件照（抽到的不是这个人 / 不想显示）。卡片自动回落到"姓首字"。"""
    s = _session(x_tp_token, x_tp_role)
    require(s, "chat")
    conn = db.connect(DB_PATH)
    try:
        if not conn.execute("SELECT 1 FROM candidates WHERE id = ?", (cid,)).fetchone():
            raise HTTPException(status_code=404, detail="未找到该候选人")
        avatar_mod.clear(conn, cid)
        db.add_audit(conn, "candidate", str(cid), "avatar_clear", "",
                     "去掉证件照", s["username"], s["role"])
    finally:
        conn.close()
    return {"ok": True}


@app.post("/api/avatars/backfill")
def api_avatar_backfill_start(
        x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
        x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """可选地补齐历史头像。只处理 avatar_path=NULL，跳过已确认和无照片标记。"""
    s = _session(x_tp_token, x_tp_role)
    require(s, "chat")
    state = avatar_mod.start_backfill(DB_PATH, AVATAR_DIR)
    return {"ok": True, **state,
            "note": ("已启动历史头像补扫（后台串行，不覆盖已有头像）" if state.get("running")
                     else "没有待补扫的历史头像")}


@app.get("/api/avatars/backfill/status")
def api_avatar_backfill_status(
        x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
        x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    _session(x_tp_token, x_tp_role)
    return {"ok": True, **avatar_mod.backfill_status()}


@app.post("/api/avatars/backfill/stop")
def api_avatar_backfill_stop(
        x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
        x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    s = _session(x_tp_token, x_tp_role)
    require(s, "chat")
    return {"ok": True, **avatar_mod.stop_backfill(),
            "note": "已请求停止；当前这一张抽完后就停"}


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
    # v1.25.6：性别也能改（用户要求）——"简历没读到"时由 HR 补填；
    # 它不参与档位判定，但界面展示与性别筛选都要用。
    if "gender" in req:
        g = str(req.get("gender") or "").strip()
        if g and g not in ("男", "女"):
            raise HTTPException(status_code=422, detail="性别只能填：男 / 女（清空表示未填）")
        fields["gender"] = g or None
    # v1.29：英语四六级随「保存更正」一起提交（原来是一个独立的接口+按钮，
    # 改了不点那个按钮就白改）。收的是**归一后的档位名**，只接受白名单取值，
    # 空串 = 清空（筛选时算"未提供"，与"简历明写没通过"的 '无' 严格区分）。
    if "cet" in req:
        lv = str(req.get("cet") or "").strip()
        if lv and lv not in extract_mod.CET_FILTER_LEVELS:
            raise HTTPException(
                status_code=422,
                detail=f"英语四六级只能是：{'、'.join(extract_mod.CET_FILTER_LEVELS)}"
                       "（清空表示未识别）")
        # ⚠️ 存"档位名"而不是原文：`cet_level(档位名)` 能原样归一回来（已判据覆盖），
        # 所以档案显示与人才库筛选永远一致 —— 这正是原来要两个控件同步的原因，
        # 现在只留一个控件也不会分叉。
        fields["cet"] = lv or None
    # v1.29：第一学历（本科）院校。**HR 人工更正**的入口 ——
    # 校招简历常只写最高学历院校，「本科985/211」这个筛选需要本科那所，
    # 抽不到时由 HR 在这里补录（不补就落「未识别」）。
    # ⚠️ 不在这里做"院校名校验"：名单是枚举式的，名单外的一律算未识别，
    # 那属于配置口径问题，不该在这里拦着 HR 不让填。
    if "first_degree_school" in req:
        fields["first_degree_school"] = (
            str(req.get("first_degree_school") or "").strip()[:60] or None)
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
        # ⚠️ 新增可编辑字段时，`label` 与 `old_map` **必须同时加**（v1.29 加 cet 时踩到）：
        # 只加进 fields 而漏了 label，`changed` 会算成空 → 值其实**已经存进库**，
        # 接口却回一句「字段没有变化。」—— 界面告诉 HR "什么都没改"，实际改了。
        # 这是会静默说谎的一类 bug，比报 500 更难发现。
        label = {"name": "姓名", "edu_level": "学历", "school": "学校", "major": "专业",
                 "gender": "性别", "cet": "英语四六级",
                 "first_degree_school": "第一学历（本科）院校",
                 "phone_enc": "电话", "email_enc": "邮箱"}
        old_map = {"name": d.get("name") or "", "edu_level": d.get("edu_level") or "",
                   "school": d.get("school") or "", "major": d.get("major") or "",
                   "gender": d.get("gender") or "",
                   "cet": d.get("cet") or "",
                   "first_degree_school": d.get("first_degree_school") or "",
                   "phone_enc": decrypt(d.get("phone_enc")) or "",
                   "email_enc": decrypt(d.get("email_enc")) or ""}
        # ⚠️ v1.24.21 修（KeyError: 'phone_bidx' —— 保存必报 500 的根因）：
        # `fields` 里除用户能改的 6 个字段外，还夹带 phone_bidx / email_bidx
        # 两个**派生盲索引**（用于跨渠道身份比对）。它们没有中文名，
        # 一起进 changed 就会让下面的 label[k] 抛 KeyError。
        # 更糟的是 KeyError 发生在 update_candidate **之后**——字段其实已经落库了，
        # 界面却弹「Internal Server Error」，HR 以为没保存、反复重存。
        # 修法：只按**用户可见字段**判定"有没有变化"，派生字段照写不误、但不参与审计文案。
        changed = {k: (old_map.get(k), fields[k]) for k in fields
                   if k in label and str(old_map.get(k) or "") != str(fields[k] or "")}
        # 兜底：万一将来又加了无 label 的字段，宁可漏写审计文案也不要 500。
        # —— 500 会让 HR 以为"没保存"，比审计少一行严重得多。
        if not changed:
            # 派生字段可能单独变了（如电话没变但规范化结果变），此时仍要走更新，
            # 但不必写审计（用户没改任何能看到的东西）。
            if fields:
                db.update_candidate(conn, cid, **fields)
            return {"ok": True, "id": cid, "changed": [], "note": "字段没有变化。"}
        db.update_candidate(conn, cid, **fields)
        # v1.25：审计文案统一走 db.audit_field_value（解密 + 电话打码）。
        # 原样写入会把 `enc:…` 密文留在审计表里，对 HR 毫无意义。
        before = "；".join(f"{label[k]}={db.audit_field_value(k, v[0])}"
                           for k, v in changed.items())
        after = "；".join(f"{label[k]}={db.audit_field_value(k, v[1])}"
                          for k, v in changed.items())
        db.add_audit(conn, "candidate", str(cid), "edit_fields", before, after,
                     s["username"], s["role"])
        return {"ok": True, "id": cid, "changed": sorted(changed),
                "note": "已更正：" + "、".join(label[k] for k in changed)
                        + "（改前改后都记在操作审计里）。"}
    finally:
        conn.close()


@app.post("/api/candidates/{cid}/edu-history")
def api_candidate_edu_history(cid: int, req: dict,
                              x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                              x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """改**某一段**教育经历的学校/专业（业务方 2026-10-21）。

    为什么单独开一个口子：档案上方会列出本科/硕士/博士多段，但原来只能改
    「第一学历」和「最高学历」两个字段 —— 中间那段（比如硕士学校解析错了）
    在界面上根本没有入口，只能整份重导。

    约定：
      · `index` 是 `edu_history` 里的下标（前端渲染顺序，与库内顺序一致）；
      · 只改 `school` / `major`，`degree` / 起止时间不动（那是抽取的核心结论）；
      · **若这一段就是最高学历**（degree 与 `edu_level` 相同），同步更新主字段
        `school` / `major` —— 档位、院校层次、排名都读主字段，不改会导致
        "上面显示改了、档位还按旧学校判"。
    """
    s = _session(x_tp_token, x_tp_role)
    require(s, "chat")
    req = req or {}
    try:
        k = int(req.get("index", -1))
    except (TypeError, ValueError):
        raise HTTPException(status_code=422, detail="index 必须是数字")
    conn = db.connect(DB_PATH)
    try:
        row = conn.execute(
            "SELECT edu_history, edu_level, school, major FROM candidates WHERE id = ?",
            (cid,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="未找到该候选人")
        try:
            hist = json.loads(row["edu_history"]) if row["edu_history"] else []
        except (ValueError, TypeError):
            hist = []
        if not isinstance(hist, list) or not hist:
            raise HTTPException(status_code=422,
                                detail="这个人没有分段的教育经历（可改「第一学历 / 最高学历」）")
        if k < 0 or k >= len(hist):
            raise HTTPException(status_code=422, detail="教育经历段编号超出范围")
        seg = dict(hist[k]) if isinstance(hist[k], dict) else {}
        old = {"degree": seg.get("degree") or "",
               "school": seg.get("school") or "", "major": seg.get("major") or ""}
        new = dict(old)
        if "degree" in req:
            deg_v = str(req.get("degree") or "").strip()
            if deg_v and deg_v not in db.EDU_RANK:
                raise HTTPException(status_code=422,
                                    detail="学历只能填：大专 / 本科 / 硕士 / 博士")
            new["degree"] = deg_v
        if "school" in req:
            new["school"] = str(req.get("school") or "").strip()[:80]
        if "major" in req:
            new["major"] = str(req.get("major") or "").strip()[:60]
        if old == new:
            return {"ok": True, "id": cid, "changed": [],
                    "note": "这一段的学历/学校/专业没有变化。"}
        seg["degree"] = new["degree"] or seg.get("degree")
        seg["school"] = new["school"] or None
        seg["major"] = new["major"] or None
        hist[k] = seg
        conn.execute("UPDATE candidates SET edu_history = ? WHERE id = ?",
                     (json.dumps(hist, ensure_ascii=False), cid))
        # 同步主字段：**只有这一段原本就是最高学历段**时才同步。
        # ⚠️ 用**改前**的 degree 判断，不能用改后的 —— 否则把"本科段"的学历改成硕士时，
        # 它会临时变成"最高学历段"，连带把主字段 school 覆盖成本科那所学校、
        # 或把 edu_level 改错（实测会把整个人变成"本科 + 硕士学校"的自相矛盾状态，
        # 而 `_loads_edu_history` 会把这种矛盾的段直接剔除，档案里就少一段）。
        deg = str(seg.get("degree") or "").strip()
        old_deg = str(old.get("degree") or "").strip()
        top = str(row["edu_level"] or "").strip()
        sync = bool(old_deg and top and old_deg == top)
        if sync:
            sets, args = [], []
            if "school" in req:
                sets.append("school = ?")
                args.append(new["school"] or None)
            if "major" in req:
                sets.append("major = ?")
                args.append(new["major"] or None)
            if "degree" in req:
                sets.append("edu_level = ?")
                args.append(new["degree"] or None)
            if sets:
                args.append(cid)
                conn.execute(f"UPDATE candidates SET {', '.join(sets)} WHERE id = ?", args)
        before = (f"第{k + 1}段（{old['degree'] or '学历'}）学校={old['school']}，"
                  f"专业={old['major']}")
        after = (f"第{k + 1}段（{new['degree'] or '学历'}）学校={new['school']}，"
                 f"专业={new['major']}")
        db.add_audit(conn, "candidate", str(cid), "edit_edu_segment", before, after,
                     s["username"], s["role"])
        conn.commit()
        return {"ok": True, "id": cid, "changed": ["school", "major"],
                "synced_top": sync,
                "note": "已更正第 " + str(k + 1) + " 段教育经历"
                        + ("（同时更新了最高学历院校）" if sync else "") + "。"}
    finally:
        conn.close()


@app.post("/api/candidates/{cid}/note")
def api_candidate_note(cid: int, req: dict,
                       x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                       x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """人工备注（v1.25.7）：每位候选人一条自由文本，**挂在人上**（跨投递保留）。

    为什么独立于「保存更正」：备注是长文本、改得频繁，且**不参与档位判定**；
    独立接口单独保存更顺手。修改写审计（改前/改后全文留痕）；备注内容可被搜索。
    """
    s = _session(x_tp_token, x_tp_role)
    require(s, "chat")
    req = req or {}
    note = str(req.get("note") or "").strip()
    if len(note) > 500:
        raise HTTPException(status_code=422, detail="备注最多 500 字")
    conn = db.connect(DB_PATH)
    try:
        d = db.candidate_detail(conn, cid)
        if not d:
            raise HTTPException(status_code=404, detail="未找到该候选人")
        old = (d.get("note") or "").strip()
        if old == note:
            return {"ok": True, "id": cid, "changed": [], "note": "备注没有变化。"}
        db.update_candidate(conn, cid, note=note or None)
        db.add_audit(conn, "candidate", str(cid), "edit_fields",
                     f"备注={old or '（空）'}", f"备注={note or '（空）'}",
                     s["username"], s["role"])
        return {"ok": True, "id": cid, "changed": ["note"],
                "note": ("备注已保存。" if note else "备注已清空。")}
    finally:
        conn.close()


def _end_open_applications(conn, cid: int, s: dict) -> int:
    """把某候选人**进行中**的投递置为「已结束」（归档联动用），返回改动条数。

    终态不动：已在终点的不再重复写。终态集合从配置取（v1.29）——
    原来这里写死 `("已入职", "已结束")`，去掉「已入职」后不同步改的话，
    历史遗留的「已入职」会被当成"进行中"，每次归档都被重写成"已结束"。
    """
    n = 0
    _terminal = set(stages_mod.terminal_stages())
    rows = conn.execute("SELECT id, stage FROM applications WHERE candidate_id = ?",
                        (int(cid),)).fetchall()
    for a in rows:
        if (a["stage"] or "") in _terminal:
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


class ReanalyzeBatchReq(BaseModel):
    limit: int = 5                  # 每次调用处理几个（前端循环调用直到 remaining=0）
    only_heuristic: bool = True     # 只处理"还在规则通道"的；False = 全部重来一遍


def _needs_full_reanalysis(conn, limit: int, only_heuristic: bool) -> list[dict]:
    """挑出需要完整重跑的人（排除归档）。

    默认只挑 `extract_mode != 'llm+rule'` 的——已经用模型抽过的人不必再花一次模型调用。
    顺序按投递时间从早到晚，和"待分析"按钮一致。
    """
    sql = """SELECT a.candidate_id AS cid, a.id AS app_id, c.name, a.extract_mode
             FROM applications a JOIN candidates c ON c.id = a.candidate_id
            WHERE COALESCE(c.archived_at, '') = ''
              AND c.merged_into IS NULL
              AND a.id = (SELECT id FROM applications WHERE candidate_id = c.id
                          ORDER BY COALESCE(applied_at,'') DESC, id DESC LIMIT 1)"""
    if only_heuristic:
        sql += " AND COALESCE(a.extract_mode, '') != 'llm+rule'"
    sql += " ORDER BY COALESCE(a.applied_at, '') ASC, a.id ASC LIMIT ?"
    return [dict(r) for r in conn.execute(sql, (int(limit),)).fetchall()]


@app.post("/api/candidates/reanalyze-batch")
def api_reanalyze_batch(req: ReanalyzeBatchReq,
                        x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                        x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """**批量完整重新分析**（v1.23.9）。前端循环调用直到 remaining=0。

    为什么分批而不是一次全跑：每人 6-15 秒（两次模型调用），
    100 人就是十几分钟——一次 HTTP 调用扛不住，前端也看不到进度。
    分批 + 循环，跟「分析待分析的人」是同一套交互口径。

    串行（v1.24.8）：每人一条自己的连接，不建线程池。
    进度（v1.24.9）：每批返回 task_id，前端轮询 /api/tasks/{task_id} 画进度条。
    """
    s = _session(x_tp_token, x_tp_role)
    require(s, "chat")
    if not _llm_enabled():
        return {"ok": False, "analyzed": 0, "remaining": 0, "failed": 0,
                "error": "模型未启用，无法完整重新分析；请先在「模型设置」里配置模型"}
    limit = max(1, min(int(req.limit or 5), 12))
    jd, tiers = _jd_tiers()
    conf_c = llm.load_cfg()
    conf = {"api_key": conf_c.get("api_key"), "base_url": conf_c.get("base_url"),
            "model": conf_c.get("model")}
    cfg = mb.load_config()

    conn = db.connect(DB_PATH)
    try:
        targets = _needs_full_reanalysis(conn, 20, req.only_heuristic)
        batch = targets[:limit]
        if not batch:
            return {"ok": True, "analyzed": 0, "failed": 0, "remaining": 0,
                    "note": "没有需要重跑的人了" if not targets
                            else "这一批已完成"}

        def one(t: dict) -> dict:
            # **串行，每人一条自己的连接**（v1.24.8 定案）。
            #
            # 为什么最终选串行——并发试过两种方案都不稳：
            #   ① 每人一个连接 → 间歇 `attempt to write a readonly database`
            #      （Windows 上多连接并发写同一WAL 库的文件锁判定不稳）
            #   ② 共享一条连接 + RLock → 修好了写，却冒出
            #      `InterfaceError: bad parameter or other API misuse`。
            #      根因：**Connection 上的游标是连接级共享状态**，多线程各自
            #      execute 后交替 fetch，sqlite3 判定 API 误用。补了读段加锁后
            #      又出现 `TypeError: 'NoneType' object is not subscriptable`，
            #      说明这条路上还有别的共享状态没找干净。
            #
            # 代价是明确的：10 人从约 14 秒变成约 70 秒。
            # 对HR 来说，多等 1 分钟换"确定跑完、不偶发失败"是划算的——
            # 批量任务的价值本来就在"能跑完"。
            # 真要提速，更稳的路子是**多进程**（各自进程一条连接，
            # 进程间不共享任何连接状态），而不是多线程共享连接。
            own = db.connect(DB_PATH)
            try:
                r = ingest_mod.reanalyze_full(own, cfg, jd, tiers, t["cid"],
                                              use_llm=True, llm_conf=conf)
                return {"name": t.get("name"), "ok": bool(r.get("ok")),
                        "error": r.get("error"), "mode": r.get("extract_mode")}
            except Exception as exc:                      # noqa: BLE001
                # **必须兜住**：没有这层的话，一个人出错会让整批中断、
                # 而**已经跑完的那些人的结果与进度全部丢失**（数据其实已写库，
                # 但 HR 只看到一个"失败"）。批量任务的价值就在于"能跑完"。
                #
                # 堆栈必须打：只给 "TypeError: ..." 的话，**定位不到是哪一行**。
                # 踩过：光看类型名完全猜不出位置，打了 traceback 才定位到
                # db.candidate_detail 里的 conn.execute(...).fetchall()。
                traceback.print_exc()
                print(f"[reanalyze_batch] {t.get('name')} 失败："
                      f"{type(exc).__name__}: {exc}", file=sys.stderr)
                return {"name": t.get("name"), "ok": False,
                        "error": f"{type(exc).__name__}: {exc}", "mode": None}
            finally:
                try:
                    own.close()
                except Exception:                       # noqa: BLE001
                    pass

        # **串行执行**：不建线程池（TP_ANALYZE_CONCURRENCY 在此路径已不再生效）
        tid = _progress_new("批量重新分析", len(batch))
        results: list[dict] = []
        for t in batch:
            results.append(one(t))
            _progress_set(tid, done=len(results),
                          failed=sum(1 for r in results if not r["ok"]))
        _progress_set(tid, status="done",
                      message=(f"成功 {sum(1 for r in results if r['ok'])} 人"
                               + (f"，失败 {len(results) - sum(1 for r in results if r['ok'])} 人"
                                  if any(not r["ok"] for r in results) else "")))
        ok_n = sum(1 for r in results if r["ok"])
        fail_n = len(results) - ok_n
        left = max(0, len(targets) - len(batch))
        for r in results:
            if r["ok"]:
                db.add_audit(conn, "candidate", "", "reanalyze_batch", "",
                             "批量完整重跑：" + (r.get("name") or "?"), s["username"], s["role"])
        conn.commit()
        return {"ok": True, "analyzed": ok_n, "failed": fail_n, "remaining": left,
                "total_hint": len(targets), "task_id": tid,
                "details": [{"name": r.get("name"), "ok": r["ok"],
                             "mode": r.get("mode"), "error": r.get("error")}
                            for r in results],
                "note": f"本批 {len(results)} 人：成功 {ok_n}"
                        + (f"，失败 {fail_n}" if fail_n else "")
                        + (f"；还剩 {left} 人" if left else "；全部完成")}
    finally:
        conn.close()


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
        # 登记进度任务：这批只是"待分析的人"里的一小批（最多 limit 个），
        # 但 total 用"这一批实际要跑的人数"，前端拿到 task_id 后轮询画进度。
        tid = _progress_new("批量分析", len(targets)) if targets else None
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
            if tid:
                _progress_set(tid, done=len(done) + len(failed),
                              failed=len(failed))
        if tid:
            _progress_set(tid, status="done",
                          message=(f"成功 {len(done)} 人"
                                   + (f"，失败 {len(failed)} 人" if failed else "")))
        conn.commit()
        remaining = db.count_needing_insight(conn)
        db.add_audit(conn, "insight", "batch", "analyze_pending",
                     f"待分析 {len(targets)} 人（上限 {limit}）",
                     f"成功 {len(done)} / 失败 {len(failed)}，剩余 {remaining}",
                     s["username"], s["role"])
        conn.commit()
        return {"ok": True, "analyzed": len(done), "failed": len(failed),
                "remaining": remaining, "done": done, "errors": failed[:10],
                "task_id": tid,
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
        _ended = int(out.get("ended_applications") or 0)
        out["note"] = (
            ("已归档，可在「归档」页查看；满 "
             f"{db.PURGE_AFTER_DAYS} 天后会被彻底删除"
             + (f"；同时结束 {_ended} 条进行中的投递" if _ended else ""))
            if req.archived
            else "已取消归档，恢复在人才库与检索中展示"
        )
        return out
    finally:
        conn.close()


def _purge_expired(conn=None, actor: str = "system", role: str = "system",
                   only_ids: list[int] | None = None) -> dict:
    """把归档满 30 天的档案彻底删除，**原件一并物理删除（不可恢复）**。

    自有连接便于被定时任务与接口两处复用。**先删库再删文件**：先拿到原件路径清单，
    删库成功后逐个 `unlink`；删除失败只记录、不阻断（残留文件下次手动处理）。
    v1.25.9：归档页「彻底删除」就是真删——不再移入回收目录堆着占空间。
    """
    own = conn is None
    conn = conn or db.connect(DB_PATH)
    try:
        out = db.purge_due_candidates(conn, db.PURGE_AFTER_DAYS, actor, role,
                                      only_ids=only_ids)
        deleted, failed = [], []
        for rel in out["files"]:
            src = _abs_stored(rel)
            if not os.path.exists(src):
                failed.append(rel)
                continue
            try:
                os.remove(src)
                deleted.append(rel)
            except OSError:
                failed.append(rel)
        out["files_deleted"] = len(deleted)
        out["files_failed"] = failed
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
                   f"原件已物理删除 {out['files_deleted']} 份")
    return out


@app.post("/api/candidates/{cid}/purge")
def api_candidate_purge(cid: int,
                        x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                        x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """彻底删除**单个**已归档的人（v1.24.8：取消 30 天冷静期，当场生效）。

    仍然要求"已归档"——人才库和检索里的人不给这条按钮，得先归档，
    避免误点直接抹掉在用的人。审计留痕 + 原件一并物理删除（不可恢复）。
    """
    s = _session(x_tp_token, x_tp_role)
    require(s, "set_stage")
    conn = db.connect(DB_PATH)
    try:
        meta = db.archive_meta(conn)
        info = meta.get(cid)
        if info is None:
            raise HTTPException(status_code=404, detail="未找到该候选人（或该档案未归档）")
        # 不再校验 days_left（v1.24.8 取消冷静期）
        # **只删这个人**（v1.13.6 修：原来这里删的是"所有到期归档的人"）
        out = _purge_expired(conn=conn, actor=s["username"], role=s["role"], only_ids=[cid])
        got = [p for p in out["people"] if p["id"] == cid]
        if not got:
            raise HTTPException(status_code=409,
                                detail="未删除：该档案可能已被取消归档或并发改过。"
                                       "请刷新后重试")
        return {"ok": True, "id": cid, "purged": got[0],
                "files_deleted": out["files_deleted"],
                "note": "已彻底删除（原件已物理删除，不可恢复）"}
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
                  f"{out['days']} 天的人（原件已物理删除 {out['files_deleted']} 份）")
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
                "note": "仅姓名完全一致时提示疑似重复；不会自动合并，合并需 HR 确认且可撤销"}
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
    #: v1.29：结论标签（可选）。只在「离开的阶段」允许它时才认：
    #: 已面试→不录用；已发放offer→已签约/拒签。不给 = 不改结论。
    #: 想**清空**结论传空串 ""（与"不改"区分开）。
    #: v2 的「只改结论」会传当前阶段；若当前还在「已面试」之前，
    #: 服务端会把实际阶段自动推进到「已面试」。
    outcome: str | None = None


class NoteReq(BaseModel):
    note: str


@app.post("/api/applications/{aid}/tier")
def api_set_tier(aid: int, req: TierReq, x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                 x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    s = _session(x_tp_token, x_tp_role)
    require(s, "confirm")
    if req.tier not in ("A", "B", "C", "D"):
        raise HTTPException(status_code=400,
                            detail="档位只能是 " + tiers_mod.enum_str(" / "))
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
        _before = db.get_application(conn, aid)
        if not _before:
            raise HTTPException(status_code=404, detail="未找到该投递")
        _actual_stage = stages_mod.auto_advance_stage(
            _before.get("stage"), req.stage, req.outcome)
        # v1.29：结论标签一起落库；自动归档由 db.set_application_stage **统一负责**
        # （它会先确认该人所有投递都已到终点）。原来这里还写了一份"看到已结束就归档"的
        # 无条件版本 —— 那人若同时在两个岗位流程里，结束一个就把整档收起来是错的。
        try:
            r = db.set_application_stage(conn, aid, _actual_stage, req.outcome,
                                         s["username"], s["role"])
        except ValueError as exc:
            # 非法阶段/非法结论：如实回 400 并说清可用取值，别让它变成 500
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if not r:
            raise HTTPException(status_code=404, detail="未找到该投递")
        archived = bool(r.pop("_auto_archived", False))
        _before_stage = str(_before.get("stage") or "").strip()
        _advanced_to = (_actual_stage
                        if _actual_stage != _before_stage and req.stage == _before_stage
                        else None)
        return {**r, "auto_archived": archived, "advanced_to": _advanced_to,
                "note": ("阶段已更新，并已自动归档该候选人"
                         "（他的投递都走完了；可在「归档」页随时取消归档）。"
                         if archived else "")}
    finally:
        conn.close()


def _export_filters(overseas, bachelor, date_from, date_to,
                    interview_unit, cet, univ, stage, education, kw) -> dict:
    """把导出接口的筛选参数收成 `list_candidates` 的 kwargs —— **一处转换**。

    为什么不让导出接口自己拼：界面上能筛、导出不能筛是这类需求最常见的漏。
    转换放一处，导出与界面永远同一口径。
    """
    _f: dict = {}
    if kw:
        _f["keyword"] = kw
    if education:
        _f["education"] = education
    if univ:
        _f["univ"] = univ
    if cet:
        _f["cet"] = cet
    if overseas:
        _f["overseas"] = overseas
    if bachelor:
        _f["bachelor"] = bachelor
    if date_from:
        _f["date_from"] = date_from
    if date_to:
        _f["date_to"] = date_to
    if interview_unit:
        _f["interview_unit"] = interview_unit
    if stage:
        _f["stage"] = stage
    return _f


def _export_response(headers: list[str], rows: list[list], text_cols: set[int],
                     meta: str, scope: str, n: int, fmt: str, stamp: str):
    """导出响应：xlsx（默认）或 csv。**同时把导出动作写审计。**"""
    if (fmt or "xlsx").lower() == "csv":
        data = xlsx_mod.csv_bytes(rows, headers)
        media = "text/csv; charset=utf-8"
        ext = "csv"
    else:
        data = xlsx_mod.write_sheet(rows, headers, text_cols=text_cols,
                                    meta=meta, sheet_title=scope[:31])
        media = ("application/vnd.openxmlformats-officedocument"
                 ".spreadsheetml.sheet")
        ext = "xlsx"
    conn = db.connect(DB_PATH)
    try:
        db.add_audit(conn, "export", scope, "export", f"{n} 条",
                     f"导出「{scope}」{n} 条｜{meta}",
                     _export_operator[0], _export_operator[1])
    finally:
        conn.close()
    name = urllib.parse.quote(f"{scope}-{stamp}.{ext}")
    return Response(
        content=data, media_type=media,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{name}",
                 "X-Export-Rows": str(n)})


#: 导出接口的操作用户（`_export_response` 里写审计要用；由路由设置）
_export_operator = ["HR", "hr"]


@app.get("/api/export/roster")
def api_export_roster(scope: str = "候选人名单",
                       fmt: str = "xlsx", format: str | None = None,
                       overseas: str | None = None, bachelor: str | None = None,
                       date_from: str | None = None, date_to: str | None = None,
                       interview_unit: str | None = None, cet: str | None = None,
                       univ: str | None = None, stage: str | None = None,
                       education: str | None = None, kw: str | None = None,
                       x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                       x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> Response:
    """导出候选人名单 —— **唯一的导出入口**（业务方 2026-10-15 定调）。

    三条口径钉死：
      · **一份名单一套列**：固定 13 列，不随阶段增减；没走到那个阶段的字段给 `—`。
      · **含归档**：自动归档之后那些人从人才库看不见了，而这恰恰是「是否录用」
        列里最该出现的一批（不录用 / 拒签）。漏了他们，这张表只剩好消息。
      · **跟界面同一套筛选**（`poolQuery` 同一个真源），所以不会出现
        "界面上筛了 8 人、导出给了全库"。

    每次点击**实时查库**（不缓存、不做快照），永远是最新数据；导出动作写审计
    （13 列含电话明文、是要外发的，这是受控动作）。
    """
    s = _session(x_tp_token, x_tp_role)
    require(s, "read")
    _export_operator[0], _export_operator[1] = s["username"], s["role"]
    conn = db.connect(DB_PATH)
    try:
        headers, rows, tcols, n = export_mod.build_roster(
            conn, operator=s["username"], role=s["role"], include_archived=True,
            **_export_filters(overseas, bachelor, date_from, date_to,
                              interview_unit, cet, univ, stage, education, kw))
    finally:
        conn.close()
    meta = xlsx_mod.meta_line(scope, "（含已归档；未加其它筛选）", n)
    # `fmt` 与 `format` 两个名字都收：HR/脚本两种写法都会用，
    # 而"传了 format=csv 却拿到 xlsx"是那种**看起来成功**的错。
    return _export_response(headers, rows, tcols, meta, scope, n, format or fmt,
                            datetime.now().strftime("%Y%m%d-%H%M"))


@app.post("/api/bundle/initial-pass")
def api_bundle_initial_pass(x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                            x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> Response:
    """把**初筛通过**人员的简历打包下载，文件名按 `匹配岗位-学历-学校-最高学历专业-姓名-性别`。

    ⚠️ 只改**包内名**，不动归档原件的文件名（原件是审计凭据，见 `app/naming.py` 的规则 2）。

    v2.0.1（业务方要求）：**包里再附一份「初筛通过名单」Excel**，
    用的是导出入口那同一套 13 列（`export.bundle_roster` → `build_roster`），
    所以"简历包里那张表"和"导出的名单"永远是一套口径。
    """
    s = _session(x_tp_token, x_tp_role)
    require(s, "read")
    conn = db.connect(DB_PATH)
    try:
        zname, pairs = export_mod.bundle_name(conn, {}, operator=s["username"],
                                              role=s["role"], scope="初筛通过",
                                              n=0)
        # 表是**每次点击现查库**生成的，没有磁盘路径 → 走 _zip_response 的 extra 通道
        roster = export_mod.bundle_roster(conn, operator=s["username"], role=s["role"])
        db.add_audit(conn, "bundle", "initial-pass", "bundle_download", "",
                     f"下载初筛通过简历 {len(pairs)} 份，附名单表 {roster['n']} 条",
                     s["username"], s["role"])
    finally:
        conn.close()
    if not pairs:
        raise HTTPException(status_code=404,
                            detail="当前筛选下没有可打包的简历（只有已入库并留了原件的候选人）")
    return _zip_response(pairs, zname,
                         extra=[(roster["filename"], roster["data"])])


@app.post("/api/applications/{aid}/interview-info")
def api_interview_info(aid: int, req: dict,
                       x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                       x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """登记 / 更正**面试与录用信息**（人工维护入口）。

    分工（v1.29）：
      · **人工**：面试单位、面试日期/时段/方式（发信时会自动回写，HR 也可手工改）、
        签约时间、入职最后期限、入职情况。
      · **自动**：录用函发送时间（发信成功那一刻写入，不给手工入口 ——
        它是后面所有时限的起算点，HR 常记错）。

    校验只挡"明显不合法"（日期格式、入职情况的取值），
    不在这里做名单/口径判断 —— 那是配置（universities.json / interview.json）的事。
    """
    s = _session(x_tp_token, x_tp_role)
    require(s, "chat")
    conn = db.connect(DB_PATH)
    try:
        if not db.get_application(conn, aid):
            raise HTTPException(status_code=404, detail="未找到该投递")
        patch: dict = {}
        advanced = False        # v1.29.3：本次是否顺带推进了阶段（待面试 → 已面试）

        def _date(key: str) -> None:
            if key in req:
                v = str(req.get(key) or "").strip()[:10]
                if v and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
                    raise HTTPException(status_code=422, detail=f"{key} 格式应为 YYYY-MM-DD")
                patch[key] = v or None

        def _text(key: str, maxlen: int) -> None:
            if key in req:
                patch[key] = (str(req.get(key) or "").strip()[:maxlen] or None)

        _text("interview_unit", 40)        # 面试单位（=面试部门）
        _text("interview_slot", 24)        # 面试时段原文
        _text("interview_mode", 16)        # 面试方式（现场 / 线上）
        _text("signed_at", 32)             # 签约时间
        _date("interview_date")            # 面试日期 YYYY-MM-DD
        _date("onboard_deadline")           # 入职最后期限（**人工维护**）
        # v1.29.2：面试结论（拟录用 / 不录用）。
        # 打「不录用」**不自动进「已结束」并归档** —— 归档是不可逆的隐藏动作，
        # 而结论可能还要改；但人若还在「已面试」之前，需要同步推进到「已面试」。
        if "outcome" in req:
            v = str(req.get("outcome") or "").strip()
            # 取值范围 = 配置里**「已面试」这一档**允许的标签，不写死字面量
            _ok = stages_mod.outcomes_of("已面试")
            if v and v not in _ok:
                raise HTTPException(
                    status_code=422,
                    detail=f"面试结论只能是：{'、'.join(_ok) or '（未配置）'}（清空表示未判）")
            # v1.29.3（业务方 2026-10-16）：**在「已面试」之前打结论标签 → 自动进「已面试」**。
            # 当前配置下能打这两个结论的前序阶段是「待面试」；这里仍走统一判定，
            # 避免把阶段名散落成多份。已在「已面试」或之后只改标签，
            # 要不要推进到已发放offer 是另一步，由 HR 单独决定。
            # ⚠️ 放**服务端**而不是前端：助手提案、脚本调接口也应该同一套行为 ——
            # 流转规则写在界面里，就等于只有界面那条路算数。
            _cur_stage = stages_mod.normalize_stage(
                (db.get_application(conn, aid) or {}).get("stage"))
            _target_stage = stages_mod.auto_advance_stage(_cur_stage, _cur_stage, v)
            if v and _target_stage != _cur_stage:
                db.set_application_stage(conn, aid, _target_stage, outcome=v,
                                         operator=s["username"], role=s["role"],
                                         skip_auto_archive=True)
                advanced = True          # outcome 由 set_application_stage 写，不重复
            else:
                patch["outcome"] = v or None
        # v1.29.2：录用函发送时间。发录用函时**系统自动写**；这里允许人事手填
        # （比如函是纸质/由别人代发的）。手填前端会弹一次确认。
        if "offer_sent_at" in req:
            v = str(req.get("offer_sent_at") or "").strip()[:19]
            if v and not re.fullmatch(r"\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?", v):
                raise HTTPException(status_code=422,
                                    detail="录用函发送时间格式应为 YYYY-MM-DD 或 YYYY-MM-DD HH:MM")
            patch["offer_sent_at"] = v.replace("T", " ") or None
        if "onboard_status" in req:
            v = str(req.get("onboard_status") or "").strip()
            if v and v not in ONBOARD_STATUS:
                raise HTTPException(status_code=422,
                                    detail=f"入职情况只能是：{'、'.join(ONBOARD_STATUS)}")
            patch["onboard_status"] = v or None
        # 只带日期没带时段时，把时段起始时间补进 interview_date
        #（导出列要显示到分钟；HR 事后只改日期的也走这条）
        if ("interview_date" in patch and patch.get("interview_date")
                and not patch.get("interview_slot") and "interview_slot" not in patch):
            cur = db.get_application(conn, aid) or {}
            _full = cur.get("interview_date") or ""
            _t = _SLOT_START_RE.match(str(cur.get("interview_slot") or "").strip())
            patch["interview_date"] = (f"{patch['interview_date']} "
                                       f"{int(_t.group(1)):02d}:{int(_t.group(2)):02d}"
                                       ) if _t else patch["interview_date"]
        if not patch:
            # ⚠️ 这里**不能**只看 patch：在「待面试」打结论标签时 outcome 是由
            # `set_application_stage` 写的（patch 里当然为空），但阶段**确实推进了**。
            # 只按 patch 判断会回一句"没有要修改的字段" —— 而界面上阶段明明变了，
            # HR 会以为系统没记（实测踩到）。
            if advanced:
                _ap = db.get_application(conn, aid) or {}
                return {"ok": True, "id": aid, "changed": ["stage", "outcome"],
                        "advanced": True,
                        "application": {k: _ap.get(k) for k in ("stage", "outcome")},
                        "note": "已保存：面试结论 + 阶段已推进到「已面试」。"}
            return {"ok": True, "id": aid, "changed": [], "advanced": False,
                    "note": "没有要修改的字段。"}
        r = db.update_application_fields(conn, aid, operator=s["username"],
                                         role=s["role"], **patch) or {}
        # v1.29.4（业务方）：人事处**填完面试基本信息**（面试单位已登记）而阶段
        # 还在「待面试」之前时 → 自动推进到「待面试」，回话带 `advanced_to`，
        # 前端弹提醒。判定用**保存后的实际状态**（不看 patch）：单位可能这次填的、
        # 也可能早先登记过。与「打结论自动进已面试」同一纪律 —— 联动写**服务端**，
        # 助手/脚本走同一套行为，不只在界面那条路生效。
        _ap_now = db.get_application(conn, aid) or {}
        _stage_now = stages_mod.normalize_stage(_ap_now.get("stage"))
        _order = stages_mod.all_stages()
        _promoted = False
        if (_ap_now.get("interview_unit") or "").strip():
            if (_stage_now in _order and "待面试" in _order
                    and _order.index(_stage_now) < _order.index("待面试")):
                _promoted = True
        if _promoted:
            db.set_application_stage(conn, aid, "待面试", operator=s["username"],
                                     role=s["role"], skip_auto_archive=True)
        _changed = sorted(patch) + (["stage", "outcome"] if advanced else [])
        if _promoted:
            _changed = sorted(set(_changed) | {"stage"})
        if advanced:
            _note = "已保存：面试结论 + 阶段已推进到「已面试」。"
        else:
            _note = ("已保存：" + "、".join(
                         db.APPLICATION_FIELD_LABEL.get(k, k) for k in sorted(patch))
                     if patch else "已清除面试结论")
            _note += ("。阶段已自动推进到「待面试」（面试单位已登记，已写审计）。"
                      if _promoted else "（改前改后都记在操作审计里）。")
        return {"ok": True, "id": aid,
                "changed": _changed,
                "advanced": advanced,
                "advanced_to": "待面试" if _promoted else None,
                "application": {k: r.get(k) for k in patch},
                "note": _note}
    finally:
        conn.close()


@app.post("/api/candidates/{cid}/cet")
def api_candidate_cet(cid: int, req: dict,
                      x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                      x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """人工更正英语四六级（v1.27）。

    接受 `level`（归一档位，如 '六级'）或 `raw`（简历原文表述）。给 `level` 时
    **同时把 raw 写成档位名** —— 库里只有一列 `cet`，存原文；两个字段分开存
    会出现"档案显示六级、筛选按原文判成没写"这种对不上。
    两者都空 = 清空（回到"未提供"）。

    写审计：`AUDIT_FIELD_LABEL["cet"]` 会把它显示成「英语四六级」。
    """
    s = _session(x_tp_token, x_tp_role)
    require(s, "chat")
    req = req or {}
    level = str(req.get("level") or "").strip()
    raw = str(req.get("raw") or "").strip()
    if level:
        if level not in set(extract_mod.CET_FILTER_LEVELS):
            raise HTTPException(status_code=422,
                                detail=f"四六级只能是：{'、'.join(extract_mod.CET_FILTER_LEVELS)} 之一")
        value = level            # 档位名本身也是合法的原文表述，归一后还是它
    else:
        value = raw
    conn = db.connect(DB_PATH)
    try:
        d = db.candidate_detail(conn, cid)
        if not d:
            raise HTTPException(status_code=404, detail="未找到该候选人")
        old = (d.get("cet") or "").strip()
        if old == value:
            return {"ok": True, "id": cid, "changed": [],
                    "note": "四六级没有变化。"}
        db.update_candidate(conn, cid, cet=value or None)
        db.add_audit(conn, "candidate", str(cid), "edit_fields",
                     f"cet={extract_mod.cet_level(old) or '（空）'}",
                     f"cet={extract_mod.cet_level(value) or '（空）'}",
                     s["username"], s["role"])
        return {"ok": True, "id": cid, "changed": ["cet"],
                "cet_level": extract_mod.cet_level(value),
                "note": ("四六级已保存。" if value else "四六级已清空（筛选时算未提供）。")}
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


def _safe_ingest_dir(value: str | None, cfg: dict) -> str:
    """校验导入目录（代码审查 P1-12）。

    规则只有一份，在 `ingest.resolve_source_dir`——**服务端与助手工具共用同一条**，
    免得两处各写一份慢慢分叉（接口收紧了、助手那条路还敞着，等于没修）。
    这里只负责把 `ValueError` 翻译成 HTTP 状态码。
    """
    try:
        return ingest_mod.resolve_source_dir(value, cfg)
    except ValueError as exc:
        raise HTTPException(status_code=400 if ".." in (value or "") else 403, detail=str(exc))


@app.post("/api/ingest")
def api_ingest(req: IngestReq | None = Body(default=None),
               x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
               x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """开始收取/导入（**异步任务**，v1.29.4）。

    立即返回 `task_id`；进度与最终报表由 `/api/tasks/{task_id}` 轮询，
    停止用 `/api/tasks/{task_id}/stop`（读完当前文件即停）。

    为什么拆成任务：业务方要求"先报数量、实时进度、可优雅停止，
    读取完成后才逐条分析"—— 同步请求没有中途通道，做不到这三件事。
    """
    s = _session(x_tp_token, x_tp_role)
    require(s, "ingest")
    req = req or IngestReq()

    # 路径类校验留在这里同步做：目录错/建不出来当场 4xx，不进任务排队
    cfg = mb.load_config()
    folder = None
    if req.source == "folder":
        folder = _safe_ingest_dir(req.folder, cfg)
        # v1.24.20：目录不存在时**建出来并说清楚**，不要静默扫0。
        if not os.path.isdir(folder):
            try:
                os.makedirs(folder, exist_ok=True)
            except OSError as exc:
                raise HTTPException(status_code=400,
                                    detail=f"导入文件夹不存在且无法创建：{folder}（{exc}）")
    title = "收取邮箱简历" if req.source != "folder" else "导入本地文件夹"
    tid = ingest_mod.register_progress(title, 0, stoppable=True)
    threading.Thread(target=_run_ingest_task, name="ingest-fetch", daemon=True,
                     args=(tid, req.source, folder, s.get("username") or "hr",
                           req.use_llm)).start()
    return {"ok": True, "task_id": tid}


def _run_ingest_task(tid: str, source: str, folder: str | None,
                     operator: str, use_llm: bool | None) -> None:
    """后台执行"收取/导入"的**抓取阶段**。

    口径与旧同步接口严格一致（同一个 ingest_dir / sync_mailbox）；
    区别只有两处：带进度（progress_tid）与把最终报表挂在任务上供前端接力。
    分析阶段不在这里跑 —— `spawn_auto_analysis`（在 ingest 内部）会把
    `insight_task_id` 写进报表，前端读取完成后用同一个弹窗接力轮询分析进度。
    """
    try:
        jd, tiers = _jd_tiers()
        _use_llm = use_llm
        if _use_llm is None:
            _c0 = llm.load_cfg()
            _use_llm = bool(_c0.get("api_key"))
        llm_conf = None
        if _use_llm:
            c = llm.load_cfg()
            llm_conf = {"api_key": c.get("api_key"), "base_url": c.get("base_url"),
                        "model": c.get("model")}
        cfg = mb.load_config()
        if source == "folder":
            report = ingest_mod.ingest_dir(folder, jd, tiers, DB_PATH, cfg=cfg, job_id=None,
                                           use_llm=_use_llm, llm_conf=llm_conf,
                                           progress_tid=tid)
            report["source"] = "folder"
            report["source_label"] = f"本地文件夹 {os.path.abspath(folder)}"
            # 扫到 0 个文件要**说清是哪一种 0**（v1.24.20 的口径保持不变）
            if not report.get("scanned"):
                report["empty_hint"] = (
                    f"这个文件夹里没有可导入的简历。目录：{os.path.abspath(folder)}"
                    "（新建的空目录）。把 PDF / Word / 图片放进去再点一次即可；"
                    "如果简历在别处，可在系统配置里改「导入文件夹」路径。")
        else:
            report = ingest_mod.sync_mailbox(jd, tiers, DB_PATH, job_id=None,
                                             use_llm=_use_llm, llm_conf=llm_conf,
                                             cfg=cfg, progress_tid=tid, limit=0)
            ic = cfg.get("imap") or {}
            report["source"] = "mailbox"
            report["source_label"] = (
                f"邮箱 {ic.get('user') or '（未配置账号）'} @ "
                f"{ic.get('host') or '（未配置服务器）'} / {ic.get('folder','INBOX')}"
                if cfg.get("mode") == "imap"
                else f"演练邮件目录 {mb.resolve_dir(cfg.get('eml_dir',''))}")
        # 导入后自动做**增量**建索引：只补"画像变了/还没索引"的人，不做全量重建
        report["index"] = _auto_index()
        _remember_ingest(report, operator)
        if report.get("error") and not (report.get("mails") or report.get("added")):
            ingest_mod.update_progress(tid, status="error",
                                       message=f"收取失败：{report['error']}",
                                       extra={"report": report})
        else:
            ingest_mod.update_progress(
                tid, status="done",
                message=("已按你的要求停止：完成当前文件后停下"
                         if report.get("stopped") else "读取完成"),
                extra={"report": report})
    except Exception as exc:                       # noqa: BLE001 —— 后台任务不能让异常静默
        traceback.print_exc()
        ingest_mod.update_progress(tid, status="error",
                                   message=f"导入任务出错：{type(exc).__name__}: {exc}")


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

    `?token=` 的代价是令牌会落进浏览器历史、`Referer` 头与代理日志。彻底改法是
    一次性签名（`文件名 + 过期时间 + HMAC`）；在改成签名之前，先用响应头把两处
    最容易漏的地方堵上（代码审查 P1-17）：
    · `Referrer-Policy: no-referrer`——跳转/内联资源不再把带令牌的 URL 带出去；
    · `Cache-Control: no-store`——不进磁盘缓存，换机器/公用机不留痕。
    """
    return _session(header_token or query_token, None)


#: 文件类响应的兜底安全头（配合 `_file_session` 的说明一起看）
_FILE_SAFE_HEADERS = {"Referrer-Policy": "no-referrer",
                      "Cache-Control": "private, no-store",
                      "Pragma": "no-cache"}


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
    path = _abs_stored(path)
    if not path:
        raise HTTPException(status_code=404, detail="原件未记录路径")
    # 只允许发送本仓库目录下的文件，避免被构造路径读到系统文件。
    # 顺序要点：**先判越界、再判存在**。若反过来，构造一个仓库外但不存在的路径
    # 会先得到 404——等于把"该文件在不在"变成了可探测的信息（存在性 oracle）；
    # 而且自检里"构造越界路径必须被拒"这条断言，在 Windows（该路径不存在）
    # 上会拿到 404 而不是 403，掩盖真实的越界分支是否生效。
    real = os.path.realpath(path)
    if not _inside_allowed_root(real):
        raise HTTPException(status_code=403, detail="原件路径超出允许范围，已拒绝")
    if not os.path.isfile(path):
        raise HTTPException(status_code=404, detail=f"原件不在磁盘上：{path}")

    name = d.get("file_name") or os.path.basename(real)
    disposition = "inline" if inline else "attachment"
    return FileResponse(real, filename=name,
                        content_disposition_type=disposition,
                        media_type=d.get("mime") or _mime_of(name),
                        headers=_FILE_SAFE_HEADERS)


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


def _zip_response(pairs: list[tuple[str, str]], zip_name: str,
                  extra: list[tuple[str, bytes]] | None = None) -> Response:
    """把若干磁盘文件打成 zip 一次性下发。`pairs` 是 (磁盘路径, 包内文件名)。

    `extra`（v2.0.1）：**内存里现算、不落磁盘**的附加条目 `[(包内文件名, 字节)]`。
    为什么要这条通道：随简历包下发的名单表是**每次点击现查库生成的**，没有磁盘路径
    可以塞进 `pairs`；而"先写临时文件再打包"会留下要清理的残留，
    并发点两次还可能同名打架。
    """
    if not pairs and not extra:
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
        # 内存生成的条目（名单表等）：同一条去重规则，中文名由 zipfile 自动打 UTF-8 flag
        for name, data in (extra or []):
            z.writestr(_unique_name(os.path.basename(name), used), data)
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
                        media_type=_mime_of(base),
                        headers=_FILE_SAFE_HEADERS)


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
                path = _abs_stored(path)
                if not path:
                    skipped.append(f"{d.get('file_name') or did}（未记录路径）")
                    continue
                # 同 /api/documents/{id}/file：先判越界再判存在，
                # 否则越界文件会被记成"原件不在磁盘上"，把实情报错。
                real = os.path.realpath(path)
                if not _inside_allowed_root(real):
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
            full = _abs_stored(raw)
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
        # 包名不合法（绝对路径 / 越界 / 不存在）是**调用方的输入问题**，不是服务端故障：
        # 走 400 说清楚，别让它变成 500 再把内部路径回显出去。
        try:
            if run:
                r = domains_mod.preview_import(req.pack)
                return {"ok": True, "dry_run": True, "result": r,
                        "note": "这是预演，没有改动任何文件。确认无误后用 dry_run=false 落地。"}
            r = domains_mod.import_pack(conn, req.pack, operator=s["username"])
        except (ValueError, FileNotFoundError) as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        return {"ok": True, "dry_run": False, "result": r,
                "note": "已导入并刷新库内技能分类。已有投递要按新口径重算，"
                        "请在对应岗位上点「重新分析」（会先给差异预览）。"}
    finally:
        conn.close()


# ============================================================
# 设置（入库即分析开关、超期天数等；开关类设置变更一律留痕）
# ============================================================

class SettingsReq(BaseModel):
    # v1.13.4：入库即分析开关（默认开）。关掉后入库不自动调模型，改由 HR 手动批量补。
    auto_insight_on_ingest: bool | None = None
    # v1.25.7：投递管道的"超期"标红阈值（天），默认 15。
    pipeline_overdue_days: int | None = None


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
                "note": "性别按普通字段处理（2026-10-16 业务方定调）：可正常筛选与展示；"
                        "它只影响「列出谁」，不影响任何人被评成什么档。",
                "policy": "性别只取简历明写的标签行（绝不推断），且永不进入评分与分级。"}
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
    """改设置。变更一律留痕（写审计）。"""
    s = _session(x_tp_token, x_tp_role)
    require(s, "settings")
    conn = db.connect(DB_PATH)
    try:
        before = _settings(conn)
        if getattr(req, "auto_insight_on_ingest", None) is not None:
            db.set_setting(conn, AUTO_INSIGHT_KEY, bool(req.auto_insight_on_ingest))
        if getattr(req, "pipeline_overdue_days", None) is not None:
            # v1.25.7：投递管道超期阈值（标红口径）——1~365 天
            _od = int(req.pipeline_overdue_days)
            if not (1 <= _od <= 365):
                raise HTTPException(status_code=422, detail="超期天数应在 1-365 之间")
            db.set_setting(conn, "pipeline_overdue_days", _od)
        after = _settings(conn)
        _ai_before = bool(before.get("auto_insight_on_ingest"))
        _ai_after = bool(after.get("auto_insight_on_ingest"))
        _od_b = int(before.get("pipeline_overdue_days") or 15)
        _od_a = int(after.get("pipeline_overdue_days") or 15)
        if before != after:
            for _k, _b, _a in (("auto_insight_on_ingest", _ai_before, _ai_after),
                               ("pipeline_overdue_days", _od_b, _od_a)):
                if _b != _a:
                    db.add_audit(conn, "settings", _k, "update",
                                 f"{_k}={_b}", f"{_k}={_a}",
                                 s["username"], s["role"])
        _changed = []
        if _ai_after != _ai_before:
            _changed.append("入库即分析已开启：新简历入库后自动分析一次（每人约 2-8 秒、"
                            "消耗模型额度）。" if _ai_after
                            else "入库即分析已关闭：新简历入库不再自动调模型，"
                                 "可在人才库点「分析待分析的人」批量补上（已写入审计）。")
        if _od_a != _od_b:
            _changed.append(f"超期天数已改为 {_od_a} 天。")
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
    # v1.29.6（业务方）：招聘数量（招聘需求，计划招几人）。纯管理信息，不参与判定。
    headcount: int | None = None


class JobJdReq(BaseModel):
    must_skills: list[str] | None = None
    preferred_skills: list[str] | None = None
    education_min: str | None = None
    years_min: int | None = None
    major_required: list[str] | None = None
    note: str | None = None
    # 招聘数量（v1.29.6）：None=没传（保持原值），0=清空（未填）
    headcount: int | None = None


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
        if req.headcount is not None and req.headcount < 0:
            raise HTTPException(status_code=400, detail="招聘数量请填非负整数")
        jd = _jd_from_req(_load(JD_PATH), req, req.title.strip(), dept_name)
        jid = db.create_job(conn, req.title, dept_id=did, jd=jd,
                            operator=s["username"], headcount=req.headcount)
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
        if req.headcount is not None and req.headcount < 0:
            raise HTTPException(status_code=400, detail="招聘数量请填非负整数")
        base = j.get("jd_json") or {}
        merged = _jd_from_req(base, req, j.get("title") or "", j.get("department_name") or "")
        # 只覆盖本次传了的字段：None 表示"界面没提供"，保持原值
        cur = dict(base)
        cur.update({k: v for k, v in merged.items() if k in ("must", "preferred", "note", "origin")})
        saved = db.update_job_jd(conn, jid, cur, s["username"], headcount=req.headcount)
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

    权限用 `propose` 而不是 `read`（代码审查 P1-18）：它虽然不直接改业务数据，
    但会往 `proposals` 表**落库**（持久化副作用），按只读授权与「写操作需写权限」
    的口径不一致。
    """
    s = _session(x_tp_token, x_tp_role)
    require(s, "propose")
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
    """给还没算出毕业时间/**在读状态**的候选人补一次（以解析原文为准）。

    为什么单独补：入库路径拿到的文本可能已被处理过，识别不到"毕业"字样；
    而 `documents.raw_text` 是解析原文，一定带着教育经历。启动时补一次，
    新导入的人下次启动也会被补齐——与"存量补分析"同一套兜底思路。
    v1.29.4 起顺带补"在读"（硕士 2024.9-至今 这类；判"往届未就业"前必须知道）。
    """
    from .pipeline import freshness
    conn = db.connect(DB_PATH)
    n = 0
    try:
        rows = conn.execute(
            """SELECT c.id, c.grad_date, c.in_school, d.raw_text FROM candidates c
               JOIN documents d ON d.candidate_id = c.id
               WHERE (COALESCE(c.grad_date, '') = '' OR c.in_school IS NULL)
                 AND COALESCE(d.raw_text, '') != ''
               ORDER BY c.id LIMIT ?""", (int(limit),)).fetchall()
        for r in rows:
            gd = r["grad_date"] or freshness.find_grad_date(r["raw_text"])
            ins = 1 if freshness.find_in_school(r["raw_text"]) else 0
            if (gd or None) != (r["grad_date"] or None) or r["in_school"] != ins:
                conn.execute(
                    "UPDATE candidates SET grad_date = ?, in_school = ? WHERE id = ?",
                    (gd, ins, r["id"]))
                n += 1
        conn.commit()
        if n:
            db.add_audit(conn, "candidate", "*", "backfill_grad_date", "",
                         f"为 {n} 位候选人补齐毕业时间/在读判定（用于判定应届/往届）",
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


class MailAttachmentReq(BaseModel):
    """一封邮件的附件请求（v1.27）。只传 `document_id`，**不接受文件内容**。

    为什么不让界面直接传字节/路径：
    ① 传路径 = 让前端指定服务端读哪个文件，本地部署下就是任意文件读取
      （与 code review 的 P1-12 同类问题），不新增这个面；
    ② 传 base64 内容 = 同一份简历可能几十 MB，白白走一遍 HTTP 与内存。

    服务端只认"库里已归档的原件 id"，所以能发出去的附件一定是
    HR 手上真有的东西，且审计留得下（发出去了哪几个附件 id）。
    """
    #: v1.29.9：**二选一** —— 归档原件（document_id）或临时上传的 outbox 记录
    #: （outbox_id）。此前 document_id 是必填，于是"只带临时附件（offer 扫描件
    #: 那类）"的邮包在校验层就被 422 拦下，界面显示 [object Object]（业务方实测）。
    document_id: int | None = None
    filename: str | None = None            # 改名（默认用库里的原名）

    # v1.29：`outbox_id` = **临时上传**的附件（录用通知扫描件那类）。
    # 为什么不复用 document_id：那个通道只收"库里已归档的求职者原件"，
    # 而录用函扫描件**不属于任何候选人** —— 硬塞进 documents 会污染
    # "原件留档"的语义（HR 打开某人档案会看到一份跟他无关的录用函）。
    # 这里单开一条通道：文件落在受控目录 data/outbox/，**不入库**，
    # 发出后仍留一份（合规：录用函发出记录要能追溯附件是哪份）。
    outbox_id: str | None = None
    filename2: str | None = None           # 上传时的原名（outbox 附件改名用）


class MailSendReq(BaseModel):
    to: str
    subject: str
    body: str = ""                       # 纯文本正文（给了 html 时可留空，服务端会拆出来）
    html: str | None = None              # 富文本正文（所见即所得编辑器产出的 HTML）
    candidate_id: int | None = None
    application_id: int | None = None
    template_name: str | None = None
    attachments: list[MailAttachmentReq] | None = None   # v1.27：附件（原件 id）
    #: 浏览器为同一次“确认发送”生成的幂等键。网络中断后重试同一请求时，
    #: 服务端返回原任务，绝不重复投递 SMTP。
    client_request_id: str | None = None
    #: v1.29：HR 在写邮件页填好的**结构化**运行时变量
    #: （面试时间 / 面试时段 / 面试单位 / 面试方式 / 会议号 …）。
    #: 发信成功后按**模板用途**回写进投递档案，省得 HR 再手工录一遍。
    #: ⚠️ 为什么要前端回传而不解析正文：正文是自然语言
    #:（"本次采用线上视频面试""腾讯会议进行"……写法无穷），解析必然出错。
    #: 前端本来就有一份结构化值（`mv_面试方式` 等），顺手回传即是一处真相。
    vars: dict | None = None


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
                     f"保存邮件模板「{name}」", s["username"], s["role"])
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
            "note": "163 / QQ / 腾讯企业邮都需先开启 SMTP 服务并生成授权码（不是登录密码）。"}


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
        # v1.27：发信授权码写**独立**的 config/smtp.secret（不再写 imap.secret）。
        # 改这里时若还写 imap.secret，会把收信的凭据顶掉 —— 收发信两个邮箱
        # 是两件独立的事，共用一个文件等于"改一个坏一个"。
        mb.write_smtp_secret(req.password)
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


#: 面试时段起始时间。`^\s*(\d{1,2})[:：](\d{2})` —— 组合框允许手打（"9点半"这种
#: 取不到），取不到就**只存日期、不猜时间**（填个假时间比留空更糟）。
_SLOT_START_RE = re.compile(r"^\s*(\d{1,2})\s*[:：]\s*(\d{2})")

#: 入职情况的取值（与 `db.APPLICATION_WRITABLE` 里的 onboard_status 配套）
ONBOARD_STATUS = ("未入职", "已入职", "放弃")


def _interview_at(date_str: str | None, slot_str: str | None) -> str | None:
    """面试时间 + 面试时段 → `YYYY-MM-DD HH:MM`（存进 `interview_date` 一列）。

    为什么两列存一列：`interview_date` 存"面试安排在什么时候"，
    `interview_slot` 保留时段**原文**（"09:30-10:30" / "上午"）——
    改时段不必重填日期，档案里也能看出 HR 原本填的是什么。
    导出时只显示 `面试日期` 一列（起始时间），时段原文不进表。
    """
    d = str(date_str or "").strip()[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
        return None
    m = _SLOT_START_RE.match(str(slot_str or "").strip())
    if not m:
        return d
    hh, mm = int(m.group(1)), int(m.group(2))
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return d
    return f"{d} {hh:02d}:{mm:02d}"


def _mail_kind(conn, template_name: str | None) -> str:
    """模板名 → **用途**（`interview_invite` / `offer` / `other`）。

    ⚠️ 为什么按"用途"而不是按模板名判断：模板名 HR 随时会改
    （「初面邀约」→「面试通知」），按名字匹配的话改个名发信回写就**静默失效**了。
    用途是结构化的字段，改名字不影响。
    """
    if not template_name:
        return "other"
    try:
        row = conn.execute("SELECT kind FROM mail_templates WHERE name = ?",
                           (str(template_name),)).fetchone()
        return str((row["kind"] if row else "") or "other")
    except Exception:                                     # noqa: BLE001
        return "other"


#: v1.29 —— 临时上传通道（录用通知扫描件那类"不属于任何候选人"的附件）。
#: 收**原始字节**而不是 multipart 表单，是为了不新增 `python-multipart` 依赖：
#: 前端 `fetch(url, {body: file})` 直接发二进制即可，服务端按 `Content-Length` 收。
OUTBOX_DIRNAME = "outbox"
OUTBOX_EXTS = (
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
    ".docm", ".dot", ".dotx", ".xlsm", ".xlsb", ".xltx",
    ".pptm", ".pps", ".ppsx",
    ".txt", ".csv", ".md", ".rtf", ".odt", ".ods", ".odp",
    ".wps", ".et", ".dps", ".ofd", ".zip", ".rar", ".7z",
    ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif",
    ".jfif", ".tif", ".tiff", ".heic", ".heif", ".avif",
)
OUTBOX_MAX_BYTES = 20 * 1024 * 1024
#: SMTP 的 25MB 限制通常按编码后的报文计算；附件原始字节合计再卡一道，
#: 避免多个单文件都合法、最后在 SMTP 阶段才失败。
OUTBOX_MAX_TOTAL_BYTES = 20 * 1024 * 1024


def _outbox_dir() -> str:
    d = os.path.join(DATA_DIR, OUTBOX_DIRNAME)
    os.makedirs(d, exist_ok=True)
    return d


def _outbox_path(outbox_id: str) -> str | None:
    """`outbox_id` → 绝对路径；**不接受任何外部路径**。

    ⚠️ id 必须严格是 `[0-9a-f]{16}`（我们自己按内容 sha256 生成）。
    这里再做一次白名单校验，是因为 `_abs_stored` 那类"把客户端给的路径还原成绝对路径"
    的写法一旦放松就是**任意文件读取**（`../../` 穿越），本地部署也一样。
    """
    oid = str(outbox_id or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{16}", oid):
        return None
    p = os.path.join(_outbox_dir(), oid)
    # v1.29.10：磁盘上的文件名是 `id + 扩展名`（id 本身不含扩展名）——原实现
    # 按 `id` 直接找，**永远找不到文件**：表现就是"带附件的邮件发不出去
    # （提示：临时附件已不在暂存区）"，业务方实测两次。
    if not os.path.exists(p):
        for _e in OUTBOX_EXTS:
            if os.path.exists(p + _e):
                p = p + _e
                break
    # 双保险：拼完再确认它确实在 outbox 目录内（防将来改动引入穿越）
    if os.path.commonpath([os.path.abspath(p), os.path.abspath(_outbox_dir())]) \
            != os.path.abspath(_outbox_dir()):
        return None
    return p


@app.post("/api/mail/outbox")
async def api_mail_outbox(request: Request, name: str = "",
                          x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                          x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """**临时上传**一个附件（录用通知扫描件那类），返回可发信的 `outbox_id`。

    为什么需要这条通道（v1.27 刻意不开）：
    原来的附件只收"库里已归档的求职者原件 id"，是**有意的收紧**（防"前端指定服务端
    读哪个文件"这个任意文件读取面）。但录用函扫描件**不属于任何候选人** ——
    硬塞进 `documents` 会污染"原件留档"的语义（HR 打开某人档案会看到一份
    跟他无关的录用函），所以这里单开一个受控通道。

    边界（都是有意收窄的）：
      · 收常见图片与办公文档（PDF/Word/Excel/PPT/TXT/压缩包等），单份 ≤ 20MB；
      · 文件名按内容 sha256 生成，**客户端给的路径一律不采信**；
      · **不入库**（不进 candidates / documents），只落在 `data/outbox/`；
      · 写审计（文件名 + 大小 + sha256 前 16 位），事后能查"发出去的是哪份"。
    """
    s = _session(x_tp_token, x_tp_role)
    require(s, "confirm")
    ext = os.path.splitext(str(name or ""))[1].lower()
    if ext not in OUTBOX_EXTS:
        raise HTTPException(status_code=422,
                            detail=f"不支持这个文件格式；可上传：{'、'.join(OUTBOX_EXTS)}")
    body = await request.body()
    if not body:
        raise HTTPException(status_code=422, detail="文件是空的")
    if len(body) > OUTBOX_MAX_BYTES:
        raise HTTPException(status_code=413,
                            detail=f"文件超过 {OUTBOX_MAX_BYTES // 1024 // 1024}MB，请压缩后再上传")
    import hashlib
    digest = hashlib.sha256(body).hexdigest()[:16]
    # 同内容不重复占空间；但**原名可能不同**（同一份扫描件改了名再传），
    # 此时复用同一个 id 会让收件人看到的名字变回第一次那个 —— 所以同内容时
    # 记一份"别名"，按最新一次的原名返回。
    path = os.path.join(_outbox_dir(), digest + ext)
    if not os.path.exists(path):
        with open(path, "wb") as fh:
            fh.write(body)
    safe_name = os.path.basename(str(name or ""))[:120] or f"附件{ext}"
    conn = db.connect(DB_PATH)
    try:
        db.add_audit(conn, "outbox", digest, "upload", safe_name,
                     f"临时附件 {safe_name}｜{len(body)} 字节｜sha256:{digest}",
                     s["username"], s["role"])
    finally:
        conn.close()
    return {"ok": True, "outbox_id": digest, "filename": safe_name,
            "size": len(body),
            "note": "已暂存。发出后文件仍保留在 data/outbox/，可追溯。"}


@app.get("/api/mail/outbox/{outbox_id}")
def api_mail_outbox_open(outbox_id: str, name: str = "", inline: int = 1,
                         token: str | None = None,
                         x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                         x_tp_role: str | None = Header(default=None, alias="X-TP-Role")):
    """打开/下载一个**临时附件**（v1.29.10，业务方要"附件要能预览"）。

    浏览器原生导航带不了自定义请求头，登录模式下用 `?token=` 兜底 ——
    与 `/api/documents/{did}/file` 同一口径。路径只认 outbox 目录内的
    十六进制 id（`_outbox_path` 双重校验），不接受任何外部路径。
    """
    s = _file_session(x_tp_token, token)
    require(s, "read")
    p = _outbox_path(outbox_id)
    if not p or not os.path.exists(p):
        raise HTTPException(status_code=404, detail="附件不存在或已被清理")
    fn = os.path.basename(str(name or ""))[:120] or os.path.basename(p)
    if not os.path.splitext(fn)[1]:
        fn += os.path.splitext(p)[1]
    return FileResponse(p, filename=fn,
                        content_disposition_type=("inline" if inline else "attachment"),
                        media_type=_mime_of(fn),
                        headers=_FILE_SAFE_HEADERS)


@app.post("/api/mail/outbox/{outbox_id}/delete")
def api_mail_outbox_delete(outbox_id: str,
                           x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                           x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """删除一个**尚未发出**的临时附件（v1.29.10，业务方要"附件删除按钮"）。

    只删文件本身（data/outbox/ 下）；归档原件（documents）不走这条。附件按
    内容哈希存 id —— 同一份文件在别的草稿里也用同一个 id，删除是"这份文件
    不要了"的语义，都清掉。
    """
    s = _session(x_tp_token, x_tp_role)
    require(s, "confirm")
    p = _outbox_path(outbox_id)
    if not p:
        raise HTTPException(status_code=400, detail="附件编号不合法")
    removed = os.path.exists(p)
    if removed:
        try:
            os.remove(p)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"删除失败：{exc}")
    conn = db.connect(DB_PATH)
    try:
        db.add_audit(conn, "outbox", str(outbox_id), "delete",
                     str(outbox_id), f"删除临时附件（{os.path.basename(p)}）",
                     s["username"], s["role"])
    finally:
        conn.close()
    return {"ok": True, "removed": removed, "note": "附件已删除"}


def _mail_send_sync(req: MailSendReq, s: dict) -> dict:
    """**真正发信**。需要 `confirm` 权限；成功与否都留痕。

    内容里若还残留 `【待填：...】` 会被拦下——宁可让 HR 补一句，也不发半成品出去。
    正文用了表格/粗体标记时按 **multipart/alternative** 发（HTML + 纯文本双版本），
    转换只在这里做一次：模板渲染 → 转 HTML → 发送，**单一真源**，
    避免前后端各转一遍导致"预览和收到的不是一回事"。

    附件（v1.27）：`attachments` 只收**库内原件 id**，服务端从归档区读字节。
    读不到（文件被挪走/删了）的**跳过并如实回报**，不静默少发 ——
    面试通知漏附岗位说明比发不出去更糟，HR 不会知道少了什么。
    """
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
    # v1.27：先把附件字节备齐。**全部读不出来就直接 400**（不发这封邮件）：
    # 静默发一封"该附的没附"的信，比让 HR 知道"原件读不到了、请重新上传"更糟。
    att_payloads: list[dict] = []
    att_missing: list[str] = []
    if req.attachments:
        conn0 = db.connect(DB_PATH)
        try:
            for _a in req.attachments:
                # v1.29：先看是不是**临时上传**的附件（录用函扫描件那类）
                if getattr(_a, "outbox_id", None):
                    _op = _outbox_path(_a.outbox_id)
                    _on = _a.filename2 or _a.filename or "附件"
                    if not _op or not os.path.exists(_op):
                        att_missing.append(f"{_on}（临时附件已不在暂存区）")
                        continue
                    try:
                        with open(_op, "rb") as _fh:
                            att_payloads.append({"filename": _on, "data": _fh.read()})
                    except OSError as _e:
                        att_missing.append(f"{_on}（读不出来：{type(_e).__name__}）")
                    continue
                if not _a.document_id:
                    att_missing.append(f"{_a.filename2 or _a.filename or '附件'}"
                                       "（既不是归档原件、也没有临时上传记录编号）")
                    continue
                _d = db.get_document(conn0, int(_a.document_id))
                _name = (_a.filename or (_d or {}).get("file_name") or "附件")
                if not _d:
                    att_missing.append(f"{_name}（附件 #{_a.document_id} 不在库里）")
                    continue
                _p = _d.get("archived_path") or ""
                if not _p or not os.path.exists(_p):
                    att_missing.append(f"{_name}（原件文件已不在归档区）")
                    continue
                try:
                    with open(_p, "rb") as _fh:
                        att_payloads.append({"filename": _name, "data": _fh.read()})
                except OSError as _e:
                    att_missing.append(f"{_name}（读不出来：{type(_e).__name__}）")
        finally:
            conn0.close()
        if att_missing and not att_payloads:
            raise HTTPException(status_code=400,
                                detail="附件都读不出来，邮件未发送："
                                       + "；".join(att_missing[:5])
                                       + "。请在档案里重新上传原件，或去掉附件先发。")
        _att_bytes = sum(len(a.get("data") or b"") for a in att_payloads)
        if _att_bytes > OUTBOX_MAX_TOTAL_BYTES:
            raise HTTPException(
                status_code=413,
                detail=(f"附件合计 {_att_bytes / 1024 / 1024:.1f}MB，"
                        f"超过邮件上限 {OUTBOX_MAX_TOTAL_BYTES // 1024 // 1024}MB；"
                        "请压缩或减少附件后再发。"),
            )
    r = mail_send_mod.send_mail(req.to, req.subject, plain, html=html_body,
                                 attachments=att_payloads or None)
    _written: dict = {}          # 实际回写下去的字段（给前端提示用）
    _write_warn = ""             # 回写失败时的警告（邮件已发出，不算发送失败）
    conn = None
    try:
        conn = db.connect(DB_PATH)
        _att_names = [a["filename"] for a in att_payloads]
        _att_txt = ("｜附件：" + "、".join(_att_names)) if _att_names else ""
        db.add_audit(conn, "mail", str(req.candidate_id or ""), "send",
                     "ok" if r.get("ok") else "fail",
                     f"发送邮件给 {req.to}｜主题：{req.subject}"
                         f"｜模板：{req.template_name or '（无）'}{_att_txt}"
                         f"｜{'成功' if r.get('ok') else '失败'}",
                     s["username"], s["role"])
        if r.get("ok") and req.application_id:
            db.add_note(conn, int(req.application_id),
                        f"已发送邮件：{req.subject}"
                        f"（模板：{req.template_name or '无'}"
                        + (f"，附件 {'、'.join(_att_names)}" if _att_names else "") + "）",
                        s["username"], s["role"])
        # ---- v1.29：发信成功后按**模板用途**回写投递档案 ----
        # 省掉"HR 再去档案里手工录一遍面试时间/单位/方式"这一步（漏录的下游：
        # 面试部门筛选筛不到人、13 列导出里是空的）。
        if r.get("ok") and req.application_id and req.vars:
            try:
                _kind = _mail_kind(conn, req.template_name)
                _v = {str(k).strip(): str(x).strip()
                      for k, x in (req.vars or {}).items() if str(x or "").strip()}
                if _kind == "interview_invite":
                    _at = _interview_at(_v.get("面试时间"), _v.get("面试时段"))
                    _patch: dict = {}
                    if _at:
                        _patch["interview_date"] = _at
                    if _v.get("面试时段"):
                        _patch["interview_slot"] = _v["面试时段"]
                    if _v.get("面试方式"):
                        _patch["interview_mode"] = _v["面试方式"]
                    if _v.get("面试单位"):
                        # 邮件里改过单位就以**邮件为准**（候选人被告知的是邮件里那个），
                        # `update_application_fields` 会写审计，前后不一致看得见。
                        _patch["interview_unit"] = _v["面试单位"]
                    if _patch:
                        db.update_application_fields(conn, int(req.application_id),
                                                     operator=s["username"], role=s["role"],
                                                     **_patch)
                        _written = _patch
                    # 发出面试邀请 = 已经进入面试安排。与 interview-info 的
                    # “登记面试单位后推进到待面试”保持同一条业务口径。
                    _cur = db.get_application(conn, int(req.application_id)) or {}
                    _cur_stage = stages_mod.normalize_stage(_cur.get("stage"))
                    _order = stages_mod.all_stages()
                    if (_cur_stage in _order and "待面试" in _order
                            and _order.index(_cur_stage) < _order.index("待面试")):
                        db.set_application_stage(
                            conn, int(req.application_id), "待面试",
                            operator=s["username"], role=s["role"],
                            skip_auto_archive=True,
                        )
                        _written["stage"] = "待面试"
                elif _kind == "offer":
                    # 录用函发出时刻 = 这一刻。**绝不手工填**，HR 常记错日期，
                    # 而"录用函发送时间"是后面所有时限的起算点。
                    db.update_application_fields(conn, int(req.application_id),
                                                 operator=s["username"], role=s["role"],
                                                 offer_sent_at=db.now())
                    _written = {"offer_sent_at": "已记录"}
                    # 发出录用函 = 流程推进到「已发放offer」（若还没到）
                    _cur = db.get_application(conn, int(req.application_id)) or {}
                    if _cur.get("stage") in ("待面试", "已面试"):
                        db.set_application_stage(conn, int(req.application_id),
                                                 "已发放offer", operator=s["username"],
                                                 role=s["role"])
            except Exception as _e:                       # noqa: BLE001
                # ⚠️ **邮件已经发出去了**，回写失败只能记警告，不能反过来报错 ——
                # 让 HR 以为"发送失败"会促使他重发一封（重复打扰候选人）。
                # 与"邮件游标写失败不许拖垮入库"同一条纪律。
                _write_warn = f"邮件已发出，但没能自动回写面试/录用信息：{type(_e).__name__}"
    except Exception as _e:                               # noqa: BLE001
        # 审计、备注或数据库连接失败也必须降级为警告：SMTP 已成功时，
        # 再返回“发送失败”会直接诱发重复发送。
        if r.get("ok"):
            _write_warn = (f"邮件已发出，但后续审计/备注/档案回写失败："
                           f"{type(_e).__name__}")
    finally:
        if conn is not None:
            conn.close()
    if not r.get("ok"):
        # ⚠️ 必须把 hint 一起抛出去。原来只传 r["error"]，
        # 于是 mail_send 里辛苦写好的那段"465 用 SSL、587 用 STARTTLS"的提示
        # **根本没到 HR 眼前** —— 界面上只剩一句看不懂的 SSLError。
        # "写了提示但传不出去"和"没写提示"对用户是同一回事。
        _msg = r.get("error") or "发送失败"
        if r.get("hint"):
            _msg = f"{_msg}｜{r['hint']}"
        raise HTTPException(status_code=400, detail=_msg)
    # 部分附件读不到（不是全部）→ 邮件已发，但必须让 HR 知道少了什么
    if att_missing:
        return {"ok": True, **r, "attachments_missing": att_missing,
                "written": _written, "write_warning": _write_warn,
                "note": ((r.get("note") or "") + f" 另有 {len(att_missing)} 个附件没发出去："
                         + "；".join(att_missing[:5])).strip()}
    # v1.29：把「自动回写了哪些字段」如实返回 —— HR 要能确认"面试时间到底记进去没有"，
    # 而不是发完信还要自己去档案里核对一遍。
    _extra = ""
    if _written:
        _extra = ("｜已自动记录：" + "、".join(
            db.APPLICATION_FIELD_LABEL.get(k, k) for k in _written))
    if _write_warn:
        _extra += f"｜⚠️ {_write_warn}"
    return {"ok": True, **r, "written": _written, "write_warning": _write_warn,
            "note": ((r.get("note") or "邮件已发出，动作已写入审计。") + _extra)}


def _run_mail_send_task(tid: str, req: MailSendReq, session: dict) -> None:
    """后台真正发信；浏览器只轮询任务，避免大附件把 HTTP 连接拖断。"""
    try:
        _progress_set(tid, message="正在连接发信服务器…")
        result = _mail_send_sync(req, session)
        _progress_set(
            tid, done=1, status="done",
            message="邮件已发送",
            extra={"result": result},
        )
    except HTTPException as exc:
        msg = str(exc.detail or "发送失败")
        _progress_set(
            tid, done=1, status="error", message=msg,
            extra={"result": {"ok": False, "error": msg}},
        )
    except Exception as exc:                                   # noqa: BLE001
        traceback.print_exc()
        msg = f"发送任务异常：{type(exc).__name__}: {exc}"
        _progress_set(
            tid, done=1, status="error", message=msg,
            extra={"result": {"ok": False, "error": msg}},
        )


@app.post("/api/mail/send")
def api_mail_send(req: MailSendReq,
                  async_mode: bool = Query(default=False, alias="async"),
                  x_tp_token: str | None = Header(default=None, alias="X-TP-Token"),
                  x_tp_role: str | None = Header(default=None, alias="X-TP-Role")) -> dict:
    """发信入口。`?async=1` 时立即返回任务号，SMTP 在后台继续执行。"""
    s = _session(x_tp_token, x_tp_role)
    require(s, "confirm")
    if not async_mode:
        return _mail_send_sync(req, s)
    req_id = str(req.client_request_id or "").strip()[:80]
    if req_id:
        with _MAIL_REQ_LOCK:
            existing_tid = _MAIL_REQ.get(req_id)
            if existing_tid:
                with _PROGRESS_LOCK:
                    existing = dict(_PROGRESS.get(existing_tid) or {})
                if existing:
                    out = {
                        "ok": True,
                        "pending": existing.get("status") == "running",
                        "task_id": existing_tid,
                        "deduped": True,
                        "note": "同一封邮件的发送请求已存在，未重复提交。",
                    }
                    if "result" in existing:
                        out["result"] = existing["result"]
                    return out
            # 创建任务与登记幂等键必须原子完成，防两个同键请求同时穿透。
            tid = _progress_new(
                f"发送邮件：{req.subject or '（无主题）'}", 1, kind="mail",
                meta={"candidate_id": req.candidate_id,
                      "application_id": req.application_id,
                      "to": req.to, "subject": req.subject,
                      "client_request_id": req_id},
            )
            _MAIL_REQ[req_id] = tid
    else:
        tid = _progress_new(
            f"发送邮件：{req.subject or '（无主题）'}", 1, kind="mail",
            meta={"candidate_id": req.candidate_id,
                  "application_id": req.application_id,
                  "to": req.to, "subject": req.subject,
                  "client_request_id": ""},
        )
    threading.Thread(
        target=_run_mail_send_task,
        name="mail-send",
        daemon=True,
        args=(tid, req, dict(s)),
    ).start()
    return {"ok": True, "pending": True, "task_id": tid, "deduped": False,
            "note": "邮件已进入后台发送队列。"}
