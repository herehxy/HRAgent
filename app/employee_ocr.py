"""兼容入口：旧的 `employee_ocr.extract_fields()` 现在委托给通用文档层。

真正的实现已经移到 `app.documents`：
- 文档类型/字段/表格在 `config/doc_profiles/*.json`；
- 表格/键值/正则抽取在 `app.documents.extractors`；
- 员工字段白名单在 `app.documents.adapters`。

保留这个模块是为了不打断现有服务端、测试与旧脚本的 import。
"""
from __future__ import annotations

from .documents import extract_document
from .documents.profiles import load_profiles


def _labels() -> dict:
    for p in load_profiles().values():
        if p.doc_type == "onboarding_form":
            return p.field_labels
    return {}


FIELD_LABELS = _labels()


def extract_fields(doc_type: str, text: str, filename: str = "",
                   pdf_path: str | None = None,
                   profile_id: str | None = None) -> dict:
    return extract_document(doc_type, text, filename=filename,
                            pdf_path=pdf_path, profile_id=profile_id)
