"""PDF / 图片 → `OcrDocument` 的确定性管线。

流程与 HRAgent 原 `parse.py` 保持一致，但多留下三样以后有用的信息：
- 每行的 bbox / confidence；
- 版面块与阅读顺序；
- 引擎、profile、耗时、告警。

PDF 仍遵守“文本层优先”：
文本层字符数达到 `profile.text_min_chars` 就不 OCR；扫描页才按 dpi 渲染后 OCR。
OCR 结果少于 `profile.ocr_min_chars` 时保留文本层，避免半截 OCR 覆盖正常文本。
"""
from __future__ import annotations

import os
import re
import time
from typing import Any

from . import engines
from .models import OcrBlock, OcrDocument, OcrLine, OcrPage
from .profiles import OcrProfile, get_profile
from .reading_order import build_blocks, text_of

IMAGE_EXT = (".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff")


class OcrUnsupported(RuntimeError):
    """不是本模块能处理的文件类型。"""


def _strip_spaces(text: str) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def _union(boxes: list[list[float]]) -> list[float]:
    boxes = [b for b in boxes if len(b) >= 4]
    if not boxes:
        return []
    return [min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)]


def _pdf_text_blocks(page, index: int) -> list[OcrBlock]:
    """PyMuPDF 文本层 → 带 bbox 的块。文本层没有置信度，记为 1.0。"""
    try:
        data = page.get_text("dict")
    except Exception:                                        # noqa: BLE001
        return []
    blocks: list[OcrBlock] = []
    for raw in (data.get("blocks") or []):
        if int(raw.get("type", 0) or 0) != 0:
            continue
        lines: list[OcrLine] = []
        for ln in (raw.get("lines") or []):
            text = "".join(str(s.get("text") or "")
                           for s in (ln.get("spans") or [])).strip()
            if not text:
                continue
            try:
                bbox = [float(x) for x in (ln.get("bbox") or [])]
            except (TypeError, ValueError):
                bbox = []
            lines.append(OcrLine(text=text, bbox=bbox, confidence=1.0,
                                 page=index))
        if not lines:
            continue
        try:
            bbox = [float(x) for x in (raw.get("bbox") or [])]
        except (TypeError, ValueError):
            bbox = []
        blocks.append(OcrBlock(
            text="\n".join(ln.text for ln in lines),
            bbox=bbox or _union([ln.bbox for ln in lines]),
            confidence=1.0, lines=lines))
    return blocks


def _page_size(src: Any) -> tuple[int, int]:
    from PIL import Image
    import io as _io

    if isinstance(src, Image.Image):
        return int(src.width), int(src.height)
    if isinstance(src, (bytes, bytearray)):
        img = Image.open(_io.BytesIO(bytes(src)))
    elif isinstance(src, str):
        img = Image.open(src)
    else:
        arr = src
        return int(arr.shape[1]), int(arr.shape[0])
    try:
        return int(img.width), int(img.height)
    finally:
        try:
            img.close()
        except Exception:                                # noqa: BLE001
            pass


def _source_of(filename: str, pages: int) -> dict:
    return {"filename": os.path.basename(str(filename or "")),
            "pages": int(pages)}


def _image_doc(src: Any, profile: OcrProfile, filename: str = "") -> OcrDocument:
    started = time.time()
    width, height = _page_size(src)
    lines, engine, version = engines.recognize_image(src)
    for line in lines:
        line.page = 0
    blocks = build_blocks(lines)
    page = OcrPage(index=0, width=width, height=height, unit="px", blocks=blocks)
    text = page.text
    warnings: list[str] = []
    if not text:
        warnings.append("OCR 没有识别出文字，建议人工核对原件")
    return OcrDocument(
        text=text, pages=[page], engine=engine, engine_version=version,
        profile=profile.name, source=_source_of(filename or str(src)[:120], 1),
        warnings=warnings,
        metrics={"duration_ms": int((time.time() - started) * 1000),
                 "pages": 1, "ocr_pages": 1, "text_chars": len(text)})


