"""邮件模板：把 `{变量}` 替换成真实数据。

一条重要规则：**变量取不到值时输出 `【待填：xxx】`，不填空、不猜**。
空缺一眼可见，才不会把"未填写面试时间"的邮件发出去。渲染结果里同时返回
未取值变量清单，界面据此高亮提示。
"""
from __future__ import annotations

import re

#: 模板里可直接用的内置变量（由系统从候选人/岗位/当前用户自动取）
BUILTIN_VARS = [
    {"key": "姓名", "desc": "候选人姓名"},
    {"key": "性别", "desc": "简历上明写的性别（未写则为空）"},
    {"key": "学历", "desc": "最高学历"},
    {"key": "专业", "desc": "所学专业"},
    {"key": "毕业院校", "desc": "毕业院校"},
    {"key": "工作年限", "desc": "工作年限"},
    {"key": "应聘岗位", "desc": "该投递对应的岗位"},
    {"key": "HR姓名", "desc": "当前登录的 HR"},
    {"key": "日期", "desc": "今天日期"},
]

#: 需要 HR 在发送前填写的运行时变量（系统不猜）
RUNTIME_VARS = [
    {"key": "面试时间", "desc": "如 4 月 22 日（周三）"},
    {"key": "面试时段", "desc": "如 8:30-9:00"},
    {"key": "面试地点", "desc": "如 创新大楼 1519 会议室"},
    {"key": "面试单位", "desc": "如 数字化中心"},
    {"key": "面试方式", "desc": "如 现场面试 / 线上面试"},
    # v1.20：会议号。**只在选"线上面试"时**才要求填（现场面试留空即可，
    # 模板里那一行会随方式一起消失/ 保留空白）。
    {"key": "会议号", "desc": "线上面试的会议号或入会链接"},
    {"key": "联系人", "desc": "如 人力资源部 张老师"},
    {"key": "联系电话", "desc": "留给候选人的联系电话"},
]

_VAR_RE = re.compile(r"\{([^{}\n]{1,15})\}")


# 条件块：<!--IF:变量==值-->…<!--ENDIF--> / <!--IF:变量-->…<!--ENDIF-->
# 不满足就**整块删除**——现场面试不该看到"腾讯会议：xxx"这一行。
_IF_RE = re.compile(
    r"<!--IF:(?P<var>[^{}]{1,20}?)(?:==(?P<val>[^<>]{1,40}?))?-->(?P<body>.*?)<!--ENDIF-->",
    re.S)


def _apply_conditions(text: str, ctx: dict) -> str:
    def one(m):
        var = m.group("var").strip()
        want = m.group("val")
        val = str(ctx.get(var) or "").strip()
        keep = (val == want.strip()) if want is not None else bool(val)
        return m.group("body") if keep else ""
    # 反复套用：允许嵌套（内层先判）
    out = text or ""
    for _ in range(3):
        new = _IF_RE.sub(one, out)
        if new == out:
            break
        out = new
    return out


def render(text: str, ctx: dict) -> dict:
    """渲染模板。

    返回 {text, missing, used}：
      - `missing`：模板里用到但本次没取到值的变量 → 界面要高亮提醒
      - `used`：本次真正替换成功的变量
    """
    missing: list[str] = []
    used: list[str] = []

    def sub(m):
        key = m.group(1).strip()
        val = ctx.get(key)
        if val is None or str(val).strip() == "":
            if key not in missing:
                missing.append(key)
            return f"【待填：{key}】"
        if key not in used:
            used.append(key)
        return str(val)

    # 顺序很重要：先删掉不满足的条件块，再替换 {} 变量——
    # 否则被删掉的块里的变量还会被算成"待填"。
    return {"text": _VAR_RE.sub(sub, _apply_conditions(text or "", ctx)),
            "missing": missing, "used": used}


