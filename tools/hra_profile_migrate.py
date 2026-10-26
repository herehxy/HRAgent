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
    """识别一个旧版根目录的数据/配置布局。

    候选布局（按可信度）：
      1. `<root>/data` + `<root>/config`   —— 源码版 / 免安装版根目录
      2. `<root>/_internal/data` + `<root>/_internal/config` —— **v2.0.0 及更早**
         的分发形态：解压即用，库与原件在 `_internal/` 里。
      3. `<root>` 本身是数据目录（库里就放在根上）

    ⚠️ 候选 2 是实测踩出来的：2.0.0 的用户手里是一个解压出来的文件夹，
    里面是 `HRAgent工作台/_internal/data/workbench.db`。安装器让用户手选旧目录时，
    用户选的是**外层**（`HRAgent工作台`），而旧版 layout() 只认 `<root>/data` ——
    于是迁移直接报"没有在旧版目录找到 workbench.db"，用户以为数据丢了。
    """
    base = os.path.abspath(str(root or ""))
    candidates = [
        (os.path.join(base, "data"), os.path.join(base, "config")),
        (os.path.join(base, "_internal", "data"),
         os.path.join(base, "_internal", "config")),
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


#: 补搬原件时只动这几类 —— 简历原件、证件照。不碰库、不碰配置、
#: 不搬 inbox/outbox（邮件正文与附件可能有几百 MB，且库里没它们的索引意义）。
SUPPLEMENT_ITEMS = ("archive", "avatars")


def supplement_originals(legacy_root: str, user_home: str, *,
                         log=print) -> dict:
    """把旧目录里**用户目录还没有的**简历原件补过来。库与配置一律不动。

    为什么需要它（真实场景）：
      `migrate_profile()` 在目标已有数据库时直接 `FileExistsError`（拒绝覆盖），
      这是对的 —— 不能拿旧库盖掉用户正在用的库。但那条规则会连带把**原件**
      也挡在门外：老用户先装过 v2.x（用户目录有库），后来又回了 2.0.0 解压版
      用了一段时间（新简历原件落在 `_internal/data/archive`），再升级时
      库不迁 → 原件也不搬 → 界面表现是「列表里有人，但点开取不到原件」。

      更严重的后果是**没法重新分析**：分析要读简历原文，原件不在就等于
      这几十个人再也跑不了模型。所以这条补搬链必须存在。

    语义：
      · 只复制「目标不存在」的文件（不覆盖任何已有原件）
      · 只动 SUPPLEMENT_ITEMS 这几类目录
      · 不复制 master.key —— 两个环境的密钥可能不同，混进来反而解不开
      · **跳过种子目录**（`_internal/seed`）：那是演示数据，
        补进用户的真实库里会出现"凭空多出几个人"的错觉
    """
    result = {"copied": 0, "root": None, "skipped_seed": False}
    root = os.path.abspath(str(legacy_root or ""))
    if not root or not os.path.isdir(root):
        return result
    if "seed" in [p.lower() for p in os.path.normpath(root).split(os.sep)]:
        result["skipped_seed"] = True
        return result
    old = layout(root)
    if not old:
        return result

    target_data = os.path.join(os.path.abspath(user_home), "data")
    copied = 0
    conflicts: list[str] = []
    for name in SUPPLEMENT_ITEMS:
        src = os.path.join(old["data"], name)
        if not _lp_isdir(src):
            continue
        dst = os.path.join(target_data, name)
        copied += _copy_tree_if_missing(src, dst, conflicts)

    if copied:
        result["copied"] = copied
        result["root"] = root
        log(f"[supplement] 已有库不动，补搬缺失原件 {copied} 个：{root} → {target_data}")
    return result



# ---------------------------------------------------------------- 长路径（Windows）
#
# 这个文件会被**单独复制**成独立脚本（tools/hra_profile_migrate.py），
# 所以不能 import app.paths——长路径工具就地实现一份。
#
# 为什么迁移必须处理长路径：旧数据里存的是**绝对路径**（旧数据根 + 长文件名），
# 迁移到新数据根后整条路径会变长；附件原名又是 150+ 字符的那种，
# 两者一叠加就可能越过 Windows 的 260 字符上限——
# 拷贝直接报 WinError 206，于是"迁移完成了但原件没跟过来"，
# 界面点"在线查看简历"就是 404（文件其实在旧目录里）。

_PATH_BUDGET = 240


def _lp(path: str) -> str:
    """超长路径补 \\\\?\\ 前缀（Windows 扩展长路径）。"""
    if os.name != "nt" or not path:
        return path
    try:
        p = os.path.abspath(str(path))
    except (OSError, ValueError):
        p = os.path.normpath(os.path.join(os.getcwd(), str(path)))
    if p.startswith("\\\\?\\"):
        return p
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p[2:]
    if len(p) <= _PATH_BUDGET:
        return p
    return "\\\\?\\" + p


def _lp_isfile(path: str) -> bool:
    return os.path.isfile(_lp(path))


def _lp_isdir(path: str) -> bool:
    return os.path.isdir(_lp(path))


def _lp_exists(path: str) -> bool:
    return os.path.exists(_lp(path))


def _lp_makedirs(path: str, exist_ok: bool = True) -> None:
    os.makedirs(_lp(path), exist_ok=exist_ok)


def _lp_walk(path: str):
    return os.walk(_lp(path))


def _copy_file_if_missing(src: str, dst: str, conflicts: list[str]) -> bool:
    if not _lp_isfile(src):
        return False
    if _lp_exists(dst):
        conflicts.append(dst)
        return False
    _lp_makedirs(os.path.dirname(dst), exist_ok=True)
    # 拷长路径必须两端都带 @@BS@@?@@BS@@ 前缀，否则 shutil 内部会踩 206。
    shutil.copy2(_lp(src), _lp(dst))
    return True


def _copy_tree_if_missing(src: str, dst: str, conflicts: list[str]) -> int:
    copied = 0
    if not _lp_isdir(src):
        return copied
    for current, dirs, files in _lp_walk(src):
        rel = os.path.relpath(current, src)
        target_dir = dst if rel == "." else os.path.join(dst, rel)
        _lp_makedirs(target_dir, exist_ok=True)
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


def _is_under(path: str, root: str) -> bool:
    """`path` 是否在 `root` 之下（Windows 下大小写不敏感）。"""
    try:
        p = os.path.normcase(os.path.abspath(path))
        r = os.path.normcase(os.path.abspath(root))
    except (OSError, ValueError):
        return False
    return p == r or p.startswith(r + os.sep)


#: `mailbox.json` 里存目录的键。绝对路径落在这里 → 换机/换目录后必然失效。
CONFIG_DIR_KEYS = ("eml_dir", "folder_dir", "archive_dir")


def normalize_config_paths(config_dir: str, new_data: str,
                            old_data: str | None = None) -> list[str]:
    """把 `mailbox.json` 里指向**旧位置**的目录改成当前数据根下的同名子目录。

    为什么必须修（实测）：迁移只改写了**数据库**里的路径（`rewrite_db_paths`），
    但 `mailbox.json` 的 `eml_dir` / `folder_dir` / `archive_dir` 原样复制过来了。
    2.0.0 的用户如果把来源目录设成了绝对路径（界面上"保存路径"就写绝对路径），
    升级后这个值仍指向**旧安装目录**（甚至旧电脑）：
      · 来源目录取不到 → 「在线预览」全部 404，界面显示"目录不存在"；
      · 收信目录取不到 → 邮件收不到；
      · 旧目录一旦被删/换机，这些路径彻底失效。

    判定规则（宁可不动也不乱动）：
      · 路径在 `old_data` 之下 → 明确是旧数据根，改成新数据根下的同名子目录；
      · 路径**已存在**且不在 old_data 之下 → 是用户自己指定的位置（外接盘等），
        原样保留；
      · 路径**不存在**但含 `/data/` 段 → 是旧机器留下的残骸，改成新数据根下的
        同名子目录（对应库内 `normalize_existing_data_paths` 的口径）。

    返回被改动的说明列表（每条一句人话），便于在日志/界面上说清楚。
    """
    import json

    notes: list[str] = []
    path = os.path.join(os.path.abspath(config_dir), "mailbox.json")
    if not os.path.isfile(path):
        return notes
    try:
        with open(path, encoding="utf-8") as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        return notes
    if not isinstance(cfg, dict):
        return notes

    new_data = os.path.abspath(new_data)
    changed = False
    for key in CONFIG_DIR_KEYS:
        value = cfg.get(key)
        if not isinstance(value, str) or not value.strip():
            continue
        value = value.strip()
        if not os.path.isabs(value):
            continue                       # 相对路径（data/xxx）本来就跟着数据根走
        if _is_under(value, new_data):
            continue                       # 已经指向当前数据根
        replacement = None
        if old_data and _is_under(value, os.path.abspath(old_data)):
            suffix = os.path.relpath(value, os.path.abspath(old_data))
            replacement = os.path.join(new_data, suffix)
        elif not _lp_isdir(value):
            marker = value.replace("\\", "/").lower().rfind("/data/")
            if marker >= 0:
                suffix = value.replace("\\", "/")[marker + 6:]
                replacement = os.path.join(new_data, suffix.replace("/", os.sep))
        if replacement is None:
            continue
        cfg[key] = replacement
        changed = True
        notes.append(f"{key}：{value} → {replacement}")

    if changed:
        backup = path + ".bak_pathfix-" + datetime.now().strftime("%Y%m%d-%H%M%S")
        try:
            shutil.copy2(path, backup)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh, ensure_ascii=False, indent=2)
                fh.write("\n")
            os.replace(tmp, path)
        except OSError:
            return []
        notes.append(f"（原文件已备份：{os.path.basename(backup)}）")
    return notes


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

    # 配置里的目录也要跟着搬家（库里的路径改了，mailbox.json 的没改 →
    # 来源目录仍指向旧位置，「在线预览」就全取不到）。
    config_notes = normalize_config_paths(
        os.path.join(target, "config"), os.path.join(target, "data"),
        old_data=old["data"])

    marker = {
        "schema": 1,
        "migrated_at": _now(),
        "source_root": os.path.abspath(legacy_root),
        "target_root": target,
        "counts": after,
        "rewritten_paths": rewritten,
        "rewritten_config_paths": config_notes,
        "copied_data_files": copied_data,
        "copied_config_files": copied_config,
        "conflicts_skipped": len(conflicts),
        "master_key_sha256": key_after,
        "old_data_retained": True,
    }
    _write_marker(target, marker)
    log(f"[migrate] 完成：{json.dumps(after, ensure_ascii=False)}，"
        f"路径改写 {rewritten} 处；旧目录保持原样")
    for note in config_notes:
        log(f"[migrate]   配置路径：{note}")
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
    if not _lp_isdir(template_dir):
        return {"created": created, "merged": merged}
    _lp_makedirs(config_dir, exist_ok=True)
    for current, dirs, files in _lp_walk(template_dir):
        rel = os.path.relpath(current, template_dir)
        target_dir = config_dir if rel == "." else os.path.join(config_dir, rel)
        _lp_makedirs(target_dir, exist_ok=True)
        dirs[:] = [d for d in dirs if d not in ("__pycache__",)]
        for name in files:
            if name in CONFIG_TEMPLATE_EXCLUDE or name.endswith((".pyc", ".pyo")):
                continue
            source = os.path.join(current, name)
            destination = os.path.join(target_dir, name)
            relname = os.path.relpath(destination, config_dir)
            if not _lp_exists(destination):
                shutil.copy2(_lp(source), _lp(destination))
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
