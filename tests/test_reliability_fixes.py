# -*- coding: utf-8 -*-
"""6.2.1 可靠性修复回归：发信幂等、附件不静默丢、批量归档原子性、导出结论。

这些用例只碰 DB/纯函数，不依赖模型或网络。
"""
from __future__ import annotations

import os
import smtplib
import sys
import tempfile
import unittest
from email.message import EmailMessage
from unittest import mock

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from app import db, export, ingest, mail_send, mailbox as mb  # noqa: E402


class ReliabilityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="reliability_")
        cls.db_path = os.path.join(cls.tmp, "test.db")

    def _conn(self):
        return db.connect(self.db_path)

    # ---------------------------------------------------------- 发信幂等
    def test_mail_request_state_machine(self):
        conn = self._conn()
        conn.close()
        rid = f"req-{self.id()}"
        rec = db.mail_request_begin(self.db_path, rid, "task-1", status="queued")
        self.assertTrue(rec["created"])
        # queued 阶段进程崩掉：允许接管（SMTP 一定没开始）
        rec = db.mail_request_begin(self.db_path, rid, "task-2", status="queued")
        self.assertTrue(rec["created"] and rec.get("takeover"))
        # 进入 sending 后不允许再接管
        self.assertTrue(db.mail_request_mark_sending(self.db_path, rid, "task-2"))
        rec = db.mail_request_begin(self.db_path, rid, "task-3", status="queued")
        self.assertFalse(rec["created"])
        self.assertEqual(rec["status"], "sending")
        # 成功结果可回放
        self.assertTrue(db.mail_request_finish(
            self.db_path, rid, "task-2", "sent", result={"ok": True, "n": 2}))
        rec = db.mail_request_begin(self.db_path, rid, "task-4", status="queued")
        self.assertFalse(rec["created"])
        self.assertEqual(rec["status"], "sent")
        self.assertEqual((rec.get("result") or {}).get("n"), 2)

    def test_stale_mail_requests_are_never_resent(self):
        rid1, rid2 = f"stale-q-{self.id()}", f"stale-s-{self.id()}"
        db.mail_request_begin(self.db_path, rid1, "tq", status="queued")
        db.mail_request_begin(self.db_path, rid2, "ts", status="queued")
        db.mail_request_mark_sending(self.db_path, rid2, "ts")
        db.mail_requests_mark_stale(self.db_path)
        r1 = db.mail_request_begin(self.db_path, rid1, "x", status="queued")
        r2 = db.mail_request_begin(self.db_path, rid2, "y", status="queued")
        self.assertEqual(r1["status"], "failed")
        self.assertEqual(r2["status"], "unknown")

    def test_restart_reconciles_sent_mail_task_snapshot(self):
        rid, tid = f"recon-{self.id()}", f"recon-task-{self.id()}"
        db.task_upsert(self.db_path, {
            "task_id": tid, "kind": "mail", "title": "发送邮件",
            "status": "error", "message": "服务重启，任务已中断"})
        db.mail_request_begin(self.db_path, rid, tid, status="queued")
        db.mail_request_mark_sending(self.db_path, rid, tid)
        db.mail_request_finish(self.db_path, rid, tid, "sent",
                               result={"ok": True, "n": 3})
        db.reconcile_mail_tasks(self.db_path)
        t = db.task_get(self.db_path, tid)
        self.assertEqual(t.get("status"), "done")
        self.assertEqual((t.get("result") or {}).get("n"), 3,
                         "已发送的请求不能被重启误报成失败")

    def test_smtp_disconnect_during_send_is_uncertain(self):
        """sendmail 阶段断线不能被当作“确定未发出”，否则会诱导重发。"""
        fake = mock.MagicMock()
        fake.sendmail.side_effect = smtplib.SMTPServerDisconnected("boom")
        used = {"host": "smtp.example.com", "port": 465, "ssl": True, "mode": "SSL"}
        cfg = {"smtp": {"user": "from@example.com", "host": "smtp.example.com",
                        "port": 465, "ssl": True}}
        with mock.patch.object(mail_send, "connect_smtp",
                               return_value=(fake, used, False, [])):
            out = mail_send.send_mail("to@example.com", "主题", "正文",
                                      cfg=cfg, password="pw")
        self.assertFalse(out.get("ok"))
        self.assertTrue(out.get("uncertain"), "发送中断必须标记为结果不确定")

    # ---------------------------------------------------------- 附件可追踪
    def test_unknown_attachment_is_tracked_and_archived(self):
        msg = EmailMessage()
        msg["Subject"] = "附件测试"
        msg.set_content("hello")
        msg.add_attachment(b"pdf-bytes", maintype="application", subtype="pdf",
                           filename="简历.pdf")
        msg.add_attachment(
            b"pptx-bytes", maintype="application",
            subtype="vnd.openxmlformats-officedocument.presentationml.presentation",
            filename="作品集.pptx")
        cfg = {"attachment_ext": [".pdf"], "max_attachment_mb": 20}
        atts, skipped = mb.extract_attachments_ex(msg, cfg)
        self.assertEqual([a["filename"] for a in atts], ["简历.pdf"])
        self.assertEqual(len(skipped), 1)
        self.assertEqual(skipped[0]["reason"], "extension_not_allowed")
        self.assertEqual(skipped[0]["data"], b"pptx-bytes")

        conn = self._conn()
        try:
            cfg2 = dict(cfg)
            cfg2["archive_dir"] = os.path.join(self.tmp, "archive")
            res = ingest._save_other_attachments(
                conn, cfg2, skipped, candidate_id=None, message_id="m-1")
            self.assertEqual(res["saved"], ["作品集.pptx"])
            self.assertFalse(res["failed"])
            row = conn.execute(
                "SELECT kind, parse_ok FROM documents WHERE file_name = ?",
                ("作品集.pptx",)).fetchone()
            self.assertIsNotNone(row)
            self.assertEqual(row["kind"], "other")
            self.assertIsNone(row["parse_ok"], "未解析不能被误报成解析失败")
        finally:
            conn.close()

    def test_small_inline_image_is_not_silently_lost(self):
        from email.mime.image import MIMEImage
        msg = EmailMessage()
        msg["Subject"] = "只有一张小图"
        msg.set_content("hello")
        msg.make_mixed()
        img = MIMEImage(b"x" * 128, _subtype="png")
        img.add_header("Content-ID", "<photo1>")
        img.add_header("Content-Disposition", "inline", filename="照片.png")
        msg.attach(img)
        atts, skipped = mb.extract_attachments_ex(
            msg, {"attachment_ext": [".png"], "max_attachment_mb": 20})
        self.assertEqual(len(atts), 1, "整封只有小图时应把它当简历候选，而不是静默丢掉")
        self.assertEqual(skipped, [])

    def test_oversize_attachment_is_reported_not_silent(self):
        msg = EmailMessage()
        msg["Subject"] = "超限"
        msg.set_content("hello")
        msg.add_attachment(b"x" * 64, maintype="application", subtype="pdf",
                           filename="大简历.pdf")
        atts, skipped = mb.extract_attachments_ex(
            msg, {"attachment_ext": [".pdf"], "max_attachment_mb": 0})
        self.assertEqual(atts, [])
        self.assertEqual(skipped[0]["reason"], "oversize")

    # ---------------------------------------------------------- 批量归档
    def test_batch_archive_rolls_back_on_unexpected_error(self):
        conn = self._conn()
        try:
            c1 = db.insert_candidate(conn, {"identity_key": f"batch:a:{self.id()}",
                                            "name": "批量甲"})
            c2 = db.insert_candidate(conn, {"identity_key": f"batch:b:{self.id()}",
                                            "name": "批量乙"})
            a1 = db.insert_application(conn, {"candidate_id": c1, "stage": "待面试"})
            a2 = db.insert_application(conn, {"candidate_id": c2, "stage": "待面试"})
            orig = db._set_candidate_archived_locked
            state = {"n": 0}

            def flaky(conn_, cid, *a, **kw):
                state["n"] += 1
                if state["n"] == 2:
                    raise RuntimeError("模拟第二人写入失败")
                return orig(conn_, cid, *a, **kw)

            db._set_candidate_archived_locked = flaky
            try:
                with self.assertRaises(RuntimeError):
                    db.set_candidates_archived(conn, [c1, c2], True, "tester", "hr")
            finally:
                db._set_candidate_archived_locked = orig
            for cid in (c1, c2):
                row = db.get_candidate(conn, cid)
                self.assertFalse(row.get("archived_at"),
                                 "批量归档失败必须整批回滚，不能只归档前一半")
            for aid in (a1, a2):
                self.assertEqual(db.get_application(conn, aid).get("stage"),
                                 "待面试", "回滚必须把自动结束的投递阶段也还原")
            n_audit = conn.execute(
                "SELECT COUNT(*) AS n FROM audit_log WHERE action IN "
                "('archive','auto_end_stage') AND entity='candidate' "
                "AND entity_id IN (?,?)", (str(c1), str(c2))).fetchone()["n"]
            self.assertEqual(n_audit, 0, "回滚后不能留下归档审计")
        finally:
            conn.close()

    def test_batch_archive_skips_merged_shell(self):
        conn = self._conn()
        try:
            src = db.insert_candidate(conn, {"identity_key": f"merged-src:{self.id()}",
                                             "name": "合并源甲"})
            dst = db.insert_candidate(conn, {"identity_key": f"merged-dst:{self.id()}",
                                             "name": "合并目标甲"})
            r = db.merge_candidates(conn, src, dst, "tester", "hr")
            self.assertTrue(r.get("ok"), r)
            out = db.set_candidates_archived(conn, [src], True, "tester", "hr")
            self.assertEqual(out["changed"], 0)
            self.assertIn(src, out["skipped_ids"])
            self.assertFalse(db.get_candidate(conn, src).get("archived_at"),
                             "合并壳档不能写 archived_at，否则 split 后仍在归档区")
        finally:
            conn.close()

    def test_stage_update_rolls_back_if_audit_write_fails(self):
        """add_audit 抛异常时，阶段 UPDATE 不能留给后续 commit 顺手提交。"""
        conn = self._conn()
        try:
            cid = db.insert_candidate(conn, {"identity_key": f"stage-tx:{self.id()}",
                                             "name": "阶段事务甲"})
            aid = db.insert_application(conn, {"candidate_id": cid, "stage": "新投递"})
            orig = db.add_audit

            def flaky(conn_, entity, entity_id, action, *a, **k):
                if action == "set_stage":
                    raise RuntimeError("模拟审计写入失败")
                return orig(conn_, entity, entity_id, action, *a, **k)

            db.add_audit = flaky
            try:
                with self.assertRaises(RuntimeError):
                    db.set_application_stage(conn, aid, "初筛通过", None,
                                             operator="tester", role="hr")
            finally:
                db.add_audit = orig
            self.assertEqual(db.get_application(conn, aid).get("stage"), "新投递",
                             "审计失败必须整笔回滚阶段")
            n = conn.execute(
                "SELECT COUNT(*) AS n FROM audit_log WHERE action='set_stage' "
                "AND entity_id=?", (str(aid),)).fetchone()["n"]
            self.assertEqual(n, 0)
        finally:
            conn.close()

    def test_archive_before_year_rechecks_new_application_in_transaction(self):
        conn = self._conn()
        try:
            cid = db.insert_candidate(conn, {"identity_key": f"year:{self.id()}",
                                             "name": "跨年投递甲"})
            db.insert_application(conn, {"candidate_id": cid, "stage": "待面试",
                                         "applied_at": "2026-03-01 10:00:00"})
            out = db.set_candidates_archived(
                conn, [cid], True, "tester", "hr", before_year=2025)
            self.assertEqual(out["changed"], 0)
            self.assertEqual(out["skipped"], 1)
            self.assertFalse(db.get_candidate(conn, cid).get("archived_at"))
            self.assertEqual(db.get_application(
                conn, conn.execute("SELECT id FROM applications WHERE candidate_id=?",
                                   (cid,)).fetchone()["id"]).get("stage"),
                "待面试")
        finally:
            conn.close()

    # ---------------------------------------------------------- 导出结论
    def test_roster_keeps_earlier_signed_application(self):
        conn = self._conn()
        try:
            cid = db.insert_candidate(conn, {"identity_key": f"multi:{self.id()}",
                                             "name": "多投签约甲"})
            old = db.insert_application(conn, {
                "candidate_id": cid, "stage": "已发放offer",
                "applied_at": "2026-01-01 10:00:00"})
            db.insert_application(conn, {
                "candidate_id": cid, "stage": "新投递",
                "applied_at": "2026-02-01 10:00:00"})
            db.update_application_fields(conn, old, outcome="已签约",
                                         signed_at="2026-01-15",
                                         offer_sent_at="2026-01-10",
                                         operator="tester", role="hr")
            headers, rows, _tcols, _n = export.build_roster(
                conn, include_archived=True)
            row = [r for r in rows if r[headers.index("姓名")] == "多投签约甲"]
            self.assertEqual(len(row), 1)
            r = row[0]
            # 业务口径：签约情况=已签约 → 「是否录用」显示「拟录用」
            self.assertEqual(r[headers.index("是否录用")], "拟录用")
            self.assertNotEqual(r[headers.index("签约时间")], export.EMPTY)
            self.assertNotEqual(r[headers.index("录用函发送时间")], export.EMPTY)
        finally:
            conn.close()

    def test_roster_positive_outcome_query_handles_1000_plus(self):
        """1100 人的库不能因为 IN (?) 超过 SQLite 变量上限而导出失败。"""
        conn = self._conn()
        try:
            ids = []
            for i in range(1001):
                cur = conn.execute(
                    "INSERT INTO candidates (identity_key, name, created_at, updated_at) "
                    "VALUES (?,?,?,?)",
                    (f"bulk:{self.id()}:{i}", f"批量{i}", "2026-01-01", "2026-01-01"))
                ids.append(int(cur.lastrowid))
            conn.commit()
            items = [{"id": cid, "stage": "新投递", "outcome": None,
                      "sign_status": None} for cid in ids]
            export._apply_positive_outcomes(conn, items, db)
            self.assertEqual(len(items), 1001)
        finally:
            conn.close()

    def test_roster_old_rejection_does_not_cover_new_active_application(self):
        conn = self._conn()
        try:
            cid = db.insert_candidate(conn, {"identity_key": f"multi-oldneg:{self.id()}",
                                             "name": "旧拒新投甲"})
            old = db.insert_application(conn, {
                "candidate_id": cid, "stage": "已结束",
                "applied_at": "2026-01-01 10:00:00"})
            db.insert_application(conn, {
                "candidate_id": cid, "stage": "待面试",
                "applied_at": "2026-03-01 10:00:00"})
            db.update_application_fields(conn, old, outcome="拒签",
                                         sign_status="拒签",
                                         offer_sent_at="2026-01-05",
                                         operator="tester", role="hr")
            headers, rows, _tcols, _n = export.build_roster(
                conn, include_archived=True)
            r = [x for x in rows if x[headers.index("姓名")] == "旧拒新投甲"][0]
            self.assertEqual(r[headers.index("是否录用")], export.EMPTY,
                             "旧岗位的拒签不能把新岗位的待面试盖成不录用")
            self.assertEqual(r[headers.index("录用函发送时间")], export.EMPTY)
        finally:
            conn.close()

    def test_roster_keeps_latest_interview_fields_with_old_offer(self):
        """旧 offer 只能补录用结论，不能把当前面试单位/日期整行盖掉。"""
        conn = self._conn()
        try:
            cid = db.insert_candidate(conn, {"identity_key": f"multi-int:{self.id()}",
                                             "name": "多投面试甲"})
            old = db.insert_application(conn, {
                "candidate_id": cid, "stage": "已发放offer",
                "applied_at": "2026-01-01 10:00:00"})
            new = db.insert_application(conn, {
                "candidate_id": cid, "stage": "待面试",
                "applied_at": "2026-03-01 10:00:00"})
            db.update_application_fields(conn, old, offer_sent_at="2026-01-10",
                                         operator="tester", role="hr")
            db.update_application_fields(conn, new, interview_unit="新单位",
                                         interview_date="2026-03-10",
                                         operator="tester", role="hr")
            headers, rows, _tcols, _n = export.build_roster(
                conn, include_archived=True)
            r = [x for x in rows if x[headers.index("姓名")] == "多投面试甲"][0]
            self.assertEqual(r[headers.index("面试单位")], "新单位")
            self.assertEqual(r[headers.index("面试日期")], "2026-03-10")
            self.assertEqual(r[headers.index("是否录用")], "拟录用")
            self.assertNotEqual(r[headers.index("录用函发送时间")], export.EMPTY)
        finally:
            conn.close()

    def test_roster_old_signed_beats_new_positive(self):
        conn = self._conn()
        try:
            cid = db.insert_candidate(conn, {"identity_key": f"multi-pos:{self.id()}",
                                             "name": "多投拟录甲"})
            old = db.insert_application(conn, {
                "candidate_id": cid, "stage": "已发放offer",
                "applied_at": "2026-01-01 10:00:00"})
            new = db.insert_application(conn, {
                "candidate_id": cid, "stage": "已面试",
                "applied_at": "2026-03-01 10:00:00"})
            db.update_application_fields(conn, old, outcome="已签约",
                                         signed_at="2026-01-15",
                                         offer_sent_at="2026-01-10",
                                         operator="tester", role="hr")
            db.update_application_fields(conn, new, outcome="拟录用",
                                         operator="tester", role="hr")
            headers, rows, _tcols, _n = export.build_roster(
                conn, include_archived=True)
            r = [x for x in rows if x[headers.index("姓名")] == "多投拟录甲"][0]
            self.assertEqual(r[headers.index("是否录用")], "拟录用")
            self.assertNotEqual(r[headers.index("签约时间")], export.EMPTY)
            self.assertNotEqual(r[headers.index("录用函发送时间")], export.EMPTY)
        finally:
            conn.close()

    def test_roster_does_not_hide_later_rejection(self):
        conn = self._conn()
        try:
            cid = db.insert_candidate(conn, {"identity_key": f"multi-neg:{self.id()}",
                                             "name": "先签后拒甲"})
            old = db.insert_application(conn, {
                "candidate_id": cid, "stage": "已发放offer",
                "applied_at": "2026-01-01 10:00:00"})
            new = db.insert_application(conn, {
                "candidate_id": cid, "stage": "已结束",
                "applied_at": "2026-02-01 10:00:00"})
            db.update_application_fields(conn, old, outcome="已签约",
                                         signed_at="2026-01-15",
                                         offer_sent_at="2026-01-10",
                                         operator="tester", role="hr")
            db.update_application_fields(conn, new, outcome="拒签",
                                         sign_status="拒签",
                                         operator="tester", role="hr")
            headers, rows, _tcols, _n = export.build_roster(
                conn, include_archived=True)
            r = [x for x in rows if x[headers.index("姓名")] == "先签后拒甲"][0]
            self.assertEqual(r[headers.index("是否录用")], "不录用",
                             "最新的拒签结论不能被较早的签约记录盖掉")
            self.assertEqual(r[headers.index("签约时间")], export.EMPTY)
        finally:
            conn.close()


if __name__ == "__main__":
    unittest.main()
