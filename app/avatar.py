"""从简历原件里抽证件照（v2.0.4）。

**为什么是启发式、不是人脸检测**：本机 OpenCV 是 5.0，`CascadeClassifier`（Haar）已被移除；
只剩 `FaceDetectorYN`，而它要额外下一个 `.onnx` 模型 —— 部署环境（代理/内网）未必拿得到。
所以这里用最稳的一条：**挑「竖构图 + 够大 + 面积最大」的那张图**。

实测 94 份真实简历原件：**54% 能抽到候选**，抽出来的基本是蓝底证件照；
剩下抽不到的（纯文字/表格简历、扫描件整页图、图片型简历）本来就没有"单独那张人脸"。

抽错是真实风险（logo / 组织架构图 / 签名 / 整页缩略图），所以这个模块**只负责抽**，
  "能不能用"由调用方决定：结果落 `data/avatars/<id>.jpg`，可删可重抽；
  卡片上的照片只是叠加层，抽不到就自动回落到"姓首字"。

`candidates.avatar_path` 三态（见 db 的 `_ADD`）：
  NULL  = 还没试过 → 首次渲染懒抽一次
  "-"   = 试过，没有可用照片 → 不再重复尝试
  其他  = 相对 BASE 的图片路径（如 `data/avatars/12.jpg`）
"""
from __future__ import annotations

import io
import os
import queue
import threading
import zipfile

from PIL import Image

#: 存盘尺寸（正方形）。头像圆是 44px，2 倍图给 88 够用；留 256 是为了将来放大或导出。
AVATAR_SIZE = 256
#: 超过这个大小的原件不做抽取（扫描件动辄几十 MB，抽出来的也是整页）
MAX_BYTES = 40 * 1024 * 1024
#: 明确没有照片的哨兵值
NONE_MARK = "-"
#: **只看第 1 页**。业务方给的口径（2026-10-20）：一个 PDF 里常夹着成绩单、证书等一堆材料，
#: **简历一定是第一个**，照片就在简历首页。实测 87 份真实 PDF：只扫第 1 页与扫前 3 页
#: 命中数**完全一样（48 份）**，只扫第 1 页还快 25%，并且能避开"证书页上某张更大的图"
#: 被面积打分误选（有 2 份在两种模式下抽到的不是同一张）。
MAX_PAGES = 1
#: 一份简历最多解码几张（按声明面积从大到小取前 N）——**先按声明尺寸筛，再解码**，
#: 这是关键：不解码 4000×6000 的扫描页，单份耗时从 13 秒降到几十毫秒（实测）。
MAX_DECODE = 4
#: 参与候选的最大像素边长（超过的多半是整页扫描图，不是证件照）
MAX_SIDE = 2000


def _score(im: Image.Image) -> float:
    """给候选图打分：像证件照的分高，明显不像的给 0（宁缺勿滥）。

    证件照的共同点：**竖构图**（高/宽 1.15~1.5 最常见）、最短边不至于太小。
    排除项：超宽长条（横幅/签名）、很扁的（图标）、很小的（水印/logo）。
    """
    w, h = im.size
    if min(w, h) < 60 or max(w, h) < 120:
        return 0.0
    ar = h / w
    if not (0.9 <= ar <= 1.8):
        return 0.0
    s = float(min(w * h, 400 * 400))       # 超过 400x400 不再加分，避免被大图通吃
    if not (1.15 <= ar <= 1.5):
        s *= 0.6                            # 比例不太像证件照，降权但不直接排除
    return s


def _square(im: Image.Image, size: int) -> Image.Image:
    """裁成正方形。**从顶部偏下一点起裁**，不是居中裁 ——
    证件照的人头在上半部，居中裁会把下巴/脖子切掉，44px 圆里只剩一张脸的上半截。"""
    w, h = im.size
    side = min(w, h)
    top = max(0, int((h - side) * 0.08))
    left = (w - side) // 2
    return im.crop((left, top, left + side, top + side)).resize((size, size), Image.LANCZOS)


def _jpeg(im: Image.Image, size: int) -> bytes:
    buf = io.BytesIO()
    _square(im, size).save(buf, "JPEG", quality=88, optimize=True)
    return buf.getvalue()


