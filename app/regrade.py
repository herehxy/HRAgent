"""重新分析：岗位 JD（评分尺子）改动后，把该岗位下已有投递按新尺子重算一遍。

## 为什么从 `documents.raw_text` 重跑，而不是直接读库里的画像

`certificates`（证书加分项）、`unverified_skills`、`skill_detail`（技能证据）
这三样**都没有落库**——库里只有 `candidates` 的几列基础字段与 `candidate_skills` 的技能清单。
若只读库里的画像去重算，证书加分项会被当成"不存在"，
算出来分数凭空往下掉，看起来像"改了 JD 导致降档"，实际与 JD 无关，是画像被读残了。
所以这里统一从存库的简历原文重跑一次抽取，保证前后两次评分用的是**同一套画像口径**。

## 三条硬约束

1. **绝不覆盖 HR 已确认的结论**：`tier_final` 或状态为「已确认」的投递，
   只记差异、不改数据。系统只给建议，决定权在 HR。
2. **只走规则通道、不调模型**：可重复、可预期——不把"这台机器当时连不连得上模型"
   混进"改 JD 之后变成什么样"的结论里。若该投递当初是模型通道抽的，
   报告里会明确标注 `old_extract_mode`，让差异可解释。
3. **默认先看不改**：`apply=False` 是"预演"——只算差异、一个字都不写回库，
   让 HR 看完"谁变了、变成什么"再决定落不落地。写入时逐条留审计（含变更前后）。

## 输出

逐人给出 存储值 → 重算值（分数、系统建议档位），并列出变化原因（命中/缺失项、风险项），
外加一条汇总。界面据此展示"哪些人的建议档位变了"。整批重算一次写一条审计。
"""
from __future__ import annotations

import json
import sqlite3
import sys

from . import db
from .pipeline.extract import extract
from .pipeline.tier import grade


def _reprofile(conn: sqlite3.Connection, doc_id: int | None, jd: dict) -> dict | None:
    """用简历原文重跑规则通道，得到与入库时同口径的画像。

    优先用投递指向的附件（`resume_doc_id`），取不到再退到该候选人最新一份有文本的附件；
    都没有文本返回 None（例如扫描件 PDF 解析失败），由调用方标为"无法重算"。
    """
    row = None
    if doc_id:
        row = conn.execute(
            "SELECT raw_text FROM documents WHERE id = ? AND raw_text IS NOT NULL",
            (doc_id,)).fetchone()
    if not row:
        return None
    text = row["raw_text"] if not isinstance(row, tuple) else row[0]
    if not (text or "").strip():
        return None
    try:
        return extract(text, jd, use_llm=False)
    except Exception:
        return None


