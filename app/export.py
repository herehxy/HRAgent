"""导出数据组装（v1.29）：**一份名单、一套列、一个入口**。

业务方 2026-10-15 定调：「无论什么时候导出都是一个按钮入口」，表格字段固定为那 13 列；
**没进行到对应阶段的人，字段不填、用 `—` 标识；进行到该阶段的自动填好**。
所以这里只有**一张表**（候选人名单），不再是"已面试人员表 + 初筛通过表"两张。

**口径逐条钉死（这一层最容易错）**

1. **行集 = 全部候选人（含归档）**，按当前筛选条件过滤。
   归档的人**必须在里面**：「不录用 /拒签 → 已结束 → 自动归档」之后他们就从人才库
   看不见了，而这恰恰是「是否录用」列里最该出现的一批 —— 漏了他们，这张表只剩好消息。
   （之前分两张表时"已面试表"已含归档、"初筛通过表"不含；合成一张就没这个分歧了。）

2. **列固定 13 列，不随阶段变化。** 没走到的阶段给 `—`（不是空单元格 ——
   空着分不清"系统确认这里没有"与"导出漏了/串列了"）。

3. 哪些列在什么阶段才有值：
   · 姓名 / 学校 / 学历 / 电话 —— 一直有
   · 面试单位 —— **初筛通过登记时**就有了（进入待面试前必填），所以早于面试就有值
   · 面试日期 / 面试方式 —— 发面试邀请时回写
   · 是否录用 —— 由 (阶段, 结论) **推导**（`stages.verdict`），不另存字段，
     与界面显示同一口径
   · 录用函发送时间 —— 发录用函那一刻系统写
   · 签约时间 / 入职最后期限 / 入职情况 —— 人事处手工登记

4. **固定排序**：阶段顺序 → 投递时间倒序 → id。同一份数据每次导出顺序一致，
   拿两份表能直接比对"这周新增/变化了谁"。
"""
from __future__ import annotations

import os
from datetime import datetime

from . import stages as stages_mod
from . import xlsx as xlsx_mod

EMPTY = xlsx_mod.EMPTY

#: 人事处 2026-10-15 指定的 13 列，**顺序即列序**（不要擅自调整、不要按阶段增减）
ROSTER_COLS = ["序号", "面试日期", "面试单位", "姓名", "学校", "学历",
               "面试方式", "电话", "是否录用", "录用函发送时间",
               "签约时间", "入职最后期限", "入职情况"]

#: 旧名保留别名：外部脚本/已发的判据可能还引用它（指向同一份列定义，别分叉）
INTERVIEWED_COLS = ROSTER_COLS


def _v(val) -> str:
    """空值 → `—`（唯一出口，不许各写一份 `or "—"`）。"""
    s = "" if val is None else str(val).strip()
    return s if s and s != EMPTY else EMPTY


def _phone(c: dict) -> str:
    """从库里解出明文电话；解不开/没有都给 `—`。

    ⚠️ 演示库里 phone_enc 被 `strip_pii` 清空了（随包不含 PII 主密钥，
    密文在收件人机器上也解不开）——那种情况返回 `—`，不是一串乱码。
    """
    from .crypto import decrypt
    try:
        return _v(decrypt(c.get("phone_enc")))
    except Exception:                                   # noqa: BLE001
        return EMPTY


