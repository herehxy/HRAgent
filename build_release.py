"""制作可分发的测试包：干净的应用 + 10 位样例候选人 + 使用说明。

三条硬要求（每条都是踩过的坑）：

1. **绝不能带真实凭据**。安装目录 `_internal/config/` 里躺着
   `secrets.json`（模型 API Key）、`mailbox.json`（HR 邮箱账号）、
   `imap.secret`（邮箱授权码）、`master.key`（库内联系方式加密主密钥）——打包前一律剔除，
   只保留 `*.example.json` 作模板。

2. **master.key 必须与样例数据配套**。库里电话/邮箱是用 master.key 加密的：
   带旧库配新 key（或反过来）都会解不出来、联系人显示成乱码。所以流程做成
   "先生成一把全新的 key → 用这把 key 导入样例"，key 与数据同源、开箱即用，
   同时**不把我本机那把 key 发出去**（通过 `TP_MASTER_KEY` 环境变量注入）。

3. **数据目录里只留样例**：本机的 webapp.log / mail_in / backup / removed 等痕迹全部清掉。

用法：``python build_release.py``（会自行调用 PyInstaller 之外的一切；dist 需已构建）
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import sqlite3
import sys
import time
import zipfile
import zipfile as _zip

# 先关掉一切后台自动化：入库会 fork 一个"自动分析"线程，它会一直占着 SQLite 句柄，
# 导致打包前连 -wal/-shm 都改不了名（WinError 32）。样例数据不需要模型分析。
os.environ.setdefault("TP_AUTO_INSIGHT", "0")
os.environ.setdefault("TP_DAILY_TASK", "0")
os.environ.setdefault("TP_BRIEF_LLM", "0")

ROOT = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(ROOT, "repo_src")
DIST = os.path.join(ROOT, "dist", "HRAgent工作台")
REL = os.path.join(ROOT, "release", "HRAgent工作台")
DATA = os.path.join(REL, "_internal", "data")
CFG = os.path.join(REL, "_internal", "config")
DB = os.path.join(DATA, "workbench.db")

#: 打包前必须删掉的凭据/密钥（示例文件保留）
SECRETS = ["secrets.json", "mailbox.json", "imap.secret", "master.key", "tp.secret",
           "model.key", "auth.json"]

ZIP_NAME = "HRAgent工作台-Windows版-v1.8.9-含10份样例数据.zip"

README = """企业人才库智能体 · HR 工作台（测试版）
================================================

一、怎么跑起来
--------------
1. 把整个文件夹解压到任意位置（**别只解压 exe**，_internal 是程序本体）。
2. 双击 `HRAgent工作台.exe`。
3. 服务在 http://127.0.0.1:8756 启动后会自动打开浏览器。没弹出就手动访问该地址。
4. 右上角/托盘图标：右键托盘图标可以「打开工作台 / 打开数据目录 / 退出」。
   退出请用它——直接关浏览器窗口不会停服务。

二、这份包里带了什么
--------------------
- 10 位样例候选人（4 个在招岗位：科学研究 / 工艺技术 / 检验检测 / 数字化工程师），
  简历是 PDF，文件名带岗位名，所以导入时能自动归岗。
- 2 个邮件模板（其中「面试邀请（院标准版式）」是院里那张表格的版式）。
- 全部数据都在本机：`_internal/data/workbench.db`（数据库）、
  `_internal/data/archive/`（简历原件）、`_internal/config/`（配置）。

三、建议的试用顺序
------------------
1. 「今日待办」看系统自己发现了什么（待确认提案）。
2. 「人才库」看候选人卡片：档位、学历是否达线、专业方向、命中/缺失技能及原文证据。
3. 点「完整档案」看简历原文、附件、投递记录、操作审计。
4. 「岗位管理」看 JD 尺子（评分按它算），可改必需技能/最低学历后按新尺子重算。
5. 「写邮件」选一位候选人 + 选模板 → 生成草稿 → 在编辑器里改表格 → 确认发送。
   （发信需要先配好自己的邮箱，见下。）

四、要真正用起来，需要配两处
----------------------------
1. 模型（可选）：系统配置 → 模型与密钥，填 API Key。
   不填也能用：规则通道照样完成解析、打分、归档，只是没有模型判断的分析文本。
2. 邮箱（可选）：
   收信：系统配置 → 收信配置（IMAP 服务器 + 授权码，163/QQ 都行）
   发信：系统配置 → 发信配置（SMTP，163 默认 smtp.163.com:465 SSL）
   点「检查发信配置」只验证凭据、不会真发信。
   注意：国内邮箱要用**授权码**，不是登录密码。

