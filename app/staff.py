"""在职人员档案（v5.2）：招聘之后的员工主数据。

边界：
- 招聘阶段仍以 candidates/applications 为准；
- `onboard_status` 变为「已入职」时，由本模块**幂等**生成 employees 记录；
- 生成后在职信息以 employees 为准，不再让招聘页和员工页两处同时改。
"""
from __future__ import annotations

import io
import json
import os
import re
from datetime import datetime

from . import crypto, db

EMPLOYEE_STATUSES = ("试用期", "在职", "离职", "待确认")
ACTIVE_STATUSES = ("试用期", "在职")

#: 可从 API 写入的普通字段（密文字段单独处理）。
PLAIN_FIELDS = (
    "employee_no", "name", "former_name", "avatar_path", "gender", "birth_date",
    "ethnicity", "political_status", "marital_status", "native_place",
    "address", "household_address", "health_status",
    "emergency_contact", "emergency_relation",
    "edu_level", "school", "major", "graduation_date", "work_start_date",
    "professional_title", "foreign_language", "computer_skill",
    "personnel_file_location", "self_evaluation",
    "unit", "department", "position", "job_level",
    "direct_manager", "work_location", "employment_type", "employment_status",
    "onboard_date", "probation_end_date", "regular_date", "leave_date",
    "contract_no", "contract_start", "contract_end", "source", "note",
)
JSON_FIELDS = ("education_json", "work_history_json", "family_json",
               "certificates_json", "extra_json")
ENCRYPTED_FIELDS = {
    "phone": "phone_enc",
    "email": "email_enc",
    "id_card": "id_card_enc",
    "emergency_phone": "emergency_phone_enc",
}
#: 外键字段只允许创建时写入，普通编辑不改归属。
LINK_FIELDS = ("candidate_id", "application_id")
_DATE_FIELDS = {
    "birth_date", "graduation_date", "work_start_date", "onboard_date",
    "probation_end_date", "regular_date", "leave_date", "contract_start",
    "contract_end",
}

#: 导入/导出的中文列名（顺序即模板列序）。
IMPORT_COLUMNS = [
    ("工号", "employee_no"), ("姓名", "name"), ("曾用名", "former_name"),
    ("性别", "gender"), ("出生日期", "birth_date"), ("民族", "ethnicity"),
    ("政治面貌", "political_status"), ("婚姻状况", "marital_status"),
    ("籍贯", "native_place"), ("手机", "phone"), ("邮箱", "email"),
    ("身份证号", "id_card"), ("现住址", "address"),
    ("户籍地址", "household_address"), ("健康状况", "health_status"),
    ("紧急联系人", "emergency_contact"), ("与本人关系", "emergency_relation"),
    ("紧急联系电话", "emergency_phone"),
    ("学历", "edu_level"), ("毕业院校", "school"), ("专业", "major"),
    ("毕业时间", "graduation_date"), ("参加工作时间", "work_start_date"),
    ("职称", "professional_title"), ("外语水平", "foreign_language"),
    ("计算机水平", "computer_skill"), ("档案所在地", "personnel_file_location"),
    ("单位", "unit"), ("部门", "department"), ("岗位", "position"),
    ("职级", "job_level"), ("直属上级", "direct_manager"),
    ("工作地点", "work_location"), ("用工形式", "employment_type"),
    ("人员状态", "employment_status"), ("入职日期", "onboard_date"),
    ("试用期结束", "probation_end_date"), ("转正日期", "regular_date"),
    ("离职日期", "leave_date"), ("合同编号", "contract_no"),
    ("合同开始", "contract_start"), ("合同结束", "contract_end"),
    ("来源", "source"), ("备注", "note"),
]
_HEADER_TO_FIELD = {h: f for h, f in IMPORT_COLUMNS}


def _s(v, n: int = 200) -> str:
    return str(v or "").strip()[:n]


