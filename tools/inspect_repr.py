import pathlib
import sys


path = pathlib.Path(sys.argv[1])
start = int(sys.argv[2])
end = int(sys.argv[3])
for number, line in enumerate(
        path.read_text(encoding="utf-8").splitlines()[start - 1:end], start):
    print(number, ascii(line))
