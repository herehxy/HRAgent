"""HTTP 调用的小工具：**回环地址绕过代理**，**外部地址走代理失败时自动直连重试**。

为什么需要它（两条都是实测踩出来的）：

1. **回环地址**：开发机与院内环境经常设置 `HTTP_PROXY/HTTPS_PROXY`（抓包工具、
   上网行为管理、容器注入等）。`urllib` 默认会把这套代理也用在 `127.0.0.1` 上，
   于是本地明明跑着 Ollama / vLLM，程序却报 `502 Bad Gateway` 或"连接被拒绝"，
   排查方向完全被带偏——看起来像"模型没起来"，实际是代理不转发回环地址。

2. **外部地址 + 失效的代理**：环境里残留一个已经不可用的代理变量（例如从别的
   会话继承来的）时，访问 api.deepseek.com 会直接报
   `WinError 10061 目标计算机积极拒绝`。应用按设计降级到规则通道并在界面标注
   "模型不可用"，但 HR 看不出"其实是代理坏了"。所以这里在**连接层面失败**时
   自动用直连再试一次，并把"实际走了哪条路、为什么重试"记下来，
   供 `/api/meta` 与界面解释（见 `llm.status()` 的 `route`/`proxy_env`）。

重试的安全性：只在 `URLError`（连接被拒/超时/DNS 失败——请求尚未送达）时重试，
不会造成重复提交；对方已给出响应（HTTPError）时一律不重试。
"""
from __future__ import annotations

import os
import urllib.error
import urllib.request
from urllib.parse import urlsplit

_LOOPBACK_HOSTS = {"localhost", "::1", "0.0.0.0"}

#: 显式空代理的 opener：既忽略环境变量，也保留标准的重定向/错误处理行为
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))

#: 代理相关环境变量（大小写两种写法都认，这是各家工具的惯例）
_PROXY_VARS = ("HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy",
               "ALL_PROXY", "all_proxy")

#: 最近一次请求走的路线，供界面解释"为什么模型刚才不可用、现在又通了"
_LAST: dict = {"url": "", "route": "", "note": ""}


def is_loopback(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower().strip("[]")
    if not host:
        return False
    return host in _LOOPBACK_HOSTS or host.startswith("127.")


def proxy_env() -> dict[str, str]:
    """当前环境里的代理设置（仅用于诊断展示，不参与鉴权）。"""
    return {k: os.environ[k] for k in _PROXY_VARS if os.environ.get(k)}


def last_route() -> dict:
    """最近一次请求的路线记录：`{url, route, note}`。

    route ∈ direct（直连）/ proxy（按环境代理）/ direct_fallback（代理失败后改直连）。
    """
    return dict(_LAST)


def urlopen(req, timeout: float = 60):
    """`urllib.request.Request` -> 响应对象。

    - 回环地址：**直连**（不走代理）；
    - 其他地址：先按环境变量走代理；**连接失败且环境里确实配了代理**时，
      自动直连重试一次并记录原因——"代理坏了"不该表现成"模型不可用"。
    """
    url = getattr(req, "full_url", None) or str(req)
    if is_loopback(url):
        _LAST.update({"url": url, "route": "direct", "note": "回环地址直连"})
        return _DIRECT.open(req, timeout=timeout)

    env = proxy_env()
    try:
        resp = urllib.request.urlopen(req, timeout=timeout)
        _LAST.update({
            "url": url,
            "route": "proxy" if env else "direct",
            "note": (f"按环境代理 {list(env.values())[0]}" if env else "环境无代理，直连"),
        })
        return resp
    except urllib.error.URLError as exc:
        if not env:
            _LAST.update({"url": url, "route": "direct", "note": f"直连失败：{exc}"})
            raise
        try:
            resp = _DIRECT.open(req, timeout=timeout)
        except Exception:                                   # noqa: BLE001
            _LAST.update({"url": url, "route": "direct_fallback",
                          "note": f"环境代理不可用（{exc}），直连也失败"})
            raise
        _LAST.update({"url": url, "route": "direct_fallback",
                      "note": f"环境代理不可用（{exc}），已改用直连成功"})
        return resp
