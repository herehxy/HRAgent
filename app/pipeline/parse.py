"""解析层：PDF / DOCX / DOC / TXT / MD → 纯文本。确定性，不调用模型。

多引擎回退，尽量让每份简历都能读出文本：

| 格式 | 首选 | 回退 |
|---|---|---|
| PDF  | PyMuPDF（含文本层） | MarkItDown |
| DOCX | 标准库 zipfile + XML（不依赖第三方包） | MarkItDown |
| DOC  | OLE 复合文档分段表（olefile） | Word COM / LibreOffice / antiword / MarkItDown |
| TXT/MD | 直接读 | —— |
| 图片 | RapidOCR → PaddleOCR → Tesseract | 失败即标"待人工判读" |

读不出文本**不算失败**：调用方会照常入库并打"待人工判读"标记，
原件附件完整留档，HR 可自行打开核对。这是"不丢任何一份简历"的技术兜底。
"""
from __future__ import annotations

import os
import re
import threading

from . import ocr as ocr_mod

# v1.13.7：加图片格式（手机拍的简历、微信里存的图片）——靠 OCR 读
SUPPORTED = (".pdf", ".docx", ".doc", ".txt", ".md",
             ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff")
IMAGE_EXT = (".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp")


def _clean(text: str) -> str:
    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u3000", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _parse_pdf(path: str) -> str:
    """PDF → 文本。文本层优先；扫描页由 `pipeline.ocr` 逐页 OCR。

    具体阈值、dpi、引擎回退都在 `app/pipeline/ocr/pipeline.py`；
    这里保留一个薄包装，保证旧调用方和自检脚本继续可用。
    """
    return ocr_mod.recognize_file(path, profile="resume@1").text


def _strip_spaces(text: str) -> str:
    return re.sub(r"\s+", "", str(text or ""))


def _ocr_image_bytes(data: bytes) -> str:
    """兼容旧自检/旧调用：PNG/JPG 字节 → 文本。

    真正的引擎选择、bbox/score 保留、阅读顺序都在 `pipeline.ocr`；
    这里只取 `.text`，不改变旧行为。
    """
    return ocr_mod.recognize_bytes(
        data, filename="inline.png", profile="resume@1").text


def _parse_with_markitdown(path: str) -> str:
    from markitdown import MarkItDown

    return MarkItDown().convert(path).text_content or ""


# ---------------------------------------------------------------- DOCX（标准库）
#
# 为什么不再把 MarkItDown 当 DOCX 首选：打包版里 MarkItDown 依赖 magika 的
# ONNX 模型数据，PyInstaller 只收了 Python 模块、没收到模型文件，构造函数
# 直接抛 FileNotFoundError；而解析层的异常被上层吞掉，界面表现就是
# “Word 简历读不出来”。DOCX 本质是一个 zip + XML，标准库完全够用，
# 不依赖任何第三方包，打包后也不会再出现“源码能跑、安装版不能跑”。

_W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
_DOCX_MAX_PART = 64 * 1024 * 1024


def _docx_xml_text(data: bytes) -> str:
    """按文档顺序抽取 word/document.xml 里的文字（段落/表格/文本框）。"""
    import xml.etree.ElementTree as ET

    try:
        root = ET.fromstring(data)
    except Exception:
        return ""
    chunks: list[str] = []

    def walk(node) -> None:
        tag = node.tag
        if tag == _W_NS + "t":
            chunks.append(node.text or "")
            return
        # 修订记录里的删除文字不属于当前简历内容，跳过
        if tag in (_W_NS + "del", _W_NS + "delText"):
            return
        if tag == _W_NS + "tab":
            chunks.append("\t")
            return
        if tag in (_W_NS + "br", _W_NS + "cr"):
            chunks.append("\n")
            return
        if tag == _W_NS + "noBreakHyphen":
            chunks.append("-")
            return
        if tag == _W_NS + "p":
            for child in node:
                walk(child)
            chunks.append("\n")
            return
        if tag == _W_NS + "tc":
            for child in node:
                walk(child)
            chunks.append("\t")
            return
        if tag == _W_NS + "tr":
            for child in node:
                walk(child)
            chunks.append("\n")
            return
        for child in node:
            walk(child)

    walk(root)
    return "".join(chunks)


def _parse_docx(path: str) -> str:
    """DOCX → 文本（纯标准库，打包后必定可用）。"""
    import zipfile

    with zipfile.ZipFile(path) as zf:
        names = set(zf.namelist())
        if "word/document.xml" not in names:
            raise RuntimeError("docx 缺少 word/document.xml")
        # 正文优先；页眉/页脚常放姓名、电话，正文读不到时也能兜底
        parts: list[str] = []
        extras = sorted(n for n in names
                        if re.match(r"word/(header|footer)\d*\.xml$", n))
        for name in ("word/document.xml",) + tuple(extras):
            try:
                info = zf.getinfo(name)
                if info.file_size > _DOCX_MAX_PART:
                    continue
                text = _docx_xml_text(zf.read(name))
            except Exception:
                continue
            if text.strip():
                parts.append(text)
        if not parts:
            raise RuntimeError("docx 中没有可读取的文本")
        return "\n".join(parts)


# ---------------------------------------------------------------- DOC（Word 97-2003）
#
# .doc 是 OLE 复合文档，正文在 WordDocument 流里，通过表流中的分段表
# （piece table）拼出来。这里先走纯 Python 的 olefile（不依赖 Office），
# 失败再依次尝试 Word COM / LibreOffice / antiword。只有全部失败才标
# doc_failed —— 原件仍会归档，不会丢件。

_DOC_CODEPAGES = {
    0x0804: "gbk", 0x1004: "gbk",          # 简体中文
    0x0404: "big5", 0x0C04: "big5", 0x1404: "big5",  # 繁体中文
    0x0411: "cp932", 0x0412: "cp949", 0x0419: "cp1251",
}


def _parse_doc_binary(path: str) -> str:
    """Word 97-2003 .doc → 文本（olefile + FIB/分段表，纯离线）。"""
    import struct

    import olefile

    if not olefile.isOleFile(path):
        raise RuntimeError("不是 OLE 复合文档")
    ole = olefile.OleFileIO(path)
    try:
        if not ole.exists("WordDocument"):
            raise RuntimeError("缺少 WordDocument 流")
        wd = ole.openstream("WordDocument").read()
        if len(wd) < 0x1AA:
            raise RuntimeError("WordDocument 流过短")
        ident = struct.unpack_from("<H", wd, 0)[0]
        if ident != 0xA5EC:
            raise RuntimeError("WordDocument 标识不符")
        flags = struct.unpack_from("<H", wd, 0x0A)[0]
        table_name = "1Table" if (flags & 0x0200) else "0Table"
        if not ole.exists(table_name):
            table_name = "1Table" if ole.exists("1Table") else "0Table"
        if not ole.exists(table_name):
            raise RuntimeError("缺少表流")
        table = ole.openstream(table_name).read()
        fc_clx, lcb_clx = struct.unpack_from("<II", wd, 0x01A2)
        if lcb_clx <= 0 or fc_clx + lcb_clx > len(table):
            raise RuntimeError("分段表越界")
        clx = table[fc_clx:fc_clx + lcb_clx]
        i = 0
        plc = b""
        while i < len(clx):
            kind = clx[i]
            if kind == 1:                       # Prc：跳过属性块
                if i + 3 > len(clx):
                    break
                cb = struct.unpack_from("<H", clx, i + 1)[0]
                i += 3 + cb
            elif kind == 2:                     # Pcdt：分段表本体
                if i + 5 > len(clx):
                    break
                lcb = struct.unpack_from("<I", clx, i + 1)[0]
                plc = clx[i + 5:i + 5 + lcb]
                break
            else:
                break
        if len(plc) < 4:
            raise RuntimeError("缺少分段表")
        n = (len(plc) - 4) // 12                # 4*(n+1) 个 CP + n 个 PCD
        if n <= 0:
            raise RuntimeError("分段表为空")
        cps = struct.unpack_from("<" + "I" * (n + 1), plc, 0)
        pcd_off = 4 * (n + 1)
        lid = struct.unpack_from("<H", wd, 0x06)[0]
        enc = _DOC_CODEPAGES.get(lid, "cp1252")
        pieces: list[str] = []
        for k in range(n):
            fc_raw = struct.unpack_from("<I", plc, pcd_off + 8 * k + 2)[0]
            compressed = bool(fc_raw & 0x40000000)
            fc = fc_raw & 0x3FFFFFFF
            if compressed:
                fc //= 2
            count = cps[k + 1] - cps[k]
            if count <= 0:
                continue
            if compressed:
                raw = wd[fc:fc + count]
                pieces.append(raw.decode(enc, errors="replace"))
            else:
                raw = wd[fc:fc + count * 2]
                pieces.append(raw.decode("utf-16le", errors="replace"))
        text = "".join(pieces)
        # Word 控制字符：\r 段落、\x07 单元格/行、\x0b 手动换行、\x0c 分页
        text = text.replace("\r", "\n").replace("\x0b", "\n").replace("\x0c", "\n")
        text = text.replace("\x07", "\t")
        text = re.sub(r"[\x00-\x08\x0e-\x1f]", "", text)
        if len(_strip_spaces(text)) < 5:
            raise RuntimeError("未提取到文本")
        return text
    finally:
        try:
            ole.close()
        except Exception:
            pass


def _parse_doc_with_word(path: str) -> str:
    """用本机 Word（COM）把 .doc 转成文本；没装 Word 时抛错走下一档。"""
    if os.name != "nt":
        raise RuntimeError("仅 Windows 支持 Word COM")
    import subprocess
    import tempfile
    import uuid

    token = f"{os.getpid()}_{uuid.uuid4().hex}"
    script_path = os.path.join(tempfile.gettempdir(), f"hra_doc_{token}.ps1")
    out_path = os.path.join(tempfile.gettempdir(), f"hra_doc_{token}.txt")
    script = r"""
param([string]$Src, [string]$Out)
$ErrorActionPreference = 'Stop'
$word = $null
try {
    $word = New-Object -ComObject Word.Application
    $word.Visible = $false
    $word.DisplayAlerts = 0
    try { $word.AutomationSecurity = 3 } catch {}
    $doc = $word.Documents.Open($Src, $false, $true)
    try {
        $text = $doc.Content.Text
        [System.IO.File]::WriteAllText($Out, [string]$text,
            (New-Object System.Text.UTF8Encoding($false)))
    } finally {
        $doc.Close($false)
    }
} finally {
    if ($null -ne $word) { $word.Quit() }
}
"""
    try:
        with open(script_path, "w", encoding="utf-8-sig") as fh:
            fh.write(script)
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-File", script_path,
             "-Src", path, "-Out", out_path],
            capture_output=True, timeout=90, creationflags=flags)
        if proc.returncode != 0 or not os.path.isfile(out_path):
            err = (proc.stderr or b"").decode("utf-8", errors="ignore").strip()
            raise RuntimeError("Word COM 转换失败：" + err[:200])
        with open(out_path, encoding="utf-8-sig", errors="ignore") as fh:
            text = fh.read()
        if not text.strip():
            raise RuntimeError("Word COM 未读出文本")
        return text
    finally:
        for p in (script_path, out_path):
            try:
                os.remove(p)
            except OSError:
                pass


