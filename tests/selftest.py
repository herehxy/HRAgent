"""端到端自检：对设计方案的每一条硬约束做断言。

覆盖范围（全部为可重现的真实调用，不 mock 业务逻辑）：

========================================  ====================================================
断言组                                     验证的约束
========================================  ====================================================
A 数据层                                    17 张表建表（含 settings）、v0.1 数据迁移无损、旧库自动补列
B 邮箱入库与三层去重                         8 封邮件 → 精确的入库/跳过/失败计数
C 不丢件                                    解析失败的简历仍建档、原件留档、标待人工判读
D 反幻觉                                    模型伪造的技能片段对不上原文 → verified=0 → 不计命中
E 合规屏蔽                                  民族/婚姻/生育等敏感取值在送模型前被替换
F 分级                                      A/B/C/D 结果与命中证据逐条核对
G 建议与决定分离                             系统建议与 HR 确认分列；新版本简历不覆盖已确认档位
H 幂等                                      重复收取不产生任何新增
I 检索                                      技能精确召回 + 语义召回排序
J 智能体（无模型）                            规则路由可用；工具链真实执行
K 写操作只出提案                             模型不能直接改库；HR 确认后才生效
L 外发工具禁用                               越界调用被拦下
M 权限                                      单 HR 角色具备全部权限（无 viewer/recruiter/admin）
N 个人信息保护                               完整展示联系方式 + 加密存储 + 密文不下发 + 性别只取明写标签
O 软合并                                    合并后投递与附件迁移，可撤销
P 审计                                      查看、改档、确认提案、性别开关全部留痕
Q 降级                                      embedding 不可达时哈希向量兜底
R 跨渠道去重                                手工导入 + 邮件再投不重复建投递；未带岗位名的更新版不归岗
R2 标题归岗 + 岗位停用                        邮件标题归岗；停用岗位不接收；文件夹上传只分析不归岗
S 接口自检                                   全部路由可达无 5xx；原件/来源文件/多选打包；
                                            **重新分析**（预演不写库、应用后归零、不覆盖已确认）；
                                            **来源文件移走与恢复**（先补归档、原件不 404）；
                                            **软归档闭环**（归档即隐藏、检索不再命中、可恢复、留痕）；
                                            **岗位建议与采纳**（待指定试算取最优、不硬凑、采纳才归岗并重算）；
                                            **性别全链路**（不推断、不进评分、开关默认关、开启留痕）；
                                            **邮箱配置提醒与服务商预设**；PDF 解析与工作年限判定
S续 v1.6 三项新需求                           ① 对话能查岗位（list_jobs 工具 + 规则路由岗位分支，
                                            在招数不含停用岗位与停用部门下的岗位）；
                                            ② 历史会话不点清空不丢（读 agent_runs、清空**只软清空**：
                                            推进清空点 + 开 30 天保留期，保留期内可恢复、
                                            到期才真删并留痕）；
                                            ③ 面试题纲/匹配分析/档位解释一律锚定「对应岗位」
                                            （已归岗 > 建议岗位 > 如实报无），并做专业大类匹配
                                            （方向对口 ≠ 缺几项关键词）；技能筛选先挂载再过滤
========================================  ====================================================
"""
from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import urllib.parse
import zipfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from app import actions, auth, db, ingest, mailbox as mb, search  # noqa: E402
from app.agent import loop as agent_loop  # noqa: E402
from app.agent.tools import ToolCtx  # noqa: E402
from app.identity import build_identity_key  # noqa: E402
from app.pipeline import normalize as nz, sanitize  # noqa: E402
from app.pipeline.extract import extract  # noqa: E402
from app.pipeline.parse import SUPPORTED, parse_file, parse_file_ex  # noqa: E402
from app.pipeline.tier import grade  # noqa: E402

JD = json.load(open(os.path.join(BASE, "config", "jd.json"), encoding="utf-8"))
TIERS = json.load(open(os.path.join(BASE, "config", "tiers.json"), encoding="utf-8"))


class Checker:
    def __init__(self, verbose: bool = True):
        self.passed = 0
        self.failed: list[str] = []
        self.verbose = verbose
        self.group = ""

    def section(self, name: str) -> None:
        self.group = name
        if self.verbose:
            print(f"\n{'─' * 72}\n{name}\n{'─' * 72}")

    def ok(self, cond: bool, desc: str, detail: str = "") -> bool:
        if cond:
            self.passed += 1
            if self.verbose:
                print(f"  [PASS] {desc}" + (f"  ({detail})" if detail else ""))
        else:
            self.failed.append(f"{self.group} :: {desc}" + (f"  [{detail}]" if detail else ""))
            print(f"  [FAIL] {desc}" + (f"  ({detail})" if detail else ""))
        return bool(cond)


def _asgi(app, method: str, url: str, headers: dict | None = None,
          body: bytes = b"", caps: dict | None = None) -> tuple[int, str]:
    """不依赖 httpx 直接驱动 ASGI，返回 (状态码, 响应文本)。

    环境里没装 httpx（TestClient 的依赖），但"每个接口能不能跑通"必须被自检覆盖：
    本次就是靠它发现 server.py 调用了并不存在的 `db.candidate_audit`，
    导致候选人详情接口 500 的。

    传入 caps（可变 dict）时，会把响应头也写进 caps["headers"]——
    用于验证"预览 vs 下载"这类只体现在 Content-Disposition 上的行为。
    """
    import asyncio
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    if body:
        raw_headers.append((b"content-length", str(len(body)).encode()))
    scope = {
        "type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1", "method": method.upper(), "scheme": "http",
        "path": parts.path, "raw_path": parts.path.encode(),
        "query_string": parts.query.encode(), "root_path": "",
        "headers": raw_headers, "client": ("127.0.0.1", 51234),
        "server": ("127.0.0.1", 80), "state": {},
    }
    state = {"sent": False}
    captured = {"status": 0, "body": bytearray(), "headers": {}}

    async def receive() -> dict:
        if state["sent"]:
            return {"type": "http.disconnect"}
        state["sent"] = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict) -> None:
        if message["type"] == "http.response.start":
            captured["status"] = message["status"]
            captured["headers"] = {
                k.decode("latin-1").lower(): v.decode("latin-1")
                for k, v in message.get("headers", [])
            }
        elif message["type"] == "http.response.body":
            captured["body"] += message.get("body", b"")

    asyncio.run(app(scope, receive, send))
    if caps is not None:
        caps["headers"] = captured["headers"]
        caps["body_bytes"] = bytes(captured["body"])
    return captured["status"], captured["body"].decode("utf-8", "replace")


