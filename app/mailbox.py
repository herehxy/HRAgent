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

from .paths import BASE      # 根目录唯一来源（认 TP_HOME）
from .paths import CONFIG_DIR
from .paths import DATA_DIR  # 数据目录（认 TP_DATA_DIR，可能不在 BASE 下）
CONFIG_PATH = os.path.join(CONFIG_DIR, "mailbox.json")

DEFAULT_CONFIG = {
    "mode": "eml",
    "eml_dir": "data/mail_in",
    "folder_dir": "data/resumes",        # 手工导入简历的本地文件夹（界面上要看得见）
    "archive_dir": "data/archive",
    # v1.27：默认白名单补上图片格式 —— 作品集、成绩单、证书扫描件大量是
    # jpg/png。**不加就会被 extract_attachments 直接丢弃**，等于"收不到"。
    # 它们按 kind='other' 归档，不进人才库（见 classify_attachment）。
    "attachment_ext": [".pdf", ".docx", ".doc", ".txt", ".md",
                       ".jpg", ".jpeg", ".png", ".xlsx", ".xls"],
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
    """把配置里的相对目录解析成绝对路径。

    相对路径里以 `data/` 开头的（eml_dir / folder_dir / archive_dir 的默认值
    都是这个形态），要走 DATA_DIR 而不是 BASE——数据目录可能被挪到用户可写
    位置（TP_DATA_DIR，见 app/paths.py），若仍按 BASE 拼就会又落回只读的
    包内 `data/`，出现"能读不能写"。

    `config/` 开头同理要走 CONFIG_DIR：`imap.password_file` 的默认值就是
    `config/imap.secret`，而打包后 BASE 是 `_internal`，按 BASE 拼会得到
    `_internal/config/imap.secret`——那个文件不存在，于是"邮箱配好了却取不到
    口令"。其余相对路径按 BASE 拼，行为不变。
    """
    if os.path.isabs(value):
        return value
    normalized = value.replace("\\", "/")
    if normalized.startswith("data/"):
        return os.path.join(DATA_DIR, value[5:].replace("/", os.sep))
    if normalized.startswith("config/"):
        return os.path.join(CONFIG_DIR, value[7:].replace("/", os.sep))
    return os.path.join(BASE, value)


# ---------------------------------------------------------------- 配置读写（供界面）

_SECRET_DEFAULT_PATH = os.path.join(CONFIG_DIR, "imap.secret")
# v1.27：发信口令**独立文件**（config/smtp.secret）。
# 为什么必须分开：原来收发信共用 imap.secret，于是"收信 A 邮箱、发信 B 邮箱"
# 这种配置根本配不出来 —— 换发信账号就要把收信账号的授权码顶掉，
# 而同一个服务商下两者授权码往往还不是同一个（企业邮可分开授权）。
# 关键：**老配置不迁移**（见 read_smtp_secret 的回退），避免升级即失效。
_SMTP_SECRET_DEFAULT_PATH = os.path.join(CONFIG_DIR, "smtp.secret")

# UI 可编辑、且允许写回 mailbox.json 的顶层键（白名单，避免误写入口径）
_EDITABLE_KEYS = {"mode", "eml_dir", "folder_dir", "archive_dir", "attachment_ext",
                  "max_attachment_mb", "imap", "dedup",
                  # v1.8.3 发信：SMTP 服务器配置（口令走各自的 .secret 文件，不在这里）
                  "smtp"}


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
    """读取**收信**（IMAP）口令（config/imap.secret，0600）。不存在返回 None。"""
    full = path or _SECRET_DEFAULT_PATH
    if os.path.exists(full):
        with open(full, encoding="utf-8") as fh:
            v = fh.read().strip()
            return v or None
    return None


def write_secret(password: str, path: str | None = None) -> None:
    """写入**收信**（IMAP）口令到密钥文件，权限收紧为 0600（仅本用户可读）。"""
    full = path or _SECRET_DEFAULT_PATH
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as fh:
        fh.write((password or "").strip())
    try:
        os.chmod(full, 0o600)
    except OSError:
        pass  # 某些挂载不支持 chmod，不强求


def read_smtp_secret(path: str | None = None) -> str | None:
    """读取**发信**（SMTP）口令（config/smtp.secret，0600）。

    回退规则（v1.27）：`smtp.secret` 不存在时**回退读 imap.secret**。
    为什么回退而不是直接判无：v1.26 及之前收发信共用一个口令文件，
    升级后若一律判"没口令"，**已经能正常发信的部署会立刻发不出去**。
    一旦 HR 在界面上单独保存了发信授权码，就落到 smtp.secret，两边彻底分开。

    ⚠️ 回退时传的必须是**同一个目录**下的 imap.secret：
    传了 `path` 就不能用无参 `read_secret()`（那会跳到 BASE 下，
    测试里用临时目录时会读到"不存在"→ 回退静默失效，正是本次实测抓到的那条）。
    """
    full = path or _SMTP_SECRET_DEFAULT_PATH
    if os.path.exists(full):
        with open(full, encoding="utf-8") as fh:
            v = fh.read().strip()
            if v:
                return v
    return read_secret(_imap_secret_beside(full))


def _imap_secret_beside(smtp_path: str) -> str:
    """与 `smtp_path` 同目录的 imap.secret 路径。"""
    return os.path.join(os.path.dirname(smtp_path), "imap.secret")


def write_smtp_secret(password: str, path: str | None = None) -> None:
    """写入**发信**（SMTP）口令到独立密钥文件（config/smtp.secret，0600）。"""
    full = path or _SMTP_SECRET_DEFAULT_PATH
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


def _imap_count(data) -> int:
    """把 `IMAP4.select()` 的应答解析成邮件总数（解析不出来返回 0）。

    ⚠️ **必须同时吃 bytes 和 str**。`imaplib` 返回的是 **bytes**
    （`select()` → `[b'2']`），而原来写的是 `str(data[0]).isdigit()`
    —— `str(b'2')` 是 `"b'2'"`，`.isdigit()` **恒为 False**，
    于是 total 永远算成 0。症状是企业微信邮箱/腾讯企业邮上都显示
    「连接成功：INBOX 共 **0** 封，已列出最近 2 封」——**自相矛盾**，
    HR 会以为收件箱是空的、进而怀疑整个配置没生效。
    这个错误与邮箱服务商无关，任何 IMAP 服务器都会中；因为只在连真实
    邮箱时才走到，自检套件（不联网）没有覆盖到，所以一直没被发现。

    抽成独立纯函数是为了能被**离线判据**直接调用（见 tests/selftest.py 的邮件组）。
    """
    if not data:
        return 0
    v = data[0]
    if isinstance(v, (bytes, bytearray)):
        v = v.decode("ascii", "ignore")
    return int(v) if str(v).strip().isdigit() else 0


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
        total = _imap_count(data)
        # ---- v3.1.2：把"为什么这个数"摊开给 HR 看 ----
        # 业务方反馈"有的邮箱显示 1000 封、有的只有 700 封，总数都不对"。
        # 三个数含义不同，混在一起就会互相矛盾：
        #   · exists   —— SELECT 的 EXISTS，**这个文件夹里全部邮件**（含已读/无附件）
        #   · all_ids  —— SEARCH ALL 的结果数，正常应与 exists 相同；
        #                 不等说明服务器对"可搜索范围"有限制（少见）
        #   · resume   —— 其中**带简历附件**的封数，这才是"能收到多少人"
        # 把三者都返回，界面就能写清"共 1000 封，其中 700 封带简历附件"。
        typ, box = conn.search(None, "ALL")
        ids = (box[0].split() if typ == "OK" and box and box[0] else [])
        searchable = len(ids)
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
        # 预览的这几封里带简历附件的数量（只是抽样，不是全量统计）
        _resume_in_sample = sum(1 for m in mails if m.get("has_resume"))
        _note = ""
        if searchable and searchable != total:
            _note = (f"（注意：服务器报告可搜索 {searchable} 封，与文件夹总数 {total} 封"
                     f"不一致 —— 通常不影响收取）")
        return {"ok": True, "account": f"{user} @ {host}", "folder": folder or "INBOX",
                "total": total,
                "searchable": searchable,
                "resume_in_sample": _resume_in_sample,
                "sample_size": len(mails),
                "mails": mails,
                "message": (f"连接成功：{folder or 'INBOX'} 共 {total} 封"
                            f"（这是**这个文件夹里的全部邮件**，含已读、无附件的）；"
                            f"下面列出最近 {len(mails)} 封，其中 {_resume_in_sample} 封带简历附件。"
                            + _note)}
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


# ---------------------------------------------------------------- 附件分类（v1.27）
#
# 问题：求职邮件除简历外常带作品集、成绩单、证书扫描件、身份证/学位证照片。
# 过去**所有**白名单附件都走完整入库链路，于是这些文件解析不出姓名电话，
# 会各自被当成一个"解析失败的新人"塞进人才库 —— 一封多附件邮件能凭空多出几个人。
#
# 判定必须**保守**（宁可漏判成简历，也不要把真简历当附件丢掉）：
#   命中任一"是简历"的强特征 → resume（进完整链路）
#   命中任一"确定不是简历"的强特征 → other（只归档）
#   都不命中 → **看这封邮件有几个附件**：只有一个 → 当简历（保住"单附件裸投"这种
#   最常见的情况）；有多个 → 仍当简历，因为把真简历误判成附件等于丢件，
#   多出来的人可以在人才库里看到并归档，代价远小于丢简历。
_RESUME_HINTS = ("简历", "resume", "cv", "求职信", "自荐信", "应聘")
_OTHER_HINTS = ("作品", "作品集", "成绩单", "学位证", "毕业证", "身份证", "学位证书",
                "毕业证书", "获奖", "证书", "证明", "推荐信", "职称", "论文",
                "专利", "附件材料", "支撑材料", "个人陈述", "transcript", "portfolio")


def classify_attachment(filename: str, total: int = 1) -> str:
    """判断一个附件是简历还是其他材料。返回 'resume' / 'other'。

    ⚠️ v3.1.1：`total` 现在**真的参与判定**了（以前收下参数却没用）。

    为什么改：一封邮件带 `张三.pdf` + `张三-详细版.pdf` 时，两个都不命中
    `_OTHER_HINTS`、都带 .pdf 扩展名 → 都被判成简历 → **建出两个人**。
    HR 的反馈很直接：「同一个人的多份附件应该挂在一个人下面」。

    新口径（`total > 1` 即"这封邮件带了多个附件"）：
      · 命中 `_OTHER_HINTS` → other（确定不是简历）
      · 命中 `_RESUME_HINTS` → resume（明确写了"简历"）
      · 其余 → **other**：多附件邮件里的第二份起，默认当作补充材料。

    这与原来"宁可多建一个人"的取向相反，是**刻意**的：
    原取向的理由是"把真简历误判成附件等于丢件"，但那发生在"一封邮件里
    只有一份文件、它却不是简历"的极端情况；而实际数据里更常见的是
    "一份简历 + 若干补充材料"，误判的代价是人才库里凭空多出几个
    "解析失败的新人"，且会污染统计、去重和岗位计数。
    补充材料归档后在档案的「其他附件」里能看到，不会真的丢。

    `total == 1`（单附件裸投）保持原样：只要不命中 `_OTHER_HINTS` 就当简历 ——
    这是最常见也最不能出错的情况。
    """
    name = (filename or "").lower()
    if any(h in name for h in _OTHER_HINTS):
        return "other"
    if any(h in name for h in _RESUME_HINTS):
        return "resume"
    # 无扩展名/未知类型（如 `1`、`image`）：不猜，当附件
    ext = name.rsplit(".", 1)[-1] if "." in name else ""
    if ext not in ("pdf", "doc", "docx", "txt", "md", "rtf"):
        return "other"
    # 多附件邮件里，第二份及以后默认是补充材料（见 docstring）
    if total > 1:
        return "other"
    return "resume"


def extract_attachments(msg: email.message.Message, cfg: dict) -> list[dict]:
    """抽取白名单内的附件。返回 [{filename, mime, data, kind}]。

    v1.27：每条带 `kind`（resume / other），由 `classify_attachment` 判定。
    上层 `ingest.ingest_mails` 据此分流：resume 走完整入库，other 只归档。
    """
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
    # 先抽完再定 kind：`classify_attachment` 需要知道本封邮件共有几个附件。
    #
    # ⚠️ v3.1.1：**一封邮件最多只认一份简历**。
    # 逐个调用 `classify_attachment` 时它看不到"这是第几份"，于是
    # `张三.pdf` + `张三-详细版.pdf` 两份都被判成简历 → 建出两个人。
    # 这里按顺序走一遍，第一个被判为 resume 的留下，之后即使也命中
    # "简历"字样，只要本封已经有一份了就不再建档（避免重复）。
    _total = len(out)
    _has_resume = False
    for _a in out:
        _kind = classify_attachment(_a["filename"], _total)
        if _kind == "resume" and _has_resume:
            _kind = "other"        # 本封已经有简历了，这份当补充材料
        elif _kind == "resume":
            _has_resume = True
        _a["kind"] = _kind
    # 如果整封邮件一个 resume 都没有（全是"成绩单/证书"），
    # 退回原口径：**第一份**当简历 —— 否则这封邮件就完全没人可建档，
    # 附件散落在"未挂人"状态，比多建一个人更糟。
    if out and not _has_resume:
        out[0]["kind"] = "resume"
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
    """取 IMAP 口令：环境变量 → mailbox.json 指定的文件 → 默认 imap.secret。

    最后一档回退不能省：`password_file` 只在 mailbox.json 里写了才生效，
    而界面保存配置时会把它从白名单外剔除（`_EDITABLE_KEYS` 不含它），
    老库/手工编辑过的 mailbox.json 也可能整条缺失。没有这一档，
    "config/imap.secret 明明存在、界面也显示已配置"，收信却报"未取到口令"。
    """
    env_key = imap_cfg.get("password_env", "TP_IMAP_PASSWORD")
    if os.environ.get(env_key):
        return os.environ[env_key]
    path = imap_cfg.get("password_file")
    if path:
        full = resolve_dir(path)
        if os.path.exists(full):
            with open(full, encoding="utf-8") as fh:
                value = fh.read().strip()
                if value:
                    return value
    return read_secret()


def iter_imap(cfg: dict, cursor: str | None = None, limit: int = 200):
    """IMAP 只读增量拉取（**生成器版**，v1.29.4）。

    为什么拆成生成器：收取进度要"先报总数、再实时报每一封"（业务方要求），
    并且要支持"读完当前这封就停"。原 `fetch_imap` 一次性循环完才返回，
    中途既报不了进度、也没法停。连接持有在生成器生命周期内，
    调用方停止迭代 / close 时 finally 里正常 logout。

    产出：
      ("total", N)   —— 先给总数（随后开始逐封下载）
      ("mail", dict) —— 每封解析好的邮件（含 uid / mailbox）

    只读保证与 `fetch_imap` 完全一致：`select(readonly=True)` 使服务端不会因
    本次会话置位 \\Seen；取正文用 `BODY.PEEK[]`（不带 PEEK 的 BODY[] 会隐式置已读）。
    """
    ic = cfg.get("imap", {})
    if not ic.get("host"):
        raise RuntimeError("未配置 IMAP 服务器（config/mailbox.json -> imap.host）")
    password = _imap_password(ic)
    if not password:
        secret_path = resolve_dir(ic.get("password_file") or "config/imap.secret")
        raise RuntimeError(
            f"未取到邮箱口令：请设置环境变量 {ic.get('password_env', 'TP_IMAP_PASSWORD')} "
            f"或把授权码写入 {secret_path}（该文件不应提交到版本库）")

    ctx = ssl.create_default_context()
    if ic.get("ssl", True):
        conn = imaplib.IMAP4_SSL(ic["host"], int(ic.get("port", 993)), ssl_context=ctx)
    else:
        conn = imaplib.IMAP4(ic["host"], int(ic.get("port", 143)))
    try:
        conn.login(ic.get("user", ""), password)
        folder = ic.get("folder", "INBOX")
        conn.select(folder, readonly=bool(ic.get("readonly", True)))
        # ---- 邮箱里一共有多少封（用于界面显示，与"这次要拉几封"是两件事）----
        # v3.1.2：原来只发一个 `total`，值是**本次待拉数**，界面却当"邮箱总数"
        # 显示。于是"第一次收 800 封、第二次收 3 封"看起来像邮箱里的邮件
        # 忽多忽少 —— 实际是增量拉取正常工作。现在两个数分开报。
        try:
            _t, _d = conn.uid("search", None, "ALL")
            mailbox_total = len([u for u in ((_d[0] if _d else b"") or b"").split() if u]) \
                if _t == "OK" else 0
        except Exception:                                    # noqa: BLE001
            mailbox_total = 0

        start = (int(cursor) + 1) if (cursor or "").isdigit() else 1
        # **先报总数，再报本次待拉数** —— 顺序不能反：
        # 调用方收到 `total` 时要能立刻拼出"邮箱共 N 封，本次新增 M 封"，
        # 否则消息里只有一个数字，又回到"看起来忽多忽少"的老问题。
        yield ("mailbox_total", mailbox_total)
        typ, data = conn.uid("search", None, f"UID {start}:*")
        if typ != "OK":
            yield ("total", 0)
            return
        uids = [u for u in (data[0] or b"").split() if u]
        # ⚠️ **必须在这里过滤掉"回显"的那一封**（v3.1.2 修）。
        # RFC 3501 6.4.8：`UID n:*` 里当 n **大于**邮箱最大 UID 时，
        # 服务器仍会返回**最大 UID 那一封**（`*` 指"当前最大值"，
        # 而 range 两端都含）。后果：游标已经推进到最新之后，
        # **每次点「收取」都会把最后一封重新拉一遍、重新入库一次** ——
        # 带附件的邮件会被反复解析，邮件台账 processed 计数也一直涨。
        # 判据很简单：UID 小于 start 的一律不是"新邮件"。
        uids = [u for u in uids if u.isdigit() and int(u) >= start]
        # 取**最旧**的一批（uids[:limit]），不能取最新的（uids[-limit:]）。
        # UID 升序返回，首次接入存量邮箱时 cursor 为空 → start=1 → 返回全部；
        # 若只保留最新 limit 封、又把游标推到最大 UID，最旧的一批简历就
        # **永久漏收**（下次 start = max+1 直接空结果）。取最旧 + 游标停在
        # 最后成功处理的那封，才保证多轮拉取不丢件。
        uids = uids if int(limit) <= 0 else uids[:int(limit)]   # v1.29.4：limit<=0 = 不限量（"一次收完"用）
        yield ("total", len(uids))
        for uid in uids:
            uid_str = uid.decode()
            typ, payload = conn.uid("fetch", uid, "(BODY.PEEK[])")
            if typ != "OK" or not payload or not isinstance(payload[0], tuple):
                continue
            mail = parse_eml_bytes(payload[0][1], cfg, fallback_id=f"imap:{folder}:{uid_str}")
            mail["uid"] = uid_str
            mail["mailbox"] = folder
            yield ("mail", mail)
    finally:
        try:
            conn.logout()
        except Exception:
            pass


def fetch_imap(cfg: dict, cursor: str | None = None, limit: int = 200) -> tuple[list[dict], str]:
    """IMAP 只读增量拉取（一次性版）。返回（邮件列表，新游标）。

    现在只是 `iter_imap` 的薄封装 —— 拉取逻辑**只有一份**，
    "生成器版"与"一次性版"不可能分叉（游标口径随之自动一致）。
    """
    mails: list[dict] = []
    newest = cursor or ""
    for kind, item in iter_imap(cfg, cursor, limit):
        if kind == "mail":
            mails.append(item)
            newest = item.get("uid") or newest
        # "total" / "mailbox_total" 是给逐封进度模式用的统计事件，
        # 一次性版只关心邮件本身，忽略即可（v3.1.2 新增了后者）。
    return mails, newest


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
