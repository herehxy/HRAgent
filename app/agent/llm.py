"""模型接入层：任意 OpenAI 兼容接口（Ollama / vLLM / DeepSeek / 内网推理服务）。

统一走 /chat/completions + tools（function calling），因此切模型只改 config/model.json：
  Ollama 本地 14B : http://127.0.0.1:11434/v1   model=qwen2.5:14b
  内网 vLLM       : http://<gpu-host>:8000/v1   model=Qwen2.5-14B-Instruct
  DeepSeek        : https://api.deepseek.com/v1 model=deepseek-chat（需 DEEPSEEK_API_KEY）

环境变量 LLM_BASE_URL / LLM_MODEL / LLM_API_KEY 优先于配置文件。
未配置或不可达时，上层自动退回规则模式（工作流仍可用）。
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from .. import net

from ..paths import cfg as _cfg_path
CONFIG_PATH = _cfg_path("model.json")
SECRETS_PATH = _cfg_path("secrets.json")


def mask_key(key) -> str:
    """API Key 掩码：保留前 3 后 4，中间一律 ****。

    这是 key 唯一允许离开服务端的形态——完整 key 永远不下发、不进审计、
    不进数据库。太短（≤8 位）的值连首尾都不给，防止侧漏拼回。
    """
    k = str(key or "")
    if not k:
        return ""
    if len(k) <= 8:
        return "****"
    return k[:3] + "****" + k[-4:]


def save_cfg(base_url: str | None = None, model: str | None = None,
             api_key: str | None = None, enabled: bool | None = None) -> dict:
    """界面保存模型配置（v1.7.6）。

    base_url / model / enabled 写 `config/model.json`（保留 preset / _presets / 超时等其他字段）；
    api_key 写 `config/secrets.json`（0600，已 gitignore）——**不进 SQLite**：
    settings 表随库备份流动，密钥混进去等于把秘密带进每次备份。
    api_key 传 None 或空串 = 不修改（界面"留空不改"语义）。

    返回 {"changed": [字段名], "env_override": [会盖过文件值的环境变量名]}。
    """
    changed: list[str] = []
    if base_url is not None or model is not None or enabled is not None:
        cfg: dict = {}
        if os.path.exists(CONFIG_PATH):
            try:
                with open(CONFIG_PATH, encoding="utf-8") as fh:
                    cfg = json.load(fh)
            except (ValueError, OSError):
                cfg = {}
        # 与文件现值做 diff：没变的字段不写、不记审计（防止"打开就点保存"刷审计）
        if base_url and base_url.strip() and base_url.strip() != cfg.get("base_url"):
            cfg["base_url"] = base_url.strip()
            changed.append("base_url")
        if model and model.strip() and model.strip() != cfg.get("model"):
            cfg["model"] = model.strip()
            changed.append("model")
        # `enabled` 是唯一开关：缺省按开（与 load_cfg 的语义一致）
        if enabled is not None and bool(cfg.get("enabled", True)) != bool(enabled):
            cfg["enabled"] = bool(enabled)
            changed.append("enabled")
        if changed:
            tmp = CONFIG_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, CONFIG_PATH)
    if api_key:
        sec: dict = {}
        if os.path.exists(SECRETS_PATH):
            try:
                with open(SECRETS_PATH, encoding="utf-8") as fh:
                    sec = json.load(fh)
            except (ValueError, OSError):
                sec = {}
        if sec.get("api_key") != api_key.strip():
            sec["api_key"] = api_key.strip()
            # secrets.json 可能已有 base_url/model 覆盖项，一并保留
            tmp = SECRETS_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(sec, fh, ensure_ascii=False, indent=2)
            os.chmod(tmp, 0o600)
            os.replace(tmp, SECRETS_PATH)
            changed.append("api_key")
    env_override = [k for k in ("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY")
                    if os.environ.get(k)]
    return {"changed": changed, "env_override": env_override}


def load_cfg(task: str | None = None) -> dict:
    """读取模型配置。

    `task="extract"` 时支持**任务级模型覆盖**（2026-11-13）：
    简历抽取是批处理、对延迟/成本敏感，可以和对话用不同模型。
    两种写法都支持，优先级从低到高：

        "tasks": {"extract": {"model": "...", "base_url": "...", "api_key": "..."}}
        "extract_model": "...", "extract_base_url": "...", "extract_api_key": "..."

    环境变量仍然最高优先级；任务覆盖只影响传了 task 的调用方。
    """
    cfg: dict = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as fh:
                cfg = json.load(fh)
        except (ValueError, OSError):
            cfg = {}
    # 1) 预设切换：config/model.json 里写 "preset": "deepseek" / "ollama_14b" / "ollama_8b"
    preset = cfg.get("preset")
    if preset and preset in cfg.get("_presets", {}):
        cfg.update(cfg["_presets"][preset])
    # 2) 本地密钥文件（不入库、不提交）：config/secrets.json {"api_key": "..."}
    if os.path.exists(SECRETS_PATH):
        try:
            with open(SECRETS_PATH, encoding="utf-8") as fh:
                sec = json.load(fh)
            for k in ("api_key", "base_url", "model"):
                if sec.get(k):
                    cfg[k] = sec[k]
        except (ValueError, OSError):
            pass
    # 2.5) 任务级覆盖：先嵌套 tasks.<task>，再顶层 <task>_<key>。
    #      只覆盖模型相关键，不把整份任务配置混进主配置。
    _task = str(task or "").strip().lower()
    if _task:
        _over = (cfg.get("tasks") or {}).get(_task) or {}
        for k in ("model", "base_url", "api_key", "temperature", "max_tokens",
                  "timeout", "extra_body", "thinking"):
            if k in _over:
                cfg[k] = _over[k]
        for k in ("model", "base_url", "api_key", "temperature", "max_tokens",
                  "timeout", "extra_body", "thinking"):
            _k = f"{_task}_{k}"
            if _k in cfg:
                cfg[k] = cfg[_k]
    # 3) 环境变量优先级最高
    if os.environ.get("LLM_BASE_URL"):
        cfg["base_url"] = os.environ["LLM_BASE_URL"]
    if os.environ.get("LLM_MODEL"):
        cfg["model"] = os.environ["LLM_MODEL"]
    if os.environ.get("LLM_API_KEY"):
        cfg["api_key"] = os.environ["LLM_API_KEY"]
    elif cfg.get("api_key_env"):
        cfg["api_key"] = os.environ.get(cfg["api_key_env"], cfg.get("api_key", ""))
    return cfg


def _retryable(exc: Exception) -> bool:
    """模型端点的临时故障：网络错误/超时/429/5xx 值得重试；
    400/401/403/404 重试只会白发请求，立即失败让上层决定降级。"""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in (429, 500, 502, 503, 504)
    return isinstance(exc, (urllib.error.URLError, TimeoutError, ConnectionError, OSError))


def _post(path: str, payload: dict, cfg: dict) -> dict:
    """POST 非流式请求；对临时网络故障重试两次（1s/2s 退避）。"""
    last: Exception | None = None
    for attempt in range(3):
        req = urllib.request.Request(
            f"{cfg['base_url'].rstrip('/')}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {cfg.get('api_key', '')}",
            },
            method="POST",
        )
        try:
            with net.urlopen(req, timeout=cfg.get("timeout", 180)) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:                           # noqa: BLE001
            last = exc
            if attempt >= 2 or not _retryable(exc):
                raise
            time.sleep(0.8 * (2 ** attempt))
    raise last if last else RuntimeError("模型请求失败")


def _apply_provider_quirks(payload: dict, cfg: dict, stream: bool = False) -> dict:
    """按服务商/模型补上必要的请求参数（不影响其他模型）。

    Qwen3 系列（百炼兼容端点）默认开启思考模式，**非流式**调用会直接 400：
    `parameter.enable_thinking must be set to false for non-streaming calls`。
    所以只在非流式时对 `qwen3*` 自动补 `enable_thinking=false`；
    流式调用保留思考模式，才能把思维链推给前端。
    `cfg["extra_body"]` 里的键**优先级最高**（换模型或想显式开启时可覆盖）。
    """
    model = str(cfg.get("model") or "")
    if not stream and model.startswith("qwen3") and "enable_thinking" not in payload:
        payload["enable_thinking"] = False
    for k, v in (cfg.get("extra_body") or {}).items():
        payload[k] = v
    return payload


def chat(messages: list[dict], tools: list[dict] | None = None, cfg: dict | None = None,
         tool_choice: str | None = None) -> dict:
    """返回 assistant message（dict）。可能含 tool_calls。

    额外挂 `_usage`（token 用量）与 `_model`，供 `agent_runs` 记录成本与复盘。

    `tool_choice`（v1.25）：
      · None/"auto"（默认）—— 模型自己决定调不调工具；
      · "required"        —— **强制**本轮必须调用工具，不许只回文字。

    为什么需要 "required"：模型有时会说「已为您生成待确认提案」却一个工具都没调，
    于是界面上没有确认按钮、提案也没落库。过去靠出口加一句警告来兜，
    但那只是告诉 HR「没做成」——**用户要的是真做成**。
    强制它重发一次工具调用，提案就真的落库了。
    ⚠️ 只在"已经确认模型该调工具却没调"时使用；普通问答强加 required
    会逼模型对着无关问题乱调工具。
    """
    cfg = cfg or load_cfg()
    payload: dict = {
        "model": cfg.get("model", "qwen2.5:14b"),
        "messages": messages,
        "temperature": cfg.get("temperature", 0),
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice or "auto"
    body = _post("/chat/completions", _apply_provider_quirks(payload, cfg), cfg)
    msg = body["choices"][0]["message"]
    msg["_usage"] = body.get("usage") or {}
    msg["_model"] = body.get("model") or cfg.get("model")
    # 思维链：MiMo / DeepSeek / vLLM 放在 `reasoning_content`，
    # OpenRouter 一类放 `reasoning`。统一挂到 `_reasoning`，
    # 上层（助手循环 / 界面）不用为每个服务商各写一份兼容。
    _think = msg.get("reasoning_content")
    if _think is None:
        _think = msg.get("reasoning")
    msg["_reasoning"] = (_think if isinstance(_think, str) else "") or ""
    return msg


def chat_stream(messages: list[dict], tools: list[dict] | None = None,
                cfg: dict | None = None, tool_choice: str | None = None):
    """流式调用 `/chat/completions`，逐块 yield：`{delta, finish_reason, usage, model}`。

    只依赖 OpenAI 兼容的 SSE 协议，不绑定具体厂商：
      · `delta.content`        —— 答复增量
      · `delta.reasoning_content` / `delta.reasoning` —— 思维链增量（有就推，没有就跳过）
      · `delta.tool_calls`     —— 工具调用增量（由上层按 index/id 拼装）

    兼容两种"假流式"：
      1) 端点忽略 `stream:true`，直接回一个普通 JSON —— 读出来当单块处理；
      2) 端点用 `message` 而不是 `delta` —— 同样当单块处理。
    这样换任何通用 LLM 都不会因为流式支持差异把对话打断。
    """
    cfg = cfg or load_cfg()
    payload: dict = {
        "model": cfg.get("model", "qwen2.5:14b"),
        "messages": messages,
        "temperature": cfg.get("temperature", 0),
        "stream": True,
        # 让兼容端点把 token 用量作为最后一块 SSE 发回来；不支持该参数的
        # 端点通常会忽略它，不影响正常流式输出。
        "stream_options": {"include_usage": True},
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = tool_choice or "auto"
    def _open_stream(p: dict):
        req = urllib.request.Request(
            f"{cfg['base_url'].rstrip('/')}/chat/completions",
            data=json.dumps(p).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream",
                "Authorization": f"Bearer {cfg.get('api_key', '')}",
            },
            method="POST",
        )
        return net.urlopen(req, timeout=cfg.get("timeout", 180))

    try:
        resp = _open_stream(payload)
    except urllib.error.HTTPError as exc:
        # 个别严格端点不认识 stream_options（400/422）：去掉该字段重试一次，
        # 代价只是这批请求拿不到 token usage（上层会用估算兜底）。
        if exc.code in (400, 422) and "stream_options" in payload:
            payload.pop("stream_options", None)
            resp = _open_stream(payload)
        else:
            raise
    with resp:
        ctype = (resp.headers.get("Content-Type") or "").lower()
        if "application/json" in ctype:
            body = json.loads(resp.read().decode("utf-8"))
            msg = (body.get("choices") or [{}])[0].get("message") or {}
            yield {"delta": {
                "content": msg.get("content") or "",
                "reasoning_content": msg.get("reasoning_content") or msg.get("reasoning") or "",
                "tool_calls": msg.get("tool_calls") or [],
            }, "finish_reason": "stop", "usage": body.get("usage") or {},
                "model": body.get("model") or cfg.get("model")}
            return
        import socket

        saw_finish = False
        try:
            for raw in resp:
                line = raw.decode("utf-8", "ignore").strip()
                if not line or line.startswith(":"):
                    continue
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                except ValueError:
                    continue
                choices = chunk.get("choices") or []
                usage = chunk.get("usage") or {}
                if not choices:
                    # 有些服务商单独发一个 usage 块（stream_options.include_usage）
                    yield {"delta": {}, "finish_reason": None,
                           "usage": usage,
                           "model": chunk.get("model") or cfg.get("model")}
                    if usage and saw_finish:
                        break
                    continue
                ch = choices[0]
                delta = ch.get("delta") or ch.get("message") or {}
                yield {"delta": delta, "finish_reason": ch.get("finish_reason"),
                       "usage": usage,
                       "model": chunk.get("model") or cfg.get("model")}
                if ch.get("finish_reason") in ("stop", "length", "tool_calls"):
                    saw_finish = True
                    # 继续读最后一两条，把 usage 块收下来；不立刻 break。
                    # 给 3 秒兜底：有的端点发完 finish 既不发 usage 也不关连接。
                    try:
                        _sock = getattr(getattr(resp, "fp", None), "raw", None)
                        _sock = getattr(_sock, "_sock", None)
                        if _sock is not None:
                            _sock.settimeout(3)
                    except Exception:                     # noqa: BLE001
                        pass
                    if usage:
                        break
        except (TimeoutError, OSError):
            # 正常收尾：拿到 finish 后等 usage 超时，或连接已被服务端关闭
            if not saw_finish:
                raise


def chat_json(system: str, user: str, cfg: dict | None = None) -> dict | None:
    """要求模型输出 JSON（用于抽取/匹配分析等结构化任务）。失败返回 None。"""
    import re

    cfg = cfg or load_cfg()
    payload = {
        "model": cfg.get("model"),
        "temperature": cfg.get("temperature", 0),
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }
    body = _post("/chat/completions", _apply_provider_quirks(payload, cfg), cfg)
    content = body["choices"][0]["message"]["content"].strip()
    content = re.sub(r"^```(?:json)?|```$", "", content).strip()
    return json.loads(content)


def list_models(cfg: dict | None = None) -> list[str]:
    cfg = cfg or load_cfg()
    req = urllib.request.Request(
        f"{cfg['base_url'].rstrip('/')}/models",
        headers={"Authorization": f"Bearer {cfg.get('api_key', '')}"},
    )
    with net.urlopen(req, timeout=8) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return [m.get("id", "") for m in body.get("data", [])]


#: 上下文窗口字段。不同兼容端点叫法不一样，按优先级找第一个能解析的。
_CTX_KEYS = (
    "context_length", "context_window", "max_context_length", "max_model_len",
    "max_input_tokens", "input_token_limit", "max_sequence_length",
    "context_size", "n_ctx",
)
_MODEL_META_CACHE: dict[tuple[str, str], tuple[dict | None, float]] = {}
_PARAM_KEYS = (
    "parameter_count", "parameters", "num_parameters", "param_count",
    "total_params", "model_size", "parameter_size", "params", "size",
)


def _parse_ctx(value) -> int | None:
    """把 128000 / "128k" / "1M" / "1,000,000" 统一成整数。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value > 0:
        return int(value)
    s = str(value or "").strip().lower().replace(",", "").replace("_", "")
    m = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*([km])?$", s)
    if not m:
        return None
    n = float(m.group(1))
    mult = {"k": 1000, "m": 1000000}.get(m.group(2) or "", 1)
    out = int(n * mult)
    return out if out > 0 else None


