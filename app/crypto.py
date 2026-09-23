"""PII 保护：手机号 / 邮箱等个人敏感字段的加密存储与展示脱敏。

设计：
- 加密：AES-256-GCM（`cryptography` 库），随机 12 字节 nonce 前置，密文 + tag 一并 base64。
- 密钥：`config/master.key`（0600 权限，自动生成，**不入版本库**）；
  亦可由环境变量 `TP_MASTER_KEY`（base64 的 32 字节）注入，用于院内部署的集中密钥管理。
- 脱敏：仅用于界面展示，不改变库内密文；脱敏规则对手机号保留前 3 后 4，邮箱保留首字符与域名。

降级策略：若 `cryptography` 不可用（极简环境），退化为"不加密 + 明确标记"，
由 `protection()` 上报真实状态，绝不静默假装已加密。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets

_KEY_BYTES = 32
_ENV_KEY = "TP_MASTER_KEY"

try:  # pragma: no cover - 取决于运行环境
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    _HAVE_AESGCM = True
except Exception:  # pragma: no cover
    AESGCM = None  # type: ignore[assignment]
    _HAVE_AESGCM = False


def _key_path() -> str:
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "config", "master.key")


_CACHE: dict[str, bytes] = {}


def get_key(create: bool = True) -> bytes | None:
    """返回 32 字节主密钥；不可用返回 None。"""
    env = os.environ.get(_ENV_KEY)
    if env:
        try:
            raw = base64.b64decode(env)
            if len(raw) == _KEY_BYTES:
                return raw
            # 也允许直接给 32 字符文本
            if len(env.encode()) == _KEY_BYTES:
                return env.encode()
        except Exception:
            pass
        return hashlib.sha256(env.encode()).digest()

    path = _key_path()
    if path in _CACHE:
        return _CACHE[path]
    if os.path.exists(path):
        try:
            with open(path, "rb") as fh:
                raw = base64.b64decode(fh.read().strip())
            if len(raw) == _KEY_BYTES:
                _CACHE[path] = raw
                return raw
        except Exception:
            pass
        return None
    if not create:
        return None
    raw = secrets.token_bytes(_KEY_BYTES)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(base64.b64encode(raw).decode())
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    _CACHE[path] = raw
    return raw


def protection() -> dict:
    key = get_key()
    return {
        "algorithm": "AES-256-GCM" if _HAVE_AESGCM else "明文（加密库不可用）",
        "encrypted": bool(_HAVE_AESGCM and key),
        "key_source": (
            "环境变量 " + _ENV_KEY if os.environ.get(_ENV_KEY)
            else ("本地密钥文件 config/master.key" if key else "无")
        ),
        "degraded": not (_HAVE_AESGCM and key),
    }


def encrypt(plain: str | None) -> str | None:
    """加密单个字段。空值原样返回；加密不可用时以 'plain:' 前缀显式标记。"""
    if not plain:
        return None
    text = str(plain)
    key = get_key()
    if _HAVE_AESGCM and key:
        nonce = secrets.token_bytes(12)
        blob = AESGCM(key).encrypt(nonce, text.encode("utf-8"), None)
        return "enc:" + base64.b64encode(nonce + blob).decode()
    return "plain:" + text


def decrypt(stored: str | None) -> str | None:
    if not stored:
        return None
    if stored.startswith("plain:"):
        return stored[6:]
    if not stored.startswith("enc:"):
        return stored
    key = get_key(create=False)
    if not (_HAVE_AESGCM and key):
        return None
    try:
        raw = base64.b64decode(stored[4:])
        nonce, blob = raw[:12], raw[12:]
        return AESGCM(key).decrypt(nonce, blob, None).decode("utf-8")
    except Exception:
        return None


# —— 展示脱敏 ——

def mask_phone(value: str | None) -> str:
    if not value:
        return "—"
    digits = "".join(ch for ch in str(value) if ch.isdigit() or ch == "*")
    if len(digits) < 7:
        return "***"
    return digits[:3] + "****" + digits[-4:]


def mask_email(value: str | None) -> str:
    if not value:
        return "—"
    text = str(value)
    if "@" not in text:
        return "***"
    local, _, domain = text.partition("@")
    head = local[0] if local else "*"
    return f"{head}***@{domain}"


def mask_name(value: str | None) -> str:
    """盲筛用：保留姓氏。"""
    if not value:
        return "（已隐藏）"
    text = str(value)
    return text[0] + "*" * max(1, len(text) - 1)


# —— 身份去重键 ——

def hash_field(value: str | None) -> str | None:
    """不可逆散列，供身份比对用（不泄露原值）。"""
    if not value:
        return None
    norm = "".join(str(value).split()).lower()
    if not norm:
        return None
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:32]


def blind_index(value: str | None) -> str | None:
    """带密钥的盲索引：同一密钥下可比对，脱离密钥不可反推。"""
    if not value:
        return None
    norm = "".join(str(value).split()).lower()
    if not norm:
        return None
    key = get_key() or b"tp-fallback-index-key"
    return hmac.new(key, norm.encode("utf-8"), hashlib.sha256).hexdigest()[:40]


# —— 口令散列（用户登录）——

def hash_password(password: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    dk = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt), n=2 ** 14, r=8, p=1, dklen=32)
    return base64.b64encode(dk).decode(), salt


def verify_password(password: str, stored: str, salt: str) -> bool:
    try:
        calc, _ = hash_password(password, salt)
    except Exception:
        return False
    return hmac.compare_digest(calc, stored)
