"""邮件主题 / 文件名的结构化解析：`应聘方向 + 学历 + 学校 + 专业 + 姓名 + 性别`。

为什么要单独一层：这是单位招聘邮箱的**固定投递格式**，字段由投递方按我们的要求填写，
属于"人工整理过的信息"——通常比简历正文抽取更准（正文里学校可能写简称、
学历可能写成"研三在读"、姓名可能缩进页眉图片里）。所以口径是：

1. **邮件来源**：主题解析得出来的字段**优先于**正文抽取结果（并在明细里注明来源）；
2. **文件来源**：文件名常与主题同格式，同一个解析器直接复用；
3. **只认认得出来的**：学历必须在学历表内、性别必须是明写的男/女、学校必须带高校字样、
   姓名必须是 2-4 个汉字且不是方向/专业词。认不出就不给值——
   宁可退回正文抽取，也不拿一个猜出来的学校去覆盖对的。

与此前 `_find_name` 的"文件名猜姓名"是两条互补通道：这里是**结构化字段**解析，
那条是单字段兜底；两者都不硬猜。
"""
from __future__ import annotations

import re

#: 段间分隔符：+ - _ / ｜ 空格 顿号 等（投递方写法不统一，全部当分隔符）
_SEP = re.compile(r"[+\-—_/\\|｜·•,，、;；:：\s\u3000]+")

#: 主题/文件名前后的修饰：`【应聘】`、`(简历)`、`应聘-`、`简历_` 等
_DECOR = re.compile(r"[（(【\[][^）)】\]]{0,12}[）)】\]]")
_PREFIX = re.compile(r"^(应聘|求职|简历|个人简历|resume|cv|投递|申请)[:：\-—_\s]*",
                     re.IGNORECASE)

#: 学历词（含常见变体）。**只认这些**，不认"研三/大四"这类在读表述——
#: 那种要结合毕业时间判断，属于 freshness 的活，不该在这里猜。
_EDU_WORDS = {
    "大专": "大专", "专科": "大专", "高职": "大专", "大专在读": "大专",
    "本科": "本科", "学士": "本科", "大学本科": "本科",
    "硕士": "硕士", "研究生": "硕士", "硕士在读": "硕士", "硕士研究生": "硕士",
    "博士": "博士", "博士研究生": "博士", "博士后": "博士",
}

_GENDER_WORDS = {"男": "男", "男性": "男", "女": "女", "女性": "女"}

_SCHOOL_RE = re.compile(
    r"[\u4e00-\u9fa5]{2,20}(?:大学|学院|学校|研究院|研究所|职业技术学院|高等专科学校)")

#: 明显不是人名的词（方向/常见段落标题），避免把"材料工艺"当姓名
_NOT_NAME = {
    "材料", "工艺", "研发", "生产", "质量", "管理", "销售", "财务", "人力",
    "行政", "技术", "工程师", "设计", "检测", "化验", "分析", "设备", "安全",
    "环保", "采购", "物流", "信息", "数字化", "科研", "教育", "培训", "实习",
    "应届", "往届", "全职", "兼职", "校招", "社招", "应聘", "求职", "简历",
    "个人信息", "联系方式", "教育经历", "工作经历", "自我评价", "技能特长",
}


def _fname_majors():
    """延迟导入学科目录：解析器要在没有 config 的场景下也不炸。"""
    from . import majors
    return majors


def split_segments(text: str) -> list[str]:
    """把主题/文件名切成段：先去修饰、再按分隔符切、去掉空段。"""
    s = str(text or "").strip()
    if not s:
        return []
    # 去掉扩展名（文件名进来时常见）
    s = re.sub(r"\.(pdf|docx?|txt|md|wps)$", "", s, flags=re.IGNORECASE)
    s = _DECOR.sub(" ", s)
    s = _PREFIX.sub("", s.strip())
    return [p.strip() for p in _SEP.split(s) if p.strip()]


#: 噪音段：转发/寒暄类文字，不是字段。宁可少一个字段，也不把"转发的简历"当应聘方向。
_NOISE_RE = re.compile(r"(简历|转发|麻烦|谢谢|请查收|查收|你好|打扰|看一下|应聘者|求职者|"
                       r"投递人|附件|发自我的|邮件|个人资料)")

#: "像专业"的判据：能被学科目录收录，或带学科性字样（保守——不满足就不给专业值）
_MAJOR_HINT = re.compile(r"(工程|科学|技术|管理|经济|金融|会计|法律|法学|医学|护理|设计|"
                         r"物理|化学|材料|机械|电子|计算机|通信|自动化|土木|建筑|能源|"
                         r"环境|生物|数学|统计|外语|英语|新闻|教育|心理|冶金|地质|采矿|"
                         r"测绘|电气|控制|软件|网络|安全|物流|营销|审计|财务)")


def _plausible_major(seg: str) -> str | None:
    """这个段能不能当专业：目录认得出，或带学科性字样；否则不给值（不猜）。"""
    if not seg or len(seg) < 2:
        return None
    try:
        if _fname_majors().resolve(seg):
            return seg
    except Exception:                               # noqa: BLE001
        pass
    return seg if _MAJOR_HINT.search(seg) else None


