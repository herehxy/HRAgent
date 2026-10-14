"""动作层：把"待确认提案"真正落到库里。

分工很明确：

- 智能体只能**生成提案**（`agent/tools.py` 里的写类工具）；
- **只有 HR 确认**才会走到这里，由本模块执行真实写入并留审计。

这样"系统只建议、HR 才决定"就不是一句口号，而是代码结构上无法绕过的约束：
模型拿不到任何直接写库的路径。
"""
from __future__ import annotations

from . import auth, db



def _tiers() -> dict:
    """读 config/tiers.json（动作层要用；不 import server 以免循环依赖）。"""
    import json as _json
    import os as _os
    _p = _os.path.join(_os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
                       "config", "tiers.json")
    try:
        with open(_p, encoding="utf-8") as _f:
            return _json.load(_f)
    except (OSError, ValueError):
        return {}


def apply_proposal(db_path: str, proposal_id: int, decision: str,
                   operator: str = "HR", role: str = "recruiter") -> dict:
    """执行或拒绝一条提案。decision: approve / reject。"""
    conn = db.connect(db_path)
    try:
        p = db.get_proposal(conn, proposal_id)
        if not p:
            return {"ok": False, "error": f"未找到提案 #{proposal_id}"}
        if p["status"] != "待确认":
            return {"ok": False, "error": f"提案 #{proposal_id} 已是『{p['status']}』状态，无需重复处理"}

        if decision != "approve":
            db.decide_proposal(conn, proposal_id, "reject", operator, role, "已拒绝")
            return {"ok": True, "status": "已拒绝", "proposal_id": proposal_id}

        tool = p["tool"]
        args = p.get("args") or {}
        result: dict = {"ok": True, "proposal_id": proposal_id, "tool": tool}

        if tool == "set_stage":
            need = "set_stage"
            if not auth.can(role, need):
                return _denied(conn, p, operator, role, need)
            aid, stage = int(args["application_id"]), args["stage"]
            r = db.set_application_stage(conn, aid, stage, operator, role)
            result["after"] = {"application_id": aid, "stage": r.get("stage") if r else None}

        elif tool == "set_tier":
            if not auth.can(role, "confirm"):
                return _denied(conn, p, operator, role, "confirm")
            aid, tier = int(args["application_id"]), args["tier"]
            r = db.set_application_tier(conn, aid, tier, args.get("note"), operator, role)
            result["after"] = {"application_id": aid, "tier_final": r.get("tier_final") if r else None}

        elif tool == "add_tag":
            if not auth.can(role, "propose"):
                return _denied(conn, p, operator, role, "propose")
            cid = int(args["candidate_id"])
            tid = db.upsert_tag(conn, args["tag"], args.get("category") or "能力")
            db.link_tag(conn, cid, tid, args.get("evidence") or "HR 确认添加", "hr")
            db.add_audit(conn, "candidate", str(cid), "add_tag", "", args["tag"], operator, role)
            result["after"] = {"candidate_id": cid, "tag": args["tag"]}

        elif tool == "add_note":
            if not auth.can(role, "add_note"):
                return _denied(conn, p, operator, role, "add_note")
            aid = int(args["application_id"])
            db.add_note(conn, aid, args["note"], operator, role)
            result["after"] = {"application_id": aid, "note": args["note"]}

        elif tool == "assign_job":
            # 归岗 / 改岗位：统一走 regrade.assign_job（已支持两种情形）
            if not auth.can(role, "set_stage"):
                return _denied(conn, p, operator, role, "set_stage")
            from . import regrade as _rg
            jid = int(args["job_id"])
            job = db.get_job(conn, jid)
            if not job:
                return {"ok": False, "error": f"未找到岗位 #{jid}"}
            r = _rg.assign_job(conn, int(args["application_id"]), jid,
                               job.get("jd_json") or {}, _tiers(), operator, role)
            if not r.get("ok"):
                return {"ok": False, "error": r.get("error")}
            result["after"] = {"job_id": jid, "job_title": r.get("job_title")}

        elif tool == "suggest_job":
            if not auth.can(role, "set_stage"):
                return _denied(conn, p, operator, role, "set_stage")
            from . import regrade as _rg
            r = _rg.suggest_job_for_application(conn, int(args["candidate_id"]), operator, role)
            if not r.get("ok"):
                return {"ok": False, "error": r.get("error")}
            result["after"] = {"job_id": r.get("job_id"), "title": r.get("title")}

        elif tool == "mark_review":
            if not auth.can(role, "set_stage"):
                return _denied(conn, p, operator, role, "set_stage")
            cid = int(args["candidate_id"])
            d = db.candidate_detail(conn, cid) or {}
            apps = d.get("applications") or []
            if not apps:
                return {"ok": False, "error": "该候选人没有投递记录"}
            fn = db.unreview_mark if args.get("undo") else db.review_mark
            r = fn(conn, apps[0]["id"], operator, role)
            result["after"] = {"candidate_id": cid, "status": (r or {}).get("status")}

        elif tool == "set_archive":
            if not auth.can(role, "archive"):
                return _denied(conn, p, operator, role, "archive")
            cid = int(args["candidate_id"])
            db.set_candidate_archived(conn, cid, bool(args.get("archived")), operator, role)
            result["after"] = {"candidate_id": cid, "archived": bool(args.get("archived"))}

        elif tool == "archive_batch":
            if not auth.can(role, "archive"):
                return _denied(conn, p, operator, role, "archive")
            r = db.set_candidates_archived(conn, [int(x) for x in (args.get("ids") or [])],
                                           bool(args.get("archived")), operator, role)
            result["after"] = {"changed": r.get("changed"), "skipped": r.get("skipped")}

        elif tool == "archive_by_year":
            if not auth.can(role, "archive"):
                return _denied(conn, p, operator, role, "archive")
            r = _archive_before_year(conn, int(args["before_year"]), operator, role)
            result["after"] = r

        elif tool == "split_candidate":
            if not auth.can(role, "merge"):
                return _denied(conn, p, operator, role, "merge")
            r = db.split_candidate(conn, int(args["candidate_id"]), operator, role)
            result["after"] = {"candidate_id": int(args["candidate_id"]), "split": bool(r.get("ok"))}

        elif tool == "create_job":
            if not auth.can(role, "set_stage"):
                return _denied(conn, p, operator, role, "set_stage")
            jid = db.create_job(conn, args["title"], dept_id=None, jd=args.get("jd") or {})
            result["after"] = {"job_id": jid, "title": args["title"]}

        elif tool == "update_job_jd":
            if not auth.can(role, "set_stage"):
                return _denied(conn, p, operator, role, "set_stage")
            jid = int(args["job_id"])
            db.update_job_jd(conn, jid, args.get("jd") or {}, operator=operator, role=role)
            result["after"] = {"job_id": jid}

        elif tool == "regrade_job":
            if not auth.can(role, "set_stage"):
                return _denied(conn, p, operator, role, "set_stage")
            from . import regrade as _rg
            jid = int(args["job_id"])
            job = db.get_job(conn, jid) or {}
            r = _rg.regrade_job(conn, jid, job.get("jd_json") or {}, _tiers(),
                                 apply=bool(args.get("apply")), operator=operator, role=role)
            result["after"] = {"job_id": jid, "changed": r.get("changed")}

        elif tool == "merge_candidates":
            if not auth.can(role, "merge"):
                return _denied(conn, p, operator, role, "merge")
            r = db.merge_candidates(conn, int(args["source_id"]), int(args["target_id"]),
                                    operator, role)
            if not r.get("ok"):
                return {"ok": False, "error": r.get("error")}
            result["after"] = r

        # ---------------- v1.25 新增：配置 / 字典 / 字段 / 批处理 ----------------
        # 加这些分支的原因：写类工具只**生成提案**，真正落库在**这里**。
        # 漏加一条的后果是「确认按钮点了报『未知提案类型』」——
        # 所以每加一个写工具，这里必须同步加一条，两处是**一个整体**。

        elif tool == "save_mail_config":
            if not auth.can(role, "settings"):
                return _denied(conn, p, operator, role, "settings")
            cfg = dict(args.get("config") or {})
            _write_repo_config("mailbox.json", cfg)
            _audit_config(conn, "mail", cfg, operator, role)
            result["after"] = {"changed_keys": sorted(
                k for k in cfg if k.lower() != "password")}

        elif tool == "save_model_config":
            if not auth.can(role, "settings"):
                return _denied(conn, p, operator, role, "settings")
            cfg = dict(args.get("config") or {})
            _write_repo_config("model.json", cfg)
            _audit_config(conn, "model", cfg, operator, role)
            result["after"] = {"changed_keys": sorted(
                k for k in cfg if k.lower() not in ("api_key", "apikey"))}

        elif tool == "save_settings":
            if not auth.can(role, "settings"):
                return _denied(conn, p, operator, role, "settings")
            st = dict(args.get("settings") or {})
            for k, v in st.items():
                db.set_setting(conn, k, v)
            db.add_audit(conn, "settings", "app", "edit_settings", "",
                         "；".join(f"{k}={v}" for k, v in st.items()), operator, role)
            result["after"] = {"changed": sorted(st)}

        elif tool == "update_interview_dict":
            if not auth.can(role, "settings"):
                return _denied(conn, p, operator, role, "settings")
            patch = dict(args.get("interview_dict") or {})
            _merge_json_file("interview.json", patch)
            db.add_audit(conn, "settings", "interview_dict", "edit_settings", "",
                         "；".join(f"{k}={len(v) if isinstance(v, list) else v} 项"
                                   for k, v in patch.items()), operator, role)
            result["after"] = {"changed_keys": sorted(patch)}

        elif tool == "update_candidate_fields":
            if not auth.can(role, "chat"):
                return _denied(conn, p, operator, role, "chat")
            cid = int(args["candidate_id"])
            fields = dict(args.get("fields") or {})
            if not fields:
                return {"ok": False, "error": "提案里没有要改的字段"}
            from .crypto import blind_index, decrypt, encrypt
            from .identity import normalize_phone
            db_fields: dict = {}
            for k, v in fields.items():
                if k in ("phone_enc", "email_enc"):
                    val = str(v or "").strip()
                    if not val:
                        db_fields[k] = None
                        db_fields[k.replace("_enc", "_bidx")] = None
                        continue
                    db_fields[k] = encrypt(val)
                    if k == "phone_enc":
                        norm = normalize_phone(val)
                        db_fields["phone_bidx"] = blind_index(val) if norm else None
                    else:
                        db_fields["email_bidx"] = blind_index(val)
                else:
                    db_fields[k] = v if v != "" else None
            d0 = db.candidate_detail(conn, cid) or {}
            label = {"name": "姓名", "edu_level": "学历", "school": "学校",
                     "major": "专业", "phone_enc": "电话", "email_enc": "邮箱",
                     "note": "备注"}   # v1.25.7：人工备注也进审计文案
            old_map = {"name": d0.get("name") or "", "edu_level": d0.get("edu_level") or "",
                       "school": d0.get("school") or "", "major": d0.get("major") or "",
                       "note": d0.get("note") or "",
                       "phone_enc": decrypt(d0.get("phone_enc")) or "",
                       "email_enc": decrypt(d0.get("email_enc")) or ""}
            # ⚠️ 只按**用户可见字段**写审计文案：phone_bidx/email_bidx 是派生字段、
            # 没有中文名，混进来会让 label[k] 抛 KeyError —— 这正是 v1.24.21
            # 那个"保存成功却报 500"的根因。这里从一开始就按 label 过滤。
            changed = {k: v for k, v in fields.items() if k in label}
            db.update_candidate(conn, cid, **db_fields)
            # v1.25：同 server.py —— 解密 + 打码，别把 enc: 密文写进审计。
            before = "；".join(f"{label[k]}={db.audit_field_value(k, old_map.get(k))}"
                               for k in changed)
            after = "；".join(f"{label[k]}={db.audit_field_value(k, fields[k])}"
                              for k in changed)
            db.add_audit(conn, "candidate", str(cid), "edit_fields", before, after,
                         operator, role)
            result["after"] = {"candidate_id": cid, "changed": sorted(changed)}

        elif tool == "reanalyze_candidate":
            if not auth.can(role, "chat"):
                return _denied(conn, p, operator, role, "chat")
            r = _reanalyze_one(conn, int(args["candidate_id"]), operator, role)
            if not r.get("ok"):
                return {"ok": False, "error": r.get("error")}
            result["after"] = {"candidate_id": int(args["candidate_id"]),
                               "note": r.get("note")}

        elif tool == "reanalyze_batch":
            if not auth.can(role, "chat"):
                return _denied(conn, p, operator, role, "chat")
            r = _reanalyze_many(conn, [int(x) for x in (args.get("candidate_ids") or [])],
                                int(args.get("limit") or 50), operator, role)
            result["after"] = {"done": r.get("done"), "failed": r.get("failed")}

        elif tool == "route_pending":
            if not auth.can(role, "set_stage"):
                return _denied(conn, p, operator, role, "set_stage")
            from . import regrade as _rg
            r = _rg.route_pending(conn, _tiers(), apply=bool(args.get("apply", True)),
                                  operator=operator, role=role)
            result["after"] = {"changed": r.get("changed"), "scanned": r.get("scanned")}

        elif tool == "remove_source_files":
            if not auth.can(role, "ingest"):
                return _denied(conn, p, operator, role, "ingest")
            r = _move_source_files([str(x) for x in (args.get("names") or [])],
                                   to_trash=True)
            result["after"] = r

        elif tool == "restore_source_files":
            if not auth.can(role, "ingest"):
                return _denied(conn, p, operator, role, "ingest")
            r = _move_source_files([str(x) for x in (args.get("names") or [])],
                                   to_trash=False)
            result["after"] = r

        elif tool == "rebuild_brief":
            if not auth.can(role, "read"):
                return _denied(conn, p, operator, role, "read")
            from .agent import brief as _brief
            payload = _brief.build_and_save(conn, use_llm=bool(args.get("use_llm")))
            result["after"] = {"headline": payload.get("headline")}

        elif tool == "clear_chat_history":
            if not auth.can(role, "chat"):
                return _denied(conn, p, operator, role, "chat")
            n = 0
            try:
                cur = conn.execute("DELETE FROM agent_chat WHERE session_id = ?",
                                   (args.get("session_id") or "",))
                n = cur.rowcount
            except Exception:
                n = 0
            try:
                conn.execute("DELETE FROM chat_messages")
            except Exception:
                pass
            conn.commit()
            db.add_audit(conn, "settings", "agent_chat", "clear", "",
                         f"清空对话历史（{n} 条）", operator, role)
            result["after"] = {"cleared": n}

        else:
            return {"ok": False, "error": f"未知提案类型 {tool}"}

        db.decide_proposal(conn, proposal_id, "approve", operator, role,
                           str(result.get("after")))
        result["status"] = "已执行"
        result["message"] = "提案已由 HR 确认并生效"
        return result
    finally:
        conn.close()


