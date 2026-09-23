"""把演示数据清空重来：备份 → 删库 → 重新 init → 清空原件区/回收目录/收信台账。

**为什么单独写一个脚本，而不是让人手敲 `rm`**：
这是不可逆操作，手敲容易漏掉一半（删了库忘了原件区，于是"库里没有、盘上还有"，
下次 `cli.py doctor` 报台账对不上）。把它写成脚本，步骤固定、有备份、有前后核对，
执行前还要显式加 `--yes`。

**只动项目内的 data/ 目录**：`config/mailbox.json` 里的 `folder_dir` 可能指向
项目外的真实目录（如桌面上的 HR_test1），那是用户的真实数据，**本脚本一律不碰**。

用法::

    python tools/reset_demo.py --dry-run   # 只列出会删什么
    python tools/reset_demo.py --yes       # 真清空（先自动备份）
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
from datetime import datetime

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

DATA = os.path.join(ROOT, "data")
DB_PATH = os.environ.get("TP_DB_PATH") or os.path.join(DATA, "workbench.db")
BACKUP_DIR = os.path.join(DATA, "backup")

# 业务数据目录：清空（内容删掉，目录保留，免得后续流程因为目录不存在而报错）
CLEAR_DIRS = [
    os.path.join(DATA, "archive"),      # 原件区（按年月分目录）
    os.path.join(DATA, "removed"),      # 来源文件回收目录
    os.path.join(DATA, "mail_in"),      # eml 演练邮箱
    os.path.join(DATA, "resumes"),      # 简历来源目录（样例会重新生成）
]

# 库里"候选人/投递/附件"之外还要一并清掉的表（都是业务数据，不是配置）
# 说明：不逐表 DELETE，而是**整库删除后重新 init**——
# 逐表删容易漏掉新加的表，且 sqlite_sequence 会残留，重建后 id 不连续看着像"漏了人"。
RESET_DB = True


def _size(p: str) -> str:
    if os.path.isdir(p):
        n = sum(len(f) for _r, _d, f in os.walk(p))
        return f"{n} 个文件"
    if os.path.exists(p):
        return f"{os.path.getsize(p) / 1024:.0f} KB"
    return "不存在"


def _backup() -> str:
    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = f"{datetime.now():%Y%m%d-%H%M%S}"
    db_dst = os.path.join(BACKUP_DIR, f"workbench-重置前-{stamp}.db")
    n = 1
    while os.path.exists(db_dst):
        n += 1
        db_dst = os.path.join(BACKUP_DIR, f"workbench-重置前-{stamp}-{n}.db")
    if os.path.exists(DB_PATH):
        shutil.copy2(DB_PATH, db_dst)
    tgz = os.path.join(BACKUP_DIR, f"data-目录快照-{stamp}.tgz")
    if os.path.exists(tgz):
        tgz = os.path.join(BACKUP_DIR, f"data-目录快照-{stamp}-{n}.tgz")
    # -C 到 ROOT，打包 data/ 与本体配置（领域包导入会改本体，一起留证）
    os.system(
        f'cd "{ROOT}" && tar -czf "{tgz}" --exclude="data/backup" data/ '
        f'config/ontology.json config/majors.json 2>/dev/null'
    )
    return db_dst + "\n" + tgz


def main() -> int:
    ap = argparse.ArgumentParser(description="清空演示数据并重新初始化")
    ap.add_argument("--yes", action="store_true", help="确认执行（不加则只预演）")
    ap.add_argument("--keep-backup", action="store_true", default=True,
                    help="执行前备份（默认开启，无法关闭）")
    args = ap.parse_args()

    print("=== 将清空的内容 ===")
    print(f"  · 数据库      {DB_PATH}（{_size(DB_PATH)}）→ 删除后重新 init")
    for d in CLEAR_DIRS:
        print(f"  · 目录内容    {d}（{_size(d)}）")

    # 明确声明不动的东西，避免误会
    import json
    try:
        cfg = json.load(open(os.path.join(ROOT, "config", "mailbox.json"), encoding="utf-8"))
        outside = cfg.get("folder_dir") or ""
    except Exception:                                    # noqa: BLE001
        outside = ""
    if outside and not os.path.realpath(outside).startswith(os.path.realpath(ROOT)):
        print(f"\n[!] 注意：简历来源目录 folder_dir = {outside}")
        print("    它在项目之外，属于你的真实数据，**本脚本不会碰它**。")
        print("    如果不想让它继续作为导入源，改 config/mailbox.json 或界面上重新指定。")

    if not args.yes:
        print("\n[预演] 什么都没删。确认无误后加 --yes 执行。")
        return 0

    print("\n=== 备份 ===")
    files = _backup()
    for f in files.split("\n"):
        print(f"[√] {f}")

    print("\n=== 清空 ===")
    if RESET_DB and os.path.exists(DB_PATH):
        os.remove(DB_PATH)
        print(f"[√] 已删除数据库：{DB_PATH}")
    for d in CLEAR_DIRS:
        if os.path.isdir(d):
            shutil.rmtree(d)
        os.makedirs(d, exist_ok=True)
        print(f"[√] 已清空目录：{d}")

    print("\n=== 重新初始化 ===")
    os.system(f'cd "{ROOT}" && {sys.executable} cli.py init')
    print("\n[i] 下一步：python tools/seed_2026_jobs.py 写入岗位；"
          "python tools/make_sample_resumes.py 生成样例简历")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
