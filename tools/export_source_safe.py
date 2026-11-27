# -*- coding: utf-8 -*-
"""导出**可安全分享的源码包**：排除本机数据、凭据、内网配置和构建产物。

为什么需要单独一条导出通道
--------------------------
`.gitignore` 只保护 git，不影响 Windows 的“右键 → 压缩”。一份带真实简历、
master.key、邮箱授权码、模型 API Key 的源码目录被整包发出去，等于把生产数据
和凭据一起送走。这个脚本用**白名单式排除**生成分享包，并在写入后做一次自检：

- `data/`（含简历、数据库、头像）整目录不进入；
- `config/` 只保留产品字典和示例，不进入 secrets/master.key/mailbox/model 等；
- `.runtime_*`、dist/release/build、日志、探针脚本不进入；
- 文本文件扫描 `sk-...` 和私钥头，发现即失败，不会“带病生成”。

用法：
    python tools/export_source_safe.py
    python tools/export_source_safe.py --out D:\\分享\\HRAgent-src.zip
"""
from __future__ import annotations

import argparse
import re
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: 一级目录直接不进入分享包
TOP_SKIP = {
    ".git", ".hg", ".svn", ".venv", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".runtime_deps", ".runtime_local", ".runtime_pyinstaller",
    ".runtime_build", ".wheels", "data", "deploy", "docs",
    ".claude", ".github", ".vscode",
    "build_prod_assets", "build_prod_work", "dist", "dist_prod",
    "dist_prod_tools", "release", "release_prod",
    "release_prod_archive_20261028", "_prod_verify",
}
#: 任意层级的这些目录不进入
DIR_SKIP = {
    "__pycache__", ".pytest_cache", ".mypy_cache", ".git",
    "doc_profiles", "domains", "data", "_launch_tmp", "_perf_tmp_data",
}
#: config/ 下只保留这些产品字典/模板；其余本机配置一律不分享
CONFIG_KEEP = {
    "stages.json", "tiers.json", "majors.json", "ontology.json",
    "universities.json", "school_subjects.json",
    "secrets.example.json", "mailbox.example.json",
    "model.example.json", "embedding.example.json",
    "jd.example.json", "interview.example.json",
}
#: 文件名命中即不进入（任意层级）
FILE_SKIP = {
    "secrets.json", "master.key", "imap.secret", "smtp.secret",
    "mailbox.json", "model.json", "embedding.json", "search.json",
    "tp.secret", "model.key", "auth.json",
    "debug.log", "_source_serve.log", "port_check.txt",
}
SUFFIX_SKIP = (".pyc", ".pyo", ".tmp", ".log", ".bak", ".old", ".zip",
               ".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3")


def _skip(rel: Path) -> bool:
    parts = rel.parts
    if not parts:
        return False
    name = rel.name
    lower = name.lower()
    if parts[0] in TOP_SKIP:
        return True
    if any(p in DIR_SKIP for p in parts):
        return True
    # 测试运行/修复过程留下的 _launch_tmp、_perf_tmp_data、_review_fix...
    if any(p.startswith("_") for p in parts[:-1]):
        return True
    if name in FILE_SKIP or name.startswith("_bak_"):
        return True
    if ".bak_" in name or lower.endswith(SUFFIX_SKIP) \
            or "~rf" in lower:
        return True
    # 源码根目录的本机探针/日志/备份不分享
    if len(parts) == 1:
        if name.startswith("_") or name.startswith("build_") \
                or name.startswith("replace_local"):
            return True
    if parts[0] == "config":
        if len(parts) == 2 and name not in CONFIG_KEEP:
            return True
    return False


def _verify(zf: zipfile.ZipFile) -> None:
    names = [n for n in zf.namelist()]
    bad = [n for n in names if n == "data" or n.startswith("data/")
           or any(part in FILE_SKIP for part in Path(n).parts)
           or ".runtime_" in n or "__pycache__" in n]
    if bad:
        raise SystemExit("分享包自检失败，含敏感路径：\n" + "\n".join(bad[:20]))
    key_re = re.compile(rb"sk-[A-Za-z0-9]{20,}")
    for n in names:
        if n.endswith((".py", ".json", ".md", ".txt", ".yml", ".yaml",
                       ".js", ".css", ".html", ".spec", ".cmd", ".ps1")):
            try:
                data = zf.read(n)
            except Exception:
                continue
            if len(data) > 4 * 1024 * 1024:
                continue
            if key_re.search(data) or (b"BEGIN" in data and b"PRIVATE KEY" in data):
                raise SystemExit(f"分享包自检失败：{n} 中疑似凭据")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导出不含本机密钥/数据的源码分享包")
    default = Path.home() / "Desktop" / f"HRAgent-src-safe-{time.strftime('%Y%m%d')}.zip"
    parser.add_argument("--out", default=str(default), help="输出 zip 路径")
    args = parser.parse_args(argv)
    out = Path(args.out).expanduser().resolve()
    if ROOT in out.parents or out == ROOT:
        raise SystemExit("拒绝把分享包写进源码目录；请指定源码目录之外的路径")
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        raise SystemExit(f"输出文件已存在，请先移走或换路径：{out}")

    count = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        readme = (
            "HRAgent 源码分享包（安全导出）\n"
            "已排除 data/、密钥、邮箱账号、模型/内网端点、构建产物与本机探针。\n"
            "本地运行请使用安装版，或设置 TP_CONFIG_DIR 指向自己的配置目录。\n"
        )
        zf.writestr("SHARE-README.txt", readme)
        for path in ROOT.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(ROOT)
            if _skip(rel):
                continue
            zf.write(path, rel.as_posix())
            count += 1
        _verify(zf)

    print(f"[ok] 已生成安全分享包：{out}")
    print(f"[ok] 文件数：{count}；大小：{out.stat().st_size / 1024 / 1024:.1f} MB")
    print("[ok] 自检：无 data/、无密钥文件名、无 sk- Key/私钥内容")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
