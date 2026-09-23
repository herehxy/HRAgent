#!/usr/bin/env python3
"""企业人才库智能体 · 命令行入口。

常用：

    python cli.py init                    # 初始化数据库、默认部门与岗位、HR 账号
    python cli.py fixtures                # 生成 8 封测试邮件（离线演练用）
    python cli.py mail                    # 收取邮箱简历并入库（三层去重）
    python cli.py ingest --folder <dir>   # 导入手工文件夹中的简历
    python cli.py index                   # 建立/刷新向量索引
    python cli.py search 真空熔铸,钛合金   # 技能召回
    python cli.py search --semantic "有难熔合金背景的博士"
    python cli.py report                  # 人才库文字报表
    python cli.py audit                   # 最近操作审计
    python cli.py doctor                  # 环境自检（模型/检索/解析/邮箱/权限）
    python cli.py serve --port 8756       # 启动 Web 工作台
    python cli.py selftest                # 端到端自检（含断言）

生产环境启用真实登录：`TP_AUTH=1 python cli.py serve`
"""
from __future__ import annotations

import argparse
import json
import os
import sys

BASE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE)

from app import auth, db, ingest, search  # noqa: E402

DB_PATH = os.environ.get("TP_DB_PATH") or os.path.join(BASE, "data", "workbench.db")
JD_PATH = os.path.join(BASE, "config", "jd.json")
TIERS_PATH = os.path.join(BASE, "config", "tiers.json")
RESUME_DIR = os.path.join(BASE, "data", "resumes")


