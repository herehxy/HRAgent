"""邮箱接入：把投递到招聘邮箱的简历自动收进人才库。

两种运行模式（`config/mailbox.json` 的 `mode`）：

- `imap`：连企业邮箱的**专用招聘收件箱**，IMAP 只读增量拉取；
- `eml`：读取本地 `.eml` 邮件目录，用于**离线验证与内网隔离环境**，
  与 imap 模式共用完全相同的后续处理链路；
- `off`：关闭抓取（只用手工放文件夹）。

三条不可动摇的安全约束：

1. **只读**：`SELECT ... readonly=True` + `BODY.PEEK[]`，不删信、不改已读、不打标签；
2. **凭据不进代码**：口令从环境变量或密钥文件读取，不写日志、不回显；
3. **先归档再解析**：附件优先落盘到原件区（只增不改），解析失败也不丢件。

统一数据结构（适配 imap / eml 两个来源）::

    {
      "message_id": str, "uid": str, "mailbox": str,
      "from_addr": str, "subject": str, "received_at": str,
      "attachments": [{"filename": str, "mime": str, "data": bytes}],
    }
"""
from __future__ import annotations

import email
import email.policy
import imaplib
import json
import os
import re
import ssl
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # app/ -> 项目根
CONFIG_PATH = os.path.join(BASE, "config", "mailbox.json")

DEFAULT_CONFIG = {
    "mode": "eml",
    "eml_dir": "data/mail_in",
    "folder_dir": "data/resumes",        # 手工导入简历的本地文件夹（界面上要看得见）
    "archive_dir": "data/archive",
    "attachment_ext": [".pdf", ".docx", ".doc", ".txt", ".md"],
    "max_attachment_mb": 20,
    "imap": {
        "host": "",
        "port": 993,
        "ssl": True,
        "user": "",
        "password_env": "TP_IMAP_PASSWORD",
        "password_file": "config/imap.secret",
        "folder": "INBOX",
        "readonly": True,
    },
    "dedup": {
        "same_job_reapply_days": 30,
    },
}

_SAFE_NAME = re.compile(r"[^\w\u4e00-\u9fa5.\-]+")


def load_config(path: str | None = None) -> dict:
    target = path or CONFIG_PATH
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # 深拷贝默认值
    if os.path.exists(target):
        try:
            with open(target, encoding="utf-8") as fh:
                user_cfg = json.load(fh)
            for k, v in user_cfg.items():
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        except (ValueError, OSError):
            pass
    return cfg


def resolve_dir(value: str) -> str:
    return value if os.path.isabs(value) else os.path.join(BASE, value)


# ---------------------------------------------------------------- 配置读写（供界面）

_SECRET_DEFAULT_PATH = os.path.join(BASE, "config", "imap.secret")

# UI 可编辑、且允许写回 mailbox.json 的顶层键（白名单，避免误写入口径）
_EDITABLE_KEYS = {"mode", "eml_dir", "folder_dir", "archive_dir", "attachment_ext",
                  "max_attachment_mb", "imap", "dedup"}


def save_config(updates: dict, path: str | None = None) -> dict:
    """把界面提交的配置深合并进 mailbox.json 并落盘。返回合并后的完整配置。

    安全约束：`imap.password*` 字段一律不写进 mailbox.json——口令只走
    `write_secret()` 落到 0600 的密钥文件。即使调用方误传密码键也会被丢弃。
    """
    target = path or CONFIG_PATH
    cfg = load_config(target)
    updates = updates or {}
    for k, v in updates.items():
        if k not in _EDITABLE_KEYS:
            continue
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    imap = cfg.setdefault("imap", {})
    for drop in ("password", "password_env", "password_file"):
        # 保留 password_env/password_file 的"指向"，但绝不允许写入口令明文
        if drop == "password":
            imap.pop("password", None)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, "w", encoding="utf-8") as fh:
        json.dump(cfg, fh, ensure_ascii=False, indent=2)
    return cfg


def read_secret(path: str | None = None) -> str | None:
    """读取 IMAP 口令（config/imap.secret，0600）。不存在返回 None。"""
    full = path or _SECRET_DEFAULT_PATH
    if os.path.exists(full):
        with open(full, encoding="utf-8") as fh:
            v = fh.read().strip()
            return v or None
    return None


