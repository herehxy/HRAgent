from __future__ import annotations

import hashlib
import os
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "dist_prod" / "HRAgent工作台"
OUTPUT = ROOT / "release_prod" / "HRAgent工作台-v2.2.2-便携完整包.zip"
EXCLUDE = {"HRAgent更新器.exe"}


def main() -> int:
    with zipfile.ZipFile(OUTPUT, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for current, _dirs, files in os.walk(SOURCE):
            for name in files:
                if name in EXCLUDE:
                    continue
                source = Path(current) / name
                arcname = Path(SOURCE.name) / source.relative_to(SOURCE)
                zf.write(source, arcname.as_posix())
    digest = hashlib.sha256(OUTPUT.read_bytes()).hexdigest()
    print(OUTPUT)
    print(digest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
