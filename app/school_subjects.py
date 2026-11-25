"""院校的**学科强项**：把"学校整体排名"升级为"学校在某个学科上的实力"。

为什么要有这一层（业务方 2026-11-23）
--------------------------------------
只看综合排名会把行业特色校判得很低：
西安电子科技大学综合第 40（B 档），但在信息与通信/电子/计算机上是 A 档；
北京邮电大学综合第 60（C 档），信息与通信却是 A+。
HR 在计算机、通信、电子岗位上看到的是"这人学校一般"，这与事实相反。

数据分两类，**都必须可追溯**：
1. 确定性数据：`config/school_subjects.json` —— 教育部第四轮学科评估公开结果
   （**只收录 B- 及以上**，380+ 所院校、2900+ 条），叠加可核实的第五轮公开条目
   （逐条标注来源）。命中即用，不经过模型；C 档不属于强项，不收录。
2. 补全数据：名单没覆盖的「学校 × 学科」，由 LLM 给出**粗粒度**判断
   （strong / notable / neutral / unknown + 理由 + 置信度），缓存在
   `data/school_subjects_llm.json`，可在设置里复核/删除。
   **不在每次分析时重新问模型**——同一对学校×学科只会问一次。

口径
----
- `grade` → 校实力档：A+/A/A- → A，B+/B/B- → B，C+/C/C- → C。
- 学科档只用于**本岗位方向命中**的学科；岗位方向不命中时仍按综合排名。
- 最多抬一档（B→A 可以，C→A 不行）。学历门槛不受影响。
- 名单里没有 = 不知道，**不等于差**；不因此降档。
"""
from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
from functools import lru_cache

from . import paths
from . import univ as univ_mod
from .pipeline import majors as mj

CONFIG_PATH = paths.cfg("school_subjects.json")
CACHE_PATH = os.path.join(paths.DATA_DIR, "school_subjects_llm.json")

#: 综合档（A/B/C/D）顺序，索引越小越好。
_BANDS = ("A", "B", "C", "D")
_GRADE_ORDER = {
    "A+": 0, "A": 1, "A-": 2,
    "B+": 3, "B": 4, "B-": 5,
    "C+": 6, "C": 7, "C-": 8,
}
_GRADE_BAND = {
    "A+": "A", "A": "A", "A-": "A",
    "B+": "B", "B": "B", "B-": "B",
    "C+": "C", "C": "C", "C-": "C",
}

#: 学科家族：同一家族里的强项可以互相参考。
#: 例如「计算机科学与技术」的强项，对「软件工程」岗位同样成立；
#: 「信息与通信工程」与「电子科学与技术」同属电子信息。
#: 只用于**岗位方向命中**后的抬档，不改变学科名称的展示。
_FAMILIES = {
    "计算机科学与技术": "计算机", "软件工程": "计算机",
    "网络空间安全": "计算机", "人工智能": "计算机",
    "信息与通信工程": "电子信息", "电子科学与技术": "电子信息",
    "集成电路科学与工程": "电子信息",
    "材料科学与工程": "材料", "冶金工程": "材料",
    "机械工程": "机械", "仪器科学与技术": "仪器",
    "光学工程": "光电", "控制科学与工程": "自动化",
    "化学工程与技术": "化工", "化学": "化学",
    "应用经济学": "经管", "理论经济学": "经管", "工商管理": "经管",
    "管理科学与工程": "经管", "公共管理": "经管", "统计学": "经管",
    "法学": "法学", "外国语言文学": "外语",
    "新闻传播学": "传媒", "戏剧与影视学": "艺术",
    "美术学": "艺术", "设计学": "艺术", "音乐与舞蹈学": "艺术",
    "教育学": "教育", "心理学": "心理", "体育学": "体育",
    "临床医学": "医学", "基础医学": "医学", "口腔医学": "医学",
    "公共卫生与预防医学": "医学", "药学": "医学",
    "中医学": "医学", "中西医结合": "医学",
    "农业工程": "农业", "食品科学与工程": "农业", "作物学": "农业",
    "植物保护": "农业", "畜牧学": "农业", "兽医学": "农业", "草学": "农业",
    "林学": "林业", "风景园林学": "建筑",
    "水利工程": "水利", "土木工程": "土木", "建筑学": "建筑",
    "城乡规划学": "建筑", "测绘科学与技术": "测绘",
    "地质学": "地学", "地质资源与地质工程": "地学",
    "矿业工程": "矿业", "石油与天然气工程": "石油",
    "安全科学与工程": "安全", "海洋科学": "海洋", "大气科学": "大气",
    "天文学": "天文", "物理学": "物理", "数学": "数学",
    "生物学": "生物", "生态学": "生态", "地理学": "地理",
    "地球物理学": "地学", "船舶与海洋工程": "船舶",
    "航空宇航科学与技术": "航空", "兵器科学与技术": "兵器",
    "核科学与技术": "核", "交通运输工程": "交通", "系统科学": "系统",
    "力学": "力学", "动力工程及工程热物理": "动力",
    "电气工程": "电气", "环境科学与工程": "环境",
    "生物医学工程": "生医", "民族学": "民族",
    "图书情报与档案管理": "图情", "考古学": "历史",
    "中国史": "历史", "世界史": "历史", "哲学": "哲学",
    "社会学": "社会学", "政治学": "政治",
    "马克思主义理论": "马理论", "中国语言文学": "中文",
}

