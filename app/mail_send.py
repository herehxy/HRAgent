"""发信（SMTP）：把已经人工确认过的草稿真正发出去。

**这个模块只负责"发"，不负责"决定发什么"**：
正文由模板渲染（`app/mail_template.py`）、由 HR 在界面上确认后才走到这里。
这一层不做任何自动触发——「所有邮件都要人工确认」是明确的产品口径。

关于认证：SMTP 用**授权码**（不是邮箱登录密码）。163 / QQ / 腾讯企业邮都要在邮箱
设置里单独生成一个"客户端授权码"。163 的收信（IMAP）与发信（SMTP）用的是**同一个**
授权码，所以这里直接复用 `config/imap.secret`，不再多维护一个密钥文件。
"""
from __future__ import annotations

import smtplib
import ssl
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, parseaddr

from . import mailbox as mb

#: 常见服务商的发信服务器（与收信预设配对）
SMTP_PRESETS = [
    {"label": "网易 163（单位在用）", "host": "smtp.163.com", "port": 465, "ssl": True},
    {"label": "腾讯企业邮", "host": "smtp.exmail.qq.com", "port": 465, "ssl": True},
    {"label": "QQ 邮箱", "host": "smtp.qq.com", "port": 465, "ssl": True},
    {"label": "阿里云企业邮", "host": "smtp.qiye.aliyun.com", "port": 465, "ssl": True},
    {"label": "Outlook / Microsoft 365", "host": "smtp.office365.com", "port": 587, "ssl": False},
]


def load_smtp(cfg: dict | None = None) -> dict:
    """当前 SMTP 配置（缺省按 163 填，因为单位在用 163）。"""
    cfg = cfg if cfg is not None else mb.load_config()
    smtp = dict(cfg.get("smtp") or {})
    user = smtp.get("user") or (cfg.get("imap") or {}).get("user") or ""
    return {
        "host": smtp.get("host") or "smtp.163.com",
        "port": int(smtp.get("port") or 465),
        "ssl": bool(smtp.get("ssl", True)),
        "user": user,
        "from_name": smtp.get("from_name") or "",
        "reply_to": smtp.get("reply_to") or "",
        "password_set": bool(mb.read_secret()),
    }


def send_mail(to: str, subject: str, body: str,
              cfg: dict | None = None, password: str | None = None,
              dry_run: bool = False, html: str | None = None) -> dict:
    """发一封邮件。返回 {ok, ...}；失败时给出可读原因（**不含口令**）。

    `html` 不为空时按 **multipart/alternative** 发送：同一封邮件里同时带
    「纯文本」与「HTML」两个版本，由收件人的客户端自己挑——现代客户端显示表格，
    纯文本客户端退化成可读的原文。这是邮件原生富文本的标准做法，
    比只发 HTML 稳妥（只发 HTML 的邮件在部分企业邮箱里会被判成可疑、也可能显示为乱码）。

    `dry_run=True` 时只做参数与凭据**存在性**检查、不连网不发信。
    真正的"连一次服务器验证凭据"见 `check_smtp`。
    """
    conf = load_smtp(cfg)
    # 先查配置、再查收件人：否则"检查配置"这类不带收件人的调用
    # 会被"收件人邮箱为空"挡住，报错和用户在做的事完全对不上。
    if not conf["user"]:
        return {"ok": False, "error": "还没配置发信邮箱账号（在「收发信配置」里填）"}
    pwd = password if password is not None else mb.read_secret()
    if not pwd:
        return {"ok": False, "error": "还没保存授权码。邮箱需在「设置 → POP3/SMTP/IMAP」"
                                      "里开启服务并生成授权码，填在邮箱配置的口令栏。"}
    to = (to or "").strip()
    if not dry_run and (not to or "@" not in to):
        return {"ok": False, "error": "收件人邮箱为空或格式不对"}
    if dry_run:
        return {"ok": True, "dry_run": True,
                "note": f"配置齐全（{conf['host']}:{conf['port']}，账号 {conf['user']}）。"
                        f"未连网、未发信。要验证凭据请点「检查发信配置」。"}

    from_name = conf["from_name"] or conf["user"]
    if html:
        # 纯文本在前、HTML 在后：MUA 会挑它支持的**最后一个**部分，顺序不能反
        msg = MIMEMultipart("alternative")
        msg.attach(MIMEText(body or "", "plain", "utf-8"))
        msg.attach(MIMEText(html, "html", "utf-8"))
    else:
        msg = MIMEText(body or "", "plain", "utf-8")
    msg["Subject"] = Header(subject or "(无主题)", "utf-8")
    msg["From"] = formataddr((str(Header(from_name, "utf-8")), conf["user"]))
    msg["To"] = to
    if conf["reply_to"]:
        msg["Reply-To"] = conf["reply_to"]

    try:
        if conf["ssl"]:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(conf["host"], conf["port"], timeout=30, context=ctx) as s:
                s.login(conf["user"], pwd)
                s.sendmail(conf["user"], [parseaddr(to)[1] or to], msg.as_string())
        else:
            with smtplib.SMTP(conf["host"], conf["port"], timeout=30) as s:
                s.ehlo()
                s.starttls(context=ssl.create_default_context())
                s.login(conf["user"], pwd)
                s.sendmail(conf["user"], [parseaddr(to)[1] or to], msg.as_string())
    except smtplib.SMTPAuthenticationError:
        # 口令不进日志/不回显：只说"认证失败"和最常见的原因
        return {"ok": False, "error": "发信认证失败。163 邮箱要用**授权码**而不是登录密码"
                                      "（在邮箱「设置 → POP3/SMTP/IMAP」里生成）。"}
    except smtplib.SMTPRecipientsRefused:
        return {"ok": False, "error": f"对方服务器拒收了收件人地址：{to}"}
    except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
        return {"ok": False, "error": f"发信失败：{type(exc).__name__}: {exc}",
                "hint": f"确认 {conf['host']}:{conf['port']} 可达，且端口与 SSL 设置匹配"
                        f"（465 用 SSL、587 用 STARTTLS）。"}
    # v1.24.0：把**实际发出的 From 头原文**回传。
    # 为什么：QQ 邮箱会在服务端**覆盖发件人显示名**（用 QQ 账号的"发件人名"），
    # 于是对方面板与详情页可能显示不同、没有名称。回传原文才能判断
    # "我们发出去的就带名称" 还是 "我们没带" —— 前者只能去 QQ 设置里改。
    return {"ok": True, "to": to, "subject": subject, "host": conf["host"],
            "from_header": msg["From"],
            "from_account": conf["user"],
            "from_name_used": from_name}