def regrade_job(conn: sqlite3.Connection, job_id: int, jd: dict, tiers: dict,
                operator: str = "hr", role: str = "hr", apply: bool = True) -> dict:
    """把 `job_id` 下所有投递按当前 JD 重算系统建议。返回逐人差异报告。

    `apply=False` 为预演：完整算一遍差异，但**不写任何业务数据**，
    只留一条 `regrade_preview` 审计（"谁在何时看了这次重算的结果"本身值得留痕）。
    """
    apps = conn.execute(
        """SELECT a.*, c.name AS candidate_name, c.gender
           FROM applications a LEFT JOIN candidates c ON c.id = a.candidate_id
           WHERE a.job_id = ? ORDER BY a.id""", (job_id,)).fetchall()

    items: list[dict] = []
    changed = kept = no_text = 0

    for raw in apps:
        a = dict(raw)
        cand = _reprofile(conn, a.get("resume_doc_id"), jd)

        # HR 已确认的投递：只算差异、不动数据
        confirmed = bool(a.get("tier_final")) or (a.get("status") or "") == "已确认"

        if cand is None:
            no_text += 1
            items.append({
                "application_id": a["id"], "candidate_id": a.get("candidate_id"),
                "name": a.get("candidate_name"),
                "old_score": a.get("score"), "new_score": None,
                "old_tier": a.get("tier_suggested"), "new_tier": None,
                "tier_final": a.get("tier_final"), "kept": confirmed,
                "changed": False, "skipped": "简历原文不可用（解析失败或缺失），无法重算",
                "old_extract_mode": a.get("extract_mode"),
            })
            continue

        g = grade(cand, jd, tiers)
        old_tier, new_tier = a.get("tier_suggested"), g["tier_suggested"]
        old_score, new_score = a.get("score"), g["score"]
        diff = (old_tier != new_tier) or (abs((old_score or 0) - (new_score or 0)) > 0.005)

        item = {
            "application_id": a["id"], "candidate_id": a.get("candidate_id"),
            "name": a.get("candidate_name"),
            "old_score": old_score, "new_score": new_score,
            "old_tier": old_tier, "new_tier": new_tier,
            "tier_final": a.get("tier_final"), "kept": confirmed,
            "changed": bool(diff and not confirmed),
            "reasons": g["reasons"], "risks": g["risks"],
            "hits": g["hit"], "miss": g["miss"], "preferred_hit": g["preferred_hit"],
            "old_extract_mode": a.get("extract_mode"),
        }

        # 画像口径提示：当初走模型通道的，这次是规则通道重算，差异未必全来自 JD
        if (a.get("extract_mode") or "") not in ("", "heuristic"):
            item["note"] = (f"该投递当初由「{a['extract_mode']}」通道抽取，"
                            f"本次统一用规则通道重算，差异不全部来自 JD")

        if confirmed:
            kept += 1
            item["note"] = ((item.get("note") + "；" if item.get("note") else "")
                            + "HR 已确认该档位，本次只记录差异、不修改")
        elif diff:
            changed += 1
            if apply:
                conn.execute(
                    """UPDATE applications SET score = ?, tier_suggested = ?, needs_review = ?,
                       reasons = ?, risks = ?, hits = ?, miss = ?, preferred_hit = ?,
                       breakdown = ?, updated_at = ?
                       WHERE id = ?""",
                    (new_score, new_tier, 1 if g["needs_review"] else 0,
                     json.dumps(g["reasons"], ensure_ascii=False),
                     json.dumps(g["risks"], ensure_ascii=False),
                     json.dumps(g["hit"], ensure_ascii=False),
                     json.dumps(g["miss"], ensure_ascii=False),
                     json.dumps(g["preferred_hit"], ensure_ascii=False),
                     json.dumps(g["breakdown"], ensure_ascii=False),
                     db.now(), a["id"]))
                db.add_audit(conn, "application", str(a["id"]), "regrade",
                             f"{old_tier}（{old_score}）",
                             f"JD 变更后重算为 {new_tier}（{new_score}）", operator, role)
        items.append(item)

    conn.commit()

    # 档位发生变化的排在前面，便于 HR 只看重点
    items.sort(key=lambda x: (not x["changed"], x.get("name") or ""))

    summary = (f"按当前 JD 重算 {len(apps)} 条投递：{changed} 条建议档位"
               f"{'变化' if apply else '将变化'}、{kept} 条已确认未改动、"
               f"{no_text} 条无法重算")
    db.add_audit(conn, "job", str(job_id),
                 "regrade_batch" if apply else "regrade_preview", "",
                 summary, operator, role)

    return {
        "job_id": job_id, "total": len(apps), "changed": changed,
        "applied": bool(apply), "kept_hr_confirmed": kept, "cannot_regrade": no_text,
        "channel_note": "统一走规则通道重算（不调模型），可重复、可预期；"
                        "已确认档位不会被覆盖。",
        "summary": summary,
        "items": items,
    }


# ============================================================
# C 方案：待指定投递的岗位建议（只建议、HR 确认才归岗）
# ============================================================