def _load(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _jd_tiers() -> tuple[dict, dict]:
    return _load(JD_PATH), _load(TIERS_PATH)


def _llm_conf(use: bool) -> dict | None:
    if not use:
        return None
    from app.agent import llm

    c = llm.load_cfg()
    return {"api_key": c.get("api_key"), "base_url": c.get("base_url"), "model": c.get("model")}


def _report_migration(conn) -> None:
    rep = db.migration_report(conn)
    if rep:
        print(f"[i] 检测到 v0.1 数据，已自动迁移："
              f"人档 {rep['candidates']}，投递 {rep['applications']}，"
              f"附件 {rep['documents']}，审计 {rep['audit']}")


# ============================================================

def cmd_init(args: argparse.Namespace) -> int:
    """只做三件事：建表、写默认设置、建 HR 账号。**不造任何岗位、不造任何部门。**

    为什么不顺便种一个"默认岗位"（v1.8 改）：岗位是业务数据，本该由 HR 在界面上按需录入。
    种一个出来会带来两个问题——① 它带着 `config/jd.json` 里那个写死的材料类尺子，
       看起来像"系统预置了一个岗位"，实际没人要过；② 它一旦被停用，
       `/api/meta` 上的旧种子逻辑会把它**复活**，等于系统悄悄撤销人的决定。
    现在空库就是空库，界面上「岗位管理」显示"暂无岗位"，加一个就有一个。
    """
    conn = db.connect(DB_PATH)
    try:
        _report_migration(conn)
        created = auth.ensure_seed_users(conn)
        print(f"[√] 数据库就绪：{DB_PATH}")
        if created:
            print(f"[√] 已创建 HR 账号：{', '.join(created)}"
                  f"（初始口令 change-me，请立即修改；本机单机模式默认免登录）")
        n_jobs = len(db.list_jobs(conn, include_inactive=True))
        print(f"[√] 当前岗位：{n_jobs} 个"
              + ("（空库——在界面「岗位管理」里填岗位名 + JD 即可，不需要建部门）"
                 if not n_jobs else ""))
        print(f"[√] 当前人才库：{json.dumps(db.pool_stats(conn)['tiers'], ensure_ascii=False)}")
    finally:
        conn.close()
    print("[i] 下一步：python cli.py serve，然后在界面「岗位管理」新增岗位")
    return 0


def cmd_fixtures(args: argparse.Namespace) -> int:
    from tests import make_fixtures

    return make_fixtures.main()


def cmd_mail(args: argparse.Namespace) -> int:
    jd, tiers = _jd_tiers()
    conn = db.connect(DB_PATH)
    try:
        _report_migration(conn)
    finally:
        conn.close()
    # 不在这里 upsert 一个"默认岗位"：邮箱路径的归岗由**邮件标题**决定
    # （`ingest.sync_mailbox` 内部完成），传一个默认 job_id 会让标题里没有岗位名的
    # 简历被硬挂到一个默认岗位上，掩盖"待指定"这个真实状态。
    r = ingest.sync_mailbox(jd, tiers, DB_PATH, job_id=None,
                            use_llm=args.llm, llm_conf=_llm_conf(args.llm))
    if r.get("error"):
        print(f"[!] 收取失败：{r['error']}")
        return 1
    print(f"[√] 邮箱模式 {r.get('mode')}：扫描邮件 {r.get('mails', 0)} 封，附件 {r.get('attachments', 0)} 份")
    print(f"    新增 {r.get('added', 0)}｜新版本归档 {r.get('merged_versions', 0)}"
          f"｜跳过重复 {r.get('skipped_dup', 0)}｜无附件 {r.get('no_attachment', 0)}"
          f"｜解析失败仍入库 {r.get('parse_failed', 0)}｜异常 {r.get('failed', 0)}")
    for d in r.get("details", []):
        note = "; ".join(d.get("notes") or []) or "—"
        print(f"    [{d['status']:>15}] {str(d.get('file'))[:34]:<34} "
              f"{str(d.get('name') or '未识别'):<8} {str(d.get('tier') or '-'):<3} {note}")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    jd, tiers = _jd_tiers()
    conn = db.connect(DB_PATH)
    try:
        _report_migration(conn)
    finally:
        conn.close()
    # **不在这里 upsert 一个"默认岗位"**（v1.8 改，与 cmd_mail 同一处口径）：
    # 文件夹导入是"混合批次"，本来就该落「所属岗位待指定」，由 HR 事后指定。
    # 更关键的是——`config/jd.json` 里那个角色名会被 upsert 成**一个真实岗位行**，
    # 于是"只想要 4 个岗位"的库里凭空多出第 5 个（实测踩到：导入 9 份样例后
    # 「工艺工程师（钛合金 / 难熔合金方向）」自己冒了出来）。
    # 岗位是业务数据，只能由人建；导入流程不该替人建岗位。
    r = ingest.ingest_dir(args.folder, jd, tiers, DB_PATH, job_id=None,
                          use_llm=args.llm, llm_conf=_llm_conf(args.llm))
    print(f"[√] 扫描 {r['scanned']}｜新增 {r['added']}｜新版本 {r['merged_versions']}"
          f"｜跳过重复 {r['skipped_dup']}｜解析失败仍入库 {r['parse_failed']}｜异常 {r['failed']}")
    return 0


def cmd_index(args: argparse.Namespace) -> int:
    r = search.build_index(DB_PATH, force=args.force)
    print(f"[√] 索引完成：新建 {r['indexed']}，跳过 {r['skipped']}，"
          f"模型 {r.get('model') or '—'}，维度 {r.get('dim') or 0}")
    if r.get("error"):
        print(f"[!] {r['error']}")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    conn = db.connect(DB_PATH)
    try:
        if args.semantic:
            r = search.semantic(conn, args.semantic, top_k=args.top)
            print(f"[√] 语义检索（索引模型 {r.get('model') or '未建立'}）")
            if r.get("note"):
                print(f"[i] {r['note']}")
            for x in r.get("results", []):
                print(f"    {x['score']:.4f}  {x.get('name') or '未识别':<8} "
                      f"{x.get('education') or '—':<4} {x.get('years') if x.get('years') is not None else '—'}年  "
                      f"档 {x.get('tier') or '—'}")
            return 0
        words = [w.strip() for w in (args.skills or "").replace("、", ",").split(",") if w.strip()]
        if not words:
            print("[!] 请给出技能名，例如：python cli.py search 真空熔铸,钛合金")
            return 1
        r = search.by_skills(conn, words, mode=args.mode)
        print(f"[√] 技能召回：{'、'.join(r['resolved'])}（原词：{'、'.join(words)}，模式 {args.mode}）")
        if r.get("unknown_terms"):
            print(f"[i] 本体外词（按字面匹配）：{'、'.join(r['unknown_terms'])}")
        print(f"    命中 {r['count']} 人")
        for x in r["results"]:
            print(f"    {x.get('name') or '未识别':<8} #{x['candidate_id']:<4} "
                  f"{x.get('education') or '—':<4} {x.get('years') if x.get('years') is not None else '—'}年  "
                  f"档 {x.get('tier') or '—':<2} 匹配 {'、'.join(x['matched_skills'])}")
            for sk, ev in (x.get("evidence") or {}).items():
                print(f"        证据[{sk}]：{ev}")
        return 0
    finally:
        conn.close()


def cmd_report(args: argparse.Namespace) -> int:
    conn = db.connect(DB_PATH)
    try:
        s = db.pool_stats(conn)
        p = db.pipeline_stats(conn)
        print("=" * 68)
        print("人才库总览")
        print("=" * 68)
        print(f"候选人数 {s['people']}｜投递 {s['applications']}｜简历附件 {s['documents']}"
              f"｜已合并档 {s['merged']}")
        print(f"档位分布 {json.dumps(s['tiers'], ensure_ascii=False)}")
        print(f"待 HR 确认 {s['pending']}｜待人工判读 {s['needs_review']}｜在池 {s['in_pool']}")
        print(f"收信 {s['emails']} 封（待处理 {s['emails_pending']}）｜别名 {s['skills']} → 本体条目")
        print(f"待确认提案 {s['proposals_pending']}｜向量索引 {s['embeddings']} 条")
        print("-" * 68)
        print("招聘管道")
        for st, v in (p.get("stages") or {}).items():
            if v["count"]:
                flag = f"（超期 {v['overdue']}）" if v["overdue"] else ""
                print(f"  {st:<8} {v['count']:>4} 条{flag}")
        print(f"  来源分布：{json.dumps(p.get('channels', {}), ensure_ascii=False)}")
        print("-" * 68)
        print("候选人明细")
        for c in db.list_candidates(conn):
            print(f"  #{c['id']:<3} {str(c.get('name') or '未识别'):<8} "
                  f"{str(c.get('edu_level') or '—'):<4} "
                  f"{str(c.get('years_exp') if c.get('years_exp') is not None else '—'):>3}年  "
                  f"档 {str(c.get('tier_effective') or '—'):<2} "
                  f"分 {str(c.get('score') if c.get('score') is not None else '—'):<5} "
                  f"阶段 {str(c.get('stage') or '—'):<6} "
                  f"{'待确认' if (c.get('app_status') or '') != '已确认' else '已确认'}"
                  f"{'  待人工判读' if c.get('needs_review') else ''}")
        runs = db.agent_cost_summary(conn)
        print("-" * 68)
        print(f"智能体累计运行 {runs['runs']} 次｜输入 token {runs['tokens_in']}"
              f"｜输出 token {runs['tokens_out']}｜平均 {runs['avg_latency_ms']} ms")
        return 0
    finally:
        conn.close()


def cmd_audit(args: argparse.Namespace) -> int:
    conn = db.connect(DB_PATH)
    try:
        rows = db.list_audit(conn, limit=args.limit)
        print(f"[√] 最近 {len(rows)} 条审计记录")
        for r in rows:
            print(f"  {r['ts']}  {r['entity']}#{r['entity_id']:<5} {r['action']:<18} "
                  f"{str(r['before'])[:24]:<24} → {str(r['after'])[:48]}  "
                  f"[{r['operator']}/{r['role']}]")
        return 0
    finally:
        conn.close()


def cmd_doctor(args: argparse.Namespace) -> int:
    from app import mailbox as mb
    from app.agent import llm
    from app.pipeline import normalize as nz
    from app.pipeline import parse as parse_mod

    print("=" * 68)
    print("环境自检")
    print("=" * 68)

    st = llm.status()
    print(f"[对话模型] {st['model']} @ {st['base_url']}")
    print(f"           服务可达 {st['reachable']}｜模型就绪 {st['model_installed']}")
    if st["error"]:
        print(f"           [!] {st['error']}")
        print("           启用：ollama pull qwen2.5:14b，或在 config/model.json / "
              "config/secrets.json 指向内网 vLLM / DeepSeek")
    else:
        print("           [√] 智能体对话功能可用")

    se = search.status(DB_PATH)
    print(f"[向量检索] 配置模型 {se['model']} @ {se['base_url']}")
    used = se.get("index_model") or "尚未建立索引"
    print(f"           索引实际使用 {used}（{se['dim']} 维）｜已索引 {se['indexed']}/{se.get('people', 0)} 人"
          f"（覆盖 {int((se.get('coverage') or 0) * 100)}%）")
    if se.get("error"):
        print(f"           [!] {se['error']}")

    caps = parse_mod.engine_capabilities()
    print(f"[解析能力] PyMuPDF {caps['pymupdf']}｜MarkItDown {caps['markitdown']}｜OCR {caps['ocr']}")
    print(f"           {caps['archive_note']}")

    mcfg = mb.load_config()
    icfg = mcfg.get("imap") or {}
    print(f"[邮箱接入] 模式 {mcfg.get('mode')}｜只读 {icfg.get('readonly', True)}"
          f"｜附件白名单 {' '.join(mcfg.get('attachment_ext', []))}")
    if mcfg.get("mode") == "imap":
        print(f"           服务器 {icfg.get('host') or '（未配置）'}:{icfg.get('port', 993)}"
              f"｜账号 {icfg.get('user') or '（未配置）'}｜收件夹 {icfg.get('folder', 'INBOX')}")
        print(f"           口令 {'已设置（不回显）' if mb.read_secret() else '未设置'}"
              f"｜配置文件 {mb.CONFIG_PATH}")
        if not icfg.get("host"):
            print("           [!] 未配置 imap.host：请在界面「邮箱配置」里填写并测试连接")
    if mcfg.get("mode") == "eml":
        print(f"           邮件目录：{mb.resolve_dir(mcfg.get('eml_dir', ''))}")
    if mcfg.get("mode") == "off":
        print("           [i] 收信已关闭：需要收简历时在界面「邮箱配置」里改回 imap 或 eml")

    onto = nz.describe()
    print(f"[技能本体] 条目 {onto['canonical_count']}｜可匹配写法 {onto['alias_count']}"
          f"｜{json.dumps(onto['categories'], ensure_ascii=False)}")

    from app import crypto

    pii = crypto.protection()
    print(f"[信息保护] {pii['algorithm']}｜密钥来源 {pii['key_source']}"
          f"｜{'[!] 降级中，未加密' if pii['degraded'] else '[√] 正常'}")

    print(f"[访问模式] {'登录模式（TP_AUTH=1）' if os.environ.get('TP_AUTH') else '本机模式（单 HR，免登录）'}"
          f"｜角色 {','.join(auth.ROLES)}（唯一角色，不分子集权限）")
    print("=" * 68)
    print("下一步：python cli.py fixtures && python cli.py mail && python cli.py index")
    return 0


def cmd_purge(args: argparse.Namespace) -> int:
    """把归档满 30 天的档案彻底删除（服务启动时也会自动跑一次）。"""
    from app import db
    from app.server import _purge_expired

    out = _purge_expired(actor="cli", role="hr")
    if out.get("disabled"):
        print("[i] 自动清理已关闭（PURGE_AFTER_DAYS <= 0）")
        return 0
    print(f"[√] 归档满 {out['days']} 天（即 {out['cutoff']} 之前归档）的档案："
          f"彻底删除 {out['purged']} 人")
    for p in out.get("people") or []:
        print(f"    - {p['name']}（{p['archived_at']} 归档，"
              f"{p['applications']} 条投递 / {p['documents']} 份附件）")
    print(f"    原件移入回收目录 {out['files_moved']} 份"
          + (f"，未找到 {len(out['files_missing'])} 份" if out.get("files_missing") else ""))
    conn = db.connect(os.environ.get("TP_DB_PATH") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "data", "workbench.db"))
    try:
        meta = db.archive_meta(conn)
        soon = [(k, v) for k, v in meta.items() if (v.get("days_left") or 99) <= 7]
        if soon:
            print(f"[i] 还有 {len(soon)} 人将在 7 天内到期（归档页可查看剩余天数）")
    finally:
        conn.close()