def _parse_doc_with_soffice(path: str) -> str:
    """LibreOffice 无头模式转文本（没装 Office 的机器上的回退）。"""
    import shutil
    import subprocess
    import tempfile

    exe = shutil.which("soffice") or shutil.which("libreoffice")
    if not exe:
        for cand in (r"C:\Program Files\LibreOffice\program\soffice.exe",
                     r"C:\Program Files (x86)\LibreOffice\program\soffice.exe"):
            if os.path.isfile(cand):
                exe = cand
                break
    if not exe:
        raise RuntimeError("未安装 LibreOffice")
    with tempfile.TemporaryDirectory(prefix="hra_doc_") as td:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.run([exe, "--headless", "--norestore", "--convert-to",
                        "txt:Text", "--outdir", td, path],
                       capture_output=True, timeout=120, creationflags=flags)
        out = os.path.join(td, os.path.splitext(os.path.basename(path))[0] + ".txt")
        if not os.path.isfile(out):
            txts = [os.path.join(td, n) for n in os.listdir(td)
                    if n.lower().endswith(".txt")]
            if not txts:
                raise RuntimeError("LibreOffice 未生成文本")
            out = txts[0]
        with open(out, encoding="utf-8", errors="ignore") as fh:
            text = fh.read()
        if not text.strip():
            raise RuntimeError("LibreOffice 未读出文本")
        return text