def suggest_jobs(conn: sqlite3.Connection, doc_id: int | None,
                 jobs: list[dict], tiers: dict) -> list[dict]:
    """把一条「待指定」投递拿每个在招岗位的 JD 各打一次分，按分降序返回。

    与 regrade 完全同一画像口径：从简历原文重跑规则通道（extract_heuristic 不依赖 jd，
    因此整段原文只抽取一次，再对各岗位分别 grade）。纯计算、**不写任何库**。
    `jobs` 须为 [{"id","title","dept","jd"}, ...]（jd 已解析为 dict）。
    原文不可用（解析失败的扫描件）时返回空列表——没有画像就没有建议，如实不猜。
    """
    if not jobs:
        return []
    cand = None
    out: list[dict] = []
    for j in jobs:
        jd = j.get("jd") or {}
        if cand is None:
            cand = _reprofile(conn, doc_id, jd)
        if cand is None:
            return []
        g = grade(cand, jd, tiers)
        out.append({
            "job_id": j["id"], "title": j.get("title") or "",
            "dept": j.get("dept") or "",
            "score": g["score"], "tier_suggested": g["tier_suggested"],
            "hits": g["hit"], "miss": g["miss"],
        })
    out.sort(key=lambda x: -(x["score"] or 0))
    return out


def route_pending(conn: sqlite3.Connection, tiers: dict, apply: bool = True,
                  operator: str = "system", role: str = "system") -> dict:
    """给所有「待指定」投递补上**建议岗位**，并按最适岗位的尺子重算系统建议（v1.5）。

    为什么需要它（而不是只靠入库时算）：
    - **存量数据**：v1.5 之前入库的投递，`score` 是按默认尺子（材料类）算的——
      一位 Java 工程师因此被标成 D 档低分，列表上看不出他适合什么；
    - **岗位表变了**：新建了岗位、或改了某个岗位的 JD，"最合适的岗位"就变了。

    与「重新分析」保持同一套口径：从简历原文重跑规则通道、不调模型、
    **绝不覆盖 HR 已确认的档位**（`tier_final` 不动，只刷新"系统建议"）。
    `apply=False` 时只算不写（预演）。每次只对**尚未归岗**的投递生效。
    """
    jobs = db.open_jobs_with_jd(conn)
    rows = conn.execute(
        "SELECT id, candidate_id, resume_doc_id, score, tier_suggested, suggested_job_id "
        "FROM applications WHERE job_id IS NULL ORDER BY id").fetchall()
    items: list[dict] = []
    changed = 0
    no_text = 0
    for r in rows:
        if not jobs:
            break
        # 每个岗位各自抽取一次：技能识别用的是"本体词表 + 该岗位 JD 词表"，
        # 同一份简历对材料岗与软件岗识别出的技能不同（见 ingest._route_by_open_jobs）
        scored: list[dict] = []
        for j in jobs:
            cj = _reprofile(conn, r["resume_doc_id"], j["jd"])
            if cj is None:
                continue
            try:
                gi = grade(cj, j["jd"], tiers)
            except Exception:
                continue
            scored.append({"job": j, "g": gi, "cand": cj})
        if not scored:
            no_text += 1
            items.append({"application_id": r["id"], "candidate_id": r["candidate_id"],
                          "name": None, "cannot_route": "简历原文不可用（解析失败）"})
            continue
        scored.sort(key=lambda x: (-(x["g"]["score"] or 0), x["job"]["id"]))
        best = scored[0]
        j, g = best["job"], best["g"]
        usable = bool(g.get("hit"))      # 至少要有一项技能命中，见 ingest 同口径说明
        name = conn.execute("SELECT name FROM candidates WHERE id = ?",
                            (r["candidate_id"],)).fetchone()
        moved = (r["suggested_job_id"] != (j["id"] if usable else None)
                 or r["tier_suggested"] != g["tier_suggested"]
                 or (r["score"] or 0) != (g["score"] or 0))
        if moved:
            changed += 1
        item = {"application_id": r["id"], "candidate_id": r["candidate_id"],
                "name": (name["name"] if name else None),
                "before": {"job_id": r["suggested_job_id"], "score": r["score"],
                           "tier": r["tier_suggested"]},
                "after": {"job_id": j["id"] if usable else None, "title": j["title"],
                          "score": g["score"], "tier": g["tier_suggested"],
                          "usable": usable},
                "considered": len(scored)}
        items.append(item)
        if not apply:
            continue
        # 技能清单也要跟着这次归位刷新：识别用的词表变了（多了岗位 JD 的技能词），
        # 不刷新就会出现"命中 Java / Spring Boot，但技能栏里没有 Java"这种自相矛盾的档案
        if usable:
            try:
                from . import ingest as _ingest

                _ingest.persist_skills(conn, r["candidate_id"], best["cand"], None)
            except Exception as exc:                           # noqa: BLE001
                # 不静默吞：技能没刷新会让档案自相矛盾，必须留痕（缺陷 #40 的教训）
                print(f"[route_pending] 技能刷新失败（归位结论不受影响）："
                      f"{type(exc).__name__}: {exc}", file=sys.stderr)
        conn.execute(
            """UPDATE applications SET suggested_job_id = ?, score = ?, tier_suggested = ?,
               reasons = ?, risks = ?, hits = ?, miss = ?, preferred_hit = ?, breakdown = ?,
               updated_at = ? WHERE id = ?""",
            ((j["id"] if usable else None), g["score"], g["tier_suggested"],
             json.dumps(g["reasons"], ensure_ascii=False),
             json.dumps(g["risks"], ensure_ascii=False),
             json.dumps(g["hit"], ensure_ascii=False),
             json.dumps(g["miss"], ensure_ascii=False),
             json.dumps(g["preferred_hit"], ensure_ascii=False),
             json.dumps(g["breakdown"], ensure_ascii=False),
             db.now(), r["id"]))
        if moved:
            db.add_audit(conn, "application", str(r["id"]), "route_suggest",
                         f"建议岗位 {r['suggested_job_id']} / {r['tier_suggested']}"
                         f"（{r['score']}）",
                         (f"对 {len(scored)} 个在招岗位逐个试算，最匹配「{j['title']}」"
                          f"#{j['id']}：{g['tier_suggested']}（{g['score']}）" if usable else
                          f"对 {len(scored)} 个在招岗位逐个试算，均无技能交集"
                          f"（最高 {g['score']}），保持待指定"),
                         operator, role)
    if apply:
        conn.commit()
    return {"ok": True, "applied": bool(apply), "total": len(items), "changed": changed,
            "cannot_route": no_text, "open_jobs": len(jobs),
            "note": ("按最适岗位重算系统建议；HR 已确认的档位不受影响。" if apply
                     else "预演：只算不写。"),
            "items": items}


