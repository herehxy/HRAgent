# -*- coding: utf-8 -*-
"""2026-11-23：院校学科强项（学科维度）对校实力/分析提示词的影响。"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from app import school_subjects as ss  # noqa: E402
from app.pipeline import analyze  # noqa: E402


def _jd(role: str, major: str) -> dict:
    return {"role": role,
            "must": {"major_required": [major], "education_min": "本科"}}


def _cand(major: str, school: str = "西安电子科技大学") -> dict:
    return {"name": "测试", "education": "硕士", "years": 3,
            "major": major, "school": school, "raw_text": "测试简历"}


class SchoolSubjectsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="school-subjects-")
        self._old_path = ss.CACHE_PATH
        ss.CACHE_PATH = os.path.join(self.tmp, "llm.json")
        ss._CACHE = None

    def tearDown(self):
        ss.CACHE_PATH = self._old_path
        ss._CACHE = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_xidian_comm_gets_subject_boost(self):
        a = ss.annotate("西安电子科技大学", _jd("通信算法", "通信工程"),
                        _cand("通信工程"), allow_llm=False)
        self.assertEqual(a["base_band"], "B")
        self.assertEqual(a["band"], "A")
        self.assertTrue(a["boosted"])
        self.assertIn("信息与通信工程", [m["discipline"] for m in a["matches"]])

    def test_xidian_materials_not_boosted(self):
        a = ss.annotate("西安电子科技大学", _jd("材料工艺", "材料科学与工程"),
                        _cand("材料科学与工程"), allow_llm=False)
        self.assertEqual(a["base_band"], "B")
        self.assertEqual(a["band"], "B")
        self.assertFalse(a["boosted"])
        self.assertEqual(a["matches"], [])

    def test_boost_is_capped_at_one_band(self):
        # 北邮综合第 60（C 档），信通 A+；只能抬到 B，不能直接到 A。
        a = ss.annotate("北京邮电大学", _jd("通信算法", "通信工程"),
                        _cand("通信工程", "北京邮电大学"), allow_llm=False)
        self.assertEqual(a["base_band"], "C")
        self.assertEqual(a["band"], "B")
        self.assertTrue(a["boosted"])

    def test_software_major_uses_cs_strength(self):
        a = ss.annotate("西安电子科技大学", _jd("后端开发", "软件工程"),
                        _cand("软件工程"), allow_llm=False)
        self.assertIn("计算机科学与技术",
                      [m["discipline"] for m in a["matches"]])

    def test_unrelated_job_keeps_base_band(self):
        a = ss.annotate("西安电子科技大学", _jd("法务", "法学"),
                        _cand("法学"), allow_llm=False)
        self.assertEqual(a["band"], "B")
        self.assertEqual(a["matches"], [])

    def test_non_engineering_disciplines_exist(self):
        self.assertTrue(ss.strengths_for_school("中国政法大学"))
        self.assertTrue(ss.strengths_for_school("中央财经大学"))
        self.assertTrue(ss.strengths_for_school("中央美术学院"))

    def test_manual_override_is_used(self):
        ss.put_manual("测试大学", "信息与通信工程", "A", reason="人工复核")
        rows = ss.list_cache()
        self.assertEqual(rows[0]["source"], "manual")
        a = ss.annotate("测试大学", _jd("通信", "通信工程"),
                        _cand("通信工程", "测试大学"), allow_llm=False)
        self.assertIn("信息与通信工程", [m["discipline"] for m in a["matches"]])
        ss.delete_cache(rows[0]["key"])
        self.assertEqual(ss.list_cache(), [])

    def test_llm_fill_is_cached(self):
        calls = []

        def fake(school, discipline):
            calls.append((school, discipline))
            return {"strength": "strong", "grade": "A", "confidence": 0.9,
                    "reason": "行业公认", "model": "fake"}

        old = ss._ask_llm
        ss._ask_llm = fake
        try:
            a = ss.annotate("某某行业大学", _jd("通信", "通信工程"),
                            _cand("通信工程", "某某行业大学"), allow_llm=True)
            # 该校不在综合排名里：直接用学科档 A；没有"从 B 抬到 A"的相对抬档。
            self.assertEqual(a["band"], "A")
            self.assertIn("信息与通信工程",
                          [m["discipline"] for m in a["matches"]])
            self.assertEqual(len(calls), 1)
            # 第二次不再问模型
            ss.annotate("某某行业大学", _jd("通信", "通信工程"),
                        _cand("通信工程", "某某行业大学"), allow_llm=True)
            self.assertEqual(len(calls), 1)
        finally:
            ss._ask_llm = old

    def test_job_direction_wins_over_candidate_major(self):
        # 材料岗 + 计算机背景：不能因为候选人专业命中西电的计算机强项就抬档。
        a = ss.annotate("西安电子科技大学", _jd("材料工艺", "材料科学与工程"),
                        _cand("计算机科学与技术"), allow_llm=False)
        self.assertEqual(a["band"], "B")
        self.assertEqual(a["matches"], [])

    def test_analysis_brief_contains_subject_strength(self):
        text = analyze._cand_brief(
            _cand("通信工程"), _jd("通信算法", "通信工程"), allow_llm=False)
        self.assertIn("学科强项", text)
        self.assertIn("校实力 A 档", text)

    # ------------------------------------------------ 档案逐段展示（2026-11-23 业务反馈）
    def test_row_subjects_follow_that_rows_major(self):
        # 业务方口径：每一行只显示**当前阶段专业对应**的学科标签，不做同族扩展。
        self.assertEqual(
            [r["discipline"] for r in
             ss.subjects_for_major("西安电子科技大学", "信息与通信工程")],
            ["信息与通信工程"])
        # 本科「电子信息工程」按 majors.json 归一到电子科学与技术
        self.assertEqual(
            [r["discipline"] for r in
             ss.subjects_for_major("西安电子科技大学", "电子信息工程")],
            ["电子科学与技术"])

        # 本校名单里没有这个学科 → 不显示，不按同族/大类硬凑（业务方 2026-11-25）。
        # 西电第五轮公开汇总里光学工程为 B，电子信息工程仍只对应电子科学与技术。
        self.assertEqual(
            [r["discipline"] for r in
             ss.subjects_for_major("西安电子科技大学", "光学工程")],
            ["光学工程"])
        self.assertEqual(
            [r["discipline"] for r in
             ss.subjects_for_major("西安电子科技大学", "软件工程")],
            ["软件工程"])

        # 只展示 B- 及以上：西电材料是 C+，不作为"学科强项"显示。
        self.assertEqual(
            ss.subjects_for_major("西安电子科技大学", "材料科学与工程"), [])

        # 没有专业（补出来的第一学历行）不猜
        self.assertEqual(ss.subjects_for_major("西安电子科技大学", ""), [])

    def test_edu_history_each_row_uses_its_own_major(self):
        import json
        from app import db
        raw = json.dumps([
            {"degree": "本科", "school": "西安电子科技大学", "major": "通信工程"},
            {"degree": "硕士", "school": "西安电子科技大学", "major": "光学工程"},
        ], ensure_ascii=False)
        rows = db._loads_edu_history(raw)
        self.assertEqual(len(rows), 2)
        self.assertEqual([x["discipline"] for x in rows[0]["subjects"]],
                         ["信息与通信工程"])
        self.assertEqual([x["discipline"] for x in rows[1]["subjects"]],
                         ["光学工程"])


if __name__ == "__main__":
    unittest.main()