def _declared_size(doc, xref: int) -> tuple[int, int] | None:
    """读图片的**声明尺寸**，不解码。PyMuPDF 的 `xref_get_key` 只查对象字典，很便宜。

    为什么必须先做这一步：扫描件 PDF 里每一页都是一张 4000×6000 的大图，
    无条件 `Pixmap(doc, xref)` 会真去解码它 —— 实测单份能拖到 **13 秒**，
    而这活是在请求线程里跑的，直接把整页接口堵死。
    """
    try:
        w = doc.xref_get_key(xref, "Width")
        h = doc.xref_get_key(xref, "Height")
        if not w or not h or w[0] != "int" or h[0] != "int":
            return None
        return int(w[1]), int(h[1])
    except Exception:                                    # noqa: BLE001
        return None


def pdf_images(path: str) -> list[bytes]:
    """PDF 里可能像证件照的**嵌入图**（先按声明尺寸筛，再解码）。"""
    import pymupdf

    picked: list[tuple[int, int, int]] = []              # (面积, xref, 页序)
    with pymupdf.open(path) as doc:
        for pno, page in enumerate(doc):
            if pno >= MAX_PAGES:
                break
            for info in page.get_images(full=True):
                xref = info[0]
                size = _declared_size(doc, xref)
                if not size:
                    continue
                w, h = size
                if not (60 <= min(w, h) and max(w, h) <= MAX_SIDE):
                    continue                             # 太小=图标；太大=整页扫描
                ar = h / w
                if not (0.9 <= ar <= 1.8):
                    continue                             # 横构图/长条：不是证件照
                picked.append((w * h, xref, pno))

        picked.sort(reverse=True)                        # 面积大的优先，但**不重复解同一张**
        seen: set[int] = set()
        out: list[bytes] = []
        for _area, xref, _p in picked:
            if xref in seen or len(out) >= MAX_DECODE:
                break
            seen.add(xref)
            try:
                pix = pymupdf.Pixmap(doc, xref)
                if pix.n - pix.alpha > 3:                # CMYK 之类要转 RGB 才能存
                    pix = pymupdf.Pixmap(pymupdf.csRGB, pix)
                out.append(pix.tobytes("png"))
            except Exception:                            # noqa: BLE001 - 单张坏了不该中断整份
                continue
    return out


def docx_images(path: str) -> list[bytes]:
    """docx 里的图片：`word/media/` 下全是，直接取。"""
    out: list[bytes] = []
    with zipfile.ZipFile(path) as z:
        for name in z.namelist():
            low = name.lower()
            if name.startswith("word/media/") and low.endswith((".png", ".jpg", ".jpeg", ".bmp")):
                out.append(z.read(name))
    return out


def extract(path: str, *, size: int = AVATAR_SIZE) -> bytes | None:
    """从一份原件里抽一张证件照（JPEG 字节）。抽不到返回 None。"""
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".pdf":
            blobs = pdf_images(path)
        elif ext == ".docx":
            blobs = docx_images(path)
        else:
            # .txt / .doc / 图片：没有"嵌入图"这回事（.doc 的 OLE 结构要另找库，先不支持）
            return None
    except Exception:                                    # noqa: BLE001 - 抽不出就当没有
        return None

    best, best_score = None, 0.0
    for blob in blobs:
        try:
            im = Image.open(io.BytesIO(blob)).convert("RGB")
        except Exception:                                # noqa: BLE001
            continue
        s = _score(im)
        if s > best_score:
            best, best_score = im, s
    if best is None:
        return None
    try:
        return _jpeg(best, size)
    except Exception:                                    # noqa: BLE001
        return None


def _resume_doc(conn, cid: int) -> str | None:
    """这个人**最新一份** PDF/docx 原件的磁盘路径（绝对或相对 BASE 都可）。"""
    for row in conn.execute(
            "SELECT file_path, archived_path FROM documents WHERE candidate_id = ? "
            "ORDER BY id DESC", (cid,)):
        for p in (row["archived_path"], row["file_path"]):
            if p and os.path.splitext(p)[1].lower() in (".pdf", ".docx"):
                return p
    return None


def _abs(rel: str) -> str:
    from .paths import resolve_data_path

    return resolve_data_path(rel)


