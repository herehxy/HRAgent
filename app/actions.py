"""动作层：把"待确认提案"真正落到库里。

分工很明确：

- 智能体只能**生成提案**（`agent/tools.py` 里的写类工具）；
- **只有 HR 确认**才会走到这里，由本模块执行真实写入并留审计。

这样"系统只建议、HR 才决定"就不是一句口号，而是代码结构上无法绕过的约束：
模型拿不到任何直接写库的路径。
"""
from __future__ import annotations

from . import auth, db, recruit as recruit_mod, stages as stages_mod



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
        # **原子领取**：只有把「待确认」成功改成「执行中」的请求才能继续执行。
        # 两个并发点击（或双击/重试）否则会各执行一遍副作用。
        if not db.claim_proposal(conn, proposal_id, operator):
            return {"ok": False,
                    "error": f"提案 #{proposal_id} 正被处理或已处理，请刷新后查看结果"}

        if decision != "approve":
            db.decide_proposal(conn, proposal_id, "reject", operator, role, "已拒绝")
            return {"ok": True, "status": "已拒绝", "proposal_id": proposal_id}

        tool = p["tool"]
        args = db.open_sensitive_args(p.get("args") or {})
        result: dict = {"ok": True, "proposal_id": proposal_id, "tool": tool}

        if tool == "set_stage":
            need = "set_stage"
            if not auth.can(role, need):
                return _denied(conn, p, operator, role, need)
            aid, stage = int(args["application_id"]), args["stage"]
            _before = db.get_application(conn, aid) or {}
            _cur_stage = str(_before.get("stage") or "").strip()
            _expected = str(args.get("expected_stage") or "").strip()
            if _expected and _cur_stage != _expected:
                return {"ok": False,
                        "error": f"提案创建时档案在「{_expected}」，现在已是「{_cur_stage}」。"
                                 "为避免把阶段改回去，本次没有执行；请刷新提案后重新发起。"}
            # 老提案没有 expected_stage：至少拦住“目标阶段比当前更早”的倒退。
            _order = stages_mod.all_stages()
            if (not _expected and _cur_stage in _order and stage in _order
                    and _order.index(stage) < _order.index(_cur_stage)):
                return {"ok": False,
                        "error": f"档案当前已到「{_cur_stage}」，该提案会把阶段回退到"
                                 f"「{stage}」；已拒绝执行，请刷新后重新发起。"}
            _actual_stage = stages_mod.auto_advance_stage(
                _before.get("stage"), stage, args.get("outcome"))
            # v1.29：结论标签一起落库。参数是**可选**的——老提案里没有这个键，
            # 传 None 表示"不改结论"（与"把结论清空"用空串区分，见 db.update_application_fields）。
            # ⚠️ 校验放在 db.set_application_stage 里（那里是唯一真源），这里不重复判一遍：
            # 两处各判一次，迟早会出现"这处放行、那处拒绝"。
            # 关键字传参：签名里 `sign_status` 在 operator/role 之前，
            # 位置传参会让「同意提案」执行阶段变更时把用户名当成签约情况。
            try:
                r = db.set_application_stage(conn, aid, _actual_stage, args.get("outcome"),
                                             operator=operator, role=role)
            except ValueError as exc:
                # 必填字段缺失 / 非法结论：把原因原样交给 HR，不要冒成 500。
                return {"ok": False, "error": str(exc)}
            _archived = bool((r or {}).pop("_auto_archived", False))
            result["after"] = {"application_id": aid, "stage": r.get("stage") if r else None,
                               "advanced_to": (_actual_stage
                                               if _actual_stage != _before.get("stage") else None),
                               "outcome": r.get("outcome") if r else None}
            if _archived:
                result["auto_archived"] = True
                result["message"] = ("提案已执行；该候选人的投递都走完了，已自动归档"
                                     "（可在「归档」页恢复）。")

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
            # db.update_job_jd 的入参只有 (operator)，不接受 role——存量小坑：
            # 之前传 role=role 会直接 TypeError，这只是走不到才没炸（2026-10-16 修）。
            db.update_job_jd(conn, jid, args.get("jd") or {}, operator=operator)
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
                     "note": "备注", "gender": "性别", "grad_date": "毕业时间",
                     "current_org": "当前单位", "first_degree_school": "第一学历院校",
                     "cet": "四六级", "in_school": "在读状态",
                     "edu_history": "教育经历"}
            old_map = {"name": d0.get("name") or "", "edu_level": d0.get("edu_level") or "",
                       "school": d0.get("school") or "", "major": d0.get("major") or "",
                       "note": d0.get("note") or "",
                       "phone_enc": decrypt(d0.get("phone_enc")) or "",
                       "email_enc": decrypt(d0.get("email_enc")) or "",
                       "gender": d0.get("gender") or "", "grad_date": d0.get("grad_date") or "",
                       "current_org": d0.get("current_org") or "",
                       "first_degree_school": d0.get("first_degree_school") or "",
                       "cet": d0.get("cet") or "", "in_school": d0.get("in_school") or 0,
                       "edu_history": d0.get("edu_history") or ""}
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

        elif tool == "bulk_initial_pass":
            if not auth.can(role, "set_stage"):
                return _denied(conn, p, operator, role, "set_stage")
            from . import regrade as _rg
            ids = [int(x) for x in (args.get("candidate_ids") or [])]
            if not ids:
                return {"ok": False, "error": "提案里没有候选人"}
            r = _rg.bulk_initial_pass(conn, ids, _tiers(),
                                      operator=operator, role=role)
            result["after"] = {"advanced": r.get("advanced"),
                               "assigned": r.get("assigned"),
                               "no_suggestion": r.get("no_suggestion"),
                               "already_passed": r.get("already_passed"),
                               "failed": r.get("failed")}

        elif tool == "bulk_assign_job":
            if not auth.can(role, "set_stage"):
                return _denied(conn, p, operator, role, "set_stage")
            from . import regrade as _rg
            ids = [int(x) for x in (args.get("candidate_ids") or [])]
            if not ids:
                return {"ok": False, "error": "提案里没有候选人"}
            r = _rg.bulk_assign_job(conn, ids, int(args["job_id"]), _tiers(),
                                    operator=operator, role=role)
            if not r.get("ok", True):
                return {"ok": False, "error": r.get("error")}
            result["after"] = {"assigned": r.get("assigned"),
                               "unchanged": r.get("unchanged"),
                               "skipped": r.get("skipped"),
                               "failed": r.get("failed")}

        elif tool == "set_sign_status":
            if not auth.can(role, "set_stage"):
                return _denied(conn, p, operator, role, "set_stage")
            aid = int(args["application_id"])
            cur = db.get_application(conn, aid)
            if not cur:
                return {"ok": False, "error": f"未找到投递 #{aid}"}
            status = str(args.get("status") or "").strip()
            if status not in db.SIGN_STATUSES:
                return {"ok": False, "error": "签约情况只能是：" + "/".join(db.SIGN_STATUSES)}
            patch = {"sign_status": status}
            if args.get("hire_unit"):
                patch["hire_unit"] = str(args["hire_unit"]).strip()[:40]
            if args.get("onboard_deadline"):
                patch["onboard_deadline"] = str(args["onboard_deadline"]).strip()[:10]
            if status == "已签约":
                if args.get("signed_at"):
                    patch["signed_at"] = str(args["signed_at"]).strip()[:10]
                elif not cur.get("signed_at"):
                    patch["signed_at"] = db.today()
            elif status in ("未签约", "拒签"):
                patch["signed_at"] = None
            db.update_application_fields(conn, aid, operator=operator, role=role, **patch)
            result["after"] = {"application_id": aid, "sign_status": status}

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

        elif tool == "set_interview_info":
            if not auth.can(role, "set_stage"):
                return _denied(conn, p, operator, role, "set_stage")
            aid = int(args["application_id"])
            app = db.get_application(conn, aid)
            if not app:
                return {"ok": False, "error": f"未找到投递 #{aid}"}
            allowed = ("interview_unit", "interview_date", "interview_slot",
                       "interview_mode", "interview_place", "hire_unit",
                       "signed_at", "onboard_deadline", "onboard_status",
                       "offer_sent_at")
            fields = {k: args[k] for k in allowed if k in args}
            outcome = str(args.get("outcome") or "").strip() or None
            if outcome:
                cur_stage = str(app.get("stage") or "").strip()
                target = stages_mod.auto_advance_stage(cur_stage, cur_stage, outcome)
                if not stages_mod.outcome_allowed_from(cur_stage, target, outcome):
                    return {"ok": False,
                            "error": f"当前阶段「{cur_stage or '未设置'}」不能打结论「{outcome}」"}
            if fields:
                db.update_application_fields(conn, aid, operator=operator, role=role, **fields)
            if outcome:
                cur_stage = str(app.get("stage") or "").strip()
                target = stages_mod.auto_advance_stage(cur_stage, cur_stage, outcome)
                if target != cur_stage:
                    db.set_application_stage(conn, aid, target, outcome=outcome,
                                             operator=operator, role=role,
                                             skip_auto_archive=True)
                else:
                    db.update_application_fields(conn, aid, operator=operator,
                                                 role=role, outcome=outcome)
            result["after"] = {"application_id": aid, "changed": sorted(fields),
                               "outcome": outcome}

        elif tool == "manage_mail_template":
            if not auth.can(role, "settings"):
                return _denied(conn, p, operator, role, "settings")
            action = str(args.get("action") or "").strip().lower()
            if action == "save":
                name = str(args.get("name") or "").strip()
                tid = args.get("id")
                if tid:
                    old = db.get_mail_template(conn, int(tid))
                    if not old:
                        return {"ok": False, "error": f"模板 #{tid} 不存在"}
                else:
                    same = [t for t in db.list_mail_templates(conn)
                            if t.get("name") == name]
                    if same:
                        return {"ok": False,
                                "error": f"已有同名模板「{name}」，请用 id={same[0]['id']} 修改"}
                new_id = db.upsert_mail_template(
                    conn, name, str(args.get("scene") or "其他通知"),
                    str(args.get("subject") or ""), str(args.get("body") or ""),
                    int(tid) if tid else None, args.get("kind") or None)
                db.add_audit(conn, "settings", f"mail_template#{new_id}", "update", "",
                             f"保存邮件模板「{name}」", operator, role)
                result["after"] = {"template_id": new_id, "name": name}
            elif action == "delete":
                tid = int(args["id"])
                t = db.get_mail_template(conn, tid)
                if not t:
                    return {"ok": False, "error": f"模板 #{tid} 不存在"}
                db.delete_mail_template(conn, tid)
                db.add_audit(conn, "settings", f"mail_template#{tid}", "delete",
                             t.get("name") or "", "", operator, role)
                result["after"] = {"deleted": tid, "name": t.get("name")}
            else:
                return {"ok": False, "error": "action 只能是 save / delete"}

        elif tool == "set_job_active":
            if not auth.can(role, "settings"):
                return _denied(conn, p, operator, role, "settings")
            jid = int(args["job_id"])
            active = bool(args["active"])
            r = db.set_job_active(conn, jid, active, operator)
            if not r:
                return {"ok": False, "error": f"未找到岗位 #{jid}"}
            result["after"] = {"job_id": jid, "title": r.get("title"), "active": active}

        elif tool == "manage_department":
            if not auth.can(role, "settings"):
                return _denied(conn, p, operator, role, "settings")
            action = str(args.get("action") or "").strip().lower()
            if action in ("create", "update"):
                name = str(args.get("name") or "").strip()
                desc = str(args.get("description") or "")
                did = args.get("department_id")
                if did:
                    d = db.get_department(conn, int(did))
                    if not d:
                        return {"ok": False, "error": f"部门 #{did} 不存在"}
                    same = conn.execute(
                        "SELECT id FROM departments WHERE name = ? AND id <> ?",
                        (name, int(did))).fetchone()
                    if same:
                        return {"ok": False, "error": f"已有同名部门「{name}」"}
                    conn.execute(
                        "UPDATE departments SET name=?, description=?, active=1, updated_at=? "
                        "WHERE id=?", (name, desc, db.now(), int(did)))
                    conn.commit()
                    db.add_audit(conn, "department", str(did), "update",
                                 d.get("name") or "", name, operator, role)
                    out_id = int(did)
                else:
                    out_id = db.upsert_department(conn, name, desc, operator)
                result["after"] = {"department_id": out_id, "name": name}
            elif action == "active":
                did = int(args["department_id"])
                r = db.set_department_active(conn, did, bool(args["active"]), operator)
                if not r:
                    return {"ok": False, "error": f"部门 #{did} 不存在"}
                result["after"] = {"department_id": did, "active": bool(args["active"])}
            elif action == "delete":
                did = int(args["department_id"])
                r = db.set_department_active(conn, did, False, operator)
                if not r:
                    return {"ok": False, "error": f"部门 #{did} 不存在"}
                result["after"] = {"department_id": did, "deleted": True,
                                   "note": "系统不支持物理删除，已停用并保留历史投递/岗位"}
            else:
                return {"ok": False, "error": "action 只能是 create / update / active / delete"}

        elif tool == "manage_user":
            if not auth.can(role, "manage_users"):
                return _denied(conn, p, operator, role, "manage_users")
            action = str(args.get("action") or "").strip().lower()
            username = str(args.get("username") or "").strip()
            # 「最后一个管理员」校验与写入放进同一个写事务，防两个提案并发各降级一个。
            db.begin_immediate(conn)
            user = db.get_user(conn, username)
            if action == "create":
                if user:
                    return {"ok": False, "error": f"账号「{username}」已存在"}
                new_role = str(args.get("role") or "").strip()
                if new_role not in auth.ROLES:
                    return {"ok": False, "error": "角色只能是 hr / admin / viewer"}
                db.create_user(conn, username, str(args.get("password") or ""),
                               new_role, str(args.get("display_name") or "").strip())
                db.add_audit(conn, "user", username, "create", "",
                             f"{operator} 创建账号「{username}」（角色 {new_role}）",
                             operator, role)
                result["after"] = {"username": username, "role": new_role}
            elif action == "update":
                if not user:
                    return {"ok": False, "error": f"账号「{username}」不存在"}
                new_role = args.get("role")
                active = args.get("active")
                losing_admin = (user.get("role") == "admin" and user.get("active")
                                and ((new_role is not None and new_role != "admin")
                                     or active is False))
                if losing_admin and db.count_active_admins(conn) <= 1:
                    return {"ok": False,
                            "error": "这是最后一个启用中的管理员，不能再停用或降级"}
                if active is False and username == operator:
                    return {"ok": False, "error": "不能停用自己当前登录的账号"}
                if new_role is not None and new_role not in auth.ROLES:
                    return {"ok": False, "error": "角色只能是 hr / admin / viewer"}
                changed = []
                _sets: list[str] = []
                _vals: list = []
                if args.get("display_name") is not None:
                    disp = str(args.get("display_name") or "").strip() or username
                    _sets.append("display_name=?")
                    _vals.append(disp)
                    changed.append("显示名")
                if new_role is not None and new_role != user.get("role"):
                    _sets.append("role=?")
                    _vals.append(new_role)
                    changed.append("角色")
                if active is not None and bool(active) != bool(user.get("active")):
                    _sets.append("active=?")
                    _vals.append(1 if active else 0)
                    changed.append("启用状态")
                if _sets:
                    conn.execute(f"UPDATE users SET {', '.join(_sets)} WHERE username=?",
                                 (*_vals, username))
                conn.commit()
                kicked = 0
                if new_role is not None or active is not None:
                    kicked = db.delete_user_sessions(conn, username)
                db.add_audit(conn, "user", username, "update", "",
                             f"{operator} 修改账号「{username}」：{'、'.join(changed) or '无变化'}"
                             + (f"（清除 {kicked} 个会话）" if kicked else ""),
                             operator, role)
                result["after"] = {"username": username, "changed": changed}
            elif action == "password":
                if not user:
                    return {"ok": False, "error": f"账号「{username}」不存在"}
                db.set_user_password(conn, username, str(args.get("password") or ""))
                kicked = db.delete_user_sessions(conn, username)
                db.add_audit(conn, "user", username, "set_password", "",
                             f"{operator} 重置账号「{username}」的密码"
                             f"（清除 {kicked} 个会话）", operator, role)
                result["after"] = {"username": username, "sessions_cleared": kicked}
            elif action == "delete":
                if not user:
                    return {"ok": False, "error": f"账号「{username}」不存在"}
                if username == operator:
                    return {"ok": False, "error": "不能删除自己当前登录的账号"}
                if (user.get("role") == "admin" and user.get("active")
                        and db.count_active_admins(conn) <= 1):
                    return {"ok": False, "error": "这是最后一个启用中的管理员，不能删除"}
                conn.execute("UPDATE users SET active=0 WHERE username=?", (username,))
                conn.commit()
                kicked = db.delete_user_sessions(conn, username)
                db.add_audit(conn, "user", username, "delete", "",
                             f"{operator} 停用账号「{username}」（保留记录供审计）",
                             operator, role)
                result["after"] = {"username": username, "active": False,
                                   "sessions_cleared": kicked}
            else:
                return {"ok": False, "error": "action 只能是 create / update / password / delete"}

        elif tool == "manage_recruit_plan":
            if not auth.can(role, "edit_plan"):
                return _denied(conn, p, operator, role, "edit_plan")
            action = str(args.get("action") or "").strip().lower()
            if action == "create_year":
                yid = db.create_recruit_year(
                    conn, str(args.get("label") or "").strip(),
                    str(args.get("period_start") or "").strip(),
                    str(args.get("period_end") or "").strip(),
                    operator=operator, role=role)
                result["after"] = {"year_id": yid}
            elif action == "update_year":
                year_id = int(args["year_id"])
                fields = {k: args[k] for k in
                          ("label", "period_start", "period_end", "org_label", "note")
                          if k in args}
                r = db.update_recruit_year(conn, year_id, fields,
                                           operator=operator, role=role)
                if not r:
                    return {"ok": False, "error": f"未找到届别 #{year_id}"}
                result["after"] = {"year_id": year_id,
                                   "changed": sorted(fields)}
            elif action in ("add_unit", "update_rows", "delete_unit"):
                year_id = int(args["year_id"])
                year = db.get_recruit_year(conn, year_id=year_id)
                if not year:
                    return {"ok": False, "error": f"未找到届别 #{year_id}"}
                if action == "add_unit":
                    unit = str(args.get("unit_name") or "").strip()
                    levels = [x for x in (args.get("edu_levels") or [])
                              if x in recruit_mod.EDU_LEVELS] or list(recruit_mod.EDU_LEVELS)
                    added = 0
                    for i, edu in enumerate(levels):
                        out = db.save_recruit_plan(
                            conn, year_id=year_id, unit_name=unit, edu_level=edu,
                            plan_approved=int(args.get("plan_approved") or 0),
                            plan_extra=int(args.get("plan_extra") or 0),
                            plan_carryover=int(args.get("plan_carryover") or 0),
                            plan_cancel=int(args.get("plan_cancel") or 0),
                            sort_order=i, mode="merge")
                        added += 1 if out.get("action") == "inserted" else 0
                    db.add_audit(conn, "recruit_plan", str(year_id), "add_unit", "",
                                 f"{unit}：新增 {added} 行", operator, role)
                    result["after"] = {"year_id": year_id, "unit_name": unit,
                                       "added": added}
                elif action == "update_rows":
                    rows = args.get("rows") or []
                    errors = recruit_mod.validate_plan_rows(rows)
                    if errors:
                        return {"ok": False, "error": "；".join(errors)}
                    changed = db.update_recruit_plans_batch(
                        conn, year_id, rows, operator=operator, role=role)
                    result["after"] = {"year_id": year_id, "changed": len(changed)}
                else:
                    r = db.delete_recruit_unit(
                        conn, year_id, str(args.get("unit_name") or "").strip(),
                        operator=operator, role=role)
                    result["after"] = {"year_id": year_id, **r}
            else:
                return {"ok": False, "error": "action 只能是 create_year / add_unit / update_rows / delete_unit"}

        elif tool == "import_domain_pack":
            if not auth.can(role, "settings"):
                return _denied(conn, p, operator, role, "settings")
            from .pipeline import domains as _domains
            try:
                r = _domains.import_pack(conn, str(args.get("pack") or ""),
                                         operator=operator)
            except (ValueError, FileNotFoundError) as exc:
                return {"ok": False, "error": str(exc)}
            result["after"] = r

        elif tool == "manage_avatar":
            if not auth.can(role, "chat"):
                return _denied(conn, p, operator, role, "chat")
            import base64
            import io
            import os as _os
            from . import avatar as _avatar
            from .paths import DATA_DIR
            from PIL import Image
            avatars_dir = _os.path.join(DATA_DIR, "avatars")
            action = str(args.get("action") or "").strip().lower()
            if action == "backfill_start":
                result["after"] = _avatar.start_backfill(db_path, avatars_dir)
            elif action == "backfill_stop":
                result["after"] = _avatar.stop_backfill()
            else:
                cid = int(args["candidate_id"])
                cand = db.get_candidate(conn, cid)
                if not cand:
                    return {"ok": False, "error": f"未找到候选人 #{cid}"}
                if action == "delete":
                    _avatar.clear(conn, cid)
                    db.add_audit(conn, "candidate", str(cid), "avatar_clear", "",
                                 "去掉证件照", operator, role)
                    result["after"] = {"candidate_id": cid, "avatar_path": "-"}
                elif action == "refresh":
                    _avatar.reset(conn, cid)
                    rel = _avatar.ensure(conn, cid, avatars_dir)
                    ok = bool(rel) and rel != _avatar.NONE_MARK
                    db.add_audit(conn, "candidate", str(cid), "avatar_refresh", "",
                                 "重新抽取证件照" + ("" if ok else "（没抽到）"), operator, role)
                    result["after"] = {"candidate_id": cid, "avatar_path": rel, "ok": ok}
                elif action == "upload":
                    raw = str(args.get("image_base64") or "").strip()
                    try:
                        data = base64.b64decode(raw, validate=False)
                        img = Image.open(io.BytesIO(data)).convert("RGB")
                        img.thumbnail((_avatar.AVATAR_SIZE, _avatar.AVATAR_SIZE))
                    except Exception as exc:              # noqa: BLE001
                        return {"ok": False,
                                "error": f"图片解析失败：{type(exc).__name__}: {exc}"}
                    _os.makedirs(avatars_dir, exist_ok=True)
                    fn = f"{cid}.jpg"
                    path = _os.path.join(avatars_dir, fn)
                    img.save(path, "JPEG", quality=88)
                    rel = f"data/avatars/{fn}"
                    conn.execute("UPDATE candidates SET avatar_path = ?, updated_at = ? WHERE id = ?",
                                 (rel, db.now(), cid))
                    conn.commit()
                    db.add_audit(conn, "candidate", str(cid), "avatar_upload", "",
                                 "上传证件照", operator, role)
                    result["after"] = {"candidate_id": cid, "avatar_path": rel}
                else:
                    return {"ok": False, "error": "action 只能是 backfill_start / backfill_stop / delete / refresh / upload"}

        elif tool == "update_job_info":
            if not auth.can(role, "settings"):
                return _denied(conn, p, operator, role, "settings")
            jid = int(args["job_id"])
            job = db.get_job(conn, jid)
            if not job:
                return {"ok": False, "error": f"未找到岗位 #{jid}"}
            patch = {}
            if "title" in args:
                patch["title"] = str(args["title"]).strip()
            if "headcount" in args:
                patch["headcount"] = args["headcount"]
            if "department_id" in args:
                did = args["department_id"]
                patch["dept_id"] = did
                patch["dept"] = (db.get_department(conn, int(did)) or {}).get("name", "") if did else ""
            if not patch:
                return {"ok": False, "error": "没有要修改的字段"}
            sets = ", ".join(f"{k} = ?" for k in patch)
            conn.execute(f"UPDATE jobs SET {sets}, updated_at = ? WHERE id = ?",
                         (*patch.values(), db.now(), jid))
            conn.commit()
            db.add_audit(conn, "job", str(jid), "update",
                         "；".join(f"{k}={job.get(k) or '（空）'}" for k in patch),
                         "；".join(f"{k}={patch[k] or '（空）'}" for k in patch),
                         operator, role)
            result["after"] = {"job_id": jid, "changed": sorted(patch)}

        elif tool == "restore_chat_history":
            if not auth.can(role, "chat"):
                return _denied(conn, p, operator, role, "chat")
            result["after"] = db.restore_chat(conn, operator=operator, role=role)

        elif tool == "truncate_chat":
            if not auth.can(role, "chat"):
                return _denied(conn, p, operator, role, "chat")
            out = db.truncate_conversation(
                conn, str(args["conversation_id"]), operator,
                int(args["before_run_id"]), actor=operator, role=role)
            if not out:
                return {"ok": False, "error": "未找到该会话或该轮记录"}
            result["after"] = out

        elif tool == "import_recruit_plan_excel":
            if not auth.can(role, "edit_plan"):
                return _denied(conn, p, operator, role, "edit_plan")
            import os as _os
            fp = str(args.get("file_path") or "").strip()
            if not _os.path.isfile(fp):
                return {"ok": False, "error": f"文件不存在：{fp}"}
            with open(fp, "rb") as fh:
                parsed = recruit_mod.parse_plan_xlsx(fh.read())
            if not parsed.get("rows"):
                return {"ok": False, "error": "没有解析到计划行"}
            year_id = args.get("year_id")
            year = db.get_recruit_year(conn, year_id=int(year_id)) if year_id else None
            if not year:
                label = str(args.get("label") or "").strip()
                if not label:
                    return {"ok": False, "error": "首次导入请提供届别名称 label"}
                tp = parsed.get("title_period") or {}
                sug = recruit_mod.suggest_year()
                out = recruit_mod.create_year(
                    conn, label,
                    args.get("period_start") or tp.get("period_start") or sug["period_start"],
                    args.get("period_end") or tp.get("period_end") or sug["period_end"],
                    operator=operator, role=role)
                if not out.get("ok"):
                    return {"ok": False, "error": "；".join(out.get("errors") or [])}
                year = out["year"]
            report = recruit_mod.apply_plan_import(
                conn, parsed, int(year["id"]),
                mode=str(args.get("mode") or "merge"), operator=operator, role=role)
            result["after"] = {"year_id": year["id"], **report}

        else:
            return {"ok": False, "error": f"未知提案类型 {tool}"}

        db.decide_proposal(conn, proposal_id, "approve", operator, role,
                           str(result.get("after")))
        result["status"] = "已执行"
        result["message"] = "提案已由 HR 确认并生效"
        return result
    finally:
        # 任何提前返回/异常：若提案仍停在「执行中」，放回「待确认」，
        # 不让一次失败把提案永久锁死。成功路径此时已是「已执行/已拒绝」。
        try:
            # 先回滚未提交的零散写入：某条工具校验失败提前返回时，
            # 不能让 finally 里的 release/查询把半截改动顺手提交掉。
            conn.rollback()
            _cur = db.get_proposal(conn, proposal_id)
            if _cur and _cur.get("status") == "执行中":
                db.release_proposal(conn, proposal_id)
        except Exception:                                    # noqa: BLE001
            pass
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
    c = _llm.load_cfg("extract")
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
                  AND COALESCE(a.extract_mode,'') NOT IN ('llm','llm+rule')
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
