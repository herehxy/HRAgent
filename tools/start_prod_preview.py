from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "dist_prod" / "HRAgent工作台" / "HRAgent工作台.exe"


def main() -> int:
    env = dict(os.environ)
    env.update({
        "TP_USER_HOME": str(ROOT),
        "TP_CONFIG_DIR": str(ROOT / "config"),
        "TP_DATA_DIR": str(ROOT / "data"),
        "TP_PORT": "8767",
        "TP_NO_BROWSER": "1",
    })
    process = subprocess.Popen(
        [str(EXE), "--data-dir", str(ROOT), "--no-browser"],
        cwd=str(EXE.parent),
        env=env,
        creationflags=(
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        ),
        close_fds=True,
    )
    print(process.pid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
