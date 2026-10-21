"""发信（SMTP）：把已经人工确认过的草稿真正发出去。

**这个模块只负责"发"，不负责"决定发什么"**：
正文由模板渲染（`app/mail_template.py`）、由 HR 在界面上确认后才走到这里。
这一层不做任何自动触发——「所有邮件都要人工确认」是明确的产品口径。

关于认证：SMTP 用**授权码**（不是邮箱登录密码）。163 / QQ / 腾讯企业邮都要在邮箱
设置里单独生成一个"客户端授权码"。

v1.27：**发信与收信是两组独立配置**。原来发信直接复用 `config/imap.secret`
且账号回退到收信账号，理由是"163 的收信与发信用同一个授权码"——但那只对
**同一家服务商的同一账号**成立。单位里的实际情形是：招聘收件箱与对外发件
经常是两个号（甚至两个服务商），此时复用口令必然配不出来。现在发信口令走
`config/smtp.secret`、账号只读 `smtp.user`，老配置由 `mb.read_smtp_secret` 回退兼容。
"""
from __future__ import annotations

import mimetypes
import os
import smtplib
import ssl
from email.header import Header
from email.mime.application import MIMEApplication
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, parseaddr

from . import mailbox as mb

# 常见但 Windows 注册表/mimetypes 可能缺失的类型。
for _ext, _type in {
    ".heic": "image/heic", ".heif": "image/heif",
    ".webp": "image/webp", ".wps": "application/vnd.ms-works",
    ".et": "application/vnd.ms-excel", ".dps": "application/vnd.ms-powerpoint",
    ".7z": "application/x-7z-compressed", ".rar": "application/vnd.rar",
}.items():
    mimetypes.add_type(_type, _ext)

#: 常见服务商的发信服务器（与收信预设配对）
SMTP_PRESETS = [
    {"label": "网易 163（单位在用）", "host": "smtp.163.com", "port": 465, "ssl": True},
    {"label": "腾讯企业邮", "host": "smtp.exmail.qq.com", "port": 465, "ssl": True},
    {"label": "QQ 邮箱", "host": "smtp.qq.com", "port": 465, "ssl": True},
    {"label": "阿里云企业邮", "host": "smtp.qiye.aliyun.com", "port": 465, "ssl": True},
    {"label": "Outlook / Microsoft 365", "host": "smtp.office365.com", "port": 587, "ssl": False},
]


def load_smtp(cfg: dict | None = None) -> dict:
    """当前 SMTP 发信配置。

    v1.27：**发信账号与收信账号彻底分开**。原来这里是
    `smtp.user or imap.user` —— 发信账号没填就悄悄用收信账号，
    于是"收信用 A 邮箱、发信用 B 邮箱"根本配不出来，而且界面上看不出在用谁的号。
    现在只读 `smtp.user`，没填就是没填（`user_same_as_imap=False`），
    由界面明确提示"发信账号还没单独填"，而不是替HR 猜一个。

    口令同理走独立的 `config/smtp.secret`（`mb.read_smtp_secret` 内部对
    老配置有回退，见那里的说明）。
    """
    cfg = cfg if cfg is not None else mb.load_config()
    smtp = dict(cfg.get("smtp") or {})
    imap_user = (cfg.get("imap") or {}).get("user") or ""
    user = smtp.get("user") or ""
    return {
        "host": smtp.get("host") or "smtp.163.com",
        "port": int(smtp.get("port") or 465),
        "ssl": bool(smtp.get("ssl", True)),
        "user": user,
        "from_name": smtp.get("from_name") or "",
        "reply_to": smtp.get("reply_to") or "",
        "password_set": bool(mb.read_smtp_secret()),
        # 给界面用：发信账号是否与收信账号**恰好相同**（HR 常确实同号，
        # 那时要在界面上说清"发信用的就是这个号"，否则分设置的意义就看不出来了）。
        "user_same_as_imap": bool(user) and user == imap_user,
        "imap_user": imap_user,
        # 独立口令文件是否已建（没建说明还在用老配置回退读 imap.secret）
        "own_password_file": os.path.exists(
            os.path.join(mb.CONFIG_DIR, "smtp.secret")),
    }


#: 端口 ↔ 加密方式的**标准搭配**。用于自动纠错时找备选。
_ALT_PORTS = {465: 587, 587: 465}