def _pdf_doc(doc, profile: OcrProfile, filename: str = "") -> OcrDocument:
    started = time.time()
    total = len(doc)
    limit = min(total, max(1, int(profile.max_pages)))
    warnings: list[str] = []
    if total > limit:
        warnings.append(f"PDF 共 {total} 页，按 profile 只处理前 {limit} 页")
    pages: list[OcrPage] = []
    ocr_pages = 0
    ocr_engine = ""
    ocr_version = ""
    for i in range(limit):
        page = doc[i]
        text_blocks = _pdf_text_blocks(page, i)
        text_layer = text_of(text_blocks)
        need_ocr = bool(profile.force_ocr) or \
            len(_strip_spaces(text_layer)) < profile.text_min_chars
        page_obj: OcrPage | None = None
        if need_ocr:
            try:
                pix = page.get_pixmap(dpi=profile.dpi)
                lines, engine, version = engines.recognize_image(
                    pix.tobytes("png"))
                for line in lines:
                    line.page = i
                ocr_text = "\n".join(line.text for line in lines
                                     if str(line.text or "").strip()).strip()
                if len(_strip_spaces(ocr_text)) >= profile.ocr_min_chars:
                    page_obj = OcrPage(
                        index=i, width=int(pix.width), height=int(pix.height),
                        unit="px", blocks=build_blocks(lines))
                    ocr_pages += 1
                    ocr_engine, ocr_version = engine, version
                else:
                    warnings.append(
                        f"第 {i + 1} 页 OCR 有效文字过少，保留文本层/空白")
            except Exception as exc:                         # noqa: BLE001
                warnings.append(
                    f"第 {i + 1} 页 OCR 失败：{type(exc).__name__}: {exc}")
        if page_obj is None:
            page_obj = OcrPage(
                index=i, width=int(page.rect.width), height=int(page.rect.height),
                unit="pt", blocks=text_blocks)
        pages.append(page_obj)

    text = "\n".join(p.text for p in pages).strip()
    if not text:
        warnings.append("PDF 文本层和 OCR 都没有得到文字，建议人工核对原件")
    if ocr_pages and ocr_pages == len(pages):
        engine, version = (ocr_engine or "ocr"), ocr_version
    elif ocr_pages:
        engine = "mixed"
        version = "pymupdf" + (f";{ocr_engine}" if ocr_engine else "")
    else:
        engine, version = "pymupdf", ""
    return OcrDocument(
        text=text, pages=pages, engine=engine, engine_version=version,
        profile=profile.name, source=_source_of(filename, len(pages)),
        warnings=warnings,
        metrics={"duration_ms": int((time.time() - started) * 1000),
                 "pages": len(pages), "ocr_pages": ocr_pages,
                 "text_chars": len(text)})


def _check_size(size: int, profile: OcrProfile) -> None:
    max_bytes = max(1, int(profile.max_mb)) * 1024 * 1024
    if size > max_bytes:
        raise OcrUnsupported(
            f"文件 {size / 1024 / 1024:.1f}MB 超过 profile 上限 "
            f"{profile.max_mb}MB")


def recognize_file(path: str, profile: str = "resume@1") -> OcrDocument:
    """文件路径 → OcrDocument；不支持的格式抛 `OcrUnsupported`。"""
    prof = get_profile(profile)
    try:
        _check_size(os.path.getsize(path), prof)
    except OSError:
        pass
    low = str(path).lower()
    if low.endswith(".pdf"):
        import pymupdf
        doc = pymupdf.open(path)
        try:
            return _pdf_doc(doc, prof, filename=path)
        finally:
            doc.close()
    if low.endswith(IMAGE_EXT):
        return _image_doc(path, prof, filename=path)
    raise OcrUnsupported(f"OCR 模块不支持的文件类型：{os.path.splitext(path)[1]}")


def recognize_bytes(data: bytes, filename: str = "",
                    profile: str = "resume@1") -> OcrDocument:
    """字节流 → OcrDocument；为以后 HTTP 服务预留的入口。"""
    prof = get_profile(profile)
    _check_size(len(data or b""), prof)
    low = str(filename or "").lower()
    if low.endswith(".pdf") or bytes(data or b"")[:5] == b"%PDF-":
        import pymupdf
        doc = pymupdf.open(stream=bytes(data), filetype="pdf")
        try:
            return _pdf_doc(doc, prof, filename=filename or "upload.pdf")
        finally:
            doc.close()
    return _image_doc(bytes(data), prof, filename=filename or "upload.png")


def capabilities() -> dict:
    return engines.capabilities()
