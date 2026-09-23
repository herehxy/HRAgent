"""按 2026 年招聘公告写入 4 个岗位（可重复执行）。

**只写岗位名 + JD，不建任何部门。**
为什么去掉部门：部门只影响展示，不参与打分、归岗、重算任何一环
（`jobs.dept_id` 在表结构里本来就允许为空），而 4 个岗位配 3 个部门只会让
"部门太多了"——HR 得先想清楚简历归哪个部门才敢建岗位，多一步纯负担。
现在加一个岗位就是：填岗位名 → 填 JD → 保存。

做三件事，每一件都**先算后写**并写审计：

1. **备份**：先把数据库复制到 `data/backup/workbench-before-jobswap-<时间戳>.db`
   （文件名唯一，不会被同秒的第二次执行覆盖——踩过这个坑）；
2. **清掉同名旧岗位的引用**：引用它们的投递**不删**，只把 `job_id` / `suggested_job_id`
   置空，投递随即显示为「所属岗位待指定」——**人不该因为岗位调整而丢**；
3. **写入 4 个岗位**：专业需求写进 `must.major_required`（**不是**技能字段）。

## 为什么专业需求单独放一个字段

公告里的「材料科学与工程 / 凝聚态物理 / 仪器科学与技术」是**学科名**，不是技能。
塞进「必需技能」的后果是：候选人技能栏里永远不会出现这几个字，
**命中率恒为 0，全员被判 D**，而界面上完全看不出原因。

## 为什么技能项是"翻译"过的

`must_skills` 用的是**技能本体里已有的词**（`cli.py majors` 查学科、本体查技能）。
公告的专业需求 → 岗位实际要看的能力，这一步翻译必须由人（HR）来做并说清楚，
系统不该代劳——所以脚本里把它写成显式映射，而不是让模型自由发挥。

用法::

    python tools/seed_2026_jobs.py --dry-run     # 先看会改什么
    python tools/seed_2026_jobs.py               # 落地
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app import db                                    # noqa: E402
from app.pipeline import majors as mj                 # noqa: E402
from app.pipeline import normalize as nz              # noqa: E402

DB_PATH = os.environ.get("TP_DB_PATH") or os.path.join(ROOT, "data", "workbench.db")
BACKUP_DIR = os.path.join(ROOT, "data", "backup")

# 历史遗留岗位名（早期版本带部门种子建出来的）。存在就清掉，不存在就跳过——
# 脚本因此可以反复执行，也不会在旧库上留下两个"幽灵岗位"。
RETIRE = [
    "工艺工程师（钛合金 / 难熔合金方向）",
    "软件开发工程师",
]

EDU = "硕士"      # 公告：招聘对象为博士、硕士研究生 → 门槛取硕士（博士同样满足）
YEARS = 0         # 公告未设年限门槛（校招/应届为主）

JOBS = [
    {
        "title": "科学研究",
        "must_skills": ["金相分析", "X射线衍射", "力学性能测试", "扫描电镜"],
        "preferred_skills": ["透射电镜", "能谱分析", "电子背散射衍射", "组织分析", "失效分析",
                             "Materials Studio", "VASP", "LAMMPS", "Thermo-Calc",
                             "专利撰写", "科技论文写作", "项目申报"],
        "major_required": ["材料科学与工程", "材料学", "材料加工",
                           "材料物理与化学", "粉末冶金", "凝聚态物理"],
        "note": "面向博士/硕士。公告专业需求：材料科学与工程、材料学、材料加工、"
                "材料物理与化学、粉末冶金、凝聚态物理等相关专业。",
    },
    {
        "title": "工艺技术",
        "must_skills": ["真空熔铸", "热处理", "材料成型", "金相分析"],
        "preferred_skills": ["粉末冶金", "难熔合金", "钛合金", "锻造", "热加工", "增材制造",
                             "DEFORM", "ProCAST", "工艺文件编制", "技术标准编制"],
        "major_required": ["材料学", "材料工程", "化学工程", "材料物理"],
        "note": "面向博士/硕士。公告专业需求：材料学、材料工程、化学工程、材料物理等相关专业。",
    },
    {
        "title": "检验检测",
        "must_skills": ["无损检测", "超声检测", "金相分析"],
        "preferred_skills": ["工业CT", "射线检测", "渗透检测", "磁粉检测", "相控阵超声检测",
                             "涡流检测", "失效分析", "力学性能测试", "无损检测工艺编制",
                             "无损检测标准", "无损检测资质", "计量校准"],
        "major_required": ["仪器科学与技术", "仪器仪表工程", "无损检测", "测控技术与仪器"],
        "note": "面向硕士。公告专业需求：仪器科学与技术、仪器仪表工程等无损检测方向相关专业。",
    },
    {
        "title": "数字化工程师",
        "must_skills": ["Java", "Spring Boot", "MySQL", "SQL"],
        "preferred_skills": ["Spring Cloud", "MyBatis", "Redis", "PostgreSQL", "Elasticsearch",
                             "Python", "MES", "PLM", "LIMS", "Power BI",
                             "网络安全", "等级保护", "信创适配", "微服务", "数据治理", "系统设计"],
        "major_required": ["计算机", "电子信息", "网络安全", "软件工程", "信息与通信工程"],
        "note": "面向硕士。公告专业需求：计算机、电子信息、网络安全、软件工程、"
                "信息与通信工程相关专业。",
    },
]


def _backup() -> str:
    os.makedirs(BACKUP_DIR, exist_ok=True)
    base = os.path.join(BACKUP_DIR, f"workbench-before-jobswap-{datetime.now():%Y%m%d-%H%M%S}")
    dst = base + ".db"
    n = 1
    while os.path.exists(dst):
        n += 1
        dst = f"{base}-{n}.db"
    shutil.copy2(DB_PATH, dst)
    return dst


def _warn_terms(jd: dict) -> list[str]:
    """体检：把"该在专业栏却填进技能栏"和"目录里没有的专业写法"挑出来。"""
    out = []
    for t in (jd.get("must") or {}).get("skills_required") or []:
        if not nz.canonical_of(t):
            out.append(f"必需技能「{t}」不在技能本体中（候选人命中率会是 0）")
    for t in (jd.get("must") or {}).get("major_required") or []:
        if not mj.resolve(t):
            out.append(f"专业需求「{t}」不在通用学科目录中（专业维度会标「未识别」）")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="按 2026 招聘公告写入 / 更新 4 个岗位")
    ap.add_argument("--dry-run", action="store_true", help="只打印会改什么，不落盘")
    ap.add_argument("--operator", default="cli")
    args = ap.parse_args()

    conn = db.connect(DB_PATH)
    try:
        # ---------- 0. 数据体检（先于任何写入）----------
        print("=== 岗位 JD 体检 ===")
        problems = 0
        for j in JOBS:
            jd = {"must": {"skills_required": j["must_skills"],
                           "major_required": j["major_required"]}}
            bad = _warn_terms(jd)
            marks = []
            for t in j["major_required"]:
                hit = mj.resolve(t)
                marks.append(f"{t}→{(hit or {}).get('canonical', '未识别')}")
            print(f"  · {j['title']}：专业需求 " + "、".join(marks))
            for b in bad:
                print(f"      ⚠ {b}")
                problems += 1
        print(f"  体检结论：{'全部可识别' if not problems else str(problems) + ' 项需注意'}")

        # ---------- 1. 历史遗留岗位清掉（引用置空，投递不删）----------
        rows = conn.execute("SELECT id, title FROM jobs").fetchall()
        by_title = {r["title"]: r["id"] for r in rows}
        retire_ids = [by_title[t] for t in RETIRE if t in by_title]
        print(f"\n=== 历史遗留岗位清理 ===\n  待清理 {len(retire_ids)} 个："
              f"{'、'.join(t for t in RETIRE if t in by_title) or '（无）'}")
        for jid in retire_ids:
            for col in ("job_id", "suggested_job_id"):
                n = conn.execute(
                    f"SELECT COUNT(*) AS n FROM applications WHERE {col} = ?", (jid,)).fetchone()["n"]
                if n:
                    print(f"      岗位 #{jid} 的 {col}：{n} 条投递将置为「待指定」（投递本身不删）")

        # ---------- 2. 待写入的 4 个岗位 ----------
        print("\n=== 岗位写入 ===")
        for j in JOBS:
            exists = j["title"] in by_title
            print(f"  · {j['title']}" + ("  ← 已存在，将更新其 JD" if exists else "  ← 新建")
                  + "  （不归属任何部门）")
            print(f"      必须技能：{'、'.join(j['must_skills'])}")
            print(f"      专业需求：{'、'.join(j['major_required'])}")

        # 顺便报一下库里有没有"挂着的部门"——有就说明是旧库，脚本会保留它们（不擅自删数据）
        depts = db.list_departments(conn, include_inactive=True)
        if depts:
            print(f"\n[i] 库里还有 {len(depts)} 个部门（"
                  f"{'、'.join(d['name'] for d in depts)}）——脚本**不删部门**，"
                  f"岗位也不再引用它们。要清掉请单独执行 tools/reset_demo.py。")

        if args.dry_run:
            print("\n[预演] 没有改动任何文件。确认无误后去掉 --dry-run 落地。")
            return 0

        # ---------- 3. 落地 ----------
        if os.path.exists(DB_PATH):
            bak = _backup()
            print(f"\n[√] 数据库已备份：{bak}")
        mj.load(reload=True)
        nz.load_ontology(reload=True)
        stamp = db.now()

        for jid in retire_ids:
            old = db.get_job(conn, jid) or {}
            for col in ("job_id", "suggested_job_id"):
                conn.execute(
                    f"UPDATE applications SET {col} = NULL, updated_at = ? WHERE {col} = ?",
                    (stamp, jid))
            conn.execute("DELETE FROM jobs WHERE id = ?", (jid,))
            db.add_audit(conn, "job", str(jid), "retire_delete",
                         old.get("title") or "", "已下线（引用它的投递改为「待指定」，投递未删除）",
                         args.operator, "hr")
        conn.commit()

        for j in JOBS:
            jd = {
                "role": j["title"],
                # 刻意不写 "department"：岗位不归属部门。
                # 写了空串反而会在界面上显示一个空括号，不如整个键都不存在。
                "origin": "2026 招聘公告（脚本录入）",
                "must": {"education_min": EDU, "years_min": YEARS,
                         "skills_required": list(j["must_skills"]),
                         "major_required": list(j["major_required"])},
                "preferred": {"skills": list(j["preferred_skills"])},
                "note": j["note"],
            }
            jid = db.create_job(conn, j["title"], dept_id=None, jd=jd, operator=args.operator)
            print(f"[√] 岗位就绪：{j['title']}（#{jid}）")

        print("\n=== 收尾核对 ===")
        for r in conn.execute("SELECT id, title, active FROM jobs ORDER BY id"):
            n = conn.execute("SELECT COUNT(*) AS n FROM applications WHERE job_id = ?",
                             (r["id"],)).fetchone()["n"]
            print(f"  #{r['id']} {r['title']}{'在招' if r['active'] else '停用'} · 投递 {n} 条")
        pending = conn.execute(
            "SELECT COUNT(*) AS n FROM applications WHERE job_id IS NULL").fetchone()["n"]
        print(f"  所属岗位待指定：{pending} 条")
        print("\n[i] 下一步：界面上对每个岗位点一次「重新分析」可给出建议岗位；"
              "`cli.py report` 看人才库全貌。")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