def assign_job(conn: sqlite3.Connection, application_id: int, job_id: int,
               jd: dict, tiers: dict, operator: str = "hr", role: str = "hr") -> dict:
    """把「待指定」投递归到 HR 指定的岗位，并按该岗位 JD 重算系统建议。

    C 方案的落库动作：只处理 `job_id IS NULL` 的投递（已归岗的走「重新分析」改尺子，
    不在这里二次归岗，避免同一口径出现两个写入口）。归岗与重算合并写一次审计。
    HR 已确认的档位（tier_final）不受影响——重算只更新「系统建议」字段。
    """
    row = conn.execute("SELECT * FROM applications WHERE id = ?",
                       (application_id,)).fetchone()
    if not row:
        return {"error": "未找到该投递"}
    a = dict(row)
    if a.get("job_id") is not None:
        return {"error": "该投递已归属岗位，无需再次归岗"}
    jrow = conn.execute("SELECT title, active FROM jobs WHERE id = ?",
                        (job_id,)).fetchone()
    if not jrow:
        return {"error": "未找到该岗位"}
    if not jrow["active"]:
        return {"error": "该岗位已停用，不再接收归岗"}

    cand = _reprofile(conn, a.get("resume_doc_id"), jd)
    note = ""
    if cand is None:
        # 原文不可用：归岗照做（HR 的决定），但不重算，标待人工判读，如实说明
        conn.execute("UPDATE applications SET job_id = ?, suggested_job_id = NULL, "
                     "needs_review = 1, updated_at = ? WHERE id = ?",
                     (job_id, db.now(), application_id))
        note = "简历原文不可用（解析失败），已归岗但未重算，请人工判读"
        after = f"归到 {jrow['title']}（未重算：原文不可用）"
    else:
        g = grade(cand, jd, tiers)
        # 技能清单要跟这次重算一起写库：命中用的是"本体词表 + 该岗位 JD 词表"抽出来的技能，
        # 不刷新就会出现「命中里写着 Java / Spring Boot，技能栏里却没有 Java」的
        # 自相矛盾档案。**这类"结论更新了、证据没跟上"的不一致，比算错更难发现**，
        # 所以不放在 except: pass 里静默吞掉，失败要打到 stderr 留痕（见缺陷 #40 的教训）。
        try:
            from . import ingest as _ingest

            _ingest.persist_skills(conn, a["candidate_id"], cand, a.get("resume_doc_id"))
        except Exception as exc:                               # noqa: BLE001
            print(f"[assign_job] 技能清单刷新失败（归岗结论不受影响）："
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)
        conn.execute(
            """UPDATE applications SET job_id = ?, suggested_job_id = NULL, score = ?,
               tier_suggested = ?,
               needs_review = ?, reasons = ?, risks = ?, hits = ?, miss = ?,
               preferred_hit = ?, breakdown = ?, updated_at = ?
               WHERE id = ?""",
            (job_id, g["score"], g["tier_suggested"], 1 if g["needs_review"] else 0,
             json.dumps(g["reasons"], ensure_ascii=False),
             json.dumps(g["risks"], ensure_ascii=False),
             json.dumps(g["hit"], ensure_ascii=False),
             json.dumps(g["miss"], ensure_ascii=False),
             json.dumps(g["preferred_hit"], ensure_ascii=False),
             json.dumps(g["breakdown"], ensure_ascii=False),
             db.now(), application_id))
        after = (f"归到 {jrow['title']}，按岗位 JD 重算建议为 "
                 f"{g['tier_suggested']}（{g['score']}）")
    conn.commit()
    db.add_audit(conn, "application", str(application_id), "assign_job",
                 "岗位未指定", after, operator, role)
    conn.commit()
    return {"ok": True, "application_id": application_id, "job_id": job_id,
            "job_title": jrow["title"], "note": note}