def _denied(conn, proposal, operator, role, perm) -> dict:
    db.decide_proposal(conn, proposal["id"], "reject", operator, role,
                       f"权限不足：{role} 缺少 {perm} 权限")
    return {"ok": False, "error": f"当前角色（{auth.ROLES.get(role, {}).get('label', role)}）"
                                 f"无权执行该操作，提案已自动拒绝"}


# ============================================================
# v1.25：新增提案类型的执行辅助
# ============================================================
# 这些是"确认之后怎么真的落库"的实现。放在 actions.py 而不是 server.py，
# 因为 actions 是**唯一**允许写库的路径（模型拿不到直接写库的口子）。

def _repo_path(*rel: str) -> str:
    """仓库根下的路径。actions 不 import server（会循环依赖），自己按 __file__ 推。"""
    import os as _os
    return _os.path.join(_os.path.dirname(_os.path.dirname(
        _os.path.abspath(__file__))), *rel)


def _write_repo_config(name: str, cfg: dict) -> None:
    """整体写一个 config/*.json（保留原有其它键）。"""
    import json as _json
    import os as _os
    p = _repo_path("config", name)
    _os.makedirs(_os.path.dirname(p), exist_ok=True)
    cur = {}
    try:
        with open(p, encoding="utf-8") as fh:
            cur = _json.load(fh) or {}
    except (OSError, ValueError):
        cur = {}
    if not isinstance(cur, dict):
        cur = {}
    cur.update(cfg or {})
    with open(p, "w", encoding="utf-8") as fh:
        _json.dump(cur, fh, ensure_ascii=False, indent=2)


