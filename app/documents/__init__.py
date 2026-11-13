"""文档智能层：Profile + 通用抽取 + 业务 Adapter。

不负责 OCR 引擎，也不直接写业务表：
- `profiles.py`：文档类型/字段/表格的配置；
- `extractors.py`：键值、表格、正则抽取；
- `adapters.py`：把抽取结果过滤成某个业务域可写的字段。

以后新增合同、发票、证明等，只加 profile + adapter。
"""
from .extractors import extract_document
from .profiles import (DocumentProfile, FieldSpec, ArraySpec,
                       load_profiles, resolve_profile)

__all__ = [
    "extract_document", "DocumentProfile", "FieldSpec", "ArraySpec",
    "load_profiles", "resolve_profile",
]