def _tls_connect(host: str, port: int, use_ssl: bool, timeout: int = 30):
    """按指定方式建立到 SMTP 的连接并完成加密；**返回已加密的连接**。

    use_ssl=True  → 隐式 TLS（SMTP_SSL，通常 465）
    use_ssl=False → 明文打招呼后立刻 STARTTLS（通常 587），之后仍是加密链路

    ⚠️ 这条路的**底线是：绝不在未加密的链路上发凭据**。
    use_ssl=False 时如果服务器不支持 STARTTLS，直接抛错返回，
    不做"那就明文发吧"的降级 —— 那等于把授权码写在明信片上寄出去。
    """
    if use_ssl:
        ctx = ssl.create_default_context()
        s = smtplib.SMTP_SSL(host, port, timeout=timeout, context=ctx)
        s.ehlo()
        return s
    s = smtplib.SMTP(host, port, timeout=timeout)
    s.ehlo()
    if not s.has_extn("starttls"):
        s.close()
        raise smtplib.SMTPNotSupportedError(
            f"{host}:{port} 不支持 STARTTLS，为保护授权码不发信")
    s.starttls(context=ssl.create_default_context())
    s.ehlo()
    return s


def connect_smtp(conf: dict, password: str, timeout: int = 30):
    """连上并登录，返回 (连接, 实际使用的 {host, port, ssl} 及是否自动纠正)。

    **为什么要"自动识别"**：`[SSL: WRONG_VERSION_NUMBER]` 这个错误专门表示
    "按 TLS 连了一个说明文的端口"（或反之）。实测本机网络下
    `smtp.qq.com:465` 回的是**明文**问候语，按 SSL 连必然握手失败，
    而 `587 + STARTTLS` 正常。这类问题在 HR 眼里就是一句看不懂的 SSL 报错，
    但根因只是"端口和加密方式配错了"。

    所以这里按**从最标准到次标准**的顺序试，成功后回传实际用的组合，
    让上层能明确告诉 HR「已自动改用 587 + STARTTLS，建议保存」。
    安全底线不变：**只有 STARTTLS 成功后才登录**，绝不发明文凭据。
    """
    host, port = conf["host"], int(conf["port"])
    want_ssl = bool(conf["ssl"])
    candidates = [(host, port, want_ssl)]                    # 1) 按配置来
    alt = _ALT_PORTS.get(port)
    candidates.append((host, port, not want_ssl))            # 2) 同端口换方式
    if alt:
        candidates.append((host, alt, alt == 465))           # 3) 标准备选端口

    tried, last_exc = [], None
    for h, p, use_ssl in candidates:
        mode = "SSL" if use_ssl else "STARTTLS"
        if (h, p, use_ssl) in tried:
            continue
        tried.append((h, p, use_ssl))
        try:
            s = _tls_connect(h, p, use_ssl, timeout=timeout)
            try:
                s.login(conf["user"], password)
            except Exception:
                s.close()
                raise
            used = {"host": h, "port": p, "ssl": use_ssl, "mode": mode}
            corrected = (p != port) or (use_ssl != want_ssl)
            return s, used, corrected, tried
        except smtplib.SMTPAuthenticationError:
            raise                                            # 认证错不再试别的组合
        except Exception as exc:                              # noqa: BLE001
            last_exc = exc
            continue
    raise last_exc if last_exc else RuntimeError("SMTP 连接失败")


def _explain_conn_error(exc: Exception, conf: dict, tried: list) -> dict:
    """把底层异常翻译成 HR 看得懂、且能照着做的一句话。"""
    conf_port, conf_ssl = int(conf["port"]), bool(conf["ssl"])
    txt = str(exc)
    tried_txt = "、".join("%s:%d %s" % (h, p, "SSL" if u else "STARTTLS")
                         for h, p, u in tried) if tried else "—"
    if "WRONG_VERSION_NUMBER" in txt or "UNEXPECTED_EOF" in txt:
        return {"ok": False,
                "error": f"发信的**端口与加密方式不匹配**：按 "
                         f"{'SSL' if conf_ssl else 'STARTTLS'} 连 {conf['host']}:{conf_port} "
                         f"时对方返回的不是加密应答。已自动试过：{tried_txt}，都没成。",
                "hint": "常见搭配是 **465 配 SSL**、**587 配 STARTTLS**。"
                        "请到「系统配置 → 发信配置」把端口与「使用 SSL」改成匹配的一对；"
                        "若单位网络屏蔽了 465 的加密，改用 587 + 关闭 SSL 通常即可。"}
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return {"ok": False,
                "error": "发信认证失败：账号或授权码不对。",
                "hint": "用的是**授权码**而不是登录密码"
                        "（在邮箱「设置 → POP3/SMTP/IMAP」里开启服务并生成）。"}
    return {"ok": False,
            "error": f"连不上发信服务器：{type(exc).__name__}: {str(exc)[:160]}",
            "hint": f"确认 {conf['host']}:{conf_port} 可达，且端口与 SSL 设置匹配"
                    f"（465 用 SSL、587 用 STARTTLS）。已试过：{tried_txt}。"}