def check_smtp(cfg: dict | None = None, password: str | None = None) -> dict:
    """「检查发信配置」：真连一次 SMTP 并登录，验证服务器/端口/SSL/授权码，**不发任何信**。

    之前这里走的是 send_mail(dry_run=True)，而 send_mail 先校验收件人，
    导致这个按钮永远报"收件人邮箱为空"——检查配置根本不需要收件人。
    登录成功立即断开，不产生邮件、不进审计（没有发生任何写动作）。
    """
    conf = load_smtp(cfg)
    if not conf["user"]:
        return {"ok": False, "error": "还没配置发信邮箱账号（在上方「发信账号」里填）"}
    pwd = password if password is not None else mb.read_secret()
    if not pwd:
        return {"ok": False, "error": "还没保存授权码。在邮箱「设置 → POP3/SMTP/IMAP」里开启"
                                      " SMTP 服务并生成授权码（不是登录密码），填到收信口令栏即可——"
                                      "收信与发信共用同一个授权码。"}
    try:
        if conf["ssl"]:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(conf["host"], conf["port"], timeout=20, context=ctx) as s:
                s.login(conf["user"], pwd)
        else:
            with smtplib.SMTP(conf["host"], conf["port"], timeout=20) as s:
                s.ehlo()
                s.starttls(context=ssl.create_default_context())
                s.login(conf["user"], pwd)
    except smtplib.SMTPAuthenticationError:
        # 口令不进日志/不回显：只说"认证失败"和最常见的原因
        return {"ok": False, "error": "发信认证失败：账号或授权码不对。要用**授权码**而不是"
                                      "登录密码（在邮箱「设置 → POP3/SMTP/IMAP」里生成）。"}
    except (smtplib.SMTPException, OSError, ssl.SSLError) as exc:
        return {"ok": False, "error": f"连不上发信服务器：{type(exc).__name__}: {exc}",
                "hint": f"确认 {conf['host']}:{conf['port']} 可达，且端口与 SSL 设置匹配"
                        f"（465 用 SSL、587 用 STARTTLS）。"}
    return {"ok": True, "note": f"登录成功：{conf['host']}:{conf['port']}，账号 {conf['user']}。"
                                f"凭据可用，未发任何邮件。"}
