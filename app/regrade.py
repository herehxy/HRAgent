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


def _major_of(conn: sqlite3.Connection, candidate_id) -> dict:
    """取候选人已归一的专业（v1.11）。没有就返回空 dict，调用方照旧。"""
    try:
        row = conn.execute("SELECT major_canonical, major_via FROM candidates WHERE id = ?",
                           (candidate_id,)).fetchone()
        return dict(row) if row else {}
    except Exception:                                       # noqa: BLE001
        return {}


def regrade_job(conn: sqlite3.Connection, job_id: int, jd: dict, tiers: dict,
                operator: str = "hr", role: str = "hr", apply: bool = True,
                use_llm: bool = True) -> dict:
    """把 `job_id` 下所有投递按当前 JD 重算系统建议。返回逐人差异报告。

    v1.12 口径：档位不再由规则打分决定，所以重算 = **学历门槛重判 + 重新问一次模型**。
    学历不达标直接判 D（不问模型）；达标才调模型给 A/B/C。
    `apply=False` 为预演：完整算一遍差异，但**不写任何业务数据**，
    只留一条 `regrade_preview` 审计（"谁在何时看了这次重算的结果"本身值得留痕）。
    """
    apps = conn.execute(
        """SELECT a.*, c.name AS candidate_name, c.gender
           FROM applications a JOIN candidates c ON c.id = a.candidate_id
           WHERE a.job_id = ? AND COALESCE(c.archived_at, '') = ''
           ORDER BY a.id""", (job_id,)).fetchall()

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

        _mj = _major_of(conn, a.get("candidate_id"))
        if _mj.get("major_canonical"):
            cand["major_canonical"] = _mj["major_canonical"]
            cand["major_via"] = _mj.get("major_via") or "规则"
        # 按某个具体岗位重算它下面的投递：岗位明确 -> 允许按学历判 D
        g = grade(cand, jd, tiers, job_confirmed=True)
        # **档位重算（v1.12）**：规则只给学历门槛结论（不达标=D），
        # 达标时再问一次模型——HR 改的是 JD/尺子，档位本就该跟着新要求重新判断。
        # 学历不达标就不问了：结论已定（D），省一次调用也避免模型把硬门槛"说上去"。
        tier_src = "学历门槛"
        if use_llm and g["tier_suggested"] != "D":
            try:
                from .pipeline import analyze as analyze_mod

                cand["jd"] = jd
                _fit = analyze_mod.analyze_fit({**cand, "jd": jd}, jd)
                # 模型漏给档位时会单独追问一次（见 analyze.resolve_tier）
                _mt = analyze_mod.resolve_tier(cand, jd, _fit)
                if _mt:
                    g["tier_suggested"] = _mt
                    tier_src = "模型"
                    g["reasons"] = ([f"模型判断建议 {_mt} 档"]
                                    + [x for x in (g.get("reasons") or [])])[:4]
            except Exception as exc:                           # noqa: BLE001
                print(f"[regrade_job] 模型重判档位失败，保留学历结论："
                      f"{type(exc).__name__}: {exc}", file=sys.stderr)
        old_tier, new_tier = a.get("tier_suggested"), g["tier_suggested"]
        old_score, new_score = a.get("score"), None            # v1.12 起不再有分数
        diff = (old_tier or "") != (new_tier or "")

        item = {
            "application_id": a["id"], "candidate_id": a.get("candidate_id"),
            "name": a.get("candidate_name"),
            "old_score": old_score, "new_score": new_score,
            "old_tier": old_tier, "new_tier": new_tier, "tier_source": tier_src,
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
                             f"{old_tier or '未判档'}",
                             f"JD 变更后重算为 {new_tier or '待分析'}（来源：{tier_src}）",
                             operator, role)
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
        "channel_note": ("学历门槛由规则重判、档位由模型重新判断"
                          + ("（本次未调模型：模型未启用）" if not use_llm else "")
                          + "；HR 已确认的档位不会被覆盖。"),
        "summary": summary,
        "items": items,
    }


# ============================================================
# C 方案：待指定投递的岗位建议（只建议、HR 确认才归岗）
# ============================================================

