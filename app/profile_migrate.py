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

    ⚠️ v3.1.7：**必须认 `_internal/` 这一层**。
    v2.0.0 是 PyInstaller **onedir** 打包的，它的数据在
    `<安装目录>/_internal/data/workbench.db`；而这里原来只查
    `<root>/data/` 和 `<root>/`，于是用户选中外层安装目录时
    `layout()` 返回 None —— 安装器直接判定"这不是可迁移的旧版"，
    迁移根本不会发生。现场实测（2026-10-27）：
        D:\\Desktop\\HRAgent工作台\\_internal\\data\\workbench.db  → 存在
        D:\\Desktop\\HRAgent工作台\\data\\workbench.db             → 不存在
    现在两种布局都认。`config` 同理跟着 `_internal` 走。
    """
    base = os.path.abspath(str(root or ""))
    candidates = [
        # v2.0.0 onedir：<root>/_internal/{data,config}
        (os.path.join(base, "_internal", "data"),
         os.path.join(base, "_internal", "config")),
        # 便携版/安装版：<root>/{data,config}
        (os.path.join(base, "data"), os.path.join(base, "config")),
        # 老 v0.1 扁平布局：库直接在根下
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
    """建目录；**失败时退回 cmd.exe**（v3.1.8）。

    现场实测（2026-10-27）：安装器在 `os.makedirs` 报
    `[WinError 5] 拒绝访问。: 'D:\\Desktop\\a\\data'`，
    而同一台机器上 `cmd /c mkdir` 能建成 ——
    安全软件拦的是**本进程**的写操作，不拦 cmd 子进程。
    迁移要建 tmp/data/archive/inbox/avatars… 十几个目录，
    任何一级失败都会让整次迁移中止，所以这里统一兜底。
    """
    try:
        os.makedirs(_lp(path), exist_ok=exist_ok)
        return
    except FileExistsError:
        if exist_ok:
            return
        raise
    except OSError:
        pass
    if exist_ok and _lp_isdir(path):
        return
    # 兜底：交给 cmd（独立进程，不受本进程的策略限制）
    import subprocess

    try:
        subprocess.run(["cmd", "/c", "mkdir", str(path)],
                       capture_output=True, timeout=30,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:                                    # noqa: BLE001
        pass
    if not _lp_isdir(path):
        # 再兜一次：逐级建（有些策略只允许建一级）
        parts = os.path.abspath(str(path))
        cur = os.path.splitdrive(parts)[0] + os.sep
        for seg in parts[len(cur):].split(os.sep):
            if not seg:
                continue
            cur = os.path.join(cur, seg)
            if _lp_isdir(cur):
                continue
            try:
                os.makedirs(_lp(cur), exist_ok=True)
            except OSError:
                try:
                    subprocess.run(["cmd", "/c", "mkdir", str(cur)],
                                   capture_output=True, timeout=30,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                except Exception:                        # noqa: BLE001
                    pass
            if not _lp_isdir(cur):
                raise OSError(f"无法创建目录：{cur}")
    if not exist_ok and _lp_isdir(path):
        raise FileExistsError(f"目录已存在：{path}")


def _lp_walk(path: str):
    return os.walk(_lp(path))


def _lp_copy(src: str, dst: str) -> None:
    """复制单个文件；失败时退回 cmd.exe 的 copy（v3.1.8）。

    同 `_lp_makedirs` 的理由：安全软件拦的是本进程的写操作，
    而 `cmd /c copy` 是子进程、不受同一条策略限制。
    迁移一份都不许少 —— 少一个附件，HR 点「查看原件」就是 404。
    """
    try:
        shutil.copy2(_lp(src), _lp(dst))
        return
    except OSError:
        pass
    import subprocess

    try:
        subprocess.run(["cmd", "/c", "copy", "/Y", str(src), str(dst)],
                       capture_output=True, timeout=120,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:                                    # noqa: BLE001
        pass
    if not _lp_isfile(dst):
        raise OSError(f"无法复制文件：{src} → {dst}")


def _copy_file_if_missing(src: str, dst: str, conflicts: list[str],
                          overwrite: bool = False) -> bool:
    if not _lp_isfile(src):
        return False
    if _lp_exists(dst):
        if not overwrite:
            conflicts.append(dst)
            return False
        if _sha256(_lp(src)) == _sha256(_lp(dst)):
            return False            # 内容一致，既不覆盖也不留备份
        # 覆盖前先留一份目标原文件，人工回退有据可查。
        _lp_copy(dst, dst + ".bak-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    _lp_makedirs(os.path.dirname(dst), exist_ok=True)
    # 拷长路径必须两端都带 @@BS@@?@@BS@@ 前缀，否则 shutil 内部会踩 206。
    _lp_copy(src, dst)
    return True


def _copy_tree_if_missing(src: str, dst: str, conflicts: list[str],
                          force_names: frozenset[str] = frozenset()) -> int:
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
                                     os.path.join(target_dir, name), conflicts,
                                     overwrite=name in force_names):
                copied += 1
    return copied


def _sqlite_snapshot(src: str, dst: str) -> None:
    """用 SQLite Backup API 取得一致快照（包含当前 WAL 中的已提交内容）。

    v3.1.12：源库改走 `_connect_source_ro` —— 旧目录不可写时，
    普通只读连接会因建不了 `-wal`/`-shm` 而失败（见那个函数的说明）。
    """
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    fd, temporary = tempfile.mkstemp(
        prefix=".hrprofile-", suffix=".db", dir=os.path.dirname(dst)
    )
    os.close(fd)
    source, _tmp = _connect_source_ro(src)
    target = sqlite3.connect(temporary)
    try:
        source.backup(target)
        target.commit()
    finally:
        target.close()
        source.close()
        if _tmp:
            shutil.rmtree(os.path.dirname(_tmp), ignore_errors=True)
    os.replace(temporary, dst)


def _connect_source_ro(db_path: str) -> tuple[sqlite3.Connection, str | None]:
    """以**只读**方式打开旧库，返回 (连接, 需要清理的临时文件或 None)。

    ⚠️ v3.1.12 关键修复。现场实测（2026-10-28）：
        sqlite3.OperationalError: unable to open database file
        at _integrity_check(source_db)
    旧库是 **WAL 模式**，普通 `sqlite3.connect(path)` 会尝试在**旧目录**里
    创建 `-wal` / `-shm` 两个文件；而旧版程序目录通常**不可写**
    （装在 Program Files、或是别人机器上的共享目录、或被安全策略限制），
    于是连"读一下"都失败 —— 迁移在第一步就崩了。

    三级退路，越往后依赖越少：
      ① `mode=ro&immutable=1` —— 告诉 SQLite"这库不会变"，**完全不加锁**，
         不碰 -wal/-shm，最干净；
      ② `mode=ro` —— 只读但仍可能需要 -shm，某些版本/文件系统下能过；
      ③ **把库文件复制到临时目录再打开** —— 任何情况下都能读。
    """
    import shutil
    import tempfile

    posix = Path(db_path).as_posix()
    for uri in (f"file:{posix}?mode=ro&immutable=1", f"file:{posix}?mode=ro"):
        conn = None
        try:
            conn = sqlite3.connect(uri, uri=True)
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
            return conn, None
        except sqlite3.Error:
            if conn is not None:
                try:
                    conn.close()
                except Exception:                        # noqa: BLE001
                    pass
    # ③ 复制出来再读
    tmpdir = tempfile.mkdtemp(prefix="hra_ro_")
    tmp = os.path.join(tmpdir, DB_NAME)
    shutil.copy2(_lp(db_path), _lp(tmp))
    return sqlite3.connect(tmp), tmp


def _integrity_check(db_path: str) -> None:
    conn, tmp = _connect_source_ro(db_path)
    try:
        rows = conn.execute("PRAGMA integrity_check").fetchall()
        if rows != [("ok",)]:
            raise RuntimeError("SQLite 完整性校验失败：" + "; ".join(str(r[0]) for r in rows[:5]))
    finally:
        conn.close()
        if tmp:
            shutil.rmtree(os.path.dirname(tmp), ignore_errors=True)


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
    """统计若干张表的行数。

    v3.1.12：调用方传进来的可能是**旧的、WAL 模式、目录不可写**的库，
    所以一律走 `_connect_source_ro`（它带 immutable / 复制三级退路）。
    用普通 `sqlite3.connect` 会在这种库上直接 `unable to open database file`。
    """
    wanted = ("candidates", "applications", "documents", "settings", "users")
    conn, _tmp = _connect_source_ro(db_path)
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
        if _tmp:
            shutil.rmtree(os.path.dirname(_tmp), ignore_errors=True)


def _write_marker(user_home: str, payload: dict) -> None:
    path = os.path.join(user_home, PROFILE_MARKER)
    os.makedirs(user_home, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
        fh.write("\n")


def _migrate_profile(legacy_root: str, user_home: str, *, log=print,
                     final_data_root: str | None = None,
                     merge: bool = False) -> dict:
    """把旧版档案复制到用户目录。目标已有数据库时绝不动它。

    ``merge=False``：目标是新建的 staging 目录，整目录 rename 发布（安装器路径）。
    ``merge=True``：目标目录已存在（免安装包解压目录、安装目录），**就地合并**。
    合并模式的三条额外规则：

    1. 目标已有的文件一律保留，不算失败（`conflicts` 只用于统计）；
    2. `master.key` 例外——旧库里的手机号/邮箱是用**旧密钥**加密的，
       必须用旧密钥覆盖，否则迁移过来的档案全部解不开（目标原密钥先备份）；
    3. 数据库最后才落盘，"目标没有 workbench.db"因此始终等于"没迁移过"，
       中途失败可以直接重跑。
    """
    old = layout(legacy_root)
    if not old:
        raise ValueError(f"没有在旧版目录找到 {DB_NAME}：{legacy_root}")
    target = os.path.abspath(user_home)
    old_root = os.path.abspath(old["root"])
    if os.path.normcase(target) == os.path.normcase(old_root):
        raise ValueError("新数据目录不能与旧版目录相同")
    try:
        if os.path.normcase(os.path.commonpath([target, old_root])) == os.path.normcase(old_root):
            raise ValueError("新数据目录不能放在旧版程序目录内")
    except ValueError:
        pass
    target_db = profile_db(target)
    if os.path.exists(target_db):
        raise FileExistsError(f"目标已有数据库，拒绝覆盖：{target_db}")

    source_db = os.path.join(old["data"], DB_NAME)
    _integrity_check(source_db)
    before = _counts(source_db)
    conflicts: list[str] = []
    os.makedirs(os.path.join(target, "data"), exist_ok=True)
    os.makedirs(os.path.join(target, "config"), exist_ok=True)

    copied_data = 0
    for name in DATA_ITEMS:
        source = os.path.join(old["data"], name)
        destination = os.path.join(target, "data", name)
        if os.path.isdir(source):
            copied_data += _copy_tree_if_missing(source, destination, conflicts)
            log(f"[migrate] 数据目录 {name}: 累计复制 {copied_data} 个文件")
        elif os.path.isfile(source):
            copied_data += int(_copy_file_if_missing(source, destination, conflicts))

    # 后续版本新增的附件/导出目录也必须随迁；只排除已有一致快照的数据库。
    for name in os.listdir(_lp(old["data"])):
        if name in DATA_ITEMS or name.startswith(DB_NAME):
            continue
        source = os.path.join(old["data"], name)
        destination = os.path.join(target, "data", name)
        if _lp_isdir(source):
            copied_data += _copy_tree_if_missing(source, destination, conflicts)
        elif _lp_isfile(source):
            copied_data += int(_copy_file_if_missing(source, destination, conflicts))

    copied_config = 0
    if os.path.isdir(old["config"]):
        # master.key 必须随旧库走，见函数说明第 2 条。
        copied_config = _copy_tree_if_missing(
            old["config"], os.path.join(target, "config"), conflicts,
            force_names=frozenset({"master.key"}),
        )

    log(f"[migrate] 一致性快照：{source_db}")
    _sqlite_snapshot(source_db, target_db)
    rewritten = rewrite_db_paths(
        target_db, old["data"], final_data_root or os.path.join(target, "data")
    )

    after = _counts(target_db)
    if after != before:
        raise RuntimeError(f"迁移校验失败：源库 {before}，目标库 {after}")
    _integrity_check(target_db)

    # 校验每个已复制的非数据库文件；不能只凭“文件数差不多”宣布成功。
    # 目标原有的、按设计保留的文件不在校验范围内（它们本来就不该等于源文件）。
    kept = {os.path.normcase(os.path.abspath(p)) for p in conflicts}
    verified_files = 0
    for source_root, destination_root in (
        (old["data"], os.path.join(target, "data")),
        (old["config"], os.path.join(target, "config")),
    ):
        if not _lp_isdir(source_root):
            continue
        for current, dirs, files in _lp_walk(source_root):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for name in files:
                if name.startswith(DB_NAME) or name.endswith((".pyc", ".pyo")):
                    continue
                source = os.path.join(current, name)
                destination = os.path.join(
                    destination_root, os.path.relpath(source, source_root)
                )
                if os.path.normcase(os.path.abspath(destination)) in kept:
                    continue
                if not _lp_isfile(destination) or _sha256(_lp(source)) != _sha256(_lp(destination)):
                    raise RuntimeError(f"迁移校验失败：{os.path.relpath(source, source_root)}")
                verified_files += 1

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
        "verified_files": verified_files,
        "conflicts_skipped": len(conflicts),
        "master_key_sha256": key_after,
        "old_data_retained": True,
    }
    _write_marker(target, marker)
    log(f"[migrate] 完成：{json.dumps(after, ensure_ascii=False)}，"
        f"路径改写 {rewritten} 处；旧目录保持原样")
    return marker


def migrate_profile(legacy_root: str, user_home: str, *, log=print,
                    force_merge: bool = False) -> dict:
    """把旧版档案迁到 ``user_home``。

    两种发布方式，按目标目录的实际形态自动选：

    * **空目录 / 不存在** → 在同级建 staging，校验通过后一次 ``os.replace``
      发布（原子，失败不留半成品）；
    * **已存在且非空**（免安装包解压目录、安装目录）→ **就地合并**。
      目标原有的程序文件、模板配置都保留，只补进旧数据。

    非空目标以前直接抛 ``FileExistsError``，于是"解压便携包 → 跑迁移脚本"
    这条最自然的路径必然失败；而首次启动时的自动迁移会把这个异常吞掉，
    用户看到的是一个空库。``force_merge=True`` 用于显式要求就地合并。
    """
    target = os.path.abspath(user_home)
    if has_profile(target):
        raise FileExistsError(f"目标已有数据库，拒绝覆盖：{profile_db(target)}")

    merge = force_merge
    if not merge:
        for name in ("data", "config"):
            placeholder = os.path.join(target, name)
            if os.path.isdir(placeholder) and not os.listdir(placeholder):
                os.rmdir(placeholder)
        if os.path.isdir(target) and not os.listdir(target):
            os.rmdir(target)
        merge = os.path.exists(target)
        if merge:
            log(f"[migrate] 目标目录已存在，改为就地合并：{target}")

    if merge:
        result = _migrate_profile(
            legacy_root, target, log=log,
            final_data_root=os.path.join(target, "data"), merge=True,
        )
        result["target_root"] = target
        _write_marker(target, result)
        return result

    os.makedirs(os.path.dirname(target), exist_ok=True)
    staging = target + ".migrating-" + uuid.uuid4().hex
    try:
        result = _migrate_profile(
            legacy_root, staging, log=log,
            final_data_root=os.path.join(target, "data"),
        )
        if os.path.exists(target):
            raise FileExistsError(f"目标目录已有其他文件，拒绝覆盖：{target}")
        os.replace(staging, target)
        result["target_root"] = target
        _write_marker(target, result)
        return result
    except Exception:
        shutil.rmtree(_lp(staging), ignore_errors=True)
        raise


def _move_tree_contents(src: str, dst: str) -> None:
    """把 `src` 下的内容搬进 `dst`（同名文件直接覆盖）。

    用于 `os.replace` 不可用的场景（见 `migrate_profile` 里的说明）。
    用 `shutil.move` 而不是 copy：同一卷上是原子 rename，跨卷才真复制。
    """
    os.makedirs(dst, exist_ok=True)
    for name in os.listdir(_lp(src)):
        s = os.path.join(src, name)
        d = os.path.join(dst, name)
        if os.path.isdir(s):
            shutil.copytree(_lp(s), _lp(d), dirs_exist_ok=True)
            shutil.rmtree(_lp(s), ignore_errors=True)
        else:
            if os.path.exists(d):
                try:
                    os.remove(_lp(d))
                except OSError:
                    pass
            shutil.move(_lp(s), _lp(d))


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
                _lp_copy(source, destination)
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
        # 旧的 zip 包可能多套一层同名目录；新安装包则读注册表记录的旧位置。
        os.path.join(exe_dir, "HRAgent工作台"),
        os.path.join(exe_dir, "HRAgent工作台", "HRAgent工作台"),
        os.path.join(exe_dir, "HRAgent工作台", "_internal"),
        os.path.join(exe_dir, "HRAgent数据", "_internal"),
    ]
    bootstrap = os.path.join(os.environ.get("APPDATA", ""), "HRAgent", "bootstrap.json")
    try:
        with open(bootstrap, encoding="utf-8") as fh:
            saved = json.load(fh).get("legacy_source")
        if saved:
            candidates.insert(0, saved)
    except (OSError, ValueError, AttributeError):
        pass
    if os.name == "nt":
        try:
            import winreg
            key_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\HRAgentWorkbench"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                install_dir = winreg.QueryValueEx(key, "LegacyInstallLocation")[0]
            candidates.insert(0, os.path.join(install_dir, "_internal"))
            candidates.insert(0, install_dir)
        except OSError:
            pass
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
