"""入职材料 OCR 字段抽取回归测试（不依赖 OCR 引擎）。"""
from __future__ import annotations

import unittest

from app import employee_ocr


class EmployeeOcrTest(unittest.TestCase):
    def test_onboarding_form_fields(self):
        text = """入职登记表
姓名：张三
性别：男
手机：13800138000
邮箱：zs@example.com
学历：硕士
毕业院校：西安交通大学
专业：材料科学与工程
单位：钛所
部门：材料
岗位：工程师
入职日期：2026-11-10"""
        fields = employee_ocr.extract_fields("onboarding_form", text)["fields"]
        self.assertEqual(fields["name"]["value"], "张三")
        self.assertEqual(fields["phone"]["value"], "13800138000")
        self.assertEqual(fields["edu_level"]["value"], "硕士")
        self.assertEqual(fields["school"]["value"], "西安交通大学")
        self.assertEqual(fields["onboard_date"]["value"], "2026-11-10")
        self.assertIn("姓名", fields["name"]["evidence"])

    def test_id_card_derives_birth_and_gender(self):
        text = "姓名 李四\n公民身份号码 110101199203041245\n住址 北京市海淀区"
        fields = employee_ocr.extract_fields("id_card", text)["fields"]
        self.assertEqual(fields["id_card"]["value"], "110101199203041245")
        self.assertEqual(fields["birth_date"]["value"], "1992-03-04")
        self.assertEqual(fields["gender"]["value"], "女")

    def test_pdf_table_labels_and_values_on_separate_lines(self):
        text = """姓名
张明远
性别
男
学历/ 学
位
本科/ 学士
毕业院校
南京大学
联系电话
138-0013-8000
合同期限
3 年（2026-11-16 至2029-
11-15）"""
        fields = employee_ocr.extract_fields("onboarding_form", text)["fields"]
        self.assertEqual(fields["name"]["value"], "张明远")
        self.assertEqual(fields["phone"]["value"], "13800138000")
        self.assertEqual(fields["edu_level"]["value"], "本科")
        self.assertEqual(fields["school"]["value"], "南京大学")
        self.assertEqual(fields["contract_end"]["value"], "2029-11-15")

    def test_prose_does_not_become_fields(self):
        text = "我院招聘岗位，经双方洽谈，我院同意录用该学生，请贵单位协助办理。"
        fields = employee_ocr.extract_fields("other", text)["fields"]
        self.assertNotIn("unit", fields)
        self.assertNotIn("edu_level", fields)


if __name__ == "__main__":
    unittest.main()