def suggest_jobs(conn: sqlite3.Connection, doc_id: int | None,
                 jobs: list[dict]) -> list[dict]:
    """给一条「待指定」投递出**最多一个**建议岗位（v1.12）。

    为什么不再按分数降序返回一串候选：加权打分已删（见 tier.grade 的 v1.12 说明），
    没有分数就没有"第二候选"的意义——真正要回答的只有一个问题："这份简历最像哪个岗位"，
    这个交给模型；模型说不出（或原文不可用）就返回空列表，如实不猜。
    纯计算、**不写任何库**。`jobs` 须为 [{"id","title","dept","jd"}, ...]（jd 已解析）。
    """
    if not jobs:
        return []
    from .pipeline import analyze as analyze_mod

    cand = _reprofile(conn, doc_id, jobs[0].get("jd") or {})
    if cand is None:
        return []
    pick = analyze_mod.suggest_job(cand, jobs)
    if not pick:
        return []
    j = next((x for x in jobs if (x.get("title") or "").strip() == pick["title"]), None)
    if not j:
        return []
    jd = j.get("jd") or {}
    # 用该岗位的尺子重抽一次：技能词表来自岗位 JD，命中口径才与该岗位一致
    cj = _reprofile(conn, doc_id, jd) or cand
    g = grade(cj, jd, {})
    return [{"job_id": j["id"], "title": j.get("title") or "", "dept": j.get("dept") or "",
             "reason": pick.get("reason") or "", "hits": g["hit"], "miss": g["miss"],
             "tier_suggested": g["tier_suggested"]}]


