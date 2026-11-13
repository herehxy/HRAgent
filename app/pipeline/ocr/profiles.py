"""业务只选 profile，不直接调引擎参数。

第一批只做两个 profile：
- `resume@1`：简历（PDF 文本层优先，扫描页 200dpi OCR）；
- `generic@1`：通用文档，阈值更宽松。

以后加合同、票据、表格时，在这里加 profile，不要把参数散到 parse.py。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class OcrProfile:
    name: str
    dpi: int = 200
    force_ocr: bool = False
    #: PDF 文本层达到这个字符数就直接用文本层，不 OCR
    text_min_chars: int = 20
    #: OCR 结果低于这个字符数时，宁可保留文本层，避免半截文本冒充成功
    ocr_min_chars: int = 60
    max_pages: int = 200
    max_mb: int = 100

    def to_dict(self) -> dict:
        return asdict(self)


PROFILES: dict[str, OcrProfile] = {
    "resume@1": OcrProfile(name="resume@1"),
    "generic@1": OcrProfile(
        name="generic@1", text_min_chars=10, ocr_min_chars=20,
        max_pages=300, max_mb=200),
}


def get_profile(name: str | None) -> OcrProfile:
    """未知 profile 退回 resume@1，不因为拼错名字把整份简历丢掉。"""
    return PROFILES.get(str(name or "").strip(), PROFILES["resume@1"])
