"""HRAgent 安装器 —— 把程序装到指定目录，**不碰用户数据与配置**。

**安装与数据的分工（这是整个方案的地基）**
    程序文件  -> `<安装目录>`（可随时删、随时换）
    用户数据  -> `%LOCALAPPDATA%\\HRAgentData\\`（装程序时**绝不写**）
    用户配置  -> 同上，`config/` 里（模型密钥、邮箱授权码、档位阈值…）

    所以：重装程序 = 覆盖 `<安装目录>`，用户的库、简历、头像、配置一个都不动；
    卸载程序默认也**保留**用户数据（要删得单独勾选并二次确认）。

**分发形态**
    HRAgent安装器.exe  +  HRAgent程序包-vX.Y.Z.zip  （两个文件放同一目录）
    双击安装器会自动找到旁边的程序包；也可以手动指定。

**覆盖升级**
    目标目录已存在 -> 走"覆盖升级"：先备份被替换的程序文件，
    再覆盖，**不碰** `%LOCALAPPDATA%\\HRAgentData`（安装前后做快照比对）。

命令行：
    HRAgent安装器.exe --install <程序包.zip> --dir <安装目录>
    HRAgent安装器.exe --uninstall --dir <安装目录>
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import zipfile

APP_NAME = "HRAgent工作台"
EXE_NAME = "HRAgent工作台.exe"
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\HRAgent工作台"


def _log_path() -> str:
    base = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
            else os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "安装日志.log")


def make_logger():
    """控制台 + 文件双写（GUI 打包后 print 没人看得到，出错就没法查）。"""
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


def user_home_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "HRAgentData")


def default_install_dir() -> str:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "Programs", APP_NAME)


def is_user_data_path(path: str) -> bool:
    try:
        target = os.path.abspath(path)
        root = os.path.abspath(user_home_dir())
        return os.path.commonpath([target, root]) == root
    except (OSError, ValueError):
        return False


def snapshot_dir(root: str) -> dict:
    """用户数据目录快照（安装器用来证明"我真没动你的数据"）。"""
    import hashlib
    out = {}
    if not os.path.isdir(root):
        return out
    for base, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for name in files:
            p = os.path.join(base, name)
            try:
                st = os.stat(p)
            except OSError:
                continue
            digest = ""
            if st.st_size <= 32 * 1024 * 1024:
                try:
                    h = hashlib.sha256()
                    with open(p, "rb") as fh:
                        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                            h.update(chunk)
                    digest = h.hexdigest()
                except OSError:
                    digest = ""
            out[os.path.relpath(p, root)] = (st.st_size, st.st_mtime_ns, digest)
    return out


def diff_snapshots(before: dict, after: dict) -> list:
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


# ---------------------------------------------------------------- 解包

def extract_payload(payload: str, target_dir: str, log=LOG) -> int:
    """把程序包 zip 解到安装目录。返回写入的文件数。"""
    count = 0
    with zipfile.ZipFile(payload) as zf:
        for name in zf.namelist():
            if name.endswith("/"):
                continue
            rel = name.replace("\\", "/")
            if ".." in rel.split("/"):
                raise RuntimeError(f"程序包里有越界路径：{rel}")
            dest = os.path.join(target_dir, rel.replace("/", os.sep))
            if is_user_data_path(dest):
                raise RuntimeError(f"程序包试图写进用户数据目录，已拒绝：{rel}")
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with zf.open(name) as src, open(dest + ".hra_tmp", "wb") as dst:
                shutil.copyfileobj(src, dst)
            os.replace(dest + ".hra_tmp", dest)
            count += 1
    log(f"[2/5] 已写入 {count} 个程序文件 -> {target_dir}")
    return count


# ---------------------------------------------------------------- 快捷方式 / 卸载

def make_shortcut(target: str, link_path: str, log=LOG) -> bool:
    """用 PowerShell + 环境变量建 .lnk（避免中文路径的引号/编码问题）。"""
    env = dict(os.environ)
    env["HRA_LNK_TARGET"] = target
    env["HRA_LNK_PATH"] = link_path
    env["HRA_LNK_DESC"] = "HRAgent 企业人才库工作台"
    ps = (
        "$s=New-Object -ComObject WScript.Shell;"
        "$l=$s.CreateShortcut($env:HRA_LNK_PATH);"
        "$l.TargetPath=$env:HRA_LNK_TARGET;"
        "$l.WorkingDirectory=(Split-Path $env:HRA_LNK_TARGET);"
        "$l.Description=$env:HRA_LNK_DESC;"
        "$l.Save()"
    )
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-Command", ps],
                           env=env, capture_output=True, timeout=60)
        if r.returncode == 0 and os.path.isfile(link_path):
            return True
        log(f"    快捷方式创建失败：{r.stderr.decode('gbk', errors='ignore')[:120]}")
    except Exception as exc:                            # noqa: BLE001
        log(f"    快捷方式创建异常：{type(exc).__name__}: {exc}")
    return False


def register_uninstall(install_dir: str, version: str, log=LOG) -> None:
    import winreg
    key_path = UNINSTALL_KEY
    try:
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, key_path)
    except OSError as exc:
        log(f"    卸载项注册失败（不影响使用）：{exc}")
        return
    installer = os.path.join(install_dir, "HRAgent安装器.exe")
    values = {
        "DisplayName": "HRAgent 企业人才库工作台",
        "DisplayVersion": version,
        "Publisher": "HRAgent",
        "InstallLocation": install_dir,
        "UninstallString": f'"{installer}" --uninstall --dir "{install_dir}"',
        "NoModify": 1,
        "NoRepair": 1,
        "InstallDate": time.strftime("%Y%m%d"),
    }
    for k, v in values.items():
        winreg.SetValueEx(key, k, 0, winreg.REG_SZ, v)
    winreg.CloseKey(key)
    log("[4/5] 已注册卸载项（控制面板 -> 程序和功能 里能看到）")


def do_uninstall(install_dir: str, keep_data: bool = True, log=LOG) -> None:
    """卸载：删程序目录；数据默认保留。"""
    data_root = user_home_dir()
    log(f"[1/3] 删除程序目录：{install_dir}")
    if os.path.isdir(install_dir):
        shutil.rmtree(install_dir, ignore_errors=True)
    for link in _shortcut_paths():
        if os.path.exists(link):
            try:
                os.remove(link)
            except OSError:
                pass
    log("[2/3] 已清理快捷方式")
    import winreg
    try:
        winreg.DeleteKey(winreg.HKEY_CURRENT_USER, UNINSTALL_KEY)
        log("[3/3] 已移除卸载项")
    except OSError:
        log("[3/3] 卸载项已不存在")
    if keep_data:
        log(f"用户数据**已保留**：{data_root}")
        log("要彻底清除请手动删除该目录（删了就没法恢复了）。")
    else:
        if os.path.isdir(data_root):
            shutil.rmtree(data_root, ignore_errors=True)
        log(f"用户数据已删除：{data_root}")


def _shortcut_paths() -> list:
    desktop = os.path.join(os.path.expanduser("~"), "Desktop")
    start = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows",
                          "Start Menu", "Programs")
    return [
        os.path.join(desktop, APP_NAME + ".lnk"),
        os.path.join(start, APP_NAME + ".lnk"),
    ]


# ---------------------------------------------------------------- 安装主流程

def install(payload: str, install_dir: str, shortcuts: bool = True,
            log=LOG) -> dict:
    install_dir = os.path.abspath(install_dir)
    data_root = user_home_dir()
    before = snapshot_dir(data_root)
    log(f"[1/5] 目标目录：{install_dir}")

    if os.path.isdir(install_dir):
        stamp = time.strftime("%Y%m%d_%H%M%S")
        bak = install_dir + ".bak_" + stamp
        log(f"    目标已存在 -> 先整体备份到 {os.path.basename(bak)}")
        try:
            shutil.copytree(install_dir, bak)
        except OSError as exc:
            log(f"    备份失败（继续覆盖）：{exc}")

    os.makedirs(install_dir, exist_ok=True)
    count = extract_payload(payload, install_dir, log=log)

    # 版本标记
    version = "?"
    try:
        with zipfile.ZipFile(payload) as zf:
            if "version.json" in zf.namelist():
                version = json.loads(zf.read("version.json").decode("utf-8")).get("version", "?")
    except (OSError, ValueError, KeyError):
        pass
    with open(os.path.join(install_dir, "version.json"), "w", encoding="utf-8") as fh:
        json.dump({"version": version, "installed_at": time.strftime("%Y-%m-%d %H:%M:%S")},
                  fh, ensure_ascii=False, indent=2)

    exe = os.path.join(install_dir, EXE_NAME)
    if shortcuts and os.path.isfile(exe):
        ok = make_shortcut(exe, _shortcut_paths()[0], log=log)
        make_shortcut(exe, _shortcut_paths()[1], log=log)
        log(f"[3/5] 快捷方式：{'已创建' if ok else '创建失败，可手动创建'}")

    register_uninstall(install_dir, version, log=log)

    after = snapshot_dir(data_root)
    changed = diff_snapshots(before, after)
    if changed:
        log("[5/5] ⚠️ 安装过程触碰了用户数据目录，请检查：")
        for c in changed[:10]:
            log("      " + c)
    else:
        log(f"[5/5] 用户数据未被触碰（{len(after)} 个文件原样保留）[OK]")
    return {"install_dir": install_dir, "files": count, "version": version,
            "data_untouched": not changed}


# ---------------------------------------------------------------- GUI

def run_gui() -> int:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title("HRAgent 安装器")
    root.geometry("640x520")
    root.resizable(False, False)

    state = {"payload": None, "dir": default_install_dir()}
    default_payload = None
    here = os.path.dirname(os.path.abspath(sys.argv[0] if getattr(sys, "frozen", False)
                                           else __file__))
    for cand in sorted(os.listdir(here) or []):
        if cand.lower().endswith(".zip") and "程序包" in cand:
            default_payload = os.path.join(here, cand)
            break

    frm = ttk.Frame(root, padding=16)
    frm.pack(fill="both", expand=True)
    ttk.Label(frm, text="安装 HRAgent 企业人才库工作台",
              font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w")
    ttk.Label(frm, text="只安装程序文件。你的数据、简历、配置、模型密钥都不会被动。",
              foreground="#1f7a3d").pack(anchor="w", pady=(4, 12))

    pkg_var = tk.StringVar(value=default_payload or "")
    dir_var = tk.StringVar(value=state["dir"])

    def pick_pkg():
        p = filedialog.askopenfilename(title="选择程序包",
                                       filetypes=[("程序包", "*.zip"), ("所有文件", "*.*")])
        if p:
            state["payload"] = p
            pkg_var.set(p)

    def pick_dir():
        d = filedialog.askdirectory(title="选择安装目录")
        if d:
            state["dir"] = d
            dir_var.set(d)

    row = ttk.Frame(frm); row.pack(fill="x", pady=4)
    ttk.Label(row, text="程序包：", width=10).pack(side="left")
    ttk.Entry(row, textvariable=pkg_var).pack(side="left", fill="x", expand=True)
    ttk.Button(row, text="浏览…", command=pick_pkg).pack(side="left", padx=6)

    row = ttk.Frame(frm); row.pack(fill="x", pady=4)
    ttk.Label(row, text="安装到：", width=10).pack(side="left")
    ttk.Entry(row, textvariable=dir_var).pack(side="left", fill="x", expand=True)
    ttk.Button(row, text="浏览…", command=pick_dir).pack(side="left", padx=6)

    txt = tk.Text(frm, height=15, font=("Consolas", 9))
    txt.pack(fill="both", expand=True, pady=10)
    txt.configure(state="disabled")

    def log(msg):
        def _w():
            txt.configure(state="normal")
            txt.insert("end", str(msg) + "\n")
            txt.see("end")
            txt.configure(state="disabled")
        root.after(0, _w)

    def do_install():
        pkg = pkg_var.get().strip()
        if not pkg or not os.path.isfile(pkg):
            messagebox.showwarning("缺程序包", "请先选择程序包 .zip")
            return
        log("——— 开始安装 ———")
        try:
            r = install(pkg, dir_var.get().strip(), log=log)
        except Exception as exc:                        # noqa: BLE001
            log("[X] " + f"{type(exc).__name__}: {exc}")
            messagebox.showerror("安装失败", f"{type(exc).__name__}: {exc}")
            return
        log("[OK] 安装完成。双击桌面快捷方式即可使用。")
        messagebox.showinfo(
            "安装完成",
            f"版本：v{r['version']}\n安装位置：{r['install_dir']}\n\n"
            f"程序文件 {r['files']} 个。\n"
            f"你的数据与配置{'未被触碰（原有数据全部保留）' if r['data_untouched'] else '⚠️ 有变动，请检查日志'}。\n\n"
            f"首次启动会自动把演示数据放进：\n{user_home_dir()}")

    btns = ttk.Frame(frm); btns.pack(fill="x")
    ttk.Button(btns, text="开始安装", command=do_install).pack(side="left")
    ttk.Button(btns, text="退出", command=root.destroy).pack(side="right")
    log("就绪。安装不会碰 %LOCALAPPDATA%\\HRAgentData 里的任何文件。")
    root.mainloop()
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="HRAgent 安装器")
    ap.add_argument("--install", help="程序包 .zip")
    ap.add_argument("--dir", dest="dir", help="安装目录")
    ap.add_argument("--uninstall", action="store_true", help="卸载")
    ap.add_argument("--purge-data", action="store_true", help="卸载时连用户数据一起删（危险）")
    args = ap.parse_args(argv)

    if args.uninstall:
        target = args.dir or default_install_dir()
        do_uninstall(target, keep_data=not args.purge_data, log=LOG)
        return 0
    if args.install and args.dir:
        r = install(args.install, args.dir, log=LOG)
        print("[OK] 安装完成：", r)
        return 0
    return run_gui()


if __name__ == "__main__":
    sys.exit(main())
