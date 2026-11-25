"""上下文治理与运行指标的回归测试（不依赖模型、不连真实库）。"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

from app import db
from app.agent import context_governance as cg


class ContextGovernanceTest(unittest.TestCase):
    def test_resolve_budget_reduces_tools_for_small_window(self):
        small = cg.resolve_budget(32_768, max_output_tokens=4_096)
        large = cg.resolve_budget(1_000_000, max_output_tokens=4_096)
        self.assertEqual(small["max_tools"], 12)
        self.assertEqual(large["max_tools"], 30)
        self.assertLess(small["budget"], large["budget"])
        self.assertGreaterEqual(small["budget"], 2_048)

    def test_missing_window_uses_conservative_default_not_one_million(self):
        b = cg.resolve_budget(None, max_output_tokens=4_096)
        self.assertEqual(b["window"], cg.DEFAULT_CONTEXT_WINDOW)
        self.assertEqual(cg.DEFAULT_CONTEXT_WINDOW, 131_072)

    def test_model_window_falls_back_by_model_default(self):
        from app.agent import llm
        self.assertEqual(llm.model_default_context_window("qwen2.5-7b"), 32_768)
        self.assertEqual(llm.model_default_context_window("deepseek-chat"), 128_000)
        self.assertIsNone(llm.model_default_context_window("totally-unknown"))
        base = {"base_url": "", "api_key": ""}
        win, src = llm.model_context_window(dict(base, model="totally-unknown"))
        self.assertEqual((win, src), (cg.DEFAULT_CONTEXT_WINDOW, "fallback"))
        win, src = llm.model_context_window(dict(base, model="mimo-v2.6-pro"))
        self.assertEqual((win, src), (1_048_576, "model"))
        win, src = llm.model_context_window(dict(base, model="mimo-7b"))
        self.assertEqual((win, src), (32_768, "model"))
        # config/model.json 的人工确认值永远优先
        win, src = llm.model_context_window(
            dict(base, model="x", context_window="256k"))
        self.assertEqual((win, src), (256_000, "config"))

    def test_model_window_is_not_inferred_from_parameter_size(self):
        """9B/30B 这类规格不能决定窗口：同是 9B，GLM-4 是 128k，Gemma-2 是 8k。"""
        from app.agent import llm
        self.assertEqual(llm.model_default_context_window("glm-4-9b-chat"), 131_072)
        self.assertEqual(llm.model_default_context_window("gemma-2-9b"), 8_192)
        self.assertEqual(llm.model_default_context_window("gemma-3-27b"), 131_072)
        self.assertEqual(llm.model_default_context_window("llama-3.1-8b"), 131_072)
        self.assertEqual(llm.model_default_context_window("llama-3-8b"), 8_192)
        self.assertEqual(llm.model_default_context_window("qwen3-30b-a3b"), 32_768)
        self.assertEqual(llm.model_default_context_window("phi-4-14b"), 16_384)
        self.assertEqual(llm.model_default_context_window("phi-4-mini"), 131_072)
        # 云端 Qwen3.5 是 1M；本地 Qwen2.5/Qwen3 GGUF 仍是 32k。
        self.assertEqual(llm.model_default_context_window("qwen3.5-flash"), 1_000_000)
        self.assertEqual(llm.model_default_context_window("qwen3-max"), 131_072)
        self.assertEqual(llm.model_default_context_window("qwen2.5:7b"), 32_768)

    def test_extract_ctx_reads_ollama_namespaced_keys(self):
        """Ollama /api/show 的键是 `qwen2.context_length`，不能只认裸键名。"""
        from app.agent import llm
        hit = {"model_info": {"qwen2.context_length": 32_768,
                              "qwen2.embedding_length": 4_096}}
        self.assertEqual(llm._extract_ctx(hit), 32_768)

    def test_probe_reads_ollama_show_metadata(self):
        """Ollama /api/ps 不可用时，回退到 /api/show 的模型架构上限。"""
        from unittest import mock
        from app.agent import llm

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps({
                    "model_info": {"qwen2.context_length": 32_768}
                }).encode("utf-8")

        calls = []

        def fake_urlopen(req, timeout=0):
            url = req.full_url
            calls.append(url)
            if "/api/show" in url:
                return FakeResp()
            raise OSError("not available")

        llm._MODEL_META_CACHE.clear()
        with mock.patch.object(llm.net, "urlopen", side_effect=fake_urlopen):
            hit = llm._probe_model_meta({
                "base_url": "http://127.0.0.1:11434/v1",
                "model": "qwen2.5:7b", "api_key": "",
            })
        self.assertEqual(llm._extract_ctx(hit), 32_768)
        self.assertTrue(any("/api/show" in u for u in calls),
                        "OpenAI /models 不带窗口时应回退到 Ollama /api/show")

    def test_probe_prefers_ollama_live_context_over_model_max(self):
        """/api/ps 是实际加载的 num_ctx，必须优先于 /api/show 的模型上限。"""
        from unittest import mock
        from app.agent import llm

        class FakeResp:
            def __init__(self, obj):
                self.obj = obj

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps(self.obj).encode("utf-8")

        def fake_urlopen(req, timeout=0):
            url = req.full_url
            if url.endswith("/api/ps"):
                return FakeResp({"models": [
                    {"name": "qwen2.5:7b", "context_length": 4_096}]})
            if "/api/show" in url:
                return FakeResp({"model_info": {"qwen2.context_length": 40_960}})
            raise OSError("not available")

        llm._MODEL_META_CACHE.clear()
        with mock.patch.object(llm.net, "urlopen", side_effect=fake_urlopen):
            hit = llm._probe_model_meta({
                "base_url": "http://127.0.0.1:11434/v1",
                "model": "qwen2.5:7b", "api_key": "",
            })
        self.assertEqual(llm._extract_ctx(hit), 4_096,
                         "实际服务 4k 时不能按模型卡 40k 发")
        self.assertEqual(hit.get("_ctx_source"), "provider:ollama:/api/ps")

    def test_context_overflow_error_is_learned(self):
        from app.agent import llm
        cfg = {"base_url": "http://127.0.0.1:9/v1", "model": "served-32k"}
        llm._MODEL_CTX_OVERRIDE.clear()
        try:
            exc = RuntimeError(
                "This model's maximum context length is 32768 tokens. "
                "However, your messages resulted in 40000 tokens.")
            self.assertEqual(llm.context_overflow_limit(exc, cfg), 32_768)
            self.assertEqual(llm.model_context_window(cfg), (32_768, "runtime"))
            self.assertEqual(llm.context_overflow_limit(
                RuntimeError("prompt is too long: 208000 tokens > 200000 maximum"),
                cfg), 200_000)
            self.assertEqual(llm.context_overflow_limit(
                RuntimeError("Range of input length should be [1, 131072]"),
                cfg), 131_072)
        finally:
            llm._MODEL_CTX_OVERRIDE.clear()

    def test_usage_accumulator_tracks_last_prompt_separately(self):
        """上下文面板只看最后一次请求；整轮累加值会远超窗口。"""
        from app.agent import loop
        total: dict = {}
        loop._accumulate(total, {"prompt_tokens": 100, "completion_tokens": 5})
        loop._accumulate(total, {"prompt_tokens": 150, "completion_tokens": 7})
        self.assertEqual(total["prompt_tokens"], 250, "计费仍用整轮累加")
        self.assertEqual(total["last_prompt_tokens"], 150,
                         "面板要用最后一次请求，而不是 250")

    def test_system_messages_are_merged_to_one_at_top(self):
        """vLLM + Qwen3.6 只接受一条 system，且必须在开头。"""
        from app.agent import llm
        msgs = [
            {"role": "system", "content": "主提示词"},
            {"role": "user", "content": "旧问题"},
            {"role": "assistant", "content": "旧回答"},
            {"role": "system", "content": "【当前时间】..."},
            {"role": "system", "content": "【SOP】..."},
            {"role": "user", "content": "当前问题"},
        ]
        out = llm._merge_system_messages(msgs)
        self.assertEqual(out[0]["role"], "system")
        self.assertIn("主提示词", out[0]["content"])
        self.assertIn("【当前时间】", out[0]["content"])
        self.assertIn("【SOP】", out[0]["content"])
        self.assertEqual([m["role"] for m in out],
                         ["system", "user", "assistant", "user"])
        # 没有 system 时原样返回
        plain = [{"role": "user", "content": "hi"}]
        self.assertEqual(llm._merge_system_messages(plain), plain)

    def test_context_overflow_only_shrinks(self):
        from app.agent import llm
        cfg = {"base_url": "http://127.0.0.1:9/v1", "model": "same-32k"}
        key = ("http://127.0.0.1:9/v1", "same-32k")
        exc = RuntimeError("maximum context length is 32768 tokens")
        llm._MODEL_CTX_OVERRIDE.clear()
        try:
            self.assertIsNone(
                llm.context_overflow_limit(exc, cfg, current_window=32_768),
                "报错数字不小于当前窗口时不重试")
            self.assertNotIn(key, llm._MODEL_CTX_OVERRIDE)
        finally:
            llm._MODEL_CTX_OVERRIDE.clear()

    def test_compact_tool_result_keeps_actionable_fields(self):
        raw = json.dumps({
            "summary": "查询成功",
            "items": [{"candidate_id": i, "name": "张三" * 50,
                       "raw_text": "很长的简历" * 500} for i in range(20)],
            "download_url": "/export/roster.xlsx",
            "proposal_id": 42,
            "count": 20,
        }, ensure_ascii=False)
        out = cg.compact_tool_result("search_candidates", raw, max_tokens=900)
        data = json.loads(out)
        self.assertLessEqual(cg.estimate_tokens(out), 900)
        self.assertTrue(data.get("_context_trimmed"))
        self.assertEqual(data.get("download_url"), "/export/roster.xlsx")
        self.assertEqual(data.get("proposal_id"), 42)
        self.assertIn("_context_hint", data)

    def test_compact_tool_result_preserves_error(self):
        raw = json.dumps({"error": "权限不足", "hint": "需要 read 权限",
                          "raw": "x" * 50_000}, ensure_ascii=False)
        out = cg.compact_tool_result("get_candidate", raw, max_tokens=400)
        data = json.loads(out)
        self.assertEqual(data.get("error"), "权限不足")
        self.assertLessEqual(cg.estimate_tokens(out), 400)

    def test_govern_messages_keeps_system_and_current_user(self):
        messages = [
            {"role": "system", "content": "系统提示" * 500},
            {"role": "user", "content": "旧问题" * 2000},
            {"role": "assistant", "content": "旧回答" * 2000},
            {"role": "user", "content": "当前问题"},
        ]
        stats = cg.govern_messages(
            messages, budget=2_500, tool_limit=500,
            history_message_tokens=300, keep_recent_tools=1)
        self.assertLessEqual(cg.estimate_tokens(messages[0]["content"]),
                             cg.estimate_tokens("系统提示" * 500))
        self.assertEqual(messages[0]["role"], "system")
        self.assertEqual(messages[-1]["content"], "当前问题")
        self.assertGreater(stats["trimmed_tokens"], 0)
        self.assertTrue(stats["estimated_after"] <= stats["estimated_before"])

    def test_govern_messages_never_drops_tool_pair(self):
        tool = {"role": "tool", "tool_call_id": "c1", "name": "get_candidate",
                "content": json.dumps({"raw_text": "x" * 20_000}, ensure_ascii=False)}
        assistant = {"role": "assistant", "content": "",
                     "tool_calls": [{"id": "c1", "type": "function",
                                     "function": {"name": "get_candidate",
                                                  "arguments": "{}"}}]}
        messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "问题"},
            assistant, tool,
        ]
        cg.govern_messages(messages, budget=1_200, tool_limit=300)
        self.assertIn(assistant, messages)
        self.assertIn(tool, messages)
        self.assertLessEqual(cg.estimate_tokens(tool["content"]), 300)

    def test_alerts_have_levels_and_thresholds(self):
        alerts = cg.evaluate_alerts(
            tokens_in=10_000, budget=8_000, tool_tokens=6_000,
            tool_calls=4, tool_errors=2, rounds=5, max_rounds=5,
            cache_hit_tokens=0, cache_reported=1, trimmed_tokens=3_000)
        codes = {a["code"] for a in alerts}
        self.assertIn("context_over_budget", codes)
        self.assertIn("tool_ratio_high", codes)
        self.assertIn("tool_error_high", codes)
        self.assertEqual(cg.alert_level(alerts), "high")


class AgentRunMetricsDbTest(unittest.TestCase):
    def test_new_columns_and_aggregation(self):
        with tempfile.TemporaryDirectory() as d:
            conn = db.connect(os.path.join(d, "metrics.db"))
            try:
                cols = {r[1] for r in conn.execute("PRAGMA table_info(agent_runs)")}
                for name in ("tool_tokens", "tool_ratio", "tool_calls_count",
                             "tool_errors", "tool_error_rate", "context_budget",
                             "context_trimmed", "alert_level", "alerts_json",
                             "metrics_json"):
                    self.assertIn(name, cols)
                db.log_agent_run(conn, {
                    "session_id": "s1", "question": "q", "answer": "a",
                    "model": "m", "rounds": 2, "tokens_in": 1_000,
                    "tokens_out": 100, "cache_hit_tokens": 200,
                    "cache_reported": 1, "tool_tokens": 400, "tool_ratio": 0.4,
                    "tool_calls_count": 3, "tool_errors": 1,
                    "tool_error_rate": 0.3333, "context_budget": 8_000,
                    "context_trimmed": 120, "alert_level": "warn",
                    "alerts": [{"code": "x", "level": "warn", "message": "m"}],
                    "metrics": {"budget": 8_000},
                    "latency_ms": 12, "status": "ok", "mode": "llm",
                    "operator": "HR",
                })
                run = db.list_agent_runs(conn, 1)[0]
                self.assertIsInstance(run["alerts_json"], list)
                self.assertIsInstance(run["metrics_json"], dict)
                metrics = db.agent_metrics(conn, days=7)
                self.assertEqual(metrics["tool_calls"], 3)
                self.assertEqual(metrics["tool_errors"], 1)
                self.assertEqual(metrics["alert_runs"], 1)
                self.assertEqual(metrics["tool_error_rate"], 33.3)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()
