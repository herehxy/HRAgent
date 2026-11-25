# -*- coding: utf-8 -*-
"""v6.3.8：教育断档 / 在读期间工作的时间线规则。"""
from __future__ import annotations

import os
import sys
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from app import timeline  # noqa: E402


class TimelineTest(unittest.TestCase):
    def test_education_gap_over_one_year(self):
        edu = [
            {"degree": "本科", "school": "A大学", "start": "2016.09", "end": "2020.06"},
            {"degree": "硕士", "school": "B大学", "start": "2021.09", "end": "2024.06"},
        ]
        issues = timeline.analyze_timeline(edu, [])
        gaps = [i for i in issues if i["kind"] == "edu_gap"]
        self.assertEqual(len(gaps), 1)
        self.assertIn("1 年 3 个月", gaps[0]["text"])
        self.assertIn("2020.06", gaps[0]["text"])
        self.assertIn("2021.09", gaps[0]["text"])

    def test_normal_summer_break_is_not_a_gap(self):
        edu = [
            {"degree": "本科", "school": "A大学", "start": "2016.09", "end": "2020.06"},
            {"degree": "硕士", "school": "B大学", "start": "2020.09", "end": "2023.06"},
        ]
        self.assertEqual(
            [i for i in timeline.analyze_timeline(edu, []) if i["kind"] == "edu_gap"], [])

    def test_overlap_is_not_reported_as_gap(self):
        edu = [
            {"degree": "本科", "school": "A大学", "start": "2016.09", "end": "2020.06"},
            {"degree": "硕士", "school": "B大学", "start": "2019.09", "end": "2022.06"},
        ]
        self.assertEqual(
            [i for i in timeline.analyze_timeline(edu, []) if i["kind"] == "edu_gap"], [])

    def test_work_during_study(self):
        edu = [{"degree": "硕士", "school": "B大学",
                "start": "2023.09", "end": "2026.06"}]
        work = [{"org": "测试公司", "title": "工程师",
                 "start": "2024.03", "end": "2025.12"}]
        issues = timeline.analyze_timeline(edu, work)
        hits = [i for i in issues if i["kind"] == "work_during_study"]
        self.assertEqual(len(hits), 1)
        self.assertIn("测试公司", hits[0]["text"])
        self.assertIn("硕士 B大学", hits[0]["text"])

    def test_gap_mentions_work_during_gap(self):
        edu = [
            {"degree": "本科", "school": "A大学", "start": "2016.09", "end": "2020.06"},
            {"degree": "硕士", "school": "B大学", "start": "2022.09", "end": "2025.06"},
        ]
        work = [{"org": "某公司", "start": "2021.01", "end": "2022.08"}]
        issues = timeline.analyze_timeline(edu, work)
        gap = [i for i in issues if i["kind"] == "edu_gap"][0]
        self.assertIn("期间有工作经历：某公司", gap["text"])

    def test_internship_does_not_trigger_work_during_study(self):
        edu = [{"degree": "硕士", "school": "B大学",
                "start": "2023.09", "end": "2026.06"}]
        work = [{"org": "中国空间技术研究院遥感卫星总体部", "title": "暑期实习生",
                 "start": "2026.08", "end": "2026.08"}]
        issues = timeline.analyze_timeline(edu, work)
        self.assertEqual([i for i in issues if i["kind"] == "work_during_study"], [])

    def test_formal_work_still_triggers(self):
        edu = [{"degree": "硕士", "school": "B大学",
                "start": "2023.09", "end": "2026.06"}]
        work = [{"org": "某科技有限公司", "title": "数据工程师",
                 "start": "2024.03", "end": "2025.12"}]
        issues = timeline.analyze_timeline(edu, work)
        self.assertEqual(len([i for i in issues if i["kind"] == "work_during_study"]), 1)

    def test_unclear_title_is_still_flagged(self):
        """看不出是实习的，按无法区分处理，仍然提示。"""
        edu = [{"degree": "硕士", "school": "B大学",
                "start": "2023.09", "end": "2026.06"}]
        work = [{"org": "某单位", "title": "", "start": "2024.03", "end": "2024.09"}]
        issues = timeline.analyze_timeline(edu, work)
        self.assertEqual(len([i for i in issues if i["kind"] == "work_during_study"]), 1)

    def test_gap_work_mention_ignores_internship(self):
        edu = [
            {"degree": "本科", "school": "A大学", "start": "2016.09", "end": "2020.06"},
            {"degree": "硕士", "school": "B大学", "start": "2022.09", "end": "2025.06"},
        ]
        work = [{"org": "某公司", "title": "暑期实习生",
                 "start": "2021.01", "end": "2022.08"}]
        gap = [i for i in timeline.analyze_timeline(edu, work)
               if i["kind"] == "edu_gap"][0]
        self.assertNotIn("期间有工作经历", gap["text"])

    def test_llm_kind_overrides_keywords(self):
        """模型 kind 优先：标题里有“实习”但模型判定正式 → 仍提示。"""
        edu = [{"degree": "硕士", "school": "B大学",
                "start": "2023.09", "end": "2026.06"}]
        work = [{"org": "某公司", "title": "实习工程师", "kind": "正式",
                 "start": "2024.03", "end": "2025.12"}]
        issues = timeline.analyze_timeline(edu, work)
        self.assertEqual(len([i for i in issues if i["kind"] == "work_during_study"]), 1)

    def test_llm_kind_internship_skips_even_without_keyword(self):
        edu = [{"degree": "硕士", "school": "B大学",
                "start": "2023.09", "end": "2026.06"}]
        work = [{"org": "某公司", "title": "助理研究员", "kind": "实习",
                 "start": "2024.03", "end": "2024.09"}]
        issues = timeline.analyze_timeline(edu, work)
        self.assertEqual([i for i in issues if i["kind"] == "work_during_study"], [])

    def test_unknown_nature_is_flagged_with_review_hint(self):
        edu = [{"degree": "硕士", "school": "B大学",
                "start": "2023.09", "end": "2026.06"}]
        work = [{"org": "某单位", "title": "", "kind": "不明",
                 "start": "2024.03", "end": "2024.09"}]
        hits = [i for i in timeline.analyze_timeline(edu, work)
                if i["kind"] == "work_during_study"]
        self.assertEqual(len(hits), 1)
        self.assertIn("工作性质无法确定", hits[0]["text"])
        self.assertIn("建议打开档案", hits[0]["text"])

    def test_missing_dates_are_skipped(self):
        edu = [
            {"degree": "本科", "school": "A大学", "start": "", "end": "2020.06"},
            {"degree": "硕士", "school": "B大学", "start": "2021.09", "end": "2024.06"},
        ]
        self.assertEqual(
            [i for i in timeline.analyze_timeline(edu, []) if i["kind"] == "edu_gap"], [])


if __name__ == "__main__":
    unittest.main()
