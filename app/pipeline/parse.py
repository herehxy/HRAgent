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
import importlib

# v1.13.7：加图片格式（手机拍的简历、微信里存的图片）——靠 OCR 读
SUPPORTED = (".pdf", ".docx", ".doc", ".txt", ".md",
             ".jpg", ".jpeg", ".png", ".webp", ".bmp")
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp")


def _clean(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u3000", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _parse_pdf(path: str) -> str:
    """PDF → 文本。**取不到文本层时逐页 OCR 兜底**（纸质扫描件走这条）。

    为什么加兜底：原来只做 `page.get_text()`，扫描件返回空 → 整份简历被判
    "解析失败"，原文为空 → 抽取/分析全都无从下手。而 OCR 函数本来就在文件里
    （`_parse_image_ocr`），只是没人调用它——文档承诺了、代码没接上。
    纸质简历在小公司很常见，不接 OCR 等于直接丢掉这部分候选人。
    """
    import pymupdf

    doc = pymupdf.open(path)
    try:
        text = "\n".join(page.get_text() for page in doc)
        # 阈值只拦"几乎没有文本"的情况：真简历哪怕只有一页也有几百字，
        # 而扫描件通常只有 0-10 个乱码字符。用 120 做阈值会把**短但正常**的
        # 文本层简历也送去 OCR——装了 tesseract 时反而可能被更差的识别结果覆盖。
        if len(_strip_spaces(text)) >= 20:
            return text
        # 文本层几乎为空 → 大概率是扫描件：逐页渲染成图走 OCR
        chunks = []
        for page in doc:
            try:
                pix = page.get_pixmap(dpi=200)
                chunks.append(_ocr_image_bytes(pix.tobytes("png")))
            except Exception:
                continue
        ocr_text = "\n".join(c for c in chunks if c)
        # OCR 出来的文字比原文少很多时宁可不返回——半截文本比空文本更容易骗过人
        return ocr_text if len(_strip_spaces(ocr_text)) >= 60 else text
    finally:
        doc.close()


def _strip_spaces(text: str) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def _ocr_image_bytes(data: bytes) -> str:
    """PNG/JPG 字节 → OCR 文本。pytesseract 或 Pillow 缺失时抛错，由上层兜底。"""
    import io as _io

    from PIL import Image
    import pytesseract

    img = Image.open(_io.BytesIO(data))
    try:
        return pytesseract.image_to_string(img, lang="chi_sim+eng")
    finally:
        try:
            img.close()
        except Exception:
            pass


def _parse_with_markitdown(path: str) -> str:
    from markitdown import MarkItDown

    return MarkItDown().convert(path).text_content or ""


def _parse_text(path: str) -> str:
    with open(path, encoding="utf-8", errors="ignore") as fh:
        return fh.read()


#: OCR 引擎优先级（v1.13.8）：按"中文简历效果"排序，不按知名度排序。
#: RapidOCR 是 PP-OCRv4 的 ONNX 运行时版——中文接近 PaddleOCR、模型十几 MB、
#: 纯 CPU、pip 装完即用，最符合"本地小模型优先"的诉求。
_OCR_ENGINES = ("rapidocr_onnxruntime", "paddleocr", "pytesseract")

_OCR_LABEL = {"rapidocr_onnxruntime": "rapidocr(PP-OCRv4)",
              "paddleocr": "paddleocr",
              "pytesseract": "tesseract"}


def ocr_capabilities() -> dict:
    """当前机器上**实际可用**的 OCR 引擎（界面如实展示，不做承诺）。"""
    import shutil as _sh
    out = {}
    for name in _OCR_ENGINES:
        try:
            if name == "pytesseract":
                import pytesseract  # noqa: F401
                out[name] = bool(_sh.which("tesseract"))
            else:
                importlib.import_module(name)
                out[name] = True
        except Exception:
            out[name] = False
    usable = [_OCR_LABEL[n] for n in _OCR_ENGINES if out.get(n)]
    return {"engines": out, "usable": usable, "best": usable[0] if usable else None,
            "note": ("中文简历建议装 rapidocr-onnxruntime（pip install rapidocr-onnxruntime）："
                     "效果比 Tesseract 好一个档次，且不需要额外起服务。"
                     if not usable or usable[0] != "rapidocr(PP-OCRv4)"
                     else "已启用 RapidOCR（PP-OCRv4），中文识别质量最好的一档。")}


def _open_for_ocr(src):
    """bytes / 路径 → PIL.Image（由调用方负责关）。"""
    from PIL import Image
    import io as _io
    if isinstance(src, (bytes, bytearray)):
        return Image.open(_io.BytesIO(src))
    return Image.open(src)


def _ocr_via_rapidocr(src) -> str:
    """RapidOCR（PP-OCRv4 的 ONNX 版）：中文效果最好的一档，纯 CPU。"""
    import numpy as _np
    eng = getattr(_ocr_via_rapidocr, "_c", None)
    if eng is None:
        from rapidocr_onnxruntime import RapidOCR
        eng = RapidOCR()
        _ocr_via_rapidocr._c = eng
    img = _open_for_ocr(src)
    try:
        arr = _np.array(img.convert("RGB"))
    finally:
        try:
            img.close()
        except Exception:
            pass
    res, _ = eng(arr)
    return "\n".join(str(r[1]) for r in (res or []) if len(r) > 1)


def _ocr_via_paddleocr(src) -> str:
    """PaddleOCR：中文最好，但依赖重。"""
    import numpy as _np
    eng = getattr(_ocr_via_paddleocr, "_c", None)
    if eng is None:
        from paddleocr import PaddleOCR
        eng = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)
        _ocr_via_paddleocr._c = eng
    img = _open_for_ocr(src)
    try:
        arr = _np.array(img.convert("RGB"))
    finally:
        try:
            img.close()
        except Exception:
            pass
    lines = []
    for page in (eng.ocr(arr, cls=True) or []):
        for item in (page or []):
            if len(item) >= 2 and item[1]:
                lines.append(str(item[1][0]))
    return "\n".join(lines)


