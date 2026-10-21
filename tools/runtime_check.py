from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXE = ROOT / "dist_prod" / "HRAgent工作台" / "HRAgent工作台.exe"
OUT = ROOT / "_prod_runtime_check.json"


def main() -> int:
    process = subprocess.run(
        [str(EXE), "--runtime-check", str(OUT)],
        cwd=str(EXE.parent),
        timeout=60,
    )
    print(process.returncode)
    print(OUT.read_text(encoding="utf-8"))
    return process.returncode


if __name__ == "__main__":
    raise SystemExit(main())