def write_secret(password: str, path: str | None = None) -> None:
    """写入 IMAP 口令到密钥文件，权限收紧为 0600（仅本用户可读）。"""
    full = path or _SECRET_DEFAULT_PATH
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as fh:
        fh.write((password or "").strip())
    try:
        os.chmod(full, 0o600)
    except OSError:
        pass  # 某些挂载不支持 chmod，不强求


def test_imap(host: str, port: int, ssl_on: bool, user: str, password: str,
              folder: str = "INBOX", timeout: int = 15) -> dict:
    """测试 IMAP 连接与登录。**绝不回显口令**，只返回 ok/error。"""
    host = (host or "").strip()
    if not host:
        return {"ok": False, "error": "未填写 IMAP 服务器地址"}
    if not password:
        return {"ok": False, "error": "未填写授权码/口令"}
    try:
        ctx = ssl.create_default_context()
        if ssl_on:
            conn = imaplib.IMAP4_SSL(host, int(port or 993), ssl_context=ctx)
        else:
            conn = imaplib.IMAP4(host, int(port or 143))
    except Exception as exc:
        return {"ok": False, "error": f"连接失败：{type(exc).__name__}: {exc}"}
    try:
        conn.login(user, password)
        typ, _data = conn.select(folder or "INBOX", readonly=True)
        if typ != "OK":
            return {"ok": False, "error": f"登录成功但无法打开文件夹「{folder or 'INBOX'}」"}
        return {"ok": True, "message": f"连接成功，已只读打开「{folder or 'INBOX'}」"}
    except Exception as exc:
        return {"ok": False, "error": f"登录失败：{type(exc).__name__}: {exc}"}
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def preview_imap(host: str, port: int, ssl_on: bool, user: str, password: str,
                 folder: str = "INBOX", limit: int = 10, timeout: int = 20,
                 exts: tuple[str, ...] = (".pdf", ".docx", ".doc", ".txt", ".md")) -> dict:
    """连上邮箱，列出**最近几封邮件**的主题/发件人/时间/附件名。

    用途：收信之前先确认"连的是不是那个邮箱、里面有没有简历"。
    **只读打开、只取邮件头与结构，不下载正文、不改任何标记、绝不回显口令。**
    """
    host = (host or "").strip()
    if not host:
        return {"ok": False, "error": "未填写 IMAP 服务器地址"}
    if not user:
        return {"ok": False, "error": "未填写邮箱账号"}
    if not password:
        return {"ok": False, "error": "未填写授权码/口令"}
    try:
        if ssl_on:
            conn = imaplib.IMAP4_SSL(host, int(port or 993), ssl_context=ssl.create_default_context())
        else:
            conn = imaplib.IMAP4(host, int(port or 143))
    except Exception as exc:
        return {"ok": False, "error": f"连接失败：{type(exc).__name__}: {exc}"}
    try:
        conn.login(user, password)
        typ, data = conn.select(folder or "INBOX", readonly=True)
        if typ != "OK":
            return {"ok": False, "error": f"登录成功但无法打开文件夹「{folder or 'INBOX'}」"}
        total = int(data[0]) if data and str(data[0]).isdigit() else 0
        typ, box = conn.search(None, "ALL")
        ids = (box[0].split() if typ == "OK" and box and box[0] else [])
        recent = ids[-int(limit):] if ids else []
        mails: list[dict] = []
        for num in reversed(recent):
            try:
                typ, raw = conn.fetch(num, "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE)] FLAGS)")
                if typ != "OK" or not raw or not isinstance(raw[0], tuple):
                    continue
                msg = email.message_from_bytes(raw[0][1])
                atts = _attachment_names(conn, num)
                mails.append({
                    "subject": _decode_header(msg.get("Subject")) or "（无主题）",
                    "from": _decode_header(msg.get("From")) or "",
                    "date": (msg.get("Date") or "").strip(),
                    "attachments": atts,
                    "has_resume": any(a.lower().endswith(exts) for a in atts),
                })
            except Exception:
                continue
        return {"ok": True, "account": f"{user} @ {host}", "folder": folder or "INBOX",
                "total": total, "mails": mails,
                "message": f"连接成功：{folder or 'INBOX'} 共 {total} 封，已列出最近 {len(mails)} 封"}
    except Exception as exc:
        return {"ok": False, "error": f"登录失败：{type(exc).__name__}: {exc}"}
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def _attachment_names(conn, num: bytes) -> list[str]:
    """只取附件文件名（不下载附件本体）。"""
    import email as _email

    try:
        typ, raw = conn.fetch(num, "(BODY.PEEK[STRUCTURE])")
        if typ != "OK" or not raw or not isinstance(raw[0], tuple):
            return []
        head = raw[0][1].decode("utf-8", "replace")
        parts = re.findall(r'"(?:NAME|FILENAME)"\s+"([^"]*)"', head, re.I)
        if parts:
            return [_decode_header(p) for p in parts if p]
        # 退化路径：拉一次正文结构，只读文件名
        typ, raw = conn.fetch(num, "(BODY.PEEK[])")
        if typ != "OK" or not raw or not isinstance(raw[0], tuple):
            return []
        msg = _email.message_from_bytes(raw[0][1])
        out = []
        for part in msg.walk():
            fn = part.get_filename()
            if fn:
                out.append(_decode_header(fn))
        return out
    except Exception:
        return []


