"""导出 xlsx 的统一写法（v1.29）。

为什么不用 CSV 顶替
------------------
人事处要拿导出表**发给用人单位、贴进汇报**，CSV 在 Excel 里打开有三个实际麻烦：

1. 手机号被识别成科学计数法（`13800138000` → `1.38001E+10`）—— 一旦这样，
   对方打电话打不通，而**看起来表是好的**；
2. 没有表头加粗、没有列宽、没有冻结首行，13 列横向一屏放不下；
3. 含逗号/换行的内容要靠引号包裹，碰上原文里有引号就串列。

依赖 `openpyxl`（纯 Python、可离线装 wheel、PyInstaller 打包无副作用）。

**口径（所有导出接口共用这里，别各写一份）**
----------------------------------------
- 缺值一律写 `—`（与 `auth.present_candidate()` 里 `phone or "—"` 同一符号）。
  空单元格分不清"系统确认这里没有"和"导出漏了/串列了"。
- **文本列强制文本格式**：不设的话 Excel 会把手机号、证书编号当数字处理，
  长数字被截成科学计数法、前导零被吃掉（"041001" → "41001"）。
- 表尾附一行导出元信息（导出时间 / 范围 / 条数）：打印出来才知道这份表
  是"什么时候的快照"，否则两张表放一起分不清先后。
"""
from __future__ import annotations

import io
from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

#: 缺值占位符（全项目统一，勿各写一份）
EMPTY = "—"

_HEAD_FILL = PatternFill("solid", fgColor="EFF3F8")
_META_FONT = Font(size=9, color="86909C")


def _is_num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def write_sheet(rows: list[list], headers: list[str], *,
                text_cols: set[int] | None = None,
                meta: str = "",
                sheet_title: str = "导出") -> bytes:
    """生成一个 xlsx 的字节流。

    rows    数据行（list[list]），缺失的格子给 `None` 或空串都行
    headers 表头（顺序即列序）
    text_cols 需要**强制文本格式**的列序号（0 基）。不指定的话
            手机号这类长数字会被 Excel 转成科学计数法。
    meta    表尾那一行的说明文字（导出时间/范围/条数）
    """
    wb = Workbook()
    ws = wb.active
    ws.title = (sheet_title or "导出")[:31]        # Excel 工作表名上限 31 字符

    ws.append(list(headers))
    for c in range(1, len(headers) + 1):
        cell = ws.cell(row=1, column=c)
        cell.font = Font(bold=True)
        cell.fill = _HEAD_FILL
        cell.alignment = Alignment(vertical="center", horizontal="center")

    tcols = text_cols or set()
    for r in rows:
        ws.append([("" if v is None else v) for v in r])

    # 文本格式：必须在**写完值之后**设，且序号从 1 开始（openpyxl 是 1 基）
    for idx in tcols:
        if 0 <= idx < len(headers):
            col = get_column_letter(idx + 1)
            for row in range(2, ws.max_row + 1):
                ws[f"{col}{row}"].number_format = "@"

    # 列宽：按内容长度估算，中文按 2 个字符宽计（否则中文列会被截得看不清）
    for i, h in enumerate(headers):
        width = _display_len(str(h)) + 2
        for r in rows[:200]:                      # 只看前 200 行，够估宽度了
            if i < len(r):
                width = max(width, min(_display_len(str(r[i] if r[i] is not None else "")) + 2, 42))
        ws.column_dimensions[get_column_letter(i + 1)].width = max(8, min(width, 42))

    if rows:
        ws.freeze_panes = "A2"                    # 冻结首行
    if meta:
        ws.append([])
        ws.append([meta])
        ws.cell(row=ws.max_row, column=1).font = _META_FONT

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _display_len(s: str) -> int:
    """显示宽度：中文/全角算 2，其余算 1。"""
    n = 0
    for ch in str(s or ""):
        n += 2 if ("\u2e80" <= ch <= "\u9fff" or "\uff00" <= ch <= "\uffef") else 1
    return n


def csv_bytes(rows: list[list], headers: list[str]) -> bytes:
    """同口径的 CSV（保留给"要贴进邮件正文/用 Excel 打开"的场合）。

    同样带 BOM：没有 BOM 的话 Excel 会按本地编码猜，中文变乱码。
    ⚠️ CSV **无法**解决科学计数法与列宽问题 —— 那正是 xlsx 存在的理由。
    """
    import csv
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\r\n")
    w.writerow(headers)
    for r in rows:
        w.writerow(["" if v is None else v for v in r])
    return ("﻿" + out.getvalue()).encode("utf-8")


def meta_line(scope: str, filters: str, n: int) -> str:
    """表尾那一行（导出时间 / 范围 / 条数）。"""
    return f"导出时间 {datetime.now():%Y-%m-%d %H:%M}｜范围 {scope}｜共 {n} 条｜由企业人才库工作台导出"