def _merge_json_file(name: str, patch: dict) -> None:
    _write_repo_config(name, patch)


def _audit_config(conn, kind: str, cfg: dict, operator: str, role: str) -> None:
    """配置改动的审计。**密钥只记"已设置"，绝不把明文写进审计表**——
    审计是长期留存、可导出的，写明文等于把密钥抄了一份到另一个地方。"""
    safe = []
    for k, v in (cfg or {}).items():
        if k.lower() in ("password", "pass", "api_key", "apikey", "secret",
                         "token", "auth_code"):
            safe.append(f"{k}=（已设置，不记录明文）")
        else:
            safe.append(f"{k}={v}")
    db.add_audit(conn, "settings", kind, "edit_config", "", "；".join(safe),
                 operator, role)


def _reanalyze_one(conn, cid: int, operator: str, role: str) -> dict:
    """完整重跑一位候选人。走**与界面「重新分析」完全相同的** ingest.reanalyze_full，
    保证 HR 从对话里做和从界面上做，结果一致（不然两套逻辑迟早在某处对不上）。"""
    from . import ingest as _ing
    from . import mailbox as _mb
    from .agent import llm as _llm
    c = _llm.load_cfg()
    if not c.get("enabled", True) or not c.get("api_key"):
        return {"ok": False, "error": "模型未启用或未配置 API Key，无法完整重新分析"}
    conf = {"api_key": c.get("api_key"), "base_url": c.get("base_url"),
            "model": c.get("model")}
    jd = _jd_default()
    r = _ing.reanalyze_full(conn, _mb.load_config(), jd, _tiers(), cid,
                            use_llm=True, llm_conf=conf)
    if not r.get("ok"):
        return r
    db.add_audit(conn, "candidate", str(cid), "reanalyze", "",
                 f"{operator} 通过智能助手完整重跑分析", operator, role)
    return {"ok": True, "note": "完整重新分析完成：原件已重解析、字段已重抽、"
                                "技能已重建、建议档已重算。HR 已定的档位/阶段/岗位不受影响。"}


