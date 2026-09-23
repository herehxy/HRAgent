"""解析层：PDF / DOCX / DOC / TXT / MD → 纯文本。确定性，不调用模型。

多引擎回退，尽量让每份简历都能读出文本：

| 格式 | 首选 | 回退 |
|---|---|---|
| PDF  | PyMuPDF（含文本层） | MarkItDown |
| DOCX | MarkItDown | python-docx（若可用） |
| DOC  | MarkItDown | —— |
| TXT/MD | 直接读 | —— |
| 图片 | 尝试 OCR（若装了 pytesseract） | 失败即标"待人工判读" |

读不出文本**不算失败**：调用方会照常入库并打"待人工判读"标记，
原件附件完整留档，HR 可自行打开核对。这是"不丢任何一份简历"的技术兜底。
"""
from __future__ import annotations

import os
import re

SUPPORTED = (".pdf", ".docx", ".doc", ".txt", ".md")
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp")


def _clean(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u3000", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _parse_pdf(path: str) -> str:
    import pymupdf

    doc = pymupdf.open(path)
    try:
        return "\n".join(page.get_text() for page in doc)
    finally:
        doc.close()


def _parse_with_markitdown(path: str) -> str:
    from markitdown import MarkItDown

    return MarkItDown().convert(path).text_content or ""


def _parse_text(path: str) -> str:
    with open(path, encoding="utf-8", errors="ignore") as fh:
        return fh.read()


def _parse_image_ocr(path: str) -> str:
    """图片简历 OCR。仅在装了 pytesseract 时可用，否则抛错由上层兜底。"""
    import pytesseract  # noqa: F401  (可能未安装)
    from PIL import Image

    with Image.open(path) as img:
        return pytesseract.image_to_string(img, lang="chi_sim+eng")


def parse_file_ex(path: str) -> tuple[str, str, bool]:
    """返回（文本，解析引擎，是否成功）。任何异常都转为 ok=False，不向上抛。"""
    low = path.lower()
    if low.endswith(".pdf"):
        for engine, fn in (("pymupdf", _parse_pdf), ("markitdown", _parse_with_markitdown)):
            try:
                text = _clean(fn(path))
                if text:
                    return text, engine, True
            except Exception:
                continue
        return "", "pdf_failed", False

    if low.endswith(".docx"):
        for engine, fn in (("markitdown", _parse_with_markitdown), ("pymupdf", _parse_pdf)):
            try:
                text = _clean(fn(path))
                if text:
                    return text, engine, True
            except Exception:
                continue
        return "", "docx_failed", False

    if low.endswith(".doc"):
        try:
            text = _clean(_parse_with_markitdown(path))
            if text:
                return text, "markitdown", True
        except Exception:
            pass
        return "", "doc_failed", False

    if low.endswith((".txt", ".md")):
        try:
            return _clean(_parse_text(path)), "text", True
        except Exception:
            return "", "text_failed", False

    if low.endswith(IMAGE_EXT):
        try:
            text = _clean(_parse_image_ocr(path))
            if text:
                return text, "ocr", True
        except Exception:
            pass
        return "", "ocr_unavailable", False

    return "", "unsupported", False


def parse_file(path: str) -> str:
    """兼容旧签名：只返回文本（失败返回空串）。"""
    return parse_file_ex(path)[0]


def engine_capabilities() -> dict:
    caps = {"pymupdf": False, "markitdown": False, "ocr": False}
    try:
        import pymupdf  # noqa: F401
        caps["pymupdf"] = True
    except Exception:
        pass
    try:
        import markitdown  # noqa: F401
        caps["markitdown"] = True
    except Exception:
        pass
    try:
        import pytesseract  # noqa: F401
        caps["ocr"] = True
    except Exception:
        pass
    caps["archive_note"] = "解析失败不影响入库：原件留档并标『待人工判读』"
    return caps


def ext_of(name: str) -> str:
    return os.path.splitext(name.lower())[1]


# 后缀 → MIME。**入库落库与下载响应共用这一份**：落库的 mime 决定
# `Content-Type`，而浏览器只有拿到 `application/pdf` 才会就地预览而不是下载。
# 曾经入库写死 None，导致原件只能下载、不能在浏览器里打开。
MIME_BY_EXT = {
    ".pdf": "application/pdf",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".bmp": "image/bmp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".webp": "image/webp",
}


def mime_of(name: str) -> str:
    """按后缀推 MIME；认不出就用通用二进制流（浏览器会当附件下载）。"""
    return MIME_BY_EXT.get(ext_of(name or ""), "application/octet-stream")
