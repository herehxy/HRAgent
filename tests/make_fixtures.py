#!/usr/bin/env python3
"""生成邮箱抓取的测试邮件（.eml），供端到端自检使用。

覆盖 8 种真实会遇到的情况：

======  ============================================  ==========================
编号     场景                                         期望结果
======  ============================================  ==========================
01      新简历（陈志远）                              added
02      同一份简历文件再次投递（不同邮件）             skipped_dup（文件层去重）
03      新简历（刘婉清）                              added
04      新简历（赵敏，.md 附件）                      added
05      陈志远更新版简历（同邮箱）                    merged_version（内容层去重）
06      附件是损坏的 .docx（王海涛）                  added + 待人工判读
07      邮件没有附件（咨询信）                        no_attachment
08      与 01 同一 message-id 的重复投递              skipped_dup（邮件层去重）
======  ============================================  ==========================
"""
from __future__ import annotations

import os
import sys
from email.message import EmailMessage

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

MAIL_DIR = os.path.join(BASE, "data", "mail_in")
RESUME_DIR = os.path.join(BASE, "data", "resumes")

# 夹具简历**内联在本文件里**，不再读 data/resumes——那是业务目录，
# 里面的样例文件随时会被 HR 清掉（2026-09-23 清库后自检当场崩掉，教训同 #35：
# 测试数据必须自包含，不能依赖业务目录的状态）。
CHEN_V1 = """姓名：陈志远
性别：男
出生年份：1995
电话：138****1234
邮箱：chenzhiyuan@example.com

教育背景
2015.09-2018.06  西北工业大学  材料加工工程  硕士
2011.09-2015.06  西安理工大学  材料成型及控制工程  本科

工作经历
2018.07-至今  某稀有金属材料研究院  工艺工程师（6年工作经验）
- 负责钛合金真空熔铸工艺开发，主导真空电弧熔炼（VAR）工艺参数优化；
- 参与难熔合金（钨、钼、铌）材料成型与热加工工艺研究；
- 熟悉金相分析与力学性能测试，使用 ANSYS 进行热场仿真；
- 参与 GJB9001 质量体系相关工作。

专业技能
钛合金、真空熔铸、材料成型、难熔合金、热加工、金相分析、ANSYS、力学性能

证书
中级职称、计量员证
"""

LIU_V1 = """姓名：刘婉清
性别：女
电话：139****5678
邮箱：liuwanqing@example.com

教育背景
2019.09-2023.06  西安建筑科技大学  金属材料工程  本科

工作年限：3年

工作经历
2023.07-至今  XX 金属制品有限公司  工艺工程师（3年工作经验）
- 负责钛合金热加工与锻造工艺；
- 参与材料成型工艺文件编制与现场工艺支持。

专业技能
钛合金、热加工、锻造、材料成型

证书
英语六级
"""

ZHAO_V1 = """姓名：赵敏
性别：女
电话：136****3344
邮箱：zhaomin@example.com

教育背景
2015.09-2019.06  东北大学  材料学  本科
2019.09-2022.06  东北大学  材料学  博士

工作经历
2022.07-至今  某研究院  高级工程师（4年工作经验）
- 难熔高熵合金粉末冶金与真空熔铸工艺开发；
- 材料成型与热处理工艺研究；
- 增材制造（3D打印）工艺探索与工程化应用。

专业技能
难熔合金、粉末冶金、真空熔铸、材料成型、热处理、增材制造、金相分析

证书
中级职称
"""

CHEN_V2 = """姓名：陈志远
性别：男
出生年份：1995
电话：138****1234
邮箱：chenzhiyuan@example.com

教育背景
2015.09-2018.06  西北工业大学  材料加工工程  硕士
2011.09-2015.06  西安理工大学  材料成型及控制工程  本科

工作经历
2018.07-至今  某稀有金属材料研究院  工艺工程师（7年工作经验）
- 负责钛合金真空熔铸工艺开发，主导真空电弧熔炼（VAR）工艺参数优化；
- 新增职责：主导钛合金电子束冷床熔炼（EBCHM）产线工艺定型；
- 参与难熔合金（钨、钼、铌）材料成型与热加工工艺研究；
- 熟悉金相分析、扫描电镜（SEM）与力学性能测试，使用 ANSYS 进行热场仿真；
- 参与 GJB9001 质量体系相关工作，负责工艺文件编制。

专业技能
钛合金、真空熔铸、真空电弧熔炼、电子束冷床熔炼、材料成型、难熔合金、热加工、
金相分析、扫描电镜、ANSYS、力学性能

证书
中级职称、计量员证、六西格玛绿带
"""


def build_eml(sender: str, subject: str, date: str, message_id: str,
              attachments: list[tuple[str, bytes, str]] | None = None,
              body: str = "老师您好，附件为我的应聘简历，请查收。") -> bytes:
    msg = EmailMessage()
    msg["From"] = sender
    msg["To"] = "jobs@example-institute.cn"
    msg["Subject"] = subject
    msg["Date"] = date
    msg["Message-ID"] = message_id
    msg.set_content(body)
    for filename, data, mime in (attachments or []):
        maintype, _, subtype = mime.partition("/")
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return msg.as_bytes()