def _ocr_via_tesseract(src) -> str:
    """Tesseract：最轻，但中文排版复杂的表格容易串行（兜底）。"""
    import pytesseract  # noqa: F401
    img = _open_for_ocr(src)
    try:
        return pytesseract.image_to_string(img, lang="chi_sim+eng")
    finally:
        try:
            img.close()
        except Exception:
            pass


def _parse_image_ocr(path: str) -> str:
    """图片/扫描件 OCR（v1.13.8：可插拔，按中文效果排序）。

    依次尝试 RapidOCR → PaddleOCR → Tesseract，第一个成功的就用它。
    全都没有 → 抛错，由上层如实标"解析失败"（**绝不返回半截文本冒充识别结果**）。
    """
    errs = []
    for name in _OCR_ENGINES:
        fn = {"rapidocr_onnxruntime": _ocr_via_rapidocr,
              "paddleocr": _ocr_via_paddleocr,
              "pytesseract": _ocr_via_tesseract}[name]
        try:
            text = fn(path)
            if text and text.strip():
                return text
            errs.append(name + " 返回空")
        except Exception as exc:                             # noqa: BLE001
            errs.append(f"{name}: {type(exc).__name__}")
    raise RuntimeError("没有可用的 OCR 引擎（" + "；".join(errs) + "）")


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

    if low.endswith((".jpg", ".jpeg", ".png", ".webp", ".bmp")):
        try:
            return _clean(_parse_image_ocr(path)), "ocr", True
        except Exception:
            return "", "ocr_failed", False

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
    # v1.13.8：OCR 变成可插拔（RapidOCR/PaddleOCR/Tesseract）后，
    # 这里如实报告**当前真正能用哪个** + 没装时怎么装。
    # 原来的 `caps["ocr"]=True` 只看 pytesseract 能不能 import，
    # 装了包但没装 Tesseract 可执行文件时也会报"可用"——假的。
    _oc = ocr_capabilities()
    caps["ocr"] = bool(_oc["usable"])
    caps["ocr_engines"] = _oc["usable"]
    caps["ocr_best"] = _oc["best"]
    caps["ocr_note"] = _oc["note"]
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
    # v1.13.7：图片简历（纸质件的手机拍图）也支持入库
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".bmp": "image/bmp",
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
