"""OCR 内嵌模块的回归测试（不依赖 HRAgent 数据库/模型）。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from app.pipeline import ocr
from app.pipeline.ocr.models import OcrLine
from app.pipeline.ocr.reading_order import build_blocks


class ReadingOrderTest(unittest.TestCase):
    def test_two_columns_stay_separate(self):
        lines = [
            OcrLine("A", [0, 0, 100, 10], 0.9),
            OcrLine("B", [0, 14, 100, 24], 0.9),
            OcrLine("C", [200, 0, 300, 10], 0.9),
            OcrLine("D", [200, 14, 300, 24], 0.9),
        ]
        blocks = build_blocks(lines)
        self.assertEqual([b.text for b in blocks], ["A\nB", "C\nD"])
        self.assertTrue(all(b.bbox for b in blocks))

    def test_no_bbox_keeps_engine_order(self):
        lines = [OcrLine("first"), OcrLine("second")]
        blocks = build_blocks(lines)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].text, "first\nsecond")


class OcrContractTest(unittest.TestCase):
    def test_profile_and_capabilities(self):
        self.assertEqual(ocr.get_profile("resume@1").dpi, 200)
        self.assertEqual(ocr.get_profile("unknown").name, "resume@1")
        caps = ocr.capabilities()
        self.assertIn("engines", caps)
        self.assertIn("usable", caps)
        self.assertIn("best", caps)

    def test_document_to_dict_is_json_serializable(self):
        doc = ocr.recognize_file(self._pdf("Hello Resume\nEducation"), "resume@1")
        data = doc.to_dict()
        self.assertEqual(data["schema_version"], "ocr@1")
        self.assertEqual(data["profile"], "resume@1")
        json.dumps(data, ensure_ascii=False)

    def _pdf(self, text: str) -> str:
        try:
            import pymupdf
        except Exception as exc:                             # noqa: BLE001
            self.skipTest(f"pymupdf 不可用：{exc}")
        path = os.path.join(self._tmp.name, "resume.pdf")
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((72, 72), text.split("\n")[0])
        for i, line in enumerate(text.split("\n")[1:], start=1):
            page.insert_text((72, 72 + i * 24), line)
        doc.save(path)
        doc.close()
        return path

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self._tmp.cleanup()

    def test_pdf_text_layer_keeps_bbox(self):
        doc = ocr.recognize_file(self._pdf("Hello Resume\nEducation"), "resume@1")
        self.assertEqual(doc.engine, "pymupdf")
        self.assertIn("Hello Resume", doc.text)
        self.assertGreaterEqual(len(doc.pages), 1)
        block = doc.pages[0].blocks[0]
        self.assertEqual(len(block.bbox), 4)
        self.assertTrue(block.lines)

    def test_pdf_bytes(self):
        path = self._pdf("Hello Resume")
        with open(path, "rb") as fh:
            data = fh.read()
        doc = ocr.recognize_bytes(data, filename="resume.pdf")
        self.assertIn("Hello Resume", doc.text)


class ImageOcrTest(unittest.TestCase):
    def test_image_keeps_lines_and_confidence(self):
        if not ocr.available_engines():
            self.skipTest("没有可用 OCR 引擎")
        try:
            from PIL import Image, ImageDraw, ImageFont
            font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 64)
        except Exception as exc:                             # noqa: BLE001
            self.skipTest(f"测试字体不可用：{exc}")
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "resume.png")
            img = Image.new("RGB", (1000, 300), "white")
            draw = ImageDraw.Draw(img)
            draw.text((60, 80), "HELLO RESUME", fill="black", font=font)
            img.save(path)
            doc = ocr.recognize_file(path)
            self.assertIn("HELLO", doc.text.upper())
            self.assertTrue(doc.pages[0].blocks)
            self.assertTrue(any(line.bbox for block in doc.pages[0].blocks
                                for line in block.lines))
            self.assertTrue(any(line.confidence is not None
                                for block in doc.pages[0].blocks
                                for line in block.lines))


if __name__ == "__main__":
    unittest.main()
