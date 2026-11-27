# -*- coding: utf-8 -*-
"""语义检索降级：向量服务不可用时不能静默返回 0 条。

真实库通常同时存着真模型向量和 local-hash-v1 兜底向量。查询侧退化后
必须整轮切到哈希索引，否则 384 维查询与 1024 维索引的相似度全是 -1。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from unittest.mock import patch

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from app import db, search  # noqa: E402

FAKE_MODEL = "fake-embedding"


def _seed(conn, with_hash: bool) -> None:
    for i, name in enumerate(("张三", "李四"), start=1):
        cid = db.insert_candidate(conn, {
            "identity_key": f"sem-{i}", "name": name,
        })
        db.save_embedding(conn, "candidate", cid, FAKE_MODEL,
                          [0.1] * 8, f"fake-{i}")
        if with_hash:
            db.save_embedding(conn, "candidate", cid, search._HASH_MODEL,
                              search.hash_embed("钛合金 材料 研发"),
                              f"hash-{i}")


class SemanticFallbackTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sem-fallback-")
        self.conn = db.connect(os.path.join(self.tmp, "t.db"))

    def tearDown(self):
        self.conn.close()

    def test_switches_to_hash_index_when_query_falls_back(self):
        _seed(self.conn, with_hash=True)
        with patch.object(search, "embed_one",
                          return_value=(search.hash_embed("钛合金"),
                                        search._HASH_MODEL,
                                        "embedding 服务不可用")):
            out = search.semantic(self.conn, "钛合金", top_k=3,
                                  cfg={"model": FAKE_MODEL})
        self.assertEqual(out["model"], search._HASH_MODEL)
        self.assertEqual(len(out["results"]), 2)
        self.assertIn("不可用", out["error"] or "")

    def test_reports_reason_when_no_hash_index_exists(self):
        _seed(self.conn, with_hash=False)
        with patch.object(search, "embed_one",
                          return_value=(search.hash_embed("钛合金"),
                                        search._HASH_MODEL,
                                        "embedding 服务不可用")):
            out = search.semantic(self.conn, "钛合金", top_k=3,
                                  cfg={"model": FAKE_MODEL})
        self.assertEqual(out["results"], [])
        self.assertIn("未建哈希索引", out["error"] or "")

    def test_similar_uses_hash_for_candidate_missing_model_vector(self):
        _seed(self.conn, with_hash=True)
        cid = db.insert_candidate(self.conn, {
            "identity_key": "sem-3", "name": "王五",
        })
        db.save_embedding(self.conn, "candidate", cid, search._HASH_MODEL,
                          search.hash_embed("钛合金 材料 研发"), "hash-3")
        out = search.similar_to(self.conn, cid, top_k=5,
                                cfg={"model": FAKE_MODEL})
        self.assertEqual(out["model"], search._HASH_MODEL)
        self.assertGreater(len(out["results"]), 0)
        self.assertNotIn("尚未建立索引", out.get("note") or "")


if __name__ == "__main__":
    unittest.main()