def route_pending(conn: sqlite3.Connection, tiers: dict, apply: bool = True,
                  operator: str = "system", role: str = "system",
                  use_llm: bool = True) -> dict:
    """给所有「待指定」投递补上**建议岗位**，并按该岗位的尺子重算系统建议（v1.12）。

    为什么需要它（而不是只靠入库时算）：
    - **存量数据**：早期入库的投递，`suggested_job_id` 是按"默认尺子打分最高"给的；
      加权打分已在 v1.12 删除，这些结论需要按新口径（模型判断）重刷一遍；
    - **岗位表变了**：新建了岗位、或改了某个岗位的 JD，"最像哪个岗位"就变了。

    v1.12 口径：**每份待指定简历调一次模型**判断最像哪个在招岗位（不再逐岗位打分——
    没有分数就无法排序，逐岗位"试算"无从比较）。模型说不出来 → 保持待指定、清掉旧建议，
    不硬凑一个岗位。**绝不覆盖 HR 已确认的档位**（`tier_final` 不动，只刷新"系统建议"）。
    `apply=False` 时只算不写（预演）。只对**尚未归岗**的投递生效。
    """
    from .pipeline import analyze as analyze_mod

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
        # 先用默认尺子抽一次画像（只为让模型读懂这份简历），再问模型最像哪个岗位
        cand = _reprofile(conn, r["resume_doc_id"], jobs[0].get("jd") or {})
        if cand is None:
            no_text += 1
            items.append({"application_id": r["id"], "candidate_id": r["candidate_id"],
                          "name": None, "cannot_route": "简历原文不可用（解析失败）"})
            continue
        _mjc = _major_of(conn, r["candidate_id"])
        if _mjc.get("major_canonical"):
            cand["major_canonical"] = _mjc["major_canonical"]
            cand["major_via"] = _mjc.get("major_via") or "规则"
        pick = analyze_mod.suggest_job(cand, jobs) if use_llm else None
        j = next((x for x in jobs
                  if (x.get("title") or "").strip() == (pick or {}).get("title")), None)
        if j is not None:
            # 用该岗位的 JD 尺子重抽：技能词表来自岗位 JD，命中口径才与岗位一致
            cj = _reprofile(conn, r["resume_doc_id"], j["jd"]) or cand
            if _mjc.get("major_canonical"):
                cj["major_canonical"] = _mjc["major_canonical"]
                cj["major_via"] = _mjc.get("major_via") or "规则"
            cand = cj
            g = grade(cj, j["jd"], tiers, job_confirmed=False)
            usable = True
        else:
            # 模型未判断出岗位：保持待指定。档位无从判定——没有岗位门槛可比
            g = {"tier_suggested": None, "score": None,
                 "reasons": ["未归岗：模型未判断出对应岗位" if use_llm
                             else "未归岗：模型未启用，未做归岗判断"],
                 "risks": [], "hit": [], "miss": [], "preferred_hit": [],
                 "hit_detail": [], "breakdown": {}, "needs_review": False}
            usable = False
        name = conn.execute("SELECT name FROM candidates WHERE id = ?",
                            (r["candidate_id"],)).fetchone()
        moved = (r["suggested_job_id"] != (j["id"] if usable else None)
                 or (r["tier_suggested"] or "") != (g["tier_suggested"] or ""))
        if moved:
            changed += 1
        items.append({
            "application_id": r["id"], "candidate_id": r["candidate_id"],
            "name": (name["name"] if name else None),
            "before": {"job_id": r["suggested_job_id"], "tier": r["tier_suggested"]},
            "after": {"job_id": j["id"] if usable else None,
                      "title": (j or {}).get("title") or "",
                      "reason": (pick or {}).get("reason") or "",
                      "tier": g["tier_suggested"], "usable": usable},
            "considered": len(jobs)})
        if not apply:
            continue
        # 技能清单也要跟着这次归位刷新：识别用的词表变了（多了岗位 JD 的技能词），
        # 不刷新就会出现"命中 Java / Spring Boot，但技能栏里没有 Java"这种自相矛盾的档案
        if usable:
            try:
                from . import ingest as _ingest

                _ingest.persist_skills(conn, r["candidate_id"], cand, None)
            except Exception as exc:                           # noqa: BLE001
                # 不静默吞：技能没刷新会让档案自相矛盾，必须留痕（缺陷 #40 的教训）
                print(f"[route_pending] 技能刷新失败（归位结论不受影响）："
                      f"{type(exc).__name__}: {exc}", file=sys.stderr)
        conn.execute(
            """UPDATE applications SET suggested_job_id = ?, suggested_job_reason = ?,
               score = ?, tier_suggested = ?,
               reasons = ?, risks = ?, hits = ?, miss = ?, preferred_hit = ?, breakdown = ?,
               updated_at = ? WHERE id = ?""",
            ((j["id"] if usable else None),
             ((pick or {}).get("reason") or None) if usable else None,
             g["score"], g["tier_suggested"],
             json.dumps(g["reasons"], ensure_ascii=False),
             json.dumps(g["risks"], ensure_ascii=False),
             json.dumps(g["hit"], ensure_ascii=False),
             json.dumps(g["miss"], ensure_ascii=False),
             json.dumps(g["preferred_hit"], ensure_ascii=False),
             json.dumps(g["breakdown"], ensure_ascii=False),
             db.now(), r["id"]))
        if moved:
            _why = ((f"模型从 {len(jobs)} 个在招岗位中判断最像「{j['title']}」#{j['id']}"
                     + (f"（依据：{pick['reason']}）" if (pick or {}).get("reason") else ""))
                    if usable else
                    (f"模型未判断出对应岗位（在招 {len(jobs)} 个），保持待指定" if use_llm
                     else f"模型未启用，未做归岗判断（在招 {len(jobs)} 个），保持待指定"))
            db.add_audit(conn, "application", str(r["id"]), "route_suggest",
                         f"建议岗位 {r['suggested_job_id']} / {r['tier_suggested']}",
                         _why, operator, role)
    if apply:
        conn.commit()
    return {"ok": True, "applied": bool(apply), "total": len(items), "changed": changed,
            "cannot_route": no_text, "open_jobs": len(jobs),
            "note": ("按模型判断重算「建议岗位」；HR 已确认的档位不受影响。" if apply
                     else "预演：只算不写。"),
            "items": items}


