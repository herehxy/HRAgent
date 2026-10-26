# -*- coding: utf-8 -*-
"""`hra_updater.validate_targets` 的路径闸门边界自检。

为什么要有这个：更新器的闸门是"宁可报错也不静默跳过"的安全网，但**闸门太宽
会把合法更新误杀，太窄会漏掉真威胁**。两次实测都被误杀打中：

  ① `_internal/config/imap.secret` → `.secret` 在全局禁后缀里 → 更新链断
  ② `_internal/seed/data/workbench.db` → `.db` 在全局禁后缀里 → 更新链断

修完又立刻冒出第三个风险：种子豁免如果写成"整目录放行"，`_internal/seed/master.key`
也会被放行（密钥文件可被更新包替换）。所以这里把边界**逐条钉死**。

判据设计（按记忆里的验收纪律）：
  · **两边都验**：每条既断言"该放行的放行"，也断言"该拒的拒绝"
  · **只命中 X**：不放行时用异常类型 + 消息，而不是"没报错就算过"

跑法：python tools/_test_updater_gates.py
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hra_updater as U  # noqa: E402

PASS, FAIL = [], []


def ok(cond, name, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'OK ' if cond else 'FAIL'}] {name}" + (f"  {extra}" if extra else ""))
    return bool(cond)


def _verdict(rel: str) -> str:
    """跑一遍 validate_targets，看这条路径的结局（放行 / 拒绝:原因）。"""
    try:
        U.validate_targets(os.path.abspath("C:/tmp/fake_install"),
                           {"files": [{"path": rel}]})
        return "放行"
    except U.UpdateError as e:
        return f"拒绝({e})"


#: (相对路径, 期望结局) —— 期望写"放行"或"拒绝"
CASES = [
    # ---- 必须放行：程序资源 ----
    ("_internal/seed/data/workbench.db", "放行"),          # 演示库（曾被误杀）
    ("_internal/seed/data/archive/张三-简历.pdf", "放行"),   # 演示简历原件
    ("_internal/config/imap.secret", "放行"),              # 配置模板（曾被误杀）
    ("_internal/config/secrets.json", "放行"),
    ("_internal/seed/config/tiers.json", "放行"),
    ("_internal/app/ui.py", "放行"),
    ("_internal/cv2/data/config.py", "放行"),               # 第三方包自带 data 段

    # ---- 必须拒绝：用户数据 ----
    ("data/workbench.db", "拒绝"),
    ("_internal/data/workbench.db", "拒绝"),
    ("config/master.key", "拒绝"),
    ("_internal/master.key", "拒绝"),
    ("_internal/seed/master.key", "拒绝"),                 # ★ 种子豁免不得放开密钥
    ("_internal/seed/data/evil.sqlite3", "放行"),          # 同类演示库，允许

    # ---- 必须拒绝：路径越界 ----
    ("../../evil.txt", "拒绝"),
    ("_internal/../../evil.txt", "拒绝"),
]


def main() -> int:
    print("=" * 64)
    print("更新器路径闸门边界自检")
    print("=" * 64)

    print("\n--- 逐条边界 ---")
    for rel, expect in CASES:
        got = _verdict(rel)
        good = (got == expect) if expect == "放行" else got.startswith("拒绝")
        ok(good, f"{rel}  →  {expect}", f"实际={got}")

    print("\n--- 判据自检：闸门本身不能是恒真的 ---")
    # 若 validate_targets 被改成永远放行，上面"必须拒绝"那组会全挂 → 判据有效
    n_reject = sum(1 for r, e in CASES if e == "拒绝" and _verdict(r).startswith("拒绝"))
    ok(n_reject >= 7, "拒绝类用例确实被拒（闸门不是恒放行）", f"实拒 {n_reject} 条")
    n_allow = sum(1 for r, e in CASES if e == "放行" and _verdict(r) == "放行")
    ok(n_allow >= 7, "放行类用例确实放行（闸门不是恒拒绝）", f"实放 {n_allow} 条")

    print("\n" + "=" * 64)
    print(f"通过 {len(PASS)}，失败 {len(FAIL)}")
    for f in FAIL:
        print("  FAILED:", f)
    print("=" * 64)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())