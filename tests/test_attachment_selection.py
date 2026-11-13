"""附件内容判定与图片 OCR 回归测试。"""
from __future__ import annotations

import unittest
from unittest import mock

from app import ingest
from app import mailbox
from app.pipeline import parse as parse_mod


def _att(name, text="", data=b"", ok=True):
    return {
        "filename": name,
        "data": data or text.encode("utf-8") or b"x",
        "mime": "",
        "_text": text,
        "_engine": "ocr" if text else "ocr_empty",
        "_ok": ok,
    }


class ResumeSelectionTest(unittest.TestCase):
    def test_pdf_resume_beats_id_card_image(self):
        atts = [
            _att("5768FE3B_35CA764C.FA9BE56A00000000.png", "", ok=False,
                 data=b"p" * 608451),
            _att("CD389237_C0DE5705.FA9BE56A00000000.png",
                 "姓名 淡永康\n民族 汉\n住址 陕西省西安市\n"
                 "公民身份号码 610101199001011234\n签发机关 西安市公安局",
                 data=b"i" * 847279),
            _att("技术研发岗-硕士-西安科技大学-电子信息-淡永康-男.pdf",
                 "姓名：淡永康\n电话：13800138000\n邮箱：dan@example.com\n"
                 "教育经历\n2018-2022 西安科技大学 电子信息 本科\n"
                 "工作经历\n2022-2024 某公司 工程师\n项目经历\n" + "x" * 500,
                 data=b"d" * 118181),
        ]
        self.assertEqual(mailbox.choose_resume_attachment(atts), 2)

    def test_parsed_image_resume_beats_empty_pdf(self):
        atts = [
            _att("resume.pdf", "", ok=False, data=b"p" * 1000),
            _att("IMG_0001.png",
                 "姓名：张三\n电话：13800138000\n教育经历\n西安科技大学\n工作经历\n工程师",
                 data=b"i" * 2000),
        ]
        self.assertEqual(mailbox.choose_resume_attachment(atts), 1)

    def test_id_card_score_below_threshold(self):
        score = mailbox.attachment_resume_score(
            "CD389237.png",
            "姓名 李四\n性别 男\n民族 汉\n住址 北京市\n"
            "公民身份号码 110101199203041245\n签发机关 公安局")
        self.assertLess(score, mailbox.RESUME_SCORE_MIN)

    def test_single_attachment_is_chosen(self):
        atts = [_att("anything.png", "身份证 公民身份号码", data=b"x")]
        self.assertEqual(mailbox.choose_resume_attachment(atts), 0)

    def test_prepare_attachments_parses_text_and_marks_ok(self):
        atts = [{"filename": "a.txt", "mime": "text/plain",
                 "data": "姓名：张三\n教育经历\n".encode("utf-8")}]
        out = ingest._prepare_attachments(atts)
        self.assertEqual(len(out), 1)
        self.assertIn("教育经历", out[0]["_text"])
        self.assertTrue(out[0]["_ok"])

    def test_empty_ocr_is_not_success(self):
        with mock.patch.object(parse_mod, "_parse_image_ocr", return_value=""):
            text, engine, ok = parse_mod.parse_file_ex("empty.png")
        self.assertEqual(text, "")
        self.assertFalse(ok)
        self.assertEqual(engine, "ocr_empty")


if __name__ == "__main__":
    unittest.main()