def cmd_purge_chats(args: argparse.Namespace) -> int:  # noqa: ARG001
    """把**清空满 30 天**的对话运行记录彻底删除（服务启动时也会自动跑一次）。

    与 `purge`（归档满 30 天的档案）对称：两者共用同一套 `PURGE_AFTER_DAYS` 保留期。
    注意语义——清空当天只推进清空点、**不删数据**，所以这 30 天里 HR 还能点「恢复对话」；
    到期才由这里落最后一刀。删除前会先把条数与 token 合计写进 `chat_purge` 审计。
    """
    from app import db
    from app.server import _purge_chats

    out = _purge_chats(actor="cli", role="hr")
    if out.get("disabled"):
        print("[i] 自动清理已关闭（PURGE_AFTER_DAYS <= 0）")
        return 0
    if not out.get("purged"):
        print(f"[i] 暂无到期可清理的对话：{out.get('reason') or '没有清空记录'}")
        return 0
    print(f"[√] 清空满 {out['days']} 天的对话：删除 {out['purged']} 条运行记录"
          f"（合计 tokens {out.get('tokens', 0)}）")
    print(f"    {out['cleared_at']} 清空 → {out['purge_after']} 到期；"
          f"汇总已写入「提案与审计」（action=chat_purge），逐条明细不再保留")
    return 0


