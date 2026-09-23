"""模型接入层：任意 OpenAI 兼容接口（Ollama / vLLM / DeepSeek / 内网推理服务）。

统一走 /chat/completions + tools（function calling），因此切模型只改 config/model.json：
  Ollama 本地 14B : http://127.0.0.1:11434/v1   model=qwen2.5:14b
  内网 vLLM       : http://<gpu-host>:8000/v1   model=Qwen2.5-14B-Instruct
  DeepSeek        : https://api.deepseek.com/v1 model=deepseek-chat（需 DEEPSEEK_API_KEY）

环境变量 LLM_BASE_URL / LLM_MODEL / LLM_API_KEY 优先于配置文件。
未配置或不可达时，上层自动退回规则模式（工作流仍可用）。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from .. import net

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
CONFIG_PATH = os.path.join(_BASE_DIR, "config", "model.json")


def load_cfg() -> dict:
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
    secrets_path = os.path.join(_BASE_DIR, "config", "secrets.json")
    if os.path.exists(secrets_path):
        try:
            with open(secrets_path, encoding="utf-8") as fh:
                sec = json.load(fh)
            for k in ("api_key", "base_url", "model"):
                if sec.get(k):
                    cfg[k] = sec[k]
        except (ValueError, OSError):
            pass
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


def _post(path: str, payload: dict, cfg: dict) -> dict:
    req = urllib.request.Request(
        f"{cfg['base_url'].rstrip('/')}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg.get('api_key', '')}",
        },
        method="POST",
    )
    with net.urlopen(req, timeout=cfg.get("timeout", 180)) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _apply_provider_quirks(payload: dict, cfg: dict) -> dict:
    """按服务商/模型补上必要的请求参数（不影响其他模型）。

    目前只有一条：**Qwen3 系列（百炼兼容端点）默认开启思考模式**，
    非流式调用会直接 400：
    `parameter.enable_thinking must be set to false for non-streaming calls`。
    本项目全部走非流式 + JSON 输出，思考模式只会拖慢并污染 JSON，
    所以对 `qwen3*` 模型自动补 `enable_thinking=false`；
    `cfg["extra_body"]` 里的键**优先级最高**（换模型或想显式开启时可覆盖）。
    """
    model = str(cfg.get("model") or "")
    if model.startswith("qwen3") and "enable_thinking" not in payload:
        payload["enable_thinking"] = False
    for k, v in (cfg.get("extra_body") or {}).items():
        payload[k] = v
    return payload


def chat(messages: list[dict], tools: list[dict] | None = None, cfg: dict | None = None) -> dict:
    """返回 assistant message（dict）。可能含 tool_calls。

    额外挂 `_usage`（token 用量）与 `_model`，供 `agent_runs` 记录成本与复盘。
    """
    cfg = cfg or load_cfg()
    payload: dict = {
        "model": cfg.get("model", "qwen2.5:14b"),
        "messages": messages,
        "temperature": cfg.get("temperature", 0),
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    body = _post("/chat/completions", _apply_provider_quirks(payload, cfg), cfg)
    msg = body["choices"][0]["message"]
    msg["_usage"] = body.get("usage") or {}
    msg["_model"] = body.get("model") or cfg.get("model")
    return msg


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


def status() -> dict:
    """给界面/CLI 用的连通性检查。"""
    cfg = load_cfg()
    out = {
        "enabled": bool(cfg.get("enabled", True)),
        "base_url": cfg.get("base_url"),
        "model": cfg.get("model"),
        "reachable": False,
        "model_installed": False,
        "models": [],
        "error": None,
    }
    if not out["enabled"]:
        out["error"] = "模型接入已在 config/model.json 中关闭"
        return out
    try:
        models = list_models(cfg)
        out["reachable"] = True
        out["models"] = models
        want = cfg.get("model", "")
        out["model_installed"] = any(want == m or want in m for m in models)
        if not out["model_installed"]:
            out["error"] = f"服务可达，但未安装模型 {want}（可用：{', '.join(models) or '无'}）"
    except (urllib.error.URLError, ValueError, KeyError, OSError) as exc:
        out["error"] = f"模型服务不可达：{exc}；请确认 Ollama/vLLM 已启动"
    return out