_PUNCT = re.compile(r"[\s　()（）\[\]【】<>《》,，、。;；:：/\\|_·+\-—]+")

_CFG_LOCK = threading.Lock()
_CFG_CACHE: dict | None = None
_CACHE_LOCK = threading.RLock()
_CACHE: dict | None = None
_KEY_LOCKS: dict[str, threading.Lock] = {}
_KEY_LOCKS_GUARD = threading.Lock()
_FAILED: dict[str, float] = {}
_FAIL_TTL = 1800.0          # 模型失败后半小时内不再重试，避免每个候选人都卡一次超时


# ---------------------------------------------------------------- 基础工具

def _norm(text: str | None) -> str:
    return _PUNCT.sub("", str(text or "").lower())


def _clean_school(school: str | None) -> str:
    try:
        return univ_mod.clean_school_name(school)
    except Exception:                                        # noqa: BLE001
        return str(school or "").strip()


def _clamp_conf(v) -> float:
    try:
        return max(0.0, min(1.0, float(v)))
    except (TypeError, ValueError):
        return 0.0


def _band_of_grade(grade: str | None) -> str | None:
    g = str(grade or "").strip().upper()
    return _GRADE_BAND.get(g)


def _family(discipline: str | None) -> str:
    d = str(discipline or "").strip()
    return _FAMILIES.get(d, d)


def _uniq(items) -> list:
    out, seen = [], set()
    for x in items:
        if not x or x in seen:
            continue
        seen.add(x)
        out.append(x)
    return out


@lru_cache(maxsize=1)
def _aliases() -> dict[str, list[str]]:
    """学科 → 名称 + 别名（只收长度 >= 3 的别名，避免"法学"命中"方法学"）。"""
    out: dict[str, list[str]] = {}
    for item in mj.catalog().get("majors") or []:
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        vals = [name]
        for a in item.get("aliases") or []:
            a = str(a or "").strip()
            if len(_norm(a)) >= 3:
                vals.append(a)
        out[name] = _uniq([_norm(v) for v in vals if _norm(v)])
    return out


def _load_cfg() -> dict:
    global _CFG_CACHE
    with _CFG_LOCK:
        if _CFG_CACHE is not None:
            return _CFG_CACHE
        data: dict = {}
        try:
            with open(CONFIG_PATH, encoding="utf-8") as fh:
                obj = json.load(fh)
            if isinstance(obj, dict):
                data = obj
        except Exception as exc:                             # noqa: BLE001
            print(f"[school_subjects] 读取 {CONFIG_PATH} 失败："
                  f"{type(exc).__name__}: {exc}", file=sys.stderr)
        _CFG_CACHE = data
        return data