def cmd_sync_ontology(args: argparse.Namespace) -> int:  # noqa: ARG001
    """按技能本体刷新库内技能分类（本体新增技能后跑；服务启动时也会自动跑一次）。

    用途：专业大类匹配依赖 `skills.category`，而 `upsert_skill` 不会改写已存在技能的分类，
    所以本体升级（如补入 Java/Spring Boot 等软件开发技能）后需要显式同步一次。
    """
    from app import db
    from app.pipeline import normalize as nz

    conn = db.connect(os.environ.get("TP_DB_PATH") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "data", "workbench.db"))
    try:
        r = db.sync_skill_categories(conn, nz.ontology_categories())
    finally:
        conn.close()
    print(f"[√] 技能分类同步完成：库内 {r['total']} 条，更新 {r['changed']} 条")
    if r.get("not_in_ontology_count"):
        print(f"[i] {r['not_in_ontology_count']} 条技能不在本体里（多由岗位 JD 词表带入），"
              f"已保留并归入「其他」：{'、'.join(r['not_in_ontology'])}"
              + ("…" if r["not_in_ontology_count"] > len(r["not_in_ontology"]) else ""))
    return 0


def cmd_refresh_skills(args: argparse.Namespace) -> int:  # noqa: ARG001
    """按「对应岗位」刷新技能清单。只修技能数据，不动档位与分数（v1.6）。

    用途：技能按「本体 + 岗位 JD 词表」抽取，早期已归岗的投递没跟上刷新，
    会出现"命中里有 Java、技能栏里没有"的自相矛盾（缺陷 #47）。
    """
    from app import db, regrade

    conn = db.connect(os.environ.get("TP_DB_PATH") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "data", "workbench.db"))
    try:
        r = regrade.refresh_skills(conn, operator="cli", role="hr")
    finally:
        conn.close()
    print(f"[√] 已按「对应岗位」刷新 {r['refreshed']} 条投递的技能清单"
          f"（写入 {r['skills_written']} 项技能；档位与分数未改动）")
    for s in r.get("skipped") or []:
        print(f"    - 跳过投递 #{s['application_id']}：{s['why']}")
    return 0
    return 0