def _norm_date(v) -> str | None:
    """日期统一成 YYYY-MM-DD；空值变 NULL，非法值直接报错。

    接受 2026-11-10 / 2026/11/10 / 2026.11.10 / 2026年11月10日；
    不接受只写年份——那会变成“看起来合法、实际不知道哪一天”的假日期。
    """
    s = str(v or "").strip()
    if not s:
        return None
    m = re.fullmatch(
        r"(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?", s)
    if not m:
        raise ValueError(f"日期格式应为 YYYY-MM-DD：{s}")
    try:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return datetime(y, mo, d).strftime("%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"日期不合法：{s}") from exc


def _norm_month(v) -> str | None:
    """教育/工作经历的起止时间只到月：YYYY-MM（也接受完整日期取年月）。"""
    s = str(v or "").strip()
    if not s:
        return None
    m = re.search(r"(\d{4})\s*[-/.年]\s*(\d{1,2})", s)
    if not m:
        raise ValueError(f"月份格式应为 YYYY-MM：{s}")
    y, mo = int(m.group(1)), int(m.group(2))
    if not 1 <= mo <= 12:
        raise ValueError(f"月份不合法：{s}")
    return f"{y:04d}-{mo:02d}"


def _mask_phone(v: str) -> str:
    v = str(v or "")
    return (v[:3] + "*" * max(4, len(v) - 7) + v[-4:]) if len(v) >= 7 else v


def _mask_email(v: str) -> str:
    v = str(v or "")
    if "@" not in v:
        return v
    a, b = v.split("@", 1)
    return (a[:1] + "***@" + b) if a else v


def _mask_id(v: str) -> str:
    v = str(v or "")
    return (v[:4] + "*" * max(6, len(v) - 8) + v[-4:]) if len(v) >= 10 else v


def _audit_safe(data: dict) -> dict:
    """审计里绝不落明文手机号/邮箱/身份证。"""
    out = dict(data or {})
    for k in ("phone", "email", "id_card", "emergency_phone"):
        if k in out:
            out[k] = (_mask_phone(out[k]) if k in ("phone", "emergency_phone")
                      else (_mask_email(out[k]) if k == "email" else _mask_id(out[k])))
    return out


def _present(row: dict, *, sensitive: bool = False) -> dict:
    out = dict(row or {})
    phone = crypto.decrypt(out.get("phone_enc")) or ""
    email = crypto.decrypt(out.get("email_enc")) or ""
    id_card = crypto.decrypt(out.get("id_card_enc")) or ""
    em_phone = crypto.decrypt(out.get("emergency_phone_enc")) or ""
    out["phone"] = phone if sensitive else _mask_phone(phone)
    out["email"] = email if sensitive else _mask_email(email)
    out["id_card"] = id_card if sensitive else _mask_id(id_card)
    out["emergency_phone"] = em_phone if sensitive else _mask_phone(em_phone)
    for k in ("phone_enc", "email_enc", "id_card_enc", "id_card_bidx",
              "emergency_phone_enc"):
        out.pop(k, None)
    for k in JSON_FIELDS:
        v = out.get(k)
        if isinstance(v, str):
            try:
                v = json.loads(v)
            except (TypeError, ValueError):
                v = {} if k == "extra_json" else []
        out[k] = v if isinstance(v, (list, dict)) else ({} if k == "extra_json" else [])
    return out


def _employee_no_taken(conn, eid: int, no: str) -> bool:
    if not no:
        return False
    return bool(conn.execute(
        "SELECT 1 FROM employees WHERE employee_no=? AND id<>?",
        (no, int(eid))).fetchone())


def get_employee_by_application(conn, aid: int) -> dict | None:
    row = conn.execute("SELECT * FROM employees WHERE application_id=?",
                       (int(aid),)).fetchone()
    return _present(dict(row)) if row else None


def get_employee(conn, eid: int, *, sensitive: bool = False) -> dict | None:
    row = conn.execute("SELECT * FROM employees WHERE id=?", (int(eid),)).fetchone()
    return _present(dict(row), sensitive=sensitive) if row else None


def _normalize_json_value(field: str, value) -> object:
    if field == "extra_json":
        return value if isinstance(value, dict) else {}
    rows = value if isinstance(value, list) else []
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        row = dict(row)
        for k in ("start", "end", "date"):
            if row.get(k):
                row[k] = _norm_month(row[k]) if k in ("start", "end") else _norm_date(row[k])
        out.append(row)
    return out


def _insert_employee(conn, data: dict) -> int:
    cols, vals = [], []
    for field in list(PLAIN_FIELDS) + list(LINK_FIELDS):
        if field in data:
            cols.append(field)
            if field in LINK_FIELDS:
                vals.append(int(data[field]) if data.get(field) else None)
            else:
                vals.append(data.get(field))
    for field, col in ENCRYPTED_FIELDS.items():
        if field in data:
            cols.append(col)
            vals.append(crypto.encrypt(_s(data.get(field), 200)) if data.get(field) else None)
    if data.get("id_card"):
        cols.append("id_card_bidx")
        vals.append(crypto.blind_index(_s(data.get("id_card"), 40)))
    for field in JSON_FIELDS:
        if field in data:
            cols.append(field)
            vals.append(json.dumps(_normalize_json_value(field, data.get(field)),
                                   ensure_ascii=False))
    now = db.now()
    cols += ["created_at", "updated_at", "created_by", "updated_by"]
    vals += [now, now, data.get("_operator") or "HR", data.get("_operator") or "HR"]
    sql = "INSERT INTO employees (" + ",".join(cols) + ") VALUES (" + \
          ",".join("?" for _ in cols) + ")"
    cur = conn.execute(sql, vals)
    return int(cur.lastrowid)


def ensure_employee_from_application(conn, aid: int, operator: str = "HR",
                                     role: str = "hr") -> dict | None:
    """投递变为「已入职」时调用；按 application_id 幂等。"""
    old = conn.execute("SELECT * FROM employees WHERE application_id=?",
                       (int(aid),)).fetchone()
    if old:
        return _present(dict(old))
    row = conn.execute(
        """SELECT a.*, c.name, c.gender, c.phone_enc, c.email_enc,
                  c.edu_level, c.school, c.major, c.birth_year,
                  c.avatar_path AS candidate_avatar_path,
                  j.title AS job_title, j.dept AS job_dept
           FROM applications a
           LEFT JOIN candidates c ON c.id = a.candidate_id
           LEFT JOIN jobs j ON j.id = a.job_id
           WHERE a.id=?""", (int(aid),)).fetchone()
    if not row:
        return None
    a = dict(row)
    data = {
        # 工号由人事手工填写/Excel 导入，不由系统自动生成。
        "candidate_id": a.get("candidate_id"),
        "application_id": aid,
        "name": _s(a.get("name"), 80),
        # 人才库已有证件照就同步过来；没有也不影响建档，HR 可手动上传。
        "avatar_path": a.get("candidate_avatar_path") or "",
        "gender": _s(a.get("gender"), 8),
        # 人才库只存出生年份，不能冒充完整出生日期；留空由 HR 补全。
        "birth_date": "",
        "phone_enc": a.get("phone_enc"),
        "email_enc": a.get("email_enc"),
        "edu_level": _s(a.get("edu_level"), 20),
        "school": _s(a.get("school"), 120),
        "major": _s(a.get("major"), 120),
        "unit": _s(a.get("hire_unit") or a.get("job_dept"), 80),
        "department": _s(a.get("job_dept"), 80),
        "position": _s(a.get("job_title"), 80),
        "employment_status": "试用期",
        "onboard_date": datetime.now().strftime("%Y-%m-%d"),
        "source": _s(a.get("channel"), 30) or "招聘转入",
        "_operator": operator,
    }
    # phone_enc/email_enc 是原样密文，不能走 _insert_employee 的明文字段加密。
    for k in ("phone_enc", "email_enc"):
        data.pop(k, None)
        data[k.replace("_enc", "")] = crypto.decrypt(a.get(k)) or ""
    eid = _insert_employee(conn, data)
    if a.get("candidate_id"):
        conn.execute("UPDATE candidates SET pool_status='已入职', updated_at=? WHERE id=?",
                     (db.now(), int(a["candidate_id"])))
    db.add_audit(conn, "employee", str(eid), "create", "",
                 f"从投递 #{aid} 转入在职人员档案", operator, role)
    conn.commit()
    return get_employee(conn, eid)


def create_employee(conn, data: dict, operator: str = "HR", role: str = "hr") -> dict:
    name = _s(data.get("name"), 80)
    if not name:
        raise ValueError("姓名不能为空")
    if data.get("application_id"):
        old = conn.execute("SELECT * FROM employees WHERE application_id=?",
                           (int(data["application_id"]),)).fetchone()
        if old:
            return _present(dict(old))
    clean = {k: (_s(data.get(k), 500) if k not in ENCRYPTED_FIELDS else data.get(k))
             for k in list(PLAIN_FIELDS) + list(LINK_FIELDS) +
             list(ENCRYPTED_FIELDS) if k in data}
    for k in JSON_FIELDS:
        if k in data:
            clean[k] = data[k]
    clean["name"] = name
    clean.setdefault("employment_status", "试用期")
    if clean["employment_status"] not in EMPLOYEE_STATUSES:
        raise ValueError("人员状态不合法")
    for f in _DATE_FIELDS:
        if f in clean:
            clean[f] = _norm_date(clean[f])
    # 工号不自动生成：留空就是 NULL，等 HR 在档案或 Excel 里手工填。
    clean["employee_no"] = _s(clean.get("employee_no"), 40) or None
    if clean["employee_no"] and conn.execute(
            "SELECT 1 FROM employees WHERE employee_no=?",
            (clean["employee_no"],)).fetchone():
        raise ValueError("工号已存在")
    clean["_operator"] = operator
    eid = _insert_employee(conn, clean)
    db.add_audit(conn, "employee", str(eid), "create", "",
                 f"新建在职人员：{name}", operator, role)
    conn.commit()
    return get_employee(conn, eid)


def update_employee(conn, eid: int, data: dict, operator: str = "HR",
                    role: str = "hr") -> dict:
    old = conn.execute("SELECT * FROM employees WHERE id=?", (int(eid),)).fetchone()
    if not old:
        raise KeyError("未找到该在职人员")
    old = dict(old)
    if "employment_status" in data and data["employment_status"] not in EMPLOYEE_STATUSES:
        raise ValueError("人员状态不合法")
    if "name" in data and not _s(data.get("name"), 80):
        raise ValueError("姓名不能为空")
    sets, vals = [], []
    for field in PLAIN_FIELDS:
        if field in data:
            sets.append(field + "=?")
            vals.append(_s(data.get(field), 500) if field not in _DATE_FIELDS
                        else _norm_date(data.get(field)))
    for field, col in ENCRYPTED_FIELDS.items():
        if field in data:
            sets.append(col + "=?")
            vals.append(crypto.encrypt(_s(data.get(field), 200)) if data.get(field) else None)
            if field == "id_card":
                sets.append("id_card_bidx=?")
                vals.append(crypto.blind_index(_s(data.get("id_card"), 40))
                            if data.get("id_card") else None)
    if "employee_no" in data:
        # 清空工号 = 回到 NULL；空字符串会触发 UNIQUE 冲突（多人留空时）。
        try:
            idx = sets.index("employee_no=?")
            vals[idx] = _s(data.get("employee_no"), 40) or None
        except ValueError:
            pass
        new_no = _s(data.get("employee_no"), 40)
        if new_no and _employee_no_taken(conn, eid, new_no):
            raise ValueError("工号已存在")
    for field in JSON_FIELDS:
        if field in data:
            sets.append(field + "=?")
            vals.append(json.dumps(_normalize_json_value(field, data.get(field)),
                                   ensure_ascii=False))
    if str(data.get("employment_status") or "") == "离职" and not data.get("leave_date"):
        sets.append("leave_date=?")
        vals.append(datetime.now().strftime("%Y-%m-%d"))
    if not sets:
        return _present(old, sensitive=True)
    sets += ["updated_at=?", "updated_by=?"]
    vals += [db.now(), operator, int(eid)]
    conn.execute("UPDATE employees SET " + ",".join(sets) + " WHERE id=?", vals)
    db.add_audit(conn, "employee", str(eid), "update",
                 f"更新前：{json.dumps(_audit_safe({k: old.get(k) for k in data}), ensure_ascii=False)}",
                 f"更新后：{json.dumps(_audit_safe(data), ensure_ascii=False)}", operator, role)
    conn.commit()
    return get_employee(conn, eid, sensitive=True)


def list_employees(conn, *, q: str = "", status: str = "",
                   unit: str = "", department: str = "", year: str = "",
                   page: int = 1, page_size: int = 30) -> dict:
    where = ["COALESCE(archived_at,'')=''"]
    args: list = []
    status = _s(status, 20)
    if status in EMPLOYEE_STATUSES:
        where.append("employment_status=?")
        args.append(status)
    else:
        where.append("employment_status IN (?,?)")
        args += list(ACTIVE_STATUSES)
    q = _s(q, 80)
    if q:
        where.append("(name LIKE ? OR employee_no LIKE ? OR unit LIKE ? "
                     "OR department LIKE ? OR position LIKE ?)")
        args += [f"%{q}%"] * 5
    if unit:
        where.append("unit=?")
        args.append(_s(unit, 80))
    if department:
        where.append("department=?")
        args.append(_s(department, 80))
    if year:
        where.append("substr(onboard_date,1,4)=?")
        args.append(_s(year, 4))
    where_sql = " WHERE " + " AND ".join(where)
    total = conn.execute("SELECT COUNT(*) AS n FROM employees" + where_sql,
                         args).fetchone()["n"]
    page_size = max(1, min(int(page_size or 30), 500))
    page = max(1, int(page or 1))
    rows = conn.execute(
        "SELECT e.*, (SELECT COUNT(*) FROM employee_documents d "
        "WHERE d.employee_id=e.id) AS documents_count, "
        "(SELECT COUNT(*) FROM employee_documents d WHERE d.employee_id=e.id "
        " AND d.ocr_status='已识别' AND d.review_status='待复核') AS ocr_pending_count "
        "FROM employees e" + where_sql +
        " ORDER BY COALESCE(e.onboard_date,'') DESC, e.id DESC LIMIT ? OFFSET ?",
        args + [page_size, (page - 1) * page_size]).fetchall()
    return {"items": [_present(dict(r)) for r in rows],
            "total": int(total), "page": page, "page_size": page_size}


def list_all_employees(conn, *, q: str = "", status: str = "",
                       unit: str = "", department: str = "", year: str = "") -> list[dict]:
    res = list_employees(conn, q=q, status=status, unit=unit,
                         department=department, year=year,
                         page=1, page_size=500)
    # 500 是分页上限；导出需要全量时再取余下页
    out = list(res["items"])
    page = 2
    while len(out) < res["total"]:
        nxt = list_employees(conn, q=q, status=status, unit=unit,
                             department=department, year=year,
                             page=page, page_size=500)
        if not nxt["items"]:
            break
        out += nxt["items"]
        page += 1
    return out


def _document_present(row: dict) -> dict:
    out = dict(row or {})
    out["ocr_json"] = out.get("ocr_json") or {}
    out["extracted_json"] = out.get("extracted_json") or {}
    return out


def list_documents(conn, eid: int) -> list[dict]:
    # `ocr_json` 是给证据定位/未来服务用的，档案详情不需要整包下发；
    # OCR 原文也只带前 2 万字，避免一份扫描件把接口响应撑到几 MB。
    rows = conn.execute(
        """SELECT id, employee_id, doc_type, file_hash, file_name, file_path,
                  mime, size, ocr_status, ocr_engine,
                  substr(COALESCE(ocr_text,''),1,20000) AS ocr_text,
                  extracted_json, review_status, created_at, updated_at
           FROM employee_documents WHERE employee_id=? ORDER BY id DESC""",
        (int(eid),)).fetchall()
    return [_document_present(dict(r)) for r in rows]


def get_document(conn, did: int) -> dict | None:
    row = conn.execute("SELECT * FROM employee_documents WHERE id=?",
                       (int(did),)).fetchone()
    return _document_present(dict(row)) if row else None


def add_document(conn, eid: int, *, filename: str, path: str, mime: str,
                 size: int, file_hash: str, doc_type: str = "other") -> dict:
    now = db.now()
    cur = conn.execute(
        """INSERT INTO employee_documents
           (employee_id, doc_type, file_hash, file_name, file_path, mime, size,
            ocr_status, review_status, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        (int(eid), _s(doc_type, 30) or "other", file_hash, _s(filename, 200),
         path, _s(mime, 80), int(size), "待识别", "待复核", now, now))
    conn.commit()
    return get_document(conn, int(cur.lastrowid))


def update_document_ocr(conn, did: int, *, status: str, engine: str = "",
                        text: str = "", ocr_json: dict | None = None,
                        extracted: dict | None = None) -> dict | None:
    conn.execute(
        """UPDATE employee_documents SET ocr_status=?, ocr_engine=?, ocr_text=?,
           ocr_json=?, extracted_json=?, updated_at=? WHERE id=?""",
        (status, engine, text, json.dumps(ocr_json or {}, ensure_ascii=False),
         json.dumps(extracted or {}, ensure_ascii=False), db.now(), int(did)))
    conn.commit()
    return get_document(conn, did)


def mark_document_review(conn, did: int, status: str = "已应用") -> None:
    conn.execute("UPDATE employee_documents SET review_status=?, updated_at=? WHERE id=?",
                 (status, db.now(), int(did)))
    conn.commit()


def export_rows(conn, filters: dict | None = None) -> tuple[list[str], list[list], set[int], int]:
    filters = filters or {}
    items = list_all_employees(conn, **filters)
    headers = [h for h, _ in IMPORT_COLUMNS]
    rows = []
    for i, e in enumerate(items, 1):
        row = []
        for _, field in IMPORT_COLUMNS:
            v = e.get(field, "")
            row.append("" if v is None else v)
        rows.append(row)
    text_cols = {i for i, (h, f) in enumerate(IMPORT_COLUMNS)
                 if f in ("employee_no", "phone", "id_card", "emergency_phone",
                          "contract_no")}
    return headers, rows, text_cols, len(rows)


# ---------------------------------------------------------------- Excel 导入
def parse_import_xlsx(data: bytes) -> dict:
    from openpyxl import load_workbook
    wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    ws = wb.active
    raw_rows = list(ws.iter_rows(values_only=True))
    if not raw_rows:
        raise ValueError("Excel 是空的")
    headers = [str(v or "").strip() for v in raw_rows[0]]
    fields = [_HEADER_TO_FIELD.get(h) for h in headers]
    if "name" not in fields:
        raise ValueError("表头缺少「姓名」列")
    rows = []
    for ridx, raw in enumerate(raw_rows[1:], start=2):
        if not any(str(v or "").strip() for v in raw):
            continue
        rec: dict = {"_row": ridx}
        errors: list[str] = []
        for i, field in enumerate(fields):
            if not field:
                continue
            v = raw[i] if i < len(raw) else ""
            if isinstance(v, datetime):
                v = v.strftime("%Y-%m-%d")
            rec[field] = str(v or "").strip()
        if not rec.get("name"):
            errors.append("姓名不能为空")
        for f in _DATE_FIELDS:
            if rec.get(f):
                try:
                    rec[f] = _norm_date(rec[f])
                except ValueError as exc:
                    errors.append(str(exc))
        if rec.get("employment_status") and rec["employment_status"] not in EMPLOYEE_STATUSES:
            errors.append("人员状态不合法")
        rec["_errors"] = errors
        rows.append(rec)
    return {"headers": headers, "fields": fields, "rows": rows,
            "total": len(rows), "valid": sum(1 for r in rows if not r["_errors"])}


def annotate_import_duplicates(conn, parsed: dict) -> dict:
    for rec in parsed.get("rows") or []:
        dup = []
        if rec.get("employee_no"):
            if conn.execute("SELECT 1 FROM employees WHERE employee_no=?",
                            (rec["employee_no"],)).fetchone():
                dup.append("工号已存在")
        if rec.get("id_card"):
            bidx = crypto.blind_index(rec["id_card"])
            if bidx and conn.execute("SELECT 1 FROM employees WHERE id_card_bidx=?",
                                     (bidx,)).fetchone():
                dup.append("身份证号已存在")
        rec["_duplicates"] = dup
    return parsed


def commit_import(conn, rows: list[dict], operator: str, role: str) -> dict:
    ok = skipped = 0
    errors = []
    for rec in rows:
        if rec.get("_errors") or rec.get("_duplicates"):
            skipped += 1
            continue
        try:
            create_employee(conn, rec, operator, role)
            ok += 1
        except Exception as exc:                             # noqa: BLE001
            skipped += 1
            errors.append(f"第 {rec.get('_row')} 行：{exc}")
    return {"created": ok, "skipped": skipped, "errors": errors[:30]}
