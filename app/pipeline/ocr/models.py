"""OCR 模块的数据契约。

这里刻意不依赖 HRAgent 的数据库、业务字段或模型调用：
以后把整个 `app.pipeline.ocr` 抽成独立服务时，只需把 `OcrDocument.to_dict()`
直接作为 HTTP 响应，不需要重写业务对象。

坐标约定：
- 图片/扫描页：`unit="px"`，bbox 为 `[x0, y0, x1, y1]`；
- PDF 文本层：`unit="pt"`，bbox 仍是 PDF 坐标；
- 混合文档的每一页各自带 `unit`，不强行统一，避免缩放误差。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class OcrLine:
    """一行文字。RapidOCR/PaddleOCR 的 box、score 原样保留。"""

    text: str = ""
    bbox: list[float] = field(default_factory=list)
    confidence: float | None = None
    polygon: list[list[float]] = field(default_factory=list)
    page: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class OcrBlock:
    """一个版面块（通常是一段）。`lines` 保留行级 bbox，供证据定位。"""

    type: str = "text"
    text: str = ""
    bbox: list[float] = field(default_factory=list)
    confidence: float | None = None
    lines: list[OcrLine] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class OcrPage:
    index: int = 0
    width: int = 0
    height: int = 0
    unit: str = "px"
    blocks: list[OcrBlock] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(b.text for b in self.blocks if b.text).strip()

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class OcrDocument:
    """一次 OCR/解析的完整结果，HRAgent 目前只取 `.text`。"""

    text: str = ""
    pages: list[OcrPage] = field(default_factory=list)
    engine: str = ""
    engine_version: str = ""
    profile: str = "resume@1"
    source: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    schema_version: str = "ocr@1"

    def to_dict(self) -> dict:
        return asdict(self)
