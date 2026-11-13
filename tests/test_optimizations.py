"""针对 2026-11-13 优化的轻量回归测试（只用标准库，CI 可直接跑）。

覆盖：任务持久化、schema 版本、简历优先的标题策略、学历归一、
工具参数纠偏、UI 资源拆分，以及任务级模型配置。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class OptimizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="hragent-test-")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_schema_and_background_task_persistence(self):
        from app import db

        path = os.path.join(self.tmp, "tasks.db")
        conn = db.connect(path)
        info = db.schema_info(conn)
        conn.close()
        self.assertEqual(info["schema_version"], db.SCHEMA_VERSION)
        self.assertGreaterEqual(info["current_schema"], 1)

        db.task_upsert(path, {
            "task_id": "t1", "kind": "ingest", "title": "收取邮箱",
            "total": 3, "done": 1, "status": "running",
            "started_at": time.time(), "meta": {"phase": "fetch"},
            "data": {"report": {"added": 1}},
        })
        task = db.task_get(path, "t1")
        self.assertEqual(task["status"], "running")
        self.assertEqual(task["meta"]["phase"], "fetch")
        self.assertEqual(task["report"]["added"], 1)
        self.assertTrue(db.task_list(path, kind="ingest"))

        # v6.0.0：启动标记只处理**长时间没有更新**的 running 任务，
        # 避免多实例共库时误伤另一个实例正在跑的任务。
        self.assertEqual(db.task_mark_interrupted(path, "重启"), 0)
        self.assertEqual(db.task_get(path, "t1")["status"], "running")
        conn = db.connect(path)
        conn.execute("UPDATE background_tasks SET updated_at='2000-01-01 00:00:00' "
                     "WHERE task_id='t1'")
        conn.commit()
        conn.close()
        self.assertEqual(db.task_mark_interrupted(path, "重启"), 1)
        self.assertEqual(db.task_get(path, "t1")["status"], "error")

    def test_subject_metadata_never_overrides_resume(self):
        from app.pipeline import subject_meta as sm

        meta = sm.parse("工艺技术-硕士-西安交通大学-材料科学与工程-张三-男")
        out, used = sm.apply_to_candidate(
            {"name": "李四", "education": "本科", "school": "某学院"}, meta,
            raw_text="李四 本科 某学院")
        self.assertEqual((out["name"], out["education"], out["school"]),
                         ("李四", "本科", "某学院"))
        self.assertEqual(out["major"], "材料科学与工程")
        self.assertEqual(out["gender"], "男")
        self.assertEqual(set(used), {"专业", "性别"})
        self.assertTrue(out.get("title_override_notes"))

        blank, used2 = sm.apply_to_candidate({}, meta, raw_text="")
        self.assertEqual(blank["name"], "张三")
        self.assertEqual(blank["education"], "硕士")
        self.assertEqual(set(used2), {"姓名", "学历", "学校", "专业", "性别"})

    def test_degree_normalization_and_evidence(self):
        from app.pipeline.extract import normalize_degree, verify_profile_against_text

        self.assertEqual(normalize_degree("硕士研究生"), "硕士")
        self.assertEqual(normalize_degree("博士研究生"), "博士")
        self.assertEqual(normalize_degree("工学学士"), "本科")
        self.assertEqual(normalize_degree("专科"), "大专")
        self.assertIsNone(normalize_degree("高中"))

        ok, bad = verify_profile_against_text(
            {"education": "硕士"}, {"education": "研究生"},
            "西安交通大学 研究生 材料学")
        self.assertEqual(ok.get("education"), "硕士")
        self.assertNotIn("education", bad)

    def test_tool_argument_coercion_and_rejection(self):
        from app.agent.tool_args import validate_tool_args

        spec = {"function": {"parameters": {"properties": {
            "n": {"type": "integer"}, "flag": {"type": "boolean"},
            "tags": {"type": "array"},
            "mode": {"type": "string", "enum": ["a", "b"]},
        }}}}
        args, errors = validate_tool_args(
            spec, {"n": "3", "flag": "true", "tags": "x，y", "mode": "a"})
        self.assertFalse(errors)
        self.assertEqual(args, {"n": 3, "flag": True, "tags": ["x", "y"], "mode": "a"})

        args, errors = validate_tool_args(spec, {"n": "x", "mode": "z"})
        self.assertTrue(errors)

    def test_ui_assets_split_and_staff_removed(self):
        from app import ui_v2

        html = ui_v2.render_page_v2(auth_enabled=False)
        self.assertNotIn('data-view="staff"', html)
        self.assertNotIn('id="staffView"', html)
        self.assertNotIn("__V2_CSS_BLOCK__", html)
        self.assertNotIn("__V2_JS_BLOCK__", html)
        self.assertTrue(ui_v2._ASSETS)
        self.assertGreater(len(ui_v2._JS), 10000)
        # 页面刷新后恢复“收取中”按钮状态（本轮修复的回归锁）。
        self.assertIn("restoreIngestState", ui_v2._JS)

        node = shutil.which("node")
        if node:
            path = os.path.join(self.tmp, "ui.js")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(ui_v2._JS)
            proc = subprocess.run([node, "--check", path],
                                  capture_output=True, text=True)
            self.assertEqual(proc.returncode, 0, proc.stderr[:300])

    def test_task_level_model_config(self):
        from app.agent import llm

        old_cfg, old_sec = llm.CONFIG_PATH, llm.SECRETS_PATH
        old_env = {k: os.environ.get(k) for k in
                   ("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY")}
        cfg_path = os.path.join(self.tmp, "model.json")
        with open(cfg_path, "w", encoding="utf-8") as fh:
            json.dump({
                "model": "chat-model", "base_url": "http://chat/v1", "api_key": "k",
                "tasks": {"extract": {"model": "extract-model",
                                      "base_url": "http://extract/v1"}},
                "extract_timeout": 42,
            }, fh)
        try:
            llm.CONFIG_PATH = cfg_path
            llm.SECRETS_PATH = os.path.join(self.tmp, "no-secrets.json")
            for k in old_env:
                os.environ.pop(k, None)
            chat = llm.load_cfg()
            extract = llm.load_cfg("extract")
            self.assertEqual(chat["model"], "chat-model")
            self.assertEqual(extract["model"], "extract-model")
            self.assertEqual(extract["base_url"], "http://extract/v1")
            self.assertEqual(extract["timeout"], 42)
        finally:
            llm.CONFIG_PATH, llm.SECRETS_PATH = old_cfg, old_sec
            for k, v in old_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


if __name__ == "__main__":
    unittest.main(verbosity=2)
