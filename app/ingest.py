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
from datetime import datetime

from . import db
from . import mailbox as mb
from .identity import build_identity_key
from .pipeline import parse as parse_mod
from .pipeline import sanitize
from .pipeline.extract import extract
from .pipeline.tier import grade

CONFIDENTIAL_HINTS = ("涉密", "机密", "秘密", "军工", "军品", "武器装备", "保密资格", "国防")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def archive_file(cfg: dict, filename: str, data: bytes) -> tuple[str, int]:
    """把附件写入原件区（按 年-月 分目录，文件名带内容哈希前缀，只增不改）。"""
    digest = sha256_bytes(data)
    folder = os.path.join(mb.resolve_dir(cfg.get("archive_dir", "data/archive")),
                          datetime.now().strftime("%Y-%m"))
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, f"{digest[:16]}_{mb.safe_filename(filename)}")
    if not os.path.exists(path):
        with open(path, "wb") as fh:
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
    """把归一后的技能写入本体关联表。含未核验项，但 `verified` 标记区分。"""
    stat = {"verified": 0, "unverified": 0, "skills_in_ontology": 0}
    for item in (cand.get("skill_detail") or []):
        canon = item.get("canonical")
        if not canon:
            continue
        sid = db.upsert_skill(conn, canon, item.get("category", "其他"))
        if db.find_skill(conn, canon):
            stat["skills_in_ontology"] += 1
        db.link_skill(conn, cid, sid, item.get("level"), item.get("evidence"),
                      doc_id, item.get("source", "rule"))
        if item.get("verified"):
            stat["verified"] += 1
        else:
            stat["unverified"] += 1
    return stat


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
    if existing:
        cid = existing["id"]
        db.update_candidate(conn, cid, last_active_at=db.now())
        return cid, False, {}

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
        return cid, False, {"suspect": True, "reason": "手机号/邮箱命中已有档案"}

    cid = db.insert_candidate(conn, {
        "identity_key": key,
        "name": cand.get("name"),
        "phone_enc": encrypt(phone),
        "email_enc": encrypt(email),
        "phone_bidx": pidx,
        "email_bidx": eidx,
        "edu_level": cand.get("education"),
        "school": cand.get("school"),
        "major": cand.get("major"),
        "years_exp": cand.get("years"),
        "current_org": cand.get("current_org"),
        # 性别：仅当简历明写标签行时才有值；**不参与任何评分/分级**，
        # 只用于界面标签与可开关的筛选（见 server 的 gender_filter_enabled）。
        "gender": cand.get("gender"),
        "source_first": channel,
        "pii_level": classify_pii(text),
    })
    db.add_audit(conn, "candidate", str(cid), "create", "", f"新建档案：{cand.get('name')}",
                 "system", "system")
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


