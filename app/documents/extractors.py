"""通用文档抽取器：键值、表格、正则、Profile 驱动。

这里不认识“员工入职登记表”这个具体业务，只认识 Profile：
- fields：标签别名、字段类型、是否必填、是否续行；
- arrays：section 标记、表头别名、列映射；
- table_splits：例如“部门岗位=部门/岗位”；
- derive：例如用最高教育经历回填学校/专业。
"""
from __future__ import annotations

import re
from datetime import datetime

from .profiles import ArraySpec, DocumentProfile, FieldSpec

_DATE_RE = re.compile(r"(19|20)\d{2}\s*[-年./]\s*\d{1,2}\s*[-月./]\s*\d{1,2}\s*日?")
_PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d(?:[-\s]?\d){8}(?!\d)")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_ID_RE = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
_PHOTO_NOISE = {"一寸免冠", "照片", "（粘贴", "处）", "（粘贴处）"}


def _clean(text: str) -> str:
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("　", " ")
    text = re.sub(r"学\s*历\s*/\s*学\s*\n\s*位", "学历/ 学位", text)
    text = re.sub(r"紧\s*急\s*联\s*系\s*\n\s*人", "紧急联系人", text)
    text = re.sub(r"月\s*薪\s*（\s*税\s*\n\s*前\s*）", "月薪（税前）", text)
    return re.sub(r"[ \t]+", " ", text)


def _date(raw: str) -> str:
    s0 = re.sub(r"(\d{4})\s+(\d{1,2})\s+(\d{1,2})", r"\1-\2-\3",
                str(raw or ""))
    m = _DATE_RE.search(s0)
    if not m:
        return ""
    s = m.group(0).replace("年", "-").replace("月", "-").replace("日", "")
    s = s.replace("/", "-").replace(".", "-").replace(" ", "")
    try:
        y, mo, d = [int(x) for x in s.split("-")[:3]]
        return datetime(y, mo, d).strftime("%Y-%m-%d")
    except (ValueError, IndexError):
        return ""


def _month(raw: str) -> str:
    s = str(raw or "")
    m = re.search(r"(19|20)\d{2}\s*[-/.年]?\s*\d{1,2}", s)
    if not m:
        return ""
    part = re.sub(r"\s+", "-", m.group(0).replace("年", "-")
                  .replace(".", "-").replace("/", "-"))
    nums = [p for p in re.split(r"-+", part) if p]
    if len(nums) < 2:
        return ""
    y, mo = int(nums[0]), int(nums[1])
    if not 1 <= mo <= 12:
        return ""
    return f"{y:04d}-{mo:02d}"


def _date_range(v: str) -> tuple[str, str]:
    s = str(v or "").replace("\n", " ")
    vals = [m.group(0) for m in re.finditer(
        r"(19|20)\d{2}\s*[-/.年]?\s*\d{1,2}", s)]
    if len(vals) >= 2:
        return _month(vals[0]), _month(vals[1])
    return "", ""


def _cell(v) -> str:
    s = str(v or "").replace("\n", " ")
    s = re.sub(r"\s+", " ", s).strip()
    s = re.sub(r"(?<=[一-龥])\s+(?=[一-龥])", "", s)
    s = re.sub(r"\s*([（(）)【】])\s*", r"\1", s)
    return re.sub(r"\s+", "", s) if len(s) <= 12 else s


def _norm_label(v: str) -> str:
    return re.sub(r"\s+", "", str(v or ""))


def _line_value(text: str, labels: tuple[str, ...], known_labels: set[str],
                maxlen: int = 120, join_next: bool = False) -> tuple[str, str]:
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    label_only = {_norm_label(x) for x in known_labels} | {
        "登记表编号", "填表日期", "档案编号", "一、基本信息", "二、入职信息",
        "三、教育经历", "四、工作经历", "五、家庭成员", "六、签字与审核",
        "公民身份号码", "签发机关", "有效期限", "邮政编码", "出生", "性别",
        "民族", "住址", "姓", "名", "性", "别", "职", "务", "关", "系",
    }

    def looks_label(s: str) -> bool:
        t = _norm_label(s)
        return (t in label_only
                or any(t.startswith(x) for x in label_only if len(x) >= 2)
                or bool(re.match(r"^[一-龥]{2,10}\s*[：:]", s)))

    for i, s in enumerate(lines):
        for label in labels:
            m = re.match(r"^\s*" + re.escape(label) + r"\s*(?:[：:]\s*|\s+)", s)
            if m:
                tail = re.split(r"\s{2,}|[，,；;]", s[m.end():], 1)[0].strip()
                if tail and tail not in _PHOTO_NOISE:
                    if (tail.endswith(("-", "至", "~", "—"))
                            and i + 1 < len(lines)):
                        tail += " " + lines[i + 1].lstrip("）) ")
                    if (join_next and i + 1 < len(lines)
                            and not looks_label(lines[i + 1])):
                        tail += " " + lines[i + 1]
                    return tail[:maxlen], s[:160]
                continue
            base = _norm_label(s.rstrip("：: "))
            if base != _norm_label(label) and not base.startswith(_norm_label(label) + "/"):
                continue
            for j, nxt in enumerate(lines[i + 1:i + 4], start=i + 1):
                if nxt in _PHOTO_NOISE or looks_label(nxt):
                    continue
                if (nxt.endswith(("-", "至", "~", "—"))
                        and j + 1 < len(lines)):
                    nxt += " " + lines[j + 1].lstrip("）) ")
                if (join_next and j + 1 < len(lines)
                        and not looks_label(lines[j + 1])):
                    nxt += " " + lines[j + 1]
                return nxt[:maxlen], (s + " / " + nxt)[:160]
    return "", ""


