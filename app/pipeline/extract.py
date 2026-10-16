"""抽取层：把简历文本变成结构化字段。管道中唯一调用模型的节点。

三道保护，缺一不可：

1. **合规屏蔽**（`sanitize`）：送出前剔除民族/婚姻/生育/宗教/健康/身份证/住址等敏感属性，
   模型实际拿到的文本里这些取值已被替换为占位符；模型返回结果里的敏感键也会被丢弃。
2. **双通道**：`llm`（OpenAI 兼容）失败或不可用时静默回退 `heuristic`，
   保证流水线不中断——任何简历都不会因抽取失败而消失。
3. **证据强制**（`normalize`）：每条技能都要有简历原文片段；
   模型声称的片段会被逐字核对，对不上则 `verified=0`，**不计入岗位命中**。

输出契约（供 tier / ingest 消费）::

    {
      "name", "education", "years", "school", "major", "current_org",
      "certificates", "contact": {"phone","email"}, "gender",
      "cet",                                 # 英语四六级：简历原文表述（见 `_cet_from_llm`）
      "skills": [canonical, ...],            # 仅含 verified=1
      "skill_detail": [{canonical, category, evidence, verified, level}],
      "unverified_skills": [...],            # 未通过证据校验的，留待人工核对
      "unverified_fields": [...],            # 模型给了但原文核实不了的字段（含 cet）
      "extract_mode", "confidence", "sensitive_found": {类别: 次数}
    }

英语四六级（`cet`）的取值口径（v1.27.1）：**模型判定优先，正则兜底**。
模型给档位 + 原文证据，两者能对上且证据在原文里逐字存在才采信；
对不上或模型没给，退回规则抽取，并在 `unverified_fields` 里留痕。
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime

from .. import net
from . import normalize as nz
from . import sanitize

EDU_RANK = {"大专": 1, "专科": 1, "本科": 2, "学士": 2, "研究生": 3, "硕士": 3, "博士": 4}

# 证书词表（与技能本体分开：证书属于"资格"而非"能力"）
CERT_LEXICON = [
    "中级职称", "高级职称", "正高级职称", "助理工程师", "高级工程师",
    "计量员证", "计量师", "注册安全工程师", "注册质量工程师",
    "英语六级", "英语四级", "计算机二级", "计算机三级",
    "六西格玛绿带", "六西格玛黑带", "内审员", "PMP",
    "特种设备作业证", "焊接工程师", "无损检测资格证", "保密资格证",
]

_YEARS_PATTERNS = [
    r"(\d{1,2})\s*年(?:以上)?(?:的)?(?:相关)?(?:工作)?经验",
    r"工作年限[:：]?\s*(\d{1,2})",
    r"从业\s*(\d{1,2})\s*年",
    r"(\d{1,2})\s*年(?:以上的)?(?:研发|工艺|行业|相关)经验",
    r"(\d{1,2})\+?\s*年(?:以上)?(?:工作)?经历",
]

# ---- 工作年限：必须把「教育背景」整段排除在外 ----
#
# 简历里教育经历与工作经历的年份区间长得**一模一样**（都是 `2021.09-2025.06`），
# 不区分段落就会把「本科四年」也累加进工作年限。后果不是少算而是**多算**：
# 年限是岗位的硬性门槛，虚高会让明显不够年限的人误过门槛——对招聘来说，
# 假通过比漏判更危险。因此先定位教育段落，再把落在其中的年份区间剔除。
_EDU_HEADS = ("教育背景", "教育经历", "学习经历", "学历背景", "求学经历",
              "教育与培训", "教育及培训")
_OTHER_HEADS = ("工作经历", "工作经验", "工作与实习经历", "工作及实习经历",
                "实习经历", "实践经历", "工作履历", "职业经历", "职业发展",
                "项目经历", "科研经历", "专业技能", "技能特长", "技能清单",
                "证书", "荣誉", "获奖", "自我评价", "个人评价", "论文", "专利",
                "联系方式", "基本信息", "兴趣爱好", "语言能力", "培训经历")

_DATE_RANGE = re.compile(
    r"(\d{4})\s*[.\-/年]?\s*\d{0,2}\s*[-–—~至到]\s*(\d{4}|至今|现在|今)")


def _edu_span(text: str) -> tuple[int, int] | None:
    """定位「教育背景」所在段落的字符区间（从该标题起，到下一个标题为止）。

    找不到教育标题时返回 None——此时不做排除，保持旧行为，宁可多算也不错杀。
    """
    start = -1
    for head in _EDU_HEADS:
        i = text.find(head)
        if i != -1 and (start == -1 or i < start):
            start = i
    if start == -1:
        return None
    stop = len(text)
    for head in _OTHER_HEADS:
        i = text.find(head, start + len(head) if head in _EDU_HEADS else start + 1)
        if i != -1:
            stop = min(stop, i)
    if stop <= start:
        stop = len(text)
    return start, stop


def _sanity_years(text: str, years) -> tuple[int | None, str]:
    """工作年限口径。返回（可用值, 说明）。

    为什么需要：模型会把**出生年份当工作年限**。实测：校招简历写着
    「出生年份：1999」「2026年6月毕业」，模型算出 years=27（2026-1999）——
    一个"27 年经验的应届硕士"。

    **应届生口径（HR 明确要求）**：毕业时间在**当年或次年**的都算应届生，
    工作年限按 **0 年**记，不是 1 年也不是 2 年 —— 应届生的实习/项目
    不构成"工作年限"，记成 1-2 年会让分级与统计失真。
    简历自称「校招 / 应届 / 应届生」同样按应届处理。

    当前是**校招场景**：凡校招简历一律按应届归 0，不推断工作年限
    （判档本就不看年限，tier.py 不引用 years_exp；界面也不展示）。
    故 `years` 参数仅保留签名、不再参与计算，避免误读成"还会做上限质检"。
    """
    cur = datetime.now().year
    fresh_grad = False
    # ① 毕业时间在当年或次年 → 应届
    g = re.search(r"毕业时间[:：]?\s*(\d{4})|(\d{4})\s*年\s*\d{0,2}\s*月?\s*毕业", text)
    if g:
        gy = int(g.group(1) or g.group(2))
        if gy >= cur - 1:                     # 当年或次年毕业
            fresh_grad = True
    # ② 自称校招 / 应届
    if re.search(r"校招|应届", text):
        fresh_grad = True
    if fresh_grad:
        # 应届生一律记 0 年。简历若明写了工作经验（如"1 年研发"）也仍按应届处理——
        # HR 的口径是"毕业当年/次年 = 应届"，不按简历自己写的年数。
        return 0, "应届生（毕业当年或次年 / 自称校招应届），工作年限按 0 年记"
    # 校招口径：**一律按应届处理，不推断工作年限**（HR 明确要求）。
    # 判档本来就不看年限（tier.py 不引用 years_exp），这里归 0 不影响档位；
    # 界面上也不展示年限（见 ui.expBadge / CSV 表头）。
    # 因此 `years` 参数（模型抽取的年限值）在当前口径下不再参与质检——
    # 无论模型算出多少，都按应届归 0，此分支之后没有可执行的代码。
    return 0, "校招口径：不推断工作年限，统一按应届（0 年）处理"


def _find_years(text: str) -> int | None:
    """工作年限（整数年）。显式表述优先，否则按**工作经历的年份跨度**推算。"""
    for pat in _YEARS_PATTERNS:
        m = re.search(pat, text)
        if m:
            return int(m.group(1))

    edu = _edu_span(text)
    current = datetime.now().year
    spans: list[tuple[int, int]] = []
    for m in _DATE_RANGE.finditer(text):
        if edu and edu[0] <= m.start() < edu[1]:
            continue                      # 落在教育段落里，不算工作年限
        start = int(m.group(1))
        end = current if m.group(2) in ("至今", "现在", "今") else int(m.group(2))
        if 1990 <= start <= end <= current + 1:
            spans.append((start, end))
    if not spans:
        return None
    # 取整体跨度而非各段相加：相邻/重叠的履历相加会重复计数
    return max(end for _s, end in spans) - min(start for start, _e in spans)


def _find_name(text: str) -> str | None:
    """从正文抽姓名：显式「姓名：」标签 > 首行。

    首行兜底必须**排除段落标题**：不少 PDF 的姓名在图片/页眉里，解析出来的
    第一行是「教育经历」这类标题——曾经被当成姓名建档，整个档案变成"教育经历"。
    """
    m = re.search(r"姓名\s*[:：]\s*([\u4e00-\u9fa5·]{2,4})", text)
    if m:
        return m.group(1)
    first = (text.strip().split("\n") or [""])[0].strip()
    first = re.sub(r"^(个人简历|简历|应聘简历)[\s:：]*", "", first)
    if re.fullmatch(r"[\u4e00-\u9fa5·]{2,4}", first) and first not in SECTION_HEADS:
        return first
    return None


# 简历的段落标题词表：它们长得像人名（2-4 个汉字），但永远不是人名。
# 与 _EDU_HEADS/_OTHER_HEADS（年限排除用）合并并补充若干常见变体。
SECTION_HEADS = set(_EDU_HEADS) | set(_OTHER_HEADS) | {
    "求职意向", "个人信息", "个人资料", "基本情况", "教育", "工作", "实习",
    "技能", "擅长", "校园经历", "社会经历", "学术成果", "获奖情况", "荣誉奖项",
    "自我介绍", "个人优势", "代表作品", "研究方向", "业务方向", "专业方向",
    "主讲课程", "承担项目", "发表论文", "授权专利", "联系方式",
}


def _fix_name_from_filename(out: dict, filename: str | None) -> None:
    """姓名缺失（或误取了段落标题）时按文件名兜底，并如实标注来源供 HR 核对。"""
    nm = out.get("name")
    if not nm or nm in SECTION_HEADS:
        guess = name_from_filename(filename)
        if guess:
            out["name"] = guess
            out["name_source"] = "文件名"


def _name_is_email_local(name: str | None, contact: dict | None) -> bool:
    """姓名其实是从邮箱地址里抠出来的（实测：`zhangsan` ← zhangsan@example.com）。

    为什么没被挡住：简历里本来就写了那个邮箱，**"原文里能找到这个字符串"**
    所以证据校验反而放行了它。证据校验只管"有没有出处"，
    不管"这个值合不合理"——这是两件事，得分开判。
    """
    if not name or not contact:
        return False
    em = str(contact.get("email") or "").strip().lower()
    if "@" not in em:
        return False
    local = em.split("@")[0].strip().lower()
    if not local:
        return False
    n = str(name).strip().lower()
    variants = {local, local.replace(".", ""), local.replace("_", ""), local.replace(".", "_")}
    return n in variants


# 文件名兜底要排除的"不是姓名"的词：在线/工具/模板 这类取出来会是「在线工具」这种
_NOT_A_NAME_WORDS = ("在线", "工具", "模板", "表格", "下载", "生成", "网站", "官网",
                     "制作", "免费", "简易", "通用", "示例", "样本", "demo", "test")


def name_from_filename(filename: str | None) -> str | None:
    """从文件名猜姓名（正文识别不出时的兜底）。

    绝大多数简历文件以姓名开头：`张三-简历.pdf`、`张三_数字IC工程师_硕士.pdf`、
    `李四.pdf`。取扩展名前的第一段，剥掉"简历/resume/CV"字样、数字与括号后，
    剩下恰好是 2-4 个汉字才采纳——**猜不出就返回 None，绝不硬凑**。
    """
    if not filename:
        return None
    stem = os.path.splitext(os.path.basename(filename))[0]
    stem = re.sub(r"(个人|求职)??简历|resume|cv", "", stem, flags=re.IGNORECASE)
    # 先剥掉常见的"工具名尾巴"：`在线简历工具-CodeCV简历` 剥完是 `在线工具-CodeCV`
    for w in _NOT_A_NAME_WORDS:
        stem = stem.replace(w, "")
    stem = re.split(r"[-_—\s·（）()【】\[\],，、.]", stem, maxsplit=1)[0]
    stem = re.sub(r"[\d（）()【】\[\]]+", "", stem).strip()
    if re.fullmatch(r"[\u4e00-\u9fa5·]{2,4}", stem) and stem not in SECTION_HEADS:
        return stem
    return None


def _find_education(text: str) -> str | None:
    best: tuple[str, int] | None = None
    for kw, rank in EDU_RANK.items():
        if kw in text and (best is None or rank > best[1]):
            best = (kw, rank)
    return best[0] if best else None


# ---- 性别（校招简历常有；**只做本地规则抽取，不送模型、不采纳模型结果**）----
#
# 为什么坚持规则通道：性别属于《就业促进法》第 27 条、《妇女权益保障法》第 43 条
# 明确禁止在招聘中设限的属性，让模型"顺便读一下"既无必要也不可控。这里只认
# 简历上**明写的**标签行（如「性别：女」），不做任何推测；抽不到就是 None。
# 另有一道结构性约束：性别**绝不进入 `tier.grade()`**，不参与评分、分级与排序。
_GENDER_LABEL = re.compile(
    r"(?:性别|性\s*别|sex|gender)\s*(?:要求|填写|备注)?\s*[:：]?\s*"
    r"(男|女|男性|女性|male|female)",
    re.IGNORECASE)


def _find_gender(text: str) -> str | None:
    """从简历原文抽性别标签，抽不到返回 None（不做任何推断）。"""
    m = _GENDER_LABEL.search(text or "")
    if not m:
        return None
    v = m.group(1)
    if v in ("男", "男性"):
        return "男"
    if v in ("女", "女性"):
        return "女"
    low = v.lower()
    if low == "male":
        return "男"
    if low == "female":
        return "女"
    return None


#: 院校名（中文写法）。抽成常量供 `_find_school` 与 `_find_first_degree_school` 共用 ——
#: 两处各写一份的话，改一处（比如加上「分校」「校区」）另一处就悄悄漏了。
_SCHOOL_RE = re.compile(r"([\u4e00-\u9fa5]{2,15}(?:大学|学院|研究院|研究所|职业技术学院))")

#: 「这是本科段」的关键词。**「学士」也算** —— 简历里常写「XX大学 工学学士」。
_BACHELOR_KW = ("本科", "学士")

#: 显式标签写法（最强信号，"第一学历"就是 HR 想要的那个东西）
_FIRST_DEGREE_LABEL_RE = re.compile(
    r"(?:第一学历|本科院校|本科学校|本科毕业于|本科就读于|本科学校为)\s*[:：]?\s*"
    r"([\u4e00-\u9fa5]{2,15}(?:大学|学院|研究院|研究所|职业技术学院))")


def _find_school(text: str) -> str | None:
    m = _SCHOOL_RE.search(text)
    return m.group(1) if m else None


#: v1.29.2：学历层次白名单（与 `EDU_RANK` 的键一致，模型给别的词就丢那一项）
DEGREE_WORDS = ("大专", "本科", "硕士", "博士")


def _same_school(name: str, seen: set) -> bool:
    """这个校名是不是 `seen` 里某所学校的**同一个**（含"同校不同院系"）。

    为什么不能只比全名：简历常写「北京航空航天大学」又写
    「北京航空航天大学仪器学院」（在职读研时），两句话是同一所学校。
    只比全名会把它列成两段，HR 看到重复的学校会以为数据有问题 ——
    而这份表是要给用人单位看的。

    判据：**一方是另一方的前缀**（保留短的那个，它才是院校名单里认的形式，
    长的那个查层次查不到：`uni_tier('北航仪器学院')` 是 None，
    `uni_tier('北京航空航天大学')` 才是 985）。
    """
    n = str(name or "").strip()
    if n in seen:
        return True
    for prev in seen:
        if n.startswith(prev) or prev.startswith(n):
            return True
    return False


def _edu_history_heuristic(text: str) -> list[dict]:
    """规则版学历院校经历：逐行找「院校 + 层次词」，按出现顺序。

    刻意保守：**同一行必须既有院校名又有层次词**才算一段（只写校名不写层次的行
    无法判断是不是本科段 —— 与 `_find_first_degree_school` 同一个纪律）。
    """
    out: list[dict] = []
    seen: set[str] = set()
    for line in (text or "").split("\n"):
        schools = [m.group(1) for m in _SCHOOL_RE.finditer(line)]
        if not schools:
            continue
        deg = next((d for d in ("博士", "硕士", "本科", "大专") if d in line), None)
        if not deg:
            continue
        sch = schools[0]
        if _same_school(sch, seen):
            continue
        seen.add(sch)
        mjr = None
        mm = re.search(r"(?:专业|所学专业)\s*[:：]?\s*([\u4e00-\u9fa5A-Za-z]{2,20})", line)
        if mm:
            mjr = mm.group(1)
        out.append({"degree": deg, "school": sch, "major": mjr or ""})
    return out


def _edu_history_from_llm(raw, text: str) -> list[dict]:
    """模型给的 `education_history` → 可入库的列表（每段都必须在原文里）。

    三道闸门，缺一不可：
      ① **结构**：`{degree, school, major}`，缺 `degree`/`school` 的整项丢掉；
         `degree` 必须在 `DEGREE_RANGES` 的白名单里（模型自造"研究生"这类词不算）。
      ② **校名在原文里**（与基本信息同一道关）：原文没有的院校直接丢。
         宁可少一段，也不要一个编出来的学校 —— HR 会拿它做取舍。
      ③ **去重**：同一所学校重复出现只留一条（模型偶尔会把同一段输出两遍）。

    返回空列表 = 抽不到（界面显示「教育经历未识别」，**不编**）。
    """
    src = str(text or "")
    out: list[dict] = []
    seen: set[str] = set()
    for item in (raw if isinstance(raw, list) else []):
        if not isinstance(item, dict):
            continue
        deg = str(item.get("degree") or "").strip()
        sch = str(item.get("school") or "").strip()
        if not deg or not sch or deg not in DEGREE_WORDS:
            continue
        if sch not in src:                    # ② 编出来的院校
            continue
        if _same_school(sch, seen):            # ③ 重复段（含"同校不同院系"）
            continue
        seen.add(sch)
        out.append({"degree": deg, "school": sch,
                    "major": str(item.get("major") or "").strip()[:40]})
    return out


def _find_first_degree_school(text: str) -> str | None:
    """从教育经历里取**本科那一段**的院校（取不到返回 None）。

    ⚠️ 为什么必须单独抽，不能拿 `school` 顶替：
    `school` 是**最高学历**院校。校招简历常见写法是——

        2019.09-2023.06  XX学院   材料成型及控制工程  本科
        2023.09-2026.06  YY大学   材料科学与工程      硕士

    这时 `school` = YY大学（985），但第一学历是双非。**拿 school 当第一学历，
    会把"本科双非、只有硕士是 985"的人筛进「本科985」名单** ——
    而 HR 是拿这个名单做取舍的，筛错人比筛不到人严重得多。

    识别口径（**只认"同一段里既有院校名又有本科/学士"**）：
      ① 显式标签优先：「第一学历：XX大学」「本科院校：XX大学」；
      ② 否则按行找：该行含「本科」或「学士」，且含院校名 → 取离「本科/学士」**最近**的院校。
         为什么要"最近"：简历常把本硕写在同一行
         （「2019-2023 XX大学 本科；2023-2026 YY大学 硕士」），
         简单取第一个学校在这类写法下会取错。
      ③ 一行只写校名不写层次 → **不认**（无法判断那是不是本科段）。

    取不到就返回 None → 界面显示「第一学历院校未识别（可补录）」、
    筛选里落**独立的「未识别」档**。绝不能退回 `school`：
    那等于用一个已知不可靠的值冒充事实，而 HR 看不出来。
    """
    src = text or ""
    if not src.strip():
        return None
    # ① 显式标签
    m = _FIRST_DEGREE_LABEL_RE.search(src)
    if m:
        return m.group(1)
    # ② 逐行找「本科/学士」段
    for line in src.split("\n"):
        kws = [line.find(k) for k in _BACHELOR_KW if k in line]
        if not kws:
            continue
        schools = [(m2.start(), m2.group(1)) for m2 in _SCHOOL_RE.finditer(line)]
        if not schools:
            continue
        # 取离任一「本科/学士」关键词最近的院校
        best = min(schools, key=lambda s: min(abs(s[0] - k) for k in kws))
        if best[1]:
            return best[1]
    return None


def _find_major(text: str) -> str | None:
    m = re.search(r"(?:专业|所学专业)[:：]\s*([\u4e00-\u9fa5A-Za-z]{2,20})", text)
    if m:
        return m.group(1)
    m = re.search(
        r"\d{4}[.\-/]\d{2}\s*[-–~至到]\s*\d{4}[.\-/]\d{2}\s+\S+\s+(\S+)\s+(?:硕士|本科|博士|学士)",
        text)
    return m.group(1) if m else None


def _find_org(text: str) -> str | None:
    m = re.search(r"(?:现单位|目前就职于|工作单位|所在单位|就职于)[:：]?\s*([^\n，,。;；]{2,30})", text)
    if m:
        return m.group(1).strip()
    m = re.search(r"([\u4e00-\u9fa5]{2,20}(?:有限)?(?:公司|集团|研究院|研究所|厂))", text)
    return m.group(1) if m else None


def _find_certs(text: str) -> list[str]:
    return [c for c in CERT_LEXICON if c in text]


# ---------------------------------------------------------------- 英语四六级（v1.27）
#
# 为什么单独抽一个字段而不是靠 `certificates` 里那两条「英语四级/英语六级」：
#  ① 筛选要能比 —— certificates 是**数组**，界面上只能当标签展示，不能当筛选条件；
#  ② 表述千变万化 ——「大学英语六级」「CET-6」「英语6级」「通过全国英语等级考试六级」
#     「英语专业八级」「雅思 6.5」，而 certificates 走的是 `CERT_LEXICON` 的字面包含，
#     只能认出「英语四级」「英语六级」这两种写法，其余全漏。
#
# 归一后的档位（存 `candidates.cet` 的**原文**，归一在读取时做，见 `cet_level`）：
#     "六级" / "四级" / "专业八级" / "专业四级" / "考研英语" / "无" / ""
# 取"最高"的一档：同一个人既写了四级又写了六级（六级通常是后考的），存六级。

#: 归一后的可比等级。数值越大 = 水平越高，用于"取最高档"与界面排序。
CET_RANK: dict[str, int] = {
    "无": 0, "四级": 1, "专业四级": 2, "考研英语": 3, "六级": 4, "专业八级": 5,
}
#: 人才库筛选下拉用（顺序 = 从高到低，与 `CET_RANK` 同源，别在两处各写一份）
CET_FILTER_LEVELS = ["六级", "四级", "专业八级", "专业四级", "考研英语"]
#: 「未通过/未取得/没有」这类表述 → 归一后的 '无'（与"没写"的 '' 严格区分）
#: ⚠️ 「无四六级」必须单列：它是简历里很常见的写法（「无四六级证书」），
#: 但 `[四六]\s*级` 那条命中正则会从「无**四六**级」里抠出「六级」，
#: 而这组负判里原本只有"未通过/未取得"——于是写了"无四六级"的人被判成六级。
#: 这是写 v1.27.1 的 `_cet_from_llm` 用例时才撞出来的（规则通道的兜底路径）。
_CET_NEG_RE = re.compile(
    r"未通过|未取得|没有.{0,3}英语|无(?:英语)?等级"
    r"|无\s*四\s*[、,，和及]?\s*六\s*级|无\s*四六级|无\s*六级|无\s*四级")

#: 候选表述的正则。**顺序无关**（取最高档靠 `CET_RANK`），
#: 但每条都要能覆盖一种真实写法 —— 这组是靠 `cet_level` 的 15 条用例逐条试出来的：
#:   漏「专八」「专业四级」「考研英语」时，"非985/211强势院校+四六级"这种
#:   HR 常问的组合就会漏人；而漏「考研英语」是因为它不带"级"字，
#:   只匹配 `[1-8]\s*[级]?` 那几条永远抓不到。
_CET_HIT_PATS = (
    r"TEM\s*-?\s*8",                       # TEM-8
    r"TEM\s*-?\s*4",                       # TEM-4
    r"英语专业[^，,。;；\n]{0,6}",          # 英语专业八级
    # 「专业四级」「专八」「专4」—— 专业/简称、数字与汉字四种组合都要
    # （`cet_level` 同理，漏任何一种都会归一成空 → 界面上是空标签、筛选永远 0 人）
    r"专\s*业\s*[4四8八]\s*[级]?",
    r"专\s*[8八]\s*[级]?",
    r"专\s*[4四]\s*[级]?",
    r"考研[^，,。;；\n]{0,3}英语|英语[^，,。;；\n]{0,3}考研",   # 考研英语
    r"(?:大学|全国)?英语\s*(?:等级考试)?\s*[1-8一二三四五六七八]\s*[级]?",
    r"CET\s*-?\s*[1-8]\b",
    r"(?:英语|英文)\s*[1-8一二三四五六七八]\s*[级]?",
    # v1.27：裸的「四级」「六级」「4级」「6级」，**但必须带英语上下文**。
    # 为什么必须有：「通过大学英语四级**和六级**」里第二个"六级"前面是"和"、
    # 不是"英语"，上面几条都抓不到 → 取最高档时只剩"四级"，存成四级，
    # HR 筛"六级"就漏掉这个实际过了六级的人。
    # 为什么必须带上下文：裸 `[1-8]` 会把简历里**所有**数字都命中 ——
    # 出生年份、工作年限、专利号、电话，全被判成"六级/四级"。这就是为什么
    # 这里写成「英语/四六级/通过…和…」这类**锚定**形式，而不是一个宽泛的数字。
    r"(?:英语|四六级|级英语)[^，,。;；\n]{0,6}?[1-8一二三四五六七八]\s*[级]?",
    # v1.27：裸的「四级」「六级」（**必须带"级"字**）。
    # 为什么安全：「四级」「六级」带"级"在简历里没有第二种含义，
    # 而「四节」「六项」之类的假阳性被"级"字挡掉了。
    # ⚠️ 绝不放行不带"级"的裸「四/六」或阿拉伯数字 —— 那样会把
    # 「周四面试」「3年经验」之类全部误判，而**数字版本**更致命：
    # 出生年份 1994、工作年限 6、专利号、电话都会被当成英语等级。
    r"[四六]\s*级",
    # 带上下文的**阿拉伯数字**：CET-6 / 6级 / 考试6 —— 必须有"级"或"考试"收尾，
    # 否则「6人团队」「6年经验」全是误报。
    r"(?:CET\s*-?\s*[48]|英语\s*[48]\s*[级]|考试\s*[48]\s*[级]|[48]\s*级)",
    r"[1-8一二三四五六七八]\s*[级]\s*(?:和|及|、|,|，|/|＋|\+)?\s*[1-8一二三四五六七八]\s*[级]",
)


def _find_cet(text: str) -> str | None:
    """从简历原文抽英语等级，返回**原文表述**（抽不到返回 None）。

    原文表述存库、归一在读取时做（`cet_level`）：库里保持"简历上写了什么"，
    HR 打开档案看到的与原件一致；筛选与展示要的那一档由 `cet_level()` 算。

    v1.27.1 定位：**兜底通道**。模型可用时四六级以模型判定为准
    （见 `_cet_from_llm`），这里只在两种情况下被调用：
      ① 模型不可用 / 抽取失败（规则通道、`mode="failed"`）；
      ② 模型给了档位但没通过原文证据自证，退回这里取。
    仍然保留它而不是删掉：模型挂了的时候字段不能整个消失，
    而"抽不到"的代价（界面显示 —、HR 可手填）远小于"给个错值"。
    拿不准就返回 None —— 界面显示"—"，HR 可以手填，不会拿到错值。
    """
    src = text or ""
    if not src.strip():
        return None
    # 收集候选表述：每命中一个正则就把那一段原文存起来（存原文是为了可追溯）
    hits: list[str] = []
    for pat in _CET_HIT_PATS:
        for m in re.finditer(pat, src, re.I):
            got = m.group(0).strip()
            hits.append(got)
            # 「四级和六级」这类并列：整串归一只会认出靠前的"四级"，
            # 必须把并列的**每一项单独**也作为候选塞进去，取最高档才对。
            # 用 `[1-8一二三四五六七八]级` 在**命中串内部**切，不看整篇原文，
            # 所以不会把工作年限「3年」之类误当等级。
            for one in re.findall(r"[1-8一二三四五六七八]\s*[级]", got):
                hits.append(one)
    # v1.27：**「未通过」优先于任何正等级，且必须在这里、归一之前判**。
    # 两个坑叠在一起过：
    # ① 这里原来写成"先算 best、只在没命中时才看未通过" → 「英语六级 未通过」
    #    被当成六级；
    # ② `cet_level` 原来又把负判放在最后 → 即使这里返回了带"未通过"的原文，
    #    归一时仍会先命中"六级"分支。
    # 现在两边都是"负判优先"，口径才真正一致。
    # 误判代价不对称：漏认只是少一个人，误判会把没通过的人放进面谈名单。
    _neg = _CET_NEG_RE.search(src)
    if _neg:
        # 存**命中的那一段原文**（不是硬编码"未通过"）——简历写的是「无四六级证书」
        # 就存「无四六级」，HR 打开档案看到的才与原件一致。
        return _neg.group(0)
    if hits:
        # 同一次抽取里可能同时命中「四级」与「六级」，取**等级最高**的那段原文 ——
        # 与 `cet_level` 的"最高档优先"口径一致，否则库里存了四级、
        # 归一后却是六级，两边对不上（同一个值两套口径 = 迟早分叉）。
        best = max(hits, key=lambda h: (CET_RANK.get(cet_level(h), 0), len(h)))
        if CET_RANK.get(cet_level(best), 0) > 0:
            return best
    return None


def cet_level(raw: str | None) -> str:
    """英文/等级表述 → 归一档位。

    返回值取值范围：`'专业八级' / '专业四级' / '考研英语' / '六级' / '四级' / '无' / ''`
    - `''`  = 简历里没写（**不是**"没有证书"）——筛选时算"未提供"，
              必须与 `'无'`（简历明写"未通过"）分开，两者对 HR 的含义完全相反；
    - `'无'` = 简历明确写了没通过。

    **最高档优先**：`专业八级 > 六级 > 考研英语 > 专业四级 > 四级`。
    一个人既写了四级又写了六级（六级通常是后考的），展示与筛选都该按六级 ——
    否则 HR 筛"六级"会漏掉这个人，而对 HR 而言他就是六级。

    读取侧唯一口径：`server` 的筛选与 `ui` 的展示都调它，不在别处重写归一规则。
    """
    s = (raw or "").strip()
    if not s:
        return ""
    # v1.27：「未通过」必须**最先判**。
    # 原来放在最后，于是「英语六级 未通过」先命中下面的"六级"分支直接返回
    # —— 归一成六级，HR 筛"六级"会把一个没通过的人筛进来。这比漏一个严重得多：
    # 漏只是少一个人，误判是把不该要的人排进了面谈名单。
    if _CET_NEG_RE.search(s):
        return "无"
    # 往下按"最高档优先"扫，一旦命中即返回 —— 顺序即优先级。
    # ⚠️ 每一档都必须同时认**阿拉伯数字与汉字**（「专八」/「专8」、「四级」/「四」）。
    # 原来只写 `[8]` 时「专八」匹配不上、`cet_level` 返回 "" → 界面上是空标签、
    # 筛选"专业八级"永远 0 人，而数据其实是抽到了的。
    if re.search(r"专业\s*八|专\s*八|专\s*8|tem\s*-?\s*8", s, re.I):
        return "专业八级"
    if re.search(r"六级|六\s*级|cet\s*-?\s*6|(?:^|[^\d])6(?:[^\d]|$)", s, re.I):
        return "六级"
    if re.search(r"考研.{0,4}英语|英语.{0,4}考研", s):
        return "考研英语"
    if re.search(r"专业\s*四|专\s*四|专\s*4|tem\s*-?\s*4", s, re.I):
        return "专业四级"
    if re.search(r"四级|四\s*级|cet\s*-?\s*4|(?:^|[^\d])4(?:[^\d]|$)", s, re.I):
        return "四级"
    return ""


#: **不是**四六级的英语考试（雅思/托福/托业/剑桥商务/全国英语等级考试 PETS）。
#: 为什么必须显式排除：提示词里已经写了"别往档位里套"，但弱模型不听；
#: 而 `cet_level` 的裸数字分支 `(?:^|[^\d])6(?:[^\d]|$)` 会把「雅思 6.5」
#: 里的 6 直接判成"六级" —— 光靠"模型自证"拦不住（它引的原文自己也会被判成六级）。
#: 这是一条**排除**规则，不是抽取规则：它不会让谁被判成六级，只会挡掉误套。
#: ⚠️ 只列英文/缩写与雅思托福这类专名，**不要**把"全国英语等级考试"的中文名
#: 写进来 —— 简历里「通过全国英语等级考试六级」实际就是指六级，排掉会漏人。
_CET_NOT_ENGLISH_RE = re.compile(
    r"雅思|ielts|托福|toefl|托业|toeic|bec\b|剑桥商务|商务英语|pets", re.I)


def _cet_from_llm(level, evidence, text: str) -> tuple[str | None, bool]:
    """四六级**以模型判定为准**，正则（`_find_cet`）只当兜底。

    返回 `(存库的原文表述, 模型值是否被拒)`。

    v1.27.1：由「正则优先、模型值一律不采信」改为「模型优先」。
    为什么改：四六级是**硬筛选条件**，而简历上的写法规则穷举不完 ——
    「CET6 已过」「英语六级 520 分」「考研英语一 78」「TEM-8（良好）」
    「通过全国英语等级考试六级」……每遇到一种就得补一条正则，
    补错了还会误判（裸数字会把出生年份、工作年限、专利号全当成等级）。
    语义理解正是模型的强项，所以判定交给模型。

    但模型值**不能裸用** —— 与 `verify_profile_against_text` 同款纪律，
    必须同时满足三条，缺一即退回规则值：
      1. 档位在白名单里（`CET_RANK` 的键，挡住模型自造的档位）；
      2. 给了 `cet_evidence` 证据片段；
      3. 证据能在简历**原文**里逐字找到（证明它不是凭空编的）。
    再加一条**模型自证**：它给的档位必须能由它自己引的原文片段推出
    （`cet_level(证据) == 档位`），对不上就当它没给。

    ⚠️ 这里的 `cet_level()` 只做**一致性校验**，不是抽取手段 ——
    在整篇简历里「找」英语等级这件事由模型完成，它不在原文里扫数字。

    存库的是**证据原文**而不是档位字符串：与"库里保持简历上写了什么"一致，
    HR 打开档案能看到原件表述；筛选/展示要的档位由 `cet_level()` 归一。
    第二个返回值用于**不留静默降级**：模型给了却没过校验时要告诉 HR，
    而不是悄悄换成规则值（本项目的反幻觉纪律）。
    """
    lv = str(level or "").strip()
    ev = str(evidence or "").strip()
    gave = bool(lv)
    if lv in CET_RANK and ev and not _CET_NOT_ENGLISH_RE.search(ev) and cet_level(ev) == lv:
        # 证据校验两档：先认**逐字相同**，再认**排版差异**（空格/全角/换行不同，
        # 与 `verify_profile_against_text` 用 `_norm_for_search` 同款口径）。
        # 只做逐字会让模型因一个空格就被判"编造"→ 频繁退回规则值，
        # 等于把模型优先改回正则优先，所以这里必须有第二档。
        src = str(text or "")
        if ev in src or _norm_for_search(ev) in _norm_for_search(src):
            return ev, False
    return _find_cet(text), gave


_PHONE_LABEL = r"(?:手机(?:号码)?|电话|联系方式|Tel|TEL|Phone|Mobile)"
# 允许 138****1234（求职者常自行脱敏）、138 1234 5678、138-1234-5678、(029)8888xxxx
_PHONE_TOKEN = r"1[3-9](?:[\d*\-‑\s]{6,14})\d"
_LANDLINE_TOKEN = r"0\d{2,3}[\-\s]?\d{7,8}"


def _find_contact(text: str) -> dict:
    """抽取联系方式。

    要点：简历上的手机号经常是**求职者自己脱敏过的**（如 `138****1234`），
    严格按 11 位纯数字匹配会漏掉，进而导致身份键退化到邮箱、联系方式为空。
    因此先在有"电话/手机"标签的行里取号，再去掉分隔符校验位数。
    """
    phone = None
    labeled = re.search(_PHONE_LABEL + r"[:：]?\s*(" + _PHONE_TOKEN + r")", text)
    if labeled:
        phone = re.sub(r"[\s\-‑]+", "", labeled.group(1))
    if not phone:
        m = re.search(_PHONE_TOKEN, text)
        if m:
            phone = re.sub(r"[\s\-‑]+", "", m.group(0))
    if not phone:
        m = re.search(_LANDLINE_TOKEN, text)
        if m:
            phone = m.group(0).strip()
    email = re.search(r"[\w.+-]+@[\w-]+\.[\w.]+", text)
    return {
        "phone": phone,
        "email": email.group(0) if email else None,
    }


def _jd_terms(jd: dict) -> list[str]:
    """岗位 JD 里写明的技能词（必需 + 加分）。

    为什么要让 JD 的词表参与技能识别：本体（`config/ontology.json`）是按材料领域
    建的，遇到软件岗位的 `Java`、`Spring Boot` 根本认不出来——简历抽取结果里
    只有 `SQL`，于是"软件岗候选人"对软件岗 JD 命中 0 项、被当成不适合（实测踩到）。
    把 JD 里的词也当作待识别词表，等于**让每把尺子自带词表**：
    以后招财务、法务、IT 运维，只要在 JD 里写清楚技能，命中判定就成立，
    不需要每次都去扩本体。命中同样要过"原文必须有证据"这一关（反幻觉口径不变）。
    """
    must = ((jd or {}).get("must") or {}).get("skills_required") or []
    pref = ((jd or {}).get("preferred") or {}).get("skills") or []
    out: list[str] = []
    for s in list(must) + list(pref):
        t = str(s or "").strip()
        if t and t not in out:
            out.append(t)
    return out


def extract_heuristic(text: str, jd: dict) -> dict:
    """纯规则通道：不需要任何模型，字段 + 本体词表扫描 + **岗位 JD 词表**。"""
    # `normalize_skills` 末尾会自动补上本体词表在原文中的扫描结果，
    # 因此这里只需把 JD 词表喂进去，两份词表的结果最终合并去重（按 canonical）。
    detail = nz.normalize_skills(_jd_terms(jd), text, source="jd")
    return _assemble(
        text,
        {
            "name": _find_name(text),
            "education": _find_education(text),
            "years": _find_years(text),
            "school": _find_school(text),
            # v1.29：第一学历（本科）院校。与 school 是两件事，抽不到留空（筛选落「未识别」）
            "first_degree_school": _find_first_degree_school(text),
            # v1.29.2：完整学历院校经历（规则通道兜底，模型不可用时至少给得出）
            "education_history": _edu_history_heuristic(text),
            "major": _find_major(text),
            "current_org": _find_org(text),
            "certificates": _find_certs(text),
            "contact": _find_contact(text),
            "gender": _find_gender(text),
            # v1.27.1：四六级原文表述。**规则通道 = 兜底**（模型可用时以模型判定为准，
            # 见 `_cet_from_llm`）；HR 拿它做硬筛选，不能只靠 certificates。
            "cet": _find_cet(text),
        },
        detail,
        mode="heuristic",
    )


_LLM_SYSTEM = (
    "你是简历结构化抽取器。只依据给定简历原文抽取字段，**严禁编造**。\n"
    "严格输出 JSON，不要解释、不要代码块标记。字段：\n"
    "name, education(大专/本科/硕士/博士), years(整数), school, major, current_org,\n"
    # v1.29：第一学历（本科）院校。必须与 school 分开给 —— 简历里学校常不止一所，
    # 而「本科985/211」这个筛选要的是**本科那所**，不是最高学历那所。
    "first_degree_school(第一学历/本科就读的院校；简历没写本科是哪所就**不要输出**),\n"
    "first_degree_evidence(first_degree_school 对应的**原文原样片段**，逐字照抄),\n"
    # v1.29.2（业务方要求）：学历院校经历要**完整**，不能只给最高那一所 ——
    # HR 看学校水平是从本科一路看到博士的，中间的研究生段丢了就没法判断。
    "education_history(数组，**教育经历里写了几段就给几段**，按时间先后；每项 {degree:大专/本科/硕士/博士, school:院校全名, major:专业}；只写了一段就给一项，简历没写学历经历则输出空数组),\n"
    "skills(数组，每项 {name:技能名, evidence:该技能在原文中出现的**原样片段**}),\n"
    "certificates(字符串数组),\n"
    # v1.27：**四六级由模型判定**（用户 2026-10-15 要求：不要正则匹配）。
    # 语义理解是模型的强项：「通过全国英语等级考试（PETS）五级」「英语六级 520 分」
    # 「CET6 已过」「英语专业八级」「考研英语一 78」这类写法规则穷举不完。
    # 但它是**硬筛选条件**（HR 会拿它筛人），所以模型给的值必须**过原文校验**
    # （`verify_profile_against_text` 的同款纪律：证据不在原文里就退回规则值）。
    # 模型要给两样东西：档位（归一后的）＋ 原文片段（可追溯、可被校验）。
    "cet_level(英语等级档位，从下面这些里**选一个**：\"六级\" / \"四级\" / "
    "\"专业八级\" / \"专业四级\" / \"考研英语\" / \"无\"；"
    "写\"无\"只用于简历**明确说没通过/未取得**的情况；"
    "简历里压根没提英语等级就**不要输出这个字段**),\n"
    "cet_evidence(cet_level 对应的**原文原样片段**，逐字照抄，不得改写拼接),\n"
    "honors(数组，每项 {category:奖学金/荣誉/论文/专利, name:简短名称, "
    "evidence:该条在原文中出现的**原样片段**}),\n"
    "contact{phone,email},\n"
    "confidence(0-1，抽取可信度)。\n"
    "field_evidence(对象)：基本信息每个字段对应的**原文原样片段**，"
    "键为 name/education/school/major/years/current_org/first_degree_school，"
    "例如 {\"school\":\"西安电子科技大学\",\"major\":\"计算机科学与技术\"}；"
    "原文里没有的字段不要给证据，也不要给值。\n"
    "纪律：\n"
    "1) 只抽与岗位相关的技术/工艺/表征/软件/管理技能，通用软技能不要；\n"
    "2) evidence 必须是原文中逐字存在的片段，不得改写、不得拼接；\n"
    "3) 原文未提及的信息一律留空或不输出，不要猜测；\n"
    "   3.1) honors 要抽全：奖学金/奖项（含国家、校级、专项）、荣誉称号、论文（含发表/收录/篇数/期刊）、专利（含专利号、实用新型、软著），以及连续多年获奖、团体奖这类表述；\n"
    "   3.2) cet_level 只认**英语等级**：一个人同时写了四级和六级，给**更高的那档**（六级）；"
    "「雅思 6.5 / 托福 90 / PETS 五级 / 商务英语」**不属于**上面的档位，不要往里套——"
    "那就输出这个字段会害 HR 筛错人。\n"
    "4) 禁止抽取民族、婚姻、生育、宗教、健康、身份证号、住址、政治面貌、身高体重、照片等敏感信息。"
)



def _norm_for_search(v: str) -> str:
    """比对用的规范化：去掉所有空白与常见分隔符，手机号/邮箱的排版差异不该算"找不到"。"""
    return re.sub(r"[\s\-()（）·,，.。:：]", "", str(v or "")).lower()


def verify_profile_against_text(fields: dict, field_evidence: dict,
                                text: str) -> tuple[dict, list[str]]:
    """**逐个字段验证据**：值必须能在原文里找到，否则丢掉并退回规则值。

    为什么必须有这一步：模型抽的是"看起来对"的值，一旦它把
    `13800001111` 写成 `138-0000-1111` 还好（规范化后能对上），
    但把年份写成 2015（原文 2015.09）也对不上——
    没有校验就等于允许模型悄悄改写事实，档案里出现的东西原文没有，
    HR 无从发现（这正是本项目反幻觉原则要防的）。

    返回 `(通过校验的字段, 未通过的字段名列表)`。
    """
    t_raw = str(text or "")
    t_norm = _norm_for_search(t_raw)
    ok: dict = {}
    bad: list[str] = []
    # v1.29：`first_degree_school` 同样要过"值必须在原文里"这道关 ——
    # 它和 school 一样是可直接改变筛选结果的事实，不能凭模型一句话就写进库。
    for k in ("name", "education", "school", "major", "current_org", "first_degree_school"):
        v = fields.get(k)
        if v in (None, "", "—"):
            continue
        ev = str((field_evidence or {}).get(k) or "")
        # 证据片段本身必须在原文里（证明模型不是凭空写的），且值也要在原文里
        ev_ok = bool(ev) and _norm_for_search(ev) in t_norm
        val_ok = _norm_for_search(v) in t_norm
        if ev_ok and val_ok:
            ok[k] = v
        else:
            bad.append(k)
    # 年限：只要求是原文里出现过的整数（"3 年经验"里的 3）
    yrs = fields.get("years")
    if isinstance(yrs, int) and yrs > 0:
        ok["years"] = yrs
    elif yrs is not None:
        bad.append("years")
    # 联系方式：必须与原文里的号码/地址逐位一致（去掉分隔符后比对）
    ct = fields.get("contact") or {}
    for kind in ("phone", "email"):
        val = str(ct.get(kind) or "").strip()
        if not val:
            continue
        if _norm_for_search(val) and _norm_for_search(val) in t_norm:
            ok.setdefault("contact", {})[kind] = val
        else:
            bad.append(kind)
    return ok, bad



def verify_honors_against_text(items, text: str) -> list[dict]:
    """**逐条验原文**：证据片段必须逐字出现在简历里，否则丢掉。

    和 `verify_profile_against_text` 同一个道理：模型说"他获过国家奖学金"很容易，
    但简历里根本没写就是**编的**，档案里出现 HR 无从核对的东西比没有更糟。
    顺带做规范化比对（去掉空白/标点差异），避免因为排版空格被判假。
    """
    t_norm = _norm_for_search(text or "")
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for it in items or []:
        if not isinstance(it, dict):
            continue
        cat = (it.get("category") or "").strip()
        name = (it.get("name") or "").strip()
        ev = (it.get("evidence") or "").strip()
        if cat not in ("奖学金", "荣誉", "论文", "专利") or not name or not ev:
            continue
        if _norm_for_search(ev) not in t_norm:
            continue                     # 证据不在原文里 → 丢弃（不猜、不放行）
        key = (cat, _norm_for_search(ev)[:40])
        if key in seen:
            continue
        seen.add(key)
        out.append({"kind": cat, "name": name, "evidence": ev,
                    "from": "模型（已核对原文）"})
    return out


def merge_honors(llm_items, rule_items) -> list[dict]:
    """合并模型抽取与规则抽取：模型优先（同一条不重复），规则兜底。

    为什么两路都要：模型抽得全但可能漏掉格式很规整的（规则擅长）；
    规则稳但认不出自由表述。两路合并后覆盖面最大，且都有原文兜底。
    """
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for src in (llm_items or [], rule_items or []):
        for it in src or []:
            ev = (it.get("evidence") or "").strip()
            key = (it.get("kind") or "", _norm_for_search(ev)[:40])
            if not ev or key in seen:
                continue
            seen.add(key)
            out.append(it)
    # 排序：奖学金 → 荣誉 → 论文 → 专利，同类保持原顺序（原文出现顺序）
    order = {"奖学金": 0, "荣誉": 1, "论文": 2, "专利": 3}
    out.sort(key=lambda x: order.get(x.get("kind"), 9))
    return out


def extract_llm(text: str, jd: dict, model: str, base_url: str, api_key: str) -> dict:
    # 简历原文不设上限会撑爆上下文窗口：附件上限 20MB，解析出的文本远超模型
    # 可接收长度 → 超时或非 JSON → 被 except 静默降级为规则通道（用户只看到
    # "抽取方式变成 heuristic"，不知发生了啥）。截断到 8000 字符足够抽取字段，
    # 与 analyze._cand_brief 的 3000 上限同思路（抽取需要更多原文，故放宽）。
    _text = (text or "")[:8000]
    payload = {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": _LLM_SYSTEM},
            {"role": "user", "content": f"【岗位】{jd.get('role', '')}\n【简历原文】\n{_text}"},
        ],
    }
    # v1.23.2：**必须走同一套 provider 适配**。
    # 原来这里是独立拼 payload，没经过 llm._apply_provider_quirks，
    # 于是 qwen3 缺 enable_thinking=false → 400 → 被 except 静默降级到规则通道：
    # 界面上看起来"分析过了"，实际模型一次都没调成功。
    try:
        from ..agent.llm import _apply_provider_quirks
        payload = _apply_provider_quirks(payload, {"model": model})
    except Exception:                                         # noqa: BLE001
        if str(model).startswith("qwen3"):
            payload["enable_thinking"] = False
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"},
        method="POST",
    )
    with net.urlopen(req, timeout=90) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    content = body["choices"][0]["message"]["content"].strip()
    content = re.sub(r"^```(?:json)?|```$", "", content).strip()
    return json.loads(content)


def _assemble(text: str, fields: dict, detail: list[dict], mode: str) -> dict:
    # v1.24.1：**姓名不能是邮箱账号的本地部分**。放在这里是因为两条抽取通道
    # （规则 / 模型）都会经过它，一处拦截就够——放在通道里各写一遍必然漏。
    if _name_is_email_local(fields.get("name"), fields.get("contact")):
        fields = dict(fields)
        fields["name"] = None
        fields["name_rejected"] = "邮箱地址的本地部分不是姓名"
    # v1.24.5：**校招口径的工作年限一律归 0**。
    # 为什么必须放在这里：之前只加在**模型分支**里，规则通道（模型不可用时走的
    # 那条）仍按 `_find_years` 推断——实测演示数据 10 人全是 `years_exp=1`。
    # 两条通道都经过 `_assemble`，放这里才是一处生效。
    # 判档不看年限（tier.py 不引用 years_exp），所以归 0 不影响档位。
    if fields.get("years") is not None:
        fields = dict(fields)
        fields["years"] = 0
        fields["years_note"] = "校招口径：不推断工作年限，统一按应届（0 年）处理"
    verified = [d for d in detail if d.get("verified")]
    unverified = [d for d in detail if not d.get("verified")]
    found = sum(1 for k in ("name", "education", "years") if fields.get(k))
    if verified:
        found += 1
    return {
        **fields,
        "skills": [d["canonical"] for d in verified],
        "skill_detail": detail,
        "unverified_skills": [d["canonical"] for d in unverified],
        "extract_mode": mode,
        "confidence": round(found / 4, 2),
    }



# ============================================================================
# 荣誉 / 论文 / 专利（v1.17）
# ============================================================================
# 只认**原文里逐字出现**的说法：正则的好处之一就是天然满足"每个结论都能指回原文"。

_HONOR_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("奖学金", (
        "国家奖学金", "政府奖学金", "校一等奖学金", "校二等奖学金", "校三等奖学金",
        "一等奖学金", "二等奖学金", "三等奖学金", "特等奖学金", "学业奖学金",
        "优秀学生奖学金", "助学奖学金", "格林奖学金", "国家励志奖学金", "单项奖学金",
    )),
    ("荣誉", (
        "优秀毕业生", "优秀学生干部", "三好学生", "优秀学生", "优秀团员", "优秀共产党员",
        "先进个人", "优秀员工", "优秀论文奖", "优秀导师", "优秀课程", "标兵",
        "优秀青年", "杰出青年", "先进个人称号",
    )),
    ("论文", (
        "期刊论文", "学术论文", "会议论文", "SCI", "EI", "CSSCI", "核心期刊",
        "发表论文", "论文发表", "CNKI", "万方", " ResearchGate",
    )),
    ("专利", (
        "发明专利", "实用新型", "外观设计", "专利号", "专利申请", "软件著作权", "软著",
    )),
)

# 「获/授予/荣获」前缀：用来确认上下文是"拿到了"而不是"申请了"
_AWARD_VERB = ("获", "授予", "荣获", "获得", "被评为", "当选")

# 专利号：ZL + 年份 + 序号（CN/US 等可带）
_PATENT_NO = re.compile(r"(?:ZL|CN|US)\s?\d{6,}[A-Z0-9.\-]{0,12}", re.I)
# 篇数：发表论文 3 篇 / 论文 2 篇
_PAPER_COUNT = re.compile(r"(?:发表)?(?:论文|文章)\s*\d{1,2}\s*篇")


def _looks_like_paper_context(line: str) -> bool:
    return any(k in line for k in ("论文", "期刊", "会议", "发表", "SCI", "EI", "CNKI"))


def extract_honors(text: str) -> list[dict]:
    """抽取奖学金 / 荣誉 / 论文 / 专利。每条带原文片段（evidence）。

    规则：
    - 命中关键词**且**所在行有"获/授予/授予"等动词，或该行就是一行荣誉/论文罗列；
    - 专利另外接受"专利号 ZL2024…"这种强特征（不要求动词）；
    - 同一条同一行只出一次，跨行去重；
    - **不推断**：原文没写就一条都没有（缺=未识别，不是"没有"）。
    """
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for raw in (text or "").split("\n"):
        line = raw.strip().strip("·•|\t ")
        if not line or len(line) > 200:
            continue
        has_verb = any(v in line for v in _AWARD_VERB)
        # ① 专利（强特征优先：专利号不需要动词）
        for _m in _PATENT_NO.finditer(line):
            _no = _m.group(0)
            key = ("专利", _no)
            if key not in seen:
                seen.add(key)
                out.append({"kind": "专利", "name": _no, "evidence": line})
        if "专利" in line:
            key = ("专利", line[:40])
            if key not in seen and (has_verb or "申请" in line or "授权" in line):
                seen.add(key)
                out.append({"kind": "专利", "name": line[:60], "evidence": line})
        # ② 奖学金 / 荣誉
        for kind, kws in _HONOR_RULES[:2]:
            for kw in kws:
                if kw in line and (has_verb or len(line) <= 24):
                    key = (kind, kw)
                    if key not in seen:
                        seen.add(key)
                        out.append({"kind": kind, "name": kw, "evidence": line})
                    break
        # ③ 论文
        m = _PAPER_COUNT.search(line)
        if m or any(k in line for k in ("SCI", "EI", "期刊论文", "会议论文", "学术论文",
                                        "核心期刊", "CNKI", "发表论文")):
            if has_verb or _looks_like_paper_context(line):
                name = (m.group(0) if m else "") or "、".join(
                    k for k in ("SCI", "EI", "核心期刊", "期刊论文", "会议论文") if k in line) or "论文"
                key = ("论文", name)
                if key not in seen:
                    seen.add(key)
                    out.append({"kind": "论文", "name": name, "evidence": line})
    return out


def extract(text: str, jd: dict, use_llm: bool = False, llm_conf: dict | None = None,
            filename: str | None = None) -> dict:
    """统一入口。**绝不抛错**——任何失败都退化为规则通道，简历不会因抽取失败而丢失。

    `filename`：来源文件名。姓名在图片/页眉里的 PDF 解析不出正文姓名，
    此时按文件名兜底（大多数简历文件以姓名开头），并在 `name_source` 里如实标注。
    """
    safe_text, sensitive_found = sanitize.scrub(text)

    if use_llm and llm_conf and llm_conf.get("api_key"):
        try:
            raw = extract_llm(safe_text, jd,
                              model=llm_conf.get("model", "deepseek-chat"),
                              base_url=llm_conf.get("base_url", "https://api.deepseek.com/v1"),
                              api_key=llm_conf["api_key"])
            clean, removed = sanitize.strip_forbidden(raw)
            # v1.27.1：四六级**由模型判定**（带原文证据自证），规则只兜底。
            # 必须在 `_llm_fields` 之前算 —— 下面两处都要用同一个值，
            # 各算一遍必然分叉（本项目在别处已经为这个栽过）。
            cet_val, cet_rejected = _cet_from_llm(
                clean.get("cet_level"), clean.get("cet_evidence"), text)

            raw_skills: list[str] = []
            claims: dict[str, str] = {}
            for item in (clean.get("skills") or []):
                if isinstance(item, dict):
                    nm = str(item.get("name") or "").strip()
                    if nm:
                        raw_skills.append(nm)
                        if item.get("evidence"):
                            claims[nm] = str(item["evidence"])
                elif isinstance(item, str):
                    raw_skills.append(item)

            detail = nz.normalize_skills(raw_skills, safe_text, llm_evidence=claims)
            # v1.13.7：基本信息先过**证据校验**再采信——模型说姓名/学校/专业，
            # 前提是它们真的写在简历里（原文没有的一律丢掉，退回规则值）。
            _llm_fields = {
                "name": clean.get("name"),
                "education": clean.get("education") if clean.get("education") in EDU_RANK else None,
                "years": int(clean["years"]) if str(clean.get("years", "")).strip().isdigit() else None,
                "school": clean.get("school"),
                # v1.29：第一学历院校。与 school 分开抽、分开验 —— 简历里常有两所学校，
                # 合在一起就会把"本科双非、硕士985"的人算成"本科985"。
                "first_degree_school": clean.get("first_degree_school"),
                # v1.29.2：完整学历院校经历。**逐段校验校名在原文里**（与
                # 其它基本信息同一道关）——编一个原文没有的院校比少抽一段严重。
                "education_history": _edu_history_from_llm(clean.get("education_history"), text),
                "major": clean.get("major"),
                "current_org": clean.get("current_org"),
                "certificates": [c for c in (clean.get("certificates") or []) if isinstance(c, str)],
                "contact": clean.get("contact") or {},
                # 性别**只取本地规则的结果，原文取（不是 safe_text）**：
                # 模型即便返回了 gender 也已在 strip_forbidden 里被丢掉，这里再明确覆盖一次，
                # 保证"性别永远不会来自模型"这条约束不依赖上游是否记得过滤。
                "gender": _find_gender(text),
                # v1.27.1：四六级取**模型判定 + 原文证据自证**的结果（见 `_cet_from_llm`）。
                "cet": cet_val,
            }
            # 规则通道兜底填空，保证字段不全时不至于全空
            heur = extract_heuristic(safe_text, jd)
            # 第一学历的证据单独给（模型按 first_degree_evidence 返回），
            # 合并进 field_evidence 才能走同一套"证据必须逐字在原文里"的校验。
            _ev = dict(clean.get("field_evidence") or {})
            if clean.get("first_degree_evidence"):
                _ev.setdefault("first_degree_school", str(clean["first_degree_evidence"]))
            fields, _unverified = verify_profile_against_text(_llm_fields, _ev, text)
            # 校验只负责"值可信"，证书与性别不参与那个校验，但必须补回来：
            # 性别**永远只取本地规则**（合规红线，不依赖模型自觉）。
            fields["certificates"] = _llm_fields.get("certificates") or []
            fields["gender"] = _find_gender(text)
            # v1.27.1：四六级**不进** `verify_profile_against_text` ——
            # 那条校验的规则是"值本身要在原文里找到"，而四六级存的是档位判断，
            # 靠的是模型引的证据片段（`_cet_from_llm` 已经验过），不是值本身。
            # 这里用上面算好的同一个值补回，别再调一次（两处各算一遍 = 必然分叉）。
            fields["cet"] = cet_val
            if cet_rejected:
                # 模型给了档位但没通过原文自证 → 如实告诉 HR，不静默换成规则值
                _unverified = list(_unverified or []) + ["cet"]
            for k in ("name", "education", "years", "school", "major", "current_org",
                      "first_degree_school", "education_history"):
                if not fields.get(k):
                    fields[k] = heur.get(k)
            # v1.23.6：**工作年限合理性质检**。实测踩到：校招简历里写着
            # 「出生年份：1999」+「2026年6月毕业」，模型算出 years=27
            # （2026-1999），把出生年份当成了工作年限 —— 27 年经验的应届生。
            # 规则通道没这个问题，所以只在模型值上做质检。
            fields["years"], _ys = _sanity_years(text, fields.get("years"))
            if not fields["certificates"]:
                fields["certificates"] = heur["certificates"]
            _hc = heur.get("contact") or {}
            _fc = fields.get("contact") or {}
            fields["contact"] = {"phone": _fc.get("phone") or _hc.get("phone"),
                                 "email": _fc.get("email") or _hc.get("email")}

            out = _assemble(safe_text, fields, detail, mode="llm+rule")
            # 年限被质检拦下时如实标注（不静默改数字）
            if _ys:
                out["years_rejected"] = _ys
            # 校验没过的字段如实告诉 HR：模型给了但原文里找不到，已退回规则值
            if _unverified:
                out["unverified_fields"] = _unverified
            try:
                model_conf = float(clean.get("confidence") or 0)
            except (TypeError, ValueError):
                model_conf = 0.0
            out["confidence"] = round(max(out["confidence"], min(1.0, model_conf)), 2)
            out["sensitive_found"] = sensitive_found
            out["sensitive_fields_removed"] = removed
            # v1.18：荣誉/论文/专利——**模型抽的逐条验过原文**才带出来，
            # 验不过的直接丢（不让模型编的荣誉进档案）。规则抽的那份作为兜底补上。
            out["honors"] = merge_honors(verify_honors_against_text(clean.get("honors"), safe_text),
                                        extract_honors(safe_text))
            _fix_name_from_filename(out, filename)
            return out
        except (urllib.error.URLError, KeyError, ValueError, TypeError,
                json.JSONDecodeError) as exc:
            # 静默回退是**反模式**：抽取降级了没人知道，档案里就少了字段也没人查。
            # 回退本身仍然安全（规则通道永远可用），但必须留痕（v1.13.7）。
            print(f"[extract] 模型抽取不可用，已回退规则通道：{type(exc).__name__}: {exc}",
                  file=sys.stderr)

    try:
        out = extract_heuristic(safe_text, jd)
    except Exception:
        out = _assemble(safe_text, {
            "name": None, "education": None, "years": None, "school": None, "major": None,
            "current_org": None, "certificates": [], "contact": {},
            "first_degree_school": None,
            "gender": _find_gender(text),
            "cet": _find_cet(text),
        }, [], mode="failed")
    out["sensitive_found"] = sensitive_found
    out["sensitive_fields_removed"] = []
    _fix_name_from_filename(out, filename)
    return out
