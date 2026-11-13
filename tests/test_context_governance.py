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