def _route_by_open_jobs(conn, text: str, tiers: dict) -> dict | None:
    """把简历拿**每个在招岗位**的 JD 各试算一次，返回最合适的那个（附带画像、JD 与评分）。

    为什么在入库时就轮询、而不是等界面展示时再算：
    1. **评分口径统一**——落库的 `score/tier_suggested` 就是"与最合适岗位的匹配度"，
       详情页、导出、统计看到的是同一个数，不会出现"列表按软件岗、详情按材料岗"；
    2. 规则通道很便宜（纯正则 + 词表，不调模型），入库时做一次即可；
    3. 结论可复现：历史投递的分数不会因为"后来又新建了一个岗位"而悄悄变。

    注意**每个岗位要各自抽取一次**：技能识别用的是"本体词表 + 该岗位 JD 词表"，
    同一份简历对材料岗与软件岗能识别出的技能本就不同（软件岗 JD 写了 Java，
    简历里的 Java 才算命中）——因此不能"抽一次、对多岗位打分"。
    """
    jobs = _open_jobs_with_jd(conn)
    if not jobs:
        return None
    scored: list[dict] = []
    for j in jobs:
        try:
            cand = extract(text, j["jd"], use_llm=False)
            gi = grade(cand, j["jd"], tiers)
        except Exception:
            continue                     # 某个岗位的 JD 配置有问题，不影响其他岗位
        scored.append({**j, "cand": cand, "score": gi["score"],
                       "tier_suggested": gi["tier_suggested"], "hit": gi.get("hit") or []})
    if not scored:
        return None
    # 同分时取 id 小的，保证结果稳定可复现（不依赖字典/查询顺序）
    scored.sort(key=lambda x: (-(x.get("score") or 0), x["id"]))
    best = scored[0]
    best["considered"] = len(scored)
    # `usable`：**至少命中一项技能**才算"像得上这个岗位"。不能只看分数——
    # 学历达标、年限够、格式完整都能把分数抬到 0.3 上下，一位做应付账款的简历
    # 照样会被"算"出一个不高不低的分（实测：财务简历对材料岗得 0.3）。
    # 所以判定用命中项（`hit`）而不是分数：没有一项技能交集，就不出建议——
    # 硬凑一个岗位比不推荐更糟。
    best["usable"] = bool(best.get("hit"))
    return best