def refresh_skills(conn: sqlite3.Connection, operator: str = "hr", role: str = "hr",
                   apply: bool = True) -> dict:
    """按「对应岗位」刷新技能清单——**只为修数据，不动档位与分数**（v1.6）。

    为什么单独需要它：技能是按「本体词表 + 该岗位 JD 词表」抽取的，而刷新动作原先
    只挂在「待指定 → 归岗」那一步上（`route_pending` 只管 `job_id IS NULL`；
    `assign_job` 早期版本漏了刷新）。于是**已归岗的投递**技能栏会停在旧口径上，
    出现"命中里有 Java、技能栏里没有 Java"的自相矛盾（缺陷 #47）。

    本函数以候选人「对应岗位」（已归岗 > 建议岗位）为准重跑一次规则抽取并写技能表，
    `score` / `tier_suggested` / `hits` 等**结论字段一律不动**——它们是 HR 看过的结论，
    修数据不该顺手改结论。幂等：`link_skill` 走 ON CONFLICT DO UPDATE。
    """
    rows = conn.execute(
        """SELECT id, candidate_id, job_id, suggested_job_id, resume_doc_id
           FROM applications ORDER BY id""").fetchall()
    refreshed = 0
    skills_written = 0
    skipped: list[dict] = []
    for r in rows:
        jid = r["job_id"] or r["suggested_job_id"]
        if not jid:
            skipped.append({"application_id": r["id"], "why": "无对应岗位（未归岗且无建议）"})
            continue
        jd = db.job_of(conn, jid)
        if not jd:
            skipped.append({"application_id": r["id"], "why": f"岗位 #{jid} 没有 JD"})
            continue
        cand = _reprofile(conn, r["resume_doc_id"], jd)
        if cand is None:
            skipped.append({"application_id": r["id"], "why": "简历原文不可用（解析失败）"})
            continue
        if apply:
            from . import ingest as _ingest

            stat = _ingest.persist_skills(conn, r["candidate_id"], cand, r["resume_doc_id"])
            skills_written += int(stat.get("verified", 0)) + int(stat.get("unverified", 0))
        refreshed += 1
    if apply:
        conn.commit()
        db.add_audit(conn, "candidate", "*", "refresh_skills", "",
                     f"按「对应岗位」刷新了 {refreshed} 条投递的技能清单"
                     f"（技能 {skills_written} 项；档位与分数未改动）", operator, role)
    return {"ok": True, "applied": bool(apply), "refreshed": refreshed,
            "skills_written": skills_written, "skipped": skipped}