def _auth_table() -> dict[str, dict[str, dict]]:
    """归一名 → {学科: {grade, source}}。支持字符串简写。"""
    out: dict[str, dict[str, dict]] = {}
    for school, subjects in (_load_cfg().get("schools") or {}).items():
        key = _norm(_clean_school(school))
        if not key or not isinstance(subjects, dict):
            continue
        row: dict[str, dict] = {}
        for disc, val in subjects.items():
            if isinstance(val, str):
                meta = {"grade": val, "source": "公开资料"}
            elif isinstance(val, dict):
                meta = {"grade": val.get("grade") or val.get("level") or "",
                        "source": val.get("source") or "公开资料"}
            else:
                continue
            if str(meta.get("grade") or "").strip().upper() in _GRADE_ORDER:
                row[str(disc).strip()] = meta
        if row:
            out[key] = row
    return out


def strengths_for_school(school: str | None, include_llm: bool = False) -> list[dict]:
    """某校的学科强项**全量**列表（按强弱排序）。用于设置页复核/调试。

    档案教育经历**不**用这条——它不管行内专业，会把全校强项挂到每一行；
    逐行展示请用 `subjects_for_major()`。
    `include_llm=False` 时只返回权威名单。
    """
    key = _norm(_clean_school(school))
    if not key:
        return []
    rows: list[dict] = []
    for disc, meta in (_auth_table().get(key) or {}).items():
        grade = str(meta.get("grade") or "").upper()
        band = _band_of_grade(grade)
        if not band:
            continue
        rows.append({"discipline": disc, "grade": grade, "band": band,
                     "source": meta.get("source") or "公开资料",
                     "confidence": 1.0, "reason": ""})
    if include_llm:
        for e in list_cache():
            if _norm(_clean_school(e.get("school"))) == key:
                band = _band_of_grade(e.get("grade"))
                if band:
                    rows.append({"discipline": e.get("discipline"),
                                 "grade": e.get("grade"), "band": band,
                                 "source": "模型判断", "confidence": e.get("confidence"),
                                 "reason": e.get("reason") or ""})
    rows.sort(key=lambda r: (_GRADE_ORDER.get(r["grade"], 99), r["discipline"]))
    return _uniq_rows(rows)


def subjects_for_major(school: str | None, major: str | None,
                       limit: int = 6) -> list[dict]:
    """某校**这一段专业对应**的学科强项（档案教育经历逐段展示用）。

    业务方 2026-11-25 口径：**不要胡乱归类**。
    只认「专业归一到的一级学科」在本校名单里的**精确命中**
    （靠 `majors.json` 的别名归一，例如本科电子信息工程 → 电子科学与技术）。
    本校名单里没有这个学科，就**不显示**，不按同族/大类去凑一个相近学科 ——
    那种"归类"会把光学工程显示成电子信息 A+，反而误导 HR。

    只读权威名单，不调用模型——档案页不能因为补全而变慢。
    """
    if not school or not str(major or "").strip():
        return []
    table = _auth_table().get(_norm(_clean_school(str(school)))) or {}
    if not table:
        return []
    hit = mj.resolve(str(major))
    if not hit:
        return []
    canon = hit["canonical"]
    meta = table.get(canon)
    if not meta:
        return []
    grade = str(meta.get("grade") or "").upper()
    band = _band_of_grade(grade)
    if not band:
        return []
    return [{"discipline": canon, "grade": grade, "band": band,
             "source": meta.get("source") or "公开资料"}][:limit]


def _uniq_rows(rows: list[dict]) -> list[dict]:
    out, seen = [], set()
    for r in rows:
        k = (r.get("discipline"), r.get("grade"), r.get("source"))
        if k in seen:
            continue
        seen.add(k)
        out.append(r)
    return out