def ingest_one(conn, cfg: dict, jd: dict, tiers: dict, *, filename: str, data: bytes | None,
               local_path: str | None, channel: str, applied_at: str,
               source_message_id: str | None, job_id: int | None,
               use_llm: bool = False, llm_conf: dict | None = None) -> dict:
    """单份简历的完整处理链路，返回一条结果记录。"""
    result: dict = {"file": filename, "status": "unknown", "name": None, "tier": None,
                    "score": None, "notes": []}

    digest = sha256_bytes(data) if data is not None else _hash_path(local_path)
    result["hash"] = digest

    # —— 第二层去重：文件层 ——
    # 关键顺序：**先判重、后落盘**。若先落盘再判重，每一份重复投递都会在原件区
    # 留下一个台账（documents）里没有记录的同哈希副本——磁盘上出现"无账野文件"，
    # 审计时无法解释来源。内容逐字节相同的原件已有一份，重复投递的事实由
    # email_messages 台账与审计记录承担（谁、何时、又投了一次），信息并未丢失。
    if db.exists_hash(conn, digest):
        result["status"] = "skipped_dup"
        result["notes"].append("同一份简历已入库（SHA256 相同），原件已在库，不重复落盘")
        # 重复的这份原件虽然不落盘，但库里已有一份：把它的附件编号带出去，
        # 界面才能在"逐份明细"里直接预览/下载（否则 HR 只能看到文件名）
        prev = conn.execute("SELECT id FROM documents WHERE file_hash = ? "
                            "ORDER BY id LIMIT 1", (digest,)).fetchone()
        if prev:
            result["document_id"] = prev["id"]
        db.add_audit(conn, "document", digest[:16], "duplicate_skipped", "",
                     f"重复投递的附件 {filename} 未重复落盘（原件已在库）",
                     "system", "system")
        return result

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
    text, engine, ok = parse_mod.parse_file_ex(archived)
    if not ok:
        result["notes"].append(f"解析未成功（{engine}），已标『待人工判读』，原件保留")

    cand = extract(text, jd, use_llm=use_llm, llm_conf=llm_conf)

    # —— 归岗口径（v1.5）：**没有岗位的投递不拿默认尺子草草打分** ——
    # 旧做法：无岗位名（或文件夹导入）→ 用 config/jd.json 这把默认尺子（材料类）评分，
    # 于是一位 Java 工程师被"必需技能：真空熔铸/钛合金"打成 D 档低分，
    # 列表上完全看不出他适合什么（试用反馈："好几个明显是软件开发岗位的，标记却是材料"）。
    # 新做法：把**每个在招岗位**的 JD 都试一遍，取分数最高的那个作为「建议岗位」，
    # 并用它的尺子算分（命中/缺失/档位都是相对这个岗位的），落 `suggested_job_id`。
    # 岗位本身仍然不落 `job_id`——系统只建议，HR 点「采纳」才真正归岗。
    route = _route_by_open_jobs(conn, text, tiers) if job_id is None else None
    if route:
        # 画像用"胜出岗位"那一轮抽取的结果：技能清单里会包含该岗位 JD 写的词
        # （例如软件岗的 Java/Spring Boot），HR 点开档案看到的技能才与岗位对得上。
        cand = route["cand"]
        g = grade(cand, route["jd"], tiers)
    else:
        cand = extract(text, jd, use_llm=use_llm, llm_conf=llm_conf)
        g = grade(cand, jd, tiers)
    if not ok:
        g["needs_review"] = True
        g["risks"] = list(g.get("risks") or []) + ["原文未能解析，需人工查看附件原件"]
    result["name"] = cand.get("name")
    result["tier"] = g["tier_suggested"]
    result["score"] = g["score"]
    if route:
        result["suggested_job"] = {"job_id": route["id"] if route["usable"] else None,
                                   "title": route["title"],
                                   "score": route["score"],
                                   "tier_suggested": route["tier_suggested"],
                                   "considered": route["considered"]}
        result["notes"].append(
            f"未归岗：已对 {route['considered']} 个在招岗位逐个试算，"
            + (f"最匹配「{route['title']}」（{route['tier_suggested']}·{route['score']}），待 HR 采纳"
               if route["usable"] else
               f"与所有在招岗位均无技能交集（最高 {route['score']}），保持待指定、不出建议"))

    # —— 第三层去重：内容层（人）——
    cid, is_new, hint = ensure_person(conn, cand, channel, text, doc_digest=digest)
    if hint.get("suspect"):
        result["notes"].append(hint["reason"] + "，已归入已有档案")

    if is_new:
        apply_tags(conn, cid, cand, channel, text)
    skill_stat = persist_skills(conn, cid, cand, None)
    if skill_stat["unverified"]:
        result["notes"].append(f"{skill_stat['unverified']} 项技能未能在原文定位证据，未计入命中")

    # —— 投递：同一岗位近期重复投递 -> 作为新版本附件，不新建投递 ——
    window = int((cfg.get("dedup") or {}).get("same_job_reapply_days", 30) or 0)
    existing = _find_recent_application(conn, cid, job_id, window)

    if existing:
        # 既有投递没有岗位（v0.1 迁移数据）时回填，避免同一个人长期挂着"未指定岗位"的投递
        if existing.get("job_id") is None and job_id is not None:
            conn.execute("UPDATE applications SET job_id = ?, updated_at = ? WHERE id = ?",
                         (job_id, db.now(), existing["id"]))
            conn.commit()
            db.add_audit(conn, "application", str(existing["id"]), "job_bound",
                         "岗位未指定", f"按本次投递回填岗位 #{job_id}", "system", "system")
            existing = db.get_application(conn, existing["id"])

        # 仍「待指定」的投递：刷新建议岗位（新人新简历可能更匹配别的岗位，
        # 岗位表也可能新增/停用了岗位——建议是"当前在招岗位里的最适者"，本就不是定值）
        if route and existing.get("job_id") is None:
            conn.execute("UPDATE applications SET suggested_job_id = ? WHERE id = ?",
                         (route["id"], existing["id"]))
            conn.commit()
            existing = db.get_application(conn, existing["id"])

        doc_id = db.insert_document(conn, {
            "file_hash": digest, "candidate_id": cid, "application_id": existing["id"],
            "file_name": filename, "file_path": local_path or archived, "archived_path": archived,
            "mime": parse_mod.mime_of(filename), "size": size, "received_at": applied_at,
            "source_message_id": source_message_id, "raw_text": text,
            "parse_engine": engine, "parse_ok": ok,
        })
        refreshed = _regrade_if_unconfirmed(conn, existing, g)
        result["document_id"] = doc_id
        _fill_missing_gender(conn, cid, cand.get("gender"))
        if refreshed:
            result["status"] = "merged_version"
            result["notes"].append("同岗位近期已投递，本次作为新版本附件并刷新系统建议")
            db.add_audit(conn, "application", str(existing["id"]), "new_version",
                         f"档 {existing.get('tier_suggested')}",
                         f"新简历版本，系统建议更新为 {g['tier_suggested']}", "system", "system")
        else:
            result["status"] = "merged_version"
            result["notes"].append("同岗位近期已投递，新版本已归档；HR 已确认的档位未被覆盖")
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
    doc_id = db.insert_document(conn, {
        "file_hash": digest, "candidate_id": cid, "application_id": app_id,
        "file_name": filename, "file_path": local_path or archived, "archived_path": archived,
        "mime": parse_mod.mime_of(filename), "size": size, "received_at": applied_at,
        "source_message_id": source_message_id, "raw_text": text,
        "parse_engine": engine, "parse_ok": ok,
    })
    conn.execute("UPDATE applications SET resume_doc_id = ? WHERE id = ?", (doc_id, app_id))
    if route and route["usable"]:
        conn.execute("UPDATE applications SET suggested_job_id = ? WHERE id = ?",
                     (route["id"], app_id))
    conn.commit()
    db.add_audit(conn, "application", str(app_id), "ingest", "",
                 f"入库，系统建议 {g['tier_suggested']}（{g['score']}）"
                 + (f"，最匹配岗位「{route['title']}」#{route['id']}" if route else ""),
                 "system", "system")

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

