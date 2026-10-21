"""HRAgent 热更新器 —— 只换功能，不碰数据与配置。

**它到底更新什么**
    安装目录里的**程序文件**：`HRAgent工作台.exe` 与 `_internal/` 下的运行时文件。
    PyInstaller onedir 的业务代码打包在 exe 里的 PYZ 归档中，所以绝大多数
    功能更新 = 换一个新的 exe；换 `_internal/` 是为了带上新依赖与模型文件。

**它绝不碰什么**
    1. 用户数据与配置：`%LOCALAPPDATA%\\HRAgentData\\`（data + config）。
       —— 程序启动时把 TP_USER_HOME 指到这里，用户的库、简历原件、头像、
       邮件模板、模型密钥、档位阈值全在这里面。
    2. 安装目录里的 `data/` 目录（历史布局）与任何 `*.db / *.key / *.secret`。
    3. 配置文件里**用户改过的值**不在更新范围内：更新只替换安装目录里的
       **模板**，用户配置在外置目录，启动器的 `seed_config()` 只补缺失键、
       不覆盖已有值（`app/profile_migrate.py`）。

**安全网（三道）**
    第一道：路径白名单。更新包清单里出现"用户数据目录"内的路径 -> 直接拒绝。
    第二道：写前备份。每个将被覆盖的文件先复制到
            `<安装目录>/_update_backup/<时间戳>/`，失败可一键回滚。
    第三道：数据快照比对。更新前后各给用户数据目录做一次快照
            （文件清单 + 大小 + mtime + 库文件 sha256），不一致立刻回滚并报错。

用法：
    双击运行 -> 选更新包 .zip -> 选安装目录 -> 点"开始更新"
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

APP_DIR_NAME = "HRAgent工作台"
EXE_NAME = "HRAgent工作台.exe"
#: 安装目录里**永远不写**的路径段（相对路径任一段命中即拒绝）
FORBIDDEN_SEGMENTS = {"data"}
#: 永远不写的文件名后缀（密钥/库文件）
FORBIDDEN_SUFFIX = (".db", ".key", ".secret", ".sqlite", ".sqlite3")


def is_seed_path(rel: str) -> bool:
    """种子目录算程序资源，可写。

    ⚠️ 必须按**路径段**判，不能按前缀判：实际路径是
    `_internal/seed/data/workbench.db`（种子在 _internal 里面），
    按 "seed/" 前缀判会把合法种子误杀 —— 我第一版就是这么写的，
    更新包生成被自己的闸门拦下来才发现。
    """
    return "seed" in rel.replace("\\", "/").split("/")


# ---------------------------------------------------------------- 路径

def user_home_dir() -> str:
    """用户数据根：%LOCALAPPDATA%\\HRAgentData。"""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "HRAgentData")


def _log_path() -> str:
    base = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
            else os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "更新日志.log")


def make_logger():
    """控制台 + 文件双写。

    打包成 GUI 程序（console=False）后 print 没人看得到 ——
    出错时用户只会说"更新失败了"，没日志就无从查起。
    """
    path = _log_path()

    def log(msg):
        line = str(msg)
        try:
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(time.strftime("%Y-%m-%d %H:%M:%S ") + line + "\n")
        except OSError:
            pass
        try:
            print(line, flush=True)
        except Exception:                               # noqa: BLE001
            pass
    return log


LOG = make_logger()

# Windows 控制台默认 GBK，日志里出现非 GBK 字符会直接抛 UnicodeEncodeError
# 把进程都带崩（更新都成功了，却因为打一行 ✅ 而退出码 1）。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")
    except Exception:                                   # noqa: BLE001
        pass


def is_user_data_path(path: str) -> bool:
    """这个绝对路径是否落在用户数据目录里（落在里面就绝不能写）。"""
    try:
        target = os.path.abspath(path)
        root = os.path.abspath(user_home_dir())
    except (OSError, ValueError):
        return False
    return os.path.commonpath([target, root]) == root


# ---------------------------------------------------------------- 工具

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
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in files:
            p = os.path.join(base, name)
            rel = os.path.relpath(p, root)
            try:
                st = os.stat(p)
            except OSError:
                continue
            digest = ""
            if st.st_size <= 32 * 1024 * 1024:
                try:
                    digest = sha256_file(p)
                except OSError:
                    digest = ""
            out[rel] = (st.st_size, st.st_mtime_ns, digest)
    return out


def diff_snapshots(before: dict, after: dict) -> list:
    """返回"变了"的相对路径列表（数据目录被动过 = 事故）。"""
    changed = []
    for rel, sig in after.items():
        if rel not in before:
            changed.append(rel + "（新增）")
        elif before[rel] != sig:
            changed.append(rel + "（已修改）")
    for rel in before:
        if rel not in after:
            changed.append(rel + "（被删除）")
    return changed


def app_running(install_dir: str) -> list:
    """**目标安装目录**里的程序是否在跑。返回进程路径列表。

    ⚠️ 必须按**路径**判断，不能按进程名：
    按进程名会把"另一套安装"也当成占用 —— 同一台机器上装了两份时
    （开发目录 + D 盘正式版），更新其中一份会被另一份无辜拦住。
    tasklist 不给路径，所以借 PowerShell 的 Get-Process 拿 ExecutablePath。
    """
    target = os.path.abspath(install_dir).lower()
    try:
        ps = ("Get-Process | Where-Object { $_.Path -like '*HRAgent*' -or "
              "$_.ProcessName -like '*HRAgent*' } | "
              "ForEach-Object { try { $_.Path } catch { '' } }")
        raw = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                             capture_output=True, timeout=30)
        text = raw.stdout.decode("utf-8", errors="ignore")
    except Exception:
        return []
    hits = []
    for line in text.splitlines():
        p = line.strip().strip('"')
        if not p or not p.lower().endswith(".exe"):
            continue
        if "hra" not in os.path.basename(p).lower():
            continue
        if p.lower().startswith(target):
            hits.append(p)
    return hits


# ---------------------------------------------------------------- 核心

class UpdateError(Exception):
    pass


def read_manifest(zf: zipfile.ZipFile) -> dict:
    try:
        raw = zf.read("update.json")
    except KeyError:
        raise UpdateError("更新包里没有 update.json —— 这不是合法的更新包")
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise UpdateError(f"update.json 解析失败：{exc}")


def validate_targets(install_dir: str, manifest: dict) -> list:
    """按路径规则挑出**允许写入**的目标；命中禁区就报错（不是跳过，是报错）。

    报错而不是静默跳过：静默跳过会让人以为"更新了"，
    实际某个关键文件没换（这正是本项目反复踩的"假通过"）。
    """
    ok = []
    for item in manifest.get("files", []):
        rel = str(item.get("path") or "").replace("\\", "/").lstrip("/")
        if not rel:
            continue
        if ".." in rel.split("/"):
            raise UpdateError(f"更新包里有越界路径：{rel}")
        abs_target = os.path.join(install_dir, rel.replace("/", os.sep))
        if is_user_data_path(abs_target):
            raise UpdateError(f"更新包试图改写用户数据目录，已拒绝：{rel}")
        segs = rel.split("/")
        if any(seg in FORBIDDEN_SEGMENTS for seg in segs) and not is_seed_path(rel):
            raise UpdateError(f"更新包试图改写数据目录，已拒绝：{rel}")
        low = rel.lower()
        if low.endswith((".db", ".sqlite", ".sqlite3")) and not is_seed_path(rel):
            raise UpdateError(f"更新包试图改写库文件，已拒绝：{rel}")
        if os.path.basename(rel).lower() == "master.key":
            # PII 主密钥：进了包等于把手机号/邮箱的解密能力一起送人
            raise UpdateError(f"更新包试图改写 PII 主密钥，已拒绝：{rel}")
        if low.endswith((".secret", ".key")) and not rel.startswith(
                ("config/", "config_template/")):
            raise UpdateError(f"更新包试图改写密钥文件，已拒绝：{rel}")
        ok.append((rel, abs_target, item))
    return ok


def apply_update(install_dir: str, package: str, log=LOG) -> dict:
    """执行更新。返回结果字典；失败会抛 UpdateError。"""
    install_dir = os.path.abspath(install_dir)
    if not os.path.isdir(install_dir):
        raise UpdateError(f"安装目录不存在：{install_dir}")

    running = app_running(install_dir)
    if running:
        raise UpdateError(
            "程序还在运行（" + "、".join(running) + "）。\n"
            "请先在托盘图标上右键「退出」，再运行更新器。")

    data_root = user_home_dir()
    before = snapshot_dir(data_root)
    log(f"[1/6] 用户数据快照：{len(before)} 个文件（{data_root}）")

    with zipfile.ZipFile(package) as zf:
        manifest = read_manifest(zf)
        version = manifest.get("version", "?")
        targets = validate_targets(install_dir, manifest)
        log(f"[2/6] 更新包：v{version}，{len(targets)} 个文件待更新")

        # ---- 校验包内文件完整性（先验再写，别写到一半发现包坏了）----
        for rel, abs_target, item in targets:
            want = str(item.get("sha256") or "")
            if not want:
                continue
            try:
                got = hashlib.sha256(zf.read(rel)).hexdigest()
            except KeyError:
                raise UpdateError(f"更新包缺文件：{rel}")
            if got != want:
                raise UpdateError(f"更新包文件校验不通过：{rel}")
        log("[3/6] 包内文件校验通过")

        # ---- 备份被覆盖的文件 ----
        stamp = time.strftime("%Y%m%d_%H%M%S")
        backup_dir = os.path.join(install_dir, "_update_backup", stamp)
        written = []
        for rel, abs_target, item in targets:
            if not os.path.isfile(abs_target):
                continue
            bak = os.path.join(backup_dir, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(bak), exist_ok=True)
            shutil.copy2(abs_target, bak)
        log(f"[4/6] 已备份 {len(written) or sum(1 for r, a, i in targets if os.path.isfile(a))} "
            f"个旧文件 -> _update_backup/{stamp}")

        # ---- 写入（临时文件 + 原子替换）----
        try:
            for rel, abs_target, item in targets:
                os.makedirs(os.path.dirname(abs_target), exist_ok=True)
                tmp = abs_target + ".hra_tmp"
                with zf.open(rel) as src, open(tmp, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                os.replace(tmp, abs_target)
                written.append(rel)
        except Exception as exc:
            _rollback(install_dir, backup_dir, log)
            raise UpdateError(f"写入失败已回滚：{type(exc).__name__}: {exc}")

        # ---- 删除清单里标记为删除的文件 ----
        removed = []
        kept = []
        for rel in manifest.get("remove", []) or []:
            rel = str(rel).replace("\\", "/").lstrip("/")
            abs_target = os.path.join(install_dir, rel.replace("/", os.sep))
            if is_user_data_path(abs_target):
                raise UpdateError(f"更新包试图删除用户数据目录里的文件，已拒绝：{rel}")
            # data/ 下的东西**一律不删**：对老版本用户来说，那个位置的
            # workbench.db 可能就是他的真实数据（还没迁到用户目录的机器）。
            # 留着最多占几百 KB，删错了就是数据没了 —— 不对称风险不做。
            if any(seg in FORBIDDEN_SEGMENTS for seg in rel.split("/")) and not is_seed_path(rel):
                kept.append(rel)
                continue
            if os.path.isfile(abs_target):
                os.remove(abs_target)
                removed.append(rel)
        if kept:
            log(f"      保留未删（疑似旧数据目录，不动）：{', '.join(kept[:5])}")

    # ---- 写版本标记 ----
    with open(os.path.join(install_dir, "version.json"), "w", encoding="utf-8") as fh:
        json.dump({"version": version, "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                   "package": os.path.basename(package)}, fh, ensure_ascii=False, indent=2)

    # ---- 第三道安全网：用户数据必须一个字节都没变 ----
    after = snapshot_dir(data_root)
    changed = diff_snapshots(before, after)
    if changed:
        _rollback(install_dir, backup_dir, log)
        raise UpdateError("更新过程中用户数据发生变化，已回滚：\n  " + "\n  ".join(changed[:10]))
    log(f"[5/6] 用户数据校验：{len(after)} 个文件全部未变 [OK]")

    log(f"[6/6] 更新完成 -> v{version}"
        + (f"（删除旧文件 {len(removed)} 个）" if removed else ""))
    return {"version": version, "written": len(written), "removed": removed,
            "backup_dir": backup_dir, "data_files": len(after)}


def _rollback(install_dir: str, backup_dir: str, log=LOG) -> None:
    """把备份里的文件拷回去。"""
    if not os.path.isdir(backup_dir):
        return
    count = 0
    for base, dirs, files in os.walk(backup_dir):
        for name in files:
            src = os.path.join(base, name)
            rel = os.path.relpath(src, backup_dir)
            dst = os.path.join(install_dir, rel)
            try:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.copy2(src, dst)
                count += 1
            except OSError:
                continue
    log(f"[回滚] 已还原 {count} 个文件")


# ---------------------------------------------------------------- GUI

def run_gui() -> int:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title("HRAgent 更新器")
    root.geometry("660x520")
    root.resizable(False, False)

    state = {"pkg": None, "install_dir": None}

    frm = ttk.Frame(root, padding=16)
    frm.pack(fill="both", expand=True)

    ttk.Label(frm, text="HRAgent 更新器（只更新程序，不动你的数据和配置）",
              font=("Microsoft YaHei UI", 11, "bold")).pack(anchor="w")
    ttk.Label(frm, text="步骤：1 选更新包  ->  2 选安装目录  ->  3 点「开始更新」",
              foreground="#51565d").pack(anchor="w", pady=(4, 12))

    pkg_var = tk.StringVar()
    dir_var = tk.StringVar()

    def pick_pkg():
        p = filedialog.askopenfilename(
            title="选择更新包", filetypes=[("更新包", "*.zip"), ("所有文件", "*.*")])
        if p:
            state["pkg"] = p
            pkg_var.set(p)
            if not state["install_dir"]:
                guess = os.path.dirname(p)
                d2 = os.path.join(guess, APP_DIR_NAME)
                if os.path.isdir(d2):
                    state["install_dir"] = d2
                    dir_var.set(d2)

    def pick_dir():
        d = filedialog.askdirectory(title="选择已安装的 HRAgent 目录")
        if d:
            state["install_dir"] = d
            dir_var.set(d)

    row = ttk.Frame(frm); row.pack(fill="x", pady=4)
    ttk.Label(row, text="更新包：", width=10).pack(side="left")
    ttk.Entry(row, textvariable=pkg_var).pack(side="left", fill="x", expand=True)
    ttk.Button(row, text="浏览…", command=pick_pkg).pack(side="left", padx=6)

    row = ttk.Frame(frm); row.pack(fill="x", pady=4)
    ttk.Label(row, text="安装目录：", width=10).pack(side="left")
    ttk.Entry(row, textvariable=dir_var).pack(side="left", fill="x", expand=True)
    ttk.Button(row, text="浏览…", command=pick_dir).pack(side="left", padx=6)

    txt = tk.Text(frm, height=16, font=("Consolas", 9))
    txt.pack(fill="both", expand=True, pady=10)
    txt.configure(state="disabled")

    def log(msg):
        def _w():
            txt.configure(state="normal")
            txt.insert("end", msg + "\n")
            txt.see("end")
            txt.configure(state="disabled")
        root.after(0, _w)

    def do_update():
        if not state["pkg"] or not state["install_dir"]:
            messagebox.showwarning("还差一步", "请先选更新包和安装目录")
            return
        log("——— 开始更新 ———")
        try:
            r = apply_update(state["install_dir"], state["pkg"], log=log)
        except UpdateError as exc:
            log("[X] " + str(exc))
            messagebox.showerror("更新失败", str(exc))
            return
        except Exception as exc:                       # noqa: BLE001
            log("[X] " + f"{type(exc).__name__}: {exc}")
            messagebox.showerror("更新失败", f"{type(exc).__name__}: {exc}")
            return
        log("[OK] 全部完成。请重新启动 HRAgent 工作台。")
        messagebox.showinfo(
            "更新完成",
            f"已更新到 v{r['version']}。\n\n"
            f"更新了 {r['written']} 个程序文件；\n"
            f"你的数据与配置（{r['data_files']} 个文件）**一个字节都没变**。\n\n"
            f"旧文件备份在：\n{r['backup_dir']}")

    btns = ttk.Frame(frm)
    btns.pack(fill="x")
    ttk.Button(btns, text="开始更新", command=do_update).pack(side="left")
    ttk.Button(btns, text="退出", command=root.destroy).pack(side="right")

    log("就绪。更新前请先在托盘图标上右键「退出」工作台。")
    root.mainloop()
    return 0


def _relocate_self(argv) -> int | None:
    """把自己复制到临时目录再跑，然后返回子进程退出码。

    为什么需要：更新包通常**包含新版更新器自己**（让下一次更新用新版工具）。
    但 Windows 会锁住正在运行的 exe —— 不搬走，替换自己那一步必然失败
    （PermissionError），整个更新回滚。搬走之后原位置的 exe 就没被占用。
    """
    if not getattr(sys, "frozen", False):
        return None
    if os.environ.get("HRA_UPDATER_RELOCATED"):
        return None
    import tempfile
    tmp_dir = os.path.join(tempfile.gettempdir(), "hra_updater_run")
    os.makedirs(tmp_dir, exist_ok=True)
    tmp_exe = os.path.join(tmp_dir, os.path.basename(sys.executable))
    try:
        shutil.copy2(sys.executable, tmp_exe)
    except OSError:
        return None
    env = dict(os.environ)
    env["HRA_UPDATER_RELOCATED"] = "1"
    r = subprocess.run([tmp_exe] + list(argv), env=env)
    return r.returncode


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    code = _relocate_self(argv)
    if code is not None:
        return code

    ap = argparse.ArgumentParser(description="HRAgent 热更新器")
    ap.add_argument("package", nargs="?", help="更新包 .zip")
    ap.add_argument("--install-dir", dest="install_dir", help="安装目录")
    ap.add_argument("--gui", action="store_true", help="强制图形界面")
    args = ap.parse_args(argv)

    if args.package and args.install_dir:
        try:
            r = apply_update(args.install_dir, args.package, log=print)
        except UpdateError as exc:
            print("[X] " + str(exc))
            return 1
        print("[OK] 更新完成 ->", r["version"])
        return 0
    return run_gui()


if __name__ == "__main__":
    sys.exit(main())
