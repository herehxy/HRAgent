"""对外查询的**硬护栏** + 搜索实现（v2.0.7）。

## 为什么护栏必须写在代码里

助手能拿到用户的原话。只靠提示词说"别把候选人信息发出去"，模型换个说法就绕过去了 ——
而**数据一旦发出去就收不回来**。所以这里是**两道关同时过才放行**：

  ① **必须**命中「公开信息词」：学校名 / 专业名 / 行业通用词
     —— 否则就不是"查学校、查专业、查行业"，一律拒绝；
  ② **不能**命中「个人信息」：库内任何候选人姓名 / 手机号 / 邮箱 / 身份证样式。

任一不满足就拒绝，并把**拒绝原因原样返回给模型**（不能静默失败 ——
静默失败模型会以为是"没搜到"，然后自己编一个答案）。

## 默认不产生任何外发流量

`config/search.json` 没配 `provider` + `api_key`（或 `enabled` 为 false）时，
`search()` 直接返回"未配置"，**不发请求**。开不开、开给谁，由 HR 在配置里决定。

## 支持的 provider

  bocha   博查（国内，中文效果好）  endpoint 默认 https://api.bochaai.com/v1/web-search
  tavily  Tavily                   endpoint 默认 https://api.tavily.com/search
  custom  自定义：POST {query} → 自行解析 items[].title/url/snippet

三者都只需要一个 key；`custom` 额外要 `endpoint`。
"""
from __future__ import annotations

import json
import os
import re

#: 行业 / 公开信息类通用词。命中一个就算"查的是公开信息"，不是查人。
PUBLIC_WORDS = (
    "排名", "学科评估", "评估", "双一流", "一流学科", "软科", "QS", "泰晤士", "USNEWS",
    "录取分", "分数线", "投档", "保研", "考研", "就业", "就业率", "教育部", "学科",
    "专业", "院校", "大学", "学院", "学校", "行业", "政策", "招生", "研究生",
    "硕士", "博士", "本科", "专科", "留学", "认证", "名录", "名单",
    # 用户常直接说层次（"西北农林 985"），这两个词本身就是学校话题
    "985", "211",
)
# ⚠️ **不要把"怎么样 / 什么水平 / 对比 / 比较"这类问句词放进来**：
# 它们不是"话题"，任何问题都能带上（"今天天气怎么样"就命中了），白名单会形同虚设。
# 话题必须来自学校名 / 专业名 / 具体领域词（排名、学科评估…）。

#: 手机号 / 邮箱 / 身份证 —— 出现就说明在拿个人信息去搜
_PII_PATTERNS = (
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "手机号"),
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"), "邮箱"),
    (re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), "身份证号"),
)

#: 常见姓氏（用于兜住"**不在库里的**人名" —— 新投简历的人、同事、第三方）。
#: 库里的人名能精确匹配，库外的只能靠这个启发式。**宁可错杀**：
#: 多拒一次只是让 HR 换个说法，漏一次就是个人信息离开这台机器。
_SURNAMES = (
    "王李张刘陈杨黄赵吴周徐孙马朱胡郭何高林罗郑梁谢宋唐许韩冯邓曹彭曾肖田董袁潘"
    "于蒋蔡余杜叶程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石廖贾夏韦付方白邹孟熊秦邱江"
    "尹薛闫段雷侯龙史陶黎贺顾毛郝龚邵万钱严覃武戴莫孔向汤欧"
)

_CFG_NAME = "search.json"


def _cfg_path(base: str | None = None) -> str:
    if base:
        return os.path.join(base, "config", _CFG_NAME)
    from .paths import cfg

    return cfg(_CFG_NAME)


