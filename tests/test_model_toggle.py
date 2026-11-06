# -*- coding: utf-8 -*-
"""模型启用开关回归测试（v4.2.1）。"""
from __future__ import annotations

import json
import os
import sys
import tempfile

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

from app.agent import llm  # noqa: E402

PASS = FAIL = 0


def ok(cond, label, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  [PASS] {label}" + (f"  ({detail})" if detail else ""))
    else:
        FAIL += 1
        print(f"  [FAIL] {label}" + (f"  ({detail})" if detail else ""))


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="model_toggle_")
    cfg_path = os.path.join(tmp, "model.json")
    sec_path = os.path.join(tmp, "secrets.json")
    with open(cfg_path, "w", encoding="utf-8") as fh:
        json.dump({"enabled": True, "base_url": "http://old/v1",
                   "model": "old", "timeout": 180}, fh)
    old_cfg, old_sec = llm.CONFIG_PATH, llm.SECRETS_PATH
    llm.CONFIG_PATH, llm.SECRETS_PATH = cfg_path, sec_path
    try:
        r = llm.save_cfg(enabled=False)
        data = _read(cfg_path)
        ok(r["changed"] == ["enabled"] and data["enabled"] is False,
           "关闭模型只改 enabled，不动其它字段",
           f"{r} {data}")

        r = llm.save_cfg(enabled=True)
        data = _read(cfg_path)
        ok(r["changed"] == ["enabled"] and data["enabled"] is True,
           "重新开启模型", str(data))

        r = llm.save_cfg(enabled=True)
        ok(r["changed"] == [], "重复保存相同状态不产生变更", str(r))

        r = llm.save_cfg(base_url="http://new/v1", model="new-model",
                         enabled=False)
        data = _read(cfg_path)
        ok(data["base_url"] == "http://new/v1" and data["model"] == "new-model"
           and data["enabled"] is False and data["timeout"] == 180
           and set(r["changed"]) == {"base_url", "model", "enabled"},
           "改地址/模型/开关时保留 timeout 等其它字段", f"{r} {data}")

        r = llm.save_cfg(api_key="sk-test-123456")
        sec = _read(sec_path)
        ok(r["changed"] == ["api_key"] and sec.get("api_key") == "sk-test-123456",
           "API Key 仍写 secrets.json", str(sec))
    finally:
        llm.CONFIG_PATH, llm.SECRETS_PATH = old_cfg, old_sec

    print("\n" + "=" * 70)
    print(f"结果：{PASS} 通过，{FAIL} 失败")
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
