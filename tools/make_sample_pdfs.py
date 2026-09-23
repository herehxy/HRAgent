#!/usr/bin/env python3
"""生成 PDF 简历样例，放进来源文件夹用于演示「导入 → 预览 → 下载」全链路。

**为什么用 Chrome 打印而不是直接写 PDF：**

PyMuPDF 内置的字体（如 `china-s`）只带字形子集，写进去的中文能看、
但 `get_text()` 取不回正确 Unicode（实测提取出的是乱码），
而本系统整条链路（解析 → 抽字段 → 分级）**恰恰依赖文本可提取**。
用无头 Chrome 把 HTML 打成 PDF，字体与 Unicode 映射由 Chrome 保证正确，
解析链路实测能完整取回姓名、电话、邮箱、技能。

**样例设计**（覆盖三种典型判定结果，便于验收分级逻辑）：

=================  ==========================================  ==========================
文件                画像                                         预期判定
=================  ==========================================  ==========================
005_林一诺         硕士 / 4年 / 钛合金·真空熔铸·材料成型 全中        强匹配（必备三条全命中）
006_周子墨        本科 / 3年 / Python·SQL·数据治理               弱匹配（必备技能未命中）
007_苏沐白         本科 / 应届 / 材料成型·金相分析                 年限不足（风险项）
=================  ==========================================  ==========================

用法::

    python tools/make_sample_pdfs.py            # 生成到 data/resumes/
    python tools/make_sample_pdfs.py --out DIR  # 指定输出目录
    python tools/make_sample_pdfs.py --verify   # 只校验已有样例的文本可提取性
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_OUT = os.path.join(BASE, "data", "resumes")

CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    shutil.which("google-chrome") or "",
    shutil.which("chromium") or "",
)

CSS = """
@page { size: A4; margin: 16mm 15mm; }
body { font-family: "Songti SC", "STSong", "PingFang SC", "Hiragino Sans GB", serif;
       font-size: 10.5pt; line-height: 1.65; color: #111; margin: 0; }
h1 { font-size: 17pt; margin: 0 0 2pt; letter-spacing: 2pt; }
.contact { font-size: 9.5pt; color: #333; margin-bottom: 3pt; }
h2 { font-size: 11pt; margin: 13pt 0 4pt; padding-bottom: 2pt;
     border-bottom: 0.8pt solid #666; letter-spacing: 1pt; }
.row { display: flex; justify-content: space-between; margin-top: 5pt; }
.row .rt { color: #444; font-size: 9.5pt; }
ul { margin: 3pt 0 0; padding-left: 15pt; }
li { margin-bottom: 1.5pt; }
p { margin: 3pt 0; }
"""

RESUMES = [
    {
        "file": "005_林一诺_工艺工程师.pdf",
        "name": "林一诺",
        "gender": "女",
        "birth": "1997",
        "phone": "139****6543",
        "email": "linyinuo@example.com",
        "city": "陕西西安",
        "edu": [
            ("2019.09-2022.06", "西北工业大学", "材料加工工程", "硕士"),
            ("2015.09-2019.06", "西安建筑科技大学", "材料成型及控制工程", "本科"),
        ],
        "work": [("2022.07-至今", "某稀有金属材料研究院", "工艺工程师（4年工作经验）")],
        "duties": [
            "负责钛合金真空自耗电弧熔炼（VAR）与真空熔铸工艺开发，主导熔炼参数正交试验，"
            "铸锭成分偏析合格率由 88% 提升至 96%；",
            "参与难熔合金（钨、钼、铌）材料成型与热加工工艺研究，完成锻造与热处理工艺窗口标定；",
            "独立完成金相分析与力学性能测试，使用 ANSYS 进行熔炼热场仿真；",
            "参与 GJB9001C 质量体系审核与计量器具管理工作。",
        ],
        "skills": "钛合金、真空熔铸、材料成型、难熔合金、热加工、锻造、热处理、金相分析、ANSYS、力学性能测试、粉末冶金",
        "certs": "中级职称（材料工程）、计量员证、GJB9001C 内审员",
    },
    {
        "file": "006_周子墨_数智化工程师.pdf",
        "name": "周子墨",
        "gender": "男",
        "birth": "1998",
        "phone": "137****2210",
        "email": "zhouzimo@example.com",
        "city": "陕西西安",
        "edu": [
            ("2017.09-2021.06", "西安电子科技大学", "计算机科学与技术", "本科"),
        ],
        "work": [("2021.07-至今", "某工业软件公司", "数据开发工程师（3年工作经验）")],
        "duties": [
            "负责制造企业数据中台建设，基于 Python 与 SQL 完成多源异构数据采集与清洗；",
            "搭建生产质量数据看板（BI），支撑研发与生产运营的指标口径统一；",
            "参与数据治理规范编写，完成主数据标准化与数据质量稽核规则落地；",
            "熟悉 Docker 部署与 Git 协作流程，了解 MES / PLM 系统集成。",
        ],
        "skills": "Python、SQL、数据治理、数据中台、BI、ETL、Docker、Git、MES、PLM、数据质量、指标体系",
        "certs": "软件设计师（中级）、CDA 数据分析师",
    },
    {
        "file": "007_苏沐白_材料成型.pdf",
        "name": "苏沐白",
        "gender": "男",
        "birth": "2003",
        "phone": "135****8876",
        "email": "sumubai@example.com",
        "city": "陕西西安",
        "edu": [
            ("2021.09-2025.06", "西安理工大学", "材料成型及控制工程", "本科"),
        ],
        "work": [
            ("2024.07-2024.09", "某钛业股份有限公司", "工艺实习生（暑期实习）"),
            ("2025.07-至今", "某金属材料有限公司", "工艺助理（应届入职）"),
        ],
        "duties": [
            "实习期间协助钛合金板材轧制工艺跟线，记录轧制温度与压下量参数；",
            "参与金相试样制备与组织观察，完成硬度与拉伸试样送检；",
            "协助整理工艺文件与检验记录，参与车间 5S 与计量台账维护。",
        ],
        "skills": "材料成型、金属材料、金相分析、拉伸试验、硬度测试、AutoCAD、SolidWorks",
        "certs": "计算机二级、英语四级",
    },
    {
        "file": "008_沈亦风_Java后端.pdf",
        "name": "沈亦风",
        "gender": "男",
        "birth": "1997",
        "phone": "139****4412",
        "email": "shenyifeng@example.com",
        "city": "四川成都",
        "edu": [
            ("2016.09-2020.06", "电子科技大学", "软件工程", "本科"),
        ],
        "work": [("2020.07-至今", "某互联网科技公司", "Java 后端工程师（4年工作经验）")],
        "duties": [
            "负责订单与支付域后端服务开发，基于 Java 21 与 Spring Boot 3，日均处理订单 200 万笔；",
            "设计并落地分布式事务一致性方案（Outbox 模式 + 幂等消费），消息投递成功率达到 99.99%；",
            "主导 MySQL 分库分表与慢查询治理，核心接口 P99 延迟由 800ms 降至 120ms；",
            "搭建 CI/CD 流水线与灰度发布流程，参与告警与可观测性体系建设。",
        ],
        "skills": "Java、Spring Boot、MySQL、Redis、RocketMQ、分布式事务、微服务、Docker、Kubernetes、CI/CD",
        "certs": "Oracle OCP、软件设计师（中级）",
    },
    {
        "file": "009_陆知行_算法工程师.pdf",
        "name": "陆知行",
        "gender": "女",
        "birth": "1999",
        "phone": "138****7703",
        "email": "luzhixing@example.com",
        "city": "湖北武汉",
        "edu": [
            ("2021.09-2024.06", "华中科技大学", "计算机技术", "硕士"),
        ],
        "work": [("2024.07-至今", "某人工智能公司", "算法工程师（1年工作经验）")],
        "duties": [
            "负责工业质检场景的目标检测模型训练与部署，基于 PyTorch 与 YOLO 系列；",
            "将缺陷检测模型部署到产线边缘设备，误检率下降 35%；",
            "搭建数据标注与版本管理流程，维护 10 万级工业图像数据集；",
            "跟进大模型在知识问答场景的落地，完成 RAG 检索链路原型验证。",
        ],
        "skills": "Python、PyTorch、目标检测、模型部署、RAG、大模型、OpenCV、C++",
        "certs": "英语六级",
    },
]


def _html(r: dict) -> str:
    edu = "".join(
        f'<div class="row"><span>{a}　<b>{b}</b>　{c}　{d}</span></div>' for a, b, c, d in r["edu"]
    )
    work = "".join(
        f'<div class="row"><span>{a}　<b>{b}</b>　{c}</span></div>' for a, b, c in r["work"]
    )
    duties = "".join(f"<li>{d}</li>" for d in r["duties"])
    return f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<title>{r['name']}-个人简历</title><style>{CSS}</style></head><body>
<h1>{r['name']}</h1>
<div class="contact">性别：{r['gender']}　出生年份：{r['birth']}　现居：{r['city']}</div>
<div class="contact">电话：{r['phone']}　邮箱：{r['email']}</div>

<h2>教育背景</h2>{edu}

<h2>工作与实习经历</h2>{work}
<ul>{duties}</ul>

<h2>专业技能</h2>
<p>{r['skills']}</p>

<h2>证书与荣誉</h2>
<p>{r['certs']}</p>
</body></html>"""


def _chrome() -> str:
    for c in CHROME_CANDIDATES:
        if c and os.path.exists(c):
            return c
    raise SystemExit(
        "未找到 Chrome / Chromium。\n"
        "本脚本依赖无头浏览器把 HTML 打成 PDF（保证中文可被提取）。\n"
        "改动方案：安装 Google Chrome，或用 --verify 只校验已有样例。"
    )


def _print_pdf(chrome: str, html_path: str, pdf_path: str) -> None:
    cmd = [
        chrome, "--headless=new", "--no-sandbox", "--disable-gpu",
        "--no-proxy-server", "--no-pdf-header-footer",
        f"--print-to-pdf={pdf_path}", f"file://{html_path}",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    if not os.path.isfile(pdf_path) or os.path.getsize(pdf_path) < 800:
        raise SystemExit(
            f"Chrome 未能生成 PDF（exit={proc.returncode}）\n"
            f"stdout: {proc.stdout[-400:]}\nstderr: {proc.stderr[-400:]}"
        )


def verify(path: str) -> tuple[bool, str]:
    """校验一份 PDF 的文本可提取性——这是整条链路的前提。"""
    try:
        import pymupdf
    except Exception:
        return False, "未安装 pymupdf"
    try:
        doc = pymupdf.open(path)
        try:
            text = "".join(p.get_text() for p in doc)
            pages = doc.page_count
        finally:
            doc.close()
    except Exception as exc:
        return False, f"打不开：{type(exc).__name__} {exc}"
    ok = len(text.strip()) >= 60
    return ok, f"{pages} 页 / 提取 {len(text.strip())} 字"


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 PDF 简历样例")
    ap.add_argument("--out", default=DEFAULT_OUT, help="输出目录（默认 data/resumes）")
    ap.add_argument("--verify", action="store_true", help="只校验输出目录里已有 PDF 的可提取性")
    args = ap.parse_args()
    out = os.path.abspath(args.out)
    os.makedirs(out, exist_ok=True)

    if args.verify:
        files = sorted(f for f in os.listdir(out) if f.lower().endswith(".pdf"))
        if not files:
            print(f"{out} 下没有 PDF 样例")
            return 1
        bad = 0
        for f in files:
            ok, msg = verify(os.path.join(out, f))
            print(f"{'OK  ' if ok else '坏  '} {f}　{msg}")
            bad += 0 if ok else 1
        return 1 if bad else 0

    chrome = _chrome()
    tmpdir = tempfile.mkdtemp(prefix="sample_pdf_")
    print(f"浏览器：{chrome}\n输出目录：{out}\n")
    failed = 0
    for r in RESUMES:
        html_path = os.path.join(tmpdir, r["file"].replace(".pdf", ".html"))
        pdf_path = os.path.join(out, r["file"])
        with open(html_path, "w", encoding="utf-8") as fh:
            fh.write(_html(r))
        _print_pdf(chrome, html_path, pdf_path)
        ok, msg = verify(pdf_path)
        size_kb = os.path.getsize(pdf_path) / 1024
        tag = "OK  " if ok else "坏  "
        print(f"{tag} {r['file']}　{size_kb:.0f} KB　{msg}　姓名「{r['name']}」")
        failed += 0 if ok else 1
    shutil.rmtree(tmpdir, ignore_errors=True)
    if failed:
        print(f"\n{failed} 份样例文本提取异常，导入后会落成『待人工判读』，请检查。")
        return 1
    print(f"\n{len(RESUMES)} 份样例已生成且文本可提取；"
          f"在界面「导入与来源」点『导入这个文件夹』即可入库。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
