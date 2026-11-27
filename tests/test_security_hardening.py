# -*- coding: utf-8 -*-
"""安全加固回归：模板变量与导出文本的注入边界。"""
from __future__ import annotations

import json
import os
import smtplib
import sys
import tempfile
import unittest
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from app import db, mail_send, mail_template  # noqa: E402
from app.agent import tools  # noqa: E402


class MailTemplateEscapeTest(unittest.TestCase):
    def test_variable_cannot_break_out_of_attribute(self):
        out = mail_template.render(
            '<p><a href="{姓名}">打开</a></p>',
            {"姓名": '张三" onmouseover="alert(1)'})["text"]
        self.assertIn("&quot;", out)
        self.assertNotIn('onmouseover="alert(1)', out)
        self.assertIn("&lt;", mail_template.render(
            "<p>{备注}</p>", {"备注": "<img src=x onerror=alert(1)>"})["text"])


class SqlToolGuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sql-guard-")
        self.path = os.path.join(self.tmp, "t.db")
        conn = db.connect(self.path)
        try:
            db.insert_candidate(conn, {"identity_key": "sql-guard-1",
                                       "name": "自检甲"})
            db.create_user(conn, "sqlguard", "pw12345678", "hr", "自检")
        finally:
            conn.close()
        self.ctx = tools.ToolCtx(db_path=self.path, role="hr",
                                 cfg={"local": True})

    def test_comma_join_cannot_read_users(self):
        out = json.loads(tools.execute(
            "sql_query", {"sql": "select * from v_candidates, users limit 1"},
            self.ctx))
        self.assertIn("error", out)
        self.assertNotIn("password_hash", json.dumps(out, ensure_ascii=False))

    def test_business_view_still_works(self):
        out = json.loads(tools.execute(
            "sql_query", {"sql": "select id, name from v_candidates limit 1"},
            self.ctx))
        self.assertIn("rows", out)
        self.assertEqual(len(out["rows"]), 1)


class SmtpErrorReportTest(unittest.TestCase):
    conf = {"host": "smtp.qq.com", "port": 465, "ssl": True,
            "user": "nobody@qq.com", "from_name": "", "reply_to": ""}

    def test_login_disconnect_points_to_authorization_code(self):
        exc = mail_send.SMTPLoginDisconnected("Connection unexpectedly closed")
        out = mail_send._explain_conn_error(exc, self.conf,
                                            [("smtp.qq.com", 465, True)])
        self.assertIn("授权码", out["hint"])
        self.assertIn("16 位", out["hint"])
        self.assertIn("smtp.qq.com:465", out["hint"])
        self.assertNotIn("已试过：—", out["hint"])

    def test_failed_attempts_are_retained_for_error_text(self):
        tried = []
        with mock.patch.object(mail_send, "_tls_connect",
                               side_effect=smtplib.SMTPServerDisconnected("closed")):
            with self.assertRaises(smtplib.SMTPServerDisconnected):
                mail_send.connect_smtp(self.conf, "x", timeout=1,
                                       tried_out=tried)
        self.assertEqual(len(tried), 3)
        self.assertEqual(tried[0], ("smtp.qq.com", 465, True))


if __name__ == "__main__":
    unittest.main()
