"""2026-11-16 数据安全修复的回归测试（不依赖真实库/网络）。"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response

import app.pipeline.extract as extract_mod
import update_agent
from app import db, ingest, server
from app.agent import llm
from app import mail_template


def _close_physical(conn) -> None:
    """关掉池化连接并把它从线程池里摘掉，避免 Windows 删临时库时被占用。"""
    key = getattr(conn, "_pool_key", None)
    conn.close()
    if not key:
        return
    pool = getattr(db._db_pool_local, "by_key", None)
    if pool:
        pooled = pool.pop(key, None)
        if pooled is not None:
            sqlite3.Connection.close(pooled)


def _close_pool_path(path: str) -> None:
    pool = getattr(db._db_pool_local, "by_key", None)
    if not pool:
        return
    pooled = pool.pop((os.path.abspath(path), True), None)
    if pooled is not None:
        sqlite3.Connection.close(pooled)


class IdentityMergeTest(unittest.TestCase):
    def test_reingest_after_merge_resolves_to_root(self):
        with tempfile.TemporaryDirectory() as d:
            conn = db.connect(os.path.join(d, "workbench.db"))
            try:
                key = "ph:13800138000"
                a = db.insert_candidate(conn, {"identity_key": key, "name": "A"})
                b = db.insert_candidate(conn, {"identity_key": "ph:13900139000", "name": "B"})
                merged = db.merge_candidates(conn, a, b, "tester", "admin")
                self.assertTrue(merged["ok"])

                # 查重必须能沿 merged_into 找到 B
                found = db.find_candidate_by_identity(conn, key)
                self.assertIsNotNone(found)
                self.assertEqual(found["id"], b)

                # 再投同一手机号：insert 会撞 UNIQUE，helper 应复用根档而不是抛异常
                cid = ingest._insert_candidate_or_existing(
                    conn, {"identity_key": key, "name": "A"}, key)
                self.assertEqual(cid, b)
            finally:
                _close_physical(conn)


class PurgeBoundaryTest(unittest.TestCase):
    def test_purge_never_deletes_source_file_outside_archive(self):
        with tempfile.TemporaryDirectory() as d:
            data = os.path.join(d, "data")
            archive = os.path.join(data, "archive")
            os.makedirs(archive, exist_ok=True)
            source = os.path.join(d, "source.pdf")
            archived = os.path.join(archive, "copy.pdf")
            with open(source, "wb") as fh:
                fh.write(b"source")
            with open(archived, "wb") as fh:
                fh.write(b"archive")

            conn = db.connect(os.path.join(data, "workbench.db"))
            old_data_dir = server.DATA_DIR
            try:
                cid = db.insert_candidate(conn, {"identity_key": "test:purge", "name": "P"})
                conn.execute(
                    "UPDATE candidates SET archived_at = ? WHERE id = ?",
                    ("2020-01-01 00:00:00", cid),
                )
                conn.commit()
                db.insert_document(conn, {
                    "file_hash": "purge-test-hash",
                    "candidate_id": cid,
                    "file_name": "source.pdf",
                    "file_path": source,
                    "archived_path": archived,
                    "mime": "application/pdf",
                    "size": 6,
                    "parse_ok": True,
                })
                server.DATA_DIR = data
                out = server._purge_expired(
                    conn=conn, actor="tester", role="admin", only_ids=[cid])
            finally:
                server.DATA_DIR = old_data_dir
                _close_physical(conn)

            # 归档副本删掉；数据目录外的来源文件必须保留
            self.assertFalse(os.path.exists(archived))
            self.assertTrue(os.path.exists(source))
            self.assertEqual(len(out["files_skipped"]), 1)
            self.assertEqual(out["files_deleted"], 1)


class ModelInputTest(unittest.TestCase):
    """简历不做脱敏：模型拿到的就是完整原文（2026-11-16 业务口径）。"""

    def test_resume_is_sent_to_model_in_full(self):
        captured = {}

        def _fake_extract(text, *args, **kwargs):
            captured["text"] = text
            return {"name": "张三", "contact": {"phone": "13900139000"}}

        with mock.patch.object(extract_mod, "extract_llm",
                               side_effect=_fake_extract):
            out = extract_mod.extract(
                "姓名：张三\n手机：13800138000\n邮箱：zs@example.com", {},
                use_llm=True,
                llm_conf={"api_key": "x",
                          "base_url": "https://api.example.com/v1",
                          "model": "m"})
        self.assertIn("张三", captured["text"])
        self.assertIn("13800138000", captured["text"])
        self.assertIn("zs@example.com", captured["text"])
        # 联系方式本地规则优先：模型漏抽/写错也能落库
        self.assertEqual(out["contact"].get("phone"), "13800138000")


class SubmissionTimelineTest(unittest.TestCase):
    def test_events_keep_each_time_but_duplicates_are_not_analyzed(self):
        with tempfile.TemporaryDirectory() as d:
            conn = db.connect(os.path.join(d, "workbench.db"))
            try:
                cid = db.insert_candidate(conn, {"identity_key": "test:sub", "name": "S"})
                db.record_submission(conn, {
                    "candidate_id": cid, "file_hash": "hash-1", "file_name": "cv.pdf",
                    "channel": "邮箱", "submitted_at": "2026-01-01 10:00:00",
                })
                db.record_submission(conn, {
                    "candidate_id": cid, "file_hash": "hash-1", "file_name": "cv.pdf",
                    "channel": "邮箱", "submitted_at": "2026-02-01 10:00:00",
                    "duplicate": True,
                })
                self.assertEqual(len(db.list_submissions(conn, cid)), 2)
                detail = db.candidate_detail(conn, cid)
                self.assertEqual(len(detail["submissions"]), 2)
                targets = ingest._insight_targets({"details": [
                    {"status": "skipped_dup", "candidate_id": cid, "application_id": 1},
                    {"status": "added", "candidate_id": cid, "application_id": 2,
                     "reused_document": True},
                ]})
                self.assertEqual(targets, [])
            finally:
                _close_physical(conn)

    def test_archive_and_unarchive_are_idempotent(self):
        with tempfile.TemporaryDirectory() as d:
            conn = db.connect(os.path.join(d, "workbench.db"))
            try:
                cid = db.insert_candidate(conn, {"identity_key": "test:archive", "name": "A"})
                first = db.set_candidate_archived(conn, cid, True)
                stamp = first.get("archived_at")
                second = db.set_candidate_archived(conn, cid, True)
                self.assertTrue(second.get("_unchanged"))
                self.assertEqual(second.get("archived_at"), stamp)
                db.set_candidate_archived(conn, cid, False)
                again = db.set_candidate_archived(conn, cid, False)
                self.assertTrue(again.get("_unchanged"))
            finally:
                _close_physical(conn)


class MailRetryTest(unittest.TestCase):
    def test_failed_attachment_keeps_mail_unprocessed(self):
        with tempfile.TemporaryDirectory() as d:
            dbp = os.path.join(d, "workbench.db")
            conn = db.connect(dbp)
            _close_physical(conn)
            mail = {
                "message_id": "retry-test-1", "subject": "测试岗位",
                "from_addr": "a@example.com", "received_at": "2026-01-01 10:00:00",
                "attachments": [{
                    "filename": "cv.pdf", "mime": "application/pdf",
                    "data": b"%PDF-1.4 test",
                }],
            }
            with mock.patch.object(ingest, "ingest_one",
                                   side_effect=RuntimeError("boom")):
                ingest.ingest_mails([mail], {}, {}, {}, dbp, spawn=False)
            conn = db.connect(dbp)
            try:
                row = conn.execute(
                    "SELECT processed, failed, error FROM email_messages "
                    "WHERE message_id = ?", ("retry-test-1",)).fetchone()
                self.assertEqual(row["processed"], 0)
                self.assertEqual(row["failed"], 1)
                self.assertIn("重试", row["error"] or "")
            finally:
                _close_physical(conn)


class DocumentAndMergeSafetyTest(unittest.TestCase):
    def test_same_hash_cannot_cross_link_candidates(self):
        with tempfile.TemporaryDirectory() as d:
            conn = db.connect(os.path.join(d, "workbench.db"))
            try:
                a = db.insert_candidate(conn, {"identity_key": "test:doc-a", "name": "A"})
                b = db.insert_candidate(conn, {"identity_key": "test:doc-b", "name": "B"})
                did = db.insert_document(conn, {
                    "file_hash": "same-hash", "candidate_id": a, "file_name": "a.pdf",
                    "file_path": "", "archived_path": "", "parse_ok": True,
                })
                self.assertEqual(db.insert_document(conn, {
                    "file_hash": "same-hash", "candidate_id": a, "file_name": "a.pdf",
                    "file_path": "", "archived_path": "", "parse_ok": True,
                }), did)
                with self.assertRaises(ValueError):
                    db.insert_document(conn, {
                        "file_hash": "same-hash", "candidate_id": b, "file_name": "b.pdf",
                        "file_path": "", "archived_path": "", "parse_ok": True,
                    })
            finally:
                _close_physical(conn)

    def test_merge_into_merged_candidate_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            conn = db.connect(os.path.join(d, "workbench.db"))
            try:
                a = db.insert_candidate(conn, {"identity_key": "test:m-a", "name": "A"})
                b = db.insert_candidate(conn, {"identity_key": "test:m-b", "name": "B"})
                c = db.insert_candidate(conn, {"identity_key": "test:m-c", "name": "C"})
                self.assertTrue(db.merge_candidates(conn, a, b)["ok"])
                self.assertFalse(db.merge_candidates(conn, b, a)["ok"])
                self.assertFalse(db.merge_candidates(conn, c, a)["ok"])
            finally:
                _close_physical(conn)

    def test_update_guard_removed(self):
        # 按用户要求：更新器允许降级，不再做版本拒绝
        self.assertFalse(hasattr(update_agent, "_is_downgrade"))


class UniversityNormalizationTest(unittest.TestCase):
    def test_joint_training_school_is_not_degree_granting_school(self):
        fields = {
            "school": "西安交通大学", "education": "硕士",
            "education_history": [
                {"degree": "硕士", "school": "西安交通大学"},
                {"degree": "硕士", "school": "西安科技大学"},
            ],
        }
        text = ("2025.6-至今 西安交通大学（联合培养硕士）\n"
                "2024.9-至今 西安科技大学（硕士）")
        extract_mod._prefer_degree_granting_school(fields, text)
        self.assertEqual(fields["school"], "西安科技大学")
        self.assertEqual(fields["school_joint"], "西安交通大学")


class EmailSanitizerTest(unittest.TestCase):
    def test_allowlist_drops_scripts_events_and_js_urls(self):
        dirty = ('<p onclick="x()">你好<script>alert(1)</script></p>'
                 '<a href="javascript:alert(2)" target="_blank">点我</a>'
                 '<img src="x" onerror="x()">'
                 '<table><tr><td style="color:red">A</td></tr></table>')
        out = mail_template.sanitize_email_html(dirty)
        for bad in ("<script", "onclick", "onerror", "javascript:"):
            self.assertNotIn(bad, out)
        self.assertIn("你好", out)
        self.assertIn("<table>", out)
        self.assertIn("color:red", out)
        self.assertIn('rel="noopener noreferrer"', out)


class AvatarHeuristicTest(unittest.TestCase):
    def test_document_and_flat_logo_are_rejected_but_portrait_is_kept(self):
        from PIL import Image, ImageDraw
        from app import avatar

        portrait = Image.new("RGB", (200, 260), (50, 90, 160))
        draw = ImageDraw.Draw(portrait)
        draw.ellipse((55, 40, 145, 130), fill=(230, 180, 150))
        draw.rectangle((60, 130, 140, 240), fill=(30, 40, 80))
        self.assertGreater(avatar._score(portrait), 0)

        document = Image.new("RGB", (200, 260), (250, 250, 250))
        draw = ImageDraw.Draw(document)
        for y in range(20, 240, 12):
            draw.line((15, y, 185, y), fill=(30, 30, 30), width=2)
        self.assertEqual(avatar._score(document), 0)

        logo = Image.new("RGB", (200, 260), (200, 200, 200))
        self.assertEqual(avatar._score(logo), 0)


class SecurityBoundaryTest(unittest.TestCase):
    def test_get_with_foreign_host_is_rejected(self):
        """DNS rebinding 防护必须覆盖 GET，而不仅是 POST。"""
        scope = {
            "type": "http", "method": "GET", "path": "/api/candidates",
            "query_string": b"", "headers": [(b"host", b"evil.example")],
            "scheme": "http", "server": ("127.0.0.1", 8756),
            "client": ("203.0.113.10", 12345), "root_path": "",
            "http_version": "1.1",
        }

        async def _next(_request):
            return Response("ok")

        resp = asyncio.run(server._guard_local_origin(Request(scope), _next))
        self.assertEqual(resp.status_code, 403)

    def test_agent_status_requires_login(self):
        with tempfile.TemporaryDirectory() as d:
            dbp = os.path.join(d, "workbench.db")
            conn = db.connect(dbp)
            _close_physical(conn)
            try:
                with mock.patch.object(server, "DB_PATH", dbp), \
                        mock.patch.object(server, "_auth_enabled", return_value=True):
                    with self.assertRaises(HTTPException) as ctx:
                        server.api_agent_status(None, None)
                self.assertEqual(ctx.exception.status_code, 401)
            finally:
                _close_pool_path(dbp)

    def test_llm_status_masks_proxy_and_hides_traceback(self):
        with mock.patch.object(llm.net, "proxy_env",
                               return_value={"HTTPS_PROXY": "http://u:p@proxy:8888"}), \
                mock.patch.object(llm.net, "last_route",
                                  return_value={"url": "https://api.example",
                                                "route": "proxy",
                                                "note": "http://u:p@proxy:8888"}), \
                mock.patch.object(llm, "list_models", side_effect=OSError("boom")):
            out = llm.status()
        self.assertNotIn("traceback", out)
        self.assertEqual(out["proxy_env"], {"HTTPS_PROXY": "（已设置）"})
        self.assertNotIn("note", out["route"])


class NameExtractionSafetyTest(unittest.TestCase):
    """2026-11-16 回归：模型 402 回退时，脱敏占位符「候选人」绝不能写进档案。"""

    def test_model_failure_uses_real_name_and_contact(self):
        import urllib.error

        text = ("个人简历—李雷 求职意向：研发\n"
                "电话：13800138000\n发表 SCI 论文两篇")
        fname = "科研工艺技术_硕士_某大学_冶金工程_李雷_男.pdf"
        conf = {"api_key": "x", "base_url": "https://api.example.com/v1",
                "model": "m"}

        def _boom(*args, **kwargs):
            raise urllib.error.HTTPError("https://api.example.com", 402,
                                         "Payment Required", None, None)

        with mock.patch.object(llm, "load_cfg", return_value={}), \
                mock.patch.object(extract_mod, "extract_llm", side_effect=_boom):
            out = extract_mod.extract(text, {}, use_llm=True, llm_conf=conf,
                                      filename=fname)
        self.assertEqual(out["name"], "李雷")
        self.assertNotEqual(out["name"], "候选人")
        self.assertEqual(out["contact"].get("phone"), "13800138000")

    def test_model_sees_real_name_and_placeholder_is_repaired(self):
        captured = {}
        text = "姓名：李雷\n电话：13800138000"

        def _fake(t, *args, **kwargs):
            captured["text"] = t
            return {"name": "候选人", "education": "硕士"}

        with mock.patch.object(llm, "load_cfg", return_value={}), \
                mock.patch.object(extract_mod, "extract_llm", side_effect=_fake):
            out = extract_mod.extract(
                text, {}, use_llm=True,
                llm_conf={"api_key": "x",
                          "base_url": "https://api.example.com/v1",
                          "model": "m"},
                filename="李雷-简历.pdf")
        self.assertIn("李雷", captured["text"])
        self.assertIn("13800138000", captured["text"])
        # 即使模型回了占位符，也不能把「候选人」写进档案
        self.assertEqual(out["name"], "李雷")

    def test_filename_org_naming_convention(self):
        cases = {
            "科研工艺技术_硕士_昆明理工大学_冶金工程_杨华成_男.pdf": "杨华成",
            "科学研究_-_硕士_-_陕西理工大学_-_化学_-_张彤_-_女.pdf": "张彤",
            "西安电子科技大学_林清皓简历.pdf": "林清皓",
            "附件2_电子材料研究所科研岗_博士研究生_华南理工大学_化学_赵越_女.pdf": "赵越",
        }
        for filename, expected in cases.items():
            self.assertEqual(extract_mod.name_from_filename(filename), expected,
                             filename)

    def test_placeholder_is_treated_as_missing(self):
        self.assertFalse(extract_mod._valid_person_name("候选人"))
        self.assertFalse(extract_mod._valid_person_name("发表"))
        out = {"name": "候选人"}
        extract_mod._fix_name_from_filename(
            out, "科研工艺技术_硕士_宝鸡文理学院_凝聚态物理_张子涵_男.pdf")
        self.assertEqual(out["name"], "张子涵")


class AgentConversationPersistenceTest(unittest.TestCase):
    """2026-11-16：提问即落库，刷新页面后未完成的对话不能消失。"""

    def test_unfinished_question_is_visible_and_marked_interrupted(self):
        with tempfile.TemporaryDirectory() as d:
            conn = db.connect(os.path.join(d, "workbench.db"))
            try:
                rid = db.begin_agent_run(conn, "conv-refresh", "HR", "帮我找 Java 人选")
                self.assertGreater(rid, 0)
                hist = db.chat_history(conn, conversation_id="conv-refresh",
                                       operator="HR")
                self.assertEqual([m["role"] for m in hist["messages"]], ["user"])
                self.assertEqual(hist["messages"][0]["content"], "帮我找 Java 人选")
                self.assertEqual(hist["messages"][0]["status"], "running")
                convs = db.list_conversations(conn, "HR")
                self.assertTrue(any(c["id"] == "conv-refresh" for c in convs))

                self.assertEqual(db.mark_interrupted_agent_runs(conn), 1)
                hist = db.chat_history(conn, conversation_id="conv-refresh",
                                       operator="HR")
                self.assertEqual(hist["messages"][0]["status"], "interrupted")
                self.assertIn("中断", hist["messages"][1]["content"])
            finally:
                _close_physical(conn)

    def test_finished_run_fills_the_same_row(self):
        with tempfile.TemporaryDirectory() as d:
            conn = db.connect(os.path.join(d, "workbench.db"))
            try:
                rid = db.begin_agent_run(conn, "conv-done", "HR", "问题")
                db.log_agent_run(conn, {
                    "run_id": rid, "session_id": "conv-done", "operator": "HR",
                    "question": "问题", "answer": "回答", "status": "ok",
                    "mode": "llm",
                })
                rows = conn.execute(
                    "SELECT id, answer, status FROM agent_runs").fetchall()
                self.assertEqual(len(rows), 1)
                self.assertEqual(rows[0]["id"], rid)
                self.assertEqual(rows[0]["answer"], "回答")
                self.assertEqual(rows[0]["status"], "ok")
            finally:
                _close_physical(conn)


if __name__ == "__main__":
    unittest.main()
