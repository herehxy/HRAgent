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
import ipaddress
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from .. import net

from . import context_governance as cg

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
             api_key: str | None = None, enabled: bool | None = None,
             context_window: str | int | None = None,
             thinking: bool | None = None) -> dict:
    """界面保存模型配置（v1.7.6）。

    base_url / model / enabled / context_window 写 `config/model.json`
    （保留 preset / _presets / 超时等其他字段）；
    api_key 写 `config/secrets.json`（0600，已 gitignore）——**不进 SQLite**：
    settings 表随库备份流动，密钥混进去等于把秘密带进每次备份。
    api_key 传 None 或空串 = 不修改（界面"留空不改"语义）。

    `context_window` 传空串 = 清除人工值、回到自动探测；None = 不修改。

    返回 {"changed": [字段名], "env_override": [会盖过文件值的环境变量名]}。
    """
    changed: list[str] = []
    if (base_url is not None or model is not None or enabled is not None
            or context_window is not None or thinking is not None):
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
        # 思考模式（2026-11-23）：部分模型（Qwen3 等）默认输出思维链。
        # 关闭后请求里带 enable_thinking=false；开启则由模型决定。
        if thinking is not None and bool(cfg.get("thinking", True)) != bool(thinking):
            cfg["thinking"] = bool(thinking)
            changed.append("thinking")
        if context_window is not None:
            raw = str(context_window).strip()
            want = _parse_ctx(raw) if raw else None
            if raw and not want:
                raise ValueError("上下文窗口要写成 32000 / 32k / 1M 这样的数字")
            have = _parse_ctx(cfg.get("context_window"))
            if want != have:
                if want:
                    cfg["context_window"] = want
                else:
                    cfg.pop("context_window", None)
                changed.append("context_window")
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
    _think = cfg.get("thinking")
    # 显式关闭思考：Qwen3 系类端点认 enable_thinking=false；其他模型忽略未知字段
    # 的居多，但为避免严格端点 400，只对 qwen 系发送。
    if _think is False and "enable_thinking" not in payload and (
            "qwen" in model.lower() or "通义" in model):
        payload["enable_thinking"] = False
    if not stream and model.startswith("qwen3") and "enable_thinking" not in payload:
        payload["enable_thinking"] = False
    for k, v in (cfg.get("extra_body") or {}).items():
        payload[k] = v
    return payload