def ensure(conn, cid: int, avatars_dir: str, *, size: int = AVATAR_SIZE) -> str | None:
    """确保这个人的头像已经**尝试过**抽取。返回 avatar_path 的当前值。

    - 已经是具体路径或 `"-"` → 直接返回，**不重复抽**（这是懒抽不能变成每次都抽的关键）
    - NULL → 抽一次：抽到就存盘并写路径；抽不到写 `"-"`
    """
    row = conn.execute("SELECT avatar_path FROM candidates WHERE id = ?", (cid,)).fetchone()
    if row is None:
        return None
    cur = row["avatar_path"]
    if cur is not None:
        return cur

    data: bytes | None = None
    doc = _resume_doc(conn, cid)
    if doc:
        full = _abs(doc)
        try:
            if os.path.isfile(full) and os.path.getsize(full) <= MAX_BYTES:
                data = extract(full, size=size)
        except Exception:                                # noqa: BLE001
            data = None

    if not data:
        conn.execute("UPDATE candidates SET avatar_path = ? WHERE id = ?", (NONE_MARK, cid))
        conn.commit()
        return NONE_MARK

    os.makedirs(avatars_dir, exist_ok=True)
    fn = f"{cid}.jpg"
    tmp = os.path.join(avatars_dir, fn + ".part")
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, os.path.join(avatars_dir, fn))       # 原子替换：渲染读不到半张图
    rel = f"data/avatars/{fn}"
    conn.execute("UPDATE candidates SET avatar_path = ? WHERE id = ?", (rel, cid))
    conn.commit()
    return rel


def clear(conn, cid: int) -> None:
    """去掉照片（记 `"-"`，不重抽）。文件留着不管 —— 下次重抽会覆盖同一个文件名。"""
    conn.execute("UPDATE candidates SET avatar_path = ? WHERE id = ?", (NONE_MARK, cid))
    conn.commit()


def reset(conn, cid: int) -> None:
    """重新抽取：先清成 NULL 让 `ensure` 再跑一次。"""
    conn.execute("UPDATE candidates SET avatar_path = NULL WHERE id = ?", (cid,))
    conn.commit()


# ---------------------------------------------------------------- 后台队列
# 为什么必须有这一层：抽图是 **CPU 活**（PyMuPDF 解码 + PIL 缩放），而 FastAPI 的同步端点
# 跑在线程池里、受 GIL 串行。实测「页面上 9 张卡片同时请求头像」时，抽图把线程占满，
# `/api/candidates` 被挤到 **9.3 秒**才返回 —— 卡片根本出不来。
# 所以：**请求线程只读缓存，抽图排进单线程队列在后台做**；抽完前返回 404，
# 卡片先显示"姓首字"，客户端隔 2 秒重试几次，照片就"长"出来了，页面本身不卡。

_QUEUE: "queue.Queue[tuple[str, int, str]]" = queue.Queue()
_SEEN: set[int] = set()
_LOCK = threading.Lock()
_WORKER: threading.Thread | None = None

#: 历史头像补扫状态。只在用户显式触发时运行；不会在升级启动时自动扫全库。
_BACKFILL_LOCK = threading.Lock()
_BACKFILL_THREAD: threading.Thread | None = None
_BACKFILL_STOP = threading.Event()
_BACKFILL: dict = {
    "running": False, "stopping": False, "total": 0, "done": 0,
    "found": 0, "none": 0, "failed": 0, "skipped": 0,
    "started_at": None, "finished_at": None, "last_candidate_id": None,
}


def _loop() -> None:
    from . import db as db_mod

    while True:
        db_path, cid, avatars_dir = _QUEUE.get()
        try:
            conn = db_mod.connect(db_path)
            try:
                ensure(conn, cid, avatars_dir)
            finally:
                conn.close()
        except Exception as exc:                         # noqa: BLE001 - 后台失败不该拖垮服务
            # **不能静默**：这里一旦吞掉，症状是"照片文件在、数据库没写、卡片永远没头像"，
            # 极难自查（实测踩过：文件写出来了、avatar_path 还是 NULL）。
            # 失败要把 id 从去重集合里摘掉，否则这个人到进程重启前都不会再试。
            import sys
            import traceback

            print(f"[avatar] 抽取失败 cid={cid}: {type(exc).__name__}: {exc}", file=sys.stderr)
            traceback.print_exc()
            with _LOCK:
                _SEEN.discard(cid)
        finally:
            _QUEUE.task_done()


