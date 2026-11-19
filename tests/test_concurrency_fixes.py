"""并发/覆盖写的回归：阶段条件更新、备注不被档位写回覆盖。"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest

from app import db


def _close_pool(path: str) -> None:
    pool = getattr(getattr(db, "_db_pool_local", None), "by_key", None)
    if not pool:
        return
    pooled = pool.pop((os.path.abspath(path), True), None)
    if pooled is not None:
        sqlite3.Connection.close(pooled)


class ConcurrencyFixTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="concurrency_test_")
        self.path = os.path.join(self.tmp, "db.sqlite")
        self.conn = db.connect(self.path)
        self.cid = db.insert_candidate(self.conn, {
            "identity_key": "test:concurrency", "name": "并发测试"})
        self.aid = db.insert_application(self.conn, {
            "candidate_id": self.cid, "stage": "新投递"})

    def tearDown(self) -> None:
        self.conn.close()
        _close_pool(self.path)
        try:
            os.remove(self.path)
            os.rmdir(self.tmp)
        except OSError:
            pass

    def test_stage_expected_mismatch_does_not_write(self):
        with self.assertRaises(ValueError):
            db.set_application_stage(self.conn, self.aid, "初筛通过",
                                     expected_stage="已面试")
        self.assertEqual(db.get_application(self.conn, self.aid)["stage"], "新投递")
        db.set_application_stage(self.conn, self.aid, "初筛通过",
                                 expected_stage="新投递")
        self.assertEqual(db.get_application(self.conn, self.aid)["stage"], "初筛通过")

    def test_tier_without_note_keeps_existing_note(self):
        db.add_note(self.conn, self.aid, "HR 手写备注", "HR", "hr")
        db.set_application_tier(self.conn, self.aid, "B")
        app = db.get_application(self.conn, self.aid)
        self.assertEqual(app["note"], "HR 手写备注")
        self.assertEqual(app["tier_final"], "B")


if __name__ == "__main__":
    unittest.main()