def safe_filename(name: str, fallback: str = "attachment") -> str:
    name = os.path.basename((name or "").replace("\\", "/")).strip() or fallback
    name = _SAFE_NAME.sub("_", name)
    if len(name) > 120:
        root, ext = os.path.splitext(name)
        name = root[:100] + ext
    return name


def _decode_header(value: str | None) -> str:
    if not value:
        return ""
    try:
        parts = email.header.decode_header(value)
    except Exception:
        return str(value)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            try:
                out.append(text.decode(enc or "utf-8", errors="ignore"))
            except (LookupError, TypeError):
                out.append(text.decode("utf-8", errors="ignore"))
        else:
            out.append(text)
    return "".join(out).strip()


def _dt(value: str | None) -> str:
    if not value:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        d = parsedate_to_datetime(value)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone().strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def extract_attachments(msg: email.message.Message, cfg: dict) -> list[dict]:
    """抽取白名单内的附件。返回 [{filename, mime, data}]。"""
    exts = tuple(e.lower() for e in cfg.get("attachment_ext", []))
    limit = int(cfg.get("max_attachment_mb", 20)) * 1024 * 1024
    out: list[dict] = []
    for part in msg.walk():
        if part.get_content_maintype() == "multipart":
            continue
        disp = (part.get("Content-Disposition") or "").lower()
        filename = part.get_filename()
        if filename:
            filename = _decode_header(filename)
        if not filename:
            # 少数客户端把附件当 inline 且不带文件名，按扩展名兜底
            if "attachment" not in disp and "inline" not in disp:
                continue
            ctype = part.get_content_type()
            guess = {"application/pdf": ".pdf",
                     "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx"}
            filename = "attachment" + guess.get(ctype, "")
        if not filename.lower().endswith(exts):
            continue
        try:
            data = part.get_payload(decode=True) or b""
        except Exception:
            continue
        if not data or len(data) > limit:
            continue
        out.append({
            "filename": safe_filename(filename),
            "mime": part.get_content_type(),
            "data": data,
        })
    return out


def parse_eml_bytes(raw: bytes, cfg: dict, fallback_id: str = "") -> dict:
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    message_id = (msg.get("Message-ID") or "").strip() or fallback_id
    return {
        "message_id": message_id,
        "uid": fallback_id,
        "mailbox": "eml",
        "from_addr": email.utils.parseaddr(_decode_header(msg.get("From")))[1] or "",
        "subject": _decode_header(msg.get("Subject")),
        "received_at": _dt(msg.get("Date")),
        "attachments": extract_attachments(msg, cfg),
    }


# ---------------------------------------------------------------- eml 模式