def cmd_majors(args: argparse.Namespace) -> int:
    """查看通用学科目录（专业需求匹配的底座）。"""
    from app.pipeline import majors as mj

    info = mj.describe()
    if args.q:
        rows = mj.search(args.q, limit=args.limit)
        print(f"[学科目录 v{info['version']}] 命中 {len(rows)} 条：")
        for r in rows:
            label = mj.family_label(r["name"])
            print(f"  {r['category']:6s} {r['name']}"
                  + ("" if label == r["name"] else f"  → {label}"))
        return 0
    print(f"[学科目录 v{info['version']}] 一级学科 {info['major_count']} 个、"
          f"可匹配写法 {info['matchable_count']} 条")
    for cat, n in sorted(info["by_category"].items(), key=lambda kv: -kv[1]):
        print(f"  {cat:6s} {n} 个")
    print("\n用 `cli.py majors -q 材料` 查具体学科；用 `cli.py domains` 看领域包。")
    return 0


def cmd_domains(args: argparse.Namespace) -> int:
    """列出可用领域包（新增行业用数据导入，不用改代码）。"""
    from app.pipeline import domains as dom

    packs = dom.list_packs()
    if not packs:
        print(f"[i] 还没有领域包。在 {dom.pack_dir()} 下放一个 JSON 即可。")
        return 0
    print(f"[领域包] 共 {len(packs)} 个（目录 {dom.pack_dir()}）")
    for p in packs:
        if p.get("error"):
            print(f"  ✗ {p['file']}：{p['error']}")
            continue
        print(f"  · {p['name']}  v{p.get('version')}  {p['skill_count']} 条技能"
              f"  大类 {('、'.join(p['categories']))}"
              + (f"  ⚠ 新增大类 {('、'.join(p['new_categories']))}" if p.get("new_categories") else ""))
        if p.get("description"):
            print(f"      {p['description']}")
    print("\n导入：`cli.py import-domain <名称|文件>`；先看会改什么：加 `--dry-run`。")
    return 0