def ingest_mails(mails: list[dict], cfg: dict, jd: dict, tiers: dict, db_path: str,
                 job_id: int | None = None, use_llm: bool = False,
                 llm_conf: dict | None = None) -> dict:
    """邮件批次入库。

    归岗口径：**只看邮件标题**。标题里出现某个在招岗位名 → 该封简历归到那个岗位，
    并用该岗位自己的 JD 评分；标题里没有岗位名（或岗位已停用）→ 投递落「所属岗位待指定」，
    由 HR 事后在界面上指定。

    因此 `job_id` 参数**不参与归岗**（保留仅为兼容调用签名）：显式传一个岗位 id
    也不会让标题没写岗位名的简历自动挂上去 —— 否则一次误传就会把整批简历写错岗位。
    """
    conn = db.connect(db_path)
    report = {"mails": len(mails), "attachments": 0, "added": 0, "merged_versions": 0,
              "skipped_dup": 0, "failed": 0, "no_attachment": 0, "parse_failed": 0,
              "routed": 0, "unassigned": 0, "details": []}
    try:
        for mail in mails:
            mid = mail.get("message_id")
            prev = conn.execute("SELECT processed FROM email_messages WHERE message_id = ?",
                                (mid,)).fetchone()
            if prev and prev["processed"]:
                report["skipped_dup"] += 1
                continue

            # —— 邮件标题归岗：主题里带了岗位名 → 归到该岗位；否则待 HR 指定 ——
            route_job_id, route_jd = db.match_job_by_subject(conn, mail.get("subject"))
            eff_job_id = route_job_id if route_job_id else None
            eff_jd = route_jd or jd
            if eff_job_id:
                report["routed"] += 1
            else:
                report["unassigned"] += 1

            db.record_email(conn, {
                "message_id": mid, "uid": mail.get("uid"), "mailbox": mail.get("mailbox"),
                "from_addr": mail.get("from_addr"), "subject": mail.get("subject"),
                "received_at": mail.get("received_at"),
                "has_attachment": bool(mail.get("attachments")),
                "attachment_count": len(mail.get("attachments") or []),
                "error": mail.get("error"),
            })

            atts = mail.get("attachments") or []
            if not atts:
                report["no_attachment"] += 1
                db.update_email(conn, mid, processed=1, processed_at=db.now(),
                                error=mail.get("error") or "无简历附件")
                continue

            added = mm = skipped = failed = pf = 0
            for att in atts:
                report["attachments"] += 1
                try:
                    r = ingest_one(conn, cfg, eff_jd, tiers,
                                   filename=att["filename"], data=att["data"],
                                   local_path=None, channel="邮箱",
                                   applied_at=mail.get("received_at") or db.now(),
                                   source_message_id=mid, job_id=eff_job_id,
                                   use_llm=use_llm, llm_conf=llm_conf)
                    if r["status"] == "added":
                        added += 1
                    elif r["status"] == "merged_version":
                        mm += 1
                    else:
                        skipped += 1
                    if any("待人工判读" in n for n in r["notes"]):
                        pf += 1
                    report["details"].append(r)
                except Exception as exc:  # 单份附件失败不影响整批
                    failed += 1
                    report["details"].append({"file": att.get("filename"), "status": "failed",
                                              "notes": [f"处理异常：{exc}"]})
            report["added"] += added
            report["merged_versions"] += mm
            report["skipped_dup"] += skipped
            report["failed"] += failed
            report["parse_failed"] += pf
            db.update_email(conn, mid, processed=1, processed_at=db.now(),
                            added=added, skipped=skipped, failed=failed)
        return report
    finally:
        conn.close()


