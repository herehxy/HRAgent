"""内嵌 OCR 模块（未来可原样抽成 OCR 能力服务）。

这个包刻意只有四层，不 import HRAgent 的数据库/业务模型：

```
profiles.py       业务 profile（resume@1 / generic@1）
engines.py        RapidOCR / PaddleOCR / Tesseract 适配，保留 bbox/score
reading_order.py  按 bbox 归块、还原阅读顺序
pipeline.py       PDF 文本层优先 → 扫描页 OCR → OcrDocument
models.py         OcrLine / OcrBlock / OcrPage / OcrDocument 契约
```

`parse.py` 现在只调用 `recognize_file()` / `recognize_bytes()`；以后做
`OCRAgent` 服务时，把 `pipeline.py` 外面套一层 FastAPI 即可，契约不用改。
"""
from __future__ import annotations

from .engines import OcrEngineError, available_engines, capabilities
from .models import OcrBlock, OcrDocument, OcrLine, OcrPage
from .pipeline import (OcrUnsupported, recognize_bytes, recognize_file)
from .profiles import PROFILES, OcrProfile, get_profile

__all__ = [
    "OcrBlock", "OcrDocument", "OcrLine", "OcrPage",
    "OcrEngineError", "OcrUnsupported",
    "OcrProfile", "PROFILES", "get_profile",
    "recognize_file", "recognize_bytes",
    "available_engines", "capabilities",
]