def cmd_import_domain(args: argparse.Namespace) -> int:
    """导入领域包：合并进技能本体 → 刷新库内分类 → 写审计。

    先 `--dry-run` 预演：它会告诉你新增几条、合并几条、哪些别名会被挪到别的条目名下。
    别名迁移必须报告——`成本核算` 这种词挪了归属，会直接改变方向判定的口径。
    """
    from app import db
    from app.pipeline import domains as dom

    if args.dry_run:
        r = dom.preview_import(args.pack)
        print(f"[预演] 领域包「{r['pack']}」（{r['file']}）")
        print(f"  新增技能 {len(r['skills_added'])} 条、合并 {len(r['skills_merged'])} 条、"
              f"别名迁移 {len(r['aliases_moved'])} 条")
        if r["skills_added"]:
            print(f"  新增：{'、'.join(r['skills_added'])}")
        if r["aliases_moved"]:
            for m in r["aliases_moved"]:
                print(f"  ⚠ 别名「{m['alias']}」：{m['from']} → {m['to']}")
        if r["categories_added"]:
            print(f"  ⚠ 新增技能大类：{'、'.join(r['categories_added'])}")
        print(f"  本体版本：{r['version_before']} → {r['version_after']}（预演不改文件）")
        return 0

    conn = db.connect(os.environ.get("TP_DB_PATH") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "data", "workbench.db"))
    try:
        r = dom.import_pack(conn, args.pack, operator=args.operator)
    finally:
        conn.close()
    print(f"[√] 已导入领域包「{r['pack']}」：新增 {len(r['skills_added'])} 条、"
          f"合并 {len(r['skills_merged'])} 条、别名迁移 {len(r['aliases_moved'])} 条")
    if r["aliases_moved"]:
        for m in r["aliases_moved"]:
            print(f"    ⚠ 别名「{m['alias']}」：{m['from']} → {m['to']}")
    if r["major_added"]:
        print(f"    补充学科目录：{'、'.join(r['major_added'])}")
    s = r.get("synced") or {}
    print(f"[√] 技能分类已刷新：库内 {s.get('total')} 条、更新 {s.get('changed')} 条")
    oa = r.get("ontology_after") or {}
    print(f"[i] 本体现为 v{oa.get('version')}：{oa.get('canonical_count')} 条技能、"
          f"{oa.get('alias_count')} 条可匹配写法")
    print(f"[i] 导入前备份：{r.get('backup')}")
    if s.get("not_in_ontology_count"):
        print(f"[i] {s['not_in_ontology_count']} 条技能不在本体里（多由岗位 JD 词表带入），保留归「其他」")
    print("[i] 下一步：`cli.py sync-ontology` 可选（本次已自动刷新）；"
          "已有投递要按新口径重算请走界面的「重新分析」。")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    print(f"[√] 工作台已启动：http://127.0.0.1:{args.port}  （Ctrl+C 停止）")
    print(f"    访问模式：{'登录（TP_AUTH=1）' if os.environ.get('TP_AUTH') else '本机（角色可切换）'}")
    uvicorn.run("app.server:app", host=args.host, port=args.port, log_level="warning")
    return 0