def _parse_doc_with_antiword(path: str) -> str:
    """antiword 回退（Linux/已装 antiword 的 Windows）。"""
    import shutil
    import subprocess

    exe = shutil.which("antiword")
    if not exe:
        raise RuntimeError("未安装 antiword")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.run([exe, path], capture_output=True, timeout=60,
                          creationflags=flags)
    if proc.returncode != 0:
        raise RuntimeError("antiword 转换失败")
    for enc in ("utf-8", "gbk", "cp1252"):
        try:
            text = proc.stdout.decode(enc)
        except UnicodeDecodeError:
            continue
        if text.strip():
            return text
    raise RuntimeError("antiword 未读出文本")


def _parse_text(path: str) -> str:
    with open(path, encoding="utf-8", errors="ignore") as fh:
        return fh.read()


def ocr_capabilities() -> dict:
    """当前机器上**实际可用**的 OCR 引擎（兼容旧调用，实现已移到 ocr 模块）。"""
    return ocr_mod.capabilities()


def _parse_image_ocr(path: str) -> str:
    """图片/扫描件 OCR：交给独立 OCR 模块，失败则如实抛出。

    引擎顺序、bbox/score、阅读顺序都在 `pipeline.ocr`；这里只取文本，
    保持旧 `parse_file_ex()` 的返回契约不变。
    """
    return ocr_mod.recognize_file(path, profile="resume@1").text


