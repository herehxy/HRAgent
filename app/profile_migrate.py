"""生产版用户目录、旧版数据迁移与配置合并。

这个模块刻意只依赖标准库：启动器和独立的更新器都要在导入业务模块之前调用它。
迁移永远是“从旧目录复制到新目录”，旧目录不改、不删；目标已有数据库则拒绝迁移。
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

DB_NAME = "workbench.db"
PROFILE_MARKER = ".hrprofile.json"
SECRET_CONFIG_NAMES = {
    "master.key", "secrets.json", "mailbox.json",
    "imap.secret", "smtp.secret",
}
CONFIG_TEMPLATE_EXCLUDE = SECRET_CONFIG_NAMES | {
    "model.json.bak", "secrets.example.json", "mailbox.example.json",
}
DATA_ITEMS = (
    "archive", "avatars", "resumes", "mail_in", "inbox", "outbox",
    "removed", "backup", "master.key",
)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def layout(root: str) -> dict[str, str] | None:
    """识别一个旧版根目录的数据/配置布局。"""
    base = os.path.abspath(str(root or ""))
    candidates = [
        (os.path.join(base, "data"), os.path.join(base, "config")),
        (base, os.path.join(os.path.dirname(base), "config")),
    ]
    for data_dir, config_dir in candidates:
        if os.path.isfile(os.path.join(data_dir, DB_NAME)):
            return {"root": base, "data": data_dir, "config": config_dir}
    return None


def profile_db(user_home: str) -> str:
    return os.path.join(os.path.abspath(user_home), "data", DB_NAME)


def has_profile(user_home: str) -> bool:
    return os.path.isfile(profile_db(user_home))


def _copy_file_if_missing(src: str, dst: str, conflicts: list[str]) -> bool:
    if not os.path.isfile(src):
        return False
    if os.path.exists(dst):
        conflicts.append(dst)
        return False
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copy2(src, dst)
    return True


def _copy_tree_if_missing(src: str, dst: str, conflicts: list[str]) -> int:
    copied = 0
    if not os.path.isdir(src):
        return copied
    for current, dirs, files in os.walk(src):
        rel = os.path.relpath(current, src)
        target_dir = dst if rel == "." else os.path.join(dst, rel)
        os.makedirs(target_dir, exist_ok=True)
        dirs[:] = [d for d in dirs if d not in ("__pycache__",)]
        for name in files:
            if name.endswith((".pyc", ".pyo")):
                continue
            if _copy_file_if_missing(os.path.join(current, name),
                                     os.path.join(target_dir, name), conflicts):
                copied += 1
    return copied


def _sqlite_snapshot(src: str, dst: str) -> None:
    """用 SQLite Backup API 取得一致快照（包含当前 WAL 中的已提交内容）。"""
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=".hrprofile-", suffix=".db", dir=os.path.dirname(dst)
    )
    os.close(fd)
    source = sqlite3.connect(f"file:{Path(src).as_posix()}?mode=ro", uri=True)
    target = sqlite3.connect(temporary)
    try:
        source.backup(target)
        target.commit()
    finally:
        target.close()
        source.close()
    os.replace(temporary, dst)


def _rewrite_prefixed_path(conn: sqlite3.Connection, table: str, column: str,
                           old_prefix: str, new_prefix: str) -> int:
    variants = {
        (old_prefix, new_prefix),
        (old_prefix.replace("\\", "/"), new_prefix.replace("\\", "/")),
        (old_prefix.replace("/", "\\"), new_prefix.replace("/", "\\")),
    }
    changed = 0
    for old, new in variants:
        if not old or old == new:
            continue
        changed += conn.execute(
            f'UPDATE "{table}" SET "{column}" = ? || substr("{column}", ?) '
            f'WHERE substr("{column}", 1, ?) = ?',
            (new, len(old) + 1, len(old), old),
        ).rowcount
    return changed


def rewrite_db_paths(db_path: str, old_data: str, new_data: str) -> int:
    """只改写以旧数据根开头的文本字段，其他路径与内容原样保留。"""
    conn = sqlite3.connect(db_path)
    changed = 0
    try:
        tables = [
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        for table in tables:
            for col in conn.execute(f'PRAGMA table_info("{table}")').fetchall():
                name, declared_type = col[1], str(col[2] or "").upper()
                if not any(token in declared_type for token in ("CHAR", "TEXT", "CLOB")):
                    continue
                changed += _rewrite_prefixed_path(
                    conn, table, name, os.path.abspath(old_data),
                    os.path.abspath(new_data),
                )
        conn.commit()
    finally:
        conn.close()
    return changed


def normalize_existing_data_paths(db_path: str, new_data: str) -> int:
    """Point stale absolute ``data`` paths at the current portable data root."""
    if not os.path.isfile(db_path):
        return 0
    conn = sqlite3.connect(db_path)
    changed_rows: list[tuple[str, str, int, str]] = []
    try:
        tables = [
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        new_norm = os.path.abspath(new_data).replace("\\", "/").lower()
        for table in tables:
            for col in conn.execute(f'PRAGMA table_info("{table}")').fetchall():
                name = col[1]
                lowered = str(name).lower()
                if not (lowered.endswith("_path") or lowered == "path"):
                    continue
                rows = conn.execute(
                    f'SELECT rowid, "{name}" FROM "{table}" WHERE "{name}" IS NOT NULL'
                ).fetchall()
                for rowid, value in rows:
                    if not isinstance(value, str) or not os.path.isabs(value):
                        continue
                    normalized = value.replace("\\", "/")
                    if normalized.lower().startswith(new_norm):
                        continue
                    marker = normalized.lower().rfind("/data/")
                    if marker < 0:
                        continue
                    replacement = os.path.join(
                        os.path.abspath(new_data),
                        normalized[marker + 6:].replace("/", os.sep),
                    )
                    if replacement != value:
                        changed_rows.append((table, name, int(rowid), replacement))
        if changed_rows:
            backup = os.path.join(
                os.path.dirname(db_path),
                "workbench.db.bak_pathfix-" + datetime.now().strftime("%Y%m%d-%H%M%S"),
            )
            _sqlite_snapshot(db_path, backup)
            for table, name, rowid, replacement in changed_rows:
                conn.execute(
                    f'UPDATE "{table}" SET "{name}" = ? WHERE rowid = ?',
                    (replacement, rowid),
                )
            conn.commit()
            print(f"[profile] 已修复 {len(changed_rows)} 条旧电脑绝对路径；备份：{backup}")
        return len(changed_rows)
    finally:
        conn.close()


def _counts(db_path: str) -> dict[str, int]:
    wanted = ("candidates", "applications", "documents", "settings", "users")
    conn = sqlite3.connect(db_path)
    try:
        tables = {
            row[0] for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        out: dict[str, int] = {}
        for table in wanted:
            if table in tables:
                out[table] = int(
                    conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                )
        return out
    finally:
        conn.close()


def _write_marker(user_home: str, payload: dict) -> None:
    path = os.path.join(user_home, PROFILE_MARKER)
    os.makedirs(user_home, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def migrate_profile(legacy_root: str, user_home: str, *, log=print) -> dict:
    """把旧版档案复制到独立用户目录。目标已有数据库时绝不动它。"""
    old = layout(legacy_root)
    if not old:
        raise ValueError(f"没有在旧版目录找到 {DB_NAME}：{legacy_root}")
    target = os.path.abspath(user_home)
    target_db = profile_db(target)
    if os.path.exists(target_db):
        raise FileExistsError(f"目标已有数据库，拒绝覆盖：{target_db}")

    source_db = os.path.join(old["data"], DB_NAME)
    before = _counts(source_db)
    conflicts: list[str] = []
    os.makedirs(os.path.join(target, "data"), exist_ok=True)
    os.makedirs(os.path.join(target, "config"), exist_ok=True)

    log(f"[migrate] 一致性快照：{source_db}")
    _sqlite_snapshot(source_db, target_db)
    rewritten = rewrite_db_paths(target_db, old["data"],
                                 os.path.join(target, "data"))

    copied_data = 0
    for name in DATA_ITEMS:
        source = os.path.join(old["data"], name)
        destination = os.path.join(target, "data", name)
        if os.path.isdir(source):
            copied_data += _copy_tree_if_missing(source, destination, conflicts)
        elif os.path.isfile(source):
            copied_data += int(_copy_file_if_missing(source, destination, conflicts))

    copied_config = 0
    if os.path.isdir(old["config"]):
        copied_config = _copy_tree_if_missing(
            old["config"], os.path.join(target, "config"), conflicts
        )

    after = _counts(target_db)
    if after != before:
        raise RuntimeError(f"迁移校验失败：源库 {before}，目标库 {after}")

    source_key = os.path.join(old["config"], "master.key")
    target_key = os.path.join(target, "config", "master.key")
    key_before = _sha256(source_key) if os.path.isfile(source_key) else None
    key_after = _sha256(target_key) if os.path.isfile(target_key) else None
    if key_before and key_before != key_after:
        raise RuntimeError("迁移校验失败：master.key 未原样复制")

    marker = {
        "schema": 1,
        "migrated_at": _now(),
        "source_root": os.path.abspath(legacy_root),
        "target_root": target,
        "counts": after,
        "rewritten_paths": rewritten,
        "copied_data_files": copied_data,
        "copied_config_files": copied_config,
        "conflicts_skipped": len(conflicts),
        "master_key_sha256": key_after,
        "old_data_retained": True,
    }
    _write_marker(target, marker)
    log(f"[migrate] 完成：{json.dumps(after, ensure_ascii=False)}，"
        f"路径改写 {rewritten} 处；旧目录保持原样")
    return marker


def _merge_missing(user, template):
    """模板只补缺失键；用户已有的任何值（包括空字符串）都优先。"""
    if not isinstance(user, dict) or not isinstance(template, dict):
        return user
    for key, value in template.items():
        if key not in user:
            user[key] = value
        elif isinstance(user[key], dict) and isinstance(value, dict):
            _merge_missing(user[key], value)
    return user


def seed_config(template_dir: str, config_dir: str, *, log=print) -> dict:
    """把只读默认配置补进用户配置目录，不覆盖已有配置和任何密钥。"""
    template_dir = os.path.abspath(template_dir)
    config_dir = os.path.abspath(config_dir)
    created: list[str] = []
    merged: list[str] = []
    if not os.path.isdir(template_dir):
        return {"created": created, "merged": merged}
    os.makedirs(config_dir, exist_ok=True)
    for current, dirs, files in os.walk(template_dir):
        rel = os.path.relpath(current, template_dir)
        target_dir = config_dir if rel == "." else os.path.join(config_dir, rel)
        os.makedirs(target_dir, exist_ok=True)
        dirs[:] = [d for d in dirs if d not in ("__pycache__",)]
        for name in files:
            if name in CONFIG_TEMPLATE_EXCLUDE or name.endswith((".pyc", ".pyo")):
                continue
            source = os.path.join(current, name)
            destination = os.path.join(target_dir, name)
            relname = os.path.relpath(destination, config_dir)
            if not os.path.exists(destination):
                shutil.copy2(source, destination)
                created.append(relname)
                continue
            if name.lower().endswith(".json"):
                try:
                    with open(source, encoding="utf-8") as fh:
                        default = json.load(fh)
                    with open(destination, encoding="utf-8") as fh:
                        existing = json.load(fh)
                    if not isinstance(existing, dict) or not isinstance(default, dict):
                        continue
                    before = json.dumps(existing, sort_keys=True, ensure_ascii=False)
                    _merge_missing(existing, default)
                    after = json.dumps(existing, sort_keys=True, ensure_ascii=False)
                    if after != before:
                        tmp = destination + ".tmp"
                        with open(tmp, "w", encoding="utf-8") as fh:
                            json.dump(existing, fh, ensure_ascii=False, indent=2)
                            fh.write("\n")
                        os.replace(tmp, destination)
                        merged.append(relname)
                except (OSError, ValueError):
                    continue
    if created or merged:
        log(f"[config] 默认配置补齐：新增 {len(created)}，补键 {len(merged)}"
            "（已有值未改）")
    return {"created": created, "merged": merged}


def candidate_legacy_roots(program_base: str, executable_dir: str) -> list[str]:
    """常见的旧版数据位置；只返回确有 workbench.db 的目录。

    v2.2.0 新增 `seed/`：安装包里的**演示数据种子**改放到 `<安装目录>/seed/`，
    安装目录从此不再夹带"看起来像用户数据"的东西。首次运行时它仍然算
    一个 legacy 根 —— 于是"装完就有演示数据、且数据落在用户目录"的行为不变。
    """
    base = os.path.abspath(program_base)
    exe_dir = os.path.abspath(executable_dir)
    candidates = [
        base,
        exe_dir,
        os.path.join(base, "_internal"),
        os.path.join(exe_dir, "_internal"),
        os.path.join(exe_dir, "HRAgent工作台", "_internal"),
        os.path.join(exe_dir, "HRAgent数据"),
        # 种子目录（新）：<安装目录>/seed/{data,config}
        os.path.join(base, "seed"),
        os.path.join(exe_dir, "seed"),
    ]
    found: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate in seen or not os.path.isdir(candidate):
            continue
        seen.add(candidate)
        if layout(candidate):
            found.append(candidate)
    return found


def atomic_write_json(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = f"{path}.{uuid.uuid4().hex}.tmp"
    with open(temporary, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")
    os.replace(temporary, path)