def _add(out: dict, key: str, value, evidence: str = "",
         confidence: float = 0.75) -> None:
    value = str(value or "").strip()
    if not value:
        return
    old = out.get(key)
    if old and float(old.get("confidence") or 0) >= confidence:
        return
    out[key] = {"value": value, "confidence": confidence,
                "evidence": str(evidence or value)[:200]}


def _normalize(spec: FieldSpec, value: str) -> str:
    v = str(value or "").strip()
    if not v:
        return ""
    if spec.type == "date":
        return _date(v)
    if spec.type == "month":
        return _month(v)
    if spec.type == "phone":
        digits = re.sub(r"\D", "", v)
        return digits if len(digits) >= 7 else ""
    if spec.type == "email":
        m = _EMAIL_RE.search(v)
        return m.group(0) if m else ""
    if spec.type == "id_card":
        m = _ID_RE.search(v)
        return m.group(0).upper() if m else ""
    if spec.type == "enum":
        return v if not spec.values or v in spec.values else ""
    if spec.type == "level":
        for level in (spec.values or ("博士", "硕士", "本科", "大专", "专科", "中专", "高中")):
            if level in v:
                return level
        return ""
    for pattern in spec.patterns:
        try:
            m = re.search(pattern, v)
        except re.error:
            continue
        if m:
            return m.group(0)
    return v


def _regex_fallback(spec: FieldSpec, text: str) -> str:
    if spec.type == "phone":
        m = _PHONE_RE.search(text)
        return re.sub(r"\D", "", m.group(0)) if m else ""
    if spec.type == "email":
        m = _EMAIL_RE.search(text)
        return m.group(0) if m else ""
    if spec.type == "id_card":
        m = _ID_RE.search(text)
        return m.group(0).upper() if m else ""
    return ""


def _derive_id(fields: dict, mapping: dict) -> None:
    idv = (fields.get("id_card") or {}).get("value") or ""
    if len(idv) < 18:
        return
    born = mapping.get("birth_date")
    gend = mapping.get("gender")
    try:
        if born:
            _add(fields, born,
                 datetime.strptime(idv[6:14], "%Y%m%d").strftime("%Y-%m-%d"),
                 idv[6:14], 0.98)
        if gend:
            _add(fields, gend, "男" if int(idv[16]) % 2 else "女",
                 idv[16], 0.95)
    except (ValueError, IndexError):
        pass


def _field_map(profile: DocumentProfile) -> dict[str, FieldSpec]:
    out: dict[str, FieldSpec] = {}
    for f in profile.fields:
        for label in f.labels:
            out[_norm_label(label)] = f
    return out


def _split_value(raw: str, delimiter: str) -> list[str]:
    if not delimiter:
        return [raw]
    return [x.strip() for x in re.split(re.escape(delimiter), raw)]