def _reanalyze_many(conn, ids: list[int], limit: int, operator: str, role: str) -> dict:
    """批量完整重跑。**串行**——与界面同口径（并发写同一 WAL 库在本机不稳，见 server 注释）。"""
    if ids:
        targets = [{"cid": i} for i in ids[:max(1, limit)]]
    else:
        rows = conn.execute(
            """SELECT DISTINCT a.candidate_id AS cid
                 FROM applications a JOIN candidates c ON c.id = a.candidate_id
                WHERE COALESCE(c.archived_at,'') = '' AND c.merged_into IS NULL
                  AND COALESCE(a.extract_mode,'') != 'llm+rule'
                ORDER BY COALESCE(a.applied_at,'') ASC, a.id ASC LIMIT ?""",
            (max(1, limit),)).fetchall()
        targets = [{"cid": r["cid"]} for r in rows]
    done, failed = 0, 0
    for t in targets:
        r = _reanalyze_one(conn, int(t["cid"]), operator, role)
        if r.get("ok"):
            done += 1
        else:
            failed += 1
    return {"done": done, "failed": failed, "total": len(targets)}


def _jd_default() -> dict:
    import json as _json
    try:
        with open(_repo_path("config", "jd.json"), encoding="utf-8") as fh:
            return _json.load(fh) or {}
    except (OSError, ValueError):
        return {}


