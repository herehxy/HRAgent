"""简历重命名模板（v1.29）：`匹配岗位-学历-学校-最高学历专业-姓名-性别`。

人事处 2026-10-15 指定。**唯一真源** —— 打包下载、界面预览、导出说明都用这里，
不在别处再拼一遍字符串（拼两遍必然分叉：改了这段、忘了那段，出来的名字不一样，
而 HR 根本看不出来）。

三条规则，都有原因：

1. **缺值一律 `—`，但绝不省略段位**
   省略会让列数忽长忽短，HR 打开文件夹看到"有人 5 段、有人 6 段"根本不知道少了什么。
   保留 `—` 就能一眼看出"这项没抽到/没填"。

2. **不改归档原件的文件名，只改"包内名"**
   原件是审计凭据（`documents.file_hash` 唯一约束、解析留档都依赖它）。
   `_zip_response` 本来就支持"磁盘名与包内名分离"，所以只在打包那一刻换名字，
   溯源依然走 `documents.id`。在库里改 `file_name` 会让"库里的名字"与
   "HR 电脑上的文件"对不上，而后者才是 HR 要用的。

3. **性别按普通字段处理**（v1.29.4，业务方 2026-10-16 定调"正常处理"）
   不做脱敏、不设开关：简历明写了就照写，没写/没识别出来输出 `—`。
"""
from __future__ import annotations

import re

#: 缺值占位（与 `xlsx.EMPTY` 同一个符号，全项目统一）
EMPTY = "—"

#: Windows 文件名非法字符 + 分隔符。踩过坑：路径里有 `/` 会让 zip 里的条目
#: 被解包器当成目录，文件"凭空消失"。
_ILLEGAL = re.compile(r'[\\/:*?"<>|\r\n\t]')

#: 段序（**不要改** —— 改了就和 HR 手上的习惯不一致）
SEGMENTS = ("匹配岗位", "学历", "学校", "最高学历专业", "姓名", "性别")

SEP = "-"
MAX_LEN = 120


def _seg(v) -> str:
    s = _ILLEGAL.sub("", str(v or "").strip())
    s = re.sub(r"\s+", " ", s).strip(" -_")
    return s or EMPTY


def resume_filename(rec: dict, ext: str = "") -> str:
    """一条候选人记录 → 下载时的文件名（不含扩展名时会自动补原始扩展名）。

    rec 需要的键：job_title / edu_level / school / major / name / gender
    """
    job = _seg(rec.get("job_title"))          # **匹配岗位**：用已归岗的岗位名
    # 未归岗时给 `—` 而不是"建议岗位" —— 建议是系统猜的，
    # 印在发给用人单位的文件名上会变成一个不存在的岗位承诺。
    edu = _seg(rec.get("edu_level"))
    school = _seg(rec.get("school"))
    major = _seg(rec.get("major"))            # 最高学历专业（简历原文的专业）
    name = _seg(rec.get("name"))
    gender = _seg(rec.get("gender"))
    parts = [job, edu, school, major, name, gender]
    out = SEP.join(parts)
    if len(out) > MAX_LEN:
        # 超长时**从后往前截**（姓名最重要），并保住扩展名
        keep = ""
        for p in reversed(parts):
            if len(keep) + len(p) + 1 > MAX_LEN - 6:
                break
            keep = p + (SEP + keep if keep else "")
        out = (keep or out[:MAX_LEN - 6])[:MAX_LEN - 6] + "…"
    if ext:
        e = ext if ext.startswith(".") else ("." + ext)
        out += e
    return out


def preview_table(rows: list[dict]) -> list[str]:
    """给界面看的"预览名"列表（同一函数，界面看到的和真下载的必然一致）。"""
    return [resume_filename(r, "") for r in rows]