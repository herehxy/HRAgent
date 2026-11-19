"""收信游标回归：中途停止不能跳过“已下载但未入库”的邮件。"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from unittest import mock

from app import db, ingest, mailbox


def _close_pool_path(path: str) -> None:
    pool = getattr(db._db_pool_local, "by_key", None) if hasattr(db, "_db_pool_local") else None
    if not pool:
        return
    pooled = pool.pop((os.path.abspath(path), True), None)
    if pooled is not None:
        sqlite3.Connection.close(pooled)


class MailboxCursorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="mail_cursor_test_")
        self.db_path = os.path.join(self.tmp, "cursor.db")
        conn = db.connect(self.db_path)
        try:
            db.set_email_cursor(conn, "INBOX", {"account": "u@h", "uid": "100"})
        finally:
            conn.close()

    def tearDown(self) -> None:
        _close_pool_path(self.db_path)
        try:
            os.remove(self.db_path)
        except OSError:
            pass
        try:
            os.rmdir(self.tmp)
        except OSError:
            pass

    @staticmethod
    def _cfg() -> dict:
        return {"mode": "imap",
                "imap": {"host": "h", "user": "u", "folder": "INBOX"}}

    def _cursor(self) -> str:
        conn = db.connect(self.db_path)
        try:
            raw = db.email_cursor(conn, "INBOX") or {}
            return str(raw.get("uid") or "")
        finally:
            conn.close()

    def test_stop_after_download_before_ingest_keeps_cursor(self):
        mails = [{"uid": "101", "subject": "a"}, {"uid": "102", "subject": "b"}]
        with mock.patch.object(mailbox, "fetch_imap", return_value=(mails, "102")), \
                mock.patch.object(ingest, "stop_requested", return_value=True), \
                mock.patch.object(ingest, "update_progress", lambda *a, **k: None), \
                mock.patch.object(ingest, "spawn_auto_analysis", lambda *a, **k: None), \
                mock.patch.object(ingest, "ingest_mails",
                                  side_effect=AssertionError("停止后不应入库")):
            report = ingest.sync_mailbox({}, {}, self.db_path, cfg=self._cfg(),
                                         progress_tid="t-stop", limit=0)
        self.assertTrue(report.get("stopped"))
        self.assertEqual(self._cursor(), "100", "未入库的邮件不能推进游标")

    def test_fetch_imap_batch_memory_cap(self):
        def fake_iter(*args, **kwargs):
            yield ("uidvalidity", "uv1")
            yield ("mail", {"uid": "101", "attachments": [{"data": b"x" * 6}]})
            yield ("mail", {"uid": "102", "attachments": [{"data": b"y" * 6}]})

        meta: dict = {}
        with mock.patch.object(mailbox, "iter_imap", side_effect=fake_iter):
            mails, newest = mailbox.fetch_imap({}, "100", meta=meta, max_bytes=10)
        self.assertEqual([m["uid"] for m in mails], ["101"],
                         "达到内存上限后要停在这一封之前，下一批再收")
        self.assertEqual(newest, "101")
        self.assertTrue(meta.get("batch_limit_hit"))

    def test_fetch_error_stops_before_failed_uid(self):
        meta: dict = {}

        def fake_iter(*args, **kwargs):
            yield ("uidvalidity", "uv1")
            yield ("mail", {"uid": "101"})
            yield ("fetch_error", {"uid": "102", "reason": "FETCH NO"})
            yield ("mail", {"uid": "103"})

        with mock.patch.object(mailbox, "iter_imap", side_effect=fake_iter):
            mails, newest = mailbox.fetch_imap({}, "100", meta=meta)
        self.assertEqual([m["uid"] for m in mails], ["101"])
        self.assertEqual(newest, "101", "FETCH 失败后不能越过它")
        self.assertEqual(meta.get("fetch_error", {}).get("uid"), "102")
        self.assertEqual(meta.get("uidvalidity"), "uv1")

    def test_uidvalidity_change_resets_cursor_once(self):
        conn = db.connect(self.db_path)
        try:
            db.set_email_cursor(conn, "INBOX",
                                {"account": "u@h", "uid": "100",
                                 "uidvalidity": "old"})
        finally:
            conn.close()
        calls: list[str] = []

        def fake_fetch(cfg, cursor, limit=200, should_stop=None, meta=None):
            calls.append(cursor)
            if meta is not None:
                meta["uidvalidity"] = "new"
            return ([{"uid": "1"}], "1")

        with mock.patch.object(mailbox, "fetch_imap", side_effect=fake_fetch), \
                mock.patch.object(ingest, "stop_requested", return_value=False), \
                mock.patch.object(ingest, "update_progress", lambda *a, **k: None), \
                mock.patch.object(ingest, "spawn_auto_analysis", lambda *a, **k: None), \
                mock.patch.object(ingest, "ingest_mails", lambda *a, **k: None):
            report = ingest.sync_mailbox({}, {}, self.db_path, cfg=self._cfg(),
                                         progress_tid="t-uv", limit=0)
        self.assertEqual(calls, ["100", ""], "UIDVALIDITY 变化后应从头发扫一次")
        self.assertTrue(report.get("uidvalidity_reset"))
        conn = db.connect(self.db_path)
        try:
            raw = db.email_cursor(conn, "INBOX") or {}
        finally:
            conn.close()
        self.assertEqual(raw.get("uid"), "1")
        self.assertEqual(raw.get("uidvalidity"), "new")

    def test_legacy_cursor_without_uidvalidity_resets_once(self):
        calls: list[str] = []

        def fake_fetch(cfg, cursor, limit=200, should_stop=None, meta=None):
            calls.append(cursor)
            if meta is not None:
                meta["uidvalidity"] = "new"
            return ([{"uid": "1"}], "1")

        with mock.patch.object(mailbox, "fetch_imap", side_effect=fake_fetch), \
                mock.patch.object(ingest, "stop_requested", return_value=False), \
                mock.patch.object(ingest, "update_progress", lambda *a, **k: None), \
                mock.patch.object(ingest, "spawn_auto_analysis", lambda *a, **k: None), \
                mock.patch.object(ingest, "ingest_mails", lambda *a, **k: None):
            report = ingest.sync_mailbox({}, {}, self.db_path, cfg=self._cfg(),
                                         progress_tid="t-legacy", limit=0)
        self.assertEqual(calls, ["100", ""],
                         "没有 uidvalidity 的旧游标要先全量重扫一次")
        self.assertTrue(report.get("uidvalidity_reset"))

    def test_auto_dead_letter_after_threshold(self):
        conn = db.connect(self.db_path)
        try:
            db.record_email(conn, {"message_id": "m-dead", "uid": "101",
                                   "mailbox": "INBOX", "subject": "坏邮件"})
            for i in range(3):
                db.bump_email_retry(conn, "m-dead", f"fail {i}")
        finally:
            conn.close()
        mails = [{"uid": "101", "message_id": "m-dead", "subject": "坏邮件"}]

        def fake_ingest(*args, **kwargs):
            report = kwargs.get("report")
            if report is not None:
                report["failed"] = int(report.get("failed") or 0) + 1
                report["blocked_files"] = ["坏简历.pdf"]

        with mock.patch.object(mailbox, "fetch_imap", return_value=(mails, "101")), \
                mock.patch.object(ingest, "stop_requested", return_value=False), \
                mock.patch.object(ingest, "update_progress", lambda *a, **k: None), \
                mock.patch.object(ingest, "spawn_auto_analysis", lambda *a, **k: None), \
                mock.patch.object(ingest, "ingest_mails", side_effect=fake_ingest):
            report = ingest.sync_mailbox({}, {}, self.db_path, cfg=self._cfg(),
                                         progress_tid="t-dead", limit=0)
        self.assertEqual(report.get("dead_lettered"), 1)
        self.assertEqual(self._cursor(), "101", "死信要越过并保存游标")
        conn = db.connect(self.db_path)
        try:
            row = conn.execute("SELECT dead_letter,processed,error FROM email_messages "
                               "WHERE message_id='m-dead'").fetchone()
        finally:
            conn.close()
        self.assertEqual(row["dead_letter"], 1)
        self.assertEqual(row["processed"], 0)
        self.assertIn("坏简历.pdf", row["error"] or "",
                      "死信要留下失败附件名，HR 才知道该补哪一份")

    def test_legacy_path_with_failure_keeps_cursor(self):
        mails = [{"uid": "101"}, {"uid": "102"}]
        with mock.patch.object(mailbox, "incoming", return_value=(mails, "102")), \
                mock.patch.object(ingest, "ingest_mails", return_value={"failed": 1}):
            ingest.sync_mailbox({}, {}, self.db_path, cfg=self._cfg(), limit=0)
        self.assertEqual(self._cursor(), "100", "旧路径有失败也不能推进游标")

    def test_other_attachment_failure_keeps_cursor(self):
        """只有补充附件失败（other_failed）时也不能推进游标。"""
        mails = [{"uid": "101", "message_id": "m-other", "subject": "补充附件坏"}]

        def fake_ingest(*args, **kwargs):
            report = kwargs.get("report")
            if report is not None:
                report["other_failed"] = int(report.get("other_failed") or 0) + 1

        with mock.patch.object(mailbox, "fetch_imap", return_value=(mails, "101")), \
                mock.patch.object(ingest, "stop_requested", return_value=False), \
                mock.patch.object(ingest, "update_progress", lambda *a, **k: None), \
                mock.patch.object(ingest, "spawn_auto_analysis", lambda *a, **k: None), \
                mock.patch.object(ingest, "ingest_mails", side_effect=fake_ingest):
            report = ingest.sync_mailbox({}, {}, self.db_path, cfg=self._cfg(),
                                         progress_tid="t-other", limit=0)
        self.assertTrue(report.get("retry_pending"))
        self.assertEqual(self._cursor(), "100", "补充附件失败也不能推进游标")

    def test_legacy_path_with_other_failure_keeps_cursor(self):
        mails = [{"uid": "101"}, {"uid": "102"}]
        with mock.patch.object(mailbox, "incoming", return_value=(mails, "102")), \
                mock.patch.object(ingest, "ingest_mails",
                                  return_value={"other_failed": 1}):
            ingest.sync_mailbox({}, {}, self.db_path, cfg=self._cfg(), limit=0)
        self.assertEqual(self._cursor(), "100", "旧路径补充附件失败也不能推进游标")

    def test_successful_ingest_advances_to_last_processed(self):
        mails = [{"uid": "101", "subject": "a"}, {"uid": "102", "subject": "b"}]
        seen: list[str] = []

        def fake_ingest(batch, *args, **kwargs):
            seen.extend(m["uid"] for m in batch)

        with mock.patch.object(mailbox, "fetch_imap", return_value=(mails, "102")), \
                mock.patch.object(ingest, "stop_requested", return_value=False), \
                mock.patch.object(ingest, "update_progress", lambda *a, **k: None), \
                mock.patch.object(ingest, "spawn_auto_analysis", lambda *a, **k: None), \
                mock.patch.object(ingest, "ingest_mails", side_effect=fake_ingest):
            ingest.sync_mailbox({}, {}, self.db_path, cfg=self._cfg(),
                                progress_tid="t-ok", limit=0)
        self.assertEqual(seen, ["101", "102"])
        self.assertEqual(self._cursor(), "102")


if __name__ == "__main__":
    unittest.main()