def ingest_dir(folder: str, jd: dict, tiers: dict, db_path: str,
               cfg: dict | None = None, channel: str = "文件夹",
               job_id: int | None = None, use_llm: bool = False,
               llm_conf: dict | None = None) -> dict:
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
    try:
        for path in files:
            base = os.path.basename(path)
            try:
                size = os.path.getsize(path)
            except OSError as exc:
                report["failed"] += 1
                report["details"].append({"file": base, "status": "failed",
                                          "notes": [f"读取文件失败：{exc}"]})
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
                continue
            try:
                r = ingest_one(conn, cfg, jd, tiers,
                               filename=base, data=None, local_path=path,
                               channel=channel, applied_at=db.now(), source_message_id=None,
                               job_id=job_id, use_llm=use_llm, llm_conf=llm_conf)
                report[{"added": "added", "merged_version": "merged_versions",
                        "skipped_dup": "skipped_dup"}.get(r["status"], "failed")] += 1
                if any("待人工判读" in n for n in r["notes"]):
                    report["parse_failed"] += 1
                report["details"].append(r)
            except Exception as exc:
                report["failed"] += 1
                report["details"].append({"file": base, "status": "failed",
                                          "notes": [f"处理异常：{exc}"]})
        return report
    finally:
        conn.close()


def sync_mailbox(jd: dict, tiers: dict, db_path: str, job_id: int | None = None,
                 use_llm: bool = False, llm_conf: dict | None = None,
                 cfg: dict | None = None) -> dict:
    """拉取邮箱 → 入库。统一的"收简历"动作。"""
    cfg = cfg or mb.load_config()
    if cfg.get("mode") == "off":
        return {"mode": "off", "mails": 0, "added": 0,
                "message": "邮箱抓取已在 config/mailbox.json 中关闭"}
    conn = db.connect(db_path)
    cursor = db.email_cursor(conn, (cfg.get("imap") or {}).get("folder", "INBOX"))
    conn.close()
    try:
        mails, new_cursor = mb.incoming(cfg, cursor)
    except Exception as exc:
        return {"mode": cfg.get("mode"), "mails": 0, "added": 0, "error": str(exc)}
    report = ingest_mails(mails, cfg, jd, tiers, db_path, job_id, use_llm, llm_conf)
    report["mode"] = cfg.get("mode")
    if cfg.get("mode") == "imap" and new_cursor:
        conn = db.connect(db_path)
        db.set_email_cursor(conn, (cfg.get("imap") or {}).get("folder", "INBOX"), new_cursor)
        conn.close()
    return report
