"""招聘进度表的 Excel 输出（v4.0.16）。

为什么单独一个文件
------------------
`app/xlsx.py` 是“单表 + 表头 + 表尾”的通用导出，官方进度表需要多工作表、
合并单元格、单位分组、合计/学历汇总/总结句、动态签约人列 —— 直接改通用层会把
现有 13 列名单导出的行为也拖进来。这里用 openpyxl 直接画，通用层保持不动。

导出是**快照**：单元格写计算后的值，不写公式。这样 HR 打开就是对的，
不会因为删行/改行出现 `#REF!`。需要“在 Excel 里改计划数自动重算”的版本，
后续单独加。
"""
from __future__ import annotations

import io
from datetime import datetime

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.properties import PageSetupProperties

from .recruit import EDU_LEVELS

EMPTY = "—"
FONT_CN = "微软雅黑"
FONT_ROSTER = "方正楷体简体"

_FILL_YELLOW = PatternFill("solid", fgColor="FFFF00")
_FILL_BLUE = PatternFill("solid", fgColor="BDD7EE")
_FILL_HEAD = PatternFill("solid", fgColor="EFF3F8")
_THIN = Side(style="thin", color="000000")
_MEDIUM = Side(style="medium", color="000000")
_BORDER = Border(left=_THIN, right=_THIN, top=_THIN, bottom=_THIN)
_BORDER_MED = Border(left=_MEDIUM, right=_MEDIUM, top=_MEDIUM, bottom=_MEDIUM)
_CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
_LEFT = Alignment(horizontal="left", vertical="center", wrap_text=True)


def _safe_write(ws, row: int, col: int, value):
    """写值；以 = + - @ 开头的文本强制按字符串写，防 Excel 公式注入。"""
    c = ws.cell(row=row, column=col)
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
        c.value = value
        c.data_type = "s"
    else:
        c.value = "" if value is None else value
    return c


def _style_range(ws, row1: int, col1: int, row2: int, col2: int, *,
                 font: Font, fill: PatternFill | None = None,
                 align: Alignment = _CENTER, border: Border = _BORDER):
    for r in range(row1, row2 + 1):
        for c in range(col1, col2 + 1):
            cell = ws.cell(row=r, column=c)
            cell.font = font
            cell.alignment = align
            cell.border = border
            if fill is not None:
                cell.fill = fill


def _set_widths(ws, widths: list[float]):
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w


def _pct(v) -> str:
    return f"{v * 100:.1f}%" if isinstance(v, (int, float)) else EMPTY


def _write_rate(ws, row: int, col: int, rate):
    """完成率写成**数值 + 百分比格式**，不是字符串 —— 这样 HR 还能排序/求平均。"""
    c = _safe_write(ws, row, col, float(rate) if isinstance(rate, (int, float)) else EMPTY)
    if isinstance(rate, (int, float)):
        c.number_format = "0.0%"
    return c


def _meta(scope: str) -> str:
    return (f"导出时间 {datetime.now():%Y-%m-%d %H:%M}｜{scope}｜"
            f"数据来源：人才库智能体")


def _unit_rows(progress: dict) -> list[tuple[dict, list[dict]]]:
    """按单位分组，并保证每个单位都有 4 个学历行（缺的补 0）。"""
    groups: list[tuple[dict, list[dict]]] = []
    index: dict[str, list[dict]] = {}
    order: list[str] = []
    for row in progress.get("rows") or []:
        key = row.get("unit_key") or row.get("unit_name") or ""
        if key not in index:
            index[key] = []
            order.append(key)
            groups.append((row, index[key]))
        index[key].append(row)
    out = []
    for key, rows in zip(order, [index[k] for k in order]):
        by_edu = {r.get("edu_level"): r for r in rows}
        unit = rows[0]
        full = []
        for i, edu in enumerate(EDU_LEVELS):
            r = by_edu.get(edu)
            if r:
                full.append(r)
            else:
                full.append({
                    "unit_name": unit.get("unit_name"), "unit_key": unit.get("unit_key"),
                    "edu_level": edu, "plan_approved": 0, "plan_extra": 0,
                    "plan_carryover": 0, "plan_cancel": 0, "should_hire": 0,
                    "signed_count": 0, "gap": 0, "rate": None, "signees": [],
                    "sort_order": int(unit.get("sort_order") or 0) + i,
                })
        out.append((unit, full))
    return out