# ---------------------------------------------------------------- LLM 补全缓存

def _load_cache() -> dict:
    global _CACHE
    with _CACHE_LOCK:
        if _CACHE is not None:
            return _CACHE
        data: dict = {}
        try:
            with open(CACHE_PATH, encoding="utf-8") as fh:
                obj = json.load(fh)
            if isinstance(obj, dict):
                data = obj
        except Exception:                                    # noqa: BLE001
            data = {}
        if not isinstance(data.get("entries"), dict):
            data["entries"] = {}
        _CACHE = data
        return data


def _save_cache() -> None:
    data = _CACHE or {"version": 1, "entries": {}}
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        tmp = f"{CACHE_PATH}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        os.replace(tmp, CACHE_PATH)
    except OSError as exc:
        print(f"[school_subjects] 写入缓存失败：{type(exc).__name__}: {exc}",
              file=sys.stderr)


def _cache_key(school: str, discipline: str) -> str:
    return f"{_norm(_clean_school(school))}|{_norm(discipline)}"


def _cache_entry(school: str, discipline: str, *, allow_stale: bool = False) -> dict | None:
    with _CACHE_LOCK:
        entry = (_load_cache().get("entries") or {}).get(_cache_key(school, discipline))
        if not isinstance(entry, dict):
            return None
    try:
        age = time.time() - float(entry.get("updated_ts") or 0)
    except (TypeError, ValueError):
        age = 10 ** 12
    if not allow_stale and age > 180 * 86400:                 # 半年后重新问一次
        return None
    return entry


def _put_cache(school: str, discipline: str, *, strength: str, grade: str | None,
               confidence: float, reason: str, model: str,
               source: str = "llm") -> dict:
    key = _cache_key(school, discipline)
    entry = {
        "key": key,
        "school": _clean_school(school),
        "discipline": discipline,
        "strength": strength,
        "grade": grade or "",
        "confidence": round(_clamp_conf(confidence), 2),
        "reason": str(reason or "")[:120],
        "source": source,
        "model": model,
        "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "updated_ts": time.time(),
    }
    with _CACHE_LOCK:
        data = _load_cache()
        data["entries"][key] = entry
        _save_cache()
    return entry


def list_cache() -> list[dict]:
    with _CACHE_LOCK:
        rows = list((_load_cache().get("entries") or {}).values())
    rows.sort(key=lambda e: (str(e.get("school") or ""), str(e.get("discipline") or "")))
    return rows


def put_manual(school: str, discipline: str, grade: str, reason: str = "",
               confidence: float = 1.0) -> dict:
    """人工复核/覆盖一条「学校 × 学科」判断（设置页用）。"""
    g = str(grade or "").strip().upper()
    if g not in _GRADE_ORDER:
        raise ValueError("grade 必须是 A+/A/A-/B+/B/B-/C+/C/C-")
    if not str(school or "").strip() or not str(discipline or "").strip():
        raise ValueError("school / discipline 不能为空")
    band = _band_of_grade(g) or "C"
    strength = "strong" if band == "A" else ("notable" if band == "B" else "neutral")
    return _put_cache(school, discipline, strength=strength, grade=g,
                      confidence=confidence, reason=reason,
                      model="manual", source="manual")


def authoritative() -> dict:
    """权威名单原文（给设置页/接口复核用）。"""
    return dict(_load_cfg().get("schools") or {})


def delete_cache(key: str | None = None) -> int:
    with _CACHE_LOCK:
        data = _load_cache()
        entries = data.get("entries") or {}
        if key:
            n = 1 if entries.pop(key, None) is not None else 0
        else:
            n = len(entries)
            entries.clear()
        data["entries"] = entries
        _save_cache()
        return n


def _key_lock(key: str) -> threading.Lock:
    with _KEY_LOCKS_GUARD:
        lk = _KEY_LOCKS.get(key)
        if lk is None:
            lk = threading.Lock()
            _KEY_LOCKS[key] = lk
        return lk