def load_cfg(base: str | None = None) -> dict:
    """读搜索配置。**读不到就给"未启用"**（默认不发任何请求）。"""
    try:
        with open(_cfg_path(base), encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except Exception:                                    # noqa: BLE001
        return {}


def configured(base: str | None = None) -> bool:
    cfg = load_cfg(base)
    return bool(cfg.get("enabled") and cfg.get("provider") and cfg.get("api_key"))


# ---------------------------------------------------------------- 词表

def _school_terms() -> set[str]:
    """全部学校名与别名（来自 universities.json，含 985/211/强势/海外/排名表）。"""
    from .paths import cfg

    out: set[str] = set()
    try:
        with open(cfg("universities.json"), encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:                                    # noqa: BLE001
        return out
    for key in ("985", "211", "strong", "overseas"):
        for n in cfg.get(key) or []:
            if isinstance(n, str) and n.strip():
                out.add(n.strip())
    for k, v in (cfg.get("aliases") or {}).items():
        out.add(str(k).strip())
        out.add(str(v).strip())
    for k, v in (cfg.get("overseas_aliases") or {}).items():
        out.add(str(k).strip())
        out.add(str(v).strip())
    for n in (cfg.get("rank") or {}):
        out.add(str(n).strip())
    # 去掉过短的（"北大""西电"这种 2 字别名要留，1 字不要 —— 太容易误命中）
    return {t for t in out if len(t) >= 2}


def _major_terms() -> set[str]:
    from .paths import cfg

    out: set[str] = set()
    try:
        with open(cfg("majors.json"), encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:                                    # noqa: BLE001
        return out
    majors = cfg.get("majors")
    if isinstance(majors, dict):
        out.update(str(k).strip() for k in majors)
    elif isinstance(majors, list):
        for m in majors:
            if isinstance(m, str):
                out.add(m.strip())
            elif isinstance(m, dict):
                for k in ("name", "major", "title"):
                    if m.get(k):
                        out.add(str(m[k]).strip())
    for c in cfg.get("categories") or []:
        if isinstance(c, str):
            out.add(c.strip())
        elif isinstance(c, dict) and c.get("name"):
            out.add(str(c["name"]).strip())
    return {t for t in out if len(t) >= 2}


# ---------------------------------------------------------------- 护栏

def _known_terms() -> set[str]:
    """所有"不算人名"的词：学校名/别名 + 专业名 + 公开信息通用词。"""
    return _school_terms() | _major_terms() | set(PUBLIC_WORDS)


def _name_like(query: str) -> str | None:
    """在查询里找**像人名的片段**（库外的人名靠这条兜住）。

    做法：先把已知词（学校/专业/通用词）**从查询里剔掉**，剩下的片段里再找
    「以常见姓氏开头、2~4 字」的连续汉字 —— 那些基本就是人名。
    剔除是必须的一步，否则"西安电子科技大学"里的"西"、"材料工程"里的"材"会被误判；
    反过来"张三""陈志远"剔完还留着，就命中。
    """
    rest = query
    for term in sorted(_known_terms(), key=len, reverse=True):
        if term and term in rest:
            rest = rest.replace(term, " ")
    for chunk in re.split(r"[\s\W_]+", rest):
        if not chunk:
            continue
        for i in range(len(chunk)):
            for size in (2, 3, 4):
                w = chunk[i:i + size]
                if len(w) == size and w[0] in _SURNAMES:
                    return w
    return None


def guard(conn, query: str) -> str | None:
    """**两关同时过才放行**。返回拒绝原因（给模型看），None = 放行。

    ① 个人信息：库内候选人姓名 / 手机号 / 邮箱 / 身份证 → 直接拒
    ② 公开信息词：必须命中学校名 / 专业名 / 行业词之一 → 否则拒
    """
    q = (query or "").strip()
    if not q:
        return "查询为空"
    if len(q) > 120:
        return "查询过长（请压到 120 字以内，只保留学校/专业/行业关键词）"

    # ① 个人信息 —— 先拦这个，理由优先级最高
    for rx, label in _PII_PATTERNS:
        if rx.search(q):
            return (f"查询里含有{label}。对外查询**不允许带任何候选人个人信息**，"
                    f"请只查学校/专业/行业本身（例如「西安电子科技大学 计算机 学科评估」）。")
    try:
        rows = conn.execute("SELECT name FROM candidates").fetchall()
    except Exception:                                    # noqa: BLE001
        rows = []
    for row in rows:
        name = (row["name"] if hasattr(row, "keys") else row[0]) or ""
        name = str(name).strip()
        # 忽略"未识别"这类占位名，以及单字
        if len(name) >= 2 and name not in ("未识别",) and name in q:
            return (f"查询里出现了候选人姓名「{name}」。对外查询**不允许带任何候选人信息**，"
                    f"要了解他的院校背景请用 school_info 查学校本身。")

    # ② 必须是在查"公开信息"
    if not any(w in q for w in PUBLIC_WORDS):
        hit_school = sorted(t for t in _school_terms() if t in q)
        hit_major = sorted(t for t in _major_terms() if t in q)
        if not hit_school and not hit_major:
            return ("只允许查询**学校 / 专业 / 行业**类的公开信息"
                    "（例如「西安电子科技大学 软科排名」「材料科学与工程 学科评估」）。")

    # ③ 库**外**的人名靠启发式兜（库里的人名上面已经精确拦过）
    hit = _name_like(q)
    if hit:
        return (f"查询里疑似出现人名「{hit}」。对外查询**不允许带任何个人信息**，"
                f"请只查学校/专业/行业本身。")
    return None


# ---------------------------------------------------------------- 搜索

def search(query: str, *, base: str | None = None, limit: int = 5) -> dict:
    """真正发请求。**未配置就不发**，返回 {ok: False, reason: ...}。"""
    cfg = load_cfg(base)
    if not (cfg.get("enabled") and cfg.get("provider") and cfg.get("api_key")):
        return {"ok": False, "configured": False,
                "reason": "未配置搜索服务（系统配置里的 search.json）；"
                          "不要编造答案，请告诉 HR「需要先配置搜索服务」。"}
    provider = str(cfg.get("provider")).lower()
    key = str(cfg.get("api_key"))
    timeout = float(cfg.get("timeout") or 20)
    try:
        import urllib.request

        # ⚠️ 绕开系统代理：本机装了代理时连内网/本地会被拦
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        if provider == "bocha":
            url = cfg.get("endpoint") or "https://api.bochaai.com/v1/web-search"
            body = {"query": query, "count": limit, "summary": True}
            req = urllib.request.Request(
                url, data=json.dumps(body).encode("utf-8"), method="POST",
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {key}"})
            with opener.open(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            items = (((data.get("data") or {}).get("webPages") or {}).get("value")) or []
            hits = [{"title": i.get("name"), "url": i.get("url"),
                     "snippet": (i.get("summary") or i.get("snippet") or "")[:400]}
                    for i in items[:limit]]
        elif provider == "tavily":
            url = cfg.get("endpoint") or "https://api.tavily.com/search"
            body = {"api_key": key, "query": query, "max_results": limit}
            req = urllib.request.Request(
                url, data=json.dumps(body).encode("utf-8"), method="POST",
                headers={"Content-Type": "application/json"})
            with opener.open(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            hits = [{"title": i.get("title"), "url": i.get("url"),
                     "snippet": (i.get("content") or "")[:400]}
                    for i in (data.get("results") or [])[:limit]]
        else:                                            # custom
            url = cfg.get("endpoint")
            if not url:
                return {"ok": False, "reason": "provider=custom 时必须配 endpoint"}
            req = urllib.request.Request(
                url, data=json.dumps({"query": query}).encode("utf-8"), method="POST",
                headers={"Content-Type": "application/json",
                         "Authorization": f"Bearer {key}"})
            with opener.open(req, timeout=timeout) as r:
                data = json.loads(r.read().decode("utf-8", "replace"))
            hits = (data.get("items") or [])[:limit]
        return {"ok": True, "provider": provider, "query": query, "results": hits,
                # 硬规矩：回答必须带来源，模型不许把搜索结果的结论当成自己的知识
                "citation_required": True}
    except Exception as exc:                             # noqa: BLE001
        return {"ok": False, "reason": f"搜索失败：{type(exc).__name__}: {exc}"}