def _attach_payload(msg, attachments: list[dict]) -> tuple[list[str], list[str]]:
    """把附件挂到 `multipart/mixed` 外壳上，返回（成功挂上的、跳过的）文件名。

    v1.27：发信支持带附件（面试通知附岗位说明、offer 附合同模板等）。

    为什么是 `mixed` 包 `alternative` 而不是把附件直接挂到 `alternative` 上：
    MIME 结构里 `alternative` 的语义是"同一内容的不同表示，让收件人挑一个"，
    附件不属于任何一种表示——把 application/pdf 塞进 alternative 会让部分
    客户端（尤其企业邮）渲染错乱或直接拒收。标准做法是：

        multipart/mixed
        ├── multipart/alternative   ← 正文（纯文本 + HTML）
        ├── application/pdf         ← 附件1
        └── application/...         ← 附件2

    没有 HTML 时也不必降级成纯 MIMEText：有附件就用 mixed 包一个单 text 部件，
    这样 `sendmail` 的字节流在有无附件两种情况下一致，不用分支两套组包逻辑。

    附件按真实 MIME 组装（图片 `image/*`、文本 `text/*`、文档 `application/*`），
    中文文件名走 RFC 2231 编码
    （`email.utils.encode_rfc2231`），否则 Windows 版Outlook / 一些企业邮
    会把 `张三-面试通知.pdf` 显示成乱码或 `=?utf-8?b?...?=`
    """
    # Windows 的 mimetypes 注册表并不稳定；这些格式显式兜底，
    # 否则 HEIC/OFD/宏文档会退化成 application/octet-stream。
    explicit_mime = {
        ".heic": "image/heic", ".heif": "image/heif",
        ".jfif": "image/jpeg", ".avif": "image/avif",
        ".ofd": "application/ofd",
        ".docm": "application/vnd.ms-word.document.macroEnabled.12",
        ".dot": "application/msword",
        ".dotx": "application/vnd.openxmlformats-officedocument.wordprocessingml.template",
        ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
        ".xlsb": "application/vnd.ms-excel.sheet.binary.macroEnabled.12",
        ".xltx": "application/vnd.openxmlformats-officedocument.spreadsheetml.template",
        ".pptm": "application/vnd.ms-powerpoint.presentation.macroEnabled.12",
        ".pps": "application/vnd.ms-powerpoint",
        ".ppsx": "application/vnd.openxmlformats-officedocument.presentationml.slideshow",
    }
    ok_names: list[str] = []
    skip_names: list[str] = []
    for att in (attachments or []):
        name = str((att or {}).get("filename") or "").strip()
        data = (att or {}).get("data")
        if not name or not isinstance(data, (bytes, bytearray)) or not data:
            skip_names.append(name or "(未命名)")
            continue
        ext = os.path.splitext(name)[1].lower()
        ctype = (explicit_mime.get(ext)
                 or mimetypes.guess_type(name)[0]
                 or "application/octet-stream")
        maintype, subtype = ctype.split("/", 1)
        if maintype == "image":
            part = MIMEImage(bytes(data), _subtype=subtype)
        elif maintype == "text":
            charset = "utf-8"
            try:
                text = bytes(data).decode(charset)
            except UnicodeDecodeError:
                text = bytes(data).decode("gb18030", errors="replace")
            part = MIMEText(text, _subtype=subtype, _charset=charset)
        else:
            part = MIMEApplication(bytes(data), _subtype=subtype)
        part.set_param("name", name, header="Content-Type", charset="utf-8")
        part.add_header("Content-Disposition", "attachment",
                        filename=("utf-8", "", name))
        msg.attach(part)
        ok_names.append(name)
    return ok_names, skip_names