五、想清掉样例数据、开始用自己的简历
------------------------------------
- 直接用「归档」页勾选整批归档（归档是软删除，可随时恢复）；或
- 停服后删掉 `_internal/data/workbench.db`，重启应用会自动重建一个空库。

六、几条设计约定（避免误解）
----------------------------
- **只建议不决定**：系统给出档位建议、提案，都要你确认后才生效，全程有审计留痕。
- **反幻觉**：技能必须有简历原文片段为证才算命中，模型说的也要逐字核对。
- **合规**：性别、民族、婚姻生育等敏感信息在送模型前就被屏蔽，且不参与评分。
- **D 档只代表学历不达标**（且投递已明确归岗），其余情况最低 C（储备），不会一票否。
"""


def _retire(path: str) -> None:
    """安全删除机制会拦批量删除，统一改名为 .old_<时间戳>（可回退）。"""
    if os.path.exists(path):
        os.rename(path, path + ".old_" + time.strftime("%m%d_%H%M%S"))


def main() -> int:
    if not os.path.isdir(DIST):
        print("[×] 找不到 dist/HRAgent工作台，请先跑 PyInstaller 打包")
        return 1

    # ---- 1) 复制出干净的发布目录 ----
    _retire(REL)
    os.makedirs(os.path.dirname(REL), exist_ok=True)
    shutil.copytree(DIST, REL)
    print(f"[√] 已复制应用 -> {REL}")

    # ---- 2) 清运行痕迹 + 剔除凭据 ----
    for f in os.listdir(DATA) if os.path.isdir(DATA) else []:
        p = os.path.join(DATA, f)
        _retire(p)
    os.makedirs(DATA, exist_ok=True)
    removed = []
    for name in SECRETS:
        p = os.path.join(CFG, name)
        if os.path.exists(p):
            os.remove(p)
            removed.append(name)
    print(f"[√] 已清理运行数据；剔除凭据 {len(removed)} 个：{removed or '（无）'}")

    # ---- 3) 生成这把发布包专属的主密钥（与样例数据同源）----
    key_bytes = os.urandom(32)
    key_b64 = base64.b64encode(key_bytes).decode("ascii")
    with open(os.path.join(CFG, "master.key"), "w", encoding="ascii", newline="\n") as fh:
        fh.write(key_b64)
    os.environ["TP_MASTER_KEY"] = key_b64        # 导入样例时用的是同一把
    print("[√] 已为本发布包生成新的加密主密钥（不含你本机那把）")

    # ---- 4) 建库 / 建岗位 / 生成并导入 10 份样例 ----
    sys.path.insert(0, SRC)
    sys.path.insert(0, ROOT)
    from app import db, ingest                                 # noqa: E402
    import import_jobs_2026 as jobs2026                        # noqa: E402
    import rebuild_samples_10 as samples                       # noqa: E402

    tiers = json.load(open(os.path.join(SRC, "config", "tiers.json"), encoding="utf-8"))
    jd_default = json.load(open(os.path.join(SRC, "config", "jd.json"), encoding="utf-8"))
    conn = db.connect(DB)
    try:
        for j in jobs2026.JOBS:
            jd = {
                "role": j["title"],
                "department": "",
                "must": {"education_min": j["education_min"],
                         "years_min": j["years_min"],
                         "skills_required": j["must_skills"],
                         "major_required": j["major_required"]},
                "preferred": {"skills": j["preferred_skills"], "certificates": []},
                "note": j.get("note", ""),
                "origin": "随包样例",
            }
            jid = db.upsert_job(conn, jd, title=j["title"], owner="system")
            print(f"    岗位 {j['title']} #{jid}")
    finally:
        conn.close()

    pdf_dir = os.path.join(os.environ.get("TEMP", "."), "hragent_release_samples")
    os.makedirs(pdf_dir, exist_ok=True)
    for f in os.listdir(pdf_dir):
        if f.lower().endswith(".pdf"):
            os.remove(os.path.join(pdf_dir, f))
    for p in samples.PEOPLE:
        samples.make_pdf(os.path.join(pdf_dir, f"{p['name']}-{p['job']}-{p['kind']}-简历.pdf"),
                         samples.body_lines(p))

    cfg = {"archive_dir": os.path.join(DATA, "archive"),
           "resume_dir": os.path.join(DATA, "resumes"),
           "max_attachment_mb": 20,
           "dedup": {"same_job_reapply_days": 30}}
    rep = ingest.ingest_dir(pdf_dir, jd_default, tiers, DB, cfg=cfg,
                            channel="文件夹", job_id=None)
    print(f"[√] 样例导入：新增 {rep['added']} 份、自动归岗 {rep.get('routed', 0)} 份、"
          f"待指定 {rep.get('unassigned', 0)} 份")

    # ---- 5) 带上邮件模板（从本机库拷，模板是内容不是密钥）----
    try:
        src_db = r"D:\HRAgent工作台\_internal\data\workbench.db"
        if os.path.exists(src_db):
            s = sqlite3.connect(src_db)
            tpls = s.execute("SELECT name, scene, subject, body FROM mail_templates").fetchall()
            s.close()
            c2 = db.connect(DB)
            for name, scene, subject, body in tpls:
                db.upsert_mail_template(c2, name, scene or "其他通知", subject or "", body or "")
            c2.close()
            print(f"[√] 已带上 {len(tpls)} 个邮件模板")
    except Exception as exc:                                   # noqa: BLE001
        print(f"[!] 模板拷贝跳过：{type(exc).__name__}: {exc}")

    # ---- 6) 校验：人数、可解密、无凭据 ----
    conn = db.connect(DB)
    try:
        n = conn.execute("SELECT COUNT(*) AS n FROM candidates").fetchone()["n"]
        apps = conn.execute("SELECT COUNT(*) AS n FROM applications").fetchone()["n"]
        routed = conn.execute("SELECT COUNT(*) AS n FROM applications WHERE job_id IS NOT NULL"
                              ).fetchone()["n"]
    finally:
        conn.close()
    from app import auth
    contact_ok = True
    conn = db.connect(DB)
    try:
        for row in conn.execute("SELECT id FROM candidates ORDER BY id").fetchall():
            d = db.candidate_detail(conn, row["id"])
            if not d:
                continue
            pr = auth.present_candidate(d)
            if not (pr.get("phone") or pr.get("email")):
                contact_ok = False
                print(f"    [!] #{row['id']} 联系方式解不出来（key 与数据不配套？）")
    finally:
        conn.close()
    print(f"[√] 校验：候选人 {n} 人 / 投递 {apps} 条（其中已归岗 {routed} 条）/ "
          f"联系方式解密 {'正常' if contact_ok else '异常'}")

    # ---- 7) 收尾：把 WAL 合并回主库（否则包里会带上 -wal/-shm 临时文件）----
    # 注意：这里**不能 os.remove**——运行环境的安全删除机制会拦住（fail-closed），
    # 统一改名成 .old_<时间戳>，打 zip 时按名字跳过（下面的过滤规则会排除 *.old_*）。
    if os.path.exists(DB):
        ck = sqlite3.connect(DB)
        try:
            ck.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            ck.commit()
        finally:
            ck.close()
    for suffix in ("-wal", "-shm"):
        _retire(DB + suffix)

    # ---- 8) 使用说明 ----
    with open(os.path.join(REL, "使用说明.txt"), "w", encoding="utf-8", newline="\r\n") as fh:
        fh.write(README)
    # v1.18：功能清单随包一起发（HR 不用来问"这个能不能做"）
    _feat = os.path.join(os.path.dirname(os.path.abspath(__file__)), "功能清单.md")
    if os.path.exists(_feat):
        shutil.copy2(_feat, os.path.join(REL, "功能清单.md"))

    # ---- 9) 打 zip（中文名用 UTF-8 flag，Win10+ 资源管理器能正确解压）----
    out = os.path.join(ROOT, ZIP_NAME)
    total = 0
    with _zip.ZipFile(out, "w", _zip.ZIP_DEFLATED, compresslevel=6) as z:
        for root, dirs, files in os.walk(REL):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for f in files:
                if (f.endswith(".pyc") or f.endswith(".old") or ".old_" in f
                        or f.endswith("-wal") or f.endswith("-shm")):
                    continue        # SQLite 临时文件（被占用删不掉）与缓存，一律不进包
                full = os.path.join(root, f)
                z.write(full, os.path.relpath(full, os.path.dirname(REL)))
                total += os.path.getsize(full)
    print(f"[√] 打包完成：{ZIP_NAME}（{total / 1024 / 1024:.1f} MB 原始 / "
          f"{os.path.getsize(out) / 1024 / 1024:.1f} MB 压缩后）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