def build_roster(conn, *, operator: str = "HR", role: str = "hr",
                 include_archived: bool = True, **filters) -> tuple[list[str], list[list], set[int], int]:
    """候选人名单（固定 13 列）。返回 `(headers, rows, text_cols, n)`。

    `include_archived=True`（默认）：**归档的人也在表里**。业务方 2026-10-15 明确要求 ——
    自动归档之后他们从人才库消失了，而这恰恰是「是否录用」列最该出现的一批。

    `text_cols` 含「序号」与「电话」—— 后者是**必须的**：不设文本格式的话
    Excel 会把 13800138000 显示成 1.38E+10，而"看起来表是好的"，
    等对方打电话打不通才发现。
    """
    from . import db as db_mod

    items = db_mod.list_candidates(conn, archived=None if include_archived else False,
                                   light=False, **filters)
    order = stages_mod.all_stages()
    rank = {s: i for i, s in enumerate(order)}

    def _k(i):
        st = stages_mod.normalize_stage(i.get("stage"))
        return (rank.get(st, len(order)), str(i.get("applied_at") or ""), int(i.get("id") or 0))

    items.sort(key=_k)                       # 阶段顺序 → 投递时间 → id，**升序**
    items.reverse()                          # 整体倒序：最新阶段/最新投递在前

    rows: list[list] = []
    for n, i in enumerate(items, 1):
        # 「是否录用」由 (阶段, 结论) 推导 —— 还没判的阶段给 `—`
        vd = stages_mod.verdict(i.get("stage"), i.get("outcome"))
        rows.append([
            n,
            _v(i.get("interview_date")),     # 发面试邀请时回写
            _v(i.get("interview_unit")),     # 初筛通过登记时就有
            _v(i.get("name")),
            _v(i.get("school")),
            _v(i.get("edu_level")),
            _v(i.get("interview_mode")),     # 发面试邀请时回写
            _phone(i),
            _v(vd) if vd else EMPTY,
            _v(i.get("offer_sent_at")),
            _v(i.get("signed_at")),
            _v(i.get("onboard_deadline")),
            _v(i.get("onboard_status")),
        ])
    return (list(ROSTER_COLS), rows, {0, 7}, len(items))





def bundle_name(conn, filters: dict, *, operator: str, role: str,
                scope: str, n: int) -> tuple[str, list[tuple[str, str]]]:
    """初筛通过人员的简历打包：`[(磁盘路径, 包内文件名)]`。

    命名模板（人事处指定）：`匹配岗位-学历-学校-最高学历专业-姓名-性别`
    —— 见 `app/naming.py`（**唯一真源**，不在这里另写一份拼串）。
    """
    from . import db as db_mod
    from . import naming as naming_mod

    items = db_mod.list_candidates(conn, archived=False, light=True, **filters)
    items = [i for i in items if stages_mod.normalize_stage(i.get("stage")) == "初筛通过"]
    items.sort(key=lambda i: int(i.get("id") or 0))
    pairs: list[tuple[str, str]] = []
    used: set[str] = set()
    for i in items:
        docs = _latest_resume_doc(conn, int(i["id"]))
        if not docs:
            continue
        path, fname = docs
        base = naming_mod.resume_filename(i, os.path.splitext(fname)[1])
        # 同名加序号（两个人都叫"张三-本科-…-男.pdf"是真会发生的）
        final = base
        k = 2
        while final in used:
            final = f"{base}({k})"
            k += 1
        used.add(final)
        pairs.append((path, final))
    stamp = datetime.now().strftime("%Y%m%d")
    return f"初筛通过-{stamp}.zip", pairs


def _latest_resume_doc(conn, cid: int) -> tuple[str, str] | None:
    """这个人最新一份**简历**原件（不含"其他附件"）的 (绝对路径, 文件名)。

    ⚠️ 必须按 `kind='resume'` 挑：附件按 id 倒序时最新一份常是作品集/成绩单，
    打包下载时把作品集当简历发出去（或反过来漏掉真简历）很难被发现。
    """
    from . import db as db_mod
    from .server import _abs_stored
    d = db_mod.candidate_detail(conn, cid) or {}
    docs = d.get("resume_documents") or []
    if not docs:
        return None
    doc = docs[0]
    rel = doc.get("archived_path") or doc.get("file_path") or ""
    p = _abs_stored(rel) if rel else ""
    if not p or not os.path.exists(p):
        return None
    return p, str(doc.get("file_name") or os.path.basename(p))