def _parse_file_ex_uncached(path: str) -> tuple[str, str, bool]:
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
        # 标准库解析优先：打包版不会因为 MarkItDown/magika 模型缺失而失明
        for engine, fn in (("docx", _parse_docx), ("markitdown", _parse_with_markitdown)):
            try:
                text = _clean(fn(path))
                if text:
                    return text, engine, True
            except Exception:
                continue
        return "", "docx_failed", False

    if low.endswith(".doc"):
        for engine, fn in (("doc", _parse_doc_binary),
                           ("word_com", _parse_doc_with_word),
                           ("libreoffice", _parse_doc_with_soffice),
                           ("antiword", _parse_doc_with_antiword),
                           ("markitdown", _parse_with_markitdown)):
            try:
                text = _clean(fn(path))
                if text:
                    return text, engine, True
            except Exception:
                continue
        return "", "doc_failed", False

    if low.endswith((".jpg", ".jpeg", ".png", ".webp", ".bmp")):
        try:
            text = _clean(_parse_image_ocr(path))
        except Exception:
            return "", "ocr_failed", False
        if text:
            return text, "ocr", True
        # OCR 没报错但一个字都没识别出来：不能记成“解析成功”。
        # 否则界面显示已解析、原文却是空的，比明确标失败更难排查。
        return "", "ocr_empty", False

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


#: 同一个文件在一次进程里可能被解析多次（选附件、入库、重新分析）。
#: 按 (绝对路径, mtime, size) 缓存，文件一变就自然失效；只缓存结果，不改行为。
_PARSE_CACHE: dict[tuple, tuple[str, str, bool]] = {}
_PARSE_CACHE_LOCK = threading.Lock()
_PARSE_CACHE_MAX = 128


def parse_file_ex(path: str) -> tuple[str, str, bool]:
    """返回（文本，解析引擎，是否成功）。带文件指纹缓存，避免重复 OCR/解析。"""
    try:
        st = os.stat(path)
        key = (os.path.abspath(path), st.st_mtime_ns, st.st_size)
    except OSError:
        return _parse_file_ex_uncached(path)
    with _PARSE_CACHE_LOCK:
        hit = _PARSE_CACHE.get(key)
    if hit is not None:
        return hit
    out = _parse_file_ex_uncached(path)
    with _PARSE_CACHE_LOCK:
        if len(_PARSE_CACHE) >= _PARSE_CACHE_MAX:
            _PARSE_CACHE.pop(next(iter(_PARSE_CACHE)), None)
        _PARSE_CACHE[key] = out
    return out


def parse_file(path: str) -> str:
    """兼容旧签名：只返回文本（失败返回空串）。"""
    return parse_file_ex(path)[0]


def parse_file_document(path: str):
    """PDF/图片 → 结构化 `OcrDocument`；其他格式返回 None。

    现有入库链路仍用 `parse_file_ex()`（只要文本）；以后要做表格回填、
    证据定位或独立 OCR 服务时，直接从 `OcrDocument` 取 bbox/confidence，
    不必再从纯文本反推。
    """
    low = str(path).lower()
    if low.endswith(".pdf") or low.endswith(IMAGE_EXT):
        return ocr_mod.recognize_file(path, profile="resume@1")
    return None


def engine_capabilities() -> dict:
    caps = {"pymupdf": False, "markitdown": False,
            "docx": True, "doc": False, "ocr": False, "errors": {}}
    try:
        import pymupdf  # noqa: F401
        caps["pymupdf"] = True
    except Exception as exc:
        caps["errors"]["pymupdf"] = f"{type(exc).__name__}: {exc}"
    try:
        import markitdown  # noqa: F401
        caps["markitdown"] = True
    except Exception as exc:
        caps["errors"]["markitdown"] = f"{type(exc).__name__}: {exc}"
    # DOCX 走标准库，永远可用；DOC 的纯离线解析依赖 olefile
    try:
        import olefile  # noqa: F401
        caps["doc"] = True
    except Exception as exc:
        caps["errors"]["olefile"] = f"{type(exc).__name__}: {exc}"
    # OCR 由 `pipeline.ocr` 统一探测：RapidOCR/PaddleOCR/Tesseract 三者
    # 实际可用哪个就报哪个，不靠“能 import pytesseract”冒充可用。
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
