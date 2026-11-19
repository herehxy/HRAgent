# -*- coding: utf-8 -*-
"""6.2.2：教育经历起止时间由模型抽取、规整并校验。"""
from __future__ import annotations

import os
import sys
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from app.pipeline.extract import _edu_history_from_llm, _norm_edu_date  # noqa: E402


class EduPeriodExtractTest(unittest.TestCase):
    def test_norm_edu_date(self):
        self.assertEqual(_norm_edu_date("2016年9月"), "2016.09")
        self.assertEqual(_norm_edu_date("2020/06"), "2020.06")
        self.assertEqual(_norm_edu_date("2023-6"), "2023.06")
        self.assertEqual(_norm_edu_date("现在"), "至今")
        self.assertEqual(_norm_edu_date("没写"), "")

    def test_llm_periods_are_normalized_and_verified(self):
        text = ("教育经历\n"
                "2016.09-2020.06  西安建筑科技大学  材料成型及控制工程  本科\n"
                "2020.09-2023.06  西安交通大学  材料科学与工程  硕士\n"
                "2023.09-至今      西安交通大学  材料科学与工程  博士")
        raw = [
            {"degree": "本科", "school": "西安建筑科技大学", "major": "材料成型",
             "start": "2016年9月", "end": "2020/06"},
            {"degree": "硕士", "school": "西安交通大学", "major": "材料科学与工程",
             "start": "2020.09", "end": "2023.06"},
            {"degree": "博士", "school": "西安交通大学", "major": "材料科学与工程",
             "start": "2023.09", "end": "现在"},
            # 年份不在原文：模型编的时间必须被清掉
            {"degree": "博士", "school": "西安交通大学", "major": "材料科学与工程",
             "start": "2024.09", "end": "2027.06"},
        ]
        out = _edu_history_from_llm(raw, text)
        by_degree = {x["degree"]: x for x in out}
        self.assertEqual(by_degree["本科"]["start"], "2016.09")
        self.assertEqual(by_degree["本科"]["end"], "2020.06")
        self.assertEqual(by_degree["硕士"]["start"], "2020.09")
        self.assertEqual(by_degree["博士"]["end"], "至今")
        # 同一学历同一学校去重：第一条博士保留，伪造年份的那条被丢掉
        self.assertEqual(len(out), 3)

    def test_invented_period_is_cleared(self):
        text = "2016.09-2020.06 西安建筑科技大学 本科"
        out = _edu_history_from_llm(
            [{"degree": "本科", "school": "西安建筑科技大学",
              "start": "2014.09", "end": "2018.06"}], text)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["start"], "")
        self.assertEqual(out[0]["end"], "")


if __name__ == "__main__":
    unittest.main()
