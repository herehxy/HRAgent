from __future__ import annotations

import os
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "release_prod" / "HRAgent工作台" / "HRAgent工作台.exe"


def main() -> int:
    env = dict(os.environ)
    for name in ("TP_USER_HOME", "TP_CONFIG_DIR", "TP_DATA_DIR"):
        env.pop(name, None)
    env["TP_PORT"] = "8756"
    env["TP_NO_BROWSER"] = "1"
    process = subprocess.Popen(
        [str(EXE), "--no-browser"],
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