def parse(text: str) -> dict:
    """解析 `方向+学历+学校+专业+姓名+性别`，返回 `{fields, note, segments}`。

    `fields` 里**只放有把握的字段**：某个字段认不出来就不出现，
    调用方据此决定"覆盖正文抽取"还是"沿用正文"。`note` 用一句话说明取到了什么。

    锚定方式按**格式的固定顺序**来，而不是"逐段猜类别"：
    学历/性别/学校是强特征段，先标出来；姓名按下面的两种投递写法分别锚定，
    专业取姓名左边那段，方向取最左段。
    这样"材料工艺"（方向）不会被学科目录的包含关系抢走当成专业。
    """
    segs = split_segments(text)
    edu = school = gender = None
    rest: list[str] = []
    gender_anchor = None      # rest 里"紧邻性别之前"那一段的下标
    after_school = None       # 学校之后的第一段在 rest 里的下标

    for s in segs:
        if edu is None and s in _EDU_WORDS:
            edu = _EDU_WORDS[s]
            continue
        if gender is None and s in _GENDER_WORDS:
            gender = _GENDER_WORDS[s]
            gender_anchor = len(rest) - 1          # 格式里姓名就挨着性别前面
            continue
        if school is None:
            m = _SCHOOL_RE.fullmatch(s) or _SCHOOL_RE.match(s)
            if m:
                school = m.group(0)
                after_school = len(rest)           # 学校之后的段（专业/姓名…）
                continue
        rest.append(s)

    rest = [s for s in rest if not _NOISE_RE.search(s)]

    # 姓名锚定——**两种投递写法都要认**，这是踩坑换来的：
    #   a) 邮件标题「方向+学历+学校+专业+姓名+性别」：姓名紧邻性别之前；
    #      没有性别时取"学校之后"的第一个像人名的段（方向/专业都在它前面）。
    #   b) 文件名「姓名-岗位-校招-简历」：没有学校/性别锚点，姓名在**最前面**。
    #      这条尤其关键：文件名末尾常是"科学研究/工艺技术"这类**岗位词**，
    #      按"最靠后"取人名会把岗位当成姓名（实测 3 位候选人被命名成岗位名）。
    def _looks_like_name(i) -> bool:
        return (isinstance(i, int) and 0 <= i < len(rest)
                and re.fullmatch(r"[\u4e00-\u9fa5·]{2,4}", rest[i]) is not None
                and rest[i] not in _NOT_NAME)

    name, name_idx = None, -1
    if _looks_like_name(gender_anchor):
        name, name_idx = rest[gender_anchor], gender_anchor
    elif edu and school and after_school is not None:
        # 邮件标题写法但**没写性别**：学校之后是「专业, 姓名」，
        # 取这一区间里**最后一个**像人名的段（否则会把专业当成姓名）
        for i in range(len(rest) - 1, after_school - 1, -1):
            if _looks_like_name(i):
                name, name_idx = rest[i], i
                break
    else:
        # 文件名写法「姓名-岗位-校招-简历」：姓名在最前（末尾是岗位词，不能从后往前找）
        for i in range(len(rest)):
            if _looks_like_name(i):
                name, name_idx = rest[i], i
                break

    major = direction = None
    if name_idx >= 1:
        major = _plausible_major(rest[name_idx - 1])
        if name_idx >= 2:
            direction = rest[0]
    elif name_idx == 0 and len(rest) >= 2:
        # 文件名写法：姓名在最前，紧跟其后的那段就是应聘方向（岗位名）
        direction = rest[1]
    elif rest:
        direction = rest[0]
    # 同一段被当成两个字段（例如只写了「张三」的文件名既被当姓名又被当方向）时，
    # 只保留更确定的那一个：方向没有独立证据就没必要留着去参与岗位弱匹配。
    if direction and direction in {name, major, school}:
        direction = None

    out: dict[str, str] = {}
    for k, v in (("direction", direction), ("education", edu), ("school", school),
                 ("major", major), ("name", name), ("gender", gender)):
        if v:
            out[k] = v

    note = ""
    if out:
        label = {"direction": "应聘方向", "education": "学历", "school": "学校",
                 "major": "专业", "name": "姓名", "gender": "性别"}
        note = "；".join(f"{label[k]}={out[k]}" for k in
                        ("direction", "education", "school", "major", "name", "gender")
                        if k in out)
    return {"fields": out, "note": note, "segments": segs}


#: 可用主题/文件名覆盖的字段：**只碰"人"的属性**。
#: 技能、年限、证书一律不动——那些必须来自简历正文与证据核对，
#: 让标题去改它们等于绕过了"反幻觉"那道闸。
_OVERRIDABLE = ("name", "education", "school", "major", "gender")


def apply_to_candidate(cand: dict, meta: dict, raw_text: str = "") -> tuple[dict, list[str]]:
    """把主题/文件名解析到的字段写进抽取结果，返回 `(cand, 实际采用的字段名)`。

    只在**解析有值**时覆盖；解析不出来就保持正文抽取的结果不动。
    冲突时以主题为准（这是投递方按我们的格式填的），但会在明细里说明。

    **姓名额外过一道证据校验**：标题/文件名的姓名是"按位置猜"出来的，位置规则
    遇到没见过的写法就可能猜错（实测把「博士应聘」「科学研究」这类词当成了姓名）。
    所以当正文已经抽出姓名、且**标题里的姓名在简历原文里找不到**时，不覆盖，
    只记一条存疑说明——宁可保留正文抽出来的名字，也不要凭空改名。
    """
    fields = (meta or {}).get("fields") or {}
    used: list[str] = []
    conflicts: list[str] = []
    label = {"name": "姓名", "education": "学历", "school": "学校",
             "major": "专业", "gender": "性别"}
    for k in _OVERRIDABLE:
        v = fields.get(k)
        if not v:
            continue
        old = cand.get(k)
        if old and str(old) != str(v):
            if k == "name" and raw_text and str(v) not in raw_text:
                conflicts.append(f"姓名（标题/文件名写「{v}」，但简历原文里没有，"
                                 f"保留原文抽出的「{old}」）")
                continue
            conflicts.append(f"{label[k]}（正文抽取为「{old}」，以标题为准）")
        cand[k] = v
        used.append(label[k])
    if conflicts:
        cand["title_override_notes"] = conflicts
    return cand, used
