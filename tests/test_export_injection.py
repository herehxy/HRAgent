# -*- coding: utf-8 -*-
"""导出防公式注入：简历里的姓名/备注不能变成 Excel 公式。"""
from __future__ import annotations

import io
import os
import sys
import unittest
import zipfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from app import feedback, xlsx  # noqa: E402


class ExportFormulaInjectionTest(unittest.TestCase):
    def test_xlsx_forces_formula_like_text(self):
        data = xlsx.write_sheet(
            [['=HYPERLINK("http://evil","点我")', '+1+1', '-1+1', '@SUM(1,1)',
              '正常姓名']],
            ["姓名", "a", "b", "c", "d"])
        xml = zipfile.ZipFile(io.BytesIO(data)).read(
            "xl/worksheets/sheet1.xml").decode("utf-8")
        self.assertNotIn("<f>", xml)
        self.assertIn("inlineStr", xml)
        self.assertIn("=HYPERLINK", xml)
        self.assertIn("+1+1", xml)

    def test_csv_prefixes_formula_like_text(self):
        data = xlsx.csv_bytes(
            [["=1+1", "+1+1", "-1+1", "@SUM(1,1)", "正常", 3]],
            ["a", "b", "c", "d", "e", "f"])
        text = data.decode("utf-8-sig")
        row = text.splitlines()[1]
        for token in ("'=1+1", "'+1+1", "'-1+1", "'@SUM(1,1)"):
            self.assertIn(token, row)
        self.assertIn("正常", row)
        self.assertIn(",3", row)

    def test_feedback_csv_sanitizer(self):
        self.assertEqual(feedback._csv_text("=1+1"), "'=1+1")
        self.assertEqual(feedback._csv_text("+1"), "'+1")
        self.assertEqual(feedback._csv_text("-1"), "'-1")
        self.assertEqual(feedback._csv_text("@x"), "'@x")
        self.assertEqual(feedback._csv_text("正常"), "正常")
        self.assertEqual(feedback._csv_text(None), "")
        self.assertEqual(feedback._csv_text(3), 3)


if __name__ == "__main__":
    unittest.main()
