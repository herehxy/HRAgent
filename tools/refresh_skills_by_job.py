"""按候选人**当前对应岗位**重抽技能清单（修数据，不改结论）。

## 为什么需要这个脚本

技能行的词表就是岗位 JD 的技能清单（`extract._jd_terms`）：JD 写了的词，
简历原文里找得到片段 → `verified=1`（候选人具备）；找不到 → `verified=0`（不具备）。

`cli.py ingest` 是**先抽取、后归岗**的：抽取那一刻还没有岗位，用的是
`config/jd.json` 那把默认尺子（材料类），于是每个人的技能表里都留了一组
默认尺子的词。等系统把人路由到「数字化工程师」之后，这组材料类词就成了
无主残留——界面上会显示成"这个人未验证的技能：增材制造、热加工、真空熔铸"，
看起来像简历里真写过，其实只是旧尺子的需求行。

本脚本把每个人的技能表按**当前对应岗位**重抽一遍，让 `verified=0` 恢复成
"当前岗位要求、但简历里没有证据"这一本来含义。

## 纪律

1. **只重写技能关联表**（`candidate_skills`），不碰 `candidates` 的基础字段，
   也不碰 `applications` 的档位/分数——重算分数是 `regrade` 的职责，两件事分开。
2. 找不到原文（扫描件解析失败等）时**跳过该人**并如实报告，不清空他的技能表——
   宁可留着旧数据，也不要把一个人的能力档案洗成空白。
3. 逐人留一条审计，记录技能条目数变化与新增/移除的词。

用法：
    python tools/refresh_skills_by_job.py            # 预演，只报告不写
    python tools/refresh_skills_by_job.py --apply    # 落库
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db                                    # noqa: E402
from app.ingest import persist_skills                  # noqa: E402
from app.pipeline.extract import extract               # noqa: E402

DB_PATH = "data/workbench.db"


def _resume_text(conn: sqlite3.Connection, detail: dict) -> tuple[int | None, str | None]:
    """取该候选人的简历原文，优先用投递指向的附件，其次取最新一份有文本的。"""
    for a in (detail.get("applications") or []):
        did = a.get("resume_doc_id")
        if not did:
            continue
        row = conn.execute("SELECT id, raw_text FROM documents WHERE id = ?", (did,)).fetchone()
        if row and (row["raw_text"] or "").strip():
            return row["id"], row["raw_text"]
    row = conn.execute(
        "SELECT id, raw_text FROM documents WHERE candidate_id = ? "
        "AND raw_text IS NOT NULL AND TRIM(raw_text) <> '' "
        "ORDER BY id DESC LIMIT 1", (detail["id"],)).fetchone()
    if row:
        return row["id"], row["raw_text"]
    return None, None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="真正写回库（默认只预演）")
    ap.add_argument("--db", default=DB_PATH)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    cands = conn.execute("SELECT id, name FROM candidates ORDER BY id").fetchall()
    changed = skipped = unchanged = 0
    report: list[dict] = []

    for c in cands:
        cid = c["id"]
        detail = db.candidate_detail(conn, cid) or {}
        jd, job_meta = db.resolve_candidate_job(conn, detail)
        # 无对应岗位时**不跳过**：空 JD 让抽取只走本体词表，正好把旧尺子的残留行清掉，
        # 而本体扫描出的 verified 项会原样保留——不会把档案洗成空白。
        no_job = not jd
        if no_job:
            jd = {}
        doc_id, text = _resume_text(conn, detail)
        if not text:
            skipped += 1
            report.append({"id": cid, "name": c["name"],
                           "status": "跳过：取不到简历原文（保留原技能表）"})
            continue

        fresh = extract(text, jd, use_llm=False)

        before = {r["name"]: r["verified"]
                  for r in db.candidate_skill_rows(conn, cid, verified_only=False)}
        after = {d["canonical"]: int(bool(d.get("verified")))
                 for d in (fresh.get("skill_detail") or []) if d.get("canonical")}

        added = sorted(set(after) - set(before))
        removed = sorted(set(before) - set(after))
        flipped = sorted(k for k in (set(after) & set(before)) if after[k] != before[k])

        if not (added or removed or flipped):
            unchanged += 1
            report.append({"id": cid, "name": c["name"], "status": "无变化",
                           "job": job_meta.get("title") or ("仅本体词表" if no_job else ""),
                           "verified": sum(1 for v in after.values() if v)})
            continue

        changed += 1
        report.append({"id": cid, "name": c["name"],
                       "status": "重抽(无岗位：仅本体词表)" if no_job else "重抽",
                       "job": job_meta.get("title") or ("仅本体词表" if no_job else ""),
                       "verified": sum(1 for v in after.values() if v),
                       "added": added, "removed": removed, "flipped": flipped})

        if args.apply:
            conn.execute("DELETE FROM candidate_skills WHERE candidate_id = ?", (cid,))
            conn.commit()
            persist_skills(conn, cid, fresh, doc_id)
            db.add_audit(
                conn, entity="candidate", entity_id=str(cid),
                action="refresh_skills_by_job",
                before=json.dumps(before, ensure_ascii=False),
                after=json.dumps({"job": job_meta.get("title") or ("(无对应岗位)" if no_job else ""),
                                  "added": added, "removed": removed, "flipped": flipped,
                                  "verified": sum(1 for v in after.values() if v)},
                                 ensure_ascii=False),
                operator="hr", role="hr")

    print(f"模式：{'落库' if args.apply else '预演（未写库）'}")
    for r in report:
        head = f"#{r['id']:<2} {r['name']:<4} [{r['status']}]"
        if not r["status"].startswith("重抽"):
            print(head + (f" 岗位={r.get('job')}" if r.get("job") else ""))
            continue
        print(f"{head} 岗位={r['job']} 核实技能 {r['verified']} 项")
        if r.get("added"):
            print(f"        + 新增 {len(r['added'])}：{'、'.join(r['added'][:12])}")
        if r.get("removed"):
            print(f"        - 移除 {len(r['removed'])}：{'、'.join(r['removed'][:12])}")
        if r.get("flipped"):
            print(f"        ~ 有无证据翻转 {len(r['flipped'])}：{'、'.join(r['flipped'][:12])}")
    print(f"\n汇总：重抽 {changed} 人 / 无变化 {unchanged} 人 / 跳过 {skipped} 人")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
