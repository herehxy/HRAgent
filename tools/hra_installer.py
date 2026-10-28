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
        # ⚠️ 整数必须用 REG_DWORD：NoModify/NoRepair 是 1（布尔型 DWORD），
        # 用 REG_SZ 写会抛 ValueError —— 而这个异常发生在**装完程序文件之后**，
        # 结果是"程序装上了但快捷方式没建、数据没迁移、还报错"（实测踩到）。
        if isinstance(v, int):
            winreg.SetValueEx(key, k, 0, winreg.REG_DWORD, v)
        else:
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


def _load_migrate_module():
    """按路径加载产品自己的 `profile_migrate.py`（**不另写一份迁移逻辑**）。

    为什么能这么干：该模块只依赖标准库（hashlib/json/os/shutil/sqlite3/
    tempfile/uuid/datetime/pathlib），不 import app 的其他东西 → 可以脱离
    主程序独立跑。打包时由 build_release_v2.py 把它拷进 tools/_vendor/ 并
    随安装器分发，**拷贝是构建期自动做的**，不会与源码分叉
    （"两处各写一份组装"这个坑本项目踩过多次）。

    迁移逻辑的正确性都在那个模块里：sqlite backup API 一致性快照、
    库内相对路径改写、行数快照比对、PII 主密钥 sha256 校验、
    目标已有档案时拒绝覆盖。
    """
    # 首选：同目录模块（构建期由 build_release_v2.py 从 repo_src/app 拷来）
    try:
        import hra_profile_migrate as _mod       # noqa: F401
        return _mod
    except ImportError:
        pass
    # 兜底：按文件路径找（_vendor/ 或源码树）
    import importlib.util
    here = os.path.dirname(os.path.abspath(__file__))
    for base in (getattr(sys, "_MEIPASS", None), here,
                 os.path.join(os.path.dirname(here), "repo_src", "app")):
        if not base:
            continue
        p = os.path.join(base, "_vendor", "profile_migrate.py")
        if not os.path.isfile(p):
            p = os.path.join(base, "profile_migrate.py")
        if os.path.isfile(p):
            break
    else:
        return None
    if not os.path.isfile(p):
        return None
    try:
        spec = importlib.util.spec_from_file_location("hra_profile_migrate", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:                                      # noqa: BLE001
        return None


def _people_count(db_path: str) -> int:
    try:
        import sqlite3
        c = sqlite3.connect("file:" + db_path.replace("\\", "/") + "?mode=ro", uri=True)
        try:
            return c.execute("SELECT count(*) FROM candidates").fetchone()[0]
        finally:
            c.close()
    except Exception:                                      # noqa: BLE001
        return -1


def detect_legacy_roots(install_dir: str, extra: str = "") -> list:
    """找"可能存着旧数据"的目录，返回 [(根路径, 候选人数)]。

    场景：用户把程序装到**新目录**，而他的库、简历、配置还躺在**旧安装目录**里
    （老版本就是 data/ 跟着安装目录走的）。这时不迁移 → 打开就是"空库"，
    看着像数据丢了，其实数据好好地待在老地方。
    """
    mod = _load_migrate_module()
    cands = []
    install_dir = os.path.abspath(install_dir)
    guesses = [
        os.path.join(install_dir, "_internal"),          # 同目录的老布局
        os.path.join(os.path.dirname(install_dir), APP_NAME),
        os.path.join(os.path.dirname(install_dir), APP_NAME, "_internal"),
        r"D:\HRAgent工作台\_internal",                    # 历史安装位置
        os.path.join(os.path.expanduser("~"), "HRAgent工作台", "_internal"),
    ]
    # 同级目录里的其他 HRAgent 安装（"旧安装"最常见的位置）
    parent = os.path.dirname(install_dir)
    try:
        for name in sorted(os.listdir(parent)):
            if "HRAgent" in name and name != APP_NAME:
                guesses.append(os.path.join(parent, name))
                guesses.append(os.path.join(parent, name, "_internal"))
    except OSError:
        pass
    if extra:
        guesses.insert(0, os.path.abspath(extra))
    seen = set()
    for g in guesses:
        g = os.path.abspath(g)
        if g in seen or not os.path.isdir(g):
            continue
        seen.add(g)
        db = os.path.join(g, "data", "workbench.db")
        if not os.path.isfile(db):
            continue
        if mod is not None and not mod.layout(g):
            continue
        cands.append((g, _people_count(db)))
    return cands


def migrate_legacy_data(install_dir: str, user_root: str, extra: str = "",
                        log=LOG) -> dict:
    """把旧安装目录里的数据搬到用户目录（**只搬，不删旧的**）。

    · 目标用户目录已有档案 → **不碰**，只报告"发现旧数据 N 人，未自动并入"
      （自动合并会覆盖 HR 的库，代价太大，不做）
    · 目标为空 → 调用产品自己的 migrate_profile（带行数/主密钥双重校验）
    """
    mod = _load_migrate_module()
    result = {"migrated": False, "candidates": [], "note": ""}
    if mod is None:
        result["note"] = "迁移模块不可用（安装器缺少 profile_migrate）"
        log("    [!] " + result["note"])
        return result
    target_db = os.path.join(user_root, "data", "workbench.db")
    cands = detect_legacy_roots(install_dir, extra)
    result["candidates"] = cands
    if not cands:
        return result
    if os.path.isfile(target_db):
        result["note"] = ("发现旧数据 %d 处，但用户目录已有档案，**未自动并入**"
                           "（避免覆盖现有库）" % len(cands))
        log("    [!] " + result["note"])
        for g, n in cands:
            log(f"        旧数据：{g}（{n} 人）")
        return result
    # 只搬第一处（候选按可信度排序），搬完就停
    root, people = cands[0]
    log(f"    · 发现旧数据：{root}（{people} 人）→ 迁移到 {user_root}")
    try:
        marker = mod.migrate_profile(root, user_root, log=log)
    except Exception as exc:                              # noqa: BLE001
        result["note"] = f"迁移失败：{type(exc).__name__}: {exc}"
        log("    [X] " + result["note"])
        return result
    result["migrated"] = True
    result["root"] = root
    result["counts"] = marker.get("counts")
    log(f"    [OK] 旧数据已迁移：{marker.get('counts')}（旧目录保持原样）")
    return result


def _known_folder(csidl: int, fallback: str = "") -> str:
    """问系统要"已知文件夹"的真实路径（SHGetFolderPathW）。

    ⚠️ **不能拼 %USERPROFILE%\\Desktop**：桌面被重定向到别处时（例如 D:////Desktop），
    那个拼出来的目录压根不存在 → 建快捷方式必然失败（实测踩到：
    桌面在 D 盘，C:////Users////admin////Desktop 不存在，装完没有桌面图标）。
    """
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(260)
        if ctypes.windll.shell32.SHGetFolderPathW(None, csidl, None, 0, buf) == 0:
            return buf.value
    except Exception:                                    # noqa: BLE001
        pass
    return fallback


def _shortcut_paths() -> list:
    # CSIDL: 0x0010=DESKTOPDIRECTORY（当前用户桌面，含重定向）
    #        0x0002=PROGRAMS（当前用户的开始菜单\程序；0x0027 是"所有用户"那个）
    desktop = _known_folder(0x0010, os.path.join(os.path.expanduser("~"), "Desktop"))
    programs = _known_folder(0x0002, os.path.join(
        os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Start Menu", "Programs"))
    return [
        os.path.join(desktop, APP_NAME + ".lnk"),
        os.path.join(programs, APP_NAME + ".lnk"),
    ]


# ---------------------------------------------------------------- 安装主流程

def install(payload: str, install_dir: str, shortcuts: bool = True,
            migrate: bool = True, legacy_dir: str = "", log=LOG) -> dict:
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

    # ---- 旧数据迁移：装到**新目录**时，把老安装目录里的库/简历/配置带过来 ----
    # 不做这一步的后果很具体：HR 的数据还在老安装目录里，新版打开是**空库**，
    # 看着像数据丢了，其实数据好好地待在老地方。
    mig = {"migrated": False, "candidates": [], "note": ""}
    if migrate:
        log("    · 检查旧安装目录里的数据…")
        mig = migrate_legacy_data(install_dir, data_root, legacy_dir, log=log)
    else:
        log("    · 跳过旧数据迁移（按你的选择）")

    # 口径：**已有文件必须零改动**；新增文件只允许来自迁移
    after = snapshot_dir(data_root)
    changed = [k for k in before if k in after and before[k] != after[k]]
    removed = [k for k in before if k not in after]
    added = [k for k in after if k not in before]
    if changed or removed:
        log("[5/5] [X] 安装过程**改动了已有数据**，请检查：")
        for c in (changed + removed)[:10]:
            log("      " + c)
    else:
        tail = ("（新增 %d 个文件 = 迁移过来的旧数据）" % len(added)) if added else ""
        log(f"[5/5] 已有数据零改动（{len(before)} 个文件原样保留）{tail} [OK]")
    return {"install_dir": install_dir, "files": count, "version": version,
            "data_untouched": not (changed or removed),
            "migrated": mig.get("migrated"), "migrate_note": mig.get("note", "")}


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
    legacy_var = tk.StringVar(value="")
    migrate_var = tk.BooleanVar(value=True)

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

    def pick_legacy():
        d = filedialog.askdirectory(title="选择旧安装目录（里面有 data / config）")
        if d:
            legacy_var.set(d)

    row = ttk.Frame(frm); row.pack(fill="x", pady=4)
    ttk.Label(row, text="程序包：", width=10).pack(side="left")
    ttk.Entry(row, textvariable=pkg_var).pack(side="left", fill="x", expand=True)
    ttk.Button(row, text="浏览…", command=pick_pkg).pack(side="left", padx=6)

    row = ttk.Frame(frm); row.pack(fill="x", pady=4)
    ttk.Label(row, text="安装到：", width=10).pack(side="left")
    ttk.Entry(row, textvariable=dir_var).pack(side="left", fill="x", expand=True)
    ttk.Button(row, text="浏览…", command=pick_dir).pack(side="left", padx=6)

    row = ttk.Frame(frm); row.pack(fill="x", pady=4)
    ttk.Label(row, text="旧安装目录：", width=10).pack(side="left")
    ttk.Entry(row, textvariable=legacy_var).pack(side="left", fill="x", expand=True)
    ttk.Button(row, text="浏览…", command=pick_legacy).pack(side="left", padx=6)

    row = ttk.Frame(frm); row.pack(fill="x", pady=(2, 4))
    ttk.Checkbutton(row, text="安装后把旧目录里的数据（库/简历/配置）迁移过来"
                             "（目标已有档案时只提示、不覆盖）",
                    variable=migrate_var).pack(side="left")
    ttk.Label(row, text="留空则自动探测常见旧位置",
              foreground="#646a73").pack(side="left", padx=8)

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
            r = install(pkg, dir_var.get().strip(),
                        migrate=bool(migrate_var.get()),
                        legacy_dir=legacy_var.get().strip(), log=log)
        except Exception as exc:                        # noqa: BLE001
            log("[X] " + f"{type(exc).__name__}: {exc}")
            messagebox.showerror("安装失败", f"{type(exc).__name__}: {exc}")
            return
        log("[OK] 安装完成。双击桌面快捷方式即可使用。")
        if r.get("migrated"):
            mig_line = "已把旧安装目录里的数据迁移过来。\n\n"
        elif r.get("migrate_note"):
            mig_line = r["migrate_note"] + "\n\n"
        else:
            mig_line = ""
        messagebox.showinfo(
            "安装完成",
            f"版本：v{r['version']}\n安装位置：{r['install_dir']}\n\n"
            f"程序文件 {r['files']} 个。\n"
            f"你的数据与配置{'未被触碰（原有数据全部保留）' if r['data_untouched'] else '⚠️ 有变动，请检查日志'}。\n\n"
            f"{mig_line}"
            f"数据与配置的位置：\n{user_home_dir()}")

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
    ap.add_argument("--legacy-dir", dest="legacy_dir", default="",
                    help="旧安装目录（含 data/config 的那个），装新目录时从这里迁移数据")
    ap.add_argument("--no-migrate", action="store_true", help="不迁移旧数据")
    ap.add_argument("--no-shortcuts", action="store_true",
                    help="不建桌面/开始菜单快捷方式（服务器或无人值守场景）")
    ap.add_argument("--purge-data", action="store_true", help="卸载时连用户数据一起删（危险）")
    args = ap.parse_args(argv)

    if args.uninstall:
        target = args.dir or default_install_dir()
        do_uninstall(target, keep_data=not args.purge_data, log=LOG)
        return 0
    if args.install and args.dir:
        r = install(args.install, args.dir,
                    shortcuts=not args.no_shortcuts,
                    migrate=not args.no_migrate,
                    legacy_dir=args.legacy_dir, log=LOG)
        print("[OK] 安装完成：", r)
        return 0
    return run_gui()


if __name__ == "__main__":
    sys.exit(main())
