# -*- coding: utf-8 -*-
"""v6.6.0：智能助手能力扩展的回归测试。

重点不是“模型变聪明了”，而是**能力是否真的交到了模型手上**：
  · 导入意图必须路由到 ingest_resumes（2026-11-25 截图的根因）；
  · 导入诊断能在不同故障下给出不同结论；
  · 检索扩展筛选真正下沉到 db 层；
  · get_candidate 能回答“投了几次、是否同一份”。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from app import db, ingest, mailbox as mb  # noqa: E402
from app.agent import loop, router, skills, tools  # noqa: E402


def _route_names(message: str, history: list[dict] | None = None) -> set[str]:
    specs, _groups = router.route_tools(message, history)
    return {s["function"]["name"] for s in specs}


class RoutingTest(unittest.TestCase):
    def test_import_phrase_has_real_import_tool(self):
        # 截图原话：不能再只给 list_source_files。
        names = _route_names("导入邮箱最新简历")
        self.assertIn("ingest_resumes", names)
        self.assertIn("import_diagnostics", names)

    def test_import_phrase_variants(self):
        for q in ("帮我收一下简历", "把文件夹里的简历导进来", "把邮件收一下",
                  "抓取新邮件", "同步一下邮箱简历"):
            self.assertIn("ingest_resumes", _route_names(q), q)

    def test_mail_history_does_not_hide_import_tool(self):
        # 历史里有大量“邮件/模板”关键词时，mail 可能成为主组；
        # mail 组现在也必须带 ingest_resumes。
        hist = [{"role": "user", "content": "有哪些邮件模板？"},
                {"role": "assistant", "content": "系统里有面试邀请模板。"}]
        names = _route_names("导入邮箱最新简历", hist)
        self.assertIn("ingest_resumes", names)

    def test_help_and_daily_brief_routes(self):
        self.assertIn("assistant_capabilities", _route_names("你能做什么"))
        self.assertIn("daily_brief", _route_names("今天先做什么"))

    def test_small_window_keeps_import_action(self):
        specs, _ = router.route_tools("导入邮箱最新简历", max_tools=18)
        names = {s["function"]["name"] for s in specs}
        self.assertIn("ingest_resumes", names)
        self.assertIn("import_diagnostics", names)

    def test_skills_match_import_and_help(self):
        self.assertEqual((skills.match_skill("导入邮箱最新简历") or {}).get("id"),
                         "resume_import")
        self.assertEqual((skills.match_skill("你能做什么") or {}).get("id"),
                         "assistant_help")
        self.assertEqual((skills.match_skill("今天先做什么") or {}).get("id"),
                         "daily_brief")


class SmallModelTest(unittest.TestCase):
    def test_param_parse(self):
        self.assertEqual(loop._param_billions("27B"), 27.0)
        self.assertEqual(loop._param_billions("7b"), 7.0)
        self.assertEqual(loop._param_billions("1.5B"), 1.5)
        self.assertEqual(loop._param_billions(""), 0.0)
        self.assertEqual(loop._param_billions("mimo-v2"), 0.0)

    def test_small_model_prompt_exists(self):
        self.assertIn("小参数模型", loop._SMALL_MODEL_PROMPT)
        self.assertIn("数字必须原样来自工具", loop._SMALL_MODEL_PROMPT)


class SearchFilterTest(unittest.TestCase):
    def test_school_alias_expands(self):
        self.assertEqual(tools._school_terms("西电,北邮"),
                         "西安电子科技大学,北京邮电大学")

    def test_extra_filters(self):
        f = tools._search_extra_filters({
            "school": "西电", "major": "通信工程",
            "cet": "CET-6", "outcome": "拟录用",
            "date_from": "2026-11-01", "archived": "true",
        })
        self.assertEqual(f["school"], "西安电子科技大学")
        self.assertEqual(f["major"], "通信工程")
        self.assertEqual(f["cet"], "CET-6")
        self.assertEqual(f["outcome"], "拟录用")
        self.assertEqual(f["date_from"], "2026-11-01")
        self.assertTrue(f["archived"])

    def test_archived_defaults_to_false(self):
        self.assertFalse(tools._search_extra_filters({})["archived"])

    def test_db_school_filter(self):
        tmp = tempfile.mkdtemp(prefix="agent-filter-")
        conn = db.connect(os.path.join(tmp, "t.db"))
        try:
            db.insert_candidate(conn, {"identity_key": "a1", "name": "张三",
                                       "school": "西安电子科技大学", "edu_level": "硕士"})
            db.insert_candidate(conn, {"identity_key": "a2", "name": "李四",
                                       "school": "北京大学", "edu_level": "本科"})
            rows = db.list_candidates(conn, school="西安电子科技大学")
            self.assertEqual([r["name"] for r in rows], ["张三"])
            rows = db.list_candidates(conn, school="西安电子科技大学,北京大学")
            self.assertEqual(len(rows), 2)
        finally:
            conn.close()


class SubmissionSummaryTest(unittest.TestCase):
    def test_duplicate_submissions(self):
        s = tools._submission_summary([
            {"submitted_at": "2026-11-01 10:00:00", "document_id": 1,
             "duplicate": False},
            {"submitted_at": "2026-11-02 10:00:00", "document_id": 1,
             "duplicate": True},
            {"submitted_at": "2026-11-03 10:00:00", "document_id": 2,
             "duplicate": False},
        ])
        self.assertEqual(s["times"], 3)
        self.assertEqual(s["distinct_documents"], 2)
        self.assertEqual(s["duplicate_times"], 1)
        self.assertEqual(s["first_at"], "2026-11-01 10:00:00")
        self.assertEqual(s["last_at"], "2026-11-03 10:00:00")

    def test_detail_brief_includes_submissions(self):
        d = {"name": "张三", "submissions": [
            {"id": 1, "document_id": 9, "file_name": "张三.pdf",
             "submitted_at": "2026-11-01", "duplicate": 0},
        ]}
        out = tools._detail_brief(1, d)
        self.assertEqual(out["submission_summary"]["times"], 1)
        self.assertEqual(out["submissions"][0]["file"], "张三.pdf")


class ImportDiagnosticsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="agent-diag-")
        self.db_path = os.path.join(self.tmp, "t.db")
        self.conn = db.connect(self.db_path)
        self.folder = os.path.join(self.tmp, "resumes")
        os.makedirs(self.folder, exist_ok=True)
        self.secret = os.path.join(self.tmp, "imap.secret")
        with open(self.secret, "w", encoding="utf-8") as fh:
            fh.write("secret")
        self.cfg = {
            "mode": "imap", "folder_dir": self.folder, "max_attachment_mb": 1,
            "eml_dir": os.path.join(self.tmp, "mail_in"),
            "imap": {"host": "imap.example.com", "user": "hr@example.com",
                     "folder": "INBOX", "password_file": self.secret,
                     "readonly": True},
        }
        self.ctx = tools.ToolCtx(db_path=self.db_path, cfg={"local": True},
                                 role="admin")

    def tearDown(self):
        self.conn.close()

    def _diag(self, cfg: dict, args: dict | None = None) -> dict:
        with patch.object(tools, "_read_json_or", return_value=cfg), \
                patch.object(mb, "load_config", return_value=cfg):
            return tools._import_diagnostics(self.conn, self.ctx, args or {})

    def test_normal_mailbox_no_new_mail(self):
        r = self._diag(self.cfg)
        self.assertIn("没有新邮件", r["verdict"])
        checks = {c["key"]: c for c in r["checks"]}
        self.assertTrue(checks["mailbox_mode"]["ok"])
        self.assertTrue(checks["imap_account"]["ok"])
        self.assertTrue(checks["imap_secret"]["ok"])
        self.assertTrue(checks["pending_files"]["ok"])  # 邮箱模式下空文件夹不算错

    def test_mode_off(self):
        cfg = dict(self.cfg)
        cfg["mode"] = "off"
        r = self._diag(cfg)
        self.assertIn("关闭", r["verdict"])
        checks = {c["key"]: c for c in r["checks"]}
        self.assertFalse(checks["mailbox_mode"]["ok"])

    def test_missing_secret(self):
        cfg = dict(self.cfg)
        cfg["imap"] = dict(cfg["imap"])
        cfg["imap"]["password_file"] = os.path.join(self.tmp, "nope.secret")
        with patch.dict(os.environ, {"TP_IMAP_PASSWORD": ""}, clear=False), \
                patch.object(mb, "read_secret", return_value=None):
            r = self._diag(cfg)
        self.assertIn("授权码", r["verdict"])
        checks = {c["key"]: c for c in r["checks"]}
        self.assertFalse(checks["imap_secret"]["ok"])

    def test_oversize_file(self):
        with open(os.path.join(self.folder, "big.pdf"), "wb") as fh:
            fh.write(b"0" * (2 * 1024 * 1024))
        r = self._diag(self.cfg, {"folder": self.folder})
        self.assertIn("超过体积上限", r["verdict"])
        checks = {c["key"]: c for c in r["checks"]}
        self.assertFalse(checks["oversize_files"]["ok"])

    def test_folder_empty(self):
        cfg = dict(self.cfg)
        cfg["mode"] = "off"
        r = self._diag(cfg, {"folder": self.folder})
        self.assertTrue(any("没有可导入" in c["detail"] for c in r["checks"]))


class JobDetailTest(unittest.TestCase):
    def test_job_detail(self):
        tmp = tempfile.mkdtemp(prefix="agent-job-")
        dbp = os.path.join(tmp, "t.db")
        conn = db.connect(dbp)
        try:
            db.upsert_job(conn, {"must": {"education_min": "硕士",
                                          "skills_required": ["通信"]},
                                 "responsibilities": "负责算法"},
                          title="通信算法", dept="数字化中心")
        finally:
            conn.close()
        ctx = tools.ToolCtx(db_path=dbp, cfg={"local": True}, role="admin")
        import json
        d = json.loads(tools.execute("job_detail", {"job": "通信算法"}, ctx))
        self.assertEqual(d["title"], "通信算法")
        self.assertEqual(d["must_skills"], ["通信"])
        self.assertIn("0 条投递", d["summary"])


class IngestPayloadTest(unittest.TestCase):
    def test_folder_payload_summary_and_names(self):
        rep = {
            "scanned": 3, "added": 2, "merged_versions": 1, "skipped_dup": 0,
            "skipped_oversize": 0, "failed": 0, "parse_failed": 0,
            "details": [
                {"file": "张三.pdf", "name": "张三", "candidate_id": 12,
                 "status": "added", "tier": "B", "notes": []},
                {"file": "李四.docx", "name": "李四", "candidate_id": 13,
                 "status": "merged_version", "tier": "A", "notes": []},
                {"file": "王五.pdf", "name": "王五", "candidate_id": 14,
                 "status": "skipped_dup", "notes": ["SHA256 相同"]},
            ],
        }
        p = tools._ingest_payload("folder", rep, folder="D:/resumes")
        self.assertTrue(p["ok"])
        self.assertIn("新增候选人 2 人", p["summary"])
        self.assertIn("同人新版本 1 份", p["summary"])
        self.assertEqual([x["name"] for x in p["new_candidates"]], ["张三", "李四"])
        self.assertEqual(p["skipped"][0]["file"], "王五.pdf")

    def test_mailbox_empty_payload_has_diagnosis(self):
        rep = {"mails": 0, "added": 0, "merged_versions": 0, "skipped_dup": 0,
               "failed": 0, "details": []}
        diag = {"verdict": "邮箱配置正常，最近没有新邮件。",
                "checks": [{"key": "x", "ok": False, "detail": "d", "fix": "f"}],
                "next_actions": ["有新邮件再收"]}
        p = tools._ingest_payload("mailbox", rep, diagnosis=diag)
        self.assertIn("没有拉到新邮件", p["summary"])
        self.assertEqual(p["diagnosis"]["verdict"], diag["verdict"])
        self.assertEqual(p["next_actions"], ["有新邮件再收"])

    def test_capabilities_payload(self):
        p = tools._capabilities_payload()
        self.assertTrue(p["groups"])
        self.assertTrue(any("发送" in x for x in p["limits"]))
        self.assertTrue(any("删除" in x for x in p["limits"]))


class ProgressTest(unittest.TestCase):
    def test_live_trace_carries_progress(self):
        ctx = tools.ToolCtx(db_path=":memory:", session_id="progress-trace",
                            operator="HR")
        try:
            loop._live_update(ctx, [], progress={"phase": "item", "done": 1,
                                                 "total": 3,
                                                 "message": "正在解析 1/3：张三.pdf"})
            t = loop.live_trace("progress-trace", "HR")
            self.assertEqual(t["progress"]["done"], 1)
            self.assertIn("张三.pdf", t["progress"]["message"])
        finally:
            loop._LIVE_TRACES.pop("progress-trace", None)

    def test_progress_event_has_counts_and_recent_files(self):
        rep = {"scanned": 2, "added": 1, "merged_versions": 0,
               "skipped_dup": 1, "skipped_oversize": 0, "failed": 0,
               "details": [
                   {"file": "张三.pdf", "status": "added"},
                   {"file": "李四.docx", "status": "skipped_dup"},
               ]}
        got = []
        ingest._emit_progress(got.append, phase="item", done=2, total=2,
                              message="已处理 2/2 份", report=rep)
        self.assertEqual(got[0]["counts"]["added"], 1)
        self.assertEqual([x["file"] for x in got[0]["items"]],
                         ["张三.pdf", "李四.docx"])
        self.assertEqual(got[0]["done"], 2)
        self.assertEqual(got[0]["total"], 2)

    def test_ingest_dir_emits_file_level_progress(self):
        tmp = tempfile.mkdtemp(prefix="agent-progress-dir-")
        folder = os.path.join(tmp, "resumes")
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "超大简历.pdf"), "wb") as fh:
            fh.write(b"0" * (2 * 1024 * 1024))
        got = []
        with patch.object(ingest, "spawn_auto_analysis"):
            rep = ingest.ingest_dir(
                folder, {}, {}, os.path.join(tmp, "t.db"),
                cfg={"max_attachment_mb": 1}, on_progress=got.append)
        phases = [e.get("phase") for e in got]
        self.assertIn("scan", phases)
        self.assertIn("parse", phases)
        self.assertIn("item", phases)
        self.assertEqual(phases[-1], "done")
        items = [x for e in got for x in (e.get("items") or [])
                 if x.get("file") == "超大简历.pdf"]
        self.assertTrue(items)
        self.assertEqual(items[-1]["status"], "skipped_oversize")
        self.assertEqual(rep["skipped_oversize"], 1)

    def test_execute_passes_progress_callback_to_ingest(self):
        tmp = tempfile.mkdtemp(prefix="agent-progress-")
        folder = os.path.join(tmp, "resumes")
        os.makedirs(folder, exist_ok=True)
        ctx = tools.ToolCtx(db_path=os.path.join(tmp, "t.db"),
                            cfg={"local": True}, role="admin")
        got = []
        captured = {}

        def fake_ingest_dir(folder_arg, jd, tiers, db_path, **kw):
            captured.update(kw)
            cb = kw.get("on_progress")
            if cb:
                cb({"phase": "item", "done": 1, "total": 1,
                    "message": "正在解析 1/1：张三.pdf"})
            return {"scanned": 1, "added": 1, "merged_versions": 0,
                    "skipped_dup": 0, "skipped_oversize": 0,
                    "failed": 0, "parse_failed": 0, "details": []}

        with patch.object(mb, "load_config",
                          return_value={"mode": "off", "folder_dir": folder}), \
                patch.object(tools, "_jd_tiers", return_value=({}, {})), \
                patch("app.ingest.ingest_dir", side_effect=fake_ingest_dir):
            tools.execute("ingest_resumes",
                          {"source": "folder", "folder": folder},
                          ctx, progress=got.append)
        self.assertTrue(callable(captured.get("on_progress")))
        self.assertEqual(got[0]["message"], "正在解析 1/1：张三.pdf")


class MailBodyProfileTest(unittest.TestCase):
    def _ingest_one(self, mail: dict) -> tuple[dict, list]:
        tmp = tempfile.mkdtemp(prefix="mail-body-")
        dbp = os.path.join(tmp, "t.db")
        db.connect(dbp).close()
        rep = ingest.ingest_mails([mail], {"max_attachment_mb": 20},
                                  {}, {}, dbp, spawn=False)
        conn = db.connect(dbp)
        try:
            rows = db.list_candidates(conn)
        finally:
            conn.close()
        return rep, rows

    def test_application_without_attachment_creates_profile(self):
        rep, rows = self._ingest_one({
            "message_id": "m-body-1", "uid": "1", "mailbox": "INBOX",
            "from_addr": "zhangsan@example.com",
            "subject": "应聘通信算法-硕士-西安电子科技大学-张三",
            "body_text": ("姓名：张三\n电话：13800138000\n"
                          "应聘通信算法岗位，西安电子科技大学通信工程硕士。"),
            "received_at": db.now(), "attachments": [],
        })
        self.assertEqual(rep["no_attachment"], 1)
        self.assertEqual(rep["from_mail_body"], 1)
        self.assertEqual(rep["added"], 1)
        self.assertEqual([r["name"] for r in rows], ["张三"])

    def test_non_application_without_attachment_is_not_created(self):
        rep, rows = self._ingest_one({
            "message_id": "m-body-2", "uid": "2", "mailbox": "INBOX",
            "from_addr": "notice@example.com",
            "subject": "本周招聘系统维护通知",
            "body_text": "系统将于周六维护，请提前保存工作。",
            "received_at": db.now(), "attachments": [],
        })
        self.assertEqual(rep["no_attachment"], 1)
        self.assertEqual(rep["from_mail_body"], 0)
        self.assertEqual(rows, [])


if __name__ == "__main__":
    unittest.main()