def suggest_job_for_application(conn: sqlite3.Connection, candidate_id: int,
                                operator: str = "hr", role: str = "hr") -> dict:
    """为某位候选人的「待指定」投递**主动判断一次**建议岗位，并把结论落库（v1.13.2）。

    为什么必须落库、而不是每次展示时现算：模型判断一次要 6-8 秒且消耗 token，
    而"建议岗位"是看一眼就够的东西。原来的列表接口对没有存储结论的投递
    **每次打开人才库都现调一次模型**——HR 实测反馈「每次点击人才库页面都会重新调用模型，
    这完全没必要，应该存储」。现在改成：库里有结论就直接读（免费），
    想更新时点一次这个动作，结论落 `suggested_job_id` + `suggested_job_reason`。

    只处理**未归岗**的投递（已归岗不需要建议）。模型判断不出 →
    清掉旧建议并如实说明，由 HR 手动「指定岗位」。
    """
    from .pipeline import analyze as analyze_mod

    row = conn.execute(
        "SELECT id, resume_doc_id, job_id FROM applications WHERE candidate_id = ? "
        "ORDER BY COALESCE(applied_at,'') DESC, id DESC LIMIT 1", (candidate_id,)).fetchone()
    if not row:
        return {"ok": False, "error": "该候选人没有投递记录"}
    if row["job_id"]:
        return {"ok": False, "error": "该候选人已归岗，不需要建议岗位（改归岗请用「指定岗位」）"}
    jobs = db.open_jobs_with_jd(conn)
    if not jobs:
        return {"ok": False, "error": "还没有在招岗位，请先到「岗位管理」建一个岗位"}
    cand = _reprofile(conn, row["resume_doc_id"], jobs[0].get("jd") or {})
    if cand is None:
        return {"ok": False, "error": "简历原文不可用（解析失败），无法判断建议岗位"}
    _mj = _major_of(conn, candidate_id)
    if _mj.get("major_canonical"):
        cand["major_canonical"] = _mj["major_canonical"]
        cand["major_via"] = _mj.get("major_via") or "规则"
    pick = analyze_mod.suggest_job(cand, jobs)
    j = next((x for x in jobs
              if (x.get("title") or "").strip() == (pick or {}).get("title")), None)
    jid = j["id"] if j else None
    reason = ((pick or {}).get("reason") or "") if jid else ""
    conn.execute(
        "UPDATE applications SET suggested_job_id = ?, suggested_job_reason = ?, updated_at = ? "
        "WHERE id = ?", (jid, reason or None, db.now(), row["id"]))
    db.add_audit(conn, "application", str(row["id"]), "suggest_job",
                 "（原建议已清空）" if not jid else f"建议岗位 {jid}",
                 (f"模型判断最像「{j['title']}」：{reason}" if jid
                  else "模型未能判断出对应岗位，保持待指定"),
                 operator, role)
    conn.commit()
    return {"ok": True, "job_id": jid, "title": (j or {}).get("title") or "",
            "reason": reason,
            "note": ("已判断并落库：下次打开人才库直接读库，不再调用模型" if jid
                     else "模型未能判断出对应岗位；可在卡片上手动「指定岗位」")}


def assign_job(conn: sqlite3.Connection, application_id: int, job_id: int,
               jd: dict, tiers: dict, operator: str = "hr", role: str = "hr") -> dict:
    """把投递归到 HR 指定的岗位（**归岗 / 改岗位通用**），并按该岗位 JD 重算系统建议。

    v1.13.3：**支持改岗位**（原来只允许「待指定 → 归岗」，已归岗的再归会被拒）。
    为什么改：HR 明确要求"给每个人一个修改岗位的按钮"——识别错、或人换了方向，
    都需要改归属；只报错不给改，等于逼 HR 去绕（实测反馈）。
    改岗位时同样重算建议档位与技能清单，并写审计（记下"从哪改到哪"）。
    HR 已确认的档位（tier_final）不受影响——重算只更新「系统建议」字段。
    """
    row = conn.execute("SELECT * FROM applications WHERE id = ?",
                       (application_id,)).fetchone()
    if not row:
        return {"error": "未找到该投递"}
    a = dict(row)
    old_job_id = a.get("job_id")
    if old_job_id == job_id:
        return {"ok": True, "application_id": application_id, "job_id": job_id,
                "job_title": "", "note": "该投递已经属于这个岗位，未做改动", "unchanged": True}
    old_title = ""
    if old_job_id:
        _oj = conn.execute("SELECT title FROM jobs WHERE id = ?", (old_job_id,)).fetchone()
        old_title = (_oj["title"] if _oj else f"#{old_job_id}")
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
        # 归岗后重算：岗位是 HR 明确指定的 -> 允许按学历判 D
        g = grade(cand, jd, tiers, job_confirmed=True)
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
    # 审计要能分清"归岗"与"改岗位"：改岗位属于**人工纠正**，不是首次归位
    _action = "reassign_job" if old_title else "assign_job"
    db.add_audit(conn, "application", str(application_id), _action,
                 (f"原岗位 {old_title}" if old_title else "岗位未指定"), after, operator, role)
    conn.commit()
    return {"ok": True, "application_id": application_id, "job_id": job_id,
            "job_title": jrow["title"], "old_job_title": old_title,
            "changed": True, "note": note}


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