def _merge_system_messages(messages: list[dict]) -> list[dict]:
    """把多条 system 消息合并成**开头的一条**。

    为什么必须合并（2026-11-23 实测）：vLLM + Qwen3.6 的聊天模板只允许
    **一条** system，且必须在第一位；HRAgent 会分别插入「主提示词 / 当前时间 /
    SOP / 会话记忆 / 工具清单 / 附加上下文」多条 system，服务端直接回
    `400 System message must be at the beginning`，表现为“模型不可达”。
    合并后内容一条不少，对接受多条 system 的端点也完全兼容。
    """
    parts: list[str] = []
    rest: list[dict] = []
    for m in messages or []:
        if m.get("role") == "system":
            c = m.get("content")
            if isinstance(c, str) and c.strip():
                parts.append(c.strip())
            continue
        rest.append(m)
    if not parts:
        return list(rest)
    return [{"role": "system", "content": "\n\n".join(parts)}] + rest


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
        "messages": _merge_system_messages(messages),
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
        "messages": _merge_system_messages(messages),
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
    "context_size", "n_ctx", "loaded_context_length", "max_total_tokens",
)
_MODEL_META_CACHE: dict[tuple[str, str], tuple[dict | None, float]] = {}
#: 运行时从“上下文超长”报错里学到的真实窗口。按 (base_url, model) 缓存，
#: 进程内有效；配置里的人工值优先级仍然最高。服务端把 128k 模型开成 32k
#: 时，第一次失败后就能纠正，不再永远按模型卡的上限发。
_MODEL_CTX_OVERRIDE: dict[tuple[str, str], int] = {}
#: 常见模型的厂商默认上下文窗口（tokens）。只在 `config/model.json` 与
#: 端点 `/models` 都拿不到时使用；顺序从上到下，先匹配到的生效。
#: 参数量**不能**决定窗口（同为 9B：GLM-4-9B 是 128k，Gemma-2-9B 是 8k），
#: 所以这里按模型家族/版本写，不按 7B/9B/30B 这类规格猜。
_MODEL_CTX_DEFAULTS: tuple[tuple[str, int], ...] = (
    (r"claude", 200_000),
    (r"gpt-4\.1", 1_000_000),
    (r"gpt-4o|gpt-4-turbo|gpt-4\.5", 128_000),
    (r"gpt-3\.5", 16_385),
    (r"(?:^|[-_])o[134](?:[-_]|$)", 200_000),
    (r"gemini", 1_000_000),
    (r"deepseek", 128_000),
    # 小米 MiMo v2.5/v2.6（含 pro / flash / ultraspeed）官方标称 1M；
    # 更早的 MiMo-7B 是 32K，不能一起按 1M 处理。
    (r"mimo-v2\.(?:5|6)|mimo-v2-pro", 1_048_576),
    (r"mimo", 32_768),
    # Qwen3.5（2026 云端版，含 Flash）官方 1M、最大输出约 64K；
    # Qwen3-Max/Plus/Flash 等云端型号按 128K 起。
    # Qwen3 / Qwen2.5 的本地 GGUF（Ollama 等）通常按 32K 服务，不能用云端值。
    (r"qwen-?3\.5|qwen3\.5", 1_000_000),
    (r"qwen-?3[-_](?:max|plus|flash|turbo)|qwen-?(?:max|plus|flash|turbo)", 131_072),
    (r"qwen|tongyi|qwq|通义", 32_768),
    (r"glm-4|chatglm", 131_072),
    # Llama 3.1/3.2/3.3 与 Llama 4 是 128K；Llama 3.0 只有 8K。
    (r"llama-?3\.[123]|llama-?4", 131_072),
    (r"llama-?3(?:$|[-_:])", 8_192),
    (r"llama", 8_192),
    # Mistral Small / Magistral 128K；Mixtral 与 Mistral 7B 是 32K。
    (r"mistral-(?:small|large)|magistral", 131_072),
    (r"mistral|mixtral", 32_768),
    (r"yi-", 200_000),
    (r"moonshot|kimi", 128_000),
    (r"ernie|文心|hunyuan|混元", 128_000),
    (r"internlm", 32_768),
    (r"baichuan", 32_768),
    # Phi-4-mini 128K；Phi-4 14B 是 16K；Phi-3 是 128K。
    (r"phi-?4-mini", 131_072),
    (r"phi-?4", 16_384),
    (r"phi-?3", 131_072),
    # Gemma 3（1B/4B/12B/27B）128K；Gemma 2 及更早是 8K。
    (r"gemma-?3", 131_072),
    (r"gemma", 8_192),
    (r"minicpm", 32_768),
    (r"command-r", 131_072),
)
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
            # 兼容 Ollama / GGUF 的命名空间键（如 `qwen2.context_length`）：
            # 只看最后一段，否则本地模型的真实窗口永远读不到。
            kl = str(k).lower()
            if kl in _CTX_KEYS or kl.rsplit(".", 1)[-1] in _CTX_KEYS:
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


def _root_of(base: str) -> str:
    """OpenAI 兼容地址的根（去掉 /v1），本地推理服务的管理接口都在根上。"""
    return base[:-3] if base.endswith("/v1") else base


def _is_local_base(base: str) -> bool:
    low = base.lower()
    if "ollama" in low or "11434" in low:
        return True
    host = (urllib.parse.urlsplit(base).hostname or "").lower()
    if host in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
        return True
    try:
        return ipaddress.ip_address(host).is_private
    except ValueError:
        return False


def _http_json(url: str, headers: dict, *, timeout: float = 3.0,
               data: bytes | None = None):
    """GET/POST 一个管理接口；失败返回 None（探测是可选信息，不抛）。"""
    try:
        req = urllib.request.Request(
            url, data=data, headers=headers,
            method="POST" if data is not None else "GET")
        with net.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:                                     # noqa: BLE001
        return None


def _match_model_item(items, model: str):
    if not isinstance(items, list):
        return None
    for it in items:
        if isinstance(it, dict) and str(it.get("id") or it.get("name") or "") == model:
            return it
    for it in items:
        if isinstance(it, dict) and model in str(it.get("id") or it.get("name") or ""):
            return it
    return None