def _archive_before_year(conn, year: int, operator: str, role: str) -> dict:
    """归档「year 年及以前」的投递所对应的人。

    口径与界面「按年归档」一致：看**最后一条投递**的年份，
    不是看档案创建时间 —— HR 说的是"上一年的简历收起来"。
    """
    rows = conn.execute(
        """SELECT c.id AS cid
             FROM candidates c
            WHERE COALESCE(c.archived_at,'') = '' AND c.merged_into IS NULL
              AND COALESCE((SELECT MAX(a.applied_at) FROM applications a
                             WHERE a.candidate_id = c.id), c.created_at, '') < ?""",
        (f"{int(year)}-01-01",)).fetchall()
    ids = [r["cid"] for r in rows]
    if not ids:
        return {"changed": 0, "note": f"没有 {year} 年及以前的在池投递需要归档"}
    r = db.set_candidates_archived(conn, ids, True, operator, role)
    return {"changed": r.get("changed"), "skipped": r.get("skipped"),
            "before_year": int(year)}


def _trash_dir() -> str:
    import os as _os
    from datetime import datetime as _dt
    from .paths import DATA_DIR
    return _os.path.join(DATA_DIR, "removed", _dt.now().strftime("%Y-%m"))


def _source_dir() -> str:
    import os as _os
    from . import mailbox as _mb
    cfg = _mb.load_config()
    return _os.path.realpath(_mb.resolve_dir(cfg.get("folder_dir") or "data/resumes"))


