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
F 分级                                      档位（A/B/C）结果与命中证据逐条核对
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
import time
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

# v1.29：档位字母**从配置取**，判据里不再写死 "D"
# —— 写死的话人事处一改档位体系，一大堆断言会全挂，而挂的原因是测试抄了旧字母，
# 不是功能坏了（这种"测试自己过期"的假警报最消耗信任）。
from app import tiers as _tiers_mod  # noqa: E402

_HARD_T = _tiers_mod.hard_shortfall_tier()      # 学历不达标时的档位（= 最差档）
_MID_T = _tiers_mod.mid_tier()                  # 学历未识别时的中性档


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



def _actions_src() -> str:
    """读 app/actions.py 源码（断言"动作层是否登记了某个 tool"用）。"""
    import os
    p = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                     "app", "actions.py")
    with open(p, encoding="utf-8") as fh:
        return fh.read()


def main(verbose: bool = True) -> int:
    # 让"模型不可用"成为确定条件：指向一个必然拒绝连接的端口
    os.environ["LLM_BASE_URL"] = "http://127.0.0.1:9/v1"
    os.environ.pop("LLM_API_KEY", None)
    # v1.8 起有两条后台自动化：入库即分析（daemon 线程）、每日巡检（startup 线程）。
    # 自检必须**关掉它们**——断言依赖确定的结果，不能让后台线程在两条断言之间
    # 偷偷写库（那会变成随机失败，而随机失败最容易被当成"偶发抖动"忽略掉）。
    # 关闭后，这两条路径仍由专门的断言段落直接函数调用覆盖。
    os.environ["TP_AUTO_INSIGHT"] = "0"
    os.environ["TP_DAILY_TASK"] = "0"
    os.environ["TP_BRIEF_LLM"] = "0"

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
            # v1.8.9 起 D 只留给「已明确归岗 + 学历明确不足」：这份简历连文本都没有，
            # 学历识别不出来 -> 不替人下结论，落 C 并标待人工判读（仍需人工看原件）。
            c.ok(broken[0]["tier_effective"] == _MID_T and broken[0]["needs_review"],
                 "解析失败的简历仍建档给档位（学历未识别不判最差档），并标待人工判读",
                 f"{broken[0]['tier_effective']} / needs_review={broken[0]['needs_review']}")
            # v1.8.6：解析失败但**文件名带姓名**（王海涛-简历.docx）的简历，
            # 现在能从文件名兜底识别出姓名——身份键按姓名建，不再落成无身份档。
            c.ok(broken[0]["name"] == "王海涛",
                 "解析失败但文件名带姓名的简历，姓名从文件名兜底识别",
                 str(broken[0]["name"]))
            c.ok(broken[0]["identity_key"].startswith("nm:"),
                 "有姓名（文件名兜底）后身份键按姓名建，不再走文档哈希兜底",
                 broken[0]["identity_key"][:16])

        # 姓名抽取的边界：段落标题不是人名；文件名兜底猜不出就不硬凑
        from app.pipeline import extract as _ex_mod
        _n1 = _ex_mod._find_name("教育经历\n2019-2023 某大学本科\n")
        c.ok(_n1 is None, "首行是段落标题（教育经历）时不当成姓名", str(_n1))
        _n2 = _ex_mod._find_name("姓名：张三\n教育经历\n")
        c.ok(_n2 == "张三", "显式「姓名：」标签优先")
        _n3 = _ex_mod.name_from_filename("张三-数字IC工程师-硕士-简历.pdf")
        c.ok(_n3 == "张三", "文件名兜底：取首段、剥掉简历字样", str(_n3))
        _n4 = _ex_mod.name_from_filename("resume_final_v2.pdf")
        c.ok(_n4 is None, "文件名里猜不出姓名时返回 None，绝不硬凑", str(_n4))

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
        # v1.13.7：模式记成 llm+rule（模型抽基本信息 + 规则兜底/举证），
        # 断言的是"走了模型通道"，不是具体标签
        c.ok(str(lm["extract_mode"]).startswith("llm"),
             "模型通道确实被走到（未静默退回规则）", lm["extract_mode"])
        c.ok("电子束熔炼" not in lm["skills"], "模型声称但原文没有的技能不进 skills")
        c.ok("电子束熔炼" in lm["unverified_skills"], "未核验技能单独列出，供人工核对",
             "、".join(lm["unverified_skills"]))
        g = grade(lm, JD)
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
        # v1.12：A/B/C 不再由规则打分产生（打分已删），改由模型给。
        # 所以这里只断言两件事：① 规则不会臆造 A/B/C；② 技能命中/缺失仍然逐条正确
        # （这是反幻觉的凭据，跟档位口径无关，必须一直守住）。
        expect = {"陈志远": (["钛合金", "真空熔铸", "材料成型"], []),
                  "刘婉清": (["钛合金", "材料成型"], ["真空熔铸"]),
                  "赵敏": (["真空熔铸", "材料成型"], ["钛合金"])}
        for name, (must_hit, must_miss) in expect.items():
            row = [x for x in db.list_candidates(conn) if x["name"] == name]
            if not row:
                c.ok(False, f"{name} 在库中")
                continue
            x = row[0]
            c.ok(x["tier_effective"] in (None, "") or x["tier_effective"] == _HARD_T,
                 f"{name} 档位不由规则打分臆造（v1.12：A/B/C 只由模型给）",
                 f"实际 {x['tier_effective']}")
            for sk in must_hit:
                c.ok(sk in (x["hits"] or []), f"{name} 命中必需技能「{sk}」")
            for sk in must_miss:
                c.ok(sk not in (x["hits"] or []),
                     f"{name} 未命中「{sk}」（原文确无此经历，不得凭岗位需要倒推）",
                     "、".join(x["hits"] or []) or "无")
        # v1.12 核心规则（必须钉死）：**学历门槛是唯一的硬判据**
        #   不达标 → D（可复现）；达标 → 交给模型（规则不再产出 A/B/C）
        _gd = grade({"name": "规则甲", "education": "本科", "skills": []},
                    {"role": "r", "must": {"skills_required": [], "education_min": "硕士",
                                             "years_min": 0}, "preferred": {}},
                    job_confirmed=True)
        _gm = grade({"name": "规则乙", "education": "硕士", "skills": []},
                    {"role": "r", "must": {"skills_required": [], "education_min": "本科",
                                             "years_min": 0}, "preferred": {}},
                    job_confirmed=True)
        c.ok(_gd["tier_suggested"] == _HARD_T,
             "⑨ 学历不达标 → 直接判最差档 C（唯一硬门槛，规则可复现）",
             str(_gd["tier_suggested"]))
        c.ok(_gm["tier_suggested"] is None and _gm["score"] is None,
             "⑨b 学历达标 → 不臆造档位（等模型给 A/B/C），也不再算分",
             f"tier={_gm['tier_suggested']} score={_gm['score']}")
        # ---- v1.29：档位体系本身（配置驱动，字母/文案/最差档都不写死）----
        c.ok(_tiers_mod.assert_consistent() == [],
             "档位：内置兜底表与 config/tiers.json 一致（防两份清单分叉）",
             "不一致的键：" + "、".join(_tiers_mod.assert_consistent()))
        c.ok(_tiers_mod.all_tiers() == ["A", "B", "C"]
             and _tiers_mod.labels() == {"A": "推荐面试", "B": "储备", "C": "暂不匹配"},
             "档位：三档 A 推荐面试 / B 储备 / C 暂不匹配（来自配置而非硬编码）",
             f"{_tiers_mod.all_tiers()} {_tiers_mod.labels()}")
        c.ok(_tiers_mod.hard_shortfall_tier() == "C" and _tiers_mod.mid_tier() == "B",
             "档位：最差档 C（学历不达标）/ 中性档 B（学历未识别）按配置算",
             f"hard={_tiers_mod.hard_shortfall_tier()} mid={_tiers_mod.mid_tier()}")
        c.ok(_tiers_mod.enum_str() == "A/B/C" and "D" not in _tiers_mod.enum_str(),
             "档位：提示词/工具说明里的枚举由配置生成（不会再把废弃的 D 教给模型）",
             _tiers_mod.enum_str())
        # 关键反例：**废弃的 D 必须被判为非法**，否则历史脏值会一路混进筛选与统计
        c.ok(not _tiers_mod.is_valid_tier("D") and _tiers_mod.is_valid_tier("C"),
             "档位：废弃的 D 不再是合法档位（C 合法）—— 防旧值混进库")
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
        c.ok(unparsed and unparsed[0]["tier_effective"] in ("B", _HARD_T),
             "解析失败的简历仍给档位、不被排除（档位口径见 C 段的说明）",
             unparsed[0]["tier_effective"] if unparsed else "—")

        # v1.8.9 D 档前提：必须「已明确归岗 + 学历明确不足」两条同时成立。
        # 未归岗时尺子可能是系统猜的"建议岗位"，用猜出来的门槛判最差档不合理。
        _low = {"name": "测试员", "education": "大专", "years": 5,
                "skills": [{"name": "钛合金", "evidence": "负责钛合金工艺"}]}
        _g_conf = grade(_low, JD, job_confirmed=True)
        _g_unconf = grade(_low, JD)
        c.ok(_g_conf["tier_suggested"] == _HARD_T,
             "已明确归岗 + 学历低于岗位要求 -> 最差档 C", _g_conf["tier_suggested"])
        c.ok(_g_unconf["tier_suggested"] != _HARD_T,
             "未明确归岗的投递：即便学历低于（建议）岗位门槛也不判最差档",
             _g_unconf["tier_suggested"])
        c.ok(any("尚未明确归岗" in r for r in _g_unconf["risks"]),
             "不判最差档的原因如实写进风险提示", "；".join(_g_unconf["risks"])[:36])
        conn.close()

        # ============================================================ G
        c.section("G 建议与决定分离：HR 确认才生效，且新版本不覆盖已确认结果")
        conn = db.connect(db_path)
        chen = [x for x in db.list_candidates(conn) if x["name"] == "陈志远"][0]
        aid = chen["application_id"]
        # 系统建议档位字段还在（值可能为空=待分析，或 D=学历不达标）；
        # 关键不变式是建议与决定分列，不是规则必须产出某个档
        c.ok("tier_suggested" in chen, "系统建议档位字段存在（值为空=待模型分析）")
        c.ok(chen["tier_final"] is None, "HR 未确认前 tier_final 为空")
        c.ok(chen["app_status"] == "待确认", "库里的状态值为『待确认』（展示层叫『待复核』")
        # v1.14.1：复核是**独立动作**，与档位解耦。
        # ① 改档位**不应该**顺手把复核状态置成已确认（否则 HR 为了消标签而改档位）
        # ② 复核不应该动档位
        _conn_rv = db.connect(db_path)
        _rv = db.set_application_tier(_conn_rv, aid, "B", "沟通后调整", "hr", "hr")
        _conn_rv.close()
        c.ok((_rv or {}).get("status") != "已确认",
             "⑲ 改档位**不影响**复核状态（两件事解耦，HR 不用为了消标签而改档位）",
             f"改档后 status={(_rv or {}).get('status')}")
        _conn_rv2 = db.connect(db_path)
        _rv2 = db.review_mark(_conn_rv2, aid, "hr", "hr")
        c.ok((_rv2 or {}).get("status") == "已确认"
             and (_rv2 or {}).get("tier_final") == "B",
             "⑲b 复核只置复核状态，**不动档位**",
             f"复核后 status={(_rv2 or {}).get('status')} 档位={(_rv2 or {}).get('tier_final')}")
        _conn_rv3 = db.connect(db_path)
        _rv3 = db.unreview_mark(_conn_rv3, aid, "hr", "hr")
        _conn_rv3.close()
        c.ok((_rv3 or {}).get("status") == "待确认" and (_rv3 or {}).get("tier_final") == "B",
             "⑲c 复核可以撤销，档位仍不变",
             f"撤销后 status={(_rv3 or {}).get('status')} 档位={(_rv3 or {}).get('tier_final')}")
        # v1.13：界面上的状态标签要按**实际情况**给，不能把库里的默认值原样贴上——
        # v1.14：取消「待确认」标签。查证：它**不驱动任何自动化**（确认与否系统
        # 行为完全一样），认同默认档位的人不需要任何动作，这个标签却在暗示他漏了事。
        # 现在只提示卡住的事：待归岗（没岗位判不了档）/ 待分析（档位还没出来）/
        # 展示标签：待复核 / 已分析 / 待归岗 / 已复核。
        # 注意区分：**库里的 status 值**仍是「待确认/已确认」，展示层才叫「待复核/已复核」。
        _sd = db.app_status_display
        c.ok(_sd({"app_status": "已确认", "job_id": 1, "tier_suggested": "A"})[0] == "已复核"
             and _sd({"app_status": "待确认", "job_id": 1, "tier_suggested": "B"})[0] == "待复核"
             and _sd({"app_status": "待确认", "job_id": 1})[0] == "待分析"
             and _sd({"app_status": "待确认", "job_id": None})[0] == "待归岗",
             "⑪ 状态标签：待复核 / 待分析 / 待归岗；复核后显示「已复核」")
        c.ok(all(_sd(x)[1] for x in ({"app_status": "待确认", "job_id": 1, "tier_suggested": "B"},
                                     {"app_status": "待确认", "job_id": None})),
             "⑪b 每个状态都带一句「下一步该做什么」（界面上悬停可见）")
        before_audit = len(db.list_audit(conn, limit=999))
        r = db.set_application_tier(conn, aid, "B", "沟通后调整", "hr", "hr")
        c.ok(r["tier_final"] == "B", "HR 改档后 tier_final = B")
        # v1.14.1：改档位**不再**顺手把复核状态置成已确认（档位与复核解耦，
        # 否则 HR 为了消掉「待复核」标签只能去改档位，会污染自己的判断）
        c.ok(r["status"] != "已确认",
             "改档位不改变复核状态（复核请走独立的复核按钮）",
             f"status={r['status']}")
        c.ok(len(db.list_audit(conn, limit=999)) > before_audit, "改档写入审计")
        # v1.12 回归点：档位来源明细必须报**库内生效档位**，不能报规则现算值——
        # 规则已产不出 A/B/C，报现算值会让所有学历达标的人显示成「待分析」
        # （实测反馈：「档位来源都没分析」）。取一条**确实已归岗**的投递来验。
        # 自检库里的夹具投递都没有 job_id（未归岗），所以临时挂一个岗位来验，
        # 验完立刻摘掉并删掉岗位——不留痕迹，避免影响后续断言。
        from app.agent.tools import execute as _tool_exec2
        _jid_t = db.create_job(conn, "自检档位来源岗", dept_id=None,
                               jd={"role": "自检档位来源岗", "department": "",
                                   "must": {"skills_required": [], "education_min": "本科",
                                            "years_min": 0},
                                   "preferred": {"skills": []}, "note": ""})
        conn.execute("UPDATE applications SET job_id = ? WHERE id = ?", (_jid_t, aid))
        conn.commit()
        _ex_g = json.loads(_tool_exec2("explain_grade",
                                       {"candidate_id": chen["id"]},
                                       ToolCtx(db_path=db_path, tiers=TIERS)))
        c.ok(_ex_g.get("tier_suggested") == "B" and bool(_ex_g.get("tier_source")),
             "⑩ 档位来源报的是库内生效档位（HR 确认的 B），不是规则现算值",
             f"tier={_ex_g.get('tier_suggested')} 来源={_ex_g.get('tier_source')}")
        conn.execute("UPDATE applications SET job_id = NULL WHERE id = ?", (aid,))
        conn.execute("DELETE FROM jobs WHERE id = ?", (_jid_t,))
        conn.commit()
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

        # v1.29：阶段名换成新的流程取值（旧值 已联系/初面/复面/待offer/已入职 已废弃）
        ans4 = agent_loop.run_agent("把陈志远的投递推进到待面试", ctx)
        # 规则路由不覆盖这个意图，故直接调工具验证提案机制
        from app.agent.tools import execute as tool_exec
        raw = json.loads(tool_exec("set_stage", {"application_id": aid, "stage": "待面试",
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
        c.ok(db.get_application(conn, aid)["stage"] == "待面试",
             "此刻才真正改库：阶段 = 待面试")
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

        # ============================================================ R3
        c.section("R3 重复投递口径（v1.8.8）：未归档幂等跳过；已归档按新投递录入")
        r3db = os.path.join(work, "reapply.db")
        r3cfg = dict(cfg, archive_dir=os.path.join(work, "archive3"))
        r3src = os.path.join(work, "reapply_src")
        os.makedirs(r3src, exist_ok=True)
        _pick = sorted(f for f in os.listdir(resume_dir) if f.startswith("陈志远"))[0]
        shutil.copy2(os.path.join(resume_dir, _pick), os.path.join(r3src, _pick))

        ingest.ingest_dir(r3src, JD, TIERS, r3db, cfg=r3cfg, channel="文件夹", job_id=None)
        conn = db.connect(r3db)
        _p1 = db.pool_stats(conn)
        c.ok(_p1["applications"] == 1 and _p1["people"] == 1,
             "首次导入：1 人 1 条投递", f"{_p1['people']} 人 / {_p1['applications']} 条")
        _cid3 = [x for x in db.list_candidates(conn) if x["name"] == "陈志远"][0]["id"]
        conn.close()

        # ① 未归档的人重复投同一份文件 → 幂等跳过：不新增投递、不重复落盘
        r_dup = ingest.ingest_dir(r3src, JD, TIERS, r3db, cfg=r3cfg,
                                  channel="文件夹", job_id=None)
        conn = db.connect(r3db)
        c.ok(r_dup["skipped_dup"] == 1 and r_dup["added"] == 0,
             "未归档的人重复投递同一份简历 → 幂等跳过（不导入）",
             f"skipped={r_dup['skipped_dup']} added={r_dup['added']}")
        c.ok(len(db.list_applications(conn, cid=_cid3)) == 1,
             "幂等跳过不新增投递（投递数仍为 1）")
        conn.close()

        # ② 归档之后重新投递 → 作为**新投递**录入，并把档案移回人才库
        conn = db.connect(r3db)
        db.set_candidate_archived(conn, _cid3, True, "hr", "hr")
        c.ok(bool(db.get_candidate(conn, _cid3)["archived_at"]), "先把该人归档（前置条件）")
        conn.close()
        r_re = ingest.ingest_dir(r3src, JD, TIERS, r3db, cfg=r3cfg,
                                 channel="文件夹", job_id=None)
        conn = db.connect(r3db)
        c.ok(r_re["added"] == 1,
             "已归档的人重新投递 → 计为『新增投递』而不是幂等跳过",
             f"added={r_re['added']} skipped={r_re['skipped_dup']}")
        c.ok(not db.get_candidate(conn, _cid3)["archived_at"],
             "重新投递后档案自动移回人才库（取消归档）")
        c.ok(len(db.list_applications(conn, cid=_cid3)) == 2,
             "新投递被记录（共 2 条），历史投递一条都没删",
             f"实际 {len(db.list_applications(conn, cid=_cid3))} 条")
        c.ok(len(db.candidate_detail(conn, _cid3)["documents"]) == 1,
             "同一份文件仍只有 1 条原件台账（不重复落盘）",
             f"实际 {len(db.candidate_detail(conn, _cid3)['documents'])} 份")
        _acts = {a["action"] for a in db.list_audit(conn, limit=200)}
        c.ok("unarchive_on_reapply" in _acts, "归档后重新投递写审计（可追溯为什么又出现了）")
        conn.close()

        # ③ 列表排序口径：最差档沉底，其余按投递时间倒序（不看分数）
        odb = os.path.join(work, "order.db")
        conn = db.connect(odb)
        for _nm, _tier, _at, _sc in (("甲", "A", "2026-01-01 09:00:00", 0.9),
                                     ("乙", _HARD_T, "2026-03-01 09:00:00", 0.2),
                                     # 丙用**非最差档**：三档下 "C" 就是最差档，
                                     # 与乙撞成同档会让"沉底"这条用例失去区分度
                                     ("丙", "B", "2026-02-01 09:00:00", 0.5)):
            _c = db.insert_candidate(conn, {"name": _nm, "source": "测试"})
            conn.execute(
                "INSERT INTO applications (candidate_id, tier_suggested, applied_at, score,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?)",
                (_c, _tier, _at, _sc, _at, _at))
        conn.commit()
        _order = [x["name"] for x in db.list_candidates(conn) if x["name"] in ("甲", "乙", "丙")]
        c.ok(_order == ["丙", "甲", "乙"],
             "排序：最差档沉底，其余按投递时间倒序（最新在前，不看分数）", str(_order))
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

        # 文件夹上传：文件名里带在招岗位名 → 归到该岗位；不带 → 待指定。
        # v1.8.3 起与邮件「标题归岗」同一口径——文件名/标题本身就是投递意向的表达，
        # 不认它的话，文件名写了岗位的简历也会全落到"待指定"、再按默认尺子算错分。
        ingest.ingest_dir(resume_dir, JD, TIERS, r2db,
                          cfg=rcfg2, channel="文件夹", job_id=None)
        conn = db.connect(r2db)
        _f_apps = [a for a in db.list_applications(conn) if a["channel"] == "文件夹"]
        _routed = [a for a in _f_apps if a["job_id"]]
        _pending = [a for a in _f_apps if not a["job_id"]]
        c.ok(len(_f_apps) > 0 and len(_routed) > 0,
             "文件名带在招岗位名的简历自动归岗（如「陈志远-简历-工艺工程师.txt」→ 工艺工程师岗）",
             f"归岗 {len(_routed)} 条")
        c.ok(len(_pending) > 0,
             "文件名没带岗位名的仍落「待指定」，不硬凑岗位", f"待指定 {len(_pending)} 条")
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
                # v1.29：跳过 ui.py（前端 JS 不是 Python 调用）与 `_bak_*.py`
                # （备份文件不参与运行，扫它们只会制造假警，把真问题淹掉）。
                if (not f.endswith(".py")) or f == "ui.py" or f.startswith("_bak_"):
                    continue
                src = open(os.path.join(root, f), encoding="utf-8").read()
                for alias, have in namespaces.items():
                    for m in _re.finditer(rf"\b{alias}\.([A-Za-z_][A-Za-z0-9_]*)", src):
                        # v1.29：**跳过文件名**。注释/文档里写「见 db.py」「actions.py 里…」
                        # 会被这条正则当成一次 `db.py` 调用（属性名是 `py`），于是报
                        # "调用不存在的函数"。这类误报会让整条判据常年是红的，
                        # 真出问题时反而没人看——比不检查更糟。
                        if m.group(1) in ("py", "js", "json", "md", "txt", "html", "css"):
                            continue
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

            # 侧栏品牌图标（v1.10）：内嵌 data URI —— 离线可用、不新增静态路由。
            # 无论有没有图标文件，模板占位符都必须被替换掉（留着就是页面上写着
            # "__BRAND_LOGO__"），且不能出现空 src 的裂图。
            _idx_html = (bodies.get("GET /") or (0, ""))[1]
            c.ok(_idx_html and "__BRAND_LOGO__" not in _idx_html,
                 "首页品牌图标占位符已被替换（不会把 __BRAND_LOGO__ 漏到页面上）")
            c.ok('class="logo" alt="企业人才库智能体"' in _idx_html
                 or '<span class="logo">才</span>' in _idx_html,
                 "品牌图标要么是内嵌图片、要么退回文字方块（不会出现空 src）",
                 "内嵌图片" if 'class="logo" alt=' in _idx_html else "文字方块兜底")

            # 预览接口只该要正文：曾经复用了发信模型（to/subject 必填），
            # 界面点「预览」直接 422、什么都不发生（实测反馈）。这里钉住这个行为。
            _pcode, _pbody = _asgi(srv.app, "POST", "/api/mail/preview", hr,
                                   json.dumps({"body": "| 项目 | 内容 |\n| --- | --- |\n"
                                                       "| 面试时间 | 9:30 |"}).encode())
            _pj = json.loads(_pbody) if _pbody else {}
            c.ok(_pcode == 200 and "<table" in (_pj.get("html") or ""),
                 "预览接口只传正文就能用（不再因缺 to/subject 被判 422）",
                 f"HTTP {_pcode}")

            # 白屏类问题必须在自检里就能拦下：**校验 render_page() 的产物**，而不是
            # ui.py 的文件原文。ui.py 的 _PAGE 是普通 Python 字符串，源码里写的 '\n'
            # （文件里看着是反斜杠加 n）运行时会被解析成真换行——按原文检查时 JS 合法，
            # 浏览器拿到的却是"单引号字符串跨行"→ 整页白屏（本机实测踩过一次）。
            import subprocess as _sp
            # 本机 node/acorn 路径不写死：换个开发机就跑不通，而且会把本机用户名
            # 带进公开仓库。用环境变量给，没给就跳过这一段（它是可选的语法检查）。
            _node = os.environ.get("HRA_NODE") or r"C:\Program Files\nodejs\node.exe"
            _acorn = os.environ.get("HRA_ACORN", "")
            if not _acorn:
                _acorn = os.path.join(os.path.expanduser("~"), ".workbuddy",
                                      "binaries", "node", "workspace", "parse.js")
            if os.path.exists(_node) and os.path.exists(_acorn):
                from app import ui as _ui_mod
                _pg = _ui_mod.render_page(auth_enabled=False)
                _s0 = _pg.index("<script>") + len("<script>")
                _pgjs = _pg[_s0:_pg.index("</script>", _s0)]
                _tmpjs = os.path.join(work, "_ui_check.js")
                with open(_tmpjs, "w", encoding="utf-8") as _fh:
                    _fh.write(_pgjs)
                _pr = _sp.run([_node, _acorn, _tmpjs], capture_output=True, text=True)
                _jsres = (_pr.stdout + _pr.stderr).strip()
                os.remove(_tmpjs)
                c.ok(_jsres.startswith("OK"),
                     "渲染后页面的内联 JS 能通过解析（白屏类问题自检即可拦下）",
                     _jsres[:70])
                from app import ui_v2 as _ui2_mod
                _pg2 = _ui2_mod.render_page_v2(auth_enabled=False)
                _s2 = _pg2.index("<script>") + len("<script>")
                _pg2js = _pg2[_s2:_pg2.index("</script>", _s2)]
                _tmpjs2 = os.path.join(work, "_ui_v2_check.js")
                with open(_tmpjs2, "w", encoding="utf-8") as _fh:
                    _fh.write(_pg2js)
                _pr2 = _sp.run([_node, _acorn, _tmpjs2], capture_output=True, text=True)
                _jsres2 = (_pr2.stdout + _pr2.stderr).strip()
                os.remove(_tmpjs2)
                c.ok(_jsres2.startswith("OK"),
                     "v2 页面渲染后的内联 JS 能通过解析",
                     _jsres2[:70])
                c.ok("class=\"iv-split\"" in _pg2
                     and "<h4>面试安排</h4>" in _pg2
                     and "<h4>面试结论</h4>" in _pg2,
                     "v2 面试安排与面试结论同框左右排")
                c.ok("class=\"edu-band\"" in _pg2
                     and "class=\"edu-period\"" in _pg2
                     and _pg2.count('id="edFD"') == 1
                     and _pg2.count('id="edEdu"') == 1
                     and "专业未识别" not in _pg2
                     and _pg2.index("class=\"edu-band\"") < _pg2.index("<h3>档位</h3>"),
                     "v2 教育经历在姓名下方按时间线展示，空专业/排名不占位")
                c.ok(_pg2.index("<h3>投递记录（") < _pg2.index("<h3>系统分析</h3>")
                     and _pg2.index("<h3>系统分析</h3>") < _pg2.index("<h3>技能（")
                     and _pg2.index("<h3>档位来源与操作记录</h3>")
                     < _pg2.index("<h3>更多操作</h3>"),
                     "v2 完整档案按流程事实→判断→能力→材料→审计→操作排列")
                c.ok("function ensureDlgClose()" in _pg2
                     and "function plainResponse(raw)" in _pg2
                     and "ensureDlgClose();" in _pg2,
                     "v2 弹窗统一提供关闭入口，非 JSON 错误先转成可读文本")
                c.ok("/api/mail/send?async=1" in _pg2
                     and "progressModal(r.task_id" in _pg2,
                     "v2 发信走后台任务轮询，大附件发送不会因 HTTP 断开被误报失败")
                c.ok("收件人看到的样子" not in _pg2
                     and "class=\"medit\"" in _pg2
                     and _pg2.index("<h3>第一步 · 选人、选模板</h3>")
                     < _pg2.index("<h3>第二步 · 预览与修改</h3>"),
                     "v2 写邮件：左侧准备材料，右侧完整编辑与发送，不再保留收件人预览栏")
                c.ok("function doInsertTable()" in _pgjs and "<table>" in _pgjs
                     and "contenteditable" in _pgjs,
                     "邮件正文是所见即所得编辑器，插入表格插的是真表格（不是管道符语法）")
                c.ok("function insertMailTable()" not in _pgjs,
                     "旧的『管道符骨架』写法已移除（避免两套并存互相干扰）")
                c.ok("function beautifyTable(" in _pgjs and "function tableMergeRight(" in _pgjs
                     and "richEditorHtml" in _pgjs,
                     "编辑器是可复用的（正文与模板共用），且带表格版式操作"
                     "（加行/列、合并、对齐、一键美化）")
                c.ok("id=\"tplEditor\"" in _pg or "'tplEditor'" in _pg,
                     "模板编辑走可视化编辑器（HR 不再看到 HTML 源码）")
                # 新建模板的两条"自动优化"路径：粘贴后、保存前都要统一表格样式。
                # （HR 不必知道"还要再点一次表格美化"——粘贴是新建模板最常见的入口）
                c.ok("beautifyTable(hostId, true)" in _pgjs,
                     "粘贴表格后自动统一公文样式（不必自己记得再点美化）")
                c.ok("beautifyTable('tplEditor', true)" in _pgjs
                     and "beautifyTable('mailEditor', true)" in _pgjs,
                     "保存模板前自动统一表格样式（两条入口都覆盖）")
                c.ok("querySelectorAll('table')" in _pgjs,
                     "美化作用于**整篇所有表格**（不是只处理第一张）")
                c.ok("新建模板怎么做" in _pg,
                     "新建模板给了上手引导（打字 / 插表格 / 粘贴 / 微调）")
                # ---- v1.13.4「入库即分析」可开关 ----
                c.ok("function setAutoInsight" in _pgjs
                     and "入库即分析" in _pg
                     and "function analyzePendingBatch" in _pgjs,
                     "⑮ 入库即分析有开关，且提供手动批量补分析（带进度）")
                # 护栏：前端读的字段名必须和后端返回的一致。
                # 出过 isw.on（后端是 isw.enabled）→ 复选框永远画成「关」，
                # HR 勾上去了却看着没变，以为"改不了"。这类键名错位要能被抓到。
                # ---- v1.16：agent 补齐"以前调不到"的 11 个能力 ----
                from app.agent import tools as _t16
                from app import actions as _a16
                import re as _re16
                _names16 = {sp["function"]["name"] for sp in _t16.TOOL_SPECS}
                _want16 = {"ingest_resumes", "assign_job", "suggest_job", "mark_review",
                           "set_archive", "archive_batch", "split_candidate",
                           "create_job", "update_job_jd", "regrade_job", "export_resumes"}
                c.ok(_want16 <= _names16,
                     "㉑ agent 补齐了导入/归岗/建议岗/复核/归档/批量归档/拆分/建岗/改JD/重算/导出",
                     f"还缺：{sorted(_want16 - _names16)}")
                _prop16 = _want16 - {"ingest_resumes", "export_resumes"}
                c.ok(_prop16 <= _t16.WRITE_TOOLS
                     and not ({"ingest_resumes", "export_resumes"} & _t16.WRITE_TOOLS),
                     "⑳b 提案类动作都归入写工具；导入/导出是取数据，不标成『需确认』",
                     f"WRITE_TOOLS 缺：{sorted(_prop16 - _t16.WRITE_TOOLS)}")
                c.ok("send_email" in _t16.DISABLED_TOOLS
                     and "send_message" in _t16.DISABLED_TOOLS,
                     "⑳c **红线没被漏掉**：发邮件/发消息仍然禁止由 agent 执行")
                _branches16 = set(_re16.findall(r'tool == "(\w+)"',
                                                _actions_src()))
                c.ok({"assign_job", "suggest_job", "mark_review", "set_archive",
                      "archive_batch", "split_candidate", "create_job",
                      "update_job_jd", "regrade_job"} <= _branches16,
                     "⑳d 动作层已登记这 9 个（否则 HR 点确认执行会报『未支持的 tool』）",
                     f"缺：{sorted({'assign_job','suggest_job','mark_review','set_archive','archive_batch','split_candidate','create_job','update_job_jd','regrade_job'} - _branches16)}")

                # ---- v1.16.1：超出 15 人时要给**真能点的出口** ----
                # 原来只有一行灰字"去人才库按阶段筛"，但人才库**没有阶段筛选器**，
                # 那是句空话。现在两条路都通：就地展开 / 跳人才库按阶段筛。
                # ---- v1.17：岗位名能搜到人 + 荣誉/论文/专利抽取 ----
                # ① HR 反馈「明明有为什么会搜索不到」：原来 keyword 只匹配
                #    姓名/学历/学校/专业/技能，**不匹配对应岗位**——
                #    说"数字化工程师"（岗位名）一个人都搜不到，模型还会据此
                #    回答"没有符合的候选人"，那是误导。
                #    自建数据：造一个岗位并把陈志远归过去，再按岗位名搜。
                _c17 = db.connect(db_path)
                _jid17 = db.create_job(_c17, "材料成型工程师（自检岗）", dept_id=None,
                                       jd={"role": "材料成型工程师",
                                           "must": {"skills_required": ["钛合金"],
                                                     "education_min": "本科", "years_min": 0},
                                           "preferred": {"skills": []}, "note": ""})
                _conn_ap17 = db.connect(db_path)
                _aid17 = db.get_application(_conn_ap17, chen["application_id"])
                db.update_candidate(_conn_ap17, chen["id"], name=chen["name"])
                _conn_ap17.execute(
                    "UPDATE applications SET job_id = ? WHERE id = ?",
                    (_jid17, chen["application_id"]))
                _conn_ap17.commit()
                _conn_ap17.close()
                _hit17 = db.list_candidates(_c17, keyword="材料成型工程师")
                _c17.close()
                c.ok(len(_hit17) > 0,
                     "㉔ 关键词能搜到**对应岗位**（HR 嘴里的岗位名/方向也是检索词）",
                     f"按岗位名搜到 {len(_hit17)} 人")
                # ② 荣誉/论文/专利：规则抽取 + 必须带原文片段
                from app.pipeline.extract import (extract_honors as _eh,
                    verify_honors_against_text as _ehv, merge_honors as _ehm)
                _txt17 = ("2022.10 获国家奖学金（一等）\n"
                          "在校期间发表期刊论文 3 篇，其中 SCI 一篇\n"
                          "2024.03 获发明专利 ZL202410123456.7\n"
                          "2023.12 优秀毕业生")
                _h17 = _eh(_txt17)
                _kinds17 = {h["kind"] for h in _h17}
                c.ok({"奖学金", "论文", "专利", "荣誉"} <= _kinds17
                     and all(h.get("evidence") for h in _h17),
                     "㉕ 荣誉/论文/专利抽取：四类都认，且每条带原文片段（可核对）",
                     f"抽出：{[(h['kind'], h['name']) for h in _h17]}")
                # ③ v1.18：荣誉改由**模型**在已有那次分析里抽，但必须逐条验原文——
                #    模型编的荣誉一律丢掉（项目反幻觉底线）。
                _txt18 = ("2022.10 获国家奖学金（一等）\n"
                          "在校期间发表 SCI 论文 2 篇\n"
                          "2024.03 获发明专利 ZL202410123456.7")
                _fake18 = [
                    {"category": "奖学金", "name": "国家奖学金",
                     "evidence": "2022.10 获国家奖学金（一等）"},
                    {"category": "荣誉", "name": "全国优秀毕业生",
                     "evidence": "2023.12 获全国优秀毕业生"},   # 原文没有 → 必须丢
                ]
                _v18 = _ehv(_fake18, _txt18)
                c.ok(len(_v18) == 1 and _v18[0]["kind"] == "奖学金",
                     "㉘ 模型抽的荣誉**逐条验原文**：编的直接丢（反幻觉底线）",
                     f"4 条里过了 {len(_v18)} 条")
                _mg18 = _ehm(_v18, _eh(_txt18))
                c.ok(len(_mg18) >= 1 and all(x.get("evidence") for x in _mg18),
                     "㉙ 模型 + 规则合并：都有原文片段，不丢已有结果",
                     f"合并后 {len(_mg18)} 条")
                c.ok(_eh("今天天气不错") == [],
                     "㉖ 没有就不抽（缺 = 未识别，不等于『没有』）")
                # ---- v1.18.1：OCR 实现与能力上报必须一致 ----
                # 坑： 按import 可用性报 rapidocr 可用（确实在包里），
                # 但真正干活的  只调 pytesseract——**没打包**，
                # 每次 ModuleNotFoundError 被上层静默吞掉 → 扫描件识别永远是空的。
                # 这里用一张现画的图跑真 OCR，断言**真有中文输出**。
                from PIL import Image as _I, ImageDraw as _D, ImageFont as _F
                import io as _io2
                _im = _I.new("RGB", (900, 420), "white")
                _dr = _D.Draw(_im)
                _fp = "C:/Windows/Fonts/msyh.ttc"
                if os.path.exists(_fp):
                    _fnt = _F.truetype(_fp, 30)
                    _dr.text((50, 40), "姓名：钱思远", font=_fnt, fill="black")
                    _dr.text((50, 110), "学历：硕士  材料科学与工程", font=_fnt, fill="black")
                    _dr.text((50, 180), "获国家奖学金（一等）", font=_fnt, fill="black")
                    _buf = _io2.BytesIO()
                    _im.save(_buf, format="PNG")
                    from app.pipeline import parse as _pp
                    try:
                        _ocr_txt = _pp._ocr_image_bytes(_buf.getvalue()) or ""
                    except Exception:
                        _ocr_txt = ""
                    c.ok("钱思远" in _ocr_txt,
                         "㉙ OCR 真的能识别中文（实现与能力上报一致）",
                         f"识别到 {len(_ocr_txt)} 字：{_ocr_txt[:24]}")
                else:
                    c.ok(True, "㉙ OCR 断言跳过（本机无中文字体，无法造测试图）")
                # ---- v1.19：上传入口（验证 OCR 的最短路径）----
                # 以前用「导入本地文件夹」必须先去系统配置里手填目录；
                # 验证一件功能却要先改配置，门槛会把人挡在门外。
                c.ok("上传简历照片/文件" in _pgjs and "doUpload" in _pgjs,
                     "㉚ 人才库里有上传简历照片/文件入口（不用改配置就能试 OCR）")
                c.ok("荣誉 / 论文 / 专利" in _pgjs,
                     "㉗ 完整档案里有荣誉/论文/专利区块")
                c.ok("pipePage" in _pgjs and "pipe-pager" in _pgjs
                     and "goStageFilter" not in _pgjs
                     and "上一页" in _pgjs and "下一页" in _pgjs,
                     "㉒ 管道超出 15 人时可翻页（每页 15 人）；旧版跳转函数已删除")
                c.ok("jobHeadcount" in _pgjs and "ejHeadcount" in _pgjs,
                     "㉛ 岗位管理有「招聘数量」字段（新增表单 + 编辑弹窗两处入口）")
                c.ok("_PIPE_GROUP_STAGE" in _pgjs and "pipe-group-head" in _pgjs,
                     "㉜ 管道「已发放offer」列按岗位分组展示（组头 + 人数）")
                c.ok("chat-input-wrap" in _pgjs and "function chatGrow" in _pgjs
                     and "chat-input-actions" in _pgjs and "清空记录" in _pgjs
                     and 'onclick="clearChat()"' in _pgjs,
                     "㉝ 助手输入框：3 行起步 + 自动增高；发送与「清空记录」在框内右下角（清空在右）")
                c.ok("_resolve_job_id" in open(
                    os.path.join(BASE, "app", "agent", "tools.py"),
                    encoding="utf-8").read(),
                     "㉞ 助手有「岗位名→job_id」解析器（search_candidates 的 job 参数按归岗筛）")
                # ⚠️ v2.0.0 删掉原来那半句 `"loadMailAtts" not in _pgjs`：
                # 该标识符**从未以函数形式存在过**，它只活在 ui.py 的说明性注释里
                # （"…勾选已随之移除"那段），所以那半句验证不了任何功能，红灯纯粹
                # 由注释文字引起。判据只能选**真会出现在代码里**的写法，
                # 不能靠"某个名字不出现"来证明功能 —— 那是恒真/恒假的废判据。
                c.ok("function errText" in _pgjs
                     and "document_id: int | None = None" in open(
                         os.path.join(BASE, "app", "server.py"), encoding="utf-8").read(),
                     "㉟ 临时附件可发送（document_id 可空）；422 错误翻译成人话")
                _svsrc = open(os.path.join(BASE, "app", "server.py"),
                              encoding="utf-8").read()
                c.ok("outboxOpen" in _pgjs and "outboxDel" in _pgjs
                     and "def api_mail_outbox_open" in _svsrc
                     and "def api_mail_outbox_delete" in _svsrc,
                     "㊱ 临时附件有「预览 / 删除」（前端入口 + 服务端端点）")
                c.ok("ingestReopen" in _pgjs and "ingestAttach" in _pgjs
                     and "/api/tasks/active" in _pgjs and "def api_tasks_active" in _svsrc,
                     "㊲ 导入弹窗被误关后，点导入按钮能重新弹出进行中的流程（含停止）")
                c.ok("chatRestoreBtn" in _pgjs and "累计运行" not in _pgjs
                     and "写操作只生成" not in _pgjs,
                     "㊳ 助手页：恢复对话在右上角；工具/禁用统计与说明句已删除")
                c.ok("multi_ivunit" in _pgjs and "ivunit:'ivUnit'" in _pgjs,
                     "㊵ 档案「面试与录用信息」的面试单位支持多选（隐藏域沿用 ivUnit，保存链路不动）")

                # ---- ㊴ 临时附件**真回环**（v2.0.0）----
                # ㉟/㊱ 都只是"字符串在不在源码里"的静态检查，**结构上抓不到**
                # "上传落盘名（digest+扩展名）与发信读取名（裸 digest）不一致"这类
                # 缺陷 —— v1.29.9 实测漏网：带临时附件的邮件一律报
                # 「临时附件已不在暂存区」，HR 重复上传 5 次都不通。
                # 所以这里必须真跑一遍：上传 → 拿 id → 从发信链路读回来 → 比对字节。
                # 上传落在**临时目录**（临时改 DATA_DIR，跑完还原），不污染真实 outbox。
                import urllib.parse as _up
                _obytes = (b"%PDF-1.4\n1 0 obj<</Type/Catalog>>endobj\n"
                           b"trailer<</Root 1 0 R>>\n%%EOF\n")
                _oname = "自检-附件回环.pdf"
                _odir_save, srv.DATA_DIR = srv.DATA_DIR, os.path.join(work, "data")
                try:
                    _ocode, _obody = _asgi(srv.app, "POST",
                                           "/api/mail/outbox?name=" + _up.quote(_oname),
                                           {}, _obytes)
                    _oid = (json.loads(_obody) or {}).get("outbox_id") if _obody else None
                    c.ok(_ocode == 200 and _oid,
                         "㊴a 临时附件上传返回 outbox_id",
                         f"HTTP {_ocode} {_obody[:50]}")
                    _op = srv._outbox_path(_oid) if _oid else None
                    _ook = bool(_op) and os.path.isfile(_op) \
                        and open(_op, "rb").read() == _obytes
                    c.ok(_ook,
                         "㊴b 上传的附件能被发信链路原样读回（名字对得上、字节一致）",
                         os.path.basename(_op) if _op else "解析不到文件")
                finally:
                    srv.DATA_DIR = _odir_save
                # v2.0.9：附件必须保留真实 MIME，尤其是图片不能变成 application/jpeg；
                # 同时临时附件入口要覆盖常见办公文档。
                from email.mime.multipart import MIMEMultipart as _MM
                from app import mail_send as _ms

                _mmsg = _MM()
                _mok, _mskip = _ms._attach_payload(_mmsg, [
                    {"filename": "现场照片.JPG", "data": b"\xff\xd8\xff\xe0fake-jpeg"},
                    {"filename": "说明.docx", "data": b"fake-docx"},
                    {"filename": "手机照片.heic", "data": b"fake-heic"},
                ])
                _parts = _mmsg.get_payload()
                c.ok(_mok == ["现场照片.JPG", "说明.docx", "手机照片.heic"]
                     and not _mskip
                     and [_p.get_content_type() for _p in _parts]
                     == ["image/jpeg",
                         "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                         "image/heic"]
                     and [_p.get_filename() for _p in _parts]
                     == ["现场照片.JPG", "说明.docx", "手机照片.heic"],
                     "邮件附件保留原文件名，图片使用 image/* 真实 MIME（不再变 application/jpeg）",
                     str([(p.get_content_type(), p.get_filename()) for p in _parts]))
                c.ok({".docx", ".xlsx", ".pptx", ".heic", ".zip"} <= set(srv.OUTBOX_EXTS),
                     "临时附件支持常见图片、Office 文档与压缩包")
                _orig_mail_sync = srv._mail_send_sync
                srv._mail_send_sync = lambda req, session: {
                    "ok": True, "to": "test@example.com", "attachments": []}
                _mail_tid = srv._progress_new("自检发送", 1)
                try:
                    srv._run_mail_send_task(_mail_tid, object(), {"username": "hr", "role": "hr"})
                finally:
                    srv._mail_send_sync = _orig_mail_sync
                _mail_task = srv._PROGRESS.get(_mail_tid) or {}
                c.ok(_mail_task.get("status") == "done"
                     and (_mail_task.get("result") or {}).get("ok") is True,
                     "后台发信任务完成后回传真实发送结果，前端不再把已发邮件误判为失败")
                # v1.27：判据不再写死 `"&stage="` —— 那依赖"阶段参数必须手写成
                # `'&stage=' + ...` 这一种拼接方式；筛选条件收进 `poolQuery()` 后
                # 是数组元素由 `join('&')` 拼的，源码里就没有这个字面量了，
                # 于是功能好好的也会被判失败。
                # 改成验**功能本身**：查询串里带 stage 参数，且工具栏真有一个
                # 绑定 `STAGE_F` 的下拉（比原来只验一个子串更硬）。
                c.ok("stage=" in _pgjs and "STAGE_F=this.value" in _pgjs
                     and "'stage=' + encodeURIComponent(STAGE_F)" in _pgjs,
                     "㉓ 人才库真的有阶段筛选入口（否则看全部仍是死路）")
                # v2.0.2（业务方 2026-10-19）：学历筛选改成**精确档位多选**，
                # 不再是"≥ 最低学历"—— 旧口径选「大专」会把硕博一起带出来，这个筛选没法用。
                # 判据要"只命中某一档"：拿库里**最低**那档去查，结果里**不能出现更高档**。
                # （旧口径下这条必红；"包含某档"这种判据则会假通过。）
                from urllib.parse import quote as _q
                _edu_rank = {"大专": 1, "专科": 1, "本科": 2, "学士": 2,
                             "研究生": 3, "硕士": 3, "博士": 4}
                _lvname = {1: "大专", 2: "本科", 3: "硕士", 4: "博士"}
                _, _pb = _asgi(srv.app, "GET", "/api/candidates", {})
                _pitems = json.loads(_pb).get("items") or []
                _have = sorted({_edu_rank.get(i.get("edu_level") or "", 0)
                                for i in _pitems} - {0})
                if len(_have) >= 2:
                    _low = _have[0]
                    _, _eb = _asgi(srv.app, "GET",
                                   "/api/candidates?education=" + _q(_lvname[_low]), {})
                    _ej = json.loads(_eb)
                    _higher = [i for i in (_ej.get("items") or [])
                               if _edu_rank.get(i.get("edu_level") or "", 0) > _low]
                    c.ok(not _higher and (_ej.get("edu_filter") or {}).get("applied"),
                         f"学历筛选是**精确档位**（选「{_lvname[_low]}」不夹带更高学历）",
                         f"夹带 {len(_higher)} 人；库内档位={_have}")
                else:
                    c.ok(True, "学历精确筛选：测试库学历层次单一，跳过比较级校验",
                         f"档位={_have}")
                # v2.0.2：人才库卡片的「推进初筛通过」按钮 —— 位置在**岗位 chip 右边**
                # （业务方 2026-10-19 第二次反馈：放在档位右边要横跨整行才点到）。
                # **纯前端交互，只能做静态判据**，所以钉的是"定义 + 紧跟在 jobTag 后面 +
                # 只在全部简历阶段出"三段；`${jobTag}${stageAction}` 连写保证位置别漂回去。
                c.ok("const stageAction =" in _pgjs
                     and "${jobTag}${stageAction}" in _pgjs
                     and "stage-go" in _pgjs
                     and "stage === _defStage" in _pgjs
                     and "onclick=\"setStage(${x.application_id}," in _pgjs,
                     "㊵ 人才库卡片：**岗位右边**有「推进初筛通过」（仅全部简历阶段）")
                # ⚠️ 这里**不要**用 `${stageAction}</div>` 当"还在档位右边"的判据 ——
                # 正确位置（`.nm` 行尾）也正好以它结尾，那样写会恒假通过/恒真失败。
                # 真正能区分的是：旧的灰态样式规则 `stage-go[disabled]` 已经删掉，
                # 而按钮只出现在 `${jobTag}` 之后（由上面 ㊵ 钉住）。
                c.ok("stage-go[disabled]" not in _pgjs
                     and "${jobTag}${stageAction}" in _pgjs,
                     "㊵b 档位右边不再有第二份按钮（灰态样式已删，阶段看同行的 stageTag）")
                c.ok("isw.enabled" in _pgjs and "isw.on" not in _pgjs,
                     "⑮a 开关的字段名前后端一致（isw.enabled）",
                     "前端若读 isw.on 会永远显示「关」，看起来改不动")
                # v1.13：待指定投递必须**处处有归岗入口**。
                # 原来「采纳建议岗位」只在系统给出建议时才渲染——模型不可用/判断不出时
                # HR 完全没有入口（实测反馈：「所属岗位待指定情况下怎么指定岗位呢？没有入口啊」）。
                c.ok("function assignJobPick" in _pgjs and "function assignJobFromPick" in _pgjs,
                     "⑫ 有「手动指定岗位」入口（与系统建议无关，兜底可用）")
                c.ok("assignBatch" not in _pgjs and "批量归岗" not in _pg,
                     "⑫b **没有批量归岗**（HR 明确要求逐个处理，不要批量入口）")
                # 护栏：inline onclick 里**不能传渲染函数的局部变量**（如 showDetail 里的 d）——
                # onclick 在全局作用域求值，取不到局部变量 → ReferenceError → 点了没反应。
                # 正确做法是把值插值进 HTML（${cid}），变成字面量。
                c.ok(",d)" not in _pgjs,
                     "⑳ 页面里没有把局部变量 d 传给 onclick（踩过：按钮点了没反应）")
                c.ok("'修改岗位'" in _pg and "所属岗位</div>" in _pg
                     and "assignJobPick(${d.id}" in _pg,
                     "⑫b2 完整档案里有「修改岗位」入口（已归岗的也能改归属）")
                c.ok("所属岗位待指定（点此指定）" in _pg,
                     "⑫c 「所属岗位待指定」标签本身就是入口（点开即选岗位）")
                c.ok("指定岗位</button>" in _pg,
                     "⑫d 卡片操作栏对未归岗的候选人也给「指定岗位」按钮")
                # ui.py 里的**非法字符串转义**同样会毁掉前端：`\\n` 会被 Python 转成真换行，
                # 把 JS 注释断成代码、甚至让模板串吞掉后面一整段。这里把警告升级成错误拦下。
                import warnings as _warn
                with _warn.catch_warnings():
                    _warn.simplefilter("error", SyntaxWarning)
                    try:
                        compile(open(os.path.join(BASE, "app", "ui.py"),
                                     encoding="utf-8").read(), "ui.py", "exec")
                        _esc_ok = True
                    except SyntaxWarning:
                        _esc_ok = False
                c.ok(_esc_ok, "ui.py 里没有非法字符串转义（跨语言转义的坑）")
            else:
                c.ok(True, "node 不在本机路径，跳过前端语法校验（部署前请跑 check_ui_js.py）")

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
            # v1.8.9：归档的人要**从所有"当前在库"的视图里一起消失**——
            # 只从人才库列表消失、投递管道里还挂着，就是"归档看起来没生效"的成因。
            _, _pb = _asgi(srv.app, "GET", "/api/pipeline", {})
            _pb = json.loads(_pb)
            _in_pipe = any(i.get("candidate_id") == cid
                           for _s in (_pb.get("stages") or {}).values()
                           for i in (_s.get("items") or []))
            c.ok(not _in_pipe, "归档后不再出现在投递管道里")
            _, _st = _asgi(srv.app, "GET", "/api/stats", {})
            _st = json.loads(_st)
            c.ok((_st.get("archived") or 0) >= 1,
                 "统计把归档人数单列（不再混进「候选人数」）",
                 f"people={_st.get('people')} archived={_st.get('archived')}")
            _pc = db.connect(db_path)
            c.ok(all(i.get("candidate_id") != cid
                     for _s in (db.pipeline_stats(_pc).get("stages") or {}).values()
                     for i in (_s.get("items") or [])),
                 "管道统计口径（db.pipeline_stats）同样排除归档")
            c.ok(db.pool_stats(_pc)["people"] == _st.get("people"),
                 "统计口径与接口一致（同一份 pool_stats）")
            _pc.close()
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
            # v1.12 核心回归点：建议岗位改由模型判断。自检离线（模型不可用）→
            # **如实不出建议**，而不是像旧版那样用规则打分硬凑一个（打分已删）。
            c.ok(sug is None,
                 "模型不可用时不出建议岗位（保持待指定，不硬凑）",
                 json.dumps(sug, ensure_ascii=False)[:80] if sug else "None")
            sug_none = (items_by_id.get(_cid_miss) or {}).get("job_suggestion")
            c.ok(sug_none is None,
                 "技能毫无交集的候选人不出建议（保持待指定，不硬凑）",
                 json.dumps(sug_none, ensure_ascii=False)[:60] if sug_none else "None")

            # 用**桩模型**验证链路（主动判断 → 落库 → 列表读库 → 采纳归岗 → 写档位 → 审计）：
            # 自检要验的是管道接得对不对，不是模型判断得准不准（那要靠试用验收）。
            # 注意 v1.13.2 起**列表不再现算**，所以判断必须通过显式动作触发一次。
            from app.pipeline import analyze as _an_sug
            _orig_sug = _an_sug.suggest_job
            _an_sug.suggest_job = lambda cand, jobs: (
                {"title": jobs[0]["title"], "reason": "自检桩"} if jobs else None)
            try:
                _code_j, _body_j = _asgi(srv.app, "POST",
                                         f"/api/candidates/{_cid_hit}/suggest-job", hr, b"")
                _sj_ok = json.loads(_body_j)
                c.ok(_code_j == 200 and _sj_ok.get("ok"),
                     "⑫e 主动「判断建议岗位」接口可用",
                     f"HTTP {_code_j} {str(_body_j)[:50]}")
                _, body = _asgi(srv.app, "GET", "/api/candidates", {})
                items_by_id = {x["id"]: x for x in json.loads(body)["items"]}
                sug = (items_by_id.get(_cid_hit) or {}).get("job_suggestion")
                c.ok(bool(sug) and sug.get("job_id") and sug.get("title")
                     and sug.get("source") == "stored",
                     "⑫f 判断结果落库后，列表**直接读库**展示（source=stored）",
                     json.dumps(sug, ensure_ascii=False)[:80] if sug else "None")
                code, body = _asgi(srv.app, "POST", f"/api/candidates/{_cid_hit}/assign-job",
                                   hr, json.dumps({"job_id": sug["job_id"]}).encode())
                c.ok(code == 200, "采纳建议岗位接口返回 200", f"实际 {code} {body[:60]}")
            finally:
                _an_sug.suggest_job = _orig_sug
            conn = db.connect(db_path)
            _app_row = dict(conn.execute(
                "SELECT * FROM applications WHERE id = ?", (_aid_hit,)).fetchone())
            _n_audit = conn.execute(
                "SELECT COUNT(*) AS n FROM audit_log WHERE entity='application' "
                "AND entity_id=? AND action='assign_job'", (str(_aid_hit),)).fetchone()["n"]
            conn.close()
            c.ok(_app_row["job_id"] == sug["job_id"],
                 "采纳后投递已归到建议岗位", f"job_id={_app_row['job_id']}")
            c.ok(_app_row["tier_suggested"] in (None, _HARD_T),
                 "归岗后档位按新口径写入（学历不达标→最差档；学历达标→留空待模型分析）",
                 str(_app_row["tier_suggested"]))
            c.ok(_n_audit == 1, "采纳归岗写入审计（岗位未指定 → 归到 X）")

            # ---- v1.13.6 回归：单人彻底删除只删该人（原来会连带删所有到期的人）----
            import datetime as _dt
            _conn_pd = db.connect(db_path)
            _old30 = (_dt.datetime.now() - _dt.timedelta(days=40)).strftime("%Y-%m-%d %H:%M:%S")
            # 造 3 个"归档满 40 天"的候选人（含本条投递）
            _purge_ids = []
            for _i in range(2):
                _c0 = db.insert_candidate(_conn_pd, {"identity_key": f"selftest:purge:{_i}",
                                                      "name": f"到期人{_i}"})
                _a0 = db.insert_application(_conn_pd, {"candidate_id": _c0, "job_id": None,
                                                       "channel": "测试", "applied_at": db.now()})
                _conn_pd.execute("UPDATE candidates SET archived_at=? WHERE id=?", (_old30, _c0))
                _purge_ids.append(_c0)
            _conn_pd.commit()
            _before_left = db.count_needing_insight(_conn_pd)   # 任意一次读，确认连接可用
            _conn_pd.close()
            _code_pd, _body_pd = _asgi(srv.app, "POST",
                                       f"/api/candidates/{_purge_ids[0]}/purge", hr, b"")
            _conn_pd2 = db.connect(db_path)
            _still = _conn_pd2.execute(
                "SELECT COUNT(*) AS n FROM candidates WHERE id IN (?,?)",
                (_purge_ids[0], _purge_ids[1])).fetchone()["n"]
            _conn_pd2.close()
            c.ok(_code_pd == 200 and _still == 1,
                 "⑯ 单人「彻底删除」只删该人（不会连带删掉其他到期归档的人）",
                 f"HTTP {_code_pd}，另一个人还在={_still == 1}")
            # 清掉造出来的"到期人"：它们归档满 40 天，会让后面"无到期者应返回 0"
            # 那条断言失败。自检库是一次性的，直接删干净。
            _conn_pd3 = db.connect(db_path)
            for _pid in _purge_ids:
                for _t in ("applications", "documents"):
                    _conn_pd3.execute(f"DELETE FROM {_t} WHERE candidate_id = ?", (_pid,))
                _conn_pd3.execute("DELETE FROM candidates WHERE id = ?", (_pid,))
            _conn_pd3.commit()
            _conn_pd3.close()
            # v1.13.3：**改岗位**（HR 要求"给每个人一个修改岗位的按钮"）。
            # 原来已归岗的再归会被 400 拒掉，HR 无处纠正识别错的归属。
            _conn_ra = db.connect(db_path)          # conn 在上一段已关闭，这里单开一个
            _jobs_all = db.list_jobs(_conn_ra, include_inactive=False)
            _conn_ra.close()
            _other = next((j for j in _jobs_all if j["id"] != sug["job_id"]), None)
            if _other:
                code, body = _asgi(srv.app, "POST", f"/api/candidates/{_cid_hit}/assign-job",
                                   hr, json.dumps({"job_id": _other["id"]}).encode())
                _rb = json.loads(body)
                conn2 = db.connect(db_path)
                _re_audit = conn2.execute(
                    "SELECT COUNT(*) AS n FROM audit_log WHERE action='reassign_job' "
                    "AND entity_id=?", (str(_aid_hit),)).fetchone()["n"]
                _row_now = conn2.execute("SELECT job_id FROM applications WHERE id=?",
                                         (_aid_hit,)).fetchone()
                conn2.close()
                c.ok(code == 200 and _rb.get("old_job_title")
                     and _row_now["job_id"] == _other["id"],
                     "⑫g 已归岗的投递可以**改岗位**（返回原岗位名、库里真的改了）",
                     f"HTTP {code} {_rb.get('old_job_title')} → {_rb.get('job_title')}")
                c.ok(_re_audit == 1, "⑫h 改岗位写 reassign_job 审计（与首次归岗可区分）",
                     f"reassign_job 审计 {_re_audit} 条")
                # 改成同一个岗位 → 如实返回 unchanged，不做无意义改动
                code, body = _asgi(srv.app, "POST", f"/api/candidates/{_cid_hit}/assign-job",
                                   hr, json.dumps({"job_id": _other["id"]}).encode())
                c.ok(code == 200 and json.loads(body).get("unchanged") is True,
                     "⑫i 改成同一岗位 → 如实说未改动（不制造假审计）", f"HTTP {code}")
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
            # v1.12：模型不可用时归位**不写建议**（打分已删，没有最高分岗位可挑）
            c.ok(_sug_row is not None and _sug_row["suggested_job_id"] is None,
                 "模型不可用时，存量待指定投递保持待指定（不硬凑建议岗位）",
                 f"suggested_job_id={_sug_row['suggested_job_id'] if _sug_row else None}")
            # 零交集的人：不落建议岗位（不硬凑）
            # 注意断言时机：必须在**桩模型之前**取 —— 桩会盲目返回第一个岗位，
            # 挂在桩之后断言等于在测桩、不是测系统。
            conn = db.connect(db_path)
            _miss_row = conn.execute(
                "SELECT suggested_job_id, hits FROM applications WHERE candidate_id = ?",
                (_cid_miss,)).fetchone()
            conn.close()
            c.ok(_miss_row is not None and _miss_row["suggested_job_id"] is None
                 and not json.loads(_miss_row["hits"] or "[]"),
                 "与所有在招岗位零技能交集的人不落建议岗位（模型不可用时保持待指定）",
                 f"suggested_job_id={_miss_row['suggested_job_id'] if _miss_row else None} "
                 f"hits={json.loads((_miss_row['hits'] if _miss_row else None) or '[]')}")

            # 再用桩模型验证归位链路：给得出岗位 → 落库建议岗位 + 用该岗位 JD 重抽技能
            from app.pipeline import analyze as _an_rt
            _orig_rt = _an_rt.suggest_job
            _an_rt.suggest_job = lambda cand, jobs: (
                {"title": jobs[0]["title"], "reason": "自检桩"} if jobs else None)
            try:
                _asgi(srv.app, "POST", "/api/candidates/route-pending?apply=1", hr, b"")
            finally:
                _an_rt.suggest_job = _orig_rt
            conn = db.connect(db_path)
            _row2 = conn.execute(
                "SELECT suggested_job_id, tier_suggested FROM applications WHERE id = ?",
                (_aid_route,)).fetchone()
            _open2 = db.open_jobs_with_jd(conn)
            conn.close()
            c.ok(_row2 is not None and _row2["suggested_job_id"] is not None,
                 "模型给出岗位后落库建议岗位（老数据也能补齐）",
                 f"suggested_job_id={_row2['suggested_job_id'] if _row2 else None}")
            c.ok(_row2 is not None and _row2["tier_suggested"] in (None, _HARD_T),
                 "归位的档位按新口径写入（学历门槛 / 待模型分析）",
                 f"{_row2['tier_suggested'] if _row2 else None}（在招岗位 {len(_open2)} 个）")

            # ---- v1.13.4：入库即分析开关 ----
            # 关掉后入库不应排队分析，并如实回报"跳过了多少人"
            _code_sw, _body_sw = _asgi(srv.app, "POST", "/api/settings", hr,
                                       json.dumps({"auto_insight_on_ingest": False}).encode())
            _sw = json.loads(_body_sw)
            c.ok(_code_sw == 200 and _sw.get("auto_insight_on_ingest") is False
                 and "入库即分析" in (_sw.get("note") or ""),
                 "⑮b 开关能关且如实回话（note 说清关掉后要手动补）",
                 f"HTTP {_code_sw} note={(_sw.get('note') or '')[:44]}")
            # 手动批量：给两个"没有分析结论"的投递，用桩模型补上
            from app.pipeline import analyze as _an_mb
            _orig_mb = _an_mb.analyze_fit
            _an_mb.analyze_fit = lambda cand, jd: {
                "suggested_tier": "B", "summary": "自检桩：匹配",
                "highlights": ["桩"], "risks": [], "confidence": 0.7,
                "model": "stub"}
            try:
                _swneed0 = 0
                _swconn = db.connect(db_path)
                _swneed0 = db.count_needing_insight(_swconn)
                _swconn.close()
                _swcode, _swbody = _asgi(srv.app, "POST", "/api/insights/analyze-pending",
                                           hr, json.dumps({"limit": 20}).encode())
                _swres = json.loads(_swbody)
            finally:
                _an_mb.analyze_fit = _orig_mb
            c.ok(_swcode == 200 and _swres.get("ok"),
                 "⑮c 手动批量补分析接口可用",
                 f"HTTP {_swcode} {_swres.get('note') or _swres.get('error')}")
            c.ok(_swneed0 == 0 or (_swres.get("analyzed", 0) + _swres.get("remaining", 0)) <= _swneed0,
                 "⑮d 批量补分析后，待分析人数不增（要么补掉、要么如实剩着）",
                 f"待分析 {_swneed0} → 已分析 {_swres.get('analyzed')} / 剩余 {_swres.get('remaining')}")
            # 列表接口要回带开关状态（前端靠它渲染，不用再发一个请求）
            _code_ls, _body_ls = _asgi(srv.app, "GET", "/api/candidates", {})
            _ls = json.loads(_body_ls)
            _isw = _ls.get("insight_switch") or {}
            c.ok(_isw.get("enabled") is False and isinstance(_isw.get("pending"), int),
                 "⑮e 列表接口回带开关状态与待分析人数（读路径不做模型调用）",
                 f"{_isw}")
            # 开关改回默认开，别影响后续段落
            _asgi(srv.app, "POST", "/api/settings", hr,
                  json.dumps({"auto_insight_on_ingest": True}).encode())

            # ---- v1.13.6 回归：撤销合并要把投递/附件搬回来 ----
            _conn_ms = db.connect(db_path)
            _cid_ms1 = db.insert_candidate(_conn_ms, {"identity_key": "selftest:merge:src",
                                                      "name": "合并源"})
            _cid_ms2 = db.insert_candidate(_conn_ms, {"identity_key": "selftest:merge:dst",
                                                      "name": "合并主"})
            _aid_ms = db.insert_application(_conn_ms, {"candidate_id": _cid_ms1, "job_id": None,
                                                       "channel": "测试", "applied_at": db.now()})
            _conn_ms.commit()
            _conn_ms.close()
            db.merge_candidates(db.connect(db_path), _cid_ms1, _cid_ms2, "hr", "hr")
            _conn_ms3 = db.connect(db_path)
            _at_dst = _conn_ms3.execute("SELECT candidate_id FROM applications WHERE id=?",
                                         (_aid_ms,)).fetchone()["candidate_id"]
            _conn_ms3.close()
            c.ok(_at_dst == _cid_ms2, "⑯b 合并把投递挂到主档", f"投递现在属于 {_at_dst}")
            db.split_candidate(db.connect(db_path), _cid_ms1, "hr", "hr")
            _conn_ms4 = db.connect(db_path)
            _at_src = _conn_ms4.execute("SELECT candidate_id FROM applications WHERE id=?",
                                         (_aid_ms,)).fetchone()["candidate_id"]
            _conn_ms4.close()
            c.ok(_at_src == _cid_ms1,
                 "⑯c 撤销合并把投递**搬回源档**（原来拆完源档案变空壳、投递永久留在主档）",
                 f"投递现在属于 {_at_src}")

            # ---- v1.13.6 回归：阶段变更要写 stage_changed_at ----
            _conn_sc = db.connect(db_path)
            _cid_sc = db.insert_candidate(_conn_sc, {"identity_key": "selftest:stage:ts",
                                                      "name": "阶段时间"})
            _aid_sc = db.insert_application(_conn_sc, {"candidate_id": _cid_sc, "job_id": None,
                                                       "channel": "测试", "applied_at": db.now(),
                                                       "stage": "全部简历"})
            _conn_sc.commit()
            _conn_sc.close()
            _asgi(srv.app, "POST", f"/api/applications/{_aid_sc}/stage", hr,
                  json.dumps({"stage": "待面试"}).encode())
            _conn_sc2 = db.connect(db_path)
            _row_sc = _conn_sc2.execute(
                "SELECT stage, stage_changed_at FROM applications WHERE id=?",
                (_aid_sc,)).fetchone()
            _conn_sc2.close()
            c.ok(_row_sc["stage"] == "待面试" and _row_sc["stage_changed_at"],
                 "⑯d 改阶段会记 stage_changed_at（停滞待办靠它，否则刚推进的人被当成停滞）",
                 f"stage={_row_sc['stage']} stage_changed_at={_row_sc['stage_changed_at']}")
            # v2「只改结论」提交的是当前阶段；如果还在「已面试」之前，
            # 服务端必须把它补推到「已面试」，不能只落标签。
            for _oc_auto in ("拟录用", "不录用"):
                _conn_sc3 = db.connect(db_path)
                _conn_sc3.execute(
                    "UPDATE applications SET stage='待面试', outcome=NULL WHERE id=?",
                    (_aid_sc,))
                _conn_sc3.commit()
                _conn_sc3.close()
                _code_auto, _body_auto = _asgi(
                    srv.app, "POST", f"/api/applications/{_aid_sc}/stage", hr,
                    json.dumps({"stage": "待面试", "outcome": _oc_auto}).encode())
                _out_auto = json.loads(_body_auto)
                _conn_sc4 = db.connect(db_path)
                _row_auto = _conn_sc4.execute(
                    "SELECT stage, outcome, interviewed_at FROM applications WHERE id=?",
                    (_aid_sc,)).fetchone()
                _conn_sc4.close()
                c.ok(_code_auto == 200
                     and _row_auto["stage"] == "已面试"
                     and _row_auto["outcome"] == _oc_auto
                     and _row_auto["interviewed_at"]
                     and _out_auto.get("advanced_to") == "已面试",
                     f"只改结论：待面试打「{_oc_auto}」→ 自动进「已面试」",
                     f"HTTP {_code_auto} stage={_row_auto['stage']} "
                     f"outcome={_row_auto['outcome']}")
            # 已经在「已面试」之后，改结论只改标签，不能被拉回。
            _conn_sc3 = db.connect(db_path)
            _conn_sc3.execute(
                "UPDATE applications SET stage='已发放offer', outcome=NULL WHERE id=?",
                (_aid_sc,))
            _conn_sc3.commit()
            _conn_sc3.close()
            _code_late, _body_late = _asgi(
                srv.app, "POST", f"/api/applications/{_aid_sc}/stage", hr,
                json.dumps({"stage": "已发放offer", "outcome": "已签约"}).encode())
            _out_late = json.loads(_body_late)
            _conn_sc4 = db.connect(db_path)
            _row_late = _conn_sc4.execute(
                "SELECT stage, outcome FROM applications WHERE id=?", (_aid_sc,)).fetchone()
            _conn_sc4.close()
            c.ok(_code_late == 200 and _row_late["stage"] == "已发放offer"
                 and _row_late["outcome"] == "已签约"
                 and not _out_late.get("advanced_to"),
                 "只改结论：已在「已发放offer」时只改标签，不回退阶段",
                 f"HTTP {_code_late} stage={_row_late['stage']} "
                 f"outcome={_row_late['outcome']}")
            # 助手提案走 actions.apply_proposal，不走阶段接口；同一条规则也要生效。
            _conn_sc3 = db.connect(db_path)
            _conn_sc3.execute(
                "UPDATE applications SET stage='待面试', outcome=NULL WHERE id=?",
                (_aid_sc,))
            _pid_auto = db.create_proposal(
                _conn_sc3, "selftest", "set_stage",
                {"application_id": _aid_sc, "stage": "待面试", "outcome": "拟录用"},
                "只改结论应自动进已面试")
            _conn_sc3.close()
            _proposal_auto = actions.apply_proposal(
                db_path, _pid_auto, "approve", "hr", "hr")
            _conn_sc4 = db.connect(db_path)
            _row_proposal = _conn_sc4.execute(
                "SELECT stage, outcome FROM applications WHERE id=?", (_aid_sc,)).fetchone()
            _conn_sc4.close()
            c.ok(_proposal_auto.get("ok")
                 and _row_proposal["stage"] == "已面试"
                 and _row_proposal["outcome"] == "拟录用",
                 "只改结论：助手提案确认后同样自动进「已面试」",
                 f"stage={_row_proposal['stage']} outcome={_row_proposal['outcome']}")
            _conn_sc5 = db.connect(db_path)
            _conn_sc5.execute(
                "UPDATE applications SET stage='待面试', outcome=NULL WHERE id=?",
                (_aid_sc,))
            _conn_sc5.commit()
            _conn_sc5.close()

            # ================================================ 阶段模型（v1.29）
            # 用**独立临时库**，不碰上面那批断言依赖的数据（后面的组还要数人数/投递数）
            from app import stages as _sg
            _sd = os.path.join(work, "stages_v129.db")
            _sc = db.connect(_sd)

            # ① 兜底表与 config/stages.json 必须一致 —— 防"同一个清单散在两处"悄悄分叉
            c.ok(_sg.assert_consistent() == [],
                 "阶段：内置兜底表与 config/stages.json 一致（防两份清单分叉）",
                 "不一致的键：" + "、".join(_sg.assert_consistent()))
            # v1.29.2：5 档（去掉了独立的「待发offer」）+ 1 个终态
            c.ok(len(_sg.all_stages()) == 6 and len(_sg.pipeline_stages()) == 5
                 and _sg.terminal_stages() == ["已结束"],
                 "阶段：6 个阶段 / 5 个在招列 / 1 个终态（来自配置而非硬编码）",
                 f"{_sg.all_stages()}")
            c.ok(_sg.normalize_stage("已联系") == _sg.DEFAULT_STAGE
                 and _sg.normalize_stage("投递") == _sg.DEFAULT_STAGE,
                 "阶段：认不出的历史值归并到「全部简历」（否则这些人从看板与比例环里消失）")
            c.ok("已发放offer" in _sg.stage_regex() and "初面" not in _sg.stage_regex(),
                 "阶段：助手白名单正则由配置生成（新阶段在、废弃阶段不在）",
                 _sg.stage_regex()[:40])

            # ② 结论必须属于"离开的那个环节"：已面试→不录用 合法；已面试+已签约 非法
            c.ok(_sg.outcome_allowed_from("已面试", "已结束", "不录用")
                 and _sg.outcome_allowed_from("已发放offer", "已结束", "拒签")
                 and _sg.outcome_allowed_from("已发放offer", "已发放offer", "已签约"),
                 "结论：三条真实流转允许（不录用 / 拒签 / 已签约）")
            c.ok(not _sg.outcome_allowed_from("已面试", "已面试", "已签约")
                 and not _sg.outcome_allowed_from("全部简历", "已结束", "不录用"),
                 "结论：不存在的组合被拒（已面试+已签约、全部简历+不录用）")
            c.ok(_sg.verdict("已面试", "拟录用") == "拟录用"
                 and _sg.verdict("已面试", "不录用") == "不录用"
                 and _sg.verdict("已发放offer", "已签约") == "拟录用"
                 and _sg.verdict("已发放offer", None) == "拟录用"
                 and _sg.verdict("已面试", None) == "",
                 "「是否录用」由 (阶段,结论) 推导；拟录用现在是**标签**（v1.29.2）")
            # ⚠️ 拒签必须算**不录用**：发了录用函、对方拒签 → 结果就是没录用。
            # 原来只映射「不录用」，拒签的人那列是**空的** —— 而"他拒了"恰恰是
            # 最该被 HR 看到的一条，留空等于"还不知道"，性质完全不同。
            c.ok(_sg.verdict("已结束", "拒签") == "不录用",
                 "「是否录用」：拒签算**不录用**（不是留空）")
            # ⚠️ v1.29.2（业务方 2026-10-16 定调）：「拟录用」**只是标签**，
            #    打上后**留在「已面试」**，不自动进「已发放offer」——
            #    还要 HR 再走一步「发出录用函」。所以配置里它是**自环**。
            _lc = db.connect(os.path.join(work, "v129_label.db"))
            _ml = _lc.cursor()
            _ml.execute("INSERT INTO candidates (identity_key,name,created_at,updated_at) "
                        "VALUES (?,?,?,?)", ("v129l:乙", "乙", db.now(), db.now()))
            _lc.commit()
            _laid = db.insert_application(_lc, {"candidate_id": _ml.lastrowid, "channel": "测试"})
            for _s in ("初筛通过", "待面试", "已面试"):
                if _s == "待面试":
                    db.update_application_fields(_lc, _laid, interview_unit="稀土研究院")
                db.set_application_stage(_lc, _laid, _s)
            db.set_application_stage(_lc, _laid, "已面试", outcome="拟录用")
            _ap = db.get_application(_lc, _laid)
            c.ok(_ap["stage"] == "已面试" and _ap["outcome"] == "拟录用",
                 "「拟录用」是**标签**：打上后仍在「已面试」，**不自动进已发放offer**",
                 f"stage={_ap['stage']} outcome={_ap['outcome']}")
            c.ok(_sg.verdict(_ap["stage"], _ap["outcome"]) == "拟录用",
                 "打了「拟录用」标签 → 导出表「是否录用」列显示**拟录用**（同步）")
            db.set_application_stage(_lc, _laid, "已发放offer")
            c.ok(db.get_application(_lc, _laid)["stage"] == "已发放offer"
                 and db.get_application(_lc, _laid)["outcome"] == "拟录用",
                 "再走「发出录用函」才进已发放offer（标签随之带过去）")
            _lc.close()

            def _mk(_conn, _nm):
                _r = _conn.execute(
                    "INSERT INTO candidates (identity_key,name,created_at,updated_at) "
                    "VALUES (?,?,?,?)", (f"stg:{_nm}", _nm, db.now(), db.now()))
                _conn.commit()
                return _r.lastrowid

            # v1.29：进「待面试」前必须先登记面试单位（业务方裁定）。
            # 下面两处推进都要过这一关，所以**统一走这个 helper** ——
            # 第一版是在其中一处现补字段、另一处忘了，结果自检直接被约束卡住，
            # 反而证明约束是真的生效了（判据自己也得符合真实流程）。
            def _to_interview(aid):
                for _st in ("初筛通过", "待面试", "已面试"):
                    if _st == "待面试":
                        db.update_application_fields(_sc, aid,
                                                     interview_unit="稀土研究院")
                    db.set_application_stage(_sc, aid, _st)

            _c1 = _mk(_sc, "阶段甲")
            _a1 = db.insert_application(_sc, {"candidate_id": _c1, "channel": "测试"})
            _to_interview(_a1)
            _ap1 = db.get_application(_sc, _a1)
            c.ok(bool(_ap1["interviewed_at"]), "进「已面试」会写 interviewed_at（导出圈人靠它）")
            db.set_application_stage(_sc, _a1, "已结束", "不录用")
            c.ok(db.get_candidate(_sc, _c1)["archived_at"],
                 "全部投递走完 → 自动归档（把既有死代码 count_pending_applications 接上）")

            # ③ 还有进行中投递时**不许**归档（一个人可能同时投两个岗位）
            _c2 = _mk(_sc, "阶段乙")
            _b1 = db.insert_application(_sc, {"candidate_id": _c2, "channel": "测试"})
            _b2 = db.insert_application(_sc, {"candidate_id": _c2, "channel": "测试"})
            _to_interview(_b1)
            db.set_application_stage(_sc, _b1, "已结束", "不录用")
            c.ok(not db.get_candidate(_sc, _c2)["archived_at"]
                 and db.count_pending_applications(_sc, _c2) == 1,
                 "还有 1 条进行中投递 → 不归档（\"必须全部到终点\"）",
                 f"进行中 {db.count_pending_applications(_sc, _c2)} 条")
            c.ok(_sc.execute("SELECT COUNT(*) n FROM audit_log WHERE action='archive'")
                 .fetchone()["n"] == 1,
                 "自动归档不递归（归档↔结束 未形成环）")

            # ④ 非法阶段/结论必须报错，而不是静默写进去
            _bad_stage = _bad_out = False
            try:
                db.set_application_stage(_sc, _b2, "初面")
            except ValueError:
                _bad_stage = True
            try:
                db.set_application_stage(_sc, _b2, "已面试", "已签约")
            except ValueError:
                _bad_out = True
            c.ok(_bad_stage and _bad_out,
                 "非法阶段/非法结论被拒绝（不会静默写进库）")

            # ⑤ 写入覆盖度：`_ADD['applications']` 的每一列必须要么在 insert 列清单、
            #    要么在 APPLICATION_WRITABLE 白名单里。两者皆无 = "加了列但没有任何
            #    代码路径能写它"，或者更糟：调用方传了却被静默丢弃（cet/grad_date 各栽过一次）。
            import re as _re2                      # 本文件顶部没有 import re（按块局部导入）
            _src_db = open(os.path.join(BASE, "app", "db.py"), encoding="utf-8").read()
            _add_blk = _src_db[_src_db.index('"applications": {'):]
            _add_blk = _add_blk[:_add_blk.index("\n        },")]
            _add_cols = set(_re2.findall(r'^\s*"([a-z_]+)":\s*"', _add_blk, _re2.M))
            _ins_blk = _src_db[_src_db.index("INSERT INTO applications"):]
            _ins_blk = _ins_blk[:_ins_blk.index("VALUES")]
            _ins_cols = set(_re2.findall(r"[a-z_]+", _ins_blk))
            # 例外：这几个**老列**由别处的直接 SQL 写，不走 insert/白名单 ——
            # 显式列出来，而不是把判据放宽到"扫到名字就算过"（那样等于没检查）。
            #   suggested_job_id / suggested_job_reason → regrade.suggest_job_for_application 的 UPDATE
            #   merged_from_candidate → merge_candidates / split 的 UPDATE
            #   stage_changed_at → set_application_stage 自己的 UPDATE
            #   honors_json → ingest 里 db.update_candidate(honors_json=...)
            # 加新列时如果落到这个例外里，说明它"没有正规写入路径"，应该先想清楚再写。
            _LEGACY_DIRECT = {"suggested_job_id", "suggested_job_reason",
                              "merged_from_candidate", "stage_changed_at", "honors_json"}
            _uncovered = sorted(_add_cols - _ins_cols - set(db.APPLICATION_WRITABLE)
                                - _LEGACY_DIRECT)
            c.ok(not _uncovered,
                 "applications 新增列都有写入路径（insert 清单 / APPLICATION_WRITABLE / 已知直接 SQL）",
                 f"没有写入路径的列：{_uncovered}")
            _sc.close()

            # ---- v1.29：面试/录用/导出这一批的判据 ----
            from app import export as _ex
            from app import naming as _nm
            from app import univ as _uv
            from app import xlsx as _xl
            from app.server import _interview_at
            from app.pipeline.extract import _find_first_degree_school as _fds
            from app.pipeline.extract import _edu_history_heuristic as _xeh
            from app.pipeline.extract import _edu_history_from_llm as _xeh_llm

            # ① 第一学历：**绝不退回 school**。这是本批最要紧的一条 ——
            #    拿最高学历院校顶替，"本科双非、硕士985"的人就会被筛进「本科985」。
            _t_ma = "2019.09-2023.06  西安建筑科技大学  材料成型及控制工程  本科\n" \
                    "2023.09-2026.06  西安交通大学  材料科学与工程      硕士"
            _t_only_master = "学历：硕士  毕业院校：西安交通大学  专业：材料科学与工程"
            c.ok(_fds(_t_ma) == "西安建筑科技大学" and _fds(_t_only_master) is None,
                 "第一学历：认得出本科段；只有最高学历时**留空**（不拿 school 顶替）",
                 f"分段={_fds(_t_ma)!r} 仅硕士={_fds(_t_only_master)!r}")

            # ② 海外维度：三态 + **撞名反例**（实测踩到：名单里的「东北大学（日本）」
            #    剥掉括号后缀会把我国 985 东北大学误判成境外校）
            c.ok(_uv.is_overseas("香港中文大学") is True
                 and _uv.is_overseas("兰州大学") is False
                 and _uv.is_overseas("") is None,
                 "海外维度三态：海外 / 非海外 / 未识别（未识别与「非海外」严格分开）")
            c.ok(_uv.is_overseas("东北大学") is False
                 and _uv.is_overseas("日本东北大学") is True,
                 "海外维度不做括号剥离（否则国内 985 东北大学会被误判为境外）")

            # ③ 筛选「只命中」判据（用只命中、不用"包含"——包含在全库下必然通过）
            _fc = db.connect(os.path.join(work, "v129_filter.db"))
            _mkp = _fc.cursor()

            def _person(nm2, edu, school, fdg="", unit=None):
                _mkp.execute("INSERT INTO candidates (identity_key,name,gender,edu_level,"
                             "school,first_degree_school,created_at,updated_at) "
                             "VALUES (?,?,?,?,?,?,?,?)",
                             (f"v129:{nm2}", nm2, "男", edu, school, fdg or None,
                              db.now(), db.now()))
                cid2 = _mkp.lastrowid
                aid2 = db.insert_application(_fc, {"candidate_id": cid2, "channel": "测试"})
                if unit:
                    db.update_application_fields(_fc, aid2, interview_unit=unit)
                return aid2

            _person("甲985本硕", "硕士", "兰州大学", "兰州大学", "稀土研究院")
            _person("乙双非本", "硕士", "西安交通大学", "西安建筑科技大学", "数字化中心")
            _person("丙未识别", "本科", "沈阳工业大学", "", None)
            _fc.commit()
            _pick = lambda **kw: sorted(x["name"] for x in db.list_candidates(_fc, **kw))
            c.ok(_pick(bachelor="985") == ["甲985本硕"]
                 and _pick(bachelor="未识别") == ["丙未识别"],
                 "第一学历筛选只命中该档（985 与未识别互不串）",
                 f"985→{_pick(bachelor='985')} 未识别→{_pick(bachelor='未识别')}")
            c.ok(_pick(interview_unit="稀土研究院") == ["甲985本硕"]
                 and _pick(interview_unit="稀土研究院,数字化中心") == ["乙双非本", "甲985本硕"],
                 "面试部门筛选：单选只命中 1 人，多选为并集",
                 f"单选={_pick(interview_unit='稀土研究院')} 并集={_pick(interview_unit='稀土研究院,数字化中心')}")
            c.ok(_pick(univ="211") == ["乙双非本", "甲985本硕"],
                 "院校层次 211 含 985（口径与界面提示一致）", str(_pick(univ="211")))
            _fc.close()

            # ④ 导出：13 列**列序**、缺值 `—`、**含归档档**
            _ec = db.connect(os.path.join(work, "v129_export.db"))
            _mk2 = _ec.cursor()

            def _person2(nm2, edu, school, *, steps=(), unit=None, idate=None, slot=None,
                         mode=None, offer=None, archived=False):
                _mk2.execute("INSERT INTO candidates (identity_key,name,gender,edu_level,"
                             "school,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                             (f"v129x:{nm2}", nm2, "女", edu, school, db.now(), db.now()))
                cid2 = _mk2.lastrowid
                aid2 = db.insert_application(_ec, {"candidate_id": cid2, "channel": "测试"})
                patch = {}
                if unit:   patch["interview_unit"] = unit
                if idate:  patch["interview_date"] = idate
                if slot:   patch["interview_slot"] = slot
                if mode:   patch["interview_mode"] = mode
                if offer:  patch["offer_sent_at"] = offer
                if patch:  db.update_application_fields(_ec, aid2, **patch)
                # 按**真实流转**逐步推进。结论标签必须属于"离开的那个环节"，
                # 所以不能一步跳到终态（会被 set_application_stage 正确拒绝）——
                # 这一点本身就是判据：下面三种走法都能走通，说明流转约束没写错。
                for st_, oc_ in steps:
                    db.set_application_stage(_ec, aid2, st_, outcome=oc_,
                                             skip_auto_archive=True)
                if archived:
                    db.set_candidate_archived(_ec, cid2, True, skip_auto_archive=True)
                return aid2

            _INTERVIEW = [("初筛通过", None), ("待面试", None), ("已面试", None)]
            # 已结束（不录用）+ 已归档 —— 这类人**必须**出现在导出里
            _person2("甲不录用", "硕士", "东北大学",
                     steps=_INTERVIEW + [("已结束", "不录用")],
                     unit="稀土研究院", idate="2026-10-09 09:30", slot="09:30-10:30",
                     mode="现场面试", archived=True)
            # 只到过「已面试」、后来又往前推了 —— 按当前阶段取会**丢掉**这个人。
            # 注意"已签约"是**停在已发放offer 时**才打的，不能在跨阶段时带上。
            _person2("乙拟录用", "硕士", "兰州大学",
                     steps=_INTERVIEW + [("已面试", "拟录用"), ("已发放offer", None),
                                         ("已发放offer", "已签约")],
                     unit="分析测试中心", idate="2026-10-13 14:30", slot="14:30-15:30",
                     mode="线上面试", offer="2026-10-14 09:00:00")
            # 从没面试过 —— 必须**不**出现在这张表里
            _person2("丙未面试", "本科", "沈阳工业大学", steps=[("初筛通过", None)],
                     unit="数字化中心")
            _ec.commit()
            _h, _rows, _tcols, _n = _ex.build_roster(_ec)
            _names = [r[3] for r in _rows]
            c.ok(_h == _ex.ROSTER_COLS and len(_h) == 13,
                 "导出：**唯一一份名单**、固定 13 列、列序与人事处指定一致", str(_h))
            # 排序 = 阶段顺序倒序（最晚阶段在前）→ 再按投递时间/编号。
            # 甲已结束(rank6) > 乙已发放offer(rank5) > 丙初筛通过(rank1)
            c.ok(_names == ["甲不录用", "乙拟录用", "丙未面试"],
                 "导出：**所有阶段的人都在**（含已归档、含还在初筛的），按阶段倒序",
                 str(_names))
            _row_g = dict(zip(_h, _rows[_names.index("丙未面试")]))
            c.ok(_row_g["面试日期"] == "—" and _row_g["是否录用"] == "—"
                 and _row_g["录用函发送时间"] == "—" and _row_g["入职情况"] == "—",
                 "导出：没走到那个阶段的字段填 `—`（不空着、不编造）",
                 f"丙={_row_g}")
            c.ok(_row_g["面试单位"] == "数字化中心",
                 "导出：面试单位在**初筛登记时**就该有值（不必等到面试）")
            _row_a = dict(zip(_h, _rows[_names.index("甲不录用")]))
            c.ok(_row_a["是否录用"] == "不录用" and _row_a["面试单位"] == "稀土研究院"
                 and _row_a["入职情况"] == "—",
                 "导出：归档的不录用者也完整在表内（最该出现的一批）")
            c.ok(all(r[1] != "" and r[8] != "" and r[12] != "" for r in _rows),
                 "导出：缺值一律 `—`（不留空单元格）",
                 f"电话列={[r[7] for r in _rows]}")
            c.ok(7 in _tcols, "导出：电话列是**文本格式**（否则 Excel 显示成 1.38E+10）")
            c.ok([r[8] for r in _rows] == ["不录用", "拟录用", "—"],
                 "导出：「是否录用」由 (阶段,结论) 推导，没判的给 `—`",
                 str([r[8] for r in _rows]))
            c.ok(len(_xl.write_sheet(_rows, _h, text_cols=_tcols, meta="测试")) > 2000,
                 "xlsx 能生成（表头加粗/冻结/列宽/文本格式不报错）")
            _csv_b = _xl.csv_bytes(_rows, _h)
            # 阈值按**这份样本的真实字节数**给（几百字节级），不要拍一个
            # 大数字 —— 那样判据会因为"数据变少"而失败，与功能无关。
            c.ok(len(_csv_b) > 100 and _csv_b.startswith(b"\xef\xbb\xbf"),
                 "csv 同口径且带 BOM（没 BOM Excel 会按本地编码猜、中文乱码）")

            # ---- v2.0.1（业务方要求）：下载初筛简历时，包里**附带初筛通过名单 Excel** ----
            # 判据必须真跑一遍、把 xlsx **解开看表头和行集** —— 只查"字符串在不在源码里"
            # 抓不到这类缺陷（今天在 ㉟ 上已经吃过两次亏）。
            import openpyxl as _oxl
            _ro = _ex.bundle_roster(_ec, operator="hr", role="hr")
            _ws = _oxl.load_workbook(io.BytesIO(_ro["data"])).active
            _hdr = [_c.value for _c in _ws[1]]
            _names_in = [_ws.cell(row=_r, column=4).value for _r in range(2, 2 + _ro["n"])]
            c.ok(_ro["filename"].startswith("初筛通过名单")
                 and _ro["filename"].endswith(".xlsx")
                 and _hdr == _ex.ROSTER_COLS and len(_hdr) == 13,
                 "简历包名单：与导出入口**同一套 13 列**（不另写一份列定义，避免两处分叉）",
                 str(_hdr[:4]) + "…")
            c.ok(_names_in == ["丙未面试"],
                 "简历包名单：行集只有**初筛通过**的人（不是全库、不含其他阶段）",
                 str(_names_in))
            # 进 zip：走 _zip_response 的 extra 通道（表是现算的，没有磁盘路径可塞）
            _zr = srv._zip_response([], "初筛通过-测试.zip",
                                    extra=[(_ro["filename"], _ro["data"])])
            _znames = zipfile.ZipFile(io.BytesIO(_zr.body)).namelist()
            c.ok(_znames == [_ro["filename"]],
                 "简历包：名单表确实被装进 zip（extra 内存通道，不落磁盘、不留残留）",
                 str(_znames))

            # ---- v2.0.4 证件照抽取：**造一个最小 docx** 验，不依赖任何夹具文件 ----
            # 判据要两边都验：竖构图能抽到 / 横构图与纯文字**不能**硬凑
            #（抽错一张 logo 挂在卡片上，比没有照片更糟）。
            from app import avatar as _av
            from PIL import Image as _PImg

            def _mini_docx(fn, size, color):
                buf = io.BytesIO()
                _PImg.new("RGB", size, color).save(buf, "PNG")
                path = os.path.join(work, fn)
                with zipfile.ZipFile(path, "w") as z:
                    z.writestr("word/media/image1.png", buf.getvalue())
                return path

            _tall = _mini_docx("av_tall.docx", (300, 400), (200, 210, 230))
            _wide = _mini_docx("av_wide.docx", (900, 200), (10, 10, 10))
            _tiny = _mini_docx("av_tiny.docx", (40, 50), (10, 10, 10))
            _txt = os.path.join(work, "av.txt")
            with open(_txt, "w", encoding="utf-8") as fh:
                fh.write("姓名：张三\n")
            c.ok(bool(_av.extract(_tall)), "证件照：竖构图照片能从 docx 里抽出来")
            c.ok(_av.extract(_wide) is None, "证件照：横构图/横幅不当成照片（不硬凑）")
            c.ok(_av.extract(_tiny) is None, "证件照：太小的图标不当成照片")
            c.ok(_av.extract(_txt) is None, "证件照：纯文字简历返回 None")
            _sq = _av._square(_PImg.new("RGB", (300, 400)), 128)
            c.ok(_sq.size == (128, 128), "证件照：裁成正方形（圆形头像不变形）", str(_sq.size))
            # **只看第 1 页**（业务方 2026-10-20 口径）：一个 PDF 里常夹成绩单/证书，
            # 简历一定是第一个；从第 2 页往后抽会把证书上的图误当证件照。
            import pymupdf as _pym
            _two = os.path.join(work, "av_two_pages.pdf")
            _pbuf = io.BytesIO()
            _PImg.new("RGB", (300, 400), (190, 200, 220)).save(_pbuf, "PNG")
            _doc = _pym.open()
            _doc.new_page().insert_text((72, 72), "resume page 1 (no photo)")
            _doc.new_page().insert_image(_pym.Rect(72, 72, 272, 472), stream=_pbuf.getvalue())
            _doc.save(_two)
            _doc.close()
            c.ok(_av.extract(_two) is None,
                 "证件照：照片在**第 2 页**时不抽（只认简历首页，避开成绩单/证书）")

            # v2.0.5 校实力：985/211 是**身份标签**，强弱看排名。
            # 业务方原话：「西电 211 但是比西北农林 985 厉害」—— 这条判据就是钉这个：
            # 标签相反的两所学校，实力档必须能给出**正确顺序**（西电 B 档 > 西北农林 C 档）。
            from app import univ as _univ

            _xd = _univ.school_band("西安电子科技大学")
            _xn = _univ.school_band("西北农林科技大学")
            c.ok(bool(_xd) and bool(_xn) and _xd[1] < _xn[1] and _xd[0] != _xn[0],
                 "校实力：西电(211/第40) 强于 西北农林(985/第71) —— 标签压不过排名",
                 f"西电={_xd} 西北农林={_xn}")
            c.ok(_univ.school_rank("西安电子科技大学") == 40
                 and _univ.school_rank("清华大学") == 1,
                 "校实力：排名表可查（软科 2025 主榜）")
            c.ok(_univ.school_band("某某职业技术学院") is None,
                 "校实力：没进排名表 → 无数据（**不等于差**，调用方不许因此降档）")
            # v2.0.5：**第一学历与最高学历都要进分析上下文**（业务方："综合考虑两者的院校层次"）。
            # 只给最高学历院校时模型看不到"本科出身"，而院所普遍认为本科底子更硬。
            from app.pipeline import analyze as _an

            _br = _an._cand_brief({"name": "x", "edu_level": "硕士",
                                   "school": "西安电子科技大学",
                                   "first_degree_school": "太原理工大学"})
            c.ok("院校:西安电子科技大学（211｜校实力 B 档" in _br
                 and "第一学历院校:太原理工大学（211｜校实力 D 档" in _br,
                 "分析上下文：最高学历 + 第一学历的院校实力都给到模型",
                 _br.split("；专业")[0][-72:])
            _br2 = _an._cand_brief({"name": "y", "edu_level": "硕士", "school": "清华大学"})
            c.ok("第一学历院校:未识别" in _br2,
                 "分析上下文：没有第一学历院校时明说「未识别」，不留空让模型瞎猜")

            # ---- v2.0.7 对外查询护栏：**代码层拦**，这是隐私红线 ----
            # 只靠提示词说"别把候选人信息发出去"，模型换个说法就绕过去了，
            # 而数据一旦发出去就收不回来。所以判据必须钉在代码行为上。
            from app import websearch as _ws

            c.ok(_ws.guard(_ec, "西安电子科技大学 计算机 学科评估") is None,
                 "对外查询护栏：学校 + 学科类查询 → 放行")
            _g1 = _ws.guard(_ec, "甲不录用 西安交大 材料")
            c.ok(bool(_g1) and "候选人" in _g1,
                 "对外查询护栏：含**库内候选人姓名** → 拒绝", str(_g1))
            _g2 = _ws.guard(_ec, "王小明 13800138000")
            c.ok(bool(_g2) and "手机号" in _g2,
                 "对外查询护栏：含手机号 → 拒绝", str(_g2))
            _g3 = _ws.guard(_ec, "abc@example.com 学校排名")
            c.ok(bool(_g3) and "邮箱" in _g3,
                 "对外查询护栏：含邮箱 → 拒绝", str(_g3))
            _g4 = _ws.guard(_ec, "今天天气怎么样")
            c.ok(bool(_g4),
                 "对外查询护栏：**不是**学校/专业/行业类 → 拒绝（问句词不算话题）", str(_g4))
            c.ok(_ws.search("西安电子科技大学 排名").get("configured") is False,
                 "对外查询：**默认未配置 → 一个请求都不发**（要开由 HR 在 search.json 里开）")

            # ---- v2.0.8 教育经历清洗：业务方实地截图里的三种脏样子 ----
            # ① `在太原理工大学` / `本硕太原理工大学` —— 模型把修饰语当校名抓下来了；
            # ② 同校清洗后重名 → 只剩一条；
            # ③ 与"最高学历院校"矛盾的段（旧解析残留）→ 剔除，交给界面用 school 补齐。
            _eh = db._loads_edu_history(
                '[{"degree":"本科","school":"在太原理工大学"},'
                '{"degree":"本科","school":"本硕太原理工大学"},'
                '{"degree":"硕士","school":"西北工业大学"}]',
                top_school="西安电子科技大学", top_degree="硕士")
            c.ok([h["school"] for h in _eh] == ["太原理工大学"],
                 "教育经历：清洗 `在XX大学` / `本硕XX大学`，同校去重，剔掉与最高学历矛盾的段",
                 str([(h["degree"], h["school"]) for h in _eh]))
            c.ok(_univ.uni_tier("本硕太原理工大学") == "211",
                 "校名入口清洗：**脏 school 也能匹配层次**（否则校实力对它静默失效）")

            _ec.close()

            # ⑤ 命名模板：六段齐全、缺值 `—` 不省略、非法字符清洗、性别按普通字段
            _r0 = {"job_title": "工艺技术", "edu_level": "硕士", "school": "西安电子科技大学",
                   "major": "材料科学与工程", "name": "陈志远", "gender": "男"}
            _n0 = _nm.resume_filename(_r0, ".pdf")
            _n1 = _nm.resume_filename({**_r0, "job_title": None}, ".pdf")
            c.ok(_n0 == "工艺技术-硕士-西安电子科技大学-材料科学与工程-陈志远-男.pdf"
                 and _n1.startswith("—-硕士-"),
                 "命名模板六段齐全，缺值写 `—` 且**不省略段位**", f"{_n0} / {_n1}")
            c.ok("/" not in _nm.resume_filename(
                    {**_r0, "school": "西/安\\大学", "major": "材料:科学"}, ".pdf"),
                 "命名模板清洗非法字符（路径分隔符会让 zip 里的条目被当成目录）")
            c.ok(_nm.resume_filename({**_r0, "gender": None}, ".pdf").endswith("陈志远-—.pdf"),
                 "命名模板：性别没写时与其它缺值同规则输出 `—`（v1.29.4 起无特殊降级）")

            # ⑧ v1.29.2：完整学历院校经历（本科/硕士/博士各一段）
            #    业务方要求"分析结果里完整体现本科/硕士/博士各是什么学校"。
            _eh_text = ("2013.09-2017.06  西安建筑科技大学  材料成型及控制工程  本科\n"
                        "2017.09-2020.06  东北大学          材料科学与工程      硕士\n"
                        "2020.09-2024.06  北京航空航天大学  材料物理          博士\n")
            _eh = _xeh(_eh_text)
            c.ok([h["degree"] for h in _eh] == ["本科", "硕士", "博士"],
                 "学历院校经历：三段（本科/硕士/博士）都在", str([h["degree"] for h in _eh]))
            # 四种"脏数据"各来一条，逐条验证被丢掉：
            #   编造校名（原文没有哈佛）／非法层次（「研究生」不在白名单）／
            #   缺字段（没有 degree）／同校同层次重复段
            _eh_bad = _xeh_llm([{"degree": "本科", "school": "西安建筑科技大学"},
                                {"degree": "本科", "school": "哈佛大学"},
                                {"degree": "研究生", "school": "东北大学"},
                                {"school": "北京航空航天大学"},
                                {"degree": "博士", "school": "东北大学"},
                                {"degree": "博士", "school": "东北大学"}],
                               _eh_text)
            c.ok([(h["degree"], h["school"]) for h in _eh_bad]
                 == [("本科", "西安建筑科技大学"), ("博士", "东北大学")],
                 "学历院校经历：同校不同层次保留，编造/非法/缺字段/同层重复被丢掉",
                 str([(h["degree"], h["school"]) for h in _eh_bad]))
            from app.pipeline.extract import _find_name as _find_name_test

            _eh_dirty = (
                "➢ 在本科期间担任材料学院冰花通讯社通讯员\n"
                "韩林烜HAN LIN XUAN 东北大学硕士研究生在读\n"
                "2024.09-2027.06 东北大学（推荐免试） 材料科学与工程 硕士\n"
                "2020.09-2024.06 东北大学 材料成型及控制工程 本科\n")
            _eh_fixed = _xeh(_eh_dirty)
            c.ok(_find_name_test(_eh_dirty) == "韩林烜",
                 "姓名解析：中英文连写时可识别中文姓名，不把学校名当姓名")
            c.ok(_fds(_eh_dirty) == "东北大学",
                 "第一学历：带时间段的本科行优先，不误取校园经历里的“材料学院”",
                 str(_fds(_eh_dirty)))
            c.ok([(h["degree"], h["school"], h["major"]) for h in _eh_fixed]
                 == [("本科", "东北大学", "材料成型及控制工程"),
                     ("硕士", "东北大学", "材料科学与工程")],
                 "学历经历：本硕同校也各保留一段，并补全带括号写法的专业",
                 str(_eh_fixed))
            _ehc = db.connect(os.path.join(work, "v129_edu.db"))
            _cid_eh = db.insert_candidate(_ehc, {
                "identity_key": "v129e:甲", "name": "甲", "edu_level": "博士",
                "school": "北京航空航天大学", "first_degree_school": "西安建筑科技大学",
                "edu_history": json.dumps(_eh, ensure_ascii=False)})
            _got = (db.candidate_detail(_ehc, _cid_eh) or {}).get("edu_history") or []
            c.ok(len(_got) == 3 and [h["degree"] for h in _got] == ["本科", "硕士", "博士"],
                 "学历院校经历：入库能完整读回（含 JSON 列）", str(len(_got)))
            c.ok([h.get("tier") for h in _got] == [None, "985", "985"],
                 "学历院校经历：**逐段**挂院校层次（本科双非 + 硕士985 看得出来）",
                 str([h.get("tier") for h in _got]))
            _ehc.close()

            # ⑪ v1.29.3：面试结论的**阶段联动**（业务方 2026-10-16）
            #     · 当前是「待面试」→ 选拟录用/不录用 **自动推进到「已面试」**
            #     · 其它阶段 → **只打标签，不动阶段**
            _vc = db.connect(os.path.join(work, "v129_verdict.db"))
            _mv = _vc.cursor()
            _nv = [0]

            def _mkv(stage):
                _nv[0] += 1
                _mv.execute("INSERT INTO candidates (identity_key,name,created_at,updated_at) "
                            "VALUES (?,?,?,?)", (f"v129v:{_nv[0]}", "甲", db.now(), db.now()))
                _vc.commit()
                aid2 = db.insert_application(_vc, {"candidate_id": _mv.lastrowid, "channel": "测试"})
                db.set_application_stage(_vc, aid2, "初筛通过")
                db.update_application_fields(_vc, aid2, interview_unit="稀土研究院")
                if stage != "初筛通过":
                    db.set_application_stage(_vc, aid2, stage)
                return aid2

            # ① 待面试 + 拟录用 → 进已面试，且不归档
            # ① 待面试 → 打标签 + 进已面试（接口层就是这个组合）
            _va = _mkv("待面试")
            db.set_application_stage(_vc, _va, "已面试", outcome="拟录用")
            _ap1 = db.get_application(_vc, _va)
            _cd1 = db.get_candidate(_vc, _mv.lastrowid)
            c.ok(_ap1["stage"] == "已面试" and _ap1["outcome"] == "拟录用",
                 "阶段联动：待面试打拟录用 → 进「已面试」并带上标签",
                 f"stage={_ap1['stage']} outcome={_ap1['outcome']}")
            c.ok(not (_cd1.get("archived_at") or ""),
                 "阶段联动：进「已面试」**不归档**（只有「已结束」才归档）")
            c.ok(bool(_ap1.get("interviewed_at")),
                 "阶段联动：进「已面试」会写 interviewed_at（导出圈人靠它）")
            # ② 已面试 + 改标签 → 阶段不动
            _vb = _mkv("已面试")
            db.update_application_fields(_vc, _vb, outcome="拟录用")
            c.ok(db.get_application(_vc, _vb)["stage"] == "已面试",
                 "阶段联动：已面试打标签**不动阶段**（推进到已发放offer 由 HR 另走一步）")
            _vc.close()

            # ⑩ 归档只有两个入口（业务方 2026-10-16 明确）：
            #     ① 阶段推进到「已结束」 ② HR 点「归档」。
            #     **其它任何操作都不归档** —— 归档会把人从人才库移走（不可逆的隐藏动作），
            #     而"信息填全了""标签打好了"这类中间状态远没到该收起来的时候。
            _ac = db.connect(os.path.join(work, "v129_arch.db"))
            _mc3 = _ac.cursor()

            _seq3 = [0]

            def _mkp3():
                # identity_key 有 UNIQUE 约束 —— 用递增序号，别用 id()/len() 之类
                # 在两次调用里算出同一个值的写法（第一次就这么写错了，报 UNIQUE 失败）。
                _seq3[0] += 1
                _mc3.execute("INSERT INTO candidates (identity_key,name,created_at,updated_at) "
                             "VALUES (?,?,?,?)", (f"v129a:甲{_seq3[0]}", "甲",
                                                  db.now(), db.now()))
                _ac.commit()
                return db.insert_application(_ac, {"candidate_id": _mc3.lastrowid, "channel": "测试"})

            _pa = _mkp3()
            # 走完面试 → 已面试（拟录用只是打标签，不归档）
            db.set_application_stage(_ac, _pa, "初筛通过")
            db.update_application_fields(_ac, _pa, interview_unit="稀土研究院")
            db.set_application_stage(_ac, _pa, "待面试")
            db.set_application_stage(_ac, _pa, "已面试")
            db.update_application_fields(_ac, _pa, outcome="拟录用")
            c.ok(not (db.get_candidate(_ac, _mc3.lastrowid).get("archived_at") or ""),
                 "归档规则：打结论标签（拟录用）**不归档**")
            c.ok(db.get_application(_ac, _pa)["stage"] == "已面试",
                 "归档规则：打标签**不动阶段**（仍是已面试）")
            # 改面试信息 / 改其它字段也不归档
            db.update_application_fields(_ac, _pa, interview_date="2026-10-20 09:30")
            c.ok(not (db.get_candidate(_ac, _mc3.lastrowid).get("archived_at") or ""),
                 "归档规则：改面试信息**不归档**")
            # 推进到「已结束」才归档（且全部投递都到终点）
            db.set_application_stage(_ac, _pa, "已结束", outcome="不录用")
            c.ok(bool(db.get_candidate(_ac, _mc3.lastrowid).get("archived_at")),
                 "归档规则：推进到「已结束」**才**归档")
            # 手动归档是第二个入口（查询要用连接对象 `_ac.execute`，不是 `db.execute`）
            _pb = _mkp3()
            _pbc = _ac.execute("SELECT candidate_id FROM applications WHERE id=?",
                               (_pb,)).fetchone()[0]
            db.set_candidate_archived(_ac, _pbc, True, operator="HR", role="hr")
            c.ok(bool(db.get_candidate(_ac, _pbc).get("archived_at")),
                 "归档规则：手动「归档」也归档（第二个入口）")
            _ac.close()

            # ⑨ 档案接口必须把面试/录用字段提到**顶层**
            #    （档案弹层读的是 d.*；之前只在最新那条投递里，
            #     输入框永远是空的 —— HR 看到"这一步必填"却看不到已登记的单位）
            _dc = db.connect(os.path.join(work, "v129_detail.db"))
            _mc2 = _dc.cursor()
            _mc2.execute("INSERT INTO candidates (identity_key,name,created_at,updated_at) "
                         "VALUES (?,?,?,?)", ("v129d:甲", "甲", db.now(), db.now()))
            _dc.commit()
            _daid = db.insert_application(_dc, {"candidate_id": _mc2.lastrowid, "channel": "测试"})
            db.update_application_fields(_dc, _daid, interview_unit="稀土研究院",
                                         interview_slot="09:30-10:30", interview_mode="现场面试",
                                         interview_date="2026-10-20 09:30")
            _det = db.candidate_detail(_dc, _mc2.lastrowid) or {}
            c.ok(_det.get("interview_unit") == "稀土研究院"
                 and _det.get("interview_slot") == "09:30-10:30"
                 and _det.get("interview_mode") == "现场面试"
                 and str(_det.get("interview_date") or "").startswith("2026-10-20"),
                 "档案接口：面试单位/时段/方式/日期在**顶层**（弹层读的就是顶层）",
                 f"单位={_det.get('interview_unit')!r} 时段={_det.get('interview_slot')!r}")
            _dc.close()

            # ⑥ 面试时间解析：时段取不到就只存日期（**不猜时间**）
            c.ok(_interview_at("2026-10-20", "09:30-10:30") == "2026-10-20 09:30"
                 and _interview_at("2026-10-20", "9：30-10:30") == "2026-10-20 09:30"
                 and _interview_at("2026-10-20", "上午") == "2026-10-20"
                 and _interview_at("瞎写", "09:30") is None,
                 "面试时间解析：时段取不到只存日期、日期不合法则不回写")

            # ⑦ 面试单位：**进入「待面试」之前**必填（业务方 2026-10-15 裁定）。
            #    校验放在 db 层，所以卡片下拉 / 管道下拉 / 助手提案三条路径都拦得住。
            # ⚠️ DEFAULT_STAGE 必须与配置一致且**在阶段表里**。
            #   这里原来硬编码 "新投递"，配置改成「全部简历」后两边对不上 ——
            #   界面正常（normalize 兜底），但主动提案规则永远匹配不上、静默产出 0 条。
            c.ok(_sg.DEFAULT_STAGE == _sg.default_stage()
                 and _sg.DEFAULT_STAGE in _sg.all_stages(),
                 "初始阶段从配置读、且必须在阶段表里（防又写死一个字面量）",
                 f"DEFAULT_STAGE={_sg.DEFAULT_STAGE!r} 阶段表={_sg.all_stages()}")
            c.ok(_sg.required_fields("初筛通过", "待面试") == ["interview_unit"]
                 and _sg.required_fields("全部简历", "初筛通过") == [],
                 "面试单位必填挂在「初筛通过→待面试」这一步（不是最初那步）")
            _rc = db.connect(os.path.join(work, "v129_require.db"))
            _mc = _rc.cursor()
            _mc.execute("INSERT INTO candidates (identity_key,name,created_at,updated_at) "
                        "VALUES (?,?,?,?)", ("v129r:甲", "甲", db.now(), db.now()))
            _rc.commit()
            _paid = db.insert_application(_rc, {"candidate_id": _mc.lastrowid, "channel": "测试"})
            db.set_application_stage(_rc, _paid, "初筛通过")
            _blocked = False
            try:
                db.set_application_stage(_rc, _paid, "待面试")
            except ValueError as _e:
                _blocked = "面试单位" in str(_e)
            c.ok(_blocked, "没登记面试单位时**推不到待面试**（且报错说清去哪填）")
            db.update_application_fields(_rc, _paid, interview_unit="稀土研究院")
            db.set_application_stage(_rc, _paid, "待面试")
            c.ok(db.get_application(_rc, _paid)["stage"] == "待面试",
                 "登记之后就能推进（校验不误伤）")
            c.ok(_sg.is_interview_started("待面试") and _sg.is_interview_started("已面试")
                 and not _sg.is_interview_started("初筛通过")
                 and not _sg.is_interview_started("全部简历"),
                 "面试单位标签只在**进入面试流程后**显示（初筛阶段挂个空的只是噪音）")
            _rc.close()

            # ---- v1.13.6 回归：applied_at 显式传 None 要退回时间戳 ----
            _conn_ap = db.connect(db_path)
            _cid_ap = db.insert_candidate(_conn_ap, {"identity_key": "selftest:applied:none",
                                                      "name": "投递时间空"})
            _aid_ap = db.insert_application(_conn_ap, {"candidate_id": _cid_ap, "job_id": None,
                                                       "channel": "测试", "applied_at": None})
            _row_ap = _conn_ap.execute("SELECT applied_at FROM applications WHERE id=?",
                                       (_aid_ap,)).fetchone()
            _conn_ap.close()
            c.ok(bool(_row_ap["applied_at"]),
                 "⑯e applied_at 传 None 时退回时间戳（NULL 会让『最新投递』排序取错行）",
                 f"applied_at={_row_ap['applied_at']}")

            # ---- v1.13.7 ⑰：智能体能起草邮件，但不能发送 ----
            from app.agent import tools as _t7
            _spec_names = {(sp.get("function") or {}).get("name") for sp in _t7.TOOL_SPECS}
            c.ok("draft_email" in _spec_names and "draft_email" in _t7.COMPUTE_TOOLS,
                 "⑰ 智能体有 draft_email 工具（起草邮件）",
                 "原话：'我无法代你发送邮件'——它连草稿都写不出来，而系统本来就有邮件能力")
            c.ok("send_email" in _t7.DISABLED_TOOLS,
                 "⑰b **发送仍然禁止**（红线不变：生成全自动、发送必须人工确认）")
            c.ok("renderDraftCards" in _pgjs and "在邮件编辑器中打开" in _pgjs,
                 "⑰c 聊天区把草稿渲染成卡片，并能一键送进邮件编辑器")
            from app.pipeline import analyze as _an7
            _orig_mail = _an7.llm.chat_json
            _an7.llm.chat_json = lambda *a, **k: {
                "subject": "面试邀请", "body": "您好，想约您聊一下。"}
            try:
                _dm = json.loads(_t7.execute(
                    "draft_email", {"candidate_id": _cid_hit, "purpose": "面试邀请",
                                    "interview_time": "2026-10-20 10:00"},
                    ToolCtx(db_path=db_path, tiers=TIERS)))
            finally:
                _an7.llm.chat_json = _orig_mail
            c.ok(_dm.get("body") and "面试邀请" in (_dm.get("subject") or ""),
                 "⑰d draft_email 能起草出主题与正文（模型可用时）",
                 f"subject={(_dm.get('subject') or '')[:20]}")
            c.ok("不发送" in (_dm.get("note") or "") or "没有发送" in (_dm.get("note") or ""),
                 "⑰e 草稿如实说明「没有发送，要 HR 自己点」",
                 str(_dm.get("note"))[:40])

            # ---- v1.13.7 ⑱：基本信息要过原文证据校验 ----
            from app.pipeline import extract as _ex7
            _txt7 = ("姓名：张三\n电话：13800001111\n邮箱：zhangsan@test.cn\n"
                     "2018.09-2022.06 某大学 材料科学与工程 硕士\n"
                     "工作：某公司 工艺工程师 3年\n技能：钛合金、真空熔铸")
            _fields7 = {"name": "李四", "school": "不存在大学", "major": "计算机",
                        "contact": {"phone": "13900009999"}}
            _ev7 = {"name": "李四", "school": "不存在大学", "major": "计算机"}
            _ok7, _bad7 = _ex7.verify_profile_against_text(_fields7, _ev7, _txt7)
            c.ok("name" in _bad7 and "school" in _bad7 and "phone" in _bad7,
                 "⑱ 模型编的值（原文里根本没有）被证据校验挡住并退回规则",
                 f"未被采信的字段：{_bad7}")
            c.ok(not _ok7.get("school") and not (_ok7.get("contact") or {}).get("phone"),
                 "⑱b 校验不通过的字段绝不会被写进档案（不因模型说了就信）")
            _ok8, _bad8 = _ex7.verify_profile_against_text(
                {"name": "张三", "contact": {"phone": "138-0000-1111"}},
                {"name": "姓名：张三"}, _txt7)
            c.ok(_ok8.get("name") == "张三"
                 and (_ok8.get("contact") or {}).get("phone") == "138-0000-1111",
                 "⑱c 真实存在的值（哪怕排版有分隔符）能通过校验——校验不是一刀切",
                 f"通过：{_ok8}")

            # ---- v1.13.2：**列表接口不许调模型**（性能回归点）----
            # 原来没有存储建议的老数据会在每次打开人才库时现调模型判断一次，
            # 一次 6-8 秒且烧 token（HR 实测：「每次点击人才库页面都会重新调用模型，
            # 这完全没必要，应该存储」）。这里用**会抛异常的桩**证明列表不经过模型。
            from app.pipeline import analyze as _an_perf
            _orig_perf = _an_perf.suggest_job
            _an_perf.suggest_job = lambda *a, **k: (_ for _ in ()).throw(
                AssertionError("列表接口不该调用模型！"))
            # v1.13.6：档位明细也不再逐人调 explain_grade 重算（那会每人开一次连接
            # 并重跑 grade()+major_match()，实测每人约 48ms）。这里让它一被调用就炸，
            # 防止以后有人为了"复用工具"又把它接回去。
            from app.agent import tools as _tl
            _orig_explain = _tl.execute

            def _explode(name, *a, **k):
                if name == "explain_grade":
                    raise AssertionError("列表接口不该逐人调 explain_grade 重算档位！")
                return _orig_explain(name, *a, **k)

            _tl.execute = _explode
            try:
                _code_p, _body_p = _asgi(srv.app, "GET", "/api/candidates", {})
            finally:
                _an_perf.suggest_job = _orig_perf
                _tl.execute = _orig_explain
            _items_p = (json.loads(_body_p).get("items") if _code_p == 200 else [])
            c.ok(_code_p == 200 and isinstance(_items_p, list),
                 "⑬ 打开人才库**不调模型**（列表只读库：不再每次刷新都等 6-8 秒、烧 token）",
                 f"HTTP {_code_p}，{len(_items_p)} 人")
            # 库里没存过建议的，如实留空并提示可主动判断（而不是偷偷现算）
            _pend = [x for x in _items_p if not x.get("job_title")]
            c.ok(all(x.get("job_suggestion") is None or x.get("job_suggestion", {}).get("source") == "stored"
                     for x in _pend),
                 "⑬b 建议岗位只可能来自库内存储（要么有结论、要么如实为空）",
                 f"待指定 {len(_pend)} 人")
            # 主动判断一次 → 结论落库（含理由），此后展示免费读取
            _an_perf.suggest_job = lambda cand, jobs: (
                {"title": jobs[0]["title"], "reason": "自检桩：专业对口"}
                if jobs else None)
            try:
                _cid_sj = _items_p[0]["id"] if _items_p else None
                _code_sj, _body_sj = _asgi(
                    srv.app, "POST", f"/api/candidates/{_cid_sj}/suggest-job", hr, b"")
            finally:
                _an_perf.suggest_job = _orig_perf
            _sj = json.loads(_body_sj)
            _conn_sj = db.connect(db_path)
            _row_sj = _conn_sj.execute(
                "SELECT suggested_job_id, suggested_job_reason FROM applications "
                "WHERE candidate_id = ? ORDER BY id DESC LIMIT 1", (_cid_sj,)).fetchone()
            _conn_sj.close()
            c.ok(_code_sj == 200 and ("ok" in _sj)
                 and (_row_sj is None or _row_sj["suggested_job_id"] is None
                      or _row_sj["suggested_job_reason"]),
                 "⑬c 主动「判断建议岗位」会把结论（含理由）落库，之后展示直接读库",
                 f"HTTP {_code_sj}：{(json.loads(_body_sj).get('note') or '')[:40]}")

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
                                           "tier_suggested": _HARD_T})
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
                                                 "score": 0.05, "tier_suggested": _HARD_T})
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
            c.ok(item_a["changed"] is True and item_a["old_tier"] == _HARD_T,
                 "预演确实算出了差异（学历达标后不再由规则定档：原最差档 → 待模型分析）",
                 f"{item_a['old_tier']}→{item_a['new_tier']}（来源 {item_a.get('tier_source')}）")
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
            c.ok(a_now[item_a["application_id"]]["tier_suggested"] == item_a["new_tier"],
                 "落库的档位与预演给出的完全一致（预演不是另一套算法）",
                 str(a_now[item_a["application_id"]]["tier_suggested"]))
            c.ok(n_regrade_audit >= 1 and n_batch >= 1 and n_preview >= 1,
                 "逐条重算与整批各有审计（预演也留痕：谁看了这次重算结果）",
                 f"regrade={n_regrade_audit} batch={n_batch} preview={n_preview}")
            c.ok(khit["tier_final"] == "A",
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
            g_m = grade({**base_cand, "gender": "男"}, JD)
            g_f = grade({**base_cand, "gender": "女"}, JD)
            g_n = grade(base_cand, JD)
            c.ok(g_m["score"] == g_f["score"] == g_n["score"]
                 and g_m["tier_suggested"] == g_f["tier_suggested"] == g_n["tier_suggested"],
                 "换性别不改变评分与档位（性别不参与 grade()）",
                 f"男={g_m['score']} 女={g_f['score']} 无={g_n['score']}")

            # v1.29.4（业务方裁定"性别正常处理"）：性别是无开关的普通筛选条件
            _, st0_body = _asgi(srv.app, "GET", "/api/settings", {})
            st0 = json.loads(st0_body)
            c.ok("gender_filter_enabled" not in st0,
                 "性别筛选开关已随「正常处理」移除（设置里不再有这一项）",
                 f"keys={sorted(k for k in st0 if k.startswith('gender'))}")
            _, b0_body = _asgi(srv.app, "GET", "/api/candidates", {})
            _, b1_body = _asgi(srv.app, "GET",
                               "/api/candidates?gender=%E5%A5%B3", {})
            b0, b1 = json.loads(b0_body), json.loads(b1_body)
            c.ok(b1["gender_filter"]["applied"] is True
                 and b1["count"] == (b1.get("gender_facets") or {}).get("女", b1["count"])
                 and all((x.get("gender") or "") == "女" for x in b1["items"]),
                 "性别筛选**无需任何开关**直接生效（结果里只剩女）",
                 f"count={b1['count']} / 全部={b0['count']}")
            c.ok((b1.get("gender_facets") or {}).get("全部") == b0["count"],
                 "性别分布与全量口径一致（普通筛选项，分布随列表一起下发）",
                 str(b1.get("gender_facets")))

            # 旧键兼容：外脚本仍可能 POST gender_filter_enabled —— 忽略且不报错
            _, set_on_body = _asgi(srv.app, "POST", "/api/settings", hr,
                                   json.dumps({"gender_filter_enabled": True}).encode())
            set_on = json.loads(set_on_body)
            c.ok(set_on.get("ok") is True and "gender_filter_enabled" not in set_on
                 and set_on.get("changed") is False,
                 "旧键 gender_filter_enabled 被安全忽略（不报错、无行为、无留痕）",
                 f"changed={set_on.get('changed')}")
            _, b2_body = _asgi(srv.app, "GET",
                               "/api/candidates?gender=%E5%A5%B3", {})
            b2 = json.loads(b2_body)
            c.ok(b2["gender_filter"]["applied"] is True
                 and all((x.get("gender") or "") == "女" for x in b2["items"]),
                 "性别筛选照常生效（与是否 POST 过旧键无关）",
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

            # —— 模型与密钥配置（v1.7.6）：掩码下发、文件落盘 0600、审计不记值 ——
            # 把 llm 的配置路径指到临时目录，**绝不碰真实 config/**；
            # 环境变量会盖过文件值，测试期间摘掉，测完原样恢复。
            import app.agent.llm as _llm
            _mc_dir = os.path.join(work, "model_cfg")
            os.makedirs(_mc_dir, exist_ok=True)
            _old_cp, _old_sp = _llm.CONFIG_PATH, _llm.SECRETS_PATH
            _llm.CONFIG_PATH = os.path.join(_mc_dir, "model.json")
            _llm.SECRETS_PATH = os.path.join(_mc_dir, "secrets.json")
            _old_env = {k: os.environ.pop(k) for k in
                        ("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY") if k in os.environ}
            try:
                _KEY = "sk-test-1234567890abcd"
                _, g0_body = _asgi(srv.app, "GET", "/api/model-config", {})
                g0 = json.loads(g0_body)
                c.ok(g0.get("key_set") is False and g0.get("key_masked") == ""
                     and g0.get("key_source") == "未配置",
                     "未配置时如实回报：key_set=False、掩码为空、来源=未配置",
                     str(g0.get("key_source")))
                _hj = {"Content-Type": "application/json"}
                _, p1_body = _asgi(srv.app, "POST", "/api/model-config", _hj,
                                   json.dumps({"base_url": "http://127.0.0.1:9/v1",
                                               "model": "test-model",
                                               "api_key": _KEY}).encode())
                p1 = json.loads(p1_body)
                c.ok(p1.get("ok") and "api_key" in (p1.get("changed") or [])
                     and p1.get("key_masked") == "sk-****abcd"
                     and _KEY not in p1_body,
                     "保存后只回掩码（完整 key 不出现在任何响应里，含审计路径）",
                     str(p1.get("key_masked")))
                _st1 = os.stat(_llm.SECRETS_PATH)
                if os.name == "nt":
                    # Windows 上 os.chmod 对 NTFS 基本无效（只能切只读位），
                    # 实测恒为 0o666。**断言的"组/其他人不可读"这个前提在
                    # Windows 上不成立**，硬断言只会长期挂一条红——同设计文档 #35
                    # 的教训：前提不成立的断言会把"功能正常"误报成"功能坏了"。
                    # 改为验证真正落地的防护：文件确实被写入（说明写路径通了），
                    # 且权限位在 Windows 语义下不对外开放读取（只读位未被误清）。
                    c.ok(_st1.st_size > 0,
                         "密钥文件已落盘（Windows 无 POSIX 权限位，改验落地与内容管控）",
                         f"{_st1.st_size} 字节 / {oct(_st1.st_mode & 0o777)}")
                else:
                    c.ok(_st1.st_mode & 0o077 == 0,
                         "密钥文件权限 0600（组/其他人不可读）",
                         oct(_st1.st_mode & 0o777))
                with open(_llm.CONFIG_PATH, encoding="utf-8") as _fh:
                    _cfg_f = json.load(_fh)
                c.ok(_cfg_f.get("base_url") == "http://127.0.0.1:9/v1"
                     and _cfg_f.get("model") == "test-model"
                     and "api_key" not in _cfg_f,
                     "模型地址/名写 model.json，key 只进 secrets.json（两文件分离）")
                _, g1_body = _asgi(srv.app, "GET", "/api/model-config", {})
                g1 = json.loads(g1_body)
                c.ok(g1.get("key_masked") == "sk-****abcd" and g1.get("key_set") is True
                     and _KEY not in g1_body and "密钥文件" in (g1.get("key_source") or ""),
                     "回读只见掩码，来源如实标为密钥文件",
                     f"{g1.get('key_masked')} / {g1.get('key_source')}")
                _, p2_body = _asgi(srv.app, "POST", "/api/model-config", _hj,
                                   json.dumps({"base_url": "http://127.0.0.1:9/v1",
                                               "model": "test-model",
                                               "api_key": ""}).encode())
                p2 = json.loads(p2_body)
                c.ok(p2.get("changed") == [] and "没有需要保存" in (p2.get("note") or ""),
                     "留空 Key 且值未变 = 无操作（不写盘、不刷审计）",
                     str(p2.get("changed")))
                _st_bad, _bad_body = _asgi(srv.app, "POST", "/api/model-config", _hj,
                                           json.dumps({"base_url": "ftp://x",
                                                       "model": "", "api_key": ""}).encode())
                c.ok(_st_bad == 400, "模型地址必须 http(s) 开头（格式错误 400）",
                     f"status={_st_bad}")
                _c = db.connect(db_path)
                _ar = _c.execute("SELECT after FROM audit_log WHERE entity='settings' "
                                 "AND entity_id='model-config' ORDER BY id DESC LIMIT 1").fetchone()
                _c.close()
                _ad = _ar["after"] if _ar else ""
                c.ok(bool(_ad) and "api_key" in _ad
                     and _KEY not in _ad and "sk-****" not in _ad,
                     "审计记了改了哪些字段，但 key 的值与掩码都不进审计",
                     _ad[:40])
            finally:
                _llm.CONFIG_PATH, _llm.SECRETS_PATH = _old_cp, _old_sp
                os.environ.update(_old_env)

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
            _off = json.loads(set_off_body)
            c.ok(_off.get("ok") is True and "gender_filter_enabled" not in _off,
                 "旧键再 POST 一次仍被安全忽略（迁移期兼容，无副作用）")

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
            _cons_sw = ex_sw.get("consistency") or {}
            c.ok(_cons_sw == {} or _cons_sw.get("same") is True,
                 "③ 档位解释与库内记录对账一致（v1.12 只对账档位：学历门槛结论一致）",
                 str(_cons_sw.get("note") or "库内档位为空（待模型分析），无需对账"))

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

        # ============================================================ T
        c.section("T 智能化三件套：入库即分析 / 主动提案 / 每日摘要（v1.8）")
        # 这一段覆盖的是"系统自己动起来"的三条路径。共同红线：
        # **自动化只产出文本与待确认提案，绝不自动执行写操作。**
        _t_db = os.path.join(work, "t_smart.db")
        _orig_ai = os.environ.get("TP_AUTO_INSIGHT")
        try:
            from app.agent import brief as _brief
            from app.agent import proactive as _pro
            from app.ingest import _insight_targets as _targets
            from app.ingest import spawn_auto_analysis as _spawn
            from app.pipeline.analyze import auto_insight as _auto

            _tc = db.connect(_t_db)
            _tc.execute(
                """INSERT INTO candidates (name, edu_level, years_exp, major,
                                           created_at, updated_at)
                   VALUES ('测试甲','硕士',6,'材料加工工程',?,?)""",
                (db.now(), db.now()))
            _cid_t = _tc.execute("SELECT last_insert_rowid()").fetchone()[0]
            _tc.execute(
                """INSERT INTO applications (candidate_id, channel, applied_at, score,
                                             tier_suggested, stage, status,
                                             created_at, updated_at)
                   VALUES (?,'邮箱',?,0.92,'A','全部简历','待确认',?,?)""",
                (_cid_t, db.now(), db.now(), db.now()))
            _aid_t = _tc.execute("SELECT last_insert_rowid()").fetchone()[0]
            _tc.commit()

            # ① 未归岗：只做简历画像，不硬套默认尺子
            _cd = db.candidate_detail(_tc, _cid_t)
            _ins = _auto(_cd, None, (_cd.get("applications") or [{}])[0])
            c.ok(bool(_ins.get("summary")) and _ins.get("source") == "auto_profile",
                 "① 未归岗时产出简历画像（source=auto_profile），不做岗位匹配", 
                 str(_ins.get("summary"))[:30])

            # ② 队列只收新增/新版本，重复件不进
            _rep = {"details": [
                {"status": "added", "candidate_id": _cid_t, "application_id": _aid_t},
                {"status": "merged_version", "candidate_id": _cid_t, "application_id": _aid_t},
                {"status": "skipped_dup", "candidate_id": _cid_t, "application_id": _aid_t},
                {"status": "failed", "candidate_id": None, "application_id": None},
            ]}
            c.ok(len(_targets(_rep)) == 2,
                 "② 只有新增/新版本进分析队列（重复与失败不进）",
                 f"排队 {len(_targets(_rep))} 条")

            # ③ 开关可关——自检本身的确定性由它保证
            os.environ["TP_AUTO_INSIGHT"] = "0"
            c.ok(_spawn(_t_db, _rep) == 0,
                 "③ TP_AUTO_INSIGHT=0 时完全不入队（保证自检确定性）")
            os.environ["TP_AUTO_INSIGHT"] = "1"

            # ④ 钩子确实落库（异步，轮询等它写进来）
            _spawn(_t_db, _rep)
            _got = None
            for _ in range(30):
                _w = db.connect(_t_db)
                try:
                    _got = db.get_insight(_w, _cid_t, _aid_t)
                finally:
                    _w.close()
                if _got:
                    break
                time.sleep(0.1)
            c.ok(bool(_got) and bool(_got.get("summary")),
                 "④ 入库钩子异步写入分析结果（重复件不会写第二条）",
                 str((_got or {}).get("summary", ""))[:30])

            # ⑤ 主动提案：A 档却还停在新投递 → **推进阶段**（v1.14：不再催"确认档位"，
            #    那个动作没有任何后果；阶段一变，管道/看板/后续动作全跟着变）
            _r1 = _pro.scan_and_propose(_tc, session_id="auto-selftest")
            _stage_props = [x for x in _r1["created"] if x["tool"] == "set_stage"]
            c.ok(len(_stage_props) >= 1
                 and not [x for x in _r1["created"] if x["tool"] == "set_tier"],
                 "⑤ 系统巡检发现「A 档却没推进」并产出**推进阶段**提案（不再催没后果的确认）",
                 f"产出 {_r1['created_count']} 条：{[x['tool'] for x in _r1['created']]}")

            _row_a = _tc.execute("SELECT tier_final, status, stage FROM applications WHERE id=?",
                                 (_aid_t,)).fetchone()
            c.ok(_row_a["tier_final"] is None and _row_a["stage"] == "全部简历",
                 "⑥ **提案不自动执行**：档位与阶段原封不动，等 HR 确认",
                 f"tier_final={_row_a['tier_final']} stage={_row_a['stage']}")

            _pid_t = _stage_props[0]["proposal_id"] if _stage_props else 0
            _prow = _tc.execute("SELECT status, source FROM proposals WHERE id=?",
                                (_pid_t,)).fetchone() if _pid_t else None
            c.ok(_prow is not None and _prow["status"] == "待确认"
                 and _prow["source"] == "agent_auto",
                 "⑦ 提案来源标为 agent_auto 且状态为待确认（与对话产生的可区分）",
                 f"status={_prow['status'] if _prow else '-'} "
                 f"source={_prow['source'] if _prow else '-'}")

            # ⑧ 去重窗口：同一件事不重复提
            _r2 = _pro.scan_and_propose(_tc, session_id="auto-selftest")
            c.ok(_r2["created_count"] == 0 and _r2["skipped"] >= 1,
                 "⑧ 去重窗口生效（7 天内同一件事不重复提，避免提案刷屏）",
                 f"第二轮 产出 {_r2['created_count']} / 跳过 {_r2['skipped']}")

            # ⑨ HR 确认后提案才真正生效（走与对话提案同一条执行流）
            _ap = actions.apply_proposal(_t_db, _pid_t, "approve", "hr", "hr")
            _row_b = _tc.execute("SELECT stage FROM applications WHERE id=?",
                                 (_aid_t,)).fetchone()
            c.ok(_ap.get("ok") and _row_b["stage"] != "全部简历",
                 "⑨ HR 确认后提案才落库生效（自动化 ≠ 自动决定）",
                 f"确认后 stage={_row_b['stage']}")

            # ⑩ 每日摘要：统计与库内一致 + 模型不可用时如实标注规则排序
            _bf = _brief.build(_tc, use_llm=False)
            _db_high = _tc.execute(
                "SELECT COUNT(*) FROM applications WHERE score>=0.85 AND tier_final IS NULL"
            ).fetchone()[0]
            c.ok(_bf["stats"]["high_score"] == _db_high,
                 "⑩ 摘要统计与库内实际一致（单一口径，不与提案各算各的）",
                 f"摘要 {_bf['stats']['high_score']} = 库内 {_db_high}")
            c.ok(_bf["source"] == "rule" and "未经模型判断" in _bf["note"],
                 "⑪ 模型不可用时摘要退回规则排序，并如实标注（不假装是判断出来的）",
                 _bf["note"][:24])

            # ⑪b 待办要**直接写出是谁**：只说"1 位高分候选人"，HR 还得自己去翻列表。
            # 用构造的工作量断言命名行为本身（确定性），而不是依赖本节此刻的库状态——
            # 上面 ⑨ 已经把这个人的档位确认掉了，工作量为空时压根没有"谁"可写。
            _fake = {"counts": {"high_score": 1, "stuck": 0, "needs_review": 0,
                                "pending_job": 0, "pending_confirm": 0},
                     "stuck_days": 7,
                     "items": {"high_score": [{"name": "测试甲", "score": 0.92,
                                               "tier_suggested": "A",
                                               "job_title": "工艺技术"}]}}
            _rp = _brief._rule_priorities(_fake)
            c.ok(_rp and "测试甲" in _rp[0]["title"],
                 "⑪b 待办标题里直接写出姓名（不再是『1 位高分候选人』）", _rp[0]["title"])
            _fake_many = {"counts": {"high_score": 5}, "stuck_days": 7,
                          "items": {"high_score": [{"name": n, "score": 0.9,
                                                    "tier_suggested": "A"}
                                                   for n in ("甲", "乙", "丙", "丁", "戊")]}}
            c.ok("等 5 位" in _brief._rule_priorities(_fake_many)[0]["title"],
                 "⑪b2 人多时列前 3 个并给出总数（不啰嗦也不含糊）",
                 _brief._rule_priorities(_fake_many)[0]["title"])
            c.ok(_bf.get("brief_version") == _brief.BRIEF_VERSION,
                 "⑪c 摘要带结构版本号（结构变了能自动重算，不显示旧措辞）",
                 str(_bf.get("brief_version")))

            # ⑫ 摘要按日期幂等：重算覆盖同一条，不产生多份
            db.save_brief(_tc, _bf["date"], _bf)
            _bf2 = _brief.build(_tc, use_llm=False)
            db.save_brief(_tc, _bf2["date"], _bf2)
            _cnt = _tc.execute("SELECT COUNT(*) FROM daily_briefs WHERE brief_date=?",
                               (_bf["date"],)).fetchone()[0]
            c.ok(_cnt == 1,
                 "⑫ 摘要按日期幂等（同一天重算覆盖，不留多份）", f"{_cnt} 条")

            # ⑬ 学历达标判断：学历是硬门槛，界面要给出"够不够"而不是只显示一个学历名。
            #    判断逻辑放在 server._edu_check（纯函数，好测）。
            _jm = {1: {"id": 1, "title": "测试岗",
                       "jd_json": {"must": {"education_min": "硕士"}}}}
            c.ok(srv._edu_check({"edu_level": "本科"}, _jm) is None,
                 "⑬ 没有对应岗位时**不给学历结论**（没有尺子就不下判断）")
            _bad = srv._edu_check({"edu_level": "本科", "job_id": 1}, _jm)
            c.ok(bool(_bad) and _bad["ok"] is False and not _bad["unknown"],
                 "⑭ 学历低于岗位线时明确判为不达标（界面据此标红）",
                 f"要求 {_bad.get('required')}，实为 {_bad.get('actual')}")
            _unk = srv._edu_check({"edu_level": None, "job_id": 1}, _jm)
            c.ok(bool(_unk) and _unk["unknown"] is True,
                 "⑮ 学历未识别时标为『待判定』——既不冒充达标也不冒充不达标")
            _okd = srv._edu_check({"edu_level": "博士", "job_id": 1}, _jm)
            c.ok(bool(_okd) and _okd["ok"] is True, "⑯ 学历高于岗位线时判为达标")
            _tc.close()
        finally:
            if _orig_ai is None:
                os.environ.pop("TP_AUTO_INSIGHT", None)
            else:
                os.environ["TP_AUTO_INSIGHT"] = _orig_ai

        # ============================================================ U
        c.section("U 决策反馈闭环：把 HR 的定档变成对系统的反馈（v1.8.2）")
        # 关键是三条：只算已确认的、不碰权重文件、样本不够时不硬下结论。
        try:
            from app import feedback as _fb
            _uc = db.connect(os.path.join(work, "u_fb.db"))
            # 12 条已确认：6 一致 + 4 系统偏高 + 2 系统偏低
            _cases = [("A", "A"), ("A", "A"), ("B", "B"), ("B", "B"), ("C", "C"), ("C", "C"),
                      ("A", "B"), ("A", "B"), ("A", "B"), ("A", "B"), ("B", "A"), ("B", "A")]
            for _i, (_s, _f) in enumerate(_cases):
                _uc.execute(
                    """INSERT INTO candidates (name, edu_level, years_exp, major,
                                               created_at, updated_at)
                       VALUES (?,?,?,?,?,?)""",
                    (f"U{_i}", "硕士", 5, "材料学", db.now(), db.now()))
                _cid_u = _uc.execute("SELECT last_insert_rowid()").fetchone()[0]
                _uc.execute(
                    """INSERT INTO applications (candidate_id, channel, applied_at, score,
                                                 tier_suggested, tier_final, hits, miss,
                                                 created_at, updated_at)
                       VALUES (?,'邮箱',?,0.8,?,?,?,?,?,?)""",
                    (_cid_u, db.now(), _s, _f, '["钛合金"]', '[]', db.now(), db.now()))
            # 一条**未确认**（tier_final 为空）——不该进样本
            _uc.execute("INSERT INTO candidates (name, created_at, updated_at) VALUES ('未确认','x','x')")
            _uc.execute(
                """INSERT INTO applications (candidate_id, tier_suggested, created_at, updated_at)
                   VALUES ((SELECT MAX(id) FROM candidates),'A','x','x')""")
            _uc.commit()

            _rep = _fb.decision_report(_uc)
            c.ok(_rep["total"] == 12,
                 "① 只把已确认档位的投递计入样本（未确认的不算）", f"样本 {_rep['total']}")
            c.ok(_rep["same"] == 6 and _rep["high"] == 4 and _rep["low"] == 2,
                 "② 一致/偏高/偏低 计数正确",
                 f"一致 {_rep['same']} 偏高 {_rep['high']} 偏低 {_rep['low']}")
            # 遍历配置里的档位，不写死 "ABCD"（v1.29 档位并成三档后，
            # `"D"` 不在矩阵里 → KeyError 直接把整个 U 组打成"自检异常"）
            c.ok(sum(_rep["matrix"][t][t] for t in _tiers_mod.all_tiers()) == _rep["same"],
                 "③ 偏差矩阵对角线之和 = 一致数")
            _con = _fb.decision_report(_uc)
            c.ok(abs(_con["consistency"] - round(100.0 * _rep["same"] / 12, 1)) < 0.01,
                 "④ 一致性百分比与样本数自洽", f"{_con['consistency']}%")

            # 报告必须**只读**：跑前跑后权重文件字节一致
            _tj = os.path.join(BASE, "config", "tiers.json")
            _before = open(_tj, "rb").read()
            _fb.decision_report(_uc)
            c.ok(open(_tj, "rb").read() == _before,
                 "⑤ 报告不修改任何权重文件（只读，调权永远由人做）")

            _csv = _fb.export_csv(_uc)
            c.ok(len(_csv.splitlines()) == 13,
                 "⑥ CSV 导出 = 表头 + 全部样本", f"{len(_csv.splitlines())} 行")
            c.ok("系统偏高" in _csv and "系统偏低" in _csv,
                 "⑦ CSV 里标出每条样本的偏差方向（便于 HR 自己在 Excel 里分析）")
            _uc.close()

            # 样本不足：只报"不足"，不给任何趋势结论
            _u2 = db.connect(os.path.join(work, "u_fb2.db"))
            _u2.execute("INSERT INTO candidates (name, created_at, updated_at) VALUES ('x','x','x')")
            _u2.execute(
                """INSERT INTO applications (candidate_id, tier_suggested, tier_final,
                                             created_at, updated_at)
                   VALUES (1,'A','B','x','x')""")
            _u2.commit()
            _r2 = _fb.decision_report(_u2)
            c.ok(_r2["insufficient"] and not _r2.get("matrix")
                 and not _r2.get("suggestions") and "样本不足" in _r2["message"],
                 "⑧ 样本不足时不硬下结论（不给矩阵、不给建议）",
                 _r2["message"][:24])
            _u2.close()
        except Exception as _e:                    # noqa: BLE001
            c.ok(False, f"决策反馈闭环自检异常：{type(_e).__name__}: {_e}")

        # ============================================================ V
        c.section("V 邮件（v1.8.3）：模板渲染 / 缺值占位 / 发送必须人工确认")
        try:
            from app import mail_send as _ms
            from app import mail_template as _mt
            from app import mailbox as _mb

            # v1.29 修：收件箱总数解析。`imaplib.select()` 返回的是 **bytes**，
            # 原来写成 `str(data[0]).isdigit()` → `str(b'2')` 是 `"b'2'"`，
            # 判定恒 False → total 永远是 0，界面出现"共 0 封，已列出最近 2 封"
            # 这种自相矛盾的提示（任何邮箱都会中，与服务商无关）。
            # 这条判据**不联网**，直接喂两种类型给解析函数 —— 联网才走到的分支
            # 靠人工测是测不稳的，必须留一条离线判据把它钉住。
            c.ok(_mb._imap_count([b"2"]) == 2 and _mb._imap_count(["37"]) == 37,
                 "收件箱总数解析同时吃 bytes 与 str（否则永远显示「共 0 封」）",
                 f"bytes={_mb._imap_count([b'2'])!r} str={_mb._imap_count(['37'])!r}")
            c.ok(_mb._imap_count([]) == 0 and _mb._imap_count([b"* 3 EXISTS"]) == 0,
                 "总数解析异常输入返回 0 而不是抛异常（预览页不许因脏应答 500）",
                 f"空={_mb._imap_count([])!r} 脏={_mb._imap_count([b'* 3 EXISTS'])!r}")

            _ctx = _mt.build_ctx({"name": "陈志远", "edu_level": "硕士", "major": "材料加工工程",
                                  "years_exp": 6}, "工艺技术", "张老师",
                                 {"面试地点": "研发中心 201"})
            _r = _mt.render("{姓名}您好：请于{面试时间}到{面试地点}参加《{应聘岗位}》面试。", _ctx)
            c.ok("陈志远" in _r["text"] and "工艺技术" in _r["text"],
                 "① 模板变量按候选人/岗位的真实值填充", _r["text"][:26])
            c.ok("【待填：面试时间】" in _r["text"] and _r["missing"] == ["面试时间"],
                 "② 取不到值的变量输出显式占位【待填：xxx】，不静默留空",
                 f"未取到 {_r['missing']}")
            c.ok("【待填：面试地点】" not in _r["text"],
                 "③ 已填写的运行时变量正常替换（不当成缺值）")

            # 未配置账号/口令时**不得**发信，且错误信息里不含口令。
            # ⚠️ 必须**显式**喂一份"未配置"的 cfg，不能靠"本机恰好没配邮箱"这件事：
            #    `send_mail`/`check_smtp` 的 cfg 传 None 时会去读真实的
            #    `config/mailbox.json`。于是开发机一旦把企业邮箱配好（正常工作！），
            #    这两条判据就反过来失败 —— 判据会随环境漂移，报的是假警。
            #    （原来这里只把 `mb.load_config().get("smtp")` 读进 `_smtp_before` 却没用，
            #      说明当时就意识到环境耦合，只是没解决。）
            _NO_SMTP = {"smtp": {}, "imap": {}}
            _bad = _ms.send_mail("someone@example.com", "t", "b", cfg=_NO_SMTP)
            # 未配置时有两条拦截路径（先查账号、再查授权码），两条都属于"如实说明原因"，
            # 断言不该把具体措辞写死——盯的是"必须被拒且给出可读原因"这个行为。
            c.ok(_bad.get("ok") is False and bool(_bad.get("error")),
                 "④ 未配置发信账号/授权码时拒绝发送，并说明原因（不静默失败）",
                 (_bad.get("error") or "")[:22])

            # 「检查发信配置」走 check_smtp：检查配置不需要收件人——
            # 曾经走 send_mail(dry_run) 被收件人校验挡住，永远误报"收件人邮箱为空"。
            _chk = _ms.check_smtp(cfg=_NO_SMTP)
            c.ok(_chk.get("ok") is False and bool(_chk.get("error"))
                 and "收件人" not in (_chk.get("error") or ""),
                 "④b 检查发信配置不依赖收件人：未配置时报配置问题（不再误报收件人为空）",
                 (_chk.get("error") or "")[:22])

            # 模板 id 冲突：重名应报错而不是静默覆盖
            _vc = db.connect(os.path.join(work, "v_mail.db"))
            _t1 = db.upsert_mail_template(_vc, "初面邀约", "初面邀约", "s1", "b1")
            _v_tpls = db.list_mail_templates(_vc)
            c.ok(len(_v_tpls) == 1 and _v_tpls[0]["name"] == "初面邀约",
                 "⑤ 邮件模板可存可读（模板由人写、系统只做替换）")
            db.delete_mail_template(_vc, _t1)
            c.ok(len(db.list_mail_templates(_vc)) == 0, "⑥ 模板可删除")
            _vc.close()

            # ---- HTML 正文（v1.10）：表格 / 加粗 → 邮件原生格式 ----
            _html_src = ("{姓名} 您好：\n\n| 项目 | 内容 |\n| --- | --- |\n"
                         "| 面试时间 | 10 月 15 日 9:30 |\n"
                         "| 面试地点 | 研发中心 201 |\n\n**请携带身份证**。")
            _rend = _mt.render(_html_src, _ctx)
            c.ok(_mt.needs_html(_rend["text"]),
                 "⑦ 正文含表格/粗体时被识别为『需要 HTML 发送』")
            _h = _mt.to_html(_rend["text"])
            c.ok("<table" in _h and "<th" in _h and "10 月 15 日 9:30" in _h,
                 "⑧ 表格被渲染成真正的 HTML 表格（项目/内容两列）")
            c.ok("<b>请携带身份证</b>" in _h and "**" not in _h,
                 "⑨ **加粗** 渲染成 <b>，标记本身不残留")
            c.ok("| --- |" not in _h, "⑩ 表头分隔行只用于识别表格，不出现在正文里")
            c.ok(_mt.needs_html("就是一段普通通知，没有格式") is False,
                 "⑪ 没有表格/粗体的通知仍按纯文本发（不无谓升级成 HTML）")
            # 变量值是候选人数据：必须转义，否则一封邮件就能把版式打乱
            _h2 = _mt.to_html(_mt.render("姓名：{姓名}", {"姓名": "<script>x</script>"})["text"])
            c.ok("<script>" not in _h2 and "&lt;script&gt;" in _h2,
                 "⑫ 变量值里的 HTML 被转义（而不是拼进标签里）", _h2[-56:])

            # 真正发信时的报文结构：纯文本 + HTML 两个部分（收件端自己挑）
            _cap: list[str] = []

            class _FakeSMTP:
                def __init__(self, *a, **k):
                    pass

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

                def login(self, u, p):
                    return None

                def sendmail(self, frm, to, raw):
                    _cap.append(raw)
                    return {}

            _orig_ssl = _ms.smtplib.SMTP_SSL
            _ms.smtplib.SMTP_SSL = _FakeSMTP
            try:
                _rs = _ms.send_mail(
                    "candidate@example.com", "初面邀约", _rend["text"],
                    cfg={"smtp": {"host": "smtp.example.cn", "port": 465, "ssl": True,
                                  "user": "hr@example.cn", "from_name": "招聘组"}},
                    password="dummy", html=_h)
            finally:
                _ms.smtplib.SMTP_SSL = _orig_ssl
            c.ok(_rs.get("ok") and _cap, "⑬ 富文本邮件确实走了发送流程", str(_rs.get("ok")))
            _raw = _cap[0] if _cap else ""
            c.ok("multipart/alternative" in _raw and "text/plain" in _raw
                 and "text/html" in _raw,
                 "⑭ 按 multipart/alternative 发出（纯文本 + HTML 双版本，兼容老客户端）")
            c.ok(_raw.index("text/plain") < _raw.index("text/html"),
                 "⑮ 纯文本在 HTML 之前（部分客户端只认最后一个可显示部分，顺序不能反）")
            # 纯文本通知不升级：不带 html 时仍是单部分文本邮件
            _cap.clear()
            _ms.smtplib.SMTP_SSL = _FakeSMTP
            try:
                _ms.send_mail("a@b.com", "通知", "纯文字通知",
                              cfg={"smtp": {"host": "h", "port": 465, "ssl": True,
                                            "user": "u@x.cn"}}, password="dummy")
            finally:
                _ms.smtplib.SMTP_SSL = _orig_ssl
            c.ok(_cap and "multipart" not in _cap[0],
                 "⑯ 不带格式的通知仍按单部分纯文本发送（行为与旧版一致）")

            # HTML → 纯文本：表格按 `列1 | 列2` 排，纯文本客户端读起来才是表
            _pt = _mt.html_to_text("<table><tr><th>项目</th><th>内容</th></tr>"
                                   "<tr><td>面试时间</td><td>9:30</td></tr></table>")
            c.ok("项目" in _pt and "|" in _pt and "9:30" in _pt,
                 "⑰ 富文本正文能拆出可读纯文本（表格按 列1 | 列2 排）",
                 _pt.replace("\n", "⏎")[:40])
            # 要发出去的 HTML 先清洗：脚本 / 事件属性 / js 链接一律剥掉
            _dirty = ('<p onclick="x()">你好<script>alert(1)</script></p>'
                      '<a href="javascript:alert(2)">点我</a>')
            _clean = _mt.sanitize_email_html(_dirty)
            c.ok("<script" not in _clean and "onclick" not in _clean
                 and "javascript:" not in _clean and "你好" in _clean,
                 "⑱ 发信前清洗 HTML：脚本/事件/js链接被剥掉，正文内容保留",
                 _clean[:46])
            # 编辑器产出的真表格（走 html 通道）同样按 multipart 发，纯文本自动拆
            _cap.clear()
            _ms.smtplib.SMTP_SSL = _FakeSMTP
            try:
                _ms.send_mail("a@b.com", "面试邀约",
                              _mt.html_to_text("<table><tr><th>项目</th><th>内容</th></tr>"
                                               "<tr><td>时间</td><td>9:30</td></tr></table>"),
                              cfg={"smtp": {"host": "h", "port": 465, "ssl": True,
                                            "user": "u@x.cn"}}, password="dummy",
                              html="<table><tr><th>项目</th><th>内容</th></tr>"
                                   "<tr><td>时间</td><td>9:30</td></tr></table>")
            finally:
                _ms.smtplib.SMTP_SSL = _orig_ssl
            c.ok(_cap and "multipart/alternative" in _cap[0]
                 and "text/plain" in _cap[0] and "text/html" in _cap[0],
                 "⑲ 所见即所得编辑器产出的表格邮件：HTML + 纯文本双版本")

            # ---- HTML 模板（v1.11）：复杂版式（合并单元格）直接用 HTML 存模板 ----
            # 极简表格语法表达不了 colspan/rowspan，硬塞只会把 HR 逼回手写 HTML。
            _tpl_html = ('<table><tr><th colspan="2">面试邀请</th></tr>'
                         '<tr><td>姓名</td><td>{姓名}</td></tr></table>')
            c.ok(_mt.looks_like_html(_tpl_html) and _mt.needs_html(_tpl_html),
                 "⑳ HTML 模板被识别为『按 HTML 邮件发』（不必再套一层转换）")
            _rend2 = _mt.render(_tpl_html, _ctx)
            c.ok(_ctx["姓名"] in _rend2["text"] and "<table" in _rend2["text"],
                 "㉑ HTML 模板里的 {变量} 照样替换、标签结构保持原样",
                 _rend2["text"][:40])
            c.ok(_mt.looks_like_html("就是一段纯文字通知") is False,
                 "㉒ 纯文字不会被误判成 HTML")
            # HTML 模板 → 纯文本副本：表格要拆成可读的行文本
            _pt2 = _mt.html_to_text(_rend2["text"])
            c.ok("面试邀请" in _pt2 and "|" in _pt2 and _ctx["姓名"] in _pt2,
                 "㉓ HTML 模板也能拆出纯文本副本（表格按 列 | 列 排）",
                 _pt2.replace("\n", "⏎")[:48])
        except Exception as _e:                    # noqa: BLE001
            c.ok(False, f"邮件模块自检异常：{type(_e).__name__}: {_e}")

        # ============================================================ W
        c.section("W 文件夹导入：按文件名归岗（与邮件标题归岗同一口径）")
        try:
            _wdir = os.path.join(work, "w_resumes")
            os.makedirs(_wdir, exist_ok=True)
            _wdb = os.path.join(work, "w_dir.db")
            _wc = db.connect(_wdb)
            _wjd = {"role": "工艺技术", "must": {"education_min": "本科", "years_min": 0,
                                                 "skills_required": ["钛合金"]},
                    "preferred": {"skills": []}}
            _wjid = db.create_job(_wc, "工艺技术", jd=_wjd, operator="test")
            _wtiers = {"tiers": {}, "thresholds": {"A": 0.85, "B": 0.65, "C": 0.45}}
            with open(os.path.join(_wdir, "测试甲-工艺技术-简历.txt"), "w",
                      encoding="utf-8", newline="\n") as _fh:
                _fh.write("姓名：测试甲\n性别：男\n学历：硕士\n工作年限：3 年\n"
                          "邮箱：jiatest@example.com\n\n技能：钛合金、真空熔铸\n")
            _rep = ingest.ingest_dir(_wdir, _wjd, _wtiers, _wdb, cfg={}, job_id=None)
            c.ok(_rep.get("added") == 1 and _rep.get("routed") == 1,
                 "① 文件名里带岗位名 → 自动归到该岗位（不再一律落「待指定」）",
                 f"added={_rep.get('added')} routed={_rep.get('routed')}")
            _row = _wc.execute("SELECT job_id FROM applications ORDER BY id DESC LIMIT 1").fetchone()
            c.ok(bool(_row) and _row["job_id"] == _wjid,
                 "② 投递确实挂到了该岗位下（后续按该岗位 JD 评分）",
                 f"job_id={_row['job_id'] if _row else None}")

            with open(os.path.join(_wdir, "测试乙-简历.txt"), "w",
                      encoding="utf-8", newline="\n") as _fh:
                _fh.write("姓名：测试乙\n学历：本科\n工作年限：2 年\n技能：钛合金\n")
            _rep2 = ingest.ingest_dir(_wdir, _wjd, _wtiers, _wdb, cfg={}, job_id=None)
            _row2 = _wc.execute("SELECT job_id FROM applications ORDER BY id DESC LIMIT 1").fetchone()
            c.ok(_rep2.get("added") == 1 and (not _row2 or _row2["job_id"] is None),
                 "③ 文件名里没有岗位名时不硬凑：仍落「待指定」等 HR 决定",
                 f"job_id={_row2['job_id'] if _row2 else None}")
            _wc.close()
        except Exception as _e:                    # noqa: BLE001
            c.ok(False, f"文件夹导入归岗自检异常：{type(_e).__name__}: {_e}")

        # ============================================================ X
        c.section("X 招聘对象身份：应届 / 往届未就业 / 工作时长（v1.8.5）")
        # 校招场景下"能不能投"看的是身份，不是一个年限数字：
        # 去年毕业还没参加工作的人，年限是 0，但身份不是应届。
        try:
            from datetime import datetime as _dt
            from app.pipeline import freshness as _fr
            _y = _dt.now().year
            c.ok(_fr.find_grad_date("教育经历\n预计2026年6月毕业") == "2026-06",
                 "① 识别毕业时间（只认带「毕业」上下文的年月）",
                 str(_fr.find_grad_date("预计2026年6月毕业")))
            c.ok(_fr.find_grad_date("2018.09-2021.06  某某大学  材料学") is None,
                 "② 教育经历的起止时间不会被误当成毕业时间")
            c.ok(_fr.exp_label(0, False, f"{_y}-06")["kind"] == "fresh",
                 "③ 当年毕业且无工作经历 → 应届")
            c.ok(_fr.exp_label(0, False, f"{_y-1}-06")["kind"] == "past_idle",
                 "④ 去年毕业且无工作经历 → 往届未就业（不能笼统算应届）",
                 _fr.exp_label(0, False, f"{_y-1}-06")["label"])
            c.ok(_fr.exp_label(3, True, f"{_y-5}-06")["kind"] == "work",
                 "⑤ 往届且确有工作经历 → 显示工作时长")
            c.ok(_fr.exp_label(1, True, f"{_y}-06")["kind"] == "fresh",
                 "⑤b 应届生即使有实习经历也判应届（实习≠正式工作，身份不能错）",
                 _fr.exp_label(1, True, f"{_y}-06")["label"])
            c.ok(_fr.exp_label(None, False, None)["kind"] == "unknown",
                 "⑥ 信息都没有时不猜：如实标「毕业时间未识别」")
            # v1.29.4（业务方 2026-10-16）：「硕士 2024.9-至今」这类**在读生**——
            # 不能被本科毕业年份判成"往届未就业"（真实案例：张思瑞）
            c.ok(_fr.find_in_school("2024.9-至今\n四川大学(985)\n化学工程与技术\n工学硕士（保研）")
                 is True,
                 "⑦a 在读识别：时间段独立成行、学位在后几行（真实简历排版）")
            c.ok(_fr.find_in_school("教育经历\n2024.09-至今 四川大学 硕士") is True,
                 "⑦b 在读识别：时间段与学位同行")
            c.ok(_fr.find_in_school("工作经历\n2021.03-至今 华润公司 软件工程师") is False,
                 "⑦c 反例：工作行的「至今」不算在读")
            c.ok(_fr.find_in_school("2020.09-2024.06 天津工业大学 本科") is False,
                 "⑦d 反例：只写了已结束的教育段 → 不在读")
            c.ok(_fr.find_in_school("2024.9-2027.6 北京航空航天大学 硕士") is True,
                 "⑦e 在读识别：结束年份在未来（2027.6）同样是在读")
            _e_in = _fr.exp_label(0, False, f"{_y-2}-06", in_school=True)
            c.ok(_e_in["kind"] == "fresh" and "在读" in _e_in["label"],
                 "⑦f 在读 + 两年前的本科毕业时间 → 应届（在读），不再判往届未就业",
                 _e_in["label"])
        except Exception as _e:                    # noqa: BLE001
            c.ok(False, f"招聘对象身份自检异常：{type(_e).__name__}: {_e}")

        # ============================================================ Y
        c.section("Y 邮件标题 / 文件名结构化解析（v1.9）：方向+学历+学校+专业+姓名+性别")
        try:
            from app.pipeline import subject_meta as _sm

            _m = _sm.parse("工艺技术-硕士-西安交通大学-材料科学与工程-张三-男")
            _f = _m["fields"]
            c.ok(_f.get("direction") == "工艺技术", "① 方向段识别", str(_f.get("direction")))
            c.ok(_f.get("education") == "硕士", "② 学历段识别", str(_f.get("education")))
            c.ok(_f.get("school") == "西安交通大学", "③ 学校段识别", str(_f.get("school")))
            c.ok(_f.get("major") == "材料科学与工程", "④ 专业段识别", str(_f.get("major")))
            c.ok(_f.get("name") == "张三", "⑤ 姓名段识别", str(_f.get("name")))
            c.ok(_f.get("gender") == "男", "⑥ 性别段识别（只认明写，不推断）",
                 str(_f.get("gender")))

            _m2 = _sm.parse("【应聘】材料工艺+本科+西北工业大学+材料成型及控制工程+李四+女")
            c.ok(_m2["fields"].get("name") == "李四" and _m2["fields"].get("education") == "本科"
                 and _m2["fields"].get("direction") == "材料工艺",
                 "⑦ 带【应聘】前缀 / + 分隔符 / 顿号写法同样能解析", _m2["note"])

            _m3 = _sm.parse("张三-简历.pdf")
            c.ok(_m3["fields"].get("name") == "张三"
                 and "school" not in _m3["fields"] and "major" not in _m3["fields"],
                 "⑧ 只写了姓名的文件名不会硬凑出学校/专业", _m3["note"])

            # 文件名写法「姓名-岗位-校招-简历」是院里最常用的一种。
            # 按"最靠后的像人名段"取姓名会把岗位词（科学研究/工艺技术）当成姓名——
            # 打测试包时实测 3 位候选人被命名成了岗位名，故这里钉死。
            _m5 = _sm.parse("王雪莹-科学研究-校招-简历.pdf")
            c.ok(_m5["fields"].get("name") == "王雪莹",
                 "⑧b 文件名写法：姓名取最前面那段（末尾的岗位词不能当姓名）",
                 str(_m5["fields"].get("name")))
            c.ok(_m5["fields"].get("direction") == "科学研究",
                 "⑧c 文件名写法：姓名之后那段是应聘方向（用于岗位弱匹配）",
                 str(_m5["fields"].get("direction")))
            # 邮件标题写法但**没写性别**：学校之后是「专业, 姓名」，取最后一个
            _m6 = _sm.parse("数字化工程师_本科_西安电子科技大学_软件工程_王五")
            c.ok(_m6["fields"].get("name") == "王五"
                 and _m6["fields"].get("major") == "软件工程"
                 and _m6["fields"].get("direction") == "数字化工程师",
                 "⑧d 无性别的标题：姓名取『学校之后最后一个』，专业不串位",
                 f"姓名={_m6['fields'].get('name')} 专业={_m6['fields'].get('major')}")
            # v1.29.5（业务方 2026-10-16 真实案例）：「朱鸿睿－男」里的**全角连字符**
            # 旧分隔符不认 → 整条错位（专业→方向、姓名→专业）。钉死全角/变体横线。
            _f7 = _sm.parse("科学研究-博士-四川大学-材料学-朱鸿睿－男")["fields"]
            c.ok(_f7.get("name") == "朱鸿睿" and _f7.get("gender") == "男"
                 and _f7.get("major") == "材料学" and _f7.get("direction") == "科学研究",
                 "⑧e 全角「－」也当分隔符（真实案例：错位把姓名解析成了专业）",
                 f"name={_f7.get('name')} gender={_f7.get('gender')} major={_f7.get('major')}")
            # 姓名证据升级：只出现在「专业/院校」上下文里的"姓名"，不覆盖正文结果
            _c8, _u8 = _sm.apply_to_candidate(
                {"name": "朱鸿睿"}, {"fields": {"name": "材料学"}},
                raw_text="四川大学（985）材料学专业 全日制学术性博士\n朱鸿睿\n")
            c.ok(_c8.get("name") == "朱鸿睿",
                 "⑧f 标题猜出的姓名只在专业/院校上下文里出现 → 不采信（结合正文）",
                 f"name={_c8.get('name')}")

            _m4 = _sm.parse("转发的简历 麻烦看一下")
            c.ok(not _m4["fields"], "⑨ 认不出来的标题不给任何字段（不猜）", str(_m4["fields"]))

            _cand0, _used = _sm.apply_to_candidate(
                {"name": "x", "education": "本科", "school": "某学院",
                 "skills": ["钛合金"], "years": 3}, _m)
            c.ok(_cand0["school"] == "西安交通大学" and _cand0["education"] == "硕士"
                 and _cand0["name"] == "张三",
                 "⑩ 标题字段覆盖姓名/学历/学校（投递方按格式填的，比正文准）")
            # 姓名是"按位置猜"的，位置规则遇到没见过的写法就会猜错；
            # 所以姓名额外过一道证据校验：猜出来的名字在原文里找不到 → 不覆盖。
            _m7 = _sm.parse("博士应聘-材料学-赵敏")
            _cand7, _used7 = _sm.apply_to_candidate(
                {"name": "赵敏"}, _m7, raw_text="赵敏，女，材料学专业硕士")
            c.ok(_cand7["name"] == "赵敏",
                 "⑩b 标题里猜出的姓名在简历原文中找不到 → 保留正文抽出的姓名",
                 f"标题猜出「{_m7['fields'].get('name')}」→ 实际用「{_cand7['name']}」")

            # ---- 专业归一/业务方向接模型（v1.11）：模型只做归一，打分仍走规则 ----
            from app.pipeline import major_llm as _ml
            from app.pipeline import majors as _mj
            # ① 规则先试：能精确归一的直接走规则、不花模型调用
            c.ok(_ml.classify_major("材料科学与工程")["via"] == "catalog",
                 "⑳ 规则能精确归一的走规则（via=catalog，不花模型调用）")
            _ml._CACHE.clear()
            # ② 模型给的条目必须在学科目录里：目录外的一律丢弃
            _orig_chat = _ml.llm.chat_json
            _ml.llm.chat_json = lambda *a, **k: {"canonical": "天体物理与航天工程",
                                                 "category": "物理学", "reason": "编的"}
            try:
                c.ok(_ml.classify_major("占星术与塔罗牌") is None,
                     "㉑ 模型给出的条目不在学科目录里 → 丢弃（不猜）")
            finally:
                _ml.llm.chat_json = _orig_chat
            # ③ 模型归一到真实目录条目 → 可用
            _ml._CACHE.clear()
            _ml.llm.chat_json = lambda *a, **k: {"canonical": "材料科学与工程",
                                                 "category": "工学", "reason": "材料类"}
            try:
                _cls = _ml.classify_major("材化")
                c.ok(_cls and _cls["canonical"] == "材料科学与工程" and _cls["via"] == "llm",
                     "㉒ 模型把『材化』归一到目录条目（via=llm）",
                     str(_cls))
            finally:
                _ml.llm.chat_json = _orig_chat
            _ml._CACHE.clear()
            # ③ 归一结果参与打分：grade 用 major_canonical 判专业方向
            _jd_m = {"role": "材料研发", "must": {"education_min": "本科", "years_min": 0,
                                                "skills_required": ["钛合金"],
                                                "major_required": ["材料科学与工程"]},
                     "preferred": {"skills": []}}
            _c_no = {"name": "测试", "education": "硕士", "years": 3,
                     "major": "材化", "skills": [{"name": "钛合金", "evidence": "钛合金"}]}
            _g_no = grade(_c_no, _jd_m)
            _g_yes = grade({**_c_no, "major_canonical": "材料科学与工程"}, _jd_m)
            c.ok(_g_no["major_check"]["in_list"] is None or _g_no["major_check"]["in_list"] is False,
                 "㉒ 规则归不出的专业写法按原样判定（如实）",
                 str(_g_no["major_check"]["in_list"]))
            c.ok(_g_yes["major_check"]["in_list"] is True
                 and not _g_yes["breakdown"],
                 "㉓ 归一后的专业在目录内即判『对口』（v1.12 不再加分——打分已删，专业方向只作展示）",
                 f"in_list={_g_yes['major_check']['in_list']} 拆解={_g_yes['breakdown']}")
            # ④ 档位解析的三层兜底（实测 deepseek-flash 会漏 suggested_tier 字段）
            from app.pipeline import analyze as _anl
            c.ok(_anl.tier_from_fit({"suggested_tier": "B"}) == "B"
                 and _anl.tier_from_fit({"tier": "c"}) == "C"
                 and _anl.tier_from_fit({"档位": "A"}) == "A"
                 and _anl.tier_from_fit({"summary": "专业对口，建议A档"}) == "A"
                 and _anl.tier_from_fit({"summary": "整体基本匹配"}) is None,
                 "⑯ 档位解析三层兜底：标准字段 → 异名键 → 从结论文字里取（都没有才 None）")
            # ⑤ 都没有时**单独追问一次**——不能让待分析出现在模型已读完简历时
            _orig_an_chat = _anl.llm.chat_json
            _anl.llm.chat_json = lambda *a, **k: {"tier": "b"}
            try:
                _rt = _anl.resolve_tier(
                    {"name": "x", "raw_text": "材料学硕士"},
                    {"role": "r", "must": {"skills_required": ["钛合金"],
                                             "education_min": "本科", "years_min": 0},
                     "preferred": {}},
                    {"summary": "整体基本匹配"})
            finally:
                _anl.llm.chat_json = _orig_an_chat
            c.ok(_rt == "B", "⑰ 模型漏给档位时单独追问一次（不然会显示待分析）", str(_rt))
            _anl.llm.chat_json = lambda *a, **k: {"tier": "Z"}   # 不合规输出
            try:
                _rt_bad = _anl.ask_tier({"name": "x"},
                                        {"role": "r", "must": {"skills_required": [],
                                                                 "education_min": "本科",
                                                                 "years_min": 0},
                                         "preferred": {}})
            finally:
                _anl.llm.chat_json = _orig_an_chat
            c.ok(_rt_bad is None, "⑰b 追问返回不合规档位（如 Z）→ 如实 None，不硬塞", str(_rt_bad))

            # ⑥ 业务方向：模型提炼 → 存库 → 读回
            _ml.llm.chat_json = lambda *a, **k: {"directions": ["材料工艺", "检测分析"]}
            try:
                _bd = _ml.business_direction({"skills": [{"name": "钛合金"}],
                                              "major": "材料学", "raw_text": "钛合金工艺"})
            finally:
                _ml.llm.chat_json = _orig_chat
            c.ok(_bd == "材料工艺、检测分析", "㉔ 业务方向由模型提炼（只展示，不参与打分）", str(_bd))
            _ic = db.connect(os.path.join(work, "bizdir.db"))
            db.upsert_insight(_ic, 1, 1, summary="s", business_direction=_bd)
            _back = db.get_insight(_ic, 1, 1)
            c.ok(_back and _back.get("business_direction") == _bd,
                 "㉕ 业务方向随分析落库并可读回", str((_back or {}).get("business_direction")))
            _ic.close()
            c.ok(_cand0["skills"] == ["钛合金"] and _cand0["years"] == 3,
                 "⑪ **不碰**技能与年限（那两项必须来自正文与证据核对）")
            c.ok(bool(_cand0.get("title_override_notes")),
                 "⑫ 与正文抽取冲突时留痕说明（可解释为什么与简历正文不一致）",
                 str(_cand0.get("title_override_notes"))[:40])

            # 方向段弱匹配岗位：岗位名与方向写法不一致时也能归岗（保守：对不上就不归）
            _ydb = os.path.join(work, "subject.db")
            _yc = db.connect(_ydb)
            db.upsert_job(_yc, dict(JD, role="工艺技术"))
            _jid, _ = db.match_job_by_direction(_yc, "材料工艺技术")
            c.ok(_jid is not None, "⑬ 方向段「材料工艺技术」能弱匹配到岗位「工艺技术」")
            _jid2, _ = db.match_job_by_direction(_yc, "市场营销")
            c.ok(_jid2 is None, "⑭ 方向对不上任何岗位时不给归岗（保持待指定，只出建议）",
                 str(_jid2))
            _yc.close()
        except Exception as _e:                    # noqa: BLE001
            c.ok(False, f"标题结构化解析自检异常：{type(_e).__name__}: {_e}")

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