def _extract_ctx(obj) -> int | None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in _CTX_KEYS:
                n = _parse_ctx(v)
                if n:
                    return n
        for v in obj.values():
            if isinstance(v, (dict, list)):
                n = _extract_ctx(v)
                if n:
                    return n
    elif isinstance(obj, list):
        for v in obj:
            n = _extract_ctx(v)
            if n:
                return n
    return None


def _parse_params(value) -> str | None:
    """把 7B / "7b" / 7000000000 / "70亿" 统一成 "7B" 这类展示值。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and value > 0:
        n = float(value)
        if n >= 1e9:
            s = f"{n / 1e9:.1f}B"
            return s.replace(".0B", "B")
        if n >= 1e6:
            return f"{n / 1e6:.0f}M"
        return None
    s = str(value or "").strip().lower().replace(",", "")
    m = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*([bm])$", s)
    if m:
        return f"{m.group(1)}{m.group(2).upper()}"
    m = re.match(r"^([0-9]+(?:\.[0-9]+)?)\s*(?:billion|亿)$", s)
    if m:
        n = float(m.group(1))
        if "亿" in s:
            n /= 10.0
        out = f"{n:.1f}B"
        return out.replace(".0B", "B")
    return None


def _extract_params(obj) -> str | None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            if str(k).lower() in _PARAM_KEYS:
                p = _parse_params(v)
                if p:
                    return p
        for v in obj.values():
            if isinstance(v, (dict, list)):
                p = _extract_params(v)
                if p:
                    return p
    elif isinstance(obj, list):
        for v in obj:
            p = _extract_params(v)
            if p:
                return p
    return None


def _probe_model_meta(cfg: dict) -> dict | None:
    """探测 `/models` / `/models/{model}` 的模型元数据（缓存 1 小时）。

    失败/不支持不报错，也不把 traceback 打到服务日志——这是可选信息，
    不该污染正常对话日志。
    """
    base = str(cfg.get("base_url") or "").rstrip("/")
    model = str(cfg.get("model") or "")
    if not base or not model:
        return None
    key = (base, model)
    now = time.time()
    cached = _MODEL_META_CACHE.get(key)
    if cached and now - cached[1] < 3600:
        return cached[0]

    headers = {"Authorization": f"Bearer {cfg.get('api_key', '')}"}
    hit: dict | None = None
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            req = urllib.request.Request(f"{base}/models", headers=headers)
            with net.urlopen(req, timeout=4) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            items = body.get("data") if isinstance(body, dict) else body
            if isinstance(items, list):
                for it in items:
                    if isinstance(it, dict) and str(it.get("id") or "") == model:
                        hit = it
                        break
                if hit is None:
                    for it in items:
                        if isinstance(it, dict) and model in str(it.get("id") or ""):
                            hit = it
                            break
        except Exception:                                    # noqa: BLE001
            hit = None
        if hit is None:
            try:
                path = "/models/" + urllib.parse.quote(model, safe="")
                req = urllib.request.Request(f"{base}{path}", headers=headers)
                with net.urlopen(req, timeout=4) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                if isinstance(body, dict):
                    hit = body
            except Exception:                                # noqa: BLE001
                hit = None
    _MODEL_META_CACHE[key] = (hit, now)
    return hit


def model_parameter_count(cfg: dict | None = None) -> str:
    """模型参数量展示值（如 "7B"）；拿不到返回空串，绝不猜。"""
    cfg = cfg or load_cfg()
    explicit = _parse_params(cfg.get("parameter_count"))
    if explicit:
        return explicit
    hit = _probe_model_meta(cfg)
    if hit:
        p = _extract_params(hit)
        if p:
            return p
    # 最后从模型名里认 7b / 14B / 72b 这类明确写出的规格
    m = re.search(r"(?:^|[-_:])(\d+(?:\.\d+)?)\s*[bB](?:\b|$)",
                  str(cfg.get("model") or ""))
    if m:
        return f"{m.group(1)}B"
    return ""


def model_context_window(cfg: dict | None = None) -> tuple[int | None, str]:
    """获取模型实际上下文窗口。

    顺序：`config/model.json` 的 `context_window`（人工确认值）→ 端点
    `/models` 元数据里的 context_length 等字段 → `/models/{model}` → 未知。
    返回 `(tokens, source)`，source 为 config/provider/unknown。
    """
    cfg = cfg or load_cfg()
    explicit = _parse_ctx(cfg.get("context_window"))
    if explicit:
        return explicit, "config"
    hit = _probe_model_meta(cfg)
    ctx = _extract_ctx(hit) if hit else None
    return ctx, ("provider" if ctx else "unknown")


def status() -> dict:
    """给界面/CLI 用的连通性检查。

    除了"通不通"，还要回答"**为什么不通**"——实测摔过两次：
    ① 环境里的代理不转发回环地址，本地 Ollama 明明在跑却报 502；
    ② 环境里残留一个失效代理，访问外部模型端点直接报"连接被拒绝"，
       而界面只显示"模型不可用"，HR 根本无从下手。
    所以这里把 `proxy_env`（环境里的代理变量）、`route`（最近一次实际走的路线）、
    `hint`（按现象给出的下一步）一并返回，界面据此直接告诉人该改什么。
    """
    cfg = load_cfg()
    out = {
        "enabled": bool(cfg.get("enabled", True)),
        "base_url": cfg.get("base_url"),
        "model": cfg.get("model"),
        "reachable": False,
        "model_installed": False,
        "models": [],
        "error": None,
        "hint": "",
        "proxy_env": net.proxy_env(),
        "route": net.last_route(),
        # 用模型的功能清单：模型不可用时这些会退回规则通道，界面要如实列出来
        "features": ["简历字段抽取", "入库自动分析", "今日待办优先级判断",
                     "智能助手对话（含工具调用）", "面试提纲生成"],
    }
    if not out["enabled"]:
        out["error"] = "模型接入已在 config/model.json 中关闭"
        out["hint"] = "想启用：系统配置 → 模型与密钥，把「启用」打开并保存。"
        return out
    try:
        models = list_models(cfg)
        out["reachable"] = True
        out["models"] = models
        want = cfg.get("model", "")
        out["model_installed"] = any(want == m or want in m for m in models)
        if not out["model_installed"]:
            out["error"] = f"服务可达，但未安装模型 {want}（可用：{', '.join(models) or '无'}）"
            out["hint"] = f"把「模型名」改成可用清单里的一个（如 {models[0]}），或先拉取该模型。"
    except (urllib.error.URLError, ValueError, KeyError, OSError) as exc:
        import traceback
        detail = traceback.format_exc()
        out["error"] = f"模型服务不可达：{exc}"
        out["traceback"] = detail
        print(detail, file=sys.stderr)
        if net.is_loopback(out.get("base_url") or ""):
            out["hint"] = ("地址是回环地址，请确认本地模型服务已启动"
                           "（Ollama：`ollama serve`；已装模型用 `ollama list` 查）。")
        elif out["proxy_env"]:
            out["hint"] = (f"环境变量里有代理 {list(out['proxy_env'])[0]}，"
                           f"若该代理不可用会导致外部端点连不上（本次已尝试直连兜底）。"
                           f"要么修好/清掉代理变量，要么改用内网或本地模型。")
        else:
            out["hint"] = "检查模型地址与网络：内网服务确认域名/端口，外部服务确认可出网。"
    return out