def _move_source_files(names: list[str], to_trash: bool) -> dict:
    """来源文件 ↔ 回收站的移动。

    **不是删除**：to_trash=True 只是挪进 data/removed/<月>/，随时可恢复。
    同名冲突时**跳过并报告**，绝不覆盖 —— 覆盖会静默毁掉一份简历。
    """
    import os as _os
    import shutil as _sh
    from .paths import DATA_DIR
    moved, skipped = [], []
    src_dir = _source_dir()
    if to_trash:
        trash = _trash_dir()
        _os.makedirs(trash, exist_ok=True)
        for nm in names:
            base = _os.path.basename(nm)
            src = _os.path.join(src_dir, base)
            if not _os.path.isfile(src):
                skipped.append({"name": nm, "why": "来源目录里没有这个文件"})
                continue
            dst = _os.path.join(trash, base)
            if _os.path.exists(dst):
                dst = _os.path.join(trash, f"{_os.path.splitext(base)[0]}_2"
                                          f"{_os.path.splitext(base)[1]}")
            _sh.move(src, dst)
            moved.append(base)
    else:
        root = _os.path.join(DATA_DIR, "removed")
        for nm in names:
            base = _os.path.basename(nm)
            found = None
            if _os.path.isdir(root):
                for month in sorted(_os.listdir(root), reverse=True):
                    cand = _os.path.join(root, month, base)
                    if _os.path.isfile(cand):
                        found = cand
                        break
            if not found:
                skipped.append({"name": nm, "why": "回收站里没有这个文件"})
                continue
            dst = _os.path.join(src_dir, base)
            if _os.path.exists(dst):
                # 绝不覆盖：宁可让 HR 自己改名
                skipped.append({"name": nm, "why": "来源目录已有同名文件，未覆盖"})
                continue
            _os.makedirs(src_dir, exist_ok=True)
            _sh.move(found, dst)
            moved.append(base)
    return {"moved": moved, "skipped": skipped,
            "note": ("已移入回收站（可恢复）" if to_trash else "已从回收站恢复")}