def official_workbook(progress: dict, roster: dict) -> bytes:
    year = progress.get("year") or {}
    label = year.get("label") or "招聘进度"
    start, end = year.get("period_start") or "", year.get("period_end") or ""
    groups = _unit_rows(progress)
    max_signees = 0
    for _unit, rows in groups:
        for r in rows:
            max_signees = max(max_signees, len(r.get("signees") or []))
    signee_cols = max(10, max_signees)
    last_col = 11 + signee_cols
    wb = Workbook()
    ws = wb.active
    ws.title = (f"{label}招聘进度表")[:31]

    # 标题
    title = (f"{label}招聘进度表（{start} 至 {end}，以签约时间为准，"
             f"计划+追加=签协议+取消）")
    _safe_write(ws, 1, 1, title)
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=last_col)
    _style_range(ws, 1, 1, 1, last_col,
                 font=Font(name=FONT_CN, size=20, bold=True), border=_BORDER_MED)
    ws.row_dimensions[1].height = 49.8

    headers = ["序号", "单位", "学历", "上会审批需求计划数量", "追加计划数",
               "转上年度计划数", "应取消计划数", "应招总数", "已签人数",
               "签约缺口", "完成率"] + [f"签约人{i}" for i in range(1, signee_cols + 1)]
    for i, h in enumerate(headers, 1):
        _safe_write(ws, 2, i, h)
    _style_range(ws, 2, 1, 2, last_col,
                 font=Font(name=FONT_CN, size=14, bold=True), border=_BORDER_MED)
    for c in range(4, 8):
        ws.cell(row=2, column=c).fill = _FILL_YELLOW
    for c in range(8, last_col + 1):
        ws.cell(row=2, column=c).fill = _FILL_BLUE
    ws.row_dimensions[2].height = 74.65

    body_font = Font(name=FONT_CN, size=14)
    row_no = 3
    unit_no = 0
    for unit, rows in groups:
        unit_no += 1
        start_row = row_no
        for r in rows:
            _safe_write(ws, row_no, 1, unit_no if row_no == start_row else None)
            _safe_write(ws, row_no, 2, unit.get("unit_name") if row_no == start_row else None)
            _safe_write(ws, row_no, 3, r.get("edu_level"))
            for c, k in ((4, "plan_approved"), (5, "plan_extra"),
                         (6, "plan_carryover"), (7, "plan_cancel"),
                         (8, "should_hire"), (9, "signed_count"), (10, "gap")):
                _safe_write(ws, row_no, c, int(r.get(k) or 0))
            _write_rate(ws, row_no, 11, r.get("rate"))
            for i, s in enumerate((r.get("signees") or [])[:signee_cols], 12):
                _safe_write(ws, row_no, i, s.get("name") or "")
            ws.row_dimensions[row_no].height = 20.25
            row_no += 1
        # A/B 两列按 4 行合并（与源表一致）
        ws.merge_cells(start_row=start_row, start_column=1,
                       end_row=row_no - 1, end_column=1)
        ws.merge_cells(start_row=start_row, start_column=2,
                       end_row=row_no - 1, end_column=2)
        _style_range(ws, start_row, 1, row_no - 1, last_col, font=body_font)
        ws.cell(row=start_row, column=1).border = Border(
            left=_MEDIUM, right=_THIN, top=_MEDIUM, bottom=_THIN)
        ws.cell(row=start_row, column=2).border = Border(
            left=_THIN, right=_THIN, top=_MEDIUM, bottom=_THIN)

    totals = progress.get("totals") or {}
    _safe_write(ws, row_no, 1, "合计")
    ws.merge_cells(start_row=row_no, start_column=1, end_row=row_no, end_column=3)
    for c, k in ((4, "plan_approved"), (5, "plan_extra"), (6, "plan_carryover"),
                 (7, "plan_cancel"), (8, "should_hire"), (9, "signed_count"),
                 (10, "gap")):
        _safe_write(ws, row_no, c, int(totals.get(k) or 0))
    _write_rate(ws, row_no, 11, totals.get("rate"))
    _style_range(ws, row_no, 1, row_no, last_col,
                 font=Font(name=FONT_CN, size=14, bold=True), fill=_FILL_BLUE,
                 border=_BORDER_MED)
    ws.row_dimensions[row_no].height = 20.25
    row_no += 2

    # 学历分布汇总
    _safe_write(ws, row_no, 1, "学历分布汇总（按学历自动统计）")
    ws.merge_cells(start_row=row_no, start_column=1, end_row=row_no, end_column=5)
    _style_range(ws, row_no, 1, row_no, 5,
                 font=Font(name=FONT_CN, size=14, bold=True), fill=_FILL_BLUE,
                 border=_BORDER_MED)
    row_no += 1
    for i, h in enumerate(["学历", "应招总数", "已签人数", "缺口", "完成率"], 1):
        _safe_write(ws, row_no, i, h)
    _style_range(ws, row_no, 1, row_no, 5,
                 font=Font(name=FONT_CN, size=14, bold=True), fill=_FILL_BLUE)
    row_no += 1
    for item in progress.get("by_education") or []:
        _safe_write(ws, row_no, 1, item.get("edu_level"))
        _safe_write(ws, row_no, 2, int(item.get("should_hire") or 0))
        _safe_write(ws, row_no, 3, int(item.get("signed_count") or 0))
        _safe_write(ws, row_no, 4, int(item.get("gap") or 0))
        _write_rate(ws, row_no, 5, item.get("rate"))
        _style_range(ws, row_no, 1, row_no, 5, font=body_font)
        row_no += 1
    _safe_write(ws, row_no, 1, "合计")
    _safe_write(ws, row_no, 2, int(totals.get("should_hire") or 0))
    _safe_write(ws, row_no, 3, int(totals.get("signed_count") or 0))
    _safe_write(ws, row_no, 4, int(totals.get("gap") or 0))
    _write_rate(ws, row_no, 5, totals.get("rate"))
    _style_range(ws, row_no, 1, row_no, 5,
                 font=Font(name=FONT_CN, size=14, bold=True), fill=_FILL_BLUE)
    row_no += 2

    _safe_write(ws, row_no, 1, progress.get("summary") or "")
    ws.merge_cells(start_row=row_no, start_column=1, end_row=row_no, end_column=11)
    _style_range(ws, row_no, 1, row_no, 11,
                 font=Font(name="等线", size=14), align=_LEFT,
                 border=_BORDER_MED)
    ws.row_dimensions[row_no].height = 45
    row_no += 1
    _safe_write(ws, row_no, 1, _meta(f"{label}招聘进度表"))
    ws.merge_cells(start_row=row_no, start_column=1, end_row=row_no, end_column=11)
    _style_range(ws, row_no, 1, row_no, 11,
                 font=Font(name=FONT_CN, size=9, color="86909C"), align=_LEFT)

    widths = [8.28, 16.57, 11.24, 16.57, 11.83, 14.2, 11.83,
              10, 10, 10, 9] + [10] * signee_cols
    _set_widths(ws, widths)
    ws.freeze_panes = "D3"
    ws.sheet_view.showGridLines = False
    ws.page_setup.orientation = "landscape"
    ws.page_setup.paperSize = 8          # A3（openpyxl 的常量在不同版本里位置不一）
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)

    # Sheet2：面试总名单
    _fill_roster_sheet(wb.create_sheet((f"{label}面试总名单")[:31]),
                       roster, label, start, end)

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _fill_roster_sheet(ws, roster: dict, label: str, start: str, end: str,
                       with_title: bool = True) -> None:
    headers = ["序号", "面试日期", "单位", "姓名", "学校", "学历", "面试方式",
               "电话", "是否录用", "录用函", "发放时间", "签约情况",
               "签约时间", "毕业时间", "入职情况", "备注"]
    title_row = 1 if with_title else 0
    if with_title:
        _safe_write(ws, 1, 1, f"{label}面试总名单及录用情况（{start} 至 {end}）")
        ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(headers))
        _style_range(ws, 1, 1, 1, len(headers),
                     font=Font(name=FONT_ROSTER, size=22, bold=True),
                     border=_BORDER_MED)
        ws.row_dimensions[1].height = 27.75
    head_row = title_row + 1
    for i, h in enumerate(headers, 1):
        _safe_write(ws, head_row, i, h)
    _style_range(ws, head_row, 1, head_row, len(headers),
                 font=Font(name=FONT_ROSTER, size=16, bold=True),
                 fill=_FILL_HEAD, border=_BORDER_MED)
    ws.row_dimensions[head_row].height = 37.0
    row = head_row + 1
    for item in roster.get("items") or []:
        vals = [item.get("seq"), item.get("interview_date"), item.get("unit"),
                item.get("name"), item.get("school"), item.get("edu_level"),
                item.get("interview_mode"), item.get("phone"), item.get("verdict"),
                item.get("offer_sent"), item.get("offer_sent_at"),
                item.get("sign_status"), item.get("signed_at"),
                item.get("grad_date"), item.get("onboard_status"), item.get("note")]
        for c, v in enumerate(vals, 1):
            _safe_write(ws, row, c, v if v not in (None, "") else EMPTY)
        for c in range(1, len(headers) + 1):
            cell = ws.cell(row=row, column=c)
            cell.font = Font(name=FONT_ROSTER, size=12)
            cell.alignment = _CENTER
            cell.border = _BORDER
        ws.cell(row=row, column=8).number_format = "@"      # 电话强制文本
        ws.row_dimensions[row].height = 20.25
        row += 1
    _set_widths(ws, [6.4, 12, 12, 10, 18, 8, 10, 15, 8, 8, 12, 10, 12, 12, 10, 24])
    ws.freeze_panes = f"A{head_row + 1}"
    ws.sheet_view.showGridLines = False
    ws.page_setup.orientation = "landscape"


