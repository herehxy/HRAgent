"""动作层：把"待确认提案"真正落到库里。

分工很明确：

- 智能体只能**生成提案**（`agent/tools.py` 里的写类工具）；
- **只有 HR 确认**才会走到这里，由本模块执行真实写入并留审计。

这样"系统只建议、HR 才决定"就不是一句口号，而是代码结构上无法绕过的约束：
模型拿不到任何直接写库的路径。
"""
from __future__ import annotations

from . import auth, db


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

        elif tool == "merge_candidates":
            if not auth.can(role, "merge"):
                return _denied(conn, p, operator, role, "merge")
            r = db.merge_candidates(conn, int(args["source_id"]), int(args["target_id"]),
                                    operator, role)
            if not r.get("ok"):
                return {"ok": False, "error": r.get("error")}
            result["after"] = r

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
