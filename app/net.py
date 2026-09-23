"""HTTP 调用的小工具：**访问本机地址时绕过系统代理**。

为什么需要它：开发机与院内环境经常设置 `HTTP_PROXY/HTTPS_PROXY`
（抓包工具、上网行为管理、容器注入等）。`urllib` 默认会把这套代理也用在
`127.0.0.1` 上，于是本地明明跑着 Ollama / vLLM，程序却报
`502 Bad Gateway` 或"连接被拒绝"，排查方向完全被带偏——
看起来像"模型没起来"，实际上是代理不转发回环地址。

规则很简单：目标是回环地址（127.0.0.0/8、localhost、::1）时**直连**，
其余地址依旧尊重环境变量里的代理设置（院内部署常常依赖代理出网）。
"""
from __future__ import annotations

import urllib.request
from urllib.parse import urlsplit

_LOOPBACK_HOSTS = {"localhost", "::1", "0.0.0.0"}

#: 显式空代理的 opener：既忽略环境变量，也保留标准的重定向/错误处理行为
_DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def is_loopback(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower().strip("[]")
    if not host:
        return False
    return host in _LOOPBACK_HOSTS or host.startswith("127.")


def urlopen(req, timeout: float = 60):
    """`urllib.request.Request` -> 响应对象；回环地址不走代理。"""
    url = getattr(req, "full_url", None) or str(req)
    if is_loopback(url):
        return _DIRECT.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)
