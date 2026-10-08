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
      "certificates", "contact": {"phone","email"},
      "skills": [canonical, ...],            # 仅含 verified=1
      "skill_detail": [{canonical, category, evidence, verified, level}],
      "unverified_skills": [...],            # 未通过证据校验的，留待人工核对
      "extract_mode", "confidence", "sensitive_found": {类别: 次数}
    }
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


def _find_school(text: str) -> str | None:
    m = re.search(r"([\u4e00-\u9fa5]{2,15}(?:大学|学院|研究院|研究所|职业技术学院))", text)
    return m.group(1) if m else None


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
            "major": _find_major(text),
            "current_org": _find_org(text),
            "certificates": _find_certs(text),
            "contact": _find_contact(text),
            "gender": _find_gender(text),
        },
        detail,
        mode="heuristic",
    )


_LLM_SYSTEM = (
    "你是简历结构化抽取器。只依据给定简历原文抽取字段，**严禁编造**。\n"
    "严格输出 JSON，不要解释、不要代码块标记。字段：\n"
    "name, education(大专/本科/硕士/博士), years(整数), school, major, current_org,\n"
    "skills(数组，每项 {name:技能名, evidence:该技能在原文中出现的**原样片段**}),\n"
    "certificates(字符串数组),\n"
    "contact{phone,email},\n"
    "confidence(0-1，抽取可信度)。\n"
    "field_evidence(对象)：基本信息每个字段对应的**原文原样片段**，"
    "键为 name/education/school/major/years/current_org，"
    "例如 {\"school\":\"西安电子科技大学\",\"major\":\"计算机科学与技术\"}；"
    "原文里没有的字段不要给证据，也不要给值。\n"
    "纪律：\n"
    "1) 只抽与岗位相关的技术/工艺/表征/软件/管理技能，通用软技能不要；\n"
    "2) evidence 必须是原文中逐字存在的片段，不得改写、不得拼接；\n"
    "3) 原文未提及的信息一律留空或不输出，不要猜测；\n"
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
    for k in ("name", "education", "school", "major", "current_org"):
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


def extract_llm(text: str, jd: dict, model: str, base_url: str, api_key: str) -> dict:
    payload = {
        "model": model,
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": _LLM_SYSTEM},
            {"role": "user", "content": f"【岗位】{jd.get('role', '')}\n【简历原文】\n{text}"},
        ],
    }
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
                "major": clean.get("major"),
                "current_org": clean.get("current_org"),
                "certificates": [c for c in (clean.get("certificates") or []) if isinstance(c, str)],
                "contact": clean.get("contact") or {},
                # 性别**只取本地规则的结果，原文取（不是 safe_text）**：
                # 模型即便返回了 gender 也已在 strip_forbidden 里被丢掉，这里再明确覆盖一次，
                # 保证"性别永远不会来自模型"这条约束不依赖上游是否记得过滤。
                "gender": _find_gender(text),
            }
            # 规则通道兜底填空，保证字段不全时不至于全空
            heur = extract_heuristic(safe_text, jd)
            fields, _unverified = verify_profile_against_text(
                _llm_fields, clean.get("field_evidence") or {}, text)
            # 校验只负责"值可信"，证书与性别不参与那个校验，但必须补回来：
            # 性别**永远只取本地规则**（合规红线，不依赖模型自觉）。
            fields["certificates"] = _llm_fields.get("certificates") or []
            fields["gender"] = _find_gender(text)
            for k in ("name", "education", "years", "school", "major", "current_org"):
                if not fields.get(k):
                    fields[k] = heur.get(k)
            if not fields["certificates"]:
                fields["certificates"] = heur["certificates"]
            _hc = heur.get("contact") or {}
            _fc = fields.get("contact") or {}
            fields["contact"] = {"phone": _fc.get("phone") or _hc.get("phone"),
                                 "email": _fc.get("email") or _hc.get("email")}

            out = _assemble(safe_text, fields, detail, mode="llm+rule")
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
            "gender": _find_gender(text),
        }, [], mode="failed")
    out["sensitive_found"] = sensitive_found
    out["sensitive_fields_removed"] = []
    _fix_name_from_filename(out, filename)
    return out
