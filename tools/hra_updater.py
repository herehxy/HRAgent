"""
HRAgent 热更新器 —— 只换功能，不碰数据与配置。

**它到底更新什么**
    安装目录里的**程序文件**：`HRAgent工作台.exe` 与 `_internal/` 下的运行时文
    件（依赖、模型文件）。

**它绝不碰什么**
    `%LOCALAPPDATA%\\HRAgentData` 下的用户数据与配置（简历库、API Key、邮箱、
    档位阈值、头像）。这不是"我们记得不覆盖"，而是结构上做不到 ——
    更新器只往安装目录里写，而用户数据在安装目录之外。

**三道安全网**
    1. 路径白名单：更新包里出现用户数据目录 / data 段 / 库文件 → **报错拒绝**
       （不是静默跳过 —— 静默跳过会让人以为"更新了"，实际某个关键文件没换，
        这正是本项目反复踩的"假通过"）
    2. 写前备份：写入中途失败 → 自动回滚到 `_update_backup/<时间戳>/`
    3. 数据快照比对：更新前后各给用户数据目录做一次快照，不一致 → 回滚报错

命令行：HRAgent更新器.exe <更新包.zip> [--install-dir <目录>]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile
from pathlib import Path

APP_DIR_NAME = "HRAgent工作台"
EXE_NAME = "HRAgent工作台.exe"

#: 更新包里绝对不许出现的东西（相对路径任一段命中即中止）
FORBIDDEN_SEGMENTS = ("data",)
#: 库/密钥类文件：全局禁写。
#: ⚠️ **`.secret` 不在这里**（2026-10-22 修）：`_internal/config/imap.secret`
#: 是安装器分发的**配置模板**，属程序资源，必须能被更新器替换；
#: 早先把它列进全局禁后缀 → 更新包必被拒（"更新包试图改写库文件"），
#: 热更新链路直接断掉。真正的用户 `.secret` 由 `is_user_data_path()` 拦。
FORBIDDEN_SUFFIX = (".db", ".key", ".sqlite", ".sqlite3")


def _seg(rel: str) -> list[str]:
    """把相对路径拆成路径段。"""
    return rel.replace("\\", "/").split("/")


def is_program_data_dir(rel: str) -> bool:
    """是不是"程序自己的数据目录"（顶层 data/ 或 _internal/data/）。

    只按**深度**判：data 段出现在第 0 或第 1 位才算（`data/x`、`_internal/data/x`）；
    更深的位置（`_internal/cv2/data/x`）是第三方包自带的，必须放行。
    """
    segs = _seg(rel)
    for i, s in enumerate(segs):
        if s.lower() == "data" and i <= 1:
            return True
    return False


def in_config_dir(rel: str) -> bool:
    """路径里含 config / config_template **段** → 配置目录（模板），可写。

    ⚠️ 按路径段判，不按前缀：程序目录是 `_internal/`，配置真实路径是
    `_internal/config/xxx`。按前缀判会把合法配置误杀（踩过两次的坑）。
    """
    segs = [s.lower() for s in _seg(rel)]
    return "config" in segs or "config_template" in segs


def is_seed_path(rel: str) -> bool:
    """种子目录算程序资源，可写。

    ⚠️ 必须按**路径段**判，不能按前缀判：实际路径是
    `_internal/seed/data/workbench.db`（种子在 _internal 里面），
    按 "seed/" 前缀判会把合法种子误杀 —— 我第一版就是这么写的，
    更新包生成被自己的闸门拦下来才发现。
    """
    segs = [s.lower() for s in _seg(rel)]
    return "seed" in segs


def user_home_dir() -> str:
    """用户数据根：%LOCALAPPDATA%\\HRAgentData。"""
    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(local, "HRAgentData")


def _log_path() -> str:
    """日志落点：exe 同级（被打包后同级目录可能只读，退回用户目录）。"""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(os.path.abspath(sys.executable))
    else:
        base = os.path.dirname(os.path.abspath(__file__))
    try:
        os.makedirs(base, exist_ok=True)
        probe = os.path.join(base, ".hra_log_probe")
        with open(probe, "w") as fh:
            fh.write("x")
        os.remove(probe)
    except OSError:
        base = user_home_dir()
        os.makedirs(base, exist_ok=True)
    return os.path.join(base, "更新日志.log")


def make_logger():
    """控制台 + 文件双写。

    打包成 GUI 程序（console=False）后 print 没人看得到 ——
    出错时用户只会说"更新失败了"，没日志就无从查起。
    """
    log_path = _log_path()

    def log(message: str = "") -> None:
        line = f"[{time.strftime('%H:%M:%S')}] {message}"
        try:
            print(line)
        except Exception:
            pass
        try:
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError:
            pass

    return log


LOG = make_logger()


def is_user_data_path(path: str) -> bool:
    """这个绝对路径是否落在用户数据目录里（落在里面就绝不能写）。"""
    try:
        target = os.path.abspath(path)
        root = os.path.abspath(user_home_dir())
        return os.path.commonpath([target, root]) == root
    except (OSError, ValueError):
        return False


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def snapshot_dir(root: str) -> dict:
    """给一个目录做快照：相对路径 -> (大小, mtime_ns, sha256 或 '')。

    sha256 只对 ≤32MB 的文件算（库文件几十 MB 够用；大文件只记大小+mtime，
    避免拖慢更新）。快照用来证明"更新过程一个字节都没动用户数据"。
    """
    out: dict = {}
    if not os.path.isdir(root):
        return out
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in filenames:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root).replace("\\", "/")
            try:
                st = os.stat(full)
            except OSError:
                continue
            digest = ""
            if st.st_size <= 32 * 1024 * 1024:
                try:
                    digest = sha256_file(full)
                except OSError:
                    digest = ""
            out[rel] = (st.st_size, st.st_mtime_ns, digest)
    return out


def diff_snapshots(before: dict, after: dict) -> list[str]:
    """返回"变了"的相对路径列表（数据目录被动过 = 事故）。"""
    changed: list[str] = []
    for rel, meta in after.items():
        if rel not in before:
            changed.append(f"{rel}（新增）")
        elif before[rel] != meta:
            changed.append(f"{rel}（已修改）")
    for rel in before:
        if rel not in after:
            changed.append(f"{rel}（被删除）")
    return changed


def app_running(install_dir: str) -> list[str]:
    """**目标安装目录**里的程序是否在跑。返回进程路径列表。

    ⚠️ 必须按**路径**判断，不能按进程名：按进程名会把"另一套安装"也当成
    占用 —— 同一台机器上装了两份时（开发目录 + D 盘正式版），更新其中一份
    会被另一份无辜拦住。tasklist 不给路径，所以借 PowerShell 的 Get-Process
    拿 ExecutablePath。
    """
    target = os.path.abspath(install_dir).lower()
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-Process | Where-Object { $_.Path -like '*HRAgent*' -or "
             "$_.ProcessName -like '*HRAgent*' } | ForEach-Object "
             "{ try { $_.Path } catch { '' } }"],
            capture_output=True, timeout=20,
        )
        raw = r.stdout.decode("utf-8", "ignore")
    except Exception:
        return []
    out: list[str] = []
    for line in raw.splitlines():
        p = line.strip()
        if not p:
            continue
        if p.lower().startswith(target):
            out.append(p)
    return out


class UpdateError(Exception):
    """更新过程中的可预期错误（会展示给用户，不是崩溃）。"""


def read_manifest(zf: zipfile.ZipFile) -> dict:
    """读更新包里的 update.json。"""
    try:
        raw = zf.read("update.json")
    except KeyError:
        raise UpdateError("更新包里没有 update.json —— 这不是合法的更新包")
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError as e:
        raise UpdateError(f"update.json 解析失败：{e}")


def validate_targets(install_dir: str, manifest: dict) -> list[str]:
    """按路径规则挑出**允许写入**的目标；命中禁区就报错（不是跳过，是报错）。

    报错而不是静默跳过：静默跳过会让人以为"更新了"，
    实际某个关键文件没换（这正是本项目反复踩的"假通过"）。
    """
    targets: list[str] = []
    for item in manifest.get("files", []):
        rel = str(item.get("path", "")).replace("\\", "/").lstrip("/")
        if not rel:
            continue
        if ".." in _seg(rel):
            raise UpdateError(f"更新包里有越界路径：{rel}")
        dest = os.path.abspath(os.path.join(install_dir, rel))
        if is_user_data_path(dest):
            raise UpdateError(f"更新包试图改写用户数据目录，已拒绝：{rel}")
        if is_program_data_dir(rel):
            raise UpdateError(f"更新包试图改写数据目录，已拒绝：{rel}")
        # 种子目录（_internal/seed/data/…）是**程序资源**，不是用户数据 ——
        # 演示库 workbench.db 就住在这儿，必须允许替换，否则更新链路对
        # "随包分发演示数据" 的版本直接死锁（2026-10-22 实测）。
        # ⚠️ 豁免**只针对演示库与演示简历**（.db/.sqlite + 简历原件），不能整目录
        # 放行：`_internal/seed/master.key` 若跟着放行，就等于允许更新包
        # 替换密钥文件（实测边界表里出现过"放行"）。
        low0 = rel.lower()
        if is_seed_path(rel) and low0.endswith(
                (".db", ".sqlite", ".sqlite3", ".pdf", ".doc", ".docx",
                 ".txt", ".md", ".xls", ".xlsx", ".jpg", ".jpeg", ".png")):
            targets.append(rel)
            continue
        low = rel.lower()
        if low.endswith(FORBIDDEN_SUFFIX):
            raise UpdateError(f"更新包试图改写库文件，已拒绝：{rel}")
        if os.path.basename(low) == "master.key":
            raise UpdateError(f"更新包试图改写库文件，已拒绝：{rel}")
        targets.append(rel)
    return targets


def _rollback(install_dir: str, backup_dir: str, log) -> None:
    """把备份里的文件拷回去。"""
    if not os.path.isdir(backup_dir):
        return
    count = 0
    for dirpath, _dirnames, filenames in os.walk(backup_dir):
        for name in filenames:
            src = os.path.join(dirpath, name)
            rel = os.path.relpath(src, backup_dir)
            dest = os.path.join(install_dir, rel)
            try:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.copy2(src, dest)
                count += 1
            except OSError:
                continue
    log(f"[回滚] 已还原 {count} 个文件")


def apply_update(install_dir: str, package: str, log=LOG) -> dict:
    """执行更新。返回结果字典；失败会抛 UpdateError。"""
    install_dir = os.path.abspath(install_dir)
    if not os.path.isdir(install_dir):
        raise UpdateError(f"安装目录不存在：{install_dir}")

    exe_path = os.path.join(install_dir, EXE_NAME)
    if not os.path.isfile(exe_path):
        internal = os.path.join(install_dir, "_internal")
        hint = internal if os.path.isdir(internal) else install_dir
        raise UpdateError(
            f"选中的目录里没有 {EXE_NAME}：\n  {install_dir}"
            f"\n\n「安装目录」要选**直接放着 {EXE_NAME} 的那一层**，"
            f"不是它的上级，也不是 _internal 里面。"
            f"\n\n你是不是想选这个：\n  {hint}"
        )

    running = app_running(install_dir)
    if running:
        raise UpdateError(
            "程序还在运行（" + "、".join(running) + "）。\n"
            "请先在托盘图标上右键「退出」，再运行更新器。"
        )

    # 安装目录里若还留着旧数据，提醒（新版首次启动会自动迁移）
    legacy_db = os.path.join(install_dir, "data", "workbench.db")
    if os.path.isfile(legacy_db):
        try:
            import sqlite3
            con = sqlite3.connect(f"file:{legacy_db}?mode=ro", uri=True)
            n = con.execute("SELECT count(*) FROM candidates").fetchone()[0]
            con.close()
            log(f"      [i] 安装目录里还有一份旧数据（{n} 人）：{legacy_db}")
            log("          新版首次启动会自动迁到用户目录；"
                "若你已迁过，这条可忽略")
        except Exception:
            pass

    # [1] 用户数据快照
    before = snapshot_dir(user_home_dir())
    log(f"[1/6] 用户数据快照：{len(before)} 个文件（{user_home_dir()}）")

    try:
        zf = zipfile.ZipFile(package)
    except (OSError, zipfile.BadZipFile) as e:
        raise UpdateError(f"打不开更新包：{package}\n{e}")

    with zf:
        manifest = read_manifest(zf)
        version = str(manifest.get("version", "?"))
        files = manifest.get("files", [])
        log(f"[2/6] 更新包：v{version}，{len(files)} 个文件待更新")

        targets = validate_targets(install_dir, manifest)

        # [3] 校验包内文件 sha256（先验再写）
        by_rel = {str(f.get("path", "")).replace("\\", "/"): f for f in files}
        for rel in targets:
            entry = by_rel.get(rel)
            if entry is None:
                raise UpdateError(f"更新包缺文件：{rel}")
            try:
                data = zf.read(rel)
            except KeyError:
                raise UpdateError(f"更新包缺文件：{rel}")
            want = str(entry.get("sha256", ""))
            got = hashlib.sha256(data).hexdigest()
            if want and got != want:
                raise UpdateError(f"更新包文件校验不通过：{rel}")
        log("[3/6] 包内文件校验通过")

    # [4] 备份旧文件
    stamp = time.strftime("%Y%m%d_%H%M%S")
    backup_dir = os.path.join(install_dir, "_update_backup", stamp)
    os.makedirs(backup_dir, exist_ok=True)
    backed = 0
    for rel in targets:
        old = os.path.join(install_dir, rel)
        if os.path.isfile(old):
            dst = os.path.join(backup_dir, rel)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(old, dst)
            backed += 1
    log(f"[4/6] 已备份 {backed} 个旧文件 -> _update_backup/{stamp}")

    # 写新文件（先写 .hra_tmp 再原子替换）
    try:
        with zipfile.ZipFile(package) as zf:
            for rel in targets:
                dest = os.path.join(install_dir, rel)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                tmp = dest + ".hra_tmp"
                with zf.open(rel) as src, open(tmp, "wb") as out:
                    shutil.copyfileobj(src, out)
                os.replace(tmp, dest)
    except Exception as e:
        _rollback(install_dir, backup_dir, log)
        raise UpdateError(f"写入失败已回滚：{type(e).__name__}: {e}")

    # 删除清单里的旧文件（只删程序目录内、且不在用户数据目录里的）
    removed = 0
    for rel in manifest.get("delete", []) or []:
        rel = str(rel).replace("\\", "/").lstrip("/")
        dest = os.path.abspath(os.path.join(install_dir, rel))
        if is_user_data_path(dest) or is_program_data_dir(rel):
            log(f"      保留未删（疑似旧数据目录，不动）：{rel}")
            continue
        if os.path.isfile(dest):
            try:
                os.remove(dest)
                removed += 1
            except OSError:
                pass

    # 写 version.json
    try:
        with open(os.path.join(install_dir, "version.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"version": version,
                       "updated_at": time.strftime("%Y-%m-%d %H:%M:%S")}, fh)
    except OSError:
        pass

    # [5] 数据快照比对
    after = snapshot_dir(user_home_dir())
    changed = diff_snapshots(before, after)
    if changed:
        _rollback(install_dir, backup_dir, log)
        raise UpdateError("更新过程中用户数据发生变化，已回滚：\n  "
                          + "\n  ".join(changed[:20]))
    log(f"[5/6] 用户数据校验：{len(after)} 个文件全部未变 [OK]")

    log(f"[6/6] 更新完成 -> v{version}（删除旧文件 {removed} 个）")
    return {"version": version, "files": len(targets),
            "removed": removed, "backup": backup_dir}


def run_gui() -> int:
    """图形界面：选更新包 → 选安装目录 → 开始更新。"""
    import tkinter as tk
    from tkinter import filedialog, messagebox

    root = tk.Tk()
    root.title("HRAgent 更新器")
    root.geometry("660x520")
    root.resizable(False, False)

    tk.Label(root, text="HRAgent 更新器（只更新程序，不动你的数据和配置）",
             font=("Microsoft YaHei UI", 12, "bold")).pack(
        anchor="w", padx=18, pady=(18, 4))
    tk.Label(root, text="步骤：1 选更新包  ->  2 选安装目录  ->  3 点「开始更新」",
             font=("Microsoft YaHei UI", 9), fg="#51565d").pack(
        anchor="w", padx=18, pady=(0, 14))

    pkg_var = tk.StringVar()
    dir_var = tk.StringVar()
    out = tk.Text(root, height=16, font=("Consolas", 9))
    out.pack(fill="both", expand=True, padx=18)

    def pick_pkg() -> None:
        p = filedialog.askopenfilename(
            title="选择更新包", filetypes=[("更新包", "HRAgent更新包-*.zip"),
                                          ("所有文件", "*.*")])
        if p:
            pkg_var.set(p)

    def pick_dir() -> None:
        d = filedialog.askdirectory(
            title="选择安装目录（放着 HRAgent工作台.exe 那一层）")
        if d:
            dir_var.set(d)

    def logline(msg: str) -> None:
        out.insert("end", msg + "\n")
        out.see("end")
        out.update()

    def do_update() -> None:
        pkg = pkg_var.get().strip()
        install = dir_var.get().strip()
        if not pkg or not os.path.isfile(pkg):
            messagebox.showwarning("提示", "请先选择更新包 .zip")
            return
        if not install or not os.path.isdir(install):
            messagebox.showwarning("提示", "请先选择安装目录")
            return
        btn.config(state="disabled")
        try:
            result = apply_update(install, pkg, log=logline)
            logline("[OK] 更新完成 -> v" + result["version"])
            messagebox.showinfo("完成",
                                f"更新完成：v{result['version']}\n\n"
                                f"现在可以启动工作台了。")
            root.destroy()
        except UpdateError as e:
            logline("[X] " + str(e))
            messagebox.showerror("更新失败", str(e))
            btn.config(state="normal")
        except Exception as e:
            logline(f"[X] {type(e).__name__}: {e}")
            messagebox.showerror("更新失败", f"{type(e).__name__}: {e}")
            btn.config(state="normal")

    row1 = tk.Frame(root); row1.pack(fill="x", padx=18, pady=(10, 6))
    tk.Label(row1, text="更新包：", width=10, anchor="w").pack(side="left")
    tk.Entry(row1, textvariable=pkg_var).pack(side="left", fill="x", expand=True)
    tk.Button(row1, text="浏览…", command=pick_pkg).pack(side="left", padx=(6, 0))

    row2 = tk.Frame(root); row2.pack(fill="x", padx=18, pady=(0, 10))
    tk.Label(row2, text="安装目录：", width=10, anchor="w").pack(side="left")
    tk.Entry(row2, textvariable=dir_var).pack(side="left", fill="x", expand=True)
    tk.Button(row2, text="浏览…", command=pick_dir).pack(side="left", padx=(6, 0))

    row3 = tk.Frame(root); row3.pack(fill="x", padx=18, pady=(0, 16))
    btn = tk.Button(row3, text="开始更新", command=do_update)
    btn.pack(side="right")
    tk.Button(row3, text="退出", command=root.destroy).pack(side="right", padx=(0, 8))

    logline("就绪。更新前请先在托盘图标上右键「退出」工作台。")
    root.mainloop()
    return 0


def _relocate_self(argv: list[str]) -> int:
    """把自己复制到临时目录再跑，然后**立即退出**，由子进程继续。

    为什么需要：更新包通常**包含新版更新器自己**（让下一次更新用新版工具）。
    但 Windows 会锁住正在运行的 exe —— 不搬走，替换自己那一步必然失败
    （PermissionError: WinError 5），整个更新回滚。搬走之后原位置的 exe
    就不再被占用。

    ⚠️ 关键（2026-10-22 实测踩到）：必须用 **Popen 后立刻 return**，
    不能用 `subprocess.run` 同步等待。同步等待 = 父进程一直活着 =
    父进程仍然打开着原 exe 文件句柄，子进程照样替换失败。
    「搬走」要成立的前提是**原进程真的退出了**，不只是"子进程在别处跑"。

    返回码约定：>=0 表示子进程已启动、本进程应按此码退出（不往下走）；
    -1 表示没搬成，调用方继续在原地执行。
    """
    if not getattr(sys, "frozen", False):
        return -1
    if os.environ.get("HRA_UPDATER_RELOCATED") == "1":
        return -1
    tmp = os.path.join(
        os.environ.get("TEMP")
        or os.path.join(os.path.expanduser("~"), "hra_updater_run"))
    os.makedirs(tmp, exist_ok=True)
    me = os.path.join(tmp, os.path.basename(sys.executable))
    try:
        shutil.copy2(sys.executable, me)
    except OSError:
        return -1
    env = dict(os.environ)
    env["HRA_UPDATER_RELOCATED"] = "1"
    # 不再 run()：起完立刻退出，把锁释放掉。返回码由子进程自己回传给 shell。
    subprocess.Popen([me] + list(argv[1:]), env=env, close_fds=True)
    return 0


def main(argv=None) -> int:
    argv = list(sys.argv if argv is None else argv)

    # 打包后先把 exe 搬到临时目录再跑（否则替换自己会失败）
    if (getattr(sys, "frozen", False)
            and os.environ.get("HRA_UPDATER_RELOCATED") != "1"):
        code = _relocate_self(argv)
        if code >= 0:
            return code

    ap = argparse.ArgumentParser(description="HRAgent 热更新器")
    ap.add_argument("package", nargs="?", help="更新包 .zip")
    ap.add_argument("--install-dir", dest="install_dir", help="安装目录")
    ap.add_argument("--gui", action="store_true", help="强制图形界面")
    args = ap.parse_args(argv[1:])

    if not args.package or not args.install_dir:
        return run_gui()

    try:
        result = apply_update(args.install_dir, args.package)
    except UpdateError as e:
        print(f"[X] {e}")
        return 1
    print(f"[OK] 更新完成 -> v{result['version']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())