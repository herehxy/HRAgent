from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from update_agent import apply_update


def main() -> int:
    install = Path(sys.argv[1]) if len(sys.argv) > 1 else (
        ROOT / "release_prod" / "HRAgent工作台"
    )
    package = ROOT / "release_prod" / "HRAgent-update-v2.2.0.zip"
    os.environ.setdefault(
        "TP_USER_HOME",
        str(Path(os.environ["LOCALAPPDATA"]) / "HRAgentData"),
    )
    result = apply_update(
        str(package), str(install), relaunch=False,
        log=lambda message: print(message),
    )
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
