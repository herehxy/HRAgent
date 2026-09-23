"""检索层：语义召回 + 技能精确召回 + 相似人才。

三件事，优先级从高到低：

1. **技能精确召回**（`by_skills`）：走本体 ID 匹配，结果确定、可解释——
   "找做过真空熔铸的人"这种问题绝不能用语义近似去猜。
2. **语义召回**（`semantic`）：embedding + 余弦相似，用于"找跟这个项目经历像的人"
   这类说不清关键词的需求。
3. **相似人才**（`similar_to`）：以某候选人的画像向量为查询，找库内最像的 N 个人。

embedding 走 OpenAI 兼容接口（Ollama / vLLM / 内网推理服务），
**不可用时自动退化为确定性哈希向量**——功能不中断，只是语义精度下降，
状态会在 `status()` 里如实上报，绝不假装索引是好的。

向量只存本机 SQLite（`embeddings` 表），候选人文本不出内网。
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import urllib.error
import urllib.request

from . import db, net

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE, "config", "embedding.json")

HASH_DIM = 384
_HASH_MODEL = "local-hash-v1"


def load_cfg() -> dict:
    cfg: dict = {"enabled": True, "base_url": "http://127.0.0.1:11434/v1",
                 "model": "embeddinggemma:latest", "timeout": 60, "batch": 16}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as fh:
                cfg.update(json.load(fh))
        except (ValueError, OSError):
            pass
    preset = cfg.get("preset")
    if preset and preset in (cfg.get("_presets") or {}):
        cfg.update(cfg["_presets"][preset])
    if os.environ.get("TP_EMBED_BASE_URL"):
        cfg["base_url"] = os.environ["TP_EMBED_BASE_URL"]
    if os.environ.get("TP_EMBED_MODEL"):
        cfg["model"] = os.environ["TP_EMBED_MODEL"]
    return cfg


# ---------------------------------------------------------------- 哈希兜底向量

def hash_embed(text: str, dim: int = HASH_DIM) -> list[float]:
    """确定性哈希向量：字符 2-gram + 词 落到桶里，L2 归一。

    不是语义模型，但能反映**字面/结构相似度**，且零依赖、零延迟、完全可复现。
    混用中英文都成立（按字符而非分词）。
    """
    vec = [0.0] * dim
    text = (text or "").lower()
    tokens: list[str] = []
    tokens.extend(text.split())
    compact = "".join(ch for ch in text if not ch.isspace())
    tokens.extend(compact[i:i + 2] for i in range(max(0, len(compact) - 1)))
    for tok in tokens:
        if not tok:
            continue
        h = hashlib.blake2b(tok.encode("utf-8"), digest_size=8).digest()
        idx = int.from_bytes(h[:4], "big") % dim
        sign = 1.0 if h[4] & 1 else -1.0
        vec[idx] += sign
    norm = math.sqrt(sum(v * v for v in vec))
    if norm <= 0:
        return vec
    return [v / norm for v in vec]


# ---------------------------------------------------------------- 远程 embedding

def _post_embeddings(texts: list[str], cfg: dict) -> list[list[float]]:
    payload = {"model": cfg.get("model"), "input": texts}
    req = urllib.request.Request(
        f"{str(cfg.get('base_url', '')).rstrip('/')}/embeddings",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {cfg.get('api_key', 'ollama')}"},
        method="POST",
    )
    with net.urlopen(req, timeout=cfg.get("timeout", 60)) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    data = body.get("data") or []
    out = []
    for item in sorted(data, key=lambda d: d.get("index", 0)):
        out.append([float(x) for x in item.get("embedding", [])])
    if len(out) != len(texts):
        raise ValueError(f"embedding 返回条数不匹配：{len(out)} != {len(texts)}")
    return out


def embed(texts: list[str], cfg: dict | None = None,
          allow_fallback: bool = True) -> tuple[list[list[float]], str, str | None]:
    """返回（向量列表，使用的模型名，错误信息）。"""
    cfg = cfg or load_cfg()
    if not texts:
        return [], cfg.get("model", ""), None
    if not cfg.get("enabled", True):
        return [hash_embed(t) for t in texts], _HASH_MODEL, "embedding 已在配置中关闭"
    try:
        batch = max(1, int(cfg.get("batch", 16)))
        out: list[list[float]] = []
        for i in range(0, len(texts), batch):
            out.extend(_post_embeddings(texts[i:i + batch], cfg))
        return out, cfg.get("model", ""), None
    except (urllib.error.URLError, ValueError, KeyError, OSError, TimeoutError) as exc:
        if not allow_fallback:
            raise
        return ([hash_embed(t) for t in texts], _HASH_MODEL,
                f"embedding 服务不可用（{exc}），本次使用本地哈希向量")


def embed_one(text: str, cfg: dict | None = None) -> tuple[list[float], str, str | None]:
    vecs, model, err = embed([text], cfg)
    return (vecs[0] if vecs else []), model, err


# ---------------------------------------------------------------- 相似度

def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return -1.0
    try:
        import numpy as np

        va, vb = np.asarray(a, dtype="float32"), np.asarray(b, dtype="float32")
        na, nb = float(np.linalg.norm(va)), float(np.linalg.norm(vb))
        if na == 0 or nb == 0:
            return -1.0
        return float(va.dot(vb) / (na * nb))
    except ImportError:
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
        if na == 0 or nb == 0:
            return -1.0
        return dot / (na * nb)


def cosine(a: list[float], b: list[float]) -> float:
    return _cosine(a, b)


# ---------------------------------------------------------------- 画像文本与索引

def profile_text(conn, cid: int, max_chars: int = 1500) -> str:
    """把一个人拼成一段可嵌入的画像文本（技能 + 标签 + 摘要 + 简历节选）。"""
    c = db.get_candidate(conn, cid)
    if not c:
        return ""
    skills = db.candidate_skill_rows(conn, cid, verified_only=False)
    grouped: dict[str, list[str]] = {}
    for s in skills:
        grouped.setdefault(s.get("category") or "其他", []).append(s["name"])
    tags = [f"{t['name']}" for t in db.candidate_tag_rows(conn, cid)]
    apps = db.list_applications(conn, cid=cid)
    doc = conn.execute(
        "SELECT raw_text FROM documents WHERE candidate_id = ? AND parse_ok = 1 "
        "ORDER BY id DESC LIMIT 1", (cid,)).fetchone()
    raw = (doc["raw_text"] if doc else "") or ""

    parts = [
        f"学历：{c.get('edu_level') or '未知'}；院校：{c.get('school') or '未知'}；"
        f"专业：{c.get('major') or '未知'}；工作年限：{c.get('years_exp') if c.get('years_exp') is not None else '未知'} 年",
    ]
    if c.get("current_org"):
        parts.append(f"现单位：{c['current_org']}")
    for cat, names in grouped.items():
        parts.append(f"{cat}技能：{'、'.join(names)}")
    if tags:
        parts.append(f"标签：{'、'.join(tags)}")
    if apps:
        parts.append("投递岗位：" + "、".join(
            sorted({a.get("job_title") or "未指定" for a in apps})))
    if raw:
        parts.append("简历摘要：\n" + raw[:max_chars])
    return "\n".join(parts)


def effective_model(cfg: dict) -> str:
    """本次索引**实际会写进库**的模型名，用作"要不要重算"的比对基准。

    必须用"实际模型"而不是"配置里的模型名"：embedding 服务不可达时会自动
    退化为本地哈希模型，此时落库的是 local-hash-v1。若仍拿配置名（如 bge-m3）
    去比对 text_hash，永远查不到行 → 每次都全量重算，"增量"就名存实亡。
    """
    if not cfg.get("enabled", True):
        return _HASH_MODEL
    try:
        embed(["__probe__"], cfg, allow_fallback=False)
        return cfg.get("model", "")
    except Exception:
        return _HASH_MODEL


def build_index(db_path: str, cfg: dict | None = None, force: bool = False,
                limit: int | None = None) -> dict:
    """为所有候选人建立/刷新向量索引。

    force=False 时是**增量**：画像文本哈希未变的人直接跳过，只算新增/变更的人。
    导入新简历后走的就是这条路径（见 server._auto_index），不会全量重建。

    代价说明：为了拿到"实际模型名"，增量模式下会先探测一次 embedding 服务
    （1 条极短文本）。索引为空时跳过探测——那时本来也没有可比对的行。
    """
    cfg = cfg or load_cfg()
    conn = db.connect(db_path)
    try:
        rows = conn.execute(
            "SELECT id FROM candidates WHERE merged_into IS NULL ORDER BY id").fetchall()
        ids = [r["id"] for r in rows]
        if limit:
            ids = ids[:int(limit)]

        has_index = conn.execute(
            "SELECT 1 FROM embeddings WHERE owner_type='candidate' LIMIT 1").fetchone()
        target = effective_model(cfg) if (has_index and not force) else None

        pending: list[int] = []
        texts: list[str] = []
        skipped = 0
        for cid in ids:
            text = profile_text(conn, cid)
            if not text.strip():
                continue
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]
            if not force:
                # 比对基准优先用"实际模型名"；未探测（首次建索引）时退回配置名
                row = conn.execute(
                    "SELECT text_hash FROM embeddings WHERE owner_type='candidate' "
                    "AND owner_id=? AND model IN (?, ?)",
                    (cid, target or cfg.get("model"), cfg.get("model"))).fetchone()
                if row and row["text_hash"] == digest:
                    skipped += 1
                    continue
            pending.append(cid)
            texts.append(text)

        if not pending:
            return {"indexed": 0, "skipped": skipped, "total": len(ids),
                    "model": target or cfg.get("model"),
                    "note": "画像未变化，无需重建（增量索引已跳过全部候选人）"}

        vecs, model, err = embed(texts, cfg)
        for cid, text, vec in zip(pending, texts, vecs):
            digest = hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]
            db.save_embedding(conn, "candidate", cid, model, vec, digest)
        return {"indexed": len(pending), "skipped": skipped,
                "model": model, "dim": len(vecs[0]) if vecs else 0,
                "error": err, "total": len(ids),
                "note": f"增量索引：新增/更新 {len(pending)} 人，跳过未变化 {skipped} 人"}
    finally:
        conn.close()


def _load_vectors(conn) -> tuple[dict[int, list[float]], str]:
    """加载索引。若存在多个模型，优先非兜底模型。"""
    rows = conn.execute(
        "SELECT DISTINCT model FROM embeddings WHERE owner_type='candidate'").fetchall()
    models = [r["model"] for r in rows]
    if not models:
        return {}, ""
    models.sort(key=lambda m: (m == _HASH_MODEL, m))
    best = models[0]
    return dict(db.load_embeddings(conn, best, "candidate")), best


def semantic(conn, query: str, top_k: int = 10, cfg: dict | None = None) -> dict:
    """语义检索候选人。返回 {model, results:[{candidate_id, name, phone, email, score, ...}]}。
    联系方式随结果一并下发，检索列表可直接拨号/发信。"""
    vectors, model = _load_vectors(conn)
    if not vectors:
        return {"model": None, "results": [],
                "note": "索引为空：导入简历后系统会自动建立增量索引，无需手动重建"}

    cfg = cfg or load_cfg()
    qvec, qmodel, err = embed_one(query, cfg)
    if qmodel != model or len(qvec) != len(next(iter(vectors.values()))):
        qvec = hash_embed(query)
    scored = [(cid, _cosine(qvec, vec)) for cid, vec in vectors.items()]
    scored = [s for s in scored if s[1] >= 0]
    scored.sort(key=lambda t: -t[1])
    scored = scored[:max(1, int(top_k))]

    items = {c["id"]: c for c in db.list_candidates(conn)}
    results = []
    for cid, score in scored:
        c = items.get(cid)
        if not c or c.get("archived_at"):
            continue  # 已归档的不再出现在检索结果里
        results.append({
            "candidate_id": cid,
            "name": c.get("name"),
            "score": round(score, 4),
            "tier": c.get("tier_effective"),
            "education": c.get("edu_level"),
            "years": c.get("years_exp"),
            "skills": c.get("skills", [])[:8],
            "matched_skills": [],
            "matched_in": ["语义向量"],
        })
    return {"model": model, "results": results, "error": err}


def similar_to(conn, cid: int, top_k: int = 5) -> dict:
    vectors, model = _load_vectors(conn)
    if cid not in vectors:
        return {"model": model, "results": [], "note": f"#{cid} 尚未建立索引"}
    base = vectors[cid]
    scored = [(oid, _cosine(base, v)) for oid, v in vectors.items() if oid != cid]
    scored.sort(key=lambda t: -t[1])
    items = {c["id"]: c for c in db.list_candidates(conn)}
    results = []
    for oid, score in scored[:max(1, int(top_k))]:
        c = items.get(oid)
        if not c or c.get("archived_at"):
            continue  # 已归档的不再出现在相似人才里
        results.append({"candidate_id": oid, "name": c.get("name"),
                        "score": round(score, 4), "tier": c.get("tier_effective"),
                        "education": c.get("edu_level"), "years": c.get("years_exp"),
                        "skills": c.get("skills", [])[:8]})
    return {"model": model, "results": results, "source": cid}


def by_skills(conn, skills: list[str], mode: str = "all", include_tier: str | None = None) -> dict:
    """技能精确召回（走本体归一，结果确定可解释）。"""
    from .pipeline import normalize as nz

    resolved = nz.resolve_query_terms(skills)
    unknown = [s for s in skills if not nz.canonical_of(s)]
    items = db.search_candidates_by_skills(conn, resolved, mode=mode, verified_only=True)
    if include_tier and include_tier != "ALL":
        items = [i for i in items if i.get("tier_effective") == include_tier]
    items = [i for i in items if not i.get("archived_at")]  # 已归档的不再召回

    results = []
    for c in items:
        rows = {r["name"]: r for r in db.candidate_skill_rows(conn, c["id"], verified_only=True)}
        matched = [s for s in resolved if s in rows]
        results.append({
            "candidate_id": c["id"], "name": c.get("name"), "tier": c.get("tier_effective"),
            "education": c.get("edu_level"), "years": c.get("years_exp"),
            "score": c.get("score"),
            "skills": c.get("skills", [])[:10],
            "matched_skills": matched,
            "evidence": {s: rows[s].get("evidence") for s in matched},
            "matched_in": ["技能本体"],
        })
    return {"query": skills, "resolved": resolved,
            "unknown_terms": unknown, "mode": mode,
            "count": len(results), "results": results}


def status(db_path: str | None = None) -> dict:
    cfg = load_cfg()
    out = {"enabled": bool(cfg.get("enabled", True)), "base_url": cfg.get("base_url"),
           "model": cfg.get("model"), "reachable": False, "error": None,
           "indexed": 0, "index_model": None, "dim": 0}
    if out["enabled"]:
        try:
            vecs, model, err = embed(["连通性测试"], cfg, allow_fallback=False)
            out["reachable"] = True
            out["dim"] = len(vecs[0]) if vecs else 0
            out["error"] = err
        except Exception as exc:
            out["error"] = f"embedding 服务不可达：{exc}（将使用本地哈希向量兜底）"
    if db_path:
        conn = db.connect(db_path)
        try:
            vectors, model = _load_vectors(conn)
            out["indexed"] = len(vectors)
            out["index_model"] = model
            if vectors:
                out["dim"] = len(next(iter(vectors.values())))
            people = conn.execute(
                "SELECT COUNT(*) AS n FROM candidates WHERE merged_into IS NULL").fetchone()["n"]
            out["people"] = people
            out["coverage"] = round(len(vectors) / people, 3) if people else 0.0
        finally:
            conn.close()
    return out
