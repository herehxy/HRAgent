# -*- coding: utf-8 -*-
"""验证「补搬原件」：用户目录已有库时，旧安装目录里的简历原件仍然会被搬过来。

这是热更新场景的真实痛点：
  更新器只换程序文件（它拒绝写任何 data/ 段），
  老用户的原件还躺在 `<安装目录>/_internal/data/archive/` 里；
  首次启动时如果用户目录**已有库**，整体迁移会被跳过（不能覆盖库），
  原件也就跟着留在旧目录 → 列表里有人、点开取不到、**更没法重新分析**。

两种场景都要验：
  A. 用户目录为空    → 整体迁移，原件跟着走
  B. 用户目录已有库  → 库不动，**只补缺失原件**

用法：python tools/_verify_supplement.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "repo_src"))

from app import profile_migrate as pm  # noqa: E402

PASS, FAIL = [], []


def ok(cond, name, extra=""):
    (PASS if cond else FAIL).append(name)
    print(f"  [{'√' if cond else '×'}] {name}" + (f"　{extra}" if extra else ""))


def make_legacy(root: str, n_pdf: int = 3) -> str:
    """造 2.0.0 形态：<root>/_internal/data/{workbench.db, archive/*.pdf, master.key}"""
    inst = os.path.join(root, "HRAgent工作台")
    data = os.path.join(inst, "_internal", "data")
    arch = os.path.join(data, "archive")
    os.makedirs(arch, exist_ok=True)
    os.makedirs(os.path.join(inst, "_internal", "config"), exist_ok=True)
    # 库：从源码树里的开发库复制（有真实表结构），没有就建空库
    src_db = os.path.join(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))), "repo_src", "data", "workbench.db")
    if os.path.isfile(src_db):
        shutil.copy2(src_db, os.path.join(data, "workbench.db"))
    else:
        import sqlite3
        con = sqlite3.connect(os.path.join(data, "workbench.db"))
        con.execute("CREATE TABLE candidates(id INTEGER PRIMARY KEY, name TEXT)")
        con.commit()
        con.close()
    for i in range(n_pdf):
        with open(os.path.join(arch, f"张{i+1}_简历.pdf"), "wb") as f:
            f.write(("%%PDF-1.4 legacy resume %d" % i).encode())
    with open(os.path.join(data, "master.key"), "wb") as f:
        f.write(b"K" * 44)
    with open(os.path.join(inst, "_internal", "config", "master.key"), "wb") as f:
        f.write(b"K" * 44)
    return inst


def count_pdfs(path: str) -> int:
    if not os.path.isdir(path):
        return 0
    return sum(len(fs) for _, _, fs in os.walk(path))


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="hra_supp_")
    try:
        # ---------- A. 用户目录为空：整体迁移（回归，别改坏） ----------
        print("=== A. 用户目录为空 → 整体迁移含原件 ===")
        a_root = os.path.join(tmp, "a")
        inst = make_legacy(a_root)
        home = os.path.join(a_root, "home")
        os.makedirs(home, exist_ok=True)
        r = pm.migrate_profile(inst, home)
        ok(r.get("counts", {}).get("candidates", 0) > 0, "候选人迁过来了",
           str(r.get("counts")))
        n = count_pdfs(os.path.join(home, "data", "archive"))
        ok(n == 3, "**原件全部迁过来**", f"{n} 个")

        # ---------- B. 用户目录已有库：只补原件 ----------
        print("\n=== B. 用户目录已有库 → 库不动，只补原件 ===")
        b_root = os.path.join(tmp, "b")
        inst2 = make_legacy(b_root)
        home2 = os.path.join(b_root, "home")
        os.makedirs(os.path.join(home2, "data", "archive"), exist_ok=True)
        # 先放一个"用户已有的库"（内容与旧库不同：只有 id/name 空表那种也无所谓，
        # 这里直接复制旧库后再加一条标记，确保"没被覆盖"可判定）
        shutil.copy2(os.path.join(inst2, "_internal", "data", "workbench.db"),
                     os.path.join(home2, "data", "workbench.db"))
        marker = os.path.join(home2, "data", "archive", "用户自己后来的.pdf")
        with open(marker, "wb") as f:
            f.write(b"%PDF-1.4 mine")
        db_md5_before = __import__("hashlib").md5(
            open(os.path.join(home2, "data", "workbench.db"), "rb").read()).hexdigest()

        res = pm.supplement_originals(inst2, home2)
        print(f"    结果：{res}")
        ok(res.get("copied") == 3, "补搬了 3 个原件", f"copied={res.get('copied')}")
        n2 = count_pdfs(os.path.join(home2, "data", "archive"))
        ok(n2 == 4, "用户原有的 1 个 + 补搬的 3 个 = 4", f"{n2} 个")
        ok(os.path.isfile(marker), "**用户原有的原件没被覆盖**")
        db_md5_after = __import__("hashlib").md5(
            open(os.path.join(home2, "data", "workbench.db"), "rb").read()).hexdigest()
        ok(db_md5_before == db_md5_after, "**库一个字节没动**")

        # 幂等：再跑一次不该重复复制
        res2 = pm.supplement_originals(inst2, home2)
        ok(res2.get("copied") == 0, "重复执行不再复制（幂等）", f"copied={res2.get('copied')}")

        # 种子目录必须跳过（否则会把演示数据塞进真实库）
        print("\n=== C. 种子目录必须跳过 ===")
        seed_root = os.path.join(tmp, "c", "HRAgent工作台", "_internal", "seed")
        make_legacy(os.path.join(tmp, "c"))
        # 把 archive 挪到 seed 下造一个"种子库"
        seed_data = os.path.join(tmp, "c", "HRAgent工作台", "_internal", "seed", "data")
        os.makedirs(os.path.join(seed_data, "archive"), exist_ok=True)
        with open(os.path.join(seed_data, "archive", "演示简历.pdf"), "wb") as f:
            f.write(b"%PDF demo")
        home3 = os.path.join(tmp, "c", "home")
        os.makedirs(os.path.join(home3, "data"), exist_ok=True)
        res3 = pm.supplement_originals(seed_data, home3)
        ok(res3.get("skipped_seed") is True, "识别为种子目录并跳过",
           f"skipped_seed={res3.get('skipped_seed')}")
        ok(count_pdfs(os.path.join(home3, "data", "archive")) == 0,
           "种子里的演示简历没被补进用户目录")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n" + "=" * 56)
    print(f"通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
    for f in FAIL:
        print("  FAIL:", f)
    print("=" * 56)
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