def build_ctx(candidate: dict | None, job_title: str = "", hr_name: str = "",
              runtime: dict | None = None) -> dict:
    """把候选人/岗位/HR 的字段拼成渲染上下文（键名与模板里的变量名一致）。"""
    c = candidate or {}
    from datetime import datetime
    ctx = {
        "姓名": c.get("name") or c.get("candidate_name") or "",
        "性别": c.get("gender") or "",
        "学历": c.get("edu_level") or "",
        "专业": c.get("major") or "",
        "毕业院校": c.get("school") or "",
        "工作年限": (f"{c['years_exp']} 年" if isinstance(c.get("years_exp"), int) else ""),
        "应聘岗位": job_title or "",
        "HR姓名": hr_name or "",
        "日期": f"{datetime.now():%Y年%m月%d日}",
    }
    for k, v in (runtime or {}).items():
        ctx[k] = v
    return ctx


#: 新建模板时可选的场景标签（只用于分类展示）
SCENES = ["初面邀约", "复面邀约", "材料补交", "录用沟通", "其他通知"]


# ============================================================
# 正文 → HTML（邮件原生格式）
# ============================================================
# 为什么要有这一层：纯文本邮件里表格是排不出来的（`| 列 | 列 |` 会原样显示），
# 而面试邀约这类正文天然就是"项目/内容"两列的表格。
#
# 为什么不让 HR 直接写 HTML：门槛太高、漏一个闭合标签整封邮件就乱版；
# 而且变量值是候选人数据，直接拼进 HTML 有转义风险。
# 所以只认**三种最小语法**，其余一律按纯文本转义处理：
#   1) 表格：`| 列1 | 列2 |`，紧跟一行 `| --- | --- |` 作为表头分隔；
#   2) 粗体：`**文字**`；
#   3) 其余：空行分段、单换行换行。
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP = re.compile(r"^\s*\|[\s\-:|]+\|\s*$")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")
_MISSING_RE = re.compile(r"【待填：([^】]{1,20})】")


def _inline(escaped: str) -> str:
    """行内标记：粗体 + 未填变量高亮（输入必须是**已转义**的文本）。"""
    out = _BOLD_RE.sub(r"<b>\1</b>", escaped)
    return _MISSING_RE.sub(
        r'<span style="color:#c0392b">【待填：\1】</span>', out)


def _cells(line: str) -> list[str]:
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def needs_html(text: str) -> bool:
    """正文是否该按 HTML 邮件发——**富文本标记**或**本身就是 HTML** 都算。

    保守判断：只有真用了富文本才升级成 HTML 邮件；
    纯文字通知保持原来的纯文本形态，HR 之间转发、老客户端显示都最稳。
    """
    raw = text or ""
    if looks_like_html(raw):
        return True
    if _BOLD_RE.search(raw):
        return True
    lines = raw.split("\n")
    for i, ln in enumerate(lines[:-1]):
        if _TABLE_ROW.match(ln) and _TABLE_SEP.match(lines[i + 1]):
            return True
    return False


#: 判"这已经是 HTML"的标签：模板里直接排复杂版式（合并单元格、居中标题）时用得上——
#: 极简表格语法表达不了 colspan/rowspan，硬塞只会把 HR 逼回手写 HTML。
_HTML_HINT = re.compile(
    r"<\s*(table|thead|tbody|tr|t[dh]|p|div|span|b|strong|i|em|u|br|ul|ol|li|h[1-6])"
    r"(\s|>|/)", re.IGNORECASE)


def looks_like_html(text: str) -> bool:
    """正文看起来是不是已经是 HTML 片段。"""
    return bool(_HTML_HINT.search(text or ""))