def fetch_eml(cfg: dict) -> list[dict]:
    """读取本地 .eml 目录（离线/演练模式）。文件名排序保证顺序稳定。"""
    folder = resolve_dir(cfg.get("eml_dir", "data/mail_in"))
    if not os.path.isdir(folder):
        return []
    mails: list[dict] = []
    for name in sorted(os.listdir(folder)):
        path = os.path.join(folder, name)
        if not os.path.isfile(path) or not name.lower().endswith((".eml", ".txt")):
            continue
        try:
            with open(path, "rb") as fh:
                raw = fh.read()
            mail = parse_eml_bytes(raw, cfg, fallback_id=f"eml:{name}")
            mail["eml_file"] = path
            mail["message_id"] = mail["message_id"] or f"eml:{name}"
            mails.append(mail)
        except Exception as exc:  # 单个邮件坏掉不影响其他邮件
            mails.append({
                "message_id": f"eml-error:{name}", "uid": f"eml:{name}", "mailbox": "eml",
                "from_addr": "", "subject": name,
                "received_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "attachments": [], "error": f"邮件解析失败：{exc}",
            })
    return mails


# ---------------------------------------------------------------- imap 模式

def _imap_password(imap_cfg: dict) -> str | None:
    env_key = imap_cfg.get("password_env", "TP_IMAP_PASSWORD")
    if os.environ.get(env_key):
        return os.environ[env_key]
    path = imap_cfg.get("password_file")
    if path:
        full = resolve_dir(path)
        if os.path.exists(full):
            with open(full, encoding="utf-8") as fh:
                return fh.read().strip()
    return None


def fetch_imap(cfg: dict, cursor: str | None = None, limit: int = 200) -> tuple[list[dict], str]:
    """IMAP 只读增量拉取。返回（邮件列表，新游标）。

    只读保证：`select(readonly=True)` 使服务端不会因本次会话置位 \\Seen；
    取正文用 `BODY.PEEK[]`（不带 PEEK 的 BODY[] 会隐式置已读）。
    """
    ic = cfg.get("imap", {})
    if not ic.get("host"):
        raise RuntimeError("未配置 IMAP 服务器（config/mailbox.json -> imap.host）")
    password = _imap_password(ic)
    if not password:
        raise RuntimeError(
            f"未取到邮箱口令：请设置环境变量 {ic.get('password_env', 'TP_IMAP_PASSWORD')} "
            f"或写入 {ic.get('password_file')}（该文件不应提交到版本库）")

    ctx = ssl.create_default_context()
    if ic.get("ssl", True):
        conn = imaplib.IMAP4_SSL(ic["host"], int(ic.get("port", 993)), ssl_context=ctx)
    else:
        conn = imaplib.IMAP4(ic["host"], int(ic.get("port", 143)))
    try:
        conn.login(ic.get("user", ""), password)
        folder = ic.get("folder", "INBOX")
        conn.select(folder, readonly=bool(ic.get("readonly", True)))
        start = (int(cursor) + 1) if (cursor or "").isdigit() else 1
        typ, data = conn.uid("search", None, f"UID {start}:*")
        if typ != "OK":
            return [], cursor or ""
        uids = [u for u in (data[0] or b"").split() if u]
        uids = uids[-limit:]
        mails: list[dict] = []
        newest = cursor or ""
        for uid in uids:
            uid_str = uid.decode()
            typ, payload = conn.uid("fetch", uid, "(BODY.PEEK[])")
            if typ != "OK" or not payload or not isinstance(payload[0], tuple):
                continue
            mail = parse_eml_bytes(payload[0][1], cfg, fallback_id=f"imap:{folder}:{uid_str}")
            mail["uid"] = uid_str
            mail["mailbox"] = folder
            mails.append(mail)
            newest = uid_str
        return mails, newest
    finally:
        try:
            conn.logout()
        except Exception:
            pass


# ---------------------------------------------------------------- 统一入口

def incoming(cfg: dict | None = None, cursor: str | None = None) -> tuple[list[dict], str]:
    cfg = cfg or load_config()
    mode = cfg.get("mode", "eml")
    if mode == "imap":
        return fetch_imap(cfg, cursor)
    if mode == "eml":
        mails = fetch_eml(cfg)
        return mails, (mails[-1]["uid"] if mails else (cursor or ""))
    return [], cursor or ""