def cmd_selftest(args: argparse.Namespace) -> int:
    from tests import selftest

    return selftest.main(verbose=not args.quiet)


def main() -> int:
    ap = argparse.ArgumentParser(description="企业人才库智能体")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("init", help="初始化数据库、默认部门与岗位、HR 账号")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("fixtures", help="生成测试邮件（.eml）")
    p.set_defaults(func=cmd_fixtures)

    p = sub.add_parser("mail", help="收取邮箱简历并入库")
    p.add_argument("--llm", action="store_true", help="启用模型通道抽取（需已配置模型）")
    p.set_defaults(func=cmd_mail)

    p = sub.add_parser("ingest", help="导入手工文件夹中的简历")
    p.add_argument("--folder", default=RESUME_DIR)
    p.add_argument("--llm", action="store_true")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("index", help="建立/刷新向量索引")
    p.add_argument("--force", action="store_true", help="忽略缓存，全量重建")
    p.set_defaults(func=cmd_index)

    p = sub.add_parser("search", help="检索人才（技能召回 / 语义召回）")
    p.add_argument("skills", nargs="?", help="技能名，逗号分隔，如：真空熔铸,钛合金")
    p.add_argument("--semantic", help="改用语义检索，提供自然语言语句")
    p.add_argument("--mode", default="all", choices=["all", "any"], help="技能匹配口径")
    p.add_argument("--top", type=int, default=8)
    p.set_defaults(func=cmd_search)

    p = sub.add_parser("report", help="打印人才库文字报表")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("audit", help="查看最近操作审计")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=cmd_audit)

    p = sub.add_parser("doctor", help="环境自检")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("serve", help="启动 Web 工作台")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8756)
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("purge", help="彻底删除归档已满 30 天的档案（原件移入回收目录）")
    p.set_defaults(func=cmd_purge)
    p = sub.add_parser("purge-chats",
                       help="彻底删除清空已满 30 天的对话运行记录（与 purge 同口径）")
    p.set_defaults(func=cmd_purge_chats)

    p = sub.add_parser("sync-ontology", help="按技能本体刷新库内技能分类（本体升级后跑）")
    p.set_defaults(func=cmd_sync_ontology)

    p = sub.add_parser("refresh-skills",
                       help="按「对应岗位」刷新技能清单（修数据，不改档位与分数）")
    p.set_defaults(func=cmd_refresh_skills)

    p = sub.add_parser("majors", help="查看通用学科目录（专业需求匹配的底座）")
    p.add_argument("-q", "--q", default=None, help="关键词过滤，如「材料」")
    p.add_argument("--limit", type=int, default=40)
    p.set_defaults(func=cmd_majors)

    p = sub.add_parser("domains", help="列出可用领域包（新增行业用数据导入，不改代码）")
    p.set_defaults(func=cmd_domains)

    p = sub.add_parser("import-domain", help="导入领域包（合并技能本体 + 刷新分类 + 写审计）")
    p.add_argument("pack", help="领域包名称或文件路径，如 财务 / config/domains/财务.json")
    p.add_argument("--dry-run", action="store_true", help="只预演：报告会改什么，不落盘")
    p.add_argument("--operator", default="cli", help="审计里的操作人")
    p.set_defaults(func=cmd_import_domain)

    p = sub.add_parser("selftest", help="端到端自检（含断言）")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_selftest)

    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
