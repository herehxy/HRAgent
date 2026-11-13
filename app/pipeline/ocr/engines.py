"""OCR 引擎适配层：RapidOCR 优先，PaddleOCR / Tesseract 兜底。

关键变化（相对旧 `parse.py` 的实现）：
- 保留 RapidOCR 的 **box / score**，不再只取文字；
- 引擎返回统一的 `OcrLine`，上层不需要知道各家返回结构；
- 引擎只在需要时导入，缺少某个包不会影响另外两个；
- `capabilities()` 如实报告当前机器真正能用的引擎。
"""
from __future__ import annotations

import importlib
import importlib.metadata as _md
import shutil
from typing import Any

from .models import OcrLine


class OcrEngineError(RuntimeError):
    """没有可用引擎，或所有引擎都返回空。"""


class BaseOcrEngine:
    name = ""
    package = ""
    label = ""

    def available(self) -> bool:
        return False

    def version(self) -> str:
        if not self.package:
            return ""
        try:
            return str(_md.version(self.package))
        except Exception:                                    # noqa: BLE001
            return ""

    def recognize(self, src: Any) -> list[OcrLine]:
        raise NotImplementedError


def _to_image(src: Any):
    """路径 / bytes / PIL.Image / numpy → RGB PIL.Image（处理 EXIF 旋转）。"""
    from PIL import Image, ImageOps
    import io as _io

    if isinstance(src, Image.Image):
        img = src
    elif isinstance(src, (bytes, bytearray)):
        img = Image.open(_io.BytesIO(bytes(src)))
    elif isinstance(src, str):
        img = Image.open(src)
    else:
        img = Image.fromarray(src)
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:                                        # noqa: BLE001
        pass
    return img.convert("RGB")


def _polygon(raw: Any) -> list[list[float]]:
    out: list[list[float]] = []
    try:
        for p in raw or []:
            if isinstance(p, (list, tuple)) and len(p) >= 2:
                out.append([float(p[0]), float(p[1])])
    except (TypeError, ValueError):
        return []
    return out


def _bbox_of(poly: list[list[float]]) -> list[float]:
    if not poly:
        return []
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    return [min(xs), min(ys), max(xs), max(ys)]


def _score(raw: Any) -> float | None:
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, v))


class RapidOcrEngine(BaseOcrEngine):
    name = "rapidocr"
    package = "rapidocr_onnxruntime"
    label = "rapidocr(PP-OCRv4)"

    def __init__(self) -> None:
        self._eng = None

    def available(self) -> bool:
        try:
            importlib.import_module(self.package)
            return True
        except Exception:                                    # noqa: BLE001
            return False

    def _get(self):
        if self._eng is None:
            from rapidocr_onnxruntime import RapidOCR
            self._eng = RapidOCR()
        return self._eng

    def recognize(self, src: Any) -> list[OcrLine]:
        import numpy as np

        img = _to_image(src)
        res = self._get()(np.array(img))
        # 1.2.x 返回 (result, elapse)；result 是 [[box, text, score], ...]
        if isinstance(res, tuple):
            res = res[0]
        out: list[OcrLine] = []
        for item in (res or []):
            if not isinstance(item, (list, tuple)) or len(item) < 2:
                continue
            text = str(item[1] or "").strip()
            if not text:
                continue
            poly = _polygon(item[0])
            out.append(OcrLine(
                text=text, bbox=_bbox_of(poly), polygon=poly,
                confidence=_score(item[2]) if len(item) > 2 else None))
        return out


class PaddleOcrEngine(BaseOcrEngine):
    name = "paddleocr"
    package = "paddleocr"
    label = "paddleocr"

    def __init__(self) -> None:
        self._eng = None

    def available(self) -> bool:
        try:
            importlib.import_module(self.package)
            return True
        except Exception:                                    # noqa: BLE001
            return False

    def _get(self):
        if self._eng is None:
            from paddleocr import PaddleOCR
            try:
                self._eng = PaddleOCR(use_angle_cls=True, lang="ch", show_log=False)
            except TypeError:
                # PaddleOCR 3.x 去掉了 show_log/use_angle_cls 参数
                self._eng = PaddleOCR(lang="ch")
        return self._eng

    def recognize(self, src: Any) -> list[OcrLine]:
        import numpy as np

        img = _to_image(src)
        res = self._get().ocr(np.array(img), cls=True)
        out: list[OcrLine] = []
        for page in (res or []):
            for item in (page or []):
                if not isinstance(item, (list, tuple)) or len(item) < 2:
                    continue
                box, ts = item[0], item[1]
                if not isinstance(ts, (list, tuple)) or not ts:
                    continue
                text = str(ts[0] or "").strip()
                if not text:
                    continue
                poly = _polygon(box)
                out.append(OcrLine(
                    text=text, bbox=_bbox_of(poly), polygon=poly,
                    confidence=_score(ts[1]) if len(ts) > 1 else None))
        return out


