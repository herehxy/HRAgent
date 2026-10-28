from __future__ import annotations

import pathlib
import sys

sys.stdout.reconfigure(errors="replace")

path = pathlib.Path(sys.argv[1])
start = int(sys.argv[2])
end = int(sys.argv[3])
lines = path.read_text(encoding="utf-8").splitlines()
for number in range(max(1, start), min(len(lines), end) + 1):
    print(f"{number:5d}  {lines[number - 1]}")