_LLM_SYSTEM = (
    "你是中国高校学科实力评估员。只判断**一所学校在一个一级学科上**的相对实力，"
    "不判断学校整体名气。必须严格输出 JSON。"
    '{"strength":"strong|notable|neutral|unknown",'
    '"confidence":0.0到1.0,'
    '"reason":"不超过40字，说明依据"}。'
    "口径："
    "strong = 该学科全国前 5%，或教育部评估 A+/A，或该领域公认的行业特色强校；"
    "notable = 该学科全国前 20%，或 A-/B+，或区域/行业内明显强于学校综合排名；"
    "neutral = 一般水平；"
    "unknown = 你不确定、该学科在该校没有明显特色、或没有可靠依据。"
    "**不要因为学校是 985/211 就判 strong**，必须看具体学科；"
    "**不要编造评估等级或名次**；不确定就 unknown。"
)


def _ask_llm(school: str, discipline: str) -> dict | None:
    """问一次模型；失败返回 None（不影响分析，按"无数据"处理）。"""
    try:
        from .agent import llm
    except Exception:                                        # noqa: BLE001
        return None
    try:
        cfg = dict(llm.load_cfg())
    except Exception:                                        # noqa: BLE001
        return None
    if not cfg.get("enabled", True) or not cfg.get("base_url") or not cfg.get("model"):
        return None
    cfg["timeout"] = min(12, int(cfg.get("timeout") or 12))
    cfg["temperature"] = 0
    try:
        r = llm.chat_json(
            _LLM_SYSTEM,
            f"学校：{_clean_school(school)}\n一级学科：{discipline}\n"
            "请只针对这个学科判断，不要评价学校整体。",
            cfg=cfg,
        )
    except Exception as exc:                                 # noqa: BLE001
        print(f"[school_subjects] {school}/{discipline} 模型判断失败："
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return None
    if not isinstance(r, dict):
        return None
    raw = str(r.get("strength") or r.get("level") or r.get("grade") or "").strip().upper()
    # 兼容模型直接吐 A+/A-/B+ 的情况
    if raw in ("A+", "A", "A-", "B+", "B", "B-", "C+", "C", "C-"):
        strength = "strong" if raw.startswith("A") else ("notable" if raw.startswith("B") else "neutral")
        grade = raw
    elif raw in ("STRONG", "强", "很强", "行业强"):
        strength, grade = "strong", "A"
    elif raw in ("NOTABLE", "较强", "中上"):
        strength, grade = "notable", "B+"
    elif raw in ("NEUTRAL", "一般", "普通"):
        strength, grade = "neutral", "C"
    else:
        strength, grade = "unknown", ""
    return {
        "strength": strength,
        "grade": grade,
        "confidence": _clamp_conf(r.get("confidence")),
        "reason": str(r.get("reason") or "").strip()[:120],
        "model": str(cfg.get("model") or ""),
    }


def _usable(entry: dict | None) -> bool:
    """模型判断是否足以参与抬档：strong 要 0.70，notable 要 0.80。"""
    if not isinstance(entry, dict):
        return False
    s = str(entry.get("strength") or "").lower()
    c = _clamp_conf(entry.get("confidence"))
    if s == "strong":
        return c >= 0.70 and _band_of_grade(entry.get("grade")) == "A"
    if s == "notable":
        return c >= 0.80 and _band_of_grade(entry.get("grade")) == "B"
    return False


def _resolve_llm(school: str, discipline: str, allow_llm: bool) -> dict | None:
    entry = _cache_entry(school, discipline)
    if entry is not None:
        return entry
    if not allow_llm:
        return None
    key = _cache_key(school, discipline)
    if time.time() - float(_FAILED.get(key) or 0) < _FAIL_TTL:
        return None
    lk = _key_lock(key)
    if not lk.acquire(blocking=False):
        # 另一个线程正在问同一对，先用已有缓存（没有再按无数据）
        return _cache_entry(school, discipline, allow_stale=True)
    try:
        entry = _cache_entry(school, discipline)
        if entry is not None:
            return entry
        r = _ask_llm(school, discipline)
        if not r:
            _FAILED[key] = time.time()
            return None
        return _put_cache(school, discipline, **r)
    finally:
        lk.release()


# ---------------------------------------------------------------- 方向命中

def _job_fields(jd: dict | None) -> list[str]:
    """岗位明确要求的专业方向（`major_required`）。"""
    out: list[str] = []
    must = (jd or {}).get("must") or {}
    pref = (jd or {}).get("preferred") or {}
    for arr in (must.get("major_required"), pref.get("major_required")):
        if isinstance(arr, str):
            arr = [arr]
        for v in arr or []:
            if v:
                r = mj.resolve(str(v))
                if r:
                    out.append(r["canonical"])
    return _uniq(out)


def _candidate_fields(cand: dict | None) -> list[str]:
    """候选人自己的专业方向（用于岗位没写专业要求时的兜底）。"""
    out: list[str] = []
    c = cand or {}
    for v in (c.get("major_canonical"), c.get("major")):
        if v:
            r = mj.resolve(str(v))
            if r:
                out.append(r["canonical"])
    for h in c.get("edu_history") or []:
        if isinstance(h, dict) and h.get("major"):
            r = mj.resolve(str(h["major"]))
            if r:
                out.append(r["canonical"])
    return _uniq(out)


def target_fields(jd: dict | None, cand: dict | None) -> list[str]:
    """取"本岗位的方向学科"：**岗位写了专业要求就以岗位为准**，
    没写才退回候选人的专业。不能把两者混在一起——否则一个材料岗 + 计算机背景的人，
    会因为候选人专业命中"计算机强项"而把学校抬档，抬的方向与岗位无关。"""
    return _job_fields(jd) or _candidate_fields(cand)


def _blob(jd: dict | None, cand: dict | None) -> str:
    parts: list[str] = []
    j = jd or {}
    for k in ("role", "title", "department", "note"):
        if j.get(k):
            parts.append(str(j[k]))
    must = j.get("must") or {}
    pref = j.get("preferred") or {}
    for k in ("skills_required", "major_required", "education_min"):
        v = must.get(k)
        if isinstance(v, (list, tuple)):
            parts.extend(str(x) for x in v)
        elif v:
            parts.append(str(v))
    for k in ("skills", "major_required"):
        v = pref.get(k)
        if isinstance(v, (list, tuple)):
            parts.extend(str(x) for x in v)
        elif v:
            parts.append(str(v))
    c = cand or {}
    for k in ("major", "major_canonical", "business_direction"):
        if c.get(k):
            parts.append(str(c[k]))
    for s in c.get("skills") or []:
        if isinstance(s, dict):
            if s.get("name"):
                parts.append(str(s["name"]))
        elif s:
            parts.append(str(s))
    return _norm(" ".join(parts))


def _match_authoritative(school: str, jd: dict | None, cand: dict | None) -> list[dict]:
    key = _norm(_clean_school(school))
    table = _auth_table().get(key) or {}
    if not table:
        return []
    targets = target_fields(jd, cand)
    target_fams = {_family(t) for t in targets}
    blob = _blob(jd, cand)
    out: list[dict] = []
    for disc, meta in table.items():
        grade = str(meta.get("grade") or "").upper()
        band = _band_of_grade(grade)
        if not band:
            continue
        # C 档只用于档案展示（"这所学校在这个学科上什么水平"），
        # 不参与分析抬档——否则一个 C+ 学科也可能把 D 档学校抬到 C 档。
        if band not in ("A", "B"):
            continue
        how = ""
        if disc in targets:
            how = "exact"
        elif _family(disc) in target_fams:
            how = "family"
        elif not targets:
            aliases = _aliases().get(disc) or [_norm(disc)]
            if any(a and a in blob for a in aliases):
                how = "alias"
        if not how:
            continue
        out.append({
            "discipline": disc,
            "grade": grade,
            "band": band,
            "source": meta.get("source") or "公开资料",
            "confidence": 1.0,
            "reason": "",
            "matched_by": how,
        })
    out.sort(key=lambda r: (_GRADE_ORDER.get(r["grade"], 99), r["discipline"]))
    return out


def match(school: str | None, jd: dict | None, cand: dict | None,
          allow_llm: bool = True) -> list[dict]:
    """返回与"本岗位方向"命中的学科强项（权威数据 + 受控的 LLM 补全）。"""
    if not school:
        return []
    out = _match_authoritative(school, jd, cand)
    covered = {_family(r["discipline"]) for r in out}
    targets = target_fields(jd, cand)
    # 权威名单没覆盖的方向，才问模型；最多问两个方向，避免一次分析里堆调用。
    for f in targets[:2]:
        fam = _family(f)
        if fam in covered:
            continue
        e = _resolve_llm(school, f, allow_llm)
        if _usable(e):
            out.append({
                "discipline": f,
                "grade": e.get("grade") or "",
                "band": _band_of_grade(e.get("grade")) or "B",
                "source": "模型判断",
                "confidence": e.get("confidence"),
                "reason": e.get("reason") or "",
                "matched_by": "llm",
            })
            covered.add(fam)
    out.sort(key=lambda r: (_GRADE_ORDER.get(r["grade"], 99), r["discipline"]))
    return out


def _effective_band(school: str, matches: list[dict]) -> dict:
    base = univ_mod.school_band(school)
    base_band, base_rank = (base if base else (None, None))
    best = None
    for m in matches:
        b = m.get("band")
        if b in _BANDS and (best is None or _BANDS.index(b) < _BANDS.index(best)):
            best = b
    if not best:
        return {"band": base_band, "rank": base_rank, "base": base_band,
                "subject_band": None, "boosted": False}
    if not base_band:
        return {"band": best, "rank": base_rank, "base": None,
                "subject_band": best, "boosted": False}
    bi, si = _BANDS.index(base_band), _BANDS.index(best)
    # 最多抬一档：综合 C + 学科 A → B，不直接给 A
    eff_i = si if si >= bi - 1 else bi - 1
    eff = _BANDS[min(eff_i, bi)]
    return {"band": eff, "rank": base_rank, "base": base_band,
            "subject_band": best, "boosted": eff != base_band}


def annotate(school: str | None, jd: dict | None, cand: dict | None,
             allow_llm: bool = True) -> dict:
    """给分析提示词/界面用：校实力档 + 命中的学科强项。"""
    school = _clean_school(school) if school else ""
    matches = match(school, jd, cand, allow_llm=allow_llm)
    eff = _effective_band(school, matches)
    return {
        "school": school,
        "matches": matches,
        "base_band": eff.get("base"),
        "band": eff.get("band"),
        "rank": eff.get("rank"),
        "subject_band": eff.get("subject_band"),
        "boosted": eff.get("boosted"),
    }


def format_brief(a: dict) -> str:
    """把 `annotate()` 的结果格式化成一行提示词摘要（不重复计算/调用模型）。"""
    ms = (a or {}).get("matches") or []
    if not ms:
        return ""
    shown = ms[:2]
    txt = "、".join(
        f"{m['discipline']} {m['grade']}"
        + ("（模型判断）" if m.get("source") == "模型判断" else "")
        for m in shown
    )
    tail = ""
    if a.get("boosted"):
        tail = f"；本岗位方向命中，校实力按 {a['band']} 档参考"
    return f"学科强项：{txt}{tail}"


def brief(school: str | None, jd: dict | None, cand: dict | None,
          allow_llm: bool = True) -> str:
    """一行提示词摘要：`学科强项：X A（岗位方向命中）；校实力按 A 档参考`。

    没有命中返回空串，调用方保持原来的 `校实力 X 档·软科第 N` 文案。
    """
    return format_brief(annotate(school, jd, cand, allow_llm=allow_llm))
