"""文档 Profile / 通用抽取器测试。"""
from __future__ import annotations

import unittest

from app.documents import extract_document, load_profiles, resolve_profile
from app.documents import adapters


class ProfileTest(unittest.TestCase):
    def test_profiles_load(self):
        profiles = load_profiles()
        self.assertIn("onboarding_form@1", profiles)
        self.assertIn("id_card@1", profiles)
        self.assertIn("education_cert@1", profiles)
        self.assertIn("contract@1", profiles)
        p = resolve_profile("onboarding_form")
        self.assertEqual(p.doc_type, "onboarding_form")
        self.assertIn("education_json", p.to_ui()["array_defs"])

    def test_type_specific_extraction(self):
        # 身份证不应把证件上的文字误扩展成员工单位/教育字段
        idr = extract_document("id_card", "姓名 李四\n住址 北京市海淀区\n"
                               "公民身份号码 110101199203041245")
        self.assertIn("id_card", idr["fields"])
        self.assertNotIn("unit", idr["fields"])
        self.assertEqual(idr["fields"]["gender"]["value"], "女")

        edu = extract_document("education_cert",
                               "姓名：李四\n毕业院校：北京大学\n专业：计算机\n"
                               "学历：硕士\n毕业时间：2020-06")
        self.assertIn("school", edu["fields"])
        self.assertNotIn("phone", edu["fields"])

        con = extract_document("contract",
                               "甲方：某公司\n乙方：李四\n合同编号：HT-001\n"
                               "岗位：工程师")
        self.assertEqual(con["fields"]["contract_no"]["value"], "HT-001")
        self.assertEqual(con["fields"]["unit"]["value"], "某公司")

    def test_adapter_filters_employee_fields(self):
        payload = adapters.employee_payload({
            "name": "张三", "phone": "13800138000",
            "education_json": [{"school": "X大学"}],
            "not_an_employee_field": "x",
        })
        self.assertIn("name", payload)
        self.assertIn("phone", payload)
        self.assertIn("education_json", payload)
        self.assertNotIn("not_an_employee_field", payload)


if __name__ == "__main__":
    unittest.main()