def _spawn() -> None:
    global _WORKER
    with _LOCK:
        if _WORKER is None or not _WORKER.is_alive():
            _WORKER = threading.Thread(target=_loop, daemon=True, name="avatar-extract")
            _WORKER.start()


def enqueue(db_path: str, cid: int, avatars_dir: str) -> None:
    """把「给这个人抽照片」排进后台队列。同一个 id 只排一次（进程内去重）。"""
    with _LOCK:
        dup = cid in _SEEN
        if not dup:
            _SEEN.add(cid)
    if not dup:
        _QUEUE.put((db_path, cid, avatars_dir))
    _spawn()                                             # 注意在锁外调用，Lock 不可重入


def backfill_status() -> dict:
    with _BACKFILL_LOCK:
        return dict(_BACKFILL)


def stop_backfill() -> dict:
    with _BACKFILL_LOCK:
        if _BACKFILL.get("running"):
            _BACKFILL["stopping"] = True
            _BACKFILL_STOP.set()
        return dict(_BACKFILL)


def _backfill_loop(db_path: str, avatars_dir: str, ids: list[int]) -> None:
    """串行补齐历史头像。保留 `"-"`，绝不覆盖已人工确认的结果。"""
    from . import db as db_mod

    try:
        for cid in ids:
            if _BACKFILL_STOP.is_set():
                break
            found = False
            try:
                conn = db_mod.connect(db_path)
                try:
                    row = conn.execute(
                        "SELECT avatar_path FROM candidates WHERE id = ?", (cid,)
                    ).fetchone()
                    if row is None or row["avatar_path"] is not None:
                        with _BACKFILL_LOCK:
                            _BACKFILL["skipped"] += 1
                    else:
                        value = ensure(conn, cid, avatars_dir)
                        found = bool(value) and value != NONE_MARK
                        with _BACKFILL_LOCK:
                            if found:
                                _BACKFILL["found"] += 1
                            else:
                                _BACKFILL["none"] += 1
                finally:
                    conn.close()
            except Exception as exc:                       # noqa: BLE001
                import sys
                print(f"[avatar-backfill] cid={cid}: {type(exc).__name__}: {exc}",
                      file=sys.stderr)
                with _BACKFILL_LOCK:
                    _BACKFILL["failed"] += 1
            with _BACKFILL_LOCK:
                _BACKFILL["done"] += 1
                _BACKFILL["last_candidate_id"] = cid
    finally:
        from datetime import datetime
        with _BACKFILL_LOCK:
            _BACKFILL["running"] = False
            _BACKFILL["stopping"] = False
            _BACKFILL["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _BACKFILL_STOP.clear()


def start_backfill(db_path: str, avatars_dir: str) -> dict:
    """启动一次历史头像补扫；已在运行则返回当前进度。"""
    global _BACKFILL_THREAD
    from datetime import datetime

    with _BACKFILL_LOCK:
        if _BACKFILL.get("running"):
            return dict(_BACKFILL)
        from . import db as db_mod

        conn = db_mod.connect(db_path)
        try:
            rows = conn.execute(
                "SELECT id FROM candidates "
                "WHERE merged_into IS NULL AND avatar_path IS NULL ORDER BY id"
            ).fetchall()
        finally:
            conn.close()
        ids = [int(r["id"]) for r in rows]
        _BACKFILL.update({
            "running": bool(ids), "stopping": False, "total": len(ids),
            "done": 0, "found": 0, "none": 0, "failed": 0, "skipped": 0,
            "started_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "finished_at": None if ids else datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "last_candidate_id": None,
        })
        if not ids:
            return dict(_BACKFILL)
        _BACKFILL_STOP.clear()
        _BACKFILL_THREAD = threading.Thread(
            target=_backfill_loop,
            args=(db_path, avatars_dir, ids),
            daemon=True,
            name="avatar-backfill",
        )
        _BACKFILL_THREAD.start()
        return dict(_BACKFILL)