def send_mail(to: str, subject: str, body: str,
              cfg: dict | None = None, password: str | None = None,
              dry_run: bool = False, html: str | None = None,
              attachments: list[dict] | None = None) -> dict:
    """发一封邮件。返回 {ok, ...}；失败时给出可读原因（**不含口令**）。

    `html` 不为空时按 **multipart/alternative** 发送：同一封邮件里同时带
    「纯文本」与「HTML」两个版本，由收件人的客户端自己挑——现代客户端显示表格，
    纯文本客户端退化成可读的原文。这是邮件原生富文本的标准做法，
    比只发 HTML 稳妥（只发 HTML 的邮件在部分企业邮箱里会被判成可疑、也可能显示为乱码）。

    `attachments`（v1.27）：`[{filename: str, data: bytes}, ...]`。
    有附件时整体升级为 `multipart/mixed`（见 `_attach_payload` 的结构说明）。
    附件**只从后端已归档的原件读**（`db.get_document` → `archived_path`），
    界面不提供任意本机路径上传——那是路径穿越面（P1-12 同类），不新增。

    `dry_run=True` 时只做参数与凭据**存在性**检查、不连网不发信。
    真正的"连一次服务器验证凭据"见 `check_smtp`。
    """
    conf = load_smtp(cfg)
    # 先查配置、再查收件人：否则"检查配置"这类不带收件人的调用
    # 会被"收件人邮箱为空"挡住，报错和用户在做的事完全对不上。
    if not conf["user"]:
        return {"ok": False,
                "error": "还没配置**发信**邮箱账号（发信与收信已分成两套配置，"
                         "发信账号要在「系统配置 → 发信配置」里单独填）"}
    pwd = password if password is not None else mb.read_smtp_secret()
    if not pwd:
        return {"ok": False, "error": "还没保存**发信**授权码。邮箱需在「设置 → POP3/SMTP/IMAP」"
                                      "里开启服务并生成授权码，填在「发信配置」的口令栏。"}
    to = (to or "").strip()
    if not dry_run and (not to or "@" not in to):
        return {"ok": False, "error": "收件人邮箱为空或格式不对"}
    if dry_run:
        return {"ok": True, "dry_run": True,
                "note": f"配置齐全（{conf['host']}:{conf['port']}，账号 {conf['user']}）。"
                        f"未连网、未发信。要验证凭据请点「检查发信配置」。"}

    from_name = conf["from_name"] or conf["user"]
    _atts = list(attachments or [])
    # 正文子结构：纯文本在前、HTML 在后（MUA 会挑它支持的**最后一个**部分，顺序不能反）
    if html:
        body_part = MIMEMultipart("alternative")
        body_part.attach(MIMEText(body or "", "plain", "utf-8"))
        body_part.attach(MIMEText(html, "html", "utf-8"))
    else:
        body_part = MIMEText(body or "", "plain", "utf-8")
    if _atts:
        # 有附件 → mixed 外壳（正文子结构 + 各附件）。结构说明见 `_attach_payload`
        msg = MIMEMultipart("mixed")
        msg.attach(body_part)
        _att_ok, _att_skip = _attach_payload(msg, _atts)
        if not _att_ok:
            return {"ok": False,
                    "error": f"附件都没能挂上（{len(_att_skip)} 个）："
                             + "；".join(_att_skip[:5])
                             + "。附件内容必须是从库内原件读出的字节。"}
    else:
        msg = body_part
        _att_ok, _att_skip = [], []
    msg["Subject"] = Header(subject or "(无主题)", "utf-8")
    msg["From"] = formataddr((str(Header(from_name, "utf-8")), conf["user"]))
    msg["To"] = to
    if conf["reply_to"]:
        msg["Reply-To"] = conf["reply_to"]

    s = None
    tried: list = []
    used: dict = {}
    try:
        s, used, corrected, tried = connect_smtp(conf, pwd, timeout=30)
        s.sendmail(conf["user"], [parseaddr(to)[1] or to], msg.as_string())
    except smtplib.SMTPRecipientsRefused:
        return {"ok": False, "error": f"对方服务器拒收了收件人地址：{to}"}
    except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
        return _explain_conn_error(exc, conf, tried)
    finally:
        if s is not None:
            try:
                s.quit()
            except Exception:                                  # noqa: BLE001
                pass
    # v1.24.0：把**实际发出的 From 头原文**回传。
    # 为什么：QQ 邮箱会在服务端**覆盖发件人显示名**（用 QQ 账号的"发件人名"），
    # 于是对方面板与详情页可能显示不同、没有名称。回传原文才能判断
    # "我们发出去的就带名称" 还是 "我们没带" —— 前者只能去 QQ 设置里改。
    out = {"ok": True, "to": to, "subject": subject, "host": conf["host"],
           "from_header": msg["From"],
           "from_account": conf["user"],
           "from_name_used": from_name,
           "attachments": _att_ok,
           "used": used}
    # `note` 用**追加**而不是赋值：附件缺失与端口自动纠正可能同时发生，
    # 原来两处都是 `out["note"] = ...`，后者静默盖掉前者 —— 附件少发这个
    # 事就没人知道了（同一个坑：v1.24.0 的端口纠正文案也是这么被盖过的）。
    _notes: list[str] = []
    # 附件有挂上就有挂不上的（原件文件被 HR 挪走/删了会走到这里）——不能静默少发
    if _att_skip:
        out["attachments_skipped"] = _att_skip
        out["partial"] = True
        _notes.append(f"有 {len(_att_skip)} 个附件没发出去："
                      + "；".join(_att_skip[:5])
                      + "（原件文件读不到，通常是被移动或删除了）")
    # 自动纠正过就明说 —— 静默换端口会让人下次又按老配置排查半天
    if corrected:
        out["corrected"] = used
        _notes.append(f"按配置的 {conf['host']}:{int(conf['port'])}"
                      f"{' SSL' if conf['ssl'] else ' STARTTLS'} 连不上，"
                      f"已自动改用 **{used['host']}:{used['port']} {used['mode']}** 发出。"
                      f"建议到「系统配置 → 发信配置」把端口与 SSL 保存成这一对，"
                      f"免得每次都要试探。")
    if _notes:
        out["note"] = " ".join(_notes)
    return out