def flat_workbook(progress: dict) -> bytes:
    year = progress.get("year") or {}
    label = year.get("label") or "招聘进度"
    headers = ["届别", "起止日期", "序号", "单位", "学历", "上会审批需求计划数量",
               "追加计划数", "转上年度计划数", "应取消计划数", "应招总数",
               "已签人数", "签约缺口", "完成率", "签约人", "备注"]
    wb = Workbook()
    ws = wb.active
    ws.title = (f"{label}招聘进度（平铺）")[:31]
    for i, h in enumerate(headers, 1):
        _safe_write(ws, 1, i, h)
    _style_range(ws, 1, 1, 1, len(headers),
                 font=Font(name=FONT_CN, size=11, bold=True), border=_BORDER_MED)
    row = 2
    for i, r in enumerate(progress.get("rows") or [], 1):
        vals = [label, f"{year.get('period_start')} 至 {year.get('period_end')}",
                i, r.get("unit_name"), r.get("edu_level"), r.get("plan_approved"),
                r.get("plan_extra"), r.get("plan_carryover"), r.get("plan_cancel"),
                r.get("should_hire"), r.get("signed_count"), r.get("gap"),
                None,
                "、".join(s.get("name") or "" for s in (r.get("signees") or [])),
                r.get("note") or ""]
        for c, v in enumerate(vals, 1):
            _safe_write(ws, row, c, v if v not in (None, "") else EMPTY)
            cell = ws.cell(row=row, column=c)
            cell.font = Font(name=FONT_CN, size=11)
            cell.alignment = _CENTER
            cell.border = _BORDER
        _write_rate(ws, row, 13, r.get("rate"))
        row += 1
    _set_widths(ws, [10, 22, 6, 16, 8, 14, 10, 12, 10, 9, 9, 9, 9, 32, 20])
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:O{max(1, row - 1)}"
    ws.sheet_view.showGridLines = False
    row += 1
    _safe_write(ws, row, 1, _meta(f"{label}招聘进度（平铺）"))
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=len(headers))
    ws.cell(row=row, column=1).font = Font(name=FONT_CN, size=9, color="86909C")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def roster_workbook(roster: dict) -> bytes:
    year = roster.get("year") or {}
    label = year.get("label") or "面试名单"
    wb = Workbook()
    ws = wb.active
    ws.title = (f"{label}面试总名单")[:31]
    _fill_roster_sheet(ws, roster, label, year.get("period_start") or "",
                       year.get("period_end") or "")
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
