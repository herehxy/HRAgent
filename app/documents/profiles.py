"""文档类型 Profile：OCR 之后“怎么理解这份文件”的配置层。

Profile 不包含 OCR 引擎代码，也不直接写数据库。它只声明：
- 这类文档有哪些字段、标签别名、类型、是否必填；
- 有没有多行表格（教育/工作/家庭等）、表头别名、列到字段的映射；
- 表格里哪些标签需要拆成多个字段；
- 抽取后回填哪个业务 Adapter（employee / contract / ...）。

以后接第二个业务域，只需要新增一个 JSON profile + 一个 Adapter。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

from ..paths import BASE, CONFIG_DIR


@dataclass(frozen=True)
class FieldSpec:
    key: str
    labels: tuple[str, ...]
    type: str = "text"
    required: bool = False
    join_next: bool = False
    values: tuple[str, ...] = ()
    patterns: tuple[str, ...] = ()
    regex_fallback: bool = True

    @property
    def label(self) -> str:
        return self.labels[0] if self.labels else self.key


@dataclass(frozen=True)
class ArraySpec:
    key: str
    title: str
    markers: tuple[str, ...]
    columns: tuple[tuple[str, str], ...]
    header_aliases: dict[str, tuple[str, ...]] = field(default_factory=dict)
    date_columns: dict[str, str] = field(default_factory=dict)
    split: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class DocumentProfile:
    id: str
    name: str
    doc_type: str
    aliases: tuple[str, ...]
    fields: tuple[FieldSpec, ...]
    arrays: tuple[ArraySpec, ...] = ()
    table_enabled: bool = False
    table_splits: tuple[dict, ...] = ()
    derive: tuple[dict, ...] = ()
    periods: tuple[dict, ...] = ()
    stop_markers: tuple[str, ...] = ()
    table_singles: tuple[dict, ...] = ()
    derive_id_card: dict[str, str] = field(default_factory=dict)
    apply: str = "employee"

    @property
    def field_labels(self) -> dict[str, str]:
        return {f.key: f.label for f in self.fields}

    @property
    def known_labels(self) -> set[str]:
        out = set()
        for f in self.fields:
            out.update(f.labels)
        for a in self.arrays:
            out.update(a.markers)
            for labels in a.header_aliases.values():
                out.update(labels)
        return out

    def to_ui(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "doc_type": self.doc_type,
            "field_labels": self.field_labels,
            "array_defs": {
                a.key: {"title": a.title,
                        "columns": [list(c) for c in a.columns]}
                for a in self.arrays
            },
        }


def _profile_dirs() -> list[str]:
    return [
        os.path.join(CONFIG_DIR, "doc_profiles"),
        os.path.join(BASE, "config", "doc_profiles"),
        os.path.join(BASE, "config_template", "doc_profiles"),
        os.path.join(BASE, "_internal", "config_template", "doc_profiles"),
    ]


_CACHE: dict[str, DocumentProfile] | None = None


def _parse_profile(data: dict) -> DocumentProfile:
    fields = []
    for raw in data.get("fields") or []:
        fields.append(FieldSpec(
            key=str(raw.get("key") or ""),
            labels=tuple(str(x) for x in (raw.get("labels") or [])),
            type=str(raw.get("type") or "text"),
            required=bool(raw.get("required")),
            join_next=bool(raw.get("join_next")),
            values=tuple(str(x) for x in (raw.get("values") or [])),
            patterns=tuple(str(x) for x in (raw.get("patterns") or [])),
            regex_fallback=bool(raw.get("regex_fallback", True)),
        ))
    arrays = []
    for raw in data.get("arrays") or []:
        arrays.append(ArraySpec(
            key=str(raw.get("key") or ""),
            title=str(raw.get("title") or raw.get("key") or ""),
            markers=tuple(str(x) for x in (raw.get("markers") or [])),
            columns=tuple((str(c[0]), str(c[1])) for c in (raw.get("columns") or [])),
            header_aliases={k: tuple(str(x) for x in v)
                            for k, v in (raw.get("header_aliases") or {}).items()},
            date_columns=dict(raw.get("date_columns") or {}),
            split=dict(raw.get("split") or {}),
        ))
    return DocumentProfile(
        id=str(data.get("id") or ""),
        name=str(data.get("name") or data.get("id") or ""),
        doc_type=str(data.get("doc_type") or "other"),
        aliases=tuple(str(x) for x in (data.get("aliases") or [])),
        fields=tuple(fields),
        arrays=tuple(arrays),
        table_enabled=bool((data.get("table") or {}).get("enabled")),
        table_splits=tuple(data.get("table_splits") or []),
        derive=tuple(data.get("derive") or []),
        periods=tuple(data.get("periods") or []),
        stop_markers=tuple(str(x) for x in (data.get("stop_markers") or [])),
        table_singles=tuple(data.get("table_singles") or []),
        derive_id_card=dict(data.get("derive_id_card") or {}),
        apply=str(data.get("apply") or "employee"),
    )


def load_profiles(force: bool = False) -> dict[str, DocumentProfile]:
    global _CACHE
    if _CACHE is not None and not force:
        return _CACHE
    out: dict[str, DocumentProfile] = {}
    for folder in _profile_dirs():
        if not os.path.isdir(folder):
            continue
        for name in sorted(os.listdir(folder)):
            if not name.lower().endswith(".json"):
                continue
            path = os.path.join(folder, name)
            try:
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
                profile = _parse_profile(data)
            except (OSError, ValueError, TypeError, IndexError):
                continue
            if profile.id and profile.id not in out:
                out[profile.id] = profile
    if not out:
        out["generic@1"] = _parse_profile({
            "id": "generic@1", "name": "通用文档", "doc_type": "other",
            "aliases": ["other", "generic"], "fields": [
                {"key": "name", "labels": ["姓名"]},
                {"key": "phone", "labels": ["手机", "联系电话"], "type": "phone"},
                {"key": "email", "labels": ["邮箱"], "type": "email"},
            ],
        })
    _CACHE = out
    return out


def resolve_profile(doc_type: str | None, filename: str = "",
                    profile_id: str | None = None) -> DocumentProfile:
    profiles = load_profiles()
    if profile_id and profile_id in profiles:
        return profiles[profile_id]
    kind = str(doc_type or "").strip().lower()
    low_name = str(filename or "").lower()
    for p in profiles.values():
        if kind and (kind == p.doc_type or kind in p.aliases):
            return p
    if not kind or kind == "other":
        if "身份证" in low_name:
            kind = "id_card"
        elif "学历" in low_name or "学位" in low_name:
            kind = "education_cert"
        elif "合同" in low_name:
            kind = "contract"
        elif "入职" in low_name or "登记表" in low_name:
            kind = "onboarding_form"
        for p in profiles.values():
            if kind == p.doc_type or kind in p.aliases:
                return p
    return profiles.get("generic@1") or next(iter(profiles.values()))