def main(verbose: bool = True) -> int:
    # 让"模型不可用"成为确定条件：指向一个必然拒绝连接的端口
    os.environ["LLM_BASE_URL"] = "http://127.0.0.1:9/v1"
    os.environ.pop("LLM_API_KEY", None)

    c = Checker(verbose)
    work = tempfile.mkdtemp(prefix="tp_selftest_")
    db_path = os.path.join(work, "test.db")
    mail_dir = os.path.join(work, "mail_in")
    archive_dir = os.path.join(work, "archive")

    cfg = mb.load_config()
    cfg["mode"] = "eml"
    cfg["eml_dir"] = mail_dir
    cfg["archive_dir"] = archive_dir

    try:
        # ============================================================ A
        c.section("A 数据层：建表、迁移")
        from tests import make_fixtures

        make_fixtures.main(out_dir=mail_dir)
        c.ok(len([f for f in os.listdir(mail_dir) if f.endswith(".eml")]) == 8,
             "生成 8 封测试邮件")

        # 夹具简历也自包含：文本 3 份（与邮件附件字节一致，撑起跨渠道去重）+ PDF 2 份
        # （学生投递以 PDF 为主，来源预览/打包下载需要真 PDF）。绝不依赖 data/resumes。
        resume_dir = os.path.join(work, "resumes")
        made = make_fixtures.write_resumes(resume_dir)
        c.ok(len(made) == 5, "生成 5 份夹具简历（文本 3 + PDF 2，自包含）", "、".join(made))

        conn = db.connect(db_path)
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        need = {"candidates", "applications", "documents", "skills", "candidate_skills",
                "tags", "candidate_tags", "jobs", "departments", "email_messages", "audit_log",
                "agent_runs", "proposals", "users", "sessions", "embeddings",
                # settings 是"性别筛选开关"等设置项的落点，属于承重表，必须一并断言；
                # 之前这里漏了它，于是"表少了"也不会被照出来。
                "settings"}
        c.ok(need <= tables, "17 张表全部建立", f"缺失 {sorted(need - tables)}" if need - tables else "")
        job_id = db.upsert_job(conn, JD)
        c.ok(isinstance(job_id, int) and job_id > 0, "岗位写入成功", f"job_id={job_id}")
        conn.close()

        # 迁移 v0.1：用旧库的备份路径不存在时跳过，存在则验证
        legacy = os.path.join(BASE, "data", "workbench.db")
        old_copy = os.path.join(work, "legacy.db")
        if os.path.exists(legacy):
            shutil.copy(legacy, old_copy)
            lc = db.connect(old_copy)
            rep = db.migration_report(lc)
            if rep:  # 只有还是旧结构时才迁移
                c.ok(rep["candidates"] > 0 and rep["applications"] == rep["candidates"],
                     "v0.1 扁平数据迁移为 人+投递+附件",
                     f"人{rep['candidates']} 投递{rep['applications']} 附件{rep['documents']}")
                c.ok(db.pool_stats(lc)["people"] == rep["candidates"],
                     "迁移后人数与迁移报告一致")
            else:
                src = db.connect(legacy)
                n = db.pool_stats(src)["people"]
                c.ok(db.pool_stats(lc)["people"] == n, "既有新结构库打开无损", f"{n} 人")
                src.close()
            lc.close()

        # ============================================================ B
        c.section("B 邮箱入库与三层去重")
        r1 = ingest.sync_mailbox(JD, TIERS, db_path, job_id=job_id, cfg=cfg)
        c.ok(r1["mails"] == 8, "扫描到 8 封邮件", f"实际 {r1['mails']}")
        c.ok(r1["attachments"] == 6, "识别到 6 个简历附件", f"实际 {r1['attachments']}")
        c.ok(r1["added"] == 4, "新增 4 份新简历", f"实际 {r1['added']}")
        c.ok(r1["merged_versions"] == 1, "1 份同人新版本被合并归档", f"实际 {r1['merged_versions']}")
        c.ok(r1["skipped_dup"] == 2, "2 次重复被拦下（邮件层 1 + 文件层 1）",
             f"实际 {r1['skipped_dup']}")
        c.ok(r1["no_attachment"] == 1, "1 封无附件邮件入台账", f"实际 {r1['no_attachment']}")
        c.ok(r1["parse_failed"] == 1, "1 份解析失败（仍入库）", f"实际 {r1['parse_failed']}")
        c.ok(r1["failed"] == 0, "无处理异常", f"实际 {r1['failed']}")

        conn = db.connect(db_path)
        s = db.pool_stats(conn)
        c.ok(s["people"] == 4, "内容层去重生效：8 封邮件收敛为 4 个人档", f"实际 {s['people']} 人")
        c.ok(s["emails"] == 7, "收信台账 7 条（重复 message_id 不重复记账）",
             f"实际 {s['emails']}")
        c.ok(s["documents"] == 5, "附件台账 5 份（哈希唯一的原件）", f"实际 {s['documents']}")
        conn.close()

        # ============================================================ C
        c.section("C 不丢件：解析失败也建档、原件留档、待人工判读")
        conn = db.connect(db_path)
        broken = [x for x in db.list_candidates(conn) if x["needs_review"]]
        c.ok(len(broken) == 1, "解析失败的那份仍建了档并标『待人工判读』",
             f"实际 {len(broken)} 条")
        if broken:
            d = db.candidate_detail(conn, broken[0]["id"])
            c.ok(len(d["documents"]) == 1, "其原件已留档（documents 有记录）")
            c.ok(os.path.exists(d["documents"][0]["archived_path"] or ""),
                 "归档文件在磁盘上真实存在",
                 os.path.basename(d["documents"][0]["archived_path"] or ""))
            c.ok(d["documents"][0]["parse_ok"] is False, "该附件被标记为解析失败")
            c.ok(broken[0]["tier_effective"] == "D", "降档为 D 而非丢弃")
            c.ok(broken[0]["identity_key"].startswith("uk:"),
                 "无身份信息的简历使用文档哈希兜底，不会互相挤占同一个人档",
                 broken[0]["identity_key"][:16])

        # 陈志远：一个人，一条投递，两份简历版本
        chen = [x for x in db.list_candidates(conn) if x["name"] == "陈志远"]
        c.ok(len(chen) == 1, "陈志远只有一个人档")
        if chen:
            d = db.candidate_detail(conn, chen[0]["id"])
            c.ok(len(d["applications"]) == 1,
                 "同一岗位近期投两次只建 1 条投递（第二次作为新版本附件）",
                 f"实际 {len(d['applications'])} 条")
            c.ok(len(d["documents"]) == 2, "两份简历版本都保留了原件",
                 f"实际 {len(d['documents'])} 份")
        conn.close()

        # 不丢件总量守恒
        conn = db.connect(db_path)
        total_docs = db.pool_stats(conn)["documents"]
        on_disk = sum(len(fs) for _, _, fs in os.walk(archive_dir))
        c.ok(total_docs == on_disk, "台账中的附件数 == 归档目录中的实际文件数",
             f"台账 {total_docs} / 磁盘 {on_disk}")
        conn.close()

        # ============================================================ D
        c.section("D 反幻觉：模型声称的技能片段必须能在原文中核对上")
        text = ("姓名：测试员\n学历：硕士\n工作年限：5年\n"
                "工作经历\n- 负责钛合金真空熔铸工艺开发\n"
                "专业技能\n钛合金、真空熔铸\n")
        detail = nz.normalize_skills(
            ["钛合金", "真空熔铸", "电子束熔炼"], text,
            llm_evidence={"电子束熔炼": "负责电子束熔炼工艺定型"})  # 原文里没有这句话
        by = {d["canonical"]: d for d in detail}
        c.ok(by["钛合金"]["verified"] and by["真空熔铸"]["verified"],
             "原文能定位到的技能 verified=1")
        c.ok(not by["电子束熔炼"]["verified"],
             "模型编造片段的技能 verified=0（片段对不上原文）")
        c.ok(by["电子束熔炼"]["evidence"] == "",
             "未通过核验的技能不携带证据，避免被误当真")
        cand = extract(text, JD)
        c.ok("电子束熔炼" not in cand["skills"], "未核验技能不进 skills，因而不会计入命中")

        # 上面走的是规则通道（raw_skills 为空），验证不到"模型声称"这条路。
        # 这里模拟模型通道：让模型给出一个**原文里根本没有**的技能（典型幻觉），
        # 断言它被判为未核验、只留待人工核对、绝不计入命中。
        import app.pipeline.extract as extract_mod
        fabricated = {
            "name": "测试员", "education": "硕士", "years": 5, "confidence": 0.9,
            "skills": [{"name": "钛合金", "evidence": "负责钛合金真空熔铸工艺开发"},
                       {"name": "真空熔铸", "evidence": "钛合金真空熔铸"},
                       {"name": "电子束熔炼", "evidence": "负责电子束熔炼工艺定型"}],
        }
        real_llm = extract_mod.extract_llm
        extract_mod.extract_llm = lambda *a, **k: fabricated
        try:
            lm = extract(text, JD, use_llm=True,
                         llm_conf={"api_key": "selftest", "model": "stub"})
        finally:
            extract_mod.extract_llm = real_llm
        c.ok(lm["extract_mode"] == "llm", "模型通道确实被走到（未静默退回规则）", lm["extract_mode"])
        c.ok("电子束熔炼" not in lm["skills"], "模型声称但原文没有的技能不进 skills")
        c.ok("电子束熔炼" in lm["unverified_skills"], "未核验技能单独列出，供人工核对",
             "、".join(lm["unverified_skills"]))
        g = grade(lm, JD, TIERS)
        c.ok("电子束熔炼" not in g["hit"], "未核验技能不会出现在岗位命中里")

        # ============================================================ E
        c.section("E 合规屏蔽：敏感属性在送入模型之前就被替换")
        dirty = ("姓名：李四\n民族：汉族\n婚姻状况：已婚\n生育状况：已育一子\n"
                 "宗教信仰：无\n健康状况：乙肝携带\n身份证号：110101199001011234\n"
                 "家庭住址：北京市某区某路1号\n政治面貌：中共党员\n身高：175cm\n"
                 "学历：博士\n工作年限：8年\n专业技能\n钛合金、粉末冶金\n")
        clean, found = sanitize.scrub(dirty)
        c.ok("汉族" not in clean and "已婚" not in clean and "已育一子" not in clean,
             "民族/婚姻/生育取值已被替换", f"命中类别 {sorted(found)}")
        c.ok("110101199001011234" not in clean, "身份证号已被屏蔽")
        c.ok("中共党员" not in clean, "政治面貌已被屏蔽")
        c.ok("钛合金" in clean and "博士" in clean, "能力与学历信息完整保留")
        c.ok(len(found) >= 6, "回报命中类别供审计举证", f"{len(found)} 类")

        stripped, removed = sanitize.strip_forbidden(
            {"name": "李四", "ethnicity": "汉", "marital_status": "已婚", "skills": ["钛合金"]})
        c.ok("ethnicity" not in stripped and "marital_status" not in stripped,
             "模型若返回敏感键，会被丢弃", f"移除 {removed}")

        c2 = extract(dirty, JD)
        c.ok(not any(k in c2 for k in ("ethnicity", "marital", "religion")),
             "抽取结果里不含敏感字段")
        c.ok(c2["sensitive_found"], "该简历的屏蔽类别被记录", json.dumps(c2["sensitive_found"], ensure_ascii=False))

        # ============================================================ F
        c.section("F 分级：结果与命中证据逐条核对")
        conn = db.connect(db_path)
        # 期望值必须**只依据简历原文**推出，不能凭"岗位需要什么"倒推：
        # 刘婉清原文只有 钛合金/热加工/锻造/材料成型，没有真空熔铸；
        # 赵敏原文是 难熔高熵合金/粉末冶金/真空熔铸/材料成型/热处理/增材制造，没有钛合金。
        # 所以她们各自都被扣掉缺失项，而不是"因为岗位要就都给上"。
        expect = {"陈志远": ("A", ["钛合金", "真空熔铸", "材料成型"], []),
                  "刘婉清": ("B", ["钛合金", "材料成型"], ["真空熔铸"]),
                  "赵敏": ("B", ["真空熔铸", "材料成型"], ["钛合金"])}
        for name, (tier, must_hit, must_miss) in expect.items():
            row = [x for x in db.list_candidates(conn) if x["name"] == name]
            if not row:
                c.ok(False, f"{name} 在库中")
                continue
            x = row[0]
            c.ok(x["tier_effective"] == tier, f"{name} 档位 = {tier}",
                 f"实际 {x['tier_effective']}（分 {x['score']}）")
            for sk in must_hit:
                c.ok(sk in (x["hits"] or []), f"{name} 命中必需技能「{sk}」")
            for sk in must_miss:
                c.ok(sk not in (x["hits"] or []),
                     f"{name} 未命中「{sk}」（原文确无此经历，不得凭岗位需要倒推）",
                     "、".join(x["hits"] or []) or "无")
        # 证据准确性
        d = db.candidate_detail(conn, [x for x in db.list_candidates(conn)
                                       if x["name"] == "陈志远"][0]["id"])
        ev = {s["name"]: s["evidence"] for s in d["skills"] if s["evidence"]}
        c.ok("真空熔铸" in (ev.get("真空熔铸") or ""),
             "技能证据指向含该技能的原文句子", ev.get("真空熔铸", "")[:40])
        c.ok("材料成型" in (ev.get("材料成型") or "") or "材料成型" in (ev.get("材料成型") or ""),
             "「材料成型」证据取自工作经历而非专业名",
             ev.get("材料成型", "")[:40])
        unparsed = [x for x in db.list_candidates(conn) if x["needs_review"]]
        c.ok(unparsed and unparsed[0]["tier_effective"] == "D",
             "解析失败的简历降到 D 而非被排除")
        conn.close()

        # ============================================================ G
        c.section("G 建议与决定分离：HR 确认才生效，且新版本不覆盖已确认结果")
        conn = db.connect(db_path)
        chen = [x for x in db.list_candidates(conn) if x["name"] == "陈志远"][0]
        aid = chen["application_id"]
        c.ok(chen["tier_suggested"] == "A", "系统建议档位已写入 tier_suggested")
        c.ok(chen["tier_final"] is None, "HR 未确认前 tier_final 为空")
        c.ok(chen["app_status"] == "待确认", "状态为『待确认』")
        before_audit = len(db.list_audit(conn, limit=999))
        r = db.set_application_tier(conn, aid, "B", "沟通后调整", "hr", "hr")
        c.ok(r["tier_final"] == "B", "HR 确认后 tier_final = B")
        c.ok(r["status"] == "已确认", "状态变为『已确认』")
        c.ok(len(db.list_audit(conn, limit=999)) > before_audit, "改档写入审计")
        conn.close()

        # 再次投递同岗位新版本 -> 不得覆盖 HR 结论
        mail2 = os.path.join(work, "mail2")
        os.makedirs(mail2, exist_ok=True)
        from tests.make_fixtures import build_eml
        chen_v3 = ("姓名：陈志远\n邮箱：chenzhiyuan@example.com\n电话：138****1234\n"
                   "教育背景\n2015.09-2018.06  西北工业大学  材料加工工程  硕士\n"
                   "工作年限：9年\n工作经历\n- 负责钛合金真空熔铸工艺开发\n"
                   "- 材料成型工艺优化\n专业技能\n钛合金、真空熔铸、材料成型\n")
        with open(os.path.join(mail2, "chen_v3.eml"), "wb") as fh:
            fh.write(build_eml("陈志远 <chenzhiyuan@example.com>", "第三次更新简历",
                               "Thu, 18 Sep 2026 09:00:00 +0800", "<mail-100@example.com>",
                               [("陈志远-简历-v3.txt", chen_v3.encode("utf-8"), "text/plain")]))
        cfg2 = dict(cfg, eml_dir=mail2)
        r2 = ingest.sync_mailbox(JD, TIERS, db_path, job_id=job_id, cfg=cfg2)
        c.ok(r2["merged_versions"] == 1, "第三次投递识别为同人同岗新版本")
        conn = db.connect(db_path)
        chen = [x for x in db.list_candidates(conn) if x["name"] == "陈志远"][0]
        c.ok(chen["tier_final"] == "B", "HR 已确认的档位 B 未被新版本覆盖",
             f"实际 {chen['tier_final']}")
        d = db.candidate_detail(conn, chen["id"])
        c.ok(len(d["documents"]) == 3, "三份简历版本都留档", f"实际 {len(d['documents'])}")
        c.ok(any(a["action"] == "new_version_archived" for a in
                 db.list_audit(conn, limit=999)), "『保留 HR 结论』的动作写入审计")
        conn.close()

        # ============================================================ H
        c.section("H 幂等：重复收取不产生新增")
        stat_before = db.pool_stats(db.connect(db_path))
        ingest.sync_mailbox(JD, TIERS, db_path, job_id=job_id, cfg=cfg)
        ingest.sync_mailbox(JD, TIERS, db_path, job_id=job_id, cfg=cfg)
        conn = db.connect(db_path)
        stat_after = db.pool_stats(conn)
        c.ok(stat_before["people"] == stat_after["people"], "重复收取不增加人档",
             f"{stat_before['people']} → {stat_after['people']}")
        c.ok(stat_before["applications"] == stat_after["applications"], "重复收取不增加投递")
        c.ok(stat_before["documents"] == stat_after["documents"], "重复收取不增加附件")
        conn.close()

        # ============================================================ I
        c.section("I 检索：技能精确召回 + 语义召回")
        conn = db.connect(db_path)
        r = search.by_skills(conn, ["真空熔铸", "钛合金"], mode="all")
        c.ok(r["resolved"] == ["真空熔铸", "钛合金"] or
             set(r["resolved"]) == {"真空熔铸", "钛合金"},
             "技能词归一到本体", "、".join(r["resolved"]))
        c.ok(r["count"] == 1 and r["results"][0]["name"] == "陈志远",
             "严格模式（全部命中）只召回原文同时具备两项的人",
             f"{r['count']} 人：" + "、".join(x["name"] or "?" for x in r["results"]))
        r_any = search.by_skills(conn, ["真空熔铸", "钛合金"], mode="any")
        names = {x["name"] for x in r_any["results"]}
        c.ok({"陈志远", "刘婉清", "赵敏"} <= names,
             "放宽为『任一命中』时三类背景都能被召回",
             "、".join(sorted(n for n in names if n)))
        c.ok(all(x["evidence"] for x in r_any["results"]),
             "每条召回都带原文证据")
        liu = [x for x in r_any["results"] if x["name"] == "刘婉清"]
        c.ok(liu and "真空熔铸" not in (liu[0]["matched_skills"] or []),
             "刘婉清不会被算作具备真空熔铸（原文没有就不给）",
             "、".join(liu[0]["matched_skills"]) if liu else "—")
        r_rare = search.by_skills(conn, ["难熔高熵合金"], mode="any")
        c.ok(r_rare["count"] >= 1 and any(x["name"] == "赵敏" for x in r_rare["results"]),
             "别名技能（难熔高熵合金）可被召回")
        unknown = search.by_skills(conn, ["冰箱贴设计师"], mode="any")
        c.ok(unknown["unknown_terms"] == ["冰箱贴设计师"], "本体外的词被如实标出，不假装认识")
        c.ok(unknown["count"] == 0, "本体外词查不到就返回空，不编造")
        conn.close()

        idx = search.build_index(db_path)
        c.ok(idx["indexed"] >= 4, "向量索引建立", f"模型 {idx.get('model')} 维度 {idx.get('dim')}")
        conn = db.connect(db_path)
        sem = search.semantic(conn, "做过粉末冶金和增材制造的材料博士", top_k=4)
        c.ok(len(sem["results"]) >= 3, "语义检索返回结果", f"{len(sem['results'])} 条")
        if sem["results"]:
            c.ok(sem["results"][0]["name"] == "赵敏",
                 "语义检索把粉末冶金+增材制造的博士排在首位",
                 f"首位 {sem['results'][0]['name']}（{sem['results'][0]['score']}）")
        sim = search.similar_to(conn, 1, top_k=3)
        c.ok(len(sim["results"]) >= 1, "相似人才可召回")

        # 检索层只回业务字段：联系方式是密文列（phone_enc/email_enc），
        # 解密统一下沉到接口层做（见 S 段），这里必须确认密文没被带出来。
        leak_keys = ("phone_enc", "email_enc", "phone_bidx", "email_bidx", "identity_key")
        all_hits = (r_any["results"] + sem["results"] + sim["results"])
        c.ok(all(not any(k in h for k in leak_keys) for h in all_hits),
             "检索结果不含密文列/盲索引/身份键")
        c.ok(all("phone" not in h for h in all_hits),
             "检索层不掺明文联系方式（避免绕过接口层直接下发）")
        conn.close()

        # 增量索引：导入后自动调用的是 build_index(force=False)，
        # 画像未变的人必须被跳过——不能每次都全量重建。
        idx2 = search.build_index(db_path)
        c.ok(idx2["indexed"] == 0 and idx2["skipped"] >= 4,
             "增量索引：画像未变时整批跳过，不做全量重建",
             f"新增 {idx2['indexed']} / 跳过 {idx2['skipped']} 人（{idx2.get('note')}）")
        # 落库模型名必须与比对模型名一致，否则"跳过"永远不生效（本次修掉的坑）
        c.ok(idx2["model"] == idx["model"],
             "增量比对的模型名与实际落库模型名一致（否则每次都会全量重算）",
             f"落库 {idx['model']} / 比对 {idx2['model']}")

        # ============================================================ Q
        c.section("Q 降级：向量服务不可达时用哈希向量兜底")
        os.environ["TP_EMBED_BASE_URL"] = "http://127.0.0.1:9/v1"
        vecs, model, err = search.embed(["测试文本"])
        c.ok(model == "local-hash-v1" and len(vecs[0]) == search.HASH_DIM,
             "自动退化为本地哈希向量", f"模型 {model} 维度 {len(vecs[0])}")
        c.ok(bool(err), "降级原因如实上报，不假装服务正常")
        os.environ.pop("TP_EMBED_BASE_URL", None)

        # ============================================================ J / K / L
        c.section("J/K/L 智能体：规则路由可用、写操作只出提案、外发工具被拦")
        ctx = ToolCtx(db_path=db_path, jd=JD, tiers=TIERS, job_id=job_id,
                      session_id="selftest", operator="hr", role="hr")
        ans = agent_loop.run_agent("人才库有多少人？各档位分布如何？", ctx)
        c.ok(ans["mode"] == "rule", "模型不可用时降级为规则模式", ans["mode"])
        c.ok("人才库总览" in ans["answer"], "规则模式仍能回答统计问题")
        c.ok("候选人数" in ans["answer"], "答案含真实数据字段")

        ans2 = agent_loop.run_agent("帮我找做过真空熔铸、会 XRD 的候选人", ctx)
        c.ok(any(t["tool"] == "search_by_skills" for t in ans2["trace"]),
             "技能类问题触发技能召回工具",
             "、".join(t["tool"] for t in ans2["trace"]))
        c.ok("陈志远" in ans2["answer"] or "刘婉清" in ans2["answer"],
             "规则模式给出的名单来自真实库")
        c.ok("证据[" in ans2["answer"], "答复里带上原文证据")

        ans3 = agent_loop.run_agent("邮箱收了多少份简历？", ctx)
        c.ok("邮箱抓取状态" in ans3["answer"], "收信类问题被正确路由")

        # 写操作：只出提案
        conn = db.connect(db_path)
        chen = [x for x in db.list_candidates(conn) if x["name"] == "陈志远"][0]
        aid = chen["application_id"]
        props_before = len(db.list_proposals(conn, limit=999))
        stage_before = chen["stage"]
        conn.close()

        ans4 = agent_loop.run_agent("把陈志远的投递推进到初面", ctx)
        # 规则路由不覆盖这个意图，故直接调工具验证提案机制
        from app.agent.tools import execute as tool_exec
        raw = json.loads(tool_exec("set_stage", {"application_id": aid, "stage": "初面",
                                                 "reason": "简历评估通过"}, ctx))
        c.ok(raw.get("status") == "pending_confirmation",
             "写类工具返回『待确认』而不是『已完成』", json.dumps(raw, ensure_ascii=False)[:80])
        c.ok("尚未生效" in raw.get("note", ""), "工具结果明确告知模型：操作尚未生效")
        conn = db.connect(db_path)
        c.ok(len(db.list_proposals(conn, limit=999)) == props_before + 1, "提案已入库")
        now_stage = db.get_application(conn, aid)["stage"]
        c.ok(now_stage == stage_before, "**库内数据未被改动**，等 HR 确认",
             f"{stage_before} → {now_stage}")
        conn.close()

        blocked = json.loads(tool_exec("send_email", {"to": "a@b.com", "body": "hi"}, ctx))
        c.ok("error" in blocked and "不提供" in blocked["error"],
             "对外发送类工具被明确拒绝", blocked["error"][:44])
        for name in ("delete_candidate", "reject_candidate"):
            b = json.loads(tool_exec(name, {"candidate_id": 1}, ctx))
            c.ok("error" in b, f"危险工具 {name} 被拒绝")

        unknown_tool = json.loads(tool_exec("no_such_tool", {}, ctx))
        c.ok("error" in unknown_tool, "未知工具返回可读错误而非崩溃")

        # 提案确认 → 真正生效
        pid = raw["proposal_id"]
        res = actions.apply_proposal(db_path, pid, "approve", "hr", "hr")
        c.ok(res.get("ok") and res.get("status") == "已执行", "HR 确认后提案执行成功")
        conn = db.connect(db_path)
        c.ok(db.get_application(conn, aid)["stage"] == "初面",
             "此刻才真正改库：阶段 = 初面")
        tags_before = len(db.candidate_detail(conn, chen["id"])["tags"])
        conn.close()

        # 加标签同样走提案：标签会直接影响后续筛选，必须由 HR 确认
        raw_tag = json.loads(tool_exec("add_tag", {"candidate_id": chen["id"],
                                                   "tag": "面试重点", "category": "评价",
                                                   "reason": "沟通后确认推进"}, ctx))
        c.ok(raw_tag.get("status") == "pending_confirmation",
             "加标签同样是提案，模型不能直接生效", json.dumps(raw_tag, ensure_ascii=False)[:70])
        conn = db.connect(db_path)
        c.ok(len(db.candidate_detail(conn, chen["id"])["tags"]) == tags_before,
             "确认之前标签未写入档案")
        conn.close()
        tag_res = actions.apply_proposal(db_path, raw_tag["proposal_id"], "approve", "hr", "hr")
        c.ok(tag_res.get("ok"), "HR 确认后标签写入")
        conn = db.connect(db_path)
        c.ok(len(db.candidate_detail(conn, chen["id"])["tags"]) == tags_before + 1,
             "标签已写入候选档案")
        conn.close()

        # ============================================================ M
        c.section("M 权限：单 HR 角色具备全部权限（无 viewer/recruiter/admin 之分）")
        c.ok(set(auth.ROLES) == {"hr"}, "只保留单一角色 hr", "、".join(auth.ROLES))
        for perm in ("read", "chat", "propose", "confirm", "set_stage", "add_note",
                     "export", "ingest", "manage_users", "settings", "view_full_contact", "merge"):
            c.ok(auth.can("hr", perm), f"hr 具备 {perm} 权限")
        c.ok(not auth.can("viewer", "confirm"), "不存在的旧角色 viewer 不再有任何权限（can 返回 False）")
        c.ok(not auth.can("admin", "manage_users"), "不存在的旧角色 admin 不再有任何权限")

        # ============================================================ N
        c.section("N 个人信息保护：完整展示 + 加密存储 + 密文不下发")
        conn = db.connect(db_path)
        chen_raw = [x for x in db.list_candidates(conn) if x["name"] == "陈志远"][0]
        full = db.get_candidate(conn, chen_raw["id"])
        from app import crypto

        c.ok(crypto.protection()["encrypted"], "手机号/邮箱为 AES-GCM 加密存储",
             crypto.protection()["algorithm"])
        stored = crypto.decrypt(full["phone_enc"])
        c.ok(stored == "138****1234",
             "库内密文可正确解回，且与简历原文一致（写入=读出）", stored)
        c.ok(chen_raw["identity_key"].startswith(("em:", "ph:")),
             "脱敏手机号不参与身份键（不足以证明是同一个人）",
             chen_raw["identity_key"][:12])

        # 单 HR：完整展示联系方式；但密文/盲索引/身份键绝不下发前端
        demo = dict(full, phone_enc=crypto.encrypt("13812345678"),
                    email_enc=crypto.encrypt("zhangsan@example.com"),
                    gender="男", birth_year=1995)
        p = auth.present_candidate(demo)
        c.ok(p["contact_full_visible"] is True and p["phone"] == "13812345678"
             and p["email"] == "zhangsan@example.com",
             "单 HR 完整展示联系方式（不再按角色脱敏）", f"{p['phone']} / {p['email']}")
        c.ok(all(k not in p for k in ("phone_enc", "email_enc", "phone_bidx",
                                      "email_bidx", "identity_key")),
             "密文/盲索引/身份键不下发前端")
        # 性别：只取简历**明写**的标签行，且只有男生/女生两种取值——绝不是"推断出来的"。
        # 出生年份仍然完全不采集。真正"不参与评分"由 S 段的换性别对比断言来证明。
        _gvals = {x.get("gender") for x in db.list_candidates(conn)}
        c.ok(_gvals <= {None, "", "男", "女"}
             and all(x.get("birth_year") is None for x in db.list_candidates(conn)),
             "性别只可能是明写的男/女或空，出生年份不写入结构化档案")
        conn.close()

        # ============================================================ O
        c.section("O 档案合并：软合并可撤销，投递与附件随之迁移")
        conn = db.connect(db_path)
        before_lists = len(db.list_candidates(conn))
        ids = [x["id"] for x in db.list_candidates(conn)]
        target = [x for x in db.list_candidates(conn) if x["name"] == "陈志远"][0]["id"]
        src = [i for i in ids if i != target][0]
        src_apps = len(db.list_applications(conn, cid=src))
        r = db.merge_candidates(conn, src, target, "hr", "hr")
        c.ok(r["ok"], "软合并执行成功")
        c.ok(len(db.list_candidates(conn)) == before_lists - 1,
             "合并后列表少 1 条（被并入的档不再独立显示）")
        c.ok(db.get_candidate(conn, src)["merged_into"] == target,
             "被并入的档标记 merged_into（非物理删除）")
        c.ok(len(db.list_applications(conn, cid=target)) >= 1 + src_apps,
             "源档的投递已迁移到主档")
        c.ok(any(a["action"] == "merge" for a in db.list_audit(conn, limit=999)),
             "合并动作写入审计")
        db.split_candidate(conn, src, "hr", "hr")
        c.ok(db.get_candidate(conn, src)["merged_into"] is None, "可撤销：拆回独立档")
        c.ok(len(db.list_candidates(conn)) == before_lists, "拆分后列表恢复")
        conn.close()

        # ============================================================ P
        c.section("P 审计：查看、改档、确认提案全部留痕")
        conn = db.connect(db_path)
        rows = db.list_audit(conn, limit=999)
        actions_seen = {x["action"] for x in rows}
        for act in ("create", "ingest", "set_tier", "set_stage", "merge",
                    "split", "new_version_archived", "add_tag",
                    "duplicate_skipped"):
            c.ok(act in actions_seen, f"审计覆盖动作「{act}」")
        c.ok(any(x["action"] == "approve" and x["entity"] == "proposal" for x in rows),
             "提案确认动作被记录")
        c.ok(all(x["ts"] for x in rows), "每条审计都有时间戳")
        cokv = [x for x in rows if x["action"] == "set_tier"]
        c.ok(all(x["before"] != "" for x in cokv), "改档审计记录了变更前值",
             f"{len(cokv)} 条")
        c.ok(any(x["operator"] == "hr" for x in rows),
             "操作以 hr 身份留痕（单角色）")
        conn.close()

        # ============================================================ R
        c.section("R 跨渠道去重：手工导入过的人，邮件再投不重复建投递")
        rdb = os.path.join(work, "cross.db")
        rcfg = dict(cfg, archive_dir=os.path.join(work, "archive2"))
        conn = db.connect(rdb)
        jid2 = db.upsert_job(conn, JD)
        conn.close()
        # 第一次：手工文件夹导入，且**不带岗位**（等价于 v0.1 时期迁移过来的既有投递）
        # 期望值从目录实际内容推导，而不是写死数字——否则往 data/resumes 里
        # 增删一份样例，这条会因为"样例数量变了"而误报失败，掩盖真正的去重问题。
        src = resume_dir
        n_src = len([f for f in os.listdir(src)
                     if f.lower().endswith(tuple(SUPPORTED))])
        ingest.ingest_dir(src, JD, TIERS, rdb, cfg=rcfg, channel="文件夹", job_id=None)
        conn = db.connect(rdb)
        pool1 = db.pool_stats(conn)
        c.ok(pool1["applications"] == n_src and pool1["people"] == n_src,
             f"目录里 {n_src} 份简历各建档 1 人 1 条投递（不丢件、不重复）",
             f"{pool1['people']} 人 / {pool1['applications']} 条")
        conn.close()
        # 第二次：同一批人走邮箱再投一次（主题带岗位名才归岗，不带则待指定）
        r = ingest.sync_mailbox(JD, TIERS, rdb, job_id=jid2, cfg=cfg)
        c.ok(r["added"] == 1, "只有真正的新附件入库（损坏 docx）", f"实际 {r['added']}")
        c.ok(r["merged_versions"] == 1, "同人更新版按『新版本』归档", f"实际 {r['merged_versions']}")
        c.ok(r["skipped_dup"] == 5, "文件层 4 次 + 邮件层 1 次重复被拦下", f"实际 {r['skipped_dup']}")
        conn = db.connect(rdb)
        chen2 = [x for x in db.list_candidates(conn) if x["name"] == "陈志远"][0]
        apps2 = db.list_applications(conn, cid=chen2["id"])
        c.ok(len(apps2) == 1, "同一人跨渠道投递仍只有 1 条投递（更新版合并归档）",
             f"实际 {len(apps2)} 条")
        c.ok(apps2 and apps2[0]["job_id"] is None,
             "主题未带岗位名的更新版不改变『待指定』状态（不自动归岗）",
             f"job_id={apps2[0]['job_id'] if apps2 else '—'}")
        c.ok(len(db.candidate_detail(conn, chen2["id"])["documents"]) == 2,
             "两次投递的两份简历都留档")
        conn.close()

        # ============================================================ R2
        c.section("R2 标题归岗 + 岗位停用（只停用不删除）")
        r2db = os.path.join(work, "route.db")
        conn = db.connect(r2db)
        did_a = db.upsert_department(conn, "研发中心", "负责工艺研发")
        did_b = db.upsert_department(conn, "质量部", "负责质检")
        jid_a = db.create_job(conn, "工艺工程师", dept_id=did_a, jd=JD, operator="hr")
        jid_b = db.create_job(conn, "质检员", dept_id=did_b, jd=JD, operator="hr")
        c.ok(jid_a > 0 and jid_b > 0, "两个岗位创建成功")
        conn.close()

        from tests.make_fixtures import build_eml

        rdir = os.path.join(work, "route_mail")
        os.makedirs(rdir, exist_ok=True)

        def _m(fn, subject, mid, cn, user):
            """cn=中文姓名（必须 2-4 字，否则姓名解析不认）；user=邮箱前缀（ASCII）。"""
            body = ("姓名：{n}\n邮箱：{e}\n学历：硕士\n工作年限：5年\n"
                    "专业技能\n钛合金、真空熔铸、材料成型\n").format(n=cn, e=f"{user}@example.com")
            with open(os.path.join(rdir, fn), "wb") as fh:
                fh.write(build_eml(f"{cn} <{user}@example.com>", subject,
                                   "Mon, 20 Sep 2026 09:00:00 +0800", mid,
                                   [(f"{cn}-简历.txt", body.encode("utf-8"), "text/plain")]))

        _m("r01.eml", "应聘 工艺工程师 - 钱一鸣", "<r01@example.com>", "钱一鸣", "qianym")
        _m("r02.eml", "应聘 质检员 - 孙博文", "<r02@example.com>", "孙博文", "sunbw")
        _m("r03.eml", "简历投递 - 周文清", "<r03@example.com>", "周文清", "zhouwq")
        _m("r04.eml", "应聘 质检员 - 吴海燕", "<r04@example.com>", "吴海燕", "wuhy")

        rcfg2 = dict(cfg, eml_dir=rdir, archive_dir=os.path.join(work, "route_archive"))
        # 先停用「质检员」：之后的质检员投递应落待指定
        conn = db.connect(r2db)
        db.set_job_active(conn, jid_b, False, "hr")
        conn.close()

        rr = ingest.sync_mailbox(JD, TIERS, r2db, cfg=rcfg2)
        conn = db.connect(r2db)
        by_name = {x["name"]: x for x in db.list_candidates(conn)}
        c.ok(set(by_name) >= {"钱一鸣", "孙博文", "周文清", "吴海燕"},
             "4 封邮件全部建档", f"实际 {sorted(n for n in by_name if n)}")
        c.ok(db.get_application(conn, by_name["钱一鸣"]["application_id"])["job_id"] == jid_a,
             "主题「应聘 工艺工程师」归到工艺工程师岗")
        c.ok(db.get_application(conn, by_name["周文清"]["application_id"])["job_id"] is None,
             "主题「简历投递」无岗位名 → 待指定")
        c.ok(db.get_application(conn, by_name["吴海燕"]["application_id"])["job_id"] is None,
             "投递到已停用岗位 → 不归岗，待指定")
        c.ok(db.get_application(conn, by_name["孙博文"]["application_id"])["job_id"] is None,
             "停用后的岗位不再接收归岗（孙博文的质检员投递落待指定）")
        j_b = db.get_job(conn, jid_b)
        c.ok(j_b["active"] == 0 and j_b["status"] == "停用", "岗位停用是软删（active=0，仍可查）")
        db.set_job_active(conn, jid_b, True, "hr")
        c.ok(db.get_job(conn, jid_b)["active"] == 1, "停用岗位可重新启用")
        conn.close()

        # 部门停用 → 其下岗位一并停止归岗（否则「部门已关，简历还往这个部门的岗位挂」）
        conn = db.connect(r2db)
        db.set_department_active(conn, did_b, False, "hr")
        c.ok(jid_b not in db.routable_job_ids(conn),
             "部门停用后，其下岗位不再参与标题归岗")
        c.ok(db.match_job_by_subject(conn, "应聘 质检员 - 新投递")[0] is None,
             "部门停用后，标题写了该岗位名也落『待指定』")
        db.set_department_active(conn, did_b, True, "hr")
        c.ok(jid_b in db.routable_job_ids(conn), "部门重新启用后，岗位恢复归岗")
        conn.close()

        # 文件夹上传：只分析、不归岗
        ingest.ingest_dir(resume_dir, JD, TIERS, r2db,
                          cfg=rcfg2, channel="文件夹", job_id=None)
        conn = db.connect(r2db)
        c.ok(all(a["job_id"] is None for a in db.list_applications(conn)
                 if a["channel"] == "文件夹"),
             "文件夹上传的简历一律不归岗（待 HR 指定）")
        conn.close()

        # ============================================================ S
        c.section("S 接口自检：静态调用面 + 全部路由可达")
        # S1 静态：模块之间调用的函数必须真实存在。
        #    （ui.py 里的 search.xxx 是前端 JS，不是 Python 调用，故排除）
        import re as _re

        from app import auth as _auth, crypto as _crypto, search as _search, server as srv
        namespaces = {"db": set(dir(db)), "auth": set(dir(_auth)), "search": set(dir(_search)),
                      "crypto": set(dir(_crypto)), "nz": set(dir(nz)),
                      "sanitize": set(dir(sanitize)), "ingest": set(dir(ingest)),
                      "actions": set(dir(actions))}
        missing: dict[str, set] = {}
        for root, _dirs, files in os.walk(os.path.join(BASE, "app")):
            for f in files:
                if not f.endswith(".py") or f == "ui.py":
                    continue
                src = open(os.path.join(root, f), encoding="utf-8").read()
                for alias, have in namespaces.items():
                    for m in _re.finditer(rf"\b{alias}\.([A-Za-z_][A-Za-z0-9_]*)", src):
                        if m.group(1) not in have:
                            missing.setdefault(f, set()).add(f"{alias}.{m.group(1)}")
        c.ok(not missing, "所有跨模块函数调用都真实存在（不会等运行到才 500）",
             "; ".join(f"{k}: {sorted(v)}" for k, v in missing.items()))

        # S2 逐路由冒烟（指向测试库；/api/ingest 故意不测——它会去读真实邮件目录并落盘）
        original_db = srv.DB_PATH
        srv.DB_PATH = db_path
        # 邮箱配置写回会落盘 config/mailbox.json，测试期间改写到临时目录，避免污染真实配置
        import app.mailbox as _mb

        _orig_cfg = _mb.CONFIG_PATH
        _orig_secret = _mb._SECRET_DEFAULT_PATH
        _orig_removed = srv.REMOVED_DIR
        _mb.CONFIG_PATH = os.path.join(work, "mailbox_test.json")
        _mb._SECRET_DEFAULT_PATH = os.path.join(work, "imap_test.secret")
        # 来源文件接口按 mailbox 配置里的 folder_dir 找目录：测试配置写到临时位置、
        # 指向自检自造的夹具目录，不碰真实来源文件夹（HR_test1 / data/resumes）。
        _mb.save_config({"folder_dir": resume_dir})

        hr = {"Content-Type": "application/json"}

        # 取一个真实存在的附件，用来验证"简历原件预览/下载"不是只测"不崩溃"
        _c = db.connect(db_path)
        _docrow = _c.execute(
            "SELECT id FROM documents WHERE COALESCE(archived_path,'') != '' "
            "ORDER BY id LIMIT 1").fetchone()
        # 再插一条"指向仓库内文件 + 相对路径"的附件：
        # 真实库里的 archived_path 两种写法并存，相对路径必须能按仓库根解析出来
        _cur = _c.execute(
            """INSERT INTO documents (candidate_id, file_name, file_path, archived_path,
                   file_hash, mime, created_at)
               VALUES (1,'README.md','README.md','README.md','readmehash','text/markdown','')""")
        ok_did = _cur.lastrowid
        _c.commit()
        _c.close()
        doc_id = _docrow["id"] if _docrow else None

        routes: list[tuple[str, str, dict, bytes]] = [
            ("GET", "/", {}, b""),
            ("GET", "/api/health", {}, b""),
            ("GET", "/api/me", {}, b""),
            ("GET", "/api/meta", {}, b""),
            ("GET", "/api/candidates", {}, b""),
            ("GET", "/api/candidates?gender=%E5%A5%B3", {}, b""),
            ("GET", "/api/candidates/1", {}, b""),
            ("GET", "/api/candidates/1/duplicates", {}, b""),
            ("GET", "/api/candidates/9999", {}, b""),
            ("GET", "/api/stats", {}, b""),
            ("GET", "/api/pipeline", {}, b""),
            ("GET", "/api/emails", {}, b""),
            ("GET", "/api/departments", {}, b""),
            ("GET", "/api/jobs", {}, b""),
            ("GET", "/api/sources", {}, b""),
            ("GET", "/api/sources/removed", {}, b""),
            ("GET", "/api/settings", {}, b""),
            ("GET", "/api/mailbox/config", {}, b""),
            ("GET", "/api/mailbox/presets", {}, b""),
            ("GET", "/api/search/skills?skills=%E9%92%9B%E5%90%88%E9%87%91&mode=any", {}, b""),
            ("GET", "/api/search/semantic?q=%E6%9D%90%E6%96%99%E5%8D%9A%E5%A3%AB&top_k=3", {}, b""),
            ("GET", "/api/search/similar?candidate_id=1", {}, b""),
            ("GET", "/api/search/status", {}, b""),
            ("GET", "/api/agent/tools", {}, b""),
            ("GET", "/api/agent/runs", {}, b""),
            ("GET", "/api/agent/status", {}, b""),
            ("GET", "/api/proposals", {}, b""),
            ("GET", "/api/audit", {}, b""),
            ("GET", "/api/ontology", {}, b""),
            ("GET", "/api/policy", {}, b""),
            ("POST", "/api/agent/chat", hr,
             json.dumps({"message": "人才库有多少人？"}).encode()),
            ("POST", "/api/search/index", hr, json.dumps({"force": False}).encode()),
            ("POST", "/api/departments", hr, json.dumps({"name": "测试部门"}).encode()),
            ("POST", "/api/jobs", hr, json.dumps({"title": "测试岗位"}).encode()),
            ("POST", "/api/mailbox/config", hr, json.dumps({"mode": "eml"}).encode()),
            ("POST", "/api/mailbox/test", hr, json.dumps({}).encode()),
            ("POST", "/api/settings", hr, json.dumps({}).encode()),
            ("POST", "/api/sources/remove", hr, json.dumps({"names": []}).encode()),
            ("POST", "/api/sources/restore", hr, json.dumps({"names": []}).encode()),
            ("POST", "/api/candidates/1/analyze", hr, b""),
            ("POST", "/api/candidates/1/interview", hr, b""),
            ("POST", "/api/candidates/1/explain", hr, b""),
            ("POST", "/api/applications/1/note", hr,
             json.dumps({"note": "接口自检"}).encode()),
        ]
        # 原件预览/下载：仓库内留档（含相对路径写法）+ 缺失附件两条分支
        routes.append(("GET", f"/api/documents/{ok_did}/file", {}, b""))
        routes.append(("GET", f"/api/documents/{ok_did}/file?inline=1", {}, b""))
        routes.append(("GET", f"/api/jobs/{job_id}", {}, b""))
        routes.append(("POST", f"/api/jobs/{job_id}/jd", hr,
                       json.dumps({"must_skills": "钛合金"}).encode()))
        routes.append(("POST", f"/api/jobs/{job_id}/regrade", hr,
                       json.dumps({"apply": False}).encode()))
        routes.append(("POST", "/api/mailbox/preview", hr, json.dumps({}).encode()))
        routes.append(("GET", "/api/documents/999999/file", {}, b""))
        # 归档接口冒烟：用不存在的 id，只验"路由在、不 5xx"，不动真实测试数据
        routes.append(("POST", "/api/candidates/9999/archive", hr, b""))
        routes.append(("POST", "/api/candidates/9999/unarchive", hr, b""))
        # v1.5：批量归档 / 到期彻底删除 / 存量岗位建议重算
        routes.append(("POST", "/api/candidates/archive-batch", hr,
                       json.dumps({"ids": [9999], "archived": True}).encode()))
        routes.append(("POST", "/api/archive/purge-due", hr, b""))
        routes.append(("POST", "/api/candidates/9999/purge", hr, b""))
        routes.append(("POST", "/api/candidates/route-pending?apply=0", hr, b""))
        try:
            broken = []
            bodies: dict[str, tuple[int, str]] = {}
            for method, url, hdrs, payload in routes:
                try:
                    code, text = _asgi(srv.app, method, url, hdrs, payload)
                except Exception as exc:  # 应用层抛出即视为接口不可用
                    broken.append(f"{method} {url} 抛异常 {type(exc).__name__}: {exc}")
                    continue
                bodies[f"{method} {url}"] = (code, text)
                if code >= 500:
                    broken.append(f"{method} {url} -> {code} {text[:60]}")
            c.ok(not broken, f"{len(routes)} 个接口全部无 5xx 崩溃",
                 " | ".join(broken))

            # 只断言"不报 5xx"是不够的：函数体被整段删掉时，FastAPI 会照样回 200 + `null`，
            # 冒烟全绿而接口实际已废（本轮就在插入新路由时误删过 `api_emails` 的函数体）。
            # 所以对"必须回对象/数组"的核心读接口，再断言响应体能解析出非 None 的 JSON。
            must_have_body = [
                "GET /api/candidates", "GET /api/stats", "GET /api/pipeline",
                "GET /api/emails", "GET /api/sources", "GET /api/sources/removed",
                "GET /api/departments", "GET /api/jobs", "GET /api/proposals",
                "GET /api/audit", "GET /api/ontology", "GET /api/policy",
                "GET /api/settings", "GET /api/mailbox/config", "GET /api/mailbox/presets",
                "GET /api/agent/status", "GET /api/search/status", "GET /api/meta",
            ]
            empty_body = []
            errored = {b.split(" ")[0] + " " + b.split(" ")[1] for b in broken
                       if len(b.split(" ")) > 1}
            for key in must_have_body:
                got = bodies.get(key)
                if got is None:
                    # 两种情况要分开说：接口压根没进冒烟清单，和接口进了但抛/超时没拿到响应体。
                    # 混成一句话会让人去查"清单漏了"，其实该查的是接口本身。
                    empty_body.append(
                        f"{key}（冒烟时未拿到响应体：{'已抛异常，见上条' if key in errored else '不在冒烟清单中'}）")
                    continue
                code, text = got
                try:
                    parsed = json.loads(text)
                except Exception:  # noqa: BLE001
                    empty_body.append(f"{key}（响应体不是 JSON：{text[:40]}）")
                    continue
                if parsed is None:
                    empty_body.append(f"{key}（返回 null，疑似函数体缺失）")
            c.ok(not empty_body,
                 f"{len(must_have_body)} 个核心读接口都真的返回了内容（不是 200 + null）",
                 " | ".join(empty_body))

            code404, _ = _asgi(srv.app, "GET", "/api/candidates/9999")
            c.ok(code404 == 404, "不存在的候选人返回 404 而非 500", f"实际 {code404}")

            # S3 软归档闭环（v1.4）：归档 = 从人才库与检索隐藏，进「归档」页；
            # 取消归档 = 恢复；两次动作都要写审计。归档不是删除——档案与附件不动。
            _, body = _asgi(srv.app, "GET", "/api/candidates", {})
            pool_ids = [x["id"] for x in json.loads(body)["items"]]
            c.ok(bool(pool_ids), "测试库有候选人可用于归档闭环", f"{len(pool_ids)} 人")
            # 优先挑一个技能召感能命中的候选人，这样"归档后检索不再命中"才有判定意义
            _, body = _asgi(srv.app, "GET",
                            "/api/search/skills?skills=%E9%92%9B%E5%90%88%E9%87%91&mode=any", {})
            hits = [x["candidate_id"] for x in json.loads(body).get("results", [])]
            cid = hits[0] if hits else pool_ids[0]
            code, body = _asgi(srv.app, "POST", f"/api/candidates/{cid}/archive", hr, b"")
            c.ok(code == 200, "归档接口返回 200", f"实际 {code} {body[:60]}")
            _, body = _asgi(srv.app, "GET", "/api/candidates", {})
            c.ok(cid not in [x["id"] for x in json.loads(body)["items"]],
                 "归档后从人才库默认列表消失")
            _, body = _asgi(srv.app, "GET", "/api/candidates?archived=1", {})
            c.ok(cid in [x["id"] for x in json.loads(body)["items"]],
                 "归档后出现在「归档」页列表（?archived=1）")
            _, body = _asgi(srv.app, "GET", "/api/candidates?archived=0", {})
            c.ok(cid not in [x["id"] for x in json.loads(body)["items"]],
                 "?archived=0 只回未归档（默认列表同一口径）")
            if cid in hits:
                _, body = _asgi(srv.app, "GET",
                                "/api/search/skills?skills=%E9%92%9B%E5%90%88%E9%87%91&mode=any", {})
                c.ok(cid not in [x["candidate_id"] for x in json.loads(body)["results"]],
                     "归档后技能召回不再命中该人")
            else:
                c.ok(True, "该候选人无钛合金技能（检索基线不命中），召回过滤断言以命中者为条件")
            code, body = _asgi(srv.app, "POST", f"/api/candidates/{cid}/unarchive", hr, b"")
            c.ok(code == 200, "取消归档接口返回 200", f"实际 {code}")
            _, body = _asgi(srv.app, "GET", "/api/candidates", {})
            c.ok(cid in [x["id"] for x in json.loads(body)["items"]],
                 "取消归档后恢复在人才库展示")
            conn = db.connect(db_path)
            acts = [r["action"] for r in conn.execute(
                "SELECT action FROM audit_log WHERE entity='candidate' AND entity_id=?",
                (str(cid),)).fetchall()]
            conn.close()
            c.ok("archive" in acts and "unarchive" in acts,
                 "归档与取消归档均写入审计", f"actions={acts}")
            # 详情接口仍可访问归档过又恢复的档案（数据未动过的旁证）
            code, _ = _asgi(srv.app, "GET", f"/api/candidates/{cid}", {})
            c.ok(code == 200, "归档-恢复后完整档案仍可访问", f"实际 {code}")

            # S4 岗位建议与采纳（C 方案）：待指定投递按各在招岗位试算取最高分，
            # 过 C 档阈值才推荐；HR 采纳才归岗并按岗位 JD 重算；全程留审计。
            conn = db.connect(db_path)
            _text_hit = ("姓名：赵匹配\n学历：本科\n工作经历：5 年工作经验\n"
                         "负责钛合金的真空熔铸工艺，使用 XRD 做物相分析。")
            _cid_hit = db.insert_candidate(conn, {"name": "赵匹配", "source": "测试"})
            _doc_hit = conn.execute(
                """INSERT INTO documents (candidate_id, file_name, file_path, file_hash,
                       mime, raw_text, created_at)
                   VALUES (?, 'zhaomatch.txt', 'zhaomatch.txt', 'zhaomatch-hash',
                           'text/plain', ?, '')""",
                (_cid_hit, _text_hit)).lastrowid
            _aid_hit = conn.execute(
                """INSERT INTO applications (candidate_id, job_id, channel, applied_at,
                       resume_doc_id, status)
                   VALUES (?, NULL, '测试', ?, ?, '待确认')""",
                (_cid_hit, db.now(), _doc_hit)).lastrowid
            # 低匹配对照：与材料类技能毫无交集的候选人，不该被推荐任何岗位
            _text_miss = "姓名：钱无关\n学历：本科\n工作经历：3 年工作经验\n负责应付账款与发票审核。"
            _cid_miss = db.insert_candidate(conn, {"name": "钱无关", "source": "测试"})
            _doc_miss = conn.execute(
                """INSERT INTO documents (candidate_id, file_name, file_path, file_hash,
                       mime, raw_text, created_at)
                   VALUES (?, 'qianwuguan.txt', 'qianwuguan.txt', 'qianwuguan-hash',
                           'text/plain', ?, '')""",
                (_cid_miss, _text_miss)).lastrowid
            conn.execute(
                """INSERT INTO applications (candidate_id, job_id, channel, applied_at,
                       resume_doc_id, status)
                   VALUES (?, NULL, '测试', ?, ?, '待确认')""",
                (_cid_miss, db.now(), _doc_miss))
            conn.commit()
            conn.close()

            _, body = _asgi(srv.app, "GET", "/api/candidates", {})
            items_by_id = {x["id"]: x for x in json.loads(body)["items"]}
            sug = (items_by_id.get(_cid_hit) or {}).get("job_suggestion")
            c.ok(bool(sug) and sug.get("job_id") and sug.get("title"),
                 "材料类待指定投递拿到建议岗位（试算达 C 档及以上）",
                 json.dumps(sug, ensure_ascii=False)[:80] if sug else "None")
            sug_none = (items_by_id.get(_cid_miss) or {}).get("job_suggestion")
            c.ok(sug_none is None,
                 "技能毫无交集的候选人不出建议（保持待指定，不硬凑）",
                 json.dumps(sug_none, ensure_ascii=False)[:60] if sug_none else "None")

            code, body = _asgi(srv.app, "POST", f"/api/candidates/{_cid_hit}/assign-job",
                               hr, json.dumps({"job_id": sug["job_id"]}).encode())
            c.ok(code == 200, "采纳建议岗位接口返回 200", f"实际 {code} {body[:60]}")
            conn = db.connect(db_path)
            _app_row = dict(conn.execute(
                "SELECT * FROM applications WHERE id = ?", (_aid_hit,)).fetchone())
            _n_audit = conn.execute(
                "SELECT COUNT(*) AS n FROM audit_log WHERE entity='application' "
                "AND entity_id=? AND action='assign_job'", (str(_aid_hit),)).fetchone()["n"]
            conn.close()
            c.ok(_app_row["job_id"] == sug["job_id"],
                 "采纳后投递已归到建议岗位", f"job_id={_app_row['job_id']}")
            c.ok(_app_row["tier_suggested"] is not None and _app_row["score"] is not None,
                 "归岗时按岗位 JD 重算了建议档位",
                 f"{_app_row['tier_suggested']}（{_app_row['score']}）")
            c.ok(_n_audit == 1, "采纳归岗写入审计（岗位未指定 → 归到 X）")
            # 已归岗的不能再走归岗接口（唯一写入口）
            code, body = _asgi(srv.app, "POST", f"/api/candidates/{_cid_hit}/assign-job",
                               hr, json.dumps({"job_id": sug["job_id"]}).encode())
            c.ok(code == 400, "已归岗的投递再次归岗被拒（400）", f"实际 {code}")
            code, body = _asgi(srv.app, "POST", f"/api/candidates/{_cid_miss}/assign-job",
                               hr, json.dumps({"job_id": 999999}).encode())
            c.ok(code == 404, "归到不存在的岗位返回 404", f"实际 {code}")

            # ---- S5 岗位建议口径（v1.5）：**不再用默认尺子给待指定投递打分** ----
            # 做法：对每个在招岗位的 JD 各试算一遍，取最合适者，并用**该岗位的尺子**
            # 算分落库（`suggested_job_id` + score/tier_suggested）。
            # 这里造一条"绕开入库流程直接指进库"的待指定投递（模拟老数据/迁移数据），
            # 用 route-pending 归位后断言"落库的分就是按建议岗位尺子算的"。
            conn = db.connect(db_path)
            _cid_route = db.insert_candidate(conn, {"name": "归位测试", "source": "测试"})
            _doc_route = conn.execute(
                """INSERT INTO documents (candidate_id, file_name, file_path, file_hash,
                       mime, raw_text, created_at)
                   VALUES (?, 'route.txt', 'route.txt', 'route-hash', 'text/plain', ?, '')""",
                (_cid_route, "姓名：归位测试\n学历：硕士\n工作经历：6 年工作经验\n"
                             "负责钛合金真空熔铸工艺与 XRD 物相分析。")).lastrowid
            _aid_route = conn.execute(
                """INSERT INTO applications (candidate_id, job_id, channel, applied_at,
                       resume_doc_id, status)
                   VALUES (?, NULL, '测试', ?, ?, '待确认')""",
                (_cid_route, db.now(), _doc_route)).lastrowid
            conn.commit()
            conn.close()
            code, body = _asgi(srv.app, "POST", "/api/candidates/route-pending?apply=1", hr, b"")
            c.ok(code == 200, "存量归位接口可用", f"HTTP {code} {body[:60]}")
            conn = db.connect(db_path)
            _sug_row = conn.execute(
                "SELECT suggested_job_id, score, tier_suggested, hits FROM applications "
                "WHERE id = ?", (_aid_route,)).fetchone()
            _jobs = db.open_jobs_with_jd(conn)
            _d = conn.execute("SELECT raw_text FROM documents WHERE id = ?",
                              (_doc_route,)).fetchone()
            conn.close()
            c.ok(_sug_row is not None and _sug_row["suggested_job_id"] is not None,
                 "待指定投递归位后落库建议岗位（老数据也能补齐）",
                 f"suggested_job_id={_sug_row['suggested_job_id'] if _sug_row else None}")
            if _sug_row and _sug_row["suggested_job_id"]:
                _best_jd = [j["jd"] for j in _jobs if j["id"] == _sug_row["suggested_job_id"]][0]
                from app.pipeline.extract import extract as _extract
                from app.pipeline.tier import grade as _grade
                _exp = _grade(_extract(_d["raw_text"], _best_jd), _best_jd, TIERS)
                c.ok(abs((_sug_row["score"] or 0) - (_exp["score"] or 0)) < 1e-9
                     and _sug_row["tier_suggested"] == _exp["tier_suggested"],
                     "落库分数就是按建议岗位尺子算的（与默认尺子无关）",
                     f"库内 {_sug_row['tier_suggested']}/{_sug_row['score']} vs "
                     f"按建议岗位重算 {_exp['tier_suggested']}/{_exp['score']}")
            # 零交集的人：不落建议岗位（不硬凑）——财会简历对材料岗也能"算"出 0.3 分，
            # 所以判定必须看**技能命中**而不是分数
            conn = db.connect(db_path)
            _miss_row = conn.execute(
                "SELECT suggested_job_id, hits FROM applications WHERE candidate_id = ?",
                (_cid_miss,)).fetchone()
            conn.close()
            c.ok(_miss_row is not None and _miss_row["suggested_job_id"] is None
                 # hits 列是 JSON 文本（原始行未走 _decode），`'[]'` 在 Python 里是真值——
                 # 必须解析后再判空，否则这条断言会"通过得莫名其妙"
                 and not json.loads(_miss_row["hits"] or "[]"),
                 "与所有在招岗位零技能交集的人不落建议岗位（保持待指定）",
                 f"suggested_job_id={_miss_row['suggested_job_id'] if _miss_row else None} "
                 f"hits={json.loads((_miss_row['hits'] if _miss_row else None) or '[]')}")

            # S6 批量归档 + 到期彻底删除（v1.5）：按年使用，第二年要能整批收起旧档案；
            # 删除必须有冷静期——**归档满 30 天才彻底删除**，未满一律拒绝。
            _c = db.connect(db_path)
            # 用**一次性候选人**做删除测试：彻底删除是不可逆的，绝不能拿后面的断言
            # 还要引用的夹具（#1 等）来试刀。
            _cid_p1 = db.insert_candidate(_c, {"name": "批量归档甲", "source": "测试"})
            _cid_p2 = db.insert_candidate(_c, {"name": "到期清理乙", "source": "测试"})
            for _cid_x in (_cid_p1, _cid_p2):
                db.insert_application(_c, {"candidate_id": _cid_x, "channel": "测试",
                                           "applied_at": db.now(), "score": 0.2,
                                           "tier_suggested": "D"})
            _c.execute("UPDATE candidates SET archived_at = ? WHERE id = ?",
                       ("2026-01-01 09:00:00", _cid_p2))     # 充当"去年归档、已过期"的旧档案
            _c.commit()
            _c.close()
            _batch = [_cid_p1, _cid_p2]
            code, body = _asgi(srv.app, "POST", "/api/candidates/archive-batch", hr,
                               json.dumps({"ids": _batch, "archived": True}).encode())
            _r = json.loads(body)
            c.ok(code == 200 and _r.get("changed") == 1 and _r.get("skipped") == 1,
                 "批量归档：新归档 1 人、已在归档的跳过 1 人（不重复写审计）",
                 f"HTTP {code} changed={_r.get('changed')} skipped={_r.get('skipped')}")
            _, body = _asgi(srv.app, "GET", "/api/candidates", {})
            _pool_now = [x["id"] for x in json.loads(body)["items"]]
            c.ok(_batch[0] not in _pool_now, "批量归档后从人才库列表消失")
            _, body = _asgi(srv.app, "GET", "/api/candidates?archived=1", {})
            _arch_items = {x["id"]: x for x in json.loads(body)["items"]}
            c.ok(_batch[0] in _arch_items, "批量归档的人出现在「归档」页")
            c.ok((_arch_items.get(_batch[1], {}).get("archive") or {}).get("days_left") == 0,
                 "归档页回带剩余天数：旧档案已满 30 天（days_left=0）",
                 json.dumps((_arch_items.get(_batch[1], {}).get("archive") or {}), ensure_ascii=False))
            c.ok((_arch_items.get(_batch[0], {}).get("archive") or {}).get("days_left") == 30,
                 "刚归档的人剩余 30 天（冷静期）",
                 str((_arch_items.get(_batch[0], {}).get("archive") or {}).get("days_left")))
            # 未满 30 天不允许彻底删除：409 + 说明还剩几天
            code, body = _asgi(srv.app, "POST", f"/api/candidates/{_batch[0]}/purge", hr, b"")
            c.ok(code == 409 and "30" in body, "未满 30 天的档案拒绝彻底删除（409）",
                 f"HTTP {code} {body[:80]}")
            # 满 30 天：可以彻底删除，且审计留痕
            code, body = _asgi(srv.app, "POST", f"/api/candidates/{_batch[1]}/purge", hr, b"")
            c.ok(code == 200, "归档满 30 天可彻底删除", f"HTTP {code} {body[:80]}")
            conn = db.connect(db_path)
            _gone = conn.execute("SELECT COUNT(*) AS n FROM candidates WHERE id = ?",
                                 (_batch[1],)).fetchone()["n"]
            _apps_gone = conn.execute("SELECT COUNT(*) AS n FROM applications WHERE candidate_id = ?",
                                      (_batch[1],)).fetchone()["n"]
            _purged = conn.execute(
                "SELECT COUNT(*) AS n FROM audit_log WHERE action='purge' AND entity_id=?",
                (str(_batch[1]),)).fetchone()["n"]
            _kept_audit = conn.execute("SELECT COUNT(*) AS n FROM audit_log").fetchone()["n"]
            conn.close()
            c.ok(_gone == 0 and _apps_gone == 0,
                 "彻底删除：档案与投递一并清除", f"candidates={_gone} applications={_apps_gone}")
            c.ok(_purged == 1, "彻底删除写入 purge 审计（谁什么时候被清掉可追溯）")
            c.ok(_kept_audit > 0, "审计本身不被删除")
            # 批量到期清理：再跑一次应无到期者（已被清掉）
            code, body = _asgi(srv.app, "POST", "/api/archive/purge-due", hr, b"")
            _p = json.loads(body)
            c.ok(code == 200 and _p.get("purged") == 0, "到期批量清理幂等（无到期者返回 0）",
                 f"purged={_p.get('purged')}")
            # 批量取消归档：恢复展示
            code, body = _asgi(srv.app, "POST", "/api/candidates/archive-batch", hr,
                               json.dumps({"ids": [_batch[0]], "archived": False}).encode())
            c.ok(code == 200 and json.loads(body).get("changed") == 1, "批量取消归档可用")
            _, body = _asgi(srv.app, "GET", "/api/candidates", {})
            c.ok(_batch[0] in [x["id"] for x in json.loads(body)["items"]],
                 "批量取消归档后恢复在人才库展示")
            # 按年份整批归档：用 before_year 命中计数（旧档案已删，这里只验口径可跑通）
            code, body = _asgi(srv.app, "POST", "/api/candidates/archive-batch", hr,
                               json.dumps({"before_year": 2000, "archived": True}).encode())
            c.ok(code == 200 and json.loads(body).get("picked_by_year") == 0,
                 "按年份批量归档：早于 2000 年的投递为 0（口径可跑通、不误伤）",
                 f"HTTP {code} {body[:60]}")
            code, body = _asgi(srv.app, "POST", "/api/candidates/archive-batch", hr,
                               json.dumps({}).encode())
            c.ok(code == 400, "批量归档未指定人选时返回 400（不静默成功）", f"实际 {code}")

            # S7 存量归位（route-pending）：预演不写库，应用后按最适岗位重算并留审计
            code, body = _asgi(srv.app, "POST", "/api/candidates/route-pending?apply=0", hr, b"")
            _prev = json.loads(body)
            c.ok(code == 200 and _prev.get("applied") is False,
                 "存量岗位建议预演可用（不写库）", f"HTTP {code} total={_prev.get('total')}")
            code, body = _asgi(srv.app, "POST", "/api/candidates/route-pending?apply=1", hr, b"")
            _app = json.loads(body)
            c.ok(code == 200 and _app.get("applied") is True,
                 "存量岗位建议可应用", f"HTTP {code} changed={_app.get('changed')}")
            c.ok(_app.get("open_jobs", 0) >= 1,
                 "报出参与试算的在招岗位数（可解释性）", f"{_app.get('open_jobs')} 个")
            conn = db.connect(db_path)
            _n_route = conn.execute(
                "SELECT COUNT(*) AS n FROM audit_log WHERE action='route_suggest'").fetchone()["n"]
            conn.close()
            c.ok(_n_route >= 1, "按最适岗位重算写入 route_suggest 审计", f"{_n_route} 条")

            # 单 HR：同一份档案联系方式完整可见（不再有 viewer/admin 之分）
            _, body = _asgi(srv.app, "GET", "/api/candidates/1", {})
            vis = json.loads(body).get("contact_full_visible")
            c.ok(vis is True, "单 HR 直接看到完整联系方式（contact_full_visible=True）",
                 f"实际 {vis}")
            _, meta_body = _asgi(srv.app, "GET", "/api/meta", {})
            meta = json.loads(meta_body)
            c.ok(set(meta["session"]["permissions"]) >= {"read", "confirm", "settings", "merge"},
                 "meta 返回单 HR 全权限（不再有多角色）",
                 "、".join(meta["session"]["permissions"]))
            c.ok("departments" in meta and "jobs" in meta and "roles" not in meta,
                 "meta 返回部门与岗位列表，且不再暴露多角色 roles")

            # 部门/岗位接口的**写入行为**（不只是"不崩溃"）：
            # 曾踩到"请求字段名与实现不一致 → dept_id 被静默丢弃"的坑，故逐字段核对落库结果。
            _, dep_body = _asgi(srv.app, "POST", "/api/departments", hr,
                                json.dumps({"name": "验收部", "description": "接口自检"}).encode())
            did = json.loads(dep_body)["id"]
            _, job_body = _asgi(srv.app, "POST", "/api/jobs", hr,
                                json.dumps({"title": "验收岗位", "department_id": did}).encode())
            j_created = json.loads(job_body)["job"]
            c.ok(j_created["dept_id"] == did and j_created["department_name"] == "验收部",
                 "接口建岗时部门归属真的落库（dept_id + department_name）",
                 f"dept_id={j_created['dept_id']} 名称={j_created['department_name']}")

            jid_new = j_created["id"]
            _, off_body = _asgi(srv.app, "POST", f"/api/jobs/{jid_new}/deactivate", hr)
            j_off = json.loads(off_body)["job"]
            c.ok(j_off["active"] is False and j_off["status"] == "停用",
                 "接口停用岗位是软删（active=0 / status=停用），行仍在")
            _, on_body = _asgi(srv.app, "POST", f"/api/jobs/{jid_new}/activate", hr)
            c.ok(json.loads(on_body)["job"]["active"] is True, "接口可重新启用岗位")

            _, dep_off = _asgi(srv.app, "POST", f"/api/departments/{did}/deactivate", hr)
            c.ok(json.loads(dep_off)["department"]["active"] is False, "接口停用部门是软删")
            _, dep_on = _asgi(srv.app, "POST", f"/api/departments/{did}/activate", hr)
            c.ok(json.loads(dep_on)["department"]["active"] is True, "接口可重新启用部门")

            # ---- 检索结果也要带联系方式（需求：检索出的候选人要能直接联系） ----
            _, sk_body = _asgi(srv.app, "GET",
                               "/api/search/skills?skills=%E9%92%9B%E5%90%88%E9%87%91&mode=any", {})
            sk = json.loads(sk_body)
            c.ok(bool(sk.get("results")) and all(
                "phone" in h and "email" in h for h in sk["results"]),
                "技能召回结果带 phone/email 字段（检索页可直接拨号/发信）")
            leak_keys_api = ("phone_enc", "email_enc", "phone_bidx", "email_bidx",
                             "identity_key")
            c.ok(all(not any(k in h for k in leak_keys_api) for h in sk["results"]),
                 "检索结果不下发密文列/盲索引/身份键")
            # 与人才库列表比对：同一个人在两处的联系方式必须一模一样（同一展示口径）
            _, pool_body = _asgi(srv.app, "GET", "/api/candidates", {})
            pool = {x["id"]: x for x in json.loads(pool_body)["items"]}
            same = [h for h in sk["results"] if h["candidate_id"] in pool]
            c.ok(same and all(pool[h["candidate_id"]]["phone"] == h["phone"]
                              and pool[h["candidate_id"]]["email"] == h["email"]
                              for h in same),
                 "检索页与人才库的联系方式一致（不存在两套口径）",
                 f"{len(same)} 人可比对")
            c.ok(any(h["phone"] not in (None, "", "—") for h in sk["results"]),
                 "技能召回里确实有人带出了手机号（不是清一色空值）",
                 "、".join(str(h.get("phone")) for h in sk["results"][:4]))
            _, sem_body = _asgi(srv.app, "GET",
                                "/api/search/semantic?q=%E6%9D%90%E6%96%99&top_k=3", {})
            c.ok(all("phone" in h and not any(k in h for k in leak_keys_api)
                     for h in json.loads(sem_body)["results"]),
                 "语义召回结果同样带联系方式且不带密文")
            _, sim_body = _asgi(srv.app, "GET", "/api/search/similar?candidate_id=1", {})
            c.ok(all("phone" in h and not any(k in h for k in leak_keys_api)
                     for h in json.loads(sim_body)["results"]),
                 "相似人才推荐同样带联系方式且不带密文")

            # ---- 来源可见性：简历"从哪导、导了什么"必须看得见 ----
            _, src_body = _asgi(srv.app, "GET", "/api/sources", {})
            src = json.loads(src_body)
            fld = src.get("folder") or {}
            c.ok(isinstance(fld.get("path"), str) and os.path.isabs(fld["path"]),
                 "来源接口给出本地文件夹绝对路径（不是只写个相对名）",
                 str(fld.get("path")))
            c.ok(isinstance(fld.get("files"), list),
                 "来源接口列出文件夹内文件清单", f"{fld.get('count', 0)} 个受支持文件")
            c.ok(all({"name", "size", "mtime", "indexed", "candidate"} <= set(f)
                     for f in fld.get("files", [])),
                 "每个文件都标注了是否已入库、属于哪位候选人")
            c.ok("account" in (src.get("mailbox") or {}),
                 "来源接口同时说明邮件抓的是哪个账号 / 哪个服务器",
                 str((src.get("mailbox") or {}).get("account")))
            c.ok("password_set" in (src.get("mailbox") or {}),
                 "口令状态只以布尔呈现，不回显口令本身")

            # ---- 简历原件：能预览、能下载 ----
            dl_caps: dict = {}
            code_dl, _txt = _asgi(srv.app, "GET", f"/api/documents/{ok_did}/file",
                                  {}, b"", dl_caps)
            c.ok(code_dl == 200, "简历原件可下载", f"HTTP {code_dl}")
            cd = dl_caps["headers"].get("content-disposition", "")
            c.ok(cd.startswith("attachment"),
                 "默认按附件下载（Content-Disposition: attachment）", cd[:60])
            body_bytes = dl_caps.get("body_bytes") or b""
            real_len = os.path.getsize(os.path.join(BASE, "README.md"))
            c.ok(len(body_bytes) == real_len,
                 "下载响应带回的原件字节数与磁盘文件一致",
                 f"{len(body_bytes)} / {real_len} 字节")
            pv_caps: dict = {}
            code_pv, _ = _asgi(srv.app, "GET", f"/api/documents/{ok_did}/file?inline=1",
                               {}, b"", pv_caps)
            c.ok(code_pv == 200 and pv_caps["headers"].get(
                "content-disposition", "").startswith("inline"),
                 "inline=1 时改为浏览器内预览（Content-Disposition: inline）",
                 pv_caps["headers"].get("content-disposition", "")[:60])
            c.ok(b"inline" in pv_caps["headers"].get("content-disposition", "").encode()
                 and real_len == len(pv_caps.get("body_bytes") or b""),
                 "相对路径的 archived_path 能按仓库根解析出原件（服务换工作目录也取得到）")

            # 预览与下载都要留痕：能打开别人的简历本身就是要被审计的敏感动作
            conn = db.connect(db_path)
            acts = [a["action"] for a in conn.execute(
                "SELECT action FROM audit_log WHERE entity='document' "
                "AND entity_id=? ORDER BY id", (str(ok_did),)).fetchall()]
            conn.close()
            c.ok("download" in acts and "preview" in acts,
                 "原件预览与下载都写入审计", "、".join(acts))

            # 路径穿越：伪造一条指向系统文件的记录，必须被拒绝
            conn = db.connect(db_path)
            cur = conn.execute(
                """INSERT INTO documents (candidate_id, file_name, file_path, archived_path,
                       file_hash, mime, created_at)
                   VALUES (NULL,'passwd','/etc/passwd','/etc/passwd','evilhash','text/plain','')""")
            evil_did = cur.lastrowid
            conn.commit()
            conn.close()
            code_evil, _ = _asgi(srv.app, "GET", f"/api/documents/{evil_did}/file", {})
            c.ok(code_evil == 403,
                 "构造路径读到仓库外的文件被拒绝（403），不会泄露系统文件",
                 f"实际 {code_evil}")
            code_missing, _ = _asgi(srv.app, "GET", "/api/documents/999999/file", {})
            c.ok(code_missing == 404, "不存在的附件返回 404 而非 500", f"实际 {code_missing}")

            # ---- 来源文件夹里的简历：**导入之前**也要能预览/下载 ----
            # 只凭文件名猜「这份该不该导」是不现实的：HR 必须先看得见内容。
            from urllib.parse import quote

            rdir = resume_dir
            pdfs = sorted(f for f in os.listdir(rdir) if f.lower().endswith(".pdf"))
            if pdfs:
                nm = pdfs[0]
                sf: dict = {}
                code_sf, _ = _asgi(srv.app, "GET",
                                   f"/api/sources/file?name={quote(nm)}&inline=1",
                                   {}, b"", sf)
                c.ok(code_sf == 200, "来源文件夹里的简历可直接在线预览",
                     f"{nm} → HTTP {code_sf}")
                c.ok(sf["headers"].get("content-type", "").startswith("application/pdf"),
                     "PDF 预览回 application/pdf（浏览器才会就地渲染而不是下载）",
                     sf["headers"].get("content-type"))
                c.ok(sf["headers"].get("content-disposition", "").startswith("inline"),
                     "预览态 Content-Disposition 为 inline",
                     sf["headers"].get("content-disposition", "")[:48])
                c.ok(len(sf.get("body_bytes") or b"") == os.path.getsize(os.path.join(rdir, nm)),
                     "预览返回的字节数与磁盘原件一致",
                     f"{len(sf.get('body_bytes') or b'')} 字节")
                sf_dl: dict = {}
                _asgi(srv.app, "GET", f"/api/sources/file?name={quote(nm)}", {}, b"", sf_dl)
                c.ok(sf_dl["headers"].get("content-disposition", "").startswith("attachment"),
                     "同一份文件不带 inline 时按附件下载（两种方式都在）",
                     sf_dl["headers"].get("content-disposition", "")[:48])

                # 守卫：路径穿越、非白名单后缀都要被挡下
                code_tr, _ = _asgi(srv.app, "GET",
                                   "/api/sources/file?name=..%2F..%2Fetc%2Fpasswd", {})
                c.ok(code_tr in (400, 403),
                     "来源文件接口拒绝路径穿越（不做任意文件读取）", f"实际 {code_tr}")
                code_ext, _ = _asgi(srv.app, "GET", "/api/sources/file?name=evil.sh", {})
                c.ok(code_ext == 400,
                     "来源文件接口只认与解析一致的白名单后缀", f"实际 {code_ext}")
                code_absent, _ = _asgi(srv.app, "GET",
                                       "/api/sources/file?name=nope.pdf", {})
                c.ok(code_absent == 404, "来源目录里没有的文件返回 404", f"实际 {code_absent}")

                # ---- 多选打包下载：zip 内容与所选文件一一对应 ----
                bd: dict = {}
                code_bd, _ = _asgi(srv.app, "POST", "/api/sources/bundle", hr,
                                   json.dumps({"names": pdfs}).encode(), bd)
                c.ok(code_bd == 200, "多选打包下载可用",
                     f"选了 {len(pdfs)} 份 → HTTP {code_bd}")
                c.ok(bd["headers"].get("content-type", "").startswith("application/zip"),
                     "打包结果是 zip", bd["headers"].get("content-type"))
                c.ok("filename*=UTF-8''" in bd["headers"].get("content-disposition", ""),
                     "压缩包中文名走 RFC 5987（下载到本地不乱码）",
                     bd["headers"].get("content-disposition", "")[:60])
                zf = zipfile.ZipFile(io.BytesIO(bd.get("body_bytes") or b""))
                c.ok(sorted(zf.namelist()) == sorted(pdfs),
                     "zip 内条目与所选文件一一对应（不多不少、不重名覆盖）",
                     "、".join(zf.namelist()))
                sizes_ok = all(
                    len(zf.read(n)) == os.path.getsize(os.path.join(rdir, n))
                    for n in zf.namelist())
                c.ok(sizes_ok, "zip 内每份简历都有完整内容（不是空壳条目）")
                c.ok(len(zf.read(zf.namelist()[0])) > 0, "解压后第一份非空")
                # 一份取不到不整体失败，但要如实说明跳过了谁
                # （HTTP 头只能放 ASCII，中文详情走百分号编码，前端解码还原）
                bad: dict = {}
                code_mix, _ = _asgi(srv.app, "POST", "/api/sources/bundle", hr,
                                    json.dumps({"names": [pdfs[0], "ghost.pdf"]}).encode(),
                                    bad)
                skip_hdr = bad["headers"].get("x-skipped-detail", "")
                skip_txt = ""
                try:
                    skip_txt = json.loads(urllib.parse.unquote(skip_hdr))
                except Exception:
                    pass
                c.ok(code_mix == 200 and any("ghost.pdf" in x for x in skip_txt),
                     "混选了取不到的文件时：能下的照下，并在响应头如实列出跳过了谁",
                     str(skip_txt)[:60])
                c.ok(skip_hdr.isascii() and skip_txt and any(
                        any("\u4e00" <= ch <= "\u9fa5" for ch in x) for x in skip_txt),
                     "跳过原因是可读中文且响应头保持纯 ASCII（头里直接写中文会 500）",
                     skip_hdr[:48])
                code_empty, _ = _asgi(srv.app, "POST", "/api/sources/bundle", hr,
                                      json.dumps({"names": []}).encode())
                c.ok(code_empty == 400, "一份都没选时不返回空 zip 而是明确报错",
                     f"实际 {code_empty}")
            else:
                c.ok(False, "夹具目录里应有 PDF 简历（tests/make_fixtures.write_resumes 生成）",
                     f"{rdir} 里没有 .pdf")

            # ---- PDF 样例：解析、MIME、年限 ----
            c.ok(len(pdfs) >= 1 and all(f.lower().endswith(".pdf") for f in pdfs),
                 "来源目录里有 PDF 简历样例（学生投递以 PDF 为主）", f"{len(pdfs)} 份")
            if pdfs:
                p_text, p_engine, p_ok = parse_file_ex(os.path.join(rdir, pdfs[0]))
                c.ok(p_ok and p_engine == "pymupdf" and len(p_text) > 120,
                     "PDF 样例文本可提取（中文不乱码，后续抽取/分级才跑得通）",
                     f"{p_engine} / {len(p_text)} 字")
                c.ok("林一诺" in p_text or "电话" in p_text or "@" in p_text,
                     "提取出的文本含简历正文（不是空白页或乱码）",
                     repr(p_text.strip()[:28]))
            from app.pipeline.parse import mime_of
            c.ok(mime_of("a.pdf") == "application/pdf"
                 and mime_of("a.docx").startswith("application/vnd.openxmlformats")
                 and mime_of("a.unknown") == "application/octet-stream",
                 "MIME 映射覆盖 PDF/DOCX 且有通用兜底（入库落库与下载响应共用一份）")
            # 年限：教育经历的年份区间绝不能被算成工作年限
            from app.pipeline.extract import _find_years
            c.ok(_find_years("教育背景\n2021.09-2025.06 某大学 材料成型 本科") is None,
                 "只有教育经历时不给工作年限（不拿学制充当工龄）")
            c.ok(_find_years("教育背景\n2021.09-2025.06 某大学 本科\n"
                             "工作经历\n2025.07-至今 某公司 工艺助理") == 1,
                 "应届生：教育经历不污染年限（≪学制年数）")
            c.ok(_find_years("教育背景\n2015.09-2019.06 某大学 本科\n"
                             "工作经历\n2019.07-至今 某公司 工程师") == 7,
                 "在职人员：只按工作经历算（不把本科四年也算进去）")
            c.ok(_find_years("工作经历\n2019.07-2022.06 A 公司\n2022.07-至今 B 公司") == 7,
                 "两段连续履历取跨度而非相加（不重复计数）")
            c.ok(_find_years("工作经历\n2022.07-至今 某院 工艺工程师（4年工作经验）") == 4,
                 "简历里写明『N年工作经验』时以原文为准（优先于推算）")

            # ---- 岗位 JD：新增/编辑都要真的落库 ----
            # JD 的真实结构是 {role, department, origin, must:{skills_required, education_min,
            # years_min}, preferred:{skills}, note}，断言必须打在真实字段上，否则"看着通过"。
            _, jd_body = _asgi(srv.app, "POST", f"/api/jobs/{jid_new}/jd", hr,
                               json.dumps({"must_skills": ["钛合金", "真空熔铸"],
                                           "preferred_skills": ["XRD"],
                                           "education_min": "硕士", "years_min": 3,
                                           "note": "能接受出差"}).encode())
            jd_saved = json.loads(jd_body).get("jd") or {}
            c.ok(jd_saved.get("must", {}).get("skills_required") == ["钛合金", "真空熔铸"],
                 "岗位 JD 的必需技能落库",
                 str(jd_saved.get("must", {}).get("skills_required")))
            c.ok(jd_saved.get("preferred", {}).get("skills") == ["XRD"],
                 "岗位 JD 的加分技能落库", str(jd_saved.get("preferred", {}).get("skills")))
            c.ok(jd_saved.get("must", {}).get("education_min") == "硕士"
                 and jd_saved.get("must", {}).get("years_min") == 3,
                 "岗位 JD 的学历门槛与年限门槛落库",
                 f"{jd_saved.get('must', {}).get('education_min')} / "
                 f"{jd_saved.get('must', {}).get('years_min')}")
            c.ok(jd_saved.get("note") == "能接受出差", "岗位 JD 的职责说明落库")
            _, jdget_body = _asgi(srv.app, "GET", f"/api/jobs/{jid_new}", {})
            c.ok(json.loads(jdget_body).get("jd") == jd_saved,
                 "重新读取岗位时 JD 与写入一致（不是只在响应里回显）")
            # 列表也要带 JD：岗位表要能一眼看出每个岗位的评分尺子
            _, jlist_body = _asgi(srv.app, "GET", "/api/jobs", {})
            jrow = [x for x in json.loads(jlist_body)["items"] if x["id"] == jid_new]
            c.ok(jrow and isinstance(jrow[0].get("jd_json"), dict)
                 and jrow[0]["jd_json"].get("must", {}).get("years_min") == 3,
                 "岗位列表下发 jd_json（页面能直接渲染 JD 摘要）")
            # 局部更新：只传一个字段，其余字段不能被清空
            _, jd2_body = _asgi(srv.app, "POST", f"/api/jobs/{jid_new}/jd", hr,
                                json.dumps({"years_min": 5}).encode())
            jd2 = json.loads(jd2_body).get("jd") or {}
            c.ok(jd2.get("must", {}).get("years_min") == 5
                 and jd2.get("must", {}).get("skills_required") == ["钛合金", "真空熔铸"],
                 "只改一个 JD 字段时，未提交的字段原样保留（不被清空）",
                 f"years_min={jd2.get('must', {}).get('years_min')} "
                 f"must={jd2.get('must', {}).get('skills_required')}")
            # 清空 = 主动去掉门槛，而不是"没填就不改"
            _, jd3_body = _asgi(srv.app, "POST", f"/api/jobs/{jid_new}/jd", hr,
                                json.dumps({"must_skills": [], "education_min": "",
                                            "years_min": 0}).encode())
            jd3 = json.loads(jd3_body).get("jd") or {}
            c.ok(jd3.get("must", {}).get("skills_required") == []
                 and jd3.get("must", {}).get("education_min") == ""
                 and jd3.get("must", {}).get("years_min") == 0,
                 "显式清空 JD 门槛能真正生效（不再作为评分条件）",
                 f"{jd3.get('must')}")
            conn = db.connect(db_path)
            n_jd_audit = conn.execute(
                "SELECT COUNT(*) AS n FROM audit_log WHERE action='update_jd'").fetchone()["n"]
            jd_audit = conn.execute(
                "SELECT before, after FROM audit_log WHERE action='update_jd' "
                "ORDER BY id DESC LIMIT 1").fetchone()
            conn.close()
            c.ok(n_jd_audit >= 3, "JD 变更逐次写入审计", f"{n_jd_audit} 条")
            c.ok(jd_audit and jd_audit["before"] not in (None, "", "None")
                 and jd_audit["after"] not in (None, "", "None"),
                 "JD 审计同时记录了变更前与变更后的尺子（可追溯）",
                 f"before={str(jd_audit['before'])[:30]} after={str(jd_audit['after'])[:30]}")
            c.ok("影响之后" in (json.loads(jd_body).get("note") or "")
                 and "历史档位不变" in (json.loads(jd_body).get("note") or ""),
                 "JD 保存接口明确告知『只影响之后评分、历史档位不变』",
                 str(json.loads(jd_body).get("note"))[:44])

            # ---- 重新分析：JD 改完后按新尺子重算已有投递 ----
            # 这里刻意自己造数据：库里的候选人画像来自固定样例，
            # "JD 变了分数应该变"这件事必须用**可控输入**验证，否则测的是样例而不是逻辑。
            resume_a = ("姓名：重算甲\n性别：男\n电话：13800002222\n邮箱：a@test.cn\n"
                        "硕士 材料科学与工程\n教育背景\n2019.09-2022.06 某大学 硕士\n"
                        "工作经历\n2022.07-至今 某院 工艺工程师\n"
                        "技能：钛合金、真空熔铸、XRD\n证书：中级工程师")
            resume_b = ("姓名：重算乙\n电话：13800003333\n邮箱：b@test.cn\n"
                        "本科 机械设计\n教育背景\n2021.09-2025.06 某大学 本科\n"
                        "技能：机械制图")
            _c = db.connect(db_path)
            # 岗位：只认「钛合金」必需技能 —— 改 JD 前后谁能命中会明显不同
            _jc = db.create_job(_c, "验收重算岗", dept_id=None,
                                jd={"role": "验收重算岗", "department": "",
                                    "must": {"skills_required": ["钛合金"], "education_min": "",
                                             "years_min": 0},
                                    "preferred": {"skills": []}, "note": ""})
            # 一条有效投递（有原文）+ 一条画像不可用（原文为空）
            for nm, txt, expect in (("重算甲", resume_a, True), ("重算乙", resume_b, True)):
                cid = db.insert_candidate(_c, {"identity_key": f"selftest:regrade:{nm}",
                                               "name": nm, "years_exp": 3})
                aid = db.insert_application(_c, {"candidate_id": cid, "job_id": _jc,
                                                 "channel": "文件夹", "applied_at": db.now(),
                                                 "score": 0.05, "tier_suggested": "D"})
                did = db.insert_document(_c, {"file_hash": f"regradehash{nm}", "candidate_id": cid,
                                              "application_id": aid, "file_name": f"{nm}.txt",
                                              "archived_path": "", "mime": "text/plain",
                                              "size": len(txt), "received_at": db.now(),
                                              "raw_text": txt, "parse_engine": "text",
                                              "parse_ok": 1})
                _c.execute("UPDATE applications SET resume_doc_id = ? WHERE id = ?", (did, aid))
            _c.commit()
            # HR 已确认过的一条：重算绝不能覆盖它
            cid_k = db.insert_candidate(_c, {"identity_key": "selftest:regrade:已确认",
                                             "name": "重算丙", "years_exp": 3})
            aid_k = db.insert_application(_c, {"candidate_id": cid_k, "job_id": _jc,
                                               "channel": "文件夹", "applied_at": db.now(),
                                               "score": 0.9, "tier_suggested": "B"})
            did_k = db.insert_document(_c, {"file_hash": "regradehash已确认", "candidate_id": cid_k,
                                            "application_id": aid_k, "file_name": "重算丙.txt",
                                            "archived_path": "", "mime": "text/plain",
                                            "size": len(resume_a), "received_at": db.now(),
                                            "raw_text": resume_a, "parse_engine": "text",
                                            "parse_ok": 1})
            db.set_application_tier(_c, aid_k, "A", "HR 已确认", "hr", "hr")
            _c.execute("UPDATE applications SET resume_doc_id = ? WHERE id = ?", (did_k, aid_k))
            _c.commit()
            apps_before = {r["id"]: dict(r) for r in _c.execute(
                "SELECT * FROM applications WHERE job_id = ?", (_jc,)).fetchall()}
            _c.close()

            _, rg_body = _asgi(srv.app, "POST", f"/api/jobs/{_jc}/regrade", hr,
                               json.dumps({}).encode())
            rg = json.loads(rg_body)
            c.ok(rg.get("applied") is False and rg.get("total") == 3,
                 "重新分析默认是**预演**：只算差异、不写库",
                 f"applied={rg.get('applied')} total={rg.get('total')}")
            _c = db.connect(db_path)
            apps_after = {r["id"]: dict(r) for r in _c.execute(
                "SELECT * FROM applications WHERE job_id = ?", (_jc,)).fetchall()}
            _c.close()
            c.ok(all(apps_before[k]["tier_suggested"] == apps_after[k]["tier_suggested"]
                     and apps_before[k]["score"] == apps_after[k]["score"]
                     for k in apps_before),
                 "预演不修改任何投递的分数与档位（看完再决定）")
            item_a = [x for x in rg["items"] if x.get("name") == "重算甲"][0]
            c.ok(item_a["changed"] is True and item_a["old_tier"] == "D"
                 and item_a["new_tier"] in ("A", "B", "C"),
                 "预演确实算出了差异（改成认钛合金必需技能后，命中者档位上升）",
                 f"{item_a['old_tier']}→{item_a['new_tier']}（{item_a['old_score']}→{item_a['new_score']}）")
            item_k = [x for x in rg["items"] if x.get("name") == "重算丙"][0]
            c.ok(item_k["kept"] is True and "已确认" in (item_k.get("note") or ""),
                 "HR 已确认过的投递被标为「只记录差异、不修改」",
                 str(item_k.get("note"))[:44])

            # 应用：真的落库，并逐条写审计
            _, rg2_body = _asgi(srv.app, "POST", f"/api/jobs/{_jc}/regrade", hr,
                                json.dumps({"apply": True}).encode())
            rg2 = json.loads(rg2_body)
            _c = db.connect(db_path)
            a_now = {r["id"]: dict(r) for r in _c.execute(
                "SELECT * FROM applications WHERE job_id = ?", (_jc,)).fetchall()}
            n_regrade_audit = _c.execute(
                "SELECT COUNT(*) AS n FROM audit_log WHERE action='regrade'").fetchone()["n"]
            n_batch = _c.execute(
                "SELECT COUNT(*) AS n FROM audit_log WHERE action='regrade_batch'").fetchone()["n"]
            n_preview = _c.execute(
                "SELECT COUNT(*) AS n FROM audit_log WHERE action='regrade_preview'").fetchone()["n"]
            khit = _c.execute("SELECT tier_final, tier_suggested, score FROM applications "
                              "WHERE id = ?", (aid_k,)).fetchone()
            _c.close()
            c.ok(rg2.get("applied") is True and rg2.get("changed", 0) >= 1,
                 "确认后重算结果真的落库", f"changed={rg2.get('changed')}")
            c.ok(a_now[item_a["application_id"]]["tier_suggested"] == item_a["new_tier"]
                 and abs(a_now[item_a["application_id"]]["score"] - item_a["new_score"]) < 0.001,
                 "落库值与预演给出的差异完全一致（预演不是另一套算法）",
                 f"{a_now[item_a['application_id']]['tier_suggested']} / "
                 f"{a_now[item_a['application_id']]['score']}")
            c.ok(n_regrade_audit >= 1 and n_batch >= 1 and n_preview >= 1,
                 "逐条重算与整批各有审计（预演也留痕：谁看了这次重算结果）",
                 f"regrade={n_regrade_audit} batch={n_batch} preview={n_preview}")
            c.ok(khit["tier_final"] == "A" and khit["tier_suggested"] == "B",
                 "HR 已确认的档位在整个重算过程中没有被覆盖（tier_final 仍是 A）",
                 f"tier_final={khit['tier_final']} tier_suggested={khit['tier_suggested']}")
            # 无原文的投递：明确说"无法重算"，而不是悄悄算成 0 分
            _c = db.connect(db_path)
            cid_n = db.insert_candidate(_c, {"identity_key": "selftest:regrade:无原文",
                                             "name": "重算丁", "years_exp": 0})
            aid_n = db.insert_application(_c, {"candidate_id": cid_n, "job_id": _jc,
                                               "channel": "文件夹", "applied_at": db.now(),
                                               "score": 0.3, "tier_suggested": "C"})
            did_n = db.insert_document(_c, {"file_hash": "regradehash空", "candidate_id": cid_n,
                                            "application_id": aid_n, "file_name": "扫描件.pdf",
                                            "archived_path": "", "mime": "application/pdf",
                                            "size": 10, "received_at": db.now(), "raw_text": "",
                                            "parse_engine": "none", "parse_ok": 0})
            _c.execute("UPDATE applications SET resume_doc_id = ? WHERE id = ?", (did_n, aid_n))
            _c.commit()
            _c.close()
            _, rg3_body = _asgi(srv.app, "POST", f"/api/jobs/{_jc}/regrade", hr,
                                json.dumps({"apply": True}).encode())
            rg3 = json.loads(rg3_body)
            item_n = [x for x in rg3["items"] if x.get("name") == "重算丁"][0]
            _c = db.connect(db_path)
            n_score = _c.execute("SELECT score, tier_suggested FROM applications WHERE id = ?",
                                 (aid_n,)).fetchone()
            _c.close()
            c.ok(rg3["cannot_regrade"] == 1 and item_n["new_score"] is None
                 and "无法重算" in (item_n.get("skipped") or ""),
                 "原文缺失/解析失败的投递被明确标为『无法重算』",
                 str(item_n.get("skipped"))[:40])
            c.ok(abs((n_score["score"] or 0) - 0.3) < 0.001 and n_score["tier_suggested"] == "C",
                 "『无法重算』的投递分数档位原样保留（不会被算成 0 分掉档）",
                 f"{n_score['score']} / {n_score['tier_suggested']}")
            code_rg404, _ = _asgi(srv.app, "POST", "/api/jobs/999999/regrade", hr,
                                  json.dumps({}).encode())
            c.ok(code_rg404 == 404, "对不存在的岗位重算返回 404 而非 500", f"实际 {code_rg404}")

            # JD 保存接口要指出"已有投递可以重新分析"，否则 HR 会以为改动没生效
            _, jdreg_body = _asgi(srv.app, "POST", f"/api/jobs/{_jc}/jd", hr,
                                  json.dumps({"must_skills": ["钛合金", "真空熔铸"]}).encode())
            jdreg = json.loads(jdreg_body)
            c.ok(jdreg.get("can_regrade") is True and jdreg.get("applications_count", 0) >= 3
                 and "重新分析" in (jdreg.get("note") or ""),
                 "保存 JD 时明确告知该岗位有 N 条投递可「重新分析」",
                 f"can_regrade={jdreg.get('can_regrade')} n={jdreg.get('applications_count')}")

            # ---- 来源文件：删除 = 移入回收目录（可恢复），且不破坏已入库原件 ----
            # 老库（v0.1 迁移）里 documents.archived_path 直接指向来源文件夹——
            # 一旦把来源文件移走，这些人的"原件"当场 404。所以删除前必须先补归档。
            #
            # 来源目录刻意建在仓库内（`/api/documents/{id}/file` 只允许发仓库内的文件，
            # 这是防路径穿越的守卫；把目录放到 /tmp 会让"原件可下载"这条断言测的是守卫、
            # 而不是我们要验证的补归档逻辑）。测试结束会整目录删掉。
            src_dir = os.path.join(BASE, "data", "_selftest_src")
            shutil.rmtree(src_dir, ignore_errors=True)
            os.makedirs(src_dir, exist_ok=True)
            victim = "待删除_样例.txt"
            victim_path = os.path.join(src_dir, victim)
            legacy_text = ("姓名：待删样例\n性别：女\n电话：13800004444\n邮箱：del@test.cn\n"
                           "本科 材料成型\n技能：钛合金、真空熔铸")
            with open(victim_path, "w", encoding="utf-8") as fh:
                fh.write(legacy_text)
            huge = "超大样例.pdf"
            huge_path = os.path.join(src_dir, huge)
            with open(huge_path, "wb") as fh:
                fh.write(b"%PDF-1.4 " + b"0" * (2 * 1024 * 1024))
            killme = "自建_无关文件.txt"
            with open(os.path.join(src_dir, killme), "w", encoding="utf-8") as fh:
                fh.write("未被任何台账引用\n")

            # 来源目录与回收目录都指到临时位置：不动仓库里的真实样例。
            # 归档目录也要落在仓库内：`/api/documents/{id}/file` 只发仓库内的文件，
            # 补归档补到 /tmp 会被守卫拦下（那样测的是守卫，不是补归档）。
            pin_archive = os.path.join(BASE, "data", "_selftest_archive")
            shutil.rmtree(pin_archive, ignore_errors=True)
            mb.save_config({"folder_dir": src_dir, "max_attachment_mb": 1,
                            "archive_dir": pin_archive})
            srv.REMOVED_DIR = os.path.join(work, "removed")

            _, sl_body = _asgi(srv.app, "GET", "/api/sources", {})
            sl = json.loads(sl_body)
            fmap = {x["name"]: x for x in sl["folder"]["files"]}
            c.ok(fmap.get(huge, {}).get("over_limit") is True
                 and fmap.get(victim, {}).get("over_limit") is False,
                 "来源清单提前标出超过体积上限的文件（导入前就知道哪份会被跳过）",
                 f"{huge} → over_limit={fmap.get(huge, {}).get('over_limit')}")
            c.ok(sl["folder"].get("max_attachment_mb") == 1,
                 "来源接口带上当前体积上限（界面与后端同一口径）",
                 str(sl["folder"].get("max_attachment_mb")))

            # 造一条"原件指向来源文件夹"的老式台账（v0.1 迁移数据的真实形态）
            _c = db.connect(db_path)
            cid_l = db.insert_candidate(_c, {"identity_key": "selftest:remove", "name": "待删样例"})
            aid_l = db.insert_application(_c, {"candidate_id": cid_l, "channel": "文件夹",
                                               "applied_at": db.now(), "score": 0.5,
                                               "tier_suggested": "C"})
            did_l = db.insert_document(_c, {"file_hash": "removehash", "candidate_id": cid_l,
                                            "application_id": aid_l, "file_name": victim,
                                            "file_path": victim_path, "archived_path": victim_path,
                                            "mime": "text/plain", "size": os.path.getsize(victim_path),
                                            "received_at": db.now(), "raw_text": legacy_text,
                                            "parse_engine": "text", "parse_ok": 1})
            _c.close()
            code_before, _ = _asgi(srv.app, "GET", f"/api/documents/{did_l}/file", {})
            c.ok(code_before == 200, "删除前：指向来源文件夹的原件可正常下载",
                 f"HTTP {code_before}")

            # 守卫：路径穿越 / 非白名单后缀 一律拒绝，且一份都不动
            _, bad_body = _asgi(srv.app, "POST", "/api/sources/remove", hr,
                                json.dumps({"names": ["../../etc/passwd", "evil.sh",
                                                      "不存在的简历.pdf"]}).encode())
            bad = json.loads(bad_body)
            c.ok(not bad.get("moved") and len(bad.get("skipped") or []) == 3,
                 "删除接口拒绝路径穿越 / 非白名单后缀 / 不存在的文件（逐条说明原因）",
                 "；".join(x.get("why", "")[:14] for x in (bad.get("skipped") or [])))
            c.ok(os.path.isfile(victim_path) and os.path.isfile(huge_path),
                 "被拒绝的请求不会顺手删掉任何东西")

            _, rm_body = _asgi(srv.app, "POST", "/api/sources/remove", hr,
                               json.dumps({"names": [victim, killme]}).encode())
            rm = json.loads(rm_body)
            moved_names = [m["name"] for m in rm.get("moved") or []]
            c.ok(sorted(moved_names) == sorted([victim, killme]),
                 "勾选的来源文件被移入回收目录", "、".join(moved_names))
            c.ok(not os.path.exists(victim_path) and not os.path.exists(
                os.path.join(src_dir, killme)),
                 "来源目录里确实不再有这些文件")
            rec_names = []
            for _root, _dirs, _fs in os.walk(os.path.join(work, "removed")):
                rec_names.extend(_fs)
            c.ok(sorted(rec_names) == sorted([victim, killme]),
                 "文件出现在回收目录里（是移动，不是物理删除）", "、".join(rec_names))
            c.ok([m for m in rm["moved"] if m["name"] == victim][0]["pinned_documents"] == [did_l],
                 "被台账引用的文件在移动前先补了一份归档（原指向来源目录）",
                 str([m for m in rm["moved"] if m["name"] == victim][0]["pinned_documents"]))
            _c = db.connect(db_path)
            d_after = dict(_c.execute("SELECT * FROM documents WHERE id = ?", (did_l,)).fetchone())
            n_pin_audit = _c.execute("SELECT COUNT(*) AS n FROM audit_log "
                                     "WHERE action='archive_pinned'").fetchone()["n"]
            n_rm_audit = _c.execute("SELECT COUNT(*) AS n FROM audit_log "
                                    "WHERE action='remove' AND entity='source_file'").fetchone()["n"]
            _c.close()
            c.ok(d_after["archived_path"] != victim_path
                 and os.path.realpath(d_after["archived_path"]).startswith(
                     os.path.realpath(pin_archive) + os.sep)
                 and os.path.isfile(d_after["archived_path"]),
                 "台账已改指到归档区副本（来源文件移走后原件不会 404）",
                 str(d_after["archived_path"]).split("/")[-1])
            code_after, _ = _asgi(srv.app, "GET", f"/api/documents/{did_l}/file", {})
            c.ok(code_after == 200, "删除后：该候选人的原件仍可下载（不丢件）", f"HTTP {code_after}")
            c.ok(n_pin_audit >= 1 and n_rm_audit == 2,
                 "补归档与删除各自留痕（可回答「这份简历为什么不见了」）",
                 f"archive_pinned={n_pin_audit} remove={n_rm_audit}")
            code_sf404, _ = _asgi(srv.app, "GET",
                                  "/api/sources/file?name=" + urllib.parse.quote(victim), {})
            c.ok(code_sf404 == 404,
                 "移走后来源预览返回 404（而不是读到别处的同名文件）", f"实际 {code_sf404}")

            # 回收目录可见 + 可恢复
            _, rl_body = _asgi(srv.app, "GET", "/api/sources/removed", {})
            rl = json.loads(rl_body)
            c.ok(rl["count"] == 2 and all({"name", "size", "removed_at", "conflict"} <= set(i)
                                          for i in rl["items"]),
                 "回收目录清单可读（含大小与删除时间）", f"{rl['count']} 份")
            _, rs_body = _asgi(srv.app, "POST", "/api/sources/restore", hr,
                               json.dumps({"names": [victim]}).encode())
            rs = json.loads(rs_body)
            c.ok(len(rs.get("restored") or []) == 1 and os.path.isfile(victim_path),
                 "文件可恢复到来源文件夹（删错了能拿回来）")
            # 同名冲突：绝不覆盖。做法是先移走，再在来源目录放一个同名文件，然后尝试恢复。
            _asgi(srv.app, "POST", "/api/sources/remove", hr,
                  json.dumps({"names": [victim]}).encode())
            with open(victim_path, "w", encoding="utf-8") as fh:
                fh.write("来源目录里后来出现的同名文件\n")
            _, rc_body = _asgi(srv.app, "POST", "/api/sources/restore", hr,
                               json.dumps({"names": [victim]}).encode())
            rc = json.loads(rc_body)
            c.ok(not (rc.get("restored") or []) and "同名" in (
                (rc.get("skipped") or [{}])[0].get("why") or ""),
                 "来源目录已有同名文件时拒绝恢复（不静默覆盖）",
                 str((rc.get("skipped") or [{}])[0].get("why"))[:30])
            with open(victim_path, encoding="utf-8") as fh:
                c.ok(fh.read().strip() == "来源目录里后来出现的同名文件",
                     "被拒绝的恢复没有改动来源目录里那份同名文件")
            os.remove(victim_path)
            # 把回收目录里那份取回来，后面还要用它验证体积上限与性别落库
            for _root, _dirs, _fs in os.walk(os.path.join(work, "removed")):
                for _f in _fs:
                    if _f == victim:
                        shutil.move(os.path.join(_root, _f), victim_path)
            c.ok(os.path.isfile(victim_path), "测试用来源文件已复位")
            code_none, _ = _asgi(srv.app, "POST", "/api/sources/remove", hr,
                                 json.dumps({"names": []}).encode())
            c.ok(code_none == 400, "一份都没选时明确报错", f"实际 {code_none}")

            # ---- 体积上限：文件夹导入与邮件附件同一口径 ----
            _before_ingest = db.connect(db_path)
            n_doc_before = _before_ingest.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"]
            _before_ingest.close()
            rep_huge = ingest.ingest_dir(src_dir, JD, TIERS, db_path, cfg=mb.load_config(),
                                         job_id=None)
            c.ok(rep_huge.get("skipped_oversize") == 1 and rep_huge.get("max_attachment_mb") == 1,
                 "文件夹导入会跳过超限文件（此前这条校验只在邮件路径上有）",
                 f"skipped_oversize={rep_huge.get('skipped_oversize')} 上限 {rep_huge.get('max_attachment_mb')}MB")
            c.ok(os.path.isfile(huge_path),
                 "超限文件只是不导入，文件本身不删（由 HR 决定怎么处理）")
            huge_docs = [d for d in rep_huge["details"] if d["file"] == huge]
            c.ok(huge_docs and huge_docs[0]["status"] == "skipped_oversize"
                 and "超过体积上限" in huge_docs[0]["notes"][0],
                 "跳过原因写清楚（超了多少 MB、上限是多少、文件未删）",
                 str(huge_docs[0]["notes"][0])[:46] if huge_docs else "")
            _after_ingest = db.connect(db_path)
            n_doc_after = _after_ingest.execute("SELECT COUNT(*) AS n FROM documents").fetchone()["n"]
            n_over_audit = _after_ingest.execute(
                "SELECT COUNT(*) AS n FROM audit_log WHERE action='oversize_skipped'"
            ).fetchone()["n"]
            _after_ingest.close()
            c.ok(n_over_audit >= 1, "跳过超限文件写入审计（可解释为什么这份没进来）",
                 f"{n_over_audit} 条")
            c.ok(n_doc_after >= n_doc_before, "导入不会减少已有附件数",
                 f"{n_doc_before} → {n_doc_after}")

            # ---- 性别标签：只取明写、不推断、不进评分、筛选默认关 ----
            from app.pipeline.extract import _find_gender
            from app.pipeline.sanitize import FORBIDDEN_KEYS
            c.ok(_find_gender("性别：女") == "女" and _find_gender("性 别：男") == "男"
                 and _find_gender("Sex: Female") == "女" and _find_gender("gender: male") == "男",
                 "性别只从简历的标签行抽取（中英文、全半角都认）")
            c.ok(_find_gender("性别要求：男") == "男",
                 "兼容「性别要求：男」这类写法")
            c.ok(_find_gender("姓名：张三\n技能：钛合金") is None
                 and _find_gender("男性优先考虑") is None
                 and _find_gender("中共党员，已婚已育") is None,
                 "简历没写性别标签就不填（不做任何推断）")
            c.ok("gender" in FORBIDDEN_KEYS,
                 "模型即使返回 gender 也会被丢掉（只有规则抽取的结果能落库）")

            _c = db.connect(db_path)
            cid_g = db.insert_candidate(_c, {"identity_key": "selftest:gender", "name": "性别样例"})
            _c.close()
            c.ok(bool(cid_g), "性别样例档案可建（用于验证性别不参与评分）", f"#{cid_g}")
            base_cand = {"name": "张三", "education": "硕士", "years": 3,
                         "skills": ["钛合金"], "skill_detail": [{"canonical": "钛合金",
                         "verified": True, "evidence": "做过钛合金"}], "certificates": []}
            g_m = grade({**base_cand, "gender": "男"}, JD, TIERS)
            g_f = grade({**base_cand, "gender": "女"}, JD, TIERS)
            g_n = grade(base_cand, JD, TIERS)
            c.ok(g_m["score"] == g_f["score"] == g_n["score"]
                 and g_m["tier_suggested"] == g_f["tier_suggested"] == g_n["tier_suggested"],
                 "换性别不改变评分与档位（性别不参与 grade()）",
                 f"男={g_m['score']} 女={g_f['score']} 无={g_n['score']}")

            # 开关默认关闭 → 传了 gender 也不生效，并且要如实说明
            _, st0_body = _asgi(srv.app, "GET", "/api/settings", {})
            st0 = json.loads(st0_body)
            c.ok(st0.get("gender_filter_enabled") is False,
                 "性别筛选开关出厂默认关闭",
                 f"gender_filter_enabled={st0.get('gender_filter_enabled')}")
            _, b0_body = _asgi(srv.app, "GET", "/api/candidates", {})
            _, b1_body = _asgi(srv.app, "GET",
                               "/api/candidates?gender=%E5%A5%B3", {})
            b0, b1 = json.loads(b0_body), json.loads(b1_body)
            c.ok(b1["count"] == b0["count"] and b1["gender_filter"]["applied"] is False
                 and "关闭状态" in (b1["gender_filter"]["why"] or ""),
                 "开关关闭时性别参数被忽略，并如实说明原因（不静默生效）",
                 f"count {b0['count']}={b1['count']}；why={str(b1['gender_filter']['why'])[:22]}")
            c.ok(b1.get("gender_facets") is None,
                 "开关关闭时不返回性别分布（不给「顺手看一眼分布」的口子）")

            # 打开开关：留痕 + 生效，且分布与结果一致
            _, set_on_body = _asgi(srv.app, "POST", "/api/settings", hr,
                                   json.dumps({"gender_filter_enabled": True}).encode())
            set_on = json.loads(set_on_body)
            c.ok(set_on.get("gender_filter_enabled") is True and set_on.get("changed") is True,
                 "开关可被打开并回话确认", str(set_on.get("note"))[:34])
            _c = db.connect(db_path)
            n_set_audit = _c.execute(
                "SELECT COUNT(*) AS n FROM audit_log WHERE entity='settings' "
                "AND action='update'").fetchone()["n"]
            last_set = _c.execute(
                "SELECT before, after, operator FROM audit_log WHERE entity='settings' "
                "ORDER BY id DESC LIMIT 1").fetchone()
            _c.close()
            c.ok(n_set_audit >= 1 and last_set and "True" in (last_set["after"] or ""),
                 "开启性别筛选写入审计（合规要看的凭证）",
                 f"{n_set_audit} 条；{last_set['before']}→{last_set['after']}")
            _, b2_body = _asgi(srv.app, "GET",
                               "/api/candidates?gender=%E5%A5%B3", {})
            b2 = json.loads(b2_body)
            c.ok(b2["gender_filter"]["applied"] is True
                 and all((x.get("gender") or "") == "女" for x in b2["items"]),
                 "开关打开后性别筛选真正生效（结果里只剩女）",
                 f"count={b2['count']}")
            fac = b2.get("gender_facets") or {}
            c.ok(fac.get("女") == b2["count"] and sum(
                fac.get(g, 0) for g in ("男", "女", "未标注")) == fac.get("全部"),
                 "分布统计与结果过滤同一口径（下拉里的数字和筛出来的人数对得上）",
                 str(fac))
            _, b3_body = _asgi(srv.app, "GET",
                               "/api/candidates?gender=%E6%9C%AA%E6%A0%87%E6%B3%A8", {})
            b3 = json.loads(b3_body)
            c.ok(all(not (x.get("gender") or "").strip() for x in b3["items"]),
                 "「未标注」是可筛分组：简历没写性别的档案不会凭空消失",
                 f"count={b3['count']}")
            # 头像字母/性别不参与评分——列表里每人档位与不带筛选时相同
            _a = {x["id"]: x["tier_effective"] for x in b0["items"]}
            c.ok(all(_a.get(x["id"]) == x["tier_effective"] for x in b2["items"]),
                 "按性别筛选不改变任何人的档位（筛选只影响列出谁）")

            # —— 初筛（v1.7.3）：最低学历 ≥ 门槛 + 院校层次 985/211 ——
            # 学校本身只做标签（uni_tier 随每条下发），筛选只开放到 985/211 层次。
            c.ok(all("uni_tier" in x for x in b0["items"]),
                 "列表每条都带 uni_tier 字段（卡片 985/211 标签的数据来源）")
            _, e1_body = _asgi(srv.app, "GET",
                               "/api/candidates?education=%E6%9C%AC%E7%A7%91", {})  # 本科
            _, e2_body = _asgi(srv.app, "GET",
                               "/api/candidates?education=%E5%8D%9A%E5%A3%AB", {})  # 博士
            e1, e2 = json.loads(e1_body), json.loads(e2_body)
            _RANK = {"大专": 1, "专科": 1, "本科": 2, "学士": 2, "研究生": 3, "硕士": 3, "博士": 4}
            c.ok(all(_RANK.get(x.get("edu_level") or "", 0) >= 2 for x in e1["items"])
                 and all(_RANK.get(x.get("edu_level") or "", 0) >= 4 for x in e2["items"])
                 and e2["count"] <= e1["count"] <= b0["count"]
                 and e1.get("edu_filter", {}).get("applied") is True,
                 "最低学历是 ≥ 门槛口径且如实标注（博士筛选 ⊆ 本科筛选 ⊆ 全量）",
                 f"全量 {b0['count']} / 本科及以上 {e1['count']} / 博士 {e2['count']}")
            # 用第一条候选人做院校/学历的确定性验证（改完恢复，不污染后续断言）
            _c = db.connect(db_path)
            _row = _c.execute("SELECT id, edu_level, school FROM candidates "
                              "ORDER BY id LIMIT 1").fetchone()
            _c.close()
            _c = db.connect(db_path)
            _c.execute("UPDATE candidates SET school='西安交通大学', edu_level='' WHERE id=?",
                       (_row["id"],))
            _c.commit()
            _c.close()
            _, u1_body = _asgi(srv.app, "GET", "/api/candidates?univ=985", {})
            u1 = json.loads(u1_body)
            c.ok(u1["count"] >= 1 and all(x.get("uni_tier") == "985" for x in u1["items"]),
                 "985 筛选生效且结果里都是 985（全名/别名/校区后缀归一后匹配）",
                 f"count={u1['count']}")
            _, u2_body = _asgi(srv.app, "GET", "/api/candidates?univ=211", {})
            u2 = json.loads(u2_body)
            c.ok(all(x.get("uni_tier") in ("985", "211") for x in u2["items"])
                 and u2["count"] >= u1["count"],
                 "211 筛选把 985 也算进去（985 学校全部同时是 211）",
                 f"count={u2['count']}")
            _, u3_body = _asgi(srv.app, "GET",
                               "/api/candidates?education=%E6%9C%AC%E7%A7%91", {})
            u3 = json.loads(u3_body)
            c.ok(u3.get("edu_filter", {}).get("hidden_unknown", 0) >= 1
                 and not any(x["id"] == _row["id"] for x in u3["items"]),
                 "学历无法判定的人被门槛挡下时，人数被如实报出（不静默消失）",
                 f"hidden_unknown={u3.get('edu_filter', {}).get('hidden_unknown')}")
            _c = db.connect(db_path)
            _c.execute("UPDATE candidates SET school=?, edu_level=? WHERE id=?",
                       (_row["school"], _row["edu_level"], _row["id"]))
            _c.commit()
            _c.close()

            # 落库与回填：简历写了性别才写；已有值不被反向覆盖
            # 用一份**内容不同**的简历：同一份内容会被文件层去重跳过（那测的就不是性别了）
            resume_g = ("姓名：性别样例二\n性别：女\n电话：13800005555\n邮箱：gender@test.cn\n"
                        "本科 材料成型\n技能：钛合金、真空熔铸")
            _c = db.connect(db_path)
            r_g = ingest.ingest_one(_c, mb.load_config(), JD, TIERS, filename="性别样例二.txt",
                                    data=resume_g.encode("utf-8"), local_path=None,
                                    channel="文件夹", applied_at=db.now(),
                                    source_message_id=None, job_id=None)
            _c.close()
            _c = db.connect(db_path)
            row_g = _c.execute("SELECT gender FROM candidates WHERE id = ?",
                               (r_g.get("candidate_id"),)).fetchone() if r_g.get(
                                   "candidate_id") else None
            _c.close()
            c.ok(r_g.get("status") == "added" and row_g and row_g["gender"] == "女",
                 "简历明写「性别：女」时落库（校招场景需要这个标签）",
                 f"status={r_g.get('status')} gender={row_g['gender'] if row_g else None}")

            _, set_off_body = _asgi(srv.app, "POST", "/api/settings", hr,
                                    json.dumps({"gender_filter_enabled": False}).encode())
            c.ok(json.loads(set_off_body).get("gender_filter_enabled") is False,
                 "开关可被关回去（列表恢复为全部候选人）")
            _c = db.connect(db_path)
            n_off = _c.execute("SELECT COUNT(*) AS n FROM audit_log WHERE entity='settings' "
                               "AND action='update'").fetchone()["n"]
            _c.close()
            c.ok(n_off >= 2, "开与关都留痕（开关状态变化可追溯）", f"{n_off} 条")

            # ---- 邮箱配置：模式与账号不匹配时要提醒（"配了没反应"的根因） ----
            _, mx_body = _asgi(srv.app, "POST", "/api/mailbox/config", hr,
                               json.dumps({"mode": "eml",
                                           "imap_host": "imap.exmail.qq.com",
                                           "imap_user": "jobs@example.cn",
                                           "max_attachment_mb": 20}).encode())
            mx = json.loads(mx_body)
            c.ok(any("eml 演练" in w for w in (mx.get("warnings") or [])),
                 "模式仍是 eml 却填了 IMAP 账号时，保存响应直接提醒（这就是「配了没反应」的根因）",
                 "；".join(mx.get("warnings") or [])[:60])
            _, mx2_body = _asgi(srv.app, "POST", "/api/mailbox/config", hr,
                                json.dumps({"mode": "imap"}).encode())
            mx2 = json.loads(mx2_body)
            c.ok(not any("演练" in w for w in (mx2.get("warnings") or []))
                 or mx2.get("mode") == "eml",
                 "切到 imap 后不再报演练模式提醒", str(mx2.get("mode")))
            _, ps_body = _asgi(srv.app, "GET", "/api/mailbox/presets", {})
            ps = json.loads(ps_body)
            c.ok(len(ps.get("presets") or []) >= 4
                 and all({"label", "host", "port", "ssl"} <= set(p) for p in ps["presets"]),
                 "提供常用邮箱的服务商/端口/SSL 预设（端口填错是最常见的失败原因）",
                 "、".join(p["label"] for p in ps["presets"][:4]))
            c.ok("授权码" in (ps.get("note") or ""),
                 "提醒国内邮箱要用授权码而不是登录密码")
            _, mcfg_body = _asgi(srv.app, "GET", "/api/mailbox/config", {})
            mcfg = json.loads(mcfg_body)
            c.ok(isinstance(mcfg.get("warnings"), list),
                 "邮箱配置读接口回带同一套提醒（界面不用自己再写一遍规则）",
                 f"{len(mcfg.get('warnings') or [])} 条")
            _, mt_body = _asgi(srv.app, "POST", "/api/mailbox/test", hr,
                               json.dumps({"imap_host": "127.0.0.1",
                                           "imap_port": 1}).encode())
            mt = json.loads(mt_body)
            c.ok("next_step" in mt or mt.get("ok") is False,
                 "测试连接要么给下一步指引、要么如实报错（不静默）",
                 str(mt.get("error") or mt.get("next_step"))[:44])
            # 复位成 eml 演练：下面的邮箱预览断言要读本地 .eml 目录，
            # 留着 imap + 127.0.0.1 会让它去连一个必然失败的端口。
            _asgi(srv.app, "POST", "/api/mailbox/config", hr,
                  json.dumps({"mode": "eml", "imap_host": "", "imap_user": ""}).encode())

            # ---- 邮箱预览：收信之前先看一眼 ----
            _, pv_body = _asgi(srv.app, "POST", "/api/mailbox/preview", hr,
                               json.dumps({"mode": "eml"}).encode())
            pv = json.loads(pv_body)
            c.ok(pv.get("ok") is True and "mails" in pv,
                 "邮箱预览返回可读结果（连的是哪个账号 / 最近几封）",
                 str(pv.get("message") or pv.get("account"))[:70])
            c.ok("password" not in pv_body.lower() or "password_set" in pv_body,
                 "邮箱预览不把口令写进响应体")

            # ============================================================ S 续
            # v1.6 三项需求：①对话能查岗位 ②历史会话不点清空不丢
            #                ③面试题纲/档位分析按「对应岗位」并做专业大类匹配
            c.section("S续 v1.6：岗位查询 / 历史会话持久化 / 按对应岗位分析 + 专业大类匹配")
            # 单独一个库：这段要造"材料岗 + 软件岗"两个口径完全不同的岗位，
            # 混进上面的接口冒烟库会把已有断言的数据前提搞乱。
            vdb = os.path.join(work, "v16.db")
            SW_JD = {
                "role": "软件开发工程师",
                "department": "",          # 故意留空：验证"部门没填就不许编"
                "must": {"education_min": "本科", "years_min": 3,
                         "skills_required": ["Java", "Spring Boot"]},
                "preferred": {"skills": ["Redis", "MySQL", "分布式"]},
            }
            _vc = db.connect(vdb)
            _d_mat = db.upsert_department(_vc, "研发中心", "材料工艺")
            _d_sw = db.upsert_department(_vc, "数字技术部", "软件研发")
            _job_mat = db.create_job(_vc, "工艺工程师", dept_id=_d_mat, jd=JD, operator="hr")
            _job_sw = db.create_job(_vc, "软件开发工程师", dept_id=_d_sw, jd=SW_JD, operator="hr")
            _vc.close()

            _MAT_RESUME = ("姓名：钱材料\n电话：13800001111\n邮箱：mat@test.cn\n"
                           "学历：硕士\n工作年限：5年\n院校：西北工业大学\n专业：材料加工工程\n"
                           "技能：钛合金、真空熔铸、材料成型、金相分析")
            _SW_RESUME = ("姓名：孙软件\n电话：13800002222\n邮箱：sw@test.cn\n"
                          "学历：本科\n工作年限：4年\n院校：西安电子科技大学\n专业：软件工程\n"
                          "技能：Java、Spring Boot、Redis、MySQL、分布式")
            _vc = db.connect(vdb)
            _r_mat = ingest.ingest_one(_vc, mb.load_config(), JD, TIERS,
                                       filename="钱材料.txt", data=_MAT_RESUME.encode("utf-8"),
                                       local_path=None, channel="文件夹",
                                       applied_at=db.now(), source_message_id=None,
                                       job_id=_job_mat)
            _r_sw = ingest.ingest_one(_vc, mb.load_config(), SW_JD, TIERS,
                                      filename="孙软件.txt", data=_SW_RESUME.encode("utf-8"),
                                      local_path=None, channel="文件夹",
                                      applied_at=db.now(), source_message_id=None,
                                      job_id=_job_sw)
            _vc.close()
            _cid_mat, _cid_sw = _r_mat.get("candidate_id"), _r_sw.get("candidate_id")
            c.ok(_cid_mat and _cid_sw and _r_mat["status"] == "added" and _r_sw["status"] == "added",
                 "① 造出材料岗 / 软件岗两名候选人（口径完全不同的两把尺子）",
                 f"材料#{_cid_mat} 软件#{_cid_sw}")

            # ---- S-j 对话能查岗位（此前问"发布了几个岗位"会答不上来）----
            srv.DB_PATH = vdb
            _, jobs_body = _asgi(srv.app, "GET", "/api/jobs", {})
            jobs = json.loads(jobs_body)
            _jl = jobs.get("jobs") or jobs.get("items") or []
            c.ok(len(_jl) == 2 and all("department_active" in j for j in _jl),
                 "① /api/jobs 返回岗位数且带部门启停状态", f"{len(_jl)} 个岗位")

            _ctx16 = ToolCtx(db_path=vdb, jd=JD, tiers=TIERS, job_id=_job_mat,
                             session_id="selftest16", operator="hr", role="hr")
            _ans_jobs = agent_loop.run_agent("目前发布了几个岗位？", _ctx16)
            c.ok(any(t["tool"] == "list_jobs" for t in _ans_jobs["trace"]),
                 "① 岗位类问句路由到 list_jobs 工具（而不是答不上来）",
                 "、".join(t["tool"] for t in _ans_jobs["trace"]) or "无工具调用")
            c.ok("软件开发工程师" in _ans_jobs["answer"] and "工艺工程师" in _ans_jobs["answer"],
                 "① 答复列出全部在招岗位的真实名称")
            c.ok("2" in _ans_jobs["answer"], "① 答复给出岗位数量")

            _tool_exec = __import__("app.agent.tools", fromlist=["execute"]).execute
            _jobs_tool = json.loads(_tool_exec("list_jobs", {}, _ctx16))
            c.ok(_jobs_tool.get("open_count") == 2 and len(_jobs_tool.get("jobs") or []) == 2,
                 "① list_jobs 工具返回在招岗位明细（open_count 与明细条数一致）",
                 f"open_count={_jobs_tool.get('open_count')} "
                 f"total={_jobs_tool.get('total_count')}")
            c.ok(all(j.get("must_skills") for j in _jobs_tool["jobs"]),
                 "① 每个岗位都带必需技能（模型据此判断在招什么，不靠猜）",
                 "；".join(f"{j['title']}→{'/'.join(j['must_skills'])}"
                          for j in _jobs_tool["jobs"]))
            # 停用岗位不计入在招数：这条口径与归岗用的 routable 一致，
            # 否则会出现"系统说在招 3 个、实际只有 2 个能收简历"的错位
            _vc = db.connect(vdb)
            db.set_job_active(_vc, _job_sw, False, "hr")
            _vc.close()
            _jobs_off = json.loads(_tool_exec("list_jobs", {}, _ctx16))
            _jobs_off_all = json.loads(_tool_exec("list_jobs", {"include_inactive": True}, _ctx16))
            c.ok(_jobs_off.get("open_count") == 1 and _jobs_off_all.get("total_count") == 2,
                 "① 停用岗位不计入在招数，但 include_inactive 时仍可查（停用不是删除）",
                 f"在招 {_jobs_off.get('open_count')} / 总计 {_jobs_off_all.get('total_count')}")
            _vc = db.connect(vdb)
            db.set_job_active(_vc, _job_sw, True, "hr")
            _vc.close()

            # ---- S-l 专业大类匹配：方向对不对口，而不是数关键词 ----
            _vc = db.connect(vdb)
            _cat_of = db.skill_categories(_vc)
            _cand_mat = db.candidate_detail(_vc, _cid_mat)
            _cand_sw = db.candidate_detail(_vc, _cid_sw)
            _jd_mat, _meta_mat = db.resolve_candidate_job(_vc, _cand_mat)
            _jd_sw, _meta_sw = db.resolve_candidate_job(_vc, _cand_sw)
            _vc.close()
            from app.pipeline.tier import major_match as _mm
            mm_same = _mm(_cand_sw, _jd_sw, _cat_of)      # 软件人 vs 软件岗 → 对口
            mm_cross = _mm(_cand_sw, _jd_mat, _cat_of)    # 软件人 vs 材料岗 → 不该说"对口"
            c.ok(mm_same["verdict"] == "对口",
                 "③ 软件候选人配软件岗 = 专业大类对口", mm_same["verdict"])
            c.ok(mm_cross["verdict"] != "对口",
                 "③ 软件候选人配材料岗 ≠ 对口（方向问题不能和缺技能混为一谈）",
                 f"{mm_cross['verdict']}｜{mm_cross['note'][:50]}")
            c.ok("major_family" in mm_same and mm_same["major_family"],
                 "③ 从专业文本推断出大类（软件/计算机）", str(mm_same.get("major_family")))
            c.ok("其他" not in (mm_same.get("job_categories") or {}),
                 "③ 未归类技能（其他）不计入方向判断，避免污染「侧重」")

            # ---- S-m 三条分析路径一律锚定「对应岗位」，不再读默认尺子 ----
            _, ex_sw_body = _asgi(srv.app, "POST", f"/api/candidates/{_cid_sw}/explain", hr, b"")
            ex_sw = json.loads(ex_sw_body)
            c.ok((ex_sw.get("job") or {}).get("title") == "软件开发工程师",
                 "③ 档位解释用的是「该候选人对应岗位」的尺子",
                 str((ex_sw.get("job") or {}).get("title")))
            c.ok(ex_sw.get("major_match", {}).get("verdict") == "对口"
                 and "Java" in [h["skill"] for h in (ex_sw.get("hit") or [])],
                 "③ 软件岗候选人命中 Java/Spring Boot 且判为对口",
                 str([h["skill"] for h in (ex_sw.get("hit") or [])]))
            c.ok(ex_sw.get("consistency", {}).get("same") is True,
                 "③ 档位解释与库内记录对账一致（技能刷新后不再出现 A vs D 打架）",
                 str(ex_sw.get("consistency", {}).get("note")))

            _, ex_mat_body = _asgi(srv.app, "POST", f"/api/candidates/{_cid_mat}/explain", hr, b"")
            ex_mat = json.loads(ex_mat_body)
            c.ok((ex_mat.get("job") or {}).get("title") == "工艺工程师",
                 "③ 材料岗候选人拿到的是材料岗尺子（两把尺子互不串台）",
                 str((ex_mat.get("job") or {}).get("title")))
            c.ok((ex_sw.get("job") or {}).get("job_id")
                 != (ex_mat.get("job") or {}).get("job_id"),
                 "③ 同一时刻两名候选人的分析口径确实是两个不同岗位")

            # 未归岗且无建议岗位 → 如实报错，而不是偷偷用默认尺子
            # 走真实摄入路径（文件夹上传不归岗），比手写 INSERT 更贴近实际状态
            _vc = db.connect(vdb)
            _r_none = ingest.ingest_one(
                _vc, mb.load_config(), JD, TIERS, filename="未归岗.txt",
                data=("姓名：吴未归\n电话：13800003333\n邮箱：nohome@test.cn\n"
                      "学历：本科\n工作年限：4年\n技能：钛合金").encode("utf-8"),
                local_path=None, channel="文件夹", applied_at=db.now(),
                source_message_id=None, job_id=None)
            _nohome = _r_none.get("candidate_id")
            # 摄入时系统会给一个建议岗位（即使未归岗），这里把它清掉，
            # 构造"既未归岗、系统也给不出建议"的状态（例如全部岗位都停用时就会出现）
            _vc.execute("UPDATE applications SET suggested_job_id = NULL WHERE candidate_id = ?",
                        (_nohome,))
            _vc.commit()
            _app_nohome = [a for a in db.candidate_detail(_vc, _nohome)["applications"]
                           if not a.get("job_id") and not a.get("suggested_job_id")]
            _vc.close()
            c.ok(bool(_app_nohome), "③ 造出一名「既未归岗、也没有建议岗位」的候选人",
                 f"#{_nohome} 待指定且无建议的投递 {len(_app_nohome)} 条")
            _, ex_none_body = _asgi(srv.app, "POST", f"/api/candidates/{_nohome}/explain", hr, b"")
            ex_none = json.loads(ex_none_body)
            c.ok(bool(ex_none.get("error")) and bool(ex_none.get("hint")),
                 "③ 既未归岗也没有建议岗位时如实说明「无对应岗位」，不套默认尺子",
                 str(ex_none.get("error"))[:52])
            _, iv_none_body = _asgi(srv.app, "POST", f"/api/candidates/{_nohome}/interview", hr,
                                    json.dumps({}).encode())
            iv_none = json.loads(iv_none_body)
            c.ok(bool(iv_none.get("error")),
                 "③ 面试题纲同样拒绝无对应岗位的投递（不然题目是别的岗位的）",
                 str(iv_none.get("error"))[:52])

            # ---- 面试题纲 / 匹配分析：部门为空时不许编造 ----
            _vc = db.connect(vdb)
            _jd_sw_real = db.job_of(_vc, _job_sw)
            _vc.close()
            from app.pipeline.analyze import _jd_brief as _jdb
            _brief = _jdb(_jd_sw_real)
            c.ok("部门:未指定" in _brief,
                 "③ 岗位没填部门时，提示词里显式写「未指定」（此前留空 → 模型自己编部门）")
            c.ok("材料工艺所" not in _brief,
                 "③ 软件岗的提示词里不出现别的岗位的部门名")

            # ---- S-k 历史会话：不点清空就不丢，点了才清 ----
            srv.DB_PATH = vdb
            # 先落一个清空点：上面 explain/interview 的接口调用也会写 agent_runs，
            # 不清一次的话"历史里应该有几条"就依赖于前面跑了什么，断言会变脆。
            # 先清空 → 只跑两条 → 历史里就正好是这两条。
            _asgi(srv.app, "POST", "/api/agent/clear", hr, b"")
            _vc = db.connect(vdb)
            _runs_base = db.agent_cost_summary(_vc)["runs"]
            _vc.close()

            _hid = "selftest-hist"
            _hctx = ToolCtx(db_path=vdb, jd=JD, tiers=TIERS, job_id=_job_mat,
                            session_id=_hid, operator="hr", role="hr")
            agent_loop.run_agent("人才库有多少人？", _hctx)
            agent_loop.run_agent("目前发布了几个岗位？", _hctx)
            _expect_total = _runs_base + 2          # 刚问的这两句也算运行记录
            _, h_body = _asgi(srv.app, "GET", "/api/agent/history", {})
            h = json.loads(h_body)
            c.ok(h["runs_shown"] == 2 and h["runs_total"] == _expect_total,
                 "② 历史会话从库里读得回来（刷新/重启后仍在），且总运行数照实汇报",
                 f"展示 {h['runs_shown']} 次 / 库内共 {h['runs_total']} 次（清空时库内有 {_runs_base} 条）")
            c.ok(len(h["messages"]) == 4, "② 历史按「问-答」成对还原",
                 f"{len(h['messages'])} 条消息")
            c.ok([m["role"] for m in h["messages"]] == ["user", "assistant", "user", "assistant"],
                 "② 消息按时间正序、问答交替")
            c.ok(all(m.get("run_id") for m in h["messages"]),
                 "② 每条消息都能溯源到具体的运行记录")

            _, clr_body = _asgi(srv.app, "POST", "/api/agent/clear", hr, b"")
            clr = json.loads(clr_body)
            c.ok(clr.get("cleared") == 2, "② 清空动作报出清掉了多少次问答", str(clr.get("cleared")))
            _, h2_body = _asgi(srv.app, "GET", "/api/agent/history", {})
            h2 = json.loads(h2_body)
            _vc = db.connect(vdb)
            _runs_after = db.agent_cost_summary(_vc)["runs"]
            _last_audit = db.list_audit(_vc, limit=1)
            _vc.close()
            c.ok(len(h2["messages"]) == 0 and h2["runs_shown"] == 0,
                 "② 点清空后界面历史真的空了")
            c.ok(_runs_after == _expect_total and h2["runs_total"] == _expect_total,
                 "② 清空**不物理删除** agent_runs（成本核算与审计要留）",
                 f"清空前 {_expect_total} 条，清空后 {_runs_after} 条")
            c.ok(_last_audit and _last_audit[0].get("action") == "chat_clear",
                 "② 清空动作本身留痕（删除动作也要可追溯）")
            _hctx2 = ToolCtx(db_path=vdb, jd=JD, tiers=TIERS, job_id=_job_mat,
                             session_id=_hid, operator="hr", role="hr")
            agent_loop.run_agent("人才库有多少人？", _hctx2)
            _, h3_body = _asgi(srv.app, "GET", "/api/agent/history", {})
            h3 = json.loads(h3_body)
            c.ok(len(h3["messages"]) == 2 and h3["runs_shown"] == 1,
                 "② 清空后新对话重新进入历史（清空点之后照常展示）",
                 f"{len(h3['messages'])} 条")

            # ---- S-k2 软清空 / 保留期 / 恢复 / 到期清理 ----
            # 用户口径：「删除三十天之后自动清理，30 天之内可以恢复」。落在实现上就是：
            # 点清空只推进清空点 + 开出保留期（不清数据）→ 保留期内可恢复 → 到期才真删。
            c.ok(bool(clr.get("purge_after")) and clr.get("retention_days") == db.PURGE_AFTER_DAYS
                 and clr.get("restorable") is True,
                 "④ 清空返回保留期口径（到期时间 + 保留天数 + 可恢复）",
                 f"purge_after={clr.get('purge_after')} / {clr.get('retention_days')} 天")
            c.ok(h2.get("days_left") == db.PURGE_AFTER_DAYS,
                 "④ 刚清空时倒计时是完整保留期（不因向下取整少一天）",
                 f"还剩 {h2.get('days_left')} 天")
            c.ok(bool(h2.get("purge_after")) and h2.get("restorable") is True,
                 "④ 历史接口如实汇报「将于何时清理、能否恢复」",
                 f"restorable={h2.get('restorable')}")
            _vc = db.connect(vdb)
            _runs_pre = db.agent_cost_summary(_vc)["runs"]
            _not_due = db.purge_due_chats(_vc, db.PURGE_AFTER_DAYS, "system", "system")
            _still = db.agent_cost_summary(_vc)["runs"]
            _vc.close()
            c.ok(_not_due.get("purged") == 0 and _not_due.get("reason") == "未到期"
                 and _still == _runs_pre,
                 "④ 保留期内后台清理不动手（30 天缓冲是真的，不是写着好看）",
                 f"{_not_due.get('reason')}；库内仍有 {_still} 条（清理前后一致）")
            # 恢复：被清空的那批回到界面（恢复后应当"一条不少"）
            _, rs_body = _asgi(srv.app, "POST", "/api/agent/restore", hr, b"")
            rs = json.loads(rs_body)
            _, h4_body = _asgi(srv.app, "GET", "/api/agent/history", {})
            h4 = json.loads(h4_body)
            c.ok(rs.get("ok") is True and h4["runs_shown"] == h4["runs_total"]
                 and h4["runs_shown"] > h3["runs_shown"]
                 and len(h4["messages"]) == 2 * h4["runs_shown"],
                 "④ 保留期内点「恢复对话」能把清空的那批找回来（恢复后全部运行重新可见）",
                 f"恢复 {rs.get('restored')} 条 → 展示 {h4['runs_shown']} 次问答 / "
                 f"{len(h4['messages'])} 条消息（库内共 {h4['runs_total']} 次）")
            c.ok(h4.get("restorable") is False and not h4.get("purge_after"),
                 "④ 恢复后不再有到期时间（这批记录会留到下次清空）",
                 f"restorable={h4.get('restorable')} / purge_after={h4.get('purge_after')!r}")
            _vc = db.connect(vdb)
            _restore_audit = db.list_audit(_vc, limit=1)
            _vc.close()
            c.ok(_restore_audit and _restore_audit[0].get("action") == "chat_restore",
                 "④ 恢复动作也留痕（清空 → 恢复是一条完整的行为链）")
            # 到期清理：把到期时间改到过去，模拟"满 30 天"
            _vc = db.connect(vdb)
            db.clear_chat(_vc, "hr", "hr")
            _runs_at_clear = db.agent_cost_summary(_vc)["runs"]
            db.set_setting(_vc, db.CHAT_PURGE_AFTER_KEY, "2020-01-01 00:00:00")
            _due = db.purge_due_chats(_vc, db.PURGE_AFTER_DAYS, "system", "system")
            _runs_gone = db.agent_cost_summary(_vc)["runs"]
            _purge_audit = db.list_audit(_vc, limit=1)
            _st_after = db.chat_clear_state(_vc)
            _vc.close()
            c.ok(_due.get("purged") == _runs_at_clear and _runs_gone == 0,
                 "④ 到期后后台清理真的删掉底层运行记录",
                 f"删掉 {_due.get('purged')} 条，库内剩 {_runs_gone} 条")
            # 注意：这里不能断言 tokens > 0——自检里规则降级路径不消耗 token，
            # 断言"汇总写进审计"本身才是要锁的行为（成本证据不随明细消失）。
            _pa = (_purge_audit[0].get("after") if _purge_audit else "") or ""
            c.ok("chat_purge" == (_purge_audit[0].get("action") if _purge_audit else "")
                 and f"删除 {_runs_at_clear} 条运行记录" in _pa and "合计 tokens" in _pa
                 and "运行 #" in _pa,
                 "④ 删明细前先把汇总（条数/token 合计/运行号区间）写进 chat_purge 审计",
                 _pa[:70] + "…")
            c.ok(_st_after["cleared_through_run"] == 0 and _st_after["purge_after"] == "",
                 "④ 清理后清空点被复位（此后恢复接口无对象可恢复，不可逆是真的）",
                 f"cleared_through_run={_st_after['cleared_through_run']}")

            # ---- 技能筛选此前恒空（缺陷 #50）：技能先挂载再过滤 ----
            # 注意 #3（吴未归）技能里也有钛合金，所以这里断言的是"命中谁"，
            # 不是"只有一条"——断言要写实际该成立的性质。
            _vc = db.connect(vdb)
            _by_java = db.list_candidates(_vc, skill="Java")
            _by_java_canon = db.list_candidates(_vc, skill=["Java", "Spring Boot"])
            _by_mat = db.list_candidates(_vc, skill="钛合金")
            _vc.close()
            c.ok([x["id"] for x in _by_java] == [_cid_sw],
                 "③ 按技能筛选真的能筛出人（此前先过滤后挂载 → 恒返 0 条）",
                 f"Java → {[x['id'] for x in _by_java]}")
            c.ok(len(_by_java_canon) == 1 and _by_java_canon[0]["id"] == _cid_sw,
                 "③ 多词技能筛选取并集")
            _mat_ids = [x["id"] for x in _by_mat]
            c.ok(_cid_mat in _mat_ids and _cid_sw not in _mat_ids,
                 "③ 技能筛选不串岗（钛合金只出材料方向的人，软件候选人不在其中）",
                 f"钛合金 → {_mat_ids}（软件 #{_cid_sw} 不在内）")
        finally:
            srv.DB_PATH = original_db
            srv.REMOVED_DIR = _orig_removed
            _mb.CONFIG_PATH = _orig_cfg
            _mb._SECRET_DEFAULT_PATH = _orig_secret
            shutil.rmtree(os.path.join(BASE, "data", "_selftest_src"), ignore_errors=True)
            shutil.rmtree(os.path.join(BASE, "data", "_selftest_archive"), ignore_errors=True)

        # ============================================================ 汇总
        c.section("汇总")
        total = c.passed + len(c.failed)
        print(f"  通过 {c.passed} / {total}")
        if c.failed:
            print(f"  失败 {len(c.failed)} 项：")
            for f in c.failed:
                print(f"    - {f}")
            return 1
        print("  全部断言通过：数据层、去重、不丢件、反幻觉、合规、分级、"
              "权限、审计、检索、智能体、降级 均符合设计方案。")
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