def to_html(text: str) -> str:
    """把正文转成 HTML 片段（仅供邮件正文使用）。

    整体先转义、再套标记——顺序不能反：反过来会让候选人姓名里的 `<>`
    直接变成标签，一封邮件就能把版式打乱（也属于注入面）。
    """
    import html as _html

    lines = (text or "").split("\n")
    parts: list[str] = []
    buf: list[str] = []          # 普通段落缓冲
    i = 0

    def flush() -> None:
        if buf:
            parts.append("<p style=\"margin:0 0 12px\">"
                         + "<br>".join(buf) + "</p>")
            buf.clear()

    while i < len(lines):
        ln = lines[i]
        is_table = (_TABLE_ROW.match(ln) and i + 1 < len(lines)
                    and _TABLE_SEP.match(lines[i + 1]))
        if not is_table:
            if not ln.strip():
                flush()
            else:
                buf.append(_inline(_html.escape(ln, quote=False)))
            i += 1
            continue

        flush()
        head = _cells(ln)
        i += 2                                    # 跳过表头分隔行
        body_rows: list[list[str]] = []
        while i < len(lines) and _TABLE_ROW.match(lines[i]):
            body_rows.append(_cells(lines[i]))
            i += 1
        ths = "".join(
            f'<th style="border:1px solid #d0d5dd;background:#f2f4f7;padding:6px 10px;'
            f'text-align:left;font-weight:600">{_inline(_html.escape(c, quote=False))}</th>'
            for c in head)
        trs = "".join(
            "<tr>" + "".join(
                f'<td style="border:1px solid #d0d5dd;padding:6px 10px">'
                f'{_inline(_html.escape(c, quote=False))}</td>' for c in row)
            + "</tr>" for row in body_rows)
        parts.append(
            '<table cellspacing="0" cellpadding="0" '
            'style="border-collapse:collapse;font-size:14px;margin:0 0 12px">'
            f"<thead><tr>{ths}</tr></thead><tbody>{trs}</tbody></table>")

    flush()
    inner = "\n".join(parts) or "<p style=\"margin:0\"></p>"
    return ('<div style="font-family:-apple-system,\'Segoe UI\',\'Microsoft YaHei\',sans-serif;'
            'font-size:14px;line-height:1.75;color:#1d2129">' + inner + "</div>")


# ------------------------------------------------------------
# HTML 邮件的两个配套工具：拆纯文本、做安全清洗
# ------------------------------------------------------------
_TAG_STRIP = re.compile(r"<(script|style|iframe|object|embed|link|meta)\b[^>]*>.*?</\1>",
                        re.IGNORECASE | re.DOTALL)
_ON_ATTR = re.compile(r"\son[a-z]+\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)", re.IGNORECASE)
_JS_URL = re.compile(r"(href|src)\s*=\s*(\"|')?\s*javascript:[^\"'>\s]*(\"|')?",
                     re.IGNORECASE)


def sanitize_email_html(html: str) -> str:
    """给要发出去的 HTML 做最小清洗。

    正文是 HR 自己写/粘贴的，本机单角色使用，风险本来就低；但邮件正文一旦带上
    `<script>` / `onclick=` / `javascript:` 链接，在别人的客户端里可能被判为可疑邮件
    甚至被拦截——**发出去的东西收不回来**，所以这两类一律剥掉。
      （`document.execCommand('bold')` 之类产生的内容不受影响。）
    """
    s = _TAG_STRIP.sub("", html or "")
    s = _ON_ATTR.sub("", s)
    s = _JS_URL.sub(r"\1=\"#\"", s)
    return s


def html_to_text(html: str) -> str:
    """HTML → 纯文本，供"不显示 HTML 的客户端"读取（multipart 的纯文本部分）。

    表格转成 `列1 | 列2` 的行文本、单元格之间用 ` | `、每条记录一行——
    纯文本客户端里读起来仍然是一张表，而不是一坨被压扁的字符串。
    """
    import html as _html

    s = html or ""
    s = re.sub(r"<(br|BR)\s*/?>", "\n", s)
    s = re.sub(r"</(p|div|h[1-6]|li|tr)\s*>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"</t[dh]\s*>", " | ", s, flags=re.IGNORECASE)
    s = re.sub(r"<table\b[^>]*>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"</table\s*>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", "", s)
    s = _html.unescape(s)
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()
