"""把 OCR 的散行按版面还原成块，并给出阅读顺序。

为什么不能用 OCR 引擎的返回顺序：
RapidOCR 通常按检测框从上到下返回，但双栏简历、左侧时间轴、表格里的
多列文字会被交错。这里用 bbox 做**确定性**排序：

1. 先按 top/left 排序；
2. 只有横向重叠足够、纵向间距不大的行才归入同一个块；
3. 块按 top/left 排序，块内按 top/left 排序；
4. 没有 bbox 的引擎结果保留原顺序，不凭空重排。
"""
from __future__ import annotations

from .models import OcrBlock, OcrLine


def _bbox(line: OcrLine) -> tuple[float, float, float, float] | None:
    b = line.bbox or []
    if len(b) < 4:
        return None
    try:
        x0, y0, x1, y1 = (float(b[0]), float(b[1]), float(b[2]), float(b[3]))
    except (TypeError, ValueError):
        return None
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    return x0, y0, x1, y1


def _union(boxes: list[tuple[float, float, float, float]]) -> list[float]:
    if not boxes:
        return []
    return [min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes)]


def _overlap_x(a: tuple[float, float, float, float],
               b: tuple[float, float, float, float]) -> float:
    return max(0.0, min(a[2], b[2]) - max(a[0], b[0]))


def _sort_lines(lines: list[OcrLine]) -> list[OcrLine]:
    def key(line: OcrLine):
        b = _bbox(line)
        if not b:
            return (0.0, 0.0)
        return (b[1], b[0])
    return sorted(lines, key=key)


def build_blocks(lines: list[OcrLine]) -> list[OcrBlock]:
    """按 bbox 归块；结果稳定、可重复，不依赖引擎返回顺序。"""
    cleaned = [ln for ln in lines if str(ln.text or "").strip()]
    if not cleaned:
        return []

    # 没有坐标（Tesseract 某些模式）时只保留一个块，顺序按引擎原顺序。
    if not any(_bbox(ln) for ln in cleaned):
        return [OcrBlock(text="\n".join(ln.text.strip() for ln in cleaned),
                         lines=cleaned)]

    ordered = _sort_lines(cleaned)
    groups: list[dict] = []
    for line in ordered:
        lb = _bbox(line)
        if not lb:
            # 个别行没 bbox：附到最近块，实在没有就单独成块
            if groups:
                groups[-1]["lines"].append(line)
                continue
            groups.append({"lines": [line], "boxes": []})
            continue
        lw = max(1.0, lb[2] - lb[0])
        lh = max(1.0, lb[3] - lb[1])
        best = None
        best_score = None
        for g in groups:
            gb = _union(g["boxes"])
            if not gb:
                continue
            overlap = _overlap_x(lb, gb)
            ratio = overlap / max(1.0, min(lw, max(1.0, gb[2] - gb[0])))
            gap = lb[1] - gb[3]
            # 只允许“在当前块下方、间距不超过 1.8 行高”的行归入；
            # 双栏的左右两栏通常横向不重叠，会自然分成两个块。
            if ratio >= 0.20 and -lh <= gap <= max(18.0, lh * 1.8):
                score = (max(0.0, gap), -ratio)
                if best_score is None or score < best_score:
                    best, best_score = g, score
        if best is None:
            groups.append({"lines": [line], "boxes": [lb]})
        else:
            best["lines"].append(line)
            best["boxes"].append(lb)

    blocks: list[OcrBlock] = []
    for g in groups:
        glines = _sort_lines(g["lines"])
        boxes = [_bbox(ln) for ln in glines if _bbox(ln)]
        scores = [float(ln.confidence) for ln in glines
                  if ln.confidence is not None]
        blocks.append(OcrBlock(
            text="\n".join(ln.text.strip() for ln in glines if ln.text.strip()),
            bbox=_union([b for b in boxes if b]),
            confidence=(sum(scores) / len(scores)) if scores else None,
            lines=glines,
        ))

    def block_key(block: OcrBlock):
        b = block.bbox or []
        if len(b) >= 4:
            return (float(b[1]), float(b[0]))
        return (0.0, 0.0)

    blocks.sort(key=block_key)
    return blocks


def text_of(blocks: list[OcrBlock]) -> str:
    return "\n".join(b.text for b in blocks if b.text).strip()