def _probe_model_meta(cfg: dict) -> dict | None:
    """探测模型元数据（缓存 1 小时）。

    优先读**当前服务实际窗口**，而不是模型卡上限：
      1. OpenAI `/models` / `/models/{model}`（vLLM/SGLang 的 max_model_len）
      2. Ollama `/api/ps`（已加载实例的 context_length，即 num_ctx）
      3. llama.cpp `/props`（n_ctx）
      4. LM Studio `/api/v0/models`（max_context_length / loaded_context_length）
      5. TGI `/info`（max_input_tokens）
      6. Ollama `/api/show`（模型架构上限，最后兜底）
    都拿不到时返回不带窗口的元数据（参数量展示仍可用）。
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
    root = _root_of(base)
    hit: dict | None = None
    fallback_hit: dict | None = None

    def _use(obj, source: str) -> bool:
        nonlocal hit
        if isinstance(obj, dict) and _extract_ctx(obj):
            out = dict(obj)
            out["_ctx_source"] = source
            hit = out
            return True
        return False

    with contextlib.redirect_stderr(io.StringIO()):
        # 1) OpenAI 兼容列表：vLLM / SGLang / OpenRouter / 部分 LM Studio
        body = _http_json(f"{base}/models", headers, timeout=4)
        items = body.get("data") if isinstance(body, dict) else body
        item = _match_model_item(items, model)
        if item is not None:
            fallback_hit = item
            _use(item, "provider:/models")
        if hit is None:
            body = _http_json(
                f"{base}/models/{urllib.parse.quote(model, safe='')}",
                headers, timeout=4)
            if isinstance(body, dict):
                fallback_hit = fallback_hit or body
                _use(body, "provider:/models/{model}")

        # 2) Ollama：/api/ps 是实际加载的 num_ctx，比 /api/show 的模型上限准
        if hit is None and _is_local_base(base):
            body = _http_json(f"{root}/api/ps", headers, timeout=3)
            items = body.get("models") if isinstance(body, dict) else None
            item = _match_model_item(items, model)
            if isinstance(item, dict):
                ctx = _extract_ctx(item)
                if ctx:
                    out = dict(item)
                    out["context_length"] = ctx
                    out["_ctx_source"] = "provider:ollama:/api/ps"
                    hit = out

        # 3) llama.cpp server：/props 的 n_ctx 是当前服务窗口
        if hit is None and _is_local_base(base):
            body = _http_json(f"{root}/props", headers, timeout=3)
            _use(body, "provider:llamacpp:/props")

        # 4) LM Studio：/api/v0/models
        if hit is None and _is_local_base(base):
            body = _http_json(f"{root}/api/v0/models", headers, timeout=3)
            items = body.get("data") if isinstance(body, dict) else body
            item = _match_model_item(items, model)
            if item is not None:
                _use(item, "provider:lmstudio:/api/v0/models")

        # 5) TGI：/info（本地或远端都可能）
        if hit is None:
            body = _http_json(f"{root}/info", headers, timeout=3)
            _use(body, "provider:tgi:/info")

        # 6) Ollama /api/show：模型架构上限，只有前面都失败时才用
        if hit is None and _is_local_base(base):
            payload = json.dumps({"name": model, "model": model}).encode("utf-8")
            body = _http_json(
                f"{root}/api/show",
                {**headers, "Content-Type": "application/json"},
                timeout=4, data=payload)
            if isinstance(body, dict):
                fallback_hit = fallback_hit or body
                _use(body, "provider:ollama:/api/show")

    out = hit or fallback_hit
    _MODEL_META_CACHE[key] = (out, now)
    return out


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


def model_default_context_window(model: str) -> int | None:
    """按模型名推断厂商默认窗口；只用于配置与端点元数据都缺失时。"""
    s = str(model or "").strip().lower()
    if not s:
        return None
    for pat, win in _MODEL_CTX_DEFAULTS:
        if re.search(pat, s):
            return win
    return None


def model_context_window(cfg: dict | None = None) -> tuple[int, str]:
    """获取模型实际上下文窗口。

    顺序：`config/model.json` 的 `context_window`（人工确认值）→ 运行时从
    “上下文超长”报错里学到的真实窗口 → 端点管理接口（vLLM /models 的
    max_model_len、Ollama /api/ps、llama.cpp /props、LM Studio、TGI）
    → 按模型名推断厂商默认 → 128k 保守默认。
    返回 `(tokens, source)`，source 为 config/runtime/provider:…/model/fallback。
    """
    cfg = cfg or load_cfg()
    base = str(cfg.get("base_url") or "").rstrip("/")
    model = str(cfg.get("model") or "")
    explicit = _parse_ctx(cfg.get("context_window"))
    if explicit:
        return explicit, "config"
    runtime = _MODEL_CTX_OVERRIDE.get((base, model))
    if runtime:
        return runtime, "runtime"
    hit = _probe_model_meta(cfg)
    ctx = _extract_ctx(hit) if hit else None
    if ctx:
        return ctx, str((hit or {}).get("_ctx_source") or "provider")
    guess = model_default_context_window(model)
    if guess:
        return guess, "model"
    return cg.DEFAULT_CONTEXT_WINDOW, "fallback"


def _exception_text(exc: Exception) -> str:
    parts = [str(exc)]
    if isinstance(exc, urllib.error.HTTPError):
        body = getattr(exc, "_hr_body", None)
        if body is None:
            try:
                body = exc.read().decode("utf-8", "ignore")
            except Exception:                             # noqa: BLE001
                body = ""
            # HTTPError 的 body 只能读一次；缓存到异常上，后面的
            # `_tools_unsupported()` 还要用它判断“不支持 tools”。
            setattr(exc, "_hr_body", body)
        if body:
            parts.append(str(body)[:6000])
    return "\n".join(parts)


_CTX_ERROR_PATTERNS = (
    r"maximum context length is\s*([0-9][0-9,]{2,})",
    r"max(?:imum)?\s+(?:model\s+)?(?:context\s+)?(?:length|window)"
    r"\s*(?:is|of|:)?\s*([0-9][0-9,]{2,})",
    r"context (?:length|window)\s*(?:is|of|:)\s*([0-9][0-9,]{2,})",
    r"max(?:imum)?\s+seq(?:uence)?\s+len(?:gth)?\s*[\(\s]*([0-9][0-9,]{2,})",
    r"input length.*?\[1,\s*([0-9][0-9,]{2,})\]",
    r"([0-9][0-9,]{2,})\s*(?:tokens?\s*)?(?:maximum|max)",
    r"limit of\s*([0-9][0-9,]{2,})",
)


def error_detail(exc: Exception, limit: int = 600) -> str:
    """给界面看的错误详情：带上服务端返回的 body（截断）。

    只显示 `HTTP Error 400` 时，HR 无法知道是模型名、参数还是权限问题；
    把 body 里的 `detail` 带出来，才能直接定位（例如 vLLM 的
    `"model not found"` / `"enable_thinking is not supported"`）。

    这是**诊断信息**，只放在「系统设置 → 模型与密钥」和运行日志里；
    聊天正文不要直接用它，见 `friendly_error()`。
    """
    text = _exception_text(exc).strip()
    return text[:limit]


def friendly_error(exc: Exception) -> str:
    """给聊天正文用的短原因：只说人话，不带服务端原始 body / 密钥细节。

    原始报错仍然通过 `error_detail()` 暴露给系统设置页，排查不受影响；
    聊天正文只保留「哪一类问题、下一步怎么办」。
    """
    if isinstance(exc, urllib.error.HTTPError):
        code = int(getattr(exc, "code", 0) or 0)
        if code in (401, 403):
            return f"模型服务拒绝访问（{code}）：密钥无效或没有调用权限"
        if code == 404:
            return "模型服务地址或模型名不对（404）"
        if code == 429:
            return "模型服务限流（429），请稍后再试"
        if 500 <= code < 600:
            return f"模型服务暂时不可用（{code}）"
        return f"模型服务返回错误（{code}）"
    text = str(exc).lower()
    if "timed out" in text or "timeout" in text:
        return "连接模型服务超时"
    if ("refused" in text or "unreachable" in text or "connection" in text
            or "getaddrinfo" in text or "name or service" in text):
        return "连不上模型服务（网络或地址不通）"
    if "未配置" in text or "not configured" in text:
        return "还没有配置模型"
    if "空答复" in text or "empty" in text:
        return "模型没有返回内容，请再试一次"
    return f"模型调用失败（{type(exc).__name__}）"


def context_overflow_limit(exc: Exception, cfg: dict | None = None,
                           current_window: int | None = None) -> int | None:
    """从“上下文超长”报错里解析真实窗口，并记到进程内覆盖表。

    典型报错：
      · vLLM/OpenAI：`This model's maximum context length is 32768 tokens`
      · Anthropic：`prompt is too long: 208000 tokens > 200000 maximum`
      · 百炼 Qwen：`Range of input length should be [1, 131072]`
    只处理明确“窗口不够”的报错；网络/鉴权/普通参数错误返回 None。
    `current_window` 用于只往小里收：报错数字不小于当前窗口时不记、不重试。
    """
    text = _exception_text(exc)
    if not re.search(r"context|sequence length|input length|too long|max seq",
                     text, re.I):
        return None
    for pat in _CTX_ERROR_PATTERNS:
        m = re.search(pat, text, re.I)
        if not m:
            continue
        try:
            n = int(m.group(1).replace(",", ""))
        except (TypeError, ValueError):
            continue
        if not (1_024 <= n <= 10_000_000):
            continue
        if current_window:
            try:
                if n >= int(current_window):
                    return None
            except (TypeError, ValueError):
                pass
        cfg = cfg or load_cfg()
        key = (str(cfg.get("base_url") or "").rstrip("/"),
               str(cfg.get("model") or ""))
        _MODEL_CTX_OVERRIDE[key] = n
        return n
    return None


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
    _route = net.last_route()
    _route.pop("note", None)     # note 可能带代理 URL（含凭据），不下发
    out = {
        "enabled": bool(cfg.get("enabled", True)),
        "base_url": cfg.get("base_url"),
        "model": cfg.get("model"),
        "reachable": False,
        "model_installed": False,
        "models": [],
        "error": None,
        "hint": "",
        # 只回变量名，不回代理值（值可能是 http://user:pass@host）
        "proxy_env": {k: "（已设置）" for k in net.proxy_env()},
        "route": _route,
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
        # 完整 traceback 只进本机日志，不回给浏览器（可能含路径/环境信息）
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