def check_smtp(cfg: dict | None = None, password: str | None = None) -> dict:
    """「检查发信配置」：真连一次 SMTP 并登录，验证服务器/端口/SSL/授权码，**不发任何信**。

    之前这里走的是 send_mail(dry_run=True)，而 send_mail 先校验收件人，
    导致这个按钮永远报"收件人邮箱为空"——检查配置根本不需要收件人。
    登录成功立即断开，不产生邮件、不进审计（没有发生任何写动作）。
    """
    conf = load_smtp(cfg)
    if not conf["user"]:
        return {"ok": False, "error": "还没配置**发信**邮箱账号（发信与收信已分成两套配置，"
                                      "发信账号要在「系统配置 → 发信配置」里单独填）"}
    pwd = password if password is not None else mb.read_smtp_secret()
    if not pwd:
        return {"ok": False, "error": "还没保存**发信**授权码。在邮箱「设置 → POP3/SMTP/IMAP」里"
                                      "开启 SMTP 服务并生成授权码（不是登录密码），"
                                      "填到「发信配置」的口令栏。"}
    s = None
    tried: list = []
    try:
        s, used, corrected, tried = connect_smtp(conf, pwd, timeout=20)
    except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
        return _explain_conn_error(exc, conf, tried)
    finally:
        if s is not None:
            try:
                s.quit()
            except Exception:                                  # noqa: BLE001
                pass
    note = (f"登录成功：{used['host']}:{used['port']}（{used['mode']}），"
            f"账号 {conf['user']}。凭据可用，未发任何邮件。")
    out = {"ok": True, "note": note, "used": used}
    if corrected:
        # 自动纠正必须说出来：否则 HR 看到的"成功"和配置里写的对不上，
        # 下次出问题又得从零排查。
        out["note"] = (note + f" ⚠️ 注意：按配置的 {conf['host']}:{int(conf['port'])}"
                              f"{' SSL' if conf['ssl'] else ' STARTTLS'} 连不上，"
                              f"是自动改用 {used['port']} {used['mode']} 才通的，"
                              f"建议把这一对保存下来。")
        out["corrected"] = used
        out["hint"] = "点「保存」即把端口与「使用 SSL」改成上面这对，之后就不用再试探。"
    return out