def _table_fields(path: str, profile: DocumentProfile) -> tuple[dict, dict]:
    """从 PDF 表格恢复字段/数组；单表多 section 也支持。"""
    import pymupdf

    fields: dict[str, dict] = {}
    arrays: dict[str, list] = {a.key: [] for a in profile.arrays}
    field_map = _field_map(profile)
    split_map = {_norm_label(x.get("label")): x
                 for x in profile.table_splits if x.get("label")}
    doc = pymupdf.open(path)
    try:
        for page in doc:
            try:
                tables = page.find_tables().tables
            except Exception:                                # noqa: BLE001
                continue
            for table in tables:
                rows = table.extract() or []
                if not rows:
                    continue
                section = ""
                idx: dict[str, int] = {}
                single_cfg: dict | None = None
                single_idx: dict[str, int] = {}
                for row in rows:
                    vals = [_cell(c) for c in row]
                    joined = "".join(vals)
                    if not joined:
                        continue

                    if any(m in joined for m in profile.stop_markers) and not any(vals[1:]):
                        section, idx = "__stop__", {}
                        continue
                    # 0) 单行小表：如“紧急联系人 / 姓名 / 电话 / 地址”
                    single_hit = None
                    for sc in profile.table_singles:
                        if any(m in joined for m in (sc.get("markers") or []))                                 and not any(vals[1:]):
                            single_hit = sc
                            break
                    if single_hit:
                        section, single_cfg, single_idx = "__single__", single_hit, {}
                        continue
                    # 有些 PDF 把“紧急联系人”标题放上一页/上一表，当前表直接从表头开始。
                    if (not single_hit and section not in [a.key for a in profile.arrays]
                            and section != "__single__"):
                        for sc in profile.table_singles:
                            aliases = sc.get("header_aliases") or {}
                            if aliases and all(
                                    any(a in joined for a in als)
                                    for als in aliases.values()):
                                single_hit = sc
                                section, single_cfg, single_idx = "__single__", sc, {}
                                break
                        if single_hit:
                            continue
                    if section == "__single__" and single_cfg:
                        aliases = single_cfg.get("header_aliases") or {}
                        hit = sum(1 for als in aliases.values()
                                  if any(a in joined for a in als))
                        if hit >= max(2, len(aliases) - 1):
                            for key, als in aliases.items():
                                single_idx[key] = next(
                                    (i for a in als for i, v in enumerate(vals)
                                     if a in v), -1)
                            continue
                        for src, dst in (single_cfg.get("fields") or {}).items():
                            i = single_idx.get(src, -1)
                            if i >= 0 and i < len(vals):
                                val = vals[i]
                                if src == "phone":
                                    val = re.sub(r"\D", "", val)
                                _add(fields, dst, val, "PDF 表格", 0.95)
                        continue
                    # 1) section 标题（整行只有标题）
                    marker_hit = False
                    for arr in profile.arrays:
                        if any(m in joined for m in arr.markers) and not any(vals[1:]):
                            section, idx = arr.key, {}
                            marker_hit = True
                            break
                    if marker_hit:
                        continue
                    else:
                        # 2) 数组表头：记录列索引
                        header_hit = None
                        for arr in profile.arrays:
                            aliases = [a for labels in arr.header_aliases.values()
                                       for a in labels]
                            # 必须命中足够多的表头列，避免把数据行里的“单位/职务”
                            # 误判成表头，导致 section 提前切换。
                            hits = sum(1 for key, als in arr.header_aliases.items()
                                       if any(a in joined for a in als))
                            need = max(2, len(arr.header_aliases) - 1)
                            if arr.key in ("education_json", "work_history_json")                                     and "起止时间" not in joined:
                                continue
                            if hits >= need:
                                header_hit = arr
                                break
                        if header_hit:
                            section = header_hit.key
                            idx = {}
                            for key, aliases in header_hit.header_aliases.items():
                                # 别名按声明顺序优先：“毕业专业”要先于“专业”，
                                # 否则会被“学历 / 专业”这一列抢走。
                                idx[key] = next(
                                    (i for a in aliases for i, v in enumerate(vals)
                                     if a in v), -1)
                            continue

                    # 3) 数组数据行
                    arr = next((a for a in profile.arrays if a.key == section), None)
                    if arr:
                        self_vals = vals
                        if not any(self_vals):
                            continue
                        item: dict = {}
                        for key, _label in arr.columns:
                            i = idx.get(key)
                            if i is None or i < 0 or i >= len(self_vals):
                                i = next((j for j, (k, _) in enumerate(arr.columns)
                                          if k == key), -1)
                            raw = self_vals[i] if 0 <= i < len(self_vals) else ""
                            if key in ("phone", "emergency_phone"):
                                raw = re.sub(r"\D", "", raw)
                            if key in arr.date_columns:
                                raw = (_month(raw) if arr.date_columns[key] == "month"
                                       else _date(raw))
                            if key in arr.split:
                                parts = _split_value(raw, arr.split[key])
                                raw = parts[0] if parts else ""
                            item[key] = raw
                        # 教育/工作的起止时间是同一个单元格时，从 start 里拆出 end
                        if arr.key in ("education_json", "work_history_json"):
                            start_raw = self_vals[idx.get("start", 0)] if idx.get("start", 0) >= 0 else ""
                            ds, de = _date_range(start_raw)
                            if ds:
                                item["start"] = ds
                            if de:
                                item["end"] = de
                        # 忽略全是空/表头的行
                        if any(item.values()):
                            arrays[arr.key].append(item)
                        continue

                    # 4) 基本信息行：标签/值可能一行多组
                    for j, cell in enumerate(vals):
                        nxt = vals[j + 1] if j + 1 < len(vals) else ""
                        spec = field_map.get(_norm_label(cell))
                        if spec and nxt:
                            val = _normalize(spec, nxt)
                            _add(fields, spec.key, val, "PDF 表格", 0.95)
                            continue
                        sm = split_map.get(_norm_label(cell))
                        if sm and nxt:
                            parts = _split_value(nxt, sm.get("delimiter") or "/")
                            for n, key in enumerate(sm.get("fields") or []):
                                if n < len(parts):
                                    _add(fields, key, parts[n], "PDF 表格", 0.9)
    finally:
        doc.close()
    arrays = {k: v for k, v in arrays.items() if v}
    # 用最高一段教育经历回填学校/专业（Profile 声明，不写死业务）
    for rule in profile.derive:
        arr = arrays.get(str(rule.get("from_array") or "")) or []
        if not arr:
            continue
        src = arr[-1] if rule.get("last") else arr[0]
        for dst, src_key in (rule.get("fields") or {}).items():
            _add(fields, str(dst), src.get(str(src_key), ""), "PDF 表格", 0.9)
    return fields, arrays


