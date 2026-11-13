# -*- coding: utf-8 -*-
"""v6.0.0 代码审查修复的回归测试。

覆盖：
  B01 弱身份键（nm:/ns:）不再自动并档
  B02 任务/配置接口必须鉴权；meta 不下发部署细节
  B03 viewer 不能通过 rename / 导入等写接口改数据
  B05 导出接口要求 export 权限
  B06 提案 args/summary 不泄露手机号、邮箱
  B07 邮件幂等键跨进程持久化
  B08 提案决定是原子认领，非法 decision 422
  B10 最新简历会刷新联系方式与盲索引
  B11 审计中的邮箱打码
  B12 已有导入任务时拒绝并发发起
  B16 过期任务快照会清理；重启只中断长时间无更新的任务
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

_TMP = tempfile.mkdtemp(prefix="review_fix_test_")
_DB = os.path.join(_TMP, "review.db")
os.environ["TP_DB_PATH"] = _DB
os.environ["TP_AUTH"] = "0"
os.environ["TP_AUTO_INSIGHT"] = "0"
os.environ["TP_DAILY_TASK"] = "0"
os.environ["TP_BRIEF_LLM"] = "0"

from app import auth, crypto, db, ingest, server  # noqa: E402
from app.agent import tools as agent_tools  # noqa: E402
from tests.selftest import _asgi  # noqa: E402


class ReviewFixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        conn = db.connect(_DB)
        conn.close()

    # ---------------------------------------------------------------- B01
    def test_weak_identity_key_does_not_auto_merge(self):
        conn = db.connect(_DB)
        try:
            c1, new1, hint1 = ingest.ensure_person(
                conn, {"name": "同名甲", "contact": {}}, "测试", "同名甲 简历")
            c2, new2, hint2 = ingest.ensure_person(
                conn, {"name": "同名甲", "contact": {}}, "测试", "同名甲 简历")
            self.assertTrue(new1)
            self.assertTrue(new2, "弱身份键命中时也必须新建档案")
            self.assertNotEqual(c1, c2, "同名无联系方式不能并成一个人")
            self.assertTrue(hint2.get("suspect"), "新建后仍要提示同名疑似")

            c3, _new3, _ = ingest.ensure_person(
                conn, {"name": "强键乙", "contact": {"phone": "13800138000"}},
                "测试", "强键乙 简历")
            c4, new4, _ = ingest.ensure_person(
                conn, {"name": "强键乙", "contact": {"phone": "13800138000"}},
                "测试", "强键乙 简历")
            self.assertEqual(c3, c4, "同手机号必须命中同一档案")
            self.assertFalse(new4)
        finally:
            conn.close()

    # ---------------------------------------------------------------- B10
    def test_latest_resume_refreshes_contact(self):
        conn = db.connect(_DB)
        try:
            cid, _new, _ = ingest.ensure_person(
                conn, {"name": "换号丙",
                       "contact": {"phone": "13900000001", "email": "bing@example.com"}},
                "测试", "换号丙 简历")
            ingest._apply_resume_fields(
                conn, cid,
                {"name": "换号丙",
                 "contact": {"phone": "13900000002", "email": "bing@example.com"}},
                "")
            row = db.get_candidate(conn, cid)
            self.assertEqual(crypto.decrypt(row["phone_enc"]), "13900000002")
            self.assertEqual(row["phone_bidx"], crypto.blind_index("13900000002"))
            # 邮箱没变，不应把盲索引写坏
            self.assertEqual(crypto.decrypt(row["email_enc"]), "bing@example.com")
        finally:
            conn.close()

    # ---------------------------------------------------------------- B11
    def test_audit_email_is_masked(self):
        masked = db.audit_field_value("email_enc", "alice@example.com")
        self.assertNotIn("alice@example.com", masked)
        self.assertIn("@example.com", masked)
        phone = db.audit_field_value("phone_enc", "13800138000")
        self.assertNotIn("13800138000", phone)

    # ---------------------------------------------------------------- B06
    def test_proposal_args_are_masked(self):
        conn = db.connect(_DB)
        try:
            cid = db.insert_candidate(conn, {"identity_key": "test:proposal-mask",
                                             "name": "提案脱敏丁"})
            pid = db.create_proposal(
                conn, "test-session", "update_candidate_fields",
                {"candidate_id": cid, "fields": {"phone_enc": "13800138000",
                                                 "email_enc": "ding@example.com"}},
                "更正电话/邮箱", "中")
            items = db.list_proposals(conn)
            p = next(x for x in items if x["id"] == pid)
            fields = p["args"].get("fields") or {}
            self.assertEqual(fields.get("phone_enc"), "（已设置）")
            self.assertEqual(fields.get("email_enc"), "（已设置）")
            self.assertNotIn("13800138000", json.dumps(p, ensure_ascii=False))
        finally:
            conn.close()

    # ---------------------------------------------------------------- B08
    def test_proposal_claim_is_atomic(self):
        conn = db.connect(_DB)
        try:
            pid = db.create_proposal(
                conn, "test-session", "add_note",
                {"candidate_id": 1, "note": "并发测试"}, "并发测试", "低")
            self.assertTrue(db.claim_proposal(conn, pid, "hr"))
            self.assertFalse(db.claim_proposal(conn, pid, "hr"),
                             "同一提案只能有一个请求领到执行权")
            db.release_proposal(conn, pid)
            self.assertTrue(db.claim_proposal(conn, pid, "hr"))
            db.decide_proposal(conn, pid, "reject", "hr", "hr", "测试清理")
        finally:
            conn.close()

    def test_decide_requires_valid_decision(self):
        conn = db.connect(_DB)
        try:
            pid = db.create_proposal(
                conn, "test-session", "add_note",
                {"candidate_id": 1, "note": "decision 校验"}, "decision 校验", "低")
        finally:
            conn.close()
        status, _ = _asgi(server.app, "POST", f"/api/proposals/{pid}/decide",
                          {"Content-Type": "application/json"}, b"{}")
        self.assertEqual(status, 422, "空 body 不能默认按 approve 执行")
        status, _ = _asgi(server.app, "POST", f"/api/proposals/{pid}/decide",
                          {"Content-Type": "application/json"},
                          json.dumps({"decision": "maybe"}).encode())
        self.assertEqual(status, 422, "非法 decision 必须 422")

    # ---------------------------------------------------------------- B07
    def test_mail_idempotency_is_persistent(self):
        tid, created = db.mail_request_claim(_DB, "review-req-1", "task-a")
        self.assertTrue(created)
        self.assertEqual(tid, "task-a")
        tid2, created2 = db.mail_request_claim(_DB, "review-req-1", "task-b")
        self.assertFalse(created2, "同一 client_request_id 不能再次认领")
        self.assertEqual(tid2, "task-a", "重试必须回到原任务号")

    # ---------------------------------------------------------------- B02/B03/B05
    def _make_session(self, role: str, username: str) -> str:
        conn = db.connect(_DB)
        try:
            if not db.get_user(conn, username):
                db.create_user(conn, username, "pw12345678", role, username)
            return db.create_session(conn, username, role)
        finally:
            conn.close()

    def test_auth_boundaries(self):
        conn = db.connect(_DB)
        try:
            cid = db.insert_candidate(conn, {"identity_key": "test:auth-candidate",
                                             "name": "鉴权戊"})
        finally:
            conn.close()
        os.environ["TP_AUTH"] = "1"
        try:
            hr_tok = self._make_session("hr", "hr_review")
            vw_tok = self._make_session("viewer", "viewer_review")
            vw_h = {"X-TP-Token": vw_tok, "Content-Type": "application/json"}

            for path in ("/api/tasks/active", "/api/tasks/not-a-task",
                         "/api/meta", "/api/interview-dict"):
                st, _ = _asgi(server.app, "GET", path, {})
                self.assertEqual(st, 401, f"匿名访问 {path} 必须 401")

            st, _ = _asgi(server.app, "GET", "/api/mail/smtp", vw_h)
            self.assertEqual(st, 403, "viewer 不能读 SMTP 配置")

            st, _ = _asgi(server.app, "POST", f"/api/candidates/{cid}/rename", vw_h,
                          json.dumps({"name": "改名"}).encode())
            self.assertEqual(st, 403, "viewer 不能改候选人字段")

            st, _ = _asgi(server.app, "GET", "/api/export/roster", vw_h)
            self.assertEqual(st, 403, "viewer 不能导出名单")

            st, body = _asgi(server.app, "GET", "/api/meta",
                             {"X-TP-Token": vw_tok})
            self.assertEqual(st, 200)
            meta = json.loads(body)
            for key in ("mailbox", "storage", "settings", "model"):
                self.assertNotIn(key, meta, f"viewer 的 meta 不应包含 {key}")

            # O4：文件类接口不再接受长期会话令牌放在 URL 里，改用短时票据
            st, body = _asgi(server.app, "POST", "/api/files/sign", vw_h,
                             json.dumps({"kind": "avatar", "id": cid}).encode())
            self.assertEqual(st, 200)
            sign = json.loads(body)
            self.assertIn("ticket=", sign["url"])
            # 票据本身能通过鉴权（没有头像时是 404，而不是 401）
            st, _ = _asgi(server.app, "GET", sign["url"], {})
            self.assertNotEqual(st, 401, "签名票据应通过文件鉴权")
            # 篡改票据 → 401
            st, _ = _asgi(server.app, "GET", sign["url"] + "x", {})
            self.assertEqual(st, 401, "篡改票据必须被拒")
            # 旧的 ?token=<会话令牌> 写法不再被接受
            st, _ = _asgi(server.app, "GET",
                          f"/api/candidates/{cid}/avatar?token={vw_tok}", {})
            self.assertEqual(st, 401, "URL 里不能再直接放会话令牌")

            st, body = _asgi(server.app, "GET",
                             "/api/candidates?page=1&page_size=1",
                             {"X-TP-Token": vw_tok})
            self.assertEqual(st, 200)
            _items = json.loads(body).get("items") or []
            if _items:
                self.assertIn("ticket=", _items[0].get("avatar_url") or "")

            # HR 的正常访问不能被误伤
            hr_h = {"X-TP-Token": hr_tok}
            st, _ = _asgi(server.app, "GET", "/api/export/roster", hr_h)
            self.assertEqual(st, 200)
            st, _ = _asgi(server.app, "GET", "/api/meta", hr_h)
            self.assertEqual(st, 200)
        finally:
            os.environ["TP_AUTH"] = "0"

    def test_viewer_agent_tools_are_blocked(self):
        """B03/B05 的助手入口：只读账号不能通过 chat 导出/导入。"""
        ctx = agent_tools.ToolCtx(db_path=_DB, role="viewer", cfg={"local": False})
        for name, args in (("export_roster", {}),
                           ("export_resume_bundle", {}),
                           ("ingest_resumes", {"source": "folder"}),
                           ("update_candidate_fields", {"candidate_id": 1,
                                                        "education": "本科"})):
            out = json.loads(agent_tools.execute(name, args, ctx))
            self.assertFalse(out.get("ok"), f"viewer 不应能执行 {name}：{out}")
        hr_ctx = agent_tools.ToolCtx(db_path=_DB, role="hr", cfg={"local": False})
        out = json.loads(agent_tools.execute("export_roster", {}, hr_ctx))
        self.assertNotIn("error", out, f"HR 导出不应被权限拦截：{out}")

    # ---------------------------------------------------------------- O11
    def test_default_password_forces_change(self):
        conn = db.connect(_DB)
        try:
            if not db.get_user(conn, "seed_review"):
                db.create_user(conn, "seed_review", "pw12345678", "hr",
                               "Seed Review", must_change=True)
            tok = db.create_session(conn, "seed_review", "hr")
        finally:
            conn.close()
        os.environ["TP_AUTH"] = "1"
        try:
            h = {"X-TP-Token": tok, "Content-Type": "application/json"}
            st, _ = _asgi(server.app, "GET", "/api/candidates", h)
            self.assertEqual(st, 403, "未改初始密码前业务接口必须拒绝")
            st, body = _asgi(server.app, "GET", "/api/me", h)
            self.assertEqual(st, 200)
            self.assertTrue(json.loads(body).get("must_change_password"))

            # 本人改密接口不受拦截，改完必须能正常登录并访问业务接口
            st, _ = _asgi(server.app, "POST", "/api/users/seed_review/password", h,
                          json.dumps({"old_password": "pw12345678",
                                      "new_password": "newpw12345678"}).encode())
            self.assertEqual(st, 200, "本人改密应放行")
            st, body = _asgi(server.app, "POST", "/api/login",
                             {"Content-Type": "application/json"},
                             json.dumps({"username": "seed_review",
                                         "password": "newpw12345678"}).encode())
            self.assertEqual(st, 200, "新密码应能登录")
            new_tok = json.loads(body)["token"]
            st, _ = _asgi(server.app, "GET", "/api/candidates",
                          {"X-TP-Token": new_tok})
            self.assertEqual(st, 200, "改密后业务接口应恢复")
        finally:
            os.environ["TP_AUTH"] = "0"

    # ---------------------------------------------------------------- B12
    def test_concurrent_ingest_is_rejected(self):
        tid = ingest.register_progress("并发导入测试", 0, stoppable=True,
                                       db_path=_DB)
        try:
            st, _ = _asgi(server.app, "POST", "/api/ingest",
                          {"Content-Type": "application/json"}, b"{}")
            self.assertEqual(st, 409, "已有导入任务时应拒绝新任务")
        finally:
            ingest.update_progress(tid, status="error", message="测试结束")

    # ---------------------------------------------------------------- B16
    def test_old_task_snapshots_are_cleaned(self):
        db.task_upsert(_DB, {"task_id": "old-done-task", "kind": "ingest",
                             "status": "done", "data": {"report": {"details": []}}})
        conn = db.connect(_DB)
        try:
            conn.execute(
                "UPDATE background_tasks SET finished_at='2000-01-01 00:00:00', "
                "updated_at='2000-01-01 00:00:00' WHERE task_id='old-done-task'")
            conn.commit()
        finally:
            conn.close()
        self.assertGreaterEqual(db.task_cleanup(_DB, days=30), 1)
        self.assertIsNone(db.task_get(_DB, "old-done-task"))

    def test_recent_running_task_not_marked_interrupted(self):
        db.task_upsert(_DB, {"task_id": "fresh-running", "kind": "ingest",
                             "status": "running"})
        self.assertEqual(db.task_mark_interrupted(_DB, "重启"), 0,
                         "刚更新过的任务不能被另一个实例误伤")


if __name__ == "__main__":
    unittest.main(verbosity=2)
