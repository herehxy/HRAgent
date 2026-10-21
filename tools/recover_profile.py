from __future__ import annotations

import os
import shutil
import sqlite3
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in os.sys.path:
    os.sys.path.insert(0, str(ROOT))

from app import profile_migrate


def candidate_count(db_path: Path) -> int:
    if not db_path.is_file():
        return -1
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM candidates").fetchone()[0])
    finally:
        conn.close()


def main() -> int:
    target = Path(os.environ["LOCALAPPDATA"]) / "HRAgentData"
    source_db = ROOT / "data" / "workbench.db"
    target_db = target / "data" / "workbench.db"
    source_count = candidate_count(source_db)
    target_count = candidate_count(target_db)
    print(f"source={source_count} target={target_count}")
    if source_count <= 0:
        raise RuntimeError("原目录没有候选人数据库，停止恢复")
    if target_count >= source_count:
        print("目标已经是完整档案，不执行覆盖")
        return 0

    if target.exists():
        backup = target.with_name(
            target.name + ".incomplete-" + time.strftime("%Y%m%d-%H%M%S")
        )
        if backup.exists():
            raise RuntimeError(f"备份目录已存在：{backup}")
        shutil.move(str(target), str(backup))
        print(f"空/不完整目录已保留：{backup}")

    marker = profile_migrate.migrate_profile(str(ROOT), str(target))
    restored = candidate_count(Path(profile_migrate.profile_db(str(target))))
    print(f"restored={restored}")
    print(marker)
    return 0 if restored == source_count else 1


if __name__ == "__main__":
    raise SystemExit(main())