def extract_document(doc_type: str, text: str, filename: str = "",
                     pdf_path: str | None = None,
                     profile_id: str | None = None) -> dict:
    """Profile 驱动的统一入口。"""
    from .profiles import resolve_profile

    profile = resolve_profile(doc_type, filename, profile_id=profile_id)
    text = _clean(text)
    fields: dict[str, dict] = {}
    arrays: dict[str, list] = {}
    known = profile.known_labels
    warnings: list[str] = []

    for spec in profile.fields:
        v, ev = _line_value(text, spec.labels, known,
                            join_next=spec.join_next)
        val = _normalize(spec, v)
        if not val and spec.regex_fallback:
            val = _regex_fallback(spec, text)
        if val:
            _add(fields, spec.key, val, ev or val,
                 0.95 if spec.type == "id_card" else 0.82)

    if profile.derive_id_card:
        _derive_id(fields, profile.derive_id_card)

    # “合同期限：2026-11-16 至 2029-11-15”这类一个标签带两个日期
    for rule in profile.periods:
        label = str(rule.get("label") or "")
        if not label:
            continue
        v, ev = _line_value(text, (label,), known, join_next=False)
        dates = [m.group(0) for m in _DATE_RE.finditer(str(v or ""))]
        if dates and rule.get("start"):
            _add(fields, str(rule["start"]), _date(dates[0]), ev, 0.8)
        if len(dates) > 1 and rule.get("end"):
            _add(fields, str(rule["end"]), _date(dates[1]), ev, 0.8)

    if profile.table_enabled and pdf_path and str(pdf_path).lower().endswith(".pdf"):
        try:
            tf, ta = _table_fields(pdf_path, profile)
        except Exception as exc:                            # noqa: BLE001
            tf, ta = {}, {}
            warnings.append(f"PDF 表格识别失败，已退化为文本：{type(exc).__name__}")
        for k, v in tf.items():
            old = fields.get(k)
            if not old or float(v.get("confidence") or 0) >= float(old.get("confidence") or 0):
                fields[k] = v
        arrays.update(ta)

    # 只有紧急联系人电话、没有本人手机时，避免重复
    phone = (fields.get("phone") or {}).get("value")
    emg = (fields.get("emergency_phone") or {}).get("value")
    if phone and emg and phone == emg:
        fields.pop("phone", None)

    for spec in profile.fields:
        if spec.required and not fields.get(spec.key):
            warnings.append(f"缺少必填字段：{spec.label}")
    for spec in profile.fields:
        if spec.type in ("date", "month", "phone", "email", "id_card") and fields.get(spec.key):
            if not _normalize(spec, fields[spec.key].get("value", "")):
                warnings.append(f"字段格式可疑：{spec.label}")

    ui = profile.to_ui()
    return {
        "doc_type": profile.doc_type,
        "profile": ui,
        "profile_id": profile.id,
        "profile_name": profile.name,
        "engine": "profile",
        "fields": fields,
        "arrays": arrays,
        "field_labels": ui["field_labels"],
        "array_defs": ui["array_defs"],
        "warnings": warnings,
        "apply": profile.apply,
    }
