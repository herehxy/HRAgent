"""在职人员档案的独立回归测试（不启动 HTTP 服务）。"""
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest

from openpyxl import Workbook

from app import db, staff


class StaffTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.conn = db.connect(os.path.join(self.tmp.name, "staff.db"))

    def tearDown(self):
        self.conn.close()
        self.tmp.cleanup()

    def _application(self):
        c = self.conn
        c.execute("""INSERT INTO candidates
            (identity_key,name,gender,edu_level,school,major,created_at,updated_at)
            VALUES ('k1','张三','男','硕士','X大学','材料','2026-11-10','2026-11-10')""")
        cid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.execute("""INSERT INTO jobs (title,dept,created_at,updated_at)
                     VALUES ('工艺工程师','钛所','2026-11-10','2026-11-10')""")
        jid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.execute("""INSERT INTO applications
            (candidate_id,job_id,stage,created_at,updated_at)
            VALUES (?,?,'已发放offer','2026-11-10','2026-11-10')""", (cid, jid))
        aid = c.execute("SELECT last_insert_rowid()").fetchone()[0]
        c.commit()
        return cid, aid

    def test_transfer_is_idempotent_and_links_candidate(self):
        cid, aid = self._application()
        first = staff.ensure_employee_from_application(self.conn, aid, "hr", "hr")
        second = staff.ensure_employee_from_application(self.conn, aid, "hr", "hr")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["unit"], "钛所")
        self.assertEqual(first["position"], "工艺工程师")
        row = self.conn.execute("SELECT pool_status FROM candidates WHERE id=?",
                                (cid,)).fetchone()
        self.assertEqual(row["pool_status"], "已入职")

    def test_create_masks_sensitive_fields_and_lists(self):
        item = staff.create_employee(self.conn, {
            "name": "李四", "phone": "13800138000", "email": "lisi@example.com",
            "id_card": "110101199001011234", "employment_status": "试用期",
            "onboard_date": "2026-11-10",
        }, "hr", "hr")
        self.assertNotIn("phone_enc", item)
        self.assertIn("*", item["phone"])
        raw = self.conn.execute("SELECT phone_enc,id_card_enc FROM employees WHERE id=?",
                                (item["id"],)).fetchone()
        self.assertNotEqual(raw["phone_enc"], "13800138000")
        self.assertNotEqual(raw["id_card_enc"], "110101199001011234")
        res = staff.list_employees(self.conn)
        self.assertEqual(res["total"], 1)
        self.assertEqual(res["items"][0]["documents_count"], 0)

    def test_import_preview_and_commit(self):
        wb = Workbook()
        ws = wb.active
        ws.append(["姓名", "手机", "单位", "部门", "岗位", "人员状态", "入职日期"])
        ws.append(["王五", "13900139000", "钛所", "材料", "工程师", "试用期", "2026-11-10"])
        ws.append(["", "13800138000", "", "", "", "", ""])
        buf = io.BytesIO()
        wb.save(buf)
        parsed = staff.parse_import_xlsx(buf.getvalue())
        self.assertEqual(parsed["total"], 2)
        self.assertEqual(parsed["valid"], 1)
        staff.annotate_import_duplicates(self.conn, parsed)
        out = staff.commit_import(self.conn, parsed["rows"], "hr", "hr")
        self.assertEqual(out["created"], 1)
        self.assertEqual(out["skipped"], 1)
        self.assertEqual(staff.list_employees(self.conn)["total"], 1)

    def test_dates_are_normalized_on_import(self):
        wb = Workbook()
        ws = wb.active
        ws.append(["姓名", "入职日期", "出生日期"])
        ws.append(["日期甲", "2026/11/10", "1990年1月2日"])
        ws.append(["日期乙", "2026-13-40", "不是日期"])
        buf = io.BytesIO()
        wb.save(buf)
        parsed = staff.parse_import_xlsx(buf.getvalue())
        self.assertEqual(parsed["rows"][0]["onboard_date"], "2026-11-10")
        self.assertEqual(parsed["rows"][0]["birth_date"], "1990-01-02")
        self.assertTrue(parsed["rows"][1]["_errors"])

    def test_full_profile_arrays_and_avatar(self):
        item = staff.create_employee(self.conn, {
            "name": "周八", "former_name": "周小八",
            "ethnicity": "汉", "political_status": "群众",
            "education_json": [{"school": "X大学", "degree": "本科"}],
            "work_history_json": [{"unit": "Y公司", "position": "工程师"}],
            "family_json": [{"name": "周父", "relation": "父亲"}],
            "certificates_json": [{"name": "PMP"}],
            "avatar_path": "/tmp/avatar.jpg",
        }, "hr", "hr")
        self.assertEqual(item["former_name"], "周小八")
        self.assertEqual(item["education_json"][0]["school"], "X大学")
        self.assertEqual(item["work_history_json"][0]["unit"], "Y公司")
        self.assertEqual(item["family_json"][0]["relation"], "父亲")
        self.assertEqual(item["certificates_json"][0]["name"], "PMP")
        self.assertEqual(item["avatar_path"], "/tmp/avatar.jpg")

    def test_update_status_and_audit_do_not_leak_plain_phone(self):
        item = staff.create_employee(self.conn, {
            "name": "赵六", "phone": "13600136000", "employment_status": "试用期",
        }, "hr", "hr")
        staff.update_employee(self.conn, item["id"], {
            "employment_status": "离职", "phone": "13600136000",
        }, "hr", "hr")
        logs = self.conn.execute(
            "SELECT before,after FROM audit_log WHERE entity='employee' AND entity_id=?",
            (str(item["id"]),)).fetchall()
        text = json.dumps([dict(r) for r in logs], ensure_ascii=False)
        self.assertNotIn("13600136000", text)
        self.assertIn("136****6000", text)


if __name__ == "__main__":
    unittest.main()