class TesseractOcrEngine(BaseOcrEngine):
    name = "tesseract"
    package = "pytesseract"
    label = "tesseract"

    def available(self) -> bool:
        try:
            importlib.import_module(self.package)
            return bool(shutil.which("tesseract"))
        except Exception:                                    # noqa: BLE001
            return False

    def version(self) -> str:
        try:
            import pytesseract
            return str(pytesseract.get_tesseract_version())
        except Exception:                                    # noqa: BLE001
            return ""

    def recognize(self, src: Any) -> list[OcrLine]:
        from pytesseract import Output
        import pytesseract

        img = _to_image(src)
        data = pytesseract.image_to_data(
            img, lang="chi_sim+eng", output_type=Output.DICT)
        groups: dict[tuple, dict] = {}
        n = len(data.get("text") or [])
        for i in range(n):
            text = str((data.get("text") or [""] * n)[i] or "").strip()
            if not text:
                continue
            try:
                conf = float((data.get("conf") or ["-1"] * n)[i])
            except (TypeError, ValueError):
                conf = -1.0
            if conf < 0:
                continue
            key = (data.get("block_num", [0] * n)[i],
                   data.get("par_num", [0] * n)[i],
                   data.get("line_num", [0] * n)[i])
            g = groups.setdefault(key, {"text": [], "boxes": [], "scores": []})
            g["text"].append(text)
            left = int(data.get("left", [0] * n)[i])
            top = int(data.get("top", [0] * n)[i])
            width = int(data.get("width", [0] * n)[i])
            height = int(data.get("height", [0] * n)[i])
            g["boxes"].append((left, top, left + width, top + height))
            g["scores"].append(conf / 100.0)
        out: list[OcrLine] = []
        for key in sorted(groups):
            g = groups[key]
            boxes = g["boxes"]
            poly = [[b[0], b[1]] for b in boxes[:1]] + \
                   [[b[2], b[3]] for b in boxes[-1:]]
            out.append(OcrLine(
                text=" ".join(g["text"]),
                bbox=[min(b[0] for b in boxes), min(b[1] for b in boxes),
                      max(b[2] for b in boxes), max(b[3] for b in boxes)],
                polygon=_polygon(poly),
                confidence=(sum(g["scores"]) / len(g["scores"])) if g["scores"] else None))
        return out


ENGINE_CLASSES = (RapidOcrEngine, PaddleOcrEngine, TesseractOcrEngine)
_PROBE_CACHE: dict[str, bool] = {}
#: 引擎实例必须复用：RapidOCR 初始化要 1–2 秒、模型常驻内存，
#: 每份简历新建一次会把批量入库拖慢一个数量级。
_ENGINE_CACHE: dict[str, BaseOcrEngine] = {}


def _instance(cls: type[BaseOcrEngine]) -> BaseOcrEngine:
    key = cls.__name__
    if key not in _ENGINE_CACHE:
        _ENGINE_CACHE[key] = cls()
    return _ENGINE_CACHE[key]


def _available_engines() -> list[BaseOcrEngine]:
    out: list[BaseOcrEngine] = []
    for cls in ENGINE_CLASSES:
        key = cls.__name__
        engine = _instance(cls)
        if key not in _PROBE_CACHE:
            try:
                _PROBE_CACHE[key] = bool(engine.available())
            except Exception:                                # noqa: BLE001
                _PROBE_CACHE[key] = False
        if _PROBE_CACHE[key]:
            out.append(engine)
    return out


def available_engines() -> list[str]:
    return [e.label for e in _available_engines()]


def capabilities() -> dict:
    """保持旧的 `engines/usable/best/note` 形状，另带 `details`。"""
    out: dict[str, Any] = {"engines": {}, "usable": [], "best": None,
                           "note": "", "details": {}}
    for cls in ENGINE_CLASSES:
        e = cls()
        try:
            ok = bool(e.available())
        except Exception:                                    # noqa: BLE001
            ok = False
        out["engines"][cls.package] = ok
        out["details"][cls.name] = {
            "available": ok, "package": cls.package,
            "version": e.version() if ok else ""}
        if ok:
            out["usable"].append(cls.label)
    out["best"] = out["usable"][0] if out["usable"] else None
    if not out["usable"] or out["usable"][0] != "rapidocr(PP-OCRv4)":
        out["note"] = ("中文简历建议装 rapidocr-onnxruntime"
                       "（pip install rapidocr-onnxruntime）："
                       "效果比 Tesseract 好一个档次，且不需要额外起服务。")
    else:
        out["note"] = "已启用 RapidOCR（PP-OCRv4），中文识别质量最好的一档。"
    return out


def recognize_image(src: Any) -> tuple[list[OcrLine], str, str]:
    """按优先级尝试可用引擎，返回 `(lines, engine_name, engine_version)`。"""
    engines = _available_engines()
    if not engines:
        raise OcrEngineError("没有可用的 OCR 引擎")
    errors: list[str] = []
    for engine in engines:
        try:
            lines = engine.recognize(src)
        except Exception as exc:                             # noqa: BLE001
            errors.append(f"{engine.name}: {type(exc).__name__}: {exc}")
            continue
        if any(str(ln.text or "").strip() for ln in lines):
            return lines, engine.name, engine.version()
        errors.append(f"{engine.name}: 返回空")
    raise OcrEngineError("所有 OCR 引擎都没出结果 -> " + " | ".join(errors))
