# -*- coding: utf-8 -*-
"""6.2.7：入库/分析并发设置与邮件报表合并的回归测试。"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from app import db, ingest  # noqa: E402


class IngestConcurrencyTest(unittest.TestCase):
    def test_concurrency_setting_is_clamped(self):
        # 不用 TemporaryDirectory：Windows 上连接池还会持有文件句柄，
        # 退出时删除会抛 WinError 32（既有环境问题，与本测试无关）。
        d = tempfile.mkdtemp(prefix="ingest_conc_")
        path = os.path.join(d, "c.db")
        conn = db.connect(path)
        try:
            self.assertEqual(ingest.ingest_concurrency(path), 1)
            db.set_setting(conn, ingest.INGEST_CONCURRENCY_KEY, 3)
            db.set_setting(conn, ingest.ANALYZE_CONCURRENCY_KEY, 9)
        finally:
            conn.close()
        self.assertEqual(ingest.ingest_concurrency(path), 3)
        self.assertEqual(ingest.analyze_concurrency(path), 6,
                         "分析并发上限 6，超过要夹住而不是照单全收")

    def test_merge_mail_report_sums_without_losing_details(self):
        dst = ingest._empty_mail_report()
        one = ingest._empty_mail_report()
        one.update({"mails": 1, "added": 1, "attachments": 2,
                    "details": [{"file": "a.pdf", "status": "added"}]})
        two = ingest._empty_mail_report()
        two.update({"mails": 1, "failed": 1, "other_failed": 1,
                    "blocked_files": ["坏成绩单.pdf"],
                    "details": [{"file": "b.pdf", "status": "failed"}]})
        ingest._merge_mail_report(dst, one)
        ingest._merge_mail_report(dst, two)
        self.assertEqual(dst["mails"], 2)
        self.assertEqual(dst["added"], 1)
        self.assertEqual(dst["failed"], 1)
        self.assertEqual(dst["other_failed"], 1)
        self.assertEqual(dst["blocked_files"], ["坏成绩单.pdf"])
        self.assertEqual(len(dst["details"]), 2)


if __name__ == "__main__":
    unittest.main()