def read(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


# 夹具 PDF 简历：用 pymupdf 现做（内置 china-s 中文字体，提取回读已验证不乱码）。
# 学生投递以 PDF 为主，来源文件预览/打包下载的断言都需要真 PDF。
LIN_PDF_TEXT = """姓名：林一诺
性别：女
电话：137****8899
邮箱：linyinuo@example.com

教育背景
2021.09-2025.06  西北工业大学  材料成型及控制工程  本科

实习经历
2024.07-2024.12  某钛业有限公司  工艺实习生
- 参与钛合金真空自耗熔炼（VAR）工艺跟产与数据记录；
- 协助金相制样与硬度测试。

专业技能
钛合金、真空熔炼、金相分析、Office
"""

SHEN_PDF_TEXT = """姓名：沈亦飞
性别：男
电话：135****2210
邮箱：shenyifei@example.com

教育背景
2020.09-2024.06  大连理工大学  金属材料工程  本科

工作经历
2024.07-至今  某特钢集团  质量工程师（2年工作经验）
- 负责锻件超声波探伤与力学性能检测报告编制；
- 参与质量控制体系（ISO 9001）内审。

专业技能
金属材料、探伤、力学性能、质量体系、Python
"""


def _make_pdf(path: str, text: str) -> None:
    import pymupdf

    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 72), text, fontname="china-s", fontsize=11)
    doc.save(path)
    doc.close()


def write_resumes(out_dir: str) -> list[str]:
    """把夹具简历落成真实文件（文本 3 份 + PDF 2 份），返回文件名清单。

    自包含：不再读 data/resumes——那是业务目录，样例随时会被清掉
    （2026-09-23 清库后自检当场崩掉，教训同 #35：测试数据必须自包含）。
    文本 3 份与上面的邮件附件**字节一致**（同一常量、UTF-8 落盘），
    文件夹导入 + 邮件再投的跨渠道去重断言依赖这一点。
    """
    os.makedirs(out_dir, exist_ok=True)
    for name, text in (("陈志远-简历-工艺工程师.txt", CHEN_V1),
                       ("刘婉清-简历.txt", LIU_V1),
                       ("赵敏-简历.md", ZHAO_V1)):
        with open(os.path.join(out_dir, name), "w", encoding="utf-8") as fh:
            fh.write(text)
    _make_pdf(os.path.join(out_dir, "林一诺-简历.pdf"), LIN_PDF_TEXT)
    _make_pdf(os.path.join(out_dir, "沈亦飞-简历.pdf"), SHEN_PDF_TEXT)
    return sorted(os.listdir(out_dir))


def main(out_dir: str | None = None) -> int:
    mail_dir = out_dir or MAIL_DIR
    os.makedirs(mail_dir, exist_ok=True)
    for name in os.listdir(mail_dir):
        if name.endswith(".eml"):
            os.remove(os.path.join(mail_dir, name))

    chen = CHEN_V1.encode("utf-8")
    liu = LIU_V1.encode("utf-8")
    zhao = ZHAO_V1.encode("utf-8")
    broken_docx = b"PK\x03\x04" + b"\x00" * 32 + b"this-is-not-a-valid-docx-package"

    fixtures = [
        ("01_新简历_陈志远.eml", build_eml(
            "陈志远 <chenzhiyuan@example.com>", "应聘 工艺工程师（钛合金方向）- 陈志远",
            "Mon, 15 Sep 2026 09:12:00 +0800", "<mail-001@example.com>",
            [("陈志远-简历-工艺工程师.txt", chen, "text/plain")])),

        ("02_重复文件_陈志远.eml", build_eml(
            "陈志远 <chenzhiyuan@example.com>", "补充投递：陈志远 简历（再次发送）",
            "Mon, 15 Sep 2026 15:40:00 +0800", "<mail-002@example.com>",
            [("陈志远简历.txt", chen, "text/plain")])),

        ("03_新简历_刘婉清.eml", build_eml(
            "刘婉清 <liuwanqing@example.com>", "应聘工艺工程师 - 刘婉清",
            "Tue, 16 Sep 2026 10:05:00 +0800", "<mail-003@example.com>",
            [("刘婉清-简历.txt", liu, "text/plain")])),

        ("04_新简历_赵敏.eml", build_eml(
            "赵敏 <zhaomin@example.com>", "博士应聘：难熔合金方向 - 赵敏",
            "Tue, 16 Sep 2026 14:22:00 +0800", "<mail-004@example.com>",
            [("赵敏-简历.md", zhao, "text/markdown")])),

        ("05_同人更新版_陈志远.eml", build_eml(
            "陈志远 <chenzhiyuan@example.com>", "更新简历：陈志远（新增 EBCHM 产线经验）",
            "Wed, 17 Sep 2026 08:30:00 +0800", "<mail-005@example.com>",
            [("陈志远-简历-更新版.txt", CHEN_V2.encode("utf-8"), "text/plain")])),

        ("06_损坏附件_王海涛.eml", build_eml(
            "王海涛 <wanghaitao@example.com>", "应聘简历 - 王海涛",
            "Wed, 17 Sep 2026 11:00:00 +0800", "<mail-006@example.com>",
            [("王海涛-简历.docx", broken_docx,
              "application/vnd.openxmlformats-officedocument.wordprocessingml.document")])),

        ("07_无附件咨询.eml", build_eml(
            "某同学 <student@example.com>", "咨询：贵院2027届校园招聘何时开始？",
            "Wed, 17 Sep 2026 16:45:00 +0800", "<mail-007@example.com>",
            [], body="老师您好，请问2027届校园招聘什么时候开始？谢谢！")),

        # 与 01 完全相同的 Message-ID（模拟邮件网关重复投递）
        ("08_重复邮件_陈志远.eml", build_eml(
            "陈志远 <chenzhiyuan@example.com>", "应聘 工艺工程师（钛合金方向）- 陈志远",
            "Mon, 15 Sep 2026 09:12:00 +0800", "<mail-001@example.com>",
            [("陈志远-简历-工艺工程师.txt", chen, "text/plain")])),
    ]

    for name, raw in fixtures:
        with open(os.path.join(mail_dir, name), "wb") as fh:
            fh.write(raw)

    print(f"[√] 已生成 {len(fixtures)} 封测试邮件到 {mail_dir}")
    for name, _ in fixtures:
        print("    -", name